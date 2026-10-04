"""The cloud client (clearcote.cloud) against an in-memory hosted API: option mapping, local-only
option rejection, env switching, CloudError mapping, every resource, run polling, webhook
signatures and cookie selection. Offline: the only server is tests/_fake_cloud.py.
Mirrors sdk/node/test/cloud.test.ts."""
import asyncio
import hashlib
import hmac
import json
import os
import socket

import clearcote
import pytest
from _fake_cloud import API_KEY, LIVE, RECORDING, start_fake_cloud, stop_fake_cloud
from clearcote import async_api, cloud
from clearcote._agent import AGENT_KEYS
from clearcote._fingerprint import FINGERPRINT_KEYS
from clearcote.cloud import (
    AsyncCloud,
    Cloud,
    CloudError,
    CloudTimeoutError,
    cookies_from_state,
    filter_cookies,
    session_body,
    verify_webhook,
)


@pytest.fixture
def api(monkeypatch):
    a = start_fake_cloud()
    monkeypatch.setenv("CLEARCOTE_API_KEY", API_KEY)
    monkeypatch.setenv("CLEARCOTE_API_URL", a.url)
    monkeypatch.delenv("CLEARCOTE_CLOUD", raising=False)
    yield a
    stop_fake_cloud(a)


@pytest.fixture
def client(api):
    return Cloud()


# ── local or cloud ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), ("YES", True), (" yes ", True),
                                            ("0", False), ("", False), ("no", False), ("cloud", False)])
def test_env_switch(monkeypatch, value, expected):
    monkeypatch.setenv("CLEARCOTE_CLOUD", value)
    assert cloud.cloud_requested(None) is expected


def test_explicit_flag_beats_env(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_CLOUD", "1")
    assert cloud.cloud_requested(False) is False
    assert cloud.cloud_requested("false") is False
    monkeypatch.delenv("CLEARCOTE_CLOUD")
    assert cloud.cloud_requested(True) is True


def test_launch_routes_on_the_env(monkeypatch):
    seen = []
    monkeypatch.setattr(clearcote, "launch_cloud", lambda c, kw, **k: seen.append(("cloud", c, dict(kw))) or "B")
    monkeypatch.setattr(clearcote, "_launch_on_throwaway_profile", lambda p, kw: seen.append(("local", dict(kw))) or "L")
    monkeypatch.setattr(clearcote, "_install_persistent_as_browser", lambda c: c)
    monkeypatch.setenv("CLEARCOTE_CLOUD", "1")
    assert clearcote.launch(country="us") == "B"
    assert seen[-1] == ("cloud", None, {"country": "us"})
    assert clearcote.launch(cloud=False, headless=True, api_key="k", api_url="u") == "L"
    # api_key/api_url only choose the cloud account; a local launch drops them instead of failing
    assert seen[-1] == ("local", {"headless": True})
    monkeypatch.delenv("CLEARCOTE_CLOUD")
    assert clearcote.launch() == "L"


def test_nested_local_launches_ignore_the_env(monkeypatch, tmp_path):
    """CLEARCOTE_CLOUD=1 with cloud=False: the inner launch_persistent_context the local path makes
    must stay local, not read the environment again."""
    calls = []

    class Ctx:
        pages = ()

        def on(self, *_a):
            pass

        def new_page(self, **kw):
            return kw

    class Chromium:
        def launch_persistent_context(self, udd, **kw):
            calls.append(udd)
            return Ctx()

    class PW:
        chromium = Chromium()

    exe = tmp_path / ("chrome.exe" if os.name == "nt" else "chrome")
    exe.write_bytes(b"\x00")
    monkeypatch.setenv("CLEARCOTE_CLOUD", "1")
    monkeypatch.setattr(clearcote, "launch_cloud", lambda *a, **k: pytest.fail("went to the cloud"))
    monkeypatch.setattr(clearcote, "_playwright", lambda: PW())
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "_acquire_lease_from_kwargs", lambda kw: None)  # no licence lookups
    clearcote.launch(cloud=False, executable_path=str(exe), headless=False, quiet=True)
    assert len(calls) == 1


def test_api_key_is_required(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="CLEARCOTE_API_KEY"):
        Cloud()
    with pytest.raises(ValueError, match="CLEARCOTE_API_KEY"):
        clearcote.launch(cloud=True, country="us")


def test_base_url_default_env_and_arg(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_API_URL", raising=False)
    assert Cloud(api_key="k").base_url == "https://www.clearcotelabs.com"
    monkeypatch.setenv("CLEARCOTE_API_URL", "http://127.0.0.1:8480/")
    assert Cloud(api_key="k").base_url == "http://127.0.0.1:8480"
    assert Cloud(api_key="k", base_url="https://staging.example").base_url == "https://staging.example"
    assert "cc_live_secret" not in repr(Cloud(api_key="cc_live_secret"))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8480", "http://localhost:3000/", "http://[::1]:8480",
                                 "HTTP://LOCALHOST", "https://www.clearcotelabs.com", "https://10.0.0.5"])
def test_base_url_accepted(url):
    assert Cloud(api_key="k", base_url=url).base_url == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://www.clearcotelabs.com", "http://10.0.0.5:8480", "http://127.0.0.2",
                                 "http://localhost.evil.com", "http://127.0.0.1.nip.io", "http://[::2]"])
def test_plain_http_is_refused_off_this_machine(url, monkeypatch):
    with pytest.raises(ValueError, match="unencrypted"):
        Cloud(api_key="k", base_url=url)
    monkeypatch.setenv("CLEARCOTE_API_URL", url)
    with pytest.raises(ValueError, match="unencrypted"):
        clearcote.launch(cloud=True, api_key="k")


@pytest.mark.parametrize("url", ["ftp://example.com", "www.clearcotelabs.com", "https://", "https:example.com"])
def test_base_url_must_be_a_web_url(url):
    with pytest.raises(ValueError, match="must start with https://"):
        Cloud(api_key="k", base_url=url)


# ── option mapping ────────────────────────────────────────────────────────────────────────────────

def test_every_documented_option_maps_to_its_api_field():
    body = session_body({
        "fingerprint": "seed-1", "platform": "windows", "brand": "Chrome", "timezone": "Europe/Amsterdam",
        "accept_language": "nl-NL", "geoip": False, "headless": False, "light_stealth": True,
        "country": "us", "state": "ca", "city": "los angeles", "proxy_session": "sticky-1",
        "timeout_sec": 600, "idle_timeout_sec": 120, "max_gb": 0.5, "version": "153", "profile": "acct-1",
        "url": "https://example.com", "adblock": True, "solve_sliders": False, "keep_alive": True, "record": True,
        "note": "n", "worker": "w1", "identity": "acct-1", "proxy": "managed",
    })
    assert body == {
        "fingerprint": "seed-1", "platform": "windows", "brand": "Chrome", "timezone": "Europe/Amsterdam",
        "locale": "nl-NL", "geoip": False, "headless": False, "lightStealth": True, "country": "us",
        "state": "ca", "city": "los angeles", "proxySession": "sticky-1", "timeoutSec": 600,
        "idleTimeoutSec": 120, "maxGb": 0.5, "version": "153", "profile": "acct-1",
        "url": "https://example.com", "adblock": True, "solveSliders": False, "keepAlive": True, "record": True,
        "note": "n", "worker": "w1", "identity": "acct-1", "proxy": "managed",
    }


def test_none_values_are_left_out():
    assert session_body({"country": None, "record": None, "note": "x"}) == {"note": "x"}


def test_locale_alias_conflict():
    assert session_body({"locale": "de-DE"}) == {"locale": "de-DE"}
    with pytest.raises(ValueError, match="locale or accept_language"):
        session_body({"locale": "de-DE", "accept_language": "en-US"})


@pytest.mark.parametrize("given,sent", [
    ("managed", "managed"),
    ("http://us%40er:p%3Ass@proxy.example:3128", {"server": "http://proxy.example:3128", "username": "us@er", "password": "p:ss"}),
    ("socks5://proxy.example:1080", {"server": "socks5://proxy.example:1080"}),
    ({"server": "http://proxy.example:8080", "username": "u", "password": "p"},
     {"server": "http://proxy.example:8080", "username": "u", "password": "p"}),
    ({"server": "http://a:b@proxy.example:8080"}, {"server": "http://proxy.example:8080", "username": "a", "password": "b"}),
    ({"server": "http://proxy.example:8080", "bypass": None}, {"server": "http://proxy.example:8080"}),
])
def test_proxy_forms(given, sent):
    assert session_body({"proxy": given}) == {"proxy": sent}


def test_proxy_bypass_and_junk_are_refused():
    with pytest.raises(ValueError, match="proxy.bypass is not available for cloud browsers"):
        session_body({"proxy": {"server": "http://p:1", "bypass": "*.local"}})
    with pytest.raises(ValueError, match="proxy must be"):
        session_body({"proxy": 8080})


def test_profile_forms():
    assert session_body({"profile": "acct-1"}) == {"profile": "acct-1"}
    assert session_body({"profile": {"name": "acct-1", "persist": True}}) == {"profile": {"name": "acct-1", "persist": True}}
    with pytest.raises(ValueError, match='profile="auto"'):
        session_body({"profile": "auto"})
    with pytest.raises(ValueError, match="saved local Profile"):
        session_body({"profile": clearcote.Profile("p", {"fingerprint": "x"})})
    with pytest.raises(ValueError, match="profile.seed is not a cloud profile field"):
        session_body({"profile": {"name": "a", "seed": 1}})


@pytest.mark.parametrize("name", ["executable_path", "args", "extensions", "ignore_default_args", "gpu_vendor",
                                  "hardware_concurrency", "webrtc_ip", "agent_llm_key", "license_key", "widevine",
                                  "ephemeral_profile", "slowmo_typo", "devtools", "env"])
def test_local_only_options_are_refused_by_name(name):
    with pytest.raises(ValueError, match=rf"^{name} is not available for cloud browsers$"):
        session_body({name: "x"})


def test_user_data_dir_points_at_profile():
    with pytest.raises(ValueError, match=r"user_data_dir is not available for cloud browsers.*profile="):
        session_body({"user_data_dir": "/tmp/p"})


def test_every_local_launch_option_is_classified():
    """A persona or agent switch added to the local launch must be either mapped for the cloud or
    listed as local-only, so a cloud launch never silently drops it."""
    known = set(cloud.SESSION_FIELDS) | set(cloud.LOCAL_ONLY_OPTIONS)
    assert set(FINGERPRINT_KEYS) | set(AGENT_KEYS) <= known
    for k in ("executable_path", "args", "user_data_dir", "extensions", "ignore_default_args"):
        assert k in cloud.LOCAL_ONLY_OPTIONS
    assert not set(cloud.SESSION_FIELDS) & set(cloud.LOCAL_ONLY_OPTIONS)


def test_launch_rejects_local_options_before_any_request(api):
    with pytest.raises(ValueError, match="executable_path is not available for cloud browsers"):
        clearcote.launch(cloud=True, executable_path="/opt/chrome")
    with pytest.raises(ValueError, match="user_data_dir"):
        clearcote.launch_persistent_context("/tmp/x", cloud=True, profile="p")
    with pytest.raises(ValueError, match='needs profile="name"'):
        clearcote.launch_persistent_context(cloud=True)
    assert api.log == []


def test_local_persistent_context_still_needs_a_dir(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_CLOUD", raising=False)
    with pytest.raises(TypeError, match="needs a user_data_dir"):
        clearcote.launch_persistent_context()


# ── launch(cloud=True) with a stand-in Playwright ─────────────────────────────────────────────────

class _FakeContext:
    def __init__(self):
        self.pages = []
        self.handlers = []

    def on(self, event, fn):
        self.handlers.append(event)

    def add_init_script(self, *_a):
        pass


class _FakeBrowser:
    def __init__(self):
        self.contexts = [_FakeContext()]
        self.closed = False

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return kw

    def close(self):
        self.closed = True

    def on(self, *_a):
        pass


@pytest.fixture
def fake_pw(monkeypatch):
    seen = {}

    class Chromium:
        def connect_over_cdp(self, url, **kw):
            seen["url"], seen["kw"] = url, kw
            if seen.get("fail"):
                raise RuntimeError("connect refused")
            seen["browser"] = _FakeBrowser()
            return seen["browser"]

    class PW:
        chromium = Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: PW())
    return seen


def test_cloud_launch_creates_connects_and_closes(api, fake_pw):
    api.connect_url = "wss://w1.example/v1/connect/bs_1?token=t"
    b = clearcote.launch(cloud=True, country="us", identity="acct-1", timeout=5000, slow_mo=10)
    assert fake_pw["url"] == api.connect_url
    assert fake_pw["kw"] == {"timeout": 5000, "slow_mo": 10}
    assert api.requests("POST", "/api/v1/browsers")[0]["body"] == {"country": "us", "identity": "acct-1"}
    assert b.cloud_session["id"] == "bs_1" and "connectUrl" not in b.cloud_session
    # new pages/contexts default to no emulated viewport, as a local launch does
    assert b.new_page() == {"no_viewport": True}
    b.close()
    assert b.closed and api.requests("DELETE", "/api/v1/browsers/bs_1")


def test_cloud_launch_sends_the_key_and_user_agent(api, fake_pw):
    clearcote.launch(cloud=True, api_key=API_KEY, api_url=api.url)
    h = api.requests("POST", "/api/v1/browsers")[0]["headers"]
    assert h["authorization"] == f"Bearer {API_KEY}"
    assert h["user-agent"] == f"clearcote-sdk-python/{clearcote.__version__}"
    assert h["content-type"] == "application/json"


def test_cloud_launch_failure_to_connect_ends_the_session(api, fake_pw):
    fake_pw["fail"] = True
    with pytest.raises(RuntimeError, match="connect refused"):
        clearcote.launch(cloud=True)
    assert api.requests("DELETE", "/api/v1/browsers/bs_1")


def test_cloud_launch_failure_after_connecting_disconnects_and_ends_the_session(api, fake_pw, monkeypatch):
    import clearcote._humanize as hz

    def boom(*_a, **_k):
        raise RuntimeError("page crashed")
    monkeypatch.setattr(hz, "install_humanize_on_context", boom)
    with pytest.raises(RuntimeError, match="page crashed"):
        clearcote.launch(cloud=True, humanize=True, keep_alive=True)
    assert fake_pw["browser"].closed  # the CDP connection was closed
    assert api.requests("DELETE", "/api/v1/browsers/bs_1")  # and the session stopped, keep_alive or not


def test_keep_alive_session_is_left_running_on_close(api, fake_pw):
    b = clearcote.launch(cloud=True, keep_alive=True)
    b.close()
    assert not api.requests("DELETE")


def test_cloud_launch_installs_humanize_like_a_local_launch(api, fake_pw, monkeypatch):
    seen = []
    import clearcote._humanize as hz
    monkeypatch.setattr(hz, "install_humanize", lambda b, h, s, seed=None: seen.append(("browser", h, s, seed)))
    monkeypatch.setattr(hz, "install_humanize_on_context",
                        lambda c, h, s, b=None, seed=None: seen.append(("context", h, s, seed)))
    clearcote.launch(cloud=True, humanize=True, fingerprint="seed-7")
    assert ("context", True, False, "seed-7") in seen and ("browser", True, False, "seed-7") in seen
    seen.clear()
    clearcote.launch(cloud=True, humanize=True, show_cursor=True, identity="acct-9")
    assert ("browser", True, True, "acct-9") in seen  # the identity seeds the motor persona


def test_persistent_cloud_context(api, fake_pw):
    ctx = clearcote.launch_persistent_context(cloud=True, profile="acct-1")
    assert api.requests("POST", "/api/v1/browsers")[0]["body"]["profile"] == {"name": "acct-1", "persist": True}
    assert ctx is fake_pw["browser"].contexts[0]
    assert ctx.cloud_session["id"] == "bs_1"
    ctx.close()
    assert fake_pw["browser"].closed and api.requests("DELETE", "/api/v1/browsers/bs_1")
    clearcote.launch_persistent_context(cloud=True, profile={"name": "acct-2", "persist": False})
    assert api.requests("POST", "/api/v1/browsers")[1]["body"]["profile"] == {"name": "acct-2", "persist": False}


async def test_async_cloud_launch(api, monkeypatch):
    seen = {}

    class B:
        contexts = ()

        async def close(self):
            seen["closed"] = True

        def on(self, *_a):
            pass

        async def new_page(self, **kw):
            return kw

        async def new_context(self, **kw):
            return kw

    class Chromium:
        async def connect_over_cdp(self, url, **kw):
            seen["url"] = url
            return B()

    class PW:
        chromium = Chromium()

        async def stop(self):
            seen["stopped"] = True

    async def start():
        return PW()

    monkeypatch.setattr(async_api, "_start_driver", start)
    b = await async_api.launch(cloud=True, country="de")
    assert seen["url"] == api.connect_url
    assert await b.new_page() == {"no_viewport": True}
    await b.close()
    assert seen["closed"] and seen["stopped"]
    assert api.requests("DELETE", "/api/v1/browsers/bs_1")


async def test_async_cloud_launch_failures_leave_nothing_running(api, monkeypatch):
    seen = {"stopped": 0, "closed": 0}

    async def no_driver():
        raise RuntimeError("playwright is not installed")
    monkeypatch.setattr(async_api, "_start_driver", no_driver)
    with pytest.raises(RuntimeError, match="not installed"):
        await async_api.launch(cloud=True)
    assert not api.requests("POST")  # the driver is started first: no session was created

    class PW:
        class chromium:
            @staticmethod
            async def connect_over_cdp(url, **kw):
                raise RuntimeError("connect refused")

        async def stop(self):
            seen["stopped"] += 1

    async def start():
        return PW()
    monkeypatch.setattr(async_api, "_start_driver", start)
    with pytest.raises(RuntimeError, match="connect refused"):
        await async_api.launch(cloud=True)
    assert seen["stopped"] == 1 and api.requests("DELETE", "/api/v1/browsers/bs_1")

    class B:
        contexts = ()

        async def close(self):
            seen["closed"] += 1

        def on(self, *_a):
            pass

        async def new_page(self, **kw):
            return kw

        async def new_context(self, **kw):
            return kw

    class PW2(PW):
        class chromium:
            @staticmethod
            async def connect_over_cdp(url, **kw):
                return B()

    async def start2():
        return PW2()

    async def boom(*_a, **_k):
        raise RuntimeError("humanize failed")
    import clearcote._humanize_async as hz
    monkeypatch.setattr(async_api, "_start_driver", start2)
    monkeypatch.setattr(hz, "install_humanize", boom)
    with pytest.raises(RuntimeError, match="humanize failed"):
        await async_api.launch(cloud=True, humanize=True)
    assert seen == {"stopped": 2, "closed": 1}
    assert api.requests("DELETE", "/api/v1/browsers/bs_2")


# ── errors ───────────────────────────────────────────────────────────────────────────────────────

def test_cloud_error_carries_status_code_and_message(api):
    with pytest.raises(CloudError) as e:
        Cloud(api_key="wrong").browsers.get("bs_1")
    assert (e.value.status, e.value.code, e.value.message) == (401, None, "Missing or invalid API key.")
    assert str(e.value) == "Missing or invalid API key."
    with pytest.raises(CloudError) as e:
        Cloud().profiles.import_cookies("busy", [{"name": "a", "value": "b", "domain": "x.com"}])
    assert (e.value.status, e.value.code) == (409, "PROFILE_IN_USE")
    assert e.value.message == "A live session is saving to this profile."


def test_cloud_error_for_a_non_json_answer(api):
    api.flaky = 99
    with pytest.raises(CloudError) as e:
        Cloud().runs.get("bs_run1")
    assert e.value.status == 503 and e.value.message == "upstream restarting"


def _one_shot_server(answer):
    """A raw TCP server that reads one request and writes ``answer`` (bytes), then hangs up."""
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                conn.recv(65536)
                conn.sendall(answer)
    threading.Thread(target=serve, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.getsockname()[1]}"


def _http_answer(status_line, body, length=None):
    lines = [status_line, "Content-Type: application/json", f"Content-Length: {len(body) if length is None else length}"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def test_cloud_error_for_a_connection_cut_mid_answer():
    # the headers promise 500 bytes, then the server hangs up after 6: http.client.IncompleteRead
    srv, url = _one_shot_server(_http_answer("HTTP/1.1 200 OK", b'{"id":', length=500))
    try:
        with pytest.raises(CloudError) as e:
            Cloud(api_key="k", base_url=url).runs.get("bs_1")
        assert (e.value.status, e.value.code) == (0, "NETWORK")
    finally:
        srv.close()


def test_cloud_error_from_a_nested_error_object():
    body = b'{"error":{"message":"Balance too low.","code":"INSUFFICIENT_BALANCE"}}'
    srv, url = _one_shot_server(_http_answer("HTTP/1.1 402 Payment Required", body))
    try:
        with pytest.raises(CloudError) as e:
            Cloud(api_key="k", base_url=url).browsers.create()
        assert (e.value.status, e.value.code, e.value.message) == (402, "INSUFFICIENT_BALANCE", "Balance too low.")
    finally:
        srv.close()


def test_cloud_error_when_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(CloudError) as e:
        Cloud(api_key="k", base_url=f"http://127.0.0.1:{port}").browsers.list()
    assert e.value.status == 0 and e.value.code == "NETWORK"


# ── browsers ─────────────────────────────────────────────────────────────────────────────────────

def test_browsers_resource(client, api):
    created = client.browsers.create(country="us", record=True)
    assert created["id"] == "bs_1" and created["connectUrl"]
    assert api.log[-1]["body"] == {"country": "us", "record": True}
    assert client.browsers.get("bs_1")["status"] == "active"
    assert client.browsers.list(status=["active", "ended"], limit=5)["balanceEur"] == 12.5
    assert api.log[-1]["query"] == "status=active%2Cended&limit=5"
    assert client.browsers.stop("bs_1")["status"] == "ended"
    assert client.browsers.live("bs_1", control=True)["interactive"] is True
    assert api.log[-1]["query"] == "control=1"
    assert client.browsers.live("bs_1")["interactive"] is False
    share = client.browsers.share("bs_1", recording=True, minutes=30)
    assert "/replay/" in share["url"] and api.log[-1]["body"] == {"minutes": 30, "recording": True}
    with pytest.raises(ValueError, match="humanize is not available"):
        client.browsers.create(humanize=True)


def test_handoff_request_wait_and_done(client, api):
    h = client.browsers.handoff("bs_1", reason="solve the captcha", timeout_sec=300)
    assert h == {"state": "waiting", "reason": "solve the captcha", "since": "2026-10-02T10:00:00.000Z",
                 "expiresAt": "2026-10-02T10:10:00.000Z", "liveUrl": LIVE}
    assert api.log[-1]["body"] == {"reason": "solve the captcha", "timeoutSec": 300}
    view = client.browsers.wait_handoff("bs_1", poll=0.01)
    assert view["handoff"]["state"] == "done"
    assert len(api.requests("GET", "/api/v1/browsers/bs_1")) == 3  # waiting, waiting, done
    assert client.browsers.handoff_done("bs_1")["state"] == "done"
    assert api.log[-1]["path"] == "/api/v1/browsers/bs_1/handoff/done"


def test_wait_handoff_timeout(client, api):
    api.handoff_polls = 10_000
    client.browsers.handoff("bs_1")
    with pytest.raises(CloudTimeoutError) as e:
        client.browsers.wait_handoff("bs_1", timeout=0.05, poll=0.01)
    assert e.value.last["handoff"]["state"] == "waiting"


def test_wait_handoff_without_a_handoff_is_an_error(client, api):
    # handoff: null reads as "not waiting"; returning at once would look like a finished hand-off
    with pytest.raises(CloudError) as e:
        client.browsers.wait_handoff("bs_7", poll=0.01)
    assert (e.value.status, e.value.code) == (200, "NO_HANDOFF")
    assert "no hand-off was requested for session bs_7" in e.value.message
    assert len(api.requests("GET", "/api/v1/browsers/bs_7")) == 1
    # a hand-off that is already over (done, or timed out) still returns at once
    api.handoff_polls = 0
    client.browsers.handoff("bs_8")
    assert client.browsers.wait_handoff("bs_8", poll=0.01)["handoff"]["state"] == "done"


async def test_async_wait_handoff_without_a_handoff_is_an_error(api):
    c = AsyncCloud()
    with pytest.raises(CloudError, match="no hand-off was requested for session bs_7"):
        await c.browsers.wait_handoff("bs_7", poll=0.01)
    await c.browsers.handoff("bs_8")
    assert (await c.browsers.wait_handoff("bs_8", poll=0.01))["handoff"]["state"] == "done"


def test_events_paging(client, api):
    page = client.browsers.events("bs_1")
    assert [e["seq"] for e in page["events"]] == [1, 2] and page["next"] == 2
    page = client.browsers.events("bs_1", after=page["next"], limit=10)
    assert [e["seq"] for e in page["events"]] == [3, 4] and page["next"] is None
    assert api.log[-1]["query"] == "after=2&limit=10"


def test_recording_url_download_and_errors(client, api, tmp_path):
    url = client.browsers.recording_url("bs_1")
    assert url == f"{api.url}/dev/blob/rec.mp4?sig=abc"
    out = client.browsers.download_recording("bs_1", str(tmp_path / "r.mp4"))
    with open(out, "rb") as fh:
        assert fh.read() == RECORDING
    blob = api.requests("GET", "/dev/blob/rec.mp4")[-1]
    assert "authorization" not in blob["headers"]  # the presigned URL never gets the API key
    api.recording_state = "processing"
    with pytest.raises(CloudError) as e:
        client.browsers.recording_url("bs_1")
    assert (e.value.status, e.value.code) == (409, "NOT_READY")
    with pytest.raises(CloudError) as e:
        client.browsers.download_recording("bs_unrecorded", str(tmp_path / "x.mp4"))
    assert e.value.status == 404 and not (tmp_path / "x.mp4").exists()


def test_recording_url_must_be_a_web_url(client, api, tmp_path):
    secret_file = tmp_path / "local.txt"
    secret_file.write_text("local data")
    api.recording_location = secret_file.as_uri()
    with pytest.raises(CloudError, match="did not answer with a recording URL"):
        client.browsers.download_recording("bs_1", str(tmp_path / "r.mp4"))
    assert not (tmp_path / "r.mp4").exists()


def test_recording_on_another_host_never_gets_the_key(client, api, tmp_path):
    storage = start_fake_cloud()
    try:
        api.recording_location = f"{storage.url}/dev/blob/rec.mp4?sig=abc"
        assert client.browsers.recording_url("bs_1") == api.recording_location
        assert not storage.log  # the SDK did not follow the API redirect with its key
        out = client.browsers.download_recording("bs_1", str(tmp_path / "r.mp4"))
        with open(out, "rb") as fh:
            assert fh.read() == RECORDING
        (blob,) = storage.log
        assert blob["path"] == "/dev/blob/rec.mp4" and blob["query"] == "sig=abc"
        assert "authorization" not in blob["headers"]
        assert all(r["headers"].get("authorization") == f"Bearer {API_KEY}" for r in api.log)
    finally:
        stop_fake_cloud(storage)


# ── runs ─────────────────────────────────────────────────────────────────────────────────────────

def test_run_create_maps_and_polls_to_the_end(client, api):
    updates = []
    schema = {"type": "object", "properties": {"price": {"type": "string"}}}
    run = client.runs.create("Find the price", url="https://example.com/", schema=schema,
                             secrets={"pw": {"value": "hunter2", "domains": ["example.com"]}},
                             max_steps=20, handoff=True, handoff_timeout_sec=120, record=True, country="us",
                             poll=0.01, on_update=updates.append)
    assert api.requests("POST", "/api/v1/runs")[0]["body"] == {
        "task": "Find the price", "url": "https://example.com/", "schema": schema,
        "secrets": {"pw": {"value": "hunter2", "domains": ["example.com"]}},
        "maxSteps": 20, "handoff": True, "handoffTimeoutSec": 120, "record": True, "country": "us"}
    assert run["status"] == "succeeded" and run["result"]["output"] == {"plan": "Starter", "price": "9.99"}
    assert [u["status"] for u in updates] == ["queued", "running", "waiting_for_human", "running", "succeeded"]


def test_waiting_for_human_is_reported_without_a_callback(client, api, capsys):
    client.runs.create("t", poll=0.01)
    err = capsys.readouterr().err
    assert f"run bs_run1 is waiting for a human (needs a login): {LIVE}" in err


def test_run_without_wait_returns_the_create_answer(client, api):
    created = client.runs.create("t", wait=False)
    assert created["status"] == "queued" and not api.requests("GET")


def test_run_wait_timeout_keeps_the_last_view(client, api):
    api.run_statuses = ["running"]
    with pytest.raises(CloudTimeoutError) as e:
        client.runs.wait("bs_run1", timeout=0.05, poll=0.01)
    assert e.value.last["status"] == "running" and "bs_run1" in str(e.value)


def test_run_wait_retries_transient_errors(client, api):
    api.run_statuses = ["succeeded"]
    api.flaky = 2
    assert client.runs.wait("bs_run1", poll=0.01)["status"] == "succeeded"


def test_run_rejects_local_options_and_server_rules(client, api):
    with pytest.raises(ValueError, match="args is not available for cloud runs"):
        client.runs.create("t", args=["--x"])
    with pytest.raises(CloudError, match="keepAlive does not apply to runs"):
        client.runs.create("t", keep_alive=True)


def test_runs_get_list_cancel(client, api):
    assert client.runs.get("bs_run1")["status"] == "queued"
    assert client.runs.list(limit=5)["runs"][0]["id"] == "bs_run1"
    assert api.log[-1]["query"] == "limit=5"
    assert client.runs.cancel("bs_run1")["status"] == "cancelled"
    assert api.log[-1]["method"] == "DELETE"


async def test_async_cloud_runs_and_callbacks(api):
    updates = []

    async def on_update(view):
        updates.append(view["status"])

    c = AsyncCloud()
    run = await c.runs.create("t", poll=0.01, on_update=on_update)
    assert run["status"] == "succeeded" and updates[-1] == "succeeded"
    assert (await c.browsers.get("bs_1"))["id"] == "bs_1"
    assert (await c.webhooks.list())["webhooks"]
    api.handoff_polls = 1
    await c.browsers.handoff("bs_1")
    assert (await c.browsers.wait_handoff("bs_1", poll=0.01))["handoff"]["state"] == "done"


# ── profiles ─────────────────────────────────────────────────────────────────────────────────────

STATE = {
    "cookies": [
        {"name": "sid", "value": "1", "domain": ".example.com", "path": "/", "expires": -1, "httpOnly": True,
         "secure": True, "sameSite": "Lax"},
        {"name": "pref", "value": "2", "domain": "www.example.com", "path": "/", "expires": 1893456000,
         "httpOnly": False, "secure": False, "sameSite": "None"},
        {"name": "x", "value": "3", "domain": "badexample.com", "path": "/", "expires": -1, "httpOnly": False,
         "secure": False, "sameSite": "Lax"},
        {"name": "t", "value": "4", "domain": ".tracker.net", "path": "/", "expires": -1, "httpOnly": False,
         "secure": False, "sameSite": "Lax", "size": 5, "priority": "Medium", "sourceScheme": "Secure"},
    ],
    "origins": [],
}


def test_cookie_domain_filter():
    names = [c["name"] for c in filter_cookies(STATE["cookies"], ["example.com"])]
    assert names == ["sid", "pref"]  # .example.com + www.example.com, never badexample.com
    assert [c["name"] for c in filter_cookies(STATE["cookies"], [".EXAMPLE.com "])] == ["sid", "pref"]
    # www.example.com also gets the .example.com cookie: the browser sends it there
    assert [c["name"] for c in filter_cookies(STATE["cookies"], ["www.example.com"])] == ["sid", "pref"]
    assert [c["name"] for c in filter_cookies(STATE["cookies"], ["tracker.net", "nope.org"])] == ["t"]
    assert filter_cookies(STATE["cookies"], []) == []


def _domains(cookie_domains, allowed):
    cookies = [{"name": f"c{i}", "value": "v", "domain": d} for i, d in enumerate(cookie_domains)]
    return [c["domain"] for c in filter_cookies(cookies, allowed)]


def test_cookie_domain_filter_parent_and_sub_domains():
    jar = ["www.example.com", ".example.com", "example.com", "a.www.example.com", "other.example.com",
           ".com", "com", "badexample.com", "www.badexample.com", "example.com.evil.net", ".co.uk", "localhost"]
    # the host itself, its parents (with a dot of their own) and its subdomains; never a sibling,
    # a look-alike or a bare suffix
    assert _domains(jar, ["www.example.com"]) == [
        "www.example.com", ".example.com", "example.com", "a.www.example.com"]
    assert _domains(jar, ["example.com"]) == [
        "www.example.com", ".example.com", "example.com", "a.www.example.com", "other.example.com"]
    assert _domains(jar, [".WWW.Example.com"]) == _domains(jar, ["www.example.com"])
    assert _domains(jar, ["shop.example.co.uk"]) == [".co.uk"]  # no PSL: a two-label parent is kept
    assert _domains(jar, ["localhost"]) == ["localhost"]
    assert _domains(jar, ["evil.net"]) == ["example.com.evil.net"]
    assert _domains(jar, ["net"]) == ["example.com.evil.net"]  # an explicit choice of a whole TLD


def test_storage_state_parsing():
    assert cookies_from_state(STATE) == STATE["cookies"]
    assert cookies_from_state(STATE["cookies"]) == STATE["cookies"]
    assert cookies_from_state({"cookies": []}) == []
    with pytest.raises(ValueError, match="storage state"):
        cookies_from_state({"origins": []})
    with pytest.raises(ValueError, match="name and a domain"):
        cookies_from_state([{"value": "x"}])


def test_profile_resource(client, api):
    assert client.profiles.list()["profiles"][0]["name"] == "acct-1"
    r = client.profiles.import_cookies("acct 1/x", [{"name": "a", "value": "1", "domain": "a.com"}])
    assert api.log[-1]["path"] == "/api/v1/browsers/profiles/acct%201%2Fx/cookies"
    assert api.log[-1]["body"] == {"cookies": [{"name": "a", "value": "1", "domain": "a.com"}], "mode": "merge"}
    assert r["imported"] == 1
    assert client.profiles.get("acct 1/x")["cookies"] == 1
    assert client.profiles.delete("acct-1") == {"ok": True}


def test_sync_from_file_uploads_only_the_chosen_domains(client, api, tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps(STATE))
    res = client.profiles.sync("acct-1", from_file=str(f), domains=["example.com"], replace=True)
    sent = api.requests("PUT")[-1]["body"]
    assert sent["mode"] == "replace"
    assert sent["cookies"] == [
        {"name": "sid", "value": "1", "domain": ".example.com", "path": "/", "expires": -1, "httpOnly": True,
         "secure": True, "sameSite": "Lax"},
        {"name": "pref", "value": "2", "domain": "www.example.com", "path": "/", "expires": 1893456000,
         "httpOnly": False, "secure": False, "sameSite": "None"}]
    assert res == {"name": "acct-1", "cookies": 2, "imported": 2, "domains": ["example.com", "www.example.com"],
                   "bytes": 1234, "updatedAt": "2026-10-02T10:00:00.000Z"}
    client.profiles.sync("acct-1", from_file=str(f), all_domains=True)
    sent = api.requests("PUT")[-1]["body"]
    assert sent["mode"] == "merge" and len(sent["cookies"]) == 4
    assert "size" not in sent["cookies"][3]  # CDP-only fields are not uploaded


def test_sync_refuses_without_a_domain_choice(client, api, tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps(STATE))
    with pytest.raises(ValueError, match="all_domains=True"):
        client.profiles.sync("acct-1", from_file=str(f))
    with pytest.raises(ValueError, match="not both"):
        client.profiles.sync("acct-1", from_file=str(f), domains=["a.com"], all_domains=True)
    with pytest.raises(ValueError, match="exactly one"):
        client.profiles.sync("acct-1", from_file=str(f), from_cdp="http://127.0.0.1:1", domains=["a.com"])
    with pytest.raises(ValueError, match="no cookies found for nothing.example"):
        client.profiles.sync("acct-1", from_file=str(f), domains=["nothing.example"])
    assert api.log == []


# ── webhooks ─────────────────────────────────────────────────────────────────────────────────────

def test_webhooks_resource(client, api):
    hook = client.webhooks.create("https://hooks.example.com/x", events=["run.finished"], description="d")
    assert hook["secret"] == "whsec_test_secret"
    assert api.log[-1]["body"] == {"url": "https://hooks.example.com/x", "events": ["run.finished"], "description": "d"}
    client.webhooks.create("https://hooks.example.com/y")
    assert api.log[-1]["body"] == {"url": "https://hooks.example.com/y"}
    assert [h["id"] for h in client.webhooks.list()["webhooks"]] == ["wh_1", "wh_2"]
    assert client.webhooks.delete("wh_1") == {"ok": True}
    assert client.webhooks.test("wh_2")["ok"] is True
    assert api.log[-1]["path"] == "/api/v1/webhooks/wh_2/test"


# ── verify_webhook ───────────────────────────────────────────────────────────────────────────────

SECRET = "whsec_test_secret"
BODY = '{"id":"evt_1","type":"run.finished","createdAt":"2026-10-02T10:00:00.000Z","data":{"id":"bs_run1","status":"succeeded"}}'
NOW = 1_790_000_000


def sign(body, t=NOW, secret=SECRET):
    mac = hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def test_webhook_known_vector():
    # A fixed vector, hardcoded in the Node tests too: both SDKs agree on the exact bytes signed.
    vector = "t=1790000000,v1=e7f51b8aaf2b3c2682ae9fa9dcf7d2c08b4c0f624cf5e55250685f75bdcaf419"
    assert sign(BODY) == vector
    assert verify_webhook(BODY, vector, SECRET, now=NOW)["data"]["status"] == "succeeded"
    assert verify_webhook(BODY.encode(), sign(BODY), SECRET, now=NOW + 299)["id"] == "evt_1"


def test_webhook_tampered_and_wrong_secret():
    with pytest.raises(ValueError, match="does not match"):
        verify_webhook(BODY.replace("succeeded", "failed"), sign(BODY), SECRET, now=NOW)
    with pytest.raises(ValueError, match="does not match"):
        verify_webhook(BODY, sign(BODY, secret="whsec_other"), SECRET, now=NOW)
    with pytest.raises(ValueError, match="does not match"):
        verify_webhook(BODY, sign(BODY).replace(f"t={NOW}", f"t={NOW + 1}"), SECRET, now=NOW)


def test_webhook_expired_and_future():
    with pytest.raises(ValueError, match="tolerance"):
        verify_webhook(BODY, sign(BODY), SECRET, now=NOW + 301)
    with pytest.raises(ValueError, match="tolerance"):
        verify_webhook(BODY, sign(BODY), SECRET, now=NOW - 301)
    assert verify_webhook(BODY, sign(BODY), SECRET, tolerance_sec=None, now=NOW + 10**6)["id"] == "evt_1"
    assert verify_webhook(BODY, sign(BODY), SECRET, tolerance_sec=600, now=NOW + 500)["id"] == "evt_1"


def test_webhook_multiple_v1_and_spacing():
    good = sign(BODY).split("v1=")[1]
    header = f"t={NOW}, v1={'0' * 64}, v1={good.upper()}"
    assert verify_webhook(BODY, header, SECRET, now=NOW)["type"] == "run.finished"
    with pytest.raises(ValueError, match="does not match"):
        verify_webhook(BODY, f"t={NOW},v1={'0' * 64},v1=abc", SECRET, now=NOW)


@pytest.mark.parametrize("header", ["", "v1=abc", f"t={NOW}", f"t=abc,v1={'0' * 64}", "garbage", None])
def test_webhook_malformed_header(header):
    with pytest.raises(ValueError, match="invalid Clearcote-Signature"):
        verify_webhook(BODY, header, SECRET, now=NOW)


@pytest.mark.parametrize("secret", [None, "", "  ", b""])
def test_webhook_needs_a_secret(secret):
    # str(None) == "None": without this check an unset secret verifies a delivery anyone can sign
    forged = sign(BODY, secret="None")
    with pytest.raises(ValueError, match="signing secret"):
        verify_webhook(BODY, forged, secret, now=NOW)


def test_webhook_oversized_header_is_refused_before_any_work():
    header = sign(BODY) + ",v1=" + ",v1=".join(["0" * 64] * 200)
    with pytest.raises(ValueError, match="invalid Clearcote-Signature"):
        verify_webhook(BODY, header, SECRET, now=NOW)
    with pytest.raises(ValueError, match="invalid Clearcote-Signature"):
        verify_webhook(BODY, "t=" + "9" * 100000 + ",v1=" + "0" * 64, SECRET, now=NOW)


def test_exports():
    for name in ("Cloud", "AsyncCloud", "CloudError", "CloudTimeoutError", "verify_webhook"):
        assert hasattr(clearcote, name) and name in clearcote.__all__
    for name in ("AsyncCloud", "CloudError", "verify_webhook"):
        assert hasattr(async_api, name)
    assert asyncio.iscoroutinefunction(async_api.launch)
