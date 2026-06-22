#!/usr/bin/env node
// clearcote-agent — a terminal wrapper around Clearcote's in-browser AI agent.
//
// Launches a REGULAR-profile Clearcote (anti-detect Chromium), points it at OpenRouter, and runs
// natural-language goals on a page via the SDK. The agent perceives the page, asks an LLM, and acts
// via Chrome's Actor framework.
//
//   clearcote-agent --goal "Click the 'More information...' link" --url https://example.com
//   clearcote-agent -i --url https://example.com           # interactive REPL
//
// The agent only works in a regular/persistent profile, so this always uses launchAgent() (never the
// incognito launch()). Logins/cookies persist in --profile so you can sign in once and reuse it.

import { parseArgs, type ParseArgsConfig } from "node:util";
import * as readline from "node:readline";
import { mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import type { BrowserContext, Page } from "playwright-core";
import { launchAgent, runAgentTask, type AgentTaskResult } from "./index.js";

/** Playwright proxy descriptor (creds split out of the URL). */
interface ProxyConfig {
  server: string;
  username?: string;
  password?: string;
}

const USAGE = `clearcote-agent — run natural-language goals on a page via Clearcote's in-browser AI agent.

USAGE
  clearcote-agent --goal <text> [--url <url>] [options]      one-shot
  clearcote-agent -i|--interactive [--url <url>] [options]   interactive REPL

OPTIONS
  --goal <text>        the task (required in one-shot; optional in REPL)
  --url <url>          starting page (default: about:blank)
  -i, --interactive    REPL mode (always headed + persistent)
  --model <slug>       OpenRouter provider/model slug (default: openai/gpt-4o)
  --key <k>            OpenRouter API key (env OPENROUTER_API_KEY or CLEARCOTE_AGENT_KEY)
  --llm-url <url>      OpenAI-compatible base URL (default: OpenRouter)
  --tool-mode <m>      "tools" | "json"
  --max-steps <n>      max agent steps (default: 20)
  --profile <dir>      persistent profile dir (default: ~/.clearcote/agent-profile)
  --headless           run headless (default: headed; ignored in REPL)
  --executable <path>  path to Clearcote chrome.exe (else auto-download / CLEARCOTE_BINARY)
  --fingerprint <seed> anti-detect fingerprint seed
  --proxy <url>        proxy, e.g. http://user:pass@host:port
  --timezone <tz>      IANA timezone, e.g. America/New_York
  --json               one-shot only: print the raw result object as JSON (logs to stderr)
  -h, --help           show this help

REPL COMMANDS
  :goto <url>   navigate the current page
  :url          print the current URL
  :model <slug> change the model for subsequent goals
  :steps <n>    change max-steps
  :help         list commands
  :quit, :exit  close and exit (Ctrl-D also quits)
  <anything>    run as a goal against the current page`;

/** Friendly, expected-condition error — no stack trace, custom exit code. */
class UsageError extends Error {
  constructor(message: string, readonly code: number = 2) {
    super(message);
    this.name = "UsageError";
  }
}

/** Log to stderr (results go to stdout). */
function log(msg: string): void {
  process.stderr.write(msg + "\n");
}

/** Parse a proxy URL into a Playwright proxy descriptor, splitting credentials out of the userinfo. */
function parseProxy(raw: string): ProxyConfig {
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    throw new UsageError(`Invalid --proxy URL: ${raw}`);
  }
  const proxy: ProxyConfig = { server: `${u.protocol}//${u.host}` };
  if (u.username) proxy.username = decodeURIComponent(u.username);
  if (u.password) proxy.password = decodeURIComponent(u.password);
  return proxy;
}

/** Validate a starting URL (about:blank or anything URL-parseable). A bare host like "example.com"
 * is upgraded to https:// so users don't have to type the scheme. Returns the normalized URL. */
function validateUrl(raw: string): string {
  if (raw === "about:blank") return raw;
  try {
    new URL(raw);
    return raw;
  } catch {
    try {
      const upgraded = "https://" + raw;
      new URL(upgraded);
      return upgraded;
    } catch {
      throw new UsageError(`Invalid --url: ${raw}`);
    }
  }
}

/** Parse a positive-integer step count. */
function parseMaxSteps(raw: string): number {
  const n = Number(raw);
  if (!Number.isInteger(n) || n <= 0) {
    throw new UsageError(`Invalid --max-steps (expected a positive integer): ${raw}`);
  }
  return n;
}

/** Render a result to stdout in human form (header already printed by caller). */
function printResult(result: AgentTaskResult, currentUrl: string): void {
  process.stdout.write((result.success ? "OK" : "FAIL") + "\n");
  if (result.finalText) process.stdout.write(result.finalText + "\n");
  if (result.steps.length) {
    process.stdout.write("\nsteps:\n");
    result.steps.forEach((step, i) => {
      const action = step.action ?? "(step)";
      const status = step.status != null ? ` [${step.status}]` : "";
      const fields: string[] = [];
      for (const [k, v] of Object.entries(step)) {
        if (k === "action" || k === "status") continue;
        if (v == null) continue;
        const s = typeof v === "string" ? v : JSON.stringify(v);
        fields.push(`${k}=${s.length > 80 ? s.slice(0, 77) + "..." : s}`);
      }
      const detail = fields.length ? ` ${fields.join(" ")}` : "";
      process.stdout.write(`  ${i + 1}. ${action}${detail}${status}\n`);
    });
  }
  process.stdout.write(`\nurl: ${currentUrl}\n`);
}

interface Cli {
  goal?: string;
  url: string;
  interactive: boolean;
  model: string;
  key: string;
  llmUrl?: string;
  toolMode?: string;
  maxSteps: number;
  profile: string;
  headless: boolean;
  executable?: string;
  fingerprint?: string;
  proxy?: ProxyConfig;
  timezone?: string;
  json: boolean;
}

/** Parse + validate process argv into a normalized Cli config. Throws UsageError on bad input. */
function parseCli(argv: string[]): { cli: Cli; help: boolean } {
  const config: ParseArgsConfig = {
    args: argv,
    allowPositionals: false,
    strict: true,
    options: {
      goal: { type: "string" },
      url: { type: "string" },
      interactive: { type: "boolean", short: "i" },
      model: { type: "string" },
      key: { type: "string" },
      "llm-url": { type: "string" },
      "tool-mode": { type: "string" },
      "max-steps": { type: "string" },
      profile: { type: "string" },
      headless: { type: "boolean" },
      executable: { type: "string" },
      fingerprint: { type: "string" },
      proxy: { type: "string" },
      timezone: { type: "string" },
      json: { type: "boolean" },
      help: { type: "boolean", short: "h" },
    },
  };

  let values: Record<string, unknown>;
  try {
    ({ values } = parseArgs(config) as { values: Record<string, unknown> });
  } catch (e) {
    throw new UsageError((e as Error).message);
  }

  if (values.help) return { cli: {} as Cli, help: true };

  const key =
    (values.key as string | undefined) ??
    process.env.OPENROUTER_API_KEY ??
    process.env.CLEARCOTE_AGENT_KEY;
  if (!key) {
    throw new UsageError(
      "No OpenRouter API key. Pass --key <k>, or set OPENROUTER_API_KEY or CLEARCOTE_AGENT_KEY."
    );
  }

  const interactive = !!values.interactive;
  const goal = values.goal as string | undefined;
  if (!interactive && (goal == null || goal.trim() === "")) {
    throw new UsageError("Missing --goal. Provide a task with --goal <text>, or use -i for the REPL.");
  }

  const url = validateUrl((values.url as string | undefined) ?? "about:blank");
  const maxSteps = values["max-steps"] != null ? parseMaxSteps(values["max-steps"] as string) : 20;
  const proxy = values.proxy != null ? parseProxy(values.proxy as string) : undefined;

  let headless = !!values.headless;
  if (interactive && headless) {
    log("warning: --headless is ignored in interactive mode (REPL is always headed).");
    headless = false;
  }

  const profile =
    (values.profile as string | undefined) ?? join(homedir(), ".clearcote", "agent-profile");

  return {
    cli: {
      goal,
      url,
      interactive,
      model: (values.model as string | undefined) ?? "openai/gpt-4o",
      key,
      llmUrl: values["llm-url"] as string | undefined,
      toolMode: values["tool-mode"] as string | undefined,
      maxSteps,
      profile,
      headless,
      executable: values.executable as string | undefined,
      fingerprint: values.fingerprint as string | undefined,
      proxy,
      timezone: values.timezone as string | undefined,
      json: !!values.json,
    },
    help: false,
  };
}

/** Build the launchAgent options from the parsed config. */
function launchOptions(cli: Cli) {
  mkdirSync(cli.profile, { recursive: true });
  return {
    userDataDir: cli.profile,
    agentLlmKey: cli.key,
    agentLlmUrl: cli.llmUrl,
    agentModel: cli.model,
    agentToolMode: cli.toolMode,
    headless: cli.headless,
    proxy: cli.proxy,
    fingerprint: cli.fingerprint,
    timezone: cli.timezone,
    executablePath: cli.executable,
  };
}

/** Resolve the page to drive (reuse the first open page, else create one). */
async function currentPage(ctx: BrowserContext): Promise<Page> {
  return ctx.pages()[0] ?? (await ctx.newPage());
}

/** One-shot: launch -> goto -> run goal -> print -> close. Returns the process exit code. */
async function runOneShot(cli: Cli): Promise<number> {
  log(`launching Clearcote (profile: ${cli.profile})...`);
  const ctx = await launchAgent(launchOptions(cli));
  try {
    const page = await currentPage(ctx);
    if (cli.url !== "about:blank") {
      log(`navigating to ${cli.url}...`);
      await page.goto(cli.url);
    }
    if (!cli.json) process.stdout.write(`goal: ${cli.goal}\n`);
    log(`running (${cli.model}, up to ${cli.maxSteps} steps)...`);
    const result = await runAgentTask(page, cli.goal as string, {
      model: cli.model,
      maxSteps: cli.maxSteps,
    });
    const finalUrl = page.url();
    if (cli.json) {
      process.stdout.write(JSON.stringify({ ...result, url: finalUrl }, null, 2) + "\n");
    } else {
      printResult(result, finalUrl);
    }
    return result.success ? 0 : 1;
  } finally {
    await ctx.close();
  }
}

/** Interactive REPL: persistent headed session; each non-':' line is a goal on the current page. */
async function runRepl(cli: Cli): Promise<number> {
  log(`launching Clearcote (profile: ${cli.profile})...`);
  const ctx = await launchAgent(launchOptions(cli));
  let page = await currentPage(ctx);
  if (cli.url !== "about:blank") {
    log(`navigating to ${cli.url}...`);
    await page.goto(cli.url);
  }

  let model = cli.model;
  let maxSteps = cli.maxSteps;

  log("interactive mode. Type a goal, or :help for commands. Ctrl-D to quit.");
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout, prompt: "agent> " });

  const replHelp = [
    "commands:",
    "  :goto <url>   navigate the current page",
    "  :url          print the current URL",
    "  :model <slug> change the model for subsequent goals",
    "  :steps <n>    change max-steps",
    "  :help         this help",
    "  :quit, :exit  close and exit",
    "  <anything>    run as a goal against the current page",
  ].join("\n");

  return await new Promise<number>((resolve) => {
    let closing = false;
    const finish = () => {
      if (closing) return;
      closing = true;
      rl.close();
      ctx
        .close()
        .catch((e) => log(`warning: closing browser failed: ${(e as Error).message}`))
        .finally(() => resolve(0));
    };

    // Process one input line (a ':' command or a goal); resolves when it fully settles.
    const processLine = async (line: string): Promise<void> => {
      if (line.startsWith(":")) {
        const sp = line.indexOf(" ");
        const cmd = (sp === -1 ? line : line.slice(0, sp)).toLowerCase();
        const arg = sp === -1 ? "" : line.slice(sp + 1).trim();
        switch (cmd) {
          case ":quit":
          case ":exit":
            finish();
            return;
          case ":help":
            log(replHelp);
            break;
          case ":url":
            process.stdout.write(page.url() + "\n");
            break;
          case ":goto": {
            if (!arg) {
              log("usage: :goto <url>");
              break;
            }
            let target: string;
            try {
              target = validateUrl(arg);
            } catch (e) {
              log((e as Error).message);
              break;
            }
            try {
              await page.goto(target);
              process.stdout.write(page.url() + "\n");
            } catch (e) {
              log(`navigation failed: ${(e as Error).message}`);
            }
            break;
          }
          case ":model":
            if (!arg) {
              log(`model: ${model}`);
              break;
            }
            model = arg;
            log(`model set to ${model}`);
            break;
          case ":steps":
            if (!arg) {
              log(`max-steps: ${maxSteps}`);
              break;
            }
            try {
              maxSteps = parseMaxSteps(arg);
              log(`max-steps set to ${maxSteps}`);
            } catch (e) {
              log((e as Error).message);
            }
            break;
          default:
            log(`unknown command: ${cmd} (try :help)`);
        }
        return;
      }

      // A goal line.
      process.stdout.write(`goal: ${line}\n`);
      log(`running (${model}, up to ${maxSteps} steps)...`);
      // Re-resolve the page in case the previous one was closed.
      page = await currentPage(ctx);
      const result = await runAgentTask(page, line, { model, maxSteps });
      printResult(result, page.url());
    };

    // Serialize input: pause stdin while a goal/command runs so a second Enter can't start a
    // concurrent goal against the shared page; resume + re-prompt once it settles.
    const pump = (line: string) => {
      rl.pause();
      processLine(line)
        .catch((e) => log(`error: ${(e as Error).message}`))
        .finally(() => {
          if (!closing) {
            rl.resume();
            rl.prompt();
          }
        });
    };

    // If a --goal was passed alongside -i, run it as the first goal before the prompt loop.
    const initial = cli.goal?.trim();
    if (initial) {
      pump(initial);
    } else {
      rl.prompt();
    }

    rl.on("line", (raw) => {
      const line = raw.trim();
      if (line === "") {
        rl.prompt();
        return;
      }
      pump(line);
    });

    rl.on("close", finish);
  });
}

export async function main(argv: string[] = process.argv.slice(2)): Promise<void> {
  let cli: Cli;
  try {
    const parsed = parseCli(argv);
    if (parsed.help) {
      process.stdout.write(USAGE + "\n");
      return;
    }
    cli = parsed.cli;
  } catch (e) {
    if (e instanceof UsageError) {
      log(e.message);
      process.exitCode = e.code;
      return;
    }
    throw e;
  }

  try {
    process.exitCode = cli.interactive ? await runRepl(cli) : await runOneShot(cli);
  } catch (e) {
    log(`error: ${(e as Error).message}`);
    process.exitCode = 1;
  }
}

main();
