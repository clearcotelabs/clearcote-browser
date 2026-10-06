"""`clearcote` CLI (#6): commands present, info never downloads, reports cache/engine support/licence,
login refuses a rejected key, logout, clear-cache only removes build directories (mirrors
sdk/node/test/parity-cli.test.ts)."""
import json
import os
import sys

import pytest

from clearcote import _commands, _license
from clearcote._launchopts import _SWITCH_CACHE
from clearcote.download import list_cached_builds
from clearcote.release import RELEASE

from _proxies import start_origin


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    cache = tmp_path / "cache"
    home.mkdir()
    cache.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLEARCOTE_CACHE", str(cache))
    for k in ("CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_RELEASE_CHANNEL", "CLEARCOTE_LICENSE_API",
              "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_AUTO_UPDATE"):
        monkeypatch.delenv(k, raising=False)
    _SWITCH_CACHE.clear()
    return {"home": home, "cache": cache}


def fake_cached_build(cache, tag, switches=()):
    binary = "chrome.exe" if sys.platform == "win32" else "chrome"
    d = cache / tag / "browser" / "sub"
    d.mkdir(parents=True)
    body = b"x" + b"".join(b"\0" + s.encode() + b"\0" for s in switches)
    (d / binary).write_bytes(body)
    if sys.platform == "win32":
        (d / "chrome.dll").write_bytes(body)
    (cache / tag / ".verified").write_text("sha\n")
    return str(d / binary)


def test_usage_documents_every_command():
    for c in ("install", "info", "doctor", "update", "clear-cache", "login", "logout", "serve",
              "--quick", "--json", "--proxy", "version", "cloud"):
        assert c in _commands.usage()


def test_console_script_declared():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "pyproject.toml"), encoding="utf-8") as fh:
        text = fh.read()
    assert 'clearcote = "clearcote._commands:_console_main"' in text
    assert 'clearcote-agent = "clearcote.cli:main"' in text  # the older entry points are untouched
    assert 'clearcote-serve = "clearcote._serve:_cli_main"' in text


def test_list_cached_builds_only_verified_with_binary(env):
    good = fake_cached_build(env["cache"], "pro-152.0.7977.82-r21")
    (env["cache"] / "half-downloaded" / "browser").mkdir(parents=True)  # no .verified
    assert list_cached_builds(str(env["cache"])) == [{"tag": "pro-152.0.7977.82-r21", "path": good}]


def test_info_quick_no_network_no_launch(env, monkeypatch):
    # the open build this SDK pins: what a keyless launch() runs
    exe = fake_cached_build(env["cache"], RELEASE["tag"],
                            ["fingerprint-passthrough", "allow-third-party-cookies", "proxy-auth"])

    def boom(*a, **k):
        raise AssertionError("network used under --quick")

    monkeypatch.setattr(_license, "proxied_request", boom)
    r = _commands.build_info(quick=True, launch_fn=boom)
    assert r["license"] == {"source": "none"}
    assert r["binary"]["source"] == "cache" and r["binary"]["path"] == exe
    assert r["binary"]["tag"] == RELEASE["tag"] and r["binary"]["releaseChannel"] == "stable"
    ef = r["engineFeatures"]
    assert ef["fingerprint-passthrough"] and ef["allow-third-party-cookies"] and ef["proxy-auth"]
    assert ef["transparent-proxy"] is False and ef["disable-fingerprint-voices"] is False
    assert r["launch"] == {"tested": False, "reason": "skipped (--quick)"}


def test_info_honours_binary_env_and_channel(env, monkeypatch):
    exe = fake_cached_build(env["cache"], "custom")
    monkeypatch.setenv("CLEARCOTE_BINARY", exe)
    monkeypatch.setenv("CLEARCOTE_RELEASE_CHANNEL", "preview")
    r = _commands.build_info(quick=True)
    assert (r["binary"]["source"], r["binary"]["path"], r["binary"]["releaseChannel"]) == ("CLEARCOTE_BINARY", exe, "preview")


def test_info_nothing_installed(env):
    r = _commands.build_info(quick=False)
    assert r["binary"]["source"] == "none"
    assert r["launch"] == {"tested": False, "reason": "no binary installed — run: clearcote install"}


def test_info_launch_test_runs_when_not_quick(env):
    fake_cached_build(env["cache"], RELEASE["tag"])
    calls = []

    class B:
        version = "152.0.1"

        def close(self):
            calls.append("close")

    r = _commands.build_info(quick=False, launch_fn=lambda **kw: calls.append(kw) or B())
    assert r["launch"] == {"tested": True, "ok": True, "version": "152.0.1", "build": RELEASE["tag"]}
    assert calls[0]["headless"] is True and calls[-1] == "close"


def _age(cache, tag, seconds_ago):
    t = __import__("time").time() - seconds_ago
    os.utime(cache / tag, (t, t))


def test_info_launch_tests_the_build_launch_would_use(env, monkeypatch, capsys):
    # An open-build user with an old licensed build still cached: the newest cached build is the licensed
    # one, which cannot run without a key. info must test the open build a keyless launch() runs.
    free = fake_cached_build(env["cache"], RELEASE["tag"])
    pro = fake_cached_build(env["cache"], "pro-152.0.7977.82-r21")
    _age(env["cache"], RELEASE["tag"], 3600)  # the licensed build is the newer one in the cache
    tested = []

    class B:
        version = "149.0.1"

        def close(self):
            pass

    def launch_fn(**kw):
        tested.append(kw["executable_path"])
        return B()

    r = _commands.build_info(quick=False, launch_fn=launch_fn)
    assert tested == [free]
    assert r["binary"]["path"] == free and r["binary"]["tag"] == RELEASE["tag"]
    assert "open build pinned by this SDK" in r["binary"]["selectedBy"]
    assert r["launch"]["build"] == RELEASE["tag"]
    _commands.print_info(r)
    out = capsys.readouterr().out
    assert f"Launch test     ok (149.0.1) with {RELEASE['tag']}" in out

    # with a key, launch() runs a licensed build: that one is tested
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", "cc_lic_info_pick_test_key")
    monkeypatch.setattr(_license, "proxied_request", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    tested.clear()
    r = _commands.build_info(quick=False, launch_fn=launch_fn)
    assert tested == [pro] and r["launch"]["build"] == "pro-152.0.7977.82-r21"
    assert r["binary"]["selectedBy"].startswith("licensed")


def test_info_follows_clearcote_browser_version_as_launch_does(env, monkeypatch):
    # launch() honours CLEARCOTE_BROWSER_VERSION; info must test the build it names, not the newest one.
    free149 = fake_cached_build(env["cache"], "v0.1.0-pre.22")  # the bundled catalog's 149 open build
    fake_cached_build(env["cache"], RELEASE["tag"])
    r7 = fake_cached_build(env["cache"], "pro-150.0.7871.114-r7")
    fake_cached_build(env["cache"], "pro-154.0.8037.57-r30")  # the newest
    tested = []

    class B:
        version = "1.0"

        def close(self):
            pass

    def launch_fn(**kw):
        tested.append(kw["executable_path"])
        return B()

    monkeypatch.setattr(_license, "proxied_request", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", "cc_lic_info_version_key")
    monkeypatch.setenv("CLEARCOTE_BROWSER_VERSION", "r7")  # a licensed revision
    r = _commands.build_info(quick=False, launch_fn=launch_fn)
    assert tested == [r7] and r["launch"]["build"] == "pro-150.0.7871.114-r7"
    assert "CLEARCOTE_BROWSER_VERSION=r7" in r["binary"]["selectedBy"]

    monkeypatch.delenv("CLEARCOTE_LICENSE_KEY")
    monkeypatch.setenv("CLEARCOTE_BROWSER_VERSION", "149")  # an open major, resolved through the catalog
    r = _commands.build_info(quick=True)  # --quick: the bundled catalog, no network
    assert r["binary"]["path"] == free149 and r["binary"]["tag"] == "v0.1.0-pre.22"
    tested.clear()
    _commands.build_info(quick=False, launch_fn=launch_fn)
    assert tested == [free149]

    monkeypatch.setenv("CLEARCOTE_BROWSER_VERSION", "999")
    r = _commands.build_info(quick=True)
    assert "binary" in r and "path" not in r["binary"]
    assert r["binary"]["selectedBy"].startswith("CLEARCOTE_BROWSER_VERSION=999: No Clearcote build matches")


def test_info_keyless_with_only_a_licensed_build_cached(env):
    fake_cached_build(env["cache"], "pro-152.0.7977.82-r21")
    r = _commands.build_info(quick=False, launch_fn=lambda **kw: (_ for _ in ()).throw(AssertionError("launched")))
    assert r["launch"]["tested"] is False
    assert f"the open build this SDK pins ({RELEASE['tag']}) is not installed" in r["launch"]["reason"]


def test_info_json_parseable(env, capsys):
    assert _commands.main(["info", "--quick", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["sdk"]["version"]
    assert parsed["launch"]["tested"] is False


def test_login_validates_then_saves_and_logout(env, monkeypatch, capsys):
    api = start_origin(lambda m, path, h, b: (200, '{"used":0,"limit":5,"plan":"team"}')
                       if h.get("authorization") == "Bearer cc_lic_valid_key_1234"
                       else (401, '{"error":"Invalid license key."}'))
    try:
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", f"http://127.0.0.1:{api.port}")
        assert _commands.main(["login", "cc_lic_valid_key_1234"]) == 0
        out = capsys.readouterr().out
        assert "saved to" in out and "license.key" in out
        assert "valid: 0 of 5 seats in use, plan team" in out
        assert (env["home"] / ".clearcote" / "license.key").exists()
        assert _commands.main(["logout"]) == 0
        assert not (env["home"] / ".clearcote" / "license.key").exists()
    finally:
        api.close()


def test_login_prompt_does_not_echo_the_key(env, monkeypatch, capsys):
    # `clearcote login` with no argument asks on the terminal: what is pasted must not be shown.
    import io

    api = start_origin(lambda *a: (200, '{"used":0,"limit":5,"plan":"team"}'))
    try:
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", f"http://127.0.0.1:{api.port}")

        class Tty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setattr(sys, "stdin", Tty("cc_lic_typed_key_123456\n"))  # an echoing read would get this
        asked = []

        def no_echo(prompt="", stream=None):
            asked.append(prompt)
            return "cc_lic_typed_key_123456"

        monkeypatch.setattr("getpass.getpass", no_echo)
        assert _commands.main(["login"]) == 0
    finally:
        api.close()
    assert len(asked) == 1 and "not shown" in asked[0]
    assert (env["home"] / ".clearcote" / "license.key").read_text().strip() == "cc_lic_typed_key_123456"
    captured = capsys.readouterr()
    assert "cc_lic_typed_key_123456" not in captured.out + captured.err


def test_login_key_argument_still_works_without_a_prompt(env, monkeypatch):
    api = start_origin(lambda *a: (200, '{"used":0,"limit":5,"plan":"team"}'))
    try:
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", f"http://127.0.0.1:{api.port}")
        monkeypatch.setattr("getpass.getpass", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))
        assert _commands.main(["login", "cc_lic_argument_key_9876"]) == 0
    finally:
        api.close()
    assert (env["home"] / ".clearcote" / "license.key").read_text().strip() == "cc_lic_argument_key_9876"


def test_subcommand_help(env, capsys):
    assert _commands.main(["login", "--help"]) == 0
    out = capsys.readouterr().out
    assert "clearcote login --device" in out and "clearcote login [key]" in out and "not shown" in out
    assert not (env["home"] / ".clearcote" / "license.key").exists()  # "--help" is not a key
    for args in (["login", "-h"], ["logout", "--help"], ["info", "--help"], ["doctor", "-h"], ["serve", "--help"],
                 ["install", "--help"], ["update", "--help"], ["clear-cache", "--help"]):
        assert _commands.main(args) == 0, args
        out = capsys.readouterr().out
        cmd = "info" if args[0] == "doctor" else args[0]
        assert f"clearcote {cmd}" in out and "USAGE" in out
    assert _commands.main(["info", "--help"]) == 0
    assert "--quick" in capsys.readouterr().out


def test_login_refuses_rejected_key(env, monkeypatch, capsys):
    api = start_origin(lambda *a: (401, '{"error":"Invalid license key."}'))
    try:
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", f"http://127.0.0.1:{api.port}")
        assert _commands.main(["login", "cc_lic_bad"]) == 1
        assert "rejected this key" in capsys.readouterr().err
        assert not (env["home"] / ".clearcote" / "license.key").exists()
    finally:
        api.close()


def test_login_saves_with_note_when_unavailable(env, monkeypatch, capsys):
    api = start_origin(lambda *a: (404, "{}"))
    try:
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", f"http://127.0.0.1:{api.port}")
        assert _commands.main(["login", "cc_lic_unconfirmed_1234"]) == 0
        out = capsys.readouterr().out
        assert "could not confirm the key right now (this licence server does not report seats yet)" in out
        assert (env["home"] / ".clearcote" / "license.key").exists()
    finally:
        api.close()


def test_clear_cache_removes_only_build_dirs(env, capsys):
    fake_cached_build(env["cache"], "pro-x")
    fake_cached_build(env["cache"], "unusual-name")          # has .verified
    (env["cache"] / "v0.1.0-pre.22").mkdir()                  # build-tag shaped
    (env["cache"] / "geoip").mkdir()                          # not a build: kept
    (env["cache"] / "notes.txt").write_text("keep")
    assert _commands.main(["clear-cache"]) == 0
    assert "removed 3 build(s)" in capsys.readouterr().out
    assert sorted(os.listdir(env["cache"])) == ["geoip", "notes.txt"]
    assert env["cache"].is_dir()                              # the root itself is never removed


def test_unknown_command_exit_2(env, capsys):
    assert _commands.main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_serve_splits_proxy_credentials(env, monkeypatch):
    seen = {}

    class Srv:
        def serve_forever(self):
            pass

        def close(self):
            pass

    import clearcote._multiplex as mx
    monkeypatch.setattr(mx, "serve_multiplex", lambda **kw: seen.update(kw) or Srv())
    import signal
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    assert _commands.main(["serve", "--port", "0", "--proxy", "http://us%40er:p%3Ass@proxy.test:3128",
                           "--allow-host", "cdp.example.com", "--quiet"]) == 0
    assert seen["proxy"] == {"server": "http://proxy.test:3128", "username": "us@er", "password": "p:ss"}
    assert seen["allow_hosts"] == ["cdp.example.com"] and seen["headless"] is True
