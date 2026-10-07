"""Your own fonts (font_dirs / CLEARCOTE_FONT_DIRS), fallback fonts (CLEARCOTE_FALLBACK_FONT_DIRS) and
the font lines of `clearcote info` on Linux (mirrors sdk/node/test/font-dirs.test.ts)."""
import os
import shutil
import tempfile

import pytest

from clearcote import _commands, _fonts

from _fontfiles import make_collection, make_font

ASSETS = os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets", "fonts")
REPO_TEMPLATE = os.path.join(ASSETS, "fonts.conf.template")


@pytest.fixture(autouse=True, scope="module")
def _no_font_cache_left_behind():
    """linux_font_env points fontconfig at <tmp>/cc-fc-cache, shared by every launch on the machine:
    remove it afterwards only when these tests created it."""
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    existed = os.path.exists(cache)
    yield
    if not existed:
        shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(_fonts.sys, "platform", "linux")
    for k in (_fonts.FONT_DIRS_ENV, _fonts.FALLBACK_FONT_DIRS_ENV):
        monkeypatch.delenv(k, raising=False)


def _template():
    with open(REPO_TEMPLATE, encoding="utf-8") as fh:
        return fh.read()


def _bundle(tmp_path, genuine_faces=False):
    """A release directory: fonts/ with the real template, and a stand-in engine binary that carries the
    genuine-faces marker switch only when asked to (PRO r32+ does; older engines do not)."""
    fonts = tmp_path / "bin" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "fonts.conf.template").write_text(_template(), encoding="utf-8")
    marker = b"\0" + _fonts.GENUINE_FACES_SWITCH.encode() + b"\0" if genuine_faces else b""
    (tmp_path / "bin" / "chrome").write_bytes(b"ELF\0proxy-auth\0" + marker)
    return str(tmp_path / "bin" / "chrome"), fonts


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --- reading font files ----------------------------------------------------------------------------

def test_read_font_families_and_coverage(tmp_path):
    f = make_font(tmp_path / "a.ttf", ["Segoe UI Semibold", "Segoe UI"], [0x41, 0x995, 0x1F600])
    fams, covered, color = _fonts.read_font(f, [0x41, 0x995, 0x1780, 0x1F600])
    assert fams == {"segoe ui semibold", "segoe ui"}  # name IDs 1 and 16, lower-cased
    assert covered == {0x41, 0x995, 0x1F600}  # format 4 for the BMP, format 12 beyond it
    assert color is False


def test_read_font_glyph_array_segments_and_glyph_zero(tmp_path):
    f = make_font(tmp_path / "b.ttf", ["X"], [0x41, 0x42, 0x995], via_glyph_array=(0x42, 0x995))
    assert _fonts.read_font(f, [0x41, 0x42, 0x995, 0x43, 0xFFFF])[1] == {0x41, 0x42, 0x995}


def test_read_font_colour_table_and_collections(tmp_path):
    emoji = make_font(tmp_path / "e.ttf", ["Noto Color Emoji"], [0x1F600], color=True)
    assert _fonts.read_font(emoji, [0x1F600]) == ({"noto color emoji"}, {0x1F600}, True)
    ttc = make_collection(tmp_path / "c.ttc", [(["WenQuanYi Zen Hei"], [0x4E2D]),
                                               (["WenQuanYi Zen Hei Mono"], [0xAC00])])
    fams, covered, _color = _fonts.read_font(ttc, [0x4E2D, 0xAC00])
    assert fams == {"wenquanyi zen hei", "wenquanyi zen hei mono"} and covered == {0x4E2D, 0xAC00}


@pytest.mark.parametrize("name, family, has, lacks", [
    ("Arimo-Regular.ttf", "arimo", [0x41, 0x05D0], [0x0995]),
    ("DejaVuSans.ttf", "dejavu sans", [0x0628, 0x0531, 0x0E81], [0x0995, 0x4E2D]),
    ("Inconsolata.otf", "inconsolata", [0x41], [0x0628]),
])
def test_read_font_on_the_bundled_fonts(name, family, has, lacks):
    # Real files from the release's own bundle (TrueType and CFF): name table and cmap read like any font.
    fams, covered, color = _fonts.read_font(os.path.join(ASSETS, name), has + lacks)
    assert family in fams and covered == set(has) and color is False


def test_read_font_ignores_what_is_not_a_font(tmp_path):
    (tmp_path / "junk.ttf").write_bytes(b"not a font at all")
    (tmp_path / "empty.otf").write_bytes(b"")
    (tmp_path / "short.ttc").write_bytes(b"ttcf\0\1\0\0\0\0\0\5\0\0\0\x40")  # five faces promised, none there
    for name in ("junk.ttf", "empty.otf", "short.ttc"):
        assert _fonts.read_font(str(tmp_path / name), [0x41]) == (set(), set(), False)


def test_font_files_walks_subdirectories(tmp_path):
    (tmp_path / "sub").mkdir()
    make_font(tmp_path / "sub" / "x.TTF", ["X"])
    make_font(tmp_path / "y.otf", ["Y"])
    (tmp_path / "readme.txt").write_text("no")
    assert [os.path.basename(p) for p in _fonts.font_files([str(tmp_path)])] == ["x.TTF", "y.otf"]


# --- which directories ------------------------------------------------------------------------------

def test_resolve_font_dirs_order_and_duplicates(linux, tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    for d in (a, b, c):
        d.mkdir()
    environ = {_fonts.FONT_DIRS_ENV: os.pathsep.join([str(b), str(a), str(tmp_path / "gone")]),
               _fonts.FALLBACK_FONT_DIRS_ENV: os.pathsep.join([str(c), str(a), ""])}
    user, fallback, ignored = _fonts.resolve_font_dirs([str(a)], environ)
    assert user == [str(a), str(b)]  # the option first, then the variable, no duplicates
    assert fallback == [str(c)]  # a directory that is also your own stays yours
    assert ignored == [str(tmp_path / "gone")]


def test_font_dirs_option_is_checked(linux, monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="not a directory"):
        _fonts.check_font_dirs([str(tmp_path / "typo")])
    with pytest.raises(TypeError):
        _fonts.check_font_dirs(42)
    with pytest.raises(TypeError):
        _fonts.check_font_dirs([str(tmp_path), 7])
    assert _fonts.check_font_dirs(str(tmp_path)) == [str(tmp_path)]  # one path is a list of one
    assert _fonts.check_font_dirs(tmp_path) == [str(tmp_path)]  # a Path works too
    monkeypatch.setattr(_fonts.sys, "platform", "win32")  # Windows has its own fonts: nothing to check
    assert _fonts.check_font_dirs([str(tmp_path / "typo")]) == [str(tmp_path / "typo")]


# --- the generated fontconfig file ------------------------------------------------------------------

def test_genuine_rules_without_genuine_fonts_is_the_template():
    assert _fonts.genuine_rules(_template(), set()) == _template()
    assert _fonts.genuine_rules(_template(), {"some other family"}) == _template()


def test_genuine_rules_drop_the_lookalike_and_retarget_aliases_and_generics():
    template = _template()
    out = _fonts.genuine_rules(template, {"arial", "segoe ui", "consolas"})
    for family in ("Arial", "Segoe UI", "Consolas"):
        assert f"<string>{family}</string></test>" not in out
    # Helvetica means Arial on Windows: it now points at the genuine family, not the clone.
    assert ('<test name="family"><string>Helvetica</string></test><edit name="family" mode="assign" '
            'binding="strong"><string>Arial</string></edit>') in out
    assert "<alias><family>sans-serif</family><prefer><family>Arial</family></prefer></alias>" in out
    assert "<alias><family>system-ui</family><prefer><family>Segoe UI</family></prefer></alias>" in out
    assert "<alias><family>monospace</family><prefer><family>Consolas</family></prefer></alias>" in out
    # What the genuine fonts do not provide keeps its lookalike.
    assert "<alias><family>serif</family><prefer><family>Tinos</family></prefer></alias>" in out
    assert ('<test name="family"><string>Calibri</string></test><edit name="family" mode="assign" '
            'binding="strong"><string>Carlito</string></edit>') in out
    assert ('<test name="family"><string>Times</string></test><edit name="family" mode="assign" '
            'binding="strong"><string>Tinos</string></edit>') in out
    # Only rule and generic lines change: the rendering defaults and everything else stay.
    changed = set(template.splitlines()) - set(out.splitlines())
    assert changed and all('<test name="family">' in line or "<alias>" in line for line in changed)
    assert out.count('<match target="font">') == template.count('<match target="font">')


def test_genuine_rules_follow_the_template_they_are_given():
    # r32 points Consolas at Cousine and Trebuchet MS at Selawik: the rules come from the template.
    template = _template().replace("<string>Inconsolata</string></edit>", "<string>Cousine</string></edit>")
    out = _fonts.genuine_rules(template, {"trebuchet ms"})
    assert "<string>Trebuchet MS</string></test>" not in out
    assert ('<test name="family"><string>Consolas</string></test><edit name="family" mode="assign" '
            'binding="strong"><string>Cousine</string></edit>') in out


def test_build_conf_lists_your_fonts_first_and_fallback_last():
    conf = _fonts.build_conf(_template(), "/opt/cc/fonts", "/tmp/c", ["/home/me/win & fonts"], ["/usr/local/fb"])
    dirs = [line.strip() for line in conf.splitlines() if line.strip().startswith("<dir>")]
    assert dirs == ["<dir>/home/me/win &amp; fonts</dir>", "<dir>/opt/cc/fonts</dir>", "<dir>/usr/local/fb</dir>"]
    assert "@FONTS_DIR@" not in conf and "<cachedir>/tmp/c</cachedir>" in conf
    # Without extra directories the result is the plain substitution it always was.
    assert _fonts.build_conf(_template(), "/f", "/c") == _template().replace("@FONTS_DIR@", "/f").replace("@CACHE_DIR@", "/c")


def test_linux_font_env_without_extra_dirs_is_unchanged(linux, tmp_path):
    exe, fonts = _bundle(tmp_path)
    path = _fonts.linux_font_env(exe)["FONTCONFIG_FILE"]
    assert path == os.path.join(str(fonts), "fonts.generated.conf")
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    assert _read(path) == _template().replace("@FONTS_DIR@", str(fonts)).replace("@CACHE_DIR@", cache)
    assert sorted(os.listdir(fonts)) == ["fonts.conf.template", "fonts.generated.conf"]  # no temp file left


def test_linux_font_env_with_your_windows_fonts(linux, monkeypatch, tmp_path):
    exe, fonts = _bundle(tmp_path, genuine_faces=True)
    win = tmp_path / "winfonts"
    win.mkdir()
    make_font(win / "arial.ttf", ["Arial"], [0x41])
    monkeypatch.setenv(_fonts.FONT_DIRS_ENV, str(win))
    path = _fonts.linux_font_env(exe)["FONTCONFIG_FILE"]
    assert os.path.basename(path).startswith("fonts.generated-") and os.path.dirname(path) == str(fonts)
    conf = _read(path)
    assert conf.index(f"<dir>{win}</dir>") < conf.index(f"<dir>{fonts}</dir>")
    assert "<string>Arial</string></test>" not in conf  # the real Arial renders, not Arimo
    assert "<string>Segoe UI</string></test>" in conf  # ...and what you lack keeps its lookalike
    # The option adds to the variable, ahead of it; another set of directories gets its own file.
    other = tmp_path / "other"
    other.mkdir()
    path2 = _fonts.linux_font_env(exe, [str(other)])["FONTCONFIG_FILE"]
    assert path2 != path and os.path.exists(path)
    conf2 = _read(path2)
    assert conf2.index(f"<dir>{other}</dir>") < conf2.index(f"<dir>{win}</dir>") < conf2.index(f"<dir>{fonts}</dir>")
    assert _fonts.linux_font_env(exe)["FONTCONFIG_FILE"] == path  # same directories, same file


def test_an_engine_without_genuine_faces_keeps_the_rules(linux, monkeypatch, tmp_path):
    # Measured on such an engine: "Arial" still renders through its substitute, so retargeting Helvetica
    # to the genuine Arial would give the two different widths. The directory is still listed first.
    exe, fonts = _bundle(tmp_path, genuine_faces=False)
    win = tmp_path / "winfonts"
    win.mkdir()
    make_font(win / "arial.ttf", ["Arial"], [0x41])
    conf = _read(_fonts.linux_font_env(exe, [str(win)])["FONTCONFIG_FILE"])
    assert conf.index(f"<dir>{win}</dir>") < conf.index(f"<dir>{fonts}</dir>")
    assert _fonts.genuine_rules(conf, set()) == conf  # every rule as the template has it
    assert "<string>Arial</string></test>" in conf
    assert "<alias><family>sans-serif</family><prefer><family>Arimo</family></prefer></alias>" in conf
    assert ('<test name="family"><string>Helvetica</string></test><edit name="family" mode="assign" '
            'binding="strong"><string>Arimo</string></edit>') in conf


def test_linux_font_env_with_fallback_fonts(linux, monkeypatch, tmp_path):
    exe, fonts = _bundle(tmp_path)
    fb = tmp_path / "fallback"
    fb.mkdir()
    make_font(fb / "lookalike.ttf", ["Arial"], [0x41])  # a fallback directory changes no rule
    monkeypatch.setenv(_fonts.FALLBACK_FONT_DIRS_ENV, str(fb))
    conf = _read(_fonts.linux_font_env(exe)["FONTCONFIG_FILE"])
    assert conf.index(f"<dir>{fonts}</dir>") < conf.index(f"<dir>{fb}</dir>")
    assert "<string>Arial</string></test>" in conf


def test_linux_font_env_ignores_a_missing_directory_from_the_environment(linux, monkeypatch, tmp_path):
    exe, fonts = _bundle(tmp_path)
    monkeypatch.setenv(_fonts.FONT_DIRS_ENV, str(tmp_path / "not-there"))
    assert _fonts.linux_font_env(exe)["FONTCONFIG_FILE"] == os.path.join(str(fonts), "fonts.generated.conf")


def test_apply_font_env_passes_font_dirs(linux, tmp_path):
    exe, _fonts_dir = _bundle(tmp_path)
    win = tmp_path / "w"
    win.mkdir()
    pw = {}
    _fonts.apply_font_env(exe, pw, (), [str(win)])
    assert f"<dir>{win}</dir>" in _read(pw["env"]["FONTCONFIG_FILE"])


# --- `clearcote info` -------------------------------------------------------------------------------

def _report_bundle(tmp_path):
    exe, fonts = _bundle(tmp_path)
    make_font(fonts / "Arimo-Regular.ttf", ["Arimo"], [0x41, 0x3A9, 0x416, 0x5D0])
    make_font(fonts / "DejaVuSans.ttf", ["DejaVu Sans"], [0x41, 0x628, 0x531, 0x10D0, 0xE81, 0x1F600])
    return exe, fonts


def test_font_report_lists_scripts_without_a_font(linux, tmp_path):
    exe, _fonts_dir = _report_bundle(tmp_path)
    r = _fonts.linux_font_report(exe)
    assert r["bundled"] is True and r["fontDirs"] == [] and r["fallbackFontDirs"] == []
    assert r["scripts"]["covered"] == ["Latin", "Greek", "Cyrillic", "Hebrew", "Arabic", "Armenian", "Georgian", "Lao"]
    assert "Emoji" in r["scripts"]["missing"]  # DejaVu's black-and-white smiley is not a colour emoji
    assert len(r["scripts"]["covered"]) + len(r["scripts"]["missing"]) == len(_fonts.SCRIPT_SAMPLES)
    assert "genuineWindowsFamilies" not in r and "ignoredFontDirs" not in r


def test_font_report_with_your_fonts_and_fallback(linux, tmp_path):
    exe, _fonts_dir = _report_bundle(tmp_path)
    win, fb = tmp_path / "win", tmp_path / "fb"
    win.mkdir()
    fb.mkdir()
    make_font(win / "arial.ttf", ["Arial"], [0x41])
    make_collection(win / "fonts.ttc", [(["Segoe UI"], [0x41]), (["Nirmala UI"], [0x995, 0xB95])])
    make_font(fb / "emoji.ttf", ["Noto Color Emoji"], [0x1F600], color=True)
    environ = {_fonts.FALLBACK_FONT_DIRS_ENV: os.pathsep.join([str(fb), str(tmp_path / "nope")])}
    r = _fonts.linux_font_report(exe, [str(win)], environ)
    assert r["fontDirs"] == [str(win)] and r["fallbackFontDirs"] == [str(fb)]
    assert r["ignoredFontDirs"] == [str(tmp_path / "nope")]
    assert {"Bengali", "Tamil", "Emoji"} <= set(r["scripts"]["covered"])
    assert r["genuineWindowsFamilies"] == ["Arial", "Segoe UI"]
    assert "Calibri" in r["lookalikeWindowsFamilies"] and "Arial" not in r["lookalikeWindowsFamilies"]
    assert r["genuineFacesSupported"] is False  # the stand-in engine has no marker
    exe2, _f = _bundle(tmp_path / "r32", genuine_faces=True)
    assert _fonts.linux_font_report(exe2, [str(win)], environ)["genuineFacesSupported"] is True


def test_font_report_without_a_bundle(tmp_path):
    assert _fonts.linux_font_report(str(tmp_path / "chrome")) is None


def test_info_font_lines():
    lines = _commands.font_lines({
        "bundled": True, "note": "metric-compatible Windows font clones are bundled with this build",
        "scripts": {"covered": ["Latin", "Greek"], "missing": ["Khmer", "Ethiopic"]},
        "fontDirs": ["/srv/winfonts"], "fallbackFontDirs": ["/usr/local/share/clearcote/fonts"],
        "ignoredFontDirs": ["/nope"],
        "genuineWindowsFamilies": ["Arial", "Segoe UI"], "lookalikeWindowsFamilies": ["Calibri"],
        "genuineFacesSupported": False})
    assert lines == [
        "Fonts           metric-compatible Windows font clones are bundled with this build",
        "Scripts         2 of 4 render; no font for Khmer, Ethiopic (they draw as empty boxes)",
        "Your fonts      /srv/winfonts",
        "                real: Arial, Segoe UI",
        "                lookalike: Calibri",
        "                this engine still draws its lookalikes for them (real faces need PRO r32 or newer)",
        "Fallback fonts  /usr/local/share/clearcote/fonts",
        "Ignored         /nope (not a directory)",
    ]
    assert _commands.font_lines({"note": "n", "scripts": {"covered": ["Latin"], "missing": []}, "fontDirs": []}) == [
        "Fonts           n", "Scripts         all 1 render"]
    assert _commands.font_lines({"bundled": False, "note": "no bundle"}) == ["Fonts           no bundle"]


def test_info_reports_fonts_on_linux(linux, monkeypatch, tmp_path):
    from test_parity_cli import fake_cached_build  # the same fake cache the other info tests use
    cache = tmp_path / "cache"
    cache.mkdir()
    for k in ("CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_AUTO_UPDATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLEARCOTE_CACHE", str(cache))
    exe = fake_cached_build(cache, "custom")
    fonts = os.path.join(os.path.dirname(exe), "fonts")
    os.mkdir(fonts)
    with open(os.path.join(fonts, "fonts.conf.template"), "w", encoding="utf-8") as fh:
        fh.write(_template())
    make_font(tmp_path / "cache" / "custom" / "browser" / "sub" / "fonts" / "a.ttf", ["Arimo"], [0x41])
    monkeypatch.setenv("CLEARCOTE_BINARY", exe)
    monkeypatch.setattr(_commands.sys, "platform", "linux")
    r = _commands.build_info(quick=True)
    assert r["fonts"]["bundled"] is True and r["fonts"]["scripts"]["covered"] == ["Latin"]


# --- launch()/launch_persistent_context()/serve() pass font_dirs to the font wiring ----------------

class _Stop(Exception):
    pass


@pytest.fixture
def stubbed_launch(linux, monkeypatch, tmp_path):
    """launch() up to the font wiring: _prepare records what reaches Playwright, apply_font_env records
    the directories, and the browser start itself stops the test."""
    import clearcote
    seen = {}
    monkeypatch.setattr(clearcote, "_acquire_lease_from_kwargs", lambda kwargs: seen.setdefault("lease", None))

    def prepare(kwargs):
        seen["prepare_kwargs"] = dict(kwargs)
        return "exe", [], {}, None, False, None

    monkeypatch.setattr(clearcote, "_prepare", prepare)
    monkeypatch.setattr(clearcote, "apply_font_env",
                        lambda exe, kw, args=(), font_dirs=None: seen.__setitem__("font_dirs", font_dirs))
    monkeypatch.setattr(clearcote, "apply_shader_dialect", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "_headless_geometry_kwargs", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "_win_av_retry", lambda *a, **k: (_ for _ in ()).throw(_Stop()))
    return clearcote, seen


def test_launch_hands_font_dirs_to_the_font_wiring(stubbed_launch, tmp_path):
    clearcote, seen = stubbed_launch
    with pytest.raises(_Stop):
        clearcote.launch(ephemeral_profile=False, headless=True, font_dirs=str(tmp_path))
    assert seen["font_dirs"] == [str(tmp_path)]
    assert "font_dirs" not in seen["prepare_kwargs"]  # not a Playwright option


def test_launch_persistent_context_hands_font_dirs_to_the_font_wiring(stubbed_launch, tmp_path):
    clearcote, seen = stubbed_launch
    with pytest.raises(_Stop):
        clearcote.launch_persistent_context(str(tmp_path / "profile"), headless=True, font_dirs=[str(tmp_path)])
    assert seen["font_dirs"] == [str(tmp_path)] and "font_dirs" not in seen["prepare_kwargs"]


def test_a_mistyped_font_dir_fails_before_the_lease(stubbed_launch, monkeypatch, tmp_path):
    clearcote, seen = stubbed_launch
    monkeypatch.setattr(clearcote, "_acquire_lease_from_kwargs", lambda kwargs: pytest.fail("lease taken"))
    with pytest.raises(ValueError, match="not a directory"):
        clearcote.launch(ephemeral_profile=False, headless=True, font_dirs=str(tmp_path / "typo"))
    with pytest.raises(ValueError, match="not a directory"):
        clearcote.launch_persistent_context(str(tmp_path / "p"), headless=True, font_dirs=str(tmp_path / "typo"))


async def test_async_launch_hands_font_dirs_to_the_font_wiring(linux, monkeypatch, tmp_path):
    from clearcote import async_api
    seen = {}
    monkeypatch.setattr(async_api, "_acquire_lease_from_kwargs", lambda kwargs: None)

    def prepare(kwargs, lease):
        seen["prepare_kwargs"] = dict(kwargs)
        return "exe", [], {}, None, False, None

    monkeypatch.setattr(async_api, "_prepare_releasing", prepare)

    def fonts(exe, kw, args=(), font_dirs=None):
        seen["font_dirs"] = font_dirs
        raise _Stop()

    monkeypatch.setattr(async_api, "apply_font_env", fonts)
    with pytest.raises(_Stop):
        await async_api.launch(headless=True, font_dirs=str(tmp_path))
    assert seen["font_dirs"] == [str(tmp_path)] and "font_dirs" not in seen["prepare_kwargs"]


def test_cloud_treats_font_dirs_as_a_local_option():
    from clearcote import cloud
    assert "font_dirs" in cloud.LOCAL_ONLY_OPTIONS
