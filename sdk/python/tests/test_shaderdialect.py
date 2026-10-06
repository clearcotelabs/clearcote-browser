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


# ---------------------------------------------------- default for a Windows claim (2026-10-06)
from clearcote._shaderdialect import default_shader_dialect, shader_dialect_env  # noqa: E402

WIN = ["--fingerprint=s", "--fingerprint-platform=windows"]
LINUX = ["--fingerprint=s", "--fingerprint-platform=linux"]


def test_windows_claim_on_a_linux_host_gets_hlsl_by_default(monkeypatch):
    # Measured on r30, GPU-less Linux: the Windows persona answered getTranslatedShaderSource with
    # SwiftShader text beside a Direct3D11 renderer string; with the dialect it answered in HLSL.
    monkeypatch.delenv(ENV_VAR, raising=False)
    kw = {}
    apply_shader_dialect(None, kw, WIN, host_platform="linux")
    assert kw["env"][ENV_VAR] == "hlsl"


@pytest.mark.parametrize("args,host", [
    (WIN, "win32"),      # the D3D11 backend already answers in HLSL
    (LINUX, "linux"),    # a Linux claim must keep its own dialect
    ([], "linux"),       # no claim at all
    (None, "darwin"),
])
def test_no_default_dialect_otherwise(args, host):
    assert default_shader_dialect(args, host) is None


def test_the_last_platform_switch_decides():
    assert default_shader_dialect(LINUX + ["--fingerprint-platform=windows"], "linux") == "hlsl"
    assert default_shader_dialect(WIN + ["--fingerprint-platform=linux"], "linux") is None


@pytest.mark.parametrize("off", [False, "off", "0", "none", ""])
def test_explicit_off_beats_the_default(off, monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    kw = {}
    apply_shader_dialect(off, kw, WIN, host_platform="linux")
    assert kw == {}


def test_callers_own_env_var_is_not_overridden_by_the_default(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "hlsl-custom")
    kw = {}
    apply_shader_dialect(None, kw, WIN, host_platform="linux")
    assert kw == {}  # Playwright inherits the caller's variable unchanged


def test_serve_env_gets_the_same_default():
    env = shader_dialect_env(None, WIN, {"PATH": "x"}, host_platform="linux")
    assert env == {"PATH": "x", ENV_VAR: "hlsl"}
    assert shader_dialect_env(None, LINUX, {}, host_platform="linux") == {}
    assert shader_dialect_env(False, WIN, {}, host_platform="linux") == {}
    assert shader_dialect_env(None, WIN, {ENV_VAR: "mine"}, host_platform="linux") == {ENV_VAR: "mine"}
