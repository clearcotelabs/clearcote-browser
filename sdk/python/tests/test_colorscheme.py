"""r32: leave prefers-color-scheme to the engine's persona (Playwright emulates light by default).

The default is gated on the engine binary having the --fingerprint-color-scheme switch, so an
older engine launches exactly as before, and a caller's own color_scheme always wins.
"""
import asyncio

import pytest

import clearcote
from clearcote import _colorscheme
from clearcote._colorscheme import (default_color_scheme, engine_decides_color_scheme,
                                    install_color_scheme_default,
                                    install_color_scheme_default_async)


@pytest.fixture(autouse=True)
def _no_licence(monkeypatch, tmp_path):
    """A clean home: no saved licence key or profiles of this machine reach these launches."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for k in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD"):
        monkeypatch.delenv(k, raising=False)


def _engine(tmp_path, r32):
    exe = tmp_path / ("chrome-r32" if r32 else "chrome-r31")
    body = b"\x00fingerprint-platform\x00"
    if r32:
        body += b"\x00fingerprint-color-scheme\x00"
    exe.write_bytes(body)
    return str(exe)


class _Capture:
    """Records what each launch path hands to Playwright (headed, so no window-fit CDP is needed)."""

    def __init__(self):
        self.launch_kwargs = None
        self.context_kwargs = None

    def install(self, monkeypatch):
        cap = self

        class _Page:
            def __init__(self, kw):
                self.kw = kw

        class _Ctx:
            pages = []

            def on(self, *a, **k):
                pass

            def new_page(self, **kw):
                return _Page(kw)

            def new_context(self, **kw):
                return _Ctx()

            def close(self, **kw):
                pass

        class _Browser(_Ctx):
            pass

        class _Chromium:
            def launch(self, **kw):
                cap.launch_kwargs = kw
                return _Browser()

            def launch_persistent_context(self, user_data_dir, **kw):
                cap.context_kwargs = kw
                return _Ctx()

        class _PW:
            chromium = _Chromium()

        monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
        monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
        monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
        return self


def test_default_turns_emulation_off_only_when_the_caller_chose_nothing():
    assert default_color_scheme({}) == {"color_scheme": "null"}
    assert default_color_scheme({"color_scheme": "dark"}) == {"color_scheme": "dark"}
    # an explicit None is a choice too (Playwright's own default)
    assert default_color_scheme({"color_scheme": None}) == {"color_scheme": None}
    kw = {"no_viewport": True}
    assert default_color_scheme(kw) is kw and kw == {"no_viewport": True, "color_scheme": "null"}


def test_engine_probe_reads_the_switch_literal(tmp_path):
    assert engine_decides_color_scheme(_engine(tmp_path, r32=True)) is True
    assert engine_decides_color_scheme(_engine(tmp_path, r32=False)) is False
    assert engine_decides_color_scheme(None) is False
    assert _colorscheme.COLOR_SCHEME_SWITCH == "fingerprint-color-scheme"


def test_browser_wrapper_defaults_new_pages_and_contexts():
    seen = []

    class _B:
        def new_page(self, **kw):
            seen.append(("page", kw))

        def new_context(self, **kw):
            seen.append(("context", kw))

    b = _B()
    install_color_scheme_default(b)
    b.new_page()
    b.new_context(color_scheme="light")
    b.new_context(viewport={"width": 1, "height": 1})
    assert seen == [("page", {"color_scheme": "null"}),
                    ("context", {"color_scheme": "light"}),
                    ("context", {"viewport": {"width": 1, "height": 1}, "color_scheme": "null"})]


def test_async_browser_wrapper_defaults_new_pages_and_contexts():
    seen = []

    class _B:
        async def new_page(self, **kw):
            seen.append(("page", kw))

        async def new_context(self, **kw):
            seen.append(("context", kw))

    b = _B()
    install_color_scheme_default_async(b)

    async def go():
        await b.new_page()
        await b.new_context(color_scheme="dark")

    asyncio.run(go())
    assert seen == [("page", {"color_scheme": "null"}), ("context", {"color_scheme": "dark"})]


def test_persistent_context_on_an_r32_engine_turns_the_emulation_off(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    clearcote.launch_persistent_context(
        str(tmp_path / "prof"), executable_path=_engine(tmp_path, r32=True), headless=False, quiet=True)
    assert cap.context_kwargs["color_scheme"] == "null"


def test_persistent_context_on_an_older_engine_is_unchanged(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    clearcote.launch_persistent_context(
        str(tmp_path / "prof"), executable_path=_engine(tmp_path, r32=False), headless=False, quiet=True)
    assert "color_scheme" not in cap.context_kwargs


def test_persistent_context_keeps_the_callers_color_scheme(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    clearcote.launch_persistent_context(
        str(tmp_path / "prof"), executable_path=_engine(tmp_path, r32=True), headless=False,
        color_scheme="dark", quiet=True)
    assert cap.context_kwargs["color_scheme"] == "dark"


def test_default_launch_goes_through_the_throwaway_profile_with_the_default(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    clearcote.launch(executable_path=_engine(tmp_path, r32=True), headless=False, quiet=True)
    assert cap.context_kwargs["color_scheme"] == "null"


def test_incognito_launch_defaults_new_pages_on_an_r32_engine(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    browser = clearcote.launch(executable_path=_engine(tmp_path, r32=True), ephemeral_profile=False,
                               headless=False, quiet=True)
    assert "color_scheme" not in cap.launch_kwargs, "color_scheme is not a chromium.launch option"
    assert browser.new_page().kw == {"no_viewport": True, "color_scheme": "null"}
    assert browser.new_context(color_scheme="light") is not None


def test_incognito_launch_on_an_older_engine_is_unchanged(monkeypatch, tmp_path):
    _Capture().install(monkeypatch)
    browser = clearcote.launch(executable_path=_engine(tmp_path, r32=False), ephemeral_profile=False,
                               headless=False, quiet=True)
    assert browser.new_page().kw == {"no_viewport": True}
