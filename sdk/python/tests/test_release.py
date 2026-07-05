import re
import sys

from clearcote.release import PLATFORMS, RELEASE, REPO, SIGNING_KEY_FPR, platform_release


def _check_pin(rel):
    for k in ("tag", "version", "asset", "url", "sha256", "exe_sha256", "size",
              "os", "archive", "binary", "asset_glob"):
        assert k in rel and rel[k] not in (None, ""), k
    assert re.fullmatch(r"[0-9a-f]{64}", rel["sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", rel["exe_sha256"])
    assert rel["size"] > 0
    assert rel["version"] in rel["asset"]
    assert rel["asset_glob"] in rel["asset"]
    assert rel["asset"].endswith(".zip") or rel["asset"].endswith(".tar.xz")
    assert rel["url"] == f"https://github.com/{REPO}/releases/download/{rel['tag']}/{rel['asset']}"
    assert re.match(r"^v\d+\.\d+\.\d+", rel["tag"])
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", rel["version"])


def test_all_platform_pins_wellformed():
    assert set(PLATFORMS) == {"win32", "linux"}
    for rel in PLATFORMS.values():
        _check_pin(rel)


def test_windows_pin():
    w = PLATFORMS["win32"]
    assert w["os"] == "win32" and w["archive"] == "zip" and w["binary"] == "chrome.exe"
    assert w["asset"].endswith("-windows-x64.zip")


def test_linux_pin():
    ln = PLATFORMS["linux"]
    assert ln["os"] == "linux" and ln["archive"] == "tar.xz" and ln["binary"] == "chrome"
    assert ln["asset"].endswith("-linux-x64.tar.xz")


def test_platform_release_selects_by_os():
    assert platform_release("win32") is PLATFORMS["win32"]
    assert platform_release("linux") is PLATFORMS["linux"]
    assert platform_release("darwin") is None
    # RELEASE is the current platform's pin (Windows fallback on an unsupported OS).
    assert RELEASE is (platform_release(sys.platform) or PLATFORMS["win32"])


def test_signing_key_fpr():
    assert re.fullmatch(r"[0-9A-F]{40}", SIGNING_KEY_FPR)
