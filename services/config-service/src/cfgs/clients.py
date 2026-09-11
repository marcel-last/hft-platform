"""config_service — downstream HTTP clients.

S11 is primarily a *provider* of configuration, but it also performs two
best-effort, fire-and-forget fan-outs on every mutation so operators can react
and the change is recorded in the immutable audit trail:

* :class:`AlertingClient` — S10 alerting-service: POST a WARNING-level alert for
  significant config changes (a new service's first config, or any override /
  flag flip).  Never blocks or fails the mutation path.
* :class:`AuditClient` — S13 audit-logger: append an immutable event describing
  the config change (service, revision, actor, kind).

Both use only the stdlib (``urllib``) with short timeouts and swallow every
exception — a downstream outage must never prevent a legitimate config update.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("cfgs.clients")


class _HTTP:
    """Minimal blocking JSON HTTP helper (stdlib only)."""

    def __init__(self, base_url: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_ms / 1000.0

    def post(self, path: str, body: Optional[dict] = None) -> Dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else b"{}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}


class AlertingClient:
    """Best-effort client for the alerting-service (S10)."""

    def __init__(self, base_url: str = "http://alerting-service:7700",
                 timeout_ms: int = 300) -> None:
        self.http = _HTTP(base_url, timeout_ms)
        self.target = "alerting-service"

    def fire(self, *, category: str, severity: str, message: str,
             context: Optional[Dict[str, Any]] = None) -> bool:
        """POST an alert; returns True on success, False on any failure (never raises)."""
        body = {
            "source": "config-service",
            "category": category,
            "severity": severity,
            "message": message,
            "context": context or {},
        }
        try:
            self.http.post("/alerts", body)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                ValueError) as exc:  # noqa: BLE001 - best-effort by design
            logger.debug("S10 alert fan-out failed (%s): %s", self.target, exc)
            return False


class AuditClient:
    """Best-effort client for the audit-logger (S13)."""

    def __init__(self, base_url: str = "http://audit-logger:7730",
                 timeout_ms: int = 300) -> None:
        self.http = _HTTP(base_url, timeout_ms)
        self.target = "audit-logger"

    def record(self, *, action: str, actor: str, detail: Optional[Dict[str, Any]] = None) -> bool:
        """Append an audit event; returns True on success, False on any failure."""
        body = {
            "action": action,
            "actor": actor or "config-service",
            "source_service": "config-service",
            "detail": detail or {},
        }
        try:
            self.http.post("/events", body)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
                ValueError) as exc:  # noqa: BLE001 - best-effort by design
            logger.debug("S13 audit fan-out failed (%s): %s", self.target, exc)
            return False
