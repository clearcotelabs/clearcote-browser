using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Every PRO launch used to ask /api/v1/download/pro before it looked at the installed build. The answer is now
// reused for 10 minutes (per process, and in ~/.clearcote/pro-meta-<16 hex of sha256(key + "\n" + url)>.json
// for other processes), but only to START a build that is installed and passes the cached-build check with it
// (the protected-code hashes included); a download always follows a fresh answer, and an error is never kept.
// Mirrors the Python and Node suites, against the same local fake of the download route the install-lock tests
// use (a small archive, no real browser). HOME is a temp dir.
public class ProMetaCacheTests : IDisposable
{
    private readonly Sandbox _sandbox = new();
    private readonly string _home;
    private readonly List<string> _temp = new();

    public ProMetaCacheTests()
    {
        _home = _sandbox.TempHome();
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
        var dir = TestTemp.Create("cc-meta-");
        _temp.Add(dir);
        return dir;
    }

    private const string Key = "cc_lic_meta_test";

    private static Task<string> Ensure(FakeBuildServer srv, string cache, string? version = null, string key = Key) =>
        Download.ProEnsureBinaryAsync(key, new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true, Version = version });

    private string MetaFile(FakeBuildServer srv, string? version = null, string key = Key)
    {
        var url = Download.ProDownloadUrl(srv.Url, Release.ProPlatformKey()!, version);
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key + "\n" + url))).ToLowerInvariant()[..16];
        return Path.Combine(_home, ".clearcote", $"pro-meta-{id}.json");
    }

    private string[] MetaFiles() =>
        Directory.Exists(Path.Combine(_home, ".clearcote"))
            ? Directory.GetFiles(Path.Combine(_home, ".clearcote"), "pro-meta-*").Select(f => Path.GetFileName(f)).ToArray()
            : Array.Empty<string>();

    [Fact]
    public async Task A_second_launch_within_ten_minutes_makes_no_metadata_request()
    {
        if (OperatingSystem.IsMacOS()) return; // the fake build is a Windows/Linux tree
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        Assert.Equal((1, 1), (srv.MetaHits, srv.ArchiveHits));
        using (var doc = JsonDocument.Parse(File.ReadAllText(MetaFile(srv))))
        {
            Assert.Equal(FakeBuildServer.Tag, doc.RootElement.GetProperty("meta").GetProperty("tag").GetString());
            Assert.InRange(doc.RootElement.GetProperty("at").GetDouble(),
                DateTimeOffset.UtcNow.ToUnixTimeSeconds() - 60, DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 1);
        }
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal(1, srv.MetaHits);
        Download.ForgetProMeta();                       // another process: the file on disk
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal((1, 1), (srv.MetaHits, srv.ArchiveHits));
    }

    [Fact]
    public async Task Another_process_reuses_the_answer_kept_on_disk()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache, key: "cc_lic_b");
        Download.ForgetProMeta();                       // a fresh process: nothing in memory, the file on disk
        Assert.Equal(exe, await Ensure(srv, cache, key: "cc_lic_b"));
        Assert.Equal(1, srv.MetaHits);
    }

    [Fact]
    public async Task The_in_process_memo_follows_the_file_so_another_home_asks_again()
    {
        // Keyed by the file's full path, as download.ts keys it: a home without the file has no answer kept.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        await Ensure(srv, cache);
        Assert.Equal(1, srv.MetaHits);
        using (var other = new Sandbox())
        {
            other.TempHome();
            await Ensure(srv, cache);
            Assert.Equal(2, srv.MetaHits);
        }
        await Ensure(srv, cache);                       // back home: the memo for this file still answers
        Assert.Equal(2, srv.MetaHits);
    }

    [Fact]
    public async Task A_missing_build_asks_for_fresh_metadata_and_downloads_again()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        await Ensure(srv, cache);
        Assert.Equal(1, srv.MetaHits);
        Directory.Delete(Path.Combine(cache, FakeBuildServer.Tag, "browser"), recursive: true);
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal((2, 2), (srv.MetaHits, srv.ArchiveHits));
    }

    [Fact]
    public async Task Another_cache_dir_without_the_build_asks_for_fresh_metadata()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        await Ensure(srv, Cache());
        await Ensure(srv, Cache());
        Assert.Equal((2, 2), (srv.MetaHits, srv.ArchiveHits));
    }

    [Fact]
    public async Task An_expired_entry_asks_again()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        await Ensure(srv, cache);
        await Ensure(srv, cache);
        Assert.Equal(1, srv.MetaHits);
        var file = MetaFile(srv);
        var stale = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(File.ReadAllText(file))!;
        File.WriteAllText(file, JsonSerializer.Serialize(new { at = DateTimeOffset.UtcNow.ToUnixTimeSeconds() - 601, meta = stale["meta"] }));
        Download.ForgetProMeta();
        await Ensure(srv, cache);
        Assert.Equal(2, srv.MetaHits);
        await Ensure(srv, cache);                       // and the fresh answer is reused again
        Assert.Equal((2, 1), (srv.MetaHits, srv.ArchiveHits));
    }

    [Fact]
    public async Task An_entry_from_the_future_is_a_miss()
    {
        // `at` is epoch seconds (a double, as every SDK writes it); a time ahead of this clock proves no age.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        await Ensure(srv, cache);
        var file = MetaFile(srv);
        var entry = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(File.ReadAllText(file))!;
        Assert.Equal(JsonValueKind.Number, entry["at"].ValueKind);
        File.WriteAllText(file, JsonSerializer.Serialize(new { at = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 + 3, meta = entry["meta"] }));
        Download.ForgetProMeta();
        await Ensure(srv, cache);
        Assert.Equal(2, srv.MetaHits);
    }

    [Fact]
    public async Task An_error_answer_is_never_cached()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer { MetaStatus = 503 };
        var cache = Cache();
        await Assert.ThrowsAnyAsync<Exception>(() => Ensure(srv, cache));
        Assert.Empty(MetaFiles());
        srv.MetaStatus = 200;
        var exe = await Ensure(srv, cache);
        Assert.Equal(2, srv.MetaHits);
        Assert.Single(MetaFiles());
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal(2, srv.MetaHits);
    }

    [Fact]
    public async Task A_different_version_pin_or_key_is_a_different_entry()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        Assert.Equal(exe, await Ensure(srv, cache, version: "0.0.0"));   // the same build, another request
        Assert.Equal(2, srv.MetaHits);
        await Ensure(srv, cache, version: "0.0.0");
        await Ensure(srv, cache);
        Assert.Equal(2, srv.MetaHits);
        await Ensure(srv, cache, key: "cc_lic_meta_other");
        Assert.Equal(3, srv.MetaHits);
        Assert.Equal(new[] { MetaFile(srv), MetaFile(srv, "0.0.0"), MetaFile(srv, key: "cc_lic_meta_other") }.Select(Path.GetFileName).OrderBy(n => n),
                     MetaFiles().OrderBy(n => n));
        Assert.Equal(1, srv.ArchiveHits);
    }

    [Fact]
    public async Task Cached_metadata_never_vouches_for_an_edited_browser()
    {
        // The cached-build check still re-hashes the protected executable with the cached answer's hashes on
        // every launch: a same-size edit after install sends the launch back to the server.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        await Ensure(srv, cache);
        Assert.Equal(1, srv.MetaHits);
        File.WriteAllText(exe, new string('y', 64));
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal((2, 2), (srv.MetaHits, srv.ArchiveHits));   // asked again, and installed again
        Assert.Equal(new string('x', 64), File.ReadAllText(exe));
    }

    [Fact]
    public async Task A_corrupt_cache_file_is_a_miss()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        File.WriteAllText(MetaFile(srv), "{\"at\": 1, \"meta\": [not json");
        Download.ForgetProMeta();
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal(2, srv.MetaHits);
        Assert.Equal(exe, await Ensure(srv, cache));     // rewritten whole
        Assert.Equal(2, srv.MetaHits);
    }
}
