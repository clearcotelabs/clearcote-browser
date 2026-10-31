"""docker/webrtc_ip.py: the WebRTC IP the Docker image's browser reports when CC_WEBRTC_IP is unset.

Measured in the image (bridge and host networking, with and without a proxy): with no WebRTC IP the engine gathers
no ICE candidates at all, which a regular Chrome never does. So the entrypoint fills an unset value with the egress
IP, as the SDKs' geoip does for a launch of their own: the proxy's exit when CC_PROXY is set, else the container's
own public address, through the SDK's own lookup. Loaded from the repository's docker/ directory; skipped when the
tree does not have it."""
import importlib.util
import os
import socketserver
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKER = os.path.normpath(os.path.join(HERE, "..", "..", "..", "docker"))
PATH = os.path.join(DOCKER, "webrtc_ip.py")
pytestmark = pytest.mark.skipif(not os.path.exists(PATH), reason="no docker/webrtc_ip.py in this tree")
PROXY = "http://user:pa%3Ass@127.0.0.1:1"


@pytest.fixture(scope="module")
def wip():
    spec = importlib.util.spec_from_file_location("webrtc_ip", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- what to do
@pytest.mark.parametrize("proxy", [None, PROXY])
def test_unset_is_looked_up_on_the_licensed_engine(wip, proxy):
    assert wip.plan(None, proxy, True) == ("lookup", None)


@pytest.mark.parametrize("proxy, why", [(PROXY, "UDP go around the proxy"), (None, "set CC_WEBRTC_IP")])
def test_the_open_engine_is_left_unset(wip, proxy, why):
    action, reason = wip.plan(None, proxy, False)
    assert action == "off" and "open engine" in reason and why in reason
    calls = []
    assert wip.resolve(None, proxy, False, lookup=lambda *a: calls.append(a))[:2] == (None, "off") and calls == []


@pytest.mark.parametrize("raw", ["", "  ", "off", "OFF", "0", "false", "no", "none"])
def test_off_or_an_explicit_empty_value_keeps_todays_behaviour(wip, raw):
    assert wip.plan(raw, PROXY, True)[0] == "off"
    calls = []
    ip, source, line = wip.resolve(raw, PROXY, True, lookup=lambda *a: calls.append(a) or "203.0.113.7")
    assert ip is None and source == "off" and calls == []  # no lookup at all
    assert line.startswith("[clearcote] WebRTC IP: none")


@pytest.mark.parametrize("proxy, licensed", [(None, True), (PROXY, True), (PROXY, False)])
def test_an_explicit_address_is_used_as_given_without_a_lookup(wip, proxy, licensed):
    calls = []
    ip, source, line = wip.resolve(" 198.51.100.9 ", proxy, licensed, lookup=lambda *a: calls.append(a))
    assert (ip, source, calls) == ("198.51.100.9", "env", [])
    assert line == "[clearcote] WebRTC IP: 198.51.100.9 (CC_WEBRTC_IP)"


def test_unset_with_a_proxy_is_the_proxys_exit(wip):
    calls = []
    ip, source, line = wip.resolve(None, PROXY, True, lookup=lambda p, t: calls.append((p, t)) or "203.0.113.7",
                                   environ={})
    assert (ip, source) == ("203.0.113.7", "proxy exit")
    assert calls == [(PROXY, wip.DEFAULT_TIMEOUT_S)]  # through the proxy, login included
    assert line.startswith("[clearcote] WebRTC IP: 203.0.113.7 (the proxy's exit")


def test_unset_without_a_proxy_is_the_containers_own_address(wip):
    calls = []
    ip, source, line = wip.resolve(None, None, True, lookup=lambda p, t: calls.append(p) or "198.51.100.20")
    assert (ip, source, calls) == ("198.51.100.20", "own address", [None])
    assert "this container's public address" in line


@pytest.mark.parametrize("proxy, what", [(PROXY, "proxy's exit IP"), (None, "container's public IP")])
def test_a_failed_lookup_warns_and_starts_without_it(wip, proxy, what):
    ip, source, line = wip.resolve(None, proxy, True, lookup=lambda p, t: None, environ={})
    assert ip is None and source == "lookup failed"
    assert line.startswith("[clearcote] WARNING: WebRTC IP: could not look up the " + what)


def test_the_lookup_budget_follows_the_sdks_variable(wip):
    assert wip.lookup_timeout({}) == wip.DEFAULT_TIMEOUT_S
    assert wip.lookup_timeout({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "3.5"}) == 3.5
    assert wip.lookup_timeout({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "x"}) == wip.DEFAULT_TIMEOUT_S
    assert wip.lookup_timeout({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "0"}) == wip.DEFAULT_TIMEOUT_S


# ---------------------------------------------------------------- the SDK's own lookup, through a proxy
class _Proxy(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture
def stand_in_proxy():
    """An HTTP proxy that answers every request with an IP-echo body, recording the request head."""
    seen = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            head = []
            while True:
                line = self.rfile.readline()
                if line in (b"", b"\r\n", b"\n"):
                    break
                head.append(line.decode("latin-1").rstrip("\r\n"))
            seen.append(head)
            body = b"203.0.113.44\n"
            self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                             b"Connection: close\r\n\r\n%s" % (len(body), body))

    server = _Proxy(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], seen
    server.shutdown()
    server.server_close()


def test_egress_ip_is_the_sdks_lookup_through_the_proxy(wip, stand_in_proxy, monkeypatch):
    port, seen = stand_in_proxy
    monkeypatch.setenv("CLEARCOTE_GEOIP_ECHO_URL", "http://echo.invalid/")
    assert wip.egress_ip("http://user:secret@127.0.0.1:%d" % port, 5.0) == "203.0.113.44"
    assert seen and seen[0][0].startswith("GET http://echo.invalid/")  # it went to the proxy, not direct
    assert any(h.lower().startswith("proxy-authorization: basic ") for h in seen[0][1:])


def test_egress_ip_never_raises(wip, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_GEOIP_ECHO_URL", "http://echo.invalid/")
    assert wip.egress_ip("http://127.0.0.1:1", 1.0) is None  # nothing listens there
    assert wip.egress_ip("not a proxy url ::", 1.0) is None


# ---------------------------------------------------------------- serve.py and the image
def test_serve_py_fills_the_webrtc_ip_before_the_persona_is_built_and_reports_it():
    with open(os.path.join(DOCKER, "serve.py"), encoding="utf-8") as fh:
        serve = fh.read()
    assert '"CC_WEBRTC_IP": "webrtc_ip"' not in serve  # not passed through raw: "off" is not an address
    assert "_webrtc_ip_mod.resolve(" in serve and 'opts["webrtc_ip"] = _wip' in serve
    assert serve.index('opts["webrtc_ip"] = _wip') < serve.index("args = fingerprint_args(opts)")
    assert '"webrtc_ip": _webrtc_ip_source,' in serve
    assert "SERVE_PROTOCOL = 4" in serve


def test_the_image_ships_the_module_and_says_protocol_4():
    with open(os.path.join(DOCKER, "Dockerfile"), encoding="utf-8") as fh:
        dockerfile = fh.read()
    copy = next(ln for ln in dockerfile.splitlines() if ln.startswith("COPY serve.py"))
    assert "webrtc_ip.py" in copy.split()
    assert 'LABEL com.clearcotelabs.serve-protocol="4"' in dockerfile
