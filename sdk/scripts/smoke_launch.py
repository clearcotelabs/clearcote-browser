#!/usr/bin/env python3
"""Release smoke test (Python side): actually launch the browser and prove the engine starts.

Launches the FREE build headless, and — when CLEARCOTE_LICENSE_KEY (or CCKEY) is set — the PRO
build too, then reads a real navigator.userAgent (proves the process started AND runs JS). Exits
non-zero on ANY failure so a release pipeline can gate on it. This imports whatever `clearcote` is
installed in the current environment, so run it after installing the version you want to verify
(e.g. `pip install clearcote==X`). See docs/RELEASE-SMOKE-TEST.md.
"""
import os
import platform
import sys

try:
    from clearcote import __version__, launch
except Exception as e:  # noqa: BLE001
    print(f"[PY] import clearcote FAILED: {type(e).__name__}: {e}")
    sys.exit(2)


def run(tier: str, key: str | None) -> bool:
    kw = {"license_key": key} if key else {}
    try:
        b = launch(headless=True, args=["--no-sandbox"], quiet=True, **kw)
        page = b.new_page()
        ua = page.evaluate("() => navigator.userAgent")
        b.close()
    except Exception as e:  # noqa: BLE001
        print(f"[PY {platform.system()}] {tier}: LAUNCH_FAIL ({type(e).__name__}: {e})")
        return False
    ok = "Chrome" in ua
    print(f"[PY {platform.system()}] {tier}: {'LAUNCH_OK' if ok else 'LAUNCH_FAIL'} | {ua[:58]}")
    return ok


key = os.environ.get("CLEARCOTE_LICENSE_KEY") or os.environ.get("CCKEY")
print(f"[PY] clearcote {__version__} on {platform.system()} py{sys.version.split()[0]}")

results = [run("FREE", None)]
if key:
    results.append(run("PRO ", key))
else:
    print("[PY] PRO : SKIPPED (set CLEARCOTE_LICENSE_KEY to test the licensed build)")

sys.exit(0 if all(results) else 1)
