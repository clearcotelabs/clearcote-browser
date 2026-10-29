using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// The in-browser AI agent: its engine switches (<see cref="Agent.Args"/>), their wiring into every launch,
/// <see cref="Clearcote.LaunchAgentAsync"/>, and the <c>Browser.agentRunTask</c> plumbing behind
/// <see cref="Clearcote.RunAgentTaskAsync"/>. Ports sdk/node/test/agent.test.ts and
/// sdk/python/tests/test_agent.py; the launch tests use a stand-in engine that writes down its command line
/// and exits (as StockRuntimeTests), so they are POSIX-only.
/// </summary>
public class AgentTests : IDisposable
{
    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();
    private readonly string _temp;

    public AgentTests()
    {
        _sb.TempHome();
        _temp = Dir("cc-agent-tmp-");
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_NO_WARN", null).Env("CLEARCOTE_STOCK_RUNTIME", null)
            .Env("CLEARCOTE_RUNTIME_BINDINGS", null)
            .Env(PersonaEnv.OptOutEnv, null).Env(PersonaEnv.EnvVar, null).Env("CLEARCOTE_CLOUD", null)
            .Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null)
            .Env("TMPDIR", _temp).Env("TMP", _temp).Env("TEMP", _temp);
    }

    public void Dispose()
    {
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    // ── Agent.Args (agent.test.ts "agentArgs") ───────────────────────────────

    [Fact]
    public void Is_off_unless_a_key_or_url_is_set()
    {
        Assert.Empty(Agent.Args(new LaunchOptions()));
        // a model alone does NOT enable the agent (needs a key or url)
        Assert.Empty(Agent.Args(new LaunchOptions { AgentModel = "openai/gpt-4o-mini" }));
    }

    [Fact]
    public void A_key_enables_it_and_defaults_the_url_to_OpenRouter()
    {
        var args = Agent.Args(new LaunchOptions { AgentLlmKey = "sk-or-1" });
        Assert.Contains($"--agent-llm-url={Agent.OpenRouterBaseUrl}", args);
        Assert.Contains("--agent-llm-key=sk-or-1", args);
        Assert.Equal("https://openrouter.ai/api/v1", Agent.OpenRouterBaseUrl);
    }

    [Fact]
    public void A_url_alone_enables_it_without_a_key()
    {
        var args = Agent.Args(new LaunchOptions { AgentLlmUrl = "http://localhost:1234/v1" });
        Assert.Contains("--agent-llm-url=http://localhost:1234/v1", args);
        Assert.DoesNotContain(args, a => a.StartsWith("--agent-llm-key=", StringComparison.Ordinal));
    }

    [Fact]
    public void Defaults_typing_to_the_human_cadence_when_the_agent_is_on()
    {
        var args = Agent.Args(new LaunchOptions { AgentLlmKey = "k" });
        Assert.Contains(args, a => a.StartsWith("--enable-features=GlicActorIncrementalTyping:", StringComparison.Ordinal));
        // the human profile pushes the auto-paste threshold out so long text still types key-by-key
        Assert.Contains(args, a => a.Contains("long-text-paste-threshold/100000", StringComparison.Ordinal));
        Assert.Equal(args, Agent.Args(new LaunchOptions { AgentLlmKey = "k", AgentTyping = AgentTyping.Human }));
    }

    [Fact]
    public void Instant_typing_disables_incremental_typing_and_fast_shortens_the_keys()
    {
        Assert.Contains("--disable-features=GlicActorIncrementalTyping",
            Agent.Args(new LaunchOptions { AgentLlmKey = "k", AgentTyping = AgentTyping.Instant }));
        Assert.Contains(
            "--enable-features=GlicActorIncrementalTyping:glic-actor-incremental-typing-key-down-duration/8ms/glic-actor-incremental-typing-key-up-duration/8ms",
            Agent.Args(new LaunchOptions { AgentLlmKey = "k", AgentTyping = AgentTyping.Fast }));
    }

    [Fact]
    public void Emits_no_typing_flag_when_the_agent_is_off()
        => Assert.Empty(Agent.Args(new LaunchOptions { AgentTyping = AgentTyping.Human }));

    [Fact]
    public void Maps_every_agent_option()
    {
        var args = Agent.Args(new LaunchOptions
        {
            AgentLlmUrl = "http://h/v1", AgentLlmKey = "k", AgentModel = "openai/gpt-4o-mini", AgentToolMode = "json",
        });
        foreach (var e in new[] { "--agent-llm-url=http://h/v1", "--agent-llm-key=k", "--agent-model=openai/gpt-4o-mini", "--agent-tool-mode=json" })
            Assert.Contains(e, args);
        Assert.Equal(5, args.Count);   // the four plus the typing cadence
    }

    // ── AgentKeys (agent.test.ts "splitAgentOptions") ────────────────────────

    [Fact]
    public void AgentKeys_lists_exactly_the_agent_options()
    {
        Assert.Equal(new HashSet<string> { "AgentLlmUrl", "AgentLlmKey", "AgentModel", "AgentToolMode", "AgentTyping" },
            Agent.AgentKeys.ToHashSet());
        var props = typeof(LaunchOptions).GetProperties().Select(p => p.Name).ToHashSet();
        Assert.All(Agent.AgentKeys, k => Assert.Contains(k, props));
        Assert.Equal(Agent.AgentKeys.Count, props.Count(p => p.StartsWith("Agent", StringComparison.Ordinal)));
    }

    [Fact]
    public void A_cloud_browser_refuses_the_agent_options_by_name()
    {
        // The agent drives the LOCAL engine's Actor framework (Python lists AGENT_KEYS as local-only too).
        foreach (var k in Agent.AgentKeys) Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == k);
        Assert.Equal("AgentLlmKey is not available for cloud browsers",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { AgentLlmKey = "k" })).Message);
    }

    // ── wiring: the switches reach the engine and merge with the others ─────

    [Fact]
    public void AssembleArgs_keeps_the_agent_switches_and_merges_the_typing_feature()
    {
        var o = new LaunchOptions { Fingerprint = "off", AgentLlmKey = "k", AgentModel = "m/x" };
        var args = Clearcote.AssembleArgs(Agent.WithPersona(o), new(), new(), null, null,
            new[] { "--enable-features=Foo" }, null, false);
        Assert.Contains($"--agent-llm-url={Agent.OpenRouterBaseUrl}", args);
        Assert.Contains("--agent-llm-key=k", args);
        Assert.Contains("--agent-model=m/x", args);
        // Chromium keeps only the last --enable-features: the typing feature is folded into the single one.
        var enable = Assert.Single(args, a => a.StartsWith("--enable-features=", StringComparison.Ordinal));
        Assert.Contains("GlicActorIncrementalTyping:glic-actor-incremental-typing-key-down-duration/45ms", enable);
        Assert.Contains("Foo", enable.Split('=', 2)[1].Split(','));
        // agent off: none of it
        Assert.DoesNotContain(Clearcote.AssembleArgs(Agent.WithPersona(new LaunchOptions { Fingerprint = "off" }), new(), new(), null, null,
            Array.Empty<string>(), null, false), a => a.Contains("agent", StringComparison.OrdinalIgnoreCase) || a.Contains("GlicActor", StringComparison.Ordinal));
    }

    /// A stand-in engine: writes its arguments next to itself, then exits.
    private string Standin()
    {
        var exe = Path.Combine(Dir("cc-agent-engine-"), "chrome");
        File.WriteAllText(exe, "#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\nexit 1\n");
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private async Task<List<string>> Launch(string entry, string exe, Action<LaunchOptions>? set = null)
    {
        LaunchOptions o = entry == "serve" ? new ServeOptions { ReadyTimeoutMs = 20000 } : new LaunchOptions();
        o.ExecutablePath = exe; o.Headless = true; o.Quiet = true; o.Timeout = 20000;
        set?.Invoke(o);
        var argvFile = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
        File.Delete(argvFile);
        var e = entry switch
        {
            "launch" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(o)),
            "persistent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-agent-udd-"), o)),
            "ephemeral" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(o)),
            "serve" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync((ServeOptions)o)),
            "agent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAgentAsync(o)),
            "agent-dir" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAgentAsync(o, Dir("cc-agent-udd-"))),
            _ => throw new ArgumentException(entry),
        };
        Assert.True(File.Exists(argvFile), $"the stand-in engine never ran: {e}");
        return File.ReadAllLines(argvFile).ToList();
    }

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("ephemeral")]
    [InlineData("serve")]
    [InlineData("agent")]
    [InlineData("agent-dir")]
    public async Task Every_launch_passes_the_agent_switches(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin();
        var argv = await Launch(entry, exe, o => { o.AgentLlmUrl = "http://127.0.0.1:9/v1"; o.AgentLlmKey = "test-key"; o.AgentToolMode = "json"; });
        Assert.Contains("--agent-llm-url=http://127.0.0.1:9/v1", argv);
        Assert.Contains("--agent-llm-key=test-key", argv);
        Assert.Contains("--agent-tool-mode=json", argv);
        Assert.Contains(argv, a => a.StartsWith("--enable-features=", StringComparison.Ordinal) && a.Contains("GlicActorIncrementalTyping:", StringComparison.Ordinal));
        // and nothing about the agent without the options
        var off = await Launch(entry, exe);
        Assert.DoesNotContain(off, a => a.StartsWith("--agent-", StringComparison.Ordinal) || a.Contains("GlicActor", StringComparison.Ordinal));
    }

    [Fact]
    public async Task LaunchAgentAsync_is_always_local_and_leaves_the_callers_options_alone()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin();
        LaunchOptions? given = null;
        // Cloud = true (and CLEARCOTE_CLOUD) would send any other launch to the hosted API; the agent runs here.
        _sb.Env("CLEARCOTE_CLOUD", "1");
        var argv = await Launch("agent", exe, o => { o.Cloud = true; o.AgentLlmKey = "k"; given = o; });
        Assert.Contains("--agent-llm-key=k", argv);
        Assert.True(given!.Cloud);
    }

    [Fact]
    public async Task LaunchAgentAsync_removes_its_throwaway_profile_when_the_launch_fails()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin();
        var argv = await Launch("agent", exe, o => o.AgentLlmKey = "k");
        var udd = argv.Single(a => a.StartsWith("--user-data-dir=", StringComparison.Ordinal))["--user-data-dir=".Length..];
        Assert.StartsWith("clearcote-agent-", Path.GetFileName(udd));
        Assert.False(Directory.Exists(udd));
        Assert.Empty(Directory.GetDirectories(_temp, "clearcote-agent-*"));
    }

    // ── Browser.agentRunTask: parameters and result ─────────────────────────

    [Fact]
    public void Task_parameters_carry_only_what_was_given()
    {
        Assert.Equal(new Dictionary<string, object> { ["targetId"] = "T1", ["goal"] = "do it" }, Agent.TaskParams("T1", "do it", null));
        Assert.Equal(new Dictionary<string, object> { ["targetId"] = "T1", ["goal"] = "g" }, Agent.TaskParams("T1", "g", new AgentTaskOptions()));
        Assert.Equal(
            new Dictionary<string, object> { ["targetId"] = "T", ["goal"] = "g", ["maxSteps"] = 8, ["model"] = "openai/gpt-4o-mini", ["planJson"] = "{\"a\":1}" },
            Agent.TaskParams("T", "g", new AgentTaskOptions { MaxSteps = 8, Model = "openai/gpt-4o-mini", PlanJson = "{\"a\":1}" }));
    }

    private static JsonElement J(string json) => JsonDocument.Parse(json).RootElement.Clone();

    [Fact]
    public void Parses_the_engine_answer()
    {
        var steps = """[{"action":"click","status":"ok","x":3},{"action":"type","status":"error"}]""";
        var r = Agent.ParseResult(J(JsonSerializer.Serialize(new { success = true, finalText = "done", stepsJson = steps })));
        Assert.True(r.Success);
        Assert.Equal("done", r.FinalText);
        Assert.Equal(steps, r.StepsJson);
        Assert.Equal(new[] { ("click", "ok"), ("type", "error") }, r.Steps.Select(s => (s.Action!, s.Status!)).ToArray());
        Assert.Equal(3, r.Steps[0].Raw.GetProperty("x").GetInt32());
    }

    [Fact]
    public void Parses_a_sparse_or_broken_answer_like_Node()
    {
        // missing fields: success false, finalText "", stepsJson "[]"
        var empty = Agent.ParseResult(J("{}"));
        Assert.Equal((false, "", "[]", 0), (empty.Success, empty.FinalText, empty.StepsJson, empty.Steps.Count));
        var none = Agent.ParseResult(null);
        Assert.Equal((false, "", "[]", 0), (none.Success, none.FinalText, none.StepsJson, none.Steps.Count));
        // unparseable stepsJson: steps stay empty, the raw string is kept
        var bad = Agent.ParseResult(J("""{"success":false,"finalText":"step limit","stepsJson":"[{oops"}"""));
        Assert.Equal(("step limit", "[{oops", 0), (bad.FinalText, bad.StepsJson, bad.Steps.Count));
        // an empty stepsJson parses as [] but is returned as given
        Assert.Equal(("", 0), (Agent.ParseResult(J("""{"stepsJson":""}""")).StepsJson, Agent.ParseResult(J("""{"stepsJson":""}""")).Steps.Count));
        // success is JavaScript truthiness (!!res.success)
        Assert.True(Agent.ParseResult(J("""{"success":1}""")).Success);
        Assert.False(Agent.ParseResult(J("""{"success":0}""")).Success);
        Assert.False(Agent.ParseResult(J("""{"success":""}""")).Success);
        // a step that is not an object is kept, without action/status
        var odd = Agent.ParseResult(J("""{"stepsJson":"[1,{\"action\":5}]"}"""));
        Assert.Equal(2, odd.Steps.Count);
        Assert.Null(odd.Steps[0].Action);
        Assert.Null(odd.Steps[1].Action);
        Assert.Equal(JsonValueKind.Number, odd.Steps[0].Raw.ValueKind);
    }

    [Fact]
    public async Task RunAgentTaskAsync_refuses_a_null_page()
        => await Assert.ThrowsAsync<ArgumentNullException>(() => Clearcote.RunAgentTaskAsync(null!, "g"));
}

/// <summary>
/// <see cref="Clearcote.RunAgentTaskAsync"/> against REAL browsers.
/// </summary>
/// <remarks>
/// A stock Chromium (CLEARCOTE_TEST_BINARY, else one Playwright has downloaded; see CloudLaunchLiveTests)
/// has no <c>Browser.agentRunTask</c>: the CDP plumbing runs for real up to the engine's refusal, which must
/// come back as the SDK's own error. With CLEARCOTE_LIVE_ENGINE naming a Clearcote build with the agent (add
/// CLEARCOTE_LICENSE_KEY for a PRO build), a task runs end to end against a fake OpenAI-compatible endpoint on
/// loopback: no real LLM, no API key. With neither, each test is a no-op (no Skippable* package, see
/// GeometryLiveTests).
/// </remarks>
public sealed class AgentLiveTests
{
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");

    [Fact]
    public async Task A_build_without_the_agent_is_named_in_the_error()
    {
        var exe = LocalChromium.Find();
        if (exe is null) return;
        await using var chromium = await LocalChromium.StartAsync(exe);
        using var pw = await Playwright.CreateAsync();
        var browser = await pw.Chromium.ConnectOverCDPAsync(chromium.WsUrl);
        try
        {
            var page = await browser.Contexts[0].NewPageAsync();
            var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.RunAgentTaskAsync(page, "click the button"));
            Assert.StartsWith("Browser.agentRunTask failed — make sure this is a Clearcote build with the AI agent", e.Message);
            Assert.Contains("AgentLlmKey/AgentLlmUrl", e.Message);
            Assert.Contains("Underlying error: ", e.Message);
            Assert.IsAssignableFrom<PlaywrightException>(e.InnerException);
        }
        finally
        {
            await browser.CloseAsync();
        }
    }

    [Fact]
    public async Task A_persistent_context_has_the_Browser_the_task_is_sent_through()
    {
        // LaunchAgentAsync returns a persistent context, which Microsoft.Playwright gives no Browser (Node's
        // playwright-core does): the task then goes over the page's own session, which serves the Browser domain.
        var exe = LocalChromium.Find();
        if (exe is null) return;
        var udd = TestTemp.Create("cc-agent-pc-");
        using var pw = await Playwright.CreateAsync();
        var ctx = await pw.Chromium.LaunchPersistentContextAsync(udd, new BrowserTypeLaunchPersistentContextOptions
        {
            ExecutablePath = exe, Headless = true, Args = new[] { "--no-sandbox" },
        });
        try
        {
            var page = ctx.Pages.FirstOrDefault() ?? await ctx.NewPageAsync();
            if (ctx.Browser is null)
            {
                // the content-layer Browser handler (where the engine implements agentRunTask) is on a page session
                var s = await ctx.NewCDPSessionAsync(page);
                var v = await s.SendAsync("Browser.getVersion");
                Assert.False(string.IsNullOrEmpty(v!.Value.GetProperty("product").GetString()));
            }
            var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.RunAgentTaskAsync(page, "g", new AgentTaskOptions { MaxSteps = 1 }));
            Assert.StartsWith("Browser.agentRunTask failed", e.Message);
        }
        finally
        {
            await ctx.CloseAsync();
            TestTemp.Remove(udd);
        }
    }

    [Fact]
    public async Task Runs_a_task_against_a_fake_OpenAI_compatible_endpoint()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        await using var llm = new FakeOpenRouter();
        var ctx = await Clearcote.LaunchAgentAsync(new LaunchOptions
        {
            ExecutablePath = LiveExe, Args = new[] { "--no-sandbox" }, Quiet = true,
            AgentLlmUrl = llm.BaseUrl, AgentLlmKey = "test-key", AgentModel = "test/launch-model",
        });
        try
        {
            var page = ctx.Pages.FirstOrDefault() ?? await ctx.NewPageAsync();
            await page.GotoAsync("data:text/html,<button onclick=\"document.title='clicked'\">Go</button>");   // SetContentAsync needs StockRuntime
            var r = await Clearcote.RunAgentTaskAsync(page, "Click the Go button.", new AgentTaskOptions { MaxSteps = 2, Model = "test/task-model" });
            Assert.NotNull(r.FinalText);
            Assert.NotNull(r.StepsJson);
            var calls = llm.Calls;
            Assert.NotEmpty(calls);
            Assert.All(calls, c =>
            {
                Assert.Equal("POST", c.Method);
                Assert.EndsWith("/chat/completions", c.Url.Split('?')[0]);
                Assert.Equal("Bearer test-key", c.Header("authorization"));
            });
            // the per-task model wins over the launch-time one
            Assert.Contains(calls, c => c.Body.Contains("\"test/task-model\"", StringComparison.Ordinal));
        }
        finally
        {
            await ctx.CloseAsync();
        }
    }
}
