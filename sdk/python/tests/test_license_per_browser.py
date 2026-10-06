"""Per-browser leases (the GitHub free tier, "1 browser at a time") vs the per-machine shared lease
(every paid plan). Hermetic: _post is replaced by a fake backend that behaves like the real one — a
free key gets lease_scope "browser" and one live lease per launch_id, a paid key gets the
machine-shared lease — and HOME is a temp dir so the on-disk cache and instance_id are isolated."""
import base64
import json
import os
import re
import threading
import time

import pytest

import clearcote
import clearcote._license as L

LAUNCH_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def tok(plan, n):
    return base64.urlsafe_b64encode(json.dumps({"v": 1, "plan": plan, "n": n}).encode()).decode().rstrip("=") + ".sig"


class Backend:
    """plan "free" = per-browser (limit 1 by default); anything else = per-machine."""

    def __init__(self, plan, limit=1, fail_network=False):
        self.plan, self.limit, self.fail_network = plan, limit, fail_network
        self.per_browser = plan == "free"
        self.calls = []
        self.live = {}  # lease_id -> (launch, instance)
        self.n = 0
        self.heartbeat_status = 200
        self.lock = threading.Lock()

    def __call__(self, url, key, body, timeout=15.0, proxy=None):
        if self.fail_network:
            raise OSError("network down")
        ep = url.rsplit("/", 1)[-1]
        with self.lock:
            self.calls.append((ep, dict(body)))
            if ep == "checkout":
                launch = body.get("launch_id")
                inst = body.get("instance_id")
                if self.per_browser and not launch:
                    return 426, {"code": "SDK_UPGRADE_REQUIRED", "error": "upgrade"}
                for lid, (la, ins) in list(self.live.items()):
                    if (la == launch) if self.per_browser else (ins == inst):
                        del self.live[lid]
                if len(self.live) >= self.limit:
                    return 429, {"code": "CONCURRENCY_LIMIT_EXCEEDED", "error": "The free tier runs one browser at a time."}
                self.n += 1
                lid = f"L{self.n}"
                self.live[lid] = (launch, inst)
                out = {"lease_id": lid, "token": tok(self.plan, self.n), "exp": time.time() + 900,
                       "lease_ttl_sec": 360 if self.per_browser else 810,
                       "heartbeat_interval_sec": 120 if self.per_browser else 270,
                       "concurrency": {"used": len(self.live), "limit": self.limit}}
                if self.per_browser:
                    out["lease_scope"] = "browser"
                return 200, out
            if ep == "heartbeat":
                if self.heartbeat_status == 409:
                    self.live.pop(body.get("lease_id"), None)
                    return 409, {"code": "LEASE_EXPIRED"}
                return 200, {"token": tok(self.plan, 1000 + len(self.calls)), "exp": time.time() + 900}
            if ep == "checkin":
                self.live.pop(body.get("lease_id"), None)
                return 200, {}
            return 404, {}

    def eps(self, ep):
        return [b for e, b in self.calls if e == ep]


def wait_until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLEARCOTE_INSTANCE_ID", raising=False)
    monkeypatch.setenv("CLEARCOTE_LICENSE_API", "http://test.local")
    counter = {"n": 0}

    def use(plan, **kw):
        counter["n"] += 1
        key = f"cc_lic_{plan}_{time.time_ns()}_{counter['n']}"
        monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", key)
        be = Backend(plan, **kw)
        monkeypatch.setattr(L, "_post", be)
        return key, be

    yield use
    for ml in list(L._MACHINE_LEASES.values()):
        try:
            ml.shutdown()
        except Exception:
            pass
    L._MACHINE_LEASES.clear()


def write_cache(key, token, exp_in=800):
    p = L._cache_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": token, "exp": time.time() + exp_in, "lease_id": "OLD"}))


# ── helpers ─────────────────────────────────────────────────────────────────

def test_new_launch_id_unique_and_backend_shaped():
    ids = {L.new_launch_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(LAUNCH_RE.match(i) for i in ids)


def test_token_plan_reads_claim_and_never_raises():
    assert L.token_plan(tok("free", 1)) == "free"
    assert L.token_plan(tok("pro", 1)) == "pro"
    for junk in ["", ".", "not-a-token", "%%%.sig", None, base64.urlsafe_b64encode(b"[1,2]").decode() + ".x"]:
        assert L.token_plan(junk) is None


# ── free tier: one lease per browser ───────────────────────────────────────

def test_free_second_browser_in_same_process_refused(env):
    _, be = env("free")
    b1 = L.acquire_lease(quiet=True)
    assert b1.token
    with pytest.raises(L.ConcurrencyLimitError):
        L.acquire_lease(quiet=True)
    cos = be.eps("checkout")
    assert len(cos) == 2
    assert all(LAUNCH_RE.match(c["launch_id"]) for c in cos)
    assert cos[0]["launch_id"] != cos[1]["launch_id"]
    assert cos[0]["instance_id"] == cos[1]["instance_id"]
    b1.stop(wait=True)


def test_free_close_checks_in_that_lease_and_next_browser_runs(env):
    _, be = env("free")
    b1 = L.acquire_lease(quiet=True)
    b1.stop()
    assert wait_until(lambda: len(be.eps("checkin")) == 1)
    assert be.eps("checkin")[0]["lease_id"] == b1.lease_id
    b2 = L.acquire_lease(quiet=True)
    assert b2.lease_id != b1.lease_id
    b2.stop(wait=True)
    assert be.live == {}


def test_free_stop_is_idempotent(env):
    _, be = env("free")
    b1 = L.acquire_lease(quiet=True)
    threads = [threading.Thread(target=b1.stop) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    b1.stop(wait=True)
    time.sleep(0.2)
    assert len(be.eps("checkin")) == 1


def test_free_stop_does_not_block_the_caller(env, monkeypatch):
    _, be = env("free")
    b1 = L.acquire_lease(quiet=True)
    slow = threading.Event()
    real = L._post

    def slow_post(url, key, body, timeout=15.0, proxy=None):
        if url.endswith("/checkin"):
            slow.wait(2)
        return real(url, key, body, timeout)

    monkeypatch.setattr(L, "_post", slow_post)
    t0 = time.time()
    b1.stop()  # from a Playwright event handler in real use
    assert time.time() - t0 < 0.5
    slow.set()
    assert wait_until(lambda: len(be.eps("checkin")) == 1)


def test_free_simultaneous_launches_exactly_one_runs(env):
    _, be = env("free")
    results, errors = [], []

    def go():
        try:
            results.append(L.acquire_lease(quiet=True))
        except L.ConcurrencyLimitError as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and len(errors) == 5
    assert len({c["launch_id"] for c in be.eps("checkout")}) == 6
    results[0].stop(wait=True)


def test_free_higher_cap_each_browser_its_own_lease(env):
    _, be = env("free", limit=3)
    bs = [L.acquire_lease(quiet=True) for _ in range(3)]
    assert len({b.lease_id for b in bs}) == 3
    assert len({b.token for b in bs}) == 3
    bs[1].stop(wait=True)
    assert bs[1].lease_id not in be.live and len(be.live) == 2
    for b in bs:
        b.stop(wait=True)


def test_free_token_never_written_to_cache(env):
    key, _ = env("free")
    b1 = L.acquire_lease(quiet=True)
    assert not L._cache_path(key).exists()
    b1.stop(wait=True)
    assert not L._cache_path(key).exists()


def test_free_ignores_cached_free_token_from_older_sdk(env):
    key, be = env("free")
    write_cache(key, tok("free", 99))
    b1 = L.acquire_lease(quiet=True)
    assert len(be.eps("checkout")) == 1
    assert b1.token != tok("free", 99)
    with pytest.raises(L.ConcurrencyLimitError):
        L.acquire_lease(quiet=True)
    b1.stop(wait=True)


def test_free_no_offline_grace(env):
    key, _ = env("free", fail_network=True)
    write_cache(key, tok("free", 7))
    with pytest.raises(L.LicenseError):
        L.acquire_lease(quiet=True)


def test_free_heartbeat_own_lease_and_409_rechecks_out_same_launch(env, monkeypatch):
    _, be = env("free")
    b1 = L.acquire_lease(quiet=True)
    first_launch = be.eps("checkout")[0]["launch_id"]
    first_lease = b1.lease_id
    # restart the heartbeat on a short interval for the test
    b1._stopped.set()
    b1._thread.join(1)
    b1._stopped.clear()
    b1._thread = threading.Thread(target=b1._loop, args=(0.05,), daemon=True)
    b1._thread.start()
    assert wait_until(lambda: len(be.eps("heartbeat")) >= 1)
    assert be.eps("heartbeat")[0]["lease_id"] == first_lease
    be.heartbeat_status = 409
    assert wait_until(lambda: len(be.eps("checkout")) >= 2)
    assert be.eps("checkout")[1]["launch_id"] == first_launch
    assert wait_until(lambda: b1.lease_id != first_lease)
    be.heartbeat_status = 200
    b1.stop(wait=True)
    beats = len(be.eps("heartbeat"))
    time.sleep(0.3)
    assert len(be.eps("heartbeat")) == beats


def test_free_open_browsers_released_at_process_exit(env):
    _, be = env("free", limit=2)
    L.acquire_lease(quiet=True)
    L.acquire_lease(quiet=True)
    assert len(be.live) == 2
    L._shutdown_all()
    assert be.live == {}


def test_free_launch_failure_releases_the_slot(env, monkeypatch):
    _, be = env("free")
    monkeypatch.setattr(clearcote, "_prepare", lambda kwargs: ("exe", [], {}, None, False, None))
    monkeypatch.setattr(clearcote, "apply_font_env", lambda exe, kw, args=(): None)
    monkeypatch.setattr(clearcote, "apply_shader_dialect", lambda d, kw, *a, **k: None)
    monkeypatch.setattr(clearcote, "_headless_geometry_kwargs", lambda *a: None)

    def boom(*a, **k):
        raise RuntimeError("browser failed to start")

    monkeypatch.setattr(clearcote, "_win_av_retry", boom)
    with pytest.raises(RuntimeError):
        clearcote.launch(ephemeral_profile=False, headless=True)
    assert wait_until(lambda: len(be.eps("checkin")) == 1)
    assert be.live == {}
    # and the next launch can take the slot
    b = L.acquire_lease(quiet=True)
    b.stop(wait=True)


# ── paid: the machine-shared lease is unchanged ────────────────────────────

def test_paid_launches_share_one_checkout_and_stop_does_not_checkin(env):
    _, be = env("pro", limit=5)
    hs = [L.acquire_lease(quiet=True) for _ in range(3)]
    assert len(be.eps("checkout")) == 1
    assert len({h.token for h in hs}) == 1
    for h in hs:
        h.stop()
    time.sleep(0.1)
    assert be.eps("checkin") == []


def test_paid_cap_of_one_still_runs_many_browsers(env):
    _, be = env("pro", limit=1)
    hs = [L.acquire_lease(quiet=True) for _ in range(4)]
    assert all(h.token for h in hs)
    assert len(be.eps("checkout")) == 1


def test_paid_token_written_to_and_reused_from_cache(env):
    key, be = env("pro", limit=5)
    h = L.acquire_lease(quiet=True)
    assert json.loads(L._cache_path(key).read_text())["token"] == h.token


def test_paid_cached_token_reused_with_zero_calls(env):
    key, be = env("pro")
    write_cache(key, tok("pro", 5))
    h = L.acquire_lease(quiet=True)
    assert h.token == tok("pro", 5)
    assert be.calls == []


def test_paid_offline_grace_still_works(env, monkeypatch):
    key, be = env("pro", fail_network=True)
    write_cache(key, tok("pro", 6), exp_in=800)
    # force the cold path: make the cache look unusable for reuse but fine for offline grace
    real_read = L._read_cache
    calls = {"n": 0}

    def read_once_empty(k):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_read(k)

    monkeypatch.setattr(L, "_read_cache", read_once_empty)
    h = L.acquire_lease(quiet=True)
    assert h.token == tok("pro", 6)


def test_paid_checkout_body_only_adds_launch_id(env):
    _, be = env("pro", limit=5)
    L.acquire_lease(quiet=True, sdk_version="9.9.9")
    body = be.eps("checkout")[0]
    assert set(body) == {"instance_id", "launch_id", "os", "sdk_version", "engine_version"}
    assert body["sdk_version"] == "9.9.9"


# ── run-token file (engine online-enforcement opt-in) ──────────────────────

def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_run_token_file_bind_writes_current_token_and_release_removes(env):
    _, _be = env("free")
    b1 = L.acquire_lease(quiet=True)
    try:
        path, release = b1.bind_launch()
        assert os.path.exists(path)
        assert _read(path) == b1.token  # seeded with the current token
        release()
        assert not os.path.exists(path)
    finally:
        b1.stop(wait=True)


def test_run_token_file_stop_removes_still_bound_files(env):
    _, _be = env("free")
    b1 = L.acquire_lease(quiet=True)
    path, _release = b1.bind_launch()
    assert os.path.exists(path)
    b1.stop(wait=True)
    assert not os.path.exists(path)  # close_all on stop


def test_run_token_file_follows_token_rotation(env):
    _, _be = env("free")
    b1 = L.acquire_lease(quiet=True)
    try:
        path, release = b1.bind_launch()
        b1.token = tok("free", 42)  # a heartbeat rotates the lease's token
        assert _read(path) == tok("free", 42)  # the file follows automatically
        release()
    finally:
        b1.stop(wait=True)


def test_run_token_file_two_launches_independent_and_follow_token(env):
    _, _be = env("pro", limit=5)
    h = L.acquire_lease(quiet=True)
    try:
        a_path, a_rel = h.bind_launch()
        b_path, b_rel = h.bind_launch()
        assert a_path != b_path
        assert _read(a_path) == h.token
        assert _read(b_path) == h.token
        a_rel()
        assert not os.path.exists(a_path)
        assert os.path.exists(b_path)  # b is independent
        b_rel()
        assert not os.path.exists(b_path)
    finally:
        h.stop()


def test_run_token_file_env_carries_file_alongside_token():
    pw = {"env": {"BASE": "1"}}  # controlled base so the assertions are hermetic
    L.inject_run_token(pw, "tok.sig", "/tmp/clearcote-rt-xyz.tok")
    assert pw["env"]["CLEARCOTE_RUN_TOKEN"] == "tok.sig"
    assert pw["env"]["CLEARCOTE_RUN_TOKEN_FILE"] == "/tmp/clearcote-rt-xyz.tok"
    assert pw["env"]["BASE"] == "1"  # base env preserved
    # additive: with no file arg the FILE env is absent (older-engine / no-opt-in behaviour)
    pw2 = {"env": {"BASE": "1"}}
    L.inject_run_token(pw2, "tok.sig")
    assert pw2["env"]["CLEARCOTE_RUN_TOKEN"] == "tok.sig"
    assert "CLEARCOTE_RUN_TOKEN_FILE" not in pw2["env"]


# -- profile="auto" must not deadlock the caller against its own slot -----------------------

class _FakeBrowser:
    def on(self, *a, **k):
        pass

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return kw

    def close(self):
        pass


def _fake_driver(monkeypatch):
    """Playwright stand-in: both launch entry points hand back the same fake browser."""
    class _Chromium:
        def launch(self, **kw):
            return _FakeBrowser()

        def launch_persistent_context(self, *a, **kw):
            return _FakeBrowser()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)


def test_free_auto_profile_probe_runs_on_the_callers_slot(env, monkeypatch, tmp_path):
    """profile="auto" launches the engine a second time to read the host. On a per-browser plan
    that probe used to check out its OWN slot while the caller's was already live, so the launch
    was refused by its own lease: ConcurrencyLimitError from a call the caller never wrote."""
    _, be = env("free")
    _fake_driver(monkeypatch)
    probe = {"lease_kw": None}

    def fake_measure(launch_fn, exe, major):
        # the real one launches the engine with no persona — the part that matters here
        probe["browser"] = launch_fn(executable_path=exe, headless=True, quiet=True)
        return {"os_family": "linux", "browser_major": major, "gpu_vendor": "intel",
                "screen_width": 1920, "screen_height": 1080, "device_pixel_ratio": 1,
                "hardware_concurrency": 8, "device_memory": 8}

    monkeypatch.setattr(clearcote, "measure_host", fake_measure)
    monkeypatch.setattr(clearcote, "resolve_auto", lambda host, **kw: {
        "profile": {"navigator": {"user_agent": "ua"}}, "selection": {"entry": {"id": "p1"}},
        "source": "service"})
    exe = tmp_path / "chrome"
    exe.write_bytes(bytes([0]))

    ctx = clearcote.launch_persistent_context(str(tmp_path / "udd"), profile="auto",
                                              executable_path=str(exe), quiet=True)

    assert ctx is not None
    assert probe["browser"] is not None          # the probe really did launch
    assert len(be.eps("checkout")) == 1          # ...on the caller's slot, not a second one
