"""Free-vs-PRO selection: with a license key the SDK pulls the license-gated PRO binary via the
site's authenticated route; with no key it is fully inert (free binary from GitHub, no backend
call). Hermetic — the only network is a monkeypatched urlopen."""

import importlib
import io
import urllib.error

import pytest

import clearcote
from clearcote._license import resolve_license_key

# NB: clearcote.download is shadowed by the public `download()` function in __init__, so pull the
# submodule from sys.modules explicitly (this is the module pro_ensure_binary actually lives in).
dl = importlib.import_module("clearcote.download")


# ── resolve_license_key: explicit > env > file ────────────────────────────────
def test_resolve_license_key_prefers_explicit_and_trims(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", "cc_lic_from_env")
    assert resolve_license_key("  cc_lic_explicit  ") == "cc_lic_explicit"


def test_resolve_license_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", "cc_lic_from_env")
    assert resolve_license_key() == "cc_lic_from_env"
    assert resolve_license_key("   ") == "cc_lic_from_env"  # blank explicit is ignored


# ── _resolve_binary precedence: explicit path > CLEARCOTE_BINARY > pro > free ──
def test_resolve_binary_explicit_path_wins(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_BINARY", "/opt/env/chrome")
    # explicit path beats the env binary and never triggers a download
    assert clearcote._resolve_binary("/opt/custom/chrome", pro=("cc_lic_x", None)) == "/opt/custom/chrome"


def test_resolve_binary_env_wins_over_pro(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_BINARY", "/opt/env/chrome")
    # even with a pro selector, the explicit env binary short-circuits (no network)
    assert clearcote._resolve_binary(None, pro=("cc_lic_x", None)) == "/opt/env/chrome"


def test_resolve_binary_pro_selector_routes_to_pro(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_BINARY", raising=False)
    called = {}

    def fake_pro(license_key, api_base=None, cache_dir=None, quiet=False):
        called["key"] = license_key
        called["api_base"] = api_base
        return "/cache/pro/chrome"

    monkeypatch.setattr(dl, "pro_ensure_binary", fake_pro)
    out = clearcote._resolve_binary(None, pro=("cc_lic_key", "https://example.test"))
    assert out == "/cache/pro/chrome"
    assert called == {"key": "cc_lic_key", "api_base": "https://example.test"}


# ── pro_ensure_binary: fail closed, never silently fall back to free ───────────
def _http_error(status):
    def _raise(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b'{"error":"nope"}'))
    return _raise


def test_pro_ensure_binary_auth_failure_raises(monkeypatch):
    monkeypatch.setattr(dl.urllib.request, "urlopen", _http_error(401))
    with pytest.raises(RuntimeError, match=r"not authorized \(HTTP 401\)"):
        dl.pro_ensure_binary("cc_lic_bad", quiet=True)


def test_pro_ensure_binary_no_url_raises(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"version":"149.0.0.0"}'  # no url/sha256

    monkeypatch.setattr(dl.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    with pytest.raises(RuntimeError, match="not currently available"):
        dl.pro_ensure_binary("cc_lic_ok", quiet=True)


def test_pro_ensure_binary_requests_authenticated_route(monkeypatch):
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"  # empty -> will raise "no download", but the request was captured

    def _capture(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(dl.urllib.request, "urlopen", _capture)
    with pytest.raises(RuntimeError):
        dl.pro_ensure_binary("cc_lic_probe", api_base="https://example.test", quiet=True)
    assert seen["url"].startswith("https://example.test/api/v1/download/pro?platform=")
    assert seen["auth"] == "Bearer cc_lic_probe"
