using System.Formats.Tar;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LaunchAsync on macOS runs the Clearcote Docker image (there is no native macOS build).
///
/// The host platform is mocked (Native.OsTagOverride) and the docker CLI is replaced by a recorder that
/// answers like Docker would; the "container's" CDP endpoint is a real local Chromium when one is found
/// (CLEARCOTE_TEST_BINARY, else Playwright's), or a stand-in that answers /json/version, so the SDK's connect,
/// close and the Playwright objects it returns are real. The real image is exercised end to end in
/// DockerLaunchLiveTests. Mirrors sdk/node/test/docker-launch.test.ts and sdk/python/tests/test_docker_launch.py.
public sealed class DockerLaunchTests : IDisposable
{
    private const int DeadPid = (1 << 22) + 12345;   // above any pid_max: never a live process
    private static readonly Dictionary<string, string> Protocol = new() { ["com.clearcotelabs.serve-protocol"] = "2" };

    private static string ServeState(string engine = "open", string? proxy = null, int idle = 30, bool secrets = false, string? proxyAuth = null) =>
        "[clearcote] serve-state " + JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["engine"] = engine, ["idle_exit"] = idle, ["protocol"] = 2, ["proxy"] = proxy, ["proxy_auth"] = proxyAuth, ["secrets_file"] = secrets,
        });

    private sealed class FakeDocker
    {
        public readonly List<(string[] Argv, Dictionary<string, string> Env, byte[]? Input)> Calls = new();
        public int CdpPort = 1;
        public bool Running = true;
        public string InfoError = "";
        public string CreateError = "";
        public string PullError = "";
        public Dictionary<string, string>? Labels = new(Protocol);   // null: not here until pulled
        public string Logs = "";
        public List<string> Marks = new() { ServeState() };          // what the entrypoint logged about what it applied
        public string Ps = "";                                       // `docker ps` rows: id \t owner-token
        public List<Dictionary<string, string>> Containers = new();  // or: containers {id, <label>: value}, rendered per --format

        public Task<DockerLaunch.CliResult> Run(IReadOnlyList<string> argv, IDictionary<string, string>? env, int _, byte[]? input)
        {
            Calls.Add((argv.ToArray(), new Dictionary<string, string>(env ?? new Dictionary<string, string>()), input));
            DockerLaunch.CliResult r;
            switch (argv[1])
            {
                case "info": r = InfoError.Length > 0 ? new(1, "", InfoError) : new(0, "29.1.3\n", ""); break;
                case "ps":
                    if (Containers.Count == 0) { r = new(0, Ps, ""); break; }
                    var names = System.Text.RegularExpressions.Regex.Matches(argv[argv.ToList().IndexOf("--format") + 1], "\\.Label \"([^\"]+)\"").Select(m => m.Groups[1].Value).ToList();
                    r = new(0, string.Concat(Containers.Select(c => string.Join('\t', new[] { c["id"] }.Concat(names.Select(n => c.GetValueOrDefault(n, "")))) + "\n")), "");
                    break;
                case "image":
                    r = Labels is null ? new(1, "", $"Error: No such image: {argv[^1]}") : new(0, (Labels.Count > 0 ? JsonSerializer.Serialize(Labels) : "null") + "\n", "");
                    break;
                case "pull":
                    if (PullError.Length > 0) { r = new(1, "", PullError); break; }
                    Labels ??= new(Protocol);
                    r = new(0, "", "");
                    break;
                case "create": r = CreateError.Length > 0 ? new(125, "", CreateError) : new(0, "c0ffee1234\n", ""); break;
                case "port": r = new(0, $"127.0.0.1:{CdpPort}\n[::1]:{CdpPort}\n", ""); break;
                case "inspect": r = Running ? new(0, "true\n", "") : new(1, "", "Error: No such object: c0ffee1234"); break;
                default: r = new(0, "", ""); break;   // cp, start, stop, rm
            }
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
        public Task<IReadOnlyList<string>> WaitMarksAsync(System.Text.RegularExpressions.Regex pattern, int waitMs = 10_000) => Task.FromResult<IReadOnlyList<string>>(_d.Marks.ToArray());
        public void Stop() { }
    }

    /// Answers /json/version like a browser: the container "came up". Its WebSocket is not there.
    private sealed class CdpStub : IDisposable
    {
        private readonly System.Net.HttpListener _listener = new();
        public int Port { get; }

        public CdpStub()
        {
            var probe = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
            probe.Start();
            Port = ((System.Net.IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            _listener.Prefixes.Add($"http://127.0.0.1:{Port}/");
            _listener.Start();
            _ = Task.Run(async () =>
            {
                try
                {
                    while (_listener.IsListening)
                    {
                        var ctx = await _listener.GetContextAsync();
                        var body = System.Text.Encoding.UTF8.GetBytes("{\"webSocketDebuggerUrl\":\"ws://127.0.0.1:1/devtools/browser/x\"}");
                        ctx.Response.ContentType = "application/json";
                        await ctx.Response.OutputStream.WriteAsync(body);
                        ctx.Response.Close();
                    }
                }
                catch { /* stopped */ }
            });
        }

        public void Dispose() { try { _listener.Stop(); } catch { } }
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
    private readonly Func<string, string?> _realProcText = DockerLaunch.ProcText;
    private readonly Func<bool> _realIsWindows = DockerLaunch.IsWindowsHost;
    private readonly Func<string?> _realBoot = DockerLaunch.BootIdProbe;
    private readonly Func<string?> _realPidNs = DockerLaunch.PidNamespaceProbe;
    private readonly Func<System.Diagnostics.ProcessStartInfo, string?> _realPs = DockerLaunch.RunPs;
    private readonly string _home;

    public DockerLaunchTests()
    {
        foreach (var k in new[] { "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_DOCKER_IMAGE", "CLEARCOTE_CLOUD", "CLEARCOTE_LICENSE_KEY",
                                  "CLEARCOTE_DOCKER_IDLE_EXIT", "CLEARCOTE_DOCKER_CACHE_VOLUME", DockerLaunch.TestOnlyAssumeMacos })
            _sb.Env(k, null);
        _home = _sb.TempHome();   // never this machine's own saved licence key or owner records
        _sb.Os("macos");
        DockerLaunch.ResetOwnerToken();
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
        DockerLaunch.ProcText = _realProcText;
        DockerLaunch.IsWindowsHost = _realIsWindows;
        DockerLaunch.BootIdProbe = _realBoot;
        DockerLaunch.PidNamespaceProbe = _realPidNs;
        DockerLaunch.RunPs = _realPs;
        DockerLaunch.ResetOwnerToken();
        _sb.Dispose();
    }

    private static string Image => $"teamflatearth/clearcote:sdk-{Clearcote.Version}";
    private static string[] After(string[] argv, string flag) => argv.Where((_, i) => i > 0 && argv[i - 1] == flag).ToArray();
    private string OwnersDir => Path.Combine(_home, ".clearcote", "docker-owners");

    private static LaunchOptions Keyed(LaunchOptions? o = null)
    {
        o ??= new LaunchOptions();
        o.LicenseKey = "cc_lic_docker_test_key_1234";
        o.Proxy = new ProxyOptions { Server = "http://proxy.example:8080", Username = "u", Password = "p w" };
        return o;
    }

    /// What an image from before sdk-0.40.0 can still log in to: a SOCKS5 proxy, on the licensed engine.
    private static LaunchOptions KeyedSocks(LaunchOptions? o = null)
    {
        o ??= new LaunchOptions();
        o.LicenseKey = "cc_lic_docker_test_key_1234";
        o.Proxy = new ProxyOptions { Server = "socks5://proxy.example:1080", Username = "u", Password = "pw-plain" };
        return o;
    }

    private string Record(string token, object fields, bool here = true)
    {
        Directory.CreateDirectory(OwnersDir);
        var path = Path.Combine(OwnersDir, token + ".json");
        var dict = here ? Here() : new Dictionary<string, object?>();
        foreach (var (k, v) in JsonSerializer.Deserialize<Dictionary<string, object?>>(JsonSerializer.Serialize(fields))!) dict[k] = v;
        dict["token"] = token;
        File.WriteAllText(path, JsonSerializer.Serialize(dict));
        return path;
    }

    /// What a record written by this process carries besides pid and start (host, boot id, PID namespace).
    private static Dictionary<string, object?> Here()
    {
        var (host, boot, pidns) = DockerLaunch.OwnerHere();
        return new() { ["host"] = host, ["boot"] = boot, ["pidns"] = pidns };
    }

    private static JsonElement Json(Dictionary<string, object?> d) => JsonDocument.Parse(JsonSerializer.Serialize(d)).RootElement.Clone();

    private static Dictionary<string, object?> With(Dictionary<string, object?> d, params (string Key, object? Value)[] more)
    {
        var r = new Dictionary<string, object?>(d);
        foreach (var (k, v) in more) r[k] = v;
        return r;
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
        _docker.Marks = new() { ServeState("licensed", "http://proxy.example:8080", 30, true, "engine") };
        await using var local = await LocalChromium.StartAsync(_chromium);
        _docker.CdpPort = int.Parse(local.HttpUrl[(local.HttpUrl.LastIndexOf(':') + 1)..]);
        var browser = await Clearcote.LaunchAsync(Keyed(new LaunchOptions
        {
            Fingerprint = "seed-1", Platform = "windows", Timezone = "Europe/Berlin", HardwareConcurrency = 8,
            CanvasNoise = false, Headless = true, Args = new[] { "--lang=de-DE" }, Quiet = true,
            Identity = "ignored-like-a-local-launch",
        }));
        try
        {
            Assert.True(browser.IsConnected);
            Assert.False(string.IsNullOrEmpty(browser.Version));
            Assert.Equal(new DockerContainer("c0ffee1234", Image, $"http://127.0.0.1:{_docker.CdpPort}", 2), Clearcote.DockerContainerOf(browser));
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
        Assert.Equal(new[] { "info", "ps", "image", "create", "cp", "start", "port", "stop", "rm" }, _docker.Commands);
        var (argv, env, _) = _docker.Call("create");
        Assert.Equal(new[] { "docker", "create", "--rm", "--platform", "linux/amd64", "--shm-size" }, argv[..6]);
        Assert.Equal("127.0.0.1::9222", argv[Array.IndexOf(argv, "-p") + 1]);   // loopback only
        Assert.Equal(Image, argv[^1]);
        Assert.Equal("clearcote-cache:/opt/xdg-cache", argv[Array.IndexOf(argv, "-v") + 1]);
        // owned through this process's token: the next launch removes it once this process is gone
        var lab = After(argv, "--label");
        Assert.Equal(new[] { "com.clearcotelabs.sdk-launch=1", $"com.clearcotelabs.owner-host={System.Net.Dns.GetHostName()}" }, lab[..2]);
        Assert.Matches("^com\\.clearcotelabs\\.owner-token=[0-9a-f]{32}$", lab[2]);
        using (var rec = JsonDocument.Parse(File.ReadAllText(Path.Combine(OwnersDir, lab[2].Split('=')[1] + ".json"))))
        {
            Assert.Equal(Environment.ProcessId, rec.RootElement.GetProperty("pid").GetInt32());
            Assert.Equal("dotnet", rec.RootElement.GetProperty("sdk").GetString());
        }
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

    // ── what an image understands, and what it applied ──────────────────────────────────────────────

    [Fact]
    public async Task An_image_without_the_protocol_label_gets_plain_variables()
    {
        // An image older than sdk-0.40.0 ignores CC_SECRETS_FILE: handing it the key and proxy as a file would start
        // it on the open engine with no proxy. It gets the variables it understands instead.
        using var stub = new CdpStub();
        _docker.CdpPort = stub.Port;
        _docker.Labels = new();
        _docker.Marks = new() { "[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)", "[clearcote] proxy: socks5://proxy.example:1080" };
        var (container, exe) = await DockerLaunch.StartContainerAsync(KeyedSocks(new LaunchOptions { Quiet = true }));
        Assert.Equal(0, container.ServeProtocol);
        var (argv, env, _) = _docker.Call("create");
        Assert.Equal("cc_lic_docker_test_key_1234", env["CLEARCOTE_LICENSE_KEY"]);
        Assert.Equal("socks5://u:pw-plain@proxy.example:1080", env["CC_PROXY"]);
        Assert.False(env.ContainsKey("CC_SECRETS_FILE"));
        Assert.False(env.ContainsKey("CC_IDLE_EXIT_SECONDS"));
        Assert.DoesNotContain("cp", _docker.Commands);
        Assert.DoesNotContain(argv, a => a.Contains("cc_lic_docker_test_key_1234") || a.Contains("pw-plain"));   // still not argv
        await DockerLaunch.RemoveAsync(exe, container.Id);
    }

    public static IEnumerable<object[]> NotApplied() => new[]
    {
        new object[] { 2, new[] { ServeState("open", "http://proxy.example:8080", 30, true, "relay") }, "it runs the open engine" },
        new object[] { 2, new[] { ServeState("licensed", null, 30, true) }, "did not apply it" },
        new object[] { 2, Array.Empty<string>(), "did not report what it applied" },
        // the proxy needs a password and the container did not say it can log in to it: every request would fail
        new object[] { 2, new[] { ServeState("licensed", "http://proxy.example:8080", 30, true) }, "did not say it can log in" },
        new object[] { 0, new[] { "[clearcote] engine: /opt/xdg-cache/clearcote/v0.1.0-pre.23/browser/chrome (free)", "[clearcote] proxy: socks5://proxy.example:1080" }, "it runs the open engine" },
        new object[] { 0, new[] { "[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)" }, "did not apply it" },
        new object[] { 0, new[] { "[clearcote] engine: /opt/xdg-cache/clearcote/pro-154.0.8037.57-r30/browser/chrome (licensed)", "[clearcote] proxy: socks5://other.example:3128" }, "a different proxy" },
    };

    [Theory]
    [MemberData(nameof(NotApplied))]
    public async Task A_container_that_did_not_apply_the_key_or_proxy_is_refused_and_removed(int protocol, string[] marks, string problem)
    {
        using var stub = new CdpStub();
        _docker.CdpPort = stub.Port;
        _docker.Labels = protocol > 0 ? new(Protocol) : new();
        _docker.Marks = marks.ToList();
        var opts = protocol > 0 ? Keyed(new LaunchOptions { Quiet = true }) : KeyedSocks(new LaunchOptions { Quiet = true });
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => DockerLaunch.StartContainerAsync(opts));
        Assert.Contains("did not apply what LaunchAsync asked for", e.Message);
        Assert.Contains(problem, e.Message);
        Assert.Contains("stopped and removed", e.Message);
        Assert.DoesNotContain("cc_lic_docker_test_key_1234", e.Message);
        Assert.Equal(new[] { "stop", "rm" }, _docker.Commands[^2..]);
    }

    [Fact]
    public async Task A_missing_image_is_pulled_before_its_protocol_is_read()
    {
        using var stub = new CdpStub();
        _docker.CdpPort = stub.Port;
        _docker.Labels = null;
        var (container, exe) = await DockerLaunch.StartContainerAsync(new LaunchOptions { Quiet = true });
        Assert.Equal(2, container.ServeProtocol);
        Assert.Equal(new[] { "info", "ps", "image", "pull", "image", "create" }, _docker.Commands[..6]);
        Assert.Equal(new[] { "--platform", "linux/amd64", Image }, _docker.Call("pull").Argv[2..]);
        await DockerLaunch.RemoveAsync(exe, container.Id);
    }

    [Fact]
    public async Task An_image_not_published_yet_says_what_to_do_in_Docker_29s_wording()
    {
        _docker.Labels = null;
        _docker.PullError = $"Error response from daemon: failed to resolve reference \"docker.io/{Image}\": docker.io/{Image}: not found\n";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains($"the Clearcote image {Image} is not available", e.Message);
        Assert.Contains("published a few minutes after its SDK release", e.Message);
        Assert.Contains("older than sdk-0.40.0 still works, but", e.Message);   // the trade-off, said
        Assert.DoesNotContain("create", _docker.Commands);
    }

    public static IEnumerable<object[]> OldImageProxies() => new[]
    {
        new object[] { "http://proxy.example:8080", "u", "p w", true },
        new object[] { "https://u:p%20w@proxy.example:8443", "", "", true },
        new object[] { "socks5://proxy.example:1080", "u", "p w", false },
        // its licensed engine takes a SOCKS5 password, but the image hands it over as written in the URL: escaped
        new object[] { "socks5://proxy.example:1080", "u", "p@ss w", true },
    };

    [Theory]
    [MemberData(nameof(OldImageProxies))]
    public async Task An_old_image_is_refused_a_proxy_it_cannot_log_in_to_before_it_starts(string server, string user, string password, bool licensed)
    {
        // An image from before sdk-0.40.0 drops an http(s) proxy's password (Chrome is challenged and nothing answers:
        // every request fails), and only its licensed engine takes a SOCKS5 one. Said up front instead.
        _docker.Labels = new();
        var o = new LaunchOptions
        {
            Quiet = true, LicenseKey = licensed ? "cc_lic_docker_test_key_1234" : null,
            Proxy = new ProxyOptions { Server = server, Username = user.Length > 0 ? user : null, Password = password.Length > 0 ? password : null },
        };
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => DockerLaunch.StartContainerAsync(o));
        Assert.Matches("predates sdk-0.40.0 and cannot use .* needs a password", e.Message);
        Assert.DoesNotContain("p w", e.Message);
        Assert.DoesNotContain("p%20w", e.Message);
        Assert.DoesNotContain("create", _docker.Commands);
    }

    [Fact]
    public async Task A_proxy_password_is_accepted_when_the_container_logs_in_to_it()
    {
        using var stub = new CdpStub();
        _docker.CdpPort = stub.Port;
        foreach (var how in new[] { "engine", "relay" })
        {
            _docker.Marks = new() { ServeState("open", "http://proxy.example:8080", 30, true, how) };
            var (container, exe) = await DockerLaunch.StartContainerAsync(new LaunchOptions
            {
                Quiet = true, Proxy = new ProxyOptions { Server = "http://proxy.example:8080", Username = "u", Password = "p w" },
            });
            await DockerLaunch.RemoveAsync(exe, container.Id);
        }
    }

    [Fact]
    public async Task An_unreachable_registry_is_not_called_an_unpublished_image()
    {
        // Docker 29 opens a registry it cannot reach with the same "failed to resolve reference" as a tag that is not
        // there: offline users of the default tag were told it "is not published yet".
        _docker.Labels = null;
        _docker.PullError = $"Error response from daemon: failed to resolve reference \"docker.io/{Image}\": failed to do request: Head \"https://registry-1.docker.io/v2/teamflatearth/clearcote/manifests/sdk-{Clearcote.Version}\": dial tcp: lookup registry-1.docker.io on 192.168.65.7:53: no such host\n";
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
        Assert.Contains("registry did not answer", e.Message);
        Assert.Contains("network", e.Message);
        Assert.Contains("no such host", e.Message);
        Assert.DoesNotContain("not available", e.Message);
        Assert.DoesNotContain("published a few minutes", e.Message);
    }

    // ── who owns a container ────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task Sweeps_only_containers_whose_owner_is_certainly_gone()
    {
        static string T(char c) => new(c, 32);
        var gone = Record(T('a'), new { pid = DeadPid });                                                   // owner gone: swept
        Record(T('b'), new { pid = Environment.ProcessId, start = DockerLaunch.ProcessStart(Environment.ProcessId) });   // alive: kept
        var elsewhere = Record(T('d'), new { pid = DeadPid, boot = "another-boot-entirely" });              // another boot: kept
        // T('e'): no record here (another machine, a container given the Docker socket, WSL2): kept
        _docker.Ps = string.Concat(new[] { 'a', 'b', 'd', 'e' }.Select(c => $"{new string(c, 6)}\t{T(c)}\n")) + "fff111\t\n";   // no token: kept
        var swept = await DockerLaunch.SweepStaleAsync("docker");
        Assert.Equal(new[] { "aaaaaa" }, swept);
        Assert.False(File.Exists(gone));   // its record goes too
        Assert.True(File.Exists(elsewhere));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    [InlineData(-4242)]
    public async Task A_record_whose_pid_is_not_a_positive_number_is_left_alone(int pid)
    {
        // pid 0 / negative pids name a process group or nothing at all: "not alive" proves nothing about the owner
        Record(new string('a', 32), new { pid });
        _docker.Ps = $"aaaaaa\t{new string('a', 32)}\n";
        Assert.Null(DockerLaunch.OwnerAlive(Json(With(Here(), ("pid", pid)))));
        Assert.Empty(await DockerLaunch.SweepStaleAsync("docker"));
        Assert.DoesNotContain("stop", _docker.Commands);
    }

    public static IEnumerable<object[]> Unverifiable() => new[]
    {
        new object[] { "written on macOS/Windows into a shared home", "", "", "" },
        new object[] { "no PID namespace", "linux-boot-1", "", "" },
        new object[] { "no boot id", "", "pid:[4026531836]", "" },
        new object[] { "another host", "linux-boot-1", "pid:[4026531836]", "another-machine" },
    };

    [Theory]
    [MemberData(nameof(Unverifiable))]
    public async Task A_record_that_cannot_be_fully_verified_here_is_left_alone(string label, string boot, string pidns, string host)
    {
        // This process is on Linux (boot id + PID namespace). A record that lacks either, or names another host, was
        // judged by its pid alone, and its live container removed when that pid was free here.
        Assert.NotEmpty(label);
        DockerLaunch.BootIdProbe = () => "linux-boot-1";
        DockerLaunch.PidNamespaceProbe = () => "pid:[4026531836]";
        var rec = new Dictionary<string, object?>
        {
            ["pid"] = DeadPid, ["start"] = "ps-utc:Tue Oct  6 08:00:00 2026", ["host"] = host.Length > 0 ? host : System.Net.Dns.GetHostName(),
        };
        if (boot.Length > 0) rec["boot"] = boot;
        if (pidns.Length > 0) rec["pidns"] = pidns;
        Record(new string('e', 32), rec, here: false);
        _docker.Ps = $"eeeeee\t{new string('e', 32)}\n";
        Assert.Null(DockerLaunch.OwnerAlive(Json(rec)));
        Assert.Empty(await DockerLaunch.SweepStaleAsync("docker"));
        Assert.DoesNotContain("stop", _docker.Commands);
    }

    [Fact]
    public async Task The_start_marker_is_the_same_in_every_time_zone_and_locale()
    {
        // macOS has no /proc: the start time comes from `ps -o lstart=`, which prints local time in the locale's words.
        // An owner launched with TZ=UTC and a sweeper with TZ=Asia/Tokyo read different strings for one process, and the
        // sweeper removed the live container as a reused pid (so did one program changing TZ between launches).
        DockerLaunch.ProcText = path => path.StartsWith("/proc/", StringComparison.Ordinal) ? null : _realProcText(path);   // no /proc here
        DockerLaunch.IsWindowsHost = () => false;
        DockerLaunch.RunPs = psi =>
        {
            string? Seen(string k) => psi.Environment.TryGetValue(k, out var v) ? v : Environment.GetEnvironmentVariable(k);   // what the ps child sees
            return $"started 2026-10-06 08:00:00 UTC, printed for TZ={Seen("TZ")} LC_ALL={Seen("LC_ALL")}\n";
        };
        var pid = Environment.ProcessId;   // alive
        _sb.Env("TZ", "UTC").Env("LC_ALL", "en_US.UTF-8");
        var rec = With(Here(), ("pid", pid), ("start", DockerLaunch.ProcessStart(pid)));
        Assert.NotNull(rec["start"]);
        _sb.Env("TZ", "Asia/Tokyo").Env("LC_ALL", "de_DE.UTF-8");
        Assert.Equal(rec["start"], DockerLaunch.ProcessStart(pid));
        Assert.True(DockerLaunch.OwnerAlive(Json(rec)));
        Record(new string('f', 32), rec, here: false);
        _docker.Ps = $"ffffff\t{new string('f', 32)}\n";
        Assert.Empty(await DockerLaunch.SweepStaleAsync("docker"));
    }

    [Fact]
    public async Task A_process_in_another_namespace_with_this_host_name_is_left_alone()
    {
        // The reviewer's case: a process in a container started with --network host and the Docker socket has this
        // machine's host name, and a pid that may not exist here. Host name + pid called it dead and removed its live
        // container. Its owner record is in its own filesystem, not here: never touched.
        _docker.Containers.Add(new()
        {
            ["id"] = "live99", ["com.clearcotelabs.sdk-launch"] = "1", ["com.clearcotelabs.owner-host"] = System.Net.Dns.GetHostName(),
            ["com.clearcotelabs.owner-pid"] = DeadPid.ToString(System.Globalization.CultureInfo.InvariantCulture),
            ["com.clearcotelabs.owner-token"] = new string('9', 32),
        });
        Assert.Empty(await DockerLaunch.SweepStaleAsync("docker"));
        Assert.DoesNotContain("stop", _docker.Commands);
        Assert.DoesNotContain("rm", _docker.Commands);
    }

    [Fact]
    public void Owner_record()
    {
        var token = DockerLaunch.OwnerToken();
        Assert.Equal(token, DockerLaunch.OwnerToken());   // one per process
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(OwnersDir, token + ".json")));
        var rec = doc.RootElement.Clone();
        Assert.Equal(Environment.ProcessId, rec.GetProperty("pid").GetInt32());
        var (host, boot, pidns) = DockerLaunch.OwnerHere();
        Assert.Equal(host, rec.GetProperty("host").GetString());
        Assert.Equal(boot, rec.GetProperty("boot").ValueKind == JsonValueKind.String ? rec.GetProperty("boot").GetString() : null);
        Assert.Equal(pidns, rec.GetProperty("pidns").ValueKind == JsonValueKind.String ? rec.GetProperty("pidns").GetString() : null);
        Assert.True(DockerLaunch.OwnerAlive(rec));
        Assert.False(DockerLaunch.OwnerAlive(Json(With(Here(), ("pid", DeadPid)))));
        if (rec.GetProperty("start").ValueKind == JsonValueKind.String)
        {
            var kind = rec.GetProperty("start").GetString()!.Split(':')[0];
            Assert.False(DockerLaunch.OwnerAlive(Json(With(Here(), ("pid", Environment.ProcessId), ("start", kind + ":1")))));   // the same pid, another process
        }
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
        using var stub = new CdpStub();
        _docker.CdpPort = stub.Port;
        DockerLaunch.ConnectOverride = (_, _) => throw new PlaywrightException("connect failed");
        await Assert.ThrowsAsync<PlaywrightException>(() => Clearcote.LaunchAsync(new LaunchOptions { Quiet = true }));
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
