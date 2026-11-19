"""Server-side personas (r51): the launcher-side half, against a fake persona service."""
import base64
import gzip
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clearcote import _persona
from clearcote._persona import (PersonaServiceError, apply, apply_to_pw_kwargs, inject_persona_token,
                                selector_from_args)


class FakeLease:
    def __init__(self, token="hdr.sig", lease_id="L1", proxy=None):
        self.token = token
        self.lease_id = lease_id
        self._proxy = proxy


class FakePersonaService:
    """Records every request; answers like cc-persona (status + persona_token/exp or error)."""

    def __init__(self, status=200, exp_in=3600):
        self.requests = []
        self.status = status
        self.exp_in = exp_in
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                server.requests.append((self.path, body))
                code = server.status
                if code == 200:
                    tok = "tok-" + hashlib.sha256(json.dumps(body["sel"], sort_keys=True).encode()).hexdigest()[:12]
                    out = {"persona_token": tok, "exp": 1700000000 + server.exp_in if server.exp_in < 0 else __import__("time").time() + server.exp_in}
                else:
                    out = {"error": f"refused {code}"}
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def service(monkeypatch):
    svc = FakePersonaService()
    monkeypatch.setenv(_persona.API_ENV, svc.base)
    _persona.clear_caches()
    yield svc
    svc.close()
    _persona.clear_caches()


@pytest.fixture
def facts_runner():
    calls = []

    def runner(cmd, env):
        calls.append((cmd, env))
        return 'ignored line\n{"gpu_vendor": "nvidia"}\n'
    runner.calls = calls
    return runner


@pytest.fixture
def exe(tmp_path, monkeypatch):
    p = tmp_path / "chrome"
    p.write_bytes(b"x")
    monkeypatch.setenv("HOME", str(tmp_path))  # the on-disk host-facts cache lands under the test's HOME
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return str(p)


R51 = lambda exe, name: name == "cc-persona-host-facts"  # noqa: E731
OLD = lambda exe, name: False  # noqa: E731


def test_selector_keeps_only_generator_switches_and_last_value():
    sel, prof, out = selector_from_args(["--headless", "--fingerprint=1", "--fingerprint=2", "--lang=en-US",
                                         "--fingerprint-platform=windows", "--canvas-bridge-url=ws://x:1?auth=s",
                                         "--disable-gpu-string-spoof", "--user-data-dir=/tmp/u"])
    assert sel == {"fingerprint": "2", "fingerprint-platform": "windows", "canvas-bridge-url": "1",
                   "disable-gpu-string-spoof": ""}
    assert prof is None
    assert out[-1] == "--user-data-dir=/tmp/u" and "--canvas-bridge-url=ws://x:1?auth=s" in out


def test_profile_leaves_argv_and_becomes_its_sha256():
    profile = {"screen": {"width": 1920}, "navigator": {"userAgent": "Mozilla/5.0"}}
    text = json.dumps(profile)
    b64 = base64.b64encode(gzip.compress(text.encode())).decode()
    sel, prof, out = selector_from_args(["--fingerprint=7", f"--fingerprint-profile={b64}"])
    assert prof == text
    sha = hashlib.sha256(text.encode()).hexdigest()
    assert sel["fingerprint-profile-sha256"] == sha
    assert out == ["--fingerprint=7", f"--fingerprint-profile-sha256={sha}"]


def test_old_engine_is_left_alone(exe):
    args, env = apply(exe, ["--fingerprint=1"], {"A": "1"}, FakeLease(), supports=OLD)
    assert args == ["--fingerprint=1"] and env == {"A": "1"}


def test_free_mode_without_a_lease_is_left_alone(exe):
    args, env = apply(exe, ["--fingerprint=1"], None, None, supports=R51)
    assert args == ["--fingerprint=1"] and env is None


def test_r51_launch_fetches_and_injects_the_token(exe, service, facts_runner):
    lease = FakeLease()
    pw = {"env": {"PATH": "/bin"}}
    args = apply_to_pw_kwargs(exe, ["--headless", "--fingerprint=42", "--fingerprint-platform=linux"], pw, lease,
                              supports=R51, runner=facts_runner)
    assert args == ["--headless", "--fingerprint=42", "--fingerprint-platform=linux"]
    assert pw["env"]["PATH"] == "/bin" and pw["env"][_persona.TOKEN_ENV].startswith("tok-")
    path, body = service.requests[0]
    assert path == "/v1/persona"
    assert body == {"token": "hdr.sig", "sel": {"fingerprint": "42", "fingerprint-platform": "linux"},
                    "host": {"gpu_vendor": "nvidia"}}
    # the host-facts probe ran the engine with only the switches that influence its answer, under the run token
    cmd, env = facts_runner.calls[0]
    assert cmd == [exe, "--cc-persona-host-facts", "--headless"]
    assert env["CLEARCOTE_RUN_TOKEN"] == "hdr.sig" and _persona.TOKEN_ENV not in env


def test_a_callers_own_run_token_counts_as_the_lease(exe, service, facts_runner):
    """The hosted gateway mints its own run tokens and launches with no SDK lease."""
    payload = base64.urlsafe_b64encode(json.dumps({"lic": "lic_g", "lease": "gw-lease-7"}).encode()).decode().rstrip("=")
    env = {"CLEARCOTE_RUN_TOKEN": f"{payload}.sig"}
    args, out = apply(exe, ["--fingerprint=9"], env, None, supports=R51, runner=facts_runner)
    assert out[_persona.TOKEN_ENV].startswith("tok-")
    assert service.requests[0][1]["token"] == f"{payload}.sig"
    assert facts_runner.calls[0][1]["CLEARCOTE_RUN_TOKEN"] == f"{payload}.sig"


def test_seedless_launch_still_fetches_a_default_persona(exe, service, facts_runner):
    pw = {}
    apply_to_pw_kwargs(exe, ["--fingerprint-platform=windows", "--window-size=1,1"], pw, FakeLease(),
                       supports=R51, runner=facts_runner)
    assert service.requests[0][1]["sel"] == {"fingerprint-platform": "windows"}
    assert _persona.TOKEN_ENV in pw["env"]


def test_token_and_host_facts_are_cached_per_lease_and_selector(exe, service, facts_runner):
    lease = FakeLease()
    for _ in range(3):
        apply(exe, ["--fingerprint=5"], {}, lease, supports=R51, runner=facts_runner)
    apply(exe, ["--fingerprint=6"], {}, lease, supports=R51, runner=facts_runner)
    apply(exe, ["--fingerprint=5"], {}, FakeLease(lease_id="L2"), supports=R51, runner=facts_runner)
    assert len(service.requests) == 3  # 5, 6, and 5 on another lease
    assert len(facts_runner.calls) == 1  # the same binary + switches: probed once


def test_profile_is_uploaded_as_its_exact_text(exe, service, facts_runner):
    text = '{"screen":{"width":1366},"b":1.0}'
    b64 = base64.b64encode(gzip.compress(text.encode())).decode()
    args, env = apply(exe, [f"--fingerprint-profile={b64}"], {}, FakeLease(), supports=R51, runner=facts_runner)
    body = service.requests[0][1]
    assert body["profile_json"] == text
    assert body["sel"] == {"fingerprint-profile-sha256": hashlib.sha256(text.encode()).hexdigest()}
    assert args == [f"--fingerprint-profile-sha256={hashlib.sha256(text.encode()).hexdigest()}"]


def test_refusal_and_unreachable_service_raise_clear_errors(exe, facts_runner, monkeypatch):
    svc = FakePersonaService(status=429)
    monkeypatch.setenv(_persona.API_ENV, svc.base)
    _persona.clear_caches()
    try:
        with pytest.raises(PersonaServiceError, match="refused this launch: refused 429"):
            apply(exe, ["--fingerprint=1"], {}, FakeLease(), supports=R51, runner=facts_runner)
    finally:
        svc.close()
    monkeypatch.setenv(_persona.API_ENV, "http://127.0.0.1:9,http://127.0.0.1:10")
    _persona.clear_caches()
    with pytest.raises(PersonaServiceError, match="no persona service answered"):
        apply(exe, ["--fingerprint=1"], {}, FakeLease(), supports=R51, runner=facts_runner,
              fetch=lambda *a, **k: _persona.fetch_persona_token(*a, timeout=0.5, **k))


def test_broken_host_facts_raise(exe, service):
    with pytest.raises(PersonaServiceError, match="host facts"):
        apply(exe, ["--fingerprint=1"], {}, FakeLease(), supports=R51, runner=lambda c, e: "not json")


def test_long_tokens_go_gzipped():
    env = inject_persona_token({}, "a" * 40000)
    assert _persona.TOKEN_ENV not in env
    assert gzip.decompress(base64.b64decode(env[_persona.TOKEN_GZ_ENV])) == b"a" * 40000
    env = inject_persona_token(env, "short")
    assert env == {_persona.TOKEN_ENV: "short"}


def test_selector_list_matches_the_service_and_the_engine():
    """The three lists must stay identical; this reads the other two from the pro repo when it is checked out."""
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.normpath(os.path.join(here, "..", "..", "..", "..", "wt-r51"))
    js = os.path.join(repo, "services", "cc-persona", "src", "selector.mjs")
    cc = os.path.join(repo, "services", "cc-persona", "engine", "persona_token.cc")
    if not (os.path.exists(js) and os.path.exists(cc)):
        pytest.skip("pro repo not checked out next to this one")
    import re
    js_names = set(re.findall(r'^\s*"([a-z0-9-]+)":', open(js, encoding="utf-8").read(), re.M))
    cc_text = open(cc, encoding="utf-8").read()
    cc_block = cc_text[cc_text.index("kSelectorSwitches[]"):cc_text.index("};", cc_text.index("kSelectorSwitches[]"))]
    cc_names = set(re.findall(r'"([a-z0-9-]+)"', cc_block))
    assert cc_names == set(_persona.SELECTOR_SWITCHES)
    # the service handles the library id in code rather than in its switch map
    assert js_names | {"fingerprint-profile-id"} == set(_persona.SELECTOR_SWITCHES)


def test_host_facts_probe_gets_the_launch_env_with_the_run_token_file(exe, service, facts_runner, tmp_path):
    # 0.63.0/0.63.1 on r51: the probe got os.environ + CLEARCOTE_RUN_TOKEN only, the engine refused the FREE token
    # without its file, and every free launch failed before its persona was fetched.
    tok = str(tmp_path / "run.tok")
    env = {"CLEARCOTE_RUN_TOKEN": "hdr.sig", "CLEARCOTE_RUN_TOKEN_FILE": tok, "HOME": str(tmp_path)}
    apply(exe, ["--fingerprint=77"], env, FakeLease(), supports=R51, runner=facts_runner)
    assert len(facts_runner.calls) == 1
    probe_env = facts_runner.calls[0][1]
    assert probe_env["CLEARCOTE_RUN_TOKEN_FILE"] == tok
    assert probe_env["CLEARCOTE_RUN_TOKEN"] == "hdr.sig"


# --- stale host GPU vendor (CR-ZQJ4SX) ----------------------------------------------------------------------------

STALE_MSG = ("BrowserType.launch: Target page, context or browser has been closed\n"
             "[pid=14076][err] [clearcote] no usable persona for this run (generated for another host GPU vendor "
             "(none, this machine: nvidia)). This engine needs a Clearcote SDK, Profile Manager or Helper that "
             "fetches personas (r51+).")


def test_host_facts_are_not_kept_on_disk(exe, service, facts_runner, tmp_path):
    """Up to 0.70.0 a saved answer was never checked again: a "none" saved before the NVIDIA driver (or in a Remote
    Desktop session) failed every later launch. A new process asks the engine again."""
    old = tmp_path / ".clearcote" / "persona-host-facts.json"
    old.parent.mkdir(parents=True, exist_ok=True)
    st = os.stat(exe)
    key = json.dumps([os.path.abspath(exe), st.st_size, int(st.st_mtime), []])
    old.write_text(json.dumps({key: {"gpu_vendor": "none"}}))
    apply(exe, ["--fingerprint=1"], {}, FakeLease(), supports=R51, runner=facts_runner)
    assert len(facts_runner.calls) == 1  # the old file's "none" was not used
    assert service.requests[0][1]["host"] == {"gpu_vendor": "nvidia"}
    assert json.loads(old.read_text()) == {key: {"gpu_vendor": "none"}}  # nor rewritten
    assert sorted(p.name for p in old.parent.iterdir()) == ["persona-host-facts.json"]


def test_stale_host_vendor_reads_the_engine_refusal():
    assert _persona.stale_host_vendor(Exception(STALE_MSG)) == ("none", "nvidia")
    assert _persona.stale_host_vendor(Exception("... (generated for another host GPU vendor (?, this machine: amd))")) == ("?", "amd")
    assert _persona.stale_host_vendor(Exception("no usable persona for this run (not valid yet)")) is None
    assert _persona.stale_host_vendor(Exception("Target page, context or browser has been closed")) is None


def test_heal_fetches_a_persona_for_the_vendor_the_engine_named(exe, service, monkeypatch, capsys):
    calls = []

    def none_runner(cmd, env):  # the probe keeps answering "none": the heal must not depend on it
        calls.append(cmd)
        return '{"gpu_vendor": "none"}'

    lease = FakeLease()
    args = ["--fingerprint=3", "--headless"]
    pw = {"env": {"PATH": "/bin"}}
    apply_to_pw_kwargs(exe, args, pw, lease, supports=R51, runner=none_runner)
    assert len(service.requests) == 1 and service.requests[-1][1]["host"] == {"gpu_vendor": "none"}
    pw["env"].pop(_persona.TOKEN_ENV)

    assert _persona.heal_stale_host_vendor(exe, args, pw, lease, Exception(STALE_MSG), supports=R51, runner=none_runner)
    assert len(service.requests) == 2  # a new token (the cached one was for "none")
    assert service.requests[-1][1]["host"] == {"gpu_vendor": "nvidia"}
    assert pw["env"][_persona.TOKEN_ENV].startswith("tok-") and pw["env"]["PATH"] == "/bin"
    assert len(calls) == 1  # no second probe: the engine's own answer was used
    assert "(none -> nvidia)" in capsys.readouterr().err
    # the rest of this process launches with the corrected answer
    assert _persona.host_facts(exe, args, lease.token, runner=none_runner) == {"gpu_vendor": "nvidia"}


def test_heal_ignores_other_failures(exe, service, facts_runner):
    pw = {}
    assert not _persona.heal_stale_host_vendor(exe, [], pw, FakeLease(), Exception("not valid yet"),
                                               supports=R51, runner=facts_runner)
    assert pw == {} and service.requests == []


def test_launch_retries_once_on_a_stale_host_vendor(monkeypatch):
    import clearcote
    healed, starts = [], []

    def heal(exe, args, pw_kwargs, lease, exc):
        healed.append(str(exc))
        pw_kwargs["env"] = {"T": "new"}
        return _persona.stale_host_vendor(exc) is not None

    monkeypatch.setattr(_persona, "heal_stale_host_vendor", heal)
    pw = {"env": {"T": "old"}}

    def start():
        starts.append(pw["env"]["T"])
        if len(starts) == 1:
            raise RuntimeError(STALE_MSG)
        return "browser"

    assert clearcote._retry_on_stale_host_vendor("exe", [], pw, None, start) == "browser"
    assert starts == ["old", "new"] and len(healed) == 1

    def always_stale():
        raise RuntimeError(STALE_MSG)

    with pytest.raises(RuntimeError):  # once only: a second refusal is raised, not looped on
        clearcote._retry_on_stale_host_vendor("exe", [], {"env": {}}, None, always_stale)

    def other():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        clearcote._retry_on_stale_host_vendor("exe", [], {"env": {}}, None, other)


def test_async_launch_retries_once_on_a_stale_host_vendor(monkeypatch):
    import asyncio
    from clearcote import async_api
    monkeypatch.setattr(_persona, "heal_stale_host_vendor", lambda *a, **k: _persona.stale_host_vendor(a[4]) is not None)
    n = []

    async def start():
        n.append(1)
        if len(n) == 1:
            raise RuntimeError(STALE_MSG)
        return "ctx"

    assert asyncio.run(async_api._retry_on_stale_host_vendor_async("exe", [], {}, None, start)) == "ctx"
    assert len(n) == 2
