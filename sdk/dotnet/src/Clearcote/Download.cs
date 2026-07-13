using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Clearcote;

/// Options for resolving the free binary.
public class DownloadOptions
{
    /// Override the cache directory (default: per-OS user cache dir).
    public string? CacheDir { get; set; }
    /// Suppress progress logging.
    public bool Quiet { get; set; }
    /// Opt in to downloading the LATEST GitHub release instead of the pinned one (also CLEARCOTE_AUTO_UPDATE=1).
    public bool? AutoUpdate { get; set; }
}

/// Options for <see cref="Download.ProEnsureBinaryAsync"/>.
public class ProDownloadOptions
{
    /// License API base (default: CLEARCOTE_LICENSE_API env or clearcotelabs.com).
    public string? ApiBase { get; set; }
    /// Override the cache directory (default: per-OS user cache dir).
    public string? CacheDir { get; set; }
    /// Suppress progress logging.
    public bool Quiet { get; set; }
    /// Request a specific PRO major/version; the server returns the newest match.
    public string? Version { get; set; }
}

/// A resolved version plan: a free release to download, or a pro version to fetch via the licensed route.
public sealed record VersionPlan(string Kind, ReleaseInfo? Rel, string? Version);

/// Resolve the Clearcote browser binary: download, verify (SHA-256), extract to a per-version cache,
/// return the chrome path. Ports download.ts.
public static class Download
{
    private static void Log(bool quiet, string msg) { if (!quiet) Console.Error.WriteLine($"[clearcote] {msg}"); }

    private static bool AutoUpdateRequested(bool? opt)
    {
        if (opt.HasValue) return opt.Value;
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_AUTO_UPDATE");
        return env is "1" or "true";
    }

    private static string? FindFile(string dir, string name)
    {
        var stack = new Stack<string>();
        stack.Push(dir);
        while (stack.Count > 0)
        {
            var cur = stack.Pop();
            string[] entries;
            try { entries = Directory.GetFileSystemEntries(cur); } catch { continue; }
            foreach (var e in entries)
            {
                if (Directory.Exists(e)) stack.Push(e);
                else if (string.Equals(Path.GetFileName(e), name, StringComparison.OrdinalIgnoreCase)) return e;
            }
        }
        return null;
    }

    private static async Task<string> Sha256FileAsync(string file)
    {
        await using var fs = File.OpenRead(file);
        var hash = await SHA256.HashDataAsync(fs).ConfigureAwait(false);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static async Task DownloadToAsync(string url, string dest, long expectedSize, bool quiet)
    {
        using var client = SdkHttp.Create();
        using var res = await client.GetAsync(url, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode)
            throw new Exception($"Clearcote download failed: HTTP {(int)res.StatusCode} {res.ReasonPhrase} for {url}");
        var total = res.Content.Headers.ContentLength ?? expectedSize;
        await using var input = await res.Content.ReadAsStreamAsync().ConfigureAwait(false);
        await using var output = File.Create(dest);
        var buf = new byte[1 << 20];
        long seen = 0;
        var lastPct = -1;
        int n;
        while ((n = await input.ReadAsync(buf).ConfigureAwait(false)) > 0)
        {
            await output.WriteAsync(buf.AsMemory(0, n)).ConfigureAwait(false);
            seen += n;
            if (!quiet && total > 0)
            {
                var pct = (int)(seen * 100 / total);
                if (pct != lastPct && pct % 5 == 0)
                {
                    lastPct = pct;
                    Console.Error.Write($"\r[clearcote] downloading {pct}% ({seen / 1_000_000}/{total / 1_000_000} MB)");
                }
            }
        }
        if (!quiet) Console.Error.Write("\n");
    }

    private static async Task<string> FetchTextAsync(string url)
    {
        using var client = SdkHttp.Create();
        client.DefaultRequestHeaders.UserAgent.ParseAdd("clearcote-sdk");
        client.Timeout = TimeSpan.FromSeconds(30);
        return await client.GetStringAsync(url).ConfigureAwait(false);
    }

    private static (string? Zip, string? Exe) ParseSums(string text, string assetName, string binary)
    {
        string? zip = null, exe = null;
        foreach (var raw in text.Split('\n'))
        {
            var m = Regex.Match(raw.Trim(), @"^([0-9a-fA-F]{64})\s+[*]?(.+)$");
            if (!m.Success) continue;
            var basename = Regex.Split(m.Groups[2].Value, @"[\\/]").Last();
            if (basename == assetName) zip = m.Groups[1].Value.ToLowerInvariant();
            else if (basename == binary) exe = m.Groups[1].Value.ToLowerInvariant();
        }
        return (zip, exe);
    }

    private static async Task<ReleaseInfo?> ResolveLatestAsync(bool quiet)
    {
        var pin = Release.PlatformRelease();
        if (pin is null) return null;
        var assetRe = new Regex($@"^clearcote-.*-{Regex.Escape(pin.AssetGlob)}\.(?:zip|tar\.xz)$");
        var verRe = new Regex($@"^clearcote-(.+)-{Regex.Escape(pin.AssetGlob)}\.(?:zip|tar\.xz)$");
        JsonElement list;
        try
        {
            using var client = SdkHttp.Create();
            client.DefaultRequestHeaders.UserAgent.ParseAdd("clearcote-sdk");
            client.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
            client.Timeout = TimeSpan.FromSeconds(30);
            var json = await client.GetStringAsync(
                $"https://api.github.com/repos/{Release.Repo}/releases?per_page=30").ConfigureAwait(false);
            list = JsonDocument.Parse(json).RootElement.Clone();
        }
        catch (Exception e)
        {
            Log(quiet, $"auto-update: couldn't reach GitHub ({e.Message}); using pinned {Release.Current.Tag}");
            return null;
        }
        var releases = list.EnumerateArray()
            .Where(r => r.ValueKind == JsonValueKind.Object && !(r.TryGetProperty("draft", out var d) && d.GetBoolean()))
            .OrderByDescending(r => r.TryGetProperty("published_at", out var p) ? p.GetString() ?? "" : "");
        foreach (var r in releases)
        {
            if (!r.TryGetProperty("assets", out var assets) || assets.ValueKind != JsonValueKind.Array) continue;
            JsonElement? asset = null, sums = null, asc = null, key = null;
            foreach (var a in assets.EnumerateArray())
            {
                var name = a.GetProperty("name").GetString() ?? "";
                if (asset is null && assetRe.IsMatch(name)) asset = a;
                if (name == "SHA256SUMS.txt") sums = a;
                if (name == "SHA256SUMS.txt.asc") asc = a;
                if (name == "clearcote-signing-key.asc") key = a;
            }
            if (asset is null || sums is null) continue;
            (string? Zip, string? Exe) parsed;
            try { parsed = ParseSums(await FetchTextAsync(sums.Value.GetProperty("browser_download_url").GetString()!),
                asset.Value.GetProperty("name").GetString()!, pin.Binary); }
            catch { continue; }
            if (parsed.Zip is null) continue;
            var assetName = asset.Value.GetProperty("name").GetString()!;
            var vm = verRe.Match(assetName);
            return pin with
            {
                Tag = r.GetProperty("tag_name").GetString()!,
                Version = vm.Success ? vm.Groups[1].Value : r.GetProperty("tag_name").GetString()!,
                Asset = assetName,
                Url = asset.Value.GetProperty("browser_download_url").GetString()!,
                Sha256 = parsed.Zip,
                ExeSha256 = parsed.Exe ?? "",
                Size = asset.Value.TryGetProperty("size", out var sz) ? sz.GetInt64() : 0,
                Unpinned = true,
                AscUrl = asc?.GetProperty("browser_download_url").GetString(),
                KeyUrl = key?.GetProperty("browser_download_url").GetString(),
            };
        }
        return null;
    }

    private static bool HasGpg()
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("gpg", "--version")
            { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false });
            p!.WaitForExit();
            return p.ExitCode == 0;
        }
        catch { return false; }
    }

    private static int Gpg(string home, string[] args, out string stdout)
    {
        var psi = new ProcessStartInfo("gpg") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        psi.ArgumentList.Add("--homedir"); psi.ArgumentList.Add(home); psi.ArgumentList.Add("--batch");
        foreach (var a in args) psi.ArgumentList.Add(a);
        using var p = Process.Start(psi)!;
        stdout = p.StandardOutput.ReadToEnd();
        p.WaitForExit();
        return p.ExitCode;
    }

    private static async Task<string> GpgVerifyAsync(ReleaseInfo rel, string sumsBody, string tmp, bool quiet)
    {
        if (rel.AscUrl is null || rel.KeyUrl is null) return "skipped";
        if (!HasGpg()) { Log(quiet, "auto-update: gpg not found — skipping signature check (zip is still SHA-256-verified)"); return "skipped"; }
        var home = Directory.CreateTempSubdirectory("ccgpg-").FullName;
        try
        {
            var keyPath = Path.Combine(home, "key.asc");
            var sumsPath = Path.Combine(home, "SHA256SUMS.txt");
            var ascPath = Path.Combine(home, "SHA256SUMS.txt.asc");
            await File.WriteAllTextAsync(sumsPath, sumsBody).ConfigureAwait(false);
            await File.WriteAllTextAsync(keyPath, await FetchTextAsync(rel.KeyUrl).ConfigureAwait(false)).ConfigureAwait(false);
            await File.WriteAllTextAsync(ascPath, await FetchTextAsync(rel.AscUrl).ConfigureAwait(false)).ConfigureAwait(false);
            if (Gpg(home, new[] { "--import", keyPath }, out _) != 0) return "failed";
            Gpg(home, new[] { "--with-colons", "--fingerprint" }, out var shown);
            var fprs = shown.Split('\n').Where(l => l.StartsWith("fpr:")).Select(l => l.Split(':')[9]);
            if (!fprs.Contains(Release.SigningKeyFpr)) { Log(quiet, $"auto-update: signing key fingerprint mismatch (expected {Release.SigningKeyFpr})"); return "failed"; }
            return Gpg(home, new[] { "--verify", ascPath, sumsPath }, out _) == 0 ? "ok" : "failed";
        }
        catch { return "failed"; }
        finally { try { Directory.Delete(home, true); } catch { } }
    }

    private static async Task<string> FetchAndVerifyAsync(ReleaseInfo rel, string @base, bool quiet)
    {
        var browserDir = Path.Combine(@base, "browser");
        Directory.CreateDirectory(@base);
        var zipPath = Path.Combine(@base, rel.Asset);

        Log(quiet, $"fetching Clearcote {rel.Version} ({rel.Tag}{(rel.Unpinned ? ", latest" : "")}, ~{rel.Size / 1_000_000} MB)");
        await DownloadToAsync(rel.Url, zipPath, rel.Size, quiet).ConfigureAwait(false);

        Log(quiet, "verifying SHA-256");
        var got = await Sha256FileAsync(zipPath).ConfigureAwait(false);
        if (!string.Equals(got, rel.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            TryDelete(zipPath);
            throw new Exception($"Clearcote archive SHA-256 mismatch — refusing to use it.\n  expected {rel.Sha256}\n  got      {got}");
        }

        if (rel.Unpinned && rel.AscUrl is not null)
        {
            var sumsBody = "";
            try { sumsBody = await FetchTextAsync($"https://github.com/{Release.Repo}/releases/download/{rel.Tag}/SHA256SUMS.txt").ConfigureAwait(false); } catch { }
            if (sumsBody.Length > 0)
            {
                var verdict = await GpgVerifyAsync(rel, sumsBody, @base, quiet).ConfigureAwait(false);
                if (verdict == "failed") { TryDelete(zipPath); throw new Exception($"Clearcote {rel.Tag}: GPG signature verification FAILED against the pinned key {Release.SigningKeyFpr} — refusing to use it."); }
                if (verdict == "ok") Log(quiet, $"auto-update: GPG signature OK (key {Release.SigningKeyFpr})");
            }
        }

        Log(quiet, "extracting");
        if (Directory.Exists(browserDir)) Directory.Delete(browserDir, true);
        var incoming = Path.Combine(@base, ".incoming");
        if (Directory.Exists(incoming)) Directory.Delete(incoming, true);
        Directory.CreateDirectory(incoming);
        if (rel.Asset.EndsWith(".tar.xz") || rel.Archive == "tar.xz")
            RunTar(zipPath, incoming);           // Node has no stdlib xz; the system tar auto-detects .xz
        else
            ZipFile.ExtractToDirectory(zipPath, incoming);
        Directory.Move(incoming, browserDir);

        var exe = FindFile(browserDir, rel.Binary) ?? throw new Exception($"Clearcote archive verified but {rel.Binary} was not found inside it.");
        if (!string.IsNullOrEmpty(rel.ExeSha256))
        {
            var exeHash = await Sha256FileAsync(exe).ConfigureAwait(false);
            if (!string.Equals(exeHash, rel.ExeSha256, StringComparison.OrdinalIgnoreCase))
                throw new Exception($"Clearcote {rel.Binary} SHA-256 mismatch — refusing to use it.\n  expected {rel.ExeSha256}\n  got      {exeHash}");
        }

        if (!OperatingSystem.IsWindows())
        {
            try { File.SetUnixFileMode(exe, (UnixFileMode)0b111_101_101); } catch { } // 0755
            var sandbox = Path.Combine(Path.GetDirectoryName(exe)!, "chrome-sandbox");
            if (File.Exists(sandbox)) { try { File.SetUnixFileMode(sandbox, (UnixFileMode)0b100_111_101_101); } catch { } } // 4755
        }
        else
        {
            WinLaunch.WarmFiles(browserDir); // close the chrome_elf.dll first-launch AV race
        }

        await File.WriteAllTextAsync(Path.Combine(@base, ".verified"), rel.Sha256 + "\n").ConfigureAwait(false);
        TryDelete(zipPath);
        Log(quiet, $"ready: {exe}");
        return exe;
    }

    private static void RunTar(string archive, string destDir)
    {
        var psi = new ProcessStartInfo("tar") { UseShellExecute = false };
        psi.ArgumentList.Add("-xf"); psi.ArgumentList.Add(archive);
        psi.ArgumentList.Add("-C"); psi.ArgumentList.Add(destDir);
        using var p = Process.Start(psi) ?? throw new Exception("Clearcote: failed to start `tar` to extract the archive.");
        p.WaitForExit();
        if (p.ExitCode != 0) throw new Exception($"Clearcote: `tar` extraction failed (exit {p.ExitCode}).");
    }

    private static void TryDelete(string path) { try { File.Delete(path); } catch { } }

    /// Download + verify the PRO (license-gated) browser and return its chrome path. Throws on any
    /// failure — a licensed caller must get the PRO build, never a silent free fall-back.
    public static async Task<string> ProEnsureBinaryAsync(string licenseKey, ProDownloadOptions? opts = null)
    {
        opts ??= new ProDownloadOptions();
        var baseUrl = (opts.ApiBase ?? Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_API") ?? "https://www.clearcotelabs.com").TrimEnd('/');
        var plat = Native.IsWindows ? "windows" : Native.IsLinux ? "linux" : null;
        if (plat is null) throw new Exception("Clearcote PRO ships Windows x64 and Linux x64 only.");

        using var client = SdkHttp.Create();
        var proUrl = $"{baseUrl}/api/v1/download/pro?platform={plat}";
        if (!string.IsNullOrEmpty(opts.Version)) proUrl += $"&version={Uri.EscapeDataString(opts.Version)}";
        var req = new HttpRequestMessage(HttpMethod.Get, proUrl);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", licenseKey);
        req.Headers.UserAgent.ParseAdd("clearcote-sdk");
        using var res = await client.SendAsync(req).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode)
        {
            var body = (await res.Content.ReadAsStringAsync().ConfigureAwait(false));
            if (body.Length > 200) body = body[..200];
            throw new Exception($"Clearcote PRO download not authorized (HTTP {(int)res.StatusCode}): {body}\n" +
                                "Check your license key and that your plan is active.");
        }
        using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
        var meta = doc.RootElement;
        string? Get(string k) => meta.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        var url = Get("url"); var sha = Get("sha256");
        if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(sha))
            throw new Exception($"Clearcote PRO build is not currently available for {plat} (the server returned no download).");

        var version = Get("version") ?? "";
        var rel = new ReleaseInfo
        {
            Tag = Get("tag") ?? $"pro-{version}",
            Version = version,
            Asset = Get("asset") ?? $"clearcote-pro-{version}-{plat}-x64.{(plat == "windows" ? "zip" : "tar.xz")}",
            Url = url!,
            Sha256 = sha!,
            ExeSha256 = Get("exe_sha256") ?? "",
            Size = meta.TryGetProperty("size", out var s) && s.TryGetInt64(out var sv) ? sv : 0,
            Os = plat,
            Archive = Get("archive") ?? (plat == "windows" ? "zip" : "tar.xz"),
            Binary = Get("binary") ?? (plat == "windows" ? "chrome.exe" : "chrome"),
            AssetGlob = $"{plat}-x64",
            Unpinned = false, // pinned -> sha256-only verify (no GPG), like the free pin
        };

        var @base = Path.Combine(opts.CacheDir ?? Native.CacheRoot(), rel.Tag);
        if (File.Exists(Path.Combine(@base, ".verified")))
        {
            var cached = FindFile(Path.Combine(@base, "browser"), rel.Binary);
            if (cached is not null) return cached;
        }
        return await FetchAndVerifyAsync(rel, @base, opts.Quiet).ConfigureAwait(false);
    }

    // ── Version catalog resolver ─────────────────────────────────────────────
    private static readonly JsonSerializerOptions CatalogJsonOpts = new() { PropertyNameCaseInsensitive = true };

    private static string? PlatKey() => Native.IsWindows ? "windows" : Native.IsLinux ? "linux" : null;

    /// Compare version strings numerically: "150.0.7871.115" > "149.0.7827.114".
    private static int VerCmp(string a, string b)
    {
        int[] Parts(string v) => Regex.Matches(v ?? "", @"\d+").Take(4).Select(m => int.Parse(m.Value)).ToArray();
        var x = Parts(a); var y = Parts(b);
        for (var i = 0; i < 4; i++)
        {
            var d = (i < x.Length ? x[i] : 0) - (i < y.Length ? y[i] : 0);
            if (d != 0) return d;
        }
        return 0;
    }

    private static async Task<Catalog> FetchCatalogAsync(bool quiet)
    {
        try
        {
            var json = await FetchTextAsync(Release.CatalogUrl).ConfigureAwait(false);
            var cat = JsonSerializer.Deserialize<Catalog>(json, CatalogJsonOpts);
            if (cat is { Builds.Count: > 0 }) return cat;
        }
        catch (Exception e)
        {
            Log(quiet, $"version catalog unreachable ({e.Message}); using the bundled snapshot");
        }
        return Release.CatalogFallback;
    }

    /// Resolve a version selector against the public catalog, VALIDATING that it exists (and is
    /// reachable for its tier) BEFORE any download — so a bad request fails fast with a helpful message
    /// instead of getting stuck. <paramref name="selector"/> may be a bare major ("150"), an exact
    /// version, or "latest". Throws when the version doesn't exist for this OS, or when it's a PRO build
    /// and <paramref name="hasLicense"/> is false.
    public static async Task<VersionPlan> ResolveVersionAsync(string selector, bool hasLicense, bool quiet = false)
    {
        var cat = await FetchCatalogAsync(quiet).ConfigureAwait(false);
        return ResolveFromCatalog(cat, selector, hasLicense);
    }

    /// Best-effort resolved browser build (version string) this launch will run, for lease TELEMETRY
    /// only. Never throws (a launch must never fail over telemetry). An exact "X.Y.Z.W" selector is
    /// returned as-is (no network); a bare major / "latest" / empty is resolved against the catalog
    /// (empty -&gt; newest usable, matching the binary path). Any failure falls back to the pinned build.
    public static async Task<string?> ResolvedEngineVersionAsync(string? selector, bool hasLicense, bool quiet = true)
    {
        try
        {
            var sel = (selector ?? "").Trim();
            if (System.Text.RegularExpressions.Regex.IsMatch(sel, @"^\d+(?:\.\d+){3}$")) return sel;
            var plan = await ResolveVersionAsync(string.IsNullOrEmpty(sel) ? "latest" : sel, hasLicense, quiet).ConfigureAwait(false);
            return plan.Kind == "pro" ? plan.Version : plan.Rel?.Version;
        }
        catch { return Release.Current.Version; }
    }

    /// Pure validate-first resolution against an in-memory catalog (no I/O).
    public static VersionPlan ResolveFromCatalog(Catalog cat, string selector, bool hasLicense)
    {
        var plat = PlatKey() ?? throw new Exception("Clearcote ships Windows x64 and Linux x64 only.");
        var builds = cat.Builds.Where(b => b.Platforms.ContainsKey(plat)).ToList();
        var sel = (selector ?? "").Trim();

        List<CatalogBuild> cands;
        if (Regex.IsMatch(sel, "^(latest|newest)$", RegexOptions.IgnoreCase))
            cands = builds.Where(b => b.Tier == "free" || hasLicense).ToList(); // newest ACCESSIBLE
        else if (Regex.IsMatch(sel, @"^\d+$"))
            cands = builds.Where(b => b.Major.ToString() == sel).ToList();       // bare major
        else
            cands = builds.Where(b => b.Version == sel).ToList();                // exact version

        if (cands.Count == 0)
        {
            var avail = string.Join(", ", builds.Select(b => $"{b.Version} ({b.Tier})"));
            throw new Exception($"No Clearcote build matches version '{selector}' for {plat}. Available: {(avail.Length > 0 ? avail : "none")}.");
        }
        var pick = cands.Aggregate((a, b) => VerCmp(b.Version, a.Version) > 0 ? b : a);

        if (pick.Tier == "pro" && !hasLicense)
        {
            var free = string.Join(", ", builds.Where(b => b.Tier == "free").Select(b => b.Version));
            throw new Exception(
                $"Clearcote {pick.Version} is a PRO build and isn't public yet — set a license key (CLEARCOTE_LICENSE_KEY, or pass LicenseKey) to use it.\n" +
                $"  Free versions you can use without a key: {(free.Length > 0 ? free : "none")}.");
        }
        if (pick.Tier == "pro") return new VersionPlan("pro", null, pick.Version);

        var p = pick.Platforms[plat];
        if (string.IsNullOrEmpty(p.Url) || string.IsNullOrEmpty(p.Sha256))
            throw new Exception($"Clearcote {pick.Version} is marked free but the catalog has no download for {plat}.");
        var rel = new ReleaseInfo
        {
            Tag = string.IsNullOrEmpty(pick.Tag) ? $"v-{pick.Version}" : pick.Tag,
            Version = pick.Version,
            Asset = p.Asset ?? $"clearcote-{pick.Version}-{plat}-x64.{(p.Archive == "zip" ? "zip" : "tar.xz")}",
            Url = p.Url!,
            Sha256 = p.Sha256!,
            ExeSha256 = p.ExeSha256 ?? "",
            Size = p.Size,
            Os = plat,
            Archive = p.Archive,
            Binary = p.Binary,
            AssetGlob = $"{plat}-x64",
            Unpinned = false, // catalog sha256 is the trust anchor -> sha256-only verify, like a pin
        };
        return new VersionPlan("free", rel, null);
    }

    /// Resolve a version selector to a downloaded, verified binary path (free from GitHub, pro via the licensed route).
    public static async Task<string> EnsureVersionAsync(string selector, string? licenseKey = null,
        string? apiBase = null, string? cacheDir = null, bool quiet = false)
    {
        var plan = await ResolveVersionAsync(selector, !string.IsNullOrEmpty(licenseKey), quiet).ConfigureAwait(false);
        if (plan.Kind == "pro")
            return await ProEnsureBinaryAsync(licenseKey!,
                new ProDownloadOptions { ApiBase = apiBase, CacheDir = cacheDir, Quiet = quiet, Version = plan.Version }).ConfigureAwait(false);

        var rel = plan.Rel!;
        var @base = Path.Combine(cacheDir ?? Native.CacheRoot(), rel.Tag);
        if (File.Exists(Path.Combine(@base, ".verified")))
        {
            var cached = FindFile(Path.Combine(@base, "browser"), rel.Binary);
            if (cached is not null) return cached;
        }
        return await FetchAndVerifyAsync(rel, @base, quiet).ConfigureAwait(false);
    }

    /// Ensure the free Clearcote binary is present and verified; return the chrome path. Cached per tag.
    public static async Task<string> EnsureBinaryAsync(DownloadOptions? opts = null)
    {
        opts ??= new DownloadOptions();
        var cacheRoot = opts.CacheDir ?? Native.CacheRoot();

        ReleaseInfo rel;
        if (AutoUpdateRequested(opts.AutoUpdate))
        {
            var latest = await ResolveLatestAsync(opts.Quiet).ConfigureAwait(false);
            rel = latest is not null && latest.Tag == Release.Current.Tag
                ? Release.Current with { Unpinned = false }  // newest IS pinned — use the audited hashes
                : latest ?? Release.Current with { Unpinned = false };
        }
        else
        {
            rel = Release.Current with { Unpinned = false };
        }

        var @base = Path.Combine(cacheRoot, rel.Tag);
        if (File.Exists(Path.Combine(@base, ".verified")))
        {
            var cached = FindFile(Path.Combine(@base, "browser"), rel.Binary);
            if (cached is not null) return cached;
        }
        return await FetchAndVerifyAsync(rel, @base, opts.Quiet).ConfigureAwait(false);
    }
}
