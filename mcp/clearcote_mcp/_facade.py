"""Async browser facade the MCP tools drive.

It launches ONE clearcote stealth browser via ``clearcote.serve()`` (a raw CDP endpoint) and
attaches to it with Playwright's async ``connect_over_cdp`` — so the exact same browser the tools
drive is ALSO the endpoint any other client can attach to (``get_cdp_endpoint``). Attaching over
CDP adds no launch flags, so the served persona (``navigator.webdriver=false``, Windows UA, etc.)
is preserved end to end.

The facade owns a "current page" (the active tab) so an agent can call tools without repeating a
URL; pass ``url`` to any read/act tool to navigate first.
"""
from __future__ import annotations

import asyncio
import json
import os


def _md(html: str) -> str:
    """HTML -> Markdown, best-effort. Uses markdownify if present, else falls back to a light strip."""
    try:
        from markdownify import markdownify
        return markdownify(html, heading_style="ATX", strip=["script", "style", "noscript"])
    except Exception:
        import re
        text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        return re.sub(r"[ \t\r\f\v]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


# In-page collector for interactive elements (links, buttons, inputs) with a stable-ish selector.
_ELEMENTS_JS = r"""() => {
  const out = [];
  const sel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const nm = el.getAttribute('name'); if (nm) return el.tagName.toLowerCase() + '[name="' + nm + '"]';
    const aria = el.getAttribute('aria-label'); if (aria) return el.tagName.toLowerCase() + '[aria-label="' + aria + '"]';
    return null;
  };
  const seen = new Set();
  for (const el of document.querySelectorAll('a[href], button, input, textarea, select, [role=button], [onclick]')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;                 // skip hidden
    const txt = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 120);
    const item = { tag: el.tagName.toLowerCase(), text: txt,
                   type: el.getAttribute('type') || null,
                   href: el.getAttribute('href') || null, selector: sel(el) };
    const key = JSON.stringify(item);
    if (seen.has(key)) continue; seen.add(key);
    out.push(item);
    if (out.length >= 200) break;
  }
  return out;
}"""


class ClearcoteBrowser:
    """One shared, stealth clearcote browser + its Playwright attachment."""

    def __init__(self, persona: dict | None = None):
        self._persona = persona or {}
        self._srv = None          # clearcote._serve.Server
        self._pw = None           # playwright async context manager
        self._browser = None      # playwright Browser (over CDP)
        self._ctx = None          # BrowserContext
        self._page = None         # current page

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self):
        import clearcote
        from playwright.async_api import async_playwright
        loop = asyncio.get_running_loop()
        # serve() is a blocking subprocess launch — off the event loop.
        self._srv = await loop.run_in_executor(None, lambda: clearcote.serve(quiet=True, **self._persona))
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self._srv.cdp_url)
        self._ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    def is_healthy(self) -> bool:
        try:
            return bool(self._srv and self._srv.is_alive() and self._browser and self._browser.is_connected())
        except Exception:
            return False

    async def close(self):
        for step in (
            lambda: self._browser.close() if self._browser else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                r = step()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                pass
        try:
            if self._srv:
                self._srv.close()
        except Exception:
            pass
        self._srv = self._pw = self._browser = self._ctx = self._page = None

    @property
    def cdp_url(self) -> str:
        return self._srv.cdp_url if self._srv else ""

    async def _pg(self, url: str | None):
        if url:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return self._page

    # ── read ─────────────────────────────────────────────────────────────────
    async def navigate(self, url: str) -> dict:
        pg = await self._pg(url)
        return {"status": "ok", "url": pg.url, "title": await pg.title()}

    async def read_page(self, url: str | None = None) -> dict:
        pg = await self._pg(url)
        html = await pg.content()
        return {"status": "ok", "url": pg.url, "title": await pg.title(),
                "text": (await pg.inner_text("body"))[:200000], "markdown": _md(html)[:200000]}

    async def get_html(self, url: str | None = None) -> dict:
        pg = await self._pg(url)
        return {"status": "ok", "url": pg.url, "html": await pg.content()}

    async def page_elements(self, url: str | None = None) -> dict:
        pg = await self._pg(url)
        return {"status": "ok", "url": pg.url, "elements": await pg.evaluate(_ELEMENTS_JS)}

    async def evaluate(self, expression: str, url: str | None = None) -> dict:
        pg = await self._pg(url)
        return {"status": "ok", "url": pg.url, "result": await pg.evaluate(expression)}

    async def wait_for(self, selector: str, url: str | None = None, timeout_ms: int = 10000) -> dict:
        pg = await self._pg(url)
        await pg.wait_for_selector(selector, timeout=timeout_ms)
        return {"status": "ok", "url": pg.url, "found": selector}

    async def current_page(self) -> dict:
        pg = self._page
        return {"status": "ok", "url": pg.url, "title": await pg.title()}

    # ── act ──────────────────────────────────────────────────────────────────
    async def click(self, target: str, url: str | None = None) -> dict:
        pg = await self._pg(url)
        # try as a CSS selector first, then fall back to visible text
        try:
            await pg.click(target, timeout=6000)
        except Exception:
            await pg.get_by_text(target, exact=False).first.click(timeout=6000)
        await pg.wait_for_load_state("domcontentloaded")
        return {"status": "ok", "url": pg.url, "clicked": target}

    async def fill(self, field: str, value: str, url: str | None = None) -> dict:
        pg = await self._pg(url)
        loc = None
        for attempt in (
            lambda: pg.locator(field),
            lambda: pg.get_by_label(field),
            lambda: pg.get_by_placeholder(field),
            lambda: pg.locator("[name='%s']" % field),
        ):
            try:
                cand = attempt()
                await cand.first.fill(value, timeout=4000)
                loc = field
                break
            except Exception:
                continue
        if loc is None:
            raise ValueError("no fillable field matched %r" % field)
        return {"status": "ok", "url": pg.url, "filled": field}

    async def press(self, key: str) -> dict:
        await self._page.keyboard.press(key)
        return {"status": "ok", "pressed": key}

    # ── capture ──────────────────────────────────────────────────────────────
    async def screenshot(self, path: str, url: str | None = None, full_page: bool = True) -> dict:
        pg = await self._pg(url)
        await pg.screenshot(path=path, full_page=full_page)
        return {"status": "ok", "url": pg.url, "path": path}

    async def save_pdf(self, path: str, url: str | None = None) -> dict:
        pg = await self._pg(url)
        await pg.pdf(path=path)  # headless only
        return {"status": "ok", "url": pg.url, "path": path}

    # ── state ────────────────────────────────────────────────────────────────
    async def cookies(self, url: str | None = None) -> dict:
        c = await self._ctx.cookies(url) if url else await self._ctx.cookies()
        return {"status": "ok", "cookies": c}

    async def list_tabs(self) -> dict:
        tabs = []
        for i, p in enumerate(self._ctx.pages):
            tabs.append({"index": i, "url": p.url, "title": await p.title(), "current": p is self._page})
        return {"status": "ok", "tabs": tabs}

    async def new_tab(self, url: str | None = None) -> dict:
        self._page = await self._ctx.new_page()
        if url:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        return {"status": "ok", "url": self._page.url, "index": len(self._ctx.pages) - 1}

    async def close_tab(self, index: int) -> dict:
        pages = self._ctx.pages
        if not (0 <= index < len(pages)):
            raise ValueError("tab index %d out of range (0..%d)" % (index, len(pages) - 1))
        target = pages[index]
        await target.close()
        if target is self._page:
            self._page = self._ctx.pages[-1] if self._ctx.pages else await self._ctx.new_page()
        return {"status": "ok", "closed": index}

    async def save_profile(self, path: str) -> dict:
        state = await self._ctx.storage_state()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        return {"status": "ok", "path": path,
                "cookies": len(state.get("cookies", [])), "origins": len(state.get("origins", []))}

    async def load_profile(self, path: str) -> dict:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if state.get("cookies"):
            await self._ctx.add_cookies(state["cookies"])
        return {"status": "ok", "path": path, "cookies": len(state.get("cookies", []))}

    async def egress_info(self) -> dict:
        pg = self._page
        prev = pg.url
        try:
            info = await pg.evaluate(
                "async () => { const r = await fetch('https://api.ipify.org?format=json',{cache:'no-store'});"
                " return await r.json(); }")
        except Exception as exc:
            info = {"error": str(exc)}
        try:
            if prev and prev != "about:blank":
                await pg.goto(prev, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        return {"status": "ok", "public_ip": info.get("ip") if isinstance(info, dict) else None,
                "persona": {k: self._persona.get(k) for k in ("fingerprint", "platform", "brand", "proxy") if self._persona.get(k)}}
