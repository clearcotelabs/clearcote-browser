using System.IO.Compression;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Clearcote.Tests;

/// A local stand-in for the PRO download route and the archive it points at, for the install-lock tests.
///
/// The archive is a small zip holding every file a healthy install must have (no real browser). The server
/// counts archive downloads, and can hold the route's answer until <c>metaBarrier</c> clients have asked, so
/// that several installers reach the cache check at the same moment.
///
/// Every answer carries a release manifest for the build it names, signed with the TEST key
/// (<see cref="TestReleaseKey"/>), as the real route signs with the release key.
internal sealed class FakeBuildServer : LocalServer
{
    public static readonly string Binary = OperatingSystem.IsWindows() ? "chrome.exe" : "chrome";
    public const string Tag = "pro-0.0.0-r1";
    public static readonly string[] Names = (OperatingSystem.IsWindows()
            ? new[] { "chrome.exe", "chrome.dll", "chrome_elf.dll", "icudtl.dat", "snapshot_blob.bin", "resources.pak" }
            : new[] { "icudtl.dat", "snapshot_blob.bin", "resources.pak" })
        .Append(Binary).Distinct().OrderBy(n => n, StringComparer.Ordinal).ToArray();

    private readonly byte[] _archive = FakeArchive();
    private readonly int _metaBarrier;
    private readonly int _archiveDelayMs;
    private readonly string? _exeSha;
    private readonly string _dllSha;
    private readonly Action? _onArchive;
    private readonly TaskCompletionSource _allAsked = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private int _asked;
    private int _archiveHits;

    public FakeBuildServer(int metaBarrier = 1, int archiveDelayMs = 0, string? sha = null, string? exeSha = null, Action? onArchive = null)
    {
        _onArchive = onArchive;
        _metaBarrier = metaBarrier;
        _archiveDelayMs = archiveDelayMs;
        _exeSha = exeSha ?? Convert.ToHexString(SHA256.HashData(Encoding.ASCII.GetBytes(new string('x', 64)))).ToLowerInvariant();
        _dllSha = Convert.ToHexString(SHA256.HashData(Encoding.ASCII.GetBytes(new string('x', 64)))).ToLowerInvariant();
        Sha = sha ?? Convert.ToHexString(SHA256.HashData(_archive)).ToLowerInvariant();
    }

    public string Sha { get; }
    public string Url => $"http://127.0.0.1:{Port}";
    /// Archive downloads served so far.
    public int ArchiveHits => Volatile.Read(ref _archiveHits);
    /// Requests to the download route so far.
    public int MetaHits => Volatile.Read(ref _asked);
    /// The download route's status: anything but 200 answers a JSON error instead of the metadata.
    public int MetaStatus { get; set; } = 200;
    /// True: the route answers a 403 challenge page, as the site's host answers a burst from one address.
    public bool MetaChallenge { get; set; }
    /// How long the route takes to answer, so that a burst of launches arrives while the first one asks.
    public int MetaDelayMs { get; set; }
    /// The tag and version the download route names (default <see cref="Tag"/>, 0.0.0).
    public string MetaTag { get; set; } = Tag;
    public string MetaVersion { get; set; } = "0.0.0";
    /// The revision the route names: the "r1" at the end of <see cref="MetaTag"/>.
    public string MetaRevision => System.Text.RegularExpressions.Regex.Match(MetaTag, @"-(r\d+)$") is { Success: true } m ? m.Groups[1].Value : "r0";
    /// True: the answer carries no manifest, as the route answered before releases were signed.
    public bool Unsigned { get; set; }
    /// Set: the manifest is a correctly signed one for this revision, not for the revision the answer names.
    public string? ManifestRevision { get; set; }
    /// True: the archive served has one byte changed (same length); the answer and its manifest name the real one.
    public bool AlterArchive { get; set; }
    /// The archive's status: anything but 200 answers a JSON error (with <see cref="RetryAfter"/>, when set).
    public int ArchiveStatus { get; set; } = 200;
    public string? RetryAfter { get; set; }
    /// The User-Agent of every archive request so far.
    public System.Collections.Concurrent.ConcurrentQueue<string> UserAgents { get; } = new();
    /// The User-Agent of every download-route (metadata) request so far.
    public System.Collections.Concurrent.ConcurrentQueue<string> MetaUserAgents { get; } = new();
    /// Completes on the first request to the download route.
    public TaskCompletionSource MetaSeen { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public static byte[] FakeArchive()
    {
        using var ms = new MemoryStream();
        using (var zip = new ZipArchive(ms, ZipArchiveMode.Create, leaveOpen: true))
            foreach (var name in Names)
            {
                using var s = zip.CreateEntry(name).Open();
                s.Write(Encoding.ASCII.GetBytes(new string('x', 64)));
            }
        return ms.ToArray();
    }

    /// What another installer leaves behind: {base}/browser, its manifest and the .verified marker.
    public static string VerifiedTree(string @base)
    {
        var browser = Path.Combine(@base, "browser");
        Directory.CreateDirectory(browser);
        foreach (var name in Names) File.WriteAllText(Path.Combine(browser, name), new string('x', 64));
        Download.WriteManifest(@base, browser);
        File.WriteAllText(Path.Combine(@base, ".verified"), new string('0', 64) + "\n");
        return Path.Combine(browser, Binary);
    }

    /// The names in a directory, sorted.
    public static string[] Entries(string dir) =>
        Directory.EnumerateFileSystemEntries(dir).Select(e => Path.GetFileName(e)).OrderBy(n => n, StringComparer.Ordinal).ToArray();

    protected override async Task HandleAsync(NetworkStream s, CancellationToken ct)
    {
        var head = await ReadHeadAsync(s, ct);
        if (head is null) return;
        var parts = head.Value.Lines[0].Split(' ');
        var target = parts.Length > 1 ? parts[1] : "";
        byte[] body;
        string type;
        if (target.StartsWith("/api/v1/download/pro", StringComparison.Ordinal))
        {
            MetaUserAgents.Enqueue(head.Value.Lines.Skip(1)
                .Where(l => l.StartsWith("user-agent:", StringComparison.OrdinalIgnoreCase))
                .Select(l => l["user-agent:".Length..].Trim()).FirstOrDefault() ?? "");
            MetaSeen.TrySetResult();
            if (Interlocked.Increment(ref _asked) >= _metaBarrier) _allAsked.TrySetResult();
            await Task.WhenAny(_allAsked.Task, Task.Delay(30_000, ct));
            if (MetaDelayMs > 0) await Task.Delay(MetaDelayMs, ct);
            if (MetaChallenge) // what the site's host answers a burst from one address
            {
                var page = Encoding.UTF8.GetBytes("<!DOCTYPE html><title>Vercel Security Checkpoint</title>");
                await s.WriteAsync(Encoding.ASCII.GetBytes(
                    "HTTP/1.1 403 Forbidden\r\nContent-Type: text/html; charset=utf-8\r\nx-vercel-mitigated: challenge\r\n" +
                    $"Retry-After: 30\r\nContent-Length: {page.Length}\r\nConnection: close\r\n\r\n"), ct);
                await s.WriteAsync(page, ct);
                return;
            }
            if (MetaStatus != 200)
            {
                var err = Encoding.UTF8.GetBytes("{\"error\":\"unavailable\"}");
                await s.WriteAsync(Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 {MetaStatus} X\r\nContent-Type: application/json\r\nContent-Length: {err.Length}\r\nConnection: close\r\n\r\n"), ct);
                await s.WriteAsync(err, ct);
                return;
            }
            var platform = Release.ProPlatformKey() ?? "linux";
            var answer = new Dictionary<string, object>
            {
                ["tag"] = MetaTag, ["version"] = MetaVersion, ["revision"] = MetaRevision, ["platform"] = platform,
                ["url"] = $"{Url}/fake.zip", ["sha256"] = Sha, ["exe_sha256"] = _exeSha!,
                ["chrome_dll_sha256"] = _dllSha, ["asset"] = "fake.zip",
                ["archive"] = "zip", ["binary"] = Binary, ["size"] = _archive.Length,
            };
            if (!Unsigned)
            {
                var binaries = new Dictionary<string, string> { [Binary] = _exeSha! };
                if (platform == "windows") binaries["chrome.dll"] = _dllSha;
                answer["manifest"] = TestReleaseKey.Manifest(MetaVersion, ManifestRevision ?? MetaRevision, platform, "fake.zip",
                                                             Sha, _archive.Length, binaries);
            }
            body = JsonSerializer.SerializeToUtf8Bytes(answer);
            type = "application/json";
        }
        else if (target == "/fake.zip")
        {
            Interlocked.Increment(ref _archiveHits);
            UserAgents.Enqueue(head.Value.Lines.Skip(1)
                .Where(l => l.StartsWith("user-agent:", StringComparison.OrdinalIgnoreCase))
                .Select(l => l["user-agent:".Length..].Trim()).FirstOrDefault() ?? "");
            if (ArchiveStatus != 200)
            {
                var err = Encoding.UTF8.GetBytes("{\"error\":\"Download limit reached\",\"code\":\"DOWNLOAD_RATE_LIMITED\"}");
                await s.WriteAsync(Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 {ArchiveStatus} X\r\nContent-Type: application/json\r\n" +
                    (RetryAfter is null ? "" : $"Retry-After: {RetryAfter}\r\n") +
                    $"Content-Length: {err.Length}\r\nConnection: close\r\n\r\n"), ct);
                await s.WriteAsync(err, ct);
                return;
            }
            _onArchive?.Invoke(); // e.g. another installer finishes while this one downloads
            await Task.Delay(_archiveDelayMs, ct); // long enough for a second installer to arrive meanwhile
            body = _archive;
            if (AlterArchive)
            {
                body = (byte[])_archive.Clone();
                body[^1] ^= 0x01;
            }
            type = "application/zip";
        }
        else
        {
            await s.WriteAsync("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"u8.ToArray(), ct);
            return;
        }
        await s.WriteAsync(Encoding.ASCII.GetBytes(
            $"HTTP/1.1 200 OK\r\nContent-Type: {type}\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n"), ct);
        await s.WriteAsync(body, ct);
        await s.FlushAsync(ct);
    }
}
