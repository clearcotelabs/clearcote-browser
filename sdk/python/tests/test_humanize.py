from clearcote._humanize import CURSOR_OVERLAY, install_humanize


def _fake_browser():
    b = type("FakeBrowser", (), {})()
    b.new_page = lambda **kw: object()
    b.new_context = lambda **kw: object()
    return b


def test_install_is_noop_when_off():
    b = _fake_browser()
    np, nc = b.new_page, b.new_context
    install_humanize(b, humanize=False, show_cursor=False)
    assert b.new_page is np and b.new_context is nc


def test_install_wraps_when_humanize_on():
    b = _fake_browser()
    np = b.new_page
    install_humanize(b, humanize=True)
    assert b.new_page is not np


def test_install_wraps_when_show_cursor_on():
    b = _fake_browser()
    nc = b.new_context
    install_humanize(b, show_cursor=True)
    assert b.new_context is not nc


def test_cursor_overlay_is_idempotent_iife():
    # runs as both an add_init_script source and via page.evaluate, so it must be a self-calling
    # IIFE guarded against double-injection.
    assert "__clearcoteCursor" in CURSOR_OVERLAY
    body = CURSOR_OVERLAY.strip()
    assert body.startswith("(()") and body.endswith(")();")
