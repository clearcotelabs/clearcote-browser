"""A cached browser tree can be complete at install time and damaged later (antivirus quarantine,
a full disk, an interrupted copy out of a packaged app). Chromium does not report that usefully —
it CHECK-crashes during startup ("Invalid file descriptor to ICU data received") before Playwright
can attach — so the SDK detects it itself: self-heal for trees we installed, clear error for trees
the caller supplied."""

import json
import os

import pytest

from clearcote.download import (
    MANIFEST,
    _cached,
    _scanned,
    _write_manifest,
    broken_install_error,
    check_install,
    verify_install,
)

PAYLOAD = ["chrome.dll", "chrome_elf.dll", "icudtl.dat", "snapshot_blob.bin", "resources.pak"]


def _tree(root, binary="chrome.exe"):
    """A minimal but complete install base: <root>/browser + .verified + a manifest."""
    base = os.path.join(root, "v-test")
    browser = os.path.join(base, "browser")
    os.makedirs(os.path.join(browser, "locales"))
    for name in [binary, *PAYLOAD]:
        with open(os.path.join(browser, name), "wb") as f:
            f.write(b"x" * 128)
    with open(os.path.join(browser, "locales", "en-US.pak"), "wb") as f:
        f.write(b"x" * 64)
    _write_manifest(base, browser)
    with open(os.path.join(base, ".verified"), "w", encoding="utf-8") as f:
        f.write("deadbeef\n")
    _scanned.clear()
    return base, browser


def test_healthy_tree_verifies_and_is_returned_from_cache(tmp_path):
    base, browser = _tree(str(tmp_path))
    assert verify_install(browser, base) == []
    assert _cached(base, "chrome.exe", quiet=True) == os.path.join(browser, "chrome.exe")


def test_manifest_records_every_file(tmp_path):
    base, browser = _tree(str(tmp_path))
    with open(os.path.join(base, MANIFEST), encoding="utf-8") as f:
        files = json.load(f)["files"]
    assert "locales/en-US.pak" in files  # nested entries use forward slashes
    assert files["icudtl.dat"] == 128


def test_missing_icu_data_is_detected(tmp_path):
    """The exact field failure: chrome.exe present, icudtl.dat gone."""
    base, browser = _tree(str(tmp_path))
    os.remove(os.path.join(browser, "icudtl.dat"))
    problems = verify_install(browser, base)
    assert len(problems) == 1 and problems[0].startswith("icudtl.dat")
    assert "missing" in problems[0]


def test_truncated_payload_file_is_detected(tmp_path):
    base, browser = _tree(str(tmp_path))
    with open(os.path.join(browser, "icudtl.dat"), "wb") as f:
        f.write(b"x" * 12)  # a copy that stopped part-way
    problems = verify_install(browser, base)
    assert problems and "expected 128" in problems[0]


def test_manifest_catches_a_non_critical_file(tmp_path):
    """Only the manifest can see this one — it is not in CRITICAL_FILES."""
    base, browser = _tree(str(tmp_path))
    os.remove(os.path.join(browser, "locales", "en-US.pak"))
    assert verify_install(browser, base) == ["locales/en-US.pak — missing"]


def test_damaged_cache_is_wiped_so_the_caller_redownloads(tmp_path):
    base, browser = _tree(str(tmp_path))
    os.remove(os.path.join(browser, "icudtl.dat"))
    assert _cached(base, "chrome.exe", quiet=True) is None
    assert not os.path.exists(browser)  # wiped, so the next resolve re-downloads
    assert not os.path.exists(os.path.join(base, ".verified"))  # and cannot short-circuit again


def test_full_scan_runs_once_per_process(tmp_path):
    """The critical-file check is cheap enough for every launch; the ~700-file manifest scan is
    memoised, so later launches in the same process do not re-stat the whole tree."""
    base, browser = _tree(str(tmp_path))
    assert verify_install(browser, base) == []
    os.remove(os.path.join(browser, "locales", "en-US.pak"))
    assert verify_install(browser, base) == []  # memoised: manifest-only damage not re-checked
    assert verify_install(browser, base, full=True) == ["locales/en-US.pak — missing"]


def test_check_install_rejects_a_damaged_caller_supplied_tree(tmp_path):
    _base, browser = _tree(str(tmp_path))
    os.remove(os.path.join(browser, "icudtl.dat"))
    with pytest.raises(RuntimeError, match="incomplete or corrupted"):
        check_install(os.path.join(browser, "chrome.exe"))


def test_check_install_accepts_a_healthy_caller_supplied_tree(tmp_path):
    _base, browser = _tree(str(tmp_path))
    check_install(os.path.join(browser, "chrome.exe"))  # must not raise


def test_check_install_ignores_a_non_flat_chromium_layout(tmp_path):
    """Installed Google Chrome keeps its payload in a versioned subfolder. Refusing to launch that
    would be a false alarm, so the check only engages on a flat tree."""
    app = tmp_path / "Application"
    (app / "151.0.7922.108").mkdir(parents=True)
    (app / "chrome.exe").write_bytes(b"x")
    (app / "151.0.7922.108" / "chrome.dll").write_bytes(b"x")
    check_install(str(app / "chrome.exe"))  # must not raise


def test_check_install_ignores_a_missing_binary(tmp_path):
    check_install(str(tmp_path / "nope" / "chrome.exe"))  # the launcher reports this better


def test_error_message_names_the_file_and_the_fix(tmp_path):
    _base, browser = _tree(str(tmp_path))
    err = broken_install_error(browser, ["icudtl.dat — missing"], repairable=True)
    text = str(err)
    assert "icudtl.dat" in text
    assert os.path.dirname(browser) in text  # the folder to delete
    assert "antivirus" in text


def test_error_message_for_caller_supplied_tree_does_not_promise_a_redownload(tmp_path):
    _base, browser = _tree(str(tmp_path))
    text = str(broken_install_error(browser, ["icudtl.dat — missing"], repairable=False))
    assert "re-download it" not in text
    assert "CLEARCOTE_BINARY" in text


def test_many_problems_are_truncated(tmp_path):
    _base, browser = _tree(str(tmp_path))
    text = str(broken_install_error(browser, [f"f{i}.pak — missing" for i in range(20)]))
    assert "... and 12 more" in text
