using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

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
        /// <summary>The token variable(s). Replaced by <see cref="HealAsync"/>, so each attempt reads them when it builds its env.</summary>
        public IReadOnlyDictionary<string, string> Vars { get; private set; }
        private readonly Func<Exception, Task<Plan?>>? _heal;
        internal Plan(List<string> args, IReadOnlyDictionary<string, string> vars, Func<Exception, Task<Plan?>>? heal = null) { Args = args; Vars = vars; _heal = heal; }

        /// <summary>After a launch failed with <paramref name="e"/>: when the engine refused the persona as "generated for
        /// another host GPU vendor", fetch a token for the vendor it named into <see cref="Vars"/> and return true so the
        /// caller launches once more (<see cref="RetryOnStaleHostVendorAsync{T}"/>). False for any other failure.</summary>
        public async Task<bool> HealAsync(Exception e)
        {
            if (_heal is null) return false;
            var again = await _heal(e).ConfigureAwait(false);
            if (again is null) return false;
            Vars = again.Vars;
            return true;
        }
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

    /// <summary>The probe's stdout, or an exception saying why there is none. The engine prints a refusal (no lease, a
    /// FREE token without its file, the clock set back) on stderr only: reading stdout alone turned every one of those
    /// into a JSON parse error.</summary>
    internal static string DescribeProbe(string? stdout, string? stderr, int exitCode)
    {
        if (exitCode == 0 && !string.IsNullOrWhiteSpace(stdout)) return stdout!;
        var said = (stderr ?? "").Split('\n').Select(l => l.Trim()).LastOrDefault(l => l.Length > 0) ?? "";
        if (said.StartsWith("[clearcote]", StringComparison.Ordinal)) said = said["[clearcote]".Length..].TrimStart();
        if (said.Length > 300) said = said[..300];
        var why = exitCode != 0 ? $"it exited with code {exitCode}" : "it exited without output";
        throw new InvalidOperationException(said.Length > 0 ? $"{why}: {said}" : why);
    }

    /// <summary><paramref name="run"/>(exe) with the launch's recovery (WinLaunch): the probe runs before the launch on
    /// the same binary, so without it a build Windows will not start in place failed the probe on every launch and the
    /// launch's own recovery was never reached. From this build's recovered copy when one exists; else in place, then
    /// once from a new recovered copy.</summary>
    internal static T SpawnProbe<T>(string exe, Func<string, T> run, bool windows)
    {
        if (!windows) return run(exe);
        var ready = WinLaunch.RecoveredExe(exe);
        if (ready is not null)
        {
            try { return run(ready); }
            catch (Exception e) when (WinLaunch.IsWinLaunchRace(e)) { /* the copy went bad: try in place */ }
        }
        try { return run(exe); }
        catch (Exception first) when (WinLaunch.IsWinLaunchRace(first))
        {
            string copy;
            try { copy = WinLaunch.MakeRecoveredCopy(exe, WinLaunch.WarmFiles); }
            catch (Exception) { throw first; } // the copy failed (disk full, locked files): report the first failure
            return run(copy);
        }
    }

    private static string DefaultRunner(string[] cmd, IDictionary<string, string> env)
    {
        (string Out, string Err, int Code) Run(string exe)
        {
            var psi = new ProcessStartInfo(exe) { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
            foreach (var a in cmd.Skip(1)) psi.ArgumentList.Add(a);
            foreach (var kv in env) psi.Environment[kv.Key] = kv.Value;
            using var p = Process.Start(psi) ?? throw new InvalidOperationException("Process.Start returned no process");
            var stdout = p.StandardOutput.ReadToEndAsync();
            var stderr = p.StandardError.ReadToEndAsync();
            if (!p.WaitForExit(30000)) { try { p.Kill(true); } catch { /* gone */ } throw new TimeoutException("it did not answer within 30 s"); }
            return (stdout.Result, stderr.Result, p.ExitCode);
        }
        (string Out, string Err, int Code) r;
        try { r = SpawnProbe(cmd[0], Run, OperatingSystem.IsWindows()); }
        catch (TimeoutException) { throw; }
        catch (Exception e) { throw new InvalidOperationException($"it could not be started: {e.Message}"); }
        return DescribeProbe(r.Out, r.Err, r.Code);
    }

    private static (string Key, string[] Relevant) FactsKey(string exe, IEnumerable<string> args)
    {
        var relevant = args.Where(a => Switch(a) is { } s && HostFactSwitches.Contains(s.Name)).OrderBy(a => a, StringComparer.Ordinal).ToArray();
        try
        {
            var fi = new FileInfo(exe);
            return (JsonSerializer.Serialize(new object[] { fi.FullName, fi.Length, (long)(fi.LastWriteTimeUtc - DateTime.UnixEpoch).TotalSeconds, relevant }), relevant);
        }
        catch (Exception) { return (JsonSerializer.Serialize(new object[] { exe, 0, 0, relevant }), relevant); }
    }

    /// <summary>The facts of this machine the generator needs, as the engine reports them for a launch with these
    /// switches (<c>chrome --cc-persona-host-facts</c>); cached per binary + relevant switches for the life of this
    /// process only.</summary>
    /// <remarks>Up to 0.70.0 the answer was also kept on disk (<c>persona-host-facts.json</c>) and never checked again.
    /// The vendor is that of the adapter Windows lists first, which changes with a driver install or a Remote Desktop
    /// session; a saved "none" on a machine that now renders on NVIDIA then failed every launch with "generated for
    /// another host GPU vendor". The probe takes about 0.1 s, so each process asks once.</remarks>
    public static Dictionary<string, string> HostFacts(string exe, IEnumerable<string> args, string runToken, FactsRunner? runner = null, string? runTokenFile = null)
    {
        var (key, relevant) = FactsKey(exe, args);
        lock (Lock) { if (Facts.TryGetValue(key, out var hit)) return new Dictionary<string, string>(hit); }
        var env = new Dictionary<string, string>();
        foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables()) env[(string)e.Key] = (string?)e.Value ?? "";
        env[License.RunTokenEnv] = runToken; // the probe runs after the licence gate, like every launch
        // And with the launch's token FILE: the engine refuses a FREE token without it, so a probe given only
        // CLEARCOTE_RUN_TOKEN died and every free launch failed before its persona was fetched (0.63.0/0.63.1).
        if (!string.IsNullOrEmpty(runTokenFile)) env[License.RunTokenFileEnv] = runTokenFile;
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
        return new Dictionary<string, string>(facts);
    }

    // The engine's refusal when the persona was generated for another GPU vendor than the one it renders on
    // (pro-r51-02 Bind): "generated for another host GPU vendor (<token's>, this machine: <engine's>)". The second
    // value is exactly what the probe reports ("none" for no hardware GPU), so it can stand in for a new probe.
    private static readonly Regex StaleVendor = new(@"generated for another host GPU vendor \(([^,()]*), this machine: ([A-Za-z0-9_.-]+)\)", RegexOptions.CultureInvariant);

    /// <summary>(token's vendor, this machine's vendor) when <paramref name="e"/> is the engine refusing a persona
    /// generated for another host GPU vendor, else null. Playwright's launch error carries the engine's stderr.</summary>
    public static (string Was, string Now)? StaleHostVendor(Exception e)
    {
        var m = StaleVendor.Match(e.ToString());
        return m.Success ? (m.Groups[1].Value, m.Groups[2].Value) : null;
    }

    /// <summary>After a launch the engine refused for "another host GPU vendor": take the vendor the engine named as
    /// this machine's (no new probe), fetch a persona token for it and return the new plan. <paramref name="args"/> are
    /// the launch's args as they were BEFORE <see cref="PrepareAsync"/> (a profile still on them). Null when
    /// <paramref name="e"/> is anything else.</summary>
    public static async Task<Plan?> HealStaleHostVendorAsync(string exe, IEnumerable<string> args, LeaseSession? lease, ProxySpec? proxy, Exception e,
        Func<string?, string, bool>? supports = null, FactsRunner? runner = null, Fetcher? fetch = null, string? runTokenFile = null,
        double? now = null, bool quiet = false)
    {
        if (StaleHostVendor(e) is not { } stale) return null;
        var (was, vendor) = stale;
        var list = args.ToList();
        var (key, _) = FactsKey(exe, Selector(list).Args);
        lock (Lock) Facts[key] = new Dictionary<string, string> { ["gpu_vendor"] = vendor };
        Console.Error.WriteLine($"clearcote: the GPU this machine renders on changed ({was} -> {vendor}); fetched a persona for it and launching once more");
        return await PrepareAsync(exe, list, lease, proxy, supports, runner, fetch, runTokenFile, now, quiet).ConfigureAwait(false);
    }

    /// <summary>Run <paramref name="start"/>; if <paramref name="heal"/> says the failure was a persona generated for
    /// another host GPU vendor (and fetched one for this machine's), start once more. A second refusal, and any other
    /// failure, is raised as it is. <paramref name="start"/> must read the plan's <see cref="Plan.Vars"/> when called.</summary>
    public static async Task<T> RetryOnStaleHostVendorAsync<T>(Func<Exception, Task<bool>> heal, Func<Task<T>> start)
    {
        try
        {
            return await start().ConfigureAwait(false);
        }
        catch (Exception e)
        {
            if (!await heal(e).ConfigureAwait(false)) throw;
        }
        return await start().ConfigureAwait(false);
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
        Func<string?, string, bool>? supports = null, FactsRunner? runner = null, Fetcher? fetch = null, string? runTokenFile = null,
        double? now = null, bool quiet = false)
    {
        var list = args.ToList();
        if (lease is null || string.IsNullOrEmpty(lease.Token) || !EngineWantsToken(exe, supports)) return new Plan(list, new Dictionary<string, string>());
        // A clock far off makes the engine refuse the token whatever the key: say so before the probe fails on it.
        ClockCheck.AssertFor(lease.Token, "licence token", now);
        var (sel, profileJson, outArgs) = Selector(list);
        var host = HostFacts(exe, outArgs, lease.Token, runner, runTokenFile);
        var token = await TokenForAsync(lease, sel, host, profileJson, proxy, fetch).ConfigureAwait(false);
        ClockCheck.AssertFor(token, "persona token", now);
        WarnGpuHostMismatch(token, host, outArgs, sel, profileJson, quiet);
        return new Plan(outArgs, TokenVars(token),
            e => HealStaleHostVendorAsync(exe, list, lease, proxy, e, supports, runner, fetch, runTokenFile, now, quiet));
    }


    // ── GPU vendor guard ─────────────────────────────────────────────────────────────────────────────────────────
    //
    // Sites can send a seed, run seeded arithmetic on the GPU and compare what the computing GPU's class (its vendor and
    // graphics backend, e.g. NVIDIA on Direct3D 11) returns with the WebGL renderer the page declares. A persona that
    // names another vendor than the GPU that really computes ("the card that computes is not the declared graphics
    // card"), or a graphics card's name over a software renderer (SwiftShader / WARP on Remote Desktop, a VM or a VPS),
    // is caught by that check whatever the strings say. The engine matches the persona to this machine's vendor by
    // itself on NVIDIA and Intel hosts; explicit GpuVendor / GpuRenderer, an imported profile, an AMD host or a
    // software-rendering host can still end up with another vendor. After the persona token is fetched, the GPU it
    // declares (the token's payload is the generator's own output) is compared with the vendor the engine reports for
    // this machine at this launch (read fresh every process, see HostFacts), and a mismatch is said once per process.
    // CLEARCOTE_NO_GPU_HOST_CHECK=1 silences it (as do Quiet and CLEARCOTE_NO_WARN). Mirrors persona.ts.

    public const string GpuCheckEnv = "CLEARCOTE_NO_GPU_HOST_CHECK";
    private static readonly HashSet<string> GpuCheckSkipSwitches = new(StringComparer.Ordinal)
    {
        "canvas-bridge-url", "use-adapter-luid", "disable-gpu-string-spoof", "disable-gpu-fingerprint", "fingerprint-passthrough",
    };
    private static readonly HashSet<string> HardwareMakers = new(StringComparer.Ordinal) { "nvidia", "intel", "amd", "apple", "qualcomm", "arm" };
    private static readonly Dictionary<string, string> HostVendorNames = new(StringComparer.Ordinal)
        { ["nvidia"] = "NVIDIA", ["intel"] = "Intel", ["amd"] = "AMD" };
    private static readonly Dictionary<string, string> MakerNames = new(StringComparer.Ordinal)
        { ["nvidia"] = "NVIDIA", ["intel"] = "Intel", ["amd"] = "AMD", ["apple"] = "Apple", ["qualcomm"] = "Qualcomm", ["arm"] = "ARM" };

    /// <summary>The WebGL vendor and renderer a persona token declares (its payload <c>p</c> is the generator's codec
    /// JSON), or null.</summary>
    public static (string Vendor, string Renderer)? PersonaGpuFromToken(string token)
    {
        try
        {
            var body = token.Split('.')[0].Replace('-', '+').Replace('_', '/');
            body += new string('=', (4 - body.Length % 4) % 4);
            using var doc = JsonDocument.Parse(Convert.FromBase64String(body));
            if (!doc.RootElement.TryGetProperty("p", out var p) || p.ValueKind != JsonValueKind.Object
                || !p.TryGetProperty("persona", out var persona) || persona.ValueKind != JsonValueKind.Object) return null;
            if (!persona.TryGetProperty("webgl_renderer", out var r) || r.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(r.GetString())) return null;
            var v = persona.TryGetProperty("webgl_vendor", out var ve) && ve.ValueKind == JsonValueKind.String ? ve.GetString() ?? "" : "";
            return (v, r.GetString()!);
        }
        catch
        {
            return null;
        }
    }

    /// <summary>Where the persona's GPU comes from: "options" (GpuVendor / GpuRenderer), "profile" (an imported profile
    /// carrying WebGL strings, or a library profile id) or "engine" (the engine's own rule).</summary>
    public static string GpuSource(IReadOnlyDictionary<string, string> sel, string? profileJson)
    {
        if (sel.ContainsKey("fingerprint-gpu-vendor") || sel.ContainsKey("fingerprint-gpu-renderer")) return "options";
        if (sel.ContainsKey("fingerprint-profile-id")) return "profile";
        if (profileJson is not null)
        {
            try
            {
                using var doc = JsonDocument.Parse(profileJson);
                if (doc.RootElement.ValueKind == JsonValueKind.Object
                    && doc.RootElement.TryGetProperty("webgl", out var w) && w.ValueKind == JsonValueKind.Object
                    && w.TryGetProperty("webgl1", out var w1) && w1.ValueKind == JsonValueKind.Object
                    && w1.TryGetProperty("debug", out var dbg) && dbg.ValueKind == JsonValueKind.Object
                    && ((dbg.TryGetProperty("UNMASKED_VENDOR_WEBGL", out var uv) && uv.ValueKind == JsonValueKind.String)
                        || (dbg.TryGetProperty("UNMASKED_RENDERER_WEBGL", out var ur) && ur.ValueKind == JsonValueKind.String)))
                    return "profile";
            }
            catch { /* not JSON: the engine refuses it anyway */ }
        }
        return "engine";
    }

    /// <summary>The mismatch between the GPU a persona declares and the one this machine renders on, or null when they
    /// agree or the launch has nothing to compare. <paramref name="host"/>'s gpu_vendor is the engine's answer for this
    /// launch: "nvidia", "intel", "amd", or "none" (no hardware adapter WebGL renders on: software rendering, a virtual
    /// adapter, Linux headless).</summary>
    public static (string Code, string Message)? GpuHostMismatch((string Vendor, string Renderer)? persona,
        IReadOnlyDictionary<string, string> host, IEnumerable<string> args, string source)
    {
        if (persona is not { } pg) return null;
        if (args.Any(a => Switch(a) is { } s && GpuCheckSkipSwitches.Contains(s.Name))) return null;
        var maker = Fingerprint.GpuMaker(pg.Renderer) ?? Fingerprint.GpuMaker(pg.Vendor);
        if (maker is null || !HardwareMakers.Contains(maker)) return null;   // a software or unknown renderer claims no card
        var hv = (host.TryGetValue("gpu_vendor", out var h) ? h : "").ToLowerInvariant();
        var claim = $"{MakerNames[maker]} GPU (\"{pg.Renderer}\")";
        const string why = "Sites that send a seed and run GPU arithmetic compare the card that computes with the declared " +
            "graphics card, and report the graphics as spoofed whatever the strings say.";
        var off = $" Set {GpuCheckEnv}=1 to silence this.";
        var (fix, from) = source switch
        {
            "options" => ("Remove GpuVendor / GpuRenderer (the persona then claims this machine's vendor), or set them to this machine's GPU.",
                "GpuVendor / GpuRenderer declare"),
            "profile" => ("Use a profile captured on a machine with the same GPU vendor as this one.", "the imported profile declares"),
            _ => ("This engine's persona pool has no matching GPU for this vendor yet: set GpuVendor and GpuRenderer to this " +
                  "machine's GPU (what WebGL reports with GpuStringSpoof = false).", "the persona declares"),
        };
        if (hv == "none")
            return ($"gpu-software-host:{maker}",
                "this machine renders WebGL in software (no NVIDIA, Intel or AMD adapter: Remote Desktop, a VM or a VPS, " +
                $"or Linux headless without a GPU), but {from} a {claim}. Sites that run GPU arithmetic see a software renderer " +
                "under a graphics card's name. Run on a machine with a GPU, use the canvas bridge, or pass GpuStringSpoof = false " +
                $"to show the real renderer.{off}");
        if (!HostVendorNames.TryGetValue(hv, out var hostName) || hv == maker) return null;
        return ($"gpu-vendor-host:{maker}:{hv}", $"{from} a {claim}, but this machine renders on {hostName}. {why} {fix}{off}");
    }

    /// <summary>Say a <see cref="GpuHostMismatch"/> once per process, unless quiet, CLEARCOTE_NO_WARN or
    /// CLEARCOTE_NO_GPU_HOST_CHECK.</summary>
    internal static void WarnGpuHostMismatch(string token, IReadOnlyDictionary<string, string> host, IEnumerable<string> args,
        IReadOnlyDictionary<string, string> sel, string? profileJson, bool quiet)
    {
        if (quiet) return;
        var opt = (Environment.GetEnvironmentVariable(GpuCheckEnv) ?? "").Trim().ToLowerInvariant();
        if (opt is "1" or "true" or "yes" or "on") return;
        if (GpuHostMismatch(PersonaGpuFromToken(token), host, args, GpuSource(sel, profileJson)) is { } m)
            LaunchWarnings.EmitOnce(new[] { new LaunchWarnings.Warning(m.Code, m.Message) }, quiet);
    }

    internal static void ClearCaches() { lock (Lock) { Tokens.Clear(); Facts.Clear(); } }
}
