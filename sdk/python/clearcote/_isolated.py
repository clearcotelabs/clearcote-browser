"""Evaluate humanize's DOM reads in an isolated JavaScript world, not the page's own.

``page.evaluate`` runs in the page's main world: every DOM call it makes goes through the page's
prototypes, so a page that wraps ``Document.prototype.querySelector`` or ``elementFromPoint`` sees
each read, with a Playwright ``UtilityScript.evaluate`` frame on its stack (measured on r28 with
humanize on: 3 hook hits per session; 0 with humanize off and on genuine Chrome).

An isolated world (CDP ``Page.createIsolatedWorld``, the mechanism extensions' content scripts use)
shares the DOM but has its own JavaScript globals and prototypes, so the page cannot observe what
runs there. The world is created lazily per page and again after a navigation destroys it.

``evaluate()`` returns ``None`` when the read is impossible (no CDP session, page closed, a
navigation mid-call). Callers fall back to their safe default -- never to the page world.

Mirrors sdk/node/src/isolated.ts and sdk/dotnet/src/Clearcote/IsolatedWorld.cs.
"""

import json


def _expression(fn_source, arg):
    return "(%s)(%s)" % (fn_source, json.dumps(arg))


def _value(result):
    if not isinstance(result, dict) or "exceptionDetails" in result:
        return None
    return (result.get("result") or {}).get("value")


class IsolatedWorld:
    """Sync: an isolated world in ``page``'s main frame."""

    def __init__(self, page):
        self._page = page
        self._cdp = None
        self._ctx = None

    def evaluate(self, fn_source, arg=None):
        """Run ``(fn_source)(arg)`` (``arg`` JSON-serialisable) and return its JSON value, or None."""
        for _ in range(2):  # a stale context (navigation) is re-created once
            try:
                if self._cdp is None:
                    self._cdp = self._page.context.new_cdp_session(self._page)
                if self._ctx is None:
                    frame_id = self._cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
                    self._ctx = self._cdp.send(
                        "Page.createIsolatedWorld", {"frameId": frame_id})["executionContextId"]
                return _value(self._cdp.send("Runtime.evaluate", {
                    "expression": _expression(fn_source, arg), "contextId": self._ctx,
                    "returnByValue": True}))
            except Exception:  # noqa: BLE001
                self._ctx = None
        return None


class AsyncIsolatedWorld:
    """Async: an isolated world in ``page``'s main frame."""

    def __init__(self, page):
        self._page = page
        self._cdp = None
        self._ctx = None

    async def evaluate(self, fn_source, arg=None):
        for _ in range(2):
            try:
                if self._cdp is None:
                    self._cdp = await self._page.context.new_cdp_session(self._page)
                if self._ctx is None:
                    tree = await self._cdp.send("Page.getFrameTree")
                    created = await self._cdp.send(
                        "Page.createIsolatedWorld", {"frameId": tree["frameTree"]["frame"]["id"]})
                    self._ctx = created["executionContextId"]
                return _value(await self._cdp.send("Runtime.evaluate", {
                    "expression": _expression(fn_source, arg), "contextId": self._ctx,
                    "returnByValue": True}))
            except Exception:  # noqa: BLE001
                self._ctx = None
        return None


def world_for(page):
    """The page's IsolatedWorld (created on first use and kept on the page)."""
    world = getattr(page, "_cc_isolated_world", None)
    if world is None:
        world = IsolatedWorld(page)
        try:
            page._cc_isolated_world = world
        except Exception:  # noqa: BLE001
            pass
    return world


def async_world_for(page):
    """The page's AsyncIsolatedWorld (created on first use and kept on the page)."""
    world = getattr(page, "_cc_isolated_world", None)
    if world is None:
        world = AsyncIsolatedWorld(page)
        try:
            page._cc_isolated_world = world
        except Exception:  # noqa: BLE001
            pass
    return world


# How long a trial action may look for a covered click point before the native path takes over.
COVER_CHECK_MS = 400

# The reads humanize makes, as functions of one JSON argument (run by IsolatedWorld.evaluate).
VIEWPORT = "() => [innerWidth, innerHeight]"
# The platform the page believes (the persona's), for the wheel notch size.
PLATFORM = "() => { const d = navigator.userAgentData; return (d && d.platform) || navigator.platform || ''; }"
IS_FOCUSED = ("(s) => { const e = document.querySelector(s);"
              " return !!e && e === document.activeElement; }")
SELECT_PLAN = """(a) => { const s = document.querySelector(a.sel);
     if (!s || s.multiple || s.disabled) return null;
     const os = [...s.options];
     let i = -1;
     if (a.by === 'index') i = (a.want >= 0 && a.want < os.length) ? a.want : -1;
     else if (a.by === 'label') i = os.findIndex(o => (o.label || o.textContent || '').trim() === String(a.want).trim());
     else i = os.findIndex(o => o.value === a.want);
     if (i < 0 || os[i].disabled) return null;
     return { to: i, from: s.selectedIndex, ret: os[i].value }; }"""
SELECTED_INDEX = "(s) => { const e = document.querySelector(s); return e ? e.selectedIndex : -1; }"
