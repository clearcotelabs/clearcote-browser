using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

// The signed release manifest on the PRO download path (docs/PRO-RELEASE-MANIFEST.md), through the local fake of
// the download route (FakeBuild.cs), which signs its answers with the TEST key the test project pins
// (TestReleaseKey). A fresh answer is checked before any archive byte is asked for; a kept answer
// (~/.clearcote/pro-meta-*.json, a file the user can write) is checked every time before it starts an installed
// build; and the archive and launcher hash checks that were there before still run. HOME and the browser cache
// are temp dirs.
public class ProManifestDownloadTests : IDisposable
{
    private readonly Sandbox _sandbox = new();
    private readonly string _home;
    private readonly List<string> _temp = new();

    private const string Key = "cc_lic_manifest_test";
    private static readonly string Good = new('x', 64);     // what the fake archive holds for every file
    private static readonly string Swapped = new('y', 64);  // the same size, other bytes

    public ProManifestDownloadTests()
    {
        _home = _sandbox.TempHome();
        _sandbox.Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_RELEASE_CHANNEL", null);
        Download.ForgetProMeta();
        Download.ProMetaSpreadSeconds = 0;
        ProManifest.KeysForTests = TestReleaseKey.Pins;
    }

    public void Dispose()
    {
        ProManifest.KeysForTests = TestReleaseKey.Pins;
        _sandbox.Dispose();
        Download.ForgetProMeta();
        Download.ProMetaSpreadSeconds = null;
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    private string Cache()
    {
        var dir = TestTemp.Create("cc-manifest-");
        _temp.Add(dir);
        return dir;
    }

    private static Task<string> Ensure(FakeBuildServer srv, string cache, string? version = null) =>
        Download.ProEnsureBinaryAsync(Key, new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true, Version = version });

    private string MetaFile(FakeBuildServer srv, string? version = null)
    {
        var url = Download.ProDownloadUrl(srv.Url, Release.ProPlatformKey()!, version);
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Key + "\n" + url))).ToLowerInvariant()[..16];
        return Path.Combine(_home, ".clearcote", $"pro-meta-{id}.json");
    }

    private string[] MetaFiles() =>
        Directory.Exists(Path.Combine(_home, ".clearcote"))
            ? Directory.GetFiles(Path.Combine(_home, ".clearcote"), "pro-meta-*.json")
            : Array.Empty<string>();

    private static string Sha256Hex(string text) => Convert.ToHexString(SHA256.HashData(Encoding.ASCII.GetBytes(text))).ToLowerInvariant();

    private static void NewProcess() => Download.ForgetProMeta(); // only the file is left

    /// The route answers again, and the note a failed lookup leaves for the other launches on this machine is gone.
    private void SiteIsBack(FakeBuildServer srv)
    {
        srv.MetaStatus = 200;
        foreach (var f in Directory.GetFiles(Path.Combine(_home, ".clearcote"), "*.failed")) File.Delete(f);
    }

    // ── a fresh answer ───────────────────────────────────────────────────────

    [Fact]
    public async Task A_signed_answer_installs_and_is_kept_with_its_manifest()
    {
        if (OperatingSystem.IsMacOS()) return; // the fake build is a Windows/Linux tree
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        Assert.Equal(Good, File.ReadAllText(exe));
        Assert.Equal((1, 1), (srv.MetaHits, srv.ArchiveHits));
        using var kept = JsonDocument.Parse(File.ReadAllText(MetaFile(srv)));
        var meta = kept.RootElement.GetProperty("meta");
        Assert.Equal("r1", ProManifest.Verify(meta, Release.ProPlatformKey()!).GetProperty("revision").GetString());
    }

    [Fact]
    public async Task An_unsigned_answer_is_refused_and_the_archive_is_never_asked_for()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer { Unsigned = true };
        var cache = Cache();
        var e = await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache));
        Assert.Equal("missing", e.Reason);
        Assert.StartsWith("Clearcote Pro release manifest check failed (missing): ", e.Message);
        Assert.Equal((1, 0), (srv.MetaHits, srv.ArchiveHits));
        Assert.Empty(MetaFiles());                                                   // a refused answer is not kept
        Assert.False(Directory.Exists(Path.Combine(cache, FakeBuildServer.Tag)));   // and nothing was installed
    }

    [Fact]
    public async Task A_refused_answer_never_falls_back_to_an_older_installed_build()
    {
        // A failed DOWNLOAD of a new build launches the newest installed build of the same major; a manifest
        // that does not pass is not a failed download.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer { MetaTag = "pro-155.0.8059.39-r50", MetaVersion = "155.0.8059.39", Unsigned = true };
        var cache = Cache();
        FakeBuildServer.VerifiedTree(Path.Combine(cache, "pro-155.0.8059.39-r48"));
        var e = await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache));
        Assert.Equal("missing", e.Reason);
        Assert.Equal(0, srv.ArchiveHits);
    }

    [Fact]
    public async Task A_correctly_signed_manifest_for_another_revision_is_refused()
    {
        if (OperatingSystem.IsMacOS()) return;
        // The answer names r1; the manifest is a real, signed one, of r2.
        await using var srv = new FakeBuildServer { ManifestRevision = "r2" };
        var cache = Cache();
        var e = await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache));
        Assert.Equal("revision", e.Reason);
        Assert.Equal(0, srv.ArchiveHits);
        Assert.Empty(MetaFiles());
    }

    [Fact]
    public async Task A_revision_or_version_the_caller_pinned_must_be_the_manifests()
    {
        if (OperatingSystem.IsMacOS()) return;
        // The route answers r1 of 155.0.8059.39 whatever is asked, with a manifest that matches its answer.
        await using var srv = new FakeBuildServer { MetaTag = "pro-155.0.8059.39-r1", MetaVersion = "155.0.8059.39" };
        var cache = Cache();
        Assert.Equal("revision", (await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache, "r2"))).Reason);
        Assert.Equal("revision", (await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache, "155.0.8059.39-r2"))).Reason);
        Assert.Equal("version", (await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache, "154.0.8037.57"))).Reason);
        Assert.Equal("version", (await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache, "154.0.8037.57-r1"))).Reason);
        Assert.Equal(0, srv.ArchiveHits);
        Assert.Empty(MetaFiles());

        var exe = await Ensure(srv, cache, "155.0.8059.39-r1");                       // the pin the answer does match
        Assert.Equal(exe, await Ensure(srv, cache, "r1"));
        Assert.Equal(exe, await Ensure(srv, cache, "155.0.8059.39"));
        Assert.Equal(1, srv.ArchiveHits);
    }

    [Fact]
    public async Task With_no_release_key_pinned_in_the_SDK_a_pro_download_is_refused()
    {
        // Today's state: Release.ProReleaseKeys is empty, so no manifest can pass, whoever signed it.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        ProManifest.KeysForTests = null; // the keys the SDK ships with (Dispose pins the test key again)
        Assert.DoesNotContain(TestReleaseKey.KeyId, Release.ProReleaseKeys.Keys);
        var e = await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache));
        Assert.Equal("unknown-key", e.Reason);
        Assert.Equal((1, 0), (srv.MetaHits, srv.ArchiveHits));
        Assert.Empty(MetaFiles());
    }

    // ── the checks that were there before ────────────────────────────────────

    [Fact]
    public async Task An_archive_whose_bytes_were_altered_is_refused()
    {
        if (OperatingSystem.IsMacOS()) return;
        // The answer and its manifest are right; the bytes served are not the archive they name.
        await using var srv = new FakeBuildServer { AlterArchive = true };
        var cache = Cache();
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Ensure(srv, cache));
        Assert.IsNotType<ProManifestException>(e);
        Assert.Contains("archive SHA-256 mismatch", e.Message);
        Assert.Equal(1, srv.ArchiveHits);
        Assert.Empty(FakeBuildServer.Entries(Path.Combine(cache, FakeBuildServer.Tag))); // nothing left, nothing installed
    }

    [Fact]
    public async Task An_installed_build_whose_launcher_was_altered_is_not_launched()
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        File.WriteAllText(exe, Swapped);
        NewProcess();

        // The launch-time check itself, with the hash the kept (signed) answer lists.
        string signedHash;
        using (var kept = JsonDocument.Parse(File.ReadAllText(MetaFile(srv))))
            signedHash = kept.RootElement.GetProperty("meta").GetProperty("exe_sha256").GetString()!;
        var @base = Path.Combine(cache, FakeBuildServer.Tag);
        Assert.Equal(new[] { $"{FakeBuildServer.Binary} — SHA-256 mismatch" },
            Download.VerifyInstall(Path.Combine(@base, "browser"), @base, full: false, binary: FakeBuildServer.Binary, binarySha256: signedHash));

        // The site cannot give a fresh answer: the kept one is good, the build it names is not, so no launch.
        srv.MetaStatus = 503;
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Ensure(srv, cache));
        Assert.IsNotType<ProManifestException>(e);
        Assert.Contains("no earlier answer names an installed build", e.Message);
        Assert.Equal(Swapped, File.ReadAllText(exe));

        // The site answers again: the build is installed again from the archive the manifest names.
        SiteIsBack(srv);
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal(Good, File.ReadAllText(exe));
        Assert.Equal(2, srv.ArchiveHits);
    }

    // ── a kept answer ────────────────────────────────────────────────────────

    /// What someone who can write this user's files does: swap the launcher, then make the kept answer vouch for it.
    private string SwapLauncherAndEditKeptAnswer(FakeBuildServer srv, string exe, Action<JsonObject> alsoEdit)
    {
        File.WriteAllText(exe, Swapped);
        var file = MetaFile(srv);
        var kept = JsonNode.Parse(File.ReadAllText(file))!;
        var meta = kept["meta"]!.AsObject();
        meta["exe_sha256"] = Sha256Hex(Swapped);
        alsoEdit(meta);
        File.WriteAllText(file, kept.ToJsonString());
        NewProcess();
        return file;
    }

    [Theory]
    [InlineData(false, "binary")]   // the manifest left as it was: it no longer matches the edited hashes
    [InlineData(true, "missing")]   // the manifest taken out
    public async Task A_kept_answer_whose_hashes_were_edited_is_not_used(bool manifestRemoved, string reason)
    {
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        SwapLauncherAndEditKeptAnswer(srv, exe, meta => { if (manifestRemoved) meta.Remove("manifest"); });

        // The site cannot be reached: the launch fails with the manifest error, it does not start the swapped launcher.
        srv.MetaStatus = 503;
        var e = await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache));
        Assert.Equal(reason, e.Reason);
        Assert.StartsWith($"Clearcote Pro release manifest check failed ({reason}): ", e.Message);
        Assert.Contains("the last answer kept on this machine", e.Message);
        Assert.Equal(Swapped, File.ReadAllText(exe));
        Assert.Equal(1, srv.ArchiveHits);

        // The site answers: the kept answer counts as none, so the site is asked, and the signed answer's hashes
        // send the swapped launcher back to a fresh install.
        SiteIsBack(srv);
        var asked = srv.MetaHits;
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal(asked + 1, srv.MetaHits);
        Assert.Equal(2, srv.ArchiveHits);
        Assert.Equal(Good, File.ReadAllText(exe));
    }

    [Fact]
    public async Task Control_the_same_edit_with_a_manifest_signed_for_it_is_used()
    {
        // Proves the test above is refused for its manifest and nothing else: the same swap and the same edit, but
        // with a manifest the (test) release key signed for the swapped launcher, starts it without asking the site.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);
        SwapLauncherAndEditKeptAnswer(srv, exe, meta =>
        {
            var platform = Release.ProPlatformKey()!;
            var binaries = new Dictionary<string, string> { [FakeBuildServer.Binary] = Sha256Hex(Swapped) };
            if (platform == "windows") binaries["chrome.dll"] = meta["chrome_dll_sha256"]!.GetValue<string>();
            meta["manifest"] = TestReleaseKey.Manifest(srv.MetaVersion, srv.MetaRevision, platform, "fake.zip", srv.Sha,
                                                       meta["size"]!.GetValue<long>(), binaries);
        });
        Assert.Equal(exe, await Ensure(srv, cache));
        Assert.Equal((1, 1), (srv.MetaHits, srv.ArchiveHits));
        Assert.Equal(Swapped, File.ReadAllText(exe));
    }

    [Fact]
    public async Task A_kept_answer_is_checked_against_the_callers_pin_too()
    {
        // The kept answer for the request "r2" is replaced by a good, signed answer for r1 (copied from another
        // request's file): it is for another revision than the one pinned, so it is not used.
        if (OperatingSystem.IsMacOS()) return;
        await using var srv = new FakeBuildServer();
        var cache = Cache();
        var exe = await Ensure(srv, cache);                       // r1, installed
        Directory.CreateDirectory(Path.GetDirectoryName(MetaFile(srv, "r2"))!);
        File.Copy(MetaFile(srv), MetaFile(srv, "r2"));
        NewProcess();
        srv.MetaStatus = 503;
        var e = await Assert.ThrowsAsync<ProManifestException>(() => Ensure(srv, cache, "r2"));
        Assert.Equal("revision", e.Reason);
        Assert.Equal(exe, await Ensure(srv, cache));              // the request it was kept for still starts it
    }
}
