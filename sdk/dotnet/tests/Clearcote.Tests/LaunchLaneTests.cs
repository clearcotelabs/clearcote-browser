using System.Text;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Launch parity with the Node SDK (the reference) and the Python one: http(s) proxy credentials on the engine's
/// --proxy-auth, the unsupported-engine-option warnings, the coherence warnings and once-per-process notes,
/// profile portability, the Playwright pass-through options, DownloadAsync's routing, the cached-build listing,
/// and the guard that refuses only a binary this OS has no build for.
/// </summary>
/// <remarks>
/// The launch tests run the real LaunchAsync / LaunchPersistentContextAsync / ServeAsync against a stand-in engine
/// that writes its command line next to itself and exits (as StockRuntimeTests does): the launch then fails, which
/// is all they need. The stand-in is a shell script, so those tests are POSIX-only.
/// </remarks>
public class LaunchLaneTests : IDisposable
{
    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();
    private readonly TextWriter _savedErr = Console.Error;
    private readonly StringWriter _err = new();

    public LaunchLaneTests()
    {
        _sb.TempHome();
        var temp = Dir("cc-ll-tmp-");
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_NO_WARN", null).Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_DOCKER", null)
            .Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null).Env("CLEARCOTE_RELEASE_CHANNEL", null)
            .Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_CACHE", null).Env(PersonaEnv.EnvVar, null)
            .Env("CLEARCOTE_STOCK_RUNTIME", null).Env("CLEARCOTE_RUNTIME_BINDINGS", null)
            .Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);
        LaunchWarnings.ForgetSaid();
        Console.SetError(TextWriter.Synchronized(_err));
    }

    public void Dispose()
    {
        Console.SetError(_savedErr);
        LaunchWarnings.ForgetSaid();
        Native.ArchOverride = null;
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    /// A file that claims the given engine switches the way a real binary's string table does.
    private string FakeEngine(params string[] switches)
    {
        var exe = Path.Combine(Dir("cc-ll-engine-"), "chrome");
        var body = new List<byte>(Encoding.ASCII.GetBytes("#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\nexit 1\n"));
        foreach (var s in switches) body.AddRange(Encoding.Latin1.GetBytes($"\0{s}\0"));
        body.Add((byte)'\n');
        File.WriteAllBytes(exe, body.ToArray());
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    /// Launch the stand-in through <paramref name="entry"/>; returns the command line it was started with.
    private async Task<List<string>> Argv(string entry, string exe, Action<LaunchOptions>? set = null)
    {
        LaunchOptions o = entry == "serve" ? new ServeOptions { ReadyTimeoutMs = 20000 } : new LaunchOptions();
        o.ExecutablePath = exe; o.Headless = true; o.Quiet = true; o.Timeout = 20000;
        set?.Invoke(o);
        var argvFile = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
        File.Delete(argvFile);
        var e = entry switch
        {
            "launch" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(o)),
            "persistent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-ll-udd-"), o)),
            "ephemeral" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(o)),
            "serve" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync((ServeOptions)o)),
            _ => throw new ArgumentException(entry),
        };
        Assert.True(File.Exists(argvFile), $"the stand-in engine never ran: {e}");
        return File.ReadAllLines(argvFile).ToList();
    }

    // ── 1. http(s) proxy credentials go to --proxy-auth on an engine that has it ───────────────────────

    [Theory]
    [InlineData("http://h:3128")]
    [InlineData("https://h:3129")]
    public void An_http_proxy_with_credentials_goes_to_the_engine_when_it_has_proxy_auth(string server)
    {
        var (args, pw) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = server, Username = "u", Password = "p:w", Bypass = " *.internal " }, true);
        Assert.Equal(new[] { $"--proxy-server={server}", "--proxy-auth=u:p:w", "--proxy-bypass-list=*.internal" }, args);
        Assert.Null(pw);
        // credentials in the URL count the same; userinfo never reaches --proxy-server
        var (urlArgs, _) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "http://us%40er:pa%21ss@h:3128" }, true);
        Assert.Equal(new[] { "--proxy-server=http://h:3128", "--proxy-auth=us@er:pa!ss" }, urlArgs);
    }

    [Fact]
    public void Without_proxy_auth_or_credentials_an_http_proxy_stays_with_Playwright()
    {
        var (a1, p1) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "http://u:p@h:3128" }, false);
        Assert.Empty(a1);
        Assert.Equal(("http://h:3128", "u", "p"), (p1!.Server, p1.Username, p1.Password));
        var plain = new ProxyOptions { Server = "http://h:3128" };
        var (a2, p2) = LaunchOpts.ResolveProxy(plain, true);
        Assert.Empty(a2);
        Assert.Same(plain, p2);
        // SOCKS5 is the engine's whatever the --proxy-auth probe says
        Assert.Equal("--socks5-credentials=u:p", LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://u:p@h:1080" }, false).Args[1]);
    }

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("serve")]
    public async Task Every_entry_point_probes_the_engine_for_proxy_auth(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var proxy = new ProxyOptions { Server = "http://127.0.0.1:9", Username = "u", Password = "p" };
        var withSwitch = await Argv(entry, FakeEngine("proxy-auth"), o => o.Proxy = proxy);
        Assert.Contains("--proxy-auth=u:p", withSwitch);
        // The real proxy is the LAST --proxy-server (Chromium keeps the last value); any earlier one is the
        // credential-less request-API forwarder Playwright was given (RequestProxy), on another local port.
        var servers = withSwitch.Where(a => a.StartsWith("--proxy-server=", StringComparison.Ordinal)).ToList();
        Assert.Equal("--proxy-server=http://127.0.0.1:9", servers[^1]);
        Assert.All(servers.SkipLast(1), a => Assert.Matches(@"^--proxy-server=http://127\.0\.0\.1:\d+$", a));
        var without = await Argv(entry, FakeEngine(), o => o.Proxy = proxy);
        Assert.DoesNotContain(without, a => a.StartsWith("--proxy-auth", StringComparison.Ordinal));
    }

    // ── 2. options this engine cannot honour ───────────────────────────────────────────────────────────

    [Fact]
    public void Options_the_engine_cannot_honour_are_warned_about_and_never_thrown()
    {
        var old = FakeEngine();
        Assert.Contains("PersonaSchema = 2 (engine r19+)", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 2 }, null, quiet: true)));
        Assert.Contains("PersonaSchema = 3 (engine 154 r36+)", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 3 }, null, quiet: true)));
        Assert.Contains("PersonaSchema = 4 (engine 154 r37+)", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 4 }, null, quiet: true)));
        Assert.Contains("PersonaSchema = 5 (engine 155 r45+)", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 5 }, null, quiet: true)));
        Assert.Contains("PersonaSchema = 7 (engine 156 r51+)", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 7 }, null, quiet: true)));
        Assert.Contains("RealGpuHost (engine r19+)", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { RealGpuHost = true }, null, quiet: true)));
        Assert.Contains("cannot authenticate to a SOCKS5 proxy", Assert.Single(LaunchOpts.WarnUnsupportedEngineOptions(old, null,
            new ProxyOptions { Server = "socks5://u:p@h:1080" }, quiet: true)));
        Assert.Empty(_err.ToString());   // quiet
        var current = FakeEngine("fingerprint-schema", "block-third-party-cookies", LaunchOpts.Schema4EngineMarker, LaunchOpts.PersonaServiceMarker, "fingerprint-gpu-backend-real", "socks5-credentials");
        foreach (var schema in new[] { 2, 3, 4, 5, 7 })
            Assert.Empty(LaunchOpts.WarnUnsupportedEngineOptions(current, new FingerprintOptions { PersonaSchema = schema, RealGpuHost = true },
                new ProxyOptions { Server = "socks5://h:1080", Username = "u" }));
        Assert.Empty(LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 1, RealGpuHost = false },
            new ProxyOptions { Server = "socks5://h:1080" }));
        LaunchOpts.WarnUnsupportedEngineOptions(old, new FingerprintOptions { PersonaSchema = 4 }, null);
        Assert.StartsWith("clearcote: PersonaSchema = 4", _err.ToString());
    }

    [Fact]
    public async Task A_launch_warns_about_an_option_its_engine_lacks()
    {
        if (OperatingSystem.IsWindows()) return;
        await Argv("persistent", FakeEngine(), o => { o.RealGpuHost = true; o.Quiet = false; });
        Assert.Contains("clearcote: RealGpuHost (engine r19+) is not supported by this engine build", _err.ToString());
    }

    // ── 3. coherence warnings and once-per-process notes (warnings.ts) ─────────────────────────────────

    private static HashSet<string> Codes(FingerprintOptions o, ProxyOptions? proxy = null, bool geoip = false, bool? headless = false,
        IEnumerable<string>? args = null, bool devtools = false, string? ua = null, string host = "linux")
        => LaunchWarnings.Coherence(o, proxy, geoip, headless, args, devtools, ua, host, "150").Select(w => w.Code).ToHashSet();

    [Fact]
    public void A_coherent_headed_launch_is_silent()
        => Assert.Empty(LaunchWarnings.Coherence(new FingerprintOptions { Fingerprint = "seed-1" }, headless: false, hostOs: "linux"));

    [Fact]
    public void Each_incoherent_combination_has_its_warning()
    {
        var proxy = new ProxyOptions { Server = "http://h:1" };
        Assert.Contains("proxy-no-geo", Codes(new FingerprintOptions(), proxy));
        Assert.DoesNotContain("proxy-no-geo", Codes(new FingerprintOptions(), proxy, geoip: true));
        Assert.DoesNotContain("proxy-no-geo", Codes(new FingerprintOptions { Timezone = "Europe/Berlin" }, proxy));

        // any claim the host's fonts cannot back, not only on a Mac
        Assert.Contains("platform-host-fonts", Codes(new FingerprintOptions { Platform = "windows" }, host: "linux"));
        Assert.Contains("platform-host-fonts", Codes(new FingerprintOptions { Platform = "linux" }, host: "windows"));
        Assert.DoesNotContain("platform-host-fonts", Codes(new FingerprintOptions { Platform = "Linux" }, host: "linux"));
        Assert.DoesNotContain("platform-host-fonts", Codes(new FingerprintOptions { Platform = "windows", FingerprintProfile = "{}" }, host: "linux"));
        var mac = LaunchWarnings.Coherence(new FingerprintOptions { Platform = "windows" }, headless: false, hostOs: "macos").Single();
        Assert.EndsWith("its fonts and canvas text metrics always stay the Mac's own.", mac.Message);
        Assert.Equal(new[] { "platform-host-fonts", LaunchWarnings.LinuxPersonaWindowsHost },
            LaunchWarnings.Coherence(new FingerprintOptions { Platform = "linux" }, headless: false, hostOs: "windows").Select(w => w.Code));

        Assert.Contains("gpu-platform", Codes(new FingerprintOptions { Platform = "linux", GpuRenderer = "ANGLE (Intel, Direct3D11)" }));
        Assert.Contains("gpu-platform", Codes(new FingerprintOptions { Platform = "windows", GpuRenderer = "Apple M2 Metal" }, host: "windows"));
        Assert.Contains("gpu-software", Codes(new FingerprintOptions { GpuRenderer = "ANGLE (Google, Vulkan (SwiftShader Device))" }));
        Assert.Contains("brand-mismatch", Codes(new FingerprintOptions { Brand = "Edge" }));
        Assert.DoesNotContain("brand-mismatch", Codes(new FingerprintOptions { Brand = "Google Chrome" }));
        Assert.Contains("version-mismatch", Codes(new FingerprintOptions { BrandVersion = "149.0.1.2" }));
        Assert.DoesNotContain("version-mismatch", Codes(new FingerprintOptions { BrandVersion = "150.0.1.2" }));
        Assert.Contains("150", LaunchWarnings.Coherence(new FingerprintOptions { BrandVersion = "149" }, headless: false, hostOs: "linux",
            buildMajor: "150").Single().Message);
    }

    [Fact]
    public void The_render_advice_follows_how_the_GPU_was_made_real()
    {
        Assert.Contains("gpu-noise", Codes(new FingerprintOptions { DisableGpuFingerprint = true }));
        Assert.DoesNotContain("gpu-noise", Codes(new FingerprintOptions { DisableGpuFingerprint = true, FingerprintNoise = false }));
        var narrow = Codes(new FingerprintOptions { GpuStringSpoof = false });
        Assert.Contains("gpu-noise-string", narrow);
        Assert.Contains("gpu-string-only", narrow);
        Assert.DoesNotContain("gpu-noise-string", Codes(new FingerprintOptions { GpuStringSpoof = false, FingerprintNoise = false }));
        Assert.DoesNotContain("gpu-string-only", Codes(new FingerprintOptions { GpuStringSpoof = false, DisableGpuFingerprint = true }));
        Assert.Contains("canvas-noise-toblob", Codes(new FingerprintOptions { CanvasNoise = false }));
        Assert.Contains("headless-render", Codes(new FingerprintOptions(), headless: null));
        Assert.DoesNotContain("headless-render", Codes(new FingerprintOptions { DisableGpuFingerprint = true, FingerprintNoise = false }, headless: true));
        var bridge = new CanvasBridgeOptions { Url = "ws://bridge:1" };
        Assert.Contains("bridge-no-gpu", Codes(new FingerprintOptions { CanvasBridge = bridge }));
        Assert.DoesNotContain("bridge-no-gpu", Codes(new FingerprintOptions { CanvasBridge = bridge, GpuRenderer = "ANGLE (NVIDIA)" }));
        Assert.All(LaunchWarnings.Coherence(new FingerprintOptions { GpuStringSpoof = false, CanvasNoise = false }, headless: null, hostOs: "linux")
            .Where(w => w.Code is "gpu-string-only" or "canvas-noise-toblob" or "headless-render"), w => Assert.True(w.IsNote));
    }

    [Fact]
    public void Devtools_and_a_user_agent_from_the_pass_through_warn_like_their_switches()
    {
        Assert.Contains("devtools-open", Codes(new FingerprintOptions(), devtools: true));
        Assert.Contains("custom-user-agent", Codes(new FingerprintOptions(), ua: "Mozilla/5.0 (X11; Linux x86_64)"));
        Assert.Equal(new[] { "automation-arg", "devtools-open", "custom-user-agent", "cdp-public-bind" },
            LaunchWarnings.Coherence(new FingerprintOptions(), headless: false, hostOs: "linux", devtools: true, userAgent: "x",
                userArgs: new[] { "--enable-automation", "--remote-debugging-address=0.0.0.0" }).Select(w => w.Code));
        // once each, whichever way they were asked for
        Assert.Single(LaunchWarnings.Coherence(new FingerprintOptions(), headless: false, hostOs: "linux", devtools: true,
            userArgs: new[] { "--auto-open-devtools-for-tabs" }), w => w.Code == "devtools-open");
    }

    [Fact]
    public void Notes_and_the_host_warning_are_said_once_per_process_the_rest_every_launch()
    {
        var ws = LaunchWarnings.Coherence(new FingerprintOptions { Platform = "linux", Brand = "Edge" }, headless: null, hostOs: "windows");
        for (var i = 0; i < 3; i++) LaunchWarnings.EmitCoherence(ws, quiet: false);
        var text = _err.ToString();
        int Times(string s) => text.Split(s).Length - 1;
        Assert.Equal(3, Times("clearcote: warning: Brand = \"Edge\""));
        Assert.Equal(3, Times("clearcote: warning: Platform = \"linux\" but this host is windows"));
        Assert.Equal(1, Times("clearcote: warning: a Linux persona on a Windows host"));
        Assert.Equal(1, Times("clearcote: note: headless with no CanvasBridge"));
        Assert.Equal(1, Times("clearcote: note: the engine does not forward console or page-error events"));
        Assert.Equal(1, Times("clearcote: note: the engine does not carry Runtime.addBinding bindings"));
    }

    [Fact]
    public void The_console_events_note_is_left_out_for_a_browser_with_the_stock_runtime_and_quiet_uses_nothing_up()
    {
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false, stockRuntime: true);
        Assert.Empty(_err.ToString());
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: true);
        _sb.Env("CLEARCOTE_NO_WARN", "1");
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false);
        _sb.Env("CLEARCOTE_NO_WARN", null);
        Assert.Empty(_err.ToString());
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false);
        Assert.Contains("clearcote: note: the engine does not forward console or page-error events", _err.ToString());
        Assert.Contains("StockRuntime = true", _err.ToString());
        Assert.Contains("clearcote: note: the engine does not carry Runtime.addBinding bindings", _err.ToString());
        Assert.Contains("RuntimeBindings = true", _err.ToString());
    }

    [Fact]
    public async Task A_launch_says_its_coherence_warnings()
    {
        if (OperatingSystem.IsWindows()) return;
        await Argv("persistent", FakeEngine(), o =>
        {
            o.Quiet = false; o.Brand = "Edge"; o.Proxy = new ProxyOptions { Server = "http://127.0.0.1:9" };
            o.PlaywrightContextOptions = new BrowserTypeLaunchPersistentContextOptions { UserAgent = "Mozilla/5.0 custom" };
        });
        var text = _err.ToString();
        Assert.Contains("clearcote: warning: Brand = \"Edge\"", text);
        Assert.Contains("clearcote: warning: proxy set without Geoip", text);
        Assert.Contains("clearcote: warning: a custom user agent", text);
        Assert.Contains("clearcote: note: headless with no CanvasBridge", text);
    }

    // ── 4. profile portability ─────────────────────────────────────────────────────────────────────────

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("serve")]
    public async Task PortableProfile_and_EncryptionKey_reach_the_engine(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = FakeEngine();
        Assert.Contains("--portable-profile", await Argv(entry, exe, o => o.PortableProfile = true));
        var keyed = await Argv(entry, exe, o => { o.PortableProfile = true; o.EncryptionKey = "s3cret"; });
        Assert.Contains("--profile-encryption-key=s3cret", keyed);
        Assert.DoesNotContain("--portable-profile", keyed);   // the derived key wins
        Assert.DoesNotContain(await Argv(entry, exe), a => a.Contains("portable-profile") || a.Contains("profile-encryption-key"));
    }

    [Fact]
    public void Extension_and_portability_switches_come_in_the_order_Node_assembles_them()
        => Assert.Equal(new[] { "--load-extension=/e", "--disable-extensions-except=/e", "--portable-profile" },
            Clearcote.ExtensionAndPortableArgs(new LaunchOptions { Extensions = new[] { "/e" }, PortableProfile = true }));

    // ── 7. the rest of Playwright's options ────────────────────────────────────────────────────────────

    [Fact]
    public void A_pass_through_field_LaunchOptions_owns_is_refused_by_name()
    {
        var e = Assert.Throws<ArgumentException>(() => Clearcote.CheckPlaywrightPassThrough(
            new LaunchOptions { PlaywrightLaunchOptions = new BrowserTypeLaunchOptions { Headless = false } }));
        Assert.Contains("PlaywrightLaunchOptions.Headless is set: set LaunchOptions.Headless instead", e.Message);
        Assert.Contains("PlaywrightContextOptions.ViewportSize", Assert.Throws<ArgumentException>(() => Clearcote.CheckPlaywrightPassThrough(
            new LaunchOptions { PlaywrightContextOptions = new() { ViewportSize = ViewportSize.NoViewport } }, context: true)).Message);
        Assert.Contains("PlaywrightContextOptions.Args", Assert.Throws<ArgumentException>(() => Clearcote.CheckPlaywrightPassThrough(
            new LaunchOptions { PlaywrightContextOptions = new() { Args = new[] { "--x" } } }, context: true)).Message);
        // everything else passes
        Clearcote.CheckPlaywrightPassThrough(new LaunchOptions { PlaywrightLaunchOptions = new() { ChromiumSandbox = true, DownloadsPath = "/d", TracesDir = "/t", HandleSIGINT = false } });
        Clearcote.CheckPlaywrightPassThrough(new LaunchOptions { PlaywrightContextOptions = new() { Locale = "de-DE", UserAgent = "x", AcceptDownloads = true,
            IgnoreHTTPSErrors = true, Permissions = new[] { "geolocation" }, Geolocation = new Geolocation { Latitude = 1, Longitude = 2 } } }, context: true);
    }

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("ephemeral")]
    public async Task The_pass_through_options_reach_Playwright(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = FakeEngine();
        // Playwright adds --no-sandbox unless ChromiumSandbox is on: the stand-in's command line shows which it got.
        Assert.Contains("--no-sandbox", await Argv(entry, exe));
        var sandboxed = await Argv(entry, exe, o =>
        {
            if (entry == "launch") o.PlaywrightLaunchOptions = new BrowserTypeLaunchOptions { ChromiumSandbox = true };
            else o.PlaywrightContextOptions = new BrowserTypeLaunchPersistentContextOptions { ChromiumSandbox = true, Locale = "de-DE" };
        });
        Assert.DoesNotContain("--no-sandbox", sandboxed);
        Assert.Contains("--webrtc-ip-handling-policy=disable_non_proxied_udp", sandboxed);   // and the SDK's own args are all there
    }

    [Fact]
    public async Task The_callers_pass_through_object_is_never_changed()
    {
        if (OperatingSystem.IsWindows()) return;
        var mine = new BrowserTypeLaunchPersistentContextOptions { Locale = "de-DE" };
        await Argv("persistent", FakeEngine(), o => o.PlaywrightContextOptions = mine);
        Assert.Null(mine.ExecutablePath);
        Assert.Null(mine.Args);
        Assert.Equal("de-DE", mine.Locale);
    }

    // ── 5. DownloadAsync resolves like a launch ────────────────────────────────────────────────────────

    [Fact]
    public async Task DownloadAsync_takes_a_catalog_version()
    {
        if (Release.ProPlatformKey() is null || OperatingSystem.IsMacOS()) return;
        _sb.Http(new FakeHandler(_ => throw new HttpRequestException("offline")));   // the catalog falls back to the built-in copy
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.DownloadAsync(new DownloadOptions { Version = "999", Quiet = true, CacheDir = Dir("cc-ll-c-") }));
        Assert.Contains("No Clearcote build matches version '999'", e.Message);
    }

    [Theory]
    [InlineData(null, false)]
    [InlineData(ReleaseChannel.Preview, true)]
    public async Task DownloadAsync_with_a_licence_key_gets_the_PRO_build_on_its_channel(ReleaseChannel? channel, bool preview)
    {
        if (Release.ProPlatformKey() is null) return;
        var seen = new List<string>();
        _sb.Http(new FakeHandler(r => { seen.Add(r.RequestUri!.ToString()); return new HttpResponseMessage(System.Net.HttpStatusCode.Unauthorized) { Content = new StringContent("{}") }; }));
        Download.ForgetProMeta();
        await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.DownloadAsync(new DownloadOptions
        {
            LicenseKey = "cc_lic_dl", LicenseApiBase = "http://licence.test", ReleaseChannel = channel, Quiet = true, CacheDir = Dir("cc-ll-c-"),
        }));
        Assert.StartsWith("http://licence.test/api/v1/download/pro?", Assert.Single(seen));
        Assert.Equal(preview, seen[0].Contains("&channel=preview"));
        Download.ForgetProMeta();
    }

    [Fact]
    public async Task DownloadAsync_honours_CLEARCOTE_BINARY_like_a_launch()
    {
        var exe = FakeEngine();
        _sb.Env("CLEARCOTE_BINARY", exe);
        Assert.Equal(exe, await Clearcote.DownloadAsync());
    }

    // ── 9. the cache ───────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public void ListCachedBuilds_lists_verified_builds_newest_first_and_leaves_the_rest_alone()
    {
        var root = Dir("cc-ll-cache-");
        var binary = Native.IsWindows ? "chrome.exe" : Native.OsTag == "macos" ? "Chromium" : "chrome";
        string Build(string tag, bool verified, bool exe, int minutesAgo)
        {
            var b = Path.Combine(root, tag);
            Directory.CreateDirectory(Path.Combine(b, "browser", "sub"));
            if (verified) File.WriteAllText(Path.Combine(b, ".verified"), "x\n");
            if (exe) File.WriteAllText(Path.Combine(b, "browser", "sub", binary), "x");
            Directory.SetLastWriteTimeUtc(b, DateTime.UtcNow.AddMinutes(-minutesAgo));
            return Path.Combine(b, "browser", "sub", binary);
        }
        var older = Build("v0.1.0-pre.22", true, true, 30);
        var newer = Build("v0.1.0-pre.23", true, true, 5);
        Build("half-installed", false, true, 1);
        Build("no-binary", true, false, 1);
        File.WriteAllText(Path.Combine(root, "stray-file"), "x");
        Assert.Equal(new[] { new CachedBuild("v0.1.0-pre.23", newer), new CachedBuild("v0.1.0-pre.22", older) }, Download.ListCachedBuilds(root));
        Assert.True(Directory.Exists(Path.Combine(root, "half-installed")));   // never deletes
        Assert.Empty(Download.ListCachedBuilds(Path.Combine(root, "missing")));
        _sb.Env("CLEARCOTE_CACHE", root);
        Assert.Equal(root, Download.DefaultCacheRoot());
        Assert.Equal(2, Download.ListCachedBuilds().Count);
    }

    [Fact]
    public void AutoUpdateRequested_follows_the_option_then_CLEARCOTE_AUTO_UPDATE()
    {
        _sb.Env("CLEARCOTE_AUTO_UPDATE", null);
        Assert.False(Download.AutoUpdateRequested(null));
        _sb.Env("CLEARCOTE_AUTO_UPDATE", "1");
        Assert.True(Download.AutoUpdateRequested(null));
        Assert.False(Download.AutoUpdateRequested(false));
        _sb.Env("CLEARCOTE_AUTO_UPDATE", "yes");
        Assert.False(Download.AutoUpdateRequested(null));
        Assert.True(Download.AutoUpdateRequested(true));
    }

    // ── 12. the binary guard ───────────────────────────────────────────────────────────────────────────

    [Fact]
    public void An_existing_binary_is_the_callers_to_run_whatever_its_name()
    {
        var exe = Path.Combine(Dir("cc-ll-exe-"), "chrome.exe");
        File.WriteAllText(exe, "x");
        foreach (var os in new[] { "linux", "macos", "windows" })
        {
            _sb.Os(os);
            Clearcote.EnsureRunnableHere(exe);   // a .exe under Wine, say: never refused for its name
        }
    }

    [Fact]
    public void A_missing_binary_is_refused_with_the_reason()
    {
        var missing = Path.Combine(Dir("cc-ll-exe-"), "chrome");
        _sb.Os("linux");
        Assert.Throws<FileNotFoundException>(() => Clearcote.EnsureRunnableHere(missing));
        _sb.Os("macos");
        Native.ArchOverride = "x64";   // an Intel Mac: no build at all
        Assert.Contains("there is no build for 'macos'", Assert.Throws<PlatformNotSupportedException>(() => Clearcote.EnsureRunnableHere(missing)).Message);
        Native.ArchOverride = "arm64";   // Apple Silicon has the PRO build: just not found
        Assert.Throws<FileNotFoundException>(() => Clearcote.EnsureRunnableHere(missing));
    }

    // ── 13. the geoip egress-drift check is awaited ────────────────────────────────────────────────────

    [Fact]
    public async Task The_drift_check_a_launch_awaits_finishes_its_warning_first()
    {
        // A rotating exit: the second lookup sees another IP, and the warning is written by the time the task completes.
        await using var echo = new TestOrigin(_ => (200, "203.0.113.8"));
        await using var proxy = new TestHttpProxy();
        _sb.Env(GeoIp.EchoUrlEnv, $"http://127.0.0.1:{echo.Port}/ip");
        var drift = GeoIp.StartEgressDriftCheck(new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" }, "203.0.113.7", quiet: false);
        await drift;
        Assert.Contains("the proxy's exit IP changed between two lookups at launch (203.0.113.7 -> ", _err.ToString());
        Assert.Same(Task.CompletedTask, GeoIp.StartEgressDriftCheck(null, "203.0.113.7", quiet: false));
        Assert.Same(Task.CompletedTask, GeoIp.StartEgressDriftCheck(new ProxyOptions { Server = "http://h:1" }, "203.0.113.7", quiet: true));
    }
}
