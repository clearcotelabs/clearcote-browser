"""launch() on macOS runs the Clearcote Docker image (there is no native macOS build).

The host platform is mocked (the module's view of ``sys.platform``) and the docker CLI is replaced by a
recorder that answers like Docker would; the "container's" CDP endpoint is a real local Chromium, so
the SDK's connect, close and the Playwright objects it returns are all real. The real image is
exercised end to end separately (CLEARCOTE_TEST_ONLY_ASSUME_MACOS, see test_docker_launch_live.py).
Mirrors sdk/node/test/docker-launch.test.ts."""
import asyncio
import io
import json
import os
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


class FakeDocker:
    """Stands in for the docker CLI: records every call (argv, the environment it was given, stdin)."""

    def __init__(self):
        self.calls = []
        self.cdp_port = 1
        self.running = True
        self.info_error = None
        self.create_error = None
        self.logs = ""
        self.ps = ""  # `docker ps` rows: id \t owner-host \t owner-pid

    def __call__(self, argv, env=None, timeout=120.0, input=None):
        self.calls.append({"argv": list(argv), "env": dict(env or {}), "input": input})
        cmd = argv[1]
        if cmd == "info":
            return (1, "", self.info_error) if self.info_error else (0, "29.1.3\n", "")
        if cmd == "ps":
            return 0, self.ps, ""
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
        self.stopped = False

    def text(self, wait=3.0):
        return self.docker.logs

    def stop(self):
        self.stopped = True


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """The host is macOS, as far as the docker decision is concerned (and only there). HOME is a
    throwaway, so this machine's own saved licence key is never read."""
    monkeypatch.setattr(_docker, "sys", types.SimpleNamespace(platform="darwin", stderr=_docker.sys.stderr))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
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
    with LocalChromium(CHROMIUM) as chromium:
        docker.cdp_port = int(chromium.http_url.rsplit(":", 1)[1])
        browser = clearcote.launch(
            fingerprint="seed-1", platform="windows", timezone="Europe/Berlin", hardware_concurrency=8,
            canvas_noise=False, headless=True, args=["--lang=de-DE"], humanize=True,
            proxy={"server": "http://proxy.example:8080", "username": "u", "password": "p w"},
            license_key="cc_lic_docker_test_key_1234")
        try:
            assert type(browser).__name__ == "Browser"  # the same Playwright type a local launch returns
            assert browser.is_connected()
            assert browser.docker_container == {"id": "c0ffee1234", "image": IMAGE,
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
    assert docker.commands() == ["info", "ps", "create", "cp", "start", "port", "stop", "rm"]
    create = docker.call("create")
    argv = create["argv"]
    # --rm: a stopped container (and its anonymous engine volume) goes away by itself
    assert argv[:6] == ["docker", "create", "--rm", "--platform", "linux/amd64", "--shm-size"]
    assert argv[argv.index("-p") + 1] == "127.0.0.1::9222"  # loopback only: CDP is full control
    assert argv[-1] == IMAGE
    assert argv[argv.index("-v") + 1] == "clearcote-cache:/opt/xdg-cache"  # licensed engine downloads once
    # owned: the next launch on this machine removes it if this process is gone
    assert labels(argv) == ["com.clearcotelabs.sdk-launch=1", f"com.clearcotelabs.owner-host={socket.gethostname()}",
                            f"com.clearcotelabs.owner-pid={os.getpid()}"]
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


def test_stale_containers_of_dead_owners_are_swept_at_the_next_launch(mac, docker, monkeypatch):
    here = socket.gethostname()
    docker.ps = (f"aaa111\t{here}\t{DEAD_PID}\n"          # this machine, owner gone: swept
                 f"bbb222\t{here}\t{os.getpid()}\n"        # this machine, owner alive: kept
                 f"ccc333\tsome-other-host\t{DEAD_PID}\n"   # another machine on the same daemon: kept
                 f"ddd444\t\t\n")                           # no owner labels: kept
    docker.running = False  # this launch then fails; only the sweep matters here
    with pytest.raises(RuntimeError):
        clearcote.launch()
    ps = docker.call("ps")["argv"]
    assert ps[2:5] == ["-a", "--filter", "label=com.clearcotelabs.sdk-launch=1"]
    swept = [c["argv"][2:] for c in docker.calls if c["argv"][1] in ("stop", "rm")][:2]
    assert swept == [["--time", "10", "aaa111"], ["-f", "-v", "aaa111"]]
    assert docker.commands().index("ps") < docker.commands().index("create")


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


def test_an_image_not_published_yet_says_what_to_do(mac, docker):
    docker.create_error = (f"Unable to find image '{IMAGE}' locally\nError response from daemon: manifest for "
                           f"{IMAGE} not found: manifest unknown: manifest unknown\n")
    with pytest.raises(RuntimeError) as e:
        clearcote.launch()
    msg = str(e.value)
    assert f"the Clearcote image {IMAGE} is not available" in msg
    assert "published a few minutes after each SDK release" in msg
    assert "CLEARCOTE_DOCKER_IMAGE=teamflatearth/clearcote:latest" in msg
    assert docker.commands() == ["info", "ps", "create"]


def test_create_failure_names_the_image(mac, docker, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_DOCKER_IMAGE", "example/other:tag")
    docker.create_error = "docker: Error response from daemon: Conflict. The container name is already in use.\n"
    with pytest.raises(RuntimeError, match=r"`docker create example/other:tag` failed: docker: Error response"):
        clearcote.launch()


class _BogusCdp:
    """Answers /json/version like a browser whose WebSocket is not there: the connect fails after the
    container has started."""

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


def test_a_failed_connect_after_the_container_started_removes_it(mac, docker):
    with _BogusCdp() as cdp:
        docker.cdp_port = cdp.port
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
