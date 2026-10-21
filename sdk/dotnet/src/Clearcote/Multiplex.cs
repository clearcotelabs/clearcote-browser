// ServeMultiplexAsync — one CDP endpoint, many identities (ports multiplex.ts).
//
// ServeAsync starts ONE browser with one persona. This puts an HTTP/WebSocket front on a port and
// starts a separate browser per identity on demand, chosen by the connection URL:
//
//   await pw.Chromium.ConnectOverCDPAsync("http://127.0.0.1:9222?fingerprint=acct-1&platform=windows");
//   await pw.Chromium.ConnectOverCDPAsync("http://127.0.0.1:9222?fingerprint=acct-2&proxy=socks5://u:p@host:1080&geoip=true");
//
// Playwright/Puppeteer fetch /json/version (keeping the query), receive a WebSocket URL routed
// through this server (/fingerprint/<id>/devtools/browser/<uuid>), and connect. The same seed
// reuses the same browser; asking for a running identity with DIFFERENT parameters is a 409 (close it
// first). No seed and no parameters = one shared default browser.
//
// Endpoints: GET / (status) · GET /json/version · GET /json/list · GET /json ·
// POST /fingerprint/<id>/close · WS /fingerprint/<id>/devtools/* · WS /devtools/* (default).
// WebSocket routes only reach a RUNNING identity; browsers are started by the HTTP endpoints.
//
// SAFETY, deliberately stricter than the design it mirrors (whose equivalent server had an
// unauthenticated path-traversal and a browser-origin CSRF reported against it):
//   * binds 127.0.0.1 by default; a non-loopback bind prints a warning, because anyone who can reach
//     the port can start browsers and drive them;
//   * seeds are validated against a strict pattern and profile directories are named by a HASH of
//     the identity, so no request value ever becomes part of a filesystem path;
//   * requests a web page could make are refused on every route: cross-site Sec-Fetch-Site, a browser
//     Origin that is not loopback or allowed, or a Host that is not an IP literal / localhost / allowed
//     (DNS rebinding);
//   * the number of concurrent browsers is capped (MaxBrowsers, default 16).
//
// The front is a TcpListener speaking just enough HTTP/1.1 (one request per connection, answered with
// Connection: close). WebSockets are relayed at the socket level (the upgrade request is rewritten and
// the two sockets piped), so no WebSocket library is needed and CDP traffic is not re-framed.

using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

/// Options for <see cref="Clearcote.ServeMultiplexAsync"/>. Everything inherited from
/// <see cref="LaunchOptions"/> (Headless, ExecutablePath, persona defaults, Proxy, LicenseKey, ...) is the
/// base every identity's browser starts from; a connection's query parameters override it.
public class MultiplexOptions : LaunchOptions
{
    /// Port for the multiplexer (default 9222; 0 = ephemeral).
    public int? Port { get; set; }
    /// Bind address (default 127.0.0.1).
    public string? Host { get; set; }
    /// Close an identity's browser this many seconds after its last WebSocket disconnects (0 = never,
    /// the default). Null reads CLEARCOTE_SERVE_IDLE_TIMEOUT. (Hides the cloud-only
    /// <see cref="LaunchOptions.IdleTimeoutSec"/>, which a local multiplexer has no use for.)
    public new double? IdleTimeoutSec { get; set; }
    /// Keep each identity's profile under this directory (sub-directory named by a hash of the identity),
    /// so cookies and storage survive restarts. Default: a temporary profile per browser, deleted when it
    /// closes.
    public string? DataDir { get; set; }
    /// Maximum concurrent browsers (default 16). Further identities get HTTP 429.
    public int? MaxBrowsers { get; set; }
    /// Extra browser Origins (exact, e.g. "https://tool.example") allowed to reach the endpoint.
    public IReadOnlyList<string>? AllowOrigins { get; set; }
    /// Host names accepted in the Host header besides IP literals and "localhost" (DNS-rebinding
    /// guard), e.g. "cdp.example.com" when a reverse proxy forwards that Host.
    public IReadOnlyList<string>? AllowHosts { get; set; }
    /// Per-browser startup timeout in ms (default 30000).
    public int? ReadyTimeoutMs { get; set; }
    /// Headless: each browser's outer window size, as <see cref="ServeOptions.WindowSize"/>.
    public ViewportSize? WindowSize { get; set; }
    /// How a browser is started for an identity. Defaults to <see cref="Clearcote.ServeAsync"/>. Exists
    /// so the routing, limits and WebSocket relay can be tested without a browser binary.
    public Func<ServeOptions, Task<IBrowserHandle>>? StartBrowser { get; set; }
}

/// What the multiplexer needs from a started browser: its loopback CDP port, pid and a close.
public interface IBrowserHandle
{
    int Port { get; }
    int? Pid { get; }
    Task CloseAsync();
}

/// A snapshot of a multiplexer's running browsers (the GET / body).
public sealed record MultiplexStatus(string Status, int Active, int MaxBrowsers, double IdleTimeoutSec,
    IReadOnlyList<MultiplexProcess> Processes);

/// One running browser in <see cref="MultiplexStatus"/>. <see cref="Params"/> are the accepted query
/// parameters, proxy credentials redacted.
public sealed record MultiplexProcess(string Id, string? Seed, int? Pid, int Port, int Connections,
    DateTimeOffset StartedAt, DateTimeOffset? IdleSince, IReadOnlyDictionary<string, string> Params);

/// A request the multiplexer refuses, with the HTTP status it answers.
public sealed class MultiplexRequestException : Exception
{
    public MultiplexRequestException(int status, string message) : base(message) => Status = status;
    public int Status { get; }
}

/// Parsed per-connection identity: the id used in paths, and the launch options it adds.
/// <para><see cref="Id"/> is "default", a seed, or "_p&lt;hash&gt;" for a parameter-only identity.
/// <see cref="Options"/> maps launch option names (camelCase, as in the Node SDK) to typed values
/// (int, double, bool, string, or a <see cref="ProxyOptions"/>); <see cref="Params"/> are the raw
/// accepted parameters (proxy credentials redacted), for the status page.</para>
public sealed record ConnectionIdentity(string Id, string? Seed, IReadOnlyDictionary<string, object> Options,
    IReadOnlyDictionary<string, string> Params);

/// The multiplexer's request parsing and guards (public for tools that front it themselves).
public static class Multiplex
{
    /// A seed usable in a URL path segment. Leading `_` is reserved for parameter-only identities.
    public static readonly Regex SeedPattern = new("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", RegexOptions.CultureInvariant);

    private static readonly string[] NumericKeys =
    {
        "hardwareConcurrency", "deviceMemory", "screenWidth", "screenHeight", "availWidth", "availHeight",
        "colorDepth", "devicePixelRatio", "maxTouchPoints", "storageQuota", "personaSchema",
    };
    private static readonly string[] BooleanKeys =
    {
        "lightStealth", "realGpuHost", "disableGpuFingerprint", "fingerprintNoise", "gpuStringSpoof", "canvasNoise",
        "fingerprintVoices", "allowThirdPartyCookies", "transparentProxy", "socks5Udp",
    };
    private static readonly string[] StringKeys =
    {
        "platform", "platformVersion", "brand", "brandVersion", "gpuVendor", "gpuRenderer", "location",
        "acceptLanguage", "webrtcIp", "webrtcMdns", "tlsProfile",
    };
    private static readonly string[] Special = { "fingerprint", "timezone", "locale", "proxy", "geoip" };

    private static string Kebab(string k) => Regex.Replace(k, "[A-Z]", m => "-" + m.Value.ToLowerInvariant());

    /// Query parameter (kebab-case) -> launch option name, for every parameter a connection may set.
    public static readonly IReadOnlyDictionary<string, string> QueryParams =
        NumericKeys.Concat(BooleanKeys).Concat(StringKeys).ToDictionary(Kebab, k => k);

    /// The <see cref="LaunchOptions"/> property a camelCase option name sets.
    internal static PropertyInfo OptionProperty(string key)
        => typeof(LaunchOptions).GetProperty(char.ToUpperInvariant(key[0]) + key[1..], BindingFlags.Public | BindingFlags.Instance)
           ?? throw new InvalidOperationException($"no launch option '{key}'");

    private static bool ParseBool(string name, string v)
    {
        if (Regex.IsMatch(v, "^(1|true|yes|on)$", RegexOptions.IgnoreCase)) return true;
        if (Regex.IsMatch(v, "^(0|false|no|off)$", RegexOptions.IgnoreCase)) return false;
        throw new MultiplexRequestException(400, $"query parameter '{name}' must be true or false");
    }

    /// Split a query string the way URLSearchParams does ('+' is a space; a missing '=' is an empty value).
    public static List<KeyValuePair<string, string>> ParseQuery(string? query)
    {
        var pairs = new List<KeyValuePair<string, string>>();
        if (string.IsNullOrEmpty(query)) return pairs;
        if (query[0] == '?') query = query[1..];
        static string Dec(string s) => Uri.UnescapeDataString(s.Replace('+', ' '));
        foreach (var part in query.Split('&'))
        {
            if (part.Length == 0) continue;
            var i = part.IndexOf('=');
            pairs.Add(new(Dec(i < 0 ? part : part[..i]), i < 0 ? "" : Dec(part[(i + 1)..])));
        }
        return pairs;
    }

    /// <inheritdoc cref="ParseConnectionIdentity(IEnumerable{KeyValuePair{string, string}})"/>
    public static ConnectionIdentity ParseConnectionIdentity(string? query) => ParseConnectionIdentity(ParseQuery(query));

    /// Turn a connection's query string into an identity. Unknown parameters are rejected with 400
    /// rather than guessed at, so a typo never silently launches a different identity.
    public static ConnectionIdentity ParseConnectionIdentity(IEnumerable<KeyValuePair<string, string>> search)
    {
        var options = new Dictionary<string, object>(StringComparer.Ordinal);
        var prms = new Dictionary<string, string>(StringComparer.Ordinal);
        string? seed = null;
        foreach (var (name, value) in search)
        {
            if (Special.Contains(name))
            {
                if (name == "fingerprint")
                {
                    if (!SeedPattern.IsMatch(value))
                        throw new MultiplexRequestException(400, "fingerprint must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}");
                    if (value == "default")
                        throw new MultiplexRequestException(400, "fingerprint 'default' is reserved for the no-parameter browser; pick another seed");
                    seed = value;
                    options["fingerprint"] = value;
                }
                else if (name == "timezone") options["timezone"] = value;
                else if (name == "locale") options["acceptLanguage"] = value;
                else if (name == "geoip") options["geoip"] = ParseBool(name, value);
                else
                {
                    ProxySpec? spec;
                    try { spec = ProxySpec.From(value); }
                    catch { throw new MultiplexRequestException(400, "proxy must be a URL such as socks5://user:pass@host:1080"); }
                    if (spec is null) throw new MultiplexRequestException(400, "proxy is empty");
                    options["proxy"] = new ProxyOptions { Server = spec.ServerString, Username = spec.Username, Password = spec.Password };
                    prms["proxy"] = spec.ServerString + (spec.Username is not null ? " (with credentials)" : "");
                    continue;
                }
                prms[name] = value;
                continue;
            }
            if (!QueryParams.TryGetValue(name, out var key))
                throw new MultiplexRequestException(400,
                    $"unknown query parameter '{name}'. Supported: {string.Join(", ", Special.Concat(QueryParams.Keys))}");
            if (NumericKeys.Contains(key))
            {
                if (value.Trim().Length == 0 || !double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var n) || !double.IsFinite(n))
                    throw new MultiplexRequestException(400, $"query parameter '{name}' must be a number");
                // The .NET options are typed: a count takes a whole number, the pixel ratio any number.
                var t = Nullable.GetUnderlyingType(OptionProperty(key).PropertyType) ?? OptionProperty(key).PropertyType;
                if (t == typeof(int))
                {
                    if (n != Math.Floor(n) || n < int.MinValue || n > int.MaxValue)
                        throw new MultiplexRequestException(400, $"query parameter '{name}' must be a whole number");
                    options[key] = (int)n;
                }
                else options[key] = n;
            }
            else if (BooleanKeys.Contains(key)) options[key] = ParseBool(name, value);
            else options[key] = value;
            prms[name] = value;
        }
        if (seed is not null) return new(seed, seed, options, prms);
        if (options.Count == 0) return new("default", null, options, prms);
        // Parameter-only identity: canonical form so ?a=1&b=2 and ?b=2&a=1 share one browser (and the
        // same id as the Node SDK's for the same query).
        return new("_p" + Sha256Hex(OptionsKey(options))[..16], null, options, prms);
    }

    private static readonly JsonSerializerOptions JsJson = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    /// Canonical form of an identity's launch options (proxy credentials included, never displayed):
    /// JSON.stringify of the [key, value] pairs sorted by key, as the Node SDK computes it.
    internal static string OptionsKey(IReadOnlyDictionary<string, object> options)
    {
        var arr = new JsonArray();
        foreach (var k in options.Keys.OrderBy(k => k, StringComparer.Ordinal))
        {
            JsonNode? v = options[k] switch
            {
                int i => JsonValue.Create(i),
                double d => JsonValue.Create(d),
                bool b => JsonValue.Create(b),
                string s => JsonValue.Create(s),
                ProxyOptions p => ProxyNode(p),
                _ => null,
            };
            arr.Add(new JsonArray(JsonValue.Create(k), v));
        }
        return arr.ToJsonString(JsJson);

        static JsonObject ProxyNode(ProxyOptions p)
        {
            var o = new JsonObject { ["server"] = p.Server };
            if (p.Username is not null) o["username"] = p.Username;
            if (p.Password is not null) o["password"] = p.Password;
            return o;
        }
    }

    internal static string Sha256Hex(string s)
        => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(s))).ToLowerInvariant();

    private static string? Header(IReadOnlyDictionary<string, string> headers, string name)
    {
        if (headers.TryGetValue(name, out var v)) return v;
        foreach (var (k, val) in headers) if (string.Equals(k, name, StringComparison.OrdinalIgnoreCase)) return val;
        return null;
    }

    /// Public ws(s):// base for URLs we hand back, honouring a reverse proxy's forwarded headers.
    public static string PublicWsBase(IReadOnlyDictionary<string, string> headers, string fallbackHost)
    {
        string? First(string name) => Header(headers, name)?.Split(',')[0].Trim() is { Length: > 0 } s ? s : null;
        var host = First("x-forwarded-host") ?? First("host") ?? fallbackHost;
        var proto = (First("x-forwarded-proto") ?? "").ToLowerInvariant();
        return $"{(proto is "https" or "wss" ? "wss" : "ws")}://{host}";
    }

    private static string Prefix(string id) => id == "default" ? "" : $"/fingerprint/{Uri.EscapeDataString(id)}";

    /// Rewrite a child's ws://127.0.0.1:&lt;port&gt;/devtools/... URL to route through the multiplexer.
    public static string RewriteWsUrl(string url, string wsBase, string id)
    {
        var m = Regex.Match(url, "^wss?://[^/]+(/devtools/.*)$", RegexOptions.Singleline);
        return m.Success ? $"{wsBase}{Prefix(id)}{m.Groups[1].Value}" : url;
    }

    /// Origin allowed to open a CDP WebSocket: none (non-browser client), loopback, or explicitly listed.
    public static bool OriginAllowed(string? origin, IReadOnlyList<string>? extra = null)
    {
        if (origin is null) return true;
        if (origin.Length == 0 || origin == "null") return false;
        if (extra is not null && extra.Contains(origin)) return true;
        if (!Uri.TryCreate(origin, UriKind.Absolute, out var u)) return false;
        var h = u.Host.Trim('[', ']');
        return h is "localhost" or "127.0.0.1" or "::1";
    }

    /// Host header allowed: an IP literal or "localhost" (the rule Chrome's own DevTools server uses
    /// against DNS rebinding), or a name in <paramref name="extra"/> for a deployment behind a reverse proxy.
    public static bool HostAllowed(string? hostHeader, IReadOnlyList<string>? extra = null)
    {
        if (hostHeader is null) return true;   // HTTP/1.0 client; nothing a browser page can omit
        var h = hostHeader.Trim().ToLowerInvariant();
        string name;
        if (h.StartsWith('[')) name = h.IndexOf(']') is var i and > 0 ? h[1..i] : h[1..];
        else if (h.Count(c => c == ':') == 1) name = h[..h.IndexOf(':')];
        else name = h;   // a name, or a bare IPv6 literal
        if (name.Length == 0) return false;
        if (IPAddress.TryParse(name, out _) || name == "localhost") return true;
        return extra is not null && extra.Any(e => e.ToLowerInvariant() == name || e.ToLowerInvariant() == h);
    }

    /// Why a request must be refused (403), or null: a cross-site/same-site fetch (Sec-Fetch-Site), a
    /// browser Origin that is not loopback or allowed, or a Host that is not ours (DNS rebinding).
    /// Playwright, Puppeteer and curl send none of these, so real clients are unaffected.
    public static string? RequestRefusal(IReadOnlyDictionary<string, string> headers,
        IReadOnlyList<string>? allowOrigins = null, IReadOnlyList<string>? allowHosts = null)
    {
        var origin = Header(headers, "origin");
        var site = (Header(headers, "sec-fetch-site") ?? "").ToLowerInvariant();
        if ((site is "cross-site" or "same-site") && !(origin is not null && allowOrigins is not null && allowOrigins.Contains(origin)))
            return "Forbidden: cross-site request";
        if (!OriginAllowed(origin, allowOrigins)) return "Forbidden origin";
        if (!HostAllowed(Header(headers, "host"), allowHosts)) return "Forbidden host";
        return null;
    }

    /// CLEARCOTE_SERVE_IDLE_TIMEOUT in seconds; 0 when unset or not a positive number.
    internal static double IdleTimeoutFromEnv()
        => double.TryParse((Environment.GetEnvironmentVariable("CLEARCOTE_SERVE_IDLE_TIMEOUT") ?? "").Trim(),
               NumberStyles.Float, CultureInfo.InvariantCulture, out var n) && double.IsFinite(n) && n > 0 ? n : 0;
}

/// A running multiplexer (from <see cref="Clearcote.ServeMultiplexAsync"/>). Pass <see cref="Url"/>
/// (optionally with <c>?fingerprint=...</c>) to ConnectOverCDPAsync. <see cref="CloseAsync"/> stops the
/// server and every browser.
public sealed class MultiplexServer : IAsyncDisposable
{
    private sealed class Child
    {
        public required string Id;
        public string? Seed;
        /// Canonical launch options: a later request for this id with different options gets 409.
        public required string Key;
        public required IReadOnlyDictionary<string, string> Params;
        public required IBrowserHandle Srv;
        public int Connections;
        public DateTimeOffset StartedAt;
        public DateTimeOffset? IdleSince;
        public CancellationTokenSource? IdleTimer;
    }

    private sealed record Request(string Method, string Target, List<KeyValuePair<string, string>> RawHeaders,
        IReadOnlyDictionary<string, string> Headers);

    private readonly MultiplexOptions _base;
    private readonly int _maxBrowsers;
    private readonly double _idleTimeoutSec;
    private readonly string? _dataDir;
    private readonly IReadOnlyList<string> _allowOrigins;
    private readonly IReadOnlyList<string> _allowHosts;
    private readonly int _readyTimeoutMs;
    private readonly Func<ServeOptions, Task<IBrowserHandle>> _startBrowser;
    private readonly TcpListener _listener;
    private readonly object _gate = new();
    private readonly Dictionary<string, Child> _children = new();
    private readonly Dictionary<string, (string Key, Task<Child> Task)> _pending = new();
    /// Ids whose browser is shutting down; a relaunch waits so two browsers never share a profile.
    private readonly Dictionary<string, Task> _closingIds = new();
    /// Ids closed while still launching: the launch closes its browser as soon as it is up.
    private readonly HashSet<string> _cancelled = new();
    /// Every open client and relay socket, ended on close.
    private readonly HashSet<Socket> _sockets = new();
    private bool _closing;
    private Task? _closeTask;
    private Task _acceptLoop = Task.CompletedTask;

    private MultiplexServer(MultiplexOptions options, TcpListener listener, string host)
    {
        _base = options;
        _listener = listener;
        Host = host;
        _maxBrowsers = options.MaxBrowsers ?? 16;
        _idleTimeoutSec = options.IdleTimeoutSec ?? Multiplex.IdleTimeoutFromEnv();
        _dataDir = options.DataDir;
        _allowOrigins = options.AllowOrigins ?? Array.Empty<string>();
        _allowHosts = options.AllowHosts ?? Array.Empty<string>();
        _readyTimeoutMs = options.ReadyTimeoutMs ?? 30_000;
        _startBrowser = options.StartBrowser ?? DefaultStartAsync;
        Port = ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    public string Host { get; }
    public int Port { get; }
    /// <c>http://host:port</c> — pass to ConnectOverCDPAsync (optionally with <c>?fingerprint=...</c>).
    public string Url => $"http://{(Host.Contains(':') ? $"[{Host}]" : Host)}:{Port}";

    internal static async Task<MultiplexServer> StartAsync(MultiplexOptions? options)
    {
        options = (MultiplexOptions)(options ?? new MultiplexOptions()).Clone();
        if (options.MaxBrowsers is < 1) throw new ArgumentException("maxBrowsers must be a whole number >= 1");
        var host = string.IsNullOrEmpty(options.Host) ? "127.0.0.1" : options.Host;
        var ip = IPAddress.TryParse(host.Trim('[', ']'), out var parsed) ? parsed
            : host == "localhost" ? IPAddress.Loopback
            : (await Dns.GetHostAddressesAsync(host).ConfigureAwait(false)).First();
        var listener = new TcpListener(ip, options.Port ?? 9222);
        listener.Start();
        var mux = new MultiplexServer(options, listener, host);
        mux._acceptLoop = Task.Run(mux.AcceptLoopAsync);
        var loopback = host is "127.0.0.1" or "localhost" or "::1";
        if (!loopback && !options.Quiet)
            Console.Error.WriteLine($"[clearcote] WARNING: ServeMultiplexAsync is bound to {host}. Anyone who can reach this port can " +
                "start browsers and control them. Keep it on 127.0.0.1 or put authentication in front of it.");
        if (!options.Quiet)
            Console.Error.WriteLine($"[clearcote] multiplexed CDP endpoint ready: {mux.Url}\n" +
                $"            ConnectOverCDPAsync(\"{mux.Url}?fingerprint=<seed>\")  -  status: GET {mux.Url}/");
        return mux;
    }

    private static async Task<IBrowserHandle> DefaultStartAsync(ServeOptions o)
        => new ServedBrowser(await Clearcote.ServeAsync(o).ConfigureAwait(false));

    private sealed class ServedBrowser : IBrowserHandle
    {
        private readonly Server _srv;
        public ServedBrowser(Server srv) => _srv = srv;
        public int Port => _srv.Port;
        public int? Pid => _srv.Pid;
        public Task CloseAsync() => _srv.CloseAsync();
    }

    // ── identities ───────────────────────────────────────────────────────────

    /// Snapshot of the running browsers.
    public MultiplexStatus Status()
    {
        lock (_gate)
            return new MultiplexStatus("ok", _children.Count, _maxBrowsers, _idleTimeoutSec,
                _children.Values.Select(c => new MultiplexProcess(c.Id, c.Seed, c.Srv.Pid, c.Srv.Port, c.Connections,
                    c.StartedAt, c.IdleSince, c.Params)).ToList());
    }

    /// Under _gate.
    private void ScheduleIdle(Child c)
    {
        c.IdleTimer?.Cancel();
        c.IdleTimer = null;
        if (c.Connections > 0) { c.IdleSince = null; return; }
        c.IdleSince = DateTimeOffset.UtcNow;
        if (_idleTimeoutSec > 0)
        {
            var timer = c.IdleTimer = new CancellationTokenSource();
            _ = Task.Delay(TimeSpan.FromSeconds(_idleTimeoutSec), timer.Token).ContinueWith(t =>
            {
                if (t.IsCanceled) return;
                bool current;
                lock (_gate) current = _children.TryGetValue(c.Id, out var now) && now == c && c.IdleTimer == timer;
                if (current) _ = CloseIdentityAsync(c.Id);
            }, TaskScheduler.Default);
        }
    }

    private static MultiplexRequestException Conflict(string id) => new(409,
        $"identity '{id}' is already running with different parameters; close it first (POST /fingerprint/{Uri.EscapeDataString(id)}/close)");

    private async Task<Child> EnsureChildAsync(ConnectionIdentity ident)
    {
        var key = Multiplex.OptionsKey(ident.Options);
        while (true)
        {
            Task? stillClosing = null;
            Task<Child>? inflight = null;
            TaskCompletionSource<Child>? launch = null;
            lock (_gate)
            {
                if (_closing) throw new MultiplexRequestException(503, "shutting down");
                if (_children.TryGetValue(ident.Id, out var existing))
                {
                    // Silently handing back a browser started with other options (e.g. without the proxy this
                    // request asks for) would send this client's traffic somewhere it did not choose.
                    if (existing.Key != key) throw Conflict(ident.Id);
                    ScheduleIdle(existing);   // a fresh /json/version means a client is about to connect
                    return existing;
                }
                if (_pending.TryGetValue(ident.Id, out var p))
                {
                    if (p.Key != key) throw Conflict(ident.Id);
                    inflight = p.Task;
                }
                else if (_closingIds.TryGetValue(ident.Id, out var c)) stillClosing = c;
                else
                {
                    if (_children.Count + _pending.Count >= _maxBrowsers)
                        throw new MultiplexRequestException(429, $"maxBrowsers ({_maxBrowsers}) reached; close an identity first");
                    _cancelled.Remove(ident.Id);
                    launch = new TaskCompletionSource<Child>(TaskCreationOptions.RunContinuationsAsynchronously);
                    _pending[ident.Id] = (key, launch.Task);
                }
            }
            if (stillClosing is not null) { try { await stillClosing.ConfigureAwait(false); } catch { } continue; }
            if (inflight is not null) return await inflight.ConfigureAwait(false);
            _ = LaunchAsync(ident, key, launch!);
            return await launch!.Task.ConfigureAwait(false);
        }
    }

    /// The ServeOptions an identity's browser starts with: the base options, the identity's own on top.
    internal ServeOptions ServeOptionsFor(ConnectionIdentity ident)
    {
        var o = new ServeOptions();
        foreach (var p in typeof(LaunchOptions).GetProperties(BindingFlags.Public | BindingFlags.Instance))
            if (p.CanRead && p.CanWrite && p.GetIndexParameters().Length == 0) p.SetValue(o, p.GetValue(_base));
        foreach (var (k, v) in ident.Options) Multiplex.OptionProperty(k).SetValue(o, v);
        o.Host = "127.0.0.1";
        o.Port = null;
        o.Quiet = true;
        o.ReadyTimeoutMs = _readyTimeoutMs;
        o.WindowSize = _base.WindowSize;
        // Profile directory named by a hash of the identity — never by a request value.
        o.UserDataDir = _dataDir is null ? null : Path.Combine(_dataDir, Multiplex.Sha256Hex($"clearcote:{ident.Id}")[..24]);
        return o;
    }

    private async Task LaunchAsync(ConnectionIdentity ident, string key, TaskCompletionSource<Child> launch)
    {
        Child? child = null;
        Exception? error = null;
        try
        {
            var srv = await _startBrowser(ServeOptionsFor(ident)).ConfigureAwait(false);
            bool closing, doomed;
            lock (_gate)
            {
                closing = _closing;
                doomed = closing || _cancelled.Remove(ident.Id);
                if (!doomed)
                {
                    child = new Child { Id = ident.Id, Seed = ident.Seed, Key = key, Params = ident.Params, Srv = srv, StartedAt = DateTimeOffset.UtcNow };
                    _children[ident.Id] = child;
                    ScheduleIdle(child);
                }
            }
            if (doomed)
            {
                try { await srv.CloseAsync().ConfigureAwait(false); } catch { }
                throw new MultiplexRequestException(503, closing ? "shutting down" : $"identity '{ident.Id}' was closed while starting");
            }
        }
        catch (Exception e) { error = e; }
        lock (_gate)
            if (_pending.TryGetValue(ident.Id, out var p) && p.Task == launch.Task) _pending.Remove(ident.Id);
        if (error is null) launch.SetResult(child!);
        else launch.SetException(error);
    }

    /// Close one identity's browser. True when one was running (or launching).
    public async Task<bool> CloseIdentityAsync(string id)
    {
        Child? c;
        Task<Child>? inflight = null;
        TaskCompletionSource? done = null;
        lock (_gate)
        {
            if (_children.Remove(id, out c))
            {
                c.IdleTimer?.Cancel();
                done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                _closingIds[id] = done.Task;
            }
            else if (_pending.TryGetValue(id, out var p))
            {
                _cancelled.Add(id);
                inflight = p.Task;
            }
            else return false;
        }
        if (inflight is not null)
        {
            try { await inflight.ConfigureAwait(false); } catch { }
            return true;
        }
        try { await c!.Srv.CloseAsync().ConfigureAwait(false); }
        catch { }
        finally
        {
            lock (_gate)
                if (_closingIds.TryGetValue(id, out var t) && t == done!.Task) _closingIds.Remove(id);
            done!.SetResult();
        }
        return true;
    }

    /// Stop the server and every browser (including launches still in flight). Closes once.
    public Task CloseAsync()
    {
        lock (_gate) return _closeTask ??= CloseCoreAsync();
    }

    public ValueTask DisposeAsync() => new(CloseAsync());

    private async Task CloseCoreAsync()
    {
        List<Socket> sockets;
        lock (_gate)
        {
            _closing = true;
            sockets = _sockets.ToList();
            _sockets.Clear();
        }
        try { _listener.Stop(); } catch { }
        // Keep-alive clients, requests waiting on a launch and relayed CDP sessions would hold the
        // server open indefinitely. Shutting down means ending them.
        foreach (var s in sockets) { try { s.Dispose(); } catch { } }
        try { await _acceptLoop.ConfigureAwait(false); } catch { }
        // Launches still in flight see _closing and close their own browser; wait for them, then
        // close the running ones and any close already under way.
        Task[] pending, closing;
        lock (_gate) pending = _pending.Values.Select(p => (Task)p.Task).ToArray();
        foreach (var t in pending) { try { await t.ConfigureAwait(false); } catch { } }
        string[] ids;
        lock (_gate) ids = _children.Keys.ToArray();
        await Task.WhenAll(ids.Select(CloseIdentityAsync)).ConfigureAwait(false);
        lock (_gate) closing = _closingIds.Values.ToArray();
        foreach (var t in closing) { try { await t.ConfigureAwait(false); } catch { } }
    }

    // ── HTTP front ───────────────────────────────────────────────────────────

    private async Task AcceptLoopAsync()
    {
        while (true)
        {
            Socket s;
            try { s = await _listener.AcceptSocketAsync().ConfigureAwait(false); }
            catch (Exception) when (Volatile.Read(ref _closing) || _listener.Server is not { IsBound: true }) { return; }
            catch { continue; }
            lock (_gate)
            {
                if (_closing) { s.Dispose(); return; }
                _sockets.Add(s);
            }
            _ = Task.Run(() => HandleAsync(s));
        }
    }

    private void Forget(Socket s)
    {
        lock (_gate) _sockets.Remove(s);
        try { s.Dispose(); } catch { }
    }

    private static readonly byte[] HeadEnd = "\r\n\r\n"u8.ToArray();

    /// The request head (up to the blank line) and whatever arrived after it; null on EOF, timeout or a
    /// head over 64 KiB.
    private static async Task<(string Head, byte[] Body)?> ReadHeadAsync(Stream stream)
    {
        var buf = new byte[65536];
        var len = 0;
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        while (len < buf.Length)
        {
            int n;
            try { n = await stream.ReadAsync(buf.AsMemory(len), cts.Token).ConfigureAwait(false); }
            catch { return null; }
            if (n == 0) return null;
            len += n;
            var end = buf.AsSpan(0, len).IndexOf(HeadEnd);
            if (end >= 0)
                return (Encoding.Latin1.GetString(buf, 0, end), buf.AsSpan(end + 4, len - end - 4).ToArray());
        }
        return null;
    }

    private static Request? ParseRequest(string head)
    {
        var lines = head.Split("\r\n");
        var parts = lines[0].Split(' ');
        if (parts.Length < 3) return null;
        var raw = new List<KeyValuePair<string, string>>();
        var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in lines.Skip(1))
        {
            var i = line.IndexOf(':');
            if (i <= 0) continue;
            var (k, v) = (line[..i].Trim(), line[(i + 1)..].Trim());
            raw.Add(new(k, v));
            headers.TryAdd(k.ToLowerInvariant(), v);
        }
        return new Request(parts[0], parts[1], raw, headers);
    }

    /// Path and query of a request target (origin-form or absolute-form).
    private static (string Path, string Search) SplitTarget(string target)
    {
        var t = target;
        var scheme = t.IndexOf("://", StringComparison.Ordinal);
        if (scheme > 0 && !t.StartsWith('/'))
        {
            var slash = t.IndexOf('/', scheme + 3);
            t = slash < 0 ? "/" : t[slash..];
        }
        var hash = t.IndexOf('#');
        if (hash >= 0) t = t[..hash];
        var q = t.IndexOf('?');
        return q < 0 ? (t, "") : (t[..q], q + 1 < t.Length ? t[q..] : "");
    }

    private async Task HandleAsync(Socket socket)
    {
        var stream = new NetworkStream(socket, ownsSocket: false);
        var relayed = false;
        try
        {
            if (await ReadHeadAsync(stream).ConfigureAwait(false) is not { } got) return;
            if (ParseRequest(got.Head) is not { } req) return;
            if (req.Headers.ContainsKey("upgrade"))
            {
                relayed = true;
                await UpgradeAsync(socket, stream, req, got.Body).ConfigureAwait(false);
                return;
            }
            // Drain a (small) body so the client's write completes before we answer and close.
            if (int.TryParse(req.Headers.GetValueOrDefault("content-length"), out var length))
            {
                var left = Math.Min(length, 1 << 20) - got.Body.Length;
                var sink = new byte[8192];
                while (left > 0)
                {
                    var n = await stream.ReadAsync(sink.AsMemory(0, Math.Min(left, sink.Length))).ConfigureAwait(false);
                    if (n == 0) break;
                    left -= n;
                }
            }
            var (code, body) = await RouteAsync(req).ConfigureAwait(false);
            var data = Encoding.UTF8.GetBytes(body.ToJsonString(Json));
            var headBytes = Encoding.Latin1.GetBytes(
                $"HTTP/1.1 {code} {Reason(code)}\r\nContent-Type: application/json; charset=utf-8\r\n" +
                $"Content-Length: {data.Length}\r\nConnection: close\r\n\r\n");
            await stream.WriteAsync(headBytes).ConfigureAwait(false);
            await stream.WriteAsync(data).ConfigureAwait(false);
            await stream.FlushAsync().ConfigureAwait(false);
            try { socket.Shutdown(SocketShutdown.Send); } catch { }
        }
        catch { }
        finally
        {
            if (!relayed) { stream.Dispose(); Forget(socket); }
        }
    }

    private static readonly JsonSerializerOptions Json = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    private static string Reason(int code) => code switch
    {
        200 => "OK", 400 => "Bad Request", 403 => "Forbidden", 404 => "Not Found", 405 => "Method Not Allowed",
        409 => "Conflict", 429 => "Too Many Requests", 502 => "Bad Gateway", 503 => "Service Unavailable", _ => "",
    };

    private static JsonObject Error(string message) => new() { ["error"] = message };

    private static string Iso(DateTimeOffset t) => t.UtcDateTime.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);

    private JsonObject StatusJson()
    {
        var s = Status();
        var procs = new JsonArray();
        foreach (var p in s.Processes)
        {
            var o = new JsonObject { ["id"] = p.Id };
            if (p.Seed is not null) o["seed"] = p.Seed;
            if (p.Pid is not null) o["pid"] = p.Pid;
            o["port"] = p.Port;
            o["connections"] = p.Connections;
            o["startedAt"] = Iso(p.StartedAt);
            if (p.IdleSince is { } idle) o["idleSince"] = Iso(idle);
            var prms = new JsonObject();
            foreach (var (k, v) in p.Params) prms[k] = v;
            o["params"] = prms;
            procs.Add(o);
        }
        return new JsonObject
        {
            ["status"] = s.Status, ["active"] = s.Active, ["maxBrowsers"] = s.MaxBrowsers,
            ["idleTimeoutSec"] = s.IdleTimeoutSec, ["processes"] = procs,
        };
    }

    private async Task<(int Code, JsonNode Body)> RouteAsync(Request req)
    {
        try
        {
            if (Multiplex.RequestRefusal(req.Headers, _allowOrigins, _allowHosts) is { } refusal) return (403, Error(refusal));
            var (rawPath, search) = SplitTarget(req.Target);
            var path = rawPath.TrimEnd('/');
            if (path.Length == 0) path = "/";
            // Microsoft.Playwright 1.49 builds the discovery URL by appending "/json/version/" to the endpoint
            // string, so ConnectOverCDPAsync("http://host:port?fingerprint=a") asks for
            // GET /?fingerprint=a/json/version/: the path lands at the end of the query. (Newer Playwright
            // releases append it to the URL's path.) A seed cannot contain '/', so nothing else matches.
            if (path == "/" && QueryTailPath.Match(search) is { Success: true } tail)
                (search, path) = (tail.Groups[1].Value, tail.Groups[2].Value);
            var wsBase = Multiplex.PublicWsBase(req.Headers, $"{Host}:{Port}");

            if (req.Method == "GET" && path == "/") return (200, StatusJson());

            var close = Regex.Match(path, "^/fingerprint/([^/]+)/close$");
            if (close.Success)
            {
                if (req.Method != "POST") return (405, Error("use POST"));
                var id = Uri.UnescapeDataString(close.Groups[1].Value);
                return (200, new JsonObject { ["id"] = id, ["terminated"] = await CloseIdentityAsync(id).ConfigureAwait(false) });
            }

            if (req.Method == "GET" && path is "/json/version" or "/json/list" or "/json")
            {
                var ident = Multiplex.ParseConnectionIdentity(search);
                var child = await EnsureChildAsync(ident).ConfigureAwait(false);
                var text = await ChildJsonAsync(child.Srv.Port, path == "/json" ? "/json/list" : path).ConfigureAwait(false);
                var data = JsonNode.Parse(text) ?? throw new InvalidDataException("empty answer from the browser");
                var hostPart = Regex.Replace(wsBase, "^wss?://", "");
                var prefix = child.Id == "default" ? "" : $"/fingerprint/{Uri.EscapeDataString(child.Id)}";
                void Fix(JsonNode? n)
                {
                    if (n is not JsonObject o) return;
                    if (o["webSocketDebuggerUrl"] is JsonValue w && w.TryGetValue<string>(out var ws))
                        o["webSocketDebuggerUrl"] = Multiplex.RewriteWsUrl(ws, wsBase, child.Id);
                    if (o["devtoolsFrontendUrl"] is JsonValue f && f.TryGetValue<string>(out var fe))
                        // "/devtools/inspector.html?ws=127.0.0.1:<childport>/devtools/page/<id>" -> via the multiplexer.
                        // An evaluator: a forwarded host must never be read as a substitution pattern.
                        o["devtoolsFrontendUrl"] = FrontendWs.Replace(fe, m => $"{m.Groups[1].Value}{hostPart}{prefix}{m.Groups[2].Value}", 1);
                }
                if (data is JsonArray arr) foreach (var item in arr) Fix(item);
                else Fix(data);
                return (200, data);
            }
            return (404, Error("not found"));
        }
        catch (MultiplexRequestException e) { return (e.Status, Error(e.Message)); }
        catch (Exception e) { return (502, Error(e.Message)); }
    }

    /// GET a child's /json/* answer over a connection of its own. Chrome's DevTools HTTP server keeps the
    /// connection open after answering, even to "Connection: close": read the head, then exactly
    /// Content-Length bytes (never to EOF).
    private static async Task<string> ChildJsonAsync(int port, string path)
    {
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var socket = new Socket(SocketType.Stream, ProtocolType.Tcp);
        await socket.ConnectAsync(IPAddress.Loopback, port, cts.Token).ConfigureAwait(false);
        using var stream = new NetworkStream(socket, ownsSocket: false);
        await stream.WriteAsync(Encoding.ASCII.GetBytes($"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"), cts.Token)
            .ConfigureAwait(false);
        var buf = new List<byte>();
        var chunk = new byte[16384];
        int headEnd = -1, length = -1;
        while (true)
        {
            var n = await stream.ReadAsync(chunk, cts.Token).ConfigureAwait(false);
            if (n == 0) break;
            buf.AddRange(chunk.AsSpan(0, n).ToArray());
            if (headEnd < 0)
            {
                headEnd = CollectionsMarshal.AsSpan(buf).IndexOf(HeadEnd);
                if (headEnd < 0) continue;
                var head = Encoding.Latin1.GetString(CollectionsMarshal.AsSpan(buf)[..headEnd]);
                var status = head.Split("\r\n")[0].Split(' ');
                if (status.Length < 2 || status[1] != "200") throw new InvalidDataException($"the browser answered {path} with {string.Join(' ', status.Skip(1))}");
                var cl = Regex.Match(head, @"\r\ncontent-length:\s*(\d+)", RegexOptions.IgnoreCase);
                if (cl.Success) length = int.Parse(cl.Groups[1].Value, CultureInfo.InvariantCulture);
            }
            if (length >= 0 && buf.Count - headEnd - 4 >= length) break;
        }
        if (headEnd < 0) throw new InvalidDataException("no answer from the browser");
        var bodyLen = length >= 0 ? Math.Min(length, buf.Count - headEnd - 4) : buf.Count - headEnd - 4;
        return Encoding.UTF8.GetString(CollectionsMarshal.AsSpan(buf).Slice(headEnd + 4, bodyLen));
    }

    private static readonly Regex QueryTailPath = new(@"^(\?.*?)(/json(?:/version|/list)?)/?$", RegexOptions.Singleline);

    private static readonly Regex FrontendWs =new("([?&]wss?=)[^/&]+(/devtools/)");

    // ── WebSocket relay ──────────────────────────────────────────────────────

    private async Task UpgradeAsync(Socket client, NetworkStream clientStream, Request req, byte[] rest)
    {
        async Task Reject(int code, string msg)
        {
            msg = msg.Replace('\r', ' ').Replace('\n', ' ');
            try
            {
                await clientStream.WriteAsync(Encoding.UTF8.GetBytes(
                    $"HTTP/1.1 {code} {msg}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n{msg}\n")).ConfigureAwait(false);
                client.Shutdown(SocketShutdown.Send);
            }
            catch { }
            finally { clientStream.Dispose(); Forget(client); }
        }

        Socket? upstream = null;
        try
        {
            if (Multiplex.RequestRefusal(req.Headers, _allowOrigins, _allowHosts) is { } refusal) { await Reject(403, refusal).ConfigureAwait(false); return; }
            if (!string.Equals(req.Headers["upgrade"], "websocket", StringComparison.OrdinalIgnoreCase))
            { await Reject(400, "Only WebSocket upgrades are relayed").ConfigureAwait(false); return; }
            var (path, search) = SplitTarget(req.Target);
            var id = "default";
            var rel = path;
            var m = Regex.Match(path, "^/fingerprint/([^/]+)(/devtools/.*)$", RegexOptions.Singleline);
            if (m.Success)
            {
                id = Uri.UnescapeDataString(m.Groups[1].Value);
                rel = m.Groups[2].Value;
            }
            else if (!path.StartsWith("/devtools/", StringComparison.Ordinal)) { await Reject(404, "Not Found").ConfigureAwait(false); return; }
            // The WebSocket route never starts a browser. A path carries only the id, not the proxy or
            // other options the identity was created with, so a browser started from it could send the
            // client's traffic direct; and its DevTools GUID would not match the URL anyway.
            Child? c;
            lock (_gate) _children.TryGetValue(id, out c);
            if (c is null)
            {
                await Reject(404, $"identity '{id}' is not running; connect via http://host:port/?fingerprint=... first").ConfigureAwait(false);
                return;
            }
            upstream = new Socket(SocketType.Stream, ProtocolType.Tcp);
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await upstream.ConnectAsync(IPAddress.Loopback, c.Srv.Port, cts.Token).ConfigureAwait(false);
            }
            catch
            {
                upstream.Dispose();
                await Reject(502, "Browser unreachable").ConfigureAwait(false);
                return;
            }
            lock (_gate)
            {
                if (!_closing && _children.TryGetValue(c.Id, out var now) && now == c)
                {
                    _sockets.Add(upstream);
                    c.Connections++;
                    ScheduleIdle(c);
                }
                else c = null;
            }
            if (c is null)
            {
                upstream.Dispose();
                await Reject(502, "Browser closed").ConfigureAwait(false);
                return;
            }

            // Once relaying, the stream belongs to the WebSocket: never write an HTTP response into it.
            var head = new StringBuilder($"GET {rel}{search} HTTP/1.1\r\n");
            foreach (var (k, v) in req.RawHeaders)
            {
                // The origin was checked above; the browser's own --remote-allow-origins check would
                // reject a forwarded browser Origin, and Host must name the child.
                if (k.Equals("host", StringComparison.OrdinalIgnoreCase) || k.Equals("origin", StringComparison.OrdinalIgnoreCase)) continue;
                head.Append(k).Append(": ").Append(v).Append("\r\n");
            }
            head.Append($"Host: 127.0.0.1:{c.Srv.Port}\r\n\r\n");
            using var upStream = new NetworkStream(upstream, ownsSocket: false);
            try
            {
                await upStream.WriteAsync(Encoding.Latin1.GetBytes(head.ToString())).ConfigureAwait(false);
                if (rest.Length > 0) await upStream.WriteAsync(rest).ConfigureAwait(false);
                // Tear down BOTH sides as soon as EITHER ends: a finished CDP session must not hold the
                // relay open, or its connection never counts as gone and idle close never runs.
                var a = Pump(clientStream, upStream);
                var b = Pump(upStream, clientStream);
                await Task.WhenAny(a, b).ConfigureAwait(false);
            }
            catch { }
            finally
            {
                clientStream.Dispose();
                Forget(client);
                Forget(upstream);
                lock (_gate)
                {
                    c.Connections = Math.Max(0, c.Connections - 1);
                    if (_children.TryGetValue(c.Id, out var now) && now == c) ScheduleIdle(c);
                }
            }
        }
        catch
        {
            clientStream.Dispose();
            Forget(client);
            if (upstream is not null) Forget(upstream);
        }
    }

    private static async Task Pump(Stream from, Stream to)
    {
        var buf = new byte[65536];
        try
        {
            int n;
            while ((n = await from.ReadAsync(buf).ConfigureAwait(false)) > 0)
                await to.WriteAsync(buf.AsMemory(0, n)).ConfigureAwait(false);
        }
        catch { }
    }
}
