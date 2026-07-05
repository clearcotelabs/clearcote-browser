# clearcote (Python SDK)

A **Playwright drop-in** for [Clearcote](https://github.com/clearcotelabs/clearcote-browser) — the
open, reproducible, anti-fingerprint Chromium build. `launch()` returns a standard Playwright
`Browser`, so migrating is a one-line import change.

The verified Clearcote binary is **auto-downloaded and SHA-256 checked** on first use, then cached —
no zips or paths to manage.

> **Platform:** Clearcote ships **Windows x64** and **Linux x64** binaries; `launch()` runs on both
> and the SDK auto-downloads the right one for your OS. On Linux the persona is Linux-native (Linux
> GPU/voices/audio-device values) and DRM uses the Linux CDM. macOS is on the
> [roadmap](../../ROADMAP.md). On a minimal Linux host, install the browser's runtime libs (e.g.
> `apt-get install -y libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2
> libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libpango-1.0-0
> libcairo2 libx11-6 libxcb1 libexpat1 libdbus-1-3`) and pass `args=["--no-sandbox"]` (or
> `chown root:root chrome-sandbox && chmod 4755 chrome-sandbox`) in containers.

## Install

```bash
pip install clearcote
```

`playwright` is pulled in as a dependency. You do **not** need to run `playwright install`
(Clearcote uses its own browser binary, not Playwright's bundled Chromium).

## Usage

```python
from clearcote import launch

browser = launch(
    fingerprint="user-7423",        # per-eTLD+1 seed: same seed => same identity, different => unlinkable
    platform="windows",
    timezone="America/New_York",
    headless=False,
)
page = browser.new_page()
page.goto("https://abrahamjuliot.github.io/creepjs/")
# ... standard Playwright (sync API) from here ...
browser.close()
```

Already using Playwright? Swap `p.chromium.launch(...)` for `launch(...)` from `clearcote` — the
returned object is a normal Playwright `Browser`. (One shared Playwright driver is started lazily
and stopped at interpreter exit.)

### Async API (`clearcote.async_api`)

Inside an asyncio event loop, use the async API — it mirrors the sync one and returns Playwright
**async** objects (the sync API raises "Sync API inside the asyncio loop"):

```python
import asyncio
from clearcote.async_api import launch

async def main():
    browser = await launch(
        fingerprint="user-7423",
        platform="windows",
        timezone="America/New_York",
    )
    page = await browser.new_page()
    await page.goto("https://abrahamjuliot.github.io/creepjs/")
    # ... standard Playwright (async API) from here ...
    await browser.close()

asyncio.run(main())
```

Same options as the sync `launch` (fingerprint/persona/proxy/`geoip`/`profile`/`canvas_bridge`/
`humanize`/…). `clearcote.async_api` exposes `launch`, `launch_persistent_context`, `launch_agent`,
`run_agent_task`, `executable_path`, `download`, plus `Profile` (use `launch(profile="name")`). Each
launched browser owns its Playwright driver and stops it on `await browser.close()`.

### Through a proxy (report the proxy's IP, not your host's)

```python
browser = launch(
    fingerprint="user-7423",
    proxy={"server": "http://host:8080", "username": "u", "password": "p"},  # standard Playwright option
    timezone="America/New_York",
    webrtc_ip="203.0.113.10",       # make WebRTC report the proxy egress IP, not your host's
)
```

**WebRTC won't leak your real IP.** The engine *fabricates* the WebRTC server-reflexive (`srflx`) candidate at `webrtc_ip` and sends **no real STUN** from your host — so WebRTC reports the proxy IP and your real IP never leaks at the packet level. A plain candidate "relabel" doesn't stop the leak (the real STUN packet still goes out from your host); Clearcote sends none. Raw host candidates are suppressed, and the candidate set stays coherent (not empty/disabled).

### Auto geo-match (`geoip`)

Set `geoip=True` and Clearcote resolves the **proxy's exit IP** (looked up *through* the proxy) and auto-fills any unset `timezone`, `accept_language`, `location`, **and `webrtc_ip`** so the whole identity — clock, language, geo, and WebRTC IP — matches the proxy's region:

```python
browser = launch(
    fingerprint="user-7423",
    proxy={"server": "http://host:8080", "username": "u", "password": "p"},
    geoip=True,              # timezone, languages, location, AND WebRTC IP all auto-set to the proxy's geo
)
```

Anything you set explicitly wins over `geoip`. With no proxy it uses your direct connection's IP. The lookup needs an **http(s) proxy** — SOCKS proxies are skipped (set `timezone`/`accept_language` yourself).

Geo data comes from the offline [geoip-all-in-one](https://github.com/daijro/geoip-all-in-one) MaxMind database (downloaded + cached on first use; GPL-3.0 data, the same source Camoufox uses) — more accurate than a single online API — with `ip-api.com` as a fallback.

### Humanized input (`humanize`, `show_cursor`)

```python
browser = launch(fingerprint="user-7423", humanize=True)
page = browser.new_page()
page.goto("https://example.com")

page.click("#login")                       # eased bezier glide, then a trusted click
page.fill("#user", "alice")                # focus + key-by-key typing with human timing
page.locator("#pwd").type("s3cr3t")        # locators are humanized too
page.mouse.wheel(0, 800)                   # eased, multi-step scroll
# a held-button drag (slider captchas): the button stays pressed across the move
page.mouse.move(x0, y0); page.mouse.down(); page.mouse.move(x1, y0); page.mouse.up()
```

`humanize=True` installs **one consistent human-input standard** covering moving, clicking,
dragging, scrolling and typing — all dispatched as **native trusted input** (`isTrusted === true`,
`navigator.webdriver` stays `false`), at both the page level (`page.click`/`hover`/`dblclick`/
`type`/`fill`/`press`, `page.mouse.*`, `page.keyboard.type`) and the locator level
(`locator.click`/`type`/`fill`/`hover`/`press_sequentially`/`drag_to`/`check`/…). Mouse paths are
slightly bowed cubic-beziers built from the *last* cursor position (no snap back to the corner),
walked as a **sum of sub-movements with a min-jerk velocity profile** — a ballistic primary that
slightly over/undershoots plus a corrective move, i.e. the multi-peak velocity of real reaching, not
one symmetric bell. Because they use native input, the button held by `mouse.down()` stays held
across the move, so `down → move → up` is a real drag (slider captchas work). Clicks get an
actionability pre-flight (visible + enabled + stable + not covered) and fall back to the native click
if it fails. Typing goes key-by-key with **gaussian inter-key timing** + word-boundary pauses and the
occasional fat-finger correction; `page.fill` over 200 chars stays atomic (skips per-key typing) to
avoid crawling. Scrolling uses **ease-out inertia** (a fast flick decaying to a slow settle) with the
occasional reading pause.

`show_cursor=True` injects a red cursor dot that follows the real mouse, handy for watching a
headed run. Both default to off; everything stays standard Playwright when `humanize=False`.

### Render-backend coherence check (`check_render_coherence`)

A persona can claim a GPU, but if the page is actually painted by a software rasterizer
(SwiftShader/llvmpipe — common headless with no GPU) a strict detector can tell. Probe a live page:

```python
from clearcote import launch, check_render_coherence

br = launch(fingerprint="user-7423")
page = br.new_page(); page.goto("about:blank")
verdict = check_render_coherence(page)        # {'renderer', 'software_suspected', 'coherent', 'warnings', ...}
if not verdict["coherent"]:
    print(verdict["warnings"])                 # e.g. software rasterizer / incoherent GPU family
```

It reads the (unmasked) WebGL vendor/renderer the page actually sees, flags a software rasterizer (a
fatal headless tell — enable the canvas bridge or run headed on a real GPU) and an incoherent
vendor/renderer pair. Pass `claimed_gpu=...` to also assert the rendered family. The async API
exposes the same as `await clearcote.async_api.check_render_coherence(page)`.

### Hardened launch defaults

Every `launch()` already does, with no extra options:

- **drops Playwright's `--enable-automation`** so the engine's `AutomationControlled` feature stays
  off (it otherwise flips `navigator.webdriver`-adjacent tells). Pass your own `ignore_default_args`
  to override.
- **disables QUIC/HTTP-3 when a proxy is set**, so no UDP egresses around the proxy (a SOCKS5/HTTP
  proxy carries only TCP) — coherent with proxied Chrome.
- prints a one-line **coherence warning** to stderr for incoherent option combos it can't auto-fix
  (silence with `quiet=True` or `CLEARCOTE_NO_WARN=1`).

### Persistent profile

```python
from clearcote import launch_persistent_context

context = launch_persistent_context(
    "./profile-7423",
    fingerprint="user-7423",
    platform="windows",
)
```

### Widevine / DRM (`widevine=True`)

clearcote ships the **EME/Widevine plumbing** compiled in, but — being 100% open source — it does
**not** bundle Google's proprietary CDM. Pass `widevine=True` on a **persistent** context and the SDK
fetches that CDM once from Google's own component server (same as a real Chrome receives it), seeds it
into the profile, and enables it — so `navigator.requestMediaKeySystemAccess('com.widevine.alpha')`
resolves and DRM streams play, instead of EME being a "no-Widevine" tell.

```python
from clearcote import launch_persistent_context

ctx = launch_persistent_context("./profile-drm", widevine=True)   # fetch + seed + enable the CDM
page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.goto("https://example.com")
ok = page.evaluate("""async () => {
  const a = await navigator.requestMediaKeySystemAccess('com.widevine.alpha',
    [{initDataTypes:['cenc'], videoCapabilities:[{contentType:'video/mp4;codecs="avc1.42E01E"',
      robustness:'SW_SECURE_DECODE'}]}]);
  await a.createMediaKeys(); return true;
}""")
print("Widevine:", ok)            # True
ctx.close()
```

- Requires a **persistent** context (the CDM lives in `user_data_dir`) — not the incognito `launch()`.
- The CDM is cached under `~/.clearcote/WidevineCdm`; fetch it ahead of time with `fetch_widevine()`.
- It's **opt-in**: the clearcote package never distributes Google's CDM — *you* trigger the download.
- Software-secure (L3) playback. Hardware-secure (L1) paths are out of scope.

### AI agent (OpenRouter)

Drive a page with an **in-browser AI agent** — it perceives the live page, asks an LLM what to do,
and executes the steps as real, trusted input through Chrome's Actor framework. Defaults to
[OpenRouter](https://openrouter.ai); switch models with a single slug.

```python
from clearcote import launch_agent, run_agent_task

ctx = launch_agent(
    agent_llm_key=OPENROUTER_API_KEY,        # turns the agent on
    agent_model="openai/gpt-4o-mini",        # any provider/model slug
)
page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.goto("https://example.com")

result = run_agent_task(page, "Click the 'More information...' link.", max_steps=8)
print(result["success"], result["finalText"], result["steps"])
ctx.close()
```

- `agent_llm_key` is all you need — the engine auto-enables Chrome's Actor framework (no extra flags).
- `agent_llm_url` points at any OpenAI-compatible endpoint (default OpenRouter); `agent_tool_mode` is `"tools"` (function-calling) or `"json"`.
- Override the model per task: `run_agent_task(page, goal, model="anthropic/claude-3.5-sonnet")`.
- The agent needs a **regular profile** — use `launch_agent` / `launch_persistent_context`, not the incognito `launch()`.

### Capture or import a profile

Instead of the synthetic seed-derived identity, you can have Clearcote present a **real machine's
fingerprint**. Pass it to `launch()` via `fingerprint_profile` — fields present in the profile
**override** the seed-derived persona; **absent fields fall back** to the `fingerprint` seed, so
partial profiles stay coherent.

**1. Capture from a donor Chrome** — open `tools/fingerprint-collect/collect.html` and click
**Capture** (downloads a JSON), or paste the collector script in DevTools. It records an exhaustive
profile (navigator, screen, WebGL, audio, speech voices, fonts, codecs, CSS media, WebGPU, WebRTC).
See the [collector README](../../tools/fingerprint-collect/README.md).

**2. Or convert from the open-source 10k dataset** —
[`chrome-fingerprints`](https://github.com/Vinyzu/chrome-fingerprints):

```bash
pip install chrome-fingerprints
python tools/fingerprint-collect/convert_dataset.py --out ./profiles --count 100
```

**3. Launch with the profile:**

```python
browser = launch(
    fingerprint="seed-1",                 # seeds any field the profile doesn't specify
    fingerprint_profile="profile.json",   # path / dict / JSON string — SDK gzip+base64-encodes it
)
```

## Fingerprint options

All optional. Anything not listed here is passed straight through to Playwright
(`headless`, `proxy`, `args`, `timeout`, `slow_mo`, …).

| Kwarg | Switch | Meaning |
|---|---|---|
| `fingerprint` | `--fingerprint` | Master seed (per-eTLD+1 farbling root). `str` or `int`. |
| `platform` | `--fingerprint-platform` | `"windows"` \| `"linux"` \| `"macos"`. |
| `platform_version` | `--fingerprint-platform-version` | UA-CH platform version. |
| `brand` | `--fingerprint-brand` | `"Chrome"` \| `"Edge"` \| `"Opera"` \| `"Vivaldi"`. |
| `brand_version` | `--fingerprint-brand-version` | Brand version. |
| `gpu_vendor` | `--fingerprint-gpu-vendor` | WebGL UNMASKED vendor. |
| `gpu_renderer` | `--fingerprint-gpu-renderer` | WebGL UNMASKED renderer. |
| `hardware_concurrency` | `--fingerprint-hardware-concurrency` | `navigator.hardwareConcurrency`. |
| `location` | `--fingerprint-location` | `"lat,lng"` (only when geo permission is granted). |
| `timezone` | `--timezone` | IANA timezone, e.g. `"America/New_York"`. |
| `accept_language` | `--accept-lang` | `navigator.languages` + `Accept-Language` header, e.g. `"en-US,en"`. |
| `webrtc_ip` | `--webrtc-ip` | WebRTC IP to report. The engine **fabricates** the `srflx` candidate at this IP and sends **no real STUN** from the host, so the real IP never leaks (not merely relabeled). |
| `disable_gpu_fingerprint` | `--disable-gpu-fingerprint` | Turn off GPU/WebGL spoofing. |
| `geoip` | _(directive)_ | `True` → resolve the proxy's exit-IP geo and auto-fill timezone/accept_language/location/**webrtc_ip**. |
| `fingerprint_profile` | _(directive → `--fingerprint-profile`)_ | A real captured machine profile (file path / dict / JSON string); the SDK gzip+base64-encodes it. Fields present **override** the seed-derived persona; absent fields fall back to `fingerprint`. Also derives `accept_language` from the profile's `navigator.languages` when none is set. |
| `canvas_bridge` | _(→ `--canvas-bridge-*`)_ | Forward canvas/WebGL readbacks to a remote real-GPU host so the pixels a page hashes match the GPU your persona claims. `{"url", "auth", "mode", "allow", "deny", "fallback"}`; setting `url` auto-adds `--no-sandbox`. See [docs/CANVAS-BRIDGE.md](../../docs/CANVAS-BRIDGE.md). |
| `extensions` | _(→ `--load-extension` + `--disable-extensions-except`)_ | List of unpacked-extension directory paths to load (Chromium forces headed when extensions are present). |
| `humanize` | _(directive)_ | `True` → humanize all input (move/click/drag/scroll/type) as native trusted events, at the page and locator level. See [Humanized input](#humanized-input-humanize-show_cursor). |
| `show_cursor` | _(directive)_ | `True` → inject a red cursor dot that follows the real mouse (handy for watching a headed run). |

> **Headed launches** default to `no_viewport=True` so `window.innerWidth` tracks the real OS window — an emulated `1280×720` on a real window is an impossible-window tell. Pass an explicit `viewport` to override.
>
> **Proxies:** a `socks5://user:pass@host:port` proxy is routed via `--proxy-server` (Playwright rejects credentials in its SOCKS descriptor). Chromium can't authenticate SOCKS5, so the credentials are dropped with a warning — put the auth on a local relay.

## Saved profiles (`Profile`)

A `Profile` bundles a persona (seed, GPU, brand, …) **and** its `canvas_bridge` config under one
name you can persist and re-launch — the claimed GPU, the bridge endpoint, and the bridge's
GPU-keyed cache stay coherent because they travel together.

```python
from clearcote import Profile, launch

# save once
Profile("acct-1", {
    "fingerprint": "acct-1",
    "gpu_vendor": "Google Inc. (Intel)",
    "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
    "canvas_bridge": {"url": "ws://127.0.0.1:9099", "auth": "user:secret"},
}).save()

# re-launch anywhere (explicit kwargs override the saved options)
browser = Profile.load("acct-1").launch(headless=False)
# equivalently: launch(profile="acct-1")
```

Profiles are JSON at `~/.clearcote/profiles/<name>.json` (set `CLEARCOTE_PROFILE_DIR` to relocate).

## API

- `launch(**options)` → Playwright `Browser`. Pass `profile=` (a name, path, or `Profile`) to launch a saved persona.
- `launch_persistent_context(user_data_dir, **options)` → Playwright `BrowserContext`.
- `executable_path(executable_path=None, cache_dir=None, quiet=False)` → `str` — resolve (download/verify if needed) the chrome.exe path.
- `download(cache_dir=None, quiet=False)` → `str` — pre-fetch + verify without launching.
- `Profile` — `Profile(name, options)`, `.save(path=None)`, `Profile.load(name)`, `.launch(**overrides)`, `.launch_persistent_context(dir, **overrides)`; plus `list_profiles()`, `load_profile(name)`.
- `RELEASE` — the pinned release metadata (tag, version, sha256).

## Binary resolution & verification

`launch()` resolves the browser in this order:

1. `executable_path=` argument, if given;
2. `CLEARCOTE_BINARY` environment variable, if set;
3. otherwise **download** the pinned release, **verify its SHA-256** (the hash is baked into this
   package — it's the trust anchor), extract to a per-version cache, and verify the extracted
   `chrome.exe` hash too.

Cache location (override with `CLEARCOTE_CACHE`):
- Windows: `%LOCALAPPDATA%\clearcote\Cache\<tag>`
- macOS: `~/Library/Caches/clearcote/<tag>`
- Linux: `${XDG_CACHE_HOME:-~/.cache}/clearcote/<tag>`

A SHA-256 mismatch is a hard error — the SDK refuses to run an unverified binary. You can
independently confirm the published checksums and GPG signatures on the
[release page](https://github.com/clearcotelabs/clearcote-browser/releases).

### Stay on the latest build (`auto_update`)

By default the SDK installs the **exact browser build pinned into this package** — reproducible,
and the baked-in SHA-256 is the trust anchor. To follow new browser releases **without upgrading
the package every time**, opt in:

```python
browser = launch(fingerprint="seed-123", auto_update=True)
```

or set the environment variable globally:

```bash
CLEARCOTE_AUTO_UPDATE=1
```

With `auto_update`, the SDK resolves the **newest GitHub release**, downloads its zip, and verifies
it against that release's published `SHA256SUMS.txt`. When a **`gpg`** binary is available it
additionally imports the release's public key, confirms its fingerprint equals the pinned
`CA96F185 F96A693A EDB3AC1F CB00D851 B7A86B0F`, and verifies the signed checksum — so an
auto-resolved build is cryptographically authenticated, not just downloaded. If GitHub is
unreachable it falls back to the pinned release; if the latest release *is* the pinned one, the
audited baked-in hashes are used. Each build is cached per tag, so this only downloads when a new
version actually ships. (For locked-down/reproducible deployments, leave `auto_update` off and bump
the package deliberately.)

## License

BSD-3-Clause. See [LICENSE](../../LICENSE).
