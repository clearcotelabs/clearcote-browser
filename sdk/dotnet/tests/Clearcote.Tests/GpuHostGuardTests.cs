using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>The GPU vendor guard: the GPU a persona declares against the one this machine renders on
/// (PersonaToken.GpuHostMismatch), and the vendor filter of LocalProfile = "auto" (ProfileLib.SameGpuVendor).
/// The same cases as sdk/node/test/gpu-host-guard.test.ts and sdk/python/tests/test_gpu_host_guard.py.</summary>
public sealed class GpuHostGuardTests : IDisposable
{
    private static readonly (string Vendor, string Renderer) Amd =
        ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)");
    private static readonly (string Vendor, string Renderer) Nv =
        ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 (0x00002184) Direct3D11 vs_5_0 ps_5_0, D3D11)");
    private static readonly (string Vendor, string Renderer) Intel =
        ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x00004680) Direct3D11 vs_5_0 ps_5_0, D3D11)");
    private static readonly (string Vendor, string Renderer) Swift =
        ("Google Inc. (Google)", "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)");
    private static readonly Func<string?, string, bool> R51 = (_, name) => name == "cc-persona-host-facts";

    private readonly Sandbox _sb = new();
    private readonly string _dir;
    private readonly string _exe;

    public GpuHostGuardTests()
    {
        _sb.TempHome();
        _sb.Env("CLEARCOTE_NO_WARN", null).Env(PersonaToken.GpuCheckEnv, null);
        _dir = Path.Combine(Path.GetTempPath(), "cc-gpuguard-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_dir);
        _exe = Path.Combine(_dir, "chrome");
        File.WriteAllText(_exe, "x");
        PersonaToken.ClearCaches();
        LaunchWarnings.ForgetSaid();
    }

    public void Dispose()
    {
        PersonaToken.ClearCaches();
        LaunchWarnings.ForgetSaid();
        try { Directory.Delete(_dir, true); } catch { /* best effort */ }
        _sb.Dispose();
    }

    private static Dictionary<string, string> Host(string v) => new() { ["gpu_vendor"] = v };

    /// A persona token shaped like the service's: base64url(payload JSON) + "." + signature; payload.p = codec JSON.
    private static string Token((string Vendor, string Renderer)? gpu)
    {
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        var payload = new JsonObject { ["v"] = 1, ["iat"] = now, ["nbf"] = now - 30, ["exp"] = now + 3600 };
        payload["p"] = gpu is { } g
            ? new JsonObject { ["codec"] = 1, ["persona"] = new JsonObject { ["webgl_vendor"] = g.Vendor, ["webgl_renderer"] = g.Renderer } }
            : new JsonObject();
        var body = Convert.ToBase64String(Encoding.UTF8.GetBytes(payload.ToJsonString())).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        return body + ".c2ln";
    }

    [Fact]
    public void PersonaGpuFromToken_reads_the_WebGL_vendor_and_renderer()
    {
        Assert.Equal(Amd, PersonaToken.PersonaGpuFromToken(Token(Amd)));
        Assert.Null(PersonaToken.PersonaGpuFromToken(Token(null)));
        Assert.Null(PersonaToken.PersonaGpuFromToken("tok-abc"));
        Assert.Null(PersonaToken.PersonaGpuFromToken(""));
    }

    [Fact]
    public void GpuSource_names_the_options_an_imported_profile_or_the_engine()
    {
        Assert.Equal("options", PersonaToken.GpuSource(new Dictionary<string, string> { ["fingerprint-gpu-vendor"] = "x" }, null));
        Assert.Equal("options", PersonaToken.GpuSource(new Dictionary<string, string> { ["fingerprint-gpu-renderer"] = "x" }, null));
        Assert.Equal("profile", PersonaToken.GpuSource(new Dictionary<string, string> { ["fingerprint-profile-id"] = "p1" }, null));
        Assert.Equal("profile", PersonaToken.GpuSource(new Dictionary<string, string>(),
            "{\"webgl\":{\"webgl1\":{\"debug\":{\"UNMASKED_RENDERER_WEBGL\":\"x\"}}}}"));
        // a profile without GPU strings (fonts and screen only) leaves the GPU to the engine, as the engine does
        Assert.Equal("engine", PersonaToken.GpuSource(new Dictionary<string, string>(), "{\"screen\":{\"width\":1920}}"));
        Assert.Equal("engine", PersonaToken.GpuSource(new Dictionary<string, string> { ["fingerprint"] = "7" }, null));
    }

    [Fact]
    public void A_declared_vendor_that_is_not_the_hosts_is_flagged_with_its_source_and_fix()
    {
        var m = PersonaToken.GpuHostMismatch(Amd, Host("nvidia"), new[] { "--fingerprint=1" }, "options")!.Value;
        Assert.Equal("gpu-vendor-host:amd:nvidia", m.Code);
        foreach (var part in new[] { "GpuVendor / GpuRenderer declare a AMD GPU", "this machine renders on NVIDIA", "card that computes",
                     "Remove GpuVendor / GpuRenderer", PersonaToken.GpuCheckEnv + "=1" })
            Assert.Contains(part, m.Message);
        Assert.Contains("Use a profile captured on a machine with the same GPU vendor",
            PersonaToken.GpuHostMismatch(Intel, Host("nvidia"), Array.Empty<string>(), "profile")!.Value.Message);
        var auto = PersonaToken.GpuHostMismatch(Intel, Host("amd"), Array.Empty<string>(), "engine")!.Value;
        Assert.Equal("gpu-vendor-host:intel:amd", auto.Code);
        Assert.Contains("GpuStringSpoof = false", auto.Message);
    }

    [Fact]
    public void The_same_vendor_passes_whatever_the_model()
    {
        Assert.Null(PersonaToken.GpuHostMismatch(Nv, Host("nvidia"), Array.Empty<string>(), "options"));
        Assert.Null(PersonaToken.GpuHostMismatch(Intel, Host("intel"), Array.Empty<string>(), "engine"));
        Assert.Null(PersonaToken.GpuHostMismatch(Amd, Host("amd"), Array.Empty<string>(), "profile"));
    }

    [Fact]
    public void A_graphics_cards_name_on_a_software_host_is_flagged_and_a_software_renderer_is_not()
    {
        var m = PersonaToken.GpuHostMismatch(Intel, Host("none"), Array.Empty<string>(), "engine")!.Value;
        Assert.Equal("gpu-software-host:intel", m.Code);
        Assert.Contains("renders WebGL in software", m.Message);
        Assert.Contains("GpuStringSpoof = false", m.Message);
        Assert.Contains("GpuVendor / GpuRenderer declare a NVIDIA GPU",
            PersonaToken.GpuHostMismatch(Nv, Host("none"), Array.Empty<string>(), "options")!.Value.Message);
        Assert.Null(PersonaToken.GpuHostMismatch(Swift, Host("none"), Array.Empty<string>(), "engine"));
        Assert.Null(PersonaToken.GpuHostMismatch(Swift, Host("nvidia"), Array.Empty<string>(), "options"));
    }

    [Theory]
    [InlineData("--canvas-bridge-url=ws://x:1")]
    [InlineData("--use-adapter-luid=1,2")]
    [InlineData("--disable-gpu-string-spoof")]
    [InlineData("--disable-gpu-fingerprint")]
    [InlineData("--fingerprint-passthrough")]
    public void Launches_that_show_the_real_GPU_or_render_elsewhere_are_skipped(string sw)
    {
        Assert.Null(PersonaToken.GpuHostMismatch(Amd, Host("nvidia"), new[] { sw }, "options"));
        Assert.Null(PersonaToken.GpuHostMismatch(Intel, Host("none"), new[] { sw }, "engine"));
    }

    [Fact]
    public void An_unknown_host_vendor_or_an_unreadable_persona_says_nothing()
    {
        Assert.Null(PersonaToken.GpuHostMismatch(Amd, Host(""), Array.Empty<string>(), "options"));
        Assert.Null(PersonaToken.GpuHostMismatch(Amd, Host("virtio"), Array.Empty<string>(), "options"));
        Assert.Null(PersonaToken.GpuHostMismatch(null, Host("nvidia"), Array.Empty<string>(), "options"));
    }

    private static readonly Dictionary<string, string> Sel = new() { ["fingerprint-gpu-vendor"] = Amd.Vendor, ["fingerprint-gpu-renderer"] = Amd.Renderer };

    [Fact]
    public void Warns_once_per_process_and_quiet_no_warn_and_the_opt_out_silence_it()
    {
        using (var err = new StderrCapture())
        {
            PersonaToken.WarnGpuHostMismatch(Token(Amd), Host("nvidia"), Array.Empty<string>(), Sel, null, quiet: false);
            PersonaToken.WarnGpuHostMismatch(Token(Amd), Host("nvidia"), Array.Empty<string>(), Sel, null, quiet: false);
            Assert.Single(System.Text.RegularExpressions.Regex.Matches(err.Text, "clearcote: warning: GpuVendor / GpuRenderer declare a AMD GPU"));
        }
        LaunchWarnings.ForgetSaid();
        using (var err = new StderrCapture())
        {
            PersonaToken.WarnGpuHostMismatch(Token(Amd), Host("nvidia"), Array.Empty<string>(), Sel, null, quiet: true);
            _sb.Env(PersonaToken.GpuCheckEnv, "1");
            PersonaToken.WarnGpuHostMismatch(Token(Amd), Host("nvidia"), Array.Empty<string>(), Sel, null, quiet: false);
            _sb.Env(PersonaToken.GpuCheckEnv, null).Env("CLEARCOTE_NO_WARN", "1");
            PersonaToken.WarnGpuHostMismatch(Token(Amd), Host("nvidia"), Array.Empty<string>(), Sel, null, quiet: false);
            Assert.Equal("", err.Text);
        }
    }

    private static LeaseSession Lease() => new(() => "hdr.sig", "L1", () => Task.CompletedTask, () => throw new InvalidOperationException());

    private Task<PersonaToken.Plan> Prepare(string[] args, string vendor, (string Vendor, string Renderer) gpu, bool quiet = false)
        => PersonaToken.PrepareAsync(_exe, args, Lease(), null, R51,
            (_, _) => $"{{\"gpu_vendor\":\"{vendor}\"}}\n",
            (_, _, _, _, _) => Task.FromResult((Token(gpu), (double)DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600)),
            quiet: quiet);

    [Fact]
    public async Task PrepareAsync_warns_for_GpuVendor_AMD_on_an_NVIDIA_host_and_not_for_the_hosts_vendor()
    {
        using var err = new StderrCapture();
        await Prepare(new[] { "--fingerprint=1", "--fingerprint-gpu-vendor=" + Amd.Vendor, "--fingerprint-gpu-renderer=" + Amd.Renderer }, "nvidia", Amd);
        await Prepare(new[] { "--fingerprint=2" }, "nvidia", Nv);
        Assert.Contains("GpuVendor / GpuRenderer declare a AMD GPU", err.Text);
        Assert.Contains("this machine renders on NVIDIA", err.Text);
        Assert.Single(System.Text.RegularExpressions.Regex.Matches(err.Text, "clearcote: warning:"));
    }

    [Fact]
    public async Task PrepareAsync_warns_for_an_imported_profile_of_another_vendor_and_says_nothing_when_quiet()
    {
        var profile = "{\"webgl\":{\"webgl1\":{\"debug\":{\"UNMASKED_VENDOR_WEBGL\":\"" + Intel.Vendor + "\",\"UNMASKED_RENDERER_WEBGL\":\"" + Intel.Renderer + "\"}}}}";
        using (var err = new StderrCapture())
        {
            await Prepare(new[] { "--fingerprint-profile=" + Fingerprint.EncodeProfile(profile) }, "nvidia", Intel, quiet: true);
            Assert.Equal("", err.Text);
        }
        using (var err = new StderrCapture())
        {
            await Prepare(new[] { "--fingerprint-profile=" + Fingerprint.EncodeProfile(profile) }, "nvidia", Intel);
            Assert.Contains("the imported profile declares a Intel GPU", err.Text);
        }
    }

    private static ProfileIndexEntry Entry(string id, string vendor, int screen = 1920) => new()
        { Id = id, OsFamily = "windows", BrowserMajor = 158, GpuVendor = vendor, ScreenWidth = screen, ScreenHeight = 1080 };
    private static HostFacts HostOf(string vendor) => new()
        { OsFamily = "windows", BrowserMajor = 158, GpuVendor = vendor, ScreenWidth = 1920, ScreenHeight = 1080 };

    [Fact]
    public void SameGpuVendor_keeps_only_the_host_vendors_candidates_when_there_are_any()
    {
        var pool = new List<ProfileIndexEntry> { Entry("a", "amd"), Entry("n1", "nvidia"), Entry("n2", "nvidia"), Entry("i", "intel") };
        Assert.Equal(new[] { "n1", "n2" }, ProfileLib.SameGpuVendor(pool, HostOf("nvidia")).Select(e => e.Id));
        Assert.Equal(new[] { "a" }, ProfileLib.SameGpuVendor(pool, HostOf("amd")).Select(e => e.Id));
        Assert.Equal(new[] { "i" }, ProfileLib.SameGpuVendor(new List<ProfileIndexEntry> { Entry("i", "intel") }, HostOf("amd")).Select(e => e.Id));
        Assert.Equal(4, ProfileLib.SameGpuVendor(pool, HostOf("software")).Count);
        Assert.Equal(4, ProfileLib.SameGpuVendor(pool, HostOf("unknown")).Count);
    }

    [Fact]
    public void SelectProfile_never_picks_another_vendor_when_the_index_has_the_hosts()
    {
        var pool = new List<ProfileIndexEntry> { Entry("n", "nvidia", 1280) };
        pool.AddRange(Enumerable.Range(0, 30).Select(i => Entry($"i{i}", "intel")));
        foreach (var key in new[] { "k1", "k2", "k3", "k4", "k5" })
            Assert.Equal("n", ProfileLib.SelectProfile(pool, HostOf("nvidia"), key: key).Entry.Id);
    }

    [Fact]
    public async Task MeasureHost_probe_reads_the_real_GPU_strings()
    {
        LaunchOptions? seen = null;
        await Assert.ThrowsAsync<Microsoft.Playwright.PlaywrightException>(() =>
            ProfileSource.MeasureHostAsync(o => { seen = o; throw new Microsoft.Playwright.PlaywrightException("stop here"); }, _exe, 158));
        Assert.False(seen!.GpuStringSpoof);
    }
}
