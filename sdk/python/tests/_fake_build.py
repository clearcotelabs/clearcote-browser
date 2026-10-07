"""A local stand-in for the PRO download route and the archive it points at, for the install-lock tests.

The archive is a small zip holding every file a healthy install must have (no real browser). The server
counts archive downloads, and can hold the route's answer until ``meta_barrier`` clients have asked, so
that several installers reach the cache check at the same moment."""
import hashlib
import importlib
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zipfile

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

BINARY = "chrome.exe" if sys.platform == "win32" else "chrome"
TAG = "pro-0.0.0-r1"
NAMES = sorted({BINARY, *download.CRITICAL_FILES["win32" if sys.platform == "win32" else "other"]})


def fake_archive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in NAMES:
            z.writestr(name, b"x" * 64)  # at the root, where verify_install looks for them
    return buf.getvalue()


def verified_tree(base):
    """What another installer leaves behind: <base>/browser, its manifest and the .verified marker."""
    browser = os.path.join(base, "browser")
    os.makedirs(browser, exist_ok=True)
    for name in NAMES:
        with open(os.path.join(browser, name), "wb") as f:
            f.write(b"y" * 64)
    download._write_manifest(base, browser)
    with open(os.path.join(base, ".verified"), "w", encoding="utf-8") as f:
        f.write("0" * 64 + "\n")
    return os.path.join(browser, BINARY)


class FakeBuildServer:
    def __init__(self, meta_barrier=1, archive_delay=0.0):
        self.archive = fake_archive()
        self.sha = hashlib.sha256(self.archive).hexdigest()
        self.archive_hits = 0
        self.meta_seen = threading.Event()
        self._count = threading.Lock()
        self._barrier = threading.Barrier(meta_barrier) if meta_barrier > 1 else None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/api/v1/download/pro"):
                    server.meta_seen.set()
                    if server._barrier is not None:
                        try:
                            server._barrier.wait(30)
                        except threading.BrokenBarrierError:
                            pass
                    body = json.dumps({"tag": TAG, "version": "0.0.0", "url": f"{server.url}/fake.zip",
                                       "sha256": server.sha, "asset": "fake.zip", "archive": "zip",
                                       "binary": BINARY, "size": len(server.archive)}).encode()
                    ctype = "application/json"
                elif self.path == "/fake.zip":
                    with server._count:
                        server.archive_hits += 1
                    time.sleep(archive_delay)  # long enough for a second installer to arrive meanwhile
                    body, ctype = server.archive, "application/zip"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
