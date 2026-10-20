"""Where the lease calls go (SDK 0.55): checkout, heartbeat, checkin and seats go to the licence lease server
first and to the site when it cannot be reached; a base set explicitly (api_base, CLEARCOTE_LICENSE_API,
CLEARCOTE_LEASE_API) is used alone. Served by real local HTTP servers and a closed port, so the urllib path in
_post is what is tested; the default bases are pointed at them, never at a real licence server."""
import base64
import http.server
import importlib
import io
import json
import socket
import threading
import time
import urllib.error

import pytest

import clearcote._license as L
from clearcote._net import SimpleResponse

dl = importlib.import_module("clearcote.download")

CHECKPOINT = b"<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head><body></body></html>"


def tok(plan="pro", iat=1000):
    return base64.urlsafe_b64encode(json.dumps({"plan": plan, "iat": iat}).encode()).decode().rstrip("=") + ".sig"


class Origin:
    """A local licence server. ``reply(path)`` gives (status, body bytes, content type); the default is a paid
    per-browser lease for checkout and {} for the rest. Records every request path."""

    def __init__(self, reply=None):
        self.paths = []
        origin = self

        class H(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("content-length") or 0)
                if n:
                    self.rfile.read(n)
                origin.paths.append(self.path)
                status, body, ctype = (reply or origin.default)(self.path)
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET = _answer

            def log_message(self, *a):
                pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    @staticmethod
    def default(path):
        if path.endswith("/checkout"):
            body = {"lease_id": f"L-{time.time_ns()}", "token": tok(), "exp": time.time() + 86400,
                    "lease_ttl_sec": 810, "heartbeat_interval_sec": 270, "concurrency": {"limit": 0},
                    "lease_scope": "browser"}
        elif path.endswith("/seats"):
            body = {"used": 2, "limit": None, "plan": "pro"}
        else:
            body = {}
        return 200, json.dumps(body).encode(), "application/json"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def closed_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now: connection refused
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def lic(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for var in ("CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API", "CLEARCOTE_LICENSE_THROUGH_PROXY",
                "CLEARCOTE_INSTANCE_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", f"cc_lic_base_{time.time_ns()}")
    monkeypatch.setattr(L, "_primary_down_until", {})
    monkeypatch.setattr(L, "_uncounted_launches", {})
    seen = []
    real = L._post

    def spy(url, key, body, timeout=15.0, proxy=None):
        seen.append(url)
        return real(url, key, body, timeout, proxy=proxy)

    monkeypatch.setattr(L, "_post", spy)
    L._MACHINE_LEASES.clear()
    servers = []

    def defaults(primary, fallback):
        """Point the DEFAULT bases (the ones with a fallback) at these URLs."""
        monkeypatch.setattr(L, "DEFAULT_LEASE_API_BASE", primary)
        monkeypatch.setattr(L, "DEFAULT_API_BASE", fallback)

    def origin(reply=None):
        o = Origin(reply)
        servers.append(o)
        return o

    yield {"seen": seen, "defaults": defaults, "origin": origin}
    for ml in list(L._MACHINE_LEASES.values()):
        try:
            ml.shutdown()
        except Exception:
            pass
    L._MACHINE_LEASES.clear()
    for o in servers:
        o.close()


# ── which base ───────────────────────────────────────────────────────────────────────────────────────────

def test_default_bases_are_the_lease_server_then_the_site(monkeypatch):
    for var in ("CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API"):
        monkeypatch.delenv(var, raising=False)
    assert L._lease_bases(None) == ("https://lease.clearcotelabs.com", "https://www.clearcotelabs.com")


def test_anything_set_explicitly_is_used_alone(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_LEASE_API", "https://lease-env.test/")
    assert L._lease_bases(None) == ("https://lease-env.test", None)
    monkeypatch.setenv("CLEARCOTE_LICENSE_API", "https://license-env.test")
    assert L._lease_bases(None) == ("https://license-env.test", None)   # CLEARCOTE_LICENSE_API first
    assert L._lease_bases("https://arg.test/") == ("https://arg.test", None)  # the argument before both


# ── 3. primary unreachable: the fallback, then a 60 s breaker ────────────────────────────────────────────

def test_a_closed_primary_falls_back_and_is_skipped_for_a_while(lic, monkeypatch):
    monkeypatch.setattr(L, "_PRIMARY_RETRY_SEC", 1.5)
    primary, fallback = closed_port_url(), lic["origin"]()
    lic["defaults"](primary, fallback.url)
    lease = L.acquire_lease(quiet=True)
    assert lease.token == tok()
    assert lic["seen"] == [f"{primary}/api/v1/lease/checkout", f"{fallback.url}/api/v1/lease/checkout"]
    lease.stop(wait=True)                              # within the window: straight to the fallback
    assert lic["seen"][2:] == [f"{fallback.url}/api/v1/lease/checkin"]
    assert fallback.paths == ["/api/v1/lease/checkout", "/api/v1/lease/checkin"]
    time.sleep(1.6)                                    # the window is over: the primary is tried again
    L.acquire_lease(quiet=True).stop(wait=True)
    assert lic["seen"][3:5] == [f"{primary}/api/v1/lease/checkout", f"{fallback.url}/api/v1/lease/checkout"]


def test_the_breaker_lasts_60_seconds_by_default():
    assert L._PRIMARY_RETRY_SEC == 60.0


@pytest.mark.parametrize("status,body,ctype", [
    (503, b"", "text/plain"),
    (502, b"<html>Bad gateway</html>", "text/html"),
    (522, b"", "text/html"),
    (403, CHECKPOINT, "text/html"),                    # a challenge page is not the server's answer either
])
def test_no_answer_of_its_own_goes_to_the_fallback(lic, status, body, ctype):
    primary = lic["origin"](lambda path: (status, body, ctype))
    fallback = lic["origin"]()
    lic["defaults"](primary.url, fallback.url)
    lease = L.acquire_lease(quiet=True)
    assert lease.token == tok() and primary.paths == ["/api/v1/lease/checkout"]
    assert fallback.paths == ["/api/v1/lease/checkout"]
    lease.stop(wait=True)


@pytest.mark.parametrize("status,answer,error", [
    (401, {"error": "Invalid licence key.", "code": "INVALID_LICENSE"}, L.LicenseError),
    (403, {"error": "Licence revoked.", "code": "LICENSE_REVOKED"}, L.LicenseRevokedError),
    (429, {"error": "limit", "code": "CONCURRENCY_LIMIT_EXCEEDED"}, L.ConcurrencyLimitError),
    (426, {"error": "Upgrade the SDK.", "code": "SDK_UPGRADE_REQUIRED"}, L.LicenseError),
    (500, {"error": "boom"}, L.LicenseError),
])
def test_a_json_answer_from_the_primary_is_final(lic, status, answer, error):
    primary = lic["origin"](lambda path: (status, json.dumps(answer).encode(), "application/json"))
    fallback = lic["origin"]()
    lic["defaults"](primary.url, fallback.url)
    with pytest.raises(error):
        L.acquire_lease(quiet=True)
    assert primary.paths == ["/api/v1/lease/checkout"] and fallback.paths == []


def test_seats_fall_back_too(lic):
    primary = lic["origin"](lambda path: (503, b"", "text/plain"))
    fallback = lic["origin"]()
    lic["defaults"](primary.url, fallback.url)
    assert L.get_session_seats()["state"] == "ok"
    assert primary.paths == ["/api/v1/lease/seats"] and fallback.paths == ["/api/v1/lease/seats"]
    assert L.get_session_seats()["state"] == "ok"  # within the window: the fallback only
    assert primary.paths == ["/api/v1/lease/seats"] and len(fallback.paths) == 2


def test_seats_through_a_closed_primary(lic):
    lic["defaults"](closed_port_url(), lic["origin"]().url)
    assert L.get_session_seats()["state"] == "ok"


def test_license_through_proxy_falls_back_the_same_way(lic, monkeypatch):
    lic["defaults"]("http://lease.test.invalid", "http://www.test.invalid")
    sent = []
    ok = Origin.default("/api/v1/lease/checkout")[1]

    def fake(url, method="GET", headers=None, body=None, timeout=30.0, proxy=None):
        sent.append((url, proxy["server"]))
        if url.startswith("http://lease.test.invalid"):
            raise ConnectionResetError("proxy reset the tunnel")
        return SimpleResponse(200, {"content-type": "application/json"}, ok)

    monkeypatch.setattr(L, "proxied_request", fake)
    lease = L.acquire_lease(quiet=True, license_through_proxy=True, proxy="http://127.0.0.1:9")
    assert lease.token == tok()
    assert sent == [("http://lease.test.invalid/api/v1/lease/checkout", "http://127.0.0.1:9"),
                    ("http://www.test.invalid/api/v1/lease/checkout", "http://127.0.0.1:9")]
    lease.stop(wait=True)
    assert sent[2] == ("http://www.test.invalid/api/v1/lease/checkin", "http://127.0.0.1:9")


# ── 4. a base set explicitly: no fallback, never the lease server ────────────────────────────────────────

@pytest.mark.parametrize("how", ["argument", "CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API"])
def test_an_explicit_base_is_used_alone(lic, monkeypatch, how):
    explicit = closed_port_url()
    lic["defaults"]("http://lease.test.invalid", "http://www.test.invalid")  # must not be touched
    kw = {}
    if how == "argument":
        kw["api_base"] = explicit
    else:
        monkeypatch.setenv(how, explicit)
    with pytest.raises(L.LicenseError, match="Could not reach the license server"):
        L.acquire_lease(quiet=True, **kw)
    assert lic["seen"] == [f"{explicit}/api/v1/lease/checkout"]   # once, and nowhere else
    assert not any("lease.clearcotelabs.com" in u or "test.invalid" in u for u in lic["seen"])


def test_the_lease_base_does_not_move_the_pro_download(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_LICENSE_API", raising=False)
    monkeypatch.setenv("CLEARCOTE_LEASE_API", "https://lease-env.test")
    monkeypatch.setattr(dl, "_kept_pro_meta", lambda key: None)
    seen = {}

    def capture(req, timeout=0):
        seen["url"] = req.full_url
        raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(dl.urllib.request, "urlopen", capture)
    if dl.pro_platform_key() is None:
        pytest.skip("no PRO build for this platform")
    with pytest.raises(RuntimeError):
        dl.pro_ensure_binary("cc_lic_x", quiet=True)
    assert seen["url"].startswith("https://www.clearcotelabs.com/api/v1/download/pro?")
