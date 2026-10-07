"""Async browser facade the MCP tools drive.

It launches ONE clearcote stealth browser via ``clearcote.serve()`` (a raw CDP endpoint) and
attaches to it with Playwright's async ``connect_over_cdp`` — so the exact same browser the tools
drive is ALSO the endpoint any other client can attach to (``get_cdp_endpoint``). Attaching over
CDP adds no launch flags, so the served persona (``navigator.webdriver=false``, Windows UA, etc.)
is preserved end to end.

The facade owns a "current page" (the active tab) so an agent can call tools without repeating a
URL; pass ``url`` to any read/act tool to navigate first.

CLOUD MODE (``cloud=True``, from ``CLEARCOTE_CLOUD=1``): the browser is a hosted Clearcote session
instead, from ``clearcote.async_api.launch(cloud=True)``. Every tool works the same; the one
difference is that a cloud session's CDP URL is single-use and this server holds it, so there is no
endpoint to hand to another client.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import json
import os
import threading

from . import _egress


# page_state: what the page amounts to, from neutral signals only (the main document's HTTP status and how much
# visible text it has). An agent reads it to decide whether to wait, retry or move on.
READ_FORMATS = ("markdown", "text", "both")
BLOCKED_STATUSES = frozenset({401, 403, 429, 503})
EMPTY_TEXT_CHARS = 20


# The shown document's scheme and the HTTP status the browser recorded for its response (0 when it has none).
_DOCUMENT_STATUS_JS = ("(() => { const e = performance.getEntriesByType('navigation')[0];"
                       " return [location.protocol, (e && e.responseStatus) || 0]; })()")


def page_state(http_status: int | None, text: str) -> str:
    """"blocked" for HTTP 401/403/429/503, else "empty" under EMPTY_TEXT_CHARS of visible text, else "ok"."""
    if http_status in BLOCKED_STATUSES:
        return "blocked"
    if len("".join((text or "").split())) < EMPTY_TEXT_CHARS:
        return "empty"
    return "ok"


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


def _in_thread(fn) -> concurrent.futures.Future:
    """fn() in a thread of its own. Its future stays readable when the caller stops waiting for it: a start cut short
    by a stop signal still has to stop the browser serve() goes on to launch (see ClearcoteBrowser.close)."""
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_running_or_notify_cancel()  # running: a caller that stops waiting cannot cancel it

    def run():
        try:
            future.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 -- handed to whoever reads the future
            future.set_exception(exc)
    threading.Thread(target=run, name="clearcote-serve", daemon=True).start()
    return future


def cloud_launch():
    """``clearcote.async_api.launch``, checked to know ``cloud=`` (clearcote 0.34+). Older SDKs pass
    unknown options through to Playwright, so the version is checked instead of trusting a call."""
    try:
        from clearcote.async_api import launch
        from clearcote.cloud import AsyncCloud  # noqa: F401 -- only present on 0.34+
    except ImportError:
        raise RuntimeError("cloud mode needs clearcote 0.34 or newer: pip install -U clearcote") from None
    return launch


class ClearcoteBrowser:
    """One shared, stealth clearcote browser + its Playwright attachment."""

    def __init__(self, persona: dict | None = None, cloud: bool = False, guard: bool = False):
        self._persona = persona or {}
        self._cloud = cloud
        self._guard = guard       # check every request the browser makes (see _egress.py)
        self._srv = None          # clearcote._serve.Server
        self._launching = None    # serve() still running in its thread (a concurrent Future), until start() has it
        self._pw = None           # playwright async context manager
        self._browser = None      # playwright Browser (over CDP)
        self._ctx = None          # BrowserContext
        self._page = None         # current page

    @property
    def cloud(self) -> bool:
        return self._cloud

    @property
    def cloud_session(self) -> dict | None:
        """The hosted session (id, worker, expiresAt, ...) in cloud mode, else None."""
        return getattr(self._browser, "cloud_session", None) if self._cloud else None

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self):
        if self._cloud:
            # A hosted session: the SDK creates it, connects over CDP and owns the Playwright driver
            # (closing the browser disconnects, stops that driver and ends the session).
            launch = cloud_launch()
            self._browser = await launch(cloud=True, **self._persona)
            try:
                self._ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
                self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                if self._guard:
                    await _egress.guard_context(self._ctx)
            except BaseException:
                # the caller never gets this instance, so nobody else would end the (billed) session
                await self.close()
                raise
            return
        import clearcote
        from playwright.async_api import async_playwright
        # serve() is a blocking subprocess launch — off the event loop, in a thread whose result close() can still
        # collect when this start is cancelled while it runs.
        self._launching = _in_thread(lambda: clearcote.serve(quiet=True, **self._persona))
        self._srv = await asyncio.wrap_future(self._launching)
        self._launching = None
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.connect_over_cdp(self._srv.cdp_url)
        self._ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        if self._guard:
            await _egress.guard_context(self._ctx)

    async def http_status(self, page) -> int | None:
        """HTTP status of the document the page shows now, as the browser recorded it for that document: a page
        brought back by Back reports its own status, not the last response seen. Read in an isolated world, which
        the page's scripts cannot see. None without an HTTP exchange (about:blank, file:, data:) or when the browser
        has no status for the document."""
        try:
            cdp = await self._ctx.new_cdp_session(page)
        except Exception:
            return None
        try:
            frame = (await cdp.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            world = await cdp.send("Page.createIsolatedWorld", {"frameId": frame, "worldName": "clearcote-mcp"})
            answer = await cdp.send("Runtime.evaluate", {"expression": _DOCUMENT_STATUS_JS, "returnByValue": True,
                                                         "contextId": world["executionContextId"]})
            scheme, status = answer["result"]["value"]
            return (status or None) if scheme in ("http:", "https:") else None
        except Exception:
            return None
        finally:
            try:
                await cdp.detach()
            except Exception:
                pass

    @staticmethod
    async def _goto(page, url: str, timeout: float = 45000):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        except Exception as exc:
            if "ERR_BLOCKED_BY_CLIENT" in str(exc) and _egress.refused:  # the request guard stopped it on the way
                blocked, reason = _egress.refused[-1]
                raise ValueError(f"{reason} (reached through {blocked[:120]})") from None
            raise

    @staticmethod
    async def _text(page) -> str:
        """The page's visible text, or "" when there is no body to read."""
        try:
            return await page.inner_text("body", timeout=5000)
        except Exception:
            return ""

    async def _document(self, page, text: str) -> dict:
        status = await self.http_status(page)
        return {"status": "ok", "url": page.url, "title": await page.title(),
                "http_status": status, "page_state": page_state(status, text)}

    def is_healthy(self) -> bool:
        try:
            if self._cloud:
                return bool(self._browser and self._browser.is_connected())
            return bool(self._srv and self._srv.is_alive() and self._browser and self._browser.is_connected())
        except Exception:
            return False

    async def close(self):
        if self._launching is not None and self._srv is None:
            # start() was cut short while serve() ran in its thread: wait for the browser it launches, to stop it too
            try:
                self._srv = await asyncio.wrap_future(self._launching)
            except Exception:  # the launch failed: nothing to stop
                pass
        self._launching = None
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
                # Server.close() blocks (it waits up to 10 s for the browser to exit): off the event loop, so a caller's
                # time limit still holds
                await asyncio.to_thread(self._srv.close)
        except Exception:
            pass
        self._srv = self._pw = self._browser = self._ctx = self._page = None

    @property
    def cdp_url(self) -> str:
        """The local endpoint any client can attach to; empty in cloud mode (single-use URL)."""
        return self._srv.cdp_url if self._srv else ""

    async def _pg(self, url: str | None):
        if url:
            await self._goto(self._page, url)
        return self._page

    # ── read ─────────────────────────────────────────────────────────────────
    async def navigate(self, url: str) -> dict:
        pg = await self._pg(url)
        return await self._document(pg, await self._text(pg))

    async def read_page(self, url: str | None = None, format: str = "markdown") -> dict:
        """The page as Markdown, visible text or both (`format`), with its HTTP status and page_state. Uncapped:
        the MCP tool caps the fields."""
        if format not in READ_FORMATS:
            raise ValueError(f"format must be one of {', '.join(READ_FORMATS)}, not {format!r}")
        pg = await self._pg(url)
        text = await self._text(pg)
        out = await self._document(pg, text)
        if format in ("markdown", "both"):
            out["markdown"] = _md(await pg.content())
        if format in ("text", "both"):
            out["text"] = text
        return out

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
        # Raw CDP, not pg.screenshot(): Playwright prepares the page first (by default an inline caret-color on every
        # input, textarea and contenteditable, set and then restored), DOM changes the page can observe.
        cdp = await self._ctx.new_cdp_session(pg)
        try:
            params = {"format": "png"}
            if full_page:
                size = (await cdp.send("Page.getLayoutMetrics"))["cssContentSize"]
                params.update(captureBeyondViewport=True,
                              clip={"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1})
            shot = await cdp.send("Page.captureScreenshot", params)
        finally:
            await cdp.detach()
        with open(path, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
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
        if self._guard:  # before its first navigation, so its first redirect is held too
            await _egress.guard_redirects(self._ctx, self._page)
        if url:
            await self._goto(self._page, url)
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
                await self._goto(pg, prev, timeout=15000)
        except Exception:
            pass
        out = {"status": "ok", "public_ip": info.get("ip") if isinstance(info, dict) else None,
               "persona": {k: self._persona.get(k) for k in ("fingerprint", "platform", "brand", "proxy") if self._persona.get(k)}}
        if self._cloud:
            out["cloud_session"] = (self.cloud_session or {}).get("id")
        return out
