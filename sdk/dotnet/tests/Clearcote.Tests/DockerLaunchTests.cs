using System.Formats.Tar;
using System.Text.Json;
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
    private const int DeadPid = (1 << 22) + 12345;   // above any pid_max: never a live process

    private sealed class FakeDocker
    {
        public readonly List<(string[] Argv, Dictionary<string, string> Env, byte[]? Input)> Calls = new();
        public int CdpPort = 1;
        public bool Running = true;
        public string InfoError = "";
        public string CreateError = "";
        public string Logs = "";
        public string Ps = "";   // `docker ps` rows: id \t owner-host \t owner-pid

        public Task<DockerLaunch.CliResult> Run(IReadOnlyList<string> argv, IDictionary<string, string>? env, int _, byte[]? input)
        {
            Calls.Add((argv.ToArray(), new Dictionary<string, string>(env ?? new Dictionary<string, string>()), input));
            DockerLaunch.CliResult r = argv[1] switch
            {
                "info" => InfoError.Length > 0 ? new(1, "", InfoError) : new(0, "29.1.3\n", ""),
                "ps" => new(0, Ps, ""),
                "create" => CreateError.Length > 0 ? new(125, "", CreateError) : new(0, "c0ffee1234\n", ""),
                "port" => new(0, $"127.0.0.1:{CdpPort}\n[::1]:{CdpPort}\n", ""),
                "inspect" => Running ? new(0, "true\n", "") : new(1, "", "Error: No such object: c0ffee1234"),
                _ => new(0, "", ""),   // cp, start, stop, rm
            };
            return Task.FromResult(r);
        }

        public string[] Commands => Calls.Select(c => c.Argv[1]).ToArray();
        public (string[] Argv, Dictionary<string, string> Env, byte[]? Input) Call(string cmd) => Calls.First(c => c.Argv[1] == cmd);
        public string[][] StopsAndRms => Calls.Where(c => c.Argv[1] is "stop" or "rm").Select(c => c.Argv[2..]).ToArray();
    }

    private sealed class FakeLogs : DockerLaunch.ILogTail
    {
        private readonly FakeDocker _d;
        public FakeLogs(FakeDocker d) => _d = d;
        public Task<string> TextAsync(int waitMs = 3000) => Task.FromResult(_d.Logs);
        public void Stop() { }
    }

    // Found before the constructor moves HOME (TempHome below), which is where Playwright's Chromium is
    // looked for: an instance field initializer runs before the constructor body (a static one may not).
    private readonly string? _chromium = LocalChromium.Find();

    private readonly Sandbox _sb = new();
    private readonly FakeDocker _docker = new();
    private readonly Func<string?> _realWhich = DockerLaunch.Which;
    private readonly Func<IReadOnlyList<string>, IDictionary<string, string>?, int, byte[]?, Task<DockerLaunch.CliResult>> _realRun = DockerLaunch.Run;
    private readonly Func<string, string, DockerLaunch.ILogTail> _realLogs = DockerLaunch.FollowLogs;
    private readonly Func<string, BrowserTypeConnectOverCDPOptions, Task<IBrowser>>? _realConnect = DockerLaunch.ConnectOverride;

    public DockerLaunchTests()
    {
        foreach (var k in new[] { "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD", "CLEARCOTE_LICENSE_KEY",
                                  "CLEARCOTE_DOCKER_IDLE_EXIT", "CLEARCOTE_DOCKER_CACHE_VOLUME", DockerLaunch.TestOnlyAssumeMacos })
            _sb.Env(k, null);
        _sb.TempHome();   // never this machine's own saved licence key
        _sb.Os("macos");
        DockerLaunch.Which = () => "docker";
        DockerLaunch.Run = _docker.Run;
        DockerLaunch.FollowLogs = (_, _) => new FakeLogs(_docker);
    }

    public void Dispose()
    {
        DockerLaunch.Which = _realWhich;
        DockerLaunch.Run = _realRun;
        DockerLaunch.FollowLogs = _realLogs;
        DockerLaunch.ConnectOverride = _realConnect;
        _sb.Dispose();
    }

    private static string Image => $"teamflatearth/clearcote:sdk-{Clearcote.Version}";
    private static string[] After(string[] argv, string flag) => argv.Where((_, i) => i > 0 && argv[i - 1] == flag).ToArray();

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
            Assert.Equal(new DockerContainer("c0ffee1234", Image, $"http://127.0.0.1:{_docker.CdpPort}"), Clearcote.DockerContainerOf(browser));
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
        Assert.Equal(new[] { "info", "ps", "create", "cp", "start", "port", "stop", "rm" }, _docker.Commands);
        var (argv, env, _) = _docker.Call("create");
        // --rm: a stopped container (and its anonymous engine volume) goes away by itself
        Assert.Equal(new[] { "docker", "create", "--rm", "--platform", "linux/amd64", "--shm-size" }, argv[..6]);
        Assert.Equal("127.0.0.1::9222", argv[Array.IndexOf(argv, "-p") + 1]);   // loopback only
        Assert.Equal(Image, argv[^1]);
        Assert.Equal("clearcote-cache:/opt/xdg-cache", argv[Array.IndexOf(argv, "-v") + 1]);
        // owned: the next launch on this machine removes it if this process is gone
        Assert.Equal(new[] { "com.clearcotelabs.sdk-launch=1", $"com.clearcotelabs.owner-host={System.Net.Dns.GetHostName()}",
                             $"com.clearcotelabs.owner-pid={Environment.ProcessId}" }, After(argv, "--label"));
        var eNames = After(argv, "-e");
        Assert.All(eNames, a => Assert.DoesNotContain("=", a));   // values travel in the environment only
        Assert.DoesNotContain(argv, a => a.Contains("cc_lic_docker_test_key_1234") || a.Contains("p w") || a.Contains("p%20w"));
        Assert.Equal(new Dictionary<string, string>
        {
            ["CC_FINGERPRINT"] = "seed-1", ["CC_PLATFORM"] = "windows", ["CC_TIMEZONE"] = "Europe/Berlin",
            ["CC_HARDWARE_CONCURRENCY"] = "8", ["CC_CANVAS_NOISE"] = "0", ["CC_HEADLESS"] = "1",
            ["CC_EXTRA_ARGS"] = "--lang=de-DE", ["CC_IDLE_EXIT_SECONDS"] = "30", ["CC_SECRETS_FILE"] = "/tmp/clearcote-secrets.json",
        }, env);
        Assert.Equal(env.Keys.OrderBy(k => k, StringComparer.Ordinal), eNames.OrderBy(k => k, StringComparer.Ordinal));
        // the licence key and the proxy URL go in as a file only the image's user can read
        var cp = _docker.Call("cp");
        Assert.Equal(new[] { "-", "c0ffee1234:/tmp" }, cp.Argv[2..]);
        Assert.Empty(cp.Env);
        using var reader = new TarReader(new MemoryStream(cp.Input!));
        var entry = reader.GetNextEntry()!;
        Assert.Equal(("clearcote-secrets.json", 10001, 10001, UnixFileMode.UserRead | UnixFileMode.UserWrite), (entry.Name, entry.Uid, entry.Gid, entry.Mode));
        var secrets = JsonSerializer.Deserialize<Dictionary<string, string>>(new StreamReader(entry.DataStream!).ReadToEnd());
        Assert.Equal(new Dictionary<string, string>
        {
            ["CLEARCOTE_LICENSE_KEY"] = "cc_lic_docker_test_key_1234", ["CC_PROXY"] = "http://u:p%20w@proxy.example:8080",
        }, secrets);
        Assert.Equal(new[] { new[] { "--time", "10", "c0ffee1234" }, new[] { "-f", "-v", "c0ffee1234" } }, _docker.StopsAndRms);   // -v: the anonymous VOLUME too
    }

    [Fact]
    public async Task Sweeps_stale_containers_of_dead_owners_at_the_next_launch()
    {
        var here = System.Net.Dns.GetHostName();
        _docker.Ps = $"aaa111\t{here}\t{DeadPid}\n"            // this machine, owner gone: swept
                   + $"bbb222\t{here}\t{Environment.ProcessId}\n"   // this machine, owner alive: kept
                   + $"ccc333\tsome-other-host\t{DeadPid}\n"       // another machine on the same daemon: kept
                   + "ddd444\t\t\n";                               // no owner labels: kept
        _docker.Running = false;   // this launch then fails; only the sweep matters here
        await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Equal(new[] { "-a", "--filter", "label=com.clearcotelabs.sdk-launch=1" }, _docker.Call("ps").Argv[2..5]);
        Assert.Equal(new[] { new[] { "--time", "10", "aaa111" }, new[] { "-f", "-v", "aaa111" } }, _docker.StopsAndRms[..2]);
        Assert.True(Array.IndexOf(_docker.Commands, "ps") < Array.IndexOf(_docker.Commands, "create"));
    }

    [Fact]
    public void PidAlive()
    {
        Assert.True(DockerLaunch.PidAlive(Environment.ProcessId));
        Assert.False(DockerLaunch.PidAlive(DeadPid));
    }

    [Fact]
    public async Task The_idle_exit_and_the_cache_volume_can_be_tuned()
    {
        _sb.Env("CLEARCOTE_DOCKER_IDLE_EXIT", "7").Env("CLEARCOTE_DOCKER_CACHE_VOLUME", "my-cc-cache");
        _docker.Running = false;
        await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { LicenseKey = "cc_lic_x", Quiet = true }));
        var (argv, env, _) = _docker.Call("create");
        Assert.Equal("7", env["CC_IDLE_EXIT_SECONDS"]);
        Assert.Equal("my-cc-cache:/opt/xdg-cache", argv[Array.IndexOf(argv, "-v") + 1]);
        _sb.Env("CLEARCOTE_DOCKER_IDLE_EXIT", "soon");
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains("CLEARCOTE_DOCKER_IDLE_EXIT", e.Message);
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
    public async Task An_image_not_published_yet_says_what_to_do()
    {
        _docker.CreateError = $"Unable to find image '{Image}' locally\nError response from daemon: manifest for {Image} not found: manifest unknown: manifest unknown\n";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains($"the Clearcote image {Image} is not available", e.Message);
        Assert.Contains("published a few minutes after each SDK release", e.Message);
        Assert.Contains("CLEARCOTE_DOCKER_IMAGE=teamflatearth/clearcote:latest", e.Message);
        Assert.Equal(new[] { "info", "ps", "create" }, _docker.Commands);
    }

    [Fact]
    public async Task A_failed_create_names_the_image()
    {
        _sb.Env("CLEARCOTE_DOCKER_IMAGE", "example/other:tag");
        _docker.CreateError = "docker: Error response from daemon: Conflict. The container name is already in use.\n";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains("`docker create example/other:tag` failed: docker: Error response", e.Message);
    }

    [Fact]
    public async Task A_failed_connect_after_the_container_started_removes_it()
    {
        // The container is up (its CDP answers), then the connect fails.
        using var listener = new System.Net.HttpListener();
        var port = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        port.Start();
        _docker.CdpPort = ((System.Net.IPEndPoint)port.LocalEndpoint).Port;
        port.Stop();
        listener.Prefixes.Add($"http://127.0.0.1:{_docker.CdpPort}/");
        listener.Start();
        var serve = Task.Run(async () =>
        {
            try
            {
                while (listener.IsListening)
                {
                    var ctx = await listener.GetContextAsync();
                    var body = System.Text.Encoding.UTF8.GetBytes("{\"webSocketDebuggerUrl\":\"ws://127.0.0.1:1/devtools/browser/x\"}");
                    ctx.Response.ContentType = "application/json";
                    await ctx.Response.OutputStream.WriteAsync(body);
                    ctx.Response.Close();
                }
            }
            catch { /* stopped */ }
        });
        DockerLaunch.ConnectOverride = (_, _) => throw new PlaywrightException("connect failed");
        await Assert.ThrowsAsync<PlaywrightException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        listener.Stop();
        Assert.Equal(new[] { "stop", "rm" }, _docker.Commands[^2..]);
        Assert.Equal(new[] { new[] { "--time", "10", "c0ffee1234" }, new[] { "-f", "-v", "c0ffee1234" } }, _docker.StopsAndRms);
    }

    [Fact]
    public async Task A_cloud_launch_refuses_the_docker_options()
    {
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Docker = true, ApiKey = "k" }));
        Assert.Equal("Docker is not available for cloud browsers", e.Message);
        Assert.Empty(_docker.Calls);
    }
}
