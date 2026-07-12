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
    """Backwards compat: a cache written by an older SDK (no lease_id) is reused."""
    _env(monkeypatch); _reset()
    p = L._cache_path(KEY); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": "OLDFMT", "exp": time.time() + 800}))  # legacy shape
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    h = L.acquire_lease()
    assert calls.count("checkout") == 0
    assert h.token == "OLDFMT"
    _reset()


def test_expired_cache_triggers_checkout(monkeypatch):
    _env(monkeypatch); _reset()
    L._write_cache(KEY, "OLD", time.time() - 10, "Lold")
    calls, ok = _counter(); monkeypatch.setattr(L, "_post", ok)
    h = L.acquire_lease()
    assert calls.count("checkout") == 1
    assert h.token.startswith("TOK-")
    _reset()


def test_free_mode_no_key_no_calls(monkeypatch):
    _reset()
    monkeypatch.delenv("CLEARCOTE_LICENSE_KEY", raising=False)
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


def test_public_api_surface_preserved():
    for sym in ("acquire_lease", "inject_run_token", "resolve_license_key", "resolve_instance_id",
                "LicenseError", "ConcurrencyLimitError", "LicenseRevokedError"):
        assert hasattr(L, sym), sym
    pw = {}; L.inject_run_token(pw, "TOKX")
    assert pw["env"]["CLEARCOTE_RUN_TOKEN"] == "TOKX"
