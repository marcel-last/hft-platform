"""audit_logger — service configuration.

Configuration follows the platform pattern (CONVENTIONS §5): every namespace is a
frozen dataclass with sensible defaults, ``validate_config()`` returns a list of
human-readable error strings (empty = valid), and a module-level ``CONFIG``
singleton is shared by every other module.

Design notes for S13:

* The audit trail is the *source of truth* for tamper-evidence, so there is no
  "rebuild from upstream" mode — the in-memory chain is the chain.  All bounds
  below are memory-safety valves, not correctness features.
* ``ChainConfig.max_events`` evicts the *oldest* events once the bound is hit.
  Eviction severs the tail of the chain: the surviving oldest record becomes the
  new head (its ``prev_hash`` is reset to the empty-hash constant and its hash is
  recomputed along with every record behind it), so ``/verify-chain`` stays green
  for the remaining window and reports ``evicted=true`` with the surviving head
  id.  A production deployment that must retain full history would persist the
  chain to disk between evictions; that transport is out of scope here.
* ``QueryConfig.max_limit`` caps ``?limit=`` on every list-style endpoint so a
  single request can never force an unbounded scan or response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ServerConfig:
    """HTTP listener settings."""

    bind_host: str = "0.0.0.0"
    listen_port: int = 7730


@dataclass(frozen=True)
class ChainConfig:
    """Hash-chain storage bounds."""

    # Maximum number of events retained in memory.  When appending would exceed
    # this, the oldest events are evicted (see module docstring).
    max_events: int = 65536
    # Maximum depth of /verify-chain walks per request; the walk stops early and
    # reports "truncated" when the live chain is longer than this.
    max_verify_depth: int = 131072


@dataclass(frozen=True)
class QueryConfig:
    """Read-side (GET) bounds."""

    # Default and hard cap for ?limit= on GET /events and GET /export.
    default_limit: int = 100
    max_limit: int = 5000


@dataclass(frozen=True)
class DedupConfig:
    """Idempotent-append settings."""

    # Submitters that replay an already-stored (source, kind, dedup_key) triple
    # within this window get the *original* record back with status "duplicate"
    # instead of a new chain entry, so at-least-once delivery from S4/S5/S11
    # never double-counts an action.
    enabled: bool = True
    window_ns: int = 600_000_000_000  # 10 minutes
    max_keys: int = 16384


@dataclass(frozen=True)
class AuditConfig:
    """Top-level configuration for the audit-logger service (S13)."""

    name: str = "audit-logger"
    version: str = "1.0.0"
    env: str = "production"
    server: ServerConfig = field(default_factory=ServerConfig)
    chain: ChainConfig = field(default_factory=ChainConfig)
    query: QueryConfig = field(default_factory=QueryConfig)
    dedup: DedupConfig = field(default_factory=DedupConfig)


def validate_config(cfg: AuditConfig) -> List[str]:
    """Return a list of error strings; an empty list means the config is valid.

    Called once at boot by ``main.py``; the service exits(1) on any error.
    """
    errors: List[str] = []

    if not cfg.name:
        errors.append("name must be a non-empty string")
    if cfg.version.count(".") != 2 or not all(part.isdigit() for part in cfg.version.split(".")):
        errors.append(f"version must be a dotted numeric triple, got {cfg.version!r}")
    if not cfg.env:
        errors.append("env must be a non-empty string")

    if cfg.server.listen_port < 1 or cfg.server.listen_port > 65535:
        errors.append(f"server.listen_port must be in [1, 65535], got {cfg.server.listen_port}")
    if not cfg.server.bind_host:
        errors.append("server.bind_host must be a non-empty string")

    if cfg.chain.max_events < 1:
        errors.append(f"chain.max_events must be >= 1, got {cfg.chain.max_events}")
    if cfg.chain.max_verify_depth < 1:
        errors.append(
            f"chain.max_verify_depth must be >= 1, got {cfg.chain.max_verify_depth}"
        )

    if cfg.query.default_limit < 1:
        errors.append(f"query.default_limit must be >= 1, got {cfg.query.default_limit}")
    if cfg.query.max_limit < 1:
        errors.append(f"query.max_limit must be >= 1, got {cfg.query.max_limit}")
    if cfg.query.default_limit > cfg.query.max_limit:
        errors.append(
            f"query.default_limit ({cfg.query.default_limit}) must be <= "
            f"query.max_limit ({cfg.query.max_limit})"
        )

    if cfg.dedup.window_ns < 0:
        errors.append(f"dedup.window_ns must be >= 0, got {cfg.dedup.window_ns}")
    if cfg.dedup.max_keys < 1:
        errors.append(f"dedup.max_keys must be >= 1, got {cfg.dedup.max_keys}")

    return errors


CONFIG = AuditConfig()
