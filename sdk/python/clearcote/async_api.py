"""Clearcote — async Playwright drop-in (Python).

    import asyncio
    from clearcote.async_api import launch

    async def main():
        browser = await launch(fingerprint="seed-123", platform="windows")
        page = await browser.new_page()
        await page.goto("https://abrahamjuliot.github.io/creepjs/")
        await browser.close()

    asyncio.run(main())

Same API + options as ``clearcote.launch`` (fingerprint/persona/proxy/geoip/profile/canvas-bridge —
everything maps to the same engine switches), but returns Playwright **async** objects so it works
inside an asyncio event loop, where the sync API raises
``It looks like you are using Playwright Sync API inside the asyncio loop``.

Each launched browser/context owns its Playwright driver and stops it on ``close()``.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

from . import _headed_no_viewport, _prepare, _acquire_lease_from_kwargs, _is_win_launch_race  # shared sync helpers
from ._license import inject_run_token
from ._fonts import apply_font_env
from ._humanize_async import install_humanize, install_humanize_on_context
from ._profile import Profile, list_profiles, load_profile
from ._render_async import check_render_coherence
from .download import ensure_binary, warm_files
from .geoip import resolve_geo
from .release import RELEASE

from . import __version__ as __version__  # re-export the package version

__all__ = [
    "launch",
    "launch_persistent_context",
    "launch_agent",
    "executable_path",
    "download",
    "run_agent_task",
    "resolve_geo",
    "Profile",
    "list_profiles",
    "load_profile",
    "check_render_coherence",
    "RELEASE",
    "__version__",
]


async def executable_path(executable_path=None, cache_dir=None, quiet=False, auto_update=None):
    """Resolve the Clearcote chrome.exe path (download/verify if needed). Runs the blocking
    resolve in a thread so it never stalls the event loop."""
    from . import executable_path as _sync_executable_path
    return await asyncio.to_thread(
        _sync_executable_path, executable_path, cache_dir, quiet, auto_update)


async def download(cache_dir=None, quiet=False, auto_update=None):
    """Pre-fetch + verify the Clearcote binary without launching (off-loop). Returns the path."""
    return await asyncio.to_thread(
        ensure_binary, cache_dir=cache_dir, quiet=quiet, auto_update=auto_update)


def _bind_driver(closable, pw):
    """Stop the owned Playwright driver when this browser/context is closed."""
    orig_close = closable.close

    async def close(*args, **kwargs):
        try:
            return await orig_close(*args, **kwargs)
        finally:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass

    closable.close = close


def _install_headed_viewport(browser):
    """Default a headed browser's new pages/contexts to no_viewport (async)."""
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    async def new_page(**kw):
        if "viewport" not in kw and "no_viewport" not in kw:
            kw["no_viewport"] = True
        return await orig_new_page(**kw)

    async def new_context(**kw):
        if "viewport" not in kw and "no_viewport" not in kw:
            kw["no_viewport"] = True
        return await orig_new_context(**kw)

    browser.new_page, browser.new_context = new_page, new_context


async def _start_driver():
    from playwright.async_api import async_playwright
    return await async_playwright().start()


async def _win_av_retry_async(do_launch, exe):
    """Async mirror of the sync ``_win_av_retry``: work around the Windows first-launch
    'spawn UNKNOWN' / 'side-by-side configuration is incorrect' race — a just-extracted,
    unsigned chrome.exe can fail to spawn while real-time AV is still scanning chrome_elf.dll
    (the SxS assembly member its manifest depends on), and Windows caches that negative
    activation against the path. Re-scan + back off + retry, then relaunch from a pristine
    temp copy which always gets a clean SxS evaluation. Pass-through on non-Windows.

    Without this the async API launched the engine directly and surfaced the raw
    'spawn UNKNOWN' (the sync API has had this workaround; the async API did not)."""
    if sys.platform != "win32":
        return await do_launch(exe)
    for i in range(3):
        try:
            return await do_launch(exe)
        except Exception as exc:  # noqa: BLE001
            if not _is_win_launch_race(exc):
                raise
            await asyncio.to_thread(warm_files, os.path.dirname(exe))
            await asyncio.sleep(0.8 * (i + 1))
    # The in-place SxS activation-context poison never clears; relaunch from a fresh copy.
    recover = os.path.join(tempfile.mkdtemp(prefix="clearcote-recover-"), "browser")
    await asyncio.to_thread(shutil.copytree, os.path.dirname(exe), recover)
    await asyncio.to_thread(warm_files, recover)
    return await do_launch(os.path.join(recover, os.path.basename(exe)))


async def launch(**kwargs):
    """Launch Clearcote and return a Playwright **async** ``Browser``. Same kwargs as the sync
    ``clearcote.launch`` (fingerprint, platform, brand, gpu_*, timezone, accept_language, proxy,
    geoip, profile, canvas_bridge, humanize, ... + any Playwright launch option)."""
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    lease = await asyncio.to_thread(_acquire_lease_from_kwargs, kwargs)  # opt-in; None in free mode
    exe, args, pw_kwargs, humanize, show_cursor, seed = await asyncio.to_thread(_prepare, kwargs)
    if lease:  # inject CLEARCOTE_RUN_TOKEN so the PRO engine gate lets the browser launch
        inject_run_token(pw_kwargs, lease.token)
    await asyncio.to_thread(apply_font_env, exe, pw_kwargs)  # Linux: bundled font clones (mirror sync)
    headed = _headed_no_viewport(pw_kwargs)  # launch() takes no viewport kwarg -> wrap new_page/context
    pw = await _start_driver()
    try:
        browser = await _win_av_retry_async(
            lambda e: pw.chromium.launch(executable_path=e, args=args, **pw_kwargs), exe)
    except BaseException:
        if lease:
            lease.stop()
        await pw.stop()
        raise
    _bind_driver(browser, pw)
    if lease:  # release the concurrency slot when the browser closes
        browser.on("disconnected", lambda _b=None: lease.stop())
    if headed:
        _install_headed_viewport(browser)
    await install_humanize(browser, humanize, show_cursor, seed=seed)
    return browser


async def launch_persistent_context(user_data_dir, **kwargs):
    """Launch Clearcote with a persistent profile dir; returns a Playwright **async**
    ``BrowserContext`` (cookies/storage persist in ``user_data_dir``).

    Pass ``widevine=True`` to seed + enable the (opt-in) Widevine CDM so DRM/EME works."""
    # Automation strip before the Widevine helper (it appends --disable-component-update rather than
    # clobbering ['--enable-automation']) — mirrors the sync path.
    kwargs.setdefault("ignore_default_args", ["--enable-automation"])
    if kwargs.get("widevine"):
        from ._widevine import apply_widevine_launch
        await asyncio.to_thread(apply_widevine_launch, user_data_dir, kwargs, kwargs.get("quiet", False))
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    lease = await asyncio.to_thread(_acquire_lease_from_kwargs, kwargs)  # opt-in; None in free mode
    exe, args, pw_kwargs, humanize, show_cursor, seed = await asyncio.to_thread(_prepare, kwargs)
    if lease:  # inject CLEARCOTE_RUN_TOKEN so the PRO engine gate lets the browser launch
        inject_run_token(pw_kwargs, lease.token)
    await asyncio.to_thread(apply_font_env, exe, pw_kwargs)  # Linux: bundled font clones (mirror sync)
    if _headed_no_viewport(pw_kwargs):  # no_viewport IS a valid persistent-context option
        pw_kwargs["no_viewport"] = True
    pw = await _start_driver()
    try:
        context = await _win_av_retry_async(
            lambda e: pw.chromium.launch_persistent_context(
                user_data_dir, executable_path=e, args=args, **pw_kwargs), exe)
    except BaseException:
        if lease:
            lease.stop()
        await pw.stop()
        raise
    _bind_driver(context, pw)
    if lease:  # release the concurrency slot when the context closes
        context.on("close", lambda _c=None: lease.stop())
    await install_humanize_on_context(context, humanize, show_cursor, seed=seed)
    return context


async def launch_agent(user_data_dir=None, **kwargs):
    """Launch Clearcote ready for the in-browser AI agent; returns a Playwright **async**
    ``BrowserContext``. Set ``agent_llm_key`` (+ optional ``agent_model``), then drive a page with
    ``run_agent_task``. Uses a persistent context (the Actor framework needs a regular profile)."""
    if user_data_dir is None:
        user_data_dir = tempfile.mkdtemp(prefix="clearcote-agent-")
    return await launch_persistent_context(user_data_dir, **kwargs)


async def run_agent_task(page, goal, model=None, max_steps=None, plan_json=None):
    """Run an autonomous AI-agent task against an async ``page`` (see the sync ``run_agent_task``).
    The browser must have been launched with ``agent_llm_key``."""
    browser = page.context.browser
    if browser is None:
        raise RuntimeError("run_agent_task: page is not attached to a Browser")
    session = await browser.new_browser_cdp_session()
    tsession = await page.context.new_cdp_session(page)
    info = await tsession.send("Target.getTargetInfo")
    params = {"targetId": info["targetInfo"]["targetId"], "goal": goal}
    if max_steps is not None:
        params["maxSteps"] = max_steps
    if model is not None:
        params["model"] = model
    if plan_json is not None:
        params["planJson"] = plan_json
    try:
        res = await session.send("Browser.agentRunTask", params)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Browser.agentRunTask failed -- make sure this is a Clearcote build with the AI agent "
            "and that the browser was launched with agent_llm_key/agent_llm_url set. "
            f"Underlying error: {exc}"
        ) from exc
    try:
        steps = json.loads(res.get("stepsJson") or "[]")
    except ValueError:
        steps = []
    return {
        "success": bool(res.get("success")),
        "finalText": res.get("finalText", ""),
        "steps": steps,
        "stepsJson": res.get("stepsJson", "[]"),
    }
