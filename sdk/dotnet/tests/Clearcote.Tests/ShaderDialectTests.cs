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
}
