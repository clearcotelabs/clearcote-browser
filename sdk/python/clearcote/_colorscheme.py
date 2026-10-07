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
