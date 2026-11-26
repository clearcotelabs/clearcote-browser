"""A local stand-in for the PRO download route and the archive it points at, for the install-lock tests.

The archive is a small zip holding every file a healthy install must have (no real browser). The server
counts archive downloads, and can hold the route's answer until ``meta_barrier`` clients have asked, so
that several installers reach the cache check at the same moment.

The answer carries the signed release manifest for exactly what it says (docs/PRO-RELEASE-MANIFEST.md),
signed with the TEST key (_manifest_signer.py). A test that downloads through it pins that key with the
``pro_test_key`` fixture; ``signed``, ``manifest_over`` and ``served_archive`` make the server misbehave."""
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

from _manifest_signer import signed_answer

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

BINARY = "chrome.exe" if sys.platform == "win32" else "chrome"
TAG = "pro-0.0.0-r1"
PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")  # the route's ``platform`` key
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
            f.write(b"x" * 64)
    download._write_manifest(base, browser)
    with open(os.path.join(base, ".verified"), "w", encoding="utf-8") as f:
        f.write("0" * 64 + "\n")
    return os.path.join(browser, BINARY)


class FakeBuildServer:
    def __init__(self, meta_barrier=1, archive_delay=0.0, exe_sha256=None, on_archive=None, tag=TAG,
                 version="0.0.0"):
        self.archive = fake_archive()
        self.tag, self.version = tag, version  # the build the route names
        self.revision = tag.rsplit("-", 1)[-1]  # "pro-0.0.0-r1" -> "r1"
        self.signed = True  # False: the answer carries no manifest, like a site from before the manifest
        self.manifest_over = {}  # manifest fields replaced before signing, e.g. {"revision": "r2"}
        self.served_archive = None  # bytes sent in place of the archive the answer and the manifest name
        self.sha = hashlib.sha256(self.archive).hexdigest()
        self.code_sha = hashlib.sha256(b"x" * 64).hexdigest()
        self.exe_sha256 = exe_sha256 or self.code_sha
        self.archive_hits = 0
        self.meta_hits = 0  # requests to the download route
        self.meta_status = 200  # set to an error status to make the route refuse
        self.meta_challenge = False  # True: a 403 challenge page like the site's host sends a burst
        self.archive_status = 200  # likewise for the archive, with a Retry-After header when retry_after is set
        self.retry_after = None
        self.user_agents = []  # the User-Agent of each archive download
        self.meta_user_agents = []  # the User-Agent of each download-route (metadata) request
        self.meta_extra = {}  # extra fields merged into the route's answer (e.g. a "notice")
        self.meta_seen = threading.Event()
        self._count = threading.Lock()
        self._barrier = threading.Barrier(meta_barrier) if meta_barrier > 1 else None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path.startswith("/api/v1/download/pro"):
                    with server._count:
                        server.meta_hits += 1
                        server.meta_user_agents.append(self.headers.get("User-Agent", ""))
                    server.meta_seen.set()
                    if server._barrier is not None:
                        try:
                            server._barrier.wait(30)
                        except threading.BrokenBarrierError:
                            pass
                    if server.meta_challenge:  # what the site's host answers a burst from one address
                        body = b"<!DOCTYPE html><title>Vercel Security Checkpoint</title>"
                        self.send_response(403)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("x-vercel-mitigated", "challenge")
                        self.send_header("Retry-After", "30")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if server.meta_status != 200:
                        body = json.dumps({"error": "Licence not active."}).encode()
                        self.send_response(server.meta_status)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    body = json.dumps(server.answer()).encode()
                    ctype = "application/json"
                elif self.path == "/fake.zip":
                    with server._count:
                        server.archive_hits += 1
                        server.user_agents.append(self.headers.get("User-Agent", ""))
                    if server.archive_status != 200:
                        body = b'{"error":"Download limit reached","code":"DOWNLOAD_RATE_LIMITED"}'
                        self.send_response(server.archive_status)
                        self.send_header("Content-Type", "application/json")
                        if server.retry_after:
                            self.send_header("Retry-After", server.retry_after)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if on_archive:
                        on_archive()  # e.g. another installer finishes while this one downloads
                    time.sleep(archive_delay)  # long enough for a second installer to arrive meanwhile
                    body = server.archive if server.served_archive is None else server.served_archive
                    ctype = "application/zip"
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

    def answer(self):
        """What the route answers: the build, and (while ``signed``) the release manifest for exactly that."""
        meta = {"tag": self.tag, "version": self.version, "revision": self.revision, "platform": PLATFORM,
                "url": f"{self.url}/fake.zip", "sha256": self.sha, "exe_sha256": self.exe_sha256,
                "chrome_dll_sha256": self.code_sha, "asset": "fake.zip", "archive": "zip", "binary": BINARY,
                "size": len(self.archive)}
        if self.signed:
            meta = signed_answer(meta, PLATFORM, self.manifest_over)
        return {**meta, **self.meta_extra}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
