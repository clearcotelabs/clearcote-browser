"""launch()'s macOS path against the REAL Clearcote image: docker run, CDP, a page that loads, the
persona options reaching the engine, and no container left after close().

Off by default. Set CLEARCOTE_TEST_DOCKER_IMAGE to the image to run (and have a working docker); the test
sets CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 so the macOS decision itself is what sends launch() to Docker.
Mirrors sdk/node/test/docker-launch.live.test.ts and DockerLaunchLiveTests.cs."""
import os
import shutil
import subprocess
import time

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


# A proxy the test can see into: a small CONNECT proxy run from the same image (it has Python) on Docker's
# default network, which the browser's container reaches by address. It logs every tunnel it opens, and with
# a username and password it turns away (407) whatever does not log in -- so these tests check for themselves
# that the traffic went through the proxy and logged in, instead of trusting what launch() checked.
PROXY_SCRIPT = r"""
import base64, socket, sys, threading
need = "Basic " + base64.b64encode(("%s:%s" % (sys.argv[1], sys.argv[2])).encode()).decode() if len(sys.argv) > 2 else None
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
def handle(c):
    f = c.makefile("rb")
    while True:
        line = f.readline()
        if not line:
            return
        head = []
        while True:
            h = f.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            head.append(h)
        method, target = line.decode("latin-1").split()[:2]
        auth = [h.split(b":", 1)[1].strip().decode() for h in head if h.lower().startswith(b"proxy-authorization:")]
        if need and auth != [need]:
            print("REFUSED", method, target, flush=True)
            c.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="cc"\r\nContent-Length: 0\r\n\r\n')
            continue
        print("TUNNEL", method, target, "logged-in" if need else "open", flush=True)
        if method != "CONNECT":
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        host, port = target.rsplit(":", 1)
        u = socket.create_connection((host, int(port)), timeout=20)
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=pipe, args=(u, c), daemon=True).start()
        pipe(c, u)
        return
srv = socket.create_server(("0.0.0.0", 3128))
print("READY", flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
"""


def dk(*args, check=True):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check).stdout


def start_proxy(username=None, password=None):
    """The test proxy -> (server URL, container id, its log as text)."""
    creds = [username, password] if username else []
    cid = dk("run", "-d", "--rm", "--entrypoint", "python", IMAGE, "-u", "-c", PROXY_SCRIPT, *creds).strip()
    for _ in range(100):
        if "READY" in dk("logs", cid, check=False):
            break
        time.sleep(0.1)
    ip = dk("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cid).strip()
    return f"http://{ip}:3128", cid, lambda: dk("logs", cid, check=False)


def engine_of(cid):
    """The browser binary the container runs, read from its process list (not from its own log)."""
    for line in dk("top", cid, "-eo", "pid,args").splitlines()[1:]:  # docker top wants a pid column
        args = line.split(None, 1)[1] if len(line.split(None, 1)) > 1 else ""
        exe = args.split()[0] if args.split() else ""
        if exe.endswith("/chrome") and "--type=" not in args:
            return exe
    return None


def test_a_proxy_with_a_password_carries_the_traffic():
    # Before sdk-0.40.0's image, the password of an http(s) proxy was dropped: the browser was challenged,
    # nothing answered, every navigation failed. An image that old is refused it before it starts.
    server, proxy_cid, proxy_log = start_proxy("cc-user", "p@ss:w rd")
    try:
        proxy = {"server": server, "username": "cc-user", "password": "p@ss:w rd"}
        if image_protocol() < 2:
            with pytest.raises(RuntimeError, match="predates sdk-0.40.0 and cannot use an HTTP proxy"):
                clearcote.launch(proxy=proxy, quiet=True)
            return
        b = clearcote.launch(proxy=proxy, quiet=True)
        try:
            cid = b.docker_container["id"]
            page = b.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=60_000)
            assert page.title() == "Example Domain"
            assert "TUNNEL CONNECT example.com:443 logged-in" in proxy_log()
            assert "/pro-" not in (engine_of(cid) or "/pro-")  # no key: the open engine (and one was found)
        finally:
            b.close()
        assert not container_exists(cid)
    finally:
        dk("rm", "-f", "-v", proxy_cid, check=False)


def image_protocol():
    return _docker.image_protocol("docker", IMAGE, quiet=True)


# Every chrome process in a container: its user namespace and its command line, read from the container's /proc.
CHROME_PROCESSES = r"""for p in /proc/[0-9]*; do
  a=$(tr '\0' ' ' < $p/cmdline 2>/dev/null)
  case "$a" in */chrome\ *) echo "$(readlink $p/ns/user) $a" ;; esac
done"""


def chrome_processes(cid):
    out = dk("exec", cid, "sh", "-c", CHROME_PROCESSES)
    return [tuple(line.split(" ", 1)) for line in out.splitlines() if " " in line]


def sandbox_page(browser):
    page = browser.new_page()
    page.goto("chrome://sandbox")
    page.wait_for_function("() => /adequately sandboxed/.test(document.body.innerText)", timeout=30_000)
    return page.evaluate("() => document.body.innerText")


def test_chrome_runs_with_its_sandbox():
    # launch() starts the image with the seccomp profile, and its Chrome runs sandboxed: no --no-sandbox on any
    # chrome process, renderers in user namespaces of their own, and Chrome's own sandbox page agrees. An image
    # older than serve protocol 3 runs Chrome with --no-sandbox whatever it gets: there this fails.
    b = clearcote.launch(quiet=True)
    try:
        cid = b.docker_container["id"]
        procs = chrome_processes(cid)
        assert procs and not any("--no-sandbox" in args for _ns, args in procs)
        browser_ns = next(ns for ns, args in procs if "--type=" not in args)
        renderers = [ns for ns, args in procs if "--type=renderer" in args]
        assert renderers and all(ns != browser_ns for ns in renderers)
        text = sandbox_page(b)
        assert "You are adequately sandboxed." in text and "Layer 1 Sandbox\tNamespace" in text
    finally:
        b.close()
    assert not container_exists(cid)


def test_without_the_profile_the_container_still_serves_without_the_sandbox():
    # A plain `docker run`, no profile: Docker's default refuses the sandbox's namespaces. The container must come
    # up as it always did (Chrome with --no-sandbox), and say why and how to turn the sandbox on.
    cid = dk("run", "-d", "--rm", "-p", "127.0.0.1::9222", IMAGE).strip()
    try:
        port = int(dk("port", cid, "9222/tcp").splitlines()[0].rsplit(":", 1)[1])
        for _ in range(240):
            if _docker._cdp_ready(port):
                break
            time.sleep(0.5)
        assert _docker._cdp_ready(port), dk("logs", cid, check=False)
        procs = chrome_processes(cid)
        assert procs and all("--no-sandbox" in args for _ns, args in procs)
        if image_protocol() >= 3:
            logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True).stdout
            [line] = [ln for ln in logs.splitlines() if ln.startswith("[clearcote] sandbox:")]
            assert line.startswith("[clearcote] sandbox: OFF, Chrome runs with --no-sandbox: clone(CLONE_NEWUSER) failed")
            assert "seccomp profile blocks" in line and "--security-opt seccomp=" in line
        pw = clearcote._playwright()
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        try:
            page = b.contexts[0].new_page()
            page.goto("data:text/html,<title>no profile</title>")
            assert page.title() == "no profile"
        finally:
            b.close()
    finally:
        dk("rm", "-f", "-v", cid, check=False)


# Licensed and proxied, against whatever image CLEARCOTE_TEST_DOCKER_IMAGE names (one from before sdk-0.40.0
# too): launch() must come back on the licensed engine with the proxy applied -- or refuse -- never on the open
# engine with traffic going direct. The test checks both itself: the engine from the container's process
# list, the traffic from the test proxy's log. The proxy wants a password from an image that can log in to
# one (sdk-0.40.0 and newer); an older image gets one without. Needs CLEARCOTE_TEST_DOCKER_KEY (a real key)
# and, optionally, CLEARCOTE_TEST_DOCKER_CACHE_VOLUME (a scratch volume for the licensed engine).
KEY = os.environ.get("CLEARCOTE_TEST_DOCKER_KEY")


@pytest.mark.skipif(not KEY, reason="set CLEARCOTE_TEST_DOCKER_KEY")
def test_licensed_and_proxied_launch_is_never_downgraded(monkeypatch):
    if os.environ.get("CLEARCOTE_TEST_DOCKER_CACHE_VOLUME"):
        monkeypatch.setenv("CLEARCOTE_DOCKER_CACHE_VOLUME", os.environ["CLEARCOTE_TEST_DOCKER_CACHE_VOLUME"])
    login = ("cc-user", "p@ss:w rd") if image_protocol() >= 2 else (None, None)
    server, proxy_cid, proxy_log = start_proxy(*login)
    try:
        proxy = {"server": server, **({"username": login[0], "password": login[1]} if login[0] else {})}
        b = clearcote.launch(license_key=KEY, proxy=proxy, quiet=True, timeout=600_000)
        try:
            info = b.docker_container
            env = dk("inspect", "-f", "{{json .Config.Env}}", info["id"])
            # protocol 2 keeps the key out of the container's configuration; an older image gets it as a variable.
            # (A bool first: pytest would print both operands of a failing `in`, the key among them.)
            key_visible = KEY in env
            assert key_visible == (info["serve_protocol"] < 2)
            assert "/pro-" in (engine_of(info["id"]) or "")  # the licensed engine is what runs
            page = b.new_page()
            page.goto("https://example.com/", wait_until="domcontentloaded", timeout=60_000)
            assert page.title() == "Example Domain"
            assert ("TUNNEL CONNECT example.com:443 " + ("logged-in" if login[0] else "open")) in proxy_log()
        finally:
            b.close()
        assert not container_exists(info["id"])
    finally:
        dk("rm", "-f", "-v", proxy_cid, check=False)
