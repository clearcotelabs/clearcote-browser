using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;
using Microsoft.Playwright;

namespace Clearcote;

/// <c>LaunchAsync(new() { Cloud = true })</c> and friends: create the session, connect over CDP, and hand
/// back the same Playwright types a local launch does.
///
/// C# cannot patch a method on an object the way the Node SDK does, so the browser (or context) comes
/// back wrapped in a <see cref="DispatchProxy"/>: every call goes straight to Playwright's own object
/// except CloseAsync/DisposeAsync, which also end the hosted session, and NewPageAsync/NewContextAsync,
/// which default to no emulated viewport. The proxy is generated at run time against whichever
/// Playwright is loaded, so a newer Playwright that adds members to IBrowser cannot break it the way a
/// hand-written implementation of the interface would.
internal static class CloudLaunch
{
    private static readonly string[] Truthy = { "1", "true", "yes" };

    /// Session info by browser/context, real and proxied (see <see cref="Cloud.SessionOf"/>).
    internal static readonly ConditionalWeakTable<object, JsonNode> Sessions = new();

    /// Test seam: stands in for Playwright's ConnectOverCDPAsync.
    internal static Func<string, BrowserTypeConnectOverCDPOptions, Task<IBrowser>>? ConnectOverride { get; set; }

    // LaunchOptions properties a cloud session takes (mapped onto the API body below) ...
    internal static readonly string[] SessionOptions =
    {
        "Fingerprint", "Platform", "Brand", "Timezone", "AcceptLanguage", "LightStealth", "Geoip", "Headless",
        "Proxy", "Version", "Identity", "Country", "State", "City", "ProxySession", "TimeoutSec", "IdleTimeoutSec",
        "MaxGb", "Profile", "Url", "Adblock", "SolveSliders", "SolveCheckboxes", "ChallengeService", "KeepAlive", "Record", "Note", "Worker",
    };

    // ... and those handled on THIS side: the switch, the account, and Playwright's connect options.
    internal static readonly string[] SdkSideOptions = { "Cloud", "CloudClient", "ApiKey", "ApiUrl", "Quiet", "SlowMo", "Timeout" };

    // How long the connect waits when the caller gives no Timeout (Playwright's own default is 30 s).
    // The browser starts as the client connects, and a launch with a country can take over 30 s; a
    // launch that fails is answered at once, so the longer wait only covers one in progress.
    internal const float DefaultConnectTimeoutMs = 120_000;

    /// Everything else LaunchOptions has only makes sense for a browser on this machine. Derived from the
    /// type itself, so an option added to LaunchOptions later is refused here until it is mapped.
    internal static readonly IReadOnlyList<PropertyInfo> LocalOnlyOptions = typeof(LaunchOptions)
        .GetProperties(BindingFlags.Public | BindingFlags.Instance)
        .Where(p => !SessionOptions.Contains(p.Name) && !SdkSideOptions.Contains(p.Name))
        .OrderBy(p => p.Name, StringComparer.Ordinal)
        .ToList();

    internal const string UserDataDirMsg =
        "a user data directory is not available for cloud browsers: a cloud browser keeps its cookies in a cloud " +
        "profile, so set Profile = \"name\" instead (cloud.Profiles.SyncAsync can fill one from a local profile directory)";

    /// Whether a launch goes to the cloud: an explicit <see cref="LaunchOptions.Cloud"/> wins, a
    /// <see cref="LaunchOptions.CloudClient"/> means cloud, otherwise CLEARCOTE_CLOUD decides.
    internal static bool Requested(LaunchOptions? o)
    {
        if (o?.Cloud is bool b) return b;
        if (o?.CloudClient is not null) return true;
        var env = (Environment.GetEnvironmentVariable("CLEARCOTE_CLOUD") ?? "").Trim().ToLowerInvariant();
        return Truthy.Contains(env);
    }

    /// The cloud session options of a launch; throws naming the first option a cloud browser cannot take.
    internal static CloudSessionOptions SessionOptionsOf(LaunchOptions o)
    {
        var defaults = new LaunchOptions();
        foreach (var p in LocalOnlyOptions)
        {
            var value = p.GetValue(o);
            if (value is not null && !Equals(value, p.GetValue(defaults)))
                throw new ArgumentException($"{p.Name} is not available for cloud browsers");
        }
        return new CloudSessionOptions
        {
            Fingerprint = o.Fingerprint, Platform = o.Platform, Brand = o.Brand, Timezone = o.Timezone,
            Locale = o.AcceptLanguage, LightStealth = o.LightStealth,
            // Geoip is a plain bool locally, so "unset" and "false" look the same: send it only when on,
            // and leave the server's own default (on when neither Timezone nor AcceptLanguage is set) alone.
            Geoip = o.Geoip ? true : null,
            Headless = o.Headless, Proxy = o.Proxy, Version = o.Version, Identity = o.Identity,
            Country = o.Country, State = o.State, City = o.City, ProxySession = o.ProxySession,
            TimeoutSec = o.TimeoutSec, IdleTimeoutSec = o.IdleTimeoutSec, MaxGb = o.MaxGb, Profile = o.Profile,
            Url = o.Url, Adblock = o.Adblock, SolveSliders = o.SolveSliders, SolveCheckboxes = o.SolveCheckboxes, ChallengeService = o.ChallengeService,
            KeepAlive = o.KeepAlive, Record = o.Record, Note = o.Note, Worker = o.Worker,
        };
    }

    internal static async Task<IBrowser> LaunchBrowserAsync(LaunchOptions o)
    {
        var body = Cloud.SessionBody(SessionOptionsOf(o), run: false);
        var (browser, _) = await ConnectAsync(o, body, persistent: false).ConfigureAwait(false);
        return browser;
    }

    /// <paramref name="persistent"/>: a cloud profile is required, loaded and saved back on close (the
    /// cloud LaunchPersistentContextAsync). Otherwise the session's own fresh context (the cloud
    /// LaunchEphemeralProfileAsync). Either way closing the context ends the session.
    internal static async Task<IBrowserContext> LaunchContextAsync(LaunchOptions o, bool persistent, string? userDataDir)
    {
        if (userDataDir is not null) throw new ArgumentException(UserDataDirMsg);
        var session = SessionOptionsOf(o);
        if (persistent)
        {
            if (session.Profile is null)
                throw new ArgumentException(
                    "LaunchPersistentContextAsync with Cloud needs Profile = \"name\": the cloud profile whose cookies it loads and saves back when it closes");
            session.Profile = new CloudProfile { Name = session.Profile.Name, Persist = session.Profile.Persist ?? true };
        }
        var (_, context) = await ConnectAsync(o, Cloud.SessionBody(session, run: false), persistent: true).ConfigureAwait(false);
        return context!;
    }

    /// Create the session and connect; nothing is left behind when a step after the create fails: the
    /// connection is closed and the session stopped (keep-alive or not), so a failed launch never keeps a
    /// billed browser running.
    private static async Task<(IBrowser Browser, IBrowserContext? Context)> ConnectAsync(LaunchOptions o, JsonObject body, bool persistent)
    {
        var client = o.CloudClient ?? new Cloud(o.ApiKey, o.ApiUrl);
        var created = await client.Browsers.CreateRawAsync(body).ConfigureAwait(false);
        var id = Cloud.Str(created?["id"]);
        var state = new SessionState(client, id, keepAlive: body["keepAlive"] is JsonValue v && v.TryGetValue<bool>(out var k) && k);
        IBrowser? real = null;
        try
        {
            var connectUrl = Cloud.Str(created?["connectUrl"])
                ?? throw new CloudException(200, null, $"the API created session {id} but sent no connectUrl");
            var connect = ConnectOverride ?? (async (url, opts) =>
                await (await Clearcote.PlaywrightInstanceAsync().ConfigureAwait(false)).Chromium.ConnectOverCDPAsync(url, opts).ConfigureAwait(false));
            real = await connect(connectUrl, new BrowserTypeConnectOverCDPOptions
            {
                SlowMo = o.SlowMo, Timeout = o.Timeout ?? DefaultConnectTimeoutMs,
            }).ConfigureAwait(false);
            // A disconnect by any route (CloseAsync on the proxy or on context.Browser, a lost connection)
            // ends the session; the proxy's CloseAsync also waits for it.
            real.Disconnected += (_, _) => _ = state.EndAsync();

            var info = created is JsonObject obj ? (JsonObject)obj.DeepClone() : new JsonObject();
            info.Remove("connectUrl");   // single-use and holds a token
            var browser = BrowserProxy.Wrap(real, state);
            Sessions.AddOrUpdate(real, info);
            Sessions.AddOrUpdate(browser, info);
            if (!persistent) return (browser, null);

            var ctx = real.Contexts.FirstOrDefault()
                ?? await real.NewContextAsync(new BrowserNewContextOptions { ViewportSize = ViewportSize.NoViewport }).ConfigureAwait(false);
            var context = ContextProxy.Wrap(ctx, browser);
            Sessions.AddOrUpdate(ctx, info);
            Sessions.AddOrUpdate(context, info);
            return (browser, context);
        }
        catch
        {
            if (real is not null) { try { await real.CloseAsync().ConfigureAwait(false); } catch { } }
            await state.EndAsync(force: true).ConfigureAwait(false);
            throw;
        }
    }

    /// Ends the hosted session at most once (DELETE /api/v1/browsers/{id}), best-effort: the gateway also
    /// ends a session whose client goes. A keep-alive session is left running on a normal close.
    internal sealed class SessionState
    {
        private readonly Cloud _client;
        private readonly string? _id;
        private readonly bool _keepAlive;
        private readonly object _gate = new();
        private Task? _stop;

        public SessionState(Cloud client, string? id, bool keepAlive)
        {
            _client = client;
            _id = id;
            _keepAlive = keepAlive;
        }

        public Task EndAsync(bool force = false)
        {
            if (_keepAlive && !force) return Task.CompletedTask;
            lock (_gate) return _stop ??= StopQuietlyAsync();
        }

        private async Task StopQuietlyAsync()
        {
            if (string.IsNullOrEmpty(_id)) return;
            try { await _client.Browsers.StopAsync(_id).ConfigureAwait(false); }
            catch { /* best-effort */ }
        }
    }

    private static object? Forward(object target, MethodInfo method, object?[]? args)
    {
        try { return method.Invoke(target, args); }
        catch (TargetInvocationException e) when (e.InnerException is not null)
        {
            ExceptionDispatchInfo.Capture(e.InnerException).Throw();
            throw;
        }
    }

    /// A cloud browser: Playwright's own IBrowser, except that closing it also ends the hosted session
    /// and a new page or context gets no emulated viewport unless one is asked for (a local launch's
    /// default: an emulated 1280x720 on top of the real window is an impossible-window tell).
    public class BrowserProxy : DispatchProxy
    {
        internal IBrowser Target = null!;
        internal SessionState State = null!;

        internal static IBrowser Wrap(IBrowser target, SessionState state)
        {
            var p = Create<IBrowser, BrowserProxy>();
            var bp = (BrowserProxy)(object)p;
            bp.Target = target;
            bp.State = state;
            return p;
        }

        internal async Task CloseAsync(MethodInfo? close, object?[]? args)
        {
            try
            {
                if (close is not null) await ((Task)Forward(Target, close, args)!).ConfigureAwait(false);
                else await Target.CloseAsync().ConfigureAwait(false);
            }
            finally
            {
                await State.EndAsync().ConfigureAwait(false);
            }
        }

        protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        {
            ArgumentNullException.ThrowIfNull(targetMethod);
            switch (targetMethod.Name)
            {
                case "CloseAsync":
                    return CloseAsync(targetMethod, args);
                case "DisposeAsync":
                    return new ValueTask(CloseAsync(null, null));
                case "NewPageAsync" when args is { Length: 1 }:
                    var page = args[0] is BrowserNewPageOptions po ? new BrowserNewPageOptions(po) : new BrowserNewPageOptions();
                    page.ViewportSize ??= ViewportSize.NoViewport;
                    args[0] = page;
                    break;
                case "NewContextAsync" when args is { Length: 1 }:
                    var ctx = args[0] is BrowserNewContextOptions co ? new BrowserNewContextOptions(co) : new BrowserNewContextOptions();
                    ctx.ViewportSize ??= ViewportSize.NoViewport;
                    args[0] = ctx;
                    break;
            }
            return Forward(Target, targetMethod, args);
        }
    }

    /// The context of a persistent (or ephemeral) cloud launch: closing it closes the browser, which ends
    /// the session (and, with a persisted profile, saves its cookies). <c>context.Browser</c> is the
    /// cloud browser, so closing that does the same.
    public class ContextProxy : DispatchProxy
    {
        internal IBrowserContext Target = null!;
        internal IBrowser Browser = null!;

        internal static IBrowserContext Wrap(IBrowserContext target, IBrowser browser)
        {
            var p = Create<IBrowserContext, ContextProxy>();
            var cp = (ContextProxy)(object)p;
            cp.Target = target;
            cp.Browser = browser;
            return p;
        }

        protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        {
            ArgumentNullException.ThrowIfNull(targetMethod);
            return targetMethod.Name switch
            {
                "CloseAsync" => Browser.CloseAsync(),
                "DisposeAsync" => new ValueTask(Browser.CloseAsync()),
                "get_Browser" => Browser,
                _ => Forward(Target, targetMethod, args),
            };
        }
    }
}
