"""`clearcote cloud ...` (the hosted API from the command line) against the fake API: exact output,
exit codes, the refusal to upload every cookie, and PARITY with the Node CLI.

The golden outputs below are asserted, byte for byte, in sdk/node/test/parity-cloud-cli.test.ts too.
test_node_cli_matches_python then runs BOTH CLIs as real processes against two fresh fake APIs and
compares exit codes, stdout and every request they sent. It needs node and a built Node SDK
(sdk/node/dist, `npm run build`); it skips without them."""
import contextlib
import io
import json
import os
import shutil
import subprocess

import pytest
from _fake_cloud import API_KEY, RECORDING, start_fake_cloud, stop_fake_cloud
from clearcote import _commands

HERE = os.path.dirname(os.path.abspath(__file__))
NODE_CLI = os.path.join(HERE, "..", "..", "node", "dist", "clearcote-cli.js")

STATE = {"cookies": [
    {"name": "sid", "value": "1", "domain": ".example.com", "path": "/", "expires": -1, "httpOnly": True,
     "secure": True, "sameSite": "Lax"},
    {"name": "pref", "value": "2", "domain": "www.example.com", "path": "/", "expires": 1893456000,
     "httpOnly": False, "secure": False, "sameSite": "None"},
    {"name": "x", "value": "3", "domain": "badexample.com", "path": "/", "expires": -1, "httpOnly": False,
     "secure": False, "sameSite": "Lax"},
], "origins": []}

GOLDEN = {
    "run": [
        "run bs_run1 succeeded",
        "result  done",
        "url     https://example.com/pricing",
        "title   Pricing",
        "output  {",
        '  "plan": "Starter",',
        '  "price": "9.99"',
        "}",
        "cost    €0.0019",
    ],
    "sessions": [
        "bs_a1  active  2026-10-02T09:00:00.000Z  €0.0123  crawler",
        "bs_b2  ended  2026-10-01T09:00:00.000Z  €0.5000",
        "balance €12.50",
    ],
    "stop": ["stopped bs_a1 (ended)"],
    "events": [
        '1  2026-10-02T10:00:00.000Z  session.started  {"kind":"run"}',
        '2  2026-10-02T10:00:01.000Z  navigation  {"url":"https://example.com/","title":"Example Domain"}',
        "3  2026-10-02T10:00:02.000Z  tab.closed",
        '4  2026-10-02T10:00:03.000Z  session.ended  {"reason":"run_finished"}',
    ],
    "profile": [
        "imported 2 cookies into profile acct-1 (2 in total)",
        "domains  example.com, www.example.com",
    ],
    "webhooks add": [
        "webhook wh_1 -> https://hooks.example.com/x",
        "events  run.finished",
        "secret  whsec_test_secret",
        "store the secret now: it is not shown again",
    ],
    "webhooks list": [
        "wh_9  https://hooks.example.com/cc  run.finished  last 200 2026-10-02T10:00:00.000Z",
        "wh_8  https://hooks.example.com/all  all",
    ],
}


@pytest.fixture
def api(monkeypatch, tmp_path):
    a = start_fake_cloud()
    a.run_statuses = ["succeeded"]
    monkeypatch.setenv("CLEARCOTE_API_KEY", API_KEY)
    monkeypatch.setenv("CLEARCOTE_API_URL", a.url)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state.json").write_text(json.dumps(STATE))
    yield a
    stop_fake_cloud(a)


def run_cli(capsys, *argv):
    code = _commands.main(["cloud", *argv])
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err


def test_usage_documents_cloud():
    assert "clearcote cloud run <task>" in _commands.usage()
    assert "CLEARCOTE_API_KEY" in _commands.usage()
    for flag in ("--url", "--schema", "--secret", "--secret-domain", "--handoff", "--record", "--json", "sessions",
                 "stop <id>", "events <id>", "recording <id>", "-o <file.mp4>", "profile sync", "--from-profile",
                 "--from-cdp", "--from-file", "--login", "--domain", "--all-domains", "--replace", "webhooks add",
                 "--event", "webhooks list", "webhooks rm", "webhooks test", "--country", "--state", "--city",
                 "--proxy", "--profile", "--persist-profile", "--fingerprint", "--timeout-sec", "--max-steps",
                 "--note"):
        assert flag in _commands.CLOUD_USAGE


def test_help(capsys):
    code, out, _ = run_cli(capsys, "--help")
    assert code == 0 and "\n".join(out) == _commands.CLOUD_USAGE


def test_run_prints_the_result_and_exits_on_its_status(api, capsys, tmp_path):
    (tmp_path / "schema.json").write_text('{"type": "object"}')
    code, out, err = run_cli(capsys, "run", "Find", "the", "price", "--url", "https://example.com/",
                             "--schema", "schema.json", "--secret", "pw=hunter2", "--secret", "otp=x=y",
                             "--secret-domain", "pw=Example.com", "--handoff", "--record")
    assert (code, out) == (0, GOLDEN["run"])
    assert "[clearcote] run bs_run1: succeeded" in err
    assert api.requests("POST", "/api/v1/runs")[0]["body"] == {
        "task": "Find the price", "url": "https://example.com/", "schema": {"type": "object"},
        "secrets": {"pw": {"value": "hunter2", "domains": ["example.com"]}, "otp": "x=y"},
        "handoff": True, "record": True}
    api.run_statuses = ["failed"]
    code, out, _ = run_cli(capsys, "run", "t")
    assert (code, out) == (1, ["run bs_run1 failed", "cost    €0.0019"])


def test_run_task_words_may_sit_between_flags(api, capsys):
    code, _, _ = run_cli(capsys, "run", "Find", "--url", "https://example.com/", "the", "--record", "price")
    assert code == 0
    assert api.requests("POST", "/api/v1/runs")[0]["body"] == {
        "task": "Find the price", "url": "https://example.com/", "record": True}


RUN_OPTIONS = ["--country", "us", "--state", "ca", "--city", "los angeles", "--proxy", "http://u:p@proxy.example:8080",
               "--profile", "acct-1", "--persist-profile", "--fingerprint", "seed-7", "--timeout-sec", "600",
               "--max-steps", "12", "--note", "nightly"]
RUN_OPTIONS_BODY = {
    "country": "us", "state": "ca", "city": "los angeles",
    "proxy": {"server": "http://proxy.example:8080", "username": "u", "password": "p"},
    "profile": {"name": "acct-1", "persist": True}, "fingerprint": "seed-7", "timeoutSec": 600, "maxSteps": 12,
    "note": "nightly"}


def test_run_browser_options(api, capsys):
    assert run_cli(capsys, "run", "t", *RUN_OPTIONS)[0] == 0
    assert api.requests("POST", "/api/v1/runs")[0]["body"] == {"task": "t", **RUN_OPTIONS_BODY}
    assert run_cli(capsys, "run", "t", "--proxy", "managed", "--profile", "acct-2")[0] == 0
    assert api.requests("POST", "/api/v1/runs")[1]["body"] == {"task": "t", "proxy": "managed", "profile": "acct-2"}
    sent = len(api.log)
    # usage errors exit 2 before any request
    code, _, err = run_cli(capsys, "run", "t", "--persist-profile")
    assert code == 2 and "--persist-profile needs --profile <name>" in err
    code, _, err = run_cli(capsys, "run", "t", "--max-steps", "ten")
    assert code == 2 and "--max-steps wants a whole number" in err
    assert run_cli(capsys, "run", "t", "--timeout-sec", "1.5")[0] == 2
    assert run_cli(capsys, "run", "t", "--coun", "us")[0] == 2  # no abbreviations, as in Node
    assert len(api.log) == sent


def test_run_json(api, capsys):
    code, out, _ = run_cli(capsys, "run", "t", "--json")
    run = json.loads("\n".join(out))
    assert code == 0 and run["status"] == "succeeded" and run["result"]["output"]["price"] == "9.99"


def test_run_argument_errors(api, capsys):
    code, _, err = run_cli(capsys, "run", "t", "--secret", "hunter2")
    assert code == 2 and "hunter2" not in err and "--secret wants <name>=<value>" in err
    code, _, err = run_cli(capsys, "run", "t", "--secret", "=hunter2")
    assert code == 2 and "hunter2" not in err
    # "--secret pw hunter2" (a space for the =): the value lands among the task words, and the run is
    # refused before anything is sent, without printing it
    code, _, err = run_cli(capsys, "run", "t", "--secret", "pw", "hunter2")
    assert code == 2 and "hunter2" not in err
    code, _, err = run_cli(capsys, "run", "t", "--secret", "pw=x", "--secret", "pw2", "-hunter2")
    assert code == 2 and "hunter2" not in err
    assert run_cli(capsys, "run", "t", "--secret-domain", "pw=a.com")[0] == 2
    assert run_cli(capsys, "run", "t", "--schema", "missing.json")[0] == 2
    assert run_cli(capsys, "run")[0] == 2
    assert api.log == []


def test_sessions_stop_events(api, capsys):
    assert run_cli(capsys, "sessions")[:2] == (0, GOLDEN["sessions"])
    assert run_cli(capsys, "stop", "bs_a1")[:2] == (0, GOLDEN["stop"])
    assert run_cli(capsys, "events", "bs_a1")[:2] == (0, GOLDEN["events"])
    # every page was read: after=0, after=2
    assert [r["query"] for r in api.requests("GET", "/api/v1/browsers/bs_a1/events")] == ["after=0", "after=2"]
    _code, out, _ = run_cli(capsys, "events", "bs_a1", "--json")
    assert [e["seq"] for e in json.loads("\n".join(out))["events"]] == [1, 2, 3, 4]


def test_recording(api, capsys, tmp_path):
    code, out, _ = run_cli(capsys, "recording", "bs_a1", "-o", "rec.mp4")
    assert (code, out) == (0, [f"saved rec.mp4 ({len(RECORDING)} bytes)"])
    assert (tmp_path / "rec.mp4").read_bytes() == RECORDING
    _code, out, _ = run_cli(capsys, "recording", "bs_a1", "--json")
    assert json.loads("\n".join(out)) == {"path": "bs_a1.mp4", "bytes": len(RECORDING)}
    api.recording_state = "processing"
    code, out, err = run_cli(capsys, "recording", "bs_a1")
    assert code == 1 and "The recording is still processing. (HTTP 409, NOT_READY)" in err


def test_profile_sync(api, capsys):
    assert run_cli(capsys, "profile", "sync", "acct-1", "--from-file", "state.json", "--domain", "example.com")[:2] == (
        0, GOLDEN["profile"])
    assert api.requests("PUT")[-1]["body"]["mode"] == "merge"
    run_cli(capsys, "profile", "sync", "acct-1", "--from-file", "state.json", "--all-domains", "--replace")
    assert api.requests("PUT")[-1]["body"]["mode"] == "replace" and len(api.requests("PUT")[-1]["body"]["cookies"]) == 3


def test_profile_sync_refuses_to_upload_everything_by_accident(api, capsys):
    code, out, err = run_cli(capsys, "profile", "sync", "acct-1", "--from-file", "state.json")
    assert code == 2 and out == [] and "refusing to upload every cookie" in err
    assert run_cli(capsys, "profile", "sync", "acct-1", "--domain", "a.com")[0] == 2   # no source
    assert run_cli(capsys, "profile", "sync", "acct-1", "--from-file", "state.json", "--from-cdp", "http://x",
                   "--domain", "a.com")[0] == 2
    assert run_cli(capsys, "profile", "sync", "acct-1", "--from-file", "state.json", "--domain", "a.com",
                   "--all-domains")[0] == 2
    assert run_cli(capsys, "profile")[0] == 2
    assert api.log == []


def test_webhooks(api, capsys):
    assert run_cli(capsys, "webhooks", "list")[:2] == (0, GOLDEN["webhooks list"])
    assert run_cli(capsys, "webhooks", "add", "https://hooks.example.com/x", "--event", "run.finished")[:2] == (
        0, GOLDEN["webhooks add"])
    assert run_cli(capsys, "webhooks", "rm", "wh_9")[:2] == (0, ["removed wh_9"])
    assert run_cli(capsys, "webhooks", "test", "wh_9")[:2] == (0, ["sent a ping to wh_9"])
    assert run_cli(capsys, "webhooks", "frob", "x")[0] == 2


def test_errors(api, capsys, monkeypatch):
    assert run_cli(capsys, "frobnicate")[0] == 2
    monkeypatch.setenv("CLEARCOTE_API_KEY", "wrong")
    code, _, err = run_cli(capsys, "sessions")
    assert code == 1 and "clearcote: Missing or invalid API key. (HTTP 401)" in err
    monkeypatch.delenv("CLEARCOTE_API_KEY")
    code, _, err = run_cli(capsys, "sessions")
    assert code == 1 and "CLEARCOTE_API_KEY" in err


# ── the same commands through the Node CLI ───────────────────────────────────────────────────────

CASES = [
    ["--help"],
    ["run", "Find", "the", "price", "--url", "https://example.com/", "--secret", "pw=hunter2",
     "--secret-domain", "pw=example.com", "--handoff", "--record"],
    ["run", "Find the price", "--json"],
    ["sessions"],
    ["sessions", "--json"],
    ["stop", "bs_a1"],
    ["stop", "bs_a1", "--json"],
    ["events", "bs_a1"],
    ["events", "bs_a1", "--json"],
    ["recording", "bs_a1", "-o", "rec.mp4"],
    ["recording", "bs_a1", "--json"],
    ["profile", "sync", "acct-1", "--from-file", "state.json", "--domain", "example.com", "--domain", ".tracker.net"],
    ["profile", "sync", "acct-1", "--from-file", "state.json", "--all-domains", "--replace", "--json"],
    ["profile", "sync", "acct-1", "--from-file", "state.json"],
    ["profile", "sync", "acct-1", "--from-file", "state.json", "--domain", "www.example.com"],
    ["run", "Find", "--url", "https://example.com/", "the", "--handoff", "price"],
    ["run", "t", "--secret", "pw", "hunter2"],
    ["run", "t", "--secret", "hunter2"],
    ["run", "t", "--country", "us", "--state", "ca", "--city", "los angeles", "--proxy", "http://u:p@proxy.example:8080",
     "--profile", "acct-1", "--persist-profile", "--fingerprint", "seed-7", "--timeout-sec", "600",
     "--max-steps", "12", "--note", "nightly", "--handoff", "--record"],
    ["run", "t", "--proxy", "managed", "--profile", "acct-2", "--json"],
    ["run", "t", "--persist-profile"],
    ["run", "t", "--max-steps", "ten"],
    ["run", "t", "--coun", "us"],
    ["webhooks", "add", "https://hooks.example.com/x", "--event", "run.finished", "--event", "handoff.requested"],
    ["webhooks", "add", "https://hooks.example.com/y", "--json"],
    ["webhooks", "list"],
    ["webhooks", "list", "--json"],
    ["webhooks", "rm", "wh_9"],
    ["webhooks", "test", "wh_9", "--json"],
    ["frobnicate"],
]


def _with_fake_api(cwd, run):
    """``run(api)`` -> (exit code, stdout) against a fresh fake API, in ``cwd`` (holding state.json);
    returns (exit code, stdout, the requests the API received)."""
    api = start_fake_cloud()
    api.run_statuses = ["succeeded"]
    try:
        os.makedirs(cwd, exist_ok=True)
        with open(os.path.join(cwd, "state.json"), "w") as fh:
            json.dump(STATE, fh)
        code, out = run(api)
        return code, out, [(r["method"], r["path"], r["query"], r["body"]) for r in api.log]
    finally:
        stop_fake_cloud(api)


def _python_cli(argv, cwd, monkeypatch):
    def run(api):
        monkeypatch.setenv("CLEARCOTE_API_KEY", API_KEY)
        monkeypatch.setenv("CLEARCOTE_API_URL", api.url)
        monkeypatch.chdir(cwd)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = _commands.main(["cloud", *argv])
        return code, out.getvalue()
    return _with_fake_api(cwd, run)


def _node_cli(argv, cwd):
    def run(api):
        env = dict(os.environ, CLEARCOTE_API_KEY=API_KEY, CLEARCOTE_API_URL=api.url)
        env.pop("CLEARCOTE_CLOUD", None)
        p = subprocess.run(["node", NODE_CLI, "cloud", *argv], cwd=cwd, env=env, capture_output=True,
                           text=True, encoding="utf-8", timeout=120, check=False)
        return p.returncode, p.stdout
    return _with_fake_api(cwd, run)


@pytest.mark.skipif(not (shutil.which("node") and os.path.exists(NODE_CLI)),
                    reason="needs node and a built Node SDK (cd sdk/node && npm run build)")
@pytest.mark.parametrize("argv", CASES, ids=[" ".join(c)[:60] for c in CASES])
def test_node_cli_matches_python(argv, tmp_path, monkeypatch):
    monkeypatch.delenv("CLEARCOTE_CLOUD", raising=False)
    py = _python_cli(argv, str(tmp_path / "py"), monkeypatch)
    node = _node_cli(argv, str(tmp_path / "node"))
    assert py[0] == node[0], f"exit code: python {py[0]} vs node {node[0]}"
    if "--json" in argv:
        # Machine-readable output is compared as data: the two JSON encoders spell some floats
        # differently (Python 2.1e-06, JavaScript 0.0000021), which is the same number.
        assert json.loads(py[1]) == json.loads(node[1])
    else:
        assert py[1] == node[1]
    assert py[2] == node[2], "the two CLIs sent different requests"
