"""Two installers of the same build at once (two processes, or two containers sharing the macOS Docker
launch's engine-cache volume) must not download over each other: one installs, the other waits and uses
that tree. Before the install lock both downloaded, shared one ``.incoming`` directory, and the loser failed
with "Directory not empty" (measured with two parallel licensed containers on a fresh volume)."""
import hashlib
import importlib
import io
import os
import sys
import threading
import zipfile

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

BINARY = "chrome.exe" if sys.platform == "win32" else "chrome"


def _archive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:  # every file a healthy install must have (download.CRITICAL_FILES)
        for name in {BINARY, *download.CRITICAL_FILES["win32" if sys.platform == "win32" else "other"]}:
            z.writestr(name, b"x" * 64)  # at the root, where verify_install looks for them
    return buf.getvalue()


def test_two_installers_at_once_download_once(tmp_path, monkeypatch):
    data = _archive()
    sha = hashlib.sha256(data).hexdigest()
    rel = {"tag": "pro-test-r1", "version": "1.0", "url": "https://example.invalid/a.zip", "sha256": sha,
           "asset": "a.zip", "archive": "zip", "binary": BINARY, "size": len(data), "unpinned": False}
    both_started = threading.Barrier(2)
    downloads = []

    def slow_download(url, path, size, quiet):
        downloads.append(url)
        threading.Event().wait(0.5)  # long enough for the other installer to arrive meanwhile
        with open(path, "wb") as fh:
            fh.write(data)
        return sha

    monkeypatch.setattr(download, "_download", slow_download)
    base = str(tmp_path / "cache" / "pro-test-r1")
    results, errors = [], []

    def install():
        both_started.wait()
        try:
            results.append(download._fetch_and_verify(dict(rel), base, True))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len(downloads) == 1  # the second installer waited and used the first one's tree
    assert len(results) == 2 and results[0] == results[1] and os.path.isfile(results[0])
    assert not os.path.exists(os.path.join(base, ".incoming"))
    assert os.path.exists(os.path.join(base, ".verified"))
