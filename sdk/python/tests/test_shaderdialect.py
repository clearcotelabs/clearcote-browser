import pytest

from clearcote import _shaderdialect
from clearcote._shaderdialect import ENV_VAR, apply_shader_dialect


def test_absent_by_default_leaves_env_untouched():
    # The whole point is that this is off unless asked for: an untouched pw_kwargs means Playwright
    # uses the default child env, exactly as before the option existed.
    kw = {}
    apply_shader_dialect(None, kw)
    assert kw == {}


def test_empty_string_is_also_off():
    kw = {}
    apply_shader_dialect("", kw)
    assert kw == {}


def test_hlsl_sets_the_variable():
    kw = {}
    apply_shader_dialect("hlsl", kw)
    assert kw["env"][ENV_VAR] == "hlsl"


def test_value_is_normalised():
    kw = {}
    apply_shader_dialect("  HLSL  ", kw)
    assert kw["env"][ENV_VAR] == "hlsl"


def test_unknown_dialect_is_rejected():
    # Rejected rather than silently ignored: a typo would otherwise look like it worked while the
    # engine kept reporting the honest dialect.
    with pytest.raises(ValueError):
        apply_shader_dialect("glsl", {})


def test_existing_env_is_preserved(monkeypatch):
    # Must not clobber what apply_font_env (or the caller) already put there.
    kw = {"env": {"FONTCONFIG_FILE": "/tmp/fonts.conf"}}
    apply_shader_dialect("hlsl", kw)
    assert kw["env"]["FONTCONFIG_FILE"] == "/tmp/fonts.conf"
    assert kw["env"][ENV_VAR] == "hlsl"


def test_process_env_is_the_base(monkeypatch):
    # Playwright REPLACES the child env when env= is set, so the parent environment has to be
    # carried over or setting this option would strip PATH from the browser process.
    monkeypatch.setenv("CC_TEST_MARKER", "1")
    kw = {}
    apply_shader_dialect("hlsl", kw)
    assert kw["env"]["CC_TEST_MARKER"] == "1"


def test_env_var_name_is_the_one_the_engine_reads():
    # The engine reads this exact name from the GPU process environment; renaming either side
    # silently disables the feature.
    assert _shaderdialect.ENV_VAR == "CLEARCOTE_SHADER_DIALECT"
