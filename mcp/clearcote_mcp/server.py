"""Clearcote MCP server — drive the open-source stealth Chromium from any MCP client
(Claude Desktop, Cursor, Cline, ...).

One shared stealth browser, launched via ``clearcote.serve()`` (a raw CDP endpoint the tools
attach to over Playwright, and that ``get_cdp_endpoint`` hands to any other client). The persona
(seed / platform / proxy / geoip) is read from the environment so the tool surface stays clean.

Hardening (ported from the Fortress MCP design): every tool has a wall-clock timeout and returns a
STRUCTURED error instead of crashing the server; URL args are SSRF-checked (no localhost / private /
cloud-metadata unless opted in); file writes are confined to a sandbox dir; oversized text fields
are capped so a response never floods the agent's context; the shared browser is guarded by an
asyncio lock and rebuilt if it dies.
"""
from __future__ import annotations

import asyncio
import functools
import os
import sys
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ._facade import ClearcoteBrowser

# ── tool annotations ─────────────────────────────────────────────────────────
_READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
_LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, openWorldHint=True)

_TOOL_TIMEOUT = float(os.environ.get("CLEARCOTE_MCP_TOOL_TIMEOUT", "90"))


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get("CLEARCOTE_" + name)
    return v if v is not None else default


# ── error boundary ───────────────────────────────────────────────────────────
def _safe(fn):
    """Per-tool wall-clock timeout + structured error, so a bad/slow call never wedges the server."""
    @functools.wraps(fn)
    async def wrap(*args, **kwargs):
        try:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=_TOOL_TIMEOUT)
        except asyncio.TimeoutError:
            return {"status": "error",
                    "error": f"tool timed out after {_TOOL_TIMEOUT:.0f}s "
                             f"(raise CLEARCOTE_MCP_TOOL_TIMEOUT for slow pages)"}
        except Exception as exc:  # noqa: BLE001 — deliberate catch-all at the tool boundary
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return wrap


# ── SSRF guard ───────────────────────────────────────────────────────────────
def _ip_blocked(ip_str: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def _check_url(url: str | None) -> None:
    """Refuse localhost / private / cloud-metadata targets unless CLEARCOTE_ALLOW_PRIVATE_EGRESS=1."""
    if url is None:
        return
    if _env("ALLOW_PRIVATE_EGRESS", "0") == "1":
        return
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").strip("[]")
    if not host:
        return
    if host.lower() in ("localhost", "metadata.google.internal"):
        raise ValueError(f"refused private/metadata host {host!r} (set CLEARCOTE_ALLOW_PRIVATE_EGRESS=1 to allow)")
    if _ip_blocked(host):
        raise ValueError(f"refused private/internal address {host!r} (set CLEARCOTE_ALLOW_PRIVATE_EGRESS=1 to allow)")
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
    except Exception:
        return
    for info in infos:
        if _ip_blocked(info[4][0]):
            raise ValueError(f"refused private/internal address for {host!r} (set CLEARCOTE_ALLOW_PRIVATE_EGRESS=1 to allow)")


# ── write-path sandbox ───────────────────────────────────────────────────────
def _write_root() -> str:
    import tempfile
    root = _env("MCP_WRITE_DIR") or os.path.join(tempfile.gettempdir(), "clearcote-mcp")
    os.makedirs(root, exist_ok=True)
    return os.path.abspath(root)


def _confine_path(path: str | None, suffix: str) -> str:
    import tempfile
    root = _write_root()
    if _env("MCP_ALLOW_ANY_PATH", "0") == "1" and path:
        return path
    if not path:
        fd, p = tempfile.mkstemp(suffix=suffix, prefix="clearcote_", dir=root)
        os.close(fd)
        return p
    name = os.path.basename(path) or ("out" + suffix)
    if not os.path.splitext(name)[1]:
        name += suffix
    return os.path.join(root, name)


def _cap(d: dict, limits: dict[str, int]) -> dict:
    out = dict(d)
    for field, n in limits.items():
        v = out.get(field)
        if isinstance(v, str) and len(v) > n:
            out[field] = v[:n]
            out[f"{field}_truncated"] = True
    return out


# ── shared browser ───────────────────────────────────────────────────────────
def _persona_from_env() -> dict:
    p: dict = {}
    for env_key, opt in (("FINGERPRINT", "fingerprint"), ("PLATFORM", "platform"),
                         ("BRAND", "brand"), ("TIMEZONE", "timezone"),
                         ("ACCEPT_LANGUAGE", "accept_language")):
        v = _env(env_key)
        if v:
            p[opt] = v
    if _env("GEOIP", "0") == "1":
        p["geoip"] = True
    if _env("BINARY"):
        p["executable_path"] = _env("BINARY")
    proxy = _env("PROXY")
    if proxy:
        from clearcote._serve import _parse_proxy
        p["proxy"] = _parse_proxy(proxy)
    port = _env("SERVE_PORT")
    if port:
        p["port"] = int(port)
    p["headless"] = _env("HEADLESS", "1") != "0"
    return p


_browser: ClearcoteBrowser | None = None
_lock: asyncio.Lock | None = None


async def _b() -> ClearcoteBrowser:
    """Shared stealth browser; started on first use, rebuilt if it dies. Concurrency-safe."""
    global _browser, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        if _browser is not None and not _browser.is_healthy():
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None
        if _browser is None:
            inst = ClearcoteBrowser(_persona_from_env())
            await inst.start()
            _browser = inst
    return _browser


@asynccontextmanager
async def _lifespan(_server):
    """Pre-warm the browser so the agent's first tool call is warm, not a cold launch."""
    if _env("MCP_PREWARM", "1") != "0":
        try:
            await _b()
        except Exception:
            pass
    try:
        yield
    finally:
        global _browser
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
            _browser = None


mcp = FastMCP("Clearcote Stealth Browser", lifespan=_lifespan)


# ── tools ─────────────────────────────────────────────────────────────────────
@mcp.tool(annotations=_WRITE)
@_safe
async def navigate(url: str) -> dict:
    """Navigate the current tab to a URL. Returns the resolved url + title."""
    await _check_url(url)
    return await (await _b()).navigate(url)


@mcp.tool(annotations=_READ)
@_safe
async def read_page(url: str | None = None) -> dict:
    """Read the current page (or navigate to `url` first) as clean text + Markdown."""
    await _check_url(url)
    return _cap(await (await _b()).read_page(url), {"text": 20000, "markdown": 40000})


@mcp.tool(annotations=_READ)
@_safe
async def get_page_html(url: str | None = None) -> dict:
    """Get the raw HTML of the current page (or navigate to `url` first)."""
    await _check_url(url)
    return _cap(await (await _b()).get_html(url), {"html": 80000})


@mcp.tool(annotations=_READ)
@_safe
async def page_elements(url: str | None = None) -> dict:
    """List the interactive elements (links, buttons, inputs) on the page, each with a selector."""
    await _check_url(url)
    return await (await _b()).page_elements(url)


@mcp.tool(annotations=_WRITE)
@_safe
async def click(target: str, url: str | None = None) -> dict:
    """Click an element by CSS selector or by visible text (navigate to `url` first if given)."""
    await _check_url(url)
    return await (await _b()).click(target, url)


@mcp.tool(annotations=_WRITE)
@_safe
async def fill_field(field: str, value: str, url: str | None = None) -> dict:
    """Fill an input matched by selector, label, placeholder, or name."""
    await _check_url(url)
    return await (await _b()).fill(field, value, url)


@mcp.tool(annotations=_WRITE)
@_safe
async def press_key(key: str) -> dict:
    """Press a key on the current page, e.g. 'Enter', 'Tab', 'Control+A'."""
    return await (await _b()).press(key)


@mcp.tool(annotations=_READ)
@_safe
async def evaluate_js(expression: str, url: str | None = None) -> dict:
    """Evaluate a JavaScript expression in the page and return the (JSON-serializable) result."""
    await _check_url(url)
    return await (await _b()).evaluate(expression, url)


@mcp.tool(annotations=_READ)
@_safe
async def wait_for(selector: str, url: str | None = None, timeout_ms: int = 10000) -> dict:
    """Wait until a selector appears (up to timeout_ms)."""
    await _check_url(url)
    return await (await _b()).wait_for(selector, url, timeout_ms)


@mcp.tool(annotations=_READ)
@_safe
async def current_page() -> dict:
    """Return the current tab's url + title."""
    return await (await _b()).current_page()


@mcp.tool(annotations=_WRITE)
@_safe
async def screenshot_page(url: str | None = None, path: str | None = None) -> dict:
    """Screenshot the current page (or navigate to `url` first). Saved under the sandbox dir."""
    await _check_url(url)
    return await (await _b()).screenshot(_confine_path(path, ".png"), url)


@mcp.tool(annotations=_WRITE)
@_safe
async def save_page_pdf(url: str | None = None, path: str | None = None) -> dict:
    """Save the current page as a PDF (headless only). Saved under the sandbox dir."""
    await _check_url(url)
    return await (await _b()).save_pdf(_confine_path(path, ".pdf"), url)


@mcp.tool(annotations=_READ)
@_safe
async def get_cookies(url: str | None = None) -> dict:
    """Get cookies for the current context (optionally filtered to `url`)."""
    await _check_url(url)
    return await (await _b()).cookies(url)


@mcp.tool(annotations=_LOCAL)
@_safe
async def list_tabs() -> dict:
    """List open tabs (index, url, title, which is current)."""
    return await (await _b()).list_tabs()


@mcp.tool(annotations=_WRITE)
@_safe
async def new_tab(url: str | None = None) -> dict:
    """Open a new tab (optionally navigate it) and make it current."""
    await _check_url(url)
    return await (await _b()).new_tab(url)


@mcp.tool(annotations=_WRITE)
@_safe
async def close_tab(index: int) -> dict:
    """Close the tab at `index` (from list_tabs)."""
    return await (await _b()).close_tab(index)


@mcp.tool(annotations=_WRITE)
@_safe
async def save_profile(name: str = "session") -> dict:
    """Save cookies + storage state to a named profile under the sandbox dir."""
    return await (await _b()).save_profile(_confine_path(name, ".json"))


@mcp.tool(annotations=_WRITE)
@_safe
async def load_profile(name: str = "session") -> dict:
    """Restore cookies + storage from a named profile (see save_profile)."""
    return await (await _b()).load_profile(_confine_path(name, ".json"))


@mcp.tool(annotations=_LOCAL)
@_safe
async def get_egress_info() -> dict:
    """Report the browser's public egress IP (through any configured proxy) + the active persona."""
    return await (await _b()).egress_info()


@mcp.tool(annotations=_LOCAL)
@_safe
async def get_cdp_endpoint() -> dict:
    """Return the stealth browser's raw CDP endpoint so ANY other client (Playwright / Puppeteer /
    browser-use / Crawl4AI / Stagehand) can attach to the SAME browser with `connect_over_cdp`,
    keeping the stealth persona. This is the whole point of the drop-in model."""
    b = await _b()
    return {"status": "ok", "cdp_url": b.cdp_url,
            "connect": {
                "playwright_python": f"p.chromium.connect_over_cdp({b.cdp_url!r})",
                "playwright_node": f'chromium.connectOverCDP("{b.cdp_url}")',
                "puppeteer": f'puppeteer.connect({{ browserURL: "{b.cdp_url}" }})',
                "browser_use": f'cdp_url="{b.cdp_url}"'}}


def main() -> None:
    """stdio MCP entry point (used by `clearcote-mcp` and `python -m clearcote_mcp`)."""
    try:
        mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
