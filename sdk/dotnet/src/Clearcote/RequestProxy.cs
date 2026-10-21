using System.Collections.Concurrent;
using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;

namespace Clearcote;

/// <summary>
/// A local forwarder that sends Playwright's request API (<c>context.APIRequest</c>, <c>page.APIRequest</c>)
/// through an authenticated proxy whose credentials went to the engine's own switches.
/// </summary>
/// <remarks>
/// <para>Playwright's request API does not go through the browser. The driver sends it with Node's own HTTP
/// client, through the proxy in Playwright's launch/context options (playwright-core:
/// <c>proxy: this._context._options.proxy || this._context._browser.options.proxy</c>) and never through the
/// browser's --proxy-server. A proxy WITH credentials goes to the engine's own switches
/// (<see cref="LaunchOpts.ResolveProxy"/>) and is removed from Playwright's options, so before this class those
/// requests left from this machine's own IP: measured 2026-10-21 on SDK 0.55.1 + r46, an authenticated proxy
/// logged the page's CONNECT and never saw context.request or page.request, both of which returned 200.</para>
/// <para>The fix keeps the credentials away from Playwright (credentials there make its driver turn on Fetch
/// interception and Network.setCacheDisabled for the whole context, the transport tell ResolveProxy exists to
/// avoid). Playwright gets a credential-less proxy on 127.0.0.1 instead; this forwarder adds the credentials
/// and tunnels to the real proxy (HTTP/HTTPS CONNECT, or SOCKS5 with RFC 1929 auth and remote name
/// resolution). The browser itself never uses it: Playwright puts its --proxy-server before the caller's
/// args, ours comes later in the same argv and Chromium keeps the last value of a switch; the same holds for
/// the --proxy-bypass-list it adds (<c>&lt;-loopback&gt;</c>), which <see cref="RequestProxyArgs"/> overrides.
/// Playwright's fetch always tunnels with CONNECT (HttpsProxyAgent), even for http:// URLs, so that is the one
/// method served. Mirrors sdk/node/src/reqproxy.ts and sdk/python/clearcote/_reqproxy.py.</para>
/// </remarks>
internal sealed class RequestProxy : IAsyncDisposable
{
    /// The real proxy the forwarder tunnels to. <c>Protocol</c> is "http", "https" or "socks5".
    internal sealed record Upstream(string Protocol, string Host, int Port, string Username, string Password);

    private const int HeadLimit = 16 * 1024;
    internal static TimeSpan HandshakeTimeout = TimeSpan.FromSeconds(30);

    private readonly TcpListener _listener;
    private readonly Upstream _upstream;
    private readonly CancellationTokenSource _stop = new();
    private readonly ConcurrentDictionary<IDisposable, byte> _open = new();
    private readonly Task _accepting;
    private readonly object _gate = new();
    private Task? _closing;

    /// <c>http://127.0.0.1:&lt;port&gt;</c>, plus the caller's bypass list so bypassed hosts go direct as in the
    /// browser. No credentials: the forwarder adds them.
    public ProxyOptions PlaywrightProxy { get; }

    /// The loopback port the forwarder listens on.
    public int Port { get; }

    private RequestProxy(TcpListener listener, Upstream upstream, string? bypass)
    {
        _listener = listener;
        _upstream = upstream;
        Port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var b = (bypass ?? string.Empty).Trim();
        PlaywrightProxy = new ProxyOptions { Server = $"http://127.0.0.1:{Port}", Bypass = b.Length > 0 ? b : null };
        _accepting = AcceptLoopAsync();
    }

    // ── what to forward ──────────────────────────────────────────────────────

    /// The upstream the forwarder needs, or null when nothing has to be forwarded: no proxy, a proxy with no
    /// credentials, or one ResolveProxy left with Playwright (it reaches the request API already).
    /// <paramref name="proxyArgs"/> is what ResolveProxy returned; credentials moved to the engine exactly when
    /// it holds a --proxy-server.
    internal static Upstream? RequestProxyUpstream(ProxyOptions? proxy, IEnumerable<string> proxyArgs)
    {
        if (proxy is null || !proxyArgs.Any(a => a.StartsWith("--proxy-server=", StringComparison.Ordinal))) return null;
        var (server, user, pass) = LaunchOpts.ProxyCredentials(proxy);
        if (user.Length == 0 && pass.Length == 0) return null;
        if (!Uri.TryCreate(server, UriKind.Absolute, out var url)) return null;
        var scheme = url.Scheme.ToLowerInvariant();
        string? protocol = scheme is "http" or "https" ? scheme : scheme is "socks5" or "socks5h" ? "socks5" : null;
        // socks4 has no password auth: nothing a forwarder can add
        if (protocol is null || string.IsNullOrEmpty(url.Host)) return null;
        var port = !url.IsDefaultPort && url.Port > 0 ? url.Port : protocol == "https" ? 443 : protocol == "socks5" ? 1080 : 80;
        var host = url.Host.StartsWith('[') && url.Host.EndsWith(']') ? url.Host[1..^1] : url.Host;
        return new Upstream(protocol, host, port, user, pass);
    }

    /// Engine args that keep the browser's bypass list the caller's own: Playwright adds <c>&lt;-loopback&gt;</c>
    /// (proxy even loopback) whenever it is given a proxy, and an empty --proxy-bypass-list is the same as none
    /// to Chromium. Nothing to add when ResolveProxy already wrote one.
    internal static List<string> RequestProxyArgs(IEnumerable<string> proxyArgs)
        => proxyArgs.Any(a => a.StartsWith("--proxy-bypass-list=", StringComparison.Ordinal)) ? new() : new() { "--proxy-bypass-list=" };

    /// Start a forwarder on 127.0.0.1 (a free port) for <paramref name="upstream"/>.
    internal static RequestProxy Start(Upstream upstream, string? bypass = null)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        return new RequestProxy(listener, upstream, bypass);
    }

    /// Start the forwarder when ResolveProxy moved the credentials to the engine, else null. A failure to start
    /// never fails the launch: it warns that the request API will not use the proxy.
    internal static RequestProxy? Route(ProxyOptions? proxy, IEnumerable<string> proxyArgs, bool quiet)
    {
        var upstream = RequestProxyUpstream(proxy, proxyArgs);
        if (upstream is null) return null;
        try { return Start(upstream, proxy?.Bypass); }
        catch (Exception e)
        {
            if (!quiet) Console.Error.WriteLine($"clearcote: could not start the local proxy for APIRequest ({e.Message}); context.APIRequest and page.APIRequest will connect from this machine's IP, not through your proxy.");
            return null;
        }
    }

    /// The warning ServeAsync prints when it has moved credentials to the engine: a client attached over CDP
    /// runs Playwright's request API in its own process with no proxy of its own, so nothing here can route it.
    internal const string ServeWarning =
        "clearcote: context.APIRequest / page.APIRequest of a client attached over CDP do not use this proxy and connect from the client's own IP; make API calls inside the page (page.EvaluateAsync(\"() => fetch(...)\")) instead.";

    /// The warning a Docker launch with a proxy prints: the browser in the container uses the proxy, but
    /// Playwright's request API runs in this process over CDP with no proxy of its own.
    internal const string DockerWarning =
        "clearcote: with Docker, context.APIRequest / page.APIRequest do not use the proxy and connect from this machine's IP; make API calls inside the page (page.EvaluateAsync(\"() => fetch(...)\")) instead.";

    // ── lifetime ─────────────────────────────────────────────────────────────

    /// Stop listening and drop every open tunnel. Closes once; never throws.
    public Task CloseAsync()
    {
        lock (_gate) return _closing ??= CloseCoreAsync();
    }

    public ValueTask DisposeAsync() => new(CloseAsync());

    private async Task CloseCoreAsync()
    {
        try { _stop.Cancel(); } catch { }
        try { _listener.Stop(); } catch { }
        foreach (var d in _open.Keys) { try { d.Dispose(); } catch { } }
        try { await _accepting.ConfigureAwait(false); } catch { }
    }

    private async Task AcceptLoopAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            TcpClient client;
            try { client = await _listener.AcceptTcpClientAsync(_stop.Token).ConfigureAwait(false); }
            catch { return; }   // stopped
            _ = HandleClientAsync(client);
        }
    }

    private IDisposable Track(IDisposable d)
    {
        _open[d] = 0;
        if (_stop.IsCancellationRequested) d.Dispose();   // raced a close
        return d;
    }

    private void Untrack(IDisposable d)
    {
        _open.TryRemove(d, out _);
        try { d.Dispose(); } catch { }
    }

    private async Task HandleClientAsync(TcpClient client)
    {
        // Everything this connection opens, so a close drops it and its end releases it.
        var owned = new List<IDisposable>();
        IDisposable Own(IDisposable d) { lock (owned) owned.Add(d); return Track(d); }
        Own(client);
        Stream? remote = null;
        try
        {
            client.NoDelay = true;
            var cs = client.GetStream();
            byte[] rest;
            (string Host, int Port)? dest;
            using (var cts = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token))
            {
                cts.CancelAfter(HandshakeTimeout);
                var (head, after) = await ReadHeadAsync(cs, cts.Token).ConfigureAwait(false);
                rest = after;
                var first = head.Split("\r\n", 2)[0].Split(' ');
                dest = first[0] == "CONNECT" && first.Length > 1 ? ParseHostPort(first[1]) : null;
                if (dest is null)
                {
                    await WriteAsync(cs, "HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n", cts.Token).ConfigureAwait(false);
                    return;
                }
                byte[] early;
                try
                {
                    (remote, early) = IsLoopback(dest.Value.Host)
                        ? (await DirectAsync(dest.Value.Host, dest.Value.Port, cts.Token, Own).ConfigureAwait(false), Array.Empty<byte>())
                        : await OpenTunnelAsync(_upstream, dest.Value.Host, dest.Value.Port, cts.Token, Own).ConfigureAwait(false);
                    Own(remote);
                }
                catch (Exception e)
                {
                    var msg = Regex.Replace(ErrorText(e), "[\r\n]", " ");
                    if (msg.Length > 200) msg = msg[..200];
                    await WriteAsync(cs, $"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nX-Clearcote-Proxy-Error: {msg}\r\nContent-Length: 0\r\n\r\n", cts.Token).ConfigureAwait(false);
                    return;
                }
                await WriteAsync(cs, "HTTP/1.1 200 Connection Established\r\n\r\n", cts.Token).ConfigureAwait(false);
                // Bytes the upstream sent after its own 200 belong to the client.
                if (early.Length > 0) await cs.WriteAsync(early, cts.Token).ConfigureAwait(false);
                if (rest.Length > 0) await remote.WriteAsync(rest, cts.Token).ConfigureAwait(false);
            }
            await PipeAsync(cs, client.Client, remote).ConfigureAwait(false);
        }
        catch { }   // a broken client or tunnel only ever ends that connection
        finally
        {
            lock (owned) foreach (var d in owned) Untrack(d);
        }
    }

    /// Copy both ways until both sides are done; one side ending half-closes the other, as Node's pipe does.
    private async Task PipeAsync(NetworkStream client, Socket clientSocket, Stream remote)
    {
        async Task Copy(Stream from, Stream to, Func<Task> endWrite)
        {
            try
            {
                await from.CopyToAsync(to, 64 * 1024, _stop.Token).ConfigureAwait(false);
                await endWrite().ConfigureAwait(false);
            }
            catch
            {
                // One side failed: tear both down so the other copy ends too.
                try { client.Dispose(); } catch { }
                try { remote.Dispose(); } catch { }
            }
        }
        await Task.WhenAll(
            Copy(client, remote, () => HalfCloseAsync(remote)),
            Copy(remote, client, () => { clientSocket.Shutdown(SocketShutdown.Send); return Task.CompletedTask; })).ConfigureAwait(false);
    }

    private static async Task HalfCloseAsync(Stream s)
    {
        switch (s)
        {
            case NetworkStream ns: ns.Socket.Shutdown(SocketShutdown.Send); break;
            case SslStream ssl: await ssl.ShutdownAsync().ConfigureAwait(false); break;
        }
    }

    private static string ErrorText(Exception e) => e switch
    {
        OperationCanceledException => "timed out",
        _ => e.Message,
    };

    private static Task WriteAsync(Stream s, string text, CancellationToken ct)
        => s.WriteAsync(Encoding.Latin1.GetBytes(text), ct).AsTask();

    // ── parsing ──────────────────────────────────────────────────────────────

    /// Read up to the end of an HTTP head; <c>After</c> is whatever arrived after it.
    private static async Task<(string Head, byte[] After)> ReadHeadAsync(Stream s, CancellationToken ct)
    {
        var buf = new MemoryStream();
        var chunk = new byte[4096];
        while (true)
        {
            var n = await s.ReadAsync(chunk, ct).ConfigureAwait(false);
            if (n <= 0) throw new IOException("connection closed before the end of the head");
            buf.Write(chunk, 0, n);
            var head = SplitHead(buf.GetBuffer(), (int)buf.Length);
            if (head is not null) return head.Value;
            if (buf.Length > HeadLimit) throw new IOException("proxy response head too large");
        }
    }

    private static (string Head, byte[] After)? SplitHead(byte[] buf, int length)
    {
        var data = buf.AsSpan(0, length);
        var end = data.IndexOf("\r\n\r\n"u8);
        return end < 0 ? null : (Encoding.Latin1.GetString(data[..end]), data[(end + 4)..].ToArray());
    }

    /// <c>host:port</c> or <c>[v6]:port</c>.
    internal static (string Host, int Port)? ParseHostPort(string? target)
    {
        if (string.IsNullOrEmpty(target)) return null;
        var m = Regex.Match(target, @"^\[([^\]]+)\]:(\d{1,5})$");
        if (!m.Success) m = Regex.Match(target, @"^([^:\s]+):(\d{1,5})$");
        if (!m.Success) return null;
        var port = int.Parse(m.Groups[2].Value);
        return port > 0 && port < 65536 ? (m.Groups[1].Value, port) : null;
    }

    /// Loopback never goes through the proxy, as in Chromium (its implicit bypass rules): the request API
    /// reached localhost directly before the forwarder existed, and a remote proxy cannot reach it anyway.
    internal static bool IsLoopback(string host)
    {
        var h = host.ToLowerInvariant();
        if (h.EndsWith('.')) h = h[..^1];
        return h == "localhost" || h.EndsWith(".localhost", StringComparison.Ordinal)
            || Regex.IsMatch(h, @"^127\.\d+\.\d+\.\d+$") || h == "::1" || h == "0:0:0:0:0:0:0:1";
    }

    /// The 16 bytes of an IPv6 literal; null for a form with an embedded IPv4 tail (sent as a name instead).
    internal static byte[]? Ipv6Bytes(string ip)
    {
        if (ip.Contains('.') || ip.Contains('%')) return null;
        var halves = ip.Split("::");
        if (halves.Length > 2) return null;
        var left = halves[0].Length > 0 ? halves[0].Split(':') : Array.Empty<string>();
        var right = halves.Length == 2 && halves[1].Length > 0 ? halves[1].Split(':') : Array.Empty<string>();
        var fill = halves.Length == 2 ? 8 - left.Length - right.Length : 0;
        var groups = left.Concat(Enumerable.Repeat("0", Math.Max(fill, 0))).Concat(right).ToList();
        if (groups.Count != 8 || groups.Any(g => !Regex.IsMatch(g, "^[0-9a-fA-F]{1,4}$"))) return null;
        var outb = new byte[16];
        for (var i = 0; i < 8; i++)
        {
            var v = Convert.ToUInt16(groups[i], 16);
            outb[i * 2] = (byte)(v >> 8);
            outb[i * 2 + 1] = (byte)(v & 0xff);
        }
        return outb;
    }

    // ── tunnels ──────────────────────────────────────────────────────────────

    private static async Task<Stream> DirectAsync(string host, int port, CancellationToken ct, Func<IDisposable, IDisposable>? track = null)
    {
        var c = new TcpClient { NoDelay = true };
        track?.Invoke(c);
        try
        {
            await c.ConnectAsync(host, port, ct).ConfigureAwait(false);
            return OwnedStream(c);
        }
        catch { c.Dispose(); throw; }
    }

    private static NetworkStream OwnedStream(TcpClient c) => new(c.Client, ownsSocket: true);

    /// A stream already tunnelled through <paramref name="upstream"/> to host:port, and any bytes the upstream
    /// sent after its reply (the client's, not the handshake's). <paramref name="track"/> sees the socket as soon
    /// as it exists, so a close in the middle of the handshake drops it.
    internal static async Task<(Stream Stream, byte[] Early)> OpenTunnelAsync(Upstream upstream, string host, int port,
        CancellationToken ct, Func<IDisposable, IDisposable>? track = null)
    {
        var tcp = new TcpClient { NoDelay = true };
        track?.Invoke(tcp);
        Stream s;
        try
        {
            try { await tcp.ConnectAsync(upstream.Host, upstream.Port, ct).ConfigureAwait(false); }
            catch (OperationCanceledException) { throw new IOException($"timed out connecting to the proxy {upstream.Host}:{upstream.Port}"); }
            s = OwnedStream(tcp);
            if (upstream.Protocol == "https")
            {
                var ssl = new SslStream(s, leaveInnerStreamOpen: false);
                s = ssl;
                await ssl.AuthenticateAsClientAsync(new SslClientAuthenticationOptions
                {
                    TargetHost = upstream.Host,
                    ApplicationProtocols = new List<SslApplicationProtocol> { SslApplicationProtocol.Http11 },
                }, ct).ConfigureAwait(false);
            }
        }
        catch { tcp.Dispose(); throw; }
        try
        {
            var early = upstream.Protocol == "socks5"
                ? await Socks5ConnectAsync(s, upstream, host, port, ct).ConfigureAwait(false)
                : await HttpConnectAsync(s, upstream, host, port, ct).ConfigureAwait(false);
            return (s, early);
        }
        catch
        {
            s.Dispose();
            throw;
        }
    }

    private static async Task<byte[]> HttpConnectAsync(Stream s, Upstream upstream, string host, int port, CancellationToken ct)
    {
        var authority = host.Contains(':') ? $"[{host}]:{port}" : $"{host}:{port}";
        var token = Convert.ToBase64String(Encoding.UTF8.GetBytes($"{upstream.Username}:{upstream.Password}"));
        await WriteAsync(s, $"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nProxy-Authorization: Basic {token}\r\n\r\n", ct).ConfigureAwait(false);
        var (head, rest) = await ReadHeadAsync(s, ct).ConfigureAwait(false);
        var status = head.Split("\r\n", 2)[0];
        if (!Regex.IsMatch(status, @"^HTTP/1\.[01] 2\d\d")) throw new IOException($"the proxy refused the tunnel: {status}");
        return rest;
    }

    private static readonly Dictionary<int, string> SocksReply = new()
    {
        [1] = "general failure", [2] = "connection not allowed by ruleset", [3] = "network unreachable", [4] = "host unreachable",
        [5] = "connection refused", [6] = "TTL expired", [7] = "command not supported", [8] = "address type not supported",
    };

    /// Read exactly n bytes.
    private static async Task<byte[]> ReadN(Stream s, int n, CancellationToken ct)
    {
        var b = new byte[n];
        try { await s.ReadExactlyAsync(b, ct).ConfigureAwait(false); }
        catch (EndOfStreamException) { throw new IOException("the SOCKS5 proxy closed the connection"); }
        return b;
    }

    private static async Task<byte[]> Socks5ConnectAsync(Stream s, Upstream upstream, string host, int port, CancellationToken ct)
    {
        var user = Encoding.UTF8.GetBytes(upstream.Username);
        var pass = Encoding.UTF8.GetBytes(upstream.Password);
        if (user.Length > 255 || pass.Length > 255) throw new IOException("SOCKS5 username and password are limited to 255 bytes each");
        await s.WriteAsync(new byte[] { 5, 1, 2 }, ct).ConfigureAwait(false);   // version 5, one method: username/password
        var choice = await ReadN(s, 2, ct).ConfigureAwait(false);
        if (choice[0] != 5 || choice[1] != 2) throw new IOException("the SOCKS5 proxy does not accept username/password authentication");
        await s.WriteAsync(new byte[] { 1, (byte)user.Length }.Concat(user).Append((byte)pass.Length).Concat(pass).ToArray(), ct).ConfigureAwait(false);
        var auth = await ReadN(s, 2, ct).ConfigureAwait(false);
        if (auth[1] != 0) throw new IOException("the SOCKS5 proxy rejected the username or password");
        byte[] addr;
        var v6 = IPAddress.TryParse(host, out var ip) && ip.AddressFamily == AddressFamily.InterNetworkV6 ? Ipv6Bytes(host) : null;
        if (ip is not null && ip.AddressFamily == AddressFamily.InterNetwork && Regex.IsMatch(host, @"^\d+\.\d+\.\d+\.\d+$"))
            addr = new byte[] { 1 }.Concat(ip.GetAddressBytes()).ToArray();
        else if (v6 is not null)
            addr = new byte[] { 4 }.Concat(v6).ToArray();
        else
        {
            var name = Encoding.UTF8.GetBytes(host);   // the proxy resolves the name, as the browser's SOCKS5 does
            if (name.Length > 255) throw new IOException("host name too long for SOCKS5");
            addr = new byte[] { 3, (byte)name.Length }.Concat(name).ToArray();
        }
        await s.WriteAsync(new byte[] { 5, 1, 0 }.Concat(addr).Concat(new[] { (byte)(port >> 8), (byte)(port & 0xff) }).ToArray(), ct).ConfigureAwait(false);
        var head = await ReadN(s, 4, ct).ConfigureAwait(false);
        if (head[1] != 0)
            throw new IOException($"the SOCKS5 proxy could not connect: {(SocksReply.TryGetValue(head[1], out var why) ? why : $"reply {head[1]}")}");
        var len = head[3] == 1 ? 4 : head[3] == 4 ? 16 : (await ReadN(s, 1, ct).ConfigureAwait(false))[0];
        await ReadN(s, len + 2, ct).ConfigureAwait(false);   // bound address + port, unused
        return Array.Empty<byte>();
    }
}
