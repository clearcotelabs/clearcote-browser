using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Clearcote.Tests;

// Real local servers for proxy tests: an HTTP origin, an HTTP proxy (absolute-form + CONNECT) and a
// SOCKS5 proxy (optional RFC 1929 auth). Each records what it saw, so a test can prove a request
// actually went THROUGH the proxy instead of trusting the client's word for it. Mirrors the Node
// suite's test/helpers/proxies.ts.

/// Shared accept loop + socket bookkeeping.
internal abstract class LocalServer : IAsyncDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _cts = new();
    private readonly ConcurrentDictionary<TcpClient, byte> _clients = new();
    private readonly Task _loop;

    protected LocalServer()
    {
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _loop = Task.Run(AcceptLoopAsync);
    }

    public int Port { get; }

    private async Task AcceptLoopAsync()
    {
        while (!_cts.IsCancellationRequested)
        {
            TcpClient c;
            try { c = await _listener.AcceptTcpClientAsync(_cts.Token); }
            catch { return; }
            _clients[c] = 0;
            _ = Task.Run(async () =>
            {
                try { await HandleAsync(c.GetStream(), _cts.Token); }
                catch { /* client went away */ }
                finally { _clients.TryRemove(c, out _); c.Dispose(); }
            });
        }
    }

    protected abstract Task HandleAsync(NetworkStream s, CancellationToken ct);

    /// Read until CRLFCRLF; returns (head lines, bytes already read past the head).
    protected static async Task<(string[] Lines, byte[] Tail)?> ReadHeadAsync(Stream s, CancellationToken ct)
    {
        var buf = new MemoryStream();
        var one = new byte[4096];
        while (true)
        {
            var n = await s.ReadAsync(one, ct);
            if (n <= 0) return null;
            buf.Write(one, 0, n);
            var all = buf.ToArray();
            var idx = IndexOf(all, "\r\n\r\n"u8.ToArray());
            if (idx >= 0)
                return (Encoding.Latin1.GetString(all, 0, idx).Split("\r\n"), all[(idx + 4)..]);
        }
    }

    protected static async Task<byte[]> ReadExactAsync(Stream s, int n, CancellationToken ct)
    {
        var b = new byte[n];
        await s.ReadExactlyAsync(b, ct);
        return b;
    }

    protected static int IndexOf(byte[] hay, byte[] needle) => hay.AsSpan().IndexOf(needle);

    protected static async Task PipeAsync(Stream a, Stream b, CancellationToken ct)
    {
        var t1 = a.CopyToAsync(b, ct);
        var t2 = b.CopyToAsync(a, ct);
        await Task.WhenAny(t1, t2);
    }

    public async ValueTask DisposeAsync()
    {
        _cts.Cancel();
        _listener.Stop();
        foreach (var c in _clients.Keys) { try { c.Dispose(); } catch { } }
        try { await _loop; } catch { }
    }
}

internal sealed record OriginRequest(string Method, string Url, List<KeyValuePair<string, string>> Headers, string Body)
{
    public string? Header(string name) =>
        Headers.FirstOrDefault(h => string.Equals(h.Key, name, StringComparison.OrdinalIgnoreCase)).Value;
}

/// A one-request-per-connection HTTP/1.1 origin.
internal sealed class TestOrigin : LocalServer
{
    private readonly Func<OriginRequest, (int Status, string Body)> _handler;
    private readonly string _contentType;
    public readonly ConcurrentQueue<OriginRequest> Log = new();

    public TestOrigin(Func<OriginRequest, (int Status, string Body)> handler, string contentType = "application/json")
    {
        _handler = handler;
        _contentType = contentType;
    }

    protected override async Task HandleAsync(NetworkStream s, CancellationToken ct)
    {
        var head = await ReadHeadAsync(s, ct);
        if (head is null) return;
        var (lines, rest) = head.Value;
        var parts = lines[0].Split(' ');
        var headers = lines.Skip(1).Where(l => l.Contains(':'))
            .Select(l => new KeyValuePair<string, string>(l[..l.IndexOf(':')].Trim(), l[(l.IndexOf(':') + 1)..].Trim())).ToList();
        var lenStr = headers.FirstOrDefault(h => h.Key.Equals("content-length", StringComparison.OrdinalIgnoreCase)).Value;
        var body = rest;
        if (int.TryParse(lenStr, out var len) && len > rest.Length)
            body = rest.Concat(await ReadExactAsync(s, len - rest.Length, ct)).ToArray();
        var req = new OriginRequest(parts[0], parts.Length > 1 ? parts[1] : "", headers, Encoding.UTF8.GetString(body));
        Log.Enqueue(req);
        var (status, text) = _handler(req);
        var bytes = Encoding.UTF8.GetBytes(text);
        var resp = $"HTTP/1.1 {status} X\r\nContent-Type: {_contentType}\r\nContent-Length: {bytes.Length}\r\nConnection: close\r\n\r\n";
        await s.WriteAsync(Encoding.Latin1.GetBytes(resp), ct);
        await s.WriteAsync(bytes, ct);
        await s.FlushAsync(ct);
    }
}

internal sealed record ProxyHit(string Kind, string Target, string? Auth);

/// HTTP proxy: absolute-form forwarding and CONNECT tunnelling. With RequireAuth, a request
/// without the expected Proxy-Authorization gets a 407 challenge (as real proxies send).
internal sealed class TestHttpProxy : LocalServer
{
    private readonly string? _requireAuth;
    public readonly ConcurrentQueue<ProxyHit> Log = new();

    public TestHttpProxy(string? requireAuth = null) => _requireAuth = requireAuth;

    protected override async Task HandleAsync(NetworkStream client, CancellationToken ct)
    {
        var head = await ReadHeadAsync(client, ct);
        if (head is null) return;
        var (lines, rest) = head.Value;
        var parts = lines[0].Split(' ');
        var method = parts[0];
        var target = parts[1];
        var authLine = lines.FirstOrDefault(l => l.StartsWith("proxy-authorization:", StringComparison.OrdinalIgnoreCase));
        var auth = authLine?[(authLine.IndexOf(':') + 1)..].Trim();
        Log.Enqueue(new ProxyHit(method == "CONNECT" ? "connect" : "absolute", target, auth));
        if (_requireAuth is not null && auth != _requireAuth)
        {
            await client.WriteAsync("HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"t\"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"u8.ToArray(), ct);
            return;
        }
        using var up = new TcpClient();
        if (method == "CONNECT")
        {
            var i = target.LastIndexOf(':');
            await up.ConnectAsync(target[..i].Trim('[', ']'), int.Parse(target[(i + 1)..]), ct);
            await client.WriteAsync("HTTP/1.1 200 Connection established\r\n\r\n"u8.ToArray(), ct);
            var us = up.GetStream();
            if (rest.Length > 0) await us.WriteAsync(rest, ct);
            await PipeAsync(client, us, ct);
        }
        else
        {
            var u = new Uri(target);
            await up.ConnectAsync(u.Host, u.Port, ct);
            var us = up.GetStream();
            var fwd = new StringBuilder($"{method} {u.PathAndQuery} HTTP/1.1\r\n");
            // Forward every header except the proxy's own credentials, so an origin sees what the
            // client really sent (including Proxy-Connection, which transparentProxy removes).
            foreach (var l in lines.Skip(1))
                if (!l.StartsWith("proxy-authorization:", StringComparison.OrdinalIgnoreCase)) fwd.Append(l).Append("\r\n");
            fwd.Append("\r\n");
            await us.WriteAsync(Encoding.Latin1.GetBytes(fwd.ToString()), ct);
            if (rest.Length > 0) await us.WriteAsync(rest, ct);
            await PipeAsync(client, us, ct);
        }
    }
}

internal sealed record SocksHit(string Host, int Port, string? User);

/// SOCKS5 CONNECT proxy with optional username/password auth.
internal sealed class TestSocks5 : LocalServer
{
    private readonly string? _user, _pass;
    public readonly ConcurrentQueue<SocksHit> Log = new();
    public int AuthOk;

    public TestSocks5(string? user = null, string? pass = null) { _user = user; _pass = pass; }

    protected override async Task HandleAsync(NetworkStream c, CancellationToken ct)
    {
        var hello = await ReadExactAsync(c, 2, ct);
        if (hello[0] != 5) return;
        var methods = await ReadExactAsync(c, hello[1], ct);
        string? user = null;
        if (_user is not null)
        {
            if (!methods.Contains((byte)2)) { await c.WriteAsync(new byte[] { 5, 0xff }, ct); return; }
            await c.WriteAsync(new byte[] { 5, 2 }, ct);
            var vl = await ReadExactAsync(c, 2, ct);
            user = Encoding.UTF8.GetString(await ReadExactAsync(c, vl[1], ct));
            var pl = (await ReadExactAsync(c, 1, ct))[0];
            var pass = Encoding.UTF8.GetString(await ReadExactAsync(c, pl, ct));
            var ok = user == _user && pass == _pass;
            await c.WriteAsync(new byte[] { 1, (byte)(ok ? 0 : 1) }, ct);
            if (!ok) return;
            Interlocked.Increment(ref AuthOk);
        }
        else
        {
            await c.WriteAsync(new byte[] { 5, 0 }, ct);
        }
        var req = await ReadExactAsync(c, 4, ct);
        string host;
        if (req[3] == 1) host = new IPAddress(await ReadExactAsync(c, 4, ct)).ToString();
        else if (req[3] == 4) host = new IPAddress(await ReadExactAsync(c, 16, ct)).ToString();
        else if (req[3] == 3) host = Encoding.ASCII.GetString(await ReadExactAsync(c, (await ReadExactAsync(c, 1, ct))[0], ct));
        else return;
        var pb = await ReadExactAsync(c, 2, ct);
        var port = (pb[0] << 8) | pb[1];
        Log.Enqueue(new SocksHit(host, port, user));
        using var up = new TcpClient();
        try { await up.ConnectAsync(host, port, ct); }
        catch { await c.WriteAsync(new byte[] { 5, 5, 0, 1, 0, 0, 0, 0, 0, 0 }, ct); return; }
        await c.WriteAsync(new byte[] { 5, 0, 0, 1, 0, 0, 0, 0, 0, 0 }, ct);
        await PipeAsync(c, up.GetStream(), ct);
    }
}

/// Accepts connections and never answers — a proxy that hangs.
internal sealed class SilentServer : LocalServer
{
    protected override Task HandleAsync(NetworkStream s, CancellationToken ct) => Task.Delay(Timeout.Infinite, ct);
}

/// Captures Console.Error for the duration of a using block.
internal sealed class StderrCapture : IDisposable
{
    private readonly TextWriter _old = Console.Error;
    private readonly StringWriter _sw = new();
    public StderrCapture() => Console.SetError(_sw);
    public string Text => _sw.ToString();
    public void Dispose() => Console.SetError(_old);
}

/// A stand-in for OpenRouter (any OpenAI-compatible chat-completions API) for the in-browser agent: every
/// request is recorded and answered with a completion in which the model says it is done, so an agent task
/// ends without a real LLM or API key.
internal sealed class FakeOpenRouter : IAsyncDisposable
{
    private readonly TestOrigin _origin;

    public FakeOpenRouter()
        => _origin = new TestOrigin(req => req.Url.Split('?')[0].EndsWith("/chat/completions", StringComparison.Ordinal)
            ? (200, Completion)
            : (404, """{"error":{"message":"not found"}}"""));

    /// What a client puts in <c>AgentLlmUrl</c>.
    public string BaseUrl => $"http://127.0.0.1:{_origin.Port}/api/v1";

    /// Every request seen so far.
    public List<OriginRequest> Calls => _origin.Log.ToList();

    private const string Completion = """
        {"id":"gen-test","object":"chat.completion","created":1700000000,"model":"test/model",
         "choices":[{"index":0,"finish_reason":"stop",
                     "message":{"role":"assistant","content":"{\"action\":\"done\",\"text\":\"Task complete.\"}"}}],
         "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}
        """;

    public ValueTask DisposeAsync() => _origin.DisposeAsync();
}
