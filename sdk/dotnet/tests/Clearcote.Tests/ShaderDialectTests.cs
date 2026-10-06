using Xunit;

namespace Clearcote.Tests;

public class ShaderDialectTests
{
    [Fact]
    public void EnvVar_is_the_name_the_engine_reads()
    {
        // The engine reads this exact name from the GPU process environment; renaming either side
        // silently disables the feature.
        Assert.Equal("CLEARCOTE_SHADER_DIALECT", ShaderDialect.EnvVar);
    }

    [Fact]
    public void Normalize_returns_null_when_nothing_is_asked_for()
    {
        Assert.Null(ShaderDialect.Normalize(null));
        Assert.Null(ShaderDialect.Normalize(""));
        Assert.Null(ShaderDialect.Normalize("   "));
    }

    [Fact]
    public void Normalize_trims_and_lowercases()
    {
        Assert.Equal("hlsl", ShaderDialect.Normalize("  HLSL  "));
    }

    [Fact]
    public void Normalize_rejects_an_unknown_dialect()
    {
        // Rejected rather than ignored: a typo would otherwise look like it worked while the engine
        // kept reporting the honest dialect.
        Assert.Throws<ArgumentException>(() => ShaderDialect.Normalize("glsl"));
    }

    [Fact]
    public void Apply_is_a_no_op_when_off()
    {
        // A null env must stay null so Playwright uses its default child env, exactly as before the
        // option existed.
        Assert.Null(ShaderDialect.Apply(null, null));

        var baseEnv = new Dictionary<string, string> { ["FONTCONFIG_FILE"] = "/tmp/fonts.conf" };
        Assert.Same(baseEnv, ShaderDialect.Apply(null, baseEnv));
    }

    [Fact]
    public void Apply_sets_the_variable()
    {
        var env = ShaderDialect.Apply("hlsl", new Dictionary<string, string>())!;
        Assert.Equal("hlsl", env[ShaderDialect.EnvVar]);
    }

    [Fact]
    public void Apply_keeps_what_is_already_in_the_env()
    {
        var baseEnv = new Dictionary<string, string> { ["FONTCONFIG_FILE"] = "/tmp/fonts.conf" };
        var env = ShaderDialect.Apply("hlsl", baseEnv)!;
        Assert.Equal("/tmp/fonts.conf", env["FONTCONFIG_FILE"]);
        Assert.Equal("hlsl", env[ShaderDialect.EnvVar]);
    }

    [Fact]
    public void Apply_carries_the_process_env_when_there_is_no_base()
    {
        // Playwright REPLACES the child env when Env is set, so the parent environment has to come
        // along or the browser loses PATH.
        using var _ = new Sandbox().Env("CC_TEST_MARKER", "1");
        var env = ShaderDialect.Apply("hlsl", null)!;
        Assert.Equal("1", env["CC_TEST_MARKER"]);
        Assert.Equal("hlsl", env[ShaderDialect.EnvVar]);
    }

    // ── default for a Windows claim (2026-10-06) ─────────────────────────────
    private static readonly string[] Win = { "--fingerprint=s", "--fingerprint-platform=windows" };
    private static readonly string[] Linux = { "--fingerprint=s", "--fingerprint-platform=linux" };

    [Fact]
    public void A_windows_claim_off_windows_gets_hlsl_by_default()
    {
        // Measured on r30, GPU-less Linux: the Windows persona answered getTranslatedShaderSource
        // with SwiftShader text beside a Direct3D11 renderer string; with the dialect, HLSL.
        var env = ShaderDialect.Apply(null, new Dictionary<string, string> { ["PATH"] = "x" }, Win, hostIsWindows: false)!;
        Assert.Equal("hlsl", env[ShaderDialect.EnvVar]);
        Assert.Equal("x", env["PATH"]);
    }

    [Fact]
    public void No_default_on_windows_for_a_linux_claim_or_without_a_claim()
    {
        Assert.Null(ShaderDialect.Default(Win, hostIsWindows: true));
        Assert.Null(ShaderDialect.Default(Linux, hostIsWindows: false));
        Assert.Null(ShaderDialect.Default(Array.Empty<string>(), hostIsWindows: false));
        Assert.Null(ShaderDialect.Apply(null, null, Linux, hostIsWindows: false));
    }

    [Fact]
    public void The_last_platform_switch_decides()
    {
        Assert.Equal("hlsl", ShaderDialect.Default(Linux.Append("--fingerprint-platform=windows"), hostIsWindows: false));
        Assert.Null(ShaderDialect.Default(Win.Append("--fingerprint-platform=linux"), hostIsWindows: false));
    }

    [Theory]
    [InlineData("off")]
    [InlineData("0")]
    [InlineData("none")]
    [InlineData("")]
    public void An_off_spelling_beats_the_default(string off)
    {
        var baseEnv = new Dictionary<string, string> { ["A"] = "1" };
        Assert.Same(baseEnv, ShaderDialect.Apply(off, baseEnv, Win, hostIsWindows: false));
    }

    [Fact]
    public void The_callers_own_variable_is_not_overridden_by_the_default()
    {
        var baseEnv = new Dictionary<string, string> { [ShaderDialect.EnvVar] = "mine" };
        Assert.Same(baseEnv, ShaderDialect.Apply(null, baseEnv, Win, hostIsWindows: false));
    }
}
