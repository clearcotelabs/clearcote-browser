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
