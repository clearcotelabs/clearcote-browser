import inspect

from clearcote import async_api
from clearcote._humanize_async import attach_humanize, install_humanize


def test_async_surface_is_coroutines():
    for name in ("launch", "launch_persistent_context", "launch_agent",
                 "run_agent_task", "executable_path", "download"):
        fn = getattr(async_api, name)
        assert inspect.iscoroutinefunction(fn), f"{name} should be async"


def test_async_api_reexports():
    # Profile + helpers are exposed for parity with the sync package.
    for name in ("Profile", "list_profiles", "load_profile", "resolve_geo", "RELEASE", "__version__"):
        assert hasattr(async_api, name)


# ---- async humanize, exercised against a fake page (no real browser) ----
class _FakeMouse:
    def __init__(self):
        self.moves, self.clicks, self.wheels = [], [], []

    async def move(self, x, y, **kw):
        self.moves.append((x, y))

    async def click(self, x, y, **kw):
        self.clicks.append((x, y))

    async def wheel(self, dx, dy):
        self.wheels.append((dx, dy))


class _FakeKeyboard:
    def __init__(self):
        self.typed, self.presses = [], []

    async def type(self, ch, **k):
        self.typed.append(ch)

    async def press(self, combo, **k):
        self.presses.append(combo)


class _FakeLocator:
    def __init__(self):
        self.clicked = 0

    @property
    def first(self):
        return self

    async def wait_for(self, **k):
        pass

    async def scroll_into_view_if_needed(self, **k):
        pass

    async def click(self, **k):
        self.clicked += 1

    async def bounding_box(self):
        return {"x": 10, "y": 10, "width": 100, "height": 20}

    async def is_enabled(self):
        return True

    async def element_handle(self):
        return object()


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.loc = _FakeLocator()
        self.evals = []
        self.fill_fallback, self.type_fallback = [], []
        self.main_frame = object()

    async def evaluate(self, *a, **k):
        self.evals.append(a)

    async def click(self, *a, **k):
        pass

    async def hover(self, *a, **k):
        pass

    def locator(self, sel):
        return self.loc

    async def fill(self, sel, val, **k):
        self.fill_fallback.append((sel, val))   # native bulk fill (the paste-signature fallback)

    async def type(self, sel, txt, **k):
        self.type_fallback.append((sel, txt))

    def on(self, *a, **k):
        pass


async def test_humanize_glide_is_continuous_and_lands_on_target():
    page = _FakePage()
    await attach_humanize(None, page, humanize=True)
    await page.mouse.move(200, 300)   # wrapped: glides via many native moves
    moves = page.mouse.moves
    assert len(moves) > 3, "glide should emit several intermediate moves"
    assert moves[-1] == (200, 300), "must land exactly on target"
    # a second move continues from the last position (no snap back to a corner)
    await page.mouse.move(50, 50)
    assert page.mouse.moves[-1] == (50, 50)


async def test_humanize_click_presses_after_gliding():
    page = _FakePage()
    await attach_humanize(None, page, humanize=True)
    await page.mouse.click(120, 140)
    assert page.mouse.clicks == [(120, 140)]
    assert page.mouse.moves and page.mouse.moves[-1] == (120, 140)


async def test_humanize_typing_is_per_character_not_paste():
    page = _FakePage()
    await attach_humanize(None, page, humanize=True)
    await page.fill("#email", "abc")
    # per-character key events — now via keyboard.press so each key has a keydown->keyup DWELL
    # (keyboard.type has no hold). Intent unchanged: one key per char, not a bulk paste.
    assert [p for p in page.keyboard.presses if p in ("a", "b", "c")] == ["a", "b", "c"]
    assert page.fill_fallback == []                       # native bulk fill (paste) NOT used
    assert "ControlOrMeta+a" in page.keyboard.presses     # field cleared first
    assert page.loc.clicked == 1                           # field focused via a click


async def test_show_cursor_injects_overlay():
    page = _FakePage()
    await attach_humanize(None, page, humanize=False, show_cursor=True)
    assert page.evals, "show_cursor should inject the overlay script"


async def test_install_humanize_wraps_new_page():
    made = _FakePage()

    class _FakeBrowser:
        async def new_page(self, **kw):
            return made

        async def new_context(self, **kw):
            return None

    browser = _FakeBrowser()
    await install_humanize(browser, humanize=True)
    page = await browser.new_page()       # wrapped -> attaches humanize
    await page.mouse.move(300, 200)
    assert page.mouse.moves[-1] == (300, 200)


# ---- regression: async launch must unpack _prepare's 6-tuple (PR #9) ----
async def test_async_launch_unpacks_prepare_sixtuple_and_threads_seed(monkeypatch):
    """_prepare returns 6 values (…, effective seed). The async launch paths must
    unpack 6 (not 5) and pass the profile-aware seed to install_humanize. Before the
    fix these unpacked 5 -> `ValueError: too many values to unpack (expected 5, got 6)`
    the moment launch()/launch_persistent_context() was actually awaited."""
    import tempfile
    captured = {}

    class _FakeBrowserObj:
        pass

    def fake_prepare(kwargs):
        # exe, args, pw_kwargs, humanize, show_cursor, seed
        return ("chrome", ["--x"], {}, True, False, "eff-seed-123")

    class _FakeChromium:
        async def launch(self, **kw):
            return _FakeBrowserObj()

        async def launch_persistent_context(self, user_data_dir, **kw):
            return _FakeBrowserObj()

    class _FakePW:
        chromium = _FakeChromium()

        async def stop(self):
            pass

    async def fake_start_driver():
        return _FakePW()

    async def fake_install(browser, humanize, show_cursor, seed=None):
        captured["launch_seed"] = seed
        captured["humanize"] = humanize

    async def fake_install_ctx(context, humanize, show_cursor, seed=None):
        captured["ctx_seed"] = seed

    monkeypatch.setattr(async_api, "_prepare", fake_prepare)
    monkeypatch.setattr(async_api, "_start_driver", fake_start_driver)
    monkeypatch.setattr(async_api, "_headed_no_viewport", lambda pw_kwargs: False)
    monkeypatch.setattr(async_api, "_bind_driver", lambda *a, **k: None)
    monkeypatch.setattr(async_api, "install_humanize", fake_install)
    monkeypatch.setattr(async_api, "install_humanize_on_context", fake_install_ctx)

    # launch(): must not raise the unpack ValueError; uses the _prepare (effective) seed.
    browser = await async_api.launch(fingerprint="raw-ignored")
    assert browser is not None
    assert captured["launch_seed"] == "eff-seed-123"
    assert captured["humanize"] is True

    # launch_persistent_context(): same 6-unpack + seed threading.
    context = await async_api.launch_persistent_context(tempfile.mkdtemp())
    assert context is not None
    assert captured["ctx_seed"] == "eff-seed-123"
