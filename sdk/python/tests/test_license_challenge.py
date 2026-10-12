"""A firewall/bot challenge page in front of the licence server (e.g. the "Vercel Security Checkpoint", a
403 HTML page) is not a licence verdict: it raises LicenseServerChallengeError (retryable), never
LicenseRevokedError, and a cached token still rides it out. The licence server's own JSON 403 still means
revoked. Served by a real local HTTP server so the urllib path in _post is what is tested."""
import http.server
import json
import threading
import time

import pytest

import clearcote
import clearcote._license as L

KEY = "cc_lic_TESTKEY_CHALLENGE"

CHECKPOINT = (b"<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head>"
              b"<body><p>We're verifying your browser</p></body></html>")


def _server(status, body, content_type, extra=None):
    class H(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            n = int(self.headers.get("content-length") or 0)
            if n:
                self.rfile.read(n)
            self.send_response(status)
            self.send_header("content-type", content_type)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET = _answer

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def lic(monkeypatch, tmp_path):
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", KEY)
    monkeypatch.delenv("CLEARCOTE_LICENSE_THROUGH_PROXY", raising=False)
    L._MACHINE_LEASES.clear()
    servers = []

    def serve(*a, **kw):
        srv, base = _server(*a, **kw)
        servers.append(srv)
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", base)
        return base

    yield serve
    for ml in list(L._MACHINE_LEASES.values()):
        try:
            ml.shutdown()
        except Exception:
            pass
    L._MACHINE_LEASES.clear()
    for s in servers:
        s.shutdown()


@pytest.mark.parametrize("status,body,ctype,extra", [
    (403, CHECKPOINT, "text/html; charset=utf-8", {"x-vercel-mitigated": "challenge"}),
    (403, CHECKPOINT, "text/html; charset=utf-8", None),        # the page alone is enough
    (403, b"<html>Access denied</html>", "text/html", None),    # any non-JSON 403
    (429, b"<html>Too many requests</html>", "text/html", None),  # not a concurrency verdict either
])
def test_challenge_page_is_not_a_revoke(lic, status, body, ctype, extra):
    lic(status, body, ctype, extra)
    with pytest.raises(L.LicenseServerChallengeError) as ei:
        L.acquire_lease(quiet=True)
    e = ei.value
    assert not isinstance(e, (L.LicenseRevokedError, L.ConcurrencyLimitError))
    assert isinstance(e, L.LicenseError)  # existing `except LicenseError` handlers still catch it
    assert e.code == "LICENSE_SERVER_CHALLENGE" and e.retryable is True
    assert "not revoked" in str(e) and f"HTTP {status}" in str(e)


def test_json_403_from_the_licence_server_is_still_revoked(lic):
    lic(403, json.dumps({"error": "License revoked.", "code": "LICENSE_REVOKED"}).encode(), "application/json")
    with pytest.raises(L.LicenseRevokedError):
        L.acquire_lease(quiet=True)


def test_json_429_is_still_the_concurrency_limit(lic):
    lic(429, json.dumps({"error": "limit", "code": "CONCURRENCY_LIMIT_EXCEEDED"}).encode(), "application/json")
    with pytest.raises(L.ConcurrencyLimitError):
        L.acquire_lease(quiet=True)


def test_challenge_on_a_checkout_rides_on_a_cached_token(lic):
    # A forced refresh (the engine refused the held token) heartbeats, then checks out: both get the
    # challenge. The checkout must fall back to the still-valid cached token like a network error,
    # not raise a revoke.
    lic(403, CHECKPOINT, "text/html", {"x-vercel-mitigated": "challenge"})
    L._write_cache(KEY, "CACHED-TOK", time.time() + 800, "Lcache")
    h = L.acquire_lease(quiet=True)  # reuses the cache: no call yet
    ml = next(iter(L._MACHINE_LEASES.values()))
    ml.refresh_if_behind_engine(force=True)
    assert ml.token == "CACHED-TOK" and h.token == "CACHED-TOK"


def test_seats_reports_a_challenge_as_unavailable_not_invalid(lic):
    base = lic(403, CHECKPOINT, "text/html")
    got = L.get_session_seats(KEY, api_base=base)
    assert got["state"] == "unavailable" and "challenge" in got["reason"]


def test_proxied_path_detects_the_challenge_too(monkeypatch):
    class Res:
        status = 403
        headers = {"content-type": "text/html", "x-vercel-mitigated": "challenge"}

        def text(self):
            return CHECKPOINT.decode()

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(L, "proxied_request", lambda *a, **kw: Res())
    status, body = L._post("http://test.local/api/v1/lease/checkout", KEY, {}, proxy=object())
    with pytest.raises(L.LicenseServerChallengeError):
        L._raise_for_status(status, body)


def test_exported_from_the_package():
    assert clearcote.LicenseServerChallengeError is L.LicenseServerChallengeError
    assert "LicenseServerChallengeError" in clearcote.__all__
