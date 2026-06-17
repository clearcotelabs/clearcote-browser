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
    webrtc_ip="203.0.113.10",       # make WebRTC report the proxy egress IP
)
```

### Persistent profile

```python
from clearcote import launch_persistent_context

context = launch_persistent_context(
    "./profile-7423",
    fingerprint="user-7423",
    platform="windows",
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
| `webrtc_ip` | `--webrtc-ip` | WebRTC egress IP to report (your proxy IP). |
| `disable_gpu_fingerprint` | `--disable-gpu-fingerprint` | Turn off GPU/WebGL spoofing. |

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

## License

BSD-3-Clause. See [LICENSE](../../LICENSE).
