using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// ServeMultiplexAsync: per-connection identities, routing, limits, origin guard, idle close,
/// forwarded-host URLs and the socket-level WebSocket relay — against fake browsers, so every
/// behaviour is observable without a binary. Ports node test/parity-multiplex.test.ts and
/// python tests/test_parity_multiplex.py.
/// </summary>
public class MultiplexTests : IAsyncLifetime
{
    private readonly List<Func<Task>> _cleanups = new();

    public Task InitializeAsync() => Task.CompletedTask;

    public async Task DisposeAsync()
    {
        for (var i = _cleanups.Count - 1; i >= 0; i--) { try { await _cleanups[i](); } catch { } }
    }

    private static ConnectionIdentity P(string q) => Multiplex.ParseConnectionIdentity(q);

    // ── ParseConnectionIdentity ──────────────────────────────────────────────

    [Fact]
    public void NoParametersIsTheSharedDefaultIdentity()
    {
        var r = P("");
        Assert.Equal("default", r.Id);
        Assert.Null(r.Seed);
        Assert.Empty(r.Options);
        Assert.Empty(r.Params);
    }

    [Fact]
    public void ASeedIsTheIdentityAndTypedParametersBecomeLaunchOptions()
    {
        var r = P("fingerprint=acct-1&platform=windows&hardware-concurrency=8&device-pixel-ratio=1.25&fingerprint-noise=false&locale=de-DE&timezone=Europe/Berlin&geoip=true");
        Assert.Equal("acct-1", r.Id);
        Assert.Equal("acct-1", r.Seed);
        Assert.Equal(new Dictionary<string, object>
        {
            ["fingerprint"] = "acct-1", ["platform"] = "windows", ["hardwareConcurrency"] = 8, ["devicePixelRatio"] = 1.25,
            ["fingerprintNoise"] = false, ["acceptLanguage"] = "de-DE", ["timezone"] = "Europe/Berlin", ["geoip"] = true,
        }, r.Options.ToDictionary(k => k.Key, k => k.Value));
    }

    [Fact]
    public void ParsesAProxyUrlAndRedactsItsCredentialsFromTheStatusParams()
    {
        var r = P("fingerprint=a&proxy=" + Uri.EscapeDataString("socks5://u:pw@h.test:1080"));
        var proxy = Assert.IsType<ProxyOptions>(r.Options["proxy"]);
        Assert.Equal("socks5://h.test:1080", proxy.Server);
        Assert.Equal("u", proxy.Username);
        Assert.Equal("pw", proxy.Password);
        Assert.Equal("socks5://h.test:1080 (with credentials)", r.Params["proxy"]);
        Assert.DoesNotContain("pw", string.Join(" ", r.Params.Values));
    }

    [Theory]
    [InlineData("../../etc")]
    [InlineData("a/b")]
    [InlineData("a\\b")]
    [InlineData("_p123")]
    [InlineData("")]
    [InlineData("has space")]
    [InlineData("default")]
    public void RejectsUnsafeOrReservedSeeds(string bad)
    {
        var e = Assert.Throws<MultiplexRequestException>(() => P("fingerprint=" + Uri.EscapeDataString(bad)));
        Assert.Equal(400, e.Status);
    }

    [Fact]
    public void SeedPattern()
    {
        Assert.Throws<MultiplexRequestException>(() => P("fingerprint=" + new string('x', 129)));
        Assert.Matches(Multiplex.SeedPattern, "acct.1_x-Y");
        Assert.Matches(Multiplex.SeedPattern, new string('x', 128));
    }

    [Fact]
    public void RejectsUnknownParametersInsteadOfGuessing()
    {
        Assert.Matches("unknown query parameter 'gpu-vendr'", Assert.Throws<MultiplexRequestException>(() => P("fingerprint=a&gpu-vendr=x")).Message);
        Assert.Matches("must be a number", Assert.Throws<MultiplexRequestException>(() => P("hardware-concurrency=eight")).Message);
        Assert.Matches("true or false", Assert.Throws<MultiplexRequestException>(() => P("geoip=maybe")).Message);
        // .NET only: the count options are ints.
        Assert.Matches("whole number", Assert.Throws<MultiplexRequestException>(() => P("hardware-concurrency=2.5")).Message);
        Assert.Matches("proxy is empty", Assert.Throws<MultiplexRequestException>(() => P("proxy=")).Message);
    }

    [Fact]
    public void ParameterOnlyIdentitiesAreStableAndOrderIndependent()
    {
        var a = P("platform=macos&brand=Edge");
        var b = P("brand=Edge&platform=macos");
        Assert.Matches("^_p[0-9a-f]{16}$", a.Id);
        Assert.Equal(a.Id, b.Id);
        Assert.NotEqual(a.Id, P("platform=linux").Id);
    }

    [Fact]
    public void ParameterOnlyIdsMatchTheNodeSdk()
    {
        // Computed with the Node SDK's canonical form (JSON.stringify of the sorted [key, value] pairs).
        Assert.Equal("_p9bebdd7bbfdbb6c2", P("platform=macos&brand=Edge").Id);
        Assert.Equal("_p07a2e37476a5fbf5", P("hardware-concurrency=8&device-pixel-ratio=1.25&fingerprint-noise=false&locale=de-DE"
            + "&timezone=Europe/Berlin&proxy=" + Uri.EscapeDataString("socks5://u:p+w@h.test:1080")).Id);
    }

    [Fact]
    public void ExposesTheEngineExtraOptionsAsKebabCaseParameters()
    {
        Assert.Equal("allowThirdPartyCookies", Multiplex.QueryParams["allow-third-party-cookies"]);
        Assert.Equal("transparentProxy", Multiplex.QueryParams["transparent-proxy"]);
        Assert.Equal("fingerprintVoices", Multiplex.QueryParams["fingerprint-voices"]);
        Assert.Equal("socks5Udp", Multiplex.QueryParams["socks5-udp"]);
        // Every parameter names a settable .NET launch option.
        foreach (var key in Multiplex.QueryParams.Values.Concat(new[] { "fingerprint", "timezone", "acceptLanguage", "geoip", "proxy" }))
            Assert.True(Multiplex.OptionProperty(key).CanWrite, key);
    }

    // ── URL + request guards ─────────────────────────────────────────────────

    private static Dictionary<string, string> H(params (string, string)[] kv) => kv.ToDictionary(p => p.Item1, p => p.Item2);

    [Fact]
    public void HonoursXForwardedHostAndProtoForThePublicWsBase()
    {
        Assert.Equal("ws://127.0.0.1:9222", Multiplex.PublicWsBase(H(("host", "127.0.0.1:9222")), "x"));
        Assert.Equal("wss://cdp.example.com", Multiplex.PublicWsBase(
            H(("host", "internal:9222"), ("x-forwarded-host", "cdp.example.com, proxy2"), ("x-forwarded-proto", "https")), "x"));
        Assert.Equal("ws://fallback:1", Multiplex.PublicWsBase(H(), "fallback:1"));
    }

    [Fact]
    public void RewritesAChildsBrowserUrlThroughTheMultiplexer()
    {
        const string child = "ws://127.0.0.1:41234/devtools/browser/abc";
        Assert.Equal("ws://127.0.0.1:9222/devtools/browser/abc", Multiplex.RewriteWsUrl(child, "ws://127.0.0.1:9222", "default"));
        Assert.Equal("wss://cdp.example.com/fingerprint/acct-1/devtools/browser/abc", Multiplex.RewriteWsUrl(child, "wss://cdp.example.com", "acct-1"));
        Assert.Equal("not-a-ws-url", Multiplex.RewriteWsUrl("not-a-ws-url", "ws://x", "a"));
    }

    [Fact]
    public void AllowsNonBrowserAndLoopbackOriginsRefusesOthersUnlessListed()
    {
        Assert.True(Multiplex.OriginAllowed(null));
        Assert.True(Multiplex.OriginAllowed("http://localhost:3000"));
        Assert.True(Multiplex.OriginAllowed("http://127.0.0.1"));
        Assert.True(Multiplex.OriginAllowed("http://[::1]:5173"));
        Assert.False(Multiplex.OriginAllowed("null"));
        Assert.False(Multiplex.OriginAllowed("https://evil.example"));
        Assert.True(Multiplex.OriginAllowed("https://tool.example", new[] { "https://tool.example" }));
    }

    [Fact]
    public void HostAllowedTakesIpLiteralsLocalhostAndListedNames()
    {
        foreach (var ok in new string?[] { null, "127.0.0.1:9222", "localhost", "LOCALHOST:1", "[::1]:9222", "10.0.0.5", "::1" })
            Assert.True(Multiplex.HostAllowed(ok), ok);
        foreach (var bad in new[] { "evil.example", "evil.example:9222", "127.0.0.1.nip.io", "" })
            Assert.False(Multiplex.HostAllowed(bad), bad);
        Assert.True(Multiplex.HostAllowed("cdp.example.com:443", new[] { "CDP.example.com" }));
    }

    [Fact]
    public void RequestRefusalCoversSecFetchSiteOriginAndHost()
    {
        Assert.Null(Multiplex.RequestRefusal(H(("host", "127.0.0.1"))));
        Assert.NotNull(Multiplex.RequestRefusal(H(("host", "127.0.0.1"), ("sec-fetch-site", "cross-site"))));
        Assert.NotNull(Multiplex.RequestRefusal(H(("host", "127.0.0.1"), ("sec-fetch-site", "same-site"))));
        Assert.Null(Multiplex.RequestRefusal(H(("host", "127.0.0.1"), ("sec-fetch-site", "none"))));
        Assert.Equal("Forbidden origin", Multiplex.RequestRefusal(H(("host", "127.0.0.1"), ("origin", "https://evil.example"))));
        Assert.Equal("Forbidden host", Multiplex.RequestRefusal(H(("host", "rebind.example:9222"))));
        // An allowed Origin may make a cross-site request (as in the Node SDK).
        Assert.Null(Multiplex.RequestRefusal(H(("host", "127.0.0.1"), ("origin", "https://tool.example"), ("sec-fetch-site", "cross-site")),
            new[] { "https://tool.example" }));
    }

    [Fact]
    public void IdleTimeoutComesFromTheEnvironment()
    {
        using var sb = new Sandbox();
        sb.Env("CLEARCOTE_SERVE_IDLE_TIMEOUT", " 300 ");
        Assert.Equal(300, Multiplex.IdleTimeoutFromEnv());
        sb.Env("CLEARCOTE_SERVE_IDLE_TIMEOUT", "-1");
        Assert.Equal(0, Multiplex.IdleTimeoutFromEnv());
        sb.Env("CLEARCOTE_SERVE_IDLE_TIMEOUT", "soon");
        Assert.Equal(0, Multiplex.IdleTimeoutFromEnv());
        sb.Env("CLEARCOTE_SERVE_IDLE_TIMEOUT", null);
        Assert.Equal(0, Multiplex.IdleTimeoutFromEnv());
    }

    // ── integration against fake browsers ────────────────────────────────────

    private sealed class FakeBrowser : IBrowserHandle
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly ConcurrentDictionary<Socket, byte> _sockets = new();
        public readonly ServeOptions Options;
        public readonly ConcurrentQueue<(string Path, Dictionary<string, string> Headers)> Upgrades = new();
        public volatile bool Closed;
        public Func<Task>? BeforeClose;
        public Action? AfterClose;

        public FakeBrowser(ServeOptions options)
        {
            Options = options;
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _ = Task.Run(AcceptAsync);
        }

        public int Port { get; }
        public int? Pid => 4242;

        private async Task AcceptAsync()
        {
            while (true)
            {
                Socket s;
                try { s = await _listener.AcceptSocketAsync(); } catch { return; }
                _sockets[s] = 0;
                _ = Task.Run(() => ServeAsync(s));
            }
        }

        private async Task ServeAsync(Socket s)
        {
            using var stream = new NetworkStream(s, ownsSocket: true);
            try
            {
                var head = await ReadHead(stream);
                if (head is null) return;
                var lines = head.Split("\r\n");
                var path = lines[0].Split(' ')[1];
                var headers = lines.Skip(1).Where(l => l.Contains(':'))
                    .GroupBy(l => l[..l.IndexOf(':')].Trim().ToLowerInvariant())
                    .ToDictionary(g => g.Key, g => g.First()[(g.First().IndexOf(':') + 1)..].Trim());
                if (headers.ContainsKey("upgrade"))
                {
                    Upgrades.Enqueue((path, headers));
                    await stream.WriteAsync("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"u8.ToArray());
                    var buf = new byte[4096];
                    int n;
                    while ((n = await stream.ReadAsync(buf)) > 0)
                        await stream.WriteAsync("echo:"u8.ToArray().Concat(buf.Take(n)).ToArray());
                    return;
                }
                string? body = path switch
                {
                    "/json/version" => $"{{\"Browser\":\"Chrome/152\",\"webSocketDebuggerUrl\":\"ws://127.0.0.1:{Port}/devtools/browser/uuid-1\"}}",
                    "/json/list" => $"[{{\"id\":\"p1\",\"webSocketDebuggerUrl\":\"ws://127.0.0.1:{Port}/devtools/page/p1\",\"devtoolsFrontendUrl\":\"/devtools/inspector.html?ws=127.0.0.1:{Port}/devtools/page/p1\"}}]",
                    _ => null,
                };
                var data = Encoding.UTF8.GetBytes(body ?? "");
                await stream.WriteAsync(Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 {(body is null ? "404 Not Found" : "200 OK")}\r\nContent-Type: application/json\r\nContent-Length: {data.Length}\r\n\r\n"));
                await stream.WriteAsync(data);
                // Like Chrome: the connection stays open after the answer, until the client ends it.
                var sink = new byte[1024];
                while (await stream.ReadAsync(sink) > 0) { }
            }
            catch { }
            finally { _sockets.TryRemove(s, out _); }
        }

        public async Task CloseAsync()
        {
            if (BeforeClose is not null) await BeforeClose();
            Closed = true;
            _listener.Stop();
            foreach (var s in _sockets.Keys) { try { s.Dispose(); } catch { } }
            AfterClose?.Invoke();
        }
    }

    private static async Task<string?> ReadHead(Stream stream)
    {
        var sb = new StringBuilder();
        var one = new byte[1];
        while (!sb.ToString().EndsWith("\r\n\r\n"))
        {
            if (await stream.ReadAsync(one) == 0) return null;
            sb.Append((char)one[0]);
        }
        return sb.ToString(0, sb.Length - 4);
    }

    private async Task<(MultiplexServer Mux, List<FakeBrowser> Browsers)> StartMux(Action<MultiplexOptions>? extra = null)
    {
        var browsers = new List<FakeBrowser>();
        var o = new MultiplexOptions
        {
            Port = 0,
            Quiet = true,
            StartBrowser = so =>
            {
                var b = new FakeBrowser(so);
                lock (browsers) browsers.Add(b);
                return Task.FromResult<IBrowserHandle>(b);
            },
        };
        extra?.Invoke(o);
        var mux = await Clearcote.ServeMultiplexAsync(o);
        _cleanups.Add(mux.CloseAsync);
        return (mux, browsers);
    }

    /// One raw HTTP request (any Host header) and its status + JSON body.
    private static async Task<(int Status, JsonNode? Body)> Http(int port, string path, string method = "GET",
        Dictionary<string, string>? headers = null, string? host = null)
    {
        using var c = new TcpClient();
        await c.ConnectAsync(IPAddress.Loopback, port);
        var s = c.GetStream();
        var req = new StringBuilder($"{method} {path} HTTP/1.1\r\nHost: {host ?? $"127.0.0.1:{port}"}\r\n");
        foreach (var (k, v) in headers ?? new()) req.Append($"{k}: {v}\r\n");
        if (method == "POST") req.Append("Content-Length: 0\r\n");
        req.Append("Connection: close\r\n\r\n");
        await s.WriteAsync(Encoding.ASCII.GetBytes(req.ToString()));
        using var ms = new MemoryStream();
        await s.CopyToAsync(ms).WaitAsync(TimeSpan.FromSeconds(15));
        var text = Encoding.UTF8.GetString(ms.ToArray());
        var status = int.Parse(text.Split(' ')[1]);
        var body = text[(text.IndexOf("\r\n\r\n", StringComparison.Ordinal) + 4)..];
        return (status, body.Length == 0 ? null : JsonNode.Parse(body));
    }

    /// Raw WebSocket upgrade + one echo round-trip: the status line and the echoed payload.
    private static async Task<(string Status, string? Echo)> WsRoundTrip(int port, string path, string? origin = null, string? host = null)
    {
        using var c = new TcpClient();
        await c.ConnectAsync(IPAddress.Loopback, port);
        var s = c.GetStream();
        await s.WriteAsync(Encoding.ASCII.GetBytes(
            $"GET {path} HTTP/1.1\r\nHost: {host ?? $"127.0.0.1:{port}"}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n" +
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n" +
            (origin is null ? "" : $"Origin: {origin}\r\n") + "\r\n"));
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var buf = new StringBuilder();
        var chunk = new byte[4096];
        var sent = false;
        while (true)
        {
            var n = await s.ReadAsync(chunk, cts.Token);
            if (n == 0) return (buf.ToString().Split("\r\n")[0], null);
            buf.Append(Encoding.UTF8.GetString(chunk, 0, n));
            var status = buf.ToString().Split("\r\n")[0];
            if (!status.Contains("101")) return (status, null);
            if (!sent && buf.ToString().Contains("\r\n\r\n")) { sent = true; await s.WriteAsync("ping"u8.ToArray()); continue; }
            if (buf.ToString().Contains("echo:ping")) return (status, "ping");
        }
    }

    [Fact]
    public async Task RoutesJsonVersionPerIdentityReusesASeedAndRewritesTheWsUrl()
    {
        var (mux, browsers) = await StartMux();
        var a1 = await Http(mux.Port, "/json/version?fingerprint=acct-1&platform=windows");
        var a2 = await Http(mux.Port, "/json/version/?platform=windows&fingerprint=acct-1");
        var b = await Http(mux.Port, "/json/version?fingerprint=acct-2");
        var d = await Http(mux.Port, "/json/version");
        Assert.Equal(3, browsers.Count);
        var o = browsers[0].Options;
        Assert.Equal(("acct-1", "windows", "127.0.0.1", true, (int?)null), (o.Fingerprint, o.Platform, o.Host, o.Quiet, o.Port));
        Assert.Equal($"ws://127.0.0.1:{mux.Port}/fingerprint/acct-1/devtools/browser/uuid-1", (string?)a1.Body!["webSocketDebuggerUrl"]);
        Assert.Equal((string?)a1.Body!["webSocketDebuggerUrl"], (string?)a2.Body!["webSocketDebuggerUrl"]);
        Assert.Contains("/fingerprint/acct-2/devtools/browser/", (string?)b.Body!["webSocketDebuggerUrl"]);
        Assert.Equal($"ws://127.0.0.1:{mux.Port}/devtools/browser/uuid-1", (string?)d.Body!["webSocketDebuggerUrl"]);
        Assert.Equal("Chrome/152", (string?)d.Body!["Browser"]);
        // Microsoft.Playwright 1.49's discovery URL: "/json/version/" appended to the endpoint string.
        var legacy = await Http(mux.Port, "/?platform=windows&fingerprint=acct-1/json/version/");
        Assert.Equal((string?)a1.Body!["webSocketDebuggerUrl"], (string?)legacy.Body!["webSocketDebuggerUrl"]);
        Assert.Equal("p1", (string?)(await Http(mux.Port, "/?fingerprint=acct-1&platform=windows/json/list"))!.Body![0]!["id"]);
        Assert.Equal("ok", (string?)(await Http(mux.Port, "/?fingerprint=acct-1")).Body!["status"]);
        Assert.Equal(3, browsers.Count);
    }

    [Fact]
    public async Task BaseOptionsReachEveryBrowserAndTheConnectionsOverrideThem()
    {
        var (mux, browsers) = await StartMux(o =>
        {
            o.Headless = false; o.Platform = "linux"; o.ExecutablePath = "/x/chrome"; o.ReadyTimeoutMs = 1234;
            o.WindowSize = new() { Width = 1000, Height = 700 };
        });
        await Http(mux.Port, "/json/version?fingerprint=s1&platform=windows&hardware-concurrency=12&socks5-udp=true");
        await Http(mux.Port, "/json/version");
        var (a, d) = (browsers[0].Options, browsers[1].Options);
        Assert.Equal((false, "windows", "/x/chrome", 1234, 12, true), (a.Headless, a.Platform, a.ExecutablePath, a.ReadyTimeoutMs, a.HardwareConcurrency, a.Socks5Udp));
        Assert.Equal(1000, a.WindowSize!.Width);
        Assert.Equal(("linux", (string?)null, (string?)null), (d.Platform, d.Fingerprint, d.UserDataDir));
    }

    [Fact]
    public async Task UsesXForwardedHostAndProtoForTheUrlsItHandsOut()
    {
        var (mux, _) = await StartMux();
        var fwd = new Dictionary<string, string> { ["X-Forwarded-Host"] = "cdp.example.com", ["X-Forwarded-Proto"] = "https" };
        var r = await Http(mux.Port, "/json/version?fingerprint=acct-1", headers: fwd);
        Assert.Equal("wss://cdp.example.com/fingerprint/acct-1/devtools/browser/uuid-1", (string?)r.Body!["webSocketDebuggerUrl"]);
        var list = await Http(mux.Port, "/json/list?fingerprint=acct-1", headers: fwd);
        Assert.Equal("wss://cdp.example.com/fingerprint/acct-1/devtools/page/p1", (string?)list.Body![0]!["webSocketDebuggerUrl"]);
        Assert.Equal("/devtools/inspector.html?ws=cdp.example.com/fingerprint/acct-1/devtools/page/p1", (string?)list.Body![0]!["devtoolsFrontendUrl"]);
        var json = await Http(mux.Port, "/json?fingerprint=acct-1", headers: fwd);
        Assert.Equal("p1", (string?)json.Body![0]!["id"]);
    }

    [Fact]
    public async Task Returns400ForABadSeedAnd429PastMaxBrowsers()
    {
        var (mux, browsers) = await StartMux(o => o.MaxBrowsers = 2);
        Assert.Equal(400, (await Http(mux.Port, "/json/version?fingerprint=..%2F..%2Fetc")).Status);
        Assert.Equal(200, (await Http(mux.Port, "/json/version?fingerprint=one")).Status);
        Assert.Equal(200, (await Http(mux.Port, "/json/version?fingerprint=two")).Status);
        var third = await Http(mux.Port, "/json/version?fingerprint=three");
        Assert.Equal(429, third.Status);
        Assert.Contains("maxBrowsers (2)", (string?)third.Body!["error"]);
        Assert.Equal(2, browsers.Count);
    }

    [Fact]
    public async Task RelaysAWebSocketToTheRightBrowserStrippingOriginAndFixingHost()
    {
        var (mux, browsers) = await StartMux();
        await Http(mux.Port, "/json/version?fingerprint=acct-1");
        var r = await WsRoundTrip(mux.Port, "/fingerprint/acct-1/devtools/browser/uuid-1", "http://localhost:5173");
        Assert.Contains("101", r.Status);
        Assert.Equal("ping", r.Echo);
        Assert.True(browsers[0].Upgrades.TryPeek(out var up));
        Assert.Equal("/devtools/browser/uuid-1", up.Path);
        Assert.False(up.Headers.ContainsKey("origin"));
        Assert.Equal($"127.0.0.1:{browsers[0].Port}", up.Headers["host"]);
        Assert.Equal("13", up.Headers["sec-websocket-version"]);
    }

    [Fact]
    public async Task RefusesAWebSocketFromAForeignBrowserOrigin()
    {
        var (mux, browsers) = await StartMux();
        await Http(mux.Port, "/json/version?fingerprint=acct-1");
        var r = await WsRoundTrip(mux.Port, "/fingerprint/acct-1/devtools/browser/uuid-1", "https://evil.example");
        Assert.Contains("403", r.Status);
        Assert.Empty(browsers[0].Upgrades);
    }

    [Fact]
    public async Task TheWebSocketRouteNeverStartsABrowser()
    {
        var (mux, browsers) = await StartMux();
        Assert.Contains("404", (await WsRoundTrip(mux.Port, "/fingerprint/direct-seed/devtools/browser/uuid-1")).Status);
        Assert.Contains("404", (await WsRoundTrip(mux.Port, "/fingerprint/_p0123456789abcdef/devtools/browser/x")).Status);
        Assert.Contains("404", (await WsRoundTrip(mux.Port, "/devtools/browser/uuid-1")).Status);
        Assert.Contains("404", (await WsRoundTrip(mux.Port, "/elsewhere")).Status);
        Assert.Empty(browsers);
    }

    [Fact]
    public async Task Answers409WhenARunningIdentityIsRequestedWithDifferentParameters()
    {
        var (mux, browsers) = await StartMux();
        var proxy = Uri.EscapeDataString("socks5://u:p@p.test:1080");
        Assert.Equal(200, (await Http(mux.Port, $"/json/version?fingerprint=acct&proxy={proxy}")).Status);
        var bare = await Http(mux.Port, "/json/version?fingerprint=acct");
        Assert.Equal(409, bare.Status);
        Assert.Contains("different parameters", (string?)bare.Body!["error"]);
        var otherCreds = await Http(mux.Port, $"/json/version?fingerprint=acct&proxy={Uri.EscapeDataString("socks5://u:other@p.test:1080")}");
        Assert.Equal(409, otherCreds.Status);
        Assert.DoesNotContain("other", otherCreds.Body!.ToJsonString());
        Assert.Equal(200, (await Http(mux.Port, $"/json/version?proxy={proxy}&fingerprint=acct")).Status);
        Assert.Single(browsers);
        Assert.Equal("socks5://p.test:1080", browsers[0].Options.Proxy!.Server);
        Assert.Equal("p", browsers[0].Options.Proxy!.Password);
    }

    [Fact]
    public async Task ReservesFingerprintDefaultForTheNoParameterBrowser()
    {
        var (mux, browsers) = await StartMux();
        Assert.Equal(400, (await Http(mux.Port, "/json/version?fingerprint=default")).Status);
        Assert.Empty(browsers);
    }

    [Fact]
    public async Task RefusesWhatAWebPageCouldSend()
    {
        var (mux, browsers) = await StartMux(o => o.AllowHosts = new[] { "cdp.example.com" });
        const string u = "/json/version?fingerprint=web-1";
        Assert.Equal(403, (await Http(mux.Port, u, headers: new() { ["Sec-Fetch-Site"] = "cross-site", ["Sec-Fetch-Mode"] = "no-cors" })).Status);
        Assert.Equal(403, (await Http(mux.Port, u, headers: new() { ["Origin"] = "https://evil.example" })).Status);
        Assert.Equal(403, (await Http(mux.Port, u, host: "evil.example:9222")).Status);
        Assert.Equal(403, (await Http(mux.Port, "/", host: "rebind.attacker.test")).Status);
        Assert.Equal(403, (await Http(mux.Port, "/fingerprint/web-1/close", "POST", new() { ["Sec-Fetch-Site"] = "cross-site" })).Status);
        Assert.Empty(browsers);
        // loopback names, IP literals and AllowHosts still work
        Assert.Equal(200, (await Http(mux.Port, "/", host: $"localhost:{mux.Port}")).Status);
        Assert.Equal(200, (await Http(mux.Port, "/", host: "cdp.example.com")).Status);
        Assert.Equal(200, (await Http(mux.Port, u, headers: new() { ["Sec-Fetch-Site"] = "same-origin" })).Status);
        Assert.Equal(200, (await Http(mux.Port, "/", headers: new() { ["Origin"] = "http://localhost:5173", ["Sec-Fetch-Site"] = "same-origin" })).Status);
        Assert.Contains("403", (await WsRoundTrip(mux.Port, "/fingerprint/web-1/devtools/browser/uuid-1", host: "evil.example")).Status);
    }

    [Fact]
    public async Task SurvivesClientsThatResetTheirWebSocketImmediately()
    {
        var (mux, _) = await StartMux();
        await Http(mux.Port, "/json/version?fingerprint=rst");
        for (var i = 0; i < 20; i++)
        {
            var s = new Socket(SocketType.Stream, ProtocolType.Tcp);
            await s.ConnectAsync(IPAddress.Loopback, mux.Port);
            await s.SendAsync(Encoding.ASCII.GetBytes("GET /fingerprint/rst/devtools/browser/uuid-1 HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"));
            s.LingerState = new LingerOption(true, 0);   // close = RST
            s.Close();
        }
        await Task.Delay(500);
        var st = await Http(mux.Port, "/");
        Assert.Equal(200, st.Status);
        Assert.Equal(0, (int)st.Body!["processes"]![0]!["connections"]!);
        Assert.Equal(0, mux.Status().Processes[0].Connections);
    }

    private static MultiplexOptions Gated(TaskCompletionSource gate, List<FakeBrowser> made) => new()
    {
        Port = 0,
        Quiet = true,
        StartBrowser = async so =>
        {
            await gate.Task;
            var b = new FakeBrowser(so);
            lock (made) made.Add(b);
            return b;
        },
    };

    [Fact]
    public async Task CloseDuringALaunchClosesTheBrowserThatLaunchProduces()
    {
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var made = new List<FakeBrowser>();
        var mux = await Clearcote.ServeMultiplexAsync(Gated(gate, made));
        var req = Http(mux.Port, "/json/version?fingerprint=slow");
        await Task.Delay(200);
        var closed = mux.CloseAsync();
        gate.SetResult();
        await closed;
        Assert.Single(made);
        Assert.True(made[0].Closed);
        Assert.Equal(0, mux.Status().Active);
        try { await req; } catch { }   // the socket was ended by the close
    }

    [Fact]
    public async Task CloseIdentityOnALaunchingIdClosesTheBrowserAsSoonAsItIsUp()
    {
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var made = new List<FakeBrowser>();
        var mux = await Clearcote.ServeMultiplexAsync(Gated(gate, made));
        _cleanups.Add(mux.CloseAsync);
        var req = Http(mux.Port, "/json/version?fingerprint=slow");
        await Task.Delay(200);
        var closing = mux.CloseIdentityAsync("slow");
        gate.SetResult();
        Assert.True(await closing);
        var r = await req;
        Assert.Equal(503, r.Status);
        Assert.Contains("closed while starting", (string?)r.Body!["error"]);
        Assert.True(made[0].Closed);
        Assert.Equal(0, mux.Status().Active);
    }

    [Fact]
    public async Task ARelaunchWaitsForThePreviousBrowserOfThatIdToFinishClosing()
    {
        var slowClose = true;
        var events = new ConcurrentQueue<string>();
        var mux = await Clearcote.ServeMultiplexAsync(new MultiplexOptions
        {
            Port = 0,
            Quiet = true,
            StartBrowser = so =>
            {
                var b = new FakeBrowser(so);
                events.Enqueue("start");
                b.BeforeClose = async () => { if (Volatile.Read(ref slowClose)) await Task.Delay(300); };
                b.AfterClose = () => events.Enqueue("closed");
                return Task.FromResult<IBrowserHandle>(b);
            },
        });
        _cleanups.Add(mux.CloseAsync);
        await Http(mux.Port, "/json/version?fingerprint=same");
        var closing = mux.CloseIdentityAsync("same");
        await Task.Delay(20);
        Volatile.Write(ref slowClose, false);
        await Http(mux.Port, "/json/version?fingerprint=same");
        await closing;
        Assert.Equal(new[] { "start", "closed", "start" }, events.ToArray());
    }

    [Fact]
    public async Task GetSlashReportsStatusAndPostCloseStopsThatBrowserIdempotently()
    {
        var (mux, browsers) = await StartMux();
        await Http(mux.Port, $"/json/version?fingerprint=acct-1&proxy={Uri.EscapeDataString("http://u:secret@p.test:8080")}");
        var st = await Http(mux.Port, "/");
        var body = st.Body!;
        Assert.Equal(("ok", 1, 16, 0.0), ((string?)body["status"], (int)body["active"]!, (int)body["maxBrowsers"]!, (double)body["idleTimeoutSec"]!));
        var p0 = body["processes"]![0]!;
        Assert.Equal(("acct-1", "acct-1", 4242, 0), ((string?)p0["id"], (string?)p0["seed"], (int)p0["pid"]!, (int)p0["connections"]!));
        Assert.Equal("http://p.test:8080 (with credentials)", (string?)p0["params"]!["proxy"]);
        Assert.Matches(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", (string?)p0["startedAt"]);
        Assert.NotNull(p0["idleSince"]);
        Assert.DoesNotContain("secret", body.ToJsonString());
        var c1 = await Http(mux.Port, "/fingerprint/acct-1/close", "POST");
        Assert.Equal("{\"id\":\"acct-1\",\"terminated\":true}", c1.Body!.ToJsonString());
        Assert.True(browsers[0].Closed);
        var c2 = await Http(mux.Port, "/fingerprint/acct-1/close", "POST");
        Assert.Equal("{\"id\":\"acct-1\",\"terminated\":false}", c2.Body!.ToJsonString());
        Assert.Equal(405, (await Http(mux.Port, "/fingerprint/acct-1/close")).Status);
        Assert.Equal(404, (await Http(mux.Port, "/nowhere")).Status);
    }

    [Fact]
    public async Task ClosesAnIdentityAfterItsLastConnectionWhenIdleTimeoutSecIsSet()
    {
        var (mux, browsers) = await StartMux(o => o.IdleTimeoutSec = 0.3);
        await Http(mux.Port, "/json/version?fingerprint=idle-1");
        Assert.Contains("101", (await WsRoundTrip(mux.Port, "/fingerprint/idle-1/devtools/browser/uuid-1")).Status);
        await Task.Delay(1200);
        Assert.True(browsers[0].Closed);
        Assert.Equal(0, (int)(await Http(mux.Port, "/")).Body!["active"]!);
    }

    [Fact]
    public async Task IdleTimeoutIsReadFromTheEnvironment()
    {
        using var sb = new Sandbox();
        sb.Env("CLEARCOTE_SERVE_IDLE_TIMEOUT", "0.3");
        var (mux, browsers) = await StartMux();
        Assert.Equal(0.3, mux.Status().IdleTimeoutSec);
        await Http(mux.Port, "/json/version?fingerprint=idle-env");
        await Task.Delay(1200);
        Assert.True(browsers[0].Closed);
    }

    [Fact]
    public async Task NamesPersistentProfileDirectoriesByAHashNeverByTheSeed()
    {
        var (mux, browsers) = await StartMux(o => o.DataDir = "/data/profiles");
        await Http(mux.Port, "/json/version?fingerprint=acct-1");
        var udd = browsers[0].Options.UserDataDir!.Replace('\\', '/');
        Assert.Equal("/data/profiles/5e174c5b93e71d81e018bce1", udd);   // sha256("clearcote:acct-1")[..24], as in Node
        Assert.DoesNotContain("acct-1", udd);
    }

    [Fact]
    public async Task ALaunchThatFailsAnswers502AndLeavesNothingRunning()
    {
        var mux = await Clearcote.ServeMultiplexAsync(new MultiplexOptions
        {
            Port = 0,
            Quiet = true,
            StartBrowser = _ => throw new InvalidOperationException("no binary here"),
        });
        _cleanups.Add(mux.CloseAsync);
        var r = await Http(mux.Port, "/json/version?fingerprint=x");
        Assert.Equal(502, r.Status);
        Assert.Equal("no binary here", (string?)r.Body!["error"]);
        Assert.Equal(0, mux.Status().Active);
    }

    /// LIVE (skipped unless CLEARCOTE_LIVE_ENGINE names a chrome binary): Playwright connects through the
    /// multiplexer to two identities, each its own browser with its own persona, and close ends both.
    [Fact]
    public async Task LivePlaywrightConnectsToTwoIdentitiesThroughTheMultiplexer()
    {
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe)) return;
        using var sb = new Sandbox();
        sb.TempHome();
        sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_NO_WARN", "1");
        var mux = await Clearcote.ServeMultiplexAsync(new MultiplexOptions { ExecutablePath = exe, Port = 0, Quiet = true });
        try
        {
            var first = await Http(mux.Port, "/json/version?fingerprint=live-a&platform=windows");
            Assert.True(first.Status == 200, first.Body?.ToJsonString());
            var pw = await Clearcote.PlaywrightInstanceAsync();
            var a = await pw.Chromium.ConnectOverCDPAsync($"{mux.Url}?fingerprint=live-a&platform=windows");
            var b = await pw.Chromium.ConnectOverCDPAsync($"{mux.Url}?fingerprint=live-b&platform=linux");
            var pa = a.Contexts.Count > 0 ? await a.Contexts[0].NewPageAsync() : await a.NewPageAsync();
            var pb = b.Contexts.Count > 0 ? await b.Contexts[0].NewPageAsync() : await b.NewPageAsync();
            Assert.Equal("Win32", await pa.EvaluateAsync<string>("navigator.platform"));
            Assert.StartsWith("Linux", await pb.EvaluateAsync<string>("navigator.platform"));
            var st = mux.Status();
            Assert.Equal(2, st.Active);
            Assert.All(st.Processes, p => Assert.Equal(1, p.Connections));
            Assert.NotEqual(st.Processes[0].Port, st.Processes[1].Port);
            await a.CloseAsync();
            await b.CloseAsync();
        }
        finally { await mux.CloseAsync(); }
        Assert.Equal(0, mux.Status().Active);
    }

    [Fact]
    public async Task UrlAndHostReflectTheBind()
    {
        var (mux, _) = await StartMux();
        Assert.Equal("127.0.0.1", mux.Host);
        Assert.Equal($"http://127.0.0.1:{mux.Port}", mux.Url);
        Assert.NotEqual(0, mux.Port);
        await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.ServeMultiplexAsync(new MultiplexOptions { Port = 0, MaxBrowsers = 0 }));
    }
}
