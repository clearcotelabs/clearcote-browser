using System.ComponentModel;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// The native macOS PRO build (Apple Silicon, a Chromium.app bundle).
//
// There is no Mac in CI, so every macOS path here runs with the OS (Native.OsTagOverride) and the CPU
// (Native.ArchOverride) faked. Covered: an explicit ExecutablePath is honoured on a Mac, macOS maps to the
// "macos" PRO download key (arm64 only), the bundle's own executable is found inside the .app (never the
// framework or a helper), the zip is unpacked with ditto and un-quarantined, the switch probe reads the
// framework, and LaunchAsync on a licensed Apple Silicon Mac runs the native build instead of Docker.
public class MacosNativeTests : IDisposable
{
    private static readonly string AppExe = Path.Combine("Chromium.app", "Contents", "MacOS", "Chromium");
    private static readonly string Framework = Path.Combine("Chromium.app", "Contents", "Frameworks", "Chromium Framework.framework");

    private readonly Sandbox _sb = new();
    private readonly string _tmp = TestTemp.Create("cc-macos-");
    private readonly Func<string, IReadOnlyList<string>, int> _runner = Download.MacToolRunner;

    public MacosNativeTests()
    {
        // Release.Current is fixed when Release is first touched: touch it on the real OS, before any test
        // here fakes macOS, or it would pin the Windows fallback for every later suite in this process.
        _ = Release.Current;
        _sb.TempHome();   // no saved licence key on this machine is read
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_DOCKER", null)
           .Env(DockerLaunch.TestOnlyAssumeMacos, null);
    }

    public void Dispose()
    {
        Native.ArchOverride = null;
        Download.MacToolRunner = _runner;
        _sb.Dispose();
        TestTemp.Remove(_tmp);
    }

    private void On(string os, string arch = "arm64")
    {
        _sb.Os(os);
        Native.ArchOverride = arch;
    }

    private static void Put(string file, byte[] data)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(file)!);
        File.WriteAllBytes(file, data);
    }

    /// A Chromium.app laid out like the real one, with decoys a name search could land on: a helper app
    /// inside the framework and a resource file that is also called "Chromium".
    private static Dictionary<string, byte[]> FakeApp(string root)
    {
        var files = new Dictionary<string, byte[]>
        {
            [AppExe] = Encoding.ASCII.GetBytes("launcher-without-switch-strings"),
            [Path.Combine("Chromium.app", "Contents", "Info.plist")] = Encoding.ASCII.GetBytes("<plist/>"),
            [Path.Combine(Framework, "Versions", "Current", "Chromium Framework")] =
                Encoding.ASCII.GetBytes(new string('x', 32) + "\0proxy-auth\0"),
            [Path.Combine(Framework, "Versions", "Current", "Resources", "Chromium")] = Encoding.ASCII.GetBytes("decoy"),
            [Path.Combine(Framework, "Versions", "Current", "Helpers", "Chromium Helper.app", "Contents", "MacOS", "Chromium Helper")] =
                Encoding.ASCII.GetBytes("helper"),
        };
        foreach (var (rel, data) in files) Put(Path.Combine(root, rel), data);
        return files;
    }

    [Fact]
    public void An_existing_executable_path_is_honoured_on_a_Mac_whatever_the_CPU()
    {
        FakeApp(_tmp);
        On("macos", "arm64");
        Clearcote.EnsureRunnableHere(Path.Combine(_tmp, AppExe));
        On("macos", "x64");
        Clearcote.EnsureRunnableHere(Path.Combine(_tmp, AppExe));
    }

    [Fact]
    public void macOS_maps_to_the_macos_PRO_key_on_Apple_Silicon_only()
    {
        Assert.Equal("macos", Release.ProPlatformKey("macos", "arm64"));
        Assert.Equal("windows", Release.ProPlatformKey("windows", "x64"));
        Assert.Equal("linux", Release.ProPlatformKey("linux", "x64"));
        Assert.Null(Release.ProPlatformKey("unknown", "x64"));
        Assert.Same(Release.Macos, Release.PlatformRelease("macos", "arm64"));   // the open macOS pin
        Assert.Null(Release.PlatformRelease("macos", "x64"));                    // arm64 only, like PRO
        var e = Assert.Throws<PlatformNotSupportedException>(() => Release.ProPlatformKey("macos", "x64"));
        Assert.Contains("Apple Silicon", e.Message);
        // the catalog lookup uses the macos key (no build listed for it, so the error names the key)
        On("macos", "arm64");
        var miss = Assert.ThrowsAny<Exception>(() => Download.ResolveFromCatalog(new Catalog(), "155", true));
        Assert.Contains("for macos", miss.Message);
    }

    [Fact]
    public async Task The_free_build_fails_on_an_Intel_Mac_before_downloading_anything()
    {
        On("macos", "x64");
        var handler = FakeHandler.Json(HttpStatusCode.OK, "{}");
        _sb.Http(handler);
        var e = await Assert.ThrowsAsync<PlatformNotSupportedException>(() =>
            Download.EnsureBinaryAsync(new DownloadOptions { CacheDir = _tmp, Quiet = true }));
        Assert.Contains("Apple Silicon only", e.Message);
        Assert.Empty(handler.Requests);
    }

    [Fact]
    public async Task The_free_build_on_Apple_Silicon_downloads_the_open_macOS_pin()
    {
        On("macos", "arm64");
        var handler = FakeHandler.Json(HttpStatusCode.NotFound, "{}");
        _sb.Http(handler);
        await Assert.ThrowsAnyAsync<Exception>(() =>
            Download.EnsureBinaryAsync(new DownloadOptions { CacheDir = _tmp, Quiet = true }));
        Assert.Contains(handler.Requests, r => r.RequestUri?.ToString() == Release.Macos.Url);
    }

    [Fact]
    public void The_switch_probe_reads_the_framework_binary_not_the_launcher()
    {
        FakeApp(_tmp);
        Assert.True(LaunchOpts.EngineSupportsSwitch(Path.Combine(_tmp, AppExe), "proxy-auth"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(Path.Combine(_tmp, AppExe), "no-such-switch"));
    }

    [Fact]
    public void Mock_keychain_is_a_macOS_default()
    {
        Assert.Equal(new[] { "--use-mock-keychain" }, LaunchOpts.MacosKeychainArgs("macos"));
        Assert.Empty(LaunchOpts.MacosKeychainArgs("macos", new[] { "--use-mock-keychain" }));
        Assert.Empty(LaunchOpts.MacosKeychainArgs("linux"));
        Assert.Empty(LaunchOpts.MacosKeychainArgs("windows"));
    }

    [Fact]
    public void A_Windows_persona_on_a_Mac_warns_that_only_macos_is_coherent()
    {
        var hit = LaunchWarnings.ForPersonaHost(new LaunchOptions { Platform = "windows", Fingerprint = "seed-1" }, "macos")
            .SingleOrDefault(w => w.Code == LaunchWarnings.PlatformHostFonts);
        Assert.NotNull(hit);
        Assert.Contains("only Platform = \"macos\" is coherent", hit!.Message);
        Assert.DoesNotContain(LaunchWarnings.ForPersonaHost(new LaunchOptions { Platform = "macos" }, "macos"),
            w => w.Code == LaunchWarnings.PlatformHostFonts);
        // a profile the caller captured on Windows is the documented workaround
        Assert.DoesNotContain(LaunchWarnings.ForPersonaHost(new LaunchOptions { Platform = "windows", FingerprintProfile = new { } }, "macos"),
            w => w.Code == LaunchWarnings.PlatformHostFonts);
        // not a macOS host: unchanged
        Assert.DoesNotContain(LaunchWarnings.ForPersonaHost(new LaunchOptions { Platform = "windows" }, "linux"),
            w => w.Code == LaunchWarnings.PlatformHostFonts);
    }

    [Fact]
    public void Default_persona_platform_on_a_Mac_is_macos()
    {
        On("macos");
        var args = Fingerprint.Args(new FingerprintOptions { Fingerprint = "seed-1" });
        Assert.Contains("--fingerprint-platform=macos", args);
    }

    [Fact]
    public void Launch_runs_a_native_build_on_Apple_Silicon_and_Docker_on_an_Intel_Mac()
    {
        bool Decide(string arch, LaunchOptions? o = null)
        {
            On("macos", arch);
            return DockerLaunch.Requested(o);
        }
        Assert.False(Decide("arm64", new LaunchOptions { LicenseKey = "cc_lic_x" }));   // native PRO build
        Assert.False(Decide("arm64"));                                                   // no key: the open macOS pin
        Assert.True(Decide("x64", new LaunchOptions { LicenseKey = "cc_lic_x" }));      // Intel: both builds are arm64 only
        Assert.True(Decide("x64"));
        Assert.True(Decide("arm64", new LaunchOptions { LicenseKey = "cc_lic_x", Docker = true }));   // Docker stays reachable
        // the test-only "assume macOS" switch exists to exercise the Docker path: it stays Docker
        On("linux", "arm64");
        _sb.Env(DockerLaunch.TestOnlyAssumeMacos, "1");
        Assert.True(DockerLaunch.Requested(new LaunchOptions { LicenseKey = "cc_lic_x" }));
    }

    [Fact]
    public async Task PRO_download_asks_for_macos_unpacks_with_ditto_and_clears_quarantine()
    {
        On("macos", "arm64");
        var files = FakeApp(Path.Combine(_tmp, "src"));
        byte[] zip;
        using (var ms = new MemoryStream())
        {
            using (var archive = new ZipArchive(ms, ZipArchiveMode.Create, leaveOpen: true))
                foreach (var (rel, data) in files)
                {
                    using var s = archive.CreateEntry(rel.Replace('\\', '/')).Open();
                    s.Write(data);
                }
            zip = ms.ToArray();
        }
        var sha = Convert.ToHexString(SHA256.HashData(zip)).ToLowerInvariant();
        var exeSha = Convert.ToHexString(SHA256.HashData(files[AppExe])).ToLowerInvariant();
        var handler = new FakeHandler(req =>
        {
            if (req.RequestUri!.AbsolutePath.EndsWith("/api/v1/download/pro"))
                // the route's reply for macOS, minus "binary": the SDK's macOS default must be the bundle's
                return new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent(JsonSerializer.Serialize(new
                    {
                        version = "155.0.8059.39", tag = "pro-155.0.8059.39-r42", platform = "macos",
                        asset = "clearcote-pro-155.0.8059.39-macos-arm64.zip", archive = "zip",
                        url = "https://blob.test/mac.zip", sha256 = sha, exe_sha256 = exeSha, size = zip.Length,
                    })),
                };
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(zip) };
        });
        _sb.Http(handler);
        var calls = new List<string[]>();
        Download.MacToolRunner = (file, args) =>
        {
            calls.Add(args.Prepend(file).ToArray());
            if (file == "/usr/bin/ditto") ZipFile.ExtractToDirectory(args[2], args[3]);   // minus symlinks and modes
            return 0;
        };

        var exe = await Download.ProEnsureBinaryAsync("cc_lic_mac",
            new ProDownloadOptions { ApiBase = "https://site.test", CacheDir = Path.Combine(_tmp, "cache"), Quiet = true });

        Assert.Contains("platform=macos", handler.Requests[0].RequestUri!.Query);
        Assert.EndsWith(AppExe, exe);
        Assert.Equal("launcher-without-switch-strings", File.ReadAllText(exe));
        Assert.Equal(new[] { "/usr/bin/ditto", "-x", "-k" }, calls[0].Take(3));
        var xattr = calls.Single(c => c[0] == "/usr/bin/xattr");
        Assert.Equal(new[] { "-dr", "com.apple.quarantine" }, xattr.Skip(1).Take(2));
        Assert.EndsWith("Chromium.app", xattr[3]);
        // cached: the darwin integrity list is the bundle's, and the cache finds the same executable
        Assert.Equal(exe, Download.CachedBinary(Path.Combine(_tmp, "cache", "pro-155.0.8059.39-r42"), "Chromium", quiet: true));
    }

    [Fact]
    public void Falls_back_to_unzip_without_ditto()
    {
        var calls = new List<string>();
        Download.MacToolRunner = (file, args) =>
        {
            calls.Add(file);
            if (file == "/usr/bin/ditto") throw new Win32Exception(2, "No such file or directory");
            return 0;
        };
        Download.ExtractMacBundle("x.zip", Path.Combine(_tmp, "x.zip"), Path.Combine(_tmp, "out"));
        Assert.Equal(new[] { "/usr/bin/ditto", "/usr/bin/unzip" }, calls.Take(2));
    }
}
