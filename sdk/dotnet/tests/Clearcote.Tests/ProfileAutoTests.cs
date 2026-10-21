using System.Net;
using System.Text.Json.Nodes;
using Clearcote;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <c>LocalProfile = "auto"</c>: service first, local directory as backup (Node test/profileauto.test.ts and
/// profile-compat.test.ts, Python tests/test_profileauto.py), against a captured HTTP handler and a local fixture
/// directory — no network.
public sealed class ProfileAutoTests : IDisposable
{
    private const string Base = "http://profiles.test";
    private readonly Sandbox _sb = new();
    private readonly string _root;
    private readonly string _localDir;
    private int _status = 200;

    private static readonly HostFacts HostTemplate = new()
    {
        OsFamily = "windows", BrowserMajor = 150, GpuVendor = "nvidia",
        ScreenWidth = 3440, ScreenHeight = 1440, DevicePixelRatio = 1, HardwareConcurrency = 16, DeviceMemory = 16,
    };

    // Each case uses a DISTINCT major so it gets a distinct index-cache key: FetchIndexAsync deliberately serves a
    // stale cached index when the service is down, so a previous case's cache would make a 503 look like success.
    private static int _nextMajor = 150;
    private static HostFacts FreshHost() => new()
    {
        OsFamily = HostTemplate.OsFamily, BrowserMajor = Interlocked.Increment(ref _nextMajor), GpuVendor = HostTemplate.GpuVendor,
        ScreenWidth = HostTemplate.ScreenWidth, ScreenHeight = HostTemplate.ScreenHeight, DevicePixelRatio = 1,
        HardwareConcurrency = 16, DeviceMemory = 16,
    };

    /// A VERSION-AGNOSTIC local profile: the UA carries no Chrome version, exactly like a converted dataset record,
    /// so it stays eligible whatever major the engine reports.
    private static JsonObject Blob() => (JsonObject)JsonNode.Parse("""
        {
          "meta": { "captured_at": "2026-07-01T00:00:00.000Z" },
          "navigator": { "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36", "platform": "Win32" },
          "screen": { "width": 1920, "height": 1080, "device_pixel_ratio": 1 },
          "webgl": { "webgl1": { "debug": { "UNMASKED_RENDERER_WEBGL": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)" } } },
          "hardware_concurrency": 16,
          "device_memory": 16
        }
        """)!;

    public ProfileAutoTests()
    {
        _root = _sb.TempHome();
        _sb.Env("CLEARCOTE_PROFILE_DIR", Path.Combine(_root, "cache")).Env("CLEARCOTE_PROFILE_API", null)
           .Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_PROFILE_SOURCE", null);
        _localDir = Path.Combine(_root, "imported");
        Directory.CreateDirectory(_localDir);
        File.WriteAllText(Path.Combine(_localDir, "local-1.json"), Blob().ToJsonString());
        _sb.Http(new FakeHandler(req =>
        {
            if (_status != 200) return new HttpResponseMessage((HttpStatusCode)_status) { Content = new StringContent("""{"error":"service says no"}""") };
            var q = System.Web.HttpUtility.ParseQueryString(req.RequestUri!.Query);
            if (q["id"] is not null)
            {
                var p = Blob();
                p["_from"] = "service";
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(new JsonObject { ["id"] = "svc-1", ["profile"] = p }.ToJsonString()) };
            }
            // Echo back the major asked for, as the real service does: candidates for the described host.
            return new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent($$"""
                    {"candidates":[{"id":"svc-1","os_family":"windows","browser_major":{{q["major"]}},"gpu_vendor":"nvidia",
                      "screen_width":1920,"screen_height":1080,"device_pixel_ratio":1,"hardware_concurrency":16,"device_memory":16,
                      "encoded_size":9000,"captured_at":"{{DateTime.UtcNow:o}}"}]}
                    """),
            };
        }));
        ProfileAuto.ResetHint();
    }

    public void Dispose()
    {
        ProfileAuto.ResetSeams();
        _sb.Dispose();
    }

    private AutoOptions Opts(Action<AutoOptions>? over = null)
    {
        var o = new AutoOptions { LicenseKey = "cc_lic_test", ApiBase = Base, LocalDir = _localDir, Quiet = true };
        over?.Invoke(o);
        return o;
    }

    // ── service first, local as backup ──────────────────────────────────────

    [Fact]
    public async Task Uses_the_service_when_it_is_available()
    {
        var r = await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts());
        Assert.Equal(ProfileOrigin.Service, r.Source);
        Assert.Equal("svc-1", r.Selection.Entry.Id);
        Assert.Null(r.FallbackReason);
        Assert.Equal("service", r.Profile["_from"]!.GetValue<string>());
    }

    [Theory]
    [InlineData(429)]
    [InlineData(401)]
    [InlineData(503)]
    public async Task Falls_back_to_local_when_the_service_fails(int status)
    {
        _status = status;
        var r = await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts());
        Assert.Equal(ProfileOrigin.Local, r.Source);
        Assert.Equal("local-1", r.Selection.Entry.Id);
        // The reason must survive: a silent downgrade would hide WHY the identity changed.
        Assert.Contains(status.ToString(), r.FallbackReason);
    }

    [Fact]
    public async Task The_fallback_is_announced_unless_quiet()
    {
        _status = 429;
        using var err = new StderrCapture();
        await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => o.Quiet = null));
        Assert.Contains("[clearcote] [profile] service unavailable (", err.Text);
        Assert.Contains("falling back to local profile local-1", err.Text);
    }

    [Fact]
    public async Task PreferLocal_skips_the_service_entirely()
    {
        var r = await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => o.PreferLocal = true));
        Assert.Equal(ProfileOrigin.Local, r.Source);
    }

    [Fact]
    public async Task Reports_BOTH_failures_not_just_the_local_one()
    {
        _status = 401;
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => o.LocalDir = Path.Combine(_root, "does-not-exist"))));
        Assert.Matches("(?s)could not resolve a persona.*service:.*401.*local:.*no directory at", e.Message);
    }

    [Fact]
    public async Task Includes_the_optional_setup_example_when_there_is_no_local_directory()
    {
        _status = 503;
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => o.LocalDir = Path.Combine(_root, "also-missing"))));
        Assert.Contains("convert_dataset.py", e.Message);
    }

    [Fact]
    public async Task The_backup_tip_is_said_once_per_process_and_only_with_no_backup()
    {
        using var err = new StderrCapture();
        var noBackup = Path.Combine(_root, "none");
        await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => { o.Quiet = null; o.LocalDir = noBackup; }));
        await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => { o.Quiet = null; o.LocalDir = noBackup; }));
        Assert.Single(System.Text.RegularExpressions.Regex.Matches(err.Text, "tip: no local backup directory configured"));
        ProfileAuto.ResetHint();
        await ProfileAuto.ResolveAutoAsync(FreshHost(), Opts(o => o.Quiet = null));   // a backup exists: no tip
        Assert.Single(System.Text.RegularExpressions.Regex.Matches(err.Text, "tip: no local backup"));
        Assert.Contains("using service profile svc-1 (score ", err.Text);
        Assert.Contains("mode rotate)", err.Text);
    }

    // ── localSetupHint / resolveLocal ───────────────────────────────────────

    [Fact]
    public void The_setup_hint_frames_the_dataset_as_optional_and_user_fetched()
    {
        var h = ProfileAuto.LocalSetupHint("/tmp/x");
        Assert.Contains("optional", h, StringComparison.OrdinalIgnoreCase);
        Assert.Matches("own repo|own licence|own terms", h);
        Assert.Contains("pip install chrome-fingerprints", h);
        Assert.Contains("/tmp/x", h);
        // alternatives, so the dataset is one example and not a requirement
        Assert.Matches("(?i)your own captures|capture your own", h);
        Assert.Contains("CLEARCOTE_PROFILE_SOURCE", h);
    }

    [Fact]
    public void ResolveLocal_selects_from_the_imported_directory()
    {
        var r = ProfileAuto.ResolveLocal(FreshHost(), Opts());
        Assert.Equal(ProfileOrigin.Local, r.Source);
        Assert.NotNull(r.Profile["navigator"]);
    }

    [Fact]
    public void ResolveLocal_explains_how_to_populate_a_missing_directory()
    {
        var e = Assert.Throws<InvalidOperationException>(() => ProfileAuto.ResolveLocal(FreshHost(), Opts(o => o.LocalDir = Path.Combine(_root, "nope"))));
        Assert.Matches("(?s)no local profile directory.*convert_dataset\\.py", e.Message);
    }

    [Fact]
    public void ResolveLocal_on_an_empty_directory_says_so()
    {
        var empty = Path.Combine(_root, "empty");
        Directory.CreateDirectory(empty);
        Assert.Contains("contains no usable clearcote-profile JSON",
            Assert.Throws<InvalidOperationException>(() => ProfileAuto.ResolveLocal(FreshHost(), Opts(o => o.LocalDir = empty))).Message);
    }

    [Fact]
    public void The_default_local_dir_follows_CLEARCOTE_PROFILE_SOURCE()
    {
        Assert.Equal(Path.Combine(_root, ".clearcote", "profiles", "imported"), ProfileAuto.DefaultLocalDir);
        _sb.Env("CLEARCOTE_PROFILE_SOURCE", _localDir);
        Assert.Equal(_localDir, ProfileAuto.DefaultLocalDir);
        Assert.Equal("local-1", ProfileAuto.ResolveLocal(FreshHost(), new AutoOptions()).Selection.Entry.Id);
    }

    [Fact]
    public void The_local_index_is_cached_and_invalidated_by_the_directory_mtime()
    {
        Assert.Single(ProfileAuto.LoadLocalIndex(_localDir));
        var cache = Path.Combine(_root, "cache", "library", "local-index.json");
        Assert.True(File.Exists(cache));
        Assert.Equal(_localDir, JsonNode.Parse(File.ReadAllText(cache))!["dir"]!.GetValue<string>());
        // a converted profile added to the directory is picked up without a manual step
        File.WriteAllText(Path.Combine(_localDir, "local-2.json"), Blob().ToJsonString());
        Directory.SetLastWriteTimeUtc(_localDir, DateTime.UtcNow.AddMinutes(1));
        Assert.Equal(2, ProfileAuto.LoadLocalIndex(_localDir).Count);
    }

    // ── engine support (profile-compat.test.ts) ─────────────────────────────

    [Fact]
    public void EngineSupportsProfiles_gates_on_150()
    {
        Assert.Equal(150, ProfileAuto.MinProfileEngineMajor);
        Assert.False(ProfileAuto.EngineSupportsProfiles(149));   // the free 149 engine has no persona-profile patch
        Assert.True(ProfileAuto.EngineSupportsProfiles(150));
        Assert.True(ProfileAuto.EngineSupportsProfiles(151));
        Assert.True(ProfileAuto.EngineSupportsProfiles(200));
        foreach (var m in new[] { 120, 138, 145, 148 }) Assert.False(ProfileAuto.EngineSupportsProfiles(m));
        Assert.False(ProfileAuto.EngineSupportsProfiles(null));   // unreadable: the safe path, not the optimistic one
    }

    [Fact]
    public void The_149_fallback_identity_is_a_stable_plain_seed()
    {
        Assert.Equal(ProfileLib.DefaultStickyKey(), ProfileLib.DefaultStickyKey());
        Assert.Matches("^[0-9a-f]+$", ProfileLib.DefaultStickyKey());
    }

    // ── Windows 11 faces ────────────────────────────────────────────────────

    private static JsonObject Win(string platformVersion, string fonts) => (JsonObject)JsonNode.Parse(
        """{"navigator":{"platform":"Win32","uadata":{"platform":"Windows","high_entropy":{"platform":"Windows","platformVersion":""" +
        "\"" + platformVersion + "\"}}},\"fonts\":{\"detected\":" + fonts + "}}")!;

    [Fact]
    public void A_Windows_11_donor_gains_the_Windows_11_faces_on_a_copy()
    {
        var p = Win("15.0.0", """["Arial","segoe fluent icons"]""");
        var q = (JsonObject)ProfileAuto.ProfileWithWindows11Faces(p)!;
        Assert.NotSame(p, q);
        Assert.Equal(new[] { "Arial", "segoe fluent icons", "Segoe UI Variable Display", "Segoe UI Variable Small", "Segoe UI Variable Text" },
            q["fonts"]!["detected"]!.AsArray().Select(f => f!.GetValue<string>()));
        Assert.Equal(2, p["fonts"]!["detected"]!.AsArray().Count);   // the cached corpus entry is never mutated
    }

    [Fact]
    public void Windows_10_and_other_platforms_are_unchanged()
    {
        var w10 = Win("10.0.0", """["Arial"]""");
        Assert.Same(w10, ProfileAuto.ProfileWithWindows11Faces(w10));
        var mac = (JsonObject)JsonNode.Parse("""{"navigator":{"platform":"MacIntel","uadata":{"high_entropy":{"platformVersion":"15.0.0"}}},"fonts":{"detected":["Arial"]}}""")!;
        Assert.Same(mac, ProfileAuto.ProfileWithWindows11Faces(mac));
        var noFonts = (JsonObject)JsonNode.Parse("""{"navigator":{"platform":"Win32","uadata":{"high_entropy":{"platformVersion":"15.0.0"}}}}""")!;
        Assert.Same(noFonts, ProfileAuto.ProfileWithWindows11Faces(noFonts));
        Assert.True(ProfileAuto.ClaimsWindows11("13.0.0"));
        Assert.False(ProfileAuto.ClaimsWindows11("12.9"));
        Assert.False(ProfileAuto.ClaimsWindows11(""));
        Assert.False(ProfileAuto.ClaimsWindows11("x.1"));
    }

    // ── launch wiring (Python tests/test_profile_auto_launch.py) ────────────

    private static readonly JsonObject ServiceProfile = (JsonObject)JsonNode.Parse("""{"navigator":{"user_agent":"ua"},"screen":{"width":1920}}""")!;

    private (List<AutoOptions> Resolved, List<int> Majors) Stub(int engineMajor = 150)
    {
        var resolved = new List<AutoOptions>();
        var majors = new List<int>();
        ProfileAuto.MeasureHost = (_, _, major) =>
        {
            majors.Add(major);
            return Task.FromResult(new HostFacts { OsFamily = "windows", BrowserMajor = engineMajor, GpuVendor = "intel" });
        };
        ProfileAuto.Resolve = (_, o) =>
        {
            resolved.Add(o);
            return Task.FromResult(new AutoResult { Profile = ServiceProfile, Selection = new ProfileSelection { Entry = new ProfileIndexEntry { Id = "svc-1" } } });
        };
        return (resolved, majors);
    }

    [Fact]
    public async Task Auto_sets_FingerprintProfile_from_the_resolver_and_never_a_seed()
    {
        var (resolved, majors) = Stub();
        var o = new LaunchOptions { LocalProfile = "auto", Quiet = true };
        await ProfileAuto.ApplyAsync(o, "/path/chrome");
        Assert.True(JsonNode.DeepEquals(ServiceProfile, (JsonNode)o.FingerprintProfile!));
        Assert.Null(o.Fingerprint);   // the seed is what engages farbling; "auto" exists to avoid it
        Assert.Single(resolved);
        // the SDK's pinned major is the measurement's fallback
        Assert.Equal(int.Parse(Release.Linux.Version.Split('.')[0]), majors.Single());
    }

    [Fact]
    public async Task Profile_auto_is_wired_like_LocalProfile_auto()
    {
        Stub();
        var o = new LaunchOptions { Profile = "auto", Quiet = true };
        await ProfileAuto.ApplyAsync(o, "/path/chrome");
        Assert.NotNull(o.FingerprintProfile);
    }

    [Fact]
    public async Task An_explicit_FingerprintProfile_wins_and_skips_all_work()
    {
        var (resolved, majors) = Stub();
        var o = new LaunchOptions { LocalProfile = "auto", FingerprintProfile = "mine.json", Quiet = true };
        await ProfileAuto.ApplyAsync(o, "/path/chrome");
        Assert.Equal("mine.json", o.FingerprintProfile);
        Assert.Empty(majors);
        Assert.Empty(resolved);
    }

    [Fact]
    public async Task Passthrough_and_saved_profiles_are_not_resolved()
    {
        var (resolved, majors) = Stub();
        await ProfileAuto.ApplyAsync(new LaunchOptions { LocalProfile = "auto", Fingerprint = "off" }, "/path/chrome");
        await ProfileAuto.ApplyAsync(new LaunchOptions { LocalProfile = "acct" }, "/path/chrome");
        await ProfileAuto.ApplyAsync(new LaunchOptions(), "/path/chrome");
        Assert.Empty(majors);
        Assert.Empty(resolved);
    }

    [Fact]
    public async Task Forwards_selection_options_licence_and_endpoint()
    {
        var (resolved, _) = Stub();
        await ProfileAuto.ApplyAsync(new LaunchOptions
        {
            LocalProfile = "auto", Quiet = true, LicenseKey = "cc_lic_x", LicenseApiBase = "https://api.example",
            ProfileSelect = new AutoOptions { Key = "acct-9", Mode = SelectMode.Best, PreferLocal = true },
        }, "/path/chrome");
        var o = resolved.Single();
        Assert.Equal("acct-9", o.Key);
        Assert.Equal(SelectMode.Best, o.Mode);
        Assert.True(o.PreferLocal);
        Assert.Equal("cc_lic_x", o.LicenseKey);
        Assert.Equal("https://api.example", o.ApiBase);
        Assert.True(o.Quiet);
        // ProfileSelect's own licence/endpoint win over the launch's
        await ProfileAuto.ApplyAsync(new LaunchOptions
        {
            LocalProfile = "auto", Quiet = true, LicenseKey = "cc_lic_x",
            ProfileSelect = new AutoOptions { LicenseKey = "cc_lic_sel", ApiBase = "https://sel.example" },
        }, "/path/chrome");
        Assert.Equal("cc_lic_sel", resolved[1].LicenseKey);
        Assert.Equal("https://sel.example", resolved[1].ApiBase);
    }

    [Fact]
    public async Task Warns_when_a_seed_is_also_set_unless_quiet()
    {
        Stub();
        using (var err = new StderrCapture())
        {
            await ProfileAuto.ApplyAsync(new LaunchOptions { LocalProfile = "auto", Fingerprint = "seed-1" }, "/path/chrome");
            Assert.Contains("fingerprint seed", err.Text, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("farbling", err.Text);
        }
        using (var err = new StderrCapture())
        {
            await ProfileAuto.ApplyAsync(new LaunchOptions { LocalProfile = "auto", Fingerprint = "seed-1", Quiet = true }, "/path/chrome");
            Assert.Equal("", err.Text);
        }
    }

    [Fact]
    public async Task An_engine_before_150_gets_the_stable_seed_persona_instead()
    {
        var (resolved, _) = Stub(engineMajor: 149);
        using var err = new StderrCapture();
        var o = new LaunchOptions { LocalProfile = "auto" };
        await ProfileAuto.ApplyAsync(o, "/path/chrome");
        Assert.Null(o.FingerprintProfile);   // never sent to an engine that silently ignores it
        Assert.Equal(ProfileLib.DefaultStickyKey(), o.Fingerprint);
        Assert.Empty(resolved);
        Assert.Contains("engine 149 does not support imported profiles (added in 150)", err.Text);
        // a caller's own seed is kept
        var own = new LaunchOptions { LocalProfile = "auto", Fingerprint = "mine", Quiet = true };
        await ProfileAuto.ApplyAsync(own, "/path/chrome");
        Assert.Equal("mine", own.Fingerprint);
    }

    [Fact]
    public async Task A_Windows_11_service_profile_gains_the_faces()
    {
        Stub();
        var win11 = Win("15.0.0", """["Arial"]""");
        ProfileAuto.Resolve = (_, _) => Task.FromResult(new AutoResult { Profile = win11, Selection = new ProfileSelection() });
        var o = new LaunchOptions { LocalProfile = "auto", Quiet = true };
        await ProfileAuto.ApplyAsync(o, "/path/chrome");
        Assert.Contains("Segoe Fluent Icons", ((JsonNode)o.FingerprintProfile!)["fonts"]!["detected"]!.ToJsonString());
    }

    [Fact]
    public async Task The_nested_probe_launch_is_licensed_local_and_never_recurses()
    {
        // The probe launches the SAME binary the session will use. On PRO that is the gated build, so a probe
        // launched without the licence is killed by the engine gate: the key must reach it.
        LaunchOptions? seen = null;
        ProfileAuto.ProbeLaunch = o => { seen = o; throw new InvalidOperationException("stop here — the options are the assertion"); };
        ProfileAuto.MeasureHost = async (launch, exe, _) =>
        {
            await launch(new LaunchOptions { ExecutablePath = exe, Headless = true, Quiet = true });
            return new HostFacts();
        };
        await Assert.ThrowsAsync<InvalidOperationException>(() => ProfileAuto.ApplyAsync(
            new LaunchOptions { LocalProfile = "auto", Quiet = true, LicenseKey = "cc_lic_probe", LicenseApiBase = "https://api.example" }, "/path/chrome"));
        Assert.NotNull(seen);
        Assert.Equal("cc_lic_probe", seen!.LicenseKey);
        Assert.Equal("https://api.example", seen.LicenseApiBase);
        Assert.Equal("/path/chrome", seen.ExecutablePath);
        Assert.True(seen.Headless);
        Assert.False(seen.Cloud);   // measures THIS host, whatever CLEARCOTE_CLOUD says
        Assert.False(seen.Docker);
        Assert.False(seen.Widevine);
        Assert.Null(ProfileAuto.LocalProfileOf(seen));   // no selector: it cannot recurse
    }

    [Fact]
    public async Task MeasureHost_launches_through_the_given_function_and_caches_per_binary()
    {
        // The real MeasureHostAsync, with its launch refused: nothing is cached and the error surfaces.
        var exe = Path.Combine(_root, "chrome");
        await Assert.ThrowsAsync<PlaywrightException>(() =>
            ProfileSource.MeasureHostAsync(_ => throw new PlaywrightException("no browser here"), exe, 150));
        Assert.Null(ProfileSource.ReadCachedHost(exe));
    }
}
