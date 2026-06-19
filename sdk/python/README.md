# clearcote (Python SDK)

A **Playwright drop-in** for [Clearcote](https://github.com/clearcotelabs/clearcote-browser) — the
open, reproducible, anti-fingerprint Chromium build. `launch()` returns a standard Playwright
`Browser`, so migrating is a one-line import change.

The verified Clearcote binary is **auto-downloaded and SHA-256 checked** on first use, then cached —
no zips or paths to manage.

> **Platform:** Clearcote currently ships a **Windows x64** binary, so `launch()` runs on Windows.
> (The SDK will download + verify the binary on any OS — handy for packaging — but only launches it
> on Windows. Linux/macOS builds are on the [roadmap](../../ROADMAP.md).)

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

### Persistent profile

```python
from clearcote import launch_persistent_context

context = launch_persistent_context(
    "./profile-7423",
    fingerprint="user-7423",
    platform="windows",
)
```

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

## API

- `launch(**options)` → Playwright `Browser`.
- `launch_persistent_context(user_data_dir, **options)` → Playwright `BrowserContext`.
- `executable_path(executable_path=None, cache_dir=None, quiet=False)` → `str` — resolve (download/verify if needed) the chrome.exe path.
- `download(cache_dir=None, quiet=False)` → `str` — pre-fetch + verify without launching.
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
