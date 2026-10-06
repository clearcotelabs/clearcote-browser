"""`clearcote login --device` against a local stand-in for the site's device endpoints, every branch of
the contract: pending, slow_down (+5 s, kept), 429 (+5 s, Retry-After honoured), expired_token,
access_denied, invalid_request, a dropped connection and a 5xx mid-poll (retried), an unreachable or
older server, Ctrl-C, and the expiry `clearcote info --json` then reports. Mirrors
sdk/node/test/device-login.test.ts."""
import json
import os
import stat
import sys

import pytest

from clearcote import __version__, _commands, _license

from _fake_device import DEVICE_CODE, LICENSE_KEY, USER_CODE, FakeDevice
from _proxies import start_origin


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("CLEARCOTE_CACHE", str(tmp_path / "cache"))
    for k in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_LICENSE_API", "CLEARCOTE_BINARY", "CLEARCOTE_RELEASE_CHANNEL"):
        monkeypatch.delenv(k, raising=False)
    return h


@pytest.fixture
def sleeps(monkeypatch):
    """time.sleep recorded instead of slept: the polling intervals become an assertion."""
    seen = []
    monkeypatch.setattr("time.sleep", seen.append)
    return seen


def serve(monkeypatch, request, script, **kw):
    fake = FakeDevice(script, **kw)
    request.addfinalizer(fake.close)
    monkeypatch.setenv("CLEARCOTE_LICENSE_API", fake.url)
    return fake


def key_file(home):
    return home / ".clearcote" / "license.key"


def test_usage_documents_device_login():
    assert "clearcote login --device" in _commands.usage()


def test_device_login_saves_the_key_exactly_like_paste_login(home, sleeps, monkeypatch, request, capsys):
    fake = serve(monkeypatch, request, ["pending", "pending", "ok"])
    assert _commands.main(["login", "--device"]) == 0
    out, err = capsys.readouterr()
    # the same file, the same bytes and the same permissions save_license_key (paste login) writes
    p = key_file(home)
    assert str(p) == _license.license_key_path()
    assert p.read_bytes() == (LICENSE_KEY + "\n").encode()
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert f"saved to {p}" in out
    assert "plan pro, expires 2027-01-31T00:00:00.000Z" in out
    # the link and the code go to the terminal; the key never does
    assert f"{fake.url}/device?code={USER_CODE}" in err and f"Code: {USER_CODE}" in err
    assert LICENSE_KEY not in out + err
    # the client named itself and its version, and polled with the device code at the interval
    code_req = fake.log[0]
    assert code_req["path"] == "/api/v1/device/code"
    assert code_req["body"] == {"client_name": "clearcote-python", "client_version": __version__}
    assert code_req["headers"]["user-agent"] == f"clearcote-sdk-python/{__version__}"
    assert [e["body"] for e in fake.token_polls()] == [{"device_code": DEVICE_CODE}] * 3
    assert sleeps == [5, 5, 5]


def test_slow_down_and_429_each_add_five_seconds_for_good(home, sleeps, monkeypatch, request, capsys):
    serve(monkeypatch, request, ["slow", "pending", "429", "pending", "ok"])
    assert _commands.main(["login", "--device"]) == 0
    assert sleeps == [5, 10, 10, 15, 15]
    assert key_file(home).exists()


def test_429_retry_after_is_honoured(home, sleeps, monkeypatch, request):
    serve(monkeypatch, request, ["429", "ok"], retry_after=30)
    assert _commands.main(["login", "--device"]) == 0
    assert sleeps == [5, 30]


@pytest.mark.parametrize("step,message", [
    ("expired", "the code expired before it was approved. Nothing was saved. Run `clearcote login --device` again."),
    ("denied", "the sign-in was denied in the browser. Nothing was saved."),
    ("invalid", "the licence server rejected the device login request (invalid_request). Nothing was saved."),
])
def test_refusals_save_nothing(home, sleeps, monkeypatch, request, capsys, step, message):
    fake = serve(monkeypatch, request, ["pending", step])
    assert _commands.main(["login", "--device"]) == 1
    err = capsys.readouterr().err
    assert f"clearcote: {message}" in err
    assert not key_file(home).exists() and not (home / ".clearcote" / "license.meta.json").exists()
    assert len(fake.token_polls()) == 2  # stopped at the refusal


def test_dropped_connection_and_5xx_mid_poll_are_retried(home, sleeps, monkeypatch, request, capsys):
    fake = serve(monkeypatch, request, ["drop", "500", "pending", "ok"])
    assert _commands.main(["login", "--device"]) == 0
    err = capsys.readouterr().err
    assert err.count("note: licence server unreachable") == 1  # said once, not per retry
    assert len(fake.token_polls()) == 4 and sleeps == [5, 5, 5, 5]
    assert key_file(home).read_text().strip() == LICENSE_KEY


def test_unreachable_server_fails_before_any_code(home, sleeps, monkeypatch, capsys):
    monkeypatch.setenv("CLEARCOTE_LICENSE_API", "http://127.0.0.1:1")
    assert _commands.main(["login", "--device"]) == 1
    err = capsys.readouterr().err
    assert "could not reach the licence server at http://127.0.0.1:1" in err and "Nothing was saved." in err
    assert sleeps == [] and not key_file(home).exists()


def test_server_without_device_login(home, sleeps, monkeypatch, request, capsys):
    serve(monkeypatch, request, [], code_status=404)
    assert _commands.main(["login", "--device"]) == 1
    assert "does not support device login (HTTP 404)" in capsys.readouterr().err


def test_ctrl_c_while_waiting_saves_nothing(home, monkeypatch, request, capsys):
    fake = serve(monkeypatch, request, ["pending", "ok"])
    calls = []

    def sleep(s):
        calls.append(s)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", sleep)
    assert _commands.main(["login", "--device"]) == 130
    assert "clearcote: cancelled. Nothing was saved." in capsys.readouterr().err
    assert not key_file(home).exists()
    assert len(fake.token_polls()) == 1  # the approval after Ctrl-C was never fetched


def test_code_expiring_locally_stops_polling(home, monkeypatch, request, capsys):
    fake = serve(monkeypatch, request, ["pending"] * 50, expires_in=12)
    t = [0.0]

    def sleep(s):
        t[0] += s

    with pytest.raises(_commands.CliExit) as exc:
        _commands.device_login(sleep=sleep, clock=lambda: t[0])
    assert exc.value.code == 1
    assert "the code expired before it was approved" in capsys.readouterr().err
    assert len(fake.token_polls()) == 3  # at 5, 10 and 15 s; no poll once the 12 s were up


def test_key_and_device_together_is_a_usage_error(home, capsys):
    assert _commands.main(["login", "--device", "cc_lic_x"]) == 2
    assert "pass a key or --device, not both" in capsys.readouterr().err


def info_json(capsys):
    capsys.readouterr()
    assert _commands.main(["info", "--quick", "--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_info_json_reports_the_expiry_device_login_recorded(home, sleeps, monkeypatch, request, capsys):
    serve(monkeypatch, request, ["ok"])
    assert _commands.main(["login", "--device"]) == 0
    lic = info_json(capsys)["license"]
    assert lic["source"] == "file"
    assert lic["expiry"]["expiresAt"] == "2027-01-31T00:00:00.000Z"
    assert lic["expiry"]["source"] == "device-login" and lic["expiry"]["recordedAt"]
    assert "no licence endpoint reports" in lic["expiry"]["note"]
    _commands.main(["info", "--quick"])
    assert "Expires         2027-01-31T00:00:00.000Z  (as reported at device login)" in capsys.readouterr().out
    # the record holds no key
    meta = (home / ".clearcote" / "license.meta.json").read_text()
    assert LICENSE_KEY not in meta

    # an env key is a different key: the record does not describe it
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", "cc_lic_some_other_key_9999")
    lic = info_json(capsys)["license"]
    assert lic["source"] == "env" and lic["expiry"]["source"] == "unknown" and "expiresAt" not in lic["expiry"]
    monkeypatch.delenv("CLEARCOTE_LICENSE_KEY")

    # pasting a different key drops the record; logout removes both files
    api = start_origin(lambda *a: (200, '{"used":0,"limit":null,"plan":"pro"}'))
    try:
        monkeypatch.setenv("CLEARCOTE_LICENSE_API", f"http://127.0.0.1:{api.port}")
        assert _commands.main(["login", "cc_lic_pasted_key_abcdefgh"]) == 0
    finally:
        api.close()
    assert not (home / ".clearcote" / "license.meta.json").exists()
    assert info_json(capsys)["license"]["expiry"]["source"] == "unknown"
    assert _commands.main(["logout"]) == 0
    assert "expiry" not in info_json(capsys)["license"]


def test_no_expiry_and_logout(home, sleeps, monkeypatch, request, capsys):
    serve(monkeypatch, request, ["ok"], expires_at=None, plan="free")
    assert _commands.main(["login", "--device"]) == 0
    assert "plan free, no expiry" in capsys.readouterr().out
    exp = info_json(capsys)["license"]["expiry"]
    assert exp["source"] == "device-login" and exp["expiresAt"] is None
    _commands.main(["info", "--quick"])
    assert "Expires         never" in capsys.readouterr().out
    assert _commands.main(["logout"]) == 0
    assert not (home / ".clearcote" / "license.meta.json").exists() and not key_file(home).exists()
