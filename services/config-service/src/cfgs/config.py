"""config_service — service-level configuration.

The config service (S11) is the **centralized configuration store and
distribution point** for every other service on the platform.  It owns:

* a versioned per-service configuration store (each service has exactly one
  live config blob, plus a bounded revision history);
* environment overrides that are merged over each blob at read time;
* platform-wide feature flags with typed values and change tracking;
* long-poll change notification so consumers can pick up updates in near-real
  time without busy polling.

Every other service (S1..S10, S12..S15) is a downstream consumer: at boot it
fetches its own blob via ``GET /config/{service}`` and then holds an open
``GET /changes?since=...&timeout_ms=...`` long-poll to learn about updates.

Configuration is split into four namespaces:

    :class:`StoreConfig`     — per-service revision-history retention + change-log bound
    :class:`OverrideConfig`  — environment-override merge behaviour
    :class:`FlagConfig`      — feature-flag retention bounds
    :class:`PollConfig`      — long-poll defaults (max wait, poll granularity)

All durations use the ``_ms`` suffix and timestamps on hot paths are int64
nanoseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class StoreConfig:
    """Per-service configuration store behaviour."""

    max_revisions_per_service: int = 50   # bounded revision history kept per service
    max_change_events: int = 1_000        # bounded platform-wide change log
    max_services: int = 256               # safety cap on distinct services tracked


@dataclass(frozen=True)
class OverrideConfig:
    """Environment-override merge behaviour.

    An override is a partial dict applied *on top of* the live blob for a given
    service and environment.  ``deep_merge`` controls whether nested objects are
    merged recursively (True) or replaced wholesale at the top level (False).
    """

    deep_merge: bool = True               # recursive merge of nested dicts
    default_env: str = "production"       # env used when a request omits one


@dataclass(frozen=True)
class FlagConfig:
    """Feature-flag retention bounds."""

    max_flags: int = 512                  # safety cap on distinct flags
    max_flag_history: int = 32            # bounded per-flag change history


@dataclass(frozen=True)
class PollConfig:
    """Long-poll change-notification behaviour."""

    default_timeout_ms: int = 15_000      # default wait when a caller omits timeout_ms
    max_timeout_ms: int = 60_000          # hard ceiling on any single long-poll
    poll_granularity_ms: int = 50         # wake-up granularity of the waiter loop


@dataclass(frozen=True)
class ServiceConfig:
    name: str = "config-service"
    version: str = "1.0.0"
    env: str = "production"
    listen_port: int = 7710
    store: StoreConfig = field(default_factory=StoreConfig)
    overrides: OverrideConfig = field(default_factory=OverrideConfig)
    flags: FlagConfig = field(default_factory=FlagConfig)
    poll: PollConfig = field(default_factory=PollConfig)


def validate_config(cfg: ServiceConfig) -> List[str]:
    """Validate the full configuration tree; returns error strings (empty = valid)."""
    errors: List[str] = []
    if not (1 <= cfg.listen_port <= 65535):
        errors.append(f"listen_port out of range: {cfg.listen_port}")

    st = cfg.store
    if st.max_revisions_per_service < 1:
        errors.append("store.max_revisions_per_service must be >= 1")
    if st.max_change_events < 1:
        errors.append("store.max_change_events must be >= 1")
    if st.max_services < 1:
        errors.append("store.max_services must be >= 1")

    ov = cfg.overrides
    if not ov.default_env:
        errors.append("overrides.default_env must be a non-empty string")

    fl = cfg.flags
    if fl.max_flags < 1:
        errors.append("flags.max_flags must be >= 1")
    if fl.max_flag_history < 1:
        errors.append("flags.max_flag_history must be >= 1")

    pl = cfg.poll
    if pl.default_timeout_ms < 1:
        errors.append("poll.default_timeout_ms must be >= 1")
    if pl.max_timeout_ms < pl.default_timeout_ms:
        errors.append("poll.max_timeout_ms must be >= poll.default_timeout_ms")
    if pl.poll_granularity_ms < 1:
        errors.append("poll.poll_granularity_ms must be >= 1")

    return errors


# Singleton used by every module in the service.
CONFIG = ServiceConfig()
