using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

// In-browser AI agent (OpenRouter / any OpenAI-compatible endpoint). Mirrors sdk/node/src/agent.ts and
// sdk/python/clearcote/_agent.py.
//
//   var ctx = await Clearcote.LaunchAgentAsync(new LaunchOptions
//   {
//       AgentLlmKey = Environment.GetEnvironmentVariable("OPENROUTER_API_KEY"),   // turns the agent on
//       AgentModel = "openai/gpt-4o-mini",                                          // any OpenRouter model slug
//   });
//   var page = ctx.Pages.FirstOrDefault() ?? await ctx.NewPageAsync();
//   await page.GotoAsync("https://example.com");
//   var result = await Clearcote.RunAgentTaskAsync(page, "Click the 'More information...' link.", new() { MaxSteps = 8 });
//   Console.WriteLine($"{result.Success} {result.FinalText} {result.Steps.Count}");
//
// The agent runs *inside* the browser process: it perceives the live page, asks the LLM what to do, and
// drives real (trusted) input through Chrome's Actor framework. Setting AgentLlmKey (or AgentLlmUrl) is all
// that's needed — the engine auto-enables the Actor framework whenever --agent-llm-url is present, so no
// --enable-features/--disable-actor-safety-checks flags are required. Requires a Clearcote binary that
// exposes Browser.agentRunTask.
//
// IMPORTANT — the agent needs a REGULAR profile. Chrome's Actor framework only attaches to a normal
// (persistent) profile, not an incognito one, so drive the agent from LaunchAgentAsync,
// LaunchEphemeralProfileAsync or LaunchPersistentContextAsync. Plain LaunchAsync returns an incognito browser
// where the agent can't attach the tab ("Actor service unavailable" / "could not attach tab to task").

/// The agent's keystroke cadence (engine <c>GlicActorIncrementalTyping</c>, tuned via a runtime field-trial
/// flag — no rebuild). See <see cref="LaunchOptions.AgentTyping"/>.
public enum AgentTyping
{
    /// The default: a slower, less-uniform cadence that keeps typing long text key-by-key (the engine
    /// default fast-pastes text over ~200 chars, which emits zero keystroke events — a hard tell).
    Human,
    /// About the engine default (8 ms key down / 8 ms key up).
    Fast,
    /// Incremental typing off: one-shot, machine-perfect.
    Instant,
}

public partial class LaunchOptions
{
    // ── in-browser AI agent (see Agent) ──────────────────────────────────────

    /// Base URL of an OpenAI-compatible chat-completions API. Defaults to OpenRouter
    /// (<see cref="Agent.OpenRouterBaseUrl"/>) when <see cref="AgentLlmKey"/> is set. Point it at any
    /// OpenAI-compatible gateway (a local model server, Azure OpenAI, etc.) to use a different backend.
    /// Setting it alone (no key) also turns the agent on.
    public string? AgentLlmUrl { get; set; }
    /// API key (sent as a Bearer token) for the LLM endpoint — e.g. an OpenRouter key. Setting this turns
    /// the in-browser AI agent on (and makes the engine auto-enable Chrome's Actor framework).
    public string? AgentLlmKey { get; set; }
    /// Model slug, e.g. <c>"openai/gpt-4o-mini"</c>, <c>"anthropic/claude-3.5-sonnet"</c>,
    /// <c>"google/gemini-2.0-flash-001"</c> — any model your provider exposes (OpenRouter routes by
    /// <c>provider/model</c>). Override per task via <see cref="AgentTaskOptions.Model"/>.
    public string? AgentModel { get; set; }
    /// How the agent gets actions from the model: <c>"tools"</c> (OpenAI function-calling) or <c>"json"</c>
    /// (a plain JSON reply). Defaults to the engine default; use <c>"json"</c> for models without reliable
    /// tool-calling.
    public string? AgentToolMode { get; set; }
    /// The agent's keystroke cadence. Null = <see cref="global::Clearcote.AgentTyping.Human"/> while the agent
    /// is on; no effect while it is off.
    public AgentTyping? AgentTyping { get; set; }
}

/// Options for <see cref="Clearcote.RunAgentTaskAsync"/>.
public sealed class AgentTaskOptions
{
    /// Hard cap on observe→think→act iterations before the agent stops (default: engine default, ~20).
    public int? MaxSteps { get; set; }
    /// Override the launch-time <see cref="LaunchOptions.AgentModel"/> for just this task (any OpenRouter slug).
    public string? Model { get; set; }
    /// Optional JSON plan/hint string passed verbatim to the agent's planner.
    public string? PlanJson { get; set; }
}

/// One recorded step of the agent loop (shape mirrors the engine's step journal).
public sealed class AgentStep
{
    /// The step's <c>action</c> field, when it is a string.
    public string? Action { get; init; }
    /// The step's <c>status</c> field, when it is a string.
    public string? Status { get; init; }
    /// The step as the engine wrote it (every field, including <c>action</c> and <c>status</c>).
    public JsonElement Raw { get; init; }
}

/// The outcome of <see cref="Clearcote.RunAgentTaskAsync"/>.
public sealed class AgentTaskResult
{
    /// Whether the agent reported the goal as completed (vs. hitting the step limit or erroring).
    public bool Success { get; init; }
    /// The agent's final message (completion summary, or an error description).
    public string FinalText { get; init; } = "";
    /// Parsed per-step journal (clicks/types/scrolls/navigations and their outcomes).
    public IReadOnlyList<AgentStep> Steps { get; init; } = Array.Empty<AgentStep>();
    /// The raw <c>stepsJson</c> string as returned by the engine (in case <see cref="Steps"/> failed to parse).
    public string StepsJson { get; init; } = "[]";
}

/// The in-browser AI agent's engine switches and CDP plumbing. Entry points:
/// <see cref="Clearcote.LaunchAgentAsync"/> and <see cref="Clearcote.RunAgentTaskAsync"/>.
public static class Agent
{
    /// OpenAI-compatible chat-completions endpoint used by default. OpenRouter, so you can switch between any
    /// provider/model with a single <see cref="LaunchOptions.AgentModel"/> slug.
    public const string OpenRouterBaseUrl = "https://openrouter.ai/api/v1";

    /// The <see cref="LaunchOptions"/> properties that configure the agent.
    public static readonly IReadOnlyList<string> AgentKeys = new[]
    {
        nameof(LaunchOptions.AgentLlmUrl),
        nameof(LaunchOptions.AgentLlmKey),
        nameof(LaunchOptions.AgentModel),
        nameof(LaunchOptions.AgentToolMode),
        nameof(LaunchOptions.AgentTyping),
    };

    /// Map a typing speed to the GlicActorIncrementalTyping field-trial flag (no rebuild).
    private static string TypingFeature(AgentTyping speed) => speed switch
    {
        AgentTyping.Instant => "--disable-features=GlicActorIncrementalTyping",
        AgentTyping.Fast => "--enable-features=GlicActorIncrementalTyping:glic-actor-incremental-typing-key-down-duration/8ms/glic-actor-incremental-typing-key-up-duration/8ms",
        _ => "--enable-features=GlicActorIncrementalTyping:" +   // Human (default)
             "glic-actor-incremental-typing-key-down-duration/45ms/" +
             "glic-actor-incremental-typing-key-up-duration/60ms/" +
             "glic-actor-incremental-typing-long-multiplier/0.7/" +
             "glic-actor-incremental-typing-long-text-threshold/80/" +
             "glic-actor-long-text-paste-threshold/100000",
    };

    /// Build the Chromium switches for the AI agent. Returns an empty list (agent off) unless
    /// <see cref="LaunchOptions.AgentLlmKey"/> or <see cref="LaunchOptions.AgentLlmUrl"/> is set.
    public static List<string> Args(LaunchOptions o)
    {
        var args = new List<string>();
        if (o.AgentLlmKey is null && o.AgentLlmUrl is null) return args;
        args.Add($"--agent-llm-url={o.AgentLlmUrl ?? OpenRouterBaseUrl}");
        if (o.AgentLlmKey is not null) args.Add($"--agent-llm-key={o.AgentLlmKey}");
        if (o.AgentModel is not null) args.Add($"--agent-model={o.AgentModel}");
        if (o.AgentToolMode is not null) args.Add($"--agent-tool-mode={o.AgentToolMode}");
        // default the agent's typing to the stealthier Human cadence (free; beats the fast engine default)
        args.Add(TypingFeature(o.AgentTyping ?? AgentTyping.Human));
        return args;
    }

    /// The persona switches followed by the agent's: the first argument of every launch's AssembleArgs.
    internal static List<string> WithPersona(LaunchOptions o)
    {
        var args = Fingerprint.Args(o);
        args.AddRange(Args(o));
        return args;
    }

    /// The <c>Browser.agentRunTask</c> parameters for a task.
    internal static Dictionary<string, object> TaskParams(string targetId, string goal, AgentTaskOptions? opts)
    {
        var p = new Dictionary<string, object> { ["targetId"] = targetId, ["goal"] = goal };
        if (opts?.MaxSteps is int maxSteps) p["maxSteps"] = maxSteps;
        if (opts?.Model is not null) p["model"] = opts.Model;
        if (opts?.PlanJson is not null) p["planJson"] = opts.PlanJson;
        return p;
    }

    /// The engine's <c>Browser.agentRunTask</c> answer as an <see cref="AgentTaskResult"/>.
    internal static AgentTaskResult ParseResult(JsonElement? res)
    {
        var r = res is { ValueKind: JsonValueKind.Object } o ? o : default;
        JsonElement Field(string name)
            => r.ValueKind == JsonValueKind.Object && r.TryGetProperty(name, out var v) ? v : default;
        var stepsJsonEl = Field("stepsJson");
        var finalEl = Field("finalText");
        // stepsJson ?? "[]" for the raw string; stepsJson || "[]" for the parse (an empty string parses as []).
        var stepsJson = stepsJsonEl.ValueKind switch
        {
            JsonValueKind.Undefined or JsonValueKind.Null => "[]",
            JsonValueKind.String => stepsJsonEl.GetString()!,
            _ => stepsJsonEl.GetRawText(),
        };
        var steps = new List<AgentStep>();
        try
        {
            using var doc = JsonDocument.Parse(stepsJson.Length == 0 ? "[]" : stepsJson);
            if (doc.RootElement.ValueKind == JsonValueKind.Array)
                foreach (var s in doc.RootElement.EnumerateArray())
                    steps.Add(new AgentStep { Action = Str(s, "action"), Status = Str(s, "status"), Raw = s.Clone() });
        }
        catch (JsonException) { /* leave steps empty */ }
        return new AgentTaskResult
        {
            Success = Truthy(Field("success")),
            FinalText = finalEl.ValueKind switch
            {
                JsonValueKind.Undefined or JsonValueKind.Null => "",
                JsonValueKind.String => finalEl.GetString()!,
                _ => finalEl.GetRawText(),
            },
            Steps = steps,
            StepsJson = stepsJson,
        };
    }

    private static string? Str(JsonElement e, string name)
        => e.ValueKind == JsonValueKind.Object && e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() : null;

    /// JavaScript truthiness (<c>!!value</c>) of a JSON value.
    private static bool Truthy(JsonElement e) => e.ValueKind switch
    {
        JsonValueKind.True or JsonValueKind.Object or JsonValueKind.Array => true,
        JsonValueKind.Number => e.GetDouble() != 0,
        JsonValueKind.String => e.GetString()!.Length > 0,
        _ => false,
    };

    internal static async Task<AgentTaskResult> RunTaskAsync(IPage page, string goal, AgentTaskOptions? opts)
    {
        ArgumentNullException.ThrowIfNull(page);
        var pageSession = await page.Context.NewCDPSessionAsync(page).ConfigureAwait(false);
        var info = await pageSession.SendAsync("Target.getTargetInfo").ConfigureAwait(false);
        var targetId = info!.Value.GetProperty("targetInfo").GetProperty("targetId").GetString()!;
        // Sent on a browser session, as Node and Python do. Microsoft.Playwright gives a PERSISTENT context (the
        // one LaunchAgentAsync returns) no Browser, though, so there the page's own session carries it: the
        // engine serves the Browser domain on a page target's session too and finds the tab by targetId.
        var session = page.Context.Browser is { } browser
            ? await browser.NewBrowserCDPSessionAsync().ConfigureAwait(false)
            : pageSession;
        var p = TaskParams(targetId, goal, opts);
        JsonElement? res;
        try
        {
            res = await session.SendAsync("Browser.agentRunTask", p).ConfigureAwait(false);
        }
        catch (Exception e)
        {
            throw new InvalidOperationException(
                "Browser.agentRunTask failed — make sure this is a Clearcote build with the AI agent and that " +
                "the browser was launched with AgentLlmKey/AgentLlmUrl set. Underlying error: " + e.Message, e);
        }
        return ParseResult(res);
    }
}

public static partial class Clearcote
{
    /// <summary>
    /// Launch Clearcote ready for the in-browser AI agent and return a Playwright <see cref="IBrowserContext"/>.
    /// </summary>
    /// <remarks>
    /// The agent drives Chrome's Actor framework, which only attaches to a <b>regular profile</b> — not
    /// incognito — so this uses a <i>persistent</i> context: a fresh temp profile directory, deleted when the
    /// context closes, unless you pass <paramref name="userDataDir"/> to keep. Set
    /// <see cref="LaunchOptions.AgentLlmKey"/> (+ optional <see cref="LaunchOptions.AgentModel"/>), then drive a
    /// page with <see cref="RunAgentTaskAsync"/>. Always a local browser: the agent drives the LOCAL engine's
    /// Actor framework, so <see cref="LaunchOptions.Cloud"/> and CLEARCOTE_CLOUD are ignored here. Use this (or
    /// <see cref="LaunchPersistentContextAsync(string, LaunchOptions?)"/>) for the agent — plain
    /// <see cref="LaunchAsync"/> is incognito, where the Actor framework can't attach the tab.
    /// <code>
    /// var ctx = await Clearcote.LaunchAgentAsync(new() { AgentLlmKey = key, AgentModel = "openai/gpt-4o-mini" });
    /// var page = ctx.Pages.FirstOrDefault() ?? await ctx.NewPageAsync();
    /// await page.GotoAsync("https://example.com");
    /// var result = await Clearcote.RunAgentTaskAsync(page, "Click the 'More information...' link.");
    /// </code>
    /// </remarks>
    /// <param name="options">Launch options; the agent options turn the agent on.</param>
    /// <param name="userDataDir">Profile directory to persist (cookies/storage/logins). Default: a fresh temp
    /// directory that is deleted when the context closes.</param>
    public static async Task<IBrowserContext> LaunchAgentAsync(LaunchOptions? options = null, string? userDataDir = null)
    {
        // the agent drives the LOCAL engine's Actor framework: never a cloud browser
        var local = (LaunchOptions)(options ?? new LaunchOptions()).Clone();
        local.Cloud = false;
        if (userDataDir is not null) return await LaunchPersistentContextAsync(userDataDir, local).ConfigureAwait(false);
        var dir = Path.Combine(Path.GetTempPath(), "clearcote-agent-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        IBrowserContext context;
        try { context = await LaunchPersistentContextAsync(dir, local).ConfigureAwait(false); }
        catch
        {
            // A launch that fails must not leak the throwaway profile dir.
            new ThrowawayProfile(dir).Remove();
            throw;
        }
        // Deleted once the browser has exited, never under it: see ThrowawayProfile.
        new ThrowawayProfile(dir).Attach(context);
        return context;
    }

    /// <summary>
    /// Run an autonomous AI-agent task against <paramref name="page"/>: the engine perceives the page, asks the
    /// LLM (OpenRouter by default) what to do, and executes the actions until the goal is met or
    /// <see cref="AgentTaskOptions.MaxSteps"/> is reached. The browser must have been launched with
    /// <see cref="LaunchOptions.AgentLlmKey"/> (and optionally <see cref="LaunchOptions.AgentModel"/>), on a
    /// regular profile (see <see cref="LaunchAgentAsync"/>).
    /// </summary>
    /// <exception cref="InvalidOperationException">The engine refused
    /// <c>Browser.agentRunTask</c> (not a Clearcote build with the agent, or launched without the agent options).</exception>
    public static Task<AgentTaskResult> RunAgentTaskAsync(IPage page, string goal, AgentTaskOptions? options = null)
        => Agent.RunTaskAsync(page, goal, options);
}
