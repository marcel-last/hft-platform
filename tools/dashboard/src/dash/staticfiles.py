"""M3: static file serving for the dashboard UI (PLAN §4.1).

The dashboard tool serves its own dark-theme single-page UI from
``src/dash/static/`` — no build step, no bundler, no third-party assets.
Only three files ever ship: ``index.html``, ``dashboard.css`` and
``dashboard.js``. Serving is deliberately restrictive:

* a fixed content-type map (never ``application/octet-stream`` guesses);
* a whitelist of the three known files — anything else is a 404 with the
  standard UI-404 JSON envelope (PLAN §4.1);
* a path-traversal guard: the requested name is joined under the static
  root and the resolved path must remain inside it (``..`` segments,
  absolute paths and symlinks pointing outside are all rejected);
* ``no-cache`` on every response, matching the M1/M2 "always fresh"
  behaviour of the API layer.

This module is pure (no I/O beyond ``Path`` reads) and takes the static
root as a parameter, so unit tests can point it at a temporary directory
without touching the shipped assets.
"""

import json
from pathlib import Path
from typing import Dict, Optional, Tuple


#: The three files the UI ships (PLAN §4.1: "Only the two CSS/JS files +
#: index.html ship — no build step, no bundler").
KNOWN_FILES: Tuple[str, ...] = ("index.html", "dashboard.css", "dashboard.js")

#: Explicit content-type map for the shipped files (PLAN §4.1: "correct
#: types"). ``charset=utf-8`` is mandatory on the HTML (PLAN §4.1).
CONTENT_TYPES: Dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "dashboard.css": "text/css; charset=utf-8",
    "dashboard.js": "application/javascript; charset=utf-8",
}

#: Where the shipped assets live, relative to this module. ``main.py``
#: resolves this once at boot and hands the directory to the handler.
STATIC_DIR: Path = Path(__file__).resolve().parent / "static"

#: Cap on a single static asset read. The three shipped files are all well
#: under this; a guard against pathological reads on a tampered tree.
_MAX_BYTES = 1_048_576  # 1 MiB


class StaticFileError(Exception):
    """Raised when a static request cannot be served.

    ``status`` / ``code`` mirror the dashboard's error vocabulary: unknown
    or non-whitelisted files are UI-404 (404); traversal attempts are the
    same from the caller's point of view — we never disclose which part of
    the filesystem was probed, so both collapse into UI-404.
    """

    code = "UI-404"
    status = 404

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _not_found(path: str) -> StaticFileError:
    return StaticFileError("no static file at %s" % path)


def _untrusted(path: str) -> StaticFileError:
    # Deliberately the same code as not-found: a 400 would leak that the
    # path parsing happened; 404 keeps the surface minimal (PLAN §4.1).
    return StaticFileError("untrusted static path %s" % path)


def resolve_static_file(name: str,
                        static_root: Optional[Path] = None) -> Tuple[Path, str]:
    """Resolve ``name`` to ``(path, content_type)`` inside ``static_root``.

    Raises :class:`StaticFileError` (UI-404) when the name is not one of
    the three shipped files, when it is not a regular file, or when it
    would escape the static root.
    """
    root = Path(static_root) if static_root is not None else STATIC_DIR
    root = root.resolve()

    # Reject anything that is not a plain bare filename: no separators,
    # no NULs, no dot-segments. (The whitelist below is the second gate.)
    if not name or "\x00" in name or name in (".", ".."):
        raise _untrusted(name)
    if "/" in name or "\\" in name:
        # ``/static/dashboard.css`` arrives already split by the router;
        # a slash here means the caller passed a path, not a filename.
        raise _untrusted(name)

    if name not in CONTENT_TYPES:
        raise _not_found(name)

    candidate = (root / name).resolve()
    if candidate.parent != root:
        # ``resolve()`` collapses ``..`` — anything that no longer lives
        # directly under the root is a traversal attempt.
        raise _untrusted(name)
    if not candidate.is_file():
        raise _not_found(name)
    size = candidate.stat().st_size
    if size > _MAX_BYTES:
        raise StaticFileError("static file too large: %s" % name)

    return candidate, CONTENT_TYPES[name]


def read_static_file(name: str,
                     static_root: Optional[Path] = None) -> Tuple[bytes, str]:
    """Read a whitelisted static file; returns ``(body_bytes, content_type)``."""
    path, ctype = resolve_static_file(name, static_root)
    return path.read_bytes(), ctype


def ui404_envelope(path: str) -> Dict:
    """The JSON body served for unknown ``/static/*`` names (PLAN §4.1:
    "Unknown /static/* → 404 UI-404 JSON")."""
    return {
        "error": {
            "code": "UI-404",
            "message": "no static file at %s" % path,
            "service": "dashboard",
            "retryable": False,
            "context": {},
        }
    }


def json_bytes(doc: Dict) -> bytes:
    """Serialize an envelope dict (used by the handler bridge)."""
    return json.dumps(doc).encode("utf-8")
