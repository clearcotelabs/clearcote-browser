using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LaunchAsync on macOS runs the Clearcote Docker image (there is no native macOS build).
///
/// The host platform is mocked (Native.OsTagOverride) and the docker CLI is replaced by a recorder that
/// answers like Docker would; the "container's" CDP endpoint is a real local Chromium when one is found
/// (CLEARCOTE_TEST_BINARY, else Playwright's), so the SDK's connect, close and the Playwright objects it
/// returns are real. The real image is exercised end to end in DockerLaunchLiveTests. Mirrors
/// sdk/node/test/docker-launch.test.ts and sdk/python/tests/test_docker_launch.py.
public sealed class DockerLaunchTests : IDisposable
{
    private sealed class FakeDocker
    {
        public readonly List<(string[] Argv, Dictionary<string, string> Env)> Calls = new();
        public int CdpPort = 1;
        public bool Running = true;
        public string InfoError = "";
        public string RunError = "";
        public string Logs = "";

        public Task<DockerLaunch.CliResult> Run(IReadOnlyList<string> argv, IDictionary<string, string>? env, int _)
        {
            Calls.Add((argv.ToArray(), new Dictionary<string, string>(env ?? new Dictionary<string, string>())));
            DockerLaunch.CliResult r = argv[1] switch
            {
                "info" => InfoError.Length > 0 ? new(1, "", InfoError) : new(0, "29.1.3\n", ""),
                "run" => RunError.Length > 0 ? new(125, "", RunError) : new(0, "c0ffee1234\n", ""),
                "port" => new(0, $"127.0.0.1:{CdpPort}\n[::1]:{CdpPort}\n", ""),
                "inspect" => new(0, (Running ? "true" : "false") + "\n", ""),
                "logs" => new(0, Logs, ""),
                _ => new(0, "", ""),
            };
            return Task.FromResult(r);
        }

        public string[] Commands => Calls.Select(c => c.Argv[1]).ToArray();
        public (string[] Argv, Dictionary<string, string> Env) RunCall => Calls.First(c => c.Argv[1] == "run");
    }

    // Found before the constructor moves HOME (TempHome below), which is where Playwright's Chromium is
    // looked for: an instance field initializer runs before the constructor body (a static one may not).
    private readonly string? _chromium = LocalChromium.Find();

    private readonly Sandbox _sb = new();
    private readonly FakeDocker _docker = new();
    private readonly Func<string?> _realWhich = DockerLaunch.Which;
    private readonly Func<IReadOnlyList<string>, IDictionary<string, string>?, int, Task<DockerLaunch.CliResult>> _realRun = DockerLaunch.Run;

    public DockerLaunchTests()
    {
        foreach (var k in new[] { "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD", "CLEARCOTE_LICENSE_KEY", DockerLaunch.TestOnlyAssumeMacos })
            _sb.Env(k, null);
        _sb.TempHome();   // never this machine's own saved licence key
        _sb.Os("macos");
        DockerLaunch.Which = () => "docker";
        DockerLaunch.Run = _docker.Run;
    }

    public void Dispose()
    {
        DockerLaunch.Which = _realWhich;
        DockerLaunch.Run = _realRun;
        _sb.Dispose();
    }

    [Fact]
    public void Decides_by_platform_option_binary_and_environment()
    {
        bool On(string os, LaunchOptions? o = null, params (string K, string V)[] env)
        {
            _sb.Os(os);
            foreach (var (k, v) in env) Environment.SetEnvironmentVariable(k, v);
            try { return DockerLaunch.Requested(o); }
            finally { foreach (var (k, _) in env) Environment.SetEnvironmentVariable(k, null); }
        }
        Assert.True(On("macos"));
        Assert.False(On("linux"));
        Assert.False(On("windows"));
        Assert.False(On("macos", new LaunchOptions { Docker = false }));
        Assert.True(On("linux", new LaunchOptions { Docker = true }));
        Assert.False(On("macos", new LaunchOptions { ExecutablePath = "/opt/cc/chrome" }));
        Assert.False(On("macos", null, ("CLEARCOTE_DOCKER", "0")));
        Assert.True(On("linux", null, ("CLEARCOTE_DOCKER", "1")));
        Assert.False(On("macos", null, ("CLEARCOTE_BINARY", "/opt/cc/chrome")));
        Assert.True(On("linux", null, (DockerLaunch.TestOnlyAssumeMacos, "1")));
    }

    [Fact]
    public async Task Starts_the_image_connects_and_returns_a_working_Playwright_browser()
    {
        if (_chromium is null) return;   // no Chromium here: covered where one is (see CloudLaunchLiveTests)
        await using var local = await LocalChromium.StartAsync(_chromium);
        _docker.CdpPort = int.Parse(local.HttpUrl[(local.HttpUrl.LastIndexOf(':') + 1)..]);
        var browser = await Clearcote.LaunchAsync(new LaunchOptions
        {
            Fingerprint = "seed-1", Platform = "windows", Timezone = "Europe/Berlin", HardwareConcurrency = 8,
            CanvasNoise = false, Headless = true, Args = new[] { "--lang=de-DE" }, Quiet = true,
            Proxy = new ProxyOptions { Server = "http://proxy.example:8080", Username = "u", Password = "p w" },
            LicenseKey = "cc_lic_docker_test_key_1234",
            Identity = "ignored-like-a-local-launch",
        });
        try
        {
            Assert.True(browser.IsConnected);
            Assert.False(string.IsNullOrEmpty(browser.Version));
            Assert.Equal(new DockerContainer("c0ffee1234", $"teamflatearth/clearcote:sdk-{Clearcote.Version}", $"http://127.0.0.1:{_docker.CdpPort}"),
                Clearcote.DockerContainerOf(browser));
            var page = await browser.NewPageAsync();
            Assert.Null(page.ViewportSize);   // no emulated viewport over the container's real window
            await page.SetContentAsync("<title>in docker</title>");
            Assert.Equal("in docker", await page.TitleAsync());
        }
        finally
        {
            await browser.CloseAsync();
        }
        Assert.False(browser.IsConnected);
        Assert.True(local.IsAlive);   // CloseAsync disconnects; ending the browser is the container's stop
        Assert.Equal(new[] { "info", "run", "port", "stop", "rm" }, _docker.Commands);
        var (argv, env) = _docker.RunCall;
        Assert.Equal(new[] { "docker", "run", "-d", "--platform", "linux/amd64" }, argv[..5]);
        Assert.Equal("127.0.0.1::9222", argv[Array.IndexOf(argv, "-p") + 1]);   // loopback only
        Assert.Equal($"teamflatearth/clearcote:sdk-{Clearcote.Version}", argv[^1]);
        Assert.Equal("clearcote-cache:/opt/xdg-cache", argv[Array.IndexOf(argv, "-v") + 1]);
        var eNames = argv.Where((_, i) => i > 0 && argv[i - 1] == "-e").ToArray();
        Assert.All(eNames, a => Assert.DoesNotContain("=", a));   // values travel in the environment only
        Assert.DoesNotContain(argv, a => a.Contains("cc_lic_docker_test_key_1234") || a.Contains("p w") || a.Contains("p%20w"));
        Assert.Equal(new Dictionary<string, string>
        {
            ["CC_FINGERPRINT"] = "seed-1", ["CC_PLATFORM"] = "windows", ["CC_TIMEZONE"] = "Europe/Berlin",
            ["CC_HARDWARE_CONCURRENCY"] = "8", ["CC_CANVAS_NOISE"] = "0", ["CC_HEADLESS"] = "1",
            ["CC_EXTRA_ARGS"] = "--lang=de-DE", ["CC_PROXY"] = "http://u:p%20w@proxy.example:8080",
            ["CLEARCOTE_LICENSE_KEY"] = "cc_lic_docker_test_key_1234",
        }, env);
        Assert.Equal(env.Keys.OrderBy(k => k, StringComparer.Ordinal), eNames.OrderBy(k => k, StringComparer.Ordinal));
        Assert.Equal(new[] { new[] { "--time", "10", "c0ffee1234" }, new[] { "-f", "-v", "c0ffee1234" } },   // -v: the image's anonymous VOLUME too
            _docker.Calls.Where(c => c.Argv[1] is "stop" or "rm").Select(c => c.Argv[2..]).ToArray());
    }

    [Fact]
    public async Task Docker_not_installed_says_so_and_how_to_turn_this_off()
    {
        DockerLaunch.Which = () => null;
        var e = await Assert.ThrowsAsync<DockerUnavailableException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains("no native macOS build", e.Message);
        Assert.Contains("`docker` command was not found", e.Message);
        Assert.Contains("Install Docker Desktop", e.Message);
        Assert.Contains("Docker = false", e.Message);
        Assert.Contains("CLEARCOTE_DOCKER=0", e.Message);
        Assert.Empty(_docker.Calls);
    }

    [Fact]
    public async Task Docker_not_running_says_so()
    {
        _docker.InfoError = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n";
        var e = await Assert.ThrowsAsync<DockerUnavailableException>(() => Clearcote.LaunchAsync());
        Assert.Contains("Docker is not running", e.Message);
        Assert.Contains("Cannot connect to the Docker daemon", e.Message);
        Assert.Contains("Start Docker Desktop", e.Message);
        Assert.Equal(new[] { "info" }, _docker.Commands);
    }

    [Fact]
    public async Task An_option_the_image_cannot_take_is_refused_before_docker_runs()
    {
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Extensions = new[] { "/tmp/ext" } }));
        Assert.StartsWith("Extensions is not available when LaunchAsync runs Clearcote in Docker", e.Message);
        e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Geoip = true }));
        Assert.StartsWith("Geoip is not available", e.Message);
        e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Args = new[] { "--window-name=two words" } }));
        Assert.Contains("whitespace", e.Message);
        Assert.Empty(_docker.Calls);
    }

    [Fact]
    public async Task Docker_false_a_named_binary_or_CLEARCOTE_DOCKER_0_keeps_the_local_launch()
    {
        var dir = TestTemp.Create("cc-docker-nobin-");
        try
        {
            var missing = Path.Combine(dir, "no-such-chrome");
            foreach (var o in new[] { new LaunchOptions { Docker = false, ExecutablePath = missing }, new LaunchOptions { ExecutablePath = missing } })
            {
                var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(o));
                Assert.IsNotType<DockerUnavailableException>(e);
            }
            _sb.Env("CLEARCOTE_DOCKER", "0").Env("CLEARCOTE_BINARY", missing);
            var e2 = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync());
            Assert.IsNotType<DockerUnavailableException>(e2);
            Assert.Empty(_docker.Calls);   // the local path, untouched
        }
        finally { TestTemp.Remove(dir); }
    }

    [Fact]
    public async Task A_container_that_stops_early_reports_its_logs_and_is_removed()
    {
        _docker.Running = false;
        _docker.Logs = "[clearcote] ERROR: could not lease a run token (LicenseError: Invalid license key.).\n";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { LicenseKey = "cc_lic_bad", Quiet = true }));
        Assert.Contains("stopped before its browser came up", e.Message);
        Assert.Contains("could not lease a run token", e.Message);
        Assert.Equal(new[] { "stop", "rm" }, _docker.Commands[^2..]);
    }

    [Fact]
    public async Task A_failed_docker_run_names_the_image()
    {
        _sb.Env("CLEARCOTE_DOCKER_IMAGE", "example/missing:tag");
        _docker.RunError = "Unable to find image 'example/missing:tag' locally\nmanifest unknown\n";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains("`docker run example/missing:tag` failed: Unable to find image", e.Message);
        Assert.Equal(new[] { "info", "run" }, _docker.Commands);
    }

    [Fact]
    public async Task A_cloud_launch_refuses_the_docker_options()
    {
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Docker = true, ApiKey = "k" }));
        Assert.Equal("Docker is not available for cloud browsers", e.Message);
        Assert.Empty(_docker.Calls);
    }
}
