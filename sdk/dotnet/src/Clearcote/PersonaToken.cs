using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Clearcote;

/// <summary>The persona service refused the launch or could not be reached.</summary>
public class PersonaServiceError : LicenseError
{
    public PersonaServiceError(string message) : base(message) { }
}

/// <summary>
/// Server-side personas (engine r51+): fetch the signed persona token for a launch and hand it to the engine.
/// </summary>
/// <remarks>
/// From r51 the engine derives no persona of its own. The launcher asks the persona service for one -- with the run
/// token it just leased, the persona switches it is about to launch with (the <em>selector</em>) and the one fact of
/// this machine the generator needs (the host GPU vendor) -- and passes the signed answer to the browser in
/// <c>CLEARCOTE_PERSONA_TOKEN</c>, next to <c>CLEARCOTE_RUN_TOKEN</c>. Every launch gets one, persona or not: a
/// selector without a seed yields the persona the engine computed for seed 0 up to r50. The engine checks the
/// token's signature and that it was issued to this licence and lease for exactly these switches on this machine.
/// Engines before r51 ignore the variable, so this does nothing for them (<see cref="PersonaToken.EngineWantsToken"/>).
/// A captured profile no longer rides on the command line: its exact JSON text goes to the service, which names its
/// sha256 in the token's selector; the launch carries <c>--fingerprint-profile-sha256=&lt;hash&gt;</c>.
/// The selector list mirrors the Python and Node SDKs', the engine's and the service's.
/// </remarks>
internal static class PersonaToken
{
    public const string TokenEnv = "CLEARCOTE_PERSONA_TOKEN";
    public const string TokenGzEnv = "CLEARCOTE_PERSONA_TOKEN_GZ";
    public const string ApiEnv = "CLEARCOTE_PERSONA_API";
    public const string MarkerSwitch = "cc-persona-host-facts";
    public static readonly string[] DefaultHosts = { "https://persona.clearcotelabs.com", "https://persona-us.clearcotelabs.com" };
    public const int EnvInlineLimit = 30000;
    private const int RequestTimeoutMs = 8000;
    private const int SkewSec = 60;
    private const string ProfileSwitch = "fingerprint-profile";
    private const string ProfileShaSwitch = "fingerprint-profile-sha256";

    public static readonly HashSet<string> SelectorSwitches = new(StringComparer.Ordinal)
    {
        "fingerprint", "fingerprint-platform", "fingerprint-brand", "fingerprint-brand-version",
        "fingerprint-schema", "fingerprint-gpu-vendor", "fingerprint-gpu-renderer", "fingerprint-gpu-draw",
        "fingerprint-gpu-backend-real", "fingerprint-hardware-concurrency", "fingerprint-device-pixel-ratio",
        "fingerprint-color-scheme", "fingerprint-reduced-motion", "fingerprint-webauthn", "fingerprint-bluetooth",
        "fingerprint-webgpu-vendor", "fingerprint-webgpu-architecture", "disable-gpu-string-spoof",
        "disable-gpu-fingerprint", "disable-fingerprint-voices", "canvas-bridge-url", "fingerprint-profile-id",
        "fingerprint-profile-sha256",
    };
    private static readonly HashSet<string> HostFactSwitches = new(StringComparer.Ordinal)
    {
        "headless", "disable-gpu", "enable-gpu", "use-gl", "use-angle", "use-adapter-luid", "canvas-bridge-url",
        "fingerprint-gpu-host-vendor",
    };

    public delegate string FactsRunner(string[] cmd, IDictionary<string, string> env);
    public delegate Task<(string Token, double Exp)> Fetcher(string runToken, SortedDictionary<string, string> sel,
        IReadOnlyDictionary<string, string> host, string? profileJson, ProxySpec? proxy);

    /// <summary>What a launch gets back: the args (a profile replaced by its hash) and the token for each attempt's env.</summary>
    public sealed class Plan
    {
        public List<string> Args { get; }
        public IReadOnlyDictionary<string, string> Vars { get; }
        internal Plan(List<string> args, IReadOnlyDictionary<string, string> vars) { Args = args; Vars = vars; }
        /// <summary><paramref name="env"/> with the token added (or <paramref name="env"/> itself when this engine needs none).</summary>
        public IDictionary<string, string>? Env(IDictionary<string, string>? env)
        {
            if (Vars.Count == 0) return env;
            var outEnv = env is null ? new Dictionary<string, string>() : new Dictionary<string, string>(env);
            if (env is null) foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables()) outEnv[(string)e.Key] = (string?)e.Value ?? "";
            outEnv.Remove(TokenEnv);
            outEnv.Remove(TokenGzEnv);
            foreach (var kv in Vars) outEnv[kv.Key] = kv.Value;
            return outEnv;
        }
    }

    private static readonly object Lock = new();
    private static readonly Dictionary<string, (string Token, double Exp)> Tokens = new();
    private static readonly Dictionary<string, Dictionary<string, string>> Facts = new();

    private static (string Name, string Value)? Switch(string arg)
    {
        if (!arg.StartsWith("--", StringComparison.Ordinal) || arg.Length == 2) return null;
        var eq = arg.IndexOf('=');
        var name = eq < 0 ? arg[2..] : arg[2..eq];
        return name.Length == 0 ? null : (name, eq < 0 ? "" : arg[(eq + 1)..]);
    }

    public static bool EngineWantsToken(string? exe, Func<string?, string, bool>? supports = null)
        => !string.IsNullOrEmpty(exe) && (supports ?? LaunchOpts.EngineSupportsSwitch)(exe, MarkerSwitch);

    /// <summary>The selector the generator reads from <paramref name="args"/>, the captured profile's exact text, and the args with the profile replaced by its hash.</summary>
    public static (SortedDictionary<string, string> Sel, string? ProfileJson, List<string> Args) Selector(IEnumerable<string> args)
    {
        var sel = new SortedDictionary<string, string>(StringComparer.Ordinal);
        string? profileJson = null;
        var outArgs = new List<string>();
        foreach (var arg in args)
        {
            var s = Switch(arg);
            if (s is { Name: ProfileSwitch })
            {
                var raw = Convert.FromBase64String(s.Value.Value);
                try
                {
                    using var gz = new GZipStream(new MemoryStream(raw), CompressionMode.Decompress);
                    using var ms = new MemoryStream();
                    gz.CopyTo(ms);
                    raw = ms.ToArray();
                }
                catch (Exception) { /* an uncompressed profile */ }
                profileJson = Encoding.UTF8.GetString(raw);
                continue;
            }
            if (s is not null && SelectorSwitches.Contains(s.Value.Name))
                sel[s.Value.Name] = s.Value.Name == "canvas-bridge-url" ? "1" : s.Value.Value;
            outArgs.Add(arg);
        }
        if (profileJson is not null)
        {
            sel.Remove(ProfileShaSwitch);
            outArgs.RemoveAll(a => Switch(a) is { Name: ProfileShaSwitch });
            var sha = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(profileJson))).ToLowerInvariant();
            sel[ProfileShaSwitch] = sha;
            outArgs.Add($"--{ProfileShaSwitch}={sha}");
        }
        return (sel, profileJson, outArgs);
    }

    private static string? FactsCachePath()
    {
        var baseDir = OperatingSystem.IsWindows() ? Environment.GetEnvironmentVariable("LOCALAPPDATA") : Environment.GetEnvironmentVariable("HOME");
        return string.IsNullOrEmpty(baseDir) ? null : Path.Combine(baseDir, ".clearcote", "persona-host-facts.json");
    }

    private static string DefaultRunner(string[] cmd, IDictionary<string, string> env)
    {
        var psi = new ProcessStartInfo(cmd[0]) { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
        foreach (var a in cmd.Skip(1)) psi.ArgumentList.Add(a);
        foreach (var kv in env) psi.Environment[kv.Key] = kv.Value;
        using var p = Process.Start(psi) ?? throw new InvalidOperationException("could not start the engine");
        var stdout = p.StandardOutput.ReadToEndAsync();
        _ = p.StandardError.ReadToEndAsync();
        if (!p.WaitForExit(30000)) { try { p.Kill(true); } catch { /* gone */ } throw new TimeoutException("host-facts probe timed out"); }
        return stdout.Result;
    }

    /// <summary>The facts of this machine the generator needs, as the engine reports them for a launch with these switches (<c>chrome --cc-persona-host-facts</c>); cached per binary + relevant switches.</summary>
    public static Dictionary<string, string> HostFacts(string exe, IEnumerable<string> args, string runToken, FactsRunner? runner = null)
    {
        var relevant = args.Where(a => Switch(a) is { } s && HostFactSwitches.Contains(s.Name)).OrderBy(a => a, StringComparer.Ordinal).ToArray();
        string key;
        try
        {
            var fi = new FileInfo(exe);
            key = JsonSerializer.Serialize(new object[] { fi.FullName, fi.Length, (long)(fi.LastWriteTimeUtc - DateTime.UnixEpoch).TotalSeconds, relevant });
        }
        catch (Exception) { key = JsonSerializer.Serialize(new object[] { exe, 0, 0, relevant }); }
        lock (Lock) { if (Facts.TryGetValue(key, out var hit)) return new Dictionary<string, string>(hit); }
        var disk = FactsCachePath();
        if (disk is not null && File.Exists(disk))
        {
            try
            {
                var all = JsonSerializer.Deserialize<Dictionary<string, Dictionary<string, string>>>(File.ReadAllText(disk));
                if (all is not null && all.TryGetValue(key, out var stored) && stored.TryGetValue("gpu_vendor", out _))
                {
                    lock (Lock) Facts[key] = stored;
                    return new Dictionary<string, string>(stored);
                }
            }
            catch (Exception) { /* unreadable cache: probe again */ }
        }
        var env = new Dictionary<string, string>();
        foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables()) env[(string)e.Key] = (string?)e.Value ?? "";
        env[License.RunTokenEnv] = runToken; // the probe runs after the licence gate, like every launch
        env.Remove(TokenEnv);
        env.Remove(TokenGzEnv);
        Dictionary<string, string> facts;
        try
        {
            var text = (runner ?? DefaultRunner)(new[] { exe, $"--{MarkerSwitch}" }.Concat(relevant).ToArray(), env);
            var last = text.Trim().Split('\n').Last().Trim();
            using var doc = JsonDocument.Parse(last);
            var vendor = doc.RootElement.GetProperty("gpu_vendor").GetString() ?? throw new InvalidOperationException("no gpu_vendor");
            facts = new Dictionary<string, string> { ["gpu_vendor"] = vendor };
        }
        catch (Exception e) { throw new PersonaServiceError($"the engine did not report its host facts ({e.Message})"); }
        lock (Lock) Facts[key] = facts;
        if (disk is not null)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(disk)!);
                Dictionary<string, Dictionary<string, string>> all = new();
                try { all = JsonSerializer.Deserialize<Dictionary<string, Dictionary<string, string>>>(File.ReadAllText(disk)) ?? new(); } catch (Exception) { /* fresh cache */ }
                all[key] = facts;
                File.WriteAllText(disk, JsonSerializer.Serialize(all));
            }
            catch (Exception) { /* best effort */ }
        }
        return new Dictionary<string, string>(facts);
    }

    public static string[] Hosts()
    {
        var raw = (Environment.GetEnvironmentVariable(ApiEnv) ?? "").Trim();
        return raw.Length == 0 ? DefaultHosts : raw.Split(',').Select(h => h.Trim().TrimEnd('/')).Where(h => h.Length > 0).ToArray();
    }

    /// <summary>POST the request to the first persona host that answers.</summary>
    public static async Task<(string Token, double Exp)> FetchAsync(string runToken, SortedDictionary<string, string> sel,
        IReadOnlyDictionary<string, string> host, string? profileJson, ProxySpec? proxy, string[]? hosts = null, int timeoutMs = RequestTimeoutMs)
    {
        var body = new Dictionary<string, object> { ["token"] = runToken, ["sel"] = sel, ["host"] = host };
        if (profileJson is not null) body["profile_json"] = profileJson;
        var payload = JsonSerializer.Serialize(body);
        var failures = new List<string>();
        using var client = ProxiedHttp.Create(proxy);
        foreach (var baseUrl in hosts ?? Hosts())
        {
            HttpResponseMessage res;
            string text;
            try
            {
                using var cts = new CancellationTokenSource(timeoutMs);
                using var req = new HttpRequestMessage(HttpMethod.Post, $"{baseUrl}/v1/persona") { Content = new StringContent(payload, Encoding.UTF8, "application/json") };
                req.Headers.UserAgent.ParseAdd(License.LicenseUserAgent);
                res = await client.SendAsync(req, cts.Token).ConfigureAwait(false);
                text = await res.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
            }
            catch (Exception e) { failures.Add($"{baseUrl}: {e.Message}"); continue; }
            string? token = null, error = null;
            double exp = 0;
            try
            {
                using var doc = JsonDocument.Parse(text);
                if (doc.RootElement.TryGetProperty("persona_token", out var t) && t.ValueKind == JsonValueKind.String) token = t.GetString();
                if (doc.RootElement.TryGetProperty("exp", out var x) && x.ValueKind == JsonValueKind.Number) exp = x.GetDouble();
                if (doc.RootElement.TryGetProperty("error", out var er) && er.ValueKind == JsonValueKind.String) error = er.GetString();
            }
            catch (JsonException) { /* not JSON */ }
            var status = (int)res.StatusCode;
            if (res.IsSuccessStatusCode && token is not null) return (token, exp);
            var reason = error ?? $"HTTP {status}";
            if (status is >= 400 and < 500) throw new PersonaServiceError($"the persona service refused this launch: {reason}");
            failures.Add($"{baseUrl}: {reason}");
        }
        throw new PersonaServiceError($"no persona service answered ({string.Join("; ", failures)}). The engine cannot start without a persona; check outbound HTTPS to persona.clearcotelabs.com.");
    }

    public static async Task<string> TokenForAsync(LeaseSession lease, SortedDictionary<string, string> sel, IReadOnlyDictionary<string, string> host,
        string? profileJson, ProxySpec? proxy, Fetcher? fetch = null)
    {
        var key = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new object?[] { lease.LeaseId, sel, host }))));
        var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
        lock (Lock) { if (Tokens.TryGetValue(key, out var hit) && hit.Exp > now + SkewSec) return hit.Token; }
        var got = await (fetch ?? ((rt, s, h, p, px) => FetchAsync(rt, s, h, p, px)))(lease.Token, sel, host, profileJson, proxy).ConfigureAwait(false);
        lock (Lock) Tokens[key] = got;
        return got.Token;
    }

    /// <summary>The variable(s) the engine reads the token from (gzip + base64 when long).</summary>
    public static IReadOnlyDictionary<string, string> TokenVars(string token)
    {
        if (token.Length <= EnvInlineLimit) return new Dictionary<string, string> { [TokenEnv] = token };
        using var ms = new MemoryStream();
        using (var gz = new GZipStream(ms, CompressionLevel.SmallestSize, leaveOpen: true)) gz.Write(Encoding.ASCII.GetBytes(token));
        return new Dictionary<string, string> { [TokenGzEnv] = Convert.ToBase64String(ms.ToArray()) };
    }

    /// <summary>Prepare a launch of <paramref name="exe"/>: on an r51+ engine with a lease, fetch the persona token; otherwise the args come back unchanged and <see cref="Plan.Env"/> is the identity.</summary>
    public static async Task<Plan> PrepareAsync(string exe, IEnumerable<string> args, LeaseSession? lease, ProxySpec? proxy,
        Func<string?, string, bool>? supports = null, FactsRunner? runner = null, Fetcher? fetch = null)
    {
        var list = args.ToList();
        if (lease is null || string.IsNullOrEmpty(lease.Token) || !EngineWantsToken(exe, supports)) return new Plan(list, new Dictionary<string, string>());
        var (sel, profileJson, outArgs) = Selector(list);
        var host = HostFacts(exe, outArgs, lease.Token, runner);
        var token = await TokenForAsync(lease, sel, host, profileJson, proxy, fetch).ConfigureAwait(false);
        return new Plan(outArgs, TokenVars(token));
    }

    internal static void ClearCaches() { lock (Lock) { Tokens.Clear(); Facts.Clear(); } }
}
