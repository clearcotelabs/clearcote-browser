using Xunit;

namespace Clearcote.Tests;

/// GpuVendor: a short name went out verbatim ("intel" as UNMASKED_VENDOR_WEBGL, next to the persona's AMD renderer;
/// measured 2026-10-28, SC 295). It is now written the way Chrome reports it, and a vendor/renderer pair naming two GPU
/// makers is refused. Mirrors sdk/node/test/gpu-vendor.test.ts.
public class GpuVendorTests
{
    private const string Amd = "ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001506) Direct3D11 vs_5_0 ps_5_0, D3D11)";
    private const string Intel = "ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00005917) Direct3D11 vs_5_0 ps_5_0, D3D11)";

    [Theory]
    [InlineData("intel", "Google Inc. (Intel)")]
    [InlineData("Intel Inc.", "Google Inc. (Intel)")]
    [InlineData("Intel Corporation", "Google Inc. (Intel)")]
    [InlineData("nvidia", "Google Inc. (NVIDIA)")]
    [InlineData("NVIDIA Corporation", "Google Inc. (NVIDIA)")]
    [InlineData("amd", "Google Inc. (AMD)")]
    [InlineData("ATI Technologies Inc.", "Google Inc. (AMD)")]
    [InlineData("Apple", "Google Inc. (Apple)")]
    public void Written_as_Chrome_reports_it(string given, string want)
        => Assert.Equal(want, Fingerprint.GpuVendorFor(given, null, "windows"));

    [Fact]
    public void Keeps_Chrome_form_an_Intel_Mac_and_unknown_strings()
    {
        Assert.Equal("Google Inc. (NVIDIA)", Fingerprint.GpuVendorFor("Google Inc. (NVIDIA)"));
        Assert.Equal("Google Inc. (Intel Inc.)", Fingerprint.GpuVendorFor("intel", null, "macos"));
        Assert.Equal("Google Inc.", Fingerprint.GpuVendorFor("Google Inc.", Intel));
        Assert.Equal("Some Vendor", Fingerprint.GpuVendorFor("Some Vendor"));
    }

    [Fact]
    public void Refuses_two_makers()
    {
        Assert.Contains("names Intel but GpuRenderer names AMD",
            Assert.Throws<ArgumentException>(() => Fingerprint.GpuVendorFor("intel", Amd)).Message);
        Assert.Throws<ArgumentException>(() => Fingerprint.Args(new FingerprintOptions { Fingerprint = "1", GpuVendor = "intel", GpuRenderer = Amd }));
        Assert.Equal("Google Inc. (Intel)", Fingerprint.GpuVendorFor("intel", Intel));
    }

    [Fact]
    public void Reads_the_maker()
    {
        Assert.Equal("amd", Fingerprint.GpuMaker(Amd));
        Assert.Equal("intel", Fingerprint.GpuMaker("Google Inc. (Intel)"));
        Assert.Equal("google", Fingerprint.GpuMaker("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)"));
        Assert.Null(Fingerprint.GpuMaker("WebKit"));
    }

    [Fact]
    public void Normalized_on_the_command_line()
        => Assert.Contains("--fingerprint-gpu-vendor=Google Inc. (Intel)",
            Fingerprint.Args(new FingerprintOptions { Fingerprint = "1", Platform = "windows", GpuVendor = "intel" }));

    [Fact]
    public void Warns_when_the_vendor_is_pinned_alone()
    {
        Assert.Contains(LaunchWarnings.Coherence(new FingerprintOptions { GpuVendor = "intel" }), w => w.Code == "gpu-vendor-alone");
        Assert.DoesNotContain(LaunchWarnings.Coherence(new FingerprintOptions { GpuVendor = "intel", GpuRenderer = Intel }),
            w => w.Code == "gpu-vendor-alone");
    }
}
