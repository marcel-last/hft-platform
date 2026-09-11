"""audit_logger (audl) — S13 immutable, hash-chained audit trail.

Public API re-exports (CONVENTIONS §6):

* :class:`~audl.config.AuditConfig` / :func:`~audl.config.validate_config`
* :class:`~audl.errors.AUDError` / :func:`~audl.errors.error_envelope`
* :class:`~audl.models.EventRecord` and the hashing/clock helpers
* :class:`~audl.audit_engine.AuditEngine`
* :func:`~audl.router.build_router`
* :class:`~audl.controller.AuditController`
"""

from __future__ import annotations

from .audit_engine import AppendOutcome, AuditEngine, VerifyReport
from .config import AuditConfig, CONFIG, validate_config
from .controller import AuditController
from .errors import (
    AUDError,
    AUDNoRouteError,
    InvalidEventError,
    UnknownEventError,
    error_envelope,
)
from .models import (
    EMPTY_HASH,
    Clock,
    EventRecord,
    ManualClock,
    SystemClock,
    content_hash,
    canonical_json,
    format_event_id,
    parse_event_id,
)
from .router import Router, build_router

__all__ = [
    "AppendOutcome",
    "AuditConfig",
    "AuditController",
    "AuditEngine",
    "AUDError",
    "AUDNoRouteError",
    "CONFIG",
    "Clock",
    "EMPTY_HASH",
    "EventRecord",
    "InvalidEventError",
    "ManualClock",
    "Router",
    "SystemClock",
    "UnknownEventError",
    "VerifyReport",
    "build_router",
    "canonical_json",
    "content_hash",
    "error_envelope",
    "format_event_id",
    "parse_event_id",
    "validate_config",
]
