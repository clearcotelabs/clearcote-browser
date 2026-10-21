using System.Text.Json.Nodes;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// Mirrors the Node SDK's test/profileimport.test.ts (and the import cases of Python's test_profileauto.py).
public sealed class ProfileImportTests : IDisposable
{
    private readonly string _dir = TestTemp.Create("cc-import-");

    internal static JsonObject Blob(Action<JsonObject>? over = null)
    {
        var p = (JsonObject)JsonNode.Parse("""
            {
              "meta": { "captured_at": "2026-07-01T00:00:00.000Z", "schema_version": 1 },
              "navigator": {
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
                "platform": "Win32"
              },
              "screen": { "width": 1920, "height": 1080, "device_pixel_ratio": 1 },
              "webgl": { "webgl1": { "debug": {
                "UNMASKED_RENDERER_WEBGL": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 (0x0000220A) Direct3D11 vs_5_0 ps_5_0, D3D11)" } } },
              "hardware_concurrency": 16,
              "device_memory": 16
            }
            """)!;
        over?.Invoke(p);
        return p;
    }

    private static readonly HostFacts Host = new()
    {
        OsFamily = "windows", BrowserMajor = 150, GpuVendor = "nvidia",
        ScreenWidth = 3440, ScreenHeight = 1440, DevicePixelRatio = 1, HardwareConcurrency = 16, DeviceMemory = 16,
    };

    public ProfileImportTests()
    {
        File.WriteAllText(Path.Combine(_dir, "win-nvidia.json"), Blob().ToJsonString());
        File.WriteAllText(Path.Combine(_dir, "mac.json"), Blob(p => p["navigator"] = JsonNode.Parse("""
            {"user_agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36","platform":"MacIntel"}
            """)).ToJsonString());
        // A converted dataset record: hardware identity, deliberately NO Chrome version.
        File.WriteAllText(Path.Combine(_dir, "hardware-only.json"), Blob(p => p["navigator"] = JsonNode.Parse("""
            {"user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","platform":"Win32"}
            """)).ToJsonString());
        File.WriteAllText(Path.Combine(_dir, "broken.json"), "{ not json");
        File.WriteAllText(Path.Combine(_dir, "not-a-profile.json"), """{"hello":"world"}""");
        File.WriteAllText(Path.Combine(_dir, "ignored.txt"), "not json at all");
    }

    public void Dispose() => TestTemp.Remove(_dir);

    private ProfileIndexEntry Find(string id) => ProfileImport.ImportDirectory(_dir).Index.Single(e => e.Id == id);

    [Fact]
    public void Indexes_every_valid_profile_and_reports_the_rest()
    {
        var r = ProfileImport.ImportDirectory(_dir);
        Assert.Equal(new[] { "hardware-only", "mac", "win-nvidia" }, r.Index.Select(e => e.Id).OrderBy(x => x, StringComparer.Ordinal));
        // A half-converted directory must be visible, not silently thin.
        Assert.Equal(new[] { "broken.json", "not-a-profile.json" }, r.Skipped.Select(s => s.File).OrderBy(x => x, StringComparer.Ordinal));
        Assert.StartsWith("unreadable JSON", r.Skipped.Single(s => s.File == "broken.json").Reason);
        Assert.Contains("no navigator", r.Skipped.Single(s => s.File == "not-a-profile.json").Reason);
    }

    [Fact]
    public void Derives_os_family_gpu_class_and_geometry_from_the_blob()
    {
        var e = Find("win-nvidia");
        Assert.Equal("windows", e.OsFamily);
        Assert.Equal("nvidia", e.GpuVendor);
        Assert.Equal(150, e.BrowserMajor);
        Assert.Equal(1920, e.ScreenWidth);
        Assert.Equal(16, e.HardwareConcurrency);
        Assert.Equal("2026-07-01T00:00:00.000Z", e.CapturedAt);
    }

    [Fact]
    public void Classifies_macOS_from_navigator_platform() => Assert.Equal("macos", Find("mac").OsFamily);

    [Fact]
    public void Measures_the_ENCODED_size_so_an_oversized_profile_is_caught_before_spawn()
    {
        var e = Find("win-nvidia");
        Assert.InRange(e.EncodedSize!.Value, 1, ProfileLib.DefaultMaxEncoded - 1);
        Assert.Equal(Fingerprint.EncodeProfile(Blob()).Length, e.EncodedSize);
    }

    [Fact]
    public void Throws_a_clear_error_when_the_source_is_not_a_directory()
        => Assert.Contains("not a directory", Assert.Throws<DirectoryNotFoundException>(() => ProfileImport.ImportDirectory(Path.Combine(_dir, "win-nvidia.json"))).Message);

    // ── version-agnostic (hardware-only) profiles ───────────────────────────

    [Fact]
    public void Records_no_browser_major_when_the_UA_carries_no_Chrome_version() => Assert.Null(Find("hardware-only").BrowserMajor);

    [Fact]
    public void Hardware_only_profiles_are_eligible_on_any_engine()
    {
        var e = Find("hardware-only");
        Assert.True(ProfileLib.Eligible(e, Host));
        Assert.True(ProfileLib.Eligible(e, new HostFacts { OsFamily = "windows", BrowserMajor = 138 }));
    }

    [Fact]
    public void A_profile_that_PINS_the_wrong_version_is_still_rejected()
    {
        var e = ProfileImport.IndexEntryFromProfile("old", (JsonObject)JsonNode.Parse("""
            {"navigator":{"user_agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/138.0.0.0","platform":"Win32"},"screen":{"width":1920,"height":1080}}
            """)!)!;
        Assert.Equal(138, e.BrowserMajor);
        Assert.False(ProfileLib.Eligible(e, Host));
    }

    // ── end to end ──────────────────────────────────────────────────────────

    [Fact]
    public void An_imported_directory_feeds_selection_directly()
    {
        var index = ProfileImport.ImportDirectory(_dir).Index;
        var sel = ProfileLib.SelectProfile(index, Host, new SelectOptions { Mode = SelectMode.Best });
        Assert.Contains(sel.Entry.Id, new[] { "win-nvidia", "hardware-only" });
        Assert.NotNull(ProfileImport.LoadImportedProfile(_dir, sel.Entry.Id)["navigator"]);
    }

    [Fact]
    public void Refuses_ids_that_could_escape_the_source_directory()
    {
        Assert.Contains("invalid profile id", Assert.Throws<ArgumentException>(() => ProfileImport.LoadImportedProfile(_dir, "../../etc/passwd")).Message);
        Assert.Contains("invalid profile id", Assert.Throws<ArgumentException>(() => ProfileImport.LoadImportedProfile(_dir, "sub/other")).Message);
        Assert.Contains("invalid profile id", Assert.Throws<ArgumentException>(() => ProfileImport.LoadImportedProfile(_dir, "sub\\other")).Message);
    }

    [Fact]
    public void Reports_a_missing_id_clearly()
        => Assert.Contains("not found", Assert.Throws<FileNotFoundException>(() => ProfileImport.LoadImportedProfile(_dir, "nope")).Message);

    // ── converted dataset records (UA-CH only) ─────────────────────────────

    private const string WindowsUaData = """
        { "user_agent": null, "platform": null,
          "uadata": { "platform": "Windows", "mobile": false,
                      "high_entropy": { "platform": "Windows", "platformVersion": "15.0.0", "architecture": "x86", "bitness": "64" } } }
        """;

    private static JsonObject Converted(string navigator = WindowsUaData) => (JsonObject)JsonNode.Parse($$"""
        {
          "meta": { "schema_version": 1, "source": "chrome-fingerprints", "captured_at": null, "chrome_version": null },
          "navigator": {{navigator}},
          "screen": { "width": 1536, "height": 864, "device_pixel_ratio": 1.25 },
          "webgl": { "webgl1": { "debug": { "UNMASKED_RENDERER_WEBGL": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)" } } },
          "hardware_concurrency": 12,
          "device_memory": 8
        }
        """)!;

    [Fact]
    public void Classifies_the_OS_from_uadata_when_the_UA_string_is_null()
    {
        var e = ProfileImport.IndexEntryFromProfile("conv", Converted())!;
        Assert.Equal("windows", e.OsFamily);
        Assert.Equal("intel", e.GpuVendor);
        Assert.Equal(1536, e.ScreenWidth);
        Assert.Equal(1.25, e.DevicePixelRatio);
        Assert.Null(e.BrowserMajor);   // the record stays engine-agnostic
        Assert.Null(e.CapturedAt);
    }

    [Fact]
    public void Classifies_macOS_and_Android_from_uadata_too()
    {
        Assert.Equal("macos", ProfileImport.IndexEntryFromProfile("m", Converted("""{"user_agent":null,"platform":null,"uadata":{"platform":"macOS","mobile":false}}"""))!.OsFamily);
        Assert.Equal("android", ProfileImport.IndexEntryFromProfile("a", Converted("""{"user_agent":null,"platform":null,"uadata":{"platform":"Android","mobile":true}}"""))!.OsFamily);
    }

    [Fact]
    public void Treats_uadata_mobile_as_android_even_when_the_platform_string_is_odd()
        => Assert.Equal("android", ProfileImport.IndexEntryFromProfile("x", Converted("""{"user_agent":null,"platform":null,"uadata":{"platform":"","mobile":true}}"""))!.OsFamily);

    [Fact]
    public void Still_skips_a_record_with_no_usable_platform_signal()
        => Assert.Null(ProfileImport.IndexEntryFromProfile("blank", Converted("""{"user_agent":null,"platform":null,"uadata":{}}""")));
}
