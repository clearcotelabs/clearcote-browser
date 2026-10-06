"""launch() on macOS runs the Clearcote Docker image (there is no native macOS build).

The host platform is mocked (the module's view of ``sys.platform``) and the docker CLI is replaced by a
recorder that answers like Docker would; the "container's" CDP endpoint is a real local Chromium, so
the SDK's connect, close and the Playwright objects it returns are all real. The real image is
exercised end to end separately (CLEARCOTE_TEST_ONLY_ASSUME_MACOS, see test_docker_launch_live.py).
Mirrors sdk/node/test/docker-launch.test.ts."""
import asyncio
import types

import pytest

import clearcote
from clearcote import _docker, async_api

from _chromium import LocalChromium, find_chromium

CHROMIUM = find_chromium()  # found now: the fixtures below move HOME, where it is looked for
needs_chromium = pytest.mark.skipif(not CHROMIUM, reason="no Chromium (set CLEARCOTE_TEST_BINARY)")


class FakeDocker:
    """Stands in for the docker CLI: records every call (argv + the environment it was given)."""

    def __init__(self, cdp_port=None, running=True, info_error=None, run_error=None, logs=""):
        self.calls = []
        self.cdp_port = cdp_port
        self.running = running
        self.info_error = info_error
        self.run_error = run_error
        self.logs = logs

    def __call__(self, argv, env=None, timeout=120.0):
        self.calls.append({"argv": list(argv), "env": dict(env or {})})
        cmd = argv[1]
        if cmd == "info":
            return (1, "", self.info_error) if self.info_error else (0, "29.1.3\n", "")
        if cmd == "run":
            return (125, "", self.run_error) if self.run_error else (0, "c0ffee1234\n", "")
        if cmd == "port":
            return 0, f"127.0.0.1:{self.cdp_port or 1}\n[::1]:{self.cdp_port or 1}\n", ""
        if cmd == "inspect":
            return 0, ("true" if self.running else "false") + "\n", ""
        if cmd == "logs":
            return 0, self.logs, ""
        return 0, "", ""  # stop, rm

    def commands(self):
        return [c["argv"][1] for c in self.calls]

    def run_call(self):
        return next(c for c in self.calls if c["argv"][1] == "run")


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """The host is macOS, as far as the docker decision is concerned (and only there). HOME is a
    throwaway, so this machine's own saved licence key is never read."""
    monkeypatch.setattr(_docker, "sys", types.SimpleNamespace(platform="darwin", stderr=_docker.sys.stderr))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for k in ("CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD",
              "CLEARCOTE_LICENSE_KEY", _docker.TEST_ONLY_ASSUME_MACOS):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(_docker, "_docker_cli", lambda: "docker")
    monkeypatch.setattr(_docker, "_run", fake)
    return fake


@pytest.fixture(autouse=True)
def _stop_sync_driver():
    yield
    if clearcote._pw is not None:
        clearcote._pw.stop()
        clearcote._pw = None


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
            assert browser.docker_container == {"id": "c0ffee1234", "image": f"teamflatearth/clearcote:sdk-{clearcote.__version__}",
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
    assert docker.commands() == ["info", "run", "port", "stop", "rm"]
    run = docker.run_call()
    argv = run["argv"]
    assert argv[:5] == ["docker", "run", "-d", "--platform", "linux/amd64"]
    assert argv[argv.index("-p") + 1] == "127.0.0.1::9222"  # loopback only: CDP is full control
    assert argv[-1] == f"teamflatearth/clearcote:sdk-{clearcote.__version__}"
    assert argv[argv.index("-v") + 1] == "clearcote-cache:/opt/xdg-cache"  # licensed engine downloads once
    # values travel in the environment, never on the command line
    assert all("=" not in a for i, a in enumerate(argv) if i and argv[i - 1] == "-e")
    assert not any("cc_lic_docker_test_key_1234" in a or "p w" in a or "p%20w" in a for a in argv)
    assert run["env"] == {
        "CC_FINGERPRINT": "seed-1", "CC_PLATFORM": "windows", "CC_TIMEZONE": "Europe/Berlin",
        "CC_HARDWARE_CONCURRENCY": "8", "CC_CANVAS_NOISE": "0", "CC_HEADLESS": "1",
        "CC_EXTRA_ARGS": "--lang=de-DE", "CC_PROXY": "http://u:p%20w@proxy.example:8080",
        "CLEARCOTE_LICENSE_KEY": "cc_lic_docker_test_key_1234",
    }
    assert sorted(a for i, a in enumerate(argv) if i and argv[i - 1] == "-e") == sorted(run["env"])
    assert [c["argv"][2:] for c in docker.calls if c["argv"][1] in ("stop", "rm")] == [
        ["--time", "10", "c0ffee1234"], ["-f", "-v", "c0ffee1234"]]  # -v: the image's anonymous VOLUME too


@needs_chromium
def test_async_launch_on_macos(mac, docker):
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
    assert docker.run_call()["argv"][-1] == "example/clearcote:test"
    assert docker.run_call()["env"] == {}  # nothing asked for: the image's own defaults
    assert "-v" not in docker.run_call()["argv"]  # no licence, no engine cache volume
    assert docker.commands()[-2:] == ["stop", "rm"]


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


def test_docker_run_failure_names_the_image(mac, docker, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_DOCKER_IMAGE", "example/missing:tag")
    docker.run_error = "Unable to find image 'example/missing:tag' locally\nmanifest unknown\n"
    with pytest.raises(RuntimeError, match=r"docker run example/missing:tag` failed: Unable to find image"):
        clearcote.launch()
    assert docker.commands() == ["info", "run"]


def test_cloud_launch_still_wins(mac, docker, monkeypatch):
    # launch(cloud=True) is the hosted browser; the docker path is a local launch only
    with pytest.raises(ValueError, match="docker is not available for cloud browsers"):
        clearcote.launch(cloud=True, docker=True)
    assert docker.calls == []
