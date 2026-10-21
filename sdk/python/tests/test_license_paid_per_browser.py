"""Paid licences without a cap are counted per browser (SDK 0.55): every checkout offers lease_scope "browser",
the licence server answers it for an uncapped paid key, and each launch then holds its own lease like the free
tier. Unlike the free tier a paid browser fails OPEN when the licence server gives no verdict (unreachable,
5xx, a challenge, RATE_LIMITED after its retries): it starts uncounted on the grace token
(~/.clearcote/grace-<hash>.json, never the shared lease-<hash>.json) and the next checkout reports it.

Hermetic: _post is replaced by a fake licence server, HOME/USERPROFILE/LOCALAPPDATA are a temp dir, and the
default lease bases point at .invalid hosts, so nothing here can reach a real licence server."""
import base64
import hashlib
import json
import os
import threading
import time

from pathlib import Path

import pytest

import clearcote
import clearcote._license as L

PRIMARY = "http://lease.test.invalid"
FALLBACK = "http://www.test.invalid"
UNREACHABLE = "this browser starts on a cached run-token and is not counted."


def tok(plan, n, iat):
    body = json.dumps({"v": 1, "plan": plan, "n": n, "iat": iat}).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + ".sig"


class Server:
    """The licence server as of SDK 0.55. plan "free": one lease per browser (limit 1). A paid plan with
    ``cap=None``: one lease per browser when the checkout offers lease_scope "browser" with a launch_id (24 h
    token, no ``concurrency.used``); with a cap: the machine lease. ``down`` fails every call like an
    unreachable host (both bases); ``answers`` are (endpoint, status, body) served first, in order."""

    def __init__(self, plan, cap=None):
        self.plan, self.cap = plan, cap
        self.calls = []  # (url, endpoint, body)
        self.live = {}
        self.n = 0
        self.down = False
        self.answers = []
        self.notice = None
        self.lock = threading.Lock()

    def per_browser(self, body):
        if self.plan == "free":
            return True
        return self.cap is None and "browser" in (body.get("lease_scope_supported") or []) and bool(body.get("launch_id"))

    def __call__(self, url, key, body, timeout=15.0, proxy=None):
        ep = url.rsplit("/", 1)[-1]
        with self.lock:
            self.calls.append((url, ep, dict(body)))
            if self.down:
                raise OSError("[Errno 11001] getaddrinfo failed")
            for i, (e, status, answer) in enumerate(self.answers):
                if e == ep:
                    del self.answers[i]
                    return status, answer
            now = time.time()
            if ep == "checkout":
                self.n += 1
                lid = f"L{self.n}"
                if self.per_browser(body):
                    if self.plan == "free":
                        for k, v in list(self.live.items()):
                            if v == body.get("launch_id"):
                                del self.live[k]
                        if len(self.live) >= 1:
                            return 429, {"code": "CONCURRENCY_LIMIT_EXCEEDED", "error": "one browser at a time"}
                        out = {"lease_id": lid, "token": tok("free", self.n, int(now) + self.n), "exp": now + 180,
                               "lease_ttl_sec": 360, "heartbeat_interval_sec": 60,
                               "concurrency": {"used": 1, "limit": 1}, "lease_scope": "browser"}
                    else:
                        out = {"lease_id": lid, "token": tok(self.plan, self.n, int(now) + self.n), "exp": now + 86400,
                               "lease_ttl_sec": 810, "heartbeat_interval_sec": 270,
                               "concurrency": {"limit": 0}, "lease_scope": "browser"}
                    self.live[lid] = body.get("launch_id")
                else:
                    out = {"lease_id": lid, "token": tok(self.plan, self.n, int(now) + self.n), "exp": now + 86400,
                           "lease_ttl_sec": 810, "heartbeat_interval_sec": 270,
                           "concurrency": {"used": 1, "limit": self.cap}}
                    self.live[lid] = body.get("instance_id")
                if self.notice:
                    out["notice"] = self.notice
                return 200, out
            if ep == "heartbeat":
                self.n += 1
                return 200, {"token": tok(self.plan, self.n, int(now) + self.n), "exp": now + 86400}
            if ep == "checkin":
                self.live.pop(body.get("lease_id"), None)
                return 200, {}
            return 404, {}

    def eps(self, ep):
        return [b for _u, e, b in self.calls if e == ep]

    def urls(self, ep):
        return [u for u, e, _b in self.calls if e == ep]


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
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))  # where the engine keeps its token mark
    for var in ("CLEARCOTE_INSTANCE_ID", "CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API",
                "CLEARCOTE_LICENSE_THROUGH_PROXY"):
        monkeypatch.delenv(var, raising=False)
    # The default bases, primary and fallback, on hosts that cannot resolve: a call the fake missed fails.
    monkeypatch.setattr(L, "DEFAULT_LEASE_API_BASE", PRIMARY)
    monkeypatch.setattr(L, "DEFAULT_API_BASE", FALLBACK)
    monkeypatch.setattr(L, "_primary_down_until", {})
    monkeypatch.setattr(L, "_uncounted_launches", {})
    monkeypatch.setattr(L, "_notices_shown", set())
    monkeypatch.setattr(L, "_RATE_LIMIT_RETRY_SEC", (0, 0, 0))
    L._MACHINE_LEASES.clear()
    counter = {"n": 0}

    def use(plan, cap=None):
        counter["n"] += 1
        key = f"cc_lic_{plan}_{time.time_ns()}_{counter['n']}"
        monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", key)
        server = Server(plan, cap)
        monkeypatch.setattr(L, "_post", server)
        return key, server

    yield use
    for ml in list(L._MACHINE_LEASES.values()):
        try:
            ml.shutdown()
        except Exception:
            pass
    L._MACHINE_LEASES.clear()


def new_process():
    """What a second process on this machine starts with: no in-process leases, counters or breaker."""
    for ml in list(L._MACHINE_LEASES.values()):
        ml._stop.set()
    L._MACHINE_LEASES.clear()


def grace_path(key):
    """~/.clearcote/grace-<the same 16-hex key hash as lease-<hash>.json>.json"""
    return Path.home() / ".clearcote" / f"grace-{hashlib.sha256(key.encode()).hexdigest()[:16]}.json"


def write_grace(key, token, exp_in=3600):
    p = grace_path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": token, "exp": time.time() + exp_in}))


def read_grace(key):
    return json.loads(grace_path(key).read_text())


# ── 1. paid without a cap: one lease per browser ─────────────────────────────────────────────────────────

def test_paid_uncapped_five_launches_five_checkouts_five_checkins(env):
    key, server = env("pro")
    leases = [L.acquire_lease(quiet=True) for _ in range(5)]
    checkouts = server.eps("checkout")
    assert len(checkouts) == 5
    assert all(c["lease_scope_supported"] == ["browser"] for c in checkouts)
    assert len({c["launch_id"] for c in checkouts}) == 5
    assert len({lease.lease_id for lease in leases}) == 5 and len({lease.token for lease in leases}) == 5
    assert all(isinstance(lease, L._BrowserLease) for lease in leases)
    assert all(lease._thread._args == (270,) for lease in leases)  # the server's heartbeat_interval_sec
    for lease in leases:
        lease.stop(wait=True)
    assert sorted(b["lease_id"] for b in server.eps("checkin")) == sorted(lease.lease_id for lease in leases)
    assert server.live == {}
    assert not L._cache_path(key).exists()  # never in the file older SDKs reuse
    assert read_grace(key)["token"] == leases[-1].token  # the newest paid token


def test_paid_browser_heartbeat_keeps_the_newer_token_as_grace(env):
    key, server = env("pro")
    lease = L.acquire_lease(quiet=True)
    first = lease.token
    lease._stopped.set()  # restart its heartbeat on a short interval
    lease._thread.join(1)
    lease._stopped.clear()
    lease._thread = threading.Thread(target=lease._loop, args=(0.05,), daemon=True)
    lease._thread.start()
    assert wait_until(lambda: lease.token != first)
    assert wait_until(lambda: read_grace(key)["token"] == lease.token)
    assert L.token_iat(lease.token) > L.token_iat(first)
    lease.stop(wait=True)
    assert not L._cache_path(key).exists()


# ── 2. a machine-scope answer (paid, capped): exactly as before ──────────────────────────────────────────

def test_capped_paid_machine_lease_is_unchanged(env):
    key, server = env("pro", cap=3)
    h1 = L.acquire_lease(quiet=True)
    h2 = L.acquire_lease(quiet=True)
    assert len(server.eps("checkout")) == 1          # one checkout for the process
    assert server.eps("checkout")[0]["lease_scope_supported"] == ["browser"]  # offered, not granted
    assert isinstance(h1, L._LeaseHandle) and h1.token == h2.token
    cached = json.loads(L._cache_path(key).read_text())
    assert cached["token"] == h1.token and cached["lease_id"] == "L1"
    assert not grace_path(key).exists()           # machine tokens are never grace tokens
    h1.stop()
    h2.stop()
    assert server.eps("checkin") == []               # stop() only drops a reference
    new_process()
    calls = len(server.calls)
    h3 = L.acquire_lease(quiet=True)
    assert h3.token == h1.token and len(server.calls) == calls  # another process reuses it: zero calls


# ── the shared lease cache skips a checkout only when this SDK marked it a machine lease ─────────────────

def write_lease_cache(key, token, exp_in=3600, scope=None):
    """lease-<hash>.json as SDK 0.54 writes it (no scope), or marked."""
    d = {"token": token, "exp": time.time() + exp_in, "lease_id": "L-old"}
    if scope:
        d["scope"] = scope
    L._cache_path(key).parent.mkdir(parents=True, exist_ok=True)
    L._cache_path(key).write_text(json.dumps(d))


def test_an_unmarked_cache_gets_a_checkout_and_the_browser_scope_wins(env):
    key, server = env("pro")
    write_lease_cache(key, tok("pro", 1, 1))          # a 0.54 process on this machine wrote it
    leases = [L.acquire_lease(quiet=True) for _ in range(3)]
    checkouts = server.eps("checkout")
    assert len(checkouts) == 3 and all(c["lease_scope_supported"] == ["browser"] for c in checkouts)
    assert len({lease.lease_id for lease in leases}) == 3 and tok("pro", 1, 1) not in {x.token for x in leases}
    assert json.loads(L._cache_path(key).read_text())["token"] == tok("pro", 1, 1)  # never a browser's token
    for lease in leases:
        lease.stop(wait=True)


def test_a_machine_answer_writes_the_marked_cache_and_it_is_reused_from_then_on(env):
    key, server = env("pro", cap=2)
    write_lease_cache(key, tok("pro", 1, 1))
    handle = L.acquire_lease(quiet=True)
    assert len(server.eps("checkout")) == 1
    written = json.loads(L._cache_path(key).read_text())
    assert written == {"token": handle.token, "exp": written["exp"], "lease_id": "L1", "scope": "machine"}
    assert written["exp"] > time.time() + 3600
    new_process()
    calls = len(server.calls)
    assert L.acquire_lease(quiet=True).token == handle.token and len(server.calls) == calls  # zero calls


def test_a_marked_cache_is_reused_with_zero_calls(env):
    key, server = env("pro")
    write_lease_cache(key, tok("pro", 5, 5), scope="machine")
    assert L.acquire_lease(quiet=True).token == tok("pro", 5, 5)
    assert server.calls == []


def test_an_unmarked_cache_is_still_the_fallback_when_nothing_answers(env):
    key, server = env("pro")
    write_lease_cache(key, tok("pro", 2, 2))
    server.down = True
    assert L.acquire_lease(quiet=True).token == tok("pro", 2, 2)
    assert server.urls("checkout") == [f"{PRIMARY}/api/v1/lease/checkout", f"{FALLBACK}/api/v1/lease/checkout"]


def test_keeping_an_unmarked_fallback_alive_never_marks_it(env):
    key, server = env("pro", cap=2)
    write_lease_cache(key, tok("pro", 2, 2))
    server.down = True
    L.acquire_lease(quiet=True)                        # adopted as before: this process now beats L-old
    ml = next(iter(L._MACHINE_LEASES.values()))
    server.down = False
    beats = iter([False, True])
    ml._stop.wait = lambda interval: next(beats)
    ml._heartbeat_loop(270)                            # one beat: a new token for the same lease
    rotated = json.loads(L._cache_path(key).read_text())
    assert rotated["lease_id"] == "L-old" and rotated["token"] != tok("pro", 2, 2) and "scope" not in rotated
    server.answers.append(("heartbeat", 409, {"code": "LEASE_EXPIRED"}))
    beats = iter([False, True])
    ml._heartbeat_loop(270)                            # the lease is gone: re-checkout with no launch_id
    # A re-checkout without a launch_id keeps the lease alive but confirms nothing (as in Node): still unmarked.
    rechecked = json.loads(L._cache_path(key).read_text())
    assert rechecked["lease_id"] != "L-old" and "scope" not in rechecked
    assert ml._machine_known is False


def test_a_409_recheckout_keeps_a_confirmed_lease_marked(env):
    key, server = env("pro", cap=2)
    L.acquire_lease(quiet=True)                        # a checkout offered the browser scope and got machine
    ml = next(iter(L._MACHINE_LEASES.values()))
    assert ml._machine_known is True
    server.answers.append(("heartbeat", 409, {"code": "LEASE_EXPIRED"}))
    beats = iter([False, True])
    ml._stop.wait = lambda interval: next(beats)
    ml._heartbeat_loop(270)
    assert json.loads(L._cache_path(key).read_text())["scope"] == "machine"


def test_a_refresh_keeps_the_marked_cache_marked(env):
    key, server = env("pro")
    write_lease_cache(key, tok("pro", 5, 1000), scope="machine")
    (Path.home() / ".clearcote" / ".cc_hwm").write_text("1500")  # the engine accepted a newer token here
    handle = L.acquire_lease(quiet=True)
    assert [ep for _u, ep, _b in server.calls] == ["heartbeat"]
    cached = json.loads(L._cache_path(key).read_text())
    assert cached["token"] == handle.token and cached["scope"] == "machine"


# ── 5. both bases unreachable, paid: the launch starts uncounted on the grace token ──────────────────────

def test_paid_unreachable_launch_runs_on_the_grace_token_and_is_reported_next_time(env, capsys):
    key, server = env("pro")
    first = L.acquire_lease()  # not quiet: the warning below is printed
    server.down = True
    second = L.acquire_lease()
    assert second.token == first.token == read_grace(key)["token"] and L._uncounted_launches == {key: 1}
    assert isinstance(second, L._UncountedLease)
    err = capsys.readouterr().err
    assert err.count(UNREACHABLE) == 1
    assert "[clearcote] [license] licence server unreachable (" in err and "getaddrinfo failed" in err
    attempted = server.urls("checkout")[1:]
    assert attempted == [f"{PRIMARY}/api/v1/lease/checkout", f"{FALLBACK}/api/v1/lease/checkout"]  # both tried
    assert L._uncounted_launches == {key: 1}
    path, release = second.bind_launch()
    with open(path, encoding="utf-8") as fh:
        assert fh.read() == first.token
    release()
    second.stop()
    assert server.eps("checkin") == [] and server.eps("heartbeat") == []  # no lease: nothing to beat or check in

    server.down = False
    third = L.acquire_lease()
    assert server.eps("checkout")[-1]["uncounted_launches"] == 1
    assert L._uncounted_launches == {}               # reset after the 200
    fourth = L.acquire_lease()
    assert "uncounted_launches" not in server.eps("checkout")[-1]
    for lease in (first, third, fourth):
        lease.stop(wait=True)


def test_first_launch_of_a_process_uses_the_grace_token_another_process_left(env, capsys):
    key, server = env("pro")
    write_grace(key, tok("pro", 7, 1_000))
    server.down = True
    lease = L.acquire_lease()
    assert lease.token == tok("pro", 7, 1_000) and L._uncounted_launches == {key: 1}
    assert isinstance(lease, L._UncountedLease)
    assert capsys.readouterr().err.count(UNREACHABLE) == 1
    server.down = False
    counted = L.acquire_lease(quiet=True)
    assert isinstance(counted, L._BrowserLease)
    assert server.eps("checkout")[-1]["uncounted_launches"] == 1 and L._uncounted_launches == {}
    counted.stop(wait=True)


def test_an_uncounted_browser_left_open_loses_its_token_file_at_exit(env):
    key, server = env("pro")
    write_grace(key, tok("pro", 4, 4))
    server.down = True
    lease = L.acquire_lease(quiet=True)
    path, _release = lease.bind_launch()
    assert os.path.exists(path)
    L._shutdown_all()
    assert not os.path.exists(path) and server.eps("checkin") == []


def test_the_uncounted_report_is_kept_until_a_checkout_succeeds(env):
    key, server = env("pro")
    first = L.acquire_lease(quiet=True)
    server.down = True
    L.acquire_lease(quiet=True)
    server.down = False
    server.answers.append(("checkout", 401, {"error": "Invalid licence key.", "code": "INVALID_LICENSE"}))
    with pytest.raises(L.LicenseError):
        L.acquire_lease(quiet=True)
    assert server.eps("checkout")[-1]["uncounted_launches"] == 1
    assert L._uncounted_launches == {key: 1}         # not a 200: still owed
    L.acquire_lease(quiet=True)
    assert server.eps("checkout")[-1]["uncounted_launches"] == 1 and L._uncounted_launches == {}
    first.stop(wait=True)


@pytest.mark.parametrize("failure", [
    (503, {}),                                                       # a 5xx from both bases
    (403, {L._CHALLENGE: "Vercel Security Checkpoint"}),             # a challenge page on both bases
])
def test_paid_no_verdict_answers_fail_open_too(env, capsys, failure):
    key, server = env("pro")
    first = L.acquire_lease()
    server.answers += [("checkout",) + failure] * 2                   # primary, then fallback
    second = L.acquire_lease()
    assert second.token == first.token and L._uncounted_launches == {key: 1}
    assert isinstance(second, L._UncountedLease)
    assert capsys.readouterr().err.count(UNREACHABLE) == 1
    first.stop(wait=True)


def test_without_a_grace_token_the_machine_cache_token_is_used(env):
    key, server = env("pro")
    first = L.acquire_lease(quiet=True)
    grace_path(key).unlink()
    L._write_cache(key, tok("pro", 50, 50), time.time() + 3600, "L-machine")
    server.down = True
    second = L.acquire_lease(quiet=True)
    assert second.token == tok("pro", 50, 50) and L._uncounted_launches == {key: 1}
    assert isinstance(second, L._UncountedLease)
    first.stop(wait=True)


# ── 6. both unreachable, no usable token: LicenseError as before ─────────────────────────────────────────

def test_unreachable_without_any_token_raises_as_before(env):
    key, server = env("pro")
    server.down = True
    with pytest.raises(L.LicenseError, match="Could not reach the license server"):
        L.acquire_lease(quiet=True)
    assert L._uncounted_launches == {}


def test_unreachable_with_an_expiring_grace_token_raises(env):
    key, server = env("pro")
    first = L.acquire_lease(quiet=True)
    write_grace(key, tok("pro", 9, 10**10), exp_in=30)  # inside the 60 s margin
    server.down = True
    with pytest.raises(L.LicenseError, match="Could not reach the license server to start this browser"):
        L.acquire_lease(quiet=True)
    assert L._uncounted_launches == {}
    first.stop(wait=True)


def test_a_corrupt_grace_file_is_no_token(env):
    key, server = env("pro")
    grace_path(key).parent.mkdir(parents=True, exist_ok=True)
    grace_path(key).write_text("{not json")
    server.down = True
    with pytest.raises(L.LicenseError):
        L.acquire_lease(quiet=True)


# ── 7. the free tier stays fail-closed ───────────────────────────────────────────────────────────────────

def test_free_unreachable_raises_even_with_a_paid_grace_file(env):
    key, server = env("free")
    b1 = L.acquire_lease(quiet=True)
    b1.stop(wait=True)
    assert not grace_path(key).exists()            # free tokens are never kept
    write_grace(key, tok("pro", 1, 1))                # e.g. left from when this key was paid
    server.down = True
    with pytest.raises(L.LicenseError):
        L.acquire_lease(quiet=True)
    assert L._uncounted_launches == {}


def test_a_free_token_in_the_grace_file_is_never_used(env):
    key, server = env("free")
    write_grace(key, tok("free", 1, 1))
    server.down = True
    with pytest.raises(L.LicenseError):
        L.acquire_lease(quiet=True)


# ── 8. RATE_LIMITED: retried, never a concurrency verdict ────────────────────────────────────────────────

RATE_LIMITED = {"error": "Rate limit exceeded.", "code": "RATE_LIMITED"}


def test_rate_limited_is_retried_after_1_2_4_seconds_then_succeeds(env, monkeypatch):
    key, server = env("pro")
    monkeypatch.setattr(L, "_RATE_LIMIT_RETRY_SEC", (1.0, 2.0, 4.0))
    slept = []
    monkeypatch.setattr(L.time, "sleep", slept.append)
    server.answers += [("checkout", 429, RATE_LIMITED)] * 2
    lease = L.acquire_lease(quiet=True)
    assert isinstance(lease, L._BrowserLease)
    assert slept == [1.0, 2.0] and len(server.eps("checkout")) == 3
    assert len({c["launch_id"] for c in server.eps("checkout")}) == 1  # the same launch, sent again
    lease.stop(wait=True)


def test_the_default_waits_are_1_2_and_4_seconds():
    assert L._RATE_LIMIT_RETRY_SEC == (1.0, 2.0, 4.0)


def test_persistent_rate_limited_paid_runs_on_the_grace_token(env, capsys):
    key, server = env("pro")
    first = L.acquire_lease()
    server.answers += [("checkout", 429, RATE_LIMITED)] * 4
    second = L.acquire_lease()
    assert second.token == first.token and L._uncounted_launches == {key: 1}
    assert isinstance(second, L._UncountedLease)
    assert len(server.eps("checkout")) == 1 + 4      # the launch's checkout plus 3 retries
    err = capsys.readouterr().err
    assert err.count(UNREACHABLE) == 1 and "licence server unreachable (rate limited);" in err
    first.stop(wait=True)


def test_persistent_rate_limited_without_a_token_is_not_a_concurrency_error(env):
    key, server = env("pro")
    server.answers += [("checkout", 429, RATE_LIMITED)] * 4
    with pytest.raises(L.LicenseError) as ei:
        L.acquire_lease(quiet=True)
    assert not isinstance(ei.value, L.ConcurrencyLimitError) and ei.value.code == "RATE_LIMITED"
    assert str(ei.value) == ("Rate limit exceeded. The licence server is limiting requests from this network "
                             "(RATE_LIMITED); this is not the licence's concurrency limit. Retry in a minute.")


@pytest.mark.parametrize("answer", [{"error": "limit", "code": "CONCURRENCY_LIMIT_EXCEEDED"}, {"error": "limit"}])
def test_a_concurrency_429_raises_at_once(env, monkeypatch, answer):
    key, server = env("pro")
    first = L.acquire_lease(quiet=True)
    slept = []
    monkeypatch.setattr(L.time, "sleep", slept.append)
    server.answers.append(("checkout", 429, answer))
    with pytest.raises(L.ConcurrencyLimitError):
        L.acquire_lease(quiet=True)
    assert len(server.eps("checkout")) == 2 and slept == []  # no retry, no fallback token
    assert L._uncounted_launches == {}
    first.stop(wait=True)


def test_a_rate_limited_heartbeat_waits_for_the_next_beat(env):
    key, server = env("pro")
    lease = L.acquire_lease(quiet=True)
    token, lease_id = lease.token, lease.lease_id
    server.answers.append(("heartbeat", 429, RATE_LIMITED))
    lease._stopped.set()
    lease._thread.join(1)
    lease._stopped.clear()
    lease._thread = threading.Thread(target=lease._loop, args=(0.05,), daemon=True)
    lease._thread.start()
    assert wait_until(lambda: len(server.eps("heartbeat")) >= 2)
    assert len(server.eps("checkout")) == 1          # no re-checkout on RATE_LIMITED
    assert lease.lease_id == lease_id and lease.token != token  # the next beat rotated it
    lease.stop(wait=True)


def test_rate_limited_maps_to_its_own_code():
    with pytest.raises(L.LicenseError) as ei:
        L._raise_for_status(429, RATE_LIMITED)
    assert type(ei.value) is L.LicenseError and ei.value.code == "RATE_LIMITED" and ei.value.status == 429


# ── 9. definitive verdicts never fall back ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,answer,error", [
    (426, {"error": "This SDK is too old for this licence: upgrade clearcote.", "code": "SDK_UPGRADE_REQUIRED"},
     L.LicenseError),
    (401, {"error": "Invalid licence key.", "code": "INVALID_LICENSE"}, L.LicenseError),
    (403, {"error": "Licence revoked.", "code": "LICENSE_REVOKED"}, L.LicenseRevokedError),
])
def test_a_definitive_verdict_is_raised_not_ridden_out(env, capsys, status, answer, error):
    key, server = env("pro")
    first = L.acquire_lease()
    assert read_grace(key)["exp"] > time.time() + 60  # a valid grace token is right there...
    server.answers.append(("checkout", status, answer))
    with pytest.raises(error) as ei:
        L.acquire_lease()
    assert str(ei.value) == answer["error"] and ei.value.code in (answer["code"], "LICENSE_REVOKED")
    assert server.urls("checkout")[-1].startswith(PRIMARY)  # ...and the answer was final: no fallback base
    assert len(server.eps("checkout")) == 2
    assert UNREACHABLE not in capsys.readouterr().err and L._uncounted_launches == {}
    first.stop(wait=True)


# ── 10. notice ───────────────────────────────────────────────────────────────────────────────────────────

def test_a_notice_is_printed_once_per_process(env, capsys):
    key, server = env("pro")
    server.notice = "Your licence renews in 3 days."
    a = L.acquire_lease()
    b = L.acquire_lease()
    assert len(server.eps("checkout")) == 2
    assert capsys.readouterr().err.count("[clearcote] [license] Your licence renews in 3 days.\n") == 1
    a.stop(wait=True)
    b.stop(wait=True)


def test_no_notice_prints_nothing(env, capsys):
    key, server = env("pro")
    a = L.acquire_lease()
    b = L.acquire_lease()
    assert capsys.readouterr().err == ""
    a.stop(wait=True)
    b.stop(wait=True)


def test_quiet_prints_no_notice(env, capsys):
    key, server = env("pro")
    server.notice = "Your licence renews in 3 days."
    L.acquire_lease(quiet=True).stop(wait=True)
    assert capsys.readouterr().err == ""


# ── 11. concurrency.used may be absent ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    {"lease_id": "B1", "token": tok("pro", 1, 1), "exp": time.time() + 86400, "lease_ttl_sec": 810,
     "heartbeat_interval_sec": 270, "concurrency": {"limit": 0}, "lease_scope": "browser"},
    {"lease_id": "M1", "token": tok("pro", 1, 1), "exp": time.time() + 86400, "heartbeat_interval_sec": 270},
])
def test_answers_without_concurrency_used_are_accepted(env, answer):
    key, server = env("pro")
    server.answers.append(("checkout", 200, answer))
    lease = L.acquire_lease(quiet=True)
    assert lease.token == tok("pro", 1, 1)
    assert isinstance(lease, L._BrowserLease if answer.get("lease_scope") else L._LeaseHandle)


# ── 13. the grace token only moves forward ───────────────────────────────────────────────────────────────

def test_grace_file_is_not_replaced_by_an_older_token(env):
    key, _server = env("pro")
    exp = time.time() + 3600
    L._write_grace(key, tok("pro", 1, 2000), exp)
    L._write_grace(key, tok("pro", 2, 1000), exp)     # minted earlier: ignored
    assert read_grace(key)["token"] == tok("pro", 1, 2000)
    L._write_grace(key, tok("pro", 3, 2000), exp)     # same iat: replaces
    assert read_grace(key)["token"] == tok("pro", 3, 2000)
    L._write_grace(key, tok("pro", 4, 3000), exp)
    assert read_grace(key)["token"] == tok("pro", 4, 3000)
    L._write_grace(key, tok("free", 5, 9000), exp)    # never a free token
    assert read_grace(key)["token"] == tok("pro", 4, 3000)


def test_a_reader_never_sees_a_half_written_grace_file(env):
    """Heartbeats replace the grace file while other launches (and other processes) read it: a reader must see
    the old token or the new one, never an empty or partial file."""
    key, _server = env("pro")
    exp = time.time() + 3600
    L._write_grace(key, tok("pro", 0, 1000), exp)
    stop = threading.Event()
    torn = []

    def writer():
        for i in range(1, 400):
            L._write_grace(key, tok("pro", i, 1000 + i), exp)
        stop.set()

    t = threading.Thread(target=writer)
    t.start()
    while not stop.is_set():
        try:
            json.loads(grace_path(key).read_text())
        except ValueError:
            torn.append(1)
        except PermissionError:
            pass  # Windows: the rename and the read crossed; a real reader treats it as a miss and retries
    t.join()
    assert torn == []
    assert read_grace(key)["token"] == tok("pro", 399, 1399)
    assert [f.name for f in grace_path(key).parent.iterdir() if f.name.endswith(".tmp")] == []


def test_a_browser_lease_never_rolls_the_grace_token_back(env):
    key, server = env("pro")
    write_grace(key, tok("pro", 99, 10**10))          # another process got a newer one
    lease = L.acquire_lease(quiet=True)
    assert read_grace(key)["token"] == tok("pro", 99, 10**10)
    lease.stop(wait=True)


# ── the launch path takes an uncounted handle like any other ─────────────────────────────────────────────

def test_launch_runs_an_uncounted_browser_on_the_grace_token(env, monkeypatch):
    key, server = env("pro")
    write_grace(key, tok("pro", 3, 3))
    server.down = True
    seen = {}

    class Browser:
        def __init__(self):
            self.handlers = []

        def on(self, event, fn):
            self.handlers.append(fn)

    def start(fn, exe):
        return fn(exe)

    class Chromium:
        def launch(self, **kw):
            seen.update(kw)
            seen["browser"] = Browser()
            return seen["browser"]

    class PW:
        chromium = Chromium()

    monkeypatch.setattr(clearcote, "_prepare", lambda kwargs: ("exe", [], {"env": {"KEEP": "1"}}, None, False, None))
    monkeypatch.setattr(clearcote, "apply_font_env", lambda exe, kw, *a, **k: None)
    monkeypatch.setattr(clearcote, "apply_shader_dialect", lambda d, kw, *a, **k: None)
    monkeypatch.setattr(clearcote, "_headless_geometry_kwargs", lambda *a: None)
    monkeypatch.setattr(clearcote, "_win_av_retry", start)
    monkeypatch.setattr(clearcote, "_playwright", lambda: PW())
    monkeypatch.setattr(clearcote, "engine_decides_color_scheme", lambda exe: False)
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "resolved_engine_version", lambda *a, **k: "155.0.0.0")  # no catalog call
    clearcote.launch(ephemeral_profile=False, headless=True, quiet=True)
    assert seen["env"]["CLEARCOTE_RUN_TOKEN"] == tok("pro", 3, 3) and seen["env"]["KEEP"] == "1"
    token_file = seen["env"]["CLEARCOTE_RUN_TOKEN_FILE"]
    with open(token_file, encoding="utf-8") as fh:
        assert fh.read() == tok("pro", 3, 3)
    for fn in seen["browser"].handlers:  # the browser closes
        fn()
    assert server.eps("checkin") == [] and L._uncounted_launches == {key: 1}
    assert not os.path.exists(token_file)
