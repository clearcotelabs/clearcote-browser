using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

/// <summary>Server-side personas (r51): the launcher-side half. Mirrors the Python suite's tests/test_persona.py.</summary>
public class PersonaTests : IDisposable
{
    private static readonly Func<string?, string, bool> R51 = (_, name) => name == "cc-persona-host-facts";
    private static readonly Func<string?, string, bool> Old = (_, _) => false;
    private readonly Sandbox _sb = new();
    private readonly string _home;
    private readonly string _dir;
    private readonly string _exe;
    private readonly List<string[]> _probes = new();
    private readonly List<(SortedDictionary<string, string> Sel, IReadOnlyDictionary<string, string> Host, string? Profile)> _fetches = new();

    public PersonaTests()
    {
        _home = _sb.TempHome();
        _dir = Path.Combine(Path.GetTempPath(), "cc-persona-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(_dir);
        _exe = Path.Combine(_dir, "chrome");
        File.WriteAllText(_exe, "x");
        PersonaToken.ClearCaches();
    }

    public void Dispose()
    {
        PersonaToken.ClearCaches();
        try { Directory.Delete(_dir, true); } catch { /* best effort */ }
        _sb.Dispose();
    }

    private string Runner(string[] cmd, IDictionary<string, string> env) { _probes.Add(cmd); Assert.Equal("hdr.sig", env["CLEARCOTE_RUN_TOKEN"]); return "ignored\n{\"gpu_vendor\":\"nvidia\"}\n"; }
    private Task<(string, double)> Fetch(string rt, SortedDictionary<string, string> sel, IReadOnlyDictionary<string, string> host, string? profile, ProxySpec? _)
    {
        _fetches.Add((sel, host, profile));
        return Task.FromResult(("tok-" + string.Join(",", sel.Select(kv => kv.Key + "=" + kv.Value)), (double)DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600));
    }
    private static LeaseSession Lease(string id = "L1") => new(() => "hdr.sig", id, () => Task.CompletedTask, () => throw new InvalidOperationException());

    [Fact]
    public void Selector_keeps_only_generator_switches_last_value_wins_and_bridge_is_presence()
    {
        var (sel, profile, args) = PersonaToken.Selector(new[] { "--headless", "--fingerprint=1", "--fingerprint=2", "--lang=en-US", "--fingerprint-platform=windows",
            "--canvas-bridge-url=ws://x:1?auth=s", "--disable-gpu-string-spoof", "--user-data-dir=/tmp/u" });
        Assert.Equal(new SortedDictionary<string, string> { ["fingerprint"] = "2", ["fingerprint-platform"] = "windows", ["canvas-bridge-url"] = "1", ["disable-gpu-string-spoof"] = "" }, sel);
        Assert.Null(profile);
        Assert.Equal("--user-data-dir=/tmp/u", args.Last());
    }

    [Fact]
    public void A_profile_leaves_argv_and_becomes_its_sha256()
    {
        var text = "{\"screen\":{\"width\":1920}}";
        var (sel, profile, args) = PersonaToken.Selector(new[] { "--fingerprint=7", "--fingerprint-profile=" + Fingerprint.EncodeProfile(text) });
        var sha = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(profile!))).ToLowerInvariant();
        Assert.Equal(text, profile);
        Assert.Equal(sha, sel["fingerprint-profile-sha256"]);
        Assert.Equal(new[] { "--fingerprint=7", $"--fingerprint-profile-sha256={sha}" }, args);
    }

    [Fact]
    public async Task Old_engine_and_free_launch_are_left_alone()
    {
        var a = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, Lease(), null, Old, Runner, Fetch);
        Assert.Equal(new[] { "--fingerprint=1" }, a.Args);
        var env = new Dictionary<string, string> { ["A"] = "1" };
        Assert.Same(env, a.Env(env));
        var b = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, null, null, R51, Runner, Fetch);
        Assert.Null(b.Env(null));
        Assert.Empty(_fetches);
    }

    [Fact]
    public async Task R51_launch_fetches_with_selector_and_host_facts_and_injects_the_token()
    {
        var p = await PersonaToken.PrepareAsync(_exe, new[] { "--headless", "--fingerprint=42", "--fingerprint-platform=linux" }, Lease(), null, R51, Runner, Fetch);
        Assert.Equal(new[] { "--headless", "--fingerprint=42", "--fingerprint-platform=linux" }, p.Args);
        var env = p.Env(new Dictionary<string, string> { ["PATH"] = "/bin" })!;
        Assert.Equal("/bin", env["PATH"]);
        Assert.StartsWith("tok-", env[PersonaToken.TokenEnv]);
        Assert.Equal(new[] { _exe, "--cc-persona-host-facts", "--headless" }, _probes.Single());
        Assert.Equal(new SortedDictionary<string, string> { ["fingerprint"] = "42", ["fingerprint-platform"] = "linux" }, _fetches.Single().Sel);
        Assert.Equal("nvidia", _fetches.Single().Host["gpu_vendor"]);
    }

    [Fact]
    public async Task Seedless_launch_still_fetches_a_default_persona()
    {
        var p = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint-platform=windows", "--window-size=1,1" }, Lease(), null, R51, Runner, Fetch);
        Assert.Equal(new SortedDictionary<string, string> { ["fingerprint-platform"] = "windows" }, _fetches.Single().Sel);
        Assert.Contains(PersonaToken.TokenEnv, p.Env(new Dictionary<string, string>())!.Keys);
    }

    [Fact]
    public async Task Tokens_cache_per_lease_and_selector_and_facts_per_binary()
    {
        for (var i = 0; i < 3; i++) await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=5" }, Lease(), null, R51, Runner, Fetch);
        await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=6" }, Lease(), null, R51, Runner, Fetch);
        await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=5" }, Lease("L2"), null, R51, Runner, Fetch);
        Assert.Equal(3, _fetches.Count);
        Assert.Single(_probes);
    }

    [Fact]
    public async Task A_profile_is_uploaded_as_its_exact_text()
    {
        var text = "{\"screen\":{\"width\":1366},\"b\":1.0}";
        var p = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint-profile=" + Fingerprint.EncodeProfile(text) }, Lease(), null, R51, Runner, Fetch);
        var sha = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
        Assert.Equal(text, _fetches.Single().Profile);
        Assert.Equal(new SortedDictionary<string, string> { ["fingerprint-profile-sha256"] = sha }, _fetches.Single().Sel);
        Assert.Equal(new[] { $"--fingerprint-profile-sha256={sha}" }, p.Args);
    }

    [Fact]
    public async Task Broken_host_facts_raise()
    {
        var e = await Assert.ThrowsAsync<PersonaServiceError>(() => PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, Lease(), null, R51, (_, _) => "not json", Fetch));
        Assert.Contains("host facts", e.Message);
    }

    // 0.63.0/0.63.1 on r51: the probe got CLEARCOTE_RUN_TOKEN only, the engine refused the FREE token without its
    // file, and every free launch failed before its persona was fetched.
    [Fact]
    public async Task The_host_facts_probe_gets_the_launch_run_token_file()
    {
        IDictionary<string, string>? seen = null;
        string Capture(string[] cmd, IDictionary<string, string> env) { seen = new Dictionary<string, string>(env); return Runner(cmd, env); }
        var file = Path.Combine(_dir, "run.tok");
        await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=77" }, Lease(), null, R51, Capture, Fetch, runTokenFile: file);
        Assert.NotNull(seen);
        Assert.Equal(file, seen!["CLEARCOTE_RUN_TOKEN_FILE"]);
        Assert.Equal("hdr.sig", seen["CLEARCOTE_RUN_TOKEN"]);
    }

    [Fact]
    public void Long_tokens_go_gzipped()
    {
        var vars = PersonaToken.TokenVars(new string('a', 40000));
        Assert.False(vars.ContainsKey(PersonaToken.TokenEnv));
        using var gz = new GZipStream(new MemoryStream(Convert.FromBase64String(vars[PersonaToken.TokenGzEnv])), CompressionMode.Decompress);
        using var ms = new MemoryStream();
        gz.CopyTo(ms);
        Assert.Equal(new string('a', 40000), Encoding.ASCII.GetString(ms.ToArray()));
        Assert.Equal("short", PersonaToken.TokenVars("short")[PersonaToken.TokenEnv]);
    }

    [Fact]
    public async Task Fetch_posts_to_the_first_host_that_answers_and_maps_refusals()
    {
        using var listener = new HttpListener();
        var port = FreePort();
        listener.Prefixes.Add($"http://127.0.0.1:{port}/");
        listener.Start();
        var status = 200;
        var bodies = new List<string>();
        var serving = Task.Run(async () =>
        {
            while (listener.IsListening)
            {
                HttpListenerContext ctx;
                try { ctx = await listener.GetContextAsync(); } catch { return; }
                using var reader = new StreamReader(ctx.Request.InputStream);
                bodies.Add(await reader.ReadToEndAsync());
                var answer = status == 200 ? "{\"persona_token\":\"tok-1\",\"exp\":4102444800}" : "{\"error\":\"refused " + status + "\"}";
                ctx.Response.StatusCode = status;
                ctx.Response.ContentType = "application/json";
                var bytes = Encoding.UTF8.GetBytes(answer);
                await ctx.Response.OutputStream.WriteAsync(bytes);
                ctx.Response.Close();
            }
        });
        try
        {
            var hosts = new[] { "http://127.0.0.1:9", $"http://127.0.0.1:{port}" };
            var sel = new SortedDictionary<string, string> { ["fingerprint"] = "1" };
            var host = new Dictionary<string, string> { ["gpu_vendor"] = "none" };
            var (token, exp) = await PersonaToken.FetchAsync("hdr.sig", sel, host, null, null, hosts, 5000);
            Assert.Equal("tok-1", token);
            Assert.Equal(4102444800, exp);
            using (var doc = JsonDocument.Parse(bodies.Single()))
            {
                Assert.Equal("hdr.sig", doc.RootElement.GetProperty("token").GetString());
                Assert.Equal("1", doc.RootElement.GetProperty("sel").GetProperty("fingerprint").GetString());
                Assert.False(doc.RootElement.TryGetProperty("profile_json", out _));
            }
            status = 429;
            var e = await Assert.ThrowsAsync<PersonaServiceError>(() => PersonaToken.FetchAsync("hdr.sig", sel, host, null, null, hosts, 5000));
            Assert.Contains("refused this launch: refused 429", e.Message);
            var none = await Assert.ThrowsAsync<PersonaServiceError>(() => PersonaToken.FetchAsync("hdr.sig", sel, host, null, null, new[] { "http://127.0.0.1:9" }, 1000));
            Assert.Contains("no persona service answered", none.Message);
        }
        finally { listener.Stop(); await serving; }
    }

    // --- stale host GPU vendor (CR-ZQJ4SX) --------------------------------------------------------------------------

    private const string StaleMsg = "BrowserType.LaunchAsync: Target page, context or browser has been closed\n"
        + "[pid=14076][err] [clearcote] no usable persona for this run (generated for another host GPU vendor "
        + "(none, this machine: nvidia)). This engine needs a Clearcote SDK, Profile Manager or Helper that "
        + "fetches personas (r51+).";

    // Up to 0.70.0 a saved answer was never checked again: a "none" saved before the NVIDIA driver (or in a Remote
    // Desktop session) failed every later launch. A new process asks the engine again, and writes nothing.
    [Fact]
    public async Task Host_facts_are_not_kept_on_disk()
    {
        _sb.Env("LOCALAPPDATA", _home);
        var dir = Path.Combine(_home, ".clearcote");
        var old = Path.Combine(dir, "persona-host-facts.json");
        Directory.CreateDirectory(dir);
        var fi = new FileInfo(_exe);
        var key = JsonSerializer.Serialize(new object[] { fi.FullName, fi.Length, (long)(fi.LastWriteTimeUtc - DateTime.UnixEpoch).TotalSeconds, Array.Empty<string>() });
        var saved = JsonSerializer.Serialize(new Dictionary<string, Dictionary<string, string>> { [key] = new() { ["gpu_vendor"] = "none" } });
        File.WriteAllText(old, saved);
        await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, Lease(), null, R51, Runner, Fetch);
        Assert.Single(_probes);   // the old file's "none" was not used
        Assert.Equal("nvidia", _fetches.Single().Host["gpu_vendor"]);
        Assert.Equal(saved, File.ReadAllText(old));   // nor rewritten
        Assert.Equal(new[] { old }, Directory.GetFileSystemEntries(dir));
        File.Delete(old);
        PersonaToken.ClearCaches();
        await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, Lease(), null, R51, Runner, Fetch);
        Assert.Equal(2, _probes.Count);
        Assert.Empty(Directory.GetFileSystemEntries(dir));   // and none written on a machine without one
    }

    [Fact]
    public void Stale_host_vendor_reads_the_engine_refusal()
    {
        Assert.Equal(("none", "nvidia"), PersonaToken.StaleHostVendor(new Exception(StaleMsg)));
        Assert.Equal(("?", "amd"), PersonaToken.StaleHostVendor(new Exception("... (generated for another host GPU vendor (?, this machine: amd))")));
        Assert.Equal(("none", "nvidia"), PersonaToken.StaleHostVendor(new InvalidOperationException("launch failed", new Exception(StaleMsg))));
        Assert.Null(PersonaToken.StaleHostVendor(new Exception("[clearcote] no usable persona for this run (not valid yet)")));
        Assert.Null(PersonaToken.StaleHostVendor(new Exception("Target page, context or browser has been closed")));
    }

    [Fact]
    public async Task Heal_fetches_a_persona_for_the_vendor_the_engine_named_without_a_new_probe()
    {
        var probes = 0;
        string NoneRunner(string[] cmd, IDictionary<string, string> env) { probes++; return "{\"gpu_vendor\":\"none\"}"; }   // the heal must not depend on it
        var lease = Lease();
        var text = "{\"screen\":{\"width\":1366}}";
        var args = new[] { "--fingerprint=3", "--headless", "--fingerprint-profile=" + Fingerprint.EncodeProfile(text) };
        var plan = await PersonaToken.PrepareAsync(_exe, args, lease, null, R51, NoneRunner, Fetch);
        Assert.Equal("none", _fetches.Single().Host["gpu_vendor"]);

        var err = new StringWriter();
        var stdErr = Console.Error;
        Console.SetError(err);
        bool healed;
        try { healed = await plan.HealAsync(new Exception(StaleMsg)); }
        finally { Console.SetError(stdErr); }
        Assert.True(healed);
        Assert.Equal(2, _fetches.Count);   // a new token (the cached one was for "none")
        Assert.Equal(new Dictionary<string, string> { ["gpu_vendor"] = "nvidia" }, _fetches[1].Host);
        Assert.Equal(_fetches[0].Sel, _fetches[1].Sel);   // the same persona, recomputed from the args before apply
        Assert.Equal(text, _fetches[1].Profile);
        Assert.Equal(1, probes);   // no second probe: the engine's own answer was used
        Assert.Contains("clearcote: the GPU this machine renders on changed (none -> nvidia); fetched a persona for it and launching once more", err.ToString());
        // the rest of this process launches with the corrected answer
        Assert.Equal("nvidia", PersonaToken.HostFacts(_exe, args, lease.Token, NoneRunner)["gpu_vendor"]);
        Assert.Equal(1, probes);
    }

    [Fact]
    public async Task Heal_puts_the_new_token_into_the_launch_env()
    {
        var n = 0;
        Task<(string, double)> Counting(string rt, SortedDictionary<string, string> sel, IReadOnlyDictionary<string, string> host, string? profile, ProxySpec? _)
            => Task.FromResult(($"tok-{++n}-{host["gpu_vendor"]}", (double)DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600));
        var plan = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=9" }, Lease(), null, R51, (_, _) => "{\"gpu_vendor\":\"none\"}", Counting);
        Assert.Equal("tok-1-none", plan.Env(new Dictionary<string, string>())![PersonaToken.TokenEnv]);
        var stdErr = Console.Error;
        Console.SetError(TextWriter.Null);
        try { Assert.True(await plan.HealAsync(new Exception(StaleMsg))); }
        finally { Console.SetError(stdErr); }
        var env = plan.Env(new Dictionary<string, string> { ["PATH"] = "/bin" })!;
        Assert.Equal("tok-2-nvidia", env[PersonaToken.TokenEnv]);
        Assert.Equal("/bin", env["PATH"]);
    }

    [Fact]
    public async Task Heal_ignores_other_failures_and_plans_without_a_token()
    {
        var plan = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, Lease(), null, R51, Runner, Fetch);
        Assert.False(await plan.HealAsync(new Exception("[clearcote] no usable persona for this run (not valid yet)")));
        Assert.Single(_fetches);
        Assert.Null(await PersonaToken.HealStaleHostVendorAsync(_exe, new[] { "--fingerprint=1" }, Lease(), null, new Exception("boom"), R51, Runner, Fetch));
        Assert.Single(_fetches);
        var free = await PersonaToken.PrepareAsync(_exe, new[] { "--fingerprint=1" }, null, null, R51, Runner, Fetch);
        Assert.False(await free.HealAsync(new Exception(StaleMsg)));   // no lease, no token: nothing to heal
        Assert.Single(_fetches);
    }

    [Fact]
    public async Task Launch_retries_once_on_a_stale_host_vendor()
    {
        var healed = new List<string>();
        var current = "old";
        Task<bool> Heal(Exception e)
        {
            healed.Add(e.Message);
            if (PersonaToken.StaleHostVendor(e) is null) return Task.FromResult(false);
            current = "new";
            return Task.FromResult(true);
        }
        var starts = new List<string>();
        var got = await PersonaToken.RetryOnStaleHostVendorAsync(Heal, () =>
        {
            starts.Add(current);
            if (starts.Count == 1) throw new PlaywrightLike(StaleMsg);
            return Task.FromResult("browser");
        });
        Assert.Equal("browser", got);
        Assert.Equal(new[] { "old", "new" }, starts);
        Assert.Single(healed);

        // once only: a second refusal is raised, not looped on
        var tries = 0;
        healed.Clear();
        var again = await Assert.ThrowsAsync<PlaywrightLike>(() => PersonaToken.RetryOnStaleHostVendorAsync<string>(Heal, () => { tries++; throw new PlaywrightLike(StaleMsg); }));
        Assert.Equal(2, tries);
        Assert.Single(healed);
        Assert.Contains("this machine: nvidia", again.Message);

        // any other failure passes through unchanged, without a second start
        tries = 0;
        healed.Clear();
        var boom = new InvalidOperationException("boom");
        var other = await Assert.ThrowsAsync<InvalidOperationException>(() => PersonaToken.RetryOnStaleHostVendorAsync<string>(Heal, () => { tries++; throw boom; }));
        Assert.Same(boom, other);
        Assert.Equal(1, tries);

        // a heal that fails (the persona service down) is what the caller sees
        var down = new PersonaServiceError("no persona service answered");
        Assert.Same(down, await Assert.ThrowsAsync<PersonaServiceError>(() => PersonaToken.RetryOnStaleHostVendorAsync<string>(_ => throw down, () => throw new PlaywrightLike(StaleMsg))));
    }

    private sealed class PlaywrightLike : Exception { public PlaywrightLike(string m) : base(m) { } }

    private static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}
