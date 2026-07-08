import os

from clearcote import _fonts


def _make_bundle(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    (fonts / "fonts.conf.template").write_text(
        "<fontconfig><dir>@FONTS_DIR@</dir><cachedir>@CACHE_DIR@</cachedir></fontconfig>"
    )
    return str(tmp_path / "chrome"), str(fonts)


def test_linux_font_env_non_linux(monkeypatch, tmp_path):
    # Fonts are a Linux-only concern; other platforms get nothing.
    monkeypatch.setattr(_fonts.sys, "platform", "win32")
    exe, _ = _make_bundle(tmp_path)
    assert _fonts.linux_font_env(exe) == {}


def test_linux_font_env_generates_conf(monkeypatch, tmp_path):
    monkeypatch.setattr(_fonts.sys, "platform", "linux")
    exe, fonts = _make_bundle(tmp_path)
    env = _fonts.linux_font_env(exe)
    conf_path = env["FONTCONFIG_FILE"]
    assert conf_path == os.path.join(fonts, "fonts.generated.conf")
    conf = open(conf_path, encoding="utf-8").read()
    assert "@FONTS_DIR@" not in conf and "@CACHE_DIR@" not in conf
    assert fonts in conf  # real fonts dir wired in


def test_linux_font_env_no_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(_fonts.sys, "platform", "linux")
    assert _fonts.linux_font_env(str(tmp_path / "chrome")) == {}


def test_apply_font_env_merges(monkeypatch, tmp_path):
    monkeypatch.setattr(_fonts.sys, "platform", "linux")
    exe, _ = _make_bundle(tmp_path)
    pw = {"env": {"MYVAR": "1"}}
    _fonts.apply_font_env(exe, pw)
    assert pw["env"]["MYVAR"] == "1"  # caller-supplied kept
    assert "FONTCONFIG_FILE" in pw["env"]
    assert len(pw["env"]) > 2  # os.environ merged in


def test_apply_font_env_noop_when_nothing_to_add(monkeypatch, tmp_path):
    monkeypatch.setattr(_fonts.sys, "platform", "win32")
    pw = {}
    _fonts.apply_font_env(str(tmp_path / "chrome"), pw)
    assert "env" not in pw  # untouched -> Playwright uses its default env
