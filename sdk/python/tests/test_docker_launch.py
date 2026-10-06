"""launch() on macOS runs the Clearcote Docker image (there is no native macOS build).

The host platform is mocked (the module's view of ``sys.platform``) and the docker CLI is replaced by a
recorder that answers like Docker would; the "container's" CDP endpoint is a real local Chromium, or a
stand-in that answers /json/version, so the SDK's connect, close and the Playwright objects it returns are
real. The real image is exercised end to end separately (CLEARCOTE_TEST_ONLY_ASSUME_MACOS, see
test_docker_launch_live.py). Mirrors sdk/node/test/docker-launch.test.ts."""
import asyncio
import io
import json
import os
import re
import socket
import tarfile
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import clearcote
from clearcote import _docker, async_api

from _chromium import LocalChromium, find_chromium

CHROMIUM = find_chromium()  # found now: the fixtures below move HOME, where it is looked for
needs_chromium = pytest.mark.skipif(not CHROMIUM, reason="no Chromium (set CLEARCOTE_TEST_BINARY)")
IMAGE = f"teamflatearth/clearcote:sdk-{clearcote.__version__}"
DEAD_PID = 2 ** 22 + 12345  # above any pid_max: never a live process
PROTOCOL = {"com.clearcotelabs.serve-protocol": "2"}


def serve_state(engine="open", proxy=None, idle=30, secrets=False):
    return "[clearcote] serve-state " + json.dumps({"engine": engine, "idle_exit": idle, "protocol": 2,
                                                    "proxy": proxy, "secrets_file": secrets}, sort_keys=True)


class FakeDocker:
    """Stands in for the docker CLI: records every call (argv, the environment it was given, stdin)."""

    def __init__(self):
        self.calls = []
        self.cdp_port = 1
        self.running = True
        self.info_error = None
        self.create_error = None
        self.pull_error = None
        self.labels = dict(PROTOCOL)  # the image's labels; None: not here until pulled
        self.logs = ""
        self.marks = [serve_state()]  # what the entrypoint logged about what it applied
        self.ps = ""  # `docker ps` rows: id \t owner-token
        self.containers = []  # or: containers as {"id", <label>: value}, rendered in the format asked for

    def __call__(self, argv, env=None, timeout=120.0, input=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "input": input})
        cmd = argv[1]
        if cmd == "info":
            return (1, "", self.info_error) if self.info_error else (0, "29.1.3\n", "")
        if cmd == "ps":
            if not self.containers:
                return 0, self.ps, ""
            names = re.findall(r'\.Label "([^"]+)"', argv[argv.index("--format") + 1])
            return 0, "".join("\t".join([c["id"], *(c.get(n, "") for n in names)]) + "\n"
                              for c in self.containers), ""
        if cmd == "image":
            if self.labels is None:
                return 1, "", f"Error: No such image: {argv[-1]}"
            return 0, json.dumps(self.labels or None) + "\n", ""
        if cmd == "pull":
            if self.pull_error:
                return 1, "", self.pull_error
            self.labels = self.labels if self.labels is not None else dict(PROTOCOL)
            return 0, "", ""
        if cmd == "create":
            return (125, "", self.create_error) if self.create_error else (0, "c0ffee1234\n", "")
        if cmd == "port":
            return 0, f"127.0.0.1:{self.cdp_port}\n[::1]:{self.cdp_port}\n", ""
        if cmd == "inspect":
            return (0, "true\n", "") if self.running else (1, "", "Error: No such object: c0ffee1234")
        return 0, "", ""  # cp, start, stop, rm

    def commands(self):
        return [c["argv"][1] for c in self.calls]

    def call(self, cmd):
        return next(c for c in self.calls if c["argv"][1] == cmd)


class FakeLogs:
    def __init__(self, docker):
        self.docker = docker

    def text(self, wait=3.0):
        return self.docker.logs

    def wait_marks(self, pattern, wait=10.0):
        return list(self.docker.marks)

    def stop(self):
        pass


class _CdpStub:
    """Answers /json/version like a browser: the container 'came up'. Its WebSocket is not there, so a
    real connect fails (which some tests want) and start_container alone needs nothing more."""

    def __enter__(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = json.dumps({"Browser": "x", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        return self

    def __exit__(self, *_a):
        self.srv.shutdown()
        self.srv.server_close()


class _MacSys:
    """The module's view of ``sys``: macOS, with the current stderr (pytest's capture swaps it)."""
    platform = "darwin"

    @property
    def stderr(self):
        import sys
        return sys.stderr


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """The host is macOS, as far as the docker decision is concerned (and only there). HOME is a
    throwaway, so this machine's own saved licence key is never read."""
    monkeypatch.setattr(_docker, "sys", _MacSys())
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    if hasattr(_docker, "_OWNER"):
        monkeypatch.setitem(_docker._OWNER, "token", None)  # a fresh owner token and record in this HOME
    for k in ("CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD",
              "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_DOCKER_IDLE_EXIT", "CLEARCOTE_DOCKER_CACHE_VOLUME",
              _docker.TEST_ONLY_ASSUME_MACOS):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(_docker, "_docker_cli", lambda: "docker")
    monkeypatch.setattr(_docker, "_run", fake)
    monkeypatch.setattr(_docker, "_follow_logs", lambda exe, cid: FakeLogs(fake), raising=False)
    return fake


@pytest.fixture
def cdp(docker):
    with _CdpStub() as stub:
        docker.cdp_port = stub.port
        yield stub


@pytest.fixture(autouse=True)
def _stop_sync_driver():
    yield
    if clearcote._pw is not None:
        clearcote._pw.stop()
        clearcote._pw = None


def e_names(argv):
    return [a for i, a in enumerate(argv) if i and argv[i - 1] == "-e"]


def labels(argv):
    return [a for i, a in enumerate(argv) if i and argv[i - 1] == "--label"]


def tar_members(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        return {m.name: (m, t.extractfile(m).read()) for m in t.getmembers()}


KEYED = {"license_key": "cc_lic_docker_test_key_1234",
         "proxy": {"server": "http://proxy.example:8080", "username": "u", "password": "p w"}}


def test_the_decision(monkeypatch):
    for k in ("CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", _docker.TEST_ONLY_ASSUME_MACOS):
        monkeypatch.delenv(k, raising=False)

    def on(platform, docker=None, env=(), **kw):
        monkeypatch.setattr(_docker, "sys", types.SimpleNamespace(platform=platform))
        with monkeypatch.context() as m:
            for k, v in env:
                m.setenv(k, v)
            return _docker.docker_requested(docker, kw)

    assert on("darwin") is True
    assert on("linux") is False and on("win32") is False
    assert on("darwin", docker=False) is False and on("darwin", docker="0") is False
    assert on("linux", docker=True) is True
    assert on("darwin", executable_path="/opt/cc/chrome") is False
    assert on("darwin", env=[("CLEARCOTE_DOCKER", "0")]) is False
    assert on("linux", env=[("CLEARCOTE_DOCKER", "1")]) is True
    assert on("darwin", env=[("CLEARCOTE_BINARY", "/opt/cc/chrome")]) is False
    assert on("linux", env=[(_docker.TEST_ONLY_ASSUME_MACOS, "1")]) is True


@needs_chromium
def test_macos_launch_runs_the_image_and_returns_a_working_browser(mac, docker):
    docker.marks = [serve_state("licensed", "http://proxy.example:8080", secrets=True)]
    with LocalChromium(CHROMIUM) as chromium:
        docker.cdp_port = int(chromium.http_url.rsplit(":", 1)[1])
        browser = clearcote.launch(
            fingerprint="seed-1", platform="windows", timezone="Europe/Berlin", hardware_concurrency=8,
            canvas_noise=False, headless=True, args=["--lang=de-DE"], humanize=True, **KEYED)
        try:
            assert type(browser).__name__ == "Browser"  # the same Playwright type a local launch returns
            assert browser.is_connected()
            assert browser.docker_container == {"id": "c0ffee1234", "image": IMAGE, "serve_protocol": 2,
                                                "endpoint": f"http://127.0.0.1:{docker.cdp_port}"}
            page = browser.new_page()
            assert getattr(page, "_clearcote_persona", None) is not None  # humanized, as locally
            page.set_content("<title>in docker</title>")
            assert page.title() == "in docker"
            assert page.viewport_size is None  # no emulated viewport over the container's real window
        finally:
            browser.close()
        assert not browser.is_connected()
        assert chromium.alive()  # close() disconnects; ending the browser is the container's stop
    assert docker.commands() == ["info", "ps", "image", "create", "cp", "start", "port", "stop", "rm"]
    create = docker.call("create")
    argv = create["argv"]
    # --rm: a stopped container (and its anonymous engine volume) goes away by itself
    assert argv[:6] == ["docker", "create", "--rm", "--platform", "linux/amd64", "--shm-size"]
    assert argv[argv.index("-p") + 1] == "127.0.0.1::9222"  # loopback only: CDP is full control
    assert argv[-1] == IMAGE
    assert argv[argv.index("-v") + 1] == "clearcote-cache:/opt/xdg-cache"  # licensed engine downloads once
    # owned through this process's token: the next launch removes it once this process is gone
    lab = labels(argv)
    assert lab[:2] == ["com.clearcotelabs.sdk-launch=1", f"com.clearcotelabs.owner-host={socket.gethostname()}"]
    token = lab[2].split("=", 1)[1]
    assert lab[2].startswith("com.clearcotelabs.owner-token=") and len(token) == 32
    record = json.loads((_docker._owners_dir() and open(os.path.join(_docker._owners_dir(), token + ".json")).read()))
    assert record["pid"] == os.getpid() and record["token"] == token
    # values travel in the environment, never on the command line; the secrets not even there
    assert all("=" not in a for a in e_names(argv))
    assert not any("cc_lic_docker_test_key_1234" in a or "p w" in a or "p%20w" in a for a in argv)
    assert create["env"] == {
        "CC_FINGERPRINT": "seed-1", "CC_PLATFORM": "windows", "CC_TIMEZONE": "Europe/Berlin",
        "CC_HARDWARE_CONCURRENCY": "8", "CC_CANVAS_NOISE": "0", "CC_HEADLESS": "1",
        "CC_EXTRA_ARGS": "--lang=de-DE", "CC_IDLE_EXIT_SECONDS": "30",
        "CC_SECRETS_FILE": "/tmp/clearcote-secrets.json",
    }
    assert sorted(e_names(argv)) == sorted(create["env"])
    # the licence key and the proxy URL go in as a file only the image's user can read
    cp = docker.call("cp")
    assert cp["argv"][2:] == ["-", "c0ffee1234:/tmp"] and cp["env"] == {}
    member, data = tar_members(cp["input"])["clearcote-secrets.json"]
    assert (member.uid, member.gid, member.mode) == (10001, 10001, 0o600)
    assert json.loads(data) == {"CLEARCOTE_LICENSE_KEY": "cc_lic_docker_test_key_1234",
                                "CC_PROXY": "http://u:p%20w@proxy.example:8080"}
    assert [c["argv"][2:] for c in docker.calls if c["argv"][1] in ("stop", "rm")] == [
        ["--time", "10", "c0ffee1234"], ["-f", "-v", "c0ffee1234"]]  # -v: the image's anonymous VOLUME too


@needs_chromium
def test_async_launch_on_macos(mac, docker, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_DOCKER_IDLE_EXIT", "0")  # 0: never stops on its own
    docker.marks = [serve_state(idle=0)]

    async def go():
        with LocalChromium(CHROMIUM) as chromium:
            docker.cdp_port = int(chromium.http_url.rsplit(":", 1)[1])
            browser = await async_api.launch(docker_image="example/clearcote:test")
            try:
                assert browser.docker_container["id"] == "c0ffee1234"
                page = await browser.new_page()
                await page.set_content("<title>async</title>")
                assert await page.title() == "async"
            finally:
                await browser.close()
            assert not browser.is_connected()

    asyncio.run(go())
    assert docker.call("create")["argv"][-1] == "example/clearcote:test"
    assert docker.call("create")["env"] == {}  # nothing asked for: the image's own defaults
    assert "-v" not in docker.call("create")["argv"]  # no licence, no engine cache volume
    assert "cp" not in docker.commands()  # no secrets, no file
    assert docker.commands()[-2:] == ["stop", "rm"]


# ── what an image understands, and what it applied ─────────────────────────────────────────────────

def test_an_image_without_the_protocol_label_gets_plain_variables_and_a_warning(mac, docker, cdp, capsys):
    # An image older than sdk-0.40.0 ignores CC_SECRETS_FILE: handing it the key and proxy as a file would
    # start it on the open engine with no proxy. It gets the variables it understands instead, and a warning.
    docker.labels = {}
    docker.marks = ["[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)",
                    "[clearcote] proxy: http://proxy.example:8080"]
    container = _docker.start_container(dict(KEYED))
    assert container["serve_protocol"] == 0
    create = docker.call("create")
    assert create["env"]["CLEARCOTE_LICENSE_KEY"] == "cc_lic_docker_test_key_1234"
    assert create["env"]["CC_PROXY"] == "http://u:p%20w@proxy.example:8080"
    assert "CC_SECRETS_FILE" not in create["env"] and "CC_IDLE_EXIT_SECONDS" not in create["env"]
    assert "cp" not in docker.commands()
    assert not any("cc_lic_docker_test_key_1234" in a or "p%20w" in a for a in create["argv"])  # still not argv
    err = capsys.readouterr().err
    assert "predates sdk-0.40.0" in err and "docker inspect" in err and "will not stop on its own" in err
    _docker._remove("docker", container["id"])


@pytest.mark.parametrize("protocol,marks,problem", [
    (2, [serve_state("open", "http://proxy.example:8080", secrets=True)], "it runs the open engine"),
    (2, [serve_state("licensed", None, secrets=True)], "did not apply it"),
    (2, [], "did not report what it applied"),
    (0, ["[clearcote] engine: /opt/xdg-cache/clearcote/v0.1.0-pre.23/browser/chrome (free)",
         "[clearcote] proxy: http://proxy.example:8080"], "it runs the open engine"),
    (0, ["[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)"],
     "did not apply it"),  # an image from before CC_PROXY: the traffic would go direct
    (0, ["[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)",
         "[clearcote] proxy: http://other.example:3128"], "a different proxy"),
])
def test_a_container_that_did_not_apply_the_key_or_proxy_is_refused_and_removed(mac, docker, cdp, protocol, marks, problem):
    docker.labels = dict(PROTOCOL) if protocol else {}
    docker.marks = marks
    with pytest.raises(RuntimeError, match="did not apply what launch\\(\\) asked for") as e:
        _docker.start_container(dict(KEYED), quiet=True)
    assert problem in str(e.value) and "stopped and removed" in str(e.value)
    assert docker.commands()[-2:] == ["stop", "rm"]
    assert "cc_lic_docker_test_key_1234" not in str(e.value) and "p%20w" not in str(e.value)
    assert _docker._LIVE == {}


def test_a_missing_image_is_pulled_before_its_protocol_is_read(mac, docker, cdp):
    docker.labels = None
    container = _docker.start_container({}, quiet=True)
    cmds = docker.commands()
    assert cmds[:6] == ["info", "ps", "image", "pull", "image", "create"]
    assert docker.call("pull")["argv"][2:] == ["--platform", "linux/amd64", IMAGE]
    assert container["serve_protocol"] == 2
    _docker._remove("docker", container["id"])


def test_an_image_not_published_yet_says_what_to_do(mac, docker):
    docker.labels = None
    docker.pull_error = (f'Error response from daemon: failed to resolve reference "docker.io/{IMAGE}": '
                         f"docker.io/{IMAGE}: not found\n")  # Docker 29's wording
    with pytest.raises(RuntimeError) as e:
        clearcote.launch()
    msg = str(e.value)
    assert f"the Clearcote image {IMAGE} is not available" in msg
    assert "published a few minutes after its SDK release" in msg
    assert "older than sdk-0.40.0 still works, but" in msg and "docker inspect" in msg  # the trade-off, said
    assert "create" not in docker.commands()


@pytest.mark.parametrize("err", [
    "Error response from daemon: manifest for x:y not found: manifest unknown: manifest unknown",
    "Error response from daemon: pull access denied for x, repository does not exist or may require 'docker login'",
])
def test_older_docker_wordings_for_a_missing_image(mac, docker, err):
    docker.labels = None
    docker.pull_error = err
    with pytest.raises(RuntimeError, match="is not available"):
        clearcote.launch()


# ── who owns a container ────────────────────────────────────────────────────────────────────────────

def _record(token, **fields):
    os.makedirs(_docker._owners_dir(), exist_ok=True)
    path = os.path.join(_docker._owners_dir(), token + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(dict({"token": token}, **fields), fh)
    return path


def test_sweep_removes_only_containers_whose_owner_is_certainly_gone(mac, docker, monkeypatch):
    me = os.getpid()
    t = {k: k * 32 for k in "abcdef"}
    gone = _record(t["a"], pid=DEAD_PID, start=None)                              # owner gone: swept
    _record(t["b"], pid=me, start=_docker.process_start(me))                      # owner alive: kept
    reused = _record(t["c"], pid=me, start="linux:1")                             # pid reused: swept
    _record(t["d"], pid=DEAD_PID, pidns="pid:[4026531836]-elsewhere")             # another PID namespace: kept
    # t["e"]: no record here (another machine, a container given the Docker socket, WSL2): kept
    docker.ps = "".join(f"{c * 6}\t{t[c]}\n" for c in "abcde") + "fff111\t\n"     # no token (older SDK): kept
    monkeypatch.setattr(_docker, "_pid_namespace", lambda: "pid:[4026531836]")
    assert _docker.sweep_stale("docker") == ["aaaaaa", "cccccc"]
    assert [c["argv"][2:] for c in docker.calls if c["argv"][1] in ("stop", "rm")] == [
        ["--time", "10", "aaaaaa", "cccccc"], ["-f", "-v", "aaaaaa", "cccccc"]]
    assert not os.path.exists(gone) and not os.path.exists(reused)  # their records go too


def test_a_process_in_another_namespace_with_this_host_name_is_left_alone(mac, docker):
    # The reviewer's case: a process in a container started with --network host and the Docker socket has
    # this machine's host name, and a pid that may not exist here. Host name + pid called it dead and
    # removed its live container. Its owner record is in its own filesystem, not here: never touched.
    docker.containers = [{"id": "live99", "com.clearcotelabs.sdk-launch": "1",
                          "com.clearcotelabs.owner-host": socket.gethostname(),
                          "com.clearcotelabs.owner-pid": str(DEAD_PID),  # its pid, in its own namespace
                          "com.clearcotelabs.owner-token": "9" * 32}]
    assert _docker.sweep_stale("docker") == []
    assert not any(c["argv"][1] in ("stop", "rm") for c in docker.calls)


def test_owner_record(mac):
    token = _docker.owner_token()
    assert token == _docker.owner_token()  # one per process
    rec = json.loads(open(os.path.join(_docker._owners_dir(), token + ".json"), encoding="utf-8").read())
    assert rec["pid"] == os.getpid() and rec["sdk"] == "python"
    assert rec["start"] == _docker.process_start(os.getpid()) and rec["start"]
    assert _docker.owner_alive(rec) is True
    assert _docker.owner_alive(dict(rec, start="linux:1")) is False  # the same pid, another process
    assert _docker.owner_alive(dict(rec, pid=DEAD_PID)) is False


def test_pid_alive():
    assert _docker._pid_alive(os.getpid()) is True
    assert _docker._pid_alive(DEAD_PID) is False


def test_idle_exit_and_cache_volume_can_be_tuned(mac, docker, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_DOCKER_IDLE_EXIT", "7")
    monkeypatch.setenv("CLEARCOTE_DOCKER_CACHE_VOLUME", "my-cc-cache")
    docker.running = False
    with pytest.raises(RuntimeError):
        clearcote.launch(license_key="cc_lic_x")
    argv = docker.call("create")["argv"]
    assert docker.call("create")["env"]["CC_IDLE_EXIT_SECONDS"] == "7"
    assert argv[argv.index("-v") + 1] == "my-cc-cache:/opt/xdg-cache"
    monkeypatch.setenv("CLEARCOTE_DOCKER_IDLE_EXIT", "soon")
    with pytest.raises(ValueError, match="CLEARCOTE_DOCKER_IDLE_EXIT"):
        clearcote.launch()


def test_docker_not_installed(mac, monkeypatch):
    calls = []
    monkeypatch.setattr(_docker, "_docker_cli", lambda: None)
    monkeypatch.setattr(_docker, "_run", lambda *a, **k: calls.append(a) or (0, "", ""))
    with pytest.raises(clearcote.DockerUnavailableError) as e:
        clearcote.launch()
    msg = str(e.value)
    assert "no native macOS build" in msg and "`docker` command was not found" in msg
    assert "Install Docker Desktop" in msg and "docker=False" in msg and "CLEARCOTE_DOCKER=0" in msg
    assert calls == []


def test_docker_not_running(mac, docker):
    docker.info_error = ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                         "Is the docker daemon running?\n")
    with pytest.raises(clearcote.DockerUnavailableError) as e:
        clearcote.launch()
    msg = str(e.value)
    assert "Docker is not running" in msg and "Cannot connect to the Docker daemon" in msg
    assert "Start Docker Desktop" in msg
    assert docker.commands() == ["info"]


def test_options_the_image_cannot_take_are_refused_before_docker_runs(mac, docker):
    with pytest.raises(ValueError, match=r"^user_data_dir is not available when launch\(\) runs Clearcote in Docker"):
        clearcote.launch(user_data_dir="/tmp/profile")
    with pytest.raises(ValueError, match=r"^extensions is not available"):
        clearcote.launch(extensions=["/tmp/ext"])
    with pytest.raises(ValueError, match="whitespace"):
        clearcote.launch(args=["--window-name=two words"])
    assert docker.calls == []


def test_docker_false_and_a_named_binary_keep_the_local_launch(mac, docker, tmp_path, monkeypatch):
    missing = str(tmp_path / "no-such-chrome")
    for kw in ({"docker": False, "executable_path": missing}, {"executable_path": missing}):
        with pytest.raises(FileNotFoundError, match="Clearcote binary not found"):
            clearcote.launch(**kw)
    monkeypatch.setenv("CLEARCOTE_DOCKER", "0")
    monkeypatch.setenv("CLEARCOTE_BINARY", missing)
    with pytest.raises(FileNotFoundError, match="Clearcote binary not found"):
        clearcote.launch()
    assert docker.calls == []  # the local path, untouched


def test_container_that_stops_early_reports_its_logs_and_is_removed(mac, docker):
    docker.running = False
    docker.logs = "[clearcote] ERROR: could not lease a run token (LicenseError: Invalid license key.).\n"
    with pytest.raises(RuntimeError, match="stopped before its browser came up") as e:
        clearcote.launch(license_key="cc_lic_bad")
    assert "could not lease a run token" in str(e.value)
    assert docker.commands()[-2:] == ["stop", "rm"]
    assert _docker._LIVE == {}


def test_create_failure_names_the_image(mac, docker, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_DOCKER_IMAGE", "example/other:tag")
    docker.create_error = "docker: Error response from daemon: Conflict. The container name is already in use.\n"
    with pytest.raises(RuntimeError, match=r"`docker create example/other:tag` failed: docker: Error response"):
        clearcote.launch()


def test_a_failed_connect_after_the_container_started_removes_it(mac, docker, cdp):
    with pytest.raises(Exception):  # noqa: B017 -- Playwright's own connect error
        clearcote.launch(timeout=5000)
    assert docker.commands()[-2:] == ["stop", "rm"]
    assert [c["argv"][-1] for c in docker.calls if c["argv"][1] in ("stop", "rm")] == ["c0ffee1234", "c0ffee1234"]
    assert _docker._LIVE == {}


@needs_chromium
def test_a_failure_after_the_connect_disconnects_and_removes_it(mac, docker, monkeypatch):
    from clearcote import _humanize

    def broken(*_a, **_k):
        raise RuntimeError("humanize setup failed")

    monkeypatch.setattr(_humanize, "install_humanize", broken)
    with LocalChromium(CHROMIUM) as chromium:
        docker.cdp_port = int(chromium.http_url.rsplit(":", 1)[1])
        with pytest.raises(RuntimeError, match="humanize setup failed"):
            clearcote.launch()
        assert chromium.alive()
    assert docker.commands()[-2:] == ["stop", "rm"]
    assert _docker._LIVE == {}


def test_cloud_launch_still_wins(mac, docker, monkeypatch):
    # launch(cloud=True) is the hosted browser; the docker path is a local launch only
    with pytest.raises(ValueError, match="docker is not available for cloud browsers"):
        clearcote.launch(cloud=True, docker=True)
    assert docker.calls == []
