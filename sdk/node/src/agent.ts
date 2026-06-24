// In-browser AI agent (OpenRouter / any OpenAI-compatible endpoint).
//
//   import { launchAgent, runAgentTask } from "clearcote";
//   const ctx = await launchAgent({
//     agentLlmKey: process.env.OPENROUTER_API_KEY,   // turns the agent on
//     agentModel: "openai/gpt-4o-mini",              // any OpenRouter model slug
//   });
//   const page = ctx.pages()[0] ?? (await ctx.newPage());
//   await page.goto("https://example.com");
//   const result = await runAgentTask(page, "Click the 'More information...' link.", { maxSteps: 8 });
//   console.log(result.success, result.finalText, result.steps);
//
// The agent runs *inside* the browser process: it perceives the live page, asks the LLM what to do,
// and drives real (trusted) input through Chrome's Actor framework. Setting `agentLlmKey` (or
// `agentLlmUrl`) is all that's needed — the engine auto-enables the Actor framework whenever
// `--agent-llm-url` is present, so no `--enable-features`/`--disable-actor-safety-checks` flags are
// required. Requires a Clearcote binary that exposes `Browser.agentRunTask`.
//
// IMPORTANT — the agent needs a REGULAR profile. Chrome's Actor framework only attaches to a normal
// (persistent) profile, not an incognito one, so drive the agent from `launchAgent()` or
// `launchPersistentContext()`. Plain `launch()` returns an incognito browser where the agent can't
// attach the tab ("Actor service unavailable" / "could not attach tab to task").

import type { Page } from "playwright-core";

/** OpenAI-compatible chat-completions endpoints. OpenRouter is the default so you can switch between
 * any provider/model with a single `agentModel` slug. */
export const OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1";

export interface AgentOptions {
  /**
   * Base URL of an OpenAI-compatible chat-completions API. Defaults to OpenRouter
   * (`https://openrouter.ai/api/v1`) when `agentLlmKey` is set. Point it at any OpenAI-compatible
   * gateway (a local model server, Azure OpenAI, etc.) to use a different backend.
   */
  agentLlmUrl?: string;
  /**
   * API key (sent as a Bearer token) for the LLM endpoint — e.g. an OpenRouter key. **Setting this
   * turns the in-browser AI agent on** (and makes the engine auto-enable Chrome's Actor framework).
   */
  agentLlmKey?: string;
  /**
   * Model slug, e.g. `"openai/gpt-4o-mini"`, `"anthropic/claude-3.5-sonnet"`, `"google/gemini-2.0-flash-001"`
   * — any model your provider exposes (OpenRouter routes by `provider/model`). Switching models =
   * switching this one value. Override per task via `runAgentTask(page, goal, { model })`.
   */
  agentModel?: string;
  /**
   * How the agent gets actions from the model: `"tools"` (OpenAI function-calling) or `"json"`
   * (a plain JSON reply). Defaults to the engine default; use `"json"` for models without reliable
   * tool-calling.
   */
  agentToolMode?: "tools" | "json" | (string & {});
  /**
   * The agent's keystroke cadence (engine `GlicActorIncrementalTyping`, tuned via a runtime
   * field-trial flag — no rebuild). `"human"` (default) types at a slower, less-uniform cadence and
   * keeps typing long text key-by-key (the engine default fast-pastes text over ~200 chars, which
   * emits zero keystroke events — a hard tell). `"fast"` ~ engine default; `"instant"` disables
   * incremental typing (one-shot, machine-perfect).
   */
  agentTyping?: "human" | "fast" | "instant";
}

export const AGENT_KEYS: (keyof AgentOptions)[] = [
  "agentLlmUrl",
  "agentLlmKey",
  "agentModel",
  "agentToolMode",
  "agentTyping",
];

/** Map a typing speed to the GlicActorIncrementalTyping field-trial flag (no rebuild). */
function typingFeature(speed?: string): string {
  if (speed === "instant") return "--disable-features=GlicActorIncrementalTyping";
  if (speed === "fast") {
    return "--enable-features=GlicActorIncrementalTyping:glic-actor-incremental-typing-key-down-duration/8ms/glic-actor-incremental-typing-key-up-duration/8ms";
  }
  return (
    "--enable-features=GlicActorIncrementalTyping:" + // "human" (default)
    "glic-actor-incremental-typing-key-down-duration/45ms/" +
    "glic-actor-incremental-typing-key-up-duration/60ms/" +
    "glic-actor-incremental-typing-long-multiplier/0.7/" +
    "glic-actor-incremental-typing-long-text-threshold/80/" +
    "glic-actor-long-text-paste-threshold/100000"
  );
}

/** Split an options object into its agent half and the remaining (Playwright/other) half. */
export function splitAgentOptions<T extends AgentOptions>(
  options: T
): { agent: AgentOptions; rest: Omit<T, keyof AgentOptions> } {
  const agent: AgentOptions = {};
  const rest: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(options)) {
    if ((AGENT_KEYS as string[]).includes(k)) {
      (agent as Record<string, unknown>)[k] = v;
    } else {
      rest[k] = v;
    }
  }
  return { agent, rest: rest as Omit<T, keyof AgentOptions> };
}

/** Build the Chromium switches for the AI agent. Returns `[]` (agent off) unless a key or URL is set. */
export function agentArgs(o: AgentOptions): string[] {
  const args: string[] = [];
  if (o.agentLlmKey == null && o.agentLlmUrl == null) return args;
  args.push(`--agent-llm-url=${o.agentLlmUrl ?? OPENROUTER_BASE_URL}`);
  if (o.agentLlmKey != null) args.push(`--agent-llm-key=${o.agentLlmKey}`);
  if (o.agentModel != null) args.push(`--agent-model=${o.agentModel}`);
  if (o.agentToolMode != null) args.push(`--agent-tool-mode=${o.agentToolMode}`);
  // default the agent's typing to the stealthier 'human' cadence (free; beats the fast engine default)
  args.push(typingFeature(o.agentTyping ?? "human"));
  return args;
}

export interface AgentTaskOptions {
  /** Hard cap on observe→think→act iterations before the agent stops (default: engine default, ~20). */
  maxSteps?: number;
  /** Override the launch-time `agentModel` for just this task (any OpenRouter slug). */
  model?: string;
  /** Optional JSON plan/hint string passed verbatim to the agent's planner. */
  planJson?: string;
}

/** One recorded step of the agent loop (shape mirrors the engine's step journal). */
export interface AgentStep {
  action?: string;
  status?: string;
  [k: string]: unknown;
}

export interface AgentTaskResult {
  /** Whether the agent reported the goal as completed (vs. hitting the step limit or erroring). */
  success: boolean;
  /** The agent's final message (completion summary, or an error description). */
  finalText: string;
  /** Parsed per-step journal (clicks/types/scrolls/navigations and their outcomes). */
  steps: AgentStep[];
  /** The raw `stepsJson` string as returned by the engine (in case `steps` failed to parse). */
  stepsJson: string;
}

async function pageTargetId(page: Page): Promise<string> {
  const s = await page.context().newCDPSession(page);
  const info: any = await s.send("Target.getTargetInfo");
  return info.targetInfo.targetId;
}

/**
 * Run an autonomous AI-agent task against `page`: the engine perceives the page, asks the LLM
 * (OpenRouter by default) what to do, and executes the actions until the goal is met or `maxSteps`
 * is reached. The browser must have been launched with `agentLlmKey` (and optionally `agentModel`).
 */
export async function runAgentTask(
  page: Page,
  goal: string,
  opts: AgentTaskOptions = {}
): Promise<AgentTaskResult> {
  const browser = page.context().browser();
  if (!browser) throw new Error("runAgentTask: page is not attached to a Browser");
  const session = await browser.newBrowserCDPSession();
  const params: Record<string, unknown> = { targetId: await pageTargetId(page), goal };
  if (opts.maxSteps != null) params.maxSteps = opts.maxSteps;
  if (opts.model != null) params.model = opts.model;
  if (opts.planJson != null) params.planJson = opts.planJson;
  let res: any;
  try {
    res = await session.send("Browser.agentRunTask" as any, params as any);
  } catch (e) {
    throw new Error(
      "Browser.agentRunTask failed — make sure this is a Clearcote build with the AI agent and that " +
        "the browser was launched with agentLlmKey/agentLlmUrl set. Underlying error: " +
        (e as Error).message
    );
  }
  let steps: AgentStep[] = [];
  try {
    steps = JSON.parse(res.stepsJson || "[]");
  } catch {
    /* leave steps = [] */
  }
  return {
    success: !!res.success,
    finalText: res.finalText ?? "",
    steps,
    stepsJson: res.stepsJson ?? "[]",
  };
}
