using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// <see cref="RequestProxy"/>: Playwright's request API (context.APIRequest, page.APIRequest) reaches an
/// authenticated proxy whose credentials went to the engine, through a local credential-less forwarder, instead of
/// leaving from this machine's IP. Mirrors sdk/node/test/reqproxy.test.ts.
/// </summary>
/// <remarks>
/// The fake proxies send every target to the fake origin on 127.0.0.1, so the origin is addressed by names
/// ("origin.test", "10.255.255.1") the forwarder must hand to the proxy; a loopback name is connected directly.
/// The launch tests at the end run the real launches against a stand-in engine (a shell script that writes down
/// its command line and exits), so they are POSIX-only.
/// </remarks>
public class RequestProxyTests : IDisposable
{
    private readonly List<Func<Task>> _cleanups = new();
    private readonly List<string> _dirs = new();

    public void Dispose()
    {
        for (var i = _cleanups.Count - 1; i >= 0; i--) { try { _cleanups[i]().GetAwaiter().GetResult(); } catch { } }
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    // ── fakes ────────────────────────────────────────────────────────────────

    /// A TCP server running <paramref name="handle"/> per connection; stopped at the end of the test.
    private int Serve(Func<TcpClient, Task> handle)
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var cts = new CancellationTokenSource();
        var clients = new List<TcpClient>();
        _ = Task.Run(async () =>
        {
            while (!cts.IsCancellationRequested)
            {
                TcpClient c;
                try { c = await l.AcceptTcpClientAsync(cts.Token); } catch { return; }
                lock (clients) clients.Add(c);
                _ = Task.Run(async () => { try { await handle(c); } catch { } finally { c.Dispose(); } });
            }
        });
        _cleanups.Add(() =>
        {
            cts.Cancel(); l.Stop();
            lock (clients) foreach (var c in clients) c.Dispose();
            return Task.CompletedTask;
        });
        return ((IPEndPoint)l.LocalEndpoint).Port;
    }

    private static async Task<(string Head, byte[] After)> ReadHead(Stream s)
    {
        var buf = new List<byte>();
        var one = new byte[1];
        while (true)
        {
            if (await s.ReadAsync(one) <= 0) throw new IOException("closed before the end of the head");
            buf.Add(one[0]);
            var n = buf.Count;
            if (n >= 4 && buf[n - 4] == '\r' && buf[n - 3] == '\n' && buf[n - 2] == '\r' && buf[n - 1] == '\n')
                return (Encoding.Latin1.GetString(buf.ToArray(), 0, n - 4), Array.Empty<byte>());
        }
    }

    private static Task Write(Stream s, string text) => s.WriteAsync(Encoding.Latin1.GetBytes(text)).AsTask();

    private static async Task Pipe(Stream a, Stream b)
    {
        var t1 = a.CopyToAsync(b);
        var t2 = b.CopyToAsync(a);
        await Task.WhenAny(t1, t2);
    }

    /// An origin that answers "hello &lt;path&gt;" and closes.
    private int Origin() => Serve(async c =>
    {
        var s = c.GetStream();
        var (head, _) = await ReadHead(s);
        var path = head.Split("\r\n")[0].Split(' ')[1];
        var body = $"hello {path}";
        await Write(s, $"HTTP/1.1 200 OK\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n{body}");
    });

    /// An HTTP proxy that demands Basic auth, logs what it saw and sends every CONNECT target to
    /// 127.0.0.1:<paramref name="originPort"/> (a plain-http request it answers itself, as the origin does).
    private (int Port, List<string> Log) AuthHttpProxy(string user, string pass, int originPort)
    {
        var log = new List<string>();
        var want = "Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes($"{user}:{pass}"));
        var port = Serve(async c =>
        {
            var s = c.GetStream();
            var (head, _) = await ReadHead(s);
            var lines = head.Split("\r\n");
            var first = lines[0].Split(' ');
            var auth = lines.Skip(1).Select(l => l.Split(": ", 2)).FirstOrDefault(p => p[0].Equals("Proxy-Authorization", StringComparison.OrdinalIgnoreCase))?[1];
            var ok = auth == want;
            lock (log) log.Add($"{first[1]} auth={(ok ? "ok" : "bad")}");
            if (!ok) { await Write(s, "HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"fake\"\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"); return; }
            if (first[0] != "CONNECT")
            {
                // A browser's plain-http request (absolute form): answered by the proxy itself, as the origin would.
                var body = $"hello {new Uri(first[1]).PathAndQuery}";
                await Write(s, $"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n{body}");
                return;
            }
            using var up = new TcpClient();
            await up.ConnectAsync(IPAddress.Loopback, originPort);
            await Write(s, "HTTP/1.1 200 Connection Established\r\n\r\n");
            await Pipe(s, up.GetStream());
        });
        return (port, log);
    }

    /// A SOCKS5 proxy that only accepts username/password auth, logs the target it was asked for and sends every
    /// target to 127.0.0.1:<paramref name="originPort"/>.
    private (int Port, List<string> Log) AuthSocks5(string user, string pass, int originPort)
    {
        var log = new List<string>();
        var port = Serve(async c =>
        {
            var s = c.GetStream();
            async Task<byte[]> N(int n) { var b = new byte[n]; await s.ReadExactlyAsync(b); return b; }
            var greet = await N(2);
            var methods = await N(greet[1]);
            if (!methods.Contains((byte)2)) { await s.WriteAsync(new byte[] { 5, 0xff }); return; }
            await s.WriteAsync(new byte[] { 5, 2 });
            var ver = await N(2);
            var u = Encoding.UTF8.GetString(await N(ver[1]));
            var pl = (await N(1))[0];
            var p = Encoding.UTF8.GetString(await N(pl));
            var ok = u == user && p == pass;
            lock (log) log.Add($"auth={(ok ? "ok" : "bad")}");
            if (!ok) { await s.WriteAsync(new byte[] { 1, 1 }); return; }
            await s.WriteAsync(new byte[] { 1, 0 });
            var req = await N(4);
            string host, kind;
            if (req[3] == 1) { host = new IPAddress(await N(4)).ToString(); kind = "ip"; }
            else if (req[3] == 3) { var l = (await N(1))[0]; host = Encoding.UTF8.GetString(await N(l)); kind = "name"; }
            else if (req[3] == 4) { host = new IPAddress(await N(16)).ToString(); kind = "ip6"; }
            else { await s.WriteAsync(new byte[] { 5, 8, 0, 1, 0, 0, 0, 0, 0, 0 }); return; }
            var pb = await N(2);
            lock (log) log.Add($"connect {kind} {host}:{(pb[0] << 8) | pb[1]}");
            using var up = new TcpClient();
            await up.ConnectAsync(IPAddress.Loopback, originPort);
            await s.WriteAsync(new byte[] { 5, 0, 0, 1, 127, 0, 0, 1, 0, 0 });
            await Pipe(s, up.GetStream());
        });
        return (port, log);
    }

    private RequestProxy Forwarder(RequestProxy.Upstream up, string? bypass = null)
    {
        var rp = RequestProxy.Start(up, bypass);
        _cleanups.Add(rp.CloseAsync);
        return rp;
    }

    /// What Playwright's fetch does: CONNECT through the forwarder, then a GET over the tunnel. Returns the
    /// CONNECT reply head and, when it was a 200, the origin's whole answer.
    private static async Task<(string ConnectHead, string? Response)> Fetch(int forwarderPort, string host, int port, string path)
    {
        using var c = new TcpClient();
        await c.ConnectAsync(IPAddress.Loopback, forwarderPort);
        var s = c.GetStream();
        var authority = host.Contains(':') ? $"[{host}]:{port}" : $"{host}:{port}";
        await Write(s, $"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n");
        var (head, _) = await ReadHead(s);
        if (!head.StartsWith("HTTP/1.1 200")) return (head, null);
        await Write(s, $"GET {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n");
        using var ms = new MemoryStream();
        await s.CopyToAsync(ms);
        return (head, Encoding.Latin1.GetString(ms.ToArray()));
    }

    private static RequestProxy.Upstream Up(string protocol, int port, string user = "u", string pass = "p")
        => new(protocol, "127.0.0.1", port, user, pass);

    // ── what to forward ──────────────────────────────────────────────────────

    [Fact]
    public void Upstream_is_null_unless_ResolveProxy_moved_credentials_to_the_engine()
    {
        Assert.Null(RequestProxy.RequestProxyUpstream(null, Array.Empty<string>()));
        var plain = new ProxyOptions { Server = "http://h:3128" };
        Assert.Null(RequestProxy.RequestProxyUpstream(plain, LaunchOpts.ResolveProxy(plain, true).Args));
        // an old engine without --proxy-auth leaves http credentials with Playwright, which proxies the request API itself
        var http1 = new ProxyOptions { Server = "http://h:3128", Username = "u", Password = "p" };
        Assert.Null(RequestProxy.RequestProxyUpstream(http1, LaunchOpts.ResolveProxy(http1, false).Args));
    }

    [Fact]
    public void Upstream_reads_http_https_and_socks5_with_decoded_url_credentials()
    {
        var h = new ProxyOptions { Server = "http://us%40er:p%3Ass@h:3128" };
        Assert.Equal(new RequestProxy.Upstream("http", "h", 3128, "us@er", "p:ss"),
            RequestProxy.RequestProxyUpstream(h, LaunchOpts.ResolveProxy(h, true).Args));
        var s = new ProxyOptions { Server = "https://proxy.example", Username = "u", Password = "p" };
        Assert.Equal(new RequestProxy.Upstream("https", "proxy.example", 443, "u", "p"),
            RequestProxy.RequestProxyUpstream(s, LaunchOpts.ResolveProxy(s, true).Args));
        var k = new ProxyOptions { Server = "socks5://u:p@[::1]:1080" };
        Assert.Equal(new RequestProxy.Upstream("socks5", "::1", 1080, "u", "p"),
            RequestProxy.RequestProxyUpstream(k, LaunchOpts.ResolveProxy(k).Args));
        var k2 = new ProxyOptions { Server = "socks5h://h", Username = "u", Password = "p" };
        Assert.Equal(new RequestProxy.Upstream("socks5", "h", 1080, "u", "p"),
            RequestProxy.RequestProxyUpstream(k2, LaunchOpts.ResolveProxy(k2).Args));
    }

    [Fact]
    public void Upstream_leaves_socks4_alone()
    {
        // socks4 has no password auth: nothing a forwarder can add
        var k = new ProxyOptions { Server = "socks4://u:p@h:1080" };
        Assert.Null(RequestProxy.RequestProxyUpstream(k, LaunchOpts.ResolveProxy(k).Args));
    }

    [Fact]
    public void Args_override_playwrights_loopback_bypass_unless_the_caller_set_one()
    {
        Assert.Equal(new[] { "--proxy-bypass-list=" }, RequestProxy.RequestProxyArgs(new[] { "--proxy-server=http://h:1", "--proxy-auth=u:p" }));
        Assert.Empty(RequestProxy.RequestProxyArgs(new[] { "--proxy-server=http://h:1", "--proxy-bypass-list=*.internal" }));
    }

    [Fact]
    public void Parses_connect_targets_and_ipv6_literals()
    {
        Assert.Equal(("example.com", 443), RequestProxy.ParseHostPort("example.com:443"));
        Assert.Equal(("2001:db8::1", 8443), RequestProxy.ParseHostPort("[2001:db8::1]:8443"));
        Assert.Null(RequestProxy.ParseHostPort("example.com"));
        Assert.Null(RequestProxy.ParseHostPort("h:0"));
        Assert.Null(RequestProxy.ParseHostPort("h:70000"));
        Assert.Equal("00000000000000000000000000000001", Convert.ToHexString(RequestProxy.Ipv6Bytes("::1")!).ToLowerInvariant());
        Assert.Equal("20010db8000000000000000000080001", Convert.ToHexString(RequestProxy.Ipv6Bytes("2001:db8::8:1")!).ToLowerInvariant());
        Assert.Null(RequestProxy.Ipv6Bytes("::ffff:1.2.3.4"));
        Assert.Null(RequestProxy.Ipv6Bytes("1::2::3"));
    }

    [Fact]
    public void Loopback_names_are_recognised()
    {
        Assert.True(RequestProxy.IsLoopback("127.8.9.10"));
        Assert.True(RequestProxy.IsLoopback("LOCALHOST."));
        Assert.True(RequestProxy.IsLoopback("app.localhost"));
        Assert.True(RequestProxy.IsLoopback("::1"));
        Assert.False(RequestProxy.IsLoopback("128.0.0.1"));
        Assert.False(RequestProxy.IsLoopback("localhost.example.com"));
    }

    // ── the forwarder ────────────────────────────────────────────────────────

    [Fact]
    public async Task Tunnels_through_an_authenticated_http_proxy()
    {
        var o = Origin();
        var up = AuthHttpProxy("u1", "p@ss:w0rd#é", o);
        var rp = Forwarder(Up("http", up.Port, "u1", "p@ss:w0rd#é"));
        Assert.Matches(@"^http://127\.0\.0\.1:\d+$", rp.PlaywrightProxy.Server);
        Assert.Null(rp.PlaywrightProxy.Username);
        Assert.Null(rp.PlaywrightProxy.Password);
        var (head, response) = await Fetch(rp.Port, "origin.test", 8080, "/via-forwarder");
        Assert.StartsWith("HTTP/1.1 200", head);
        Assert.EndsWith("hello /via-forwarder", response);
        Assert.Equal(new[] { "origin.test:8080 auth=ok" }, up.Log);
    }

    [Fact]
    public async Task Tunnels_through_an_authenticated_socks5_proxy_which_resolves_names_itself()
    {
        var o = Origin();
        var up = AuthSocks5("sockuser", "s3cr3t:é", o);
        var rp = Forwarder(Up("socks5", up.Port, "sockuser", "s3cr3t:é"));
        Assert.EndsWith("hello /socks", (await Fetch(rp.Port, "origin.test", 8080, "/socks")).Response);
        Assert.EndsWith("hello /ip", (await Fetch(rp.Port, "10.255.255.1", 8081, "/ip")).Response);
        Assert.EndsWith("hello /ip6", (await Fetch(rp.Port, "2001:db8::1", 8082, "/ip6")).Response);
        Assert.Equal(new[]
        {
            "auth=ok", "connect name origin.test:8080",
            "auth=ok", "connect ip 10.255.255.1:8081",
            "auth=ok", "connect ip6 2001:db8::1:8082",
        }, up.Log);
    }

    [Fact]
    public async Task Playwrights_request_api_goes_through_the_forwarder()
    {
        // The real client: Playwright's driver sends APIRequest through HttpsProxyAgent, with CONNECT.
        var o = Origin();
        var up = AuthHttpProxy("u1", "pw", o);
        var rp = Forwarder(Up("http", up.Port, "u1", "pw"));
        var pw = await Clearcote.PlaywrightInstanceAsync();
        var ctx = await pw.APIRequest.NewContextAsync(new() { Proxy = new Proxy { Server = rp.PlaywrightProxy.Server! } });
        _cleanups.Add(() => ctx.DisposeAsync().AsTask());
        var res = await ctx.GetAsync("http://origin.test:8080/api");
        Assert.Equal(200, res.Status);
        Assert.Equal("hello /api", await res.TextAsync());
        Assert.Equal(new[] { "origin.test:8080 auth=ok" }, up.Log);
    }

    [Fact]
    public async Task Answers_502_naming_the_cause_when_the_upstream_refuses_the_credentials()
    {
        var o = Origin();
        var up = AuthHttpProxy("u1", "right", o);
        var rp = Forwarder(Up("http", up.Port, "u1", "wrong"));
        var (head, response) = await Fetch(rp.Port, "origin.test", 8080, "/x");
        Assert.StartsWith("HTTP/1.1 502 ", head);
        Assert.Matches(@"(?m)^X-Clearcote-Proxy-Error: .*407", head);
        Assert.Null(response);
        Assert.Equal(new[] { "origin.test:8080 auth=bad" }, up.Log);
    }

    [Fact]
    public async Task Answers_502_when_the_socks5_proxy_refuses_the_credentials()
    {
        var o = Origin();
        var up = AuthSocks5("u1", "right", o);
        var rp = Forwarder(Up("socks5", up.Port, "u1", "wrong"));
        var (head, _) = await Fetch(rp.Port, "origin.test", 8080, "/x");
        Assert.StartsWith("HTTP/1.1 502 ", head);
        Assert.Contains("rejected the username or password", head);
    }

    [Fact]
    public async Task An_https_upstream_gets_tls_before_the_connect()
    {
        // The fake only looks at the first bytes (a TLS handshake record, carrying the proxy's name as SNI) and
        // hangs up, so the tunnel fails with a 502.
        var first = new TaskCompletionSource<byte[]>(TaskCreationOptions.RunContinuationsAsynchronously);
        var port = Serve(async c =>
        {
            var b = new byte[4096];
            var n = await c.GetStream().ReadAsync(b);
            first.TrySetResult(b[..n]);
        });
        var rp = Forwarder(new RequestProxy.Upstream("https", "localhost", port, "u", "p"));
        var (head, _) = await Fetch(rp.Port, "origin.test", 443, "/");
        Assert.StartsWith("HTTP/1.1 502 ", head);
        var hello = await first.Task.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(0x16, hello[0]);   // handshake
        Assert.Equal(0x03, hello[1]);   // TLS
        Assert.Contains("localhost", Encoding.Latin1.GetString(hello));
        Assert.DoesNotContain("CONNECT", Encoding.Latin1.GetString(hello));
    }

    [Fact]
    public async Task Answers_502_when_the_upstream_is_unreachable()
    {
        var dead = new TcpListener(IPAddress.Loopback, 0);
        dead.Start();
        var port = ((IPEndPoint)dead.LocalEndpoint).Port;
        dead.Stop();
        var rp = Forwarder(Up("http", port));
        var (head, _) = await Fetch(rp.Port, "origin.test", 8080, "/x");
        Assert.StartsWith("HTTP/1.1 502 ", head);
        Assert.Contains("X-Clearcote-Proxy-Error: ", head);
    }

    [Fact]
    public async Task Connects_loopback_targets_directly_never_through_the_proxy()
    {
        var o = Origin();
        var up = AuthHttpProxy("u1", "p", o);
        var rp = Forwarder(Up("http", up.Port, "u1", "p"));
        Assert.EndsWith("hello /lo", (await Fetch(rp.Port, "localhost", o, "/lo")).Response);
        Assert.EndsWith("hello /lo4", (await Fetch(rp.Port, "127.0.0.1", o, "/lo4")).Response);
        Assert.Empty(up.Log);
    }

    [Fact]
    public async Task Refuses_anything_but_CONNECT()
    {
        var rp = Forwarder(Up("http", 9));
        using var c = new TcpClient();
        await c.ConnectAsync(IPAddress.Loopback, rp.Port);
        var s = c.GetStream();
        await Write(s, "GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n");
        using var ms = new MemoryStream();
        await s.CopyToAsync(ms);
        Assert.StartsWith("HTTP/1.1 405 ", Encoding.Latin1.GetString(ms.ToArray()));
    }

    [Fact]
    public void Passes_the_callers_bypass_list_to_playwright()
    {
        Assert.Equal("*.internal", Forwarder(Up("http", 9), " *.internal ").PlaywrightProxy.Bypass);
        Assert.Null(Forwarder(Up("http", 9), "  ").PlaywrightProxy.Bypass);
    }

    [Fact]
    public async Task Stops_accepting_connections_and_drops_open_tunnels_after_close()
    {
        var o = Origin();
        var up = AuthHttpProxy("u", "p", o);
        var rp = RequestProxy.Start(Up("http", up.Port));
        // An open tunnel: CONNECT answered, nothing sent yet.
        using var open = new TcpClient();
        await open.ConnectAsync(IPAddress.Loopback, rp.Port);
        await Write(open.GetStream(), "CONNECT origin.test:8080 HTTP/1.1\r\nHost: origin.test:8080\r\n\r\n");
        Assert.StartsWith("HTTP/1.1 200", (await ReadHead(open.GetStream())).Head);
        await rp.CloseAsync();
        await rp.CloseAsync();   // closes once
        var read = open.GetStream().ReadAsync(new byte[1]).AsTask();
        Assert.Same(read, await Task.WhenAny(read, Task.Delay(5000)));
        Assert.Equal(0, await read.ContinueWith(t => t.IsFaulted ? 0 : t.Result));
        await Assert.ThrowsAnyAsync<SocketException>(async () =>
        {
            using var c = new TcpClient();
            await c.ConnectAsync(IPAddress.Loopback, rp.Port);
        });
    }

    [Fact]
    public async Task Route_starts_a_forwarder_only_when_credentials_went_to_the_engine()
    {
        var plain = new ProxyOptions { Server = "http://h:3128" };
        Assert.Null(RequestProxy.Route(plain, LaunchOpts.ResolveProxy(plain, true).Args, quiet: true));
        var authed = new ProxyOptions { Server = "http://u:p@h:3128" };
        var route = RequestProxy.Route(authed, LaunchOpts.ResolveProxy(authed, true).Args, quiet: true);
        Assert.NotNull(route);
        await route!.CloseAsync();
        Assert.Null(route.PlaywrightProxy.Username);
    }

    // ── the launches (stand-in engine) ───────────────────────────────────────

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    /// A stand-in engine: writes its arguments next to itself, then exits. The probe's literals sit after the
    /// exit, where the shell never reads.
    private string Standin(bool proxyAuth)
    {
        var exe = Path.Combine(Dir("cc-rp-engine-"), "chrome");
        var script = "#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\nexit 1\n" + (proxyAuth ? "\0proxy-auth\0\n" : "\n");
        File.WriteAllBytes(exe, Encoding.Latin1.GetBytes(script));
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private async Task<List<string>> LaunchArgv(string entry, string exe, Action<LaunchOptions> set)
    {
        using var sb = new Sandbox();
        sb.TempHome();
        sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_DOCKER", null)
            .Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null).Env(PersonaEnv.OptOutEnv, null);
        LaunchOptions o = entry == "serve" ? new ServeOptions { ReadyTimeoutMs = 20000 } : new LaunchOptions();
        o.ExecutablePath = exe; o.Headless = true; o.Quiet = true; o.Timeout = 20000;
        set(o);
        var argvFile = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
        File.Delete(argvFile);
        var e = entry switch
        {
            "launch" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(o)),
            "persistent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-rp-udd-"), o)),
            "ephemeral" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(o)),
            "serve" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync((ServeOptions)o)),
            _ => throw new ArgumentException(entry),
        };
        Assert.True(File.Exists(argvFile), $"the stand-in engine never ran: {e}");
        return File.ReadAllLines(argvFile).ToList();
    }

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("ephemeral")]
    public async Task A_credentialed_http_proxy_goes_to_the_engine_and_playwright_gets_the_forwarder(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var argv = await LaunchArgv(entry, Standin(proxyAuth: true),
            o => o.Proxy = new ProxyOptions { Server = "http://user:pa%3Ass@proxy.example:3128" });
        // Playwright's own switches first (the forwarder, proxying even loopback), then the engine's: the last wins.
        var pwServer = argv.FindIndex(a => Regex.IsMatch(a, @"^--proxy-server=http://127\.0\.0\.1:\d+$"));
        var ours = argv.IndexOf("--proxy-server=http://proxy.example:3128");
        Assert.True(pwServer >= 0, string.Join(" ", argv));
        Assert.True(ours > pwServer, string.Join(" ", argv));
        Assert.Contains("--proxy-auth=user:pa:ss", argv);
        Assert.Equal("--proxy-bypass-list=", argv.Last(a => a.StartsWith("--proxy-bypass-list=", StringComparison.Ordinal)));
        Assert.DoesNotContain(argv, a => a.Contains("user:pa", StringComparison.Ordinal) && a.StartsWith("--proxy-server", StringComparison.Ordinal));
        // The browser never came up: the forwarder is closed again.
        var port = int.Parse(Regex.Match(argv[pwServer], @"(\d+)$").Value);
        await Assert.ThrowsAnyAsync<SocketException>(async () =>
        {
            using var c = new TcpClient();
            await c.ConnectAsync(IPAddress.Loopback, port);
        });
    }

    [Fact]
    public async Task An_engine_without_proxy_auth_keeps_playwrights_handling()
    {
        if (OperatingSystem.IsWindows()) return;
        var argv = await LaunchArgv("launch", Standin(proxyAuth: false),
            o => o.Proxy = new ProxyOptions { Server = "http://proxy.example:3128", Username = "u", Password = "p" });
        Assert.Equal(new[] { "--proxy-server=http://proxy.example:3128" }, argv.Where(a => a.StartsWith("--proxy-server=", StringComparison.Ordinal)));
        Assert.DoesNotContain(argv, a => a.StartsWith("--proxy-auth=", StringComparison.Ordinal));
    }

    [Fact]
    public async Task A_proxy_without_credentials_gets_no_forwarder()
    {
        if (OperatingSystem.IsWindows()) return;
        var argv = await LaunchArgv("launch", Standin(proxyAuth: true), o => o.Proxy = new ProxyOptions { Server = "http://proxy.example:3128" });
        Assert.Equal(new[] { "--proxy-server=http://proxy.example:3128" }, argv.Where(a => a.StartsWith("--proxy-server=", StringComparison.Ordinal)));
        Assert.DoesNotContain("--proxy-bypass-list=", argv);
    }

    [Fact]
    public async Task Serve_moves_http_credentials_to_the_engine_and_warns_about_cdp_clients()
    {
        if (OperatingSystem.IsWindows()) return;
        using var err = new StderrCapture();
        var argv = await LaunchArgv("serve", Standin(proxyAuth: true), o =>
        {
            o.Quiet = false;
            o.Proxy = new ProxyOptions { Server = "http://proxy.example:3128", Username = "u", Password = "p" };
        });
        Assert.Equal(new[] { "--proxy-server=http://proxy.example:3128" }, argv.Where(a => a.StartsWith("--proxy-server=", StringComparison.Ordinal)));
        Assert.Contains("--proxy-auth=u:p", argv);
        Assert.Contains(RequestProxy.ServeWarning, err.Text);
    }

    // ── live engine (CLEARCOTE_LIVE_ENGINE = a Clearcote chrome with --proxy-auth; headless) ─────────

    [Theory]
    [InlineData("launch")]
    [InlineData("ephemeral")]
    public async Task Live_the_page_and_the_request_api_both_reach_the_authenticated_proxy(string entry)
    {
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe) || OperatingSystem.IsWindows()) return;   // no Skippable* package here
        Assert.True(LaunchOpts.EngineSupportsSwitch(exe, "proxy-auth"), "the live engine has no --proxy-auth");
        var o = Origin();
        var up = AuthHttpProxy("liveuser", "l1ve:pass", o);
        var options = new LaunchOptions
        {
            ExecutablePath = exe, Headless = true, Quiet = true, Timeout = 60000,
            Proxy = new ProxyOptions { Server = $"http://liveuser:l1ve%3Apass@127.0.0.1:{up.Port}" },
        };
        IBrowserContext ctx;
        IBrowser? browser = null;
        if (entry == "launch") { browser = await Clearcote.LaunchAsync(options); ctx = await browser.NewContextAsync(); }
        else ctx = await Clearcote.LaunchEphemeralProfileAsync(options);
        try
        {
            var page = await ctx.NewPageAsync();
            await page.GotoAsync("http://origin.test:8080/page");
            Assert.Equal("hello /page", await page.InnerTextAsync("body"));
            // origin.test resolves nowhere: only the proxy can answer these
            var viaContext = await ctx.APIRequest.GetAsync("http://origin.test:8080/context-request");
            Assert.Equal("hello /context-request", await viaContext.TextAsync());
            var viaPage = await page.APIRequest.GetAsync("http://origin.test:8080/page-request");
            Assert.Equal("hello /page-request", await viaPage.TextAsync());
            lock (up.Log)
            {
                Assert.Contains(up.Log, l => l == "http://origin.test:8080/page auth=ok");
                Assert.Equal(2, up.Log.Count(l => l == "origin.test:8080 auth=ok"));
                Assert.DoesNotContain("origin.test:8080 auth=bad", up.Log);   // the forwarder sends the credentials up front
            }
        }
        finally
        {
            if (browser is not null) await browser.CloseAsync(); else await ctx.CloseAsync();
        }
    }

    [Fact]
    public async Task Live_socks5_the_page_and_the_request_api_both_reach_the_authenticated_proxy()
    {
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe) || OperatingSystem.IsWindows()) return;
        var o = Origin();
        var up = AuthSocks5("sockuser", "s3cr3t", o);
        var browser = await Clearcote.LaunchAsync(new LaunchOptions
        {
            ExecutablePath = exe, Headless = true, Quiet = true, Timeout = 60000,
            Proxy = new ProxyOptions { Server = $"socks5://127.0.0.1:{up.Port}", Username = "sockuser", Password = "s3cr3t" },
        });
        try
        {
            var page = await browser.NewPageAsync();
            await page.GotoAsync("http://origin.test:8080/page");
            Assert.Equal("hello /page", await page.InnerTextAsync("body"));
            var viaPage = await page.APIRequest.GetAsync("http://origin.test:8080/page-request");
            Assert.Equal("hello /page-request", await viaPage.TextAsync());
            lock (up.Log)
            {
                Assert.DoesNotContain("auth=bad", up.Log);
                Assert.True(up.Log.Count(l => l == "connect name origin.test:8080") >= 2, string.Join(" | ", up.Log));
            }
        }
        finally { await browser.CloseAsync(); }
    }
}
