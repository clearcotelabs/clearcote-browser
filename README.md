<div align="center">

<img src="docs/assets/clyde.svg" alt="Clyde, the Clearcote chameleon" width="120" />

# Clearcote Browser

**Blend in. Stay clear.**

[![Release](https://img.shields.io/github/v/release/clearcotelabs/clearcote-browser?include_prereleases&label=release&style=flat-square&labelColor=07080a&color=38e0d6)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![npm](https://img.shields.io/npm/v/clearcote?style=flat-square&logo=npm&logoColor=white&label=npm&labelColor=07080a&color=CB3837)](https://www.npmjs.com/package/clearcote)
[![PyPI](https://img.shields.io/pypi/v/clearcote?style=flat-square&logo=pypi&logoColor=white&label=pip&labelColor=07080a&color=3776AB)](https://pypi.org/project/clearcote/)
[![NuGet](https://img.shields.io/nuget/v/Clearcote?style=flat-square&logo=nuget&logoColor=white&label=nuget&labelColor=07080a&color=004880)](https://www.nuget.org/packages/Clearcote)
[![Docker](https://img.shields.io/docker/pulls/teamflatearth/clearcote?style=flat-square&logo=docker&logoColor=white&label=docker&labelColor=07080a&color=2496ED)](https://hub.docker.com/r/teamflatearth/clearcote)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-38e0d6?style=flat-square&labelColor=07080a)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?style=flat-square&logo=discord&logoColor=white&labelColor=07080a)](https://discord.gg/WxvCjAnXZm)
[![llms.txt](https://img.shields.io/badge/for%20agents-llms.txt-24292f?style=flat-square&logo=readme&logoColor=white)](llms.txt)

**[Website](https://www.clearcotelabs.com/)** · [Docs](https://www.clearcotelabs.com/docs) · [Pricing](https://www.clearcotelabs.com/pricing) · [Playground](https://www.clearcotelabs.com/dashboard/playground) · [Discord](https://discord.gg/WxvCjAnXZm)

</div>

Clearcote is an open-source Chromium with its fingerprint controls compiled into the C++ engine. Each
browser presents one coherent machine: the JavaScript surface, the HTTP headers and the TLS handshake
all come from the same real Chromium, so they agree with each other. You drive it with the Playwright
or Puppeteer code you already have.

| | |
|---|---|
| **What it is** | A Chromium distribution (based on ungoogled-chromium) plus a stack of readable source patches |
| **Works with** | Playwright, Puppeteer, and any tool that speaks the Chrome DevTools Protocol (CDP) |
| **SDKs** | Python, Node.js / TypeScript, .NET. `launch()` returns a standard Playwright `Browser` |
| **Runs on** | Windows x64, Linux x64, Docker. Or hosted on our servers, with nothing to install |
| **License** | BSD-3-Clause for the code and patches. The open build is reproducible from this repo |
| **Cost** | Open build: free. Latest build: free for one browser at a time with GitHub. Pro: $49/month. Hosted: €1 per GB |
| **Website** | [clearcotelabs.com](https://www.clearcotelabs.com/): docs, pricing, the dashboard and hosted browsers |
| **For AI agents** | [`llms.txt`](llms.txt) · [`AGENTS.md`](AGENTS.md) · an [MCP server](mcp/) |

---

## Choose how to run it

Pick the row that matches what you want to do, then jump to its section. Every path runs the same engine.

| I want to… | Use | Section |
|---|---|---|
| Use Clearcote from my own Playwright or Puppeteer code | The SDK | [Run it from your code](#run-it-from-your-code) |
| Attach a tool that already speaks CDP (browser-use, Crawl4AI, Stagehand, …) | A CDP endpoint | [Run it as a CDP endpoint](#run-it-as-a-cdp-endpoint) |
| Run it in a container | The Docker image | [Run it in Docker](#run-it-in-docker) |
| Run it without installing anything | Hosted browsers | [Run it in the cloud](#run-it-in-the-cloud) |
| Give an AI assistant (Claude, Cursor, Cline) a browser | The MCP server | [Give an AI assistant a browser](#give-an-ai-assistant-a-browser) |
| Describe a task in words and get JSON back | Agent runs | [Let an agent do the task](#let-an-agent-do-the-task) |
| Move code I already run on another tool | Usually one changed line | [Switch from another tool](#switch-from-another-tool) |

---

## Switch from another tool

Clearcote is a real Chromium that speaks CDP, so existing code usually moves over by changing the one
line that starts or finds the browser. The code after that line stays the same.

**From Playwright.** `launch()` returns the same Playwright `Browser`, so only the launch call changes:

```diff
- from playwright.sync_api import sync_playwright
- browser = sync_playwright().start().chromium.launch()
+ from clearcote import launch
+ browser = launch(fingerprint="user-7423")
```

```diff
- import { chromium } from "playwright";
- const browser = await chromium.launch();
+ import { launch } from "clearcote";
+ const browser = await launch({ fingerprint: "user-7423" });
```

**From a tool that connects over CDP.** Start Clearcote as an endpoint (`clearcote-serve --port 9222`,
the [Docker image](#run-it-in-docker) or `serve()`), then point the tool at it:

| Tool | The line that changes |
|---|---|
| Playwright (Python / Node) | `chromium.connect_over_cdp("http://127.0.0.1:9222")` / `chromium.connectOverCDP(...)` |
| Puppeteer | `puppeteer.connect({ browserURL: "http://127.0.0.1:9222" })` |
| browser-use | `Browser(cdp_url="http://127.0.0.1:9222")` |
| Crawl4AI | `BrowserConfig(browser_mode="cdp", cdp_url="http://127.0.0.1:9222")` |
| nodriver | `uc.start(host="127.0.0.1", port=9222)` |
| Playwright MCP | `npx @playwright/mcp --cdp-endpoint http://127.0.0.1:9222` |
| Stagehand 4 | `localBrowser.connect({ cdpUrl: "http://127.0.0.1:9222" })`, then `Stagehand.create({ browser })`. Needs two launch options, below |
| Any other CDP client | The `webSocketDebuggerUrl` from `http://127.0.0.1:9222/json/version` |

Stagehand 4 runs its own extension inside the browser, so the endpoint needs the two switches that
Stagehand's own launcher adds. Keep the port on `127.0.0.1`:

```javascript
import { serve } from "clearcote";
import { Stagehand, localBrowser } from "@browserbasehq/stagehand";

const srv = await serve({
  fingerprint: "user-7423",
  allowOrigins: "*",                               // Stagehand's extension connects back to the endpoint
  args: ["--enable-unsafe-extension-debugging"],   // lets Stagehand load that extension over CDP
});
const stagehand = await Stagehand.create({ browser: await localBrowser.connect({ cdpUrl: srv.cdpUrl }) });
```

Each row was run against a Clearcote endpoint on 6 October 2026 (Playwright 1.63, Puppeteer 25,
browser-use 0.13, Crawl4AI 0.9, nodriver 0.50, Playwright MCP 0.0.83, Stagehand 4.1).

**From a hosted browser service.** If your provider gives you a CDP WebSocket URL, use the `connectUrl`
of a Clearcote cloud session instead. Your connect call does not change. See [Run it in the cloud](#run-it-in-the-cloud).

**From a stealth plugin.** Remove `puppeteer-extra-plugin-stealth`, `playwright-stealth` and similar
plugins. The persona is set in the engine, and script patches on top of it conflict with it.

**From a closed anti-detect browser.** These usually start a profile through a local API and give your
script a CDP address. Replace that call with `serve(fingerprint="profile-name")` or `launch(...)`. The
same seed returns the same machine every time; give `serve()` a `user_data_dir` to keep cookies and storage
as well, or use the [Profile Manager](https://github.com/clearcotelabs/clearcote-profile-manager) desktop app.

**From Selenium or WebDriver.** This is the exception. Clearcote is driven over CDP, so those scripts need
porting to Playwright, Puppeteer or nodriver.

Side-by-side comparisons with other tools: [clearcotelabs.com/alternatives](https://www.clearcotelabs.com/alternatives).

---

## Run it from your code

Install the SDK. It downloads the right Clearcote binary for your OS on first use, checks its SHA-256,
and caches it. You do not need `playwright install`.

```bash
pip install clearcote          # Python
npm install clearcote          # Node.js / TypeScript
dotnet add package Clearcote   # .NET
```

**Python**

```python
from clearcote import launch          # asyncio: from clearcote.async_api import launch

browser = launch(
    fingerprint="user-7423",          # same seed -> same machine every launch; new seed -> a new, unlinked one
    platform="windows",               # "windows" | "linux" | "macos" | "android"
    timezone="America/New_York",
)
page = browser.new_page()             # a standard Playwright Browser from here on
page.goto("https://example.com")
browser.close()
```

**Node.js / TypeScript**

```javascript
import { launch } from "clearcote";

const browser = await launch({
  fingerprint: "user-7423",
  platform: "windows",
  brand: "Edge",                      // "Chrome" (default) | "Edge" | "Opera" | "Vivaldi"
  timezone: "America/New_York",
});
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

**.NET**

```csharp
using Clearcote;

var browser = await Clearcote.Clearcote.LaunchAsync(new LaunchOptions {
    Fingerprint = "user-7423", Platform = "windows", Timezone = "America/New_York",
});
var page = await browser.NewPageAsync();   // a standard Microsoft.Playwright IBrowser
await page.GotoAsync("https://example.com");
await browser.CloseAsync();
```

### Common options

| Option (Python / Node) | What it does |
|---|---|
| `fingerprint` | The seed. One seed is one machine: hardware, screen, GPU, fonts, locale and per-site render noise all derive from it |
| `platform` | The operating system the browser presents: `windows`, `linux`, `macos` or `android` |
| `brand` | The browser brand in the user agent and client hints: `Chrome`, `Edge`, `Opera`, `Vivaldi` |
| `proxy` | `{"server": "http://host:8080", "username": "u", "password": "p"}`. HTTP and SOCKS5 with a username and password both work |
| `geoip` / `geoip` | `True` matches the timezone, languages and `Accept-Language` to the proxy's exit location (and the WebRTC IP, on the licensed build) |
| `humanize` / `humanize` | Moves, clicks, drags, scrolls and types as native, trusted input with human-like paths |
| `light_stealth` / `lightStealth` | Changes only hardware metadata (cores, memory, screen depth, pixel ratio, touch) and leaves rendering real |
| `fingerprint_profile` / `fingerprintProfile` | Loads a real machine captured with the [collector](tools/fingerprint-collect) or from the [profile library](https://github.com/clearcotelabs/clearcote-profiles) |
| `version` / `version` | Picks a Chromium major, for example `"150"`. Leave it out for the newest build your tier allows |

Anything else is passed straight to Playwright (`headless`, `args`, `timeout`, …). Full lists:
[Python](sdk/python/README.md#fingerprint-options) · [Node](sdk/node/README.md#fingerprint-options) · [.NET](sdk/dotnet).

### Using a plain binary instead of the SDK

Download a signed build from [Releases](https://github.com/clearcotelabs/clearcote-browser/releases), unzip it,
and point stock Playwright (or any CDP client) at it with `--fingerprint` switches:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=r"C:\clearcote\chrome.exe",
        args=["--fingerprint=seed-123", "--fingerprint-platform=windows"],
    )
    page = browser.new_page()
    page.goto("https://example.com")
    browser.close()
```

---

## Run it as a CDP endpoint

`serve()` starts Clearcote directly (not through Playwright), so no automation flag is added and
`navigator.webdriver` stays `false`. Anything that can connect over CDP attaches to it unchanged.

```bash
clearcote-serve --port 9222 --fingerprint seed-123 --platform windows   # prints http://127.0.0.1:9222
```

```python
from clearcote import serve
from playwright.sync_api import sync_playwright

with serve(fingerprint="seed-123", platform="windows") as srv:          # binds 127.0.0.1
    browser = sync_playwright().start().chromium.connect_over_cdp(srv.cdp_url)
```

Need several identities behind one port? `clearcote serve --port 9222` starts one browser per identity,
chosen in the connection URL (`http://127.0.0.1:9222?fingerprint=acct-1&timezone=Europe/Berlin`).
See [many identities on one endpoint](sdk/python/README.md#many-identities-on-one-endpoint-serve_multiplex-clearcote-serve).

---

## Run it in Docker

The official image is a Clearcote browser exposed as a CDP endpoint on port 9222.

```bash
docker run -d --rm --shm-size=1g -p 127.0.0.1:9222:9222 teamflatearth/clearcote
```

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.new_page()
    page.goto("https://example.com")
    print(page.title())
```

Set the persona with environment variables: `CC_PLATFORM` (`windows`, `linux`, `macos`, `android`),
`CC_FINGERPRINT` (the seed; unset, each container gets its own random one), `CC_BRAND`, `CC_TIMEZONE`,
`CC_ACCEPT_LANGUAGE`, `CC_TLS_PROFILE`.

```bash
docker run -d --shm-size=1g -p 127.0.0.1:9222:9222 \
  -e CC_PLATFORM=windows -e CC_FINGERPRINT=user-7423 teamflatearth/clearcote
```

The CDP port gives full control of the browser. Publish it on `127.0.0.1` or a trusted network only.
The image bakes in the signed Linux binary and the fonts a Windows persona needs.
Details: [`docker/README.md`](docker/README.md) · [deployment guide](https://www.clearcotelabs.com/docs/deployment).

---

## Run it in the cloud

Hosted Clearcote browsers run on our servers. One API call starts one and returns a CDP WebSocket URL,
and your Playwright or Puppeteer code connects to it the same way it connects to a local browser.

```python
from clearcote import launch

browser = launch(cloud=True, country="us", identity="acct-1")   # needs CLEARCOTE_API_KEY=cc_live_...
page = browser.new_page()
page.goto("https://example.com")
browser.close()                                                 # ends the hosted session
```

Setting `CLEARCOTE_CLOUD=1` moves existing `launch()` code to the cloud without editing it. Without the SDK:

```javascript
import { chromium } from "playwright";

const res = await fetch("https://www.clearcotelabs.com/api/v1/browsers", {
  method: "POST",
  headers: { authorization: "Bearer cc_live_...", "content-type": "application/json" },
  body: JSON.stringify({ identity: "acct-1", country: "us" }),
});
const { connectUrl } = await res.json();
const browser = await chromium.connectOverCDP(connectUrl);
```

- **A residential IP is included.** Pick a `country`, `state` or `city`, or leave it to us.
- **The same `identity` label returns the same device on the same exit IP** in later sessions.
- **€1 per GB of traffic, and nothing else**: no plan, no hourly charge, no separate proxy bill. Prepaid from €5; `maxGb` caps a session.
- **Dedicated physical servers**, not shared cloud virtual machines.
- **The dashboard [Playground](https://www.clearcotelabs.com/dashboard/playground)** runs a task or a script in a cloud browser and shows the live view, every step and the result.

New accounts that sign in with a GitHub account at least 30 days old get a one-time welcome credit.
API keys: [dashboard](https://www.clearcotelabs.com/dashboard/api-keys) · Docs: [hosted browsers](https://www.clearcotelabs.com/docs/hosted-browsers).

---

## Give an AI assistant a browser

The [MCP server](mcp/) gives Claude Desktop, Cursor, Cline or any MCP client one shared Clearcote browser
and about 20 tools (`read_page`, `click`, `fill_field`, `screenshot`, `save_profile`, `get_cdp_endpoint`, …).

```json
{
  "mcpServers": {
    "clearcote": {
      "command": "npx",
      "args": ["-y", "clearcote-mcp"],
      "env": { "CLEARCOTE_FINGERPRINT": "acct-1", "CLEARCOTE_PLATFORM": "windows" }
    }
  }
}
```

Python users can run `pip install clearcote-mcp` and `clearcote-mcp` instead of `npx`.

---

## Let an agent do the task

**In the cloud.** Send a task in plain words; an agent works through it in a hosted browser and returns
JSON that matches your schema. Secrets are referred to as `{{name}}` and never reach the model.

```python
from clearcote.cloud import Cloud

run = Cloud().runs.create(
    "Return the three newest posts with their title and link",
    url="https://news.ycombinator.com/newest",
    schema={"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "link": {"type": "string"}}}},
)
print(run["status"], run["result"]["output"])
```

Runs can pause and hand the browser to a person, be recorded, and report to a webhook.
Reference: [agent runs](https://www.clearcotelabs.com/docs/runs).

**On your machine.** The SDK also ships an in-browser agent that uses Chrome's own Actor framework and
any model on [OpenRouter](https://openrouter.ai) or another OpenAI-compatible endpoint:
[`launch_agent` / `run_agent_task`](sdk/python/README.md#ai-agent-openrouter).

---

## How it works

**The controls live in the engine, not in the page.** Most stealth tools change the fingerprint from
JavaScript: they replace `navigator.webdriver`, the WebGL vendor or `navigator.plugins` with script.
A replaced function can be told apart from a native one, for example:

- `Function.prototype.toString` shows its source instead of `[native code]`;
- its property descriptor and `hasOwnProperty` differ from a native getter's;
- a clean `toString` taken from a new iframe or Web Worker still sees the replacement.

In Clearcote the getter behind `navigator.vendor` is the C++ getter, so it is native code in every frame
and worker. There is no replacement to find.

**One seed, one coherent machine.** A `fingerprint` seed sets canvas, WebGL, WebGPU, audio, fonts,
screen, hardware, locale and timezone together, so the values agree with each other. Because the page's
JavaScript and the network stack come from one real Chromium, the user agent, client-hint headers, TLS
handshake and HTTP/2 settings agree too.

**What you can control**

- **Identity:** user agent and client hints (brand, platform, version, architecture), the same in JavaScript and in `Sec-CH-UA` headers.
- **GPU:** WebGL vendor, renderer, `getParameter` limits and extensions, plus WebGPU limits that match the same GPU.
- **Rendering:** per-site canvas, WebGL and audio noise from the seed, or switched off; an optional [real-GPU canvas bridge](docs/CANVAS-BRIDGE.md).
- **Fonts:** the persona's operating-system fonts with the right widths, including on a Linux server.
- **Hardware and screen:** cores, memory, storage quota, screen size, colour depth, pixel ratio, `getScreenDetails()`, touch points.
- **Locale and network:** timezone, languages, `Accept-Language`, `Intl` locale, geolocation, and a TLS and HTTP/2 shape that matches the claimed Chrome version. The licensed build also reports a coherent WebRTC IP.
- **Long tail:** speech voices, media codecs, media devices, CSS media queries, battery, network information, keyboard layout.
- **Input:** human-like, trusted mouse and keyboard input that keeps `navigator.webdriver` `false` (synthetic paths in the open build, recorded human paths in the licensed build).

Each patch and what it changes: [docs/PATCHES.md](docs/PATCHES.md) · the release gate that checks them: [docs/STEALTH-COHERENCE.md](docs/STEALTH-COHERENCE.md).

---

## Builds and tiers

There is one open build and one licensed build. The licensed build is offered two ways.

| | **Open build** | **Free with GitHub** | **Pro, $49/month** |
|---|---|---|---|
| Account | None | GitHub account at least 30 days old | Clearcote account |
| Build | Open, reproducible from this repo | Latest licensed build | Latest licensed build |
| New Chromium majors | About 2 months after release | The day they are built | The day they are built |
| Private stealth patches, recorded human motion, profile library | No | Yes | Yes |
| Older builds and version pinning | Open builds | Latest only | Yes |
| Browsers at the same time | Unlimited | 1 | Up to 250, more on request |
| Support | GitHub issues | GitHub issues | Email from the owner |

- The open build has the full identity surface: personas, render noise, TLS profiles, humanized input. No tier unlocks more spoofing.
- The licensed build adds stealth work that is kept private, so it is **not** reproducible from public source.
- A licence key goes in `CLEARCOTE_LICENSE_KEY=cc_lic_...`, `license_key=` / `licenseKey`, or `clearcote login`. Free with GitHub needs SDK 0.30.0 or newer.

[Get it free with GitHub](https://www.clearcotelabs.com/pricing#free) · [Get Pro](https://www.clearcotelabs.com/pricing) · per-feature table: [Node](sdk/node/README.md#whats-in-each-tier)

### Build availability (October 2026)

| Chromium | Licensed build (Free with GitHub, Pro) | Open build |
|---|---|---|
| **153** (`153.0.8010.53`) | **Available now** | ~Nov 2026 |
| **152** (`152.0.7977.82`) | Available | ~Nov 2026 |
| **151** (`151.0.7922.108`) | Available | ~Oct 2026 |
| **150** (`150.0.7871.114`) | Available | **Available now** |
| **149** (`149.0.7827.114`) | Available | Available |

Open-build dates are estimates on a two-month cadence, not promises. Choose a major with
`launch(version="150")`, or omit `version` for the newest your tier allows.

---

## Verify a release, or build it yourself

Every release is SHA-256 checksummed and GPG-signed with the Clearcote release key, which does not change:

```
CA96 F185 F96A 693A EDB3  AC1F CB00 D851 B7A8 6B0F
```

The open build can be rebuilt from this repo and compared with the published one
([docs/VERIFY.md](docs/VERIFY.md)). To build Windows (cross-compiled) or Linux (native) on a Linux host:

```bash
git clone https://github.com/clearcotelabs/clearcote-browser.git
cd clearcote-browser && WORK=~/clearcote-build ./build.sh
```

Guide: [docs/BUILDING.md](docs/BUILDING.md) · patches: [`patches/`](patches/) (applied in the order of [`patches/series`](patches/series)).

---

## FAQ

**Is Clearcote free?**
The open build is free and open source with no account. The latest licensed build is free for one browser
at a time with a GitHub account, and Pro ($49/month) runs up to 250 at once. Hosted browsers cost €1 per GB.

**Do I have to change my Playwright code?**
No. `launch()` returns a normal Playwright `Browser`. Replace `chromium.launch(...)` with `launch(...)` and keep the rest.

**Does it work with Puppeteer, Selenium or other tools?**
Puppeteer and any CDP client: yes, through the binary or a CDP endpoint. Selenium and WebDriver: no, because Clearcote is driven over CDP.

**Which operating systems are supported?**
Windows x64 and Linux x64, plus the Docker image. The browser can present itself as Windows, Linux, macOS or Android on either.
macOS and ARM64 builds are on the [roadmap](ROADMAP.md).

**Should I add a stealth plugin on top?**
No. Plugins such as `puppeteer-extra-plugin-stealth` change values from JavaScript, which conflicts with the engine-level persona.

**Is the licensed build open source?**
No. The open build is BSD-3 and reproducible. The licensed build adds private patches and is not reproducible from public source.

**What is it for?**
Privacy, QA and testing, research and lawful automation. Respect site terms and the law. See [DISCLAIMER.md](DISCLAIMER.md).

---

## More

| | |
|---|---|
| **Website** | [clearcotelabs.com](https://www.clearcotelabs.com/) · [Docs](https://www.clearcotelabs.com/docs) · [Pricing](https://www.clearcotelabs.com/pricing) · [Compare alternatives](https://www.clearcotelabs.com/alternatives) · [Blog](https://www.clearcotelabs.com/blog) |
| **SDK references** | [Python](sdk/python/README.md) · [Node](sdk/node/README.md) · [.NET](sdk/dotnet) |
| **Docs** | [VERIFY](docs/VERIFY.md) · [BUILDING](docs/BUILDING.md) · [PATCHES](docs/PATCHES.md) · [UPGRADING](docs/UPGRADING.md) · [CANVAS-BRIDGE](docs/CANVAS-BRIDGE.md) · [STEALTH-COHERENCE](docs/STEALTH-COHERENCE.md) |
| **Hosted** | [Hosted browsers](https://www.clearcotelabs.com/docs/hosted-browsers) · [Agent runs](https://www.clearcotelabs.com/docs/runs) · [Playground](https://www.clearcotelabs.com/dashboard/playground) |
| **Profiles** | [clearcote-profiles](https://github.com/clearcotelabs/clearcote-profiles) · [collector](tools/fingerprint-collect) · [Profile Manager app](https://github.com/clearcotelabs/clearcote-profile-manager) |
| **For agents** | [llms.txt](llms.txt) · [AGENTS.md](AGENTS.md) |
| **Project** | [ROADMAP](ROADMAP.md) · [CONTRIBUTING](CONTRIBUTING.md) · [SECURITY](SECURITY.md) · [Discord](https://discord.gg/WxvCjAnXZm) |

## Credits and license

Clearcote builds on [Chromium](https://www.chromium.org/) (BSD-3),
[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) (the de-Googled base),
[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium) (engine-level fingerprint controls),
[Brave](https://brave.com/privacy-updates/3-fingerprint-randomization/) (the per-site "farbling" model) and
[Camoufox](https://github.com/daijro/camoufox) (a sibling open browser). It is an independent project and
ships no proprietary code. Full attributions: [CREDITS.md](CREDITS.md).

Code and patches are [BSD-3-Clause](LICENSE); upstream components keep their own licenses. Provided as is
([DISCLAIMER.md](DISCLAIMER.md)).
