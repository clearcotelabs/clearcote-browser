using System.Net;
using System.Reflection;
using System.Text.Json.Nodes;
using Clearcote;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// The profile-service client (Node test/profilesource.test.ts) against a captured HTTP handler, so the request
/// shape, auth header, caching and fallback paths are tested with no network. Plus the host cache (Python
/// tests/test_profileauto.py TestHostCacheKey / TestEngineMajor).
public sealed class ProfileSourceTests : IDisposable
{
    private const string Base = "http://profiles.test";
    private readonly Sandbox _sb = new();
    private readonly string _dir;
    private readonly FakeHandler _http;
    private int _indexStatus = 200;
    private int _profileStatus = 200;

    private static readonly HostFacts Host = new()
    {
        OsFamily = "windows", BrowserMajor = 150, GpuVendor = "intel",
        ScreenWidth = 3440, ScreenHeight = 1440, DevicePixelRatio = 1, HardwareConcurrency = 16, DeviceMemory = 16,
    };

    public ProfileSourceTests()
    {
        _dir = _sb.TempHome();
        _sb.Env("CLEARCOTE_PROFILE_DIR", _dir).Env("CLEARCOTE_PROFILE_API", null).Env("CLEARCOTE_LICENSE_API", null);
        _http = new FakeHandler(req =>
        {
            var q = System.Web.HttpUtility.ParseQueryString(req.RequestUri!.Query);
            if (q["id"] is { } id)
                return Json(_profileStatus, _profileStatus == 200
                    ? "{\"id\":\"" + id + "\",\"profile\":{\"navigator\":{\"user_agent\":\"ua\"},\"screen\":{\"width\":1920}}}"
                    : """{"error":"nope"}""");
            var now = DateTime.UtcNow.ToString("o");
            return Json(_indexStatus, _indexStatus == 200
                ? $$"""
                  {"candidates":[
                    {"id":"p-best","os_family":"windows","browser_major":150,"gpu_vendor":"intel","screen_width":1920,"screen_height":1080,
                     "device_pixel_ratio":1,"hardware_concurrency":16,"device_memory":16,"encoded_size":9000,"captured_at":"{{now}}"},
                    {"id":"p-nvidia","os_family":"windows","browser_major":150,"gpu_vendor":"nvidia","screen_width":1920,"screen_height":1080,
                     "device_pixel_ratio":1,"hardware_concurrency":16,"device_memory":16,"encoded_size":9000,"captured_at":"{{now}}"}]}
                  """
                : """{"error":"nope"}""");
        });
        _sb.Http(_http);
    }

    private static HttpResponseMessage Json(int status, string body) => new((HttpStatusCode)status) { Content = new StringContent(body) };

    public void Dispose() => _sb.Dispose();

    private static ProfileSourceOptions Opts(Action<ProfileSourceOptions>? over = null)
    {
        var o = new ProfileSourceOptions { LicenseKey = "cc_lic_test", ApiBase = Base };
        over?.Invoke(o);
        return o;
    }

    [Fact]
    public void HostOsFamily_maps_onto_corpus_values() => Assert.Contains(ProfileSource.HostOsFamily(), new[] { "windows", "macos", "linux" });

    [Fact]
    public async Task Sends_the_license_as_a_bearer_token()
    {
        await ProfileSource.FetchIndexAsync(Host, Opts());
        Assert.Equal("Bearer cc_lic_test", _http.Requests[0].Headers.Authorization!.ToString());
    }

    [Fact]
    public async Task Refuses_to_call_the_service_with_no_key_and_says_why()
    {
        _sb.Env("CLEARCOTE_LICENSE_KEY", null);
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => ProfileSource.FetchIndexAsync(Host, new ProfileSourceOptions { ApiBase = Base }));
        Assert.Contains("license key", e.Message);
        Assert.Empty(_http.Requests);
    }

    [Fact]
    public async Task The_key_and_endpoint_fall_back_to_the_environment()
    {
        _sb.Env("CLEARCOTE_LICENSE_KEY", "cc_lic_env").Env("CLEARCOTE_PROFILE_API", Base + "/");
        await ProfileSource.FetchIndexAsync(Host);
        Assert.Equal("Bearer cc_lic_env", _http.Requests[0].Headers.Authorization!.ToString());
        Assert.StartsWith(Base + "/api/v1/profiles?", _http.Requests[0].RequestUri!.ToString());
    }

    [Fact]
    public async Task Requests_the_hosts_os_and_chromium_major()
    {
        await ProfileSource.FetchIndexAsync(Host, Opts());
        var url = _http.Requests[0].RequestUri!.ToString();
        Assert.Contains("os=windows", url);
        Assert.Contains("major=150", url);
        // an OS override asks for that OS
        await ProfileSource.FetchIndexAsync(Host, Opts(o => o.Os = "Linux"));
        Assert.Contains("os=linux", _http.Requests[1].RequestUri!.ToString());
    }

    [Fact]
    public async Task Falls_back_to_a_stale_cached_index_rather_than_failing_the_launch()
    {
        await ProfileSource.FetchIndexAsync(Host, Opts());   // populate the cache
        _indexStatus = 503;
        Assert.Equal(2, (await ProfileSource.FetchIndexAsync(Host, Opts())).Count);
    }

    [Fact]
    public async Task An_index_failure_with_no_cache_names_the_status()
    {
        _indexStatus = 429;
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => ProfileSource.FetchIndexAsync(Host, Opts()));
        Assert.Contains("profile index request failed (429)", e.Message);
    }

    [Fact]
    public async Task An_unreachable_service_serves_the_cached_index_too()
    {
        await ProfileSource.FetchIndexAsync(Host, Opts());
        _sb.Http(new FakeHandler(_ => throw new HttpRequestException("connection refused")));
        Assert.Equal(2, (await ProfileSource.FetchIndexAsync(Host, Opts())).Count);
    }

    [Fact]
    public async Task Offline_uses_the_cache_and_never_touches_the_network()
    {
        await ProfileSource.FetchIndexAsync(Host, Opts());
        _http.Requests.Clear();
        Assert.Equal(2, (await ProfileSource.FetchIndexAsync(Host, Opts(o => o.Offline = true))).Count);
        Assert.Empty(_http.Requests);
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            ProfileSource.FetchIndexAsync(new HostFacts { OsFamily = "linux", BrowserMajor = 151 }, Opts(o => o.Offline = true)));
        Assert.Contains("offline: no cached profile index for linux/151", e.Message);
    }

    [Fact]
    public async Task The_index_cache_is_the_shared_snake_case_file()
    {
        await ProfileSource.FetchIndexAsync(Host, Opts());
        var cached = JsonNode.Parse(File.ReadAllText(Path.Combine(_dir, "library", "index-windows-150.json")))!.AsArray();
        Assert.Equal("p-best", cached[0]!["id"]!.GetValue<string>());
        Assert.Equal("intel", cached[0]!["gpu_vendor"]!.GetValue<string>());
    }

    [Fact]
    public async Task Caches_a_profile_on_disk_so_a_repeat_costs_no_request()
    {
        await ProfileSource.FetchProfileAsync("p-cache-1", Opts());
        Assert.Single(_http.Requests);
        _http.Requests.Clear();
        var again = await ProfileSource.FetchProfileAsync("p-cache-1", Opts());
        Assert.Empty(_http.Requests);
        Assert.NotNull(again["navigator"]);
    }

    [Fact]
    public async Task Surfaces_the_services_error_on_a_failed_fetch()
    {
        _profileStatus = 429;
        Assert.Contains("429", (await Assert.ThrowsAsync<InvalidOperationException>(() => ProfileSource.FetchProfileAsync("p-limited", Opts()))).Message);
    }

    [Fact]
    public async Task Offline_without_a_cached_copy_fails_clearly()
        => Assert.Contains("not cached", (await Assert.ThrowsAsync<InvalidOperationException>(() =>
            ProfileSource.FetchProfileAsync("p-never-seen", Opts(o => o.Offline = true)))).Message);

    [Fact]
    public async Task A_profile_id_cannot_name_a_file_outside_the_cache()
        => await Assert.ThrowsAsync<ArgumentException>(() => ProfileSource.FetchProfileAsync("../x", Opts()));

    [Fact]
    public async Task Picks_the_host_coherent_candidate_not_merely_the_first()
    {
        // p-best matches the host's Intel GPU; p-nvidia does not. GPU dominates the score.
        var r = await ProfileSource.ResolveAutoProfileAsync(Host, Opts(o => o.Mode = SelectMode.Best));
        Assert.Equal("p-best", r.Selection.Entry.Id);
        Assert.NotNull(r.Profile["navigator"]);
        Assert.True(File.Exists(Path.Combine(_dir, "library", "profile-p-best.json")));
    }

    [Fact]
    public async Task Is_sticky_the_same_key_resolves_to_the_same_profile()
    {
        var a = await ProfileSource.ResolveAutoProfileAsync(Host, Opts(o => o.Key = "acct-7"));
        var b = await ProfileSource.ResolveAutoProfileAsync(Host, Opts(o => o.Key = "acct-7"));
        Assert.Equal(a.Selection.Entry.Id, b.Selection.Entry.Id);
    }

    // ── host facts cache ────────────────────────────────────────────────────

    [Fact]
    public void The_same_binary_spelled_differently_hits_the_host_cache()
    {
        var exe = Path.Combine(_dir, "bin", "chrome");
        Directory.CreateDirectory(Path.GetDirectoryName(exe)!);
        File.WriteAllText(exe, "");
        ProfileSource.WriteCachedHost(exe, Host);
        var spelled = Path.Combine(_dir, "bin", ".", "chrome");
        var hit = ProfileSource.ReadCachedHost(spelled);
        Assert.NotNull(hit);
        Assert.Equal("intel", hit!.GpuVendor);
        Assert.Equal(3440, hit.ScreenWidth);
        Assert.Null(ProfileSource.ReadCachedHost(Path.Combine(_dir, "bin", "other")));   // another binary: re-measure
    }

    [Fact]
    public void A_host_cache_older_than_30_days_is_ignored_in_either_SDKs_spelling()
    {
        var exe = Path.Combine(_dir, "chrome");
        var path = Path.Combine(_dir, "library", "host.json");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        void Write(JsonNode at) => File.WriteAllText(path, new JsonObject
        {
            ["os_family"] = "linux", ["browser_major"] = 150, ["gpu_vendor"] = "amd", ["screen_width"] = 1920, ["screen_height"] = 1080,
            ["_exe"] = exe, ["_at"] = at,
        }.ToJsonString());
        Write(DateTime.UtcNow.AddDays(-1).ToString("o"));   // Node writes an ISO time
        Assert.Equal("amd", ProfileSource.ReadCachedHost(exe)!.GpuVendor);
        Write(DateTimeOffset.UtcNow.AddDays(-1).ToUnixTimeSeconds());   // Python writes epoch seconds
        Assert.NotNull(ProfileSource.ReadCachedHost(exe));
        Write(DateTime.UtcNow.AddDays(-31).ToString("o"));
        Assert.Null(ProfileSource.ReadCachedHost(exe));
        Write(DateTimeOffset.UtcNow.AddDays(-31).ToUnixTimeSeconds());
        Assert.Null(ProfileSource.ReadCachedHost(exe));
    }

    [Fact]
    public void Reads_the_major_off_the_running_browser_and_falls_back_on_junk()
    {
        Assert.Equal(151, ProfileSource.EngineMajor(FakeBrowser.With("151.0.7922.40"), 150));
        Assert.Equal(150, ProfileSource.EngineMajor(FakeBrowser.With("not a version"), 150));
        Assert.Equal(150, ProfileSource.EngineMajor(FakeBrowser.With(null), 150));
    }

    [Fact]
    public async Task MeasureHost_uses_the_cache_without_launching()
    {
        var exe = Path.Combine(_dir, "chrome");
        ProfileSource.WriteCachedHost(exe, Host);
        var launched = 0;
        var h = await ProfileSource.MeasureHostAsync(_ => { launched++; throw new InvalidOperationException("no launch"); }, exe, 150);
        Assert.Equal(0, launched);
        Assert.Equal(16, h.HardwareConcurrency);
    }
}

/// An IBrowser whose Version is all there is; every other member throws.
public class FakeBrowser : DispatchProxy
{
    public string? VersionValue { get; set; }

    public static IBrowser With(string? version)
    {
        var b = Create<IBrowser, FakeBrowser>();
        ((FakeBrowser)(object)b).VersionValue = version;
        return b;
    }

    protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        => targetMethod?.Name == "get_Version" ? VersionValue : throw new NotSupportedException(targetMethod?.Name);
}
