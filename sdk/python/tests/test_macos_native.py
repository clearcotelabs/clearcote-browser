"""The native macOS PRO build (Apple Silicon, a Chromium.app bundle).

There is no Mac in CI, so every darwin path here runs with ``sys.platform`` and the CPU architecture
faked per module. What is covered: an explicit executable_path is honoured on any OS, darwin maps to
the "macos" PRO download key (arm64 only), the bundle's own executable is found inside the .app (never
the framework or a helper), the zip is unpacked with ditto and un-quarantined, and launch() on a
licensed Apple Silicon Mac runs the native build instead of the Docker image."""

import hashlib
import importlib
import json
import os
import types
import zipfile

import pytest

import clearcote
import clearcote._docker as _docker
import clearcote._fingerprint as _fingerprint
import clearcote._launchopts as _launchopts
import clearcote.release as release
from clearcote._warnings import coherence_warnings

dl = importlib.import_module("clearcote.download")  # the attribute clearcote.download is the function

APP_EXE = os.path.join("Chromium.app", "Contents", "MacOS", "Chromium")
FRAMEWORK = os.path.join("Chromium.app", "Contents", "Frameworks", "Chromium Framework.framework")


def _on(monkeypatch, platform, machine="arm64", *modules):
    fake = types.SimpleNamespace(platform=platform)
    for mod in (release, dl, _docker, *modules):
        monkeypatch.setattr(mod, "sys", fake)
    monkeypatch.setattr(release, "host_machine", lambda: machine)


@pytest.fixture
def no_key(monkeypatch, tmp_path):
    """No licence key anywhere this test can see (env, or a saved one in HOME)."""
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(tmp_path / "home"))
    for k in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER",
              _docker.TEST_ONLY_ASSUME_MACOS):
        monkeypatch.delenv(k, raising=False)


def _write(path, data=b"x" * 64):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _fake_app(root, engine=b"\x00proxy-auth\x00"):
    """A Chromium.app laid out like the real one, with decoys a name search could land on: a helper app
    inside the framework and a resource file that is also called ``Chromium``."""
    _write(os.path.join(root, APP_EXE), b"launcher-without-switch-strings")
    _write(os.path.join(root, "Chromium.app", "Contents", "Info.plist"), b"<plist/>")
    _write(os.path.join(root, FRAMEWORK, "Versions", "Current", "Chromium Framework"), b"x" * 32 + engine)
    _write(os.path.join(root, FRAMEWORK, "Versions", "Current", "Resources", "Chromium"), b"decoy")
    _write(os.path.join(root, FRAMEWORK, "Versions", "Current", "Helpers", "Chromium Helper.app",
                        "Contents", "MacOS", "Chromium Helper"), b"helper")
    return os.path.join(root, APP_EXE)


# ── _guard ───────────────────────────────────────────────────────────────────


def test_guard_honours_an_existing_executable_path_on_darwin(monkeypatch, tmp_path):
    exe = _fake_app(str(tmp_path))
    _on(monkeypatch, "darwin", "arm64")
    clearcote._guard(exe)  # used to raise: "pass executable_path=..." while holding one
    _on(monkeypatch, "darwin", "x86_64")
    clearcote._guard(exe)  # named by the caller: theirs to run, whatever the CPU


def test_guard_still_refuses_an_os_with_no_build(monkeypatch, tmp_path):
    _on(monkeypatch, "freebsd13")
    with pytest.raises(RuntimeError, match="no build for"):
        clearcote._guard(str(tmp_path / "missing" / "chrome"))


# ── platform key ─────────────────────────────────────────────────────────────


def test_darwin_maps_to_the_macos_pro_key(monkeypatch):
    _on(monkeypatch, "darwin", "arm64")
    assert dl._plat_key() == "macos"
    assert release.pro_platform_key("darwin", "arm64") == "macos"
    assert release.pro_platform_key("win32", "amd64") == "windows"
    assert release.pro_platform_key("linux", "x86_64") == "linux"
    assert release.pro_platform_key("freebsd13", "amd64") is None
    # still no FREE pinned release for macOS: the native build is PRO-only
    assert release.platform_release("darwin") is None


def test_intel_mac_is_told_the_build_is_apple_silicon_only(monkeypatch):
    _on(monkeypatch, "darwin", "x86_64")
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        dl._plat_key()
    with pytest.raises(RuntimeError, match="Apple Silicon"):
        dl.pro_ensure_binary("cc_lic_x", quiet=True)


def test_pro_download_asks_for_macos_and_expects_the_bundle(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # the download answer is kept in ~/.clearcote
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setattr(dl, "_pro_meta_memo", {})
    seen = {}
    meta = {"version": "155.0.8059.39", "tag": "pro-155.0.8059.39-r42", "platform": "macos",
            "asset": "clearcote-pro-155.0.8059.39-macos-arm64.zip", "archive": "zip",
            "url": "https://blob.example/x.zip", "sha256": "a" * 64, "exe_sha256": "b" * 64, "size": 1}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(meta).encode()

    def _capture(req, timeout=0):
        seen["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(dl.urllib.request, "urlopen", _capture)
    monkeypatch.setattr(dl, "_fetch_and_verify", lambda rel, base, quiet: seen.setdefault("rel", rel) and "ok")
    assert dl.pro_ensure_binary("cc_lic_mac", api_base="https://example.test", cache_dir=str(tmp_path),
                                quiet=True) == "ok"
    assert "platform=macos" in seen["url"]
    assert seen["rel"]["binary"] == "Chromium"  # no "binary" in the reply: the macOS default


def test_free_build_fails_on_a_mac_before_downloading_anything(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setattr(dl, "_download", lambda *a, **k: pytest.fail("downloaded a build for another OS"))
    with pytest.raises(RuntimeError, match="license key"):
        dl.ensure_binary(cache_dir=str(tmp_path), quiet=True)


# ── finding the binary in the bundle ─────────────────────────────────────────


def test_find_picks_the_bundle_executable_not_a_framework_file(tmp_path):
    exe = _fake_app(str(tmp_path / "browser"))
    assert dl._find(str(tmp_path / "browser"), "Chromium") == exe


def test_cached_and_listed_builds_find_the_bundle_on_darwin(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", "arm64")
    base = tmp_path / "pro-155"
    exe = _fake_app(str(base / "browser"))
    dl._write_manifest(str(base), str(base / "browser"))
    (base / ".verified").write_text("x\n")
    dl._scanned.clear()
    assert dl._cached(str(base), "Chromium", quiet=True, repair=False) == exe
    assert dl.list_cached_builds(str(tmp_path)) == [{"tag": "pro-155", "path": exe}]
    # the darwin integrity list is the bundle's, and a missing framework is caught
    os.remove(os.path.join(str(base / "browser"), FRAMEWORK, "Versions", "Current", "Chromium Framework"))
    dl._scanned.clear()
    assert dl._cached(str(base), "Chromium", quiet=True, repair=False) is None


# ── extraction ───────────────────────────────────────────────────────────────


def _bundle_zip(tmp_path):
    src = tmp_path / "src"
    exe = _fake_app(str(src))
    zpath = tmp_path / "clearcote-pro-155.0.8059.39-macos-arm64.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for root, _d, files in os.walk(src):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, src))
    with open(exe, "rb") as f:
        exe_sha = hashlib.sha256(f.read()).hexdigest()
    with open(zpath, "rb") as f:
        return zpath, hashlib.sha256(f.read()).hexdigest(), exe_sha


def test_darwin_unpacks_with_ditto_and_clears_quarantine(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", "arm64")
    zpath, sha, exe_sha = _bundle_zip(tmp_path)
    calls = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[0] == "/usr/bin/ditto":  # what ditto -x -k does, minus the symlinks and modes
            with zipfile.ZipFile(argv[3]) as z:
                for member in z.namelist():
                    z.extract(member, argv[4])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    monkeypatch.setattr(dl.zipfile.ZipFile, "extractall",
                        lambda *a, **k: pytest.fail("zipfile drops the bundle's symlinks and exec bits"),
                        raising=True)

    def fake_download(url, dest, size, quiet):
        with open(zpath, "rb") as s, open(dest, "wb") as d:
            d.write(s.read())
        return sha

    monkeypatch.setattr(dl, "_download", fake_download)
    rel = {"tag": "pro-mac", "version": "155.0.8059.39", "asset": zpath.name, "archive": "zip",
           "url": "https://blob.example/x.zip", "sha256": sha, "exe_sha256": exe_sha,
           "binary": "Chromium", "size": 1, "unpinned": False}
    exe = dl._fetch_and_verify(rel, str(tmp_path / "cache" / "pro-mac"), quiet=True)
    assert exe.endswith(APP_EXE)
    assert calls[0][:3] == ["/usr/bin/ditto", "-x", "-k"]
    xattr = [c for c in calls if c[0] == "/usr/bin/xattr"]
    assert xattr and xattr[0][1:3] == ["-dr", "com.apple.quarantine"]
    assert xattr[0][3].endswith("Chromium.app")


def test_darwin_falls_back_to_unzip_without_ditto(monkeypatch, tmp_path):
    _on(monkeypatch, "darwin", "arm64")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv[0])
        if argv[0] == "/usr/bin/ditto":
            raise FileNotFoundError(argv[0])
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    dl._extract({"asset": "x.zip", "archive": "zip"}, str(tmp_path / "x.zip"), str(tmp_path / "out"))
    assert calls[:2] == ["/usr/bin/ditto", "/usr/bin/unzip"]


def test_other_hosts_keep_zipfile(monkeypatch, tmp_path):
    _on(monkeypatch, "linux")
    zpath, _sha, _exe_sha = _bundle_zip(tmp_path)
    monkeypatch.setattr(dl.subprocess, "run", lambda *a, **k: pytest.fail("no ditto off macOS"))
    dl._extract({"asset": zpath.name, "archive": "zip"}, str(zpath), str(tmp_path / "out"))
    assert os.path.isfile(tmp_path / "out" / APP_EXE)


# ── engine probe, launch arguments, persona ──────────────────────────────────


def test_switch_probe_reads_the_framework_binary(monkeypatch, tmp_path):
    exe = _fake_app(str(tmp_path))
    monkeypatch.setattr(_launchopts, "sys", types.SimpleNamespace(platform="darwin"))
    _launchopts._SWITCH_CACHE.clear()
    assert _launchopts.engine_supports_switch(exe, "proxy-auth") is True
    assert _launchopts.engine_supports_switch(exe, "no-such-switch") is False


def test_mock_keychain_is_a_macos_default():
    assert _launchopts.macos_keychain_args("darwin") == ["--use-mock-keychain"]
    assert _launchopts.macos_keychain_args("darwin", ["--use-mock-keychain"]) == []
    assert _launchopts.macos_keychain_args("linux") == []
    assert _launchopts.macos_keychain_args("win32") == []


def test_default_persona_platform_on_a_mac_is_macos(monkeypatch):
    monkeypatch.setattr(_fingerprint, "sys", types.SimpleNamespace(platform="darwin"))
    assert _fingerprint.host_persona_platform() == "macos"
    assert _fingerprint.persona_platform({"fingerprint": "seed-1"}) == "macos"


def _codes(opts, host):
    return [w["code"] for w in coherence_warnings(opts, host_platform=host)]


def test_windows_persona_on_the_mac_build_warns_even_with_the_sdk_font_pick():
    picked = {"fonts": {"detected": ["Arial", "Segoe UI"]}}
    # the SDK's own font pick for a seeded persona cannot help on macOS: still host-native fonts
    msgs = coherence_warnings({"platform": "windows", "fingerprint_profile": picked,
                               "_sdk_font_profile": True}, host_platform="darwin")
    hit = [w for w in msgs if w["code"] == "platform-host-fonts"]
    assert hit and "only platform='macos' is coherent" in hit[0]["message"]
    assert "platform-host-fonts" in _codes({"platform": "windows"}, "darwin")
    # a profile the caller supplied (captured on Windows) is the documented workaround
    assert "platform-host-fonts" not in _codes({"platform": "windows", "fingerprint_profile": picked}, "darwin")
    assert "platform-host-fonts" not in _codes({"platform": "macos"}, "darwin")
    # unchanged elsewhere: on Linux the picked list does reach the engine
    assert "platform-host-fonts" not in _codes({"platform": "windows", "fingerprint_profile": picked,
                                                "_sdk_font_profile": True}, "linux")


# ── launch default ───────────────────────────────────────────────────────────


def test_docker_decision_on_a_mac(monkeypatch, no_key):
    def on(machine, docker=None, key=None, **kw):
        _on(monkeypatch, "darwin", machine)
        if key:
            kw["license_key"] = key
        return _docker.docker_requested(docker, kw)

    assert on("arm64", key="cc_lic_x") is False          # licensed Apple Silicon: native PRO build
    assert on("arm64") is True                           # no key: the free build has no Mac binary
    assert on("x86_64", key="cc_lic_x") is True          # Intel Mac: the PRO build is arm64 only
    assert on("arm64", docker=True, key="cc_lic_x") is True   # Docker stays reachable
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", "cc_lic_env")
    assert on("arm64") is False                          # the key from the environment counts too
    # the test-only "assume macOS" switch exists to exercise the Docker path: it stays Docker
    _on(monkeypatch, "linux", "aarch64")
    monkeypatch.setenv(_docker.TEST_ONLY_ASSUME_MACOS, "1")
    assert _docker.docker_requested(None, {"license_key": "cc_lic_x"}) is True


def test_launch_runs_the_native_build_when_licensed_on_apple_silicon(monkeypatch, no_key):
    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setattr(clearcote, "launch_docker", lambda *a, **k: pytest.fail("started Docker"))
    seen = {}

    def native(prefix, kwargs):
        seen["kwargs"] = kwargs
        return "context"

    monkeypatch.setattr(clearcote, "_launch_on_throwaway_profile", native)
    monkeypatch.setattr(clearcote, "_install_persistent_as_browser", lambda ctx: ("browser", ctx))
    assert clearcote.launch(license_key="cc_lic_mac", platform="macos") == ("browser", "context")
    assert seen["kwargs"]["license_key"] == "cc_lic_mac"


def test_launch_without_a_key_on_a_mac_still_uses_docker(monkeypatch, no_key):
    _on(monkeypatch, "darwin", "arm64")
    monkeypatch.setattr(clearcote, "launch_docker", lambda kw: "docker-browser")
    monkeypatch.setattr(clearcote, "_launch_on_throwaway_profile",
                        lambda *a, **k: pytest.fail("no native build without a key"))
    assert clearcote.launch(platform="macos") == "docker-browser"
