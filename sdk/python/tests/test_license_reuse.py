"""Per-machine token-reuse licensing client: acquire_lease shares ONE checkout
across many launches in a process. Hermetic — _post is mocked to COUNT backend
calls; the machine-lease registry + on-disk cache are reset between cases."""
import json
import time

import clearcote._license as L

KEY = "cc_lic_TESTKEY"


def _reset():
    for ml in list(L._MACHINE_LEASES.values()):
        try:
            ml.shutdown()
        except Exception:
            pass
    L._MACHINE_LEASES.clear()
    try:
        L._cache_path(KEY).unlink()
    except OSError:
        pass


def _counter():
    calls = []

    def ok(url, key, body, timeout=15.0):
        p = url.rsplit("/", 1)[-1]
        calls.append(p)
        if p == "checkout":
            return 200, {"lease_id": f"L{len(calls)}", "token": f"TOK-{len(calls)}",
                         "exp": time.time() + 800, "heartbeat_interval_sec": 270}
        if p == "heartbeat":
            return 200, {"token": "TOK-hb", "exp": time.time() + 800}
        return 200, {}

    return calls, ok


def _env(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", KEY)
    monkeypatch.setenv("CLEARCOTE_LICENSE_API", "http://test.local")


def test_one_checkout_shared_across_launches(monkeypatch):
    _env(monkeypatch); _reset()
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    h1 = L.acquire_lease(); h2 = L.acquire_lease(); h3 = L.acquire_lease()
    assert calls.count("checkout") == 1                  # the whole point
    assert h1.token and h1.token == h2.token == h3.token  # shared token
    h1.stop(); h2.stop(); h3.stop()
    assert calls.count("checkin") == 0                    # no per-launch checkin
    _reset()


def test_cold_checkout_raises_on_limit(monkeypatch):
    _env(monkeypatch); _reset()
    monkeypatch.setattr(L, "_post", lambda url, k, b, timeout=15.0: (
        (429, {"error": "limit", "code": "CONCURRENCY_LIMIT_EXCEEDED"})
        if url.endswith("/checkout") else (200, {})))
    import pytest
    with pytest.raises(L.ConcurrencyLimitError):
        L.acquire_lease()
    _reset()


def test_cold_checkout_raises_on_revoked(monkeypatch):
    _env(monkeypatch); _reset()
    monkeypatch.setattr(L, "_post", lambda url, k, b, timeout=15.0: (
        (403, {"error": "revoked", "code": "LICENSE_REVOKED"})
        if url.endswith("/checkout") else (200, {})))
    import pytest
    with pytest.raises(L.LicenseRevokedError):
        L.acquire_lease()
    _reset()


def test_offline_grace_reuses_cached_token(monkeypatch):
    _env(monkeypatch); _reset()
    L._write_cache(KEY, "CACHED-TOK", time.time() + 800, "Lcache")

    def neterr(url, k, b, timeout=15.0):
        if url.endswith("/checkout"):
            raise OSError("net down")
        return 200, {}

    monkeypatch.setattr(L, "_post", neterr)
    assert L.acquire_lease().token == "CACHED-TOK"
    _reset()


def test_cross_process_disk_reuse_zero_checkout(monkeypatch):
    _env(monkeypatch); _reset()
    L._write_cache(KEY, "DISK-TOK", time.time() + 800, "Ldisk")
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    h = L.acquire_lease()
    assert calls.count("checkout") == 0
    assert h.token == "DISK-TOK"
    _reset()


def test_legacy_cache_without_lease_id(monkeypatch):
    """Backwards compat: a cache written by an older SDK (no lease_id, no scope) is still the offline
    fallback. With the server reachable it is no reason to skip the checkout: the licence may now count
    every browser, which only a checkout can tell."""
    _env(monkeypatch); _reset()
    p = L._cache_path(KEY); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": "OLDFMT", "exp": time.time() + 800}))  # legacy shape

    def neterr(url, k, b, timeout=15.0):
        raise OSError("net down")

    monkeypatch.setattr(L, "_post", neterr)
    assert L.acquire_lease().token == "OLDFMT"
    _reset()
    p.write_text(json.dumps({"token": "OLDFMT", "exp": time.time() + 800}))
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    h = L.acquire_lease()
    assert calls.count("checkout") == 1 and h.token.startswith("TOK-")
    _reset()


def test_expired_cache_triggers_checkout(monkeypatch):
    _env(monkeypatch); _reset()
    L._write_cache(KEY, "OLD", time.time() - 10, "Lold")
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    h = L.acquire_lease()
    assert calls.count("checkout") == 1
    assert h.token.startswith("TOK-")
    _reset()


def test_free_mode_no_key_no_calls(monkeypatch, tmp_path):
    _reset()
    monkeypatch.delenv("CLEARCOTE_LICENSE_KEY", raising=False)
    # "no key" must also mean no ~/.clearcote/license.key: a machine that has ever used a PRO key
    # persists one there, which made this test fail on exactly the machines that release the SDK.
    monkeypatch.setattr(L.Path, "home", classmethod(lambda cls: tmp_path))
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    assert L.acquire_lease() is None
    assert len(calls) == 0
    _reset()


def test_exit_checks_in_once(monkeypatch):
    _env(monkeypatch); _reset()
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    L.acquire_lease(); L.acquire_lease()
    L._shutdown_all()
    assert calls.count("checkin") == 1
    _reset()


def test_checkout_sends_sdk_and_engine_version(monkeypatch):
    """Telemetry split: sdk_version = the given package version; engine_version = the resolver's
    result. Both land in the checkout body; the resolver is invoked lazily (cold checkout only)."""
    _env(monkeypatch); _reset()
    bodies = []

    def ok(url, key, body, timeout=15.0):
        bodies.append((url.rsplit("/", 1)[-1], body))
        if url.endswith("/checkout"):
            return 200, {"lease_id": "L1", "token": "T1", "exp": time.time() + 800,
                         "heartbeat_interval_sec": 270}
        return 200, {}

    monkeypatch.setattr(L, "_post", ok)
    calls = {"n": 0}

    def resolver():
        calls["n"] += 1
        return "150.0.7871.114"

    L.acquire_lease(sdk_version="0.17.1", engine_version=resolver)
    L.acquire_lease(sdk_version="0.17.1", engine_version=resolver)  # reuses -> no 2nd checkout
    checkout_bodies = [b for p, b in bodies if p == "checkout"]
    assert len(checkout_bodies) == 1
    assert checkout_bodies[0]["sdk_version"] == "0.17.1"
    assert checkout_bodies[0]["engine_version"] == "150.0.7871.114"
    assert calls["n"] == 1  # resolver memoized, run once on the cold checkout only
    _reset()


def test_engine_resolver_failure_is_soft(monkeypatch):
    """A resolver that raises must not break checkout — engine_version is simply omitted (None)."""
    _env(monkeypatch); _reset()
    bodies = []

    def ok(url, key, body, timeout=15.0):
        bodies.append(body)
        return 200, {"lease_id": "L1", "token": "T1", "exp": time.time() + 800}

    monkeypatch.setattr(L, "_post", ok)

    def boom():
        raise RuntimeError("catalog down")

    h = L.acquire_lease(sdk_version="0.17.1", engine_version=boom)
    assert h.token == "T1"                       # launch still works
    assert bodies[0]["engine_version"] is None   # field omitted, not fatal
    _reset()


def test_public_api_surface_preserved():
    for sym in ("acquire_lease", "inject_run_token", "resolve_license_key", "resolve_instance_id",
                "LicenseError", "ConcurrencyLimitError", "LicenseRevokedError"):
        assert hasattr(L, sym), sym
    pw = {}; L.inject_run_token(pw, "TOKX")
    assert pw["env"]["CLEARCOTE_RUN_TOKEN"] == "TOKX"


# ── heartbeat 409: reclaimed/expired -> re-checkout ─────────────────────────────────────────────────
# The backend answers a heartbeat for a lease it no longer holds with 409 (LEASE_NOT_FOUND / LEASE_EXPIRED)
# and the SDK must re-checkout to keep its slot. The per-browser (free) loop is covered in
# test_license_per_browser.py; these drive the MACHINE (paid) loop one beat at a time, with no thread.

def _scripted(script):
    """_post: the cold checkout gets L1; after that, heartbeat/checkout answers come from `script` in order."""
    calls = []

    def post(url, key, body, timeout=15.0):
        p = url.rsplit("/", 1)[-1]
        calls.append((p, dict(body)))
        if p == "checkout" and sum(1 for c in calls if c[0] == "checkout") == 1:
            return 200, {"lease_id": "L1", "token": "TOK-1", "exp": time.time() + 800, "heartbeat_interval_sec": 270}
        if script and script[0][0] == p:
            _, status, answer = script.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return status, answer
        return 200, {}

    return calls, post


def _machine_lease(monkeypatch, post):
    monkeypatch.setattr(L, "_post", post)
    monkeypatch.setattr(L._MachineLease, "_start_heartbeat", lambda self: None)  # beats are driven by hand
    handle = L.acquire_lease()
    ml = next(iter(L._MACHINE_LEASES.values()))
    return handle, ml


def _beat(ml, n):
    """Run exactly n iterations of the heartbeat loop."""
    stops = iter([False] * n + [True])
    ml._stop.wait = lambda interval: next(stops)
    ml._heartbeat_loop(270)


def test_heartbeat_409_rechecks_out_and_beats_the_new_lease(monkeypatch):
    _env(monkeypatch); _reset()
    calls, post = _scripted([
        ("heartbeat", 409, {"code": "LEASE_EXPIRED"}),
        ("checkout", 200, {"lease_id": "L2", "token": "TOK-2", "exp": time.time() + 900}),
        ("heartbeat", 200, {"token": "TOK-3", "exp": time.time() + 1000}),
    ])
    handle, ml = _machine_lease(monkeypatch, post)
    _beat(ml, 2)
    assert [p for p, _ in calls] == ["checkout", "heartbeat", "checkout", "heartbeat"]
    assert calls[1][1]["lease_id"] == "L1"            # the refused beat
    assert calls[3][1]["lease_id"] == "L2"            # the next beat holds the re-checked-out lease
    assert ml.lease_id == "L2" and handle.token == "TOK-3"
    assert L._read_cache(KEY)["lease_id"] == "L2"      # other processes pick up the new lease too
    _reset()


def test_heartbeat_409_refused_recheckout_retries_on_the_next_beat(monkeypatch):
    _env(monkeypatch); _reset()
    calls, post = _scripted([
        ("heartbeat", 409, {"code": "LEASE_EXPIRED"}),
        ("checkout", 429, {"code": "CONCURRENCY_LIMIT_EXCEEDED"}),
        ("heartbeat", 409, {"code": "LEASE_EXPIRED"}),
        ("checkout", 200, {"lease_id": "L9", "token": "TOK-9", "exp": time.time() + 900}),
    ])
    handle, ml = _machine_lease(monkeypatch, post)
    _beat(ml, 1)
    assert ml.lease_id == "L1" and handle.token == "TOK-1"   # a refused re-checkout changes nothing...
    _beat(ml, 1)
    assert [p for p, _ in calls] == ["checkout", "heartbeat", "checkout", "heartbeat", "checkout"]
    assert ml.lease_id == "L9" and handle.token == "TOK-9"   # ...and the next beat tries again
    _reset()


def test_heartbeat_409_network_error_on_recheckout_retries_on_the_next_beat(monkeypatch):
    _env(monkeypatch); _reset()
    calls, post = _scripted([
        ("heartbeat", 409, {"code": "LEASE_NOT_FOUND"}),
        ("checkout", 0, OSError("connection reset")),
        ("heartbeat", 409, {"code": "LEASE_NOT_FOUND"}),
        ("checkout", 200, {"lease_id": "L5", "token": "TOK-5", "exp": time.time() + 900}),
    ])
    handle, ml = _machine_lease(monkeypatch, post)
    _beat(ml, 2)
    assert [p for p, _ in calls] == ["checkout", "heartbeat", "checkout", "heartbeat", "checkout"]
    assert ml.lease_id == "L5" and handle.token == "TOK-5"
    _reset()


# ── User-Agent: every licence call names the SDK and its version ────────────────────────────────────
# So the licence server's logs can tell SDK builds apart (2026-09-29: the only clients stuck on 409s sent
# no User-Agent at all, and nothing in the logs said who they were).

def _ua_server():
    """A local HTTP server recording each request's path and EVERY User-Agent header it carried."""
    import http.server
    import threading as _t

    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            n = int(self.headers.get("content-length") or 0)
            if n:
                self.rfile.read(n)
            seen.append((self.path, self.headers.get_all("User-Agent") or []))
            body = json.dumps({"used": 1, "limit": 5}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET = _answer

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_lease_calls_send_one_user_agent_naming_the_sdk_and_version(monkeypatch):
    import clearcote
    srv, seen = _ua_server()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        L._post(f"{base}/api/v1/lease/heartbeat", KEY, {"lease_id": "L1"})
        assert L.get_session_seats(KEY, api_base=base)["state"] == "ok"
    finally:
        srv.shutdown()
    want = f"clearcote-sdk-python/{clearcote.__version__}"
    assert seen == [("/api/v1/lease/heartbeat", [want]), ("/api/v1/lease/seats", [want])]


def test_proxied_lease_calls_override_the_proxy_helpers_default_user_agent(monkeypatch):
    import clearcote
    got = []

    class Res:
        status = 200

        def text(self):
            return "{}"

        def json(self):
            return {}

    def fake(url, method="GET", headers=None, body=None, timeout=30.0, proxy=None):
        got.append(dict(headers or {}))
        return Res()

    monkeypatch.setattr(L, "proxied_request", fake)
    L._post("http://test.local/api/v1/lease/heartbeat", KEY, {"lease_id": "L1"}, proxy=object())
    # Exactly "User-Agent": _net merges the caller's headers over its own {"User-Agent": "clearcote-sdk"},
    # so any other spelling would put two User-Agent headers on the wire.
    assert got[0]["User-Agent"] == f"clearcote-sdk-python/{clearcote.__version__}"
    assert not [k for k in got[0] if k.lower() == "user-agent" and k != "User-Agent"]
