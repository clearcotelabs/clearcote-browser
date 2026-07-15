"""Version selector: launch(version="150") resolves against the public catalog, VALIDATING that the
build exists (and is reachable for this tier) BEFORE downloading — so a bad request fails fast with a
helpful message instead of getting stuck. Hermetic: the catalog is monkeypatched, no network."""

import importlib

import pytest

from clearcote.release import CATALOG_FALLBACK

dl = importlib.import_module("clearcote.download")

CATALOG = {
    "schema": 1,
    "builds": [
        {"major": 149, "version": "149.0.7827.114", "tier": "free", "tag": "v0.1.0-pre.22",
         "platforms": {
             "windows": {"asset": "cc-149-win.zip", "url": "https://x/cc-149-win.zip",
                         "sha256": "a" * 64, "archive": "zip", "binary": "chrome.exe"},
             "linux": {"asset": "cc-149-linux.tar.xz", "url": "https://x/cc-149-linux.tar.xz",
                       "sha256": "b" * 64, "archive": "tar.xz", "binary": "chrome"},
         }},
        {"major": 150, "version": "150.0.7871.115", "tier": "pro", "tag": "pro-150.0.7871.115",
         "platforms": {
             "windows": {"archive": "zip", "binary": "chrome.exe"},
             "linux": {"archive": "tar.xz", "binary": "chrome"},
         }},
    ],
}


@pytest.fixture(autouse=True)
def _mock_catalog(monkeypatch):
    monkeypatch.setattr(dl, "_fetch_catalog", lambda quiet=False: CATALOG)


def test_free_major_resolves_without_license():
    kind, rel = dl.resolve_version("149", has_license=False)
    assert kind == "free"
    assert rel["version"] == "149.0.7827.114"
    assert rel["url"] and rel["sha256"]


def test_exact_free_version_resolves():
    kind, rel = dl.resolve_version("149.0.7827.114", has_license=False)
    assert kind == "free" and rel["version"] == "149.0.7827.114"


def test_pro_major_needs_license_and_errors_clearly():
    with pytest.raises(ValueError, match=r"PRO build.*license"):
        dl.resolve_version("150", has_license=False)


def test_pro_major_with_license_routes_to_pro():
    kind, ver = dl.resolve_version("150", has_license=True)
    assert kind == "pro" and ver == "150.0.7871.115"


def test_unknown_version_lists_whats_available():
    with pytest.raises(ValueError, match=r"No Clearcote build matches version '151'.*Available"):
        dl.resolve_version("151", has_license=True)


def test_latest_is_newest_ACCESSIBLE_build():
    # no license -> newest FREE (149), not the newer pro 150
    kind, rel = dl.resolve_version("latest", has_license=False)
    assert kind == "free" and rel["version"] == "149.0.7827.114"
    # with license -> newest overall (pro 150)
    kind2, ver2 = dl.resolve_version("latest", has_license=True)
    assert kind2 == "pro" and ver2 == "150.0.7871.115"


def test_bundled_fallback_lists_only_downloadable_builds():
    builds = {b["version"]: b for b in CATALOG_FALLBACK["builds"]}
    assert builds["149.0.7827.114"]["tier"] == "free"
    assert builds["149.0.7827.114"]["platforms"]["linux"]["url"]  # free carries a url
    # 150 PRO is NOT advertised until its binary is live (else a licensed version="150" would 404).
    assert "150.0.7871.115" not in builds
    # every listed build must be actually downloadable (have a url per platform)
    for b in CATALOG_FALLBACK["builds"]:
        for plat in b["platforms"].values():
            assert plat.get("url"), f"{b['version']} listed without a download url"


# ── backwards compatibility: the version selector must NOT change existing behavior ──
import clearcote  # noqa: E402


def test_explicit_path_wins_over_version():
    # an explicit executable_path short-circuits before any catalog/version resolution
    assert clearcote._resolve_binary("/opt/x/chrome", version="150") == "/opt/x/chrome"


def test_env_binary_wins_over_version(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_BINARY", "/opt/env/chrome")
    assert clearcote._resolve_binary(None, version="150") == "/opt/env/chrome"


def test_no_version_keeps_legacy_path_and_never_fetches_catalog(monkeypatch):
    # with no version selector (and no license), resolution stays on the legacy free path and must
    # NOT consult the catalog — byte-identical behavior to pre-0.16.0.
    seen = {"catalog": False}
    monkeypatch.setattr(dl, "_fetch_catalog", lambda quiet=False: seen.__setitem__("catalog", True) or CATALOG)
    monkeypatch.setattr(clearcote, "ensure_binary", lambda **kw: "/free/chrome")
    monkeypatch.delenv("CLEARCOTE_BROWSER_VERSION", raising=False)
    monkeypatch.delenv("CLEARCOTE_BINARY", raising=False)
    assert clearcote._resolve_binary(None) == "/free/chrome"
    assert seen["catalog"] is False
