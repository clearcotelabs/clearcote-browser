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
    private readonly TaskCompletionSource _allAsked = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private int _asked;
    private int _archiveHits;

    public FakeBuildServer(int metaBarrier = 1, int archiveDelayMs = 0, string? sha = null)
    {
        _metaBarrier = metaBarrier;
        _archiveDelayMs = archiveDelayMs;
        Sha = sha ?? Convert.ToHexString(SHA256.HashData(_archive)).ToLowerInvariant();
    }

    public string Sha { get; }
    public string Url => $"http://127.0.0.1:{Port}";
    /// Archive downloads served so far.
    public int ArchiveHits => Volatile.Read(ref _archiveHits);
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
        foreach (var name in Names) File.WriteAllText(Path.Combine(browser, name), new string('y', 64));
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
            MetaSeen.TrySetResult();
            if (Interlocked.Increment(ref _asked) >= _metaBarrier) _allAsked.TrySetResult();
            await Task.WhenAny(_allAsked.Task, Task.Delay(30_000, ct));
            body = JsonSerializer.SerializeToUtf8Bytes(new
            {
                tag = Tag, version = "0.0.0", url = $"{Url}/fake.zip", sha256 = Sha, asset = "fake.zip",
                archive = "zip", binary = Binary, size = _archive.Length,
            });
            type = "application/json";
        }
        else if (target == "/fake.zip")
        {
            Interlocked.Increment(ref _archiveHits);
            await Task.Delay(_archiveDelayMs, ct); // long enough for a second installer to arrive meanwhile
            body = _archive;
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
