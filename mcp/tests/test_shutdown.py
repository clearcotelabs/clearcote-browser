"""The shared browser is closed whenever the server stops: its stdin closed, a stop signal (Ctrl+C, Ctrl+Break on
Windows, SIGTERM, SIGHUP), or an error. Otherwise the browser clearcote.serve() started keeps running after the server
is gone, with its profile and Playwright's folders left in the temp directory. No browser: a stand-in leaves a mark
when it is closed."""
import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading

import anyio
import pytest

from clearcote_mcp import server as S


class Browser:
    def __init__(self, delay=0.0):
        self.delay, self.closes = delay, 0

    async def close(self):
        await asyncio.sleep(self.delay)
        self.closes += 1


@pytest.fixture
def shared(monkeypatch):
    """A stand-in for the shared browser; nothing is launched at startup."""
    monkeypatch.setenv("CLEARCOTE_MCP_PREWARM", "0")
    monkeypatch.setattr(S, "_lock", None)

    def use(**kw):
        b = Browser(**kw)
        monkeypatch.setattr(S, "_browser", b)
        return b
    return use


def lifespan():
    return S.mcp.settings.lifespan(S.mcp)


def test_an_error_in_the_server_still_closes_the_browser(shared):
    b = shared()

    async def go():
        with pytest.raises(RuntimeError):
            async with lifespan():
                raise RuntimeError("the transport broke")
    asyncio.run(go())
    assert b.closes == 1 and S._browser is None


def test_a_stopped_server_finishes_closing_the_browser(shared):
    """A stop signal cancels everything in the server, the close included unless it is shielded."""
    b = shared(delay=0.3)

    async def go():
        async def serve():
            async with lifespan():
                await anyio.sleep(10)
        async with anyio.create_task_group() as tg:
            tg.start_soon(serve)
            await anyio.sleep(0.1)
            tg.cancel_scope.cancel()
    anyio.run(go)
    assert b.closes == 1 and S._browser is None


# The real server over stdio, as a client starts it, with a stand-in browser that leaves a mark when it is closed.
STAND_IN = r"""
import sys
from pathlib import Path
from clearcote_mcp import server

class Browser:
    async def close(self):
        Path(sys.argv[1]).write_text("closed")

server._browser = Browser()
server.main()
"""


class Server:
    def __init__(self, tmp_path, **popen):
        self.mark = tmp_path / "closed.txt"
        env = {k: v for k, v in os.environ.items() if k != "CLEARCOTE_API_KEY"}
        env["CLEARCOTE_MCP_PREWARM"] = "0"
        self.log = open(tmp_path / "stderr.txt", "wb")
        self.proc = subprocess.Popen([sys.executable, "-c", STAND_IN, str(self.mark)], cwd=tmp_path, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, **popen)
        self.lines = queue.Queue()
        threading.Thread(target=lambda: [self.lines.put(line) for line in self.proc.stdout], daemon=True).start()

    def send(self, message):
        self.proc.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode())
        self.proc.stdin.flush()

    def start(self):
        """Initialize, as a client does, and wait for the answer: the server is serving."""
        self.send({"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert json.loads(self.lines.get(timeout=60))["id"] == 1
        self.send({"method": "notifications/initialized"})
        return self

    def stopped(self, timeout):
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()


def test_closing_stdin_closes_the_browser(tmp_path):
    server = Server(tmp_path).start()
    try:
        server.proc.stdin.close()
        assert server.stopped(30) == 0
        assert server.mark.read_text() == "closed"
    finally:
        server.close()


STOP_SIGNALS = ([("Ctrl+Break", signal.CTRL_BREAK_EVENT, signal.SIGBREAK)] if sys.platform == "win32"
                else [("SIGTERM", signal.SIGTERM, signal.SIGTERM), ("SIGINT", signal.SIGINT, signal.SIGINT),
                      ("SIGHUP", signal.SIGHUP, signal.SIGHUP)])


@pytest.mark.parametrize("name,sent,received", STOP_SIGNALS, ids=[s[0] for s in STOP_SIGNALS])
def test_a_stop_signal_closes_the_browser_and_ends_the_server(tmp_path, name, sent, received):
    """The client keeps stdin open: the server still leaves once its browser is closed."""
    popen = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
    server = Server(tmp_path, **popen).start()
    try:
        try:
            os.kill(server.proc.pid, sent)
        except OSError as e:  # Windows: Ctrl+Break reaches only a process on this console
            pytest.skip(f"cannot send {name} here: {e}")
        code = server.stopped(getattr(S, "_CLOSE_SECONDS", 20) + getattr(S, "_EXIT_GRACE", 2) + 15)
        assert server.mark.exists() and server.mark.read_text() == "closed", f"{name}: the browser was not closed"
        assert code == 128 + received, f"{name}: exit code {code}"
    finally:
        server.close()


# A stop signal while the browser is still being launched (the pre-warm at startup): serve() runs in a thread that
# the cancel cannot stop, and the browser it starts must still be closed before the server leaves.
STARTING = r"""
import atexit
import subprocess
import sys
import time
from pathlib import Path

import clearcote
from clearcote_mcp import server

work = Path(sys.argv[1])


class Served:
    '''Stands in for serve()'s Server: a browser process, stopped by close().'''
    cdp_url = "http://127.0.0.1:9"

    def __init__(self, proc):
        self.proc = proc

    def is_alive(self):
        return self.proc.poll() is None

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
            (work / "closed.txt").write_text("closed")


def serve(**kwargs):
    '''A launch that takes a while: the browser runs before serve() returns and registers its close.'''
    alone = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
             else {"start_new_session": True})  # a stop signal for the server does not reach it
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **alone)
    (work / "browser.pid").write_text(str(proc.pid))
    time.sleep(5)
    srv = Served(proc)
    atexit.register(srv.close)
    return srv


clearcote.serve = serve
server.main()
"""


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_a_stop_signal_while_the_browser_starts_still_closes_it(tmp_path):
    name, sent, received = STOP_SIGNALS[0]
    env = {k: v for k, v in os.environ.items() if k not in ("CLEARCOTE_API_KEY", "CLEARCOTE_CLOUD")}
    env.update(CLEARCOTE_MCP_PREWARM="1", CLEARCOTE_HEADLESS="1")
    popen = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {}
    with open(tmp_path / "stderr.txt", "wb") as log:
        proc = subprocess.Popen([sys.executable, "-c", STARTING, str(tmp_path)], cwd=tmp_path, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log, **popen)
        pid = None
        try:
            for _ in range(300):  # the pre-warm has started the browser: serve() is still running
                if (tmp_path / "browser.pid").exists() and (tmp_path / "browser.pid").read_text():
                    pid = int((tmp_path / "browser.pid").read_text())
                    break
                assert proc.poll() is None, (tmp_path / "stderr.txt").read_text(errors="replace")
                threading.Event().wait(0.1)
            assert pid, "the stand-in launch never started"
            try:
                os.kill(proc.pid, sent)
            except OSError as e:
                pytest.skip(f"cannot send {name} here: {e}")
            code = proc.wait(getattr(S, "_CLOSE_SECONDS", 20) + getattr(S, "_EXIT_GRACE", 2) + 15)
            assert not alive(pid), f"{name}: the browser started during the pre-warm is still running"
            assert (tmp_path / "closed.txt").exists(), f"{name}: the browser was not closed"
            assert code == 128 + received, f"{name}: exit code {code}"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if pid and alive(pid):
                os.kill(pid, signal.SIGTERM)


def test_a_stop_that_cannot_say_so_still_stops(monkeypatch):
    """A signal that lands while stderr is being written makes the stop's own message raise: the server still
    stops, and still leaves once the browser is closed."""
    left = []

    async def serving():
        stop = signal.getsignal(signal.SIGTERM)

        def print(*_a, **_k):
            raise RuntimeError("reentrant call")
        monkeypatch.setattr(S, "print", print, raising=False)
        stop(signal.SIGTERM, None)
        await anyio.sleep(30)
    monkeypatch.setattr(S.mcp, "run_stdio_async", serving)
    monkeypatch.setattr(S, "_leave_once_closed", left.append)
    assert anyio.run(S._serve) == signal.SIGTERM and left == [signal.SIGTERM]


def test_the_exit_handlers_run_once(monkeypatch):
    """When the server leaves by itself, the exit after a stop signal does nothing (no second run of the exit
    handlers)."""
    ran = []
    monkeypatch.setattr(S.atexit, "_run_exitfuncs", lambda: ran.append("exit handlers"))
    monkeypatch.setattr(S.os, "_exit", ran.append)
    monkeypatch.setattr(S.anyio, "run", lambda _fn: None)  # the server has stopped by itself
    monkeypatch.setattr(S, "_EXIT_GRACE", 0)
    exiting = getattr(S, "_exiting", None)
    try:
        S.main()
        S._closed_on_stop.set()
        S._leave_once_closed(signal.SIGTERM)
        assert ran == []
    finally:
        if exiting is not None and exiting.locked():
            exiting.release()


def test_each_run_of_the_server_waits_for_its_own_close(shared):
    shared()

    async def go():
        S._closed_on_stop.set()  # an earlier run's
        async with lifespan():
            assert not S._closed_on_stop.is_set()
        assert S._closed_on_stop.is_set()
    asyncio.run(go())


def test_the_close_time_limit_holds(shared, monkeypatch):
    """Stopping the served browser blocks (up to 10 s while it exits): the server's time limit still applies."""
    import time
    from clearcote_mcp._facade import ClearcoteBrowser

    class SlowServer:
        def close(self):
            time.sleep(3)
    b = ClearcoteBrowser()
    b._srv = SlowServer()
    monkeypatch.setattr(S, "_browser", b)
    monkeypatch.setattr(S, "_CLOSE_SECONDS", 0.5)

    async def go():
        started = time.monotonic()
        async with lifespan():
            pass
        return time.monotonic() - started
    assert asyncio.run(go()) < 2  # (the close goes on in its thread)
