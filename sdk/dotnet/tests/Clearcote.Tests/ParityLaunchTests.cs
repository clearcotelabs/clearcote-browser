using System.Net;
using System.Text;
using Xunit;

namespace Clearcote.Tests;

// Launch-behaviour parity with the Node SDK (test/parity-launch.test.ts): GPU defaults, new
// engine-switch gating, fingerprint pass-through, voices, third-party cookies, transparent proxy,
// release channel, serve as root.
public class ParityLaunchTests : IDisposable
{
    private readonly List<string> _dirs = new();

    public void Dispose()
    {
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    /// A fake engine binary containing exactly these NUL-delimited switch literals.
    private string FakeEngine(IEnumerable<string> switches, int padding = 0)
    {
        var d = TestTemp.Create("cc-fake-engine-");
        _dirs.Add(d);
        var exe = Path.Combine(d, Native.IsWindows ? "chrome.exe" : "chrome");
        var body = new List<byte>(Encoding.Latin1.GetBytes("MZ\0padding\0"));
        body.AddRange(new byte[padding]);
        foreach (var s in switches) body.AddRange(Encoding.Latin1.GetBytes($"\0{s}\0"));
        body.AddRange(Encoding.Latin1.GetBytes("\0end"));
        File.WriteAllBytes(exe, body.ToArray());
        if (Native.IsWindows) File.WriteAllBytes(Path.Combine(d, "chrome.dll"), body.ToArray());
        return exe;
    }

    private static readonly string[] AllGated = LaunchOpts.GatedEngineSwitches.Keys.ToArray();

    // ── GPU launch defaults ──────────────────────────────────────────────────

    [Fact]
    public void Strips_automation_AND_swiftshader_defaults()
        => Assert.Equal(new[] { "--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars" }, LaunchOpts.DefaultIgnoredArgs);

    [Fact]
    public void Gpu_blocklist_when_headed_on_any_os_and_always_on_windows()
    {
        Assert.Equal(new[] { "--ignore-gpu-blocklist" }, LaunchOpts.GpuBlocklistArgs(true, "linux"));
        Assert.Equal(new[] { "--ignore-gpu-blocklist" }, LaunchOpts.GpuBlocklistArgs(true, "windows"));
        Assert.Equal(new[] { "--ignore-gpu-blocklist" }, LaunchOpts.GpuBlocklistArgs(false, "windows"));
        Assert.Empty(LaunchOpts.GpuBlocklistArgs(false, "linux"));
        Assert.Empty(LaunchOpts.GpuBlocklistArgs(true, "linux", new[] { "--ignore-gpu-blocklist" }));
    }

    // GPU backend per claimed platform. A remote-style DISPLAY is trusted without a socket probe and
    // ":4242" has no socket on any test host, so these stay host-independent.
    [Fact]
    public void Gpu_backend_follows_the_claim_on_a_linux_host()
    {
        // Windows claim: SwiftShader, headed and headless (GL clamps VUV to 1024 under a D3D11 label).
        Assert.Equal(new[] { "--use-angle=swiftshader-webgl" }, LaunchOpts.GpuBackendArgs("windows", true, "linux"));
        Assert.Equal(new[] { "--use-angle=swiftshader-webgl" }, LaunchOpts.GpuBackendArgs("windows", false, "linux"));
        // Linux claim: Mesa over EGL in BOTH modes whenever the host has it, display or not. Real Linux
        // Chrome shows the "OpenGL ES 3.2" (EGL) form 4:1 over desktop GL, and gl-egl renders headed under
        // Xvfb too (SC 180), so one backend serves both modes: one WebGL surface, headless or headed.
        foreach (var display in new[] { "remotehost:0", ":4242" })
        {
            Assert.Equal(new[] { "--use-angle=gl-egl", "--ignore-gpu-blocklist" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", null, display, true));
            // headed: GpuBlocklistArgs already carries the override
            Assert.Equal(new[] { "--use-angle=gl-egl" }, LaunchOpts.GpuBackendArgs("linux", true, "linux", null, display, true));
        }
        // No EGL: a reachable X display still gives Mesa (desktop GL, the rarer real form) ...
        Assert.Equal(new[] { "--use-angle=gl", "--ignore-gpu-blocklist" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", null, "remotehost:0", false));
        Assert.Equal(new[] { "--use-angle=gl" }, LaunchOpts.GpuBackendArgs("linux", true, "linux", null, "remotehost:0", false));
        // ... and with neither, nothing (stays on SwiftShader), headless or headed.
        Assert.Empty(LaunchOpts.GpuBackendArgs("linux", false, "linux", null, ":4242", false));
        Assert.Empty(LaunchOpts.GpuBackendArgs("linux", true, "linux", null, ":4242", false));
        Assert.Equal(new[] { "--use-angle=gl-egl" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", new[] { "--ignore-gpu-blocklist" }, ":4242", true));
        Assert.Equal(new[] { "--use-angle=swiftshader-webgl" }, LaunchOpts.GpuBackendArgs("windows", false, "linux", null, ":4242", true));
    }

    [Fact]
    public void Mesa_egl_needs_libegl_the_mesa_vendor_and_a_software_rasterizer()
    {
        var root = TestTemp.Create("cc-egl-");
        _dirs.Add(root);
        string[] Libs(string dir, params string[] names)
        {
            foreach (var n in names)
            {
                var p = Path.Combine(root, dir, n);
                Directory.CreateDirectory(Path.GetDirectoryName(p)!);
                File.WriteAllBytes(p, Array.Empty<byte>());
            }
            return new[] { Path.Combine(root, dir) };
        }
        Assert.True(LaunchOpts.MesaEglAvailable(Libs("debian", "libEGL.so.1", "libEGL_mesa.so.0", "dri/swrast_dri.so")));
        Assert.True(LaunchOpts.MesaEglAvailable(Libs("kms", "libEGL.so.1", "libEGL_mesa.so.0", "dri/kms_swrast_dri.so")));
        // Mesa 24.2+ keeps its drivers in libgallium-<version>.so
        Assert.True(LaunchOpts.MesaEglAvailable(Libs("gallium", "libEGL.so.1", "libEGL_mesa.so.0", "libgallium-24.2.8.so")));
        // libEGL missing (no WebGL at all under gl-egl), no Mesa vendor, no rasterizer
        Assert.False(LaunchOpts.MesaEglAvailable(Libs("nolib", "libEGL_mesa.so.0", "dri/swrast_dri.so")));
        Assert.False(LaunchOpts.MesaEglAvailable(Libs("novendor", "libEGL.so.1", "dri/swrast_dri.so")));
        Assert.False(LaunchOpts.MesaEglAvailable(Libs("nodriver", "libEGL.so.1", "libEGL_mesa.so.0")));
        // the pieces may sit in different directories (multiarch dir + /usr/lib)
        Assert.True(LaunchOpts.MesaEglAvailable(Libs("a", "libEGL.so.1").Concat(Libs("b", "libEGL_mesa.so.0", "dri/swrast_dri.so"))));
        Assert.False(LaunchOpts.MesaEglAvailable(new[] { Path.Combine(root, "missing") }));
    }

    [Fact]
    public void Gpu_backend_never_overrides_the_caller_other_hosts_or_passthrough()
    {
        Assert.Empty(LaunchOpts.GpuBackendArgs("windows", true, "linux", new[] { "--use-angle=vulkan" }));
        // A caller's own backend is kept; headless on a GPU-less host it still needs the blocklist override
        // (measured: without it a headless launch with the caller's gl-egl or gl had no WebGL context at all).
        Assert.Equal(new[] { "--ignore-gpu-blocklist" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", new[] { "--use-gl=egl" }, "remotehost:0"));
        Assert.Equal(new[] { "--ignore-gpu-blocklist" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", new[] { "--use-angle=gl-egl" }, ":4242", true));
        Assert.Equal(new[] { "--ignore-gpu-blocklist" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", new[] { "--use-angle=swiftshader" }, ":4242", true));
        Assert.Empty(LaunchOpts.GpuBackendArgs("linux", false, "linux", new[] { "--use-angle=gl", "--ignore-gpu-blocklist" }, "remotehost:0"));
        Assert.Empty(LaunchOpts.GpuBackendArgs("linux", true, "linux", new[] { "--use-angle=gl" }, "remotehost:0"));   // headed has it already
        Assert.Equal(new[] { "--use-angle=gl" }, LaunchOpts.GpuBackendArgs("linux", false, "linux", new[] { "--ignore-gpu-blocklist" }, "remotehost:0", false));
        Assert.Empty(LaunchOpts.GpuBackendArgs("windows", true, "windows"));
        Assert.Empty(LaunchOpts.GpuBackendArgs("windows", true, "macos"));
        Assert.Empty(LaunchOpts.GpuBackendArgs(null, false, "linux", null, "remotehost:0"));
        Assert.Empty(LaunchOpts.GpuBackendArgs("android", false, "linux", null, "remotehost:0"));
        Assert.False(LaunchOpts.XDisplayAvailable(":abc"));
        Assert.True(LaunchOpts.XDisplayAvailable("remotehost:0"));
    }

    // ── capability probe + gating ────────────────────────────────────────────

    [Fact]
    public void Probe_matches_only_the_NUL_delimited_literal()
    {
        var exe = FakeEngine(new[] { "allow-third-party-cookies-extra", "xtransparent-proxy", "proxy-auth" });
        Assert.True(LaunchOpts.EngineSupportsSwitch(exe, "proxy-auth"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(exe, "allow-third-party-cookies"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(exe, "transparent-proxy"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(null, "proxy-auth"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(Path.Combine(Path.GetTempPath(), "no-such-chrome-" + Guid.NewGuid()), "proxy-auth"));
    }

    [Fact]
    public void Probe_finds_a_literal_straddling_the_8MB_read_boundary()
    {
        // "MZ\0padding\0" is 11 bytes; this puts "\0transparent-proxy\0" across the chunk edge.
        var exe = FakeEngine(new[] { "transparent-proxy" }, padding: 8 * 1024 * 1024 - 11 - 6);
        Assert.True(LaunchOpts.EngineSupportsSwitch(exe, "transparent-proxy"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(exe, "allow-third-party-cookies"));
    }

    [Fact]
    public void Gate_keeps_every_new_switch_on_an_engine_that_implements_them()
    {
        var exe = FakeEngine(AllGated.Select(s => s[2..]));
        using var err = new StderrCapture();
        var (args, warnings) = LaunchOpts.GateEngineSwitches(exe, new[] { "--foo" }.Concat(AllGated), quiet: false);
        Assert.Equal(new[] { "--foo" }.Concat(AllGated), args);
        Assert.Empty(warnings);
        Assert.Equal("", err.Text);
    }

    [Fact]
    public void Gate_drops_each_unsupported_switch_with_a_warning_keeping_everything_else()
    {
        var exe = FakeEngine(new[] { "proxy-auth" });
        using var err = new StderrCapture();
        var (args, warnings) = LaunchOpts.GateEngineSwitches(exe,
            new[] { "--foo=1", "--allow-third-party-cookies", "--transparent-proxy", "--disable-fingerprint-voices", "--fingerprint-passthrough" }, quiet: false);
        Assert.Equal(new[] { "--foo=1" }, args);
        Assert.Equal(4, warnings.Count);
        Assert.Contains("clearcote: AllowThirdPartyCookies = true needs engine 152 r22 or newer; this engine ignores it, so it was not applied.", warnings);
        Assert.All(warnings, w => Assert.EndsWith("needs engine 152 r22 or newer; this engine ignores it, so it was not applied.", w));
        Assert.Equal(4, err.Text.Split('\n', StringSplitOptions.RemoveEmptyEntries).Length);
    }

    [Fact]
    public void Gate_is_silent_under_quiet_but_still_reports()
    {
        var exe = FakeEngine(Array.Empty<string>());
        using var err = new StderrCapture();
        var (args, warnings) = LaunchOpts.GateEngineSwitches(exe, new[] { "--transparent-proxy" }, quiet: true);
        Assert.Empty(args);
        Assert.Single(warnings);
        Assert.Equal("", err.Text);
    }

    // ── engine extras ────────────────────────────────────────────────────────

    [Fact]
    public void AllowThirdPartyCookies_emits_its_switch()
    {
        Assert.Equal(new[] { "--allow-third-party-cookies" }, LaunchOpts.EngineExtrasArgs(true, null, null));
        Assert.Empty(LaunchOpts.EngineExtrasArgs(false, null, null));
        Assert.Empty(LaunchOpts.EngineExtrasArgs(null, null, null));
    }

    [Fact]
    public void TransparentProxy_needs_a_proxy_else_dropped_with_a_note()
    {
        using var err = new StderrCapture();
        Assert.Equal(new[] { "--transparent-proxy" },
            LaunchOpts.EngineExtrasArgs(null, true, new ProxyOptions { Server = "http://p:8080" }));
        Assert.Equal("", err.Text);
        Assert.Empty(LaunchOpts.EngineExtrasArgs(null, true, null));
        Assert.Contains("clearcote: transparentProxy has no effect without a proxy; ignored.", err.Text);
    }

    [Fact]
    public void AssembleArgs_adds_extras_and_gates_them_on_the_engine_that_runs()
    {
        var userArgs = new[] { "--allow-third-party-cookies" };  // gated wherever it came from
        var proxy = new ProxyOptions { Server = "http://127.0.0.1:3128" };
        var newExe = FakeEngine(AllGated.Select(s => s[2..]));
        var oldExe = FakeEngine(new[] { "proxy-auth" });
        using var err = new StderrCapture();

        var onNew = Clearcote.AssembleArgs(Fingerprint.Args(new FingerprintOptions { Fingerprint = "off" }), new(), new(), null, null,
            userArgs, proxy, false, new Clearcote.EngineExtras(newExe, true, true, true, true));
        Assert.Contains("--fingerprint-passthrough", onNew);
        Assert.Contains("--transparent-proxy", onNew);
        Assert.Contains("--ignore-gpu-blocklist", onNew);
        Assert.Contains("--allow-third-party-cookies", onNew);

        var onOld = Clearcote.AssembleArgs(Fingerprint.Args(new FingerprintOptions { Fingerprint = "off" }), new(), new(), null, null,
            userArgs, proxy, false, new Clearcote.EngineExtras(oldExe, false, true, true, true));
        Assert.DoesNotContain(onOld, a => AllGated.Contains(a.Split('=')[0]));
        Assert.DoesNotContain(onOld, a => a.StartsWith("--fingerprint"));
        Assert.Contains("--disable-quic", onOld);
        Assert.Equal(Native.IsWindows, onOld.Contains("--ignore-gpu-blocklist"));

        // Without extras (legacy call shape) nothing is added or gated.
        var legacy = Clearcote.AssembleArgs(new(), new(), new(), null, null, userArgs, null, false);
        Assert.Contains("--allow-third-party-cookies", legacy);
        Assert.DoesNotContain("--ignore-gpu-blocklist", legacy);
    }

    // ── Playwright's --disable-features (2026-10-06) ─────────────────────────
    // Playwright disables ThirdPartyStoragePartitioning. Measured on r30 vs genuine Chrome 154: a
    // cross-site iframe then read the top-level site's storage, or got a SecurityError from
    // localStorage with third-party cookies blocked; genuine gives it an empty partition either way.

    private static string[] OnlyDisableFeatures(List<string> args)
    {
        var found = args.Where(a => a.StartsWith("--disable-features=", StringComparison.Ordinal)).ToList();
        Assert.Single(found);
        return found[0]["--disable-features=".Length..].Split(',');
    }

    [Theory]
    [InlineData("windows")]
    [InlineData("linux")]
    public void Launch_replaces_playwrights_list_without_partitioning(string platform)
    {
        var exe = FakeEngine(AllGated.Select(s => s[2..]));
        var args = Clearcote.AssembleArgs(Fingerprint.Args(new FingerprintOptions { Fingerprint = "s1", Platform = platform }),
            new(), new(), null, null, Array.Empty<string>(), null, false,
            new Clearcote.EngineExtras(exe, false, true, null, null, ViaPlaywright: true));
        var feats = OnlyDisableFeatures(args);
        foreach (var f in new[] { "ThirdPartyStoragePartitioning", "AcceptCHFrame", "HttpsUpgrades", "LazyFrameLoading" })
            Assert.DoesNotContain(f, feats);  // what a page or a server can observe stays as in genuine Chrome
        foreach (var f in new[] { "MediaRouter", "Translate", "PaintHolding" }) Assert.Contains(f, feats);
        Assert.Equal(platform == "linux", feats.Contains("WebBluetooth"));
    }

    [Fact]
    public void Override_re_enables_only_what_a_site_can_observe_and_needs_the_exact_switch_to_skip()
    {
        var pw = new[] { "AcceptCHFrame", "LazyFrameLoading", "MediaRouter", "ThirdPartyStoragePartitioning", "Translate" };
        Assert.Equal(new[] { "--disable-features=MediaRouter,Translate" },
            LaunchOpts.PlaywrightFeatureOverrideArgs(null, pw, screenshotSurface: false));
        Assert.Empty(LaunchOpts.PlaywrightFeatureOverrideArgs(new[] { "--disable-features=" + string.Join(",", pw) }, pw, false));
        // Playwright drops only an EXACT match, so a different value still needs the replacement.
        Assert.NotEmpty(LaunchOpts.PlaywrightFeatureOverrideArgs(new[] { "--disable-features=MediaRouter" }, pw, false));
        Assert.Equal(new[] { "--disable-features=MediaRouter", "--enable-features=CDPScreenshotNewSurface" },
            LaunchOpts.PlaywrightFeatureOverrideArgs(null, new[] { "MediaRouter" }, true));
    }

    [Fact]
    public void Dropping_the_disable_switch_keeps_the_screenshot_switch_and_parse_is_robust()
    {
        var pw = new[] { "MediaRouter", "Translate" };
        Assert.Equal(new[] { "--enable-features=CDPScreenshotNewSurface" },
            LaunchOpts.PlaywrightFeatureOverrideArgs(new[] { "--disable-features=MediaRouter,Translate" }, pw, true));
        Assert.Equal(new[] { "--disable-features=MediaRouter,Translate" },
            LaunchOpts.PlaywrightFeatureOverrideArgs(new[] { "--enable-features=CDPScreenshotNewSurface" }, pw, true));
        Assert.Equal(new[] { "MediaRouter", "Translate" }, LaunchOpts.ParsePlaywrightDisabledFeatures(
            "const disabledFeatures = [\n // see ] here\n \"MediaRouter\",\n \"Translate\"\n];"));
        Assert.Null(LaunchOpts.ParsePlaywrightDisabledFeatures("disabledFeatures = [\"SomethingElse\"];"));
        Assert.Null(LaunchOpts.ParsePlaywrightDisabledFeatures("const disabledFeatures2 = [\"MediaRouter\"];"));
    }

    [Fact]
    public void Parses_every_Playwright_release_shape_and_reads_the_installed_driver()
    {
        Assert.Equal(new[] { "AcceptCHFrame", "MediaRouter" }, LaunchOpts.ParsePlaywrightDisabledFeatures(
            "const disabledFeatures = (assistantMode) => [\n  // c\n  \"AcceptCHFrame\",\n  \"MediaRouter\",\n  assistantMode ? \"AutomationControlled\" : \"\"\n].filter(Boolean);"));
        Assert.Equal(new[] { "LazyFrameLoading", "MediaRouter" }, LaunchOpts.ParsePlaywrightDisabledFeatures(
            "['--disable-features=LazyFrameLoading,MediaRouter', '--no-first-run']"));
        Assert.Null(LaunchOpts.ParsePlaywrightDisabledFeatures(""));
        // Drift guard: the driver this test project ships must be readable.
        var installed = LaunchOpts.InstalledPlaywrightDisabledFeatures();
        Assert.NotNull(installed);
        Assert.Contains("MediaRouter", installed!);
    }

    [Fact]
    public void No_copy_of_playwrights_list_for_serve_or_when_the_caller_dropped_it()
    {
        var exe = FakeEngine(AllGated.Select(s => s[2..]));
        var fp = Fingerprint.Args(new FingerprintOptions { Fingerprint = "s1", Platform = "windows" });
        var served = Clearcote.AssembleArgs(fp, new(), new(), null, null, Array.Empty<string>(), null, false,
            new Clearcote.EngineExtras(exe, false, true, null, null, ViaPlaywright: false));
        Assert.DoesNotContain(served, a => a.Contains("MediaRouter") || a.Contains("ThirdPartyStoragePartitioning"));
        var exact = "--disable-features=" + string.Join(",", LaunchOpts.InstalledPlaywrightDisabledFeatures() ?? LaunchOpts.PlaywrightDisabledFeatures);
        var dropped = Clearcote.AssembleArgs(fp, new(), new(), null, null, Array.Empty<string>(), null, false,
            new Clearcote.EngineExtras(exe, false, true, null, null, ViaPlaywright: true, IgnoreDefaultArgs: new[] { exact }));
        Assert.DoesNotContain(dropped, a => a.Contains("MediaRouter"));
    }

    // ── fingerprint pass-through ─────────────────────────────────────────────

    [Theory]
    [InlineData("off")] [InlineData("OFF")] [InlineData("Off")] [InlineData(" off ")]
    public void Recognises_passthrough(string v) => Assert.True(Fingerprint.IsFingerprintPassthrough(v));

    [Theory]
    [InlineData("seed-1")] [InlineData("offline")] [InlineData("0x1")] [InlineData("")] [InlineData("1")] [InlineData(null)]
    // Ordinary seeds, not pass-through: only "off" is.
    [InlineData("0")] [InlineData("no")] [InlineData("false")] [InlineData("disable")] [InlineData("disabled")]
    public void Does_not_treat_as_passthrough(string? v) => Assert.False(Fingerprint.IsFingerprintPassthrough(v));

    [Fact]
    public void Former_passthrough_words_are_plain_seeds_again()
    {
        var args = Fingerprint.Args(new FingerprintOptions { Fingerprint = "0" });
        Assert.Contains("--fingerprint=0", args);
        Assert.DoesNotContain("--fingerprint-passthrough", args);
    }

    [Fact]
    public void Passthrough_emits_no_persona_switches_and_never_fingerprint_off()
    {
        var args = Fingerprint.Args(new FingerprintOptions
        {
            Fingerprint = "off", Platform = "windows", Brand = "Edge", GpuVendor = "X", LightStealth = true, FingerprintVoices = false,
        });
        Assert.Equal(new[] { "--fingerprint-passthrough" }, args);
        Assert.DoesNotContain(args, a => a.StartsWith("--fingerprint="));
    }

    [Fact]
    public void Passthrough_keeps_only_explicit_locale_and_network_values()
    {
        Assert.Equal(new[]
        {
            "--fingerprint-passthrough", "--timezone=Europe/Berlin", "--accept-lang=de-DE,de", "--lang=de", "--webrtc-ip=1.2.3.4",
        }, Fingerprint.Args(new FingerprintOptions
        {
            Fingerprint = "off", Timezone = "Europe/Berlin", AcceptLanguage = "de-DE,de;q=0.9", WebrtcIp = "1.2.3.4",
        }));
        var bare = string.Join(" ", Fingerprint.Args(new FingerprintOptions { Fingerprint = "off" }));
        Assert.DoesNotMatch("accept-lang|timezone|fingerprint-platform|fingerprint-brand", bare);
    }

    // ── voices ───────────────────────────────────────────────────────────────

    [Fact]
    public void FingerprintVoices_false_emits_its_switch_unset_or_true_does_not()
    {
        Assert.Contains("--disable-fingerprint-voices", Fingerprint.Args(new FingerprintOptions { Fingerprint = "s", FingerprintVoices = false }));
        Assert.DoesNotContain("--disable-fingerprint-voices", Fingerprint.Args(new FingerprintOptions { Fingerprint = "s", FingerprintVoices = true }));
        Assert.DoesNotContain("--disable-fingerprint-voices", Fingerprint.Args(new FingerprintOptions { Fingerprint = "s" }));
    }

    // ── release channel ──────────────────────────────────────────────────────

    [Fact]
    public void Release_channel_defaults_to_stable_option_beats_env_and_typos_throw()
    {
        using var s = new Sandbox().Env("CLEARCOTE_RELEASE_CHANNEL", null);
        Assert.Equal(ReleaseChannel.Stable, Download.ResolveReleaseChannel());
        s.Env("CLEARCOTE_RELEASE_CHANNEL", "preview");
        Assert.Equal(ReleaseChannel.Preview, Download.ResolveReleaseChannel());
        Assert.Equal(ReleaseChannel.Stable, Download.ResolveReleaseChannel(ReleaseChannel.Stable));
        Assert.Equal(ReleaseChannel.Preview, Download.ParseReleaseChannel(" Preview "));
        s.Env("CLEARCOTE_RELEASE_CHANNEL", "beta");
        var e = Assert.Throws<ArgumentException>(() => Download.ResolveReleaseChannel());
        Assert.Contains("Unknown release channel 'beta'", e.Message);
        Assert.Throws<ArgumentException>(() => Download.ResolveReleaseChannel((ReleaseChannel)7));
    }

    [Fact]
    public void Channel_preview_is_appended_to_the_pro_url_only_for_preview()
    {
        Assert.Equal("https://x.test/api/v1/download/pro?platform=linux", Download.ProDownloadUrl("https://x.test/", "linux"));
        Assert.Equal("https://x.test/api/v1/download/pro?platform=windows&version=152",
            Download.ProDownloadUrl("https://x.test", "windows", "152", ReleaseChannel.Stable));
        Assert.Equal("https://x.test/api/v1/download/pro?platform=windows&version=152.0.7977.82-r21&channel=preview",
            Download.ProDownloadUrl("https://x.test", "windows", "152.0.7977.82-r21", ReleaseChannel.Preview));
    }

    [Fact]
    public async Task ProEnsureBinary_sends_the_channel_from_the_option_or_env()
    {
        var fake = FakeHandler.Json(HttpStatusCode.OK, "{}");  // no url -> "not available" after the request
        using var s = new Sandbox().Http(fake).Env("CLEARCOTE_RELEASE_CHANNEL", null);
        await Assert.ThrowsAnyAsync<Exception>(() => Download.ProEnsureBinaryAsync("k", new ProDownloadOptions { ApiBase = "http://t.local", ReleaseChannel = ReleaseChannel.Preview, Quiet = true }));
        await Assert.ThrowsAnyAsync<Exception>(() => Download.ProEnsureBinaryAsync("k", new ProDownloadOptions { ApiBase = "http://t.local", Quiet = true }));
        s.Env("CLEARCOTE_RELEASE_CHANNEL", "preview");
        await Assert.ThrowsAnyAsync<Exception>(() => Download.ProEnsureBinaryAsync("k", new ProDownloadOptions { ApiBase = "http://t.local", Quiet = true }));
        s.Env("CLEARCOTE_RELEASE_CHANNEL", "nightly");
        await Assert.ThrowsAsync<ArgumentException>(() => Download.ProEnsureBinaryAsync("k", new ProDownloadOptions { ApiBase = "http://t.local", Quiet = true }));
        var urls = fake.Requests.Select(r => r.RequestUri!.Query).ToList();
        Assert.Equal(3, urls.Count);  // the typo threw before any request
        Assert.EndsWith("&channel=preview", urls[0]);
        Assert.DoesNotContain("channel", urls[1]);
        Assert.EndsWith("&channel=preview", urls[2]);
    }

    [Fact]
    public async Task Channel_is_honoured_on_the_pinned_version_path_too()
    {
        var fake = FakeHandler.Json(HttpStatusCode.OK, "{}");
        using var s = new Sandbox().Http(fake).Env("CLEARCOTE_RELEASE_CHANNEL", null).Env("CLEARCOTE_BINARY", null)
            .Env("CLEARCOTE_BROWSER_VERSION", null).Env("CLEARCOTE_LICENSE_KEY", null);
        s.TempHome();
        // PRO revision pin: straight to the PRO route, which must carry the channel.
        await Assert.ThrowsAnyAsync<Exception>(() => Download.EnsureVersionAsync("r7", "k", "http://t.local", quiet: true, releaseChannel: ReleaseChannel.Preview));
        // Through the launch entry point with a pinned Version and the channel from the env.
        s.Env("CLEARCOTE_RELEASE_CHANNEL", "preview");
        await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ExecutablePathAsync(new LaunchOptions
        {
            Version = "152.0.7977.82-r21", LicenseKey = "k", LicenseApiBase = "http://t.local", Quiet = true,
        }));
        var queries = fake.Requests.Select(r => r.RequestUri!.Query).ToList();
        Assert.Equal(2, queries.Count);
        Assert.All(queries, q => Assert.EndsWith("&channel=preview", q));
        Assert.Contains("version=r7", queries[0]);
        Assert.Contains("version=152.0.7977.82-r21", queries[1]);

        // A typo is rejected on every download path, before any request — free, pinned or PRO.
        s.Env("CLEARCOTE_RELEASE_CHANNEL", "beta");
        await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.ExecutablePathAsync(new LaunchOptions { Quiet = true }));
        await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.ExecutablePathAsync(new LaunchOptions { Version = "150", Quiet = true }));
        await Assert.ThrowsAsync<ArgumentException>(() => Download.EnsureVersionAsync("r7", "k", "http://t.local", quiet: true));
        Assert.Equal(2, fake.Requests.Count);
    }

    // ── serve as root ────────────────────────────────────────────────────────

    [Fact]
    public void Serve_adds_no_sandbox_only_for_root_on_linux_and_never_twice()
    {
        Assert.True(LaunchOpts.ServeNeedsNoSandbox("linux", 0, Array.Empty<string>()));
        Assert.False(LaunchOpts.ServeNeedsNoSandbox("linux", 1000, Array.Empty<string>()));
        Assert.False(LaunchOpts.ServeNeedsNoSandbox("linux", 0, new[] { "--no-sandbox" }));
        Assert.False(LaunchOpts.ServeNeedsNoSandbox("windows", null, Array.Empty<string>()));
        if (!Native.IsWindows) Assert.NotNull(LaunchOpts.EffectiveUid());
    }

    // ── serve: the "unsupported command-line flag" infobar ───────────────────

    [Fact]
    public void Serve_headless_always_disables_infobars_and_never_twice()
    {
        Assert.Equal(new[] { "--disable-infobars" }, LaunchOpts.ServeInfobarArgs(true, Array.Empty<string>()));
        Assert.Equal(new[] { "--disable-infobars" }, LaunchOpts.ServeInfobarArgs(true, new[] { "--no-sandbox" }));
        Assert.Empty(LaunchOpts.ServeInfobarArgs(true, new[] { "--disable-infobars" }));
    }

    [Fact]
    public void Serve_headed_adds_test_type_only_with_no_sandbox_and_never_twice()
    {
        Assert.Empty(LaunchOpts.ServeInfobarArgs(false, Array.Empty<string>()));
        Assert.Empty(LaunchOpts.ServeInfobarArgs(false, new[] { "--ignore-certificate-errors" }));
        Assert.Equal(new[] { "--test-type" }, LaunchOpts.ServeInfobarArgs(false, new[] { "--no-sandbox" }));
        Assert.Empty(LaunchOpts.ServeInfobarArgs(false, new[] { "--no-sandbox", "--test-type=browser" }));
    }
}
