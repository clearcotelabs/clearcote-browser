"""``clearcote connect``: the helper pin, the code format, and the download -> SHA-256 check ->
unpack -> cache path against a local server with a fake helper archive (mirrors
sdk/node/test/connect.test.ts, which also checks the two pins agree)."""
import hashlib
import http.server
import io
import os
import tarfile
import threading
import zipfile

import pytest

from clearcote import _commands
from clearcote._connect import HELPER, ensure_helper, helper_files, helper_platform, normalize_pair_code

FILES = {}
HITS = []


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        HITS.append(self.path)
        data = FILES.get(self.path.replace("/dl/", "", 1))
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def base():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/dl/"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _no_own_dir(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_HELPER_DIR", raising=False)
    HITS.clear()


def fake_archive(plat, version, fmt):
    """A helper archive shaped like the real one: <top>/lib/clearcote-helper.js + <top>/runtime/node."""
    top = f"clearcote-helper-{version}-{plat}"
    node = "node.exe" if plat.startswith("win") else "node"
    members = {f"{top}/lib/clearcote-helper.js": b"// fake helper\n", f"{top}/runtime/{node}": b"fake node\n"}
    buf = io.BytesIO()
    if fmt == "zip":
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
    else:
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                t.addfile(info, io.BytesIO(data))
    return f"{top}.{fmt}", buf.getvalue()


def pin_for(base, plat, version, name, data, sha=None):
    return {"version": version, "base_url": base,
            "assets": {plat: {"asset": name, "sha256": sha or hashlib.sha256(data).hexdigest(), "size": len(data)}}}


def test_pin_names_one_archive_per_platform():
    assert HELPER["base_url"] == ("https://github.com/clearcotelabs/clearcote-profile-manager/releases/download/"
                                  f"helper-v{HELPER['version']}/")
    assert sorted(HELPER["assets"]) == ["linux-arm64", "linux-x64", "mac-arm64", "mac-x64", "win-arm64", "win-x64"]
    for plat, a in HELPER["assets"].items():
        ext = "zip" if plat.startswith("win") else "tar.gz"
        assert a["asset"] == f"clearcote-helper-{HELPER['version']}-{plat}.{ext}"
        assert len(a["sha256"]) == 64 and int(a["sha256"], 16) >= 0
        assert a["size"] > 1_000_000


def test_helper_platform():
    assert helper_platform("win32", "AMD64") == "win-x64"
    assert helper_platform("darwin", "arm64") == "mac-arm64"
    assert helper_platform("linux", "aarch64") == "linux-arm64"
    assert helper_platform("linux", "x86_64") == "linux-x64"
    assert helper_platform("freebsd13", "amd64") is None
    assert helper_platform("linux", "i686") is None


def test_normalize_pair_code():
    assert normalize_pair_code("4f7-k2q") == "4F7-K2Q"
    assert normalize_pair_code("4F7K2Q") == "4F7-K2Q"
    assert normalize_pair_code(" 4f7 k2q ") == "4F7-K2Q"
    for bad in ("", "4F7-K2", "4F7-K2QQ", "4F0-K2Q", "1F7-K2Q", "OOO-OOO", "4F7_K2Q"):
        assert normalize_pair_code(bad) is None


def test_downloads_checks_unpacks_once_then_uses_the_cache(base, tmp_path):
    plat = "linux-x64"
    name, data = fake_archive(plat, "9.9.9", "tar.gz")
    FILES[name] = data
    helper = pin_for(base, plat, "9.9.9", name, data)
    f = ensure_helper(cache_root=str(tmp_path), quiet=True, helper=helper, plat=plat)
    assert f["script"] == os.path.join(str(tmp_path), "helper", f"9.9.9-{plat}", f"clearcote-helper-9.9.9-{plat}",
                                       "lib", "clearcote-helper.js")
    with open(f["script"], "rb") as fh:
        assert fh.read() == b"// fake helper\n"
    assert os.path.isfile(f["node"])
    if os.name != "nt":
        assert os.access(f["node"], os.X_OK)
    assert len(HITS) == 1
    assert ensure_helper(cache_root=str(tmp_path), quiet=True, helper=helper, plat=plat) == f
    assert len(HITS) == 1  # no second download


def test_unpacks_the_windows_zip(base, tmp_path):
    plat = "win-x64"
    name, data = fake_archive(plat, "9.9.9", "zip")
    FILES[name] = data
    f = ensure_helper(cache_root=str(tmp_path), quiet=True, helper=pin_for(base, plat, "9.9.9", name, data), plat=plat)
    assert f["node"].endswith("node.exe") and os.path.isfile(f["node"])


def test_refuses_an_archive_that_does_not_match_the_pin(base, tmp_path):
    plat = "linux-x64"
    name, data = fake_archive(plat, "9.9.8", "tar.gz")
    FILES[name] = data
    with pytest.raises(RuntimeError, match="does not match its pinned SHA-256"):
        ensure_helper(cache_root=str(tmp_path), quiet=True, helper=pin_for(base, plat, "9.9.8", name, data, "0" * 64),
                      plat=plat)
    assert not os.path.exists(os.path.join(str(tmp_path), "helper", f"9.9.8-{plat}"))


def test_own_helper_dir(tmp_path, monkeypatch):
    plat = "linux-x64"
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "clearcote-helper.js").write_text("")
    monkeypatch.setenv("CLEARCOTE_HELPER_DIR", str(tmp_path))
    assert ensure_helper(plat=plat) == helper_files(str(tmp_path), plat)
    assert HITS == []
    monkeypatch.setenv("CLEARCOTE_HELPER_DIR", str(tmp_path / "nothing-here"))
    with pytest.raises(RuntimeError, match="no lib/clearcote-helper.js"):
        ensure_helper(plat=plat)


def test_cli_usage_and_help():
    assert "clearcote connect [<code>] [--port <n>] [--allow-origin <origin>]... [--no-open]" in _commands.usage()
    help_text = _commands.command_usage("connect")
    assert "https://app.clearcotelabs.com" in help_text
    assert help_text == _commands.command_usage("connect")


def test_cli_refuses_a_bad_code_before_downloading(capsys):
    assert _commands.main(["connect", "hello"]) == 1
    assert "is not a code the profile manager shows" in capsys.readouterr().err
    assert HITS == []
    assert _commands.main(["connect", "4F7-K2Q", "extra"]) == 2
    assert _commands.main(["connect", "--port", "x"]) == 2
