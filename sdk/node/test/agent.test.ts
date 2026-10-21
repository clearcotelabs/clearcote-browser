import { describe, it, expect } from "vitest";
import { agentArgs, splitAgentOptions, runAgentTask, AGENT_KEYS, OPENROUTER_BASE_URL } from "../src/agent.js";

describe("agentArgs", () => {
  it("is off unless a key or url is set", () => {
    expect(agentArgs({})).toEqual([]);
    expect(agentArgs({ agentModel: "openai/gpt-4o-mini" })).toEqual([]);
  });

  it("a key enables it and defaults the url to OpenRouter", () => {
    const args = agentArgs({ agentLlmKey: "sk-or-1" });
    expect(args).toContain(`--agent-llm-url=${OPENROUTER_BASE_URL}`);
    expect(args).toContain("--agent-llm-key=sk-or-1");
  });

  it("a url alone enables it without a key", () => {
    expect(agentArgs({ agentLlmUrl: "http://localhost:1234/v1" })).toContain(
      "--agent-llm-url=http://localhost:1234/v1"
    );
  });

  it("defaults typing to the 'human' cadence when the agent is on", () => {
    const args = agentArgs({ agentLlmKey: "k" });
    expect(args.some((a) => a.startsWith("--enable-features=GlicActorIncrementalTyping:"))).toBe(true);
    expect(args.some((a) => a.includes("long-text-paste-threshold/100000"))).toBe(true);
  });

  it("agentTyping:'instant' disables incremental typing", () => {
    expect(agentArgs({ agentLlmKey: "k", agentTyping: "instant" })).toContain(
      "--disable-features=GlicActorIncrementalTyping"
    );
  });

  it("emits no typing flag when the agent is off", () => {
    expect(agentArgs({ agentTyping: "human" })).toEqual([]);
  });

  it("maps every agent option", () => {
    const args = agentArgs({
      agentLlmUrl: "http://h/v1",
      agentLlmKey: "k",
      agentModel: "openai/gpt-4o-mini",
      agentToolMode: "json",
    });
    for (const e of [
      "--agent-llm-url=http://h/v1",
      "--agent-llm-key=k",
      "--agent-model=openai/gpt-4o-mini",
      "--agent-tool-mode=json",
    ]) {
      expect(args).toContain(e);
    }
  });
});

describe("splitAgentOptions", () => {
  it("separates agent keys from the rest", () => {
    const { agent, rest } = splitAgentOptions({ agentLlmKey: "k", headless: true } as never);
    expect(agent).toEqual({ agentLlmKey: "k" });
    expect(rest).toEqual({ headless: true });
  });

  it("AGENT_KEYS lists exactly the agent options", () => {
    expect(new Set(AGENT_KEYS)).toEqual(
      new Set(["agentLlmUrl", "agentLlmKey", "agentModel", "agentToolMode", "agentTyping"])
    );
  });
});

describe("runAgentTask", () => {
  function fakePage(withBrowser: boolean) {
    const log: Array<[string, string, unknown]> = [];
    const session = (name: string) => ({
      async send(method: string, params?: unknown) {
        log.push([name, method, params]);
        if (method === "Target.getTargetInfo") return { targetInfo: { targetId: "T1" } };
        return { success: true, finalText: "done", stepsJson: '[{"action":"click"}]' };
      },
    });
    const browser = { newBrowserCDPSession: async () => session("browser") };
    // Older Playwright clients: a persistent context has no Browser.
    const context = { browser: () => (withBrowser ? browser : null), newCDPSession: async () => session("page") };
    return { page: { context: () => context } as never, log };
  }

  it("sends Browser.agentRunTask on a browser session when there is one", async () => {
    const { page, log } = fakePage(true);
    const r = await runAgentTask(page, "goal", { maxSteps: 3 });
    expect(log.at(-1)).toEqual(["browser", "Browser.agentRunTask", { targetId: "T1", goal: "goal", maxSteps: 3 }]);
    expect(r).toEqual({ success: true, finalText: "done", steps: [{ action: "click" }], stepsJson: '[{"action":"click"}]' });
  });

  it("falls back to the page's own session when context.browser() is null", async () => {
    const { page, log } = fakePage(false);
    const r = await runAgentTask(page, "goal", { model: "m" });
    expect(log).toEqual([
      ["page", "Target.getTargetInfo", undefined],
      ["page", "Browser.agentRunTask", { targetId: "T1", goal: "goal", model: "m" }],
    ]);
    expect(r.success).toBe(true);
  });
});
