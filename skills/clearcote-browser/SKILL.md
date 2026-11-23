---
name: clearcote-browser
description: Drive the Clearcote anti-fingerprint Chromium from Python or Node with Playwright, Puppeteer or any CDP client. Use when a scraper or browser agent is blocked or flagged as a bot, when it needs a stable browser identity per account (fingerprint seed), a proxy whose timezone and language match the exit IP, human-paced input, or a stealth CDP endpoint for browser-use, Crawl4AI or Stagehand. Covers install, launch, proxies with geoip, persistent profiles, serve() and the MCP server, plus the mistakes that undo the stealth.
---

# Clearcote browser

Clearcote is an open-source Chromium build that corrects its fingerprint in the C++ engine. The SDKs return a
standard Playwright `Browser`, so existing Playwright code keeps working after a one-line import change.

Docs: https://www.clearcotelabs.com/docs · Source: https://github.com/clearcotelabs/clearcote-browser

## Install

```bash
pip install clearcote          # Python
npm install clearcote          # Node
```

Do **not** run `playwright install`: the SDK downloads the Clearcote browser itself on first launch, checks its
SHA-256 and caches it. Windows x64 and Linux x64 run natively; see the docs for macOS and Docker.

## Launch (Python)

```python
from clearcote import launch

browser = launch(
    fingerprint="acct-1",     # seed: the same seed is the same device on every run, another seed is unlinkable
    platform="windows",
    headless=False,
)
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

Inside asyncio use `from clearcote.async_api import launch` and `await` the same calls.

## Launch (Node)

```js
import { launch } from "clearcote";

const browser = await launch({ fingerprint: "acct-1", platform: "windows" });
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

## Proxy with a matching identity

```python
browser = launch(
    fingerprint="acct-1",
    proxy={"server": "http://host:8080", "username": "u", "password": "p"},
    geoip=True,   # timezone, languages and location follow the proxy's exit IP
)
```

Use one seed per account and keep the same proxy region for that seed. Anything set explicitly (`timezone`,
`accept_language`, `webrtc_ip`) wins over `geoip`.

## Keep cookies and logins between runs

```python
from clearcote import launch_persistent_context

context = launch_persistent_context("./profile-acct-1", fingerprint="acct-1", platform="windows")
```

## Human-paced input

```python
browser = launch(fingerprint="acct-1", humanize=True)
```

`page.click`, `page.fill` and locators then move the mouse along a curve and type key by key.

## Existing tools over CDP (browser-use, Crawl4AI, Stagehand, Puppeteer)

```python
from clearcote import serve

with serve(fingerprint="acct-1", platform="windows") as srv:
    print(srv.cdp_url)   # point connect_over_cdp / puppeteer.connect / browser-use at this URL
```

`serve()` starts the browser directly, so `navigator.webdriver` stays `false`. It binds to `127.0.0.1`.

## From an MCP client

Add `{"command": "npx", "args": ["-y", "clearcote-mcp"]}` to the client's `mcpServers`. It gives about 20 tools
(`navigate`, `read_page`, `click`, `fill_field`, `screenshot_page`, `get_cdp_endpoint`, ...). Setup:
https://www.clearcotelabs.com/docs/mcp

## Mistakes that undo the stealth

- Adding `puppeteer-extra-plugin-stealth`, `undetected-chromedriver` or any JavaScript patching. The engine already
  handles the fingerprint, and these patches are detectable themselves.
- Overriding the user agent, viewport or screen by hand. Let the seed decide, or the values stop matching each other.
- Reusing one seed across unrelated accounts, or rotating proxy countries under one seed.
- A proxy without `geoip=True` (or an explicit `timezone`): the page then shows this machine's timezone and language.
- Sending requests with `context.request` when the site checks TLS: use `fetch` inside the page
  (`page.evaluate`) so the request comes from the browser.

## Free and paid

The open build needs no key. A licence key (`CLEARCOTE_LICENSE_KEY` or `license_key=`) unlocks the newer builds
and more concurrency. `launch(cloud=True)` runs the same code on a hosted Clearcote browser and needs an API key.
Details: https://www.clearcotelabs.com/pricing
