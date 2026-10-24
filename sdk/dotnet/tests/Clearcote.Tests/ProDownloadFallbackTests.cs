using System.Text.Json;
using System.Text.RegularExpressions;
using Xunit;

namespace Clearcote.Tests;

// A new PRO release must not break launches that cannot download it (2026-10-24: one licence on SDK 0.38.1
// relaunched 4,048 times into the per-licence download limit after r50 shipped, with r48 on disk). A failed
// download of a newer build launches the newest installed build of the same major, a 429 is remembered until its
// Retry-After, and an exact revision pin never gets another build. Mirrors pro-download-fallback.test.ts, against
// the same local fake of the download route the metadata-cache tests use. HOME and the browser cache are temp dirs.
public class ProDownloadFallbackTests : IDisposable
{
    private readonly Sandbox _sandbox = new();
    private readonly List<string> _temp = new();

    private const string Old = "pro-155.0.8059.39-r48";
    private const string New = "pro-155.0.8059.39-r50";
    private const string OtherMajor = "pro-154.0.8037.57-r41";

    public ProDownloadFallbackTests()
    {
        _sandbox.TempHome();
        _sandbox.Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_RELEASE_CHANNEL", null);
        Download.ForgetProMeta();
    }

    public void Dispose()
    {
        _sandbox.Dispose();
        Download.ForgetProMeta();
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    private string Cache()
    {
        var dir = TestTemp.Create("cc-dlfb-cache-");
        _temp.Add(dir);
        return dir;
    }

    /// The route answers <see cref="New"/>; the archive answers <paramref name="status"/> (with Retry-After, when set).
    private static FakeBuildServer Route(int status = 200, string? retryAfter = null) =>
        new() { MetaTag = New, MetaVersion = "155.0.8059.39", ArchiveStatus = status, RetryAfter = retryAfter };

    private static Task<string> Ensure(FakeBuildServer srv, string cache, string key, string? version = null) =>
        Download.ProEnsureBinaryAsync(key, new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true, Version = version });

    private static string BackoffFile(string cache, string tag) => Path.Combine(cache, $".download-backoff-{tag}.json");

    private static long Until(string cache) =>
        JsonDocument.Parse(File.ReadAllText(BackoffFile(cache, New))).RootElement.GetProperty("until").GetInt64();

    private static long Left(string cache) => Until(cache) - DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    private static async Task<Exception> Fails(Task t, string pattern)
    {
        var e = await Assert.ThrowsAnyAsync<Exception>(() => t);
        Assert.Matches(new Regex(pattern, RegexOptions.Singleline), e.Message);
        return e;
    }

    [Fact]
    public void Retry_After_reads_delta_seconds_and_HTTP_dates_and_nothing_else()
    {
        var now = DateTimeOffset.Parse("2026-10-24T10:00:00Z");
        Assert.Equal(86400, Download.RetryAfterSeconds("86400", now));
        Assert.Equal(90, Download.RetryAfterSeconds("Sat, 24 Oct 2026 10:01:30 GMT", now));
        Assert.Equal(0, Download.RetryAfterSeconds("Sat, 24 Oct 2026 09:00:00 GMT", now));
        Assert.Null(Download.RetryAfterSeconds("", now));
        Assert.Null(Download.RetryAfterSeconds("soon", now));
        Assert.Null(Download.RetryAfterSeconds(null, now));
    }

    [Fact]
    public async Task A_429_on_a_new_release_launches_the_installed_older_build_and_pauses_until_Retry_After()
    {
        if (OperatingSystem.IsMacOS()) return; // the fake build is a Windows/Linux tree
        await using var srv = Route(429, "3600");
        var cache = Cache();
        var old = FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        using var err = new StderrCapture();

        Assert.Equal(old, await Ensure(srv, cache, "cc_lic_fb1"));
        Assert.Equal(1, srv.ArchiveHits);
        using (var bo = JsonDocument.Parse(File.ReadAllText(BackoffFile(cache, New))))
            Assert.Equal(429, bo.RootElement.GetProperty("status").GetInt32());
        Assert.InRange(Left(cache), 3_500_000, 3_600_000);
        Assert.Contains($"launching the installed {Old} instead", err.Text);

        // The next launches skip the download altogether while the pause lasts.
        Assert.Equal(old, await Ensure(srv, cache, "cc_lic_fb1"));
        Assert.Equal(old, await Ensure(srv, cache, "cc_lic_fb1"));
        Assert.Equal(1, srv.ArchiveHits);
    }

    [Fact]
    public async Task Once_the_pause_is_over_the_new_build_is_downloaded_launched_and_the_pause_file_removed()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(429, "3600");
        var cache = Cache();
        FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        using var err = new StderrCapture();
        await Ensure(srv, cache, "cc_lic_fb2");

        var f = BackoffFile(cache, New);
        var entry = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(File.ReadAllText(f))!;
        File.WriteAllText(f, JsonSerializer.Serialize(new
        {
            until = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - 1, status = entry["status"], reason = entry["reason"],
        })); // expired
        srv.ArchiveStatus = 200;
        Assert.Equal(Path.Combine(cache, New, "browser", FakeBuildServer.Binary), await Ensure(srv, cache, "cc_lic_fb2"));
        Assert.Equal(2, srv.ArchiveHits);
        Assert.False(File.Exists(f));
    }

    [Fact]
    public async Task A_429_with_no_older_build_fails_then_fails_fast_without_asking_again_while_paused()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(429, "600");
        var cache = Cache();
        await Fails(Ensure(srv, cache, "cc_lic_fb3"), "HTTP 429.*CLEARCOTE_CACHE");
        await Fails(Ensure(srv, cache, "cc_lic_fb3"), "download is paused until");
        Assert.Equal(1, srv.ArchiveHits);
    }

    [Fact]
    public async Task A_429_without_Retry_After_pauses_for_60_seconds()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(429);
        var cache = Cache();
        FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        using var err = new StderrCapture();
        await Ensure(srv, cache, "cc_lic_fb4");
        Assert.InRange(Left(cache), 50_000, 60_000);
    }

    [Fact]
    public async Task Any_other_failed_download_also_falls_back_with_a_5_minute_pause_and_without_a_fall_back_nothing_is_paused()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(503);
        var cache = Cache();
        await Fails(Ensure(srv, cache, "cc_lic_fb5"), "HTTP 503");
        Assert.False(File.Exists(BackoffFile(cache, New))); // nothing to stand in: every launch keeps trying

        var old = FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        using var err = new StderrCapture();
        Assert.Equal(old, await Ensure(srv, cache, "cc_lic_fb5"));
        Assert.InRange(Left(cache), 290_000, 300_000);
    }

    [Fact]
    public async Task An_exact_revision_pin_never_gets_another_build()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(429, "3600");
        var cache = Cache();
        FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        await Fails(Ensure(srv, cache, "cc_lic_fb6", version: "r50"), "HTTP 429");
    }

    [Fact]
    public async Task Never_stands_in_a_build_of_another_Chromium_major_and_takes_the_newest_revision_of_the_same_one()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(429, "3600");
        var cache = Cache();
        FakeBuildServer.VerifiedTree(Path.Combine(cache, OtherMajor));
        await Fails(Ensure(srv, cache, "cc_lic_fb7"), "HTTP 429");

        FakeBuildServer.VerifiedTree(Path.Combine(cache, "pro-155.0.8059.39-r42"));
        var r48 = FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        using var err = new StderrCapture();
        Assert.Equal(r48, await Ensure(srv, cache, "cc_lic_fb7"));
    }

    [Fact]
    public async Task A_damaged_older_build_is_not_a_fall_back()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route(429, "3600");
        var cache = Cache();
        var old = FakeBuildServer.VerifiedTree(Path.Combine(cache, Old));
        File.WriteAllText(old, "yyy"); // truncated: fails the manifest's size check
        await Fails(Ensure(srv, cache, "cc_lic_fb8"), "HTTP 429");
        Assert.Contains("browser", FakeBuildServer.Entries(Path.Combine(cache, Old))); // left alone, never deleted
    }

    [Fact]
    public async Task The_archive_download_says_which_SDK_fetched_it()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = Route();
        await Ensure(srv, Cache(), "cc_lic_fb9");
        Assert.True(srv.UserAgents.TryPeek(out var ua));
        Assert.Matches(@"^clearcote-sdk/\d+\.\d+\.\d+ \(dotnet\)$", ua);
        Assert.Equal(Download.DownloadUserAgent, ua);
    }
}
