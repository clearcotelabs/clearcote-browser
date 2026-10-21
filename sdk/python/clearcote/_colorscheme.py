"""Leave ``prefers-color-scheme`` to the engine's persona.

From engine r32 the persona decides the colour scheme a Clearcote browser reports (about one
persona in three is dark; ``--fingerprint-color-scheme=light|dark`` overrides), and every surface
follows it: CSS and ``matchMedia``, the ``Sec-CH-Prefers-Color-Scheme`` request header and the
system colours of ``color-scheme: light dark`` content.

Playwright, by default, emulates ``prefers-color-scheme: light`` in every context it creates. That
emulation reaches the page but not the ``Critical-CH`` restart a site can ask for, so a dark
persona would tell the page "light" and that request "dark" (measured on r31 with
``--force-dark-mode``). Context options with ``color_scheme="null"`` turn the emulation off and the
three agree.

Only for an engine that has the switch (an older engine keeps Playwright's default exactly as
before), and never over a ``color_scheme`` the caller passed.

A browser the SDK CONNECTS to (the Clearcote Docker container, over CDP) gets the same default, and
also for the context(s) that exist already, which callers use directly
(``browser.contexts[0].new_page()``): the emulation is per page, so there it is turned off on each
page as it opens (``emulate_media(color_scheme="null")``) and on the pages open at connect time.
Measured 2026-10-21 with the fingerprint audit: through Docker every persona seed reported light while
the same engine launched natively reports dark on about one seed in three.
"""
from ._launchopts import engine_supports_switch

COLOR_SCHEME_SWITCH = "fingerprint-color-scheme"
NO_EMULATION = "null"  # Playwright Python: "null" = no colour-scheme emulation


def engine_decides_color_scheme(exe):
    """True when the engine binary that will run picks the colour scheme from the persona."""
    return engine_supports_switch(exe, COLOR_SCHEME_SWITCH)


def default_color_scheme(kw):
    """``kw`` (context options) with the colour-scheme emulation off unless the caller chose one."""
    if "color_scheme" not in kw:
        kw["color_scheme"] = NO_EMULATION
    return kw


def install_color_scheme_default(browser):
    """Default a browser's new pages/contexts to no colour-scheme emulation (sync API)."""
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    def new_page(**kw):
        return orig_new_page(**default_color_scheme(kw))

    def new_context(**kw):
        return orig_new_context(**default_color_scheme(kw))

    browser.new_page, browser.new_context = new_page, new_context


def install_color_scheme_default_async(browser):
    """Async twin of :func:`install_color_scheme_default`."""
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    async def new_page(**kw):
        return await orig_new_page(**default_color_scheme(kw))

    async def new_context(**kw):
        return await orig_new_context(**default_color_scheme(kw))

    browser.new_page, browser.new_context = new_page, new_context


def _emulation_off(page):
    """Turn the page's colour-scheme emulation off; a page closed in the meantime is left alone."""
    try:
        page.emulate_media(color_scheme=NO_EMULATION)
    except Exception:  # noqa: BLE001, S110 -- closed already: nothing left to emulate
        pass


async def _emulation_off_async(page):
    try:
        await page.emulate_media(color_scheme=NO_EMULATION)
    except Exception:  # noqa: BLE001, S110
        pass


def install_color_scheme_default_on_context(context):
    """Default an EXISTING context's pages to no colour-scheme emulation (sync API): the pages open
    now and every page its ``new_page`` opens. A context the SDK did not create has no options to
    set, so the emulation is turned off per page instead."""
    orig_new_page = context.new_page

    def new_page(**kw):
        page = orig_new_page(**kw)
        _emulation_off(page)
        return page

    context.new_page = new_page
    for page in list(getattr(context, "pages", None) or ()):
        _emulation_off(page)


async def install_color_scheme_default_on_context_async(context):
    """Async twin of :func:`install_color_scheme_default_on_context`."""
    orig_new_page = context.new_page

    async def new_page(**kw):
        page = await orig_new_page(**kw)
        await _emulation_off_async(page)
        return page

    context.new_page = new_page
    for page in list(getattr(context, "pages", None) or ()):
        await _emulation_off_async(page)


def install_color_scheme_default_connected(browser):
    """The default for a browser the SDK connected to over CDP (the Docker container): its new
    pages/contexts (:func:`install_color_scheme_default`) and the context(s) it already has."""
    install_color_scheme_default(browser)
    for ctx in list(getattr(browser, "contexts", None) or ()):
        install_color_scheme_default_on_context(ctx)


async def install_color_scheme_default_connected_async(browser):
    """Async twin of :func:`install_color_scheme_default_connected`."""
    install_color_scheme_default_async(browser)
    for ctx in list(getattr(browser, "contexts", None) or ()):
        await install_color_scheme_default_on_context_async(ctx)
