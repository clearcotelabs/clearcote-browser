using System.Text.Encodings.Web;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

// Live render-backend coherence probe (#7) - the page-level counterpart to the launch-time heuristic in
// LaunchWarnings. Reads what the page actually sees from WebGL and checks the two render-coherence tells a
// strict detector looks for: a software rasterizer (SwiftShader/llvmpipe/Mesa OffScreen - fatal on a
// "stealth" build), and an incoherent vendor/renderer GPU-family pair. It cannot read the real host GPU when
// the persona spoofs the unmasked strings (that's the point of the spoof) - it verifies the values the page
// is allowed to see are internally coherent and not a software fallback. For the deeper "do the pixels match
// the claimed GPU class" check, route paints through the canvas bridge. Mirrors sdk/node/src/render.ts and
// sdk/python/clearcote/_render.py.

/// The verdict of <see cref="Clearcote.CheckRenderCoherenceAsync"/>.
public sealed class RenderVerdict
{
    /// The WebGL vendor the page sees (the unmasked one when exposed).
    public string Vendor { get; init; } = "";
    /// The WebGL renderer the page sees (the unmasked one when exposed).
    public string Renderer { get; init; } = "";
    /// Whether a WebGL context could be created.
    public bool Webgl { get; init; }
    /// Whether a WebGL2 context could be created.
    public bool Webgl2 { get; init; }
    /// <c>MAX_TEXTURE_SIZE</c> (0 when not reported).
    public int MaxTextureSize { get; init; }
    /// <c>MAX_RENDERBUFFER_SIZE</c> (0 when not reported).
    public int MaxRenderbufferSize { get; init; }
    /// <c>MAX_VERTEX_UNIFORM_VECTORS</c> (0 when not reported).
    public int MaxVertexUniformVectors { get; init; }
    /// <c>MAX_FRAGMENT_UNIFORM_VECTORS</c> (0 when not reported).
    public int MaxFragmentUniformVectors { get; init; }
    /// Whether a 16384-wide texture actually allocated (null when the probe could not tell).
    public bool? CanAllocate16kTexture { get; init; }
    /// A software rasterizer is behind the renderer (named outright, or below a desktop GPU's capability
    /// floor) - a fatal tell on a stealth build.
    public bool SoftwareSuspected { get; init; }
    /// WebGL is present, nothing points at a software rasterizer, and the persona agrees with itself.
    public bool Coherent { get; init; }
    /// Human-readable findings; empty when coherent.
    public IReadOnlyList<string> Warnings { get; init; } = Array.Empty<string>();
}

/// What the probe read off a WebGL context (field names as the probe JS reports them).
internal sealed record RenderInfo
{
    public bool Webgl { get; init; }
    public bool Webgl2 { get; init; }
    public string? Vendor { get; init; }
    public string? Renderer { get; init; }
    public string? UnmaskedVendor { get; init; }
    public string? UnmaskedRenderer { get; init; }
    public int MaxTextureSize { get; init; }
    public int MaxRenderbufferSize { get; init; }
    public int MaxVertexUniformVectors { get; init; }
    public int MaxFragmentUniformVectors { get; init; }
    public bool? CanAllocate16k { get; init; }
}

/// The render-coherence probe and its pure analysis. Entry point: <see cref="Clearcote.CheckRenderCoherenceAsync"/>.
internal static class RenderCoherence
{
    /// Probe JS - reads VENDOR/RENDERER + the unmasked pair via a throwaway WebGL context.
    internal const string ProbeJs = """
() => {
  const out = { webgl: false, webgl2: false, vendor: "", renderer: "",
                unmaskedVendor: "", unmaskedRenderer: "", maxTextureSize: 0 };
  try {
    const c = document.createElement('canvas');
    const gl2 = c.getContext('webgl2');
    const gl = gl2 || c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return out;
    out.webgl = true;
    out.webgl2 = !!gl2;
    out.vendor = gl.getParameter(gl.VENDOR) || "";
    out.renderer = gl.getParameter(gl.RENDERER) || "";
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    if (dbg) {
      out.unmaskedVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || "";
      out.unmaskedRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || "";
    }
    out.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 0;
    out.maxRenderbufferSize = gl.getParameter(gl.MAX_RENDERBUFFER_SIZE) || 0;
    const vp = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
    out.maxViewportDims = vp ? Array.from(vp) : [];
    out.maxVertexUniformVectors = gl.getParameter(gl.MAX_VERTEX_UNIFORM_VECTORS) || 0;
    out.maxFragmentUniformVectors = gl.getParameter(gl.MAX_FRAGMENT_UNIFORM_VECTORS) || 0;
    // Capability probe, not a declared value: a spoofed renderer string is free, an actual
    // 16384-wide texture allocation is not. A software-rasterizer backend refuses it.
    // The error queue is drained first: getError() is sticky, so a flag left behind by anything
    // above would otherwise be read back as "the allocation failed".
    try {
      let drain = 0;
      while (gl.getError() !== gl.NO_ERROR && drain++ < 32) {}
      const t = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, t);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 16384, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      out.canAllocate16k = gl.getError() === gl.NO_ERROR;
      gl.deleteTexture(t);
    } catch (e) { out.canAllocate16k = null; }
  } catch (e) { out.error = String(e); }
  return out;
}
""";

    private static readonly string[] SoftwareMarkers =
    {
        "swiftshader", "google swiftshader", "llvmpipe", "softpipe",
        "mesa offscreen", "microsoft basic render", "software adapter",
    };

    // substring -> GPU family. The first hit wins per string.
    private static readonly (string Key, string Family)[] FamilyKeys =
    {
        ("nvidia", "nvidia"), ("geforce", "nvidia"), ("rtx", "nvidia"), ("gtx", "nvidia"), ("quadro", "nvidia"),
        ("radeon", "amd"), ("amd", "amd"), ("ati ", "amd"),
        ("intel", "intel"), ("iris", "intel"), ("uhd graphics", "intel"), ("hd graphics", "intel"),
        ("apple", "apple"), ("m1", "apple"), ("m2", "apple"), ("m3", "apple"), ("m4", "apple"),
        ("mali", "mali"), ("adreno", "adreno"), ("powervr", "powervr"),
    };

    // Desktop/laptop GPU families. A persona naming one of these claims a machine whose driver backs a
    // 16384 texture; the mobile families legitimately sit below that, so the floor skips them.
    private static readonly string[] DesktopFamilies = { "nvidia", "amd", "intel", "apple" };

    // Capability floor for a desktop-class GPU claim, measured rather than assumed:
    //   SwiftShader (ANGLE/Vulkan, genuine Chrome 153 headless Linux) ....... 8192
    //   Mesa llvmpipe (LLVM 15, genuine Chrome 153 headed Linux) ........... 16384
    //   ANGLE/D3D11 on a GeForce RTX 3070 (genuine Chrome 153 Windows) ..... 16384
    // 16384 is also the D3D11 feature-level-11 2D texture cap and the Mesa GL limit for Intel Gen7+.
    internal const int HwMinMaxTextureSize = 16384;

    // JSON.stringify's quoting for the strings named in a warning (no HTML-safe escaping).
    private static readonly JsonSerializerOptions Quote = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private static string Q(string s) => JsonSerializer.Serialize(s, Quote);

    /// Best-effort GPU family from a vendor/renderer string ("" if unknown).
    internal static string GpuFamily(string? s)
    {
        var l = (s ?? "").ToLowerInvariant();
        foreach (var (key, fam) in FamilyKeys)
            if (l.Contains(key, StringComparison.Ordinal)) return fam;
        return "";
    }

    /// Pure analysis of a probe result -> coherence verdict (unit-testable, no Playwright).
    internal static RenderVerdict Evaluate(RenderInfo info, string? claimedGpu = null)
    {
        var renderer = NonEmpty(info.UnmaskedRenderer) ?? NonEmpty(info.Renderer) ?? "";
        var vendor = NonEmpty(info.UnmaskedVendor) ?? NonEmpty(info.Vendor) ?? "";
        var rl = renderer.ToLowerInvariant();
        var vl = vendor.ToLowerInvariant();
        var warnings = new List<string>();

        var hasWebgl = info.Webgl;
        if (!hasWebgl)
            warnings.Add(
                "WebGL is unavailable - a hard tell for a real desktop browser (only headless or locked-down setups disable it).");

        var software = SoftwareMarkers.Any(m => rl.Contains(m, StringComparison.Ordinal) || vl.Contains(m, StringComparison.Ordinal));
        if (software)
            warnings.Add(
                $"software rasterizer detected in the WebGL renderer ({Q(renderer)}) - a definitive " +
                "headless/no-GPU tell. Enable the canvas bridge (CanvasBridge = ...) or run headed on a machine with a real GPU.");

        var rfam = GpuFamily(rl);
        var vfam = GpuFamily(vl);

        // Capability floor. The string check above is defeated the moment the persona renames the
        // backend, so verify the claim against a limit the renderer string cannot move: a software
        // rasterizer reports (and allocates) 8192 where every desktop GPU does 16384.
        var maxTex = info.MaxTextureSize;
        var can16k = info.CanAllocate16k;
        var vuv = info.MaxVertexUniformVectors;
        var fuv = info.MaxFragmentUniformVectors;
        if (DesktopFamilies.Contains(rfam) && maxTex != 0 && maxTex < HwMinMaxTextureSize)
        {
            software = true;
            warnings.Add(
                $"the WebGL renderer names a desktop GPU ({Q(renderer)}) but MAX_TEXTURE_SIZE is " +
                $"{maxTex}, below the {HwMinMaxTextureSize} current desktop drivers report (only " +
                "pre-feature-level-11 D3D parts cap at 8192) - on a stealth build this means the renderer " +
                "string was spoofed over a software rasterizer (headless Linux falls back to SwiftShader). " +
                "Run headed, or use the canvas bridge.");
        }
        else if (DesktopFamilies.Contains(rfam) && can16k == false)
        {
            software = true;
            warnings.Add(
                $"the WebGL renderer names a desktop GPU ({Q(renderer)}) and reports " +
                $"MAX_TEXTURE_SIZE {maxTex}, but a {HwMinMaxTextureSize}-wide texture fails to " +
                "allocate - the reported limit is not backed by the real rendering backend.");
        }

        // Uniform-vector split: an ANGLE-over-GL/Mesa renderer reports vertex == fragment on every real
        // driver (measured: SwiftShader 4096/4096, llvmpipe 1024/1024). ANGLE/D3D11 legitimately differs
        // (4095/1024), so the check is limited to the non-D3D backends.
        var incoherent = false;
        if (vuv != 0 && fuv != 0 && vuv != fuv && !rl.Contains("d3d", StringComparison.Ordinal) && !rl.Contains("direct3d", StringComparison.Ordinal))
        {
            incoherent = true;
            warnings.Add(
                $"MAX_VERTEX_UNIFORM_VECTORS ({vuv}) and MAX_FRAGMENT_UNIFORM_VECTORS ({fuv}) disagree under " +
                $"a non-D3D renderer ({Q(renderer)}); every GL backend measured here reports " +
                "them equal (SwiftShader 4096/4096, llvmpipe 1024/1024), so the persona applied to one and " +
                "not the other.");
        }

        if (rfam.Length > 0 && vfam.Length > 0 && rfam != vfam)
        {
            incoherent = true;
            warnings.Add(
                $"WebGL vendor and renderer disagree on GPU family (vendor~{vfam}, renderer~{rfam}) - an incoherent persona.");
        }

        if (!string.IsNullOrEmpty(claimedGpu))
        {
            var cfam = GpuFamily(claimedGpu);
            if (cfam.Length > 0 && rfam.Length > 0 && cfam != rfam)
            {
                incoherent = true;
                warnings.Add(
                    $"the claimed GPU ({Q(claimedGpu)}, family ~{cfam}) does not match the WebGL renderer family (~{rfam}).");
            }
        }

        // Set by the branches above rather than by matching warning text: the verdict must not depend on
        // the wording of a message.
        var coherent = hasWebgl && !software && !incoherent;
        return new RenderVerdict
        {
            Vendor = vendor,
            Renderer = renderer,
            Webgl = hasWebgl,
            Webgl2 = info.Webgl2,
            MaxTextureSize = maxTex,
            MaxRenderbufferSize = info.MaxRenderbufferSize,
            MaxVertexUniformVectors = vuv,
            MaxFragmentUniformVectors = fuv,
            CanAllocate16kTexture = can16k,
            SoftwareSuspected = software,
            Coherent = coherent,
            Warnings = warnings,
        };
    }

    private static string? NonEmpty(string? s) => string.IsNullOrEmpty(s) ? null : s;

    /// The probe's JSON answer as a <see cref="RenderInfo"/>, with JavaScript's falsy-means-unset reading
    /// (<c>info.x || 0</c>, <c>!!info.webgl</c>).
    internal static RenderInfo Parse(JsonElement? probe)
    {
        var o = probe is { ValueKind: JsonValueKind.Object } p ? p : default;
        JsonElement F(string name) => o.ValueKind == JsonValueKind.Object && o.TryGetProperty(name, out var v) ? v : default;
        string? S(string name) => F(name) is { ValueKind: JsonValueKind.String } v ? v.GetString() : null;
        int N(string name) => F(name) is { ValueKind: JsonValueKind.Number } v && double.IsFinite(v.GetDouble()) ? (int)v.GetDouble() : 0;
        bool B(string name) => F(name).ValueKind == JsonValueKind.True;
        var can = F("canAllocate16k");
        return new RenderInfo
        {
            Webgl = B("webgl"),
            Webgl2 = B("webgl2"),
            Vendor = S("vendor"),
            Renderer = S("renderer"),
            UnmaskedVendor = S("unmaskedVendor"),
            UnmaskedRenderer = S("unmaskedRenderer"),
            MaxTextureSize = N("maxTextureSize"),
            MaxRenderbufferSize = N("maxRenderbufferSize"),
            MaxVertexUniformVectors = N("maxVertexUniformVectors"),
            MaxFragmentUniformVectors = N("maxFragmentUniformVectors"),
            CanAllocate16k = can.ValueKind switch { JsonValueKind.True => true, JsonValueKind.False => false, _ => null },
        };
    }
}

public static partial class Clearcote
{
    /// <summary>
    /// Probe a live Playwright <paramref name="page"/> for render-backend coherence (#7). Returns the
    /// vendor/renderer the page actually sees, <see cref="RenderVerdict.SoftwareSuspected"/> (a
    /// SwiftShader/llvmpipe fallback is a fatal tell), <see cref="RenderVerdict.Coherent"/>, and human-readable
    /// <see cref="RenderVerdict.Warnings"/>. Pass <paramref name="claimedGpu"/> (the GPU string your persona is
    /// supposed to present) to also assert the rendered family.
    /// </summary>
    /// <remarks>
    /// The persona's own strings cannot vouch for themselves, so beyond the renderer names it checks a
    /// capability floor: a renderer naming a desktop GPU (NVIDIA, AMD, Intel, Apple) must report
    /// <c>MAX_TEXTURE_SIZE</c> of at least 16384 and actually allocate a 16384-wide texture, which a software
    /// rasterizer renamed by the persona does not. Under a non-D3D renderer the vertex and fragment
    /// uniform-vector limits must agree.
    /// <code>
    /// var browser = await Clearcote.LaunchAsync(new() { Fingerprint = "77" });
    /// var page = await browser.NewPageAsync(); await page.GotoAsync("about:blank");
    /// var verdict = await Clearcote.CheckRenderCoherenceAsync(page);
    /// if (!verdict.Coherent) Console.Error.WriteLine(string.Join("\n", verdict.Warnings));
    /// </code>
    /// </remarks>
    public static async Task<RenderVerdict> CheckRenderCoherenceAsync(IPage page, string? claimedGpu = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        var probe = await page.EvaluateAsync<JsonElement?>(RenderCoherence.ProbeJs).ConfigureAwait(false);
        return RenderCoherence.Evaluate(RenderCoherence.Parse(probe), claimedGpu);
    }
}
