using System.Collections.Concurrent;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Clearcote.Tests;

// An in-memory stand-in for the hosted Clearcote API, for the cloud client and launch tests. Mirrors
// sdk/node/test/helpers/fake-cloud.ts and sdk/python/tests/_fake_cloud.py: the same endpoints, shapes
// and canned data, so all three SDKs are tested against the same server behaviour.

internal sealed record CloudRequest(string Method, string Path, string Query, Dictionary<string, string> Headers, JsonNode? Body)
{
    public string? Header(string name) => Headers.TryGetValue(name.ToLowerInvariant(), out var v) ? v : null;
}

internal sealed class FakeCloud : LocalServer
{
    public const string ApiKey = "cc_live_test_key";
    public const string T0 = "2026-10-02T10:00:00.000Z";
    public const string Live = "https://www.clearcotelabs.test/live/bs_run1?t=tok&handoff=1";
    // Explicit bytes: C#'s \x escape takes up to four hex digits, so "\x18ftyp" would read as \x18f + "typ".
    public static readonly byte[] Recording = new byte[] { 0, 0, 0, 0x18 }.Concat(Encoding.ASCII.GetBytes("ftypmp42fake-mp4-bytes")).ToArray();

    public readonly ConcurrentQueue<CloudRequest> Log = new();
    public string ConnectUrl = "ws://127.0.0.1:9/devtools/browser/none";
    public string[] RunStatuses = { "queued", "running", "waiting_for_human", "running", "succeeded" };
    public int Flaky;
    public int HandoffPolls = 2;
    public string RecordingState = "ready";
    public string RecordingLocation = "/dev/blob/rec.mp4?sig=abc";
    public readonly Dictionary<string, Dictionary<string, JsonNode>> ProfileCookies = new();
    public readonly List<JsonObject> Webhooks = new();

    private int _n;
    private readonly Dictionary<string, int> _runGets = new();
    private readonly Dictionary<string, (string? Reason, int Polls)> _handoffs = new();
    private readonly object _gate = new();

    public string Url => $"http://127.0.0.1:{Port}";

    public List<CloudRequest> Requests(string? method = null, string? path = null)
        => Log.Where(r => (method is null || r.Method == method) && (path is null || r.Path == path)).ToList();

    private static readonly JsonArray Events = (JsonArray)JsonNode.Parse("""
        [{"seq":1,"at":"2026-10-02T10:00:00.000Z","type":"session.started","data":{"kind":"run"}},
         {"seq":2,"at":"2026-10-02T10:00:01.000Z","type":"navigation","data":{"url":"https://example.com/","title":"Example Domain"}},
         {"seq":3,"at":"2026-10-02T10:00:02.000Z","type":"tab.closed","data":{}},
         {"seq":4,"at":"2026-10-02T10:00:03.000Z","type":"session.ended","data":{"reason":"run_finished"}}]
        """)!;

    private static readonly JsonNode Result = JsonNode.Parse("""
        {"status":"done","detail":null,"url":"https://example.com/pricing","title":"Pricing",
         "output":{"plan":"Starter","price":"9.99"},"outputError":null,"markdown":"# Pricing","handoffs":1,"elapsedMs":14800}
        """)!;

    private JsonObject SessionView(string sid, string status = "active")
    {
        JsonNode? handoff = null;
        if (_handoffs.TryGetValue(sid, out var h))
        {
            var state = h.Polls > 0 ? "waiting" : "done";
            handoff = new JsonObject
            {
                ["state"] = state, ["reason"] = h.Reason, ["since"] = T0, ["expiresAt"] = "2026-10-02T10:10:00.000Z",
                ["doneAt"] = state == "waiting" ? null : "2026-10-02T10:01:00.000Z", ["liveUrl"] = state == "waiting" ? Live : null,
            };
            _handoffs[sid] = (h.Reason, h.Polls - 1);
        }
        return new JsonObject
        {
            ["id"] = sid, ["status"] = status, ["worker"] = "w1", ["proxy"] = "managed", ["note"] = null, ["profile"] = null,
            ["createdAt"] = T0, ["startedAt"] = T0, ["endedAt"] = status == "active" ? null : T0,
            ["endReason"] = status == "active" ? null : "stopped:user", ["stopRequested"] = status != "active",
            ["costEur"] = 0.0012, ["handoff"] = handoff, ["recording"] = null,
        };
    }

    private JsonObject RunView(string rid, string status)
    {
        var finished = CloudRuns.TerminalStatuses.Contains(status);
        return new JsonObject
        {
            ["id"] = rid, ["status"] = status, ["task"] = "Find the price", ["url"] = "https://example.com/", ["hasSchema"] = true,
            ["createdAt"] = T0, ["startedAt"] = status == "queued" ? null : T0, ["endedAt"] = finished ? T0 : null,
            ["result"] = status == "succeeded" ? Result.DeepClone() : null,
            ["handoff"] = status == "waiting_for_human"
                ? new JsonObject { ["state"] = "waiting", ["reason"] = "needs a login", ["since"] = T0, ["expiresAt"] = "2026-10-02T10:10:00.000Z", ["doneAt"] = null, ["liveUrl"] = Live }
                : null,
            ["session"] = SessionView(rid, finished ? "ended" : "active"),
            ["costEur"] = new JsonObject { ["browser"] = 0.0012, ["agent"] = 0.0007, ["total"] = 0.0019 },
        };
    }

    private (int Status, object Body, Dictionary<string, string>? Headers) Handle(string method, string path, Dictionary<string, string> query, CloudRequest req)
    {
        if (path == "/dev/blob/rec.mp4") return (200, Recording, new() { ["content-type"] = "video/mp4" });
        if (path == "/login-page")
            return (200, Encoding.UTF8.GetBytes("<html><title>login</title>signed in</html>"),
                new() { ["content-type"] = "text/html", ["set-cookie"] = "sid=s3cret; Path=/; HttpOnly" });
        if (req.Header("authorization") != $"Bearer {ApiKey}") return (401, new JsonObject { ["error"] = "Missing or invalid API key." }, null);
        var parts = path.Trim('/').Split('/').Select(Uri.UnescapeDataString).ToArray();
        if (parts.Length < 2 || parts[0] != "api" || parts[1] != "v1") return (404, new JsonObject { ["error"] = "Not found." }, null);
        var rest = parts[2..];
        bool Is(params string[] p) => rest.Length == p.Length && p.Select((x, i) => x == "*" || rest[i] == x).All(b => b);
        var body = req.Body;

        if (Is("browsers") && method == "POST")
        {
            var sid = $"bs_{++_n}";
            var o = new JsonObject
            {
                ["id"] = sid, ["worker"] = "w1", ["connectUrl"] = ConnectUrl, ["expiresAt"] = "2026-10-02T11:00:00.000Z",
                ["engine"] = new JsonObject { ["version"] = "154.0", ["revision"] = "r30", ["pinned"] = false }, ["warnings"] = new JsonArray(),
            };
            if (body?["profile"] is JsonObject prof) o["profile"] = prof.DeepClone();
            return (201, o, null);
        }
        if (Is("browsers") && method == "GET")
            return (200, new JsonObject
            {
                ["balanceEur"] = 12.5,
                ["sessions"] = JsonNode.Parse("""[{"id":"bs_a1","status":"active","note":"crawler"},{"id":"bs_b2","status":"ended","note":null}]"""),
            }, null);
        if (rest.Length >= 2 && rest[0] == "browsers" && rest[1] == "profiles") return Profiles(method, rest[2..], body);
        if (rest.Length >= 2 && rest[0] == "browsers")
        {
            var sid = rest[1];
            var tail = string.Join("/", rest[2..]);
            if (tail == "" && method == "GET") return (200, SessionView(sid), null);
            if (tail == "" && method == "DELETE") return (200, SessionView(sid, "ended"), null);
            if (tail == "live")
                return (200, new JsonObject { ["viewUrl"] = $"wss://w1.example/live/{sid}", ["expiresAt"] = T0, ["interactive"] = query.GetValueOrDefault("control") == "1" }, null);
            if (tail == "share")
            {
                var rec = body?["recording"]?.GetValue<bool>() == true;
                return (200, new JsonObject { ["url"] = $"https://www.clearcotelabs.test/{(rec ? "replay" : "live")}/{sid}?t=x", ["expiresAt"] = T0, ["control"] = body?["control"]?.GetValue<bool>() == true }, null);
            }
            if (tail == "handoff" && method == "POST")
            {
                var reason = Cloud.Str(body?["reason"]);
                _handoffs[sid] = (reason, HandoffPolls);
                return (200, new JsonObject { ["state"] = "waiting", ["reason"] = reason, ["since"] = T0, ["expiresAt"] = "2026-10-02T10:10:00.000Z", ["liveUrl"] = Live }, null);
            }
            if (tail == "handoff/done" && method == "POST")
            {
                if (_handoffs.TryGetValue(sid, out var h)) _handoffs[sid] = (h.Reason, 0);
                return (200, new JsonObject { ["state"] = "done", ["reason"] = null, ["since"] = T0, ["expiresAt"] = T0, ["doneAt"] = "2026-10-02T10:01:00.000Z", ["liveUrl"] = null }, null);
            }
            if (tail == "events")
            {
                var after = long.Parse(query.GetValueOrDefault("after") ?? "0");
                var limit = int.Parse(query.GetValueOrDefault("limit") ?? "2");
                var left = Events.Where(e => e!["seq"]!.GetValue<long>() > after).ToList();
                var page = left.Take(limit).ToList();
                return (200, new JsonObject
                {
                    ["events"] = new JsonArray(page.Select(e => e!.DeepClone()).ToArray()),
                    ["next"] = left.Count > page.Count ? page[^1]!["seq"]!.GetValue<long>() : null,
                }, null);
            }
            if (tail == "recording")
            {
                if (sid == "bs_unrecorded") return (404, new JsonObject { ["error"] = "This session was not recorded.", ["code"] = "NOT_FOUND" }, null);
                if (RecordingState != "ready") return (409, new JsonObject { ["error"] = "The recording is still processing.", ["code"] = "NOT_READY" }, null);
                return (302, Array.Empty<byte>(), new() { ["location"] = RecordingLocation });
            }
        }
        if (Is("runs") && method == "POST")
        {
            if (body is JsonObject b && b.ContainsKey("keepAlive")) return (400, new JsonObject { ["error"] = "keepAlive does not apply to runs" }, null);
            return (201, new JsonObject { ["id"] = "bs_run1", ["status"] = "queued", ["worker"] = "w1", ["createdAt"] = T0 }, null);
        }
        if (Is("runs") && method == "GET")
            return (200, JsonNode.Parse("""{"runs":[{"id":"bs_run1","status":"succeeded","task":"Find the price","costEur":0.0019}]}""")!, null);
        if (Is("runs", "*"))
        {
            var rid = rest[1];
            if (method == "DELETE") return (200, RunView(rid, "cancelled"), null);
            if (Flaky > 0)
            {
                Flaky--;
                return (503, Encoding.UTF8.GetBytes("upstream restarting"), new() { ["content-type"] = "text/plain" });
            }
            var n = _runGets.GetValueOrDefault(rid);
            _runGets[rid] = n + 1;
            return (200, RunView(rid, RunStatuses[Math.Min(n, RunStatuses.Length - 1)]), null);
        }
        if (Is("webhooks") && method == "POST")
        {
            var hook = new JsonObject
            {
                ["id"] = $"wh_{Webhooks.Count + 1}", ["url"] = body?["url"]?.DeepClone(), ["events"] = body?["events"]?.DeepClone() ?? new JsonArray(),
                ["description"] = body?["description"]?.DeepClone(), ["createdAt"] = T0,
            };
            Webhooks.Add((JsonObject)hook.DeepClone());
            hook["secret"] = "whsec_test_secret";
            return (201, hook, null);
        }
        if (Is("webhooks") && method == "GET")
            return (200, new JsonObject { ["webhooks"] = new JsonArray(Webhooks.Select(w => (JsonNode?)w.DeepClone()).ToArray()) }, null);
        if (Is("webhooks", "*") && method == "DELETE") return (200, new JsonObject { ["ok"] = true }, null);
        if (Is("webhooks", "*", "test")) return (200, new JsonObject { ["ok"] = true, ["status"] = 200 }, null);
        return (404, new JsonObject { ["error"] = "Not found.", ["code"] = "NOT_FOUND" }, null);
    }

    private (int, object, Dictionary<string, string>?) Profiles(string method, string[] rest, JsonNode? body)
    {
        if (rest.Length == 0)
            return (200, JsonNode.Parse("""{"profiles":[{"name":"acct-1","bytes":2048,"cookies":12,"storage":0,"inUse":false}]}""")!, null);
        var name = rest[0];
        if (rest.Length == 2 && rest[1] == "cookies" && method == "PUT")
        {
            if (name == "busy") return (409, new JsonObject { ["error"] = "A live session is saving to this profile.", ["code"] = "PROFILE_IN_USE" }, null);
            var have = Cloud.Str(body?["mode"]) == "replace"
                ? new Dictionary<string, JsonNode>()
                : new Dictionary<string, JsonNode>(ProfileCookies.GetValueOrDefault(name) ?? new());
            var incoming = body?["cookies"] as JsonArray ?? new JsonArray();
            foreach (var c in incoming)
                have[$"{Cloud.Str(c!["name"])}|{Cloud.Str(c["domain"])}|{Cloud.Str(c["path"]) ?? "/"}"] = c.DeepClone();
            ProfileCookies[name] = have;
            var domains = have.Values.Select(c => (Cloud.Str(c["domain"]) ?? "").TrimStart('.')).Distinct().OrderBy(d => d, StringComparer.Ordinal).ToList();
            return (200, new JsonObject
            {
                ["name"] = name, ["cookies"] = have.Count, ["imported"] = incoming.Count,
                ["domains"] = new JsonArray(domains.Select(d => (JsonNode?)d).ToArray()), ["bytes"] = 1234, ["updatedAt"] = T0,
            }, null);
        }
        if (rest.Length == 1 && method == "GET")
        {
            var have = ProfileCookies.GetValueOrDefault(name) ?? new();
            return (200, new JsonObject { ["name"] = name, ["cookies"] = have.Count, ["bytes"] = 1234, ["storage"] = 0, ["updatedAt"] = T0 }, null);
        }
        if (rest.Length == 1 && method == "DELETE") return (200, new JsonObject { ["ok"] = true }, null);
        return (404, new JsonObject { ["error"] = "Not found.", ["code"] = "NOT_FOUND" }, null);
    }

    protected override async Task HandleAsync(NetworkStream s, CancellationToken ct)
    {
        var head = await ReadHeadAsync(s, ct);
        if (head is null) return;
        var (lines, rest) = head.Value;
        var first = lines[0].Split(' ');
        var target = first.Length > 1 ? first[1] : "/";
        var headers = new Dictionary<string, string>();
        foreach (var l in lines.Skip(1))
        {
            var i = l.IndexOf(':');
            if (i > 0) headers[l[..i].Trim().ToLowerInvariant()] = l[(i + 1)..].Trim();
        }
        var raw = rest;
        if (int.TryParse(headers.GetValueOrDefault("content-length"), out var len) && len > rest.Length)
            raw = rest.Concat(await ReadExactAsync(s, len - rest.Length, ct)).ToArray();
        var q = target.IndexOf('?');
        var path = q < 0 ? target : target[..q];
        var query = q < 0 ? "" : target[(q + 1)..];
        var qd = query.Split('&', StringSplitOptions.RemoveEmptyEntries).Select(p => p.Split('=', 2))
            .ToDictionary(p => Uri.UnescapeDataString(p[0]), p => p.Length > 1 ? Uri.UnescapeDataString(p[1]) : "");
        JsonNode? body = null;
        if (raw.Length > 0) { try { body = JsonNode.Parse(raw); } catch (JsonException) { } }
        var req = new CloudRequest(first[0], path, query, headers, body);
        Log.Enqueue(req);
        (int Status, object Body, Dictionary<string, string>? Headers) answer;
        lock (_gate) answer = Handle(first[0], path, qd, req);
        var bytes = answer.Body is byte[] b ? b : Encoding.UTF8.GetBytes(((JsonNode)answer.Body).ToJsonString());
        var extra = answer.Headers ?? new Dictionary<string, string>();
        var sb = new StringBuilder($"HTTP/1.1 {answer.Status} X\r\n");
        if (!extra.ContainsKey("content-type")) sb.Append("Content-Type: application/json\r\n");
        foreach (var (k, v) in extra) sb.Append($"{k}: {v}\r\n");
        sb.Append($"Content-Length: {bytes.Length}\r\nConnection: close\r\n\r\n");
        await s.WriteAsync(Encoding.Latin1.GetBytes(sb.ToString()), ct);
        await s.WriteAsync(bytes, ct);
        await s.FlushAsync(ct);
    }
}

/// A raw TCP server: reads a request, writes a fixed answer, hangs up (a body shorter than its
/// Content-Length simulates a connection cut while the answer is read).
internal sealed class OneShotServer : LocalServer
{
    private readonly string _answer;
    public OneShotServer(string answer) => _answer = answer;
    public string Url => $"http://127.0.0.1:{Port}";

    public static string HttpAnswer(string statusLine, string body, int? length = null)
        => $"{statusLine}\r\nContent-Type: application/json\r\nContent-Length: {length ?? Encoding.UTF8.GetByteCount(body)}\r\n\r\n{body}";

    protected override async Task HandleAsync(NetworkStream s, CancellationToken ct)
    {
        await ReadHeadAsync(s, ct);
        await s.WriteAsync(Encoding.UTF8.GetBytes(_answer), ct);
        await s.FlushAsync(ct);
    }
}
