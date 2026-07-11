using System.IO.Compression;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

public class FingerprintTests
{
    [Fact]
    public void Default_persona_on_windows_host()
    {
        using var _ = new Sandbox().Os("windows");
        Assert.Equal(
            new[] { "--fingerprint-platform=windows", "--fingerprint-brand=chrome", "--accept-lang=en-US,en", "--lang=en-US", "--timezone=America/New_York" },
            Fingerprint.Args(new FingerprintOptions()));
    }

    [Fact]
    public void Default_persona_on_linux_host()
    {
        using var _ = new Sandbox().Os("linux");
        Assert.Equal(
            new[] { "--fingerprint-platform=linux", "--fingerprint-brand=chrome", "--accept-lang=en-US,en", "--lang=en-US", "--timezone=America/New_York" },
            Fingerprint.Args(new FingerprintOptions()));
    }

    [Fact]
    public void Lang_derived_from_primary_accept_language_tag()
    {
        using var _ = new Sandbox().Os("windows");
        Assert.Contains("--lang=fr-FR", Fingerprint.Args(new FingerprintOptions { AcceptLanguage = "fr-FR,fr" }));
        Assert.Contains("--lang=de-DE", Fingerprint.Args(new FingerprintOptions { AcceptLanguage = "de-DE,de;q=0.7,en;q=0.3" }));
    }

    [Fact]
    public void Maps_every_fingerprint_option_to_its_switch()
    {
        using var _ = new Sandbox().Os("windows");
        var args = Fingerprint.Args(new FingerprintOptions
        {
            Fingerprint = "seed-1", Platform = "windows", PlatformVersion = "10.0.0", Brand = "Edge", BrandVersion = "149",
            GpuVendor = "Google Inc.", GpuRenderer = "ANGLE (Intel)", HardwareConcurrency = 8,
            Location = "40.7,-74.0", Timezone = "America/New_York", WebrtcIp = "1.2.3.4",
        });
        foreach (var expected in new[]
        {
            "--fingerprint=seed-1", "--fingerprint-platform=windows", "--fingerprint-platform-version=10.0.0",
            "--fingerprint-brand=Edge", "--fingerprint-brand-version=149", "--fingerprint-gpu-vendor=Google Inc.",
            "--fingerprint-gpu-renderer=ANGLE (Intel)", "--fingerprint-hardware-concurrency=8",
            "--fingerprint-location=40.7,-74.0", "--timezone=America/New_York", "--webrtc-ip=1.2.3.4",
        })
            Assert.Contains(expected, args);
    }

    [Fact]
    public void Cleans_accept_language_for_switch()
        => Assert.Contains("--accept-lang=en-US,en", Fingerprint.Args(new FingerprintOptions { AcceptLanguage = "en-US,en;q=0.9" }));

    [Fact]
    public void DisableGpuFingerprint_only_when_true()
    {
        Assert.Contains("--disable-gpu-fingerprint", Fingerprint.Args(new FingerprintOptions { DisableGpuFingerprint = true }));
        Assert.DoesNotContain("--disable-gpu-fingerprint", Fingerprint.Args(new FingerprintOptions { DisableGpuFingerprint = false }));
    }

    [Fact]
    public void DisableFingerprintNoise_only_when_false()
    {
        Assert.Contains("--disable-fingerprint-noise", Fingerprint.Args(new FingerprintOptions { FingerprintNoise = false }));
        Assert.DoesNotContain("--disable-fingerprint-noise", Fingerprint.Args(new FingerprintOptions { FingerprintNoise = true }));
        Assert.DoesNotContain("--disable-fingerprint-noise", Fingerprint.Args(new FingerprintOptions()));
    }

    [Fact]
    public void Skips_empty_but_still_defaults_timezone()
    {
        using var _ = new Sandbox().Os("windows");
        var args = Fingerprint.Args(new FingerprintOptions { Fingerprint = "", Timezone = null, GpuVendor = "" });
        Assert.DoesNotContain(args, a => a.StartsWith("--fingerprint="));
        Assert.DoesNotContain(args, a => a.StartsWith("--fingerprint-gpu-vendor="));
        Assert.Contains("--timezone=America/New_York", args); // locale default; no host-UTC leak
    }

    [Fact]
    public void Encodes_fingerprint_profile_gzip_base64_lossless()
    {
        var profile = new Dictionary<string, object>
        {
            ["navigator"] = new Dictionary<string, object> { ["userAgent"] = "Mozilla/5.0 X" },
            ["screen"] = new Dictionary<string, object> { ["width"] = 1920 },
        };
        var arg = Fingerprint.Args(new FingerprintOptions { FingerprintProfile = profile })
            .First(a => a.StartsWith("--fingerprint-profile="));
        var b64 = arg["--fingerprint-profile=".Length..];
        using var ms = new MemoryStream(Convert.FromBase64String(b64));
        using var gz = new GZipStream(ms, CompressionMode.Decompress);
        using var doc = JsonDocument.Parse(new StreamReader(gz).ReadToEnd());
        Assert.Equal("Mozilla/5.0 X", doc.RootElement.GetProperty("navigator").GetProperty("userAgent").GetString());
        Assert.Equal(1920, doc.RootElement.GetProperty("screen").GetProperty("width").GetInt32());
    }

    [Theory]
    [InlineData("en-US, en ;q=0.8, , fr", "en-US,en,fr")]
    [InlineData("de-DE,de;q=0.7,en;q=0.3", "de-DE,de,en")]
    [InlineData("", "")]
    public void CleanAcceptLanguage(string input, string expected)
        => Assert.Equal(expected, Fingerprint.CleanAcceptLanguage(input));

    [Fact]
    public void Tls_match_persona_follows_brand_version_major()
        => Assert.Contains("--fingerprint-tls-profile=chrome-120",
            Fingerprint.Args(new FingerprintOptions { BrandVersion = "120.0.6099.109" }));

    [Fact]
    public void Tls_none_without_brand_version()
        => Assert.DoesNotContain(Fingerprint.Args(new FingerprintOptions()), a => a.StartsWith("--fingerprint-tls-profile"));

    [Fact]
    public void Tls_explicit_pin_and_off()
    {
        Assert.Contains("--fingerprint-tls-profile=chrome-124", Fingerprint.Args(new FingerprintOptions { TlsProfile = "chrome-124" }));
        Assert.Contains("--fingerprint-tls-profile=chrome-118", Fingerprint.Args(new FingerprintOptions { TlsProfile = "118" }));
        Assert.DoesNotContain(Fingerprint.Args(new FingerprintOptions { TlsProfile = "native", BrandVersion = "120" }), a => a.StartsWith("--fingerprint-tls-profile"));
        Assert.DoesNotContain(Fingerprint.Args(new FingerprintOptions { TlsProfile = "off", BrandVersion = "120" }), a => a.StartsWith("--fingerprint-tls-profile"));
        Assert.DoesNotContain(Fingerprint.Args(new FingerprintOptions { TlsProfile = "firefox-121" }), a => a.StartsWith("--fingerprint-tls-profile"));
    }

    [Fact]
    public void ResolveTlsProfile_unit()
    {
        Assert.Equal("chrome-131", Fingerprint.ResolveTlsProfile("match-persona", new FingerprintOptions { BrandVersion = "131.0.1" }));
        Assert.Null(Fingerprint.ResolveTlsProfile("auto", new FingerprintOptions()));
        Assert.Null(Fingerprint.ResolveTlsProfile(null, new FingerprintOptions()));
        Assert.Equal("chrome-120", Fingerprint.ResolveTlsProfile("chrome-120", new FingerprintOptions()));
        Assert.Equal("chrome-125", Fingerprint.ResolveTlsProfile("125", new FingerprintOptions()));
        Assert.Null(Fingerprint.ResolveTlsProfile("off", new FingerprintOptions { BrandVersion = "120" }));
        Assert.Null(Fingerprint.ResolveTlsProfile("garbage", new FingerprintOptions()));
    }

    [Theory]
    [InlineData("en-US", "America/New_York")]
    [InlineData("de-DE", "Europe/Berlin")]
    [InlineData("ja-JP", "Asia/Tokyo")]
    [InlineData("en-ZA", "America/New_York")]
    [InlineData("xx-YY", "America/New_York")]
    public void DefaultTimezone(string lang, string tz)
        => Assert.Equal(tz, Fingerprint.DefaultTimezone(lang));

    [Fact]
    public void Locale_coherent_timezone_default_explicit_wins()
    {
        using var _ = new Sandbox().Os("windows");
        Assert.Contains("--timezone=Europe/Paris", Fingerprint.Args(new FingerprintOptions { AcceptLanguage = "fr-FR,fr" }));
        var tzArgs = Fingerprint.Args(new FingerprintOptions { Timezone = "Asia/Dubai" }).Where(a => a.StartsWith("--timezone=")).ToList();
        Assert.Equal(new[] { "--timezone=Asia/Dubai" }, tzArgs);
    }

    [Fact]
    public void Android_persona_gets_phone_window_size()
    {
        var args = Fingerprint.Args(new FingerprintOptions { Platform = "android" });
        Assert.Contains("--fingerprint-platform=android", args);
        Assert.Contains("--window-size=412,915", args);
    }

    [Theory]
    [InlineData("windows")]
    [InlineData("linux")]
    [InlineData("macos")]
    public void No_auto_window_size_for_desktop(string platform)
        => Assert.DoesNotContain(Fingerprint.Args(new FingerprintOptions { Platform = platform }), a => a.StartsWith("--window-size"));
}
