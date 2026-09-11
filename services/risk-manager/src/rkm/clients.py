"""risk_manager — upstream/downstream HTTP clients.

Talks to four neighbours using only the stdlib (``urllib``):

* :class:`BookBuilderClient`  — S2 order-book-builder: pull top-of-book views
  for reference prices and book health used in exposure math.
* :class:`ExecutionClient`    — S4 execution-gateway: read open orders, poll
  fills to update positions, and issue flatten (cancel-all) on kill-switch.
* :class:`AlertingClient`     — S10 alerting-service: escalate HARD breaches.
* :class:`AuditClient`        — S13 audit-logger: record vetoes and
  kill-switch activations in the tamper-evident trail.

All four share the tiny blocking :class:`_HTTP` helper so the service has zero
third-party dependencies.  Timeouts come from :class:`~rkm.config`.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .config import CONFIG
from .errors import ExecutionGatewayError, UpstreamUnreachableError
from .models import OrderRecord, RiskSide

logger = logging.getLogger("rkm.clients")


class _HTTP:
    """Minimal blocking JSON HTTP helper (stdlib only)."""

    def __init__(self, base_url: str, timeout_ms: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_ms / 1000.0

    def _request(self, method: str, path: str, body: Optional[dict] = None,
                 target: str = "upstream") -> Dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = {"error": {"message": payload[:200].decode("utf-8", "replace")}}
            if target == "execution-gateway":
                raise ExecutionGatewayError(
                    f"execution gateway returned {exc.code} for {method} {path}",
                    context={"status": exc.code, "body": parsed},
                ) from exc
            raise UpstreamUnreachableError(
                target, f"{exc.code} on {method} {path}",
                context={"status": exc.code, "body": parsed},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if target == "execution-gateway":
                raise ExecutionGatewayError(
                    f"cannot reach execution gateway at {url}: {exc}", context={"url": url},
                ) from exc
            raise UpstreamUnreachableError(target, str(exc), context={"url": url}) from exc

    def get(self, path: str, target: str = "upstream") -> Dict[str, Any]:
        return self._request("GET", path, target=target)

    def post(self, path: str, body: Optional[dict] = None,
             target: str = "upstream") -> Dict[str, Any]:
        return self._request("POST", path, body, target=target)


class BookBuilderClient:
    """Client for the order-book-builder (S2)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.book_builder_url, CONFIG.ingest.request_timeout_ms)
        self.target = "order-book-builder"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def top_of_book(self, symbol: str) -> List[Dict[str, Any]]:
        """Fetch the current top-of-book for one symbol across all venues."""
        resp = self.http.get(f"/tob/{symbol}", target=self.target)
        return resp.get("tob", [])

    def books(self) -> List[Dict[str, Any]]:
        """List of all maintained books with health + ToB."""
        resp = self.http.get("/books", target=self.target)
        return resp.get("books", [])


class ExecutionClient:
    """Client for the execution-gateway (S4)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.ingest.execution_gateway_url,
                          CONFIG.ingest.request_timeout_ms)
        self.target = "execution-gateway"

    def health(self) -> Dict[str, Any]:
        return self.http.get("/healthz", target=self.target)

    def open_orders(self) -> List[OrderRecord]:
        """Snapshot of all orders (open ones are filtered by the caller/engine)."""
        resp = self.http.get("/orders", target=self.target)
        records: List[OrderRecord] = []
        for row in resp.get("orders", []):
            try:
                side = RiskSide(str(row.get("side", "BUY")).upper())
            except ValueError:
                continue
            records.append(OrderRecord(
                id=str(row.get("id", "")),
                canonical_symbol=str(row.get("symbol", "")),
                side=side,
                qty=int(row.get("qty", 0)),
                limit_price=float(row.get("limit_px", row.get("limit_price", 0.0))),
                state=str(row.get("state", "NEW")).upper(),
                updated_ns=int(row.get("updated_ns", 0)),
            ))
        return records

    def fills(self, limit: int = 512) -> List[Dict[str, Any]]:
        resp = self.http.get(f"/fills?limit={limit}", target=self.target)
        return resp.get("fills", [])

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel one open order (used by the flatten path)."""
        url = f"/orders/{order_id}"
        req = urllib.request.Request(self.http.base_url + url, method="DELETE")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.http.timeout_s) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                parsed = {"error": {"message": payload[:200].decode("utf-8", "replace")}}
            raise ExecutionGatewayError(
                f"execution gateway returned {exc.code} for DELETE {url}",
                context={"status": exc.code, "body": parsed},
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ExecutionGatewayError(
                f"cannot reach execution gateway at {self.http.base_url + url}: {exc}",
                context={"url": self.http.base_url + url},
            ) from exc

    def flatten(self, order_ids: List[str]) -> Dict[str, Any]:
        """Cancel a set of open orders; returns per-order outcomes."""
        results: Dict[str, Any] = {"cancelled": 0, "failed": 0, "errors": {}}
        for oid in order_ids:
            try:
                self.cancel_order(oid)
                results["cancelled"] += 1
            except ExecutionGatewayError as exc:
                results["failed"] += 1
                results["errors"][oid] = exc.message
        return results


class AlertingClient:
    """Client for the alerting-service (S10)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.kill_switch.escalation_url,
                          CONFIG.kill_switch.escalate_timeout_ms)
        self.target = "alerting-service"

    def send_alert(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        return self.http.post("/alerts", body=alert, target=self.target)


class AuditClient:
    """Client for the audit-logger (S13)."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.http = _HTTP(base_url or CONFIG.kill_switch.audit_url,
                          CONFIG.kill_switch.escalate_timeout_ms)
        self.target = "audit-logger"

    def record_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        return self.http.post("/events", body=event, target=self.target)
