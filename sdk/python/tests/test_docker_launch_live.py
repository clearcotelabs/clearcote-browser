"""launch()'s macOS path against the REAL Clearcote image: docker run, CDP, a page that loads, the
persona options reaching the engine, and no container left after close().

Off by default. Set CLEARCOTE_TEST_DOCKER_IMAGE to the image to run (and have a working docker); the test
sets CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 so the macOS decision itself is what sends launch() to Docker.
Mirrors sdk/node/test/docker-launch.live.test.ts and DockerLaunchLiveTests.cs."""
import os
import shutil
import subprocess

import pytest

import clearcote
from clearcote import _docker

IMAGE = os.environ.get("CLEARCOTE_TEST_DOCKER_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE or not shutil.which("docker"),
                                reason="set CLEARCOTE_TEST_DOCKER_IMAGE (and have docker) to run the image")


@pytest.fixture(autouse=True)
def _assume_macos(monkeypatch, tmp_path):
    monkeypatch.setenv(_docker.TEST_ONLY_ASSUME_MACOS, "1")
    monkeypatch.setenv("CLEARCOTE_DOCKER_IMAGE", IMAGE)
    monkeypatch.setenv("HOME", str(tmp_path))  # no saved licence key: the image's open engine
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for k in ("CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_CLOUD"):
        monkeypatch.delenv(k, raising=False)
    yield
    if clearcote._pw is not None:
        clearcote._pw.stop()
        clearcote._pw = None


def container_exists(cid):
    out = subprocess.run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter", f"id={cid}"],
                         capture_output=True, text=True, check=True).stdout
    return bool(out.strip())


def anonymous_volumes(cid):
    """The image declares VOLUME /opt/xdg-cache: every container gets an anonymous volume holding a copy
    of the engine (~0.5 GB). It must go when the container does."""
    fmt = '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}'
    return subprocess.run(["docker", "inspect", "-f", fmt, cid], capture_output=True, text=True, check=True).stdout.split()


def volume_exists(name):
    return subprocess.run(["docker", "volume", "inspect", name], capture_output=True).returncode == 0


def probe(browser):
    page = browser.new_page()
    page.goto("data:text/html,<title>loaded in docker</title><p>hi</p>")
    return page, page.evaluate(
        "() => ({title: document.title, tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
        " platform: navigator.platform, ua: navigator.userAgent, w: innerWidth})")


def test_launch_on_macos_runs_the_real_image():
    # control: the image's own defaults
    plain = clearcote.launch(quiet=True)
    try:
        cid = plain.docker_container["id"]
        assert container_exists(cid)
        volumes = anonymous_volumes(cid)
        assert volumes and all(volume_exists(v) for v in volumes)
        assert type(plain).__name__ == "Browser"
        _page, base = probe(plain)
        assert base["title"] == "loaded in docker"
    finally:
        plain.close()
    assert not container_exists(cid)
    assert not any(volume_exists(v) for v in volumes)

    # treatment: the persona options reach the engine in the container
    b = clearcote.launch(fingerprint="docker-e2e-seed", platform="windows", timezone="Asia/Tokyo", quiet=True)
    try:
        page, got = probe(b)
        assert got["title"] == "loaded in docker"
        assert got["tz"] == "Asia/Tokyo" and got["tz"] != base["tz"]
        assert got["platform"] == "Win32" and base["platform"] != "Win32"
        assert "Windows NT" in got["ua"]
        assert page.viewport_size is None and got["w"] > 0
        # a real network page loads too (the container has egress)
        page.goto("https://example.com/", wait_until="domcontentloaded", timeout=60_000)
        assert page.title() == "Example Domain"
        cid = b.docker_container["id"]
        volumes = anonymous_volumes(cid)
    finally:
        b.close()
    assert not container_exists(cid)
    assert volumes and not any(volume_exists(v) for v in volumes)


# Licensed and proxied, against whatever image CLEARCOTE_TEST_DOCKER_IMAGE names (one from before sdk-0.40.0
# too): launch() must come back on the licensed engine with the proxy applied -- or refuse -- never on the open
# engine with traffic going direct. Needs a real key and a proxy the container can reach:
#   CLEARCOTE_TEST_DOCKER_KEY, CLEARCOTE_TEST_DOCKER_PROXY (e.g. http://172.17.0.1:3128 on Linux),
#   CLEARCOTE_TEST_DOCKER_CACHE_VOLUME (a scratch volume for the licensed engine).
KEY = os.environ.get("CLEARCOTE_TEST_DOCKER_KEY")
PROXY = os.environ.get("CLEARCOTE_TEST_DOCKER_PROXY")


@pytest.mark.skipif(not KEY or not PROXY, reason="set CLEARCOTE_TEST_DOCKER_KEY and CLEARCOTE_TEST_DOCKER_PROXY")
def test_licensed_and_proxied_launch_is_never_downgraded(monkeypatch):
    if os.environ.get("CLEARCOTE_TEST_DOCKER_CACHE_VOLUME"):
        monkeypatch.setenv("CLEARCOTE_DOCKER_CACHE_VOLUME", os.environ["CLEARCOTE_TEST_DOCKER_CACHE_VOLUME"])
    b = clearcote.launch(license_key=KEY, proxy=PROXY, quiet=True, timeout=600_000)
    try:
        info = b.docker_container
        env = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Env}}", info["id"]], capture_output=True,
                             text=True, check=True).stdout
        # protocol 2 keeps the key out of the container's configuration; an older image gets it as a variable.
        # (A bool first: pytest would print both operands of a failing `in`, the key among them.)
        key_visible = KEY in env
        assert key_visible == (info["serve_protocol"] < 2)
        page = b.new_page()
        page.goto("https://example.com/", wait_until="domcontentloaded", timeout=60_000)
        assert page.title() == "Example Domain"
    finally:
        b.close()
    assert not container_exists(info["id"])
