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
