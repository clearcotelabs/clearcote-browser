using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LaunchAsync's macOS path against the REAL Clearcote image: docker run, CDP, a page that loads, the
/// persona options reaching the engine, and no container left after CloseAsync.
///
/// Off by default (a no-op, as GeometryLiveTests): set CLEARCOTE_TEST_DOCKER_IMAGE to the image to run, with
/// a working docker. CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 is set so the macOS decision itself sends LaunchAsync
/// to Docker. Mirrors sdk/node/test/docker-launch.live.test.ts and sdk/python/tests/test_docker_launch_live.py.
public sealed class DockerLaunchLiveTests : IDisposable
{
    private readonly Sandbox _sb = new();
    private readonly string? _image = Environment.GetEnvironmentVariable("CLEARCOTE_TEST_DOCKER_IMAGE");

    public DockerLaunchLiveTests()
    {
        foreach (var k in new[] { "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_CLOUD" }) _sb.Env(k, null);
        _sb.Env(DockerLaunch.TestOnlyAssumeMacos, "1").Env("CLEARCOTE_DOCKER_IMAGE", _image);
        _sb.TempHome();   // no saved licence key: the image's open engine
    }

    public void Dispose() => _sb.Dispose();

    private static (int Code, string Out) Docker(params string[] args)
    {
        var psi = new System.Diagnostics.ProcessStartInfo("docker") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        foreach (var a in args) psi.ArgumentList.Add(a);
        using var p = System.Diagnostics.Process.Start(psi)!;
        var output = p.StandardOutput.ReadToEnd();
        p.StandardError.ReadToEnd();
        p.WaitForExit();
        return (p.ExitCode, output);
    }

    private static bool ContainerExists(string id) => Docker("ps", "-a", "-q", "--no-trunc", "--filter", $"id={id}").Out.Trim().Length > 0;

    /// The image declares VOLUME /opt/xdg-cache: every container gets an anonymous volume holding a copy of
    /// the engine (~0.5 GB). It must go when the container does.
    private static string[] AnonymousVolumes(string id) =>
        Docker("inspect", "-f", "{{range .Mounts}}{{if eq .Type \"volume\"}}{{.Name}} {{end}}{{end}}", id).Out
            .Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    private static bool VolumeExists(string name) => Docker("volume", "inspect", name).Code == 0;

    private static async Task<(IPage Page, JsonElement Got)> ProbeAsync(IBrowser browser)
    {
        var page = await browser.NewPageAsync();
        await page.GotoAsync("data:text/html,<title>loaded in docker</title><p>hi</p>");
        var got = await page.EvaluateAsync<JsonElement>(
            "() => ({title: document.title, tz: Intl.DateTimeFormat().resolvedOptions().timeZone, platform: navigator.platform, ua: navigator.userAgent})");
        return (page, got);
    }

    [Fact]
    public async Task Runs_the_real_image_loads_a_page_carries_the_persona_and_leaves_no_container()
    {
        if (string.IsNullOrEmpty(_image) || DockerLaunch.Which() is null) return;

        // control: the image's own defaults
        var plain = await Clearcote.LaunchAsync(new LaunchOptions { Quiet = true });
        var id0 = Clearcote.DockerContainerOf(plain)!.Id;
        JsonElement baseline;
        string[] vols0;
        try
        {
            Assert.True(ContainerExists(id0));
            vols0 = AnonymousVolumes(id0);
            Assert.NotEmpty(vols0);
            Assert.All(vols0, v => Assert.True(VolumeExists(v)));
            baseline = (await ProbeAsync(plain)).Got;
            Assert.Equal("loaded in docker", baseline.GetProperty("title").GetString());
        }
        finally { await plain.CloseAsync(); }
        Assert.False(ContainerExists(id0));
        Assert.DoesNotContain(vols0, VolumeExists);

        // treatment: the persona options reach the engine in the container
        var b = await Clearcote.LaunchAsync(new LaunchOptions { Fingerprint = "docker-e2e-seed", Platform = "windows", Timezone = "Asia/Tokyo", Quiet = true });
        var id1 = Clearcote.DockerContainerOf(b)!.Id;
        var vols1 = AnonymousVolumes(id1);
        try
        {
            var (page, got) = await ProbeAsync(b);
            Assert.Equal("Asia/Tokyo", got.GetProperty("tz").GetString());
            Assert.NotEqual(baseline.GetProperty("tz").GetString(), got.GetProperty("tz").GetString());
            Assert.Equal("Win32", got.GetProperty("platform").GetString());
            Assert.NotEqual("Win32", baseline.GetProperty("platform").GetString());
            Assert.Contains("Windows NT", got.GetProperty("ua").GetString());
            Assert.Null(page.ViewportSize);
            await page.GotoAsync("https://example.com/", new() { WaitUntil = WaitUntilState.DOMContentLoaded, Timeout = 60_000 });
            Assert.Equal("Example Domain", await page.TitleAsync());
        }
        finally { await b.CloseAsync(); }
        Assert.False(ContainerExists(id1));
        Assert.NotEmpty(vols1);
        Assert.DoesNotContain(vols1, VolumeExists);
    }
}
