"""Outbound HTTP for the dashboard: auth-service token client + gateway proxy.

Both clients use ``urllib`` (stdlib only, CONVENTIONS §3/§11) with explicit
timeouts, and both accept an injectable *transport* for unit tests:

    transport(method: str, url: str, body: Optional[bytes],
              headers: Dict[str, str], timeout_s: float) -> (int, bytes)

The transport must raise :class:`TransportFailure` for connect/read problems
(never for non-2xx status lines — those are returned as ``(status, body)``).
Upstream response bodies pass through **verbatim**: the dashboard never
re-writes another service's §1.2 envelope.
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from dash.config import AuthConfig, GatewayConfig
from dash.errors import (
    AuthUnreachableError,
    GatewayTransportError,
    InternalError,
    TokenRefreshFailedError,
)


class TransportFailure(Exception):
    """Connect/read level failure talking to a peer (no HTTP status received)."""

    def __init__(self, peer: str, detail: str) -> None:
        super().__init__("%s: %s" % (peer, detail))
        self.peer = peer
        self.detail = detail


Transport = Callable[[str, str, Optional[bytes], Dict[str, str], float],
                     Tuple[int, bytes]]


def _default_transport(method: str, url: str, body: Optional[bytes],
                       headers: Dict[str, str],
                       timeout_s: float) -> Tuple[int, bytes]:
    """Real transport on top of ``urllib`` (production path)."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # Non-2xx is a *response*, not a transport failure: return it so the
        # caller can pass the upstream envelope through verbatim.
        return e.code, e.read()
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise TransportFailure(url, str(reason)) from e


class DashStats:
    """Thread-local-free dashboard counters shown in ``/api/gateway-stats``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.tokens_issued = 0
        self.token_refreshes = 0  # reactive (401-driven) re-fetches
        self.last_token_ok_ns: Optional[int] = None
        self.last_token_ok_jti: Optional[str] = None

    def bump_issued(self, jti: str, now_ns: int) -> None:
        with self._lock:
            self.tokens_issued += 1
            self.last_token_ok_ns = now_ns
            self.last_token_ok_jti = jti

    def bump_refresh(self) -> None:
        with self._lock:
            self.token_refreshes += 1

    def snapshot(self, now_ns: int) -> Dict[str, Any]:
        with self._lock:
            return {
                "tokens_issued": self.tokens_issued,
                "token_refreshes": self.token_refreshes,
                "last_token_ok_ns": self.last_token_ok_ns,
                "last_token_ok_jti": self.last_token_ok_jti,
                "now_ns": now_ns,
            }


class AuthClient:
    """Mint and cache bearer tokens from S12 auth-service.

    Token body sent: ``{"sub", "scopes", "ttl_ns"}`` (PLAN §1.3). The 1 h TTL
    is int64 ns. Proactive refresh happens inside :meth:`get_token` when less
    than ``refresh_watermark_frac`` of the original TTL remains; reactive
    refresh is triggered by :meth:`reactive_refresh` (gateway 401).
    """

    def __init__(self, cfg: AuthConfig,
                 transport: Optional[Transport] = None,
                 stats: Optional[DashStats] = None,
                 now_ns: Optional[Callable[[], int]] = None) -> None:
        self.cfg = cfg
        self.transport = transport or _default_transport
        self.stats = stats if stats is not None else DashStats()
        self._now_ns = now_ns or (lambda: time.time_ns())
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._jti: Optional[str] = None
        self._issued_ns: Optional[int] = None
        self._ttl_ns: Optional[int] = None

    # -- token lifecycle -------------------------------------------------

    def _token_url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/token"

    def _fetch_token(self) -> Tuple[str, str, int]:
        """POST /token; returns (token, jti, ttl_ns). Raises AuthUnreachableError."""
        body = {
            "sub": self.cfg.sub,
            "scopes": list(self.cfg.scopes),
            "ttl_ns": self.cfg.token_ttl_ns,
        }
        try:
            status, raw = self.transport(
                "POST", self._token_url(),
                json.dumps(body).encode("utf-8"),
                {}, self.cfg.read_timeout_ms / 1000.0,
            )
        except TransportFailure as e:
            raise AuthUnreachableError(
                "auth-service unreachable: %s" % e.detail,
                context={"peer": e.peer},
            ) from e
        if status != 200:
            raise AuthUnreachableError(
                "auth-service answered %d on POST /token" % status,
                context={"status": status},
            )
        try:
            doc = json.loads(raw.decode("utf-8"))
            token = doc["token"]
            jti = doc.get("jti", "")
        except (ValueError, KeyError, UnicodeDecodeError) as e:
            raise AuthUnreachableError(
                "auth-service token response was malformed: %s" % e,
                context={"detail": str(e)},
            ) from e
        return str(token), str(jti), self.cfg.token_ttl_ns

    def get_token(self) -> str:
        """Return a valid token, proactively refreshing when near expiry."""
        with self._lock:
            now = self._now_ns()
            if self._token is not None and self._issued_ns is not None:
                remaining = (self._issued_ns + self._ttl_ns) - now  # type: ignore[operator]
                if remaining > self.cfg.refresh_watermark_frac * self._ttl_ns:
                    return self._token
            # (Re)fetch outside the lock? No: single-flight is fine at this
            # scale; holding the lock serializes concurrent refreshes.
            token, jti, ttl = self._fetch_token()
            self._token, self._jti, self._issued_ns, self._ttl_ns = (
                token, jti, self._now_ns(), ttl)
            self.stats.bump_issued(self._jti, self._now_ns())
            return token

    def reactive_refresh(self) -> str:
        """Drop the cache and force a fresh token (called after a 401)."""
        with self._lock:
            self.stats.bump_refresh()
            self._token = None
            self._jti = None
            self._issued_ns = None
            token, jti, ttl = self._fetch_token()
            self._token, self._jti, self._issued_ns, self._ttl_ns = (
                token, jti, self._now_ns(), ttl)
            self.stats.bump_issued(self._jti, self._now_ns())
            return token

    def current_jti(self) -> Optional[str]:
        with self._lock:
            return self._jti

    def ready(self) -> Tuple[bool, str]:
        """Readiness for ``/readyz``: ready once a token was ever acquired."""
        with self._lock:
            if self._token is not None:
                return True, "token active"
            return False, "token-not-acquired"


class GatewayClient:
    """Reverse proxy toward S15 api-gateway with 401 auto-refresh.

    :meth:`proxy` returns the upstream ``(status, body)`` verbatim for every
    outcome, including upstream 401/403/404/429/5xx. Transport failures raise
    :class:`GatewayTransportError`; a failed *re-fetch* after a 401 raises
    :class:`TokenRefreshFailedError`.
    """

    def __init__(self, cfg: GatewayConfig, auth: AuthClient,
                 transport: Optional[Transport] = None) -> None:
        self.cfg = cfg
        self.auth = auth
        self.transport = transport or _default_transport

    def url_for(self, path: str, query: str = "") -> str:
        base = self.cfg.base_url.rstrip("/")
        url = base + path
        if query:
            url += "?" + query
        return url

    def proxy(self, method: str, path: str, query: str = "",
              body: Optional[bytes] = None) -> Tuple[int, bytes]:
        """Forward one request to the gateway; exactly one 401 retry."""
        headers = {
            "Authorization": "Bearer " + self.auth.get_token(),
            "Content-Length": str(len(body or b"")),
        }
        url = self.url_for(path, query)
        try:
            status, raw = self.transport(
                method, url, body, headers,
                self.cfg.read_timeout_ms / 1000.0,
            )
        except TransportFailure as e:
            raise GatewayTransportError(
                "api-gateway unreachable: %s" % e.detail,
                context={"peer": e.peer},
            ) from e

        if status == 401:
            # Token rejected (expired/revoked). Re-fetch ONCE and retry ONCE;
            # a second 401 is passed through verbatim.
            try:
                new_token = self.auth.reactive_refresh()
            except AuthUnreachableError as e:
                raise TokenRefreshFailedError(
                    "token re-fetch failed after gateway 401: %s" % e.message,
                    context=e.context,
                ) from e
            headers = {
                "Authorization": "Bearer " + new_token,
                "Content-Length": str(len(body or b"")),
            }
            try:
                status, raw = self.transport(
                    method, url, body, headers,
                    self.cfg.read_timeout_ms / 1000.0,
                )
            except TransportFailure as e:
                raise GatewayTransportError(
                    "api-gateway unreachable after token refresh: %s" % e.detail,
                    context={"peer": e.peer},
                ) from e
        return status, raw

    def get_json(self, path: str, query: str = "") -> Tuple[int, Dict[str, Any]]:
        """GET convenience returning the parsed body (dict when JSON)."""
        status, raw = self.proxy("GET", path, query)
        try:
            doc: Any = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            doc = {"raw": raw.decode("utf-8", "replace")}
        return status, doc
