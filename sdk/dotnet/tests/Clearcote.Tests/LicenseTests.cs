using System.Net;
using System.Text.RegularExpressions;
using Xunit;

namespace Clearcote.Tests;

public class LicenseTests
{
    // ── resolveInstanceId: env > file > generated+persisted ───────────────────
    [Fact]
    public void ResolveInstanceId_prefers_env_and_trims()
    {
        using var _ = new Sandbox().Env("CLEARCOTE_INSTANCE_ID", "  machine-7  ");
        Assert.Equal("machine-7", License.ResolveInstanceId());
    }

    [Fact]
    public void ResolveInstanceId_persists_and_is_stable()
    {
        using var s = new Sandbox().Env("CLEARCOTE_INSTANCE_ID", null);
        var home = s.TempHome();
        var a = License.ResolveInstanceId();
        var b = License.ResolveInstanceId();
        Assert.Equal(a, b);                          // a restart reuses its slot
        Assert.True(a.Length >= 8);
        Assert.Equal(a, File.ReadAllText(Path.Combine(home, ".clearcote", "instance_id")).Trim());
    }

    // ── resolveLicenseKey: explicit > env > file ──────────────────────────────
    [Fact]
    public void ResolveLicenseKey_prefers_explicit_and_trims()
    {
        using var _ = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", "cc_lic_from_env");
        Assert.Equal("cc_lic_explicit", License.ResolveLicenseKey("  cc_lic_explicit  "));
    }

    [Fact]
    public void ResolveLicenseKey_falls_back_to_env_when_blank_explicit()
    {
        using var _ = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", "cc_lic_from_env");
        Assert.Equal("cc_lic_from_env", License.ResolveLicenseKey());
        Assert.Equal("cc_lic_from_env", License.ResolveLicenseKey("   "));
    }

    [Fact]
    public void ResolveLicenseKey_reads_the_home_file_last()
    {
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null);
        var home = s.TempHome();
        Directory.CreateDirectory(Path.Combine(home, ".clearcote"));
        File.WriteAllText(Path.Combine(home, ".clearcote", "license.key"), "cc_lic_from_file\n");
        Assert.Equal("cc_lic_from_file", License.ResolveLicenseKey());
    }

    // ── executablePath precedence: explicit path > CLEARCOTE_BINARY > pro > free ─
    [Fact]
    public async Task ExecutablePath_explicit_wins_with_no_download()
    {
        using var _ = new Sandbox().Env("CLEARCOTE_BINARY", "/opt/env/chrome");
        Assert.Equal("/opt/custom/chrome",
            await Clearcote.ExecutablePathAsync(new LaunchOptions { ExecutablePath = "/opt/custom/chrome" }));
    }

    [Fact]
    public async Task ExecutablePath_env_binary_beats_pro_selector()
    {
        using var _ = new Sandbox().Env("CLEARCOTE_BINARY", "/opt/env/chrome");
        // Even WITH a license key, the explicit env binary short-circuits before any PRO fetch.
        Assert.Equal("/opt/env/chrome",
            await Clearcote.ExecutablePathAsync(new LaunchOptions { LicenseKey = "cc_lic_x" }));
    }

    // ── proEnsureBinary: license-gated download, fail closed ──────────────────
    [Fact]
    public async Task ProEnsureBinary_surfaces_auth_failure()
    {
        using var _ = new Sandbox().Http(FakeHandler.Json(HttpStatusCode.Unauthorized, "{\"error\":\"Invalid license key.\"}"));
        var ex = await Assert.ThrowsAsync<Exception>(() => Download.ProEnsureBinaryAsync("cc_lic_bad", new ProDownloadOptions { Quiet = true }));
        Assert.Contains("not authorized (HTTP 401)", ex.Message);
    }

    [Fact]
    public async Task ProEnsureBinary_throws_when_no_download_url()
    {
        using var _ = new Sandbox().Http(FakeHandler.Json(HttpStatusCode.OK, "{\"version\":\"149.0.0.0\"}"));
        var ex = await Assert.ThrowsAsync<Exception>(() => Download.ProEnsureBinaryAsync("cc_lic_ok", new ProDownloadOptions { Quiet = true }));
        Assert.Contains("not currently available", ex.Message);
    }

    [Fact]
    public async Task ProEnsureBinary_calls_authenticated_route_with_bearer()
    {
        var handler = FakeHandler.Json(HttpStatusCode.OK, "{}"); // empty -> throws "no download", but request captured
        using var _ = new Sandbox().Http(handler);
        await Assert.ThrowsAsync<Exception>(() =>
            Download.ProEnsureBinaryAsync("cc_lic_probe", new ProDownloadOptions { ApiBase = "https://example.test", Quiet = true }));

        var req = handler.Requests.Single();
        Assert.Matches(@"^https://example\.test/api/v1/download/pro\?platform=(windows|linux)$", req.RequestUri!.ToString());
        Assert.Equal("Bearer cc_lic_probe", req.Headers.Authorization!.ToString());
    }
}
