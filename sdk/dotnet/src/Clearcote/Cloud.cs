using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

// Clearcote Cloud: hosted browsers, agent runs, profiles, recordings, events, hand-off and webhooks.
//
// The same SDK runs a browser on this machine or on Clearcote's servers, and one switch picks which:
//
//   var browser = await Clearcote.LaunchAsync(new() { Cloud = true, Country = "us" }); // or CLEARCOTE_CLOUD=1
//   var page = await browser.NewPageAsync();                                           // the same Playwright IBrowser
//   await browser.CloseAsync();                                                        // disconnects, ends the session
//
// Everything else the hosted API does is on Cloud:
//
//   var cloud = new Cloud();   // CLEARCOTE_API_KEY; CLEARCOTE_API_URL points it at another server
//   var run = await cloud.Runs.CreateAsync("Find the price of the cheapest plan", new() { Url = "https://example.com" });
//   Console.WriteLine(run?["status"]);
//
// Conventions (the same as the Node and Python SDKs):
//   * Every method returns the parsed JSON body of the endpoint it calls (null for an empty body).
//   * Every non-2xx answer throws a CloudException carrying the server's own message and code.
//   * Options keep the API's own names; the API validates the values, so the SDK keeps no second copy
//     of the server's rules that could drift. Durations of the SDK's own waits are in SECONDS.

/// A request the hosted API refused or could not answer. <see cref="Status"/> is the HTTP status (0
/// when the server could not be reached), <see cref="Code"/> the API's machine-readable code when it sent
/// one ("PROFILE_IN_USE", "NOT_READY", ...; otherwise null) and the message is the API's own explanation.
public class CloudException : Exception
{
    public int Status { get; }
    public string? Code { get; }
    public CloudException(int status, string? code, string message) : base(message)
    {
        Status = status;
        Code = code;
    }
}

/// A wait ran out of time. The run (or hand-off) carries on on the server; <see cref="Last"/> holds the
/// last view the SDK read, so its id and status are at hand.
public class CloudTimeoutException : Exception
{
    public JsonNode? Last { get; }
    public CloudTimeoutException(string message, JsonNode? last) : base(message) { Last = last; }
}

/// A webhook delivery whose Clearcote-Signature does not check out (see <see cref="Cloud.VerifyWebhook(string, string?, string, int?, long?)"/>).
public class WebhookSignatureException : Exception
{
    public WebhookSignatureException(string message) : base(message) { }
}

/// A cloud profile is a NAMED COOKIE STORE on the server: a name loads it, and <c>Persist = true</c>
/// also saves it back when the session ends. A string converts to a profile that loads only:
/// <c>Profile = "acct-1"</c>.
public sealed class CloudProfile
{
    public string Name { get; set; } = "";
    /// Save the cookies back when the session ends. Null leaves it to the server (load only).
    public bool? Persist { get; set; }
    public static implicit operator CloudProfile(string name) => new() { Name = name };
}

/// The browser options of a cloud session (POST /api/v1/browsers), by their API names.
public class CloudSessionOptions
{
    public string? Fingerprint { get; set; }
    /// A stable device label: the same identity gets the same device on every session.
    public string? Identity { get; set; }
    public string? Platform { get; set; }
    public string? Brand { get; set; }
    public string? Timezone { get; set; }
    public string? Locale { get; set; }
    public bool? Geoip { get; set; }
    public bool? Headless { get; set; }
    public bool? LightStealth { get; set; }
    /// <c>Server = "managed"</c> for the included residential pool, or your own proxy (a URL with the
    /// credentials inline, or Server + Username + Password).
    public ProxyOptions? Proxy { get; set; }
    public string? Country { get; set; }
    public string? State { get; set; }
    public string? City { get; set; }
    public string? ProxySession { get; set; }
    public int? TimeoutSec { get; set; }
    public int? IdleTimeoutSec { get; set; }
    public double? MaxGb { get; set; }
    public string? Version { get; set; }
    public CloudProfile? Profile { get; set; }
    /// The page the session opens on.
    public string? Url { get; set; }
    public bool? Adblock { get; set; }
    /// Drag slide-to-verify challenges automatically (the server's default is on); false turns it off.
    public bool? SolveSliders { get; set; }
    /// Keep the session running after the client disconnects (stop it with Browsers.StopAsync).
    public bool? KeepAlive { get; set; }
    public bool? Record { get; set; }
    public string? Note { get; set; }
    public string? Worker { get; set; }
}

/// A run's own options plus the browser options of its session (<see cref="CloudRuns.CreateAsync"/>).
public class RunOptions : CloudSessionOptions
{
    /// JSON Schema (draft-07 subset) of the output; top-level type "object" or "array".
    public JsonNode? Schema { get; set; }
    /// Run-scoped secrets, referred to in the task as {{name}}; the model never sees the values. Each
    /// value is a string, or an object such as <c>new { value = "...", domains = new[] { "example.com" } }</c>
    /// or <c>new { totp = "BASE32", digits = 6 }</c>.
    public IDictionary<string, object>? Secrets { get; set; }
    public int? MaxSteps { get; set; }
    /// On needs_input / blocked, pause and hand the browser to a person.
    public bool? Handoff { get; set; }
    public int? HandoffTimeoutSec { get; set; }
    /// Wait for the run to finish (default true); false returns the create answer at once.
    public bool Wait { get; set; } = true;
    /// Seconds to wait before throwing <see cref="CloudTimeoutException"/> (default: no limit).
    public double? Timeout { get; set; }
    /// Seconds between polls (default 1.5).
    public double Poll { get; set; } = 1.5;
    /// Called whenever the status or the hand-off changes. Without it, a run that pauses for a person
    /// is reported on stderr with its live link.
    public Func<JsonNode, Task>? OnUpdate { get; set; }
}

/// Where <see cref="CloudProfiles.SyncAsync"/> reads the cookies from: exactly one source.
public class ProfileSyncOptions
{
    /// A local Chrome/Clearcote profile directory, read by a headless Clearcote started on it.
    public string? FromProfile { get; set; }
    /// A browser you already run with remote debugging, e.g. http://127.0.0.1:9222.
    public string? FromCdp { get; set; }
    /// A Playwright storage-state file, or a JSON array of cookies.
    public string? FromFile { get; set; }
    /// A visible Clearcote on a throwaway profile opens this page; you sign in, then <see cref="Confirm"/>
    /// completes (default: press Enter in this console).
    public string? LoginUrl { get; set; }
    /// Only the cookies a browser would use on these domains are uploaded: those of each domain, of its
    /// subdomains, and of its parent domains (.example.com for www.example.com).
    public IEnumerable<string>? Domains { get; set; }
    /// Upload every cookie. Needed explicitly: with neither this nor <see cref="Domains"/>, nothing is read.
    public bool AllDomains { get; set; }
    /// Replace what the profile had instead of merging into it.
    public bool Replace { get; set; }
    public Func<Task>? Confirm { get; set; }
    /// Test seam: stands in for <see cref="Clearcote.ServeAsync"/>.
    internal Func<ServeOptions, Task<CloudServed>>? Serve { get; set; }
}

/// What profile sync needs of ServeAsync: a CDP endpoint on a local Clearcote, and a way to stop it.
internal sealed record CloudServed(string CdpUrl, Func<bool> IsAlive, Func<Task> Close);

// ── HTTP ─────────────────────────────────────────────────────────────────────────────────────────

internal sealed class CloudHttp
{
    private static readonly string UserAgent = $"clearcote-sdk-dotnet/{Clearcote.Version}";
    private readonly string _key;            // never printed: Cloud.ToString() shows the base URL only
    private readonly HttpClient _http;

    public string BaseUrl { get; }
    public TimeSpan Timeout { get; }

    public CloudHttp(string key, string baseUrl, TimeSpan timeout)
    {
        _key = key;
        BaseUrl = baseUrl;
        Timeout = timeout;
        // Redirects are NOT followed: the recording endpoint redirects to presigned storage, which must
        // not receive the API key.
        _http = new HttpClient(new SocketsHttpHandler { AllowAutoRedirect = false }) { Timeout = timeout };
    }

    public string Url(string path, IEnumerable<KeyValuePair<string, object?>>? query = null)
    {
        var parts = new List<string>();
        foreach (var (k, v) in query ?? Array.Empty<KeyValuePair<string, object?>>())
        {
            if (v is null) continue;
            var s = v switch { true => "1", false => "0", IFormattable f => f.ToString(null, System.Globalization.CultureInfo.InvariantCulture), _ => v.ToString() ?? "" };
            parts.Add($"{Uri.EscapeDataString(k)}={Uri.EscapeDataString(s)}");
        }
        return BaseUrl + path + (parts.Count > 0 ? "?" + string.Join("&", parts) : "");
    }

    /// (status, location, text). 3xx comes back as is (never followed); 4xx/5xx throw.
    public async Task<(int Status, string? Location, string Text)> RawAsync(
        HttpMethod method, string path, JsonNode? body = null, IEnumerable<KeyValuePair<string, object?>>? query = null)
    {
        using var req = new HttpRequestMessage(method, Url(path, query));
        req.Headers.TryAddWithoutValidation("Authorization", $"Bearer {_key}");
        req.Headers.TryAddWithoutValidation("Accept", "application/json");
        req.Headers.TryAddWithoutValidation("User-Agent", UserAgent);
        if (body is not null) req.Content = new StringContent(body.ToJsonString(), Encoding.UTF8, "application/json");
        HttpResponseMessage res;
        try { res = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false); }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException)
        {
            throw new CloudException(0, "NETWORK", $"could not reach {BaseUrl}: {CauseOf(e)}");
        }
        using (res)
        {
            string text;
            // the connection was cut (or the timeout hit) while the answer was being read
            try { text = await res.Content.ReadAsStringAsync().ConfigureAwait(false); }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException)
            {
                throw new CloudException(0, "NETWORK", $"could not reach {BaseUrl}: {CauseOf(e)}");
            }
            var status = (int)res.StatusCode;
            if (status is >= 300 and < 400) return (status, res.Headers.Location?.OriginalString, text);
            if (status is < 200 or >= 300) throw Cloud.ApiError(status, text, res.ReasonPhrase ?? "");
            return (status, null, text);
        }
    }

    public async Task<JsonNode?> CallAsync(HttpMethod method, string path, JsonNode? body = null, IEnumerable<KeyValuePair<string, object?>>? query = null)
    {
        var r = await RawAsync(method, path, body, query).ConfigureAwait(false);
        if (r.Status is >= 300 and < 400) throw new CloudException(r.Status, null, $"unexpected redirect to {r.Location}");
        return Cloud.ParseJson(r.Status, r.Text);
    }

    public static string CauseOf(Exception e)
    {
        if (e is TaskCanceledException) return "the request timed out";
        var inner = e;
        while (inner.InnerException is not null) inner = inner.InnerException;
        return inner.Message;
    }

    public static string Seg(string segment) => Uri.EscapeDataString(segment);
}

// ── polling ──────────────────────────────────────────────────────────────────────────────────────

/// The decisions a wait makes on every poll (the same as the Node and Python SDKs): has it finished,
/// should the caller hear about it, is an error worth another try, how long to sleep.
internal sealed class CloudWatch
{
    // Errors worth another poll rather than giving up on a long wait: no connection, rate limited, or a
    // gateway in front of the API restarting. Anything else (401, 404, ...) will not fix itself.
    private static readonly int[] Transient = { 0, 429, 502, 503, 504 };
    private const int MaxTransient = 4;

    private readonly string _what;
    private readonly double? _timeout;
    private readonly Func<JsonNode?, bool> _done;
    private readonly Func<JsonNode?, string> _key;
    private readonly DateTime? _deadline;
    private string _lastKey = "\0";
    private int _failures;

    public JsonNode? Last { get; private set; }
    public bool SeenAny { get; private set; }

    public CloudWatch(string what, double? timeout, Func<JsonNode?, bool> done, Func<JsonNode?, string> key)
    {
        _what = what;
        _timeout = timeout;
        _done = done;
        _key = key;
        _deadline = timeout is null ? null : DateTime.UtcNow.AddSeconds(timeout.Value);
    }

    public (bool Done, bool Changed) Seen(JsonNode? view)
    {
        _failures = 0;
        Last = view;
        SeenAny = true;
        var k = _key(view);
        var changed = k != _lastKey;
        _lastKey = k;
        return (_done(view), changed);
    }

    public bool Retry(Exception e)
    {
        if (e is CloudException ce && Array.IndexOf(Transient, ce.Status) >= 0 && _failures < MaxTransient)
        {
            _failures++;
            return true;
        }
        return false;
    }

    public TimeSpan Pause(double poll)
    {
        if (_deadline is null) return TimeSpan.FromSeconds(Math.Max(0, poll));
        var left = (_deadline.Value - DateTime.UtcNow).TotalSeconds;
        if (left <= 0)
        {
            var status = Cloud.Str(Last?["status"]);
            throw new CloudTimeoutException(
                $"{_what} is still {(string.IsNullOrEmpty(status) ? "not finished" : status)} after {_timeout}s; it carries on on the server", Last);
        }
        return TimeSpan.FromSeconds(Math.Max(0, Math.Min(poll, left)));
    }
}

// ── resources ────────────────────────────────────────────────────────────────────────────────────

/// Hosted browser sessions: /api/v1/browsers.
public sealed class CloudBrowsers
{
    private readonly CloudHttp _http;
    internal CloudBrowsers(CloudHttp http) { _http = http; }

    /// Start a session; returns { id, connectUrl, expiresAt, ... }. For a connected Playwright browser
    /// in one step use <c>Clearcote.LaunchAsync(new() { Cloud = true, ... })</c> instead.
    public Task<JsonNode?> CreateAsync(CloudSessionOptions? options = null)
        => CreateRawAsync(Cloud.SessionBody(options ?? new CloudSessionOptions(), run: false));

    internal Task<JsonNode?> CreateRawAsync(JsonObject body) => _http.CallAsync(HttpMethod.Post, "/api/v1/browsers", body);

    public Task<JsonNode?> GetAsync(string id) => _http.CallAsync(HttpMethod.Get, $"/api/v1/browsers/{CloudHttp.Seg(id)}");

    /// { balanceEur, sessions: [...] }, newest first.
    public Task<JsonNode?> ListAsync(IEnumerable<string>? status = null, string? note = null, int? limit = null, string? before = null)
        => _http.CallAsync(HttpMethod.Get, "/api/v1/browsers", null, new KeyValuePair<string, object?>[]
        {
            new("status", status is null ? null : string.Join(",", status)), new("note", note), new("limit", limit), new("before", before),
        });

    public Task<JsonNode?> StopAsync(string id) => _http.CallAsync(HttpMethod.Delete, $"/api/v1/browsers/{CloudHttp.Seg(id)}");

    /// A live-view WebSocket for a running session (<paramref name="control"/> may also drive it).
    public Task<JsonNode?> LiveAsync(string id, bool control = false)
        => _http.CallAsync(HttpMethod.Get, $"/api/v1/browsers/{CloudHttp.Seg(id)}/live", null,
            new KeyValuePair<string, object?>[] { new("control", control ? "1" : null) });

    /// A link anyone can open: the live view (<paramref name="control"/> to let them drive) or, with
    /// <paramref name="recording"/>, the session's recording.
    public Task<JsonNode?> ShareAsync(string id, bool? control = null, int? minutes = null, bool? recording = null)
        => _http.CallAsync(HttpMethod.Post, $"/api/v1/browsers/{CloudHttp.Seg(id)}/share",
            Cloud.Compact(("control", control), ("minutes", minutes), ("recording", recording)));

    /// Hand a running session to a person: { state: "waiting", liveUrl, expiresAt, ... }.
    public Task<JsonNode?> HandoffAsync(string id, string? reason = null, int? timeoutSec = null)
        => _http.CallAsync(HttpMethod.Post, $"/api/v1/browsers/{CloudHttp.Seg(id)}/handoff",
            Cloud.Compact(("reason", reason), ("timeoutSec", timeoutSec)));

    /// Mark a waiting hand-off done (what the live page's "I'm done" button does).
    public Task<JsonNode?> HandoffDoneAsync(string id)
        => _http.CallAsync(HttpMethod.Post, $"/api/v1/browsers/{CloudHttp.Seg(id)}/handoff/done", new JsonObject());

    /// Poll until the session's hand-off is no longer waiting (done, or timed out on the server); returns
    /// the session view. <paramref name="timeout"/> (seconds) throws <see cref="CloudTimeoutException"/>.
    /// A session with no hand-off at all throws CloudException NO_HANDOFF rather than returning at once
    /// as if a person had finished.
    public async Task<JsonNode?> WaitHandoffAsync(string id, double? timeout = null, double poll = 2)
    {
        var watch = new CloudWatch($"the hand-off of {id}", timeout,
            v => Cloud.Str(v?["handoff"]?["state"]) != "waiting", v => Cloud.Str(v?["handoff"]?["state"]) ?? "null");
        while (true)
        {
            JsonNode? view;
            try { view = await GetAsync(id).ConfigureAwait(false); }
            catch (Exception e) when (watch.Retry(e))
            {
                await Task.Delay(watch.Pause(poll)).ConfigureAwait(false);
                continue;
            }
            if (!watch.SeenAny && view?["handoff"] is null)
                throw new CloudException(200, "NO_HANDOFF", $"no hand-off was requested for session {id} (request one with Browsers.HandoffAsync first)");
            if (watch.Seen(view).Done) return view;
            await Task.Delay(watch.Pause(poll)).ConfigureAwait(false);
        }
    }

    /// One page of the session's event timeline: { events: [{ seq, at, type, data }], next }. Pass
    /// <c>next</c> back as <paramref name="after"/> for the following page; it is null at the end.
    public Task<JsonNode?> EventsAsync(string id, long after = 0, int? limit = null)
        => _http.CallAsync(HttpMethod.Get, $"/api/v1/browsers/{CloudHttp.Seg(id)}/events", null,
            new KeyValuePair<string, object?>[] { new("after", after), new("limit", limit) });

    /// A short-lived URL of the session's MP4. Throws CloudException 409 NOT_READY while it is still
    /// being processed, and 404 when the session was not recorded.
    public async Task<string> RecordingUrlAsync(string id)
    {
        var r = await _http.RawAsync(HttpMethod.Get, $"/api/v1/browsers/{CloudHttp.Seg(id)}/recording").ConfigureAwait(false);
        string? url = null;
        if (r.Status is >= 300 and < 400 && !string.IsNullOrEmpty(r.Location))
            url = new Uri(new Uri(_http.BaseUrl + "/"), r.Location).AbsoluteUri;
        else if (Cloud.ParseJson(r.Status, r.Text) is JsonObject o && Cloud.Str(o["url"]) is { Length: > 0 } u)
            url = u;
        // only a web URL: the storage host gets an unauthenticated GET, never a file:// read
        if (url is null || !Regex.IsMatch(url, "^https?:", RegexOptions.IgnoreCase))
            throw new CloudException(r.Status, null, "the API did not answer with a recording URL");
        return url;
    }

    /// Save the session's recording to <paramref name="path"/>; returns the path. The storage URL is
    /// presigned, so the API key is NOT sent to it.
    public async Task<string> DownloadRecordingAsync(string id, string path)
    {
        var url = await RecordingUrlAsync(id).ConfigureAwait(false);
        var part = path + ".part";
        try
        {
            using var http = new HttpClient { Timeout = _http.Timeout > TimeSpan.FromMinutes(10) ? _http.Timeout : TimeSpan.FromMinutes(10) };
            using var req = new HttpRequestMessage(HttpMethod.Get, url);
            req.Headers.TryAddWithoutValidation("User-Agent", $"clearcote-sdk-dotnet/{Clearcote.Version}");
            using var res = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false);
            if (!res.IsSuccessStatusCode)
                throw new CloudException((int)res.StatusCode, null, $"downloading the recording failed: HTTP {(int)res.StatusCode}");
            await using (var file = File.Create(part))
                await res.Content.CopyToAsync(file).ConfigureAwait(false);
            File.Move(part, path, overwrite: true);
            return path;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or IOException)
        {
            try { File.Delete(part); } catch { }
            throw new CloudException(0, "NETWORK", $"downloading the recording failed: {CloudHttp.CauseOf(e)}");
        }
        catch
        {
            try { File.Delete(part); } catch { }
            throw;
        }
    }
}

/// Agent runs: a task in, JSON out. /api/v1/runs.
public sealed class CloudRuns
{
    /// A run is finished in exactly these states. "waiting_for_human" is NOT one of them: the run is
    /// paused on a hand-off and resumes once the person marks it done (or the hand-off times out).
    public static readonly IReadOnlyList<string> TerminalStatuses = new[] { "succeeded", "failed", "cancelled", "expired" };

    private readonly CloudHttp _http;
    internal CloudRuns(CloudHttp http) { _http = http; }

    /// Start a run. With <see cref="RunOptions.Wait"/> (the default) poll until it finishes and return
    /// the run (status, result with output, costEur, ...); otherwise return the create answer at once.
    public async Task<JsonNode?> CreateAsync(string task, RunOptions? options = null)
    {
        options ??= new RunOptions();
        var created = await _http.CallAsync(HttpMethod.Post, "/api/v1/runs", Cloud.RunBody(task, options)).ConfigureAwait(false);
        if (!options.Wait) return created;
        var id = Cloud.Str(created?["id"]) ?? throw new CloudException(200, null, "the API created a run but sent no id");
        return await WaitAsync(id, options.Timeout, options.Poll, options.OnUpdate).ConfigureAwait(false);
    }

    public Task<JsonNode?> GetAsync(string id) => _http.CallAsync(HttpMethod.Get, $"/api/v1/runs/{CloudHttp.Seg(id)}");

    /// { runs: [...] }, newest first.
    public Task<JsonNode?> ListAsync(int? limit = null, string? before = null)
        => _http.CallAsync(HttpMethod.Get, "/api/v1/runs", null, new KeyValuePair<string, object?>[] { new("limit", limit), new("before", before) });

    public Task<JsonNode?> CancelAsync(string id) => _http.CallAsync(HttpMethod.Delete, $"/api/v1/runs/{CloudHttp.Seg(id)}");

    /// Poll GET /api/v1/runs/{id} until the run is succeeded, failed, cancelled or expired.
    public async Task<JsonNode?> WaitAsync(string id, double? timeout = null, double poll = 1.5, Func<JsonNode, Task>? onUpdate = null)
    {
        var watch = new CloudWatch($"run {id}", timeout,
            v => TerminalStatuses.Contains(Cloud.Str(v?["status"]) ?? ""),
            v => $"{Cloud.Str(v?["status"])}|{Cloud.Str(v?["handoff"]?["state"])}|{Cloud.Str(v?["handoff"]?["since"])}");
        var notify = onUpdate ?? (v =>
        {
            if (Cloud.Str(v["status"]) == "waiting_for_human") Cloud.AnnounceHandoff(v);
            return Task.CompletedTask;
        });
        while (true)
        {
            JsonNode? view;
            try { view = await GetAsync(id).ConfigureAwait(false); }
            catch (Exception e) when (watch.Retry(e))
            {
                await Task.Delay(watch.Pause(poll)).ConfigureAwait(false);
                continue;
            }
            var (done, changed) = watch.Seen(view);
            if (changed && view is not null) await notify(view).ConfigureAwait(false);
            if (done) return view;
            await Task.Delay(watch.Pause(poll)).ConfigureAwait(false);
        }
    }
}

/// Cloud profiles (named cookie stores): /api/v1/browsers/profiles.
public sealed class CloudProfiles
{
    internal const string NeedDomains =
        "choose the cookies to upload: set Domains (each also covers its subdomains and the parent-domain cookies a browser sends it), or AllDomains = true to upload every cookie";

    private readonly CloudHttp _http;
    internal CloudProfiles(CloudHttp http) { _http = http; }

    public Task<JsonNode?> ListAsync() => _http.CallAsync(HttpMethod.Get, "/api/v1/browsers/profiles");

    /// { name, cookies, domains, bytes, storage, updatedAt }; never the cookie values.
    public Task<JsonNode?> GetAsync(string name) => _http.CallAsync(HttpMethod.Get, $"/api/v1/browsers/profiles/{CloudHttp.Seg(name)}");

    public Task<JsonNode?> DeleteAsync(string name) => _http.CallAsync(HttpMethod.Delete, $"/api/v1/browsers/profiles/{CloudHttp.Seg(name)}");

    /// Upload cookies (CDP or Playwright shape) into a profile, creating it if needed. "replace" drops
    /// what the profile had first. 409 PROFILE_IN_USE while a live session saves to it.
    public Task<JsonNode?> ImportCookiesAsync(string name, IEnumerable<JsonNode> cookies, string mode = "merge")
    {
        var arr = new JsonArray();
        foreach (var c in cookies) arr.Add(c.DeepClone());
        return _http.CallAsync(HttpMethod.Put, $"/api/v1/browsers/profiles/{CloudHttp.Seg(name)}/cookies",
            new JsonObject { ["cookies"] = arr, ["mode"] = mode });
    }

    /// Copy a logged-in state into a cloud profile; see <see cref="ProfileSyncOptions"/>.
    public async Task<JsonNode?> SyncAsync(string name, ProfileSyncOptions options)
    {
        var sources = new[] { options.FromProfile, options.FromCdp, options.FromFile, options.LoginUrl }.Count(s => !string.IsNullOrEmpty(s));
        if (sources != 1) throw new ArgumentException("pass exactly one of FromProfile, FromCdp, FromFile or LoginUrl");
        var domains = (options.Domains ?? Array.Empty<string>()).Where(d => !string.IsNullOrWhiteSpace(d)).ToList();
        if (domains.Count > 0 && options.AllDomains) throw new ArgumentException("pass Domains or AllDomains, not both");
        if (domains.Count == 0 && !options.AllDomains) throw new ArgumentException(NeedDomains);
        List<JsonNode> cookies;
        if (!string.IsNullOrEmpty(options.FromFile)) cookies = CloudCookies.FromFile(options.FromFile);
        else if (!string.IsNullOrEmpty(options.FromCdp)) cookies = await CloudCookies.FromCdpAsync(options.FromCdp).ConfigureAwait(false);
        else if (!string.IsNullOrEmpty(options.FromProfile)) cookies = await CloudCookies.FromProfileAsync(options.FromProfile, options.Serve).ConfigureAwait(false);
        else cookies = await CloudCookies.ByLoginAsync(options.LoginUrl!, options.Confirm, options.Serve).ConfigureAwait(false);
        var picked = options.AllDomains ? cookies : CloudCookies.Filter(cookies, domains);
        if (picked.Count == 0)
            throw new InvalidOperationException($"no cookies found {(options.AllDomains ? "anywhere" : "for " + string.Join(", ", domains))}; nothing was uploaded");
        return await ImportCookiesAsync(name, picked.Select(CloudCookies.Normalize), options.Replace ? "replace" : "merge").ConfigureAwait(false);
    }
}

/// Signed event deliveries to your HTTPS endpoint: /api/v1/webhooks.
public sealed class CloudWebhooks
{
    private readonly CloudHttp _http;
    internal CloudWebhooks(CloudHttp http) { _http = http; }

    /// Register an endpoint; the answer carries <c>secret</c> (whsec_...), shown only here.
    public Task<JsonNode?> CreateAsync(string url, IEnumerable<string>? events = null, string? description = null)
    {
        var body = new JsonObject { ["url"] = url };
        if (events is not null) body["events"] = new JsonArray(events.Select(e => (JsonNode?)JsonValue.Create(e)).ToArray());
        if (description is not null) body["description"] = description;
        return _http.CallAsync(HttpMethod.Post, "/api/v1/webhooks", body);
    }

    public Task<JsonNode?> ListAsync() => _http.CallAsync(HttpMethod.Get, "/api/v1/webhooks");

    public Task<JsonNode?> DeleteAsync(string id) => _http.CallAsync(HttpMethod.Delete, $"/api/v1/webhooks/{CloudHttp.Seg(id)}");

    /// Send a <c>ping</c> event to the endpoint.
    public Task<JsonNode?> TestAsync(string id) => _http.CallAsync(HttpMethod.Post, $"/api/v1/webhooks/{CloudHttp.Seg(id)}/test", new JsonObject());
}

/// The hosted API, one client: <see cref="Browsers"/>, <see cref="Runs"/>, <see cref="Profiles"/> and
/// <see cref="Webhooks"/>.
public sealed class Cloud
{
    public const string DefaultApiUrl = "https://www.clearcotelabs.com";

    internal const string NoApiKey =
        "no Clearcote API key: pass ApiKey or set CLEARCOTE_API_KEY (create a key in the Clearcote dashboard)";

    // Plain http:// is accepted only for these hosts (a local dev control plane): anywhere else the API
    // key would cross the network unencrypted.
    private static readonly string[] LoopbackHosts = { "127.0.0.1", "::1", "localhost" };

    public string BaseUrl { get; }
    public CloudBrowsers Browsers { get; }
    public CloudRuns Runs { get; }
    public CloudProfiles Profiles { get; }
    public CloudWebhooks Webhooks { get; }

    /// <param name="apiKey">Defaults to CLEARCOTE_API_KEY.</param>
    /// <param name="baseUrl">Defaults to CLEARCOTE_API_URL, then https://www.clearcotelabs.com.</param>
    /// <param name="timeout">Per HTTP request, in seconds (default 30).</param>
    public Cloud(string? apiKey = null, string? baseUrl = null, double timeout = 30)
    {
        var key = (string.IsNullOrWhiteSpace(apiKey) ? Environment.GetEnvironmentVariable("CLEARCOTE_API_KEY") : apiKey)?.Trim();
        if (string.IsNullOrEmpty(key)) throw new InvalidOperationException(NoApiKey);
        var envUrl = Environment.GetEnvironmentVariable("CLEARCOTE_API_URL");
        var raw = !string.IsNullOrWhiteSpace(baseUrl) ? baseUrl : !string.IsNullOrWhiteSpace(envUrl) ? envUrl : DefaultApiUrl;
        BaseUrl = CheckBaseUrl(raw.Trim().TrimEnd('/'));
        var http = new CloudHttp(key, BaseUrl, TimeSpan.FromSeconds(timeout));
        Browsers = new CloudBrowsers(http);
        Runs = new CloudRuns(http);
        Profiles = new CloudProfiles(http);
        Webhooks = new CloudWebhooks(http);
    }

    /// Never the key.
    public override string ToString() => $"Cloud({BaseUrl})";

    /// <paramref name="url"/> (already stripped of trailing slashes) if it is an https:// URL, or an
    /// http:// URL of this machine; throws naming the reason otherwise.
    internal static string CheckBaseUrl(string url)
    {
        Uri? u = null;
        if (Regex.IsMatch(url, "^https?://", RegexOptions.IgnoreCase)) Uri.TryCreate(url, UriKind.Absolute, out u);
        var scheme = u?.Scheme.ToLowerInvariant() ?? "";
        var host = (u?.Host ?? "").ToLowerInvariant().Trim('[', ']');
        if (u is null || scheme is not ("http" or "https") || host.Length == 0)
            throw new ArgumentException($"the API URL must start with https:// (got {JsonSerializer.Serialize(url)})");
        if (scheme == "http" && Array.IndexOf(LoopbackHosts, host) < 0)
            throw new ArgumentException(
                $"the API URL must use https:// (got http://{host}): over plain http the API key would travel " +
                "unencrypted. http:// is only accepted for this machine (127.0.0.1, ::1, localhost)");
        return url;
    }

    // ── errors and JSON ──────────────────────────────────────────────────────────────────────────

    internal static CloudException ApiError(int status, string text, string reason = "")
    {
        JsonNode? data = null;
        try { data = string.IsNullOrWhiteSpace(text) ? null : JsonNode.Parse(text); } catch (JsonException) { }
        if (data is JsonObject o)
        {
            // { error: { message, code } }
            if (o["error"] is JsonObject inner)
                return new CloudException(status, Str(inner["code"]) ?? Str(o["code"]), Str(inner["message"]) ?? Str(inner["error"]) ?? $"HTTP {status}");
            var msg = Str(o["error"]) ?? Str(o["message"]);
            if (!string.IsNullOrEmpty(msg)) return new CloudException(status, Str(o["code"]), msg);
        }
        var t = (text ?? "").Trim();
        return new CloudException(status, null, t.Length > 0 ? (t.Length > 300 ? t[..300] : t) : $"HTTP {status} {reason}".Trim());
    }

    internal static JsonNode? ParseJson(int status, string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return null;
        try { return JsonNode.Parse(text); }
        catch (JsonException) { throw new CloudException(status, null, "the API answered with something that is not JSON"); }
    }

    /// A string value, or null for anything else (a missing key, null, a number, an object).
    internal static string? Str(JsonNode? n)
        => n is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;

    internal static JsonObject Compact(params (string Key, object? Value)[] pairs)
    {
        var o = new JsonObject();
        foreach (var (k, v) in pairs) if (v is not null) o[k] = JsonSerializer.SerializeToNode(v);
        return o;
    }

    /// Default report when a run pauses for a person: without it a waiting run just looks stuck.
    internal static void AnnounceHandoff(JsonNode view)
    {
        var h = view["handoff"];
        var reason = Str(h?["reason"]);
        var live = Str(h?["liveUrl"]);
        Console.Error.WriteLine(
            $"[clearcote] run {Str(view["id"])} is waiting for a human{(string.IsNullOrEmpty(reason) ? "" : $" ({reason})")}: " +
            $"{(string.IsNullOrEmpty(live) ? "open it in the Clearcote dashboard" : live)}");
    }

    // ── option mapping ───────────────────────────────────────────────────────────────────────────

    private const string ProxyShape = "Proxy must have Server = \"managed\", a proxy URL, or Server + Username + Password";

    /// The API body of a session (or, <paramref name="run"/>, the browser half of a run). Null options
    /// are left out, so an optional setting can be passed through unconditionally.
    internal static JsonObject SessionBody(CloudSessionOptions o, bool run)
    {
        var b = new JsonObject();
        void Put(string key, object? value) { if (value is not null) b[key] = JsonSerializer.SerializeToNode(value); }
        Put("fingerprint", o.Fingerprint);
        Put("identity", o.Identity);
        Put("platform", o.Platform);
        Put("brand", o.Brand);
        Put("timezone", o.Timezone);
        Put("locale", o.Locale);
        Put("geoip", o.Geoip);
        Put("headless", o.Headless);
        Put("lightStealth", o.LightStealth);
        if (o.Proxy is not null) b["proxy"] = ProxyNode(o.Proxy, run);
        Put("country", o.Country);
        Put("state", o.State);
        Put("city", o.City);
        Put("proxySession", o.ProxySession);
        Put("timeoutSec", o.TimeoutSec);
        Put("idleTimeoutSec", o.IdleTimeoutSec);
        Put("maxGb", o.MaxGb);
        Put("version", o.Version);
        if (o.Profile is not null) b["profile"] = ProfileNode(o.Profile);
        Put("url", o.Url);
        Put("adblock", o.Adblock);
        Put("solveSliders", o.SolveSliders);
        Put("keepAlive", o.KeepAlive);
        Put("record", o.Record);
        Put("note", o.Note);
        Put("worker", o.Worker);
        return b;
    }

    /// The POST /api/v1/runs body: the task, where it starts, what to return, and the browser.
    internal static JsonObject RunBody(string task, RunOptions o)
    {
        var b = new JsonObject { ["task"] = task };
        var session = SessionBody(o, run: true);
        // url belongs to the run (where the agent starts); keep it next to the task, as the API documents it
        if (session["url"] is { } url) { session.Remove("url"); b["url"] = url; }
        if (o.Schema is not null) b["schema"] = o.Schema.DeepClone();
        if (o.Secrets is not null) b["secrets"] = JsonSerializer.SerializeToNode(o.Secrets);
        foreach (var (k, v) in session.ToList()) { session.Remove(k); b[k] = v; }
        if (o.MaxSteps is not null) b["maxSteps"] = o.MaxSteps;
        if (o.Handoff is not null) b["handoff"] = o.Handoff;
        if (o.HandoffTimeoutSec is not null) b["handoffTimeoutSec"] = o.HandoffTimeoutSec;
        return b;
    }

    /// A cloud proxy: "managed"/"direct", or your own. The API wants the credentials as separate fields,
    /// so a URL's user:pass@ is split out (it rejects credentials inside server).
    internal static JsonNode ProxyNode(ProxyOptions p, bool run)
    {
        if (!string.IsNullOrEmpty(p.Bypass)) throw new ArgumentException($"Proxy.Bypass is not available for cloud {(run ? "runs" : "browsers")}");
        var server = p.Server?.Trim();
        if (string.IsNullOrEmpty(server)) throw new ArgumentException(ProxyShape);
        if (server is "managed" or "direct")
        {
            if (!string.IsNullOrEmpty(p.Username) || !string.IsNullOrEmpty(p.Password)) throw new ArgumentException(ProxyShape);
            return JsonValue.Create(server)!;
        }
        ProxySpec? spec;
        try { spec = ProxySpec.From(p); } catch (UriFormatException) { throw new ArgumentException(ProxyShape); }
        if (spec is null) throw new ArgumentException(ProxyShape);
        var o = new JsonObject { ["server"] = spec.ServerString };
        if (spec.Username is not null) o["username"] = spec.Username;
        if (spec.Password is not null) o["password"] = spec.Password;
        return o;
    }

    internal static JsonNode ProfileNode(CloudProfile p)
    {
        if (string.IsNullOrWhiteSpace(p.Name)) throw new ArgumentException("a cloud profile needs a Name");
        if (p.Name == "auto")
            throw new ArgumentException("profile \"auto\" picks a local persona; for a stable cloud device set Identity, and for cookies a cloud profile name");
        if (p.Persist is null) return JsonValue.Create(p.Name)!;
        return new JsonObject { ["name"] = p.Name, ["persist"] = p.Persist };
    }

    // ── webhooks: signature check ────────────────────────────────────────────────────────────────

    // A real header is ~80 bytes (one t, one or two v1). Anything near this is not a Clearcote signature.
    private const int MaxSignatureHeader = 4096;

    /// <summary>Check a webhook delivery and return its parsed JSON event.</summary>
    /// <remarks>
    /// <paramref name="rawBody"/> must be the request body EXACTLY as received: re-serialised JSON does
    /// not match the signature. <paramref name="signatureHeader"/> is the Clearcote-Signature header,
    /// <c>t=&lt;unix seconds&gt;,v1=&lt;hex HMAC-SHA256(secret, "&lt;t&gt;.&lt;raw body&gt;")&gt;</c>; more than one v1
    /// may be present (while a secret rotates) and any one matching is enough. Comparisons are
    /// constant-time. A timestamp more than <paramref name="toleranceSec"/> away from now is refused, so a
    /// captured delivery cannot be replayed later (null turns that off). Throws
    /// <see cref="WebhookSignatureException"/> on any failure.
    /// </remarks>
    public static JsonNode? VerifyWebhook(byte[] rawBody, string? signatureHeader, string secret, int? toleranceSec = 300, long? nowSec = null)
    {
        if (string.IsNullOrWhiteSpace(secret))
            throw new WebhookSignatureException("VerifyWebhook needs the signing secret of the endpoint (whsec_...), got none");
        var header = signatureHeader ?? "";
        if (header.Length > MaxSignatureHeader) throw new WebhookSignatureException("invalid Clearcote-Signature header (expected t=<unix seconds>,v1=<hex>)");
        string? stamp = null;
        var signatures = new List<string>();
        foreach (var part in header.Split(','))
        {
            var i = part.IndexOf('=');
            if (i < 0) continue;
            var key = part[..i].Trim();
            var value = part[(i + 1)..].Trim();
            if (key == "t") stamp = value;
            else if (key == "v1" && value.Length > 0) signatures.Add(value.ToLowerInvariant());
        }
        if (stamp is null || !Regex.IsMatch(stamp, "^[0-9]+$") || signatures.Count == 0)
            throw new WebhookSignatureException("invalid Clearcote-Signature header (expected t=<unix seconds>,v1=<hex>)");
        var signed = Encoding.ASCII.GetBytes(stamp + ".").Concat(rawBody).ToArray();
        var expected = Encoding.ASCII.GetBytes(Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret), signed)).ToLowerInvariant());
        var ok = false;
        foreach (var s in signatures)
        {
            var got = Encoding.UTF8.GetBytes(s);
            // no early exit: every candidate is compared, each in constant time
            if (got.Length == expected.Length && CryptographicOperations.FixedTimeEquals(got, expected)) ok = true;
        }
        if (!ok) throw new WebhookSignatureException("webhook signature does not match");
        if (toleranceSec is not null)
        {
            var now = nowSec ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds();
            // the stamp is all digits but may be longer than a long: anything that big is out of range anyway
            if (!long.TryParse(stamp, out var t) || Math.Abs((double)now - t) > toleranceSec.Value)
                throw new WebhookSignatureException("webhook timestamp is outside the tolerance window (a replay, or a clock that is off)");
        }
        return JsonNode.Parse(rawBody);
    }

    /// <inheritdoc cref="VerifyWebhook(byte[], string?, string, int?, long?)"/>
    public static JsonNode? VerifyWebhook(string rawBody, string? signatureHeader, string secret, int? toleranceSec = 300, long? nowSec = null)
        => VerifyWebhook(Encoding.UTF8.GetBytes(rawBody), signatureHeader, secret, toleranceSec, nowSec);

    // ── launch({ cloud }) support ────────────────────────────────────────────────────────────────

    /// The hosted session a cloud launch is connected to: the create answer minus the single-use
    /// connect URL (it holds a token and is spent once connected). Null for a local browser.
    public static JsonNode? SessionOf(object browserOrContext)
        => CloudLaunch.Sessions.TryGetValue(browserOrContext, out var s) ? s.DeepClone() : null;
}

// ── cookies: profile sync ────────────────────────────────────────────────────────────────────────

internal static class CloudCookies
{
    // What PUT .../cookies keeps of a cookie (CookieParam). The CDP shape carries more (size, priority,
    // sourceScheme, ...); the API drops those anyway, so they are not uploaded at all.
    private static readonly string[] Fields = { "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite" };

    internal const string LoginPrompt = "Sign in in the browser window that just opened, then press Enter here to upload the cookies.";

    public static JsonNode Normalize(JsonNode cookie)
    {
        var o = new JsonObject();
        foreach (var f in Fields)
            if (cookie[f] is { } v) o[f] = v.DeepClone();
        return o;
    }

    private static string Bare(string? d) => (d ?? "").Trim().ToLowerInvariant().TrimStart('.');

    /// <paramref name="cookieDomain"/> is <paramref name="allowed"/>, a subdomain of it, or a PARENT domain
    /// of it (a .example.com cookie is sent to www.example.com too). A parent must still have a dot of
    /// its own: a cookie on a bare suffix (com) never matches.
    private static bool Matches(string cookieDomain, string allowed)
    {
        if (cookieDomain == allowed || cookieDomain.EndsWith("." + allowed, StringComparison.Ordinal)) return true;
        return cookieDomain.Contains('.') && allowed.EndsWith("." + cookieDomain, StringComparison.Ordinal);
    }

    /// The cookies a browser would use on <paramref name="domains"/> (see <see cref="Matches"/>); a leading
    /// dot on either side is ignored, and example.com does not match badexample.com.
    public static List<JsonNode> Filter(IEnumerable<JsonNode> cookies, IEnumerable<string> domains)
    {
        var allowed = domains.Select(Bare).Where(d => d.Length > 0).ToList();
        return cookies.Where(c =>
        {
            var d = Bare(Cloud.Str(c["domain"]));
            return d.Length > 0 && allowed.Any(a => Matches(d, a));
        }).ToList();
    }

    /// The cookies in a Playwright storage state ({ cookies, origins }), a CDP { cookies } answer, or a
    /// plain JSON array of cookies.
    public static List<JsonNode> FromState(JsonNode? data)
    {
        var cookies = data is JsonObject o ? o["cookies"] : data;
        if (cookies is not JsonArray arr)
            throw new ArgumentException("expected a Playwright storage state ({\"cookies\": [...]}) or a JSON array of cookies");
        var list = new List<JsonNode>();
        foreach (var c in arr)
        {
            if (c is not JsonObject co || string.IsNullOrEmpty(Cloud.Str(co["name"])) || string.IsNullOrEmpty(Cloud.Str(co["domain"])))
                throw new ArgumentException("every cookie needs at least a name and a domain");
            list.Add(co);
        }
        return list;
    }

    public static List<JsonNode> FromFile(string path)
    {
        var text = File.ReadAllText(path);
        JsonNode? data;
        try { data = JsonNode.Parse(text); }
        catch (JsonException e) { throw new ArgumentException($"{path} is not JSON: {e.Message}"); }
        return FromState(data);
    }

    /// Every cookie of a running browser's default context, over CDP (Storage.getCookies on the browser
    /// target). Closing a CDP connection only disconnects: the browser keeps running.
    public static async Task<List<JsonNode>> FromCdpAsync(string endpoint)
    {
        endpoint = endpoint.Trim();
        if (!Regex.IsMatch(endpoint, "^(wss?|https?)://"))
            throw new ArgumentException("FromCdp must be an http(s):// or ws(s):// CDP endpoint, e.g. http://127.0.0.1:9222");
        var pw = await Clearcote.PlaywrightInstanceAsync().ConfigureAwait(false);
        var browser = await pw.Chromium.ConnectOverCDPAsync(endpoint).ConfigureAwait(false);
        try
        {
            var session = await browser.NewBrowserCDPSessionAsync().ConfigureAwait(false);
            var answer = await session.SendAsync("Storage.getCookies").ConfigureAwait(false);
            var node = answer is { } a ? JsonNode.Parse(a.GetRawText()) : null;
            return node?["cookies"] is JsonArray arr ? arr.Where(c => c is not null).Select(c => c!.DeepClone()).ToList() : new List<JsonNode>();
        }
        finally
        {
            await browser.CloseAsync().ConfigureAwait(false);
        }
    }

    private static async Task<CloudServed> DefaultServe(ServeOptions o)
    {
        var srv = await Clearcote.ServeAsync(o).ConfigureAwait(false);
        return new CloudServed(srv.CdpUrl, () => srv.IsAlive, srv.CloseAsync);
    }

    /// Start a headless Clearcote on <paramref name="userDataDir"/> (a raw CDP endpoint via ServeAsync:
    /// no Playwright launch, nothing written but what Chrome itself writes), read its cookies, stop it.
    public static async Task<List<JsonNode>> FromProfileAsync(string userDataDir, Func<ServeOptions, Task<CloudServed>>? serve)
    {
        if (!Directory.Exists(userDataDir)) throw new ArgumentException($"{userDataDir} is not a directory");
        var srv = await (serve ?? DefaultServe)(new ServeOptions { UserDataDir = userDataDir, Headless = true, Quiet = true }).ConfigureAwait(false);
        try { return await FromCdpAsync(srv.CdpUrl).ConfigureAwait(false); }
        finally { await srv.Close().ConfigureAwait(false); }
    }

    private static Task WaitForEnter()
    {
        if (Console.IsInputRedirected)
            throw new InvalidOperationException("login needs an interactive console to confirm the sign-in (set Confirm to wait another way)");
        Console.Error.Write(LoginPrompt + " ");
        Console.ReadLine();
        return Task.CompletedTask;
    }

    /// Open <paramref name="url"/> in a VISIBLE Clearcote on a throwaway profile, wait for
    /// <paramref name="confirm"/> (default: Enter in this console), then read the cookies. The profile
    /// directory is deleted afterwards.
    public static async Task<List<JsonNode>> ByLoginAsync(string url, Func<Task>? confirm, Func<ServeOptions, Task<CloudServed>>? serve)
    {
        var srv = await (serve ?? DefaultServe)(new ServeOptions { Headless = false, Quiet = true }).ConfigureAwait(false);
        try
        {
            var pw = await Clearcote.PlaywrightInstanceAsync().ConfigureAwait(false);
            var browser = await pw.Chromium.ConnectOverCDPAsync(srv.CdpUrl).ConfigureAwait(false);
            try
            {
                var session = await browser.NewBrowserCDPSessionAsync().ConfigureAwait(false);
                await session.SendAsync("Target.createTarget", new Dictionary<string, object> { ["url"] = url }).ConfigureAwait(false);
            }
            finally
            {
                await browser.CloseAsync().ConfigureAwait(false);
            }
            await (confirm ?? WaitForEnter)().ConfigureAwait(false);
            if (!srv.IsAlive()) throw new InvalidOperationException("the browser was closed before the cookies were read; nothing was uploaded");
            return await FromCdpAsync(srv.CdpUrl).ConfigureAwait(false);
        }
        finally
        {
            await srv.Close().ConfigureAwait(false);
        }
    }
}
