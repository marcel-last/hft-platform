"""Minimal regex-based router (CONVENTIONS §3).

Supports ``{param}`` path segments:

    router.add("GET", r"/api/portfolio/pnl/(?P<symbol>[A-Z0-9_&]+)", handler)

Resolution semantics (matching the platform's other Python routers and the
Rust template router):

* exact (method, path) match  -> ``Resolution.found(name, params)``
* path matches but method differs -> ``Resolution.method_not_allowed(allowed)``
* nothing matches -> ``Resolution.not_found()``
"""

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


ParamPattern = re.compile(r"\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")


def _compile(pattern: str) -> re.Pattern:
    """Translate a ``/path/{param}/x`` pattern into a full-match regex.

    ``{name}`` becomes the named group ``(?P<name>[^/]+)``; the resulting
    pattern is anchored at both ends so ``/a/b`` never matches ``/a/b/c``.
    """
    def _sub(m: "re.Match") -> str:
        return "(?P<%s>[^/]+)" % m.group("name")

    translated = ParamPattern.sub(_sub, pattern)
    return re.compile("^" + translated + "$")


@dataclass(frozen=True)
class _Route:
    method: str
    pattern: str
    regex: re.Pattern
    name: str
    handler: Callable[..., "tuple"]


@dataclass(frozen=True)
class Found:
    name: str
    params: Dict[str, str] = field(default_factory=dict)
    handler: Optional[Callable[..., "tuple"]] = None


@dataclass(frozen=True)
class MethodNotAllowed:
    allowed: Tuple[str, ...] = ()


@dataclass(frozen=True)
class NotFound:
    pass


Resolution = Any  # Found | MethodNotAllowed | NotFound


class Router:
    """Method + path router with ``{param}`` segments."""

    def __init__(self) -> None:
        self._routes: List[_Route] = []
        self._path_methods: Dict[str, set] = {}  # raw pattern -> methods seen

    def add(self, method: str, pattern: str, name_or_handler: Any) -> "Router":
        """Register a route. Accepts a handler callable or a name string."""
        method = method.upper()
        name = name_or_handler
        handler = None
        if callable(name_or_handler) and not isinstance(name_or_handler, str):
            handler = name_or_handler
            name = getattr(name_or_handler, "__name__", "handler")
        self._routes.append(
            _Route(method, pattern, _compile(pattern), str(name), handler)
        )
        self._path_methods.setdefault(pattern, set()).add(method)
        return self

    def resolve(self, method: str, path: str) -> Resolution:
        """Resolve ``method path`` to a :class:`Resolution`."""
        method = method.upper()
        path_only = path.split("?", 1)[0].rstrip("/") or "/"
        allowed_here: List[str] = []
        for route in self._routes:
            m = route.regex.match(path_only)
            if not m:
                continue
            if route.method == method:
                params = {k: v for k, v in m.groupdict().items() if v is not None}
                return Found(name=route.name, params=params, handler=route.handler)
            allowed_here.append(route.method)
        if allowed_here:
            unique = tuple(sorted(set(allowed_here)))
            return MethodNotAllowed(allowed=unique)
        return NotFound()

    def route_names(self) -> List[str]:
        """Registered route names, in registration order (diagnostics)."""
        return [r.name for r in self._routes]
