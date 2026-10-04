"""A real Chromium with a CDP endpoint, for the cloud tests that must talk to an actual browser.

The hosted API hands the SDK a CDP WebSocket URL; these tests stand a local Chromium in for the
hosted one. Which binary: ``CLEARCOTE_TEST_BINARY`` (a Clearcote build, as the serve smoke tests
use), else Playwright's own Chromium if one is installed. Neither -> the tests skip.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


def find_chromium():
    env = os.environ.get("CLEARCOTE_TEST_BINARY")
    if env and os.path.exists(env):
        return env
    # Any Chromium Playwright has downloaded (the newest), not just the one this Playwright pins:
    # the CDP surface these tests use is stable across versions.
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser(
        "~/AppData/Local/ms-playwright" if sys.platform == "win32" else "~/.cache/ms-playwright")
    names = ("chrome.exe",) if sys.platform == "win32" else ("chrome",)
    found = sorted((p for n in names for p in glob.glob(os.path.join(root, "chromium-*", "*", n))), reverse=True)
    return found[0] if found else None


class LocalChromium:
    """``with LocalChromium() as c: c.ws_url, c.http_url``. Started headless on a throwaway profile."""

    def __init__(self, exe=None):
        self.exe = exe or find_chromium()
        self.proc = None
        self.ws_url = None
        self.http_url = None
        self._udd = None

    def __enter__(self):
        self._udd = tempfile.mkdtemp(prefix="cc-cloud-test-")
        args = [self.exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self._udd}",
                "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank"]
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        # The browser's own temp files go inside its profile, so __exit__ takes them too: its
        # singleton-socket directory on Linux (orphaned whenever the browser is killed) and its
        # component-updater downloads on Windows (chromiumcrx_*) would otherwise outlive the run.
        temp = os.path.join(self._udd, "tmp")
        os.mkdir(temp)
        env = {**os.environ, "TMPDIR": temp, "TMP": temp, "TEMP": temp}
        # Its own process group on Linux/macOS, so __exit__ can end the helper processes along with it.
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
                                     start_new_session=os.name != "nt")
        found = threading.Event()

        def read():
            for raw in iter(self.proc.stderr.readline, b""):
                m = re.search(rb"DevTools listening on (ws://\S+)", raw)
                if m and not found.is_set():
                    self.ws_url = m.group(1).decode()
                    found.set()

        threading.Thread(target=read, daemon=True).start()
        if not found.wait(30):
            self.__exit__()
            raise RuntimeError(f"{self.exe} did not open a CDP endpoint")
        port = int(re.search(r":(\d+)/", self.ws_url).group(1))
        self.http_url = f"http://127.0.0.1:{port}"
        return self

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def __exit__(self, *_a):
        if self.proc is not None and os.name != "nt":
            # The whole process group: the browser exits at once on a signal, but its helper processes
            # went on writing the profile for 10+ s, so the rmtree below lost to them and left it behind.
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(10)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    os.killpg(self.proc.pid, 0)
                except ProcessLookupError:
                    break  # nobody left in the group
                time.sleep(0.05)
        elif self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._udd:
            shutil.rmtree(self._udd, ignore_errors=True)
