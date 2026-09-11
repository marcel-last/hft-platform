"""M3: tests for the dark-theme UI serving (PLAN §4.1 / §4.4).

Two layers, both stdlib-only and network-free against the platform:

1. Unit tests for :mod:`dash.staticfiles` — the whitelist, the
   path-traversal guard, the content-type map and the UI-404 envelope —
   run against a temporary directory so the shipped assets are never
   mutated.

2. End-to-end HTTP tests that bind the real dashboard handler on an
   ephemeral port (same harness as ``test_handlers.py``) and exercise
   ``GET /`` and ``GET /static/*`` over the wire: content types,
   no-cache headers, the six panel ids in the page, and the 404/405
   behaviour for unknown and wrongly-methooded paths.
"""

import json
import threading
import urllib.error
import urllib.request

from dash import staticfiles
from dash.clients import AuthClient, DashStats, GatewayClient
from dash.config import AuthConfig, DashConfig, GatewayConfig
from dash.controller import build_router
from dash.main import serve


# ---------------------------------------------------------------------------
# Unit: dash.staticfiles
# ---------------------------------------------------------------------------

def _make_root(tmp_path):
    """A stand-in static root holding the three known files."""
    root = tmp_path / "static"
    root.mkdir()
    (root / "index.html").write_text("<html>M3</html>")
    (root / "dashboard.css").write_text("body{color:red}")
    (root / "dashboard.js").write_text("var x = 1;")
    return root


def test_resolve_whitelisted_files(tmp_path):
    root = _make_root(tmp_path)
    for name, ctype in [
        ("index.html", "text/html; charset=utf-8"),
        ("dashboard.css", "text/css; charset=utf-8"),
        ("dashboard.js", "application/javascript; charset=utf-8"),
    ]:
        path, got_ctype = staticfiles.resolve_static_file(name, root)
        assert path == (root / name)
        assert got_ctype == ctype


def test_read_static_file_returns_bytes_and_type(tmp_path):
    root = _make_root(tmp_path)
    body, ctype = staticfiles.read_static_file("dashboard.js", root)
    assert body == b"var x = 1;"
    assert ctype == "application/javascript; charset=utf-8"


def test_unknown_file_raises_ui404(tmp_path):
    root = _make_root(tmp_path)
    for name in ("nope.html", "index.html.bak", "dashboard.jsx", ""):
        try:
            staticfiles.resolve_static_file(name, root)
        except staticfiles.StaticFileError as e:
            assert e.code == "UI-404"
            assert e.status == 404
        else:
            raise AssertionError("expected StaticFileError for %r" % name)


def test_traversal_and_pathy_names_rejected(tmp_path):
    root = _make_root(tmp_path)
    # None of these may resolve, even though index.html exists in the
    # root: a subpath name or dot-segment is a path, not a filename.
    for name in ("../index.html", "..\\index.html", "a/b", "sub/index.html",
                 ".", "..", "/index.html"):
        try:
            staticfiles.resolve_static_file(name, root)
        except staticfiles.StaticFileError as e:
            assert e.status == 404
        else:
            raise AssertionError("expected rejection for %r" % name)


def test_symlink_escaping_root_rejected(tmp_path):
    root = _make_root(tmp_path)
    secret = tmp_path / "secret.css"
    secret.write_text("leak")
    # A symlink *inside* the root that points outside it must not be
    # served: resolve() collapses the link target, which then no longer
    # sits directly under the root.
    link = root / "dashboard.css"
    link.unlink()
    link.symlink_to(secret)
    try:
        staticfiles.resolve_static_file("dashboard.css", root)
    except staticfiles.StaticFileError as e:
        assert e.status == 404
    else:
        raise AssertionError("symlink escape must be rejected")


def test_ui404_envelope_shape():
    doc = staticfiles.ui404_envelope("/static/evil.css")
    err = doc["error"]
    assert err["code"] == "UI-404"
    assert err["service"] == "dashboard"
    assert err["retryable"] is False
    assert "evil.css" in err["message"]


def test_shipped_index_has_six_panel_ids():
    """PLAN §4.4: `GET /` must return HTML containing the six panel ids."""
    html = staticfiles.read_static_file("index.html")[0].decode("utf-8")
    for pid in ("panel-health", "panel-portfolio", "panel-settlement",
                "panel-live", "panel-alerts", "panel-gateway"):
        assert 'id="%s"' % pid in html, "missing panel id %s" % pid


def test_shipped_js_uses_no_innerhtml_assignments():
    """PLAN §4.3: no innerHTML with server data."""
    js = staticfiles.read_static_file("dashboard.js")[0].decode("utf-8")
    assert ".innerHTML" not in js


# ---------------------------------------------------------------------------
# HTTP end-to-end (ephemeral port, fake auth/gateway transports)
# ---------------------------------------------------------------------------

def _start(read_only=False):
    cfg = DashConfig(bind="127.0.0.1", port=0, read_only=read_only,
                     auth=AuthConfig(), gateway=GatewayConfig())
    stats = DashStats()

    def fake_auth(method, url, body, headers, timeout_s):
        return 200, json.dumps(
            {"token": "t", "jti": "j", "sub": "dashboard",
             "scope": ["read", "write"], "iat": 1, "nbf": 1,
             "exp": 1}).encode()

    def fake_gw(method, url, body, headers, timeout_s):
        return 404, b'{"error":{"code":"API-404","message":"n/a",' \
                    b'"service":"api-gateway","retryable":false,"context":{}}}'

    auth = AuthClient(cfg.auth, transport=fake_auth, stats=stats)
    gw = GatewayClient(cfg.gateway, auth, transport=fake_gw)
    httpd = serve(cfg, build_router(cfg, auth, gw, stats), auth, gw, stats)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def _raw(port, method, path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_root_serves_index_html():
    httpd = _start()
    try:
        port = httpd.server_address[1]
        status, headers, body = _raw(port, "GET", "/")
        assert status == 200
        assert headers["Content-Type"] == "text/html; charset=utf-8"
        assert headers["Cache-Control"] == "no-cache"
        html = body.decode("utf-8")
        for pid in ("panel-health", "panel-portfolio", "panel-settlement",
                    "panel-live", "panel-alerts", "panel-gateway"):
            assert 'id="%s"' % pid in html
        # The page references the two companion assets.
        assert '"/static/dashboard.css"' in html
        assert '"/static/dashboard.js"' in html
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_static_css_and_js_content_types():
    httpd = _start()
    try:
        port = httpd.server_address[1]
        status, headers, body = _raw(port, "GET", "/static/dashboard.css")
        assert status == 200
        assert headers["Content-Type"] == "text/css; charset=utf-8"
        assert headers["Cache-Control"] == "no-cache"
        assert b"--bg" in body  # the dark-theme variables

        status, headers, body = _raw(port, "GET", "/static/dashboard.js")
        assert status == 200
        assert headers["Content-Type"] == "application/javascript; charset=utf-8"
        assert b"EventSource" in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_unknown_static_is_ui404_json():
    httpd = _start()
    try:
        port = httpd.server_address[1]
        for path in ("/static/evil.css", "/static/../main.py",
                     "/static/inner/index.html", "/static"):
            status, headers, body = _raw(port, "GET", path)
            assert status == 404, path
            assert headers["Content-Type"] == "application/json"
            doc = json.loads(body.decode("utf-8"))
            assert doc["error"]["code"] == "UI-404"
            assert doc["error"]["service"] == "dashboard"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_non_get_on_ui_paths():
    httpd = _start()
    try:
        port = httpd.server_address[1]
        # POST / — the UI path exists but only for GET.
        status, _, body = _raw(port, "POST", "/")
        assert status in (404, 405)
        doc = json.loads(body.decode("utf-8"))
        assert doc["error"]["code"] in ("UI-404", "UI-405")
        # POST /static/dashboard.css — same, and the file is NOT served.
        status, _, body = _raw(port, "POST", "/static/dashboard.css")
        assert status in (404, 405)
        assert b"--bg" not in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_root_not_served_as_static_name():
    """`/index.html` is not a registered route (only `/` serves it)."""
    httpd = _start()
    try:
        port = httpd.server_address[1]
        status, _, body = _raw(port, "GET", "/index.html")
        assert status == 404
        doc = json.loads(body.decode("utf-8"))
        assert doc["error"]["code"] == "UI-404"
    finally:
        httpd.shutdown()
        httpd.server_close()
