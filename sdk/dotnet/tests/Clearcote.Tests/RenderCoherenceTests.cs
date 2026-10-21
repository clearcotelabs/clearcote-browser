using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// <see cref="Clearcote.CheckRenderCoherenceAsync"/>: the pure analysis of a WebGL probe (software rasterizer,
/// vendor/renderer family, claimed GPU, and the capability floor). Ports sdk/node/test/render.test.ts and
/// sdk/python/tests/test_render.py; the last class runs the probe in a real Chromium.
/// </summary>
public class RenderCoherenceTests
{
    // ── GpuFamily ────────────────────────────────────────────────────────────

    [Fact]
    public void Maps_common_renderer_and_vendor_substrings_to_a_family()
    {
        Assert.Equal("nvidia", RenderCoherence.GpuFamily("ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, D3D11)"));
        Assert.Equal("intel", RenderCoherence.GpuFamily("ANGLE (Intel, Intel(R) UHD Graphics 770, D3D11)"));
        Assert.Equal("amd", RenderCoherence.GpuFamily("ANGLE (AMD, AMD Radeon RX 6800, D3D11)"));
        Assert.Equal("apple", RenderCoherence.GpuFamily("Apple M2"));
        Assert.Equal("", RenderCoherence.GpuFamily(""));
        Assert.Equal("", RenderCoherence.GpuFamily(null));
    }

    // ── Evaluate ─────────────────────────────────────────────────────────────

    [Fact]
    public void Accepts_a_coherent_NVIDIA_persona()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Webgl2 = true,
            Vendor = "Google Inc. (NVIDIA)",
            Renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)",
            MaxTextureSize = 16384,
        });
        Assert.True(v.Coherent);
        Assert.False(v.SoftwareSuspected);
        Assert.Empty(v.Warnings);
    }

    [Fact]
    public void Flags_a_software_rasterizer_as_a_fatal_tell()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true,
            Vendor = "Google Inc. (Google)",
            Renderer = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 16.0.0)), SwiftShader driver)",
        });
        Assert.True(v.SoftwareSuspected);
        Assert.False(v.Coherent);
        Assert.Contains(v.Warnings, w => w.Contains("software rasterizer"));
        // the renderer is quoted as JSON.stringify would
        Assert.Contains("(\"ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 16.0.0)), SwiftShader driver)\")", v.Warnings[0]);
    }

    [Fact]
    public void Flags_an_incoherent_vendor_renderer_family()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true,
            Vendor = "Google Inc. (Apple)",
            Renderer = "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, D3D11)",
        });
        Assert.False(v.Coherent);
        Assert.Contains(v.Warnings, w => w.Contains("disagree on GPU family"));
        Assert.Contains("(vendor~apple, renderer~nvidia)", Assert.Single(v.Warnings));
    }

    [Fact]
    public void Flags_missing_WebGL()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo { Webgl = false });
        Assert.False(v.Coherent);
        Assert.Contains(v.Warnings, w => w.Contains("WebGL is unavailable"));
    }

    [Fact]
    public void Flags_a_claimed_GPU_mismatch()
    {
        var v = RenderCoherence.Evaluate(
            new RenderInfo { Webgl = true, Vendor = "Google Inc. (Intel)", Renderer = "ANGLE (Intel, Intel(R) UHD Graphics 770, D3D11)" },
            "NVIDIA GeForce RTX 4090");
        Assert.False(v.Coherent);
        Assert.Contains(v.Warnings, w => w.Contains("does not match"));
        // a matching claim is fine
        Assert.True(RenderCoherence.Evaluate(
            new RenderInfo { Webgl = true, Vendor = "Google Inc. (Intel)", Renderer = "ANGLE (Intel, Intel(R) UHD Graphics 770, D3D11)" },
            "Intel(R) UHD Graphics 770").Coherent);
    }

    [Fact]
    public void Prefers_the_unmasked_pair_over_the_masked_one()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true,
            Vendor = "WebKit", Renderer = "WebKit WebGL",
            UnmaskedVendor = "Google Inc. (Intel)",
            UnmaskedRenderer = "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, D3D11)",
        });
        Assert.StartsWith("ANGLE (Intel", v.Renderer);
        Assert.Equal("Google Inc. (Intel)", v.Vendor);
        Assert.True(v.Coherent);
    }

    // ── capability floor: the string check is defeated once the persona renames the backend ──
    // Values below are measured on a GPU-less Linux VPS with clearcote 153 r25 and genuine Chrome 153.

    private const string MesaUhd770 =
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 770 (RPL-S), OpenGL 4.6 (Core Profile) Mesa 23.2.1-1ubuntu3.1~22.04.2)";

    [Fact]
    public void Catches_a_renderer_string_spoofed_over_SwiftShader()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Webgl2 = true,
            Vendor = "Google Inc. (Intel)", Renderer = MesaUhd770,
            MaxTextureSize = 8192, MaxRenderbufferSize = 8192,
            MaxVertexUniformVectors = 4096, MaxFragmentUniformVectors = 4096,
            CanAllocate16k = false,
        });
        Assert.True(v.SoftwareSuspected);
        Assert.False(v.Coherent);
        Assert.Contains(v.Warnings, w => w.Contains("MAX_TEXTURE_SIZE is 8192"));
        Assert.Single(v.Warnings);   // the floor fires once: the allocation branch is the else
        Assert.Equal((8192, 8192, false), (v.MaxTextureSize, v.MaxRenderbufferSize, v.CanAllocate16kTexture));
    }

    [Fact]
    public void Catches_a_16k_limit_that_does_not_allocate()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Vendor = "Google Inc. (Intel)", Renderer = MesaUhd770,
            MaxTextureSize = 16384, CanAllocate16k = false,
        });
        Assert.True(v.SoftwareSuspected);
        Assert.False(v.Coherent);
        Assert.Contains("fails to allocate", Assert.Single(v.Warnings));
    }

    [Fact]
    public void Leaves_a_headed_llvmpipe_backed_persona_coherent()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Webgl2 = true,
            Vendor = "Google Inc. (Intel)", Renderer = MesaUhd770,
            MaxTextureSize = 16384, MaxRenderbufferSize = 16384,
            MaxVertexUniformVectors = 1024, MaxFragmentUniformVectors = 1024,
            CanAllocate16k = true,
        });
        Assert.False(v.SoftwareSuspected);
        Assert.True(v.Coherent);
        Assert.Empty(v.Warnings);
    }

    [Fact]
    public void Does_not_fire_on_a_genuine_ANGLE_D3D11_vertex_fragment_split()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Webgl2 = true,
            Vendor = "Google Inc. (Intel)",
            Renderer = "ANGLE (Intel, Intel(R) UHD Graphics 770 (0xA780) Direct3D11 vs_5_0 ps_5_0, D3D11)",
            MaxTextureSize = 16384, MaxVertexUniformVectors = 4095, MaxFragmentUniformVectors = 1024,
            CanAllocate16k = true,
        });
        Assert.True(v.Coherent);
        Assert.Empty(v.Warnings);
    }

    [Fact]
    public void Catches_a_half_applied_persona_that_splits_uniform_vectors_under_a_GL_renderer()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Webgl2 = true,
            Vendor = "Google Inc. (Intel)", Renderer = MesaUhd770,
            MaxTextureSize = 16384, MaxVertexUniformVectors = 4096, MaxFragmentUniformVectors = 1024,
            CanAllocate16k = true,
        });
        Assert.False(v.Coherent);
        Assert.False(v.SoftwareSuspected);
        Assert.Contains(v.Warnings, w => w.Contains("MAX_VERTEX_UNIFORM_VECTORS"));
    }

    [Fact]
    public void Skips_mobile_GPU_families_which_legitimately_report_8192()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Webgl2 = true, Vendor = "ARM", Renderer = "Mali-G78",
            MaxTextureSize = 8192, MaxVertexUniformVectors = 1024, MaxFragmentUniformVectors = 1024,
        });
        Assert.False(v.SoftwareSuspected);
        Assert.True(v.Coherent);
    }

    [Fact]
    public void Is_inert_when_the_probe_did_not_report_limits()
    {
        var v = RenderCoherence.Evaluate(new RenderInfo
        {
            Webgl = true, Vendor = "Google Inc. (Intel)",
            Renderer = "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, D3D11)",
        });
        Assert.True(v.Coherent);
        Assert.Empty(v.Warnings);
        Assert.Null(v.CanAllocate16kTexture);
        Assert.Equal(16384, RenderCoherence.HwMinMaxTextureSize);
    }

    // ── Parse: the probe's JSON with JavaScript's reading ───────────────────

    [Fact]
    public void Parses_the_probe_answer()
    {
        var info = RenderCoherence.Parse(JsonDocument.Parse("""
            {"webgl":true,"webgl2":true,"vendor":"WebKit","renderer":"WebKit WebGL",
             "unmaskedVendor":"Google Inc. (Intel)","unmaskedRenderer":"ANGLE (Intel, X, D3D11)",
             "maxTextureSize":16384,"maxRenderbufferSize":16384,"maxViewportDims":[32767,32767],
             "maxVertexUniformVectors":4095,"maxFragmentUniformVectors":1024,"canAllocate16k":true}
            """).RootElement);
        Assert.Equal(new RenderInfo
        {
            Webgl = true, Webgl2 = true, Vendor = "WebKit", Renderer = "WebKit WebGL",
            UnmaskedVendor = "Google Inc. (Intel)", UnmaskedRenderer = "ANGLE (Intel, X, D3D11)",
            MaxTextureSize = 16384, MaxRenderbufferSize = 16384, MaxVertexUniformVectors = 4095,
            MaxFragmentUniformVectors = 1024, CanAllocate16k = true,
        }, info);
        // canAllocate16k: null (the probe threw) and absent both read as "could not tell"
        Assert.Null(RenderCoherence.Parse(JsonDocument.Parse("""{"webgl":true,"canAllocate16k":null}""").RootElement).CanAllocate16k);
        Assert.Equal(new RenderInfo(), RenderCoherence.Parse(JsonDocument.Parse("""{"webgl":false,"error":"x"}""").RootElement));
        Assert.Equal(new RenderInfo(), RenderCoherence.Parse(null));
        // the masked pair is used when the unmasked one is empty ("" || renderer)
        var v = RenderCoherence.Evaluate(RenderCoherence.Parse(JsonDocument.Parse(
            """{"webgl":true,"vendor":"Google Inc. (NVIDIA)","renderer":"ANGLE (NVIDIA, RTX)","unmaskedVendor":"","unmaskedRenderer":""}""").RootElement));
        Assert.Equal(("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, RTX)"), (v.Vendor, v.Renderer));
    }

    [Fact]
    public async Task CheckRenderCoherenceAsync_refuses_a_null_page()
        => await Assert.ThrowsAsync<ArgumentNullException>(() => Clearcote.CheckRenderCoherenceAsync(null!));
}

/// <summary>
/// The probe in a real Chromium (CLEARCOTE_TEST_BINARY, else one Playwright has downloaded; see
/// CloudLaunchLiveTests): proves the probe JS is CALLED as a function by .NET's EvaluateAsync and that its
/// answer maps onto the verdict. A no-op without a Chromium (no Skippable* package, see GeometryLiveTests).
/// </summary>
public sealed class RenderCoherenceLiveTests
{
    [Fact]
    public async Task Probes_a_real_page()
    {
        var exe = LocalChromium.Find();
        if (exe is null) return;
        await using var chromium = await LocalChromium.StartAsync(exe);
        using var pw = await Playwright.CreateAsync();
        var browser = await pw.Chromium.ConnectOverCDPAsync(chromium.WsUrl);
        try
        {
            var page = await browser.Contexts[0].NewPageAsync();
            await page.GotoAsync("about:blank");
            var raw = await page.EvaluateAsync<JsonElement>(RenderCoherence.ProbeJs);
            Assert.Equal(JsonValueKind.Object, raw.ValueKind);   // the function ran (not stringified)
            var v = await Clearcote.CheckRenderCoherenceAsync(page);
            var info = RenderCoherence.Parse(raw);
            Assert.Equal(info.Webgl, v.Webgl);
            Assert.Equal(v.Coherent, v.Warnings.Count == 0);
            if (v.Webgl)
            {
                Assert.NotEqual("", v.Renderer);
                Assert.True(v.MaxTextureSize > 0);
                Assert.NotNull(v.CanAllocate16kTexture);
                // stock headless Chromium with --disable-gpu renders on SwiftShader: named, so suspected
                if (v.Renderer.Contains("SwiftShader", StringComparison.OrdinalIgnoreCase)) Assert.True(v.SoftwareSuspected);
            }
            else
            {
                Assert.Contains(v.Warnings, w => w.Contains("WebGL is unavailable"));
            }
        }
        finally
        {
            await browser.CloseAsync();
        }
    }

    [Fact]
    public async Task Probes_a_Clearcote_persona()
    {
        // CLEARCOTE_LIVE_ENGINE (add CLEARCOTE_LICENSE_KEY for a PRO build): the verdict reads the persona's
        // renderer, and the claimed GPU check follows its family.
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe)) return;
        var ctx = await Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions
        {
            ExecutablePath = exe, Args = new[] { "--no-sandbox" }, Quiet = true, Fingerprint = "render-coherence-live",
        });
        try
        {
            var page = ctx.Pages.FirstOrDefault() ?? await ctx.NewPageAsync();
            await page.GotoAsync("about:blank");
            var v = await Clearcote.CheckRenderCoherenceAsync(page);
            Assert.True(v.Webgl);
            Assert.NotEqual("", v.Renderer);
            Assert.Equal(v.Coherent, v.Warnings.Count == 0);
            var family = RenderCoherence.GpuFamily(v.Renderer);
            if (family.Length > 0)
            {
                var other = family == "nvidia" ? "AMD Radeon RX 6800" : "NVIDIA GeForce RTX 4090";
                var claimed = await Clearcote.CheckRenderCoherenceAsync(page, other);
                Assert.False(claimed.Coherent);
                Assert.Contains(claimed.Warnings, w => w.Contains("does not match"));
            }
        }
        finally
        {
            await ctx.CloseAsync();
        }
    }
}
