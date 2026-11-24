using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using Xunit;

namespace Clearcote.Tests;

// A burst of launches from one machine used to send one request per launch to the site (www), whose host answers a
// burst from one address with a 403 challenge page and keeps challenging that address for minutes. With a PRO
// licence the challenge on GET /api/v1/download/pro failed the launch ("PRO download not authorized (HTTP 403)"),
// so a customer could not start more than ~20 browsers on one server (2026-11-24).
//
// Now: one request per machine at a time (the others wait for the answer it keeps), and a challenge, a 5xx or no
// connection launches the installed build the last answer named. A licence verdict (the route's own JSON 401/403)
// still fails the launch. HOME is a temp dir; the catalog is an HttpMessageHandler and the PRO route the local fake.
// Mirrors sdk/python/tests/test_site_burst.py.
[Collection("Console")]
public class SiteBurstTests : IDisposable
{
    private const string Live = "{\"schema\":1,\"builds\":[{\"major\":999,\"version\":\"999.0.1.2\",\"tier\":\"free\",\"platforms\":{}}]}";

    private sealed class Site : HttpMessageHandler
    {
        private int _hits;
        public int Hits => Volatile.Read(ref _hits);
        public HttpStatusCode Status = HttpStatusCode.OK;
        public int DelayMs;

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Assert.Equal(Release.CatalogUrl, request.RequestUri!.ToString());
            Interlocked.Increment(ref _hits);
            await Task.Delay(DelayMs, ct); // the holder is still asking while the others arrive
            return new HttpResponseMessage(Status)
            {
                Content = new StringContent(Status == HttpStatusCode.OK ? Live : "<html>challenge</html>"),
            };
        }
    }

    private readonly Sandbox _sandbox = new();
    private readonly string _home;
    private readonly List<string> _temp = new();
    private readonly TextWriter _stderr = Console.Error;
    private readonly StringWriter _err = new();

    public SiteBurstTests()
    {
        _home = _sandbox.TempHome();
        _sandbox.Env("CLEARCOTE_RELEASE_CHANNEL", null);
        Download.ForgetCatalog();
        Download.CatalogSpreadSeconds = 0;
        Download.ForgetProMeta();
        Download.ProMetaSpreadSeconds = 0;
        Console.SetError(TextWriter.Synchronized(_err)); // many launches write at once
    }

    public void Dispose()
    {
        Console.SetError(_stderr);
        _sandbox.Dispose();
        Download.ForgetCatalog();
        Download.CatalogSpreadSeconds = null;
        Download.ForgetProMeta();
        Download.ProMetaSpreadSeconds = null;
        Download.FlightWaitSeconds = 45;
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    private string Dir => Path.Combine(_home, ".clearcote");

    /// Run fn n times at once (tasks take the same per-machine lock processes do): results or exceptions.
    private static async Task<object?[]> Together<T>(int n, Func<Task<T>> fn)
    {
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var runs = Enumerable.Range(0, n).Select(_ => Task.Run(async () =>
        {
            await gate.Task;
            try { return (object?)await fn(); }
            catch (Exception e) { return e; }
        })).ToArray();
        gate.SetResult();
        return await Task.WhenAll(runs).WaitAsync(TimeSpan.FromSeconds(90));
    }

    private static void AssertLive(object? cat) =>
        Assert.Equal("999.0.1.2", Assert.Single(Assert.IsType<Catalog>(cat).Builds).Version);

    // ── catalog ──────────────────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task A_burst_with_nothing_kept_asks_the_catalog_once()
    {
        var site = new Site { DelayMs = 300 };
        _sandbox.Http(site);
        foreach (var r in await Together(20, () => Download.FetchCatalogAsync(true))) AssertLive(r);
        Assert.Equal(1, site.Hits);
        Assert.False(File.Exists(Path.Combine(Dir, "catalog.lock")));
    }

    [Fact]
    public async Task A_failed_catalog_request_is_not_repeated_by_the_others()
    {
        var site = new Site { DelayMs = 300, Status = HttpStatusCode.Forbidden };
        _sandbox.Http(site);
        foreach (var r in await Together(20, () => Download.FetchCatalogAsync(true))) Assert.Same(Release.CatalogFallback, r);
        Assert.Equal(1, site.Hits);
    }

    [Fact]
    public async Task A_failure_older_than_the_quiet_time_is_asked_again()
    {
        var site = new Site();
        _sandbox.Http(site);
        Directory.CreateDirectory(Dir);
        var failed = Path.Combine(Dir, "catalog.failed");
        File.WriteAllText(failed, "");
        File.SetLastWriteTimeUtc(failed, DateTime.UtcNow.AddSeconds(-Download.FlightFailedQuietSeconds - 1));
        AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(1, site.Hits);
        Assert.False(File.Exists(failed));
    }

    [Fact]
    public async Task A_lock_left_by_a_dead_process_is_taken_over()
    {
        var site = new Site();
        _sandbox.Http(site);
        Directory.CreateDirectory(Dir);
        var lockFile = Path.Combine(Dir, "catalog.lock");
        File.WriteAllText(lockFile, "12345");
        File.SetLastWriteTimeUtc(lockFile, DateTime.UtcNow.AddSeconds(-Download.FlightStaleSeconds - 1));
        var t0 = DateTime.UtcNow;
        AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(1, site.Hits);
        Assert.True(DateTime.UtcNow - t0 < TimeSpan.FromSeconds(5));
    }

    [Fact]
    public async Task A_stuck_holder_is_waited_for_at_most_the_flight_wait()
    {
        var site = new Site();
        _sandbox.Http(site);
        Download.FlightWaitSeconds = 0.5;
        Directory.CreateDirectory(Dir);
        File.WriteAllText(Path.Combine(Dir, "catalog.lock"), "12345"); // live: just made
        AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(1, site.Hits);
    }

    // ── PRO build lookup ─────────────────────────────────────────────────────────────────────────────────────

    private const string Key = "cc_lic_burst";

    private string Cache()
    {
        var dir = TestTemp.Create("cc-burst-");
        _temp.Add(dir);
        return dir;
    }

    private static Task<string> Ensure(FakeBuildServer srv, string cache) =>
        Download.ProEnsureBinaryAsync(Key, new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true });

    /// Make every kept PRO answer <paramref name="seconds"/> older.
    private void ExpireKept(double seconds)
    {
        foreach (var file in Directory.GetFiles(Dir, "pro-meta-*.json"))
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(file));
            var at = doc.RootElement.GetProperty("at").GetDouble() - seconds;
            File.WriteAllText(file, $"{{\"at\":{at.ToString("R", System.Globalization.CultureInfo.InvariantCulture)},\"meta\":{doc.RootElement.GetProperty("meta").GetRawText()}}}");
        }
    }

    private static void NewProcess() => Download.ForgetProMeta(); // only the file is left

    [Fact]
    public async Task A_burst_of_pro_launches_asks_once()
    {
        if (OperatingSystem.IsMacOS()) return; // the PRO route serves Windows and Linux
        await using var srv = new FakeBuildServer { MetaDelayMs = 300 };
        var cache = Cache();
        var first = await Ensure(srv, cache); // installs the build
        ExpireKept(3600);
        NewProcess();
        var got = await Together(20, () => Ensure(srv, cache));
        Assert.All(got, r => Assert.Equal(first, r));
        Assert.Equal(2, srv.MetaHits); // the install, then one for the whole burst
        Assert.Equal(1, srv.ArchiveHits);
    }

    [Fact]
    public async Task A_challenge_launches_the_installed_build_the_last_answer_named()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var first = await Ensure(srv, cache);
        ExpireKept(3600);
        NewProcess();
        srv.MetaChallenge = true;
        srv.MetaDelayMs = 300;
        var got = await Together(10, () => Ensure(srv, cache));
        Assert.All(got, r => Assert.Equal(first, r));
        Assert.Equal(2, srv.MetaHits); // the others did not ask after the challenge
        var err = _err.ToString();
        Assert.Single(Regex.Matches(err, "PRO build lookup"));
        Assert.Contains("challenge page", err);
        Assert.Contains("launching the installed build the last answer named", err);
    }

    [Fact]
    public async Task A_challenge_with_no_installed_build_fails_with_a_clear_message()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer { MetaChallenge = true };
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Ensure(srv, Cache()));
        Assert.Matches(new Regex("build lookup .* failed .*challenge page.*Try again", RegexOptions.Singleline), e.Message);
        Assert.DoesNotContain("not authorized", e.Message);
    }

    [Fact]
    public async Task An_answer_older_than_a_week_is_not_used()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        await Ensure(srv, cache);
        ExpireKept(Download.ProMetaStaleMax.TotalSeconds + 1);
        NewProcess();
        srv.MetaChallenge = true;
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Ensure(srv, cache));
        Assert.Contains("no earlier answer names an installed build", e.Message);
    }

    [Theory]
    [InlineData(401)]
    [InlineData(403)]
    public async Task A_licence_verdict_still_fails_the_launch(int status)
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        await Ensure(srv, cache);
        ExpireKept(3600);
        NewProcess();
        srv.MetaStatus = status;
        var got = await Together(5, () => Ensure(srv, cache));
        Assert.All(got, r => Assert.Contains("not authorized", Assert.IsAssignableFrom<Exception>(r).Message));
        Assert.Empty(Directory.GetFiles(Dir, "*.failed")); // a verdict is not a site failure
    }

    [Fact]
    public async Task A_server_error_launches_the_installed_build()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var first = await Ensure(srv, cache);
        ExpireKept(3600);
        NewProcess();
        srv.MetaStatus = 503;
        Assert.Equal(first, await Ensure(srv, cache));
        Assert.Contains("HTTP 503", _err.ToString());
    }

    [Fact]
    public async Task Without_a_connection_the_installed_build_is_launched()
    {
        if (OperatingSystem.IsMacOS()) return;
        string first;
        string url;
        var cache = Cache();
        await using (var srv = new FakeBuildServer())
        {
            first = await Ensure(srv, cache);
            url = srv.Url;
        }
        ExpireKept(3600);
        NewProcess();
        // The same request (so the same kept answer), to a port nothing listens on any more.
        Assert.Equal(first, await Download.ProEnsureBinaryAsync(Key, new ProDownloadOptions { ApiBase = url, CacheDir = cache, Quiet = true }));
        Assert.Contains("PRO build lookup", _err.ToString());
    }
}
