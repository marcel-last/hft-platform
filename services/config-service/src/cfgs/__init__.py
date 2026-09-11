"""config_service — S11 of the HFT platform.

Centralized configuration store and distribution point.  Serves versioned
config blobs to every other service at boot and on change (long-poll), manages
environment overrides (deep-merged over each blob at read time) and platform
feature flags, and maintains a bounded, sequenced change log.
"""

from .config import CONFIG, ServiceConfig, validate_config
from .models import (
    ChangeEvent,
    ConfigBlob,
    FlagState,
    ServiceInfo,
    content_hash,
    deep_merge,
    now_ns,
)
from .config_store import ConfigStore
from .errors import CFGError

__all__ = [
    "CONFIG",
    "ServiceConfig",
    "validate_config",
    "ChangeEvent",
    "ConfigBlob",
    "FlagState",
    "ServiceInfo",
    "content_hash",
    "deep_merge",
    "now_ns",
    "ConfigStore",
    "CFGError",
]
