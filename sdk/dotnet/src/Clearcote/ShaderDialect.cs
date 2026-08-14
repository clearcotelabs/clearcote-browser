namespace Clearcote;

/// <summary>
/// Optional HLSL shader dialect for a Windows persona on a non-Windows host.
///
/// <para><c>WEBGL_debug_shaders.getTranslatedShaderSource()</c> returns whatever ANGLE's active
/// backend produced. A Windows persona advertises a Direct3D11 renderer, but on a Linux host the
/// Vulkan backend answers with a SPIR-V dump, so the renderer string and the dialect beside it
/// contradict each other. <c>ShaderDialect = "hlsl"</c> makes the engine re-translate the shader to
/// HLSL for that query alone — rendering is untouched, and the result is byte-identical to what the
/// Windows build reports.</para>
///
/// <para>OFF unless asked for. The re-translation is a different code path from the one that
/// rendered, so a shader the Vulkan backend accepts but the HLSL translator rejects falls back to
/// the honest SPIR-V for that shader. It is for callers who actually hit this check, not a
/// default.</para>
///
/// <para>Delivered as an environment variable because the code lives in the GPU process, which does
/// not receive the fingerprint switches. Requires a PRO engine built with the option (151 r15+);
/// older binaries ignore the variable.</para>
/// </summary>
internal static class ShaderDialect
{
    /// The name the engine reads from the GPU process environment.
    internal const string EnvVar = "CLEARCOTE_SHADER_DIALECT";

    private static readonly string[] Valid = { "hlsl" };

    /// The validated, lower-cased dialect, or null when none was asked for. Throws on an unknown
    /// value rather than ignoring it: a typo would otherwise look like it worked while the engine
    /// kept reporting the honest dialect.
    internal static string? Normalize(string? dialect)
    {
        if (string.IsNullOrWhiteSpace(dialect)) return null;
        var value = dialect!.Trim().ToLowerInvariant();
        if (Array.IndexOf(Valid, value) < 0)
        {
            throw new ArgumentException(
                $"ShaderDialect must be one of {string.Join(", ", Valid)} (got \"{dialect}\").",
                nameof(dialect));
        }
        return value;
    }

    /// <summary>
    /// Fold <c>CLEARCOTE_SHADER_DIALECT</c> into a launch env.
    /// </summary>
    /// <remarks>
    /// Returns <paramref name="baseEnv"/> untouched when no dialect is requested — including
    /// <c>null</c>, so Playwright's default child env is preserved rather than replaced by a copy
    /// of the current process environment.
    ///
    /// Throws on an unknown dialect rather than ignoring it: a typo would otherwise look like it
    /// worked while the engine kept reporting the honest dialect.
    /// </remarks>
    internal static IDictionary<string, string>? Apply(string? dialect, IDictionary<string, string>? baseEnv)
    {
        var value = Normalize(dialect);
        if (value is null) return baseEnv;

        var outEnv = new Dictionary<string, string>();
        if (baseEnv is not null)
        {
            foreach (var (k, v) in baseEnv) if (v is not null) outEnv[k] = v;
        }
        else
        {
            // Playwright REPLACES the child env when Env is set, so the parent environment has to
            // come along or the browser loses PATH.
            foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables())
                if (e.Value is string sv) outEnv[(string)e.Key] = sv;
        }
        outEnv[EnvVar] = value;
        return outEnv;
    }
}
