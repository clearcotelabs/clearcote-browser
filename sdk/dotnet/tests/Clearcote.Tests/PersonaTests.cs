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
    private readonly string _dir;
    private readonly string _exe;
    private readonly List<string[]> _probes = new();
    private readonly List<(SortedDictionary<string, string> Sel, IReadOnlyDictionary<string, string> Host, string? Profile)> _fetches = new();

    public PersonaTests()
    {
        _sb.TempHome();
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

    private static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}
