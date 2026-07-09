import os
import pytest
from clearcote_mcp import server as S


@pytest.mark.asyncio
async def test_all_tools_registered():
    tools = await S.mcp.list_tools()
    names = {t.name for t in tools}
    assert {"navigate", "read_page", "get_page_html", "page_elements", "click",
            "fill_field", "press_key", "evaluate_js", "wait_for", "current_page",
            "screenshot_page", "save_page_pdf", "get_cookies", "list_tabs", "new_tab",
            "close_tab", "save_profile", "load_profile", "get_egress_info",
            "get_cdp_endpoint"} <= names
    assert len(tools) >= 20


def test_ip_blocked():
    assert S._ip_blocked("127.0.0.1")
    assert S._ip_blocked("169.254.169.254")   # cloud metadata
    assert S._ip_blocked("10.0.0.5")
    assert S._ip_blocked("::1")
    assert not S._ip_blocked("8.8.8.8")
    assert not S._ip_blocked("not-an-ip")


@pytest.mark.asyncio
async def test_check_url_ssrf_guard(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_ALLOW_PRIVATE_EGRESS", raising=False)
    with pytest.raises(ValueError):
        await S._check_url("http://localhost/x")
    with pytest.raises(ValueError):
        await S._check_url("http://169.254.169.254/latest/meta-data/")
    await S._check_url("https://example.com")   # public → ok
    await S._check_url(None)                     # no url → ok
    monkeypatch.setenv("CLEARCOTE_ALLOW_PRIVATE_EGRESS", "1")
    await S._check_url("http://localhost/x")     # opted in → ok


def test_confine_path_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_MCP_WRITE_DIR", str(tmp_path))
    monkeypatch.delenv("CLEARCOTE_MCP_ALLOW_ANY_PATH", raising=False)
    p = S._confine_path("../../etc/passwd", ".png")
    assert os.path.dirname(p) == str(tmp_path)          # confined to sandbox root
    assert os.path.basename(p) == "passwd.png"          # traversal + missing ext handled


def test_cap_trims_and_flags():
    out = S._cap({"text": "x" * 100, "small": "ok"}, {"text": 10})
    assert out["text"] == "x" * 10 and out["text_truncated"] is True
    assert out["small"] == "ok" and "small_truncated" not in out


def test_persona_from_env(monkeypatch):
    for k in ("FINGERPRINT", "PLATFORM", "GEOIP", "HEADLESS", "PROXY", "BINARY"):
        monkeypatch.delenv("CLEARCOTE_" + k, raising=False)
    monkeypatch.setenv("CLEARCOTE_FINGERPRINT", "seed-9")
    monkeypatch.setenv("CLEARCOTE_PLATFORM", "windows")
    monkeypatch.setenv("CLEARCOTE_HEADLESS", "0")
    monkeypatch.setenv("CLEARCOTE_PROXY", "http://u:p@h:8080")
    p = S._persona_from_env()
    assert p["fingerprint"] == "seed-9" and p["platform"] == "windows"
    assert p["headless"] is False
    assert p["proxy"] == {"server": "http://h:8080", "username": "u", "password": "p"}


@pytest.mark.asyncio
async def test_safe_returns_structured_error():
    @S._safe
    async def boom():
        raise RuntimeError("nope")
    r = await boom()
    assert r["status"] == "error" and "RuntimeError" in r["error"]
