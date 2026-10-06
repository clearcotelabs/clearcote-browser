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
/// <para>ON BY DEFAULT for a Windows claim on a non-Windows host (the Docker entrypoint has done
/// this for CC_PLATFORM=windows since 0.31). Measured 2026-10-06 on a GPU-less Linux host with r30:
/// a Windows persona answered the query with SwiftShader's SPIR-V text next to "Direct3D11"; with
/// the dialect set it answered in HLSL. A shader the HLSL translator rejects falls back to the
/// backend's own output for that shader — the state every launch was in before, so the default
/// cannot make a page see anything worse. Set <c>ShaderDialect = "off"</c> to turn it off. On a
/// Windows host the D3D11 backend already answers in HLSL and nothing is set; a Linux or macOS
/// claim never gets HLSL by default.</para>
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

    private static readonly string[] Off = { "", "0", "off", "false", "no", "none" };

    /// The dialect a launch gets when the caller did not choose one: "hlsl" when the built command
    /// line claims Windows (<c>--fingerprint-platform=windows</c>, the last one wins) and the host is
    /// not Windows, else null.
    internal static string? Default(IEnumerable<string>? args, bool? hostIsWindows = null)
    {
        if (hostIsWindows ?? OperatingSystem.IsWindows()) return null;
        const string flag = "--fingerprint-platform=";
        string? claimed = null;
        foreach (var a in args ?? Array.Empty<string>())
            if (a is not null && a.StartsWith(flag, StringComparison.Ordinal))
                claimed = a[flag.Length..].Trim().ToLowerInvariant();
        return claimed == "windows" ? "hlsl" : null;
    }

    /// null -> the default for this claim/host; an "off" spelling ("", "off", "0", "false", "no",
    /// "none") -> null; otherwise the validated dialect.
    internal static string? Resolve(string? dialect, IEnumerable<string>? args, bool? hostIsWindows = null)
    {
        if (dialect is null) return Default(args, hostIsWindows);
        if (Array.IndexOf(Off, dialect.Trim().ToLowerInvariant()) >= 0) return null;
        return Normalize(dialect);
    }

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
    /// <paramref name="dialect"/> null means the default for this claim/host (see
    /// <see cref="Default"/> over <paramref name="args"/>). Returns <paramref name="baseEnv"/>
    /// untouched when nothing applies — including <c>null</c>, so Playwright's default child env is
    /// preserved rather than replaced by a copy of the current process environment. The default
    /// never overrides a variable the caller already exported.
    ///
    /// Throws on an unknown dialect rather than ignoring it: a typo would otherwise look like it
    /// worked while the engine kept reporting the honest dialect.
    /// </remarks>
    internal static IDictionary<string, string>? Apply(string? dialect, IDictionary<string, string>? baseEnv,
        IEnumerable<string>? args = null, bool? hostIsWindows = null)
    {
        var value = Resolve(dialect, args, hostIsWindows);
        if (value is null) return baseEnv;
        if (dialect is null)
        {
            var exported = baseEnv is not null ? baseEnv.ContainsKey(EnvVar) : Environment.GetEnvironmentVariable(EnvVar) is not null;
            if (exported) return baseEnv;
        }

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
