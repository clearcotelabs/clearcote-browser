import json
import os
import urllib.request

import pytest

from clearcote import Server, serve
from clearcote._serve import _parse_proxy


def test_parse_proxy():
    d = _parse_proxy("http://user:pass@host:8080")
    assert d == {"server": "http://host:8080", "username": "user", "password": "pass"}
    d2 = _parse_proxy("host:3128")   # bare host:port -> http, no creds
    assert d2 == {"server": "http://host:3128"}


def test_server_handle_urls():
    class FakeProc:
        def poll(self):
            return None
    s = Server(FakeProc(), "127.0.0.1", 9222, "/tmp/x", own_udd=False)
    assert s.cdp_url == "http://127.0.0.1:9222"
    assert s.is_alive() is True


@pytest.mark.skipif(not os.environ.get("CLEARCOTE_TEST_BINARY"),
                    reason="set CLEARCOTE_TEST_BINARY to run the serve smoke test against a real binary")
def test_serve_smoke_stealthy():
    srv = serve(executable_path=os.environ["CLEARCOTE_TEST_BINARY"], headless=True, quiet=True,
                fingerprint="t", platform="windows",
                args=["--no-sandbox", "--use-gl=angle", "--use-angle=swiftshader",
                      "--enable-unsafe-swiftshader"])
    try:
        ver = json.load(urllib.request.urlopen(srv.cdp_url + "/json/version", timeout=8))
        assert "Chrome" in ver.get("Browser", "")
        assert ver.get("webSocketDebuggerUrl")   # attachable endpoint exists
    finally:
        srv.close()
