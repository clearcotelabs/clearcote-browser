using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Widevine ON by default for licensed (PRO) launches, opt-in for free ones (0.48.0): <see cref="Widevine"/> and the
/// launches that use it. Mirrors the Python suite's tests/test_widevine_default.py and the Node suite's
/// test/widevine-default.test.ts.
/// </summary>
/// <remarks>
/// Hermetic: no network. Google's update check, the CDM download and the licence backend answer from
/// <see cref="FakeGoogle"/> (the SDK's HTTP test seam), the CDM cache is a temp directory (CLEARCOTE_WIDEVINE_DIR),
/// and the "engine" is a small file carrying (or not) the <c>widevine-cdm-path</c> switch name the probe looks for.
/// The launch tests run the real launches against a stand-in engine that writes down its command line and exits
/// (the launch then fails, which is all they need); the stand-in is a shell script, so those are POSIX-only.
/// </remarks>
public class WidevineTests : IDisposable
{
    private const string Version = "4.10.9999.0";
    // Its own key per test: a paid lease is shared per key in the process, so a shared key would let a later
    // test reuse an earlier one's lease and make no licence call at all.
    private readonly string Key = "cc_lic_test_widevine_default_" + Guid.NewGuid().ToString("N")[..12];
    private const string DownloadUrl = "https://example.invalid/cdm.crx3";
    // An engine with the --widevine-cdm-path switch (1022) AND CDM host verification (r63+): the licensed default turns
    // Widevine on on every host. Engine1022Unverified (r32..r62) keeps the default off on Windows and macOS hosts.
    private static readonly byte[] Engine1022 = Encoding.Latin1.GetBytes("\0widevine-cdm-path\0VerifyCdmHost_0\0");
    private static readonly byte[] Engine1022Unverified = Encoding.Latin1.GetBytes("\0widevine-cdm-path\0");
    private static readonly bool WinOrMac = OperatingSystem.IsWindows() || OperatingSystem.IsMacOS();
    private static readonly bool Linux = OperatingSystem.IsLinux();
    // This host's own CDM layout (read before any test fakes the OS): linux_x64, win_x64, or mac_arm64 on a Mac.
    private static readonly string Sub = Widevine.CdmPlatform().Subdir;
    private static readonly string CdmFile = Widevine.CdmPlatform().FileName;

    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();
    private readonly string _wv;
    private readonly TextWriter _stderr = Console.Error;
    private readonly StringWriter _err = new();

    public WidevineTests()
    {
        _sb.TempHome();
        _wv = Path.Combine(Dir("cc-wv-cache-"), "wv");
        _sb.Env("CLEARCOTE_WIDEVINE_DIR", _wv).Env("CLEARCOTE_WIDEVINE", null).Env("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", null)
            .Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_NO_WARN", null).Env("CLEARCOTE_CLOUD", null)
            .Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null)
            .Env("CLEARCOTE_PERSONA_ENV", null).Env("CLEARCOTE_INSTANCE_ID", "cc-wv-test");
        _sb.Http(new FakeGoogle { Unexpected = true }); // any network call a test did not set up fails
        Widevine.ResetDefaultState();
        LaunchWarnings.ForgetSaid();
        Console.SetError(_err);
    }

    public void Dispose()
    {
        Console.SetError(_stderr);
        Widevine.ResetDefaultState();
        LaunchWarnings.ForgetSaid();
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    private FakeGoogle Google(FakeGoogle g)
    {
        _sb.Http(g);
        return g;
    }

    private string[] Warnings() =>
        _err.ToString().Split('\n').Where(l => l.Contains("Widevine is on by default for licensed launches", StringComparison.Ordinal)).ToArray();

    /// A CDM already in the cache; <paramref name="stamp"/>: Google was asked just now.
    private string CachedCdm(string version = Version, bool stamp = true, bool complete = true)
    {
        var d = Path.Combine(_wv, version);
        Directory.CreateDirectory(Path.Combine(d, "_platform_specific", Sub));
        File.WriteAllText(Path.Combine(d, "manifest.json"), "{}");
        if (complete) File.WriteAllText(Path.Combine(d, "_platform_specific", Sub, CdmFile), "cdm");
        if (stamp) File.WriteAllText(Path.Combine(_wv, Widevine.StampFile), "now");
        return d;
    }

    private string FakeEngine(bool with1022, string name = "chrome", bool hostVerified = true)
    {
        var dir = Dir("cc-wv-engine-");
        var exe = Path.Combine(dir, name);
        var body = new List<byte>(Encoding.Latin1.GetBytes("\x7f" + "ELF"));
        if (with1022) body.AddRange(hostVerified ? Engine1022 : Engine1022Unverified);
        body.AddRange(Encoding.Latin1.GetBytes("\0--remote-debugging-port\0"));
        File.WriteAllBytes(exe, body.ToArray());
        if (OperatingSystem.IsWindows()) File.WriteAllBytes(Path.Combine(dir, "chrome.dll"), body.ToArray());
        return exe;
    }

    internal static byte[] Crx(Dictionary<string, byte[]> files)
    {
        using var ms = new MemoryStream();
        using (var zip = new ZipArchive(ms, ZipArchiveMode.Create, leaveOpen: true))
            foreach (var (name, data) in files)
            {
                using var s = zip.CreateEntry(name).Open();
                s.Write(data);
            }
        var head = new List<byte>(Encoding.ASCII.GetBytes("Cr24"));
        head.AddRange(BitConverter.GetBytes(3u));
        head.AddRange(BitConverter.GetBytes(4u));
        head.AddRange(new byte[] { 1, 2, 3, 4 });
        return head.Concat(ms.ToArray()).ToArray();
    }

    private static byte[] CdmCrx() => Crx(new()
    {
        ["manifest.json"] = Encoding.ASCII.GetBytes($"{{\"version\":\"{Version}\"}}"),
        [$"_platform_specific/{Sub}/{CdmFile}"] = Encoding.ASCII.GetBytes("\x7f" + "ELF fake cdm"),
    });

    /// Google's update server + download host, and the licence backend: counts calls, can fail, hang (until
    /// cancelled), delay the download or serve a bad hash.
    internal sealed class FakeGoogle : HttpMessageHandler
    {
        public byte[] Blob = CdmCrx();
        public string NextVersion = Version;   // the version the update check names (S15: untrusted)
        public string? Sha;
        public bool Fail, Hang, Unexpected;
        public int DownloadDelayMs;
        public int Checks, Downloads, Cancelled, Licence;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            var url = request.RequestUri!.ToString();
            if (url.StartsWith("http://test.local", StringComparison.Ordinal))
            {
                Interlocked.Increment(ref Licence);
                var body = url.EndsWith("/checkout", StringComparison.Ordinal) ? CheckoutJson() : "{}";
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(body) };
            }
            if (Unexpected || (url != Widevine.OmahaUrl && url != DownloadUrl))
                throw new InvalidOperationException($"unexpected request {url}");   // e.g. the release catalog: answered at once
            if (Hang)
            {
                try { await Task.Delay(Timeout.Infinite, ct); }
                catch (OperationCanceledException) { Interlocked.Increment(ref Cancelled); throw; }
            }
            if (url == Widevine.OmahaUrl)
            {
                Interlocked.Increment(ref Checks);
                if (Fail) throw new HttpRequestException("Google said 403");
                var sha = Sha ?? Convert.ToHexString(SHA256.HashData(Blob)).ToLowerInvariant();
                var json = $"{{\"response\":{{\"app\":[{{\"nextversion\":{JsonSerializer.Serialize(NextVersion)},\"updatecheck\":{{\"status\":\"ok\",\"pipelines\":[{{\"operations\":[{{\"urls\":[{{\"url\":\"{DownloadUrl}\"}}],\"out\":{{\"sha256\":\"{sha}\"}}}}]}}]}}}}]}}}}";
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(json) };
            }
            if (url == DownloadUrl)
            {
                Interlocked.Increment(ref Downloads);
                if (DownloadDelayMs > 0) await Task.Delay(DownloadDelayMs, ct);
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(Blob) };
            }
            throw new InvalidOperationException($"unexpected request {url}");
        }

        private static string CheckoutJson()
        {
            var payload = Convert.ToBase64String(JsonSerializer.SerializeToUtf8Bytes(new { v = 1, plan = "pro", n = 1 }))
                .TrimEnd('=').Replace('+', '-').Replace('/', '_');
            return JsonSerializer.Serialize(new
            {
                lease_id = "L1", token = payload + ".sig", exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 900, lease_ttl_sec = 810,
                heartbeat_interval_sec = 270, concurrency = new { used = 1, limit = 5 },
            });
        }
    }

    // ── the rules ────────────────────────────────────────────────────────────

    [Theory]
    [InlineData(null, true, null, WidevineMode.Default)]
    [InlineData(null, false, null, WidevineMode.Off)]
    [InlineData(true, true, null, WidevineMode.Explicit)]
    [InlineData(true, false, null, WidevineMode.Explicit)]
    [InlineData(false, true, null, WidevineMode.Off)]
    [InlineData(false, false, null, WidevineMode.Off)]
    [InlineData(null, true, "0", WidevineMode.Off)]
    [InlineData(null, true, "false", WidevineMode.Off)]
    [InlineData(null, true, "OFF", WidevineMode.Off)]
    [InlineData(null, true, "no", WidevineMode.Off)]
    [InlineData(null, false, "1", WidevineMode.Explicit)]
    [InlineData(null, false, "true", WidevineMode.Explicit)]
    [InlineData(null, true, "on", WidevineMode.Explicit)]
    [InlineData(null, false, "yes", WidevineMode.Explicit)]
    [InlineData(true, true, "0", WidevineMode.Explicit)]   // an explicit option wins over the environment
    [InlineData(false, false, "1", WidevineMode.Off)]
    [InlineData(null, true, "maybe", WidevineMode.Default)] // an unknown value is ignored
    [InlineData(null, false, "maybe", WidevineMode.Off)]
    [InlineData(null, true, "", WidevineMode.Default)]
    public void Mode_matrix(bool? option, bool licensed, string? env, WidevineMode expected)
        => Assert.Equal(expected, Widevine.Mode(option, licensed, k => k == "CLEARCOTE_WIDEVINE" ? env : null));

    [Fact]
    public void Mode_reads_the_process_environment()
    {
        _sb.Env("CLEARCOTE_WIDEVINE", "0");
        Assert.Equal(WidevineMode.Off, Widevine.Mode(null, true));
        _sb.Env("CLEARCOTE_WIDEVINE", "1");
        Assert.Equal(WidevineMode.Explicit, Widevine.Mode(null, false));
    }

    [Theory]
    [InlineData(null, 15)]
    [InlineData("3", 3)]
    [InlineData("0.5", 1)]
    [InlineData("0", 15)]
    [InlineData("-2", 15)]
    [InlineData("soon", 15)]
    [InlineData("", 15)]
    [InlineData("600", 600)]
    [InlineData("601", 600)]
    [InlineData("1e10", 600)]
    [InlineData("Infinity", 600)]
    [InlineData("NaN", 15)]
    [InlineData("-Infinity", 15)]
    // clamped to 1-600 s: TimeSpan/Task.Delay throw above ~49.7 days
    public void Timeout_seconds(string? value, double expected)
        => Assert.Equal(expected, Widevine.TimeoutSeconds(k => k == "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS" ? value : null));

    [Fact]
    public void The_names_match_the_other_sdks()
    {
        Assert.Equal("CLEARCOTE_WIDEVINE", Widevine.WidevineEnv);
        Assert.Equal("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", Widevine.TimeoutEnv);
        Assert.Equal(15, Widevine.DefaultTimeoutSeconds);
        Assert.Equal("oimompecagnajdejgnnjijobebaeigek", Widevine.AppId);
        Assert.StartsWith("https://update.googleapis.com/", Widevine.OmahaUrl);
        Assert.Equal("latest-component-updated-widevine-cdm", Widevine.HintFile);
    }

    [Fact]
    public void Omaha_body_and_both_response_shapes()
    {
        using var doc = JsonDocument.Parse(Widevine.OmahaRequestBody());
        var req = doc.RootElement.GetProperty("request");
        Assert.Equal("crx3", req.GetProperty("acceptformat").GetString());
        Assert.Equal(Widevine.AppId, req.GetProperty("app")[0].GetProperty("appid").GetString());
        Assert.Equal("0.0.0.0", req.GetProperty("app")[0].GetProperty("version").GetString());
        Assert.Equal(("https://x/cdm.crx3", "abcd", "4.10.3050.0"), Widevine.ParseUpdate(
            "{\"response\":{\"app\":[{\"nextversion\":\"4.10.3050.0\",\"updatecheck\":{\"status\":\"ok\",\"pipelines\":[{\"operations\":[{\"urls\":[{\"url\":\"https://x/cdm.crx3\"}],\"out\":{\"sha256\":\"abcd\"}}]}]}}]}}"));
        Assert.Equal(("https://x/dl/cdm.crx3", "beef", "4.10.3050.0"), Widevine.ParseUpdate(
            ")]}'\n{\"response\":{\"app\":[{\"updatecheck\":{\"status\":\"ok\",\"urls\":{\"url\":[{\"codebase\":\"https://x/dl/\"}]},\"manifest\":{\"version\":\"4.10.3050.0\",\"packages\":{\"package\":[{\"name\":\"cdm.crx3\",\"hash_sha256\":\"beef\"}]}}}}]}}"));
        Assert.Throws<InvalidOperationException>(() => Widevine.ParseUpdate("{\"response\":{\"app\":[{\"updatecheck\":{\"status\":\"noupdate\"}}]}}"));
        var zip = new byte[] { (byte)'P', (byte)'K', 3, 4 };
        Assert.Equal(zip, Widevine.Crx3ToZip(zip));
    }

    // ── the cache ────────────────────────────────────────────────────────────

    [Fact]
    public void Cached_cdm_picks_the_newest_complete_version()
    {
        Assert.Null(Widevine.CachedCdm(_wv));
        CachedCdm("4.10.100.0");
        var newest = CachedCdm("4.10.2830.0");
        CachedCdm("4.10.9000.0", complete: false);   // half-extracted: never used
        CachedCdm("current");
        Directory.CreateDirectory(Path.Combine(_wv, ".tmp-abc"));
        Assert.Equal(newest, Widevine.CachedCdm(_wv));
    }

    [Fact]
    public async Task Fetch_installs_verifies_and_stamps()
    {
        var g = Google(new FakeGoogle());
        var dir = await Widevine.FetchAsync(quiet: true);
        Assert.Equal(Path.Combine(_wv, Version), dir);
        Assert.True(File.Exists(Path.Combine(dir, "_platform_specific", Sub, CdmFile)));
        Assert.Equal(new[] { Widevine.StampFile, Version }, FakeBuildServer.Entries(_wv));
        Assert.Equal((1, 1), (g.Checks, g.Downloads));
    }

    [Fact]
    public async Task Fetch_does_not_download_a_cdm_it_already_has()
    {
        var g = Google(new FakeGoogle());
        await Widevine.FetchAsync(quiet: true);
        await Widevine.FetchAsync(quiet: true);
        Assert.Equal((2, 1), (g.Checks, g.Downloads));   // FetchAsync always asks; it downloads once
    }

    [Fact]
    public async Task Fetch_refuses_a_bad_hash_and_leaves_nothing()
    {
        Google(new FakeGoogle { Sha = new string('0', 64) });
        var e = await Assert.ThrowsAsync<InvalidDataException>(() => Widevine.FetchAsync(quiet: true));
        Assert.Contains("sha256 mismatch", e.Message);
        Assert.Null(Widevine.CachedCdm(_wv));
        Assert.DoesNotContain(FakeBuildServer.Entries(_wv), n => n.StartsWith(".tmp-") || n.StartsWith(".trash-") || n.StartsWith(".install-lock"));
    }

    [Fact]
    public async Task Fetch_refuses_an_update_without_a_hash()
    {
        Google(new FakeGoogle { Sha = "" });
        // an empty sha in the pipelines shape is skipped, so the response has no download URL at all
        await Assert.ThrowsAnyAsync<Exception>(() => Widevine.FetchAsync(quiet: true));
        Assert.Null(Widevine.CachedCdm(_wv));
    }

    [Fact]
    public async Task Fetch_rejects_a_crx_without_the_cdm()
    {
        Google(new FakeGoogle { Blob = Crx(new() { ["manifest.json"] = Encoding.ASCII.GetBytes("{}") }) });
        var e = await Assert.ThrowsAsync<InvalidDataException>(() => Widevine.FetchAsync(quiet: true));
        Assert.Contains($"{CdmFile} not at", e.Message);
        Assert.Null(Widevine.CachedCdm(_wv));
    }

    [Fact]
    public async Task Fetch_replaces_a_half_extracted_tree_and_sweeps_an_interrupted_install()
    {
        CachedCdm(Version, stamp: false, complete: false);   // an older SDK extracted in place and died
        File.WriteAllText(Path.Combine(_wv, Version, "stray"), "x");
        Directory.CreateDirectory(Path.Combine(_wv, ".tmp-dead"));
        Google(new FakeGoogle());
        var dir = await Widevine.FetchAsync(quiet: true);
        Assert.True(Widevine.ValidCdm(dir));
        Assert.False(File.Exists(Path.Combine(dir, "stray")));
        Assert.DoesNotContain(FakeBuildServer.Entries(_wv), n => n.StartsWith(".tmp-") || n.StartsWith(".trash-"));
    }

    [Fact]
    public async Task Concurrent_first_fetches_download_once_and_never_expose_a_partial_cdm()
    {
        var g = Google(new FakeGoogle { DownloadDelayMs = 600 });
        var all = Task.WhenAll(Enumerable.Range(0, 6).Select(_ => Task.Run(() => Widevine.FetchAsync(quiet: true))));
        var sw = Stopwatch.StartNew();
        while (Volatile.Read(ref g.Downloads) == 0 && sw.ElapsedMilliseconds < 10_000) await Task.Delay(10);
        Assert.Null(Widevine.CachedCdm(_wv));   // mid-install: nothing a reader could load yet
        Assert.True(File.Exists(Path.Combine(_wv, ".install-lock")));
        var dirs = await all;
        Assert.Equal(new[] { Path.Combine(_wv, Version) }, dirs.Distinct().ToArray());
        Assert.Equal(1, g.Downloads);   // the others waited at the install lock, then found it installed
        Assert.False(File.Exists(Path.Combine(_wv, ".install-lock")));
    }

    [Fact]
    public async Task Resolve_uses_a_fresh_cache_without_any_network()
    {
        var dir = CachedCdm();
        Assert.Equal(dir, await Widevine.ResolveAsync(quiet: true));   // the default handler fails any request
    }

    [Fact]
    public async Task Resolve_rechecks_a_stale_cache_and_keeps_it_when_offline()
    {
        var dir = CachedCdm(stamp: false);
        var g = Google(new FakeGoogle { Fail = true });
        Assert.Equal(dir, await Widevine.ResolveAsync(quiet: true));
        Assert.Equal(1, g.Checks);
    }

    [Fact]
    public async Task Resolve_without_a_cache_throws_when_offline()
    {
        Google(new FakeGoogle { Fail = true });
        await Assert.ThrowsAsync<HttpRequestException>(() => Widevine.ResolveAsync(quiet: true));
    }

    [Fact]
    public async Task An_old_stamp_is_not_fresh()
    {
        CachedCdm();
        File.SetLastWriteTimeUtc(Path.Combine(_wv, Widevine.StampFile), DateTime.UtcNow - TimeSpan.FromHours(25));
        var g = Google(new FakeGoogle());
        await Widevine.ResolveAsync(quiet: true);
        Assert.Equal(1, g.Checks);
    }

    // ── the default: bounded, fail-soft, warns once ──────────────────────────

    [Fact]
    public async Task Default_is_fail_soft_and_warns_once()
    {
        var g = Google(new FakeGoogle { Fail = true });
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: false));
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: false));   // within the retry window: no second attempt
        var w = Assert.Single(Warnings());
        Assert.Contains("Google said 403", w);
        // the advice leads with the variable, which works for every launch (cloud and Docker included)
        Assert.True(w.IndexOf("CLEARCOTE_WIDEVINE=0", StringComparison.Ordinal) < w.IndexOf("Widevine = false", StringComparison.Ordinal));
        Assert.Equal(1, g.Checks);
    }

    [Fact]
    public async Task Default_quiet_says_nothing_and_does_not_use_the_warning_up()
    {
        Google(new FakeGoogle { Fail = true });
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: true));
        Assert.Equal("", _err.ToString());
        Widevine.ResetDefaultState();
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: false));
        Assert.Single(Warnings());
    }

    [Fact]
    public async Task Default_with_no_warn_says_nothing()
    {
        _sb.Env("CLEARCOTE_NO_WARN", "1");
        Google(new FakeGoogle { Fail = true });
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: false));
        Assert.Equal("", _err.ToString());
    }

    [Fact]
    public async Task Default_is_bounded_and_cancels_the_fetch_it_stopped_waiting_for()
    {
        var g = Google(new FakeGoogle { Hang = true });
        var sw = Stopwatch.StartNew();
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: false, TimeSpan.FromMilliseconds(200)));
        Assert.True(sw.ElapsedMilliseconds < 3000, $"waited {sw.ElapsedMilliseconds} ms");
        Assert.Contains("not ready within 0.2 s", Assert.Single(Warnings()));
        for (var i = 0; i < 100 && Volatile.Read(ref g.Cancelled) == 0; i++) await Task.Delay(20);
        Assert.Equal(1, g.Cancelled);   // nothing keeps working for a launch that went on without it
        sw.Restart();
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: true, TimeSpan.FromSeconds(5)));   // a launch meanwhile does not wait again
        Assert.True(sw.ElapsedMilliseconds < 1000);
    }

    [Fact]
    public async Task Default_takes_its_timeout_from_the_environment()
    {
        _sb.Env("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "0.2");
        Google(new FakeGoogle { Hang = true });
        var sw = Stopwatch.StartNew();
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: true));
        Assert.True(sw.ElapsedMilliseconds < 3000, $"waited {sw.ElapsedMilliseconds} ms");
    }

    [Fact]
    public async Task Default_retries_after_the_window()
    {
        var g = Google(new FakeGoogle { Fail = true });
        await Widevine.DefaultCdmAsync(quiet: true);
        Widevine.AgeLastFailure(Widevine.RetryAfter + TimeSpan.FromSeconds(1));
        await Widevine.DefaultCdmAsync(quiet: true);
        Assert.Equal(2, g.Checks);
    }

    [Fact]
    public async Task Default_uses_a_stale_cache_at_once_refreshes_it_in_the_background_and_records_a_failed_check()
    {
        // S12: a stamp over a day old used to make the launch wait for Google, then warn "launching without it"
        var dir = CachedCdm(stamp: false);
        var g = Google(new FakeGoogle { Fail = true });
        var sw = Stopwatch.StartNew();
        Assert.Equal(dir, await Widevine.DefaultCdmAsync(quiet: false));
        Assert.Equal(dir, await Widevine.DefaultCdmAsync(quiet: false));   // a second launch meanwhile: no second refresh
        Assert.True(sw.ElapsedMilliseconds < 1000, $"waited {sw.ElapsedMilliseconds} ms");
        await (Widevine.RefreshInFlight() ?? Task.CompletedTask).WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(1, g.Checks);
        Assert.Equal("", _err.ToString());   // Widevine works: nothing to warn about, no progress lines
        Assert.Equal(dir, await Widevine.DefaultCdmAsync(quiet: false));   // the failed check counts: none for a day
        Assert.Equal(1, g.Checks);
    }

    [Fact]
    public async Task Default_with_a_stale_cache_never_waits_on_a_blackholed_network()
    {
        var dir = CachedCdm(stamp: false);
        Google(new FakeGoogle { Hang = true });
        var sw = Stopwatch.StartNew();
        Assert.Equal(dir, await Widevine.DefaultCdmAsync(quiet: false, TimeSpan.FromSeconds(5)));
        Assert.True(sw.ElapsedMilliseconds < 1000, $"waited {sw.ElapsedMilliseconds} ms");
        Assert.Empty(Warnings());
        Assert.NotNull(Widevine.RefreshInFlight());   // the background check, cancelled at reset
    }

    [Fact]
    public async Task A_background_refresh_installs_a_newer_cdm_for_the_next_launch()
    {
        var old = CachedCdm("4.10.1.0", stamp: false);
        var g = Google(new FakeGoogle());   // offers Version, newer than 4.10.1.0
        Assert.Equal(old, await Widevine.DefaultCdmAsync(quiet: true));   // this launch: the cached one, at once
        await (Widevine.RefreshInFlight() ?? Task.CompletedTask).WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal((1, 1), (g.Checks, g.Downloads));
        Assert.Equal(Path.Combine(_wv, Version), await Widevine.DefaultCdmAsync(quiet: true));   // the next one: the new CDM
        Assert.Equal(1, g.Checks);
    }

    [Fact]
    public async Task Default_with_a_fresh_cache_costs_no_network()
    {
        var dir = CachedCdm();
        Assert.Equal(dir, await Widevine.DefaultCdmAsync(quiet: false));
        Assert.Null(Widevine.RefreshInFlight());
    }

    [Fact]
    public async Task The_default_path_prints_nothing_but_the_warning()
    {
        Google(new FakeGoogle());
        Assert.Equal(Path.Combine(_wv, Version), await Widevine.DefaultCdmAsync(quiet: false));
        Assert.Equal("", _err.ToString());   // S17: no "[clearcote] [widevine] fetching/installed" lines
    }

    // ── an untrusted version, the Windows rename ─────────────────────────────

    [Theory]
    [InlineData("../victim")]
    [InlineData("4.10.1.0/../../victim")]
    [InlineData("/tmp/victim")]
    [InlineData("4")]
    [InlineData("4.10.1.0.5")]
    [InlineData("4.10.x.0")]
    [InlineData(" 4.10.1.0")]
    [InlineData("4.10.1.0\n")]
    [InlineData("٤.10.1.0")]
    public async Task An_unexpected_version_never_becomes_a_path(string version)
    {
        // S15: the version in Google's response named the directory the install moved aside and deleted
        var victim = Path.Combine(Path.GetDirectoryName(_wv)!, "victim");
        Directory.CreateDirectory(victim);
        File.WriteAllText(Path.Combine(victim, "precious.txt"), "keep me");
        Directory.CreateDirectory(_wv);
        Google(new FakeGoogle { NextVersion = version });
        var e = await Assert.ThrowsAsync<InvalidDataException>(() => Widevine.FetchAsync(quiet: true));
        Assert.Contains("unexpected version", e.Message);
        Assert.Equal("keep me", File.ReadAllText(Path.Combine(victim, "precious.txt")));
        Assert.Null(await Widevine.DefaultCdmAsync(quiet: true));   // the default path: fail-soft, nothing deleted
        Assert.Equal("keep me", File.ReadAllText(Path.Combine(victim, "precious.txt")));
    }

    [Theory]
    [InlineData("4.10")]
    [InlineData("4.10.2830")]
    [InlineData("4.10.2830.0")]
    public async Task A_version_that_looks_like_one_is_used(string version)
    {
        Google(new FakeGoogle { NextVersion = version });
        Assert.Equal(Path.Combine(_wv, version), await Widevine.FetchAsync(quiet: true));
    }

    [Fact]
    public async Task No_version_still_installs_as_current()
    {
        Google(new FakeGoogle { NextVersion = "" });
        Assert.Equal(Path.Combine(_wv, "current"), await Widevine.FetchAsync(quiet: true));
    }

    [Fact]
    public async Task The_install_moves_go_through_downloads_windows_retry()
    {
        // S14: Download.MoveDir retries while a Windows scanner holds the fresh tree; the CDM install uses it
        var seen = new List<(string Src, string Dst)>();
        Download.BeforeMoveDirHook = (src, dst) => seen.Add((Path.GetFileName(src)[..5], Path.GetFileName(dst)));
        try
        {
            CachedCdm(Version, stamp: false, complete: false);   // an incomplete tree is moved aside first
            Google(new FakeGoogle());
            await Widevine.FetchAsync(quiet: true);
        }
        finally { Download.BeforeMoveDirHook = null; }
        Assert.Equal(new[] { Version[..5], ".tmp-" }, seen.Select(s => s.Src).ToArray());
        Assert.StartsWith(".trash-", seen[0].Dst);
        Assert.Equal(Version, seen[1].Dst);
    }

    // ── one launch's switch ──────────────────────────────────────────────────

    [Fact]
    public async Task Launch_args_per_mode()
    {
        var cdm = CachedCdm();
        var fresh = FakeEngine(true, "new");
        var old = FakeEngine(false, "old");
        var arg = new[] { $"--widevine-cdm-path={cdm}" };
        Assert.Equal(arg, await Widevine.LaunchArgsAsync(fresh, null, true, null, quiet: true));    // licensed default
        Assert.Empty(await Widevine.LaunchArgsAsync(fresh, null, false, null, quiet: true));        // free default: off
        Assert.Equal(arg, await Widevine.LaunchArgsAsync(fresh, true, false, null, quiet: true));   // asked for
        Assert.Empty(await Widevine.LaunchArgsAsync(fresh, false, true, null, quiet: true));        // turned off
        Assert.Empty(await Widevine.LaunchArgsAsync(old, null, true, null, quiet: true));           // no 1022: old default
        Assert.Empty(await Widevine.LaunchArgsAsync(old, true, true, null, quiet: true));           // (explicit seeds instead)
        var mine = new[] { "--widevine-cdm-path=/my/cdm" };
        Assert.Empty(await Widevine.LaunchArgsAsync(fresh, null, true, mine, quiet: true));         // the caller's own wins
        Assert.Empty(await Widevine.LaunchArgsAsync(fresh, true, true, mine, quiet: true));
    }

    [Fact]
    public async Task An_older_engine_costs_the_default_no_network()
        => Assert.Empty(await Widevine.LaunchArgsAsync(FakeEngine(false), null, true, null, quiet: false));

    [Fact]
    public void Cdm_host_verified_linux_always_windows_and_macos_only_with_the_symbol()
    {
        var verified = FakeEngine(true, "hv");
        var unverified = FakeEngine(true, "nohv", hostVerified: false);
        Assert.True(Widevine.CdmHostVerified(unverified, windowsOrMac: false));
        Assert.True(Widevine.CdmHostVerified(verified));
        Assert.Equal(!WinOrMac, Widevine.CdmHostVerified(unverified));
        Assert.False(Widevine.CdmHostVerified(null, windowsOrMac: true));
    }

    [Fact]
    public async Task Licensed_default_on_an_unverified_engine_is_off_on_windows_and_macos()
    {
        var cdm = CachedCdm();
        var unverified = FakeEngine(true, "nohv2", hostVerified: false);
        var arg = new[] { $"--widevine-cdm-path={cdm}" };
        var want = WinOrMac ? Array.Empty<string>() : arg;
        Assert.Equal(want, await Widevine.LaunchArgsAsync(unverified, null, true, null, quiet: false));
        Assert.Equal(want, await Widevine.LaunchArgsAsync(unverified, null, true, null, quiet: false));
        Assert.Equal(WinOrMac ? 1 : 0, _err.ToString().Split('\n').Count(l => l.Contains("clear client id", StringComparison.Ordinal))); // once per process
        Assert.Equal(arg, await Widevine.LaunchArgsAsync(unverified, true, true, null, quiet: true)); // asked for: on
    }

    [Fact]
    public async Task An_explicit_failure_stays_best_effort_without_the_defaults_warning()
    {
        Google(new FakeGoogle { Fail = true });
        Assert.Empty(await Widevine.LaunchArgsAsync(FakeEngine(true), true, false, null, quiet: false));
        Assert.Empty(Warnings());
    }

    [Fact]
    public void Component_updater_args_for_a_requested_launch()
    {
        _sb.Os("windows");
        var (ignore, args) = Widevine.ComponentUpdaterArgs(null, new[] { "--foo" });
        Assert.Equal(LaunchOpts.DefaultIgnoredArgs.Append("--disable-component-update"), ignore);   // the strip survives
        Assert.Equal(new[] { "--foo", "--component-updater=fast-update" }, args);
        (ignore, args) = Widevine.ComponentUpdaterArgs(new[] { "--bar", "--disable-component-update" }, new[] { "--component-updater=test-request" });
        Assert.Equal(new[] { "--bar", "--disable-component-update" }, ignore);
        Assert.Equal(new[] { "--component-updater=test-request" }, args);   // the caller's mode is kept
        _sb.Os("linux");
        (_, args) = Widevine.ComponentUpdaterArgs(null, null);
        Assert.Empty(args);   // on Linux the hint file registers the CDM: no scan
    }

    [Fact]
    public async Task Seeding_copies_the_cdm_and_writes_the_hint_file()
    {
        var cdm = CachedCdm();
        var udd = Dir("cc-wv-udd-");
        var target = await Widevine.SeedAsync(udd, quiet: true);
        Assert.Equal(Path.Combine(udd, "WidevineCdm", Version), target);
        Assert.True(File.Exists(Path.Combine(target, "_platform_specific", Sub, CdmFile)));
        using var hint = JsonDocument.Parse(File.ReadAllText(Path.Combine(udd, "WidevineCdm", Widevine.HintFile)));
        Assert.Equal(target, hint.RootElement.GetProperty("Path").GetString());
        Assert.True(Directory.Exists(cdm));
    }

    [Fact]
    public void Widevine_is_a_local_only_option_whose_opt_out_cloud_and_docker_accept()
    {
        // S13: the warning's advice (Widevine = false) used to throw on a cloud or Docker launch
        Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == nameof(LaunchOptions.Widevine));
        CloudLaunch.SessionOptionsOf(new LaunchOptions());                      // unset: nothing to refuse
        CloudLaunch.SessionOptionsOf(new LaunchOptions { Widevine = false });   // the opt-out: accepted, dropped
        Assert.Equal("Widevine is not available for cloud browsers",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { Widevine = true })).Message);
        Assert.Equal(DockerLaunch.ContainerEnv(new LaunchOptions { Fingerprint = "17" }),
            DockerLaunch.ContainerEnv(new LaunchOptions { Fingerprint = "17", Widevine = false }));
        Assert.StartsWith("Widevine is not available when LaunchAsync runs Clearcote in Docker",
            Assert.Throws<ArgumentException>(() => DockerLaunch.ContainerEnv(new LaunchOptions { Widevine = true })).Message);
    }

    // ── the launch entry points ──────────────────────────────────────────────

    /// A stand-in engine: writes its arguments next to itself, then exits. The probe's literal sits after the exit.
    private string Standin(bool with1022)
    {
        var exe = Path.Combine(Dir("cc-wv-standin-"), "chrome");
        const string script = "#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\nexit 1\n";
        var body = new List<byte>(Encoding.ASCII.GetBytes(script));
        if (with1022) body.AddRange(Engine1022);
        body.Add((byte)'\n');
        File.WriteAllBytes(exe, body.ToArray());
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private static List<string> Argv(string exe, Exception launchError)
    {
        var file = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
        Assert.True(File.Exists(file), $"the stand-in engine never ran: {launchError}");
        return File.ReadAllLines(file).ToList();
    }

    private static List<string> CdmArgs(IEnumerable<string> argv) => argv.Where(a => a.StartsWith("--widevine-cdm-path", StringComparison.Ordinal)).ToList();

    private LaunchOptions Opts(string exe, bool licensed, bool? widevine = null, bool quiet = true) => new()
    {
        ExecutablePath = exe, Headless = true, Quiet = quiet, Timeout = 20000, Widevine = widevine,
        LicenseKey = licensed ? Key : null, LicenseApiBase = licensed ? "http://test.local" : null,
    };

    /// TMPDIR of the test's own, which a launch must leave empty (the CI rule for the Python and Node suites).
    private string OwnTemp()
    {
        var temp = Dir("cc-wv-tmp-");
        _sb.Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);
        return temp;
    }

    [Fact]
    public async Task A_licensed_persistent_launch_gets_widevine_by_default_without_seeding()
    {
        if (OperatingSystem.IsWindows()) return;
        var cdm = CachedCdm();
        var g = Google(new FakeGoogle { Unexpected = true });
        var exe = Standin(true);
        var udd = Dir("cc-wv-udd-");
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(udd, Opts(exe, licensed: true)));
        var argv = Argv(exe, e);
        Assert.Equal(new[] { $"--widevine-cdm-path={cdm}" }, CdmArgs(argv));
        Assert.Contains("--disable-component-update", argv);   // Playwright's default kept: the default only adds the switch
        Assert.False(Directory.Exists(Path.Combine(udd, "WidevineCdm")));
        Assert.True(g.Licence > 0);   // it really was a licensed launch
    }

    [Fact]
    public async Task A_licensed_incognito_launch_and_ephemeral_profile_get_it_too()
    {
        if (OperatingSystem.IsWindows()) return;
        var cdm = CachedCdm();
        Google(new FakeGoogle { Unexpected = true });
        var exe = Standin(true);
        var exe2 = Standin(true);
        var temp = OwnTemp();   // after every directory the test itself makes
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(Opts(exe, licensed: true)));
        Assert.Equal(new[] { $"--widevine-cdm-path={cdm}" }, CdmArgs(Argv(exe, e)));
        e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(Opts(exe2, licensed: true)));
        Assert.Equal(new[] { $"--widevine-cdm-path={cdm}" }, CdmArgs(Argv(exe2, e)));
        Assert.Empty(Directory.EnumerateFileSystemEntries(temp));   // the failed launches leave nothing behind
    }

    [Fact]
    public async Task Serve_follows_the_same_rules()
    {
        if (OperatingSystem.IsWindows()) return;
        var cdm = CachedCdm();
        Google(new FakeGoogle { Unexpected = true });
        var exe = Standin(true);
        var exe2 = Standin(true);
        var temp = OwnTemp();   // after every directory the test itself makes
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync(new ServeOptions
        {
            ExecutablePath = exe, Quiet = true, ReadyTimeoutMs = 20000, LicenseKey = Key, LicenseApiBase = "http://test.local",
        }));
        Assert.Equal(new[] { $"--widevine-cdm-path={cdm}" }, CdmArgs(Argv(exe, e)));
        e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync(new ServeOptions
        {
            ExecutablePath = exe2, Quiet = true, ReadyTimeoutMs = 20000, LicenseKey = Key, LicenseApiBase = "http://test.local", Widevine = false,
        }));
        Assert.Empty(CdmArgs(Argv(exe2, e)));
        Assert.Empty(Directory.EnumerateFileSystemEntries(temp));
    }

    [Theory]
    [InlineData("option")]
    [InlineData("env")]
    public async Task A_licensed_launch_opts_out(string how)
    {
        if (OperatingSystem.IsWindows()) return;
        CachedCdm();
        Google(new FakeGoogle { Unexpected = true });
        if (how == "env") _sb.Env("CLEARCOTE_WIDEVINE", "0");
        var exe = Standin(true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() =>
            Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: true, widevine: how == "option" ? false : null)));
        Assert.Empty(CdmArgs(Argv(exe, e)));
    }

    [Fact]
    public async Task A_free_launch_stays_off()
    {
        if (OperatingSystem.IsWindows()) return;
        CachedCdm();
        var exe = Standin(true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: false)));
        Assert.Empty(CdmArgs(Argv(exe, e)));
        var exe2 = Standin(true);
        e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(Opts(exe2, licensed: false)));
        Assert.Empty(CdmArgs(Argv(exe2, e)));
    }

    [Theory]
    [InlineData("option")]
    [InlineData("env")]
    public async Task A_free_launch_opts_in_seeded_and_enabled(string how)
    {
        if (OperatingSystem.IsWindows()) return;
        var cdm = CachedCdm();
        if (how == "env") _sb.Env("CLEARCOTE_WIDEVINE", "1");
        var exe = Standin(true);
        var udd = Dir("cc-wv-udd-");
        var e = await Assert.ThrowsAnyAsync<Exception>(() =>
            Clearcote.LaunchPersistentContextAsync(udd, Opts(exe, licensed: false, widevine: how == "option" ? true : null)));
        var argv = Argv(exe, e);
        Assert.Equal(new[] { $"--widevine-cdm-path={cdm}" }, CdmArgs(argv));
        Assert.DoesNotContain("--disable-component-update", argv);   // the component updater registers the seeded CDM
        Assert.DoesNotContain("--enable-automation", argv);          // and the SDK's strip survives
        using var hint = JsonDocument.Parse(File.ReadAllText(Path.Combine(udd, "WidevineCdm", Widevine.HintFile)));
        Assert.EndsWith(Version, hint.RootElement.GetProperty("Path").GetString());
    }

    [Fact]
    public async Task Offline_a_licensed_launch_still_starts_and_says_so_once()
    {
        if (OperatingSystem.IsWindows()) return;
        var g = Google(new FakeGoogle { Fail = true });
        for (var i = 0; i < 2; i++)
        {
            var exe = Standin(true);
            var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: true, quiet: false)));
            Assert.Empty(CdmArgs(Argv(exe, e)));   // the stand-in ran: the launch went on
        }
        Assert.Single(Warnings());
        Assert.Equal(1, g.Checks);   // the second launch did not try again (10-minute back-off)
    }

    [Fact]
    public async Task Offline_and_quiet_it_says_nothing()
    {
        if (OperatingSystem.IsWindows()) return;
        Google(new FakeGoogle { Fail = true });
        var exe = Standin(true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: true)));
        Assert.Empty(CdmArgs(Argv(exe, e)));
        Assert.Empty(Warnings());
    }

    [Fact]
    public async Task A_licensed_launch_does_not_wait_past_the_timeout()
    {
        if (OperatingSystem.IsWindows()) return;
        _sb.Env("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "0.3");
        var g = Google(new FakeGoogle { Hang = true });
        var exe = Standin(true);
        var sw = Stopwatch.StartNew();
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: true)));
        Assert.Empty(CdmArgs(Argv(exe, e)));
        Assert.True(sw.ElapsedMilliseconds < 15_000, $"took {sw.ElapsedMilliseconds} ms");   // the stand-in's own start included
        for (var i = 0; i < 100 && Volatile.Read(ref g.Cancelled) == 0; i++) await Task.Delay(20);
        Assert.Equal(1, g.Cancelled);   // the update check it stopped waiting for was cancelled
    }

    [Fact]
    public async Task Widevine_true_that_cannot_get_the_cdm_keeps_its_old_behaviour()
    {
        if (OperatingSystem.IsWindows()) return;
        Google(new FakeGoogle { Fail = true });
        var exe = Standin(true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: false, widevine: true, quiet: false)));
        var argv = Argv(exe, e);
        Assert.Empty(CdmArgs(argv));
        Assert.Contains("--disable-component-update", argv);   // seeding failed: no flags that imply DRM works
        Assert.Contains("[widevine] setup failed (continuing without DRM)", _err.ToString());
        Assert.Empty(Warnings());
    }

    [Fact]
    public async Task A_second_launch_does_not_download_again()
    {
        if (OperatingSystem.IsWindows()) return;
        var g = Google(new FakeGoogle());
        var want = new[] { $"--widevine-cdm-path={Path.Combine(_wv, Version)}" };
        for (var i = 0; i < 3; i++)
        {
            var exe = Standin(true);
            var e = await Assert.ThrowsAnyAsync<Exception>(() => i == 2
                ? Clearcote.LaunchAsync(Opts(exe, licensed: true))
                : Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), Opts(exe, licensed: true)));
            Assert.Equal(want, CdmArgs(Argv(exe, e)));
        }
        Assert.Equal((1, 1), (g.Checks, g.Downloads));   // one update check, one download, three launches
    }

    [Fact]
    public async Task The_callers_own_cdm_path_wins()
    {
        if (OperatingSystem.IsWindows()) return;
        CachedCdm();
        Google(new FakeGoogle { Unexpected = true });
        var exe = Standin(true);
        var o = Opts(exe, licensed: true);
        o.Args = new[] { "--widevine-cdm-path=/mine" };
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-wv-udd-"), o));
        Assert.Equal(new[] { "--widevine-cdm-path=/mine" }, CdmArgs(Argv(exe, e)));
    }
}
