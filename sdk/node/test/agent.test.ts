import { describe, it, expect } from "vitest";
import { agentArgs, splitAgentOptions, AGENT_KEYS, OPENROUTER_BASE_URL } from "../src/agent.js";

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
