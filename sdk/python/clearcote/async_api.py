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

``launch(cloud=True)`` (or ``CLEARCOTE_CLOUD=1``) returns the same async ``Browser`` connected to a
hosted session instead; :class:`AsyncCloud` is the asyncio client for the rest of the hosted API.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

from . import (  # shared sync helpers
    _headed_no_viewport, _headless_geometry_kwargs, _prepare, _acquire_lease_from_kwargs,
    _is_win_launch_race, _is_stale_token_refusal, _with_geometry_args, _profile_dir_remover,
    _browser_process_files, _drop_cloud_credentials, _release_launch_token,
)
from .cloud import AsyncCloud, CloudError, CloudTimeoutError, cloud_requested, launch_cloud_async, verify_webhook
from ._launchopts import DEFAULT_IGNORED_ARGS
from ._geometry import apply_headless_geometry, fit_window_to_work_area_async
from ._license import inject_run_token
from ._fonts import apply_font_env
from ._shaderdialect import apply_shader_dialect
from ._humanize_async import install_humanize, install_humanize_on_context
from ._profile import Profile, list_profiles, load_profile
from ._render_async import check_render_coherence
from .download import ensure_binary, warm_files
from .geoip import GeoipError, resolve_geo, resolve_geo_detailed  # noqa: F401  (re-exported)
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
    "AsyncCloud",
    "CloudError",
    "CloudTimeoutError",
    "verify_webhook",
    "RELEASE",
    "__version__",
]


def _prepare_releasing(kwargs, lease):
    """_prepare (looked up at call time, so tests can patch it), releasing the lease handle if it
    raises (e.g. GeoipError before any browser starts)."""
    try:
        return _prepare(kwargs)
    except BaseException:
        if lease:
            try:
                lease.stop()
            except Exception:  # noqa: BLE001
                pass
        raise


async def executable_path(executable_path=None, cache_dir=None, quiet=False, auto_update=None,
                          version=None, license_key=None, license_api_base=None,
                          release_channel=None):
    """Resolve the Clearcote chrome.exe path (download/verify if needed). Runs the blocking
    resolve in a thread so it never stalls the event loop."""
    from . import executable_path as _sync_executable_path
    return await asyncio.to_thread(
        _sync_executable_path, executable_path, cache_dir, quiet, auto_update, version,
        license_key, license_api_base, release_channel)


async def download(cache_dir=None, quiet=False, auto_update=None, version=None, license_key=None,
                   license_api_base=None, release_channel=None):
    """Pre-fetch + verify the Clearcote binary without launching (off-loop). Returns the path."""
    if version is None and license_key is None and license_api_base is None and release_channel is None:
        return await asyncio.to_thread(
            ensure_binary, cache_dir=cache_dir, quiet=quiet, auto_update=auto_update)
    from . import download as _sync_download
    return await asyncio.to_thread(
        _sync_download, cache_dir, quiet, auto_update, version, license_key, license_api_base,
        release_channel)


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


async def _install_window_fixup(container, args):
    """Async mirror of the sync ``_install_window_fixup``: fit the window to the display's work
    area, once, on the first page."""
    done = []

    async def fit(page):
        if done:
            return page
        done.append(True)
        await fit_window_to_work_area_async(page, args)
        return page

    pages = getattr(container, "pages", None)
    if pages:
        await fit(pages[0])
        return
    orig_new_page = container.new_page

    async def new_page(**kw):
        return await fit(await orig_new_page(**kw))

    container.new_page = new_page


def _install_headless_geometry(browser, args=None):
    """Default a headless browser's new pages/contexts to ``no_viewport`` plus a window fit per new
    window (async mirror of the sync installer). Any per-call geometry option keeps the caller in
    control."""
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    def _merge(kw):
        if not any(k in kw for k in ("viewport", "no_viewport", "screen")):
            kw["no_viewport"] = True
        return kw

    async def new_page(**kw):
        page = await orig_new_page(**_merge(kw))
        await fit_window_to_work_area_async(page, args)
        return page

    async def new_context(**kw):
        context = await orig_new_context(**_merge(kw))
        await _install_window_fixup(context, args)
        return context

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


async def _retry_on_stale_run_token_async(lease, pw_kwargs, launch_token, start):
    """Async twin of the sync ``_retry_on_stale_run_token``: if the engine refuses the run-token as older
    than one it has accepted on this machine, mint a fresh one (off the event loop) and launch once more."""
    try:
        return await start()
    except Exception as exc:  # noqa: BLE001
        refresh = getattr(lease, "refresh_token", None)
        if refresh is None or not _is_stale_token_refusal(exc) or not await asyncio.to_thread(refresh):
            raise
        inject_run_token(pw_kwargs, lease.token, launch_token[0] if launch_token else None)
        return await start()


async def launch(cloud=None, **kwargs):
    """Launch Clearcote and return a Playwright **async** ``Browser``. Same kwargs as the sync
    ``clearcote.launch`` (fingerprint, platform, brand, gpu_*, timezone, accept_language, proxy,
    geoip, profile, canvas_bridge, humanize, ... + any Playwright launch option).

    ``cloud=True`` (or ``CLEARCOTE_CLOUD=1`` with ``cloud`` unset) connects to a hosted session
    instead, exactly like the sync ``launch(cloud=True)``."""
    if cloud_requested(cloud):
        return await launch_cloud_async(cloud, kwargs)
    _drop_cloud_credentials(kwargs)
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    shader_dialect = kwargs.pop("shader_dialect", None)  # popped before _prepare: not a PW option
    lease = await asyncio.to_thread(_acquire_lease_from_kwargs, kwargs)  # opt-in; None in free mode
    exe, args, pw_kwargs, humanize, show_cursor, seed = await asyncio.to_thread(
        _prepare_releasing, kwargs, lease)
    launch_token = lease.bind_launch() if lease else None  # (file, release) or None; r23+ opt-in
    if lease:  # inject CLEARCOTE_RUN_TOKEN (+ the r23+ opt-in token FILE) so the gate lets it launch
        inject_run_token(pw_kwargs, lease.token, launch_token[0])
    await asyncio.to_thread(apply_font_env, exe, pw_kwargs, args)  # Linux: bundled font clones (mirror sync)
    apply_shader_dialect(shader_dialect, pw_kwargs, args)  # after fonts: that helper rebuilds the env
    headed = _headed_no_viewport(pw_kwargs)  # launch() takes no viewport kwarg -> wrap new_page/context
    # Headless: the display switches go on the command line, no_viewport rides on
    # new_page/new_context (see _geometry).
    geom = None if headed else _headless_geometry_kwargs(pw_kwargs, seed, args)
    launch_args = _with_geometry_args(args, geom)
    pw = await _start_driver()
    try:
        browser = await _retry_on_stale_run_token_async(lease, pw_kwargs, launch_token, lambda: _win_av_retry_async(
            lambda e: pw.chromium.launch(executable_path=e, args=launch_args, **pw_kwargs), exe))
    except BaseException:
        if lease:
            lease.stop()
        _release_launch_token(launch_token)  # a shared paid lease keeps it otherwise (see the sync helper)
        await pw.stop()
        raise
    _bind_driver(browser, pw)
    if lease:  # release the concurrency slot + remove the run-token file when the browser closes
        def _on_disconnect(_b=None, _lease=lease, _lt=launch_token):
            _lease.stop()
            _lt[1]()
        browser.on("disconnected", _on_disconnect)
    if headed:
        _install_headed_viewport(browser)
    elif geom:
        _install_headless_geometry(browser, args)
    await install_humanize(browser, humanize, show_cursor, seed=seed)
    return browser


async def launch_persistent_context(user_data_dir=None, cloud=None, **kwargs):
    """Launch Clearcote with a persistent profile dir; returns a Playwright **async**
    ``BrowserContext`` (cookies/storage persist in ``user_data_dir``).

    Pass ``widevine=True`` to seed + enable the (opt-in) Widevine CDM so DRM/EME works.

    ``cloud=True`` with ``profile="name"``: the context of a hosted session on that cloud profile
    (see the sync ``launch_persistent_context``)."""
    if cloud_requested(cloud):
        return await launch_cloud_async(cloud, kwargs, persistent=True, user_data_dir=user_data_dir)
    if user_data_dir is None:
        raise TypeError("launch_persistent_context() needs a user_data_dir "
                        "(or cloud=True with profile=\"name\" for a cloud profile)")
    _drop_cloud_credentials(kwargs)
    # Automation strip before the Widevine helper (it appends --disable-component-update rather than
    # clobbering ['--enable-automation']) — mirrors the sync path.
    kwargs.setdefault("ignore_default_args", list(DEFAULT_IGNORED_ARGS))
    if kwargs.get("widevine"):
        from ._widevine import apply_widevine_launch
        await asyncio.to_thread(apply_widevine_launch, user_data_dir, kwargs, kwargs.get("quiet", False))
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    shader_dialect = kwargs.pop("shader_dialect", None)  # popped before _prepare: not a PW option
    lease = await asyncio.to_thread(_acquire_lease_from_kwargs, kwargs)  # opt-in; None in free mode
    exe, args, pw_kwargs, humanize, show_cursor, seed = await asyncio.to_thread(
        _prepare_releasing, kwargs, lease)
    launch_token = lease.bind_launch() if lease else None  # (file, release) or None; r23+ opt-in
    if lease:  # inject CLEARCOTE_RUN_TOKEN (+ the r23+ opt-in token FILE) so the gate lets it launch
        inject_run_token(pw_kwargs, lease.token, launch_token[0])
    await asyncio.to_thread(apply_font_env, exe, pw_kwargs, args)  # Linux: bundled font clones (mirror sync)
    apply_shader_dialect(shader_dialect, pw_kwargs, args)  # after fonts: that helper rebuilds the env
    geom = None
    if _headed_no_viewport(pw_kwargs):  # no_viewport IS a valid persistent-context option
        pw_kwargs["no_viewport"] = True
    else:  # headless: persona owns screen -> fit the window; no persona -> set the display too
        geom = apply_headless_geometry(pw_kwargs, seed, args)
    launch_args = _with_geometry_args(args, geom)
    pw = await _start_driver()
    try:
        context = await _retry_on_stale_run_token_async(lease, pw_kwargs, launch_token, lambda: _win_av_retry_async(
            lambda e: pw.chromium.launch_persistent_context(
                user_data_dir, executable_path=e, args=launch_args, **pw_kwargs), exe))
    except BaseException:
        if lease:
            lease.stop()
        _release_launch_token(launch_token)  # a shared paid lease keeps it otherwise (see the sync helper)
        await pw.stop()
        raise
    _bind_driver(context, pw)
    if lease:  # release the concurrency slot + remove the run-token file when the context closes
        def _on_close(_c=None, _lease=lease, _lt=launch_token):
            _lease.stop()
            _lt[1]()
        context.on("close", _on_close)
    if geom:
        await _install_window_fixup(context, args)
    await install_humanize_on_context(context, humanize, show_cursor, seed=seed)
    return context


async def launch_agent(user_data_dir=None, **kwargs):
    """Launch Clearcote ready for the in-browser AI agent; returns a Playwright **async**
    ``BrowserContext``. Set ``agent_llm_key`` (+ optional ``agent_model``), then drive a page with
    ``run_agent_task``. Uses a persistent context (the Actor framework needs a regular profile): a
    fresh temp ``user_data_dir``, deleted when the context closes, unless you pass one to keep."""
    if user_data_dir is not None:
        # the agent drives the LOCAL engine's Actor framework: never a cloud browser
        return await launch_persistent_context(user_data_dir, cloud=False, **kwargs)
    # The sync path's throwaway-profile handling, async: removed once the browser has exited --
    # after close(), or when it went away by itself -- and at interpreter exit; and at once when the
    # launch fails (it used to leak one directory per failed launch). Never on the bare "close"
    # event: that fires while the browser is still writing the profile (see the sync
    # _install_ephemeral_profile_cleanup).
    udd = tempfile.mkdtemp(prefix="clearcote-agent-")
    try:
        context = await launch_persistent_context(udd, cloud=False, **kwargs)
    except BaseException:
        await asyncio.to_thread(_profile_dir_remover(udd, *_browser_process_files(udd)))
        raise
    remove = _profile_dir_remover(udd, *_browser_process_files(udd))
    closing = False
    orig_close = context.close

    async def close(*args, **kw):
        nonlocal closing
        closing = True
        try:
            return await orig_close(*args, **kw)
        finally:
            await asyncio.to_thread(remove)  # waits and retries with sleeps: off the event loop

    async def _on_close(*_a):
        if not closing:
            await asyncio.to_thread(remove)

    context.close = close
    context.on("close", _on_close)
    return context


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
