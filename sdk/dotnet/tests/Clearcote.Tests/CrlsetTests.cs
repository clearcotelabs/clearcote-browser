using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// The opt-in CRLSet (<see cref="Crlset"/>): the file, the cache, the fetch, the profile seed, the readiness probe
/// and the persistent launch that uses them. Mirrors the Python suite's tests/test_crlset.py.
/// </summary>
/// <remarks>Hermetic: no network (the SDK's HTTP test seam answers), the cache is a temp directory
/// (CLEARCOTE_CRLSET_DIR), and the launch test runs against a stand-in engine that writes down its command line and
/// exits (POSIX-only, as in WidevineTests).</remarks>
public class CrlsetTests : IDisposable
{
    private readonly Sandbox _sb = new();
    private readonly string _tmp = TestTemp.Create("cc-crlset-");
    private readonly string _cache;
    private readonly TextWriter _stderr = Console.Error;
    private readonly StringWriter _err = new();

    public CrlsetTests()
    {
        _sb.TempHome();
        _cache = Path.Combine(_tmp, "cache");
        _sb.Env("CLEARCOTE_CRLSET_DIR", _cache);
        foreach (var name in new[] { "CLEARCOTE_CRLSET", "CLEARCOTE_CRLSET_FILE", "CLEARCOTE_CRLSET_TIMEOUT", "CLEARCOTE_CRLSET_URL",
                     "CLEARCOTE_CRLSET_FETCH_TIMEOUT", "CLEARCOTE_NO_WARN", "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK" })
            _sb.Env(name, null);
        _sb.Http(new FakeHandler(r => throw new InvalidOperationException($"unexpected request {r.RequestUri}")));
        Crlset.ForgetWarnings();
        // A real background refresh would outlive the test and its sandbox (it reads CLEARCOTE_CRLSET_DIR late).
        Crlset.RefreshOverride = () => { };
        Console.SetError(_err);
    }

    public void Dispose()
    {
        Console.SetError(_stderr);
        Crlset.ForgetWarnings();
        Crlset.RefreshOverride = null;
        _sb.Dispose();
        TestTemp.Remove(_tmp);
    }

    internal static byte[] CrlsetBytes(long sequence, string body = "")
    {
        var header = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { Version = 0, ContentType = "CRLSet", Sequence = sequence, BlockedSPKIs = Array.Empty<string>() }));
        return new[] { (byte)(header.Length & 0xff), (byte)(header.Length >> 8) }.Concat(header).Concat(Encoding.ASCII.GetBytes(body)).ToArray();
    }

    private static byte[] Framed(string json)
    {
        var b = Encoding.UTF8.GetBytes(json);
        return new[] { (byte)(b.Length & 0xff), (byte)(b.Length >> 8) }.Concat(b).ToArray();
    }

    private string Write(string relative, byte[] data)
    {
        var path = Path.Combine(_tmp, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllBytes(path, data);
        return path;
    }

    private string P(string relative) => Path.Combine(_tmp, relative);

    // ── the file and the cache ───────────────────────────────────────────────

    [Fact]
    public void The_header_gives_the_sequence_and_other_files_are_refused()
    {
        Assert.Equal(10849, Crlset.Sequence(CrlsetBytes(10849, new string('\0', 40))));
        Assert.Equal("CRLSet", (string?)Crlset.ParseHeader(CrlsetBytes(1))["ContentType"]);
        var two = new byte[] { 2, 0 };
        foreach (var bad in new[]
                 {
                     Array.Empty<byte>(), new byte[] { 5, 0 }.Concat("hello"u8.ToArray()).ToArray(), two.Concat("{}"u8.ToArray()).ToArray(),
                     Framed("{\"ContentType\":\"CRLSet\",\"Sequence\":1.5}"), Framed("{\"ContentType\":\"CRL\",\"Sequence\":1}"), Framed("[1]"),
                 })
            Assert.Throws<InvalidDataException>(() => Crlset.ParseHeader(bad));
    }

    [Fact]
    public void Install_takes_a_file_or_chromes_component_directory_and_find_returns_the_newest()
    {
        Write("CertificateRevocation/10848/crl-set", CrlsetBytes(10848));
        Write("CertificateRevocation/10849/crl-set", CrlsetBytes(10849));
        var cached = Crlset.Install(P("CertificateRevocation"));
        Assert.EndsWith(Path.Combine("10849", "crl-set"), cached);
        Assert.StartsWith(_cache, cached);
        Crlset.Install(Write("old/crl-set", CrlsetBytes(10700)));
        Assert.Equal(cached, Crlset.Find());
    }

    [Fact]
    public void Find_prefers_the_explicit_path_then_the_env_file()
    {
        Crlset.Install(Write("a/crl-set", CrlsetBytes(5)));
        var envFile = Write("env/crl-set", CrlsetBytes(6));
        _sb.Env("CLEARCOTE_CRLSET_FILE", envFile);
        var explicitFile = Write("x/crl-set", CrlsetBytes(7));
        Assert.Equal(explicitFile, Crlset.Find(explicitFile));
        Assert.Equal(envFile, Crlset.Find());
    }

    [Fact]
    public void Find_is_null_without_a_cache() => Assert.Null(Crlset.Find());

    [Fact]
    public void Seed_writes_the_component_layout_and_keeps_a_newer_one()
    {
        var profile = P("profile");
        var src = Write("s/crl-set", CrlsetBytes(10849, "body"));
        Assert.Equal(10849, Crlset.Seed(profile, src));
        var versionDir = Path.Combine(profile, "CertificateRevocation", "10849");
        Assert.Equal(File.ReadAllBytes(src), File.ReadAllBytes(Path.Combine(versionDir, "crl-set")));
        var manifest = JsonNode.Parse(File.ReadAllText(Path.Combine(versionDir, "manifest.json")))!;
        Assert.Equal("10849", (string?)manifest["version"]);
        Assert.Equal("crl-set-clearcote.data", (string?)manifest["name"]);
        Assert.Equal(2, (int)manifest["manifest_version"]!);
        Write("profile/CertificateRevocation/10900/crl-set", CrlsetBytes(10900));
        Assert.Equal(10900, Crlset.Seed(profile, src));
    }

    // ── the rules ────────────────────────────────────────────────────────────

    [Theory]
    [InlineData(null, null, null, null, CrlsetMode.Off, null)]
    [InlineData(null, null, null, "0", CrlsetMode.Off, null)]
    [InlineData(null, null, null, "1", CrlsetMode.Wait, null)]
    [InlineData(null, null, null, "ON", CrlsetMode.Wait, null)]
    [InlineData(null, null, null, "nowait", CrlsetMode.NoWait, null)]
    [InlineData(true, null, null, "0", CrlsetMode.Wait, null)]
    [InlineData(false, null, null, "1", CrlsetMode.Off, null)]
    [InlineData(false, "/x/crl-set", true, "1", CrlsetMode.Off, null)]
    [InlineData(null, null, true, null, CrlsetMode.NoWait, null)]
    [InlineData(true, null, true, null, CrlsetMode.NoWait, null)]
    [InlineData(null, "/x/crl-set", null, "0", CrlsetMode.Wait, "/x/crl-set")]
    [InlineData(null, "/x/crl-set", true, null, CrlsetMode.NoWait, "/x/crl-set")]
    public void Mode(bool? option, string? path, bool? noWait, string? env, CrlsetMode mode, string? expectedPath)
    {
        Assert.Equal((mode, expectedPath), Crlset.Mode(option, path, noWait, n => n == Crlset.CrlsetEnv ? env : null));
    }

    [Fact]
    public void Timeout_option_then_env_then_15()
    {
        Assert.Equal(15, Crlset.TimeoutSeconds(null, _ => null));
        Assert.Equal(4, Crlset.TimeoutSeconds(null, n => n == Crlset.TimeoutEnv ? "4" : null));
        Assert.Equal(15, Crlset.TimeoutSeconds(null, n => n == Crlset.TimeoutEnv ? "soon" : null));
        Assert.Equal(2.5, Crlset.TimeoutSeconds(2.5, n => n == Crlset.TimeoutEnv ? "4" : null));
    }

    [Fact]
    public void Unsuppress_component_updater()
    {
        Assert.Equal(LaunchOpts.DefaultIgnoredArgs.Append("--disable-component-update"), Crlset.UnsuppressComponentUpdater(null));
        Assert.Equal(new[] { "--enable-automation", "--disable-component-update" }, Crlset.UnsuppressComponentUpdater(new[] { "--enable-automation" }));
        Assert.Equal(new[] { "--disable-component-update" }, Crlset.UnsuppressComponentUpdater(new[] { "--disable-component-update" }));
    }

    // ── a launch's plan ──────────────────────────────────────────────────────

    private static readonly IReadOnlyList<string> Strip = new[] { "--enable-automation" };

    [Fact]
    public async Task Apply_seeds_unsuppresses_and_plans_the_wait()
    {
        Crlset.Install(Write("s/crl-set", CrlsetBytes(42)));
        var (plan, ignore) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = true, CrlsetTimeout = 4, Quiet = true }, Strip);
        Assert.NotNull(plan);
        Assert.Equal((4.0, 42L), (plan!.TimeoutSeconds, plan.Sequence));
        Assert.Contains("--disable-component-update", ignore);
        Assert.Contains("--enable-automation", ignore);
        Assert.True(File.Exists(P("p/CertificateRevocation/42/crl-set")));
    }

    [Fact]
    public async Task Apply_nowait_seeds_without_a_plan()
    {
        Crlset.Install(Write("s/crl-set", CrlsetBytes(42)));
        var (plan, ignore) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { CrlsetNoWait = true }, Strip);
        Assert.Null(plan);
        Assert.Equal(new[] { "--enable-automation", "--disable-component-update" }, ignore);
        Assert.True(Directory.Exists(P("p/CertificateRevocation/42")));
    }

    [Fact]
    public async Task Apply_from_the_environment()
    {
        Crlset.Install(Write("s/crl-set", CrlsetBytes(42)));
        _sb.Env("CLEARCOTE_CRLSET", "1").Env("CLEARCOTE_CRLSET_TIMEOUT", "3");
        var (plan, _) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions(), Strip);
        Assert.Equal(3.0, plan!.TimeoutSeconds);
    }

    [Fact]
    public async Task Apply_is_silent_and_untouched_by_default_without_a_cache()
    {
        var (plan, ignore) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions(), Strip);
        Assert.Null(plan);
        Assert.Same(Strip, ignore);
        Assert.Equal("", _err.ToString());
        Assert.False(Directory.Exists(_cache));
    }

    [Fact]
    public async Task Apply_off_does_nothing()
    {
        Crlset.Install(Write("s/crl-set", CrlsetBytes(42)));
        _sb.Env("CLEARCOTE_CRLSET", "1");
        var (plan, ignore) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = false }, Strip);
        Assert.Null(plan);
        Assert.Same(Strip, ignore);
        Assert.False(Directory.Exists(P("p")));
    }

    [Fact]
    public async Task Apply_warns_once_when_the_first_fetch_fails()
    {
        var handler = new FakeHandler(_ => throw new HttpRequestException("firewall"));
        _sb.Http(handler);
        for (var i = 0; i < 2; i++)
        {
            var (plan, ignore) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = true }, Strip);
            Assert.Null(plan);
            Assert.Same(Strip, ignore);
        }
        var text = _err.ToString();
        Assert.Equal(1, text.Split("could not fetch a CRLSet").Length - 1);
        Assert.Contains("clearcote: crlset: ", text);
        Assert.Equal(Widevine.OmahaUrl, handler.Requests[0].RequestUri!.ToString());
    }

    [Fact]
    public async Task A_quiet_launch_or_CLEARCOTE_NO_WARN_says_nothing()
    {
        _sb.Http(new FakeHandler(_ => throw new HttpRequestException("firewall")));
        await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = true, Quiet = true }, Strip);
        _sb.Env("CLEARCOTE_NO_WARN", "1");
        await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = true }, Strip);
        Assert.Equal("", _err.ToString());
    }

    [Fact]
    public async Task Apply_fetches_into_an_empty_cache_then_seeds()
    {
        var google = new FakeGoogle(Crx(CrlsetBytes(10850)));
        _sb.Http(google.Handler);
        var (plan, _) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = true, Quiet = true }, Strip);
        Assert.Equal(10850, plan!.Sequence);
        Assert.EndsWith(Path.Combine("10850", "crl-set"), Crlset.Find());
        Assert.True(Crlset.StampFresh());
        Assert.Empty(Directory.EnumerateDirectories(_cache).Where(d => Path.GetFileName(d).StartsWith('.')));   // no download left over
    }

    [Fact]
    public async Task A_slow_first_fetch_is_not_waited_for()
    {
        _sb.Env("CLEARCOTE_CRLSET_FETCH_TIMEOUT", "0.2");
        var release = new TaskCompletionSource();
        _sb.Http(new FakeHandler(_ => { release.Task.Wait(TimeSpan.FromSeconds(10)); throw new HttpRequestException("late"); }));
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var (plan, _) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { Crlset = true }, Strip);
        Assert.Null(plan);
        Assert.True(sw.ElapsedMilliseconds < 5000, $"took {sw.ElapsedMilliseconds} ms");
        Assert.Contains("first download took over 0.2 s", _err.ToString());
        release.SetResult();
    }

    [Fact]
    public async Task A_stale_cache_is_used_and_refreshed_in_the_background()
    {
        var cached = Crlset.Install(Write("s/crl-set", CrlsetBytes(10849)));
        var refreshes = 0;
        Crlset.RefreshOverride = () => refreshes++;
        Assert.Equal(cached, await Crlset.ResolveAsync(null, quiet: true));
        Assert.Equal(1, refreshes);   // no stamp: stale
        File.WriteAllText(Path.Combine(_cache, Crlset.StampFile), "");
        Assert.Equal(cached, await Crlset.ResolveAsync(null, quiet: true));
        Assert.Equal(1, refreshes);   // fresh: no refresh
        File.SetLastWriteTimeUtc(Path.Combine(_cache, Crlset.StampFile), DateTime.UtcNow.AddHours(-7));
        await Crlset.ResolveAsync(null, quiet: true);
        Assert.Equal(2, refreshes);
    }

    [Fact]
    public async Task Fetch_from_a_mirror_url()
    {
        var handler = new FakeHandler(_ => new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(CrlsetBytes(77)) });
        _sb.Http(handler).Env("CLEARCOTE_CRLSET_URL", "https://mirror.example/crl-set");
        Assert.EndsWith(Path.Combine("77", "crl-set"), await Crlset.FetchAsync());
        Assert.Equal(new[] { "https://mirror.example/crl-set" }, handler.Requests.Select(r => r.RequestUri!.ToString()));
    }

    [Fact]
    public async Task A_mirror_serving_something_else_is_refused()
    {
        _sb.Http(new FakeHandler(_ => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("<html>") }))
            .Env("CLEARCOTE_CRLSET_URL", "https://mirror.example/crl-set");
        await Assert.ThrowsAsync<InvalidDataException>(() => Crlset.FetchAsync());
        Assert.Null(Crlset.Find());
    }

    internal static byte[] Crx(byte[] payload)
    {
        using var buf = new MemoryStream();
        using (var zip = new ZipArchive(buf, ZipArchiveMode.Create, leaveOpen: true))
        {
            using (var s = zip.CreateEntry("crl-set").Open()) s.Write(payload);
            using (var s = zip.CreateEntry("manifest.json").Open()) s.Write("{}"u8);
        }
        var header = new byte[8];
        return "Cr24"u8.ToArray().Concat(BitConverter.GetBytes(3u)).Concat(BitConverter.GetBytes((uint)header.Length)).Concat(header)
            .Concat(buf.ToArray()).ToArray();
    }

    /// Google's update check + the package download, answering with <paramref name="blob"/>.
    private sealed class FakeGoogle
    {
        public readonly List<string> Bodies = new();
        public readonly FakeHandler Handler;

        public FakeGoogle(byte[] blob, string? sha = null)
        {
            var digest = sha ?? Convert.ToHexString(SHA256.HashData(blob)).ToLowerInvariant();
            Handler = new FakeHandler(r =>
            {
                var url = r.RequestUri!.ToString();
                if (url == Widevine.OmahaUrl)
                {
                    Bodies.Add(r.Content!.ReadAsStringAsync().GetAwaiter().GetResult());
                    var json = "{\"response\":{\"app\":[{\"appid\":\"" + Crlset.AppId + "\",\"updatecheck\":{\"status\":\"ok\",\"pipelines\":[" +
                               "{\"operations\":[{\"type\":\"download\",\"urls\":[{\"url\":\"http://dl/c.crx3\"}],\"out\":{\"sha256\":\"" + digest + "\"}}]}]}}]}}";
                    return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(json) };
                }
                if (url == "http://dl/c.crx3") return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(blob) };
                throw new InvalidOperationException($"unexpected request {url}");
            });
        }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task Download_from_google_verifies_the_package_hash(bool goodHash)
    {
        var blob = Crx(CrlsetBytes(10850));
        var google = new FakeGoogle(blob, goodHash ? null : new string('0', 64));
        _sb.Http(google.Handler);
        if (goodHash)
            Assert.Equal(10850, Crlset.Sequence(await Crlset.DownloadFromGoogleAsync(default)));
        else
            Assert.Contains("sha256", (await Assert.ThrowsAsync<InvalidDataException>(() => Crlset.DownloadFromGoogleAsync(default))).Message);
        Assert.Equal("hfnkpimlhhgieaddgfemjhofmfblmnib", (string?)JsonNode.Parse(google.Bodies[0])!["request"]!["app"]![0]!["appid"]);
    }

    [Fact]
    public async Task Apply_never_throws_on_a_broken_path()
    {
        var (plan, _) = await Crlset.ApplyLaunchAsync(P("p"), new LaunchOptions { CrlsetPath = P("missing") }, Strip);
        Assert.Null(plan);
        Assert.Contains("clearcote: crlset: no usable CRLSet", _err.ToString());
    }

    // ── the probe ────────────────────────────────────────────────────────────

    private static async Task<string> GetAsync(NetworkStream s)
    {
        await s.WriteAsync("GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: null\r\n\r\n"u8.ToArray());
        var buf = new byte[1024];
        var sb = new StringBuilder();
        while (!sb.ToString().Contains("\r\n\r\n"))
        {
            var n = await s.ReadAsync(buf);
            if (n == 0) break;
            sb.Append(Encoding.ASCII.GetString(buf, 0, n));
        }
        return sb.ToString();
    }

    [Fact]
    public async Task The_probe_server_counts_connections_not_requests()
    {
        using var server = new Crlset.ProbeServer();
        Assert.Equal($"http://127.0.0.1:{server.Port}/", server.Url);
        using (var first = new TcpClient())
        {
            await first.ConnectAsync(IPAddress.Loopback, server.Port);
            var s = first.GetStream();
            for (var i = 0; i < 3; i++)   // keep-alive: one connection
            {
                var res = await GetAsync(s);
                Assert.StartsWith("HTTP/1.1 204", res);
                Assert.Contains("Access-Control-Allow-Origin: *", res);
                Assert.Contains("Content-Length: 0", res);
            }
            Assert.Equal(1, server.Count);
        }
        using (var second = new TcpClient())
        {
            await second.ConnectAsync(IPAddress.Loopback, server.Port);
            await GetAsync(second.GetStream());
        }
        Assert.Equal(2, server.Count);
    }

    [Theory]
    [InlineData(null, null, null)]
    [InlineData("http://p:1", null, "--proxy-bypass-list=<-loopback>;127.0.0.1:9")]
    [InlineData("http://p:1", ".corp, intra", "--proxy-bypass-list=*.corp;intra;<-loopback>;127.0.0.1:9")]
    [InlineData("http://p:1", "<-loopback>", "--proxy-bypass-list=<-loopback>;127.0.0.1:9")]
    public void The_probe_port_bypasses_a_playwright_proxy_last(string? server, string? bypass, string? expected)
    {
        var proxy = server is null ? null : new ProxyOptions { Server = server, Bypass = bypass };
        var args = Crlset.ProxyBypassArgs(proxy, "127.0.0.1:9");
        Assert.Equal(expected is null ? Array.Empty<string>() : new[] { expected }, args);
        if (proxy is not null) Assert.Equal(bypass, proxy.Bypass);   // the proxy itself is left alone
    }

    [Fact]
    public void Playwrights_forced_loopback_switch_is_respected()
    {
        _sb.Env("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", "1");
        Assert.Equal(new[] { "--proxy-bypass-list=127.0.0.1:9" }, Crlset.ProxyBypassArgs(new ProxyOptions { Server = "http://p:1" }, "127.0.0.1:9"));
    }

    [Fact]
    public async Task Readiness_starts_the_probe_before_launch_and_closes_it()
    {
        var plan = new Crlset.Readiness(1.0, 42, quiet: true);
        var args = plan.BeforeLaunch(new ProxyOptions { Server = "http://p:1" });
        var port = plan.Server!.Port;
        Assert.Equal(new[] { $"--proxy-bypass-list=<-loopback>;127.0.0.1:{port}" }, args);
        plan.Close();
        Assert.Null(plan.Server);
        Assert.False(await plan.WaitAsync(null!));   // nothing to wait with once closed
        using var c = new TcpClient();
        await Assert.ThrowsAnyAsync<SocketException>(() => c.ConnectAsync(IPAddress.Loopback, port));
    }

    private sealed class Opened : Crlset.IProbeConnections
    {
        public readonly List<double> Times;
        public Opened(params double[] times) => Times = times.ToList();
        public int Count => Times.Count;
        public bool ReopenedAfter(double moment) => Times.Skip(1).Any(t => t > moment);
    }

    [Fact]
    public void A_drop_before_startup_completes_does_not_count()
    {
        // a network change at launch (Linux) dropped the pooled connection: re-opened at t=2, startup at t=5
        var server = new Opened(1.0, 2.0);
        var state = new Crlset.ReadinessState(server);
        Assert.False(state.Ready(3.0));
        state.NoteStartup(true, firstCheck: false, now: 5.0);
        Assert.False(state.Ready(5.1));
        server.Times.Add(5.6);   // the CRLSet's drop
        Assert.True(state.Ready(5.7));
    }

    [Fact]
    public void Startup_already_complete_at_the_first_check_waits_a_grace_then_counts()
    {
        var state = new Crlset.ReadinessState(new Opened(10.0));
        state.NoteStartup(true, firstCheck: true, now: 10.0);
        Assert.False(state.Ready(10.5));
        Assert.True(state.Ready(10.0 + Crlset.AlreadyStartedGraceSeconds));
    }

    [Fact]
    public void Without_the_histogram_the_first_drop_counts()
    {
        var server = new Opened(1.0);
        var state = new Crlset.ReadinessState(server) { Histogram = false };
        Assert.False(state.Ready(2.0));
        server.Times.Add(2.5);
        Assert.True(state.Ready(2.6));
    }

    [Fact]
    public async Task The_probe_server_records_when_each_connection_opened()
    {
        using var server = new Crlset.ProbeServer();
        using (var a = new TcpClient()) { await a.ConnectAsync(IPAddress.Loopback, server.Port); await GetAsync(a.GetStream()); }
        var between = Crlset.Now();
        Assert.False(server.ReopenedAfter(between));   // only the first connection so far
        using (var b = new TcpClient()) { await b.ConnectAsync(IPAddress.Loopback, server.Port); await GetAsync(b.GetStream()); }
        Assert.True(server.ReopenedAfter(between));
        Assert.False(server.ReopenedAfter(Crlset.Now()));
    }

    // ── the launches ─────────────────────────────────────────────────────────

    [Fact]
    public void A_cloud_or_docker_launch_refuses_the_crlset()
    {
        Assert.Equal("Crlset is not available for cloud browsers",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { Crlset = true })).Message);
        Assert.Equal("CrlsetTimeout is not available for cloud browsers",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { CrlsetTimeout = 3 })).Message);
        Assert.StartsWith("CrlsetPath is not available when LaunchAsync runs Clearcote in Docker",
            Assert.Throws<ArgumentException>(() => DockerLaunch.ContainerEnv(new LaunchOptions { CrlsetPath = "/x" })).Message);
    }

    [Fact]
    public void An_incognito_launch_says_it_has_no_profile_only_when_asked_by_option()
    {
        _sb.Env("CLEARCOTE_CRLSET", "1");
        Crlset.WarnNoProfile(new LaunchOptions(), "LaunchAsync");
        Assert.Equal("", _err.ToString());
        Crlset.WarnNoProfile(new LaunchOptions { Crlset = true, Quiet = true }, "LaunchAsync");
        Assert.Equal("", _err.ToString());
        Crlset.WarnNoProfile(new LaunchOptions { Crlset = true }, "LaunchAsync");
        Assert.Contains("clearcote: crlset: LaunchAsync has no profile to seed a CRLSet into", _err.ToString());
    }

    /// A stand-in engine: writes its arguments next to itself, then exits (the launch then fails).
    private string Standin()
    {
        var exe = Path.Combine(TestTemp.Create("cc-crlset-standin-"), "chrome");
        File.WriteAllText(exe, "#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\nexit 1\n");
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    [Fact]
    public async Task A_persistent_launch_seeds_unsuppresses_bypasses_and_stops_the_probe_when_the_launch_fails()
    {
        if (OperatingSystem.IsWindows()) return;
        Crlset.Install(Write("s/crl-set", CrlsetBytes(4242)));
        File.WriteAllText(Path.Combine(_cache, Crlset.StampFile), "");   // fresh: no background refresh
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null)
            .Env("CLEARCOTE_BROWSER_VERSION", null).Env("CLEARCOTE_WIDEVINE", null).Env("CLEARCOTE_OFFLINE_KEY", null);
        var exe = Standin();
        try
        {
            var udd = P("udd");
            var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(udd, new LaunchOptions
            {
                ExecutablePath = exe, Headless = true, Quiet = true, Timeout = 20000, Crlset = true,
                Proxy = new ProxyOptions { Server = "http://127.0.0.1:1", Bypass = ".corp" },
            }));
            var argvFile = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
            Assert.True(File.Exists(argvFile), $"the stand-in engine never ran: {e}");
            var argv = File.ReadAllLines(argvFile);
            Assert.DoesNotContain("--disable-component-update", argv);
            var bypass = argv.Last(a => a.StartsWith("--proxy-bypass-list=", StringComparison.Ordinal));
            Assert.Matches(@"^--proxy-bypass-list=\*\.corp;<-loopback>;127\.0\.0\.1:\d+$", bypass);
            Assert.True(File.Exists(Path.Combine(udd, "CertificateRevocation", "4242", "crl-set")));
            var port = int.Parse(bypass[(bypass.LastIndexOf(':') + 1)..]);
            using var c = new TcpClient();
            await Assert.ThrowsAnyAsync<SocketException>(() => c.ConnectAsync(IPAddress.Loopback, port));   // the probe stopped
        }
        finally { TestTemp.Remove(Path.GetDirectoryName(exe)!); }
    }
}
