"""Opt-in Widevine CDM fetch + profile seeding (Windows).

clearcote is a 100%-open-source build, so it ships with the EME/Widevine *plumbing* compiled in
(`enable_widevine=true`) but WITHOUT the proprietary Widevine CDM binary (Google's blob can't live
in a FOSS package). This module lets a user *opt in* to fetching that CDM from Google's component
server at runtime — exactly like a real Chrome receives it via the component updater — so
`navigator.requestMediaKeySystemAccess('com.widevine.alpha')` resolves (DRM streaming works, and the
EME surface matches a real Chrome on Windows instead of being a "no-Widevine" coherence tell).

    from clearcote import fetch_widevine
    cdm_dir = fetch_widevine()          # downloads + verifies into ~/.clearcote/WidevineCdm
    # then launch a PERSISTENT context with widevine=True (auto-seeds the hint file):
    ctx = clearcote.launch_persistent_context("profile", widevine=True)

The CDM is fetched once and cached; `seed_widevine()` writes the component hint file into a
user-data-dir so the engine loads it. By using YOUR opt-in fetch, the clearcote package itself never
distributes Google's CDM.
"""

import hashlib
import io
import json
import os
import struct
import sys
import urllib.request
import zipfile

# The Widevine CDM component (Chrome's component-updater app id) + Google's Omaha JSON endpoint.
WIDEVINE_APP_ID = "oimompecagnajdejgnnjijobebaeigek"
OMAHA_URL = "https://update.googleapis.com/service/update2/json"
# update.googleapis.com is behind Google's edge; send a browser-ish UA (a bare urllib UA can 403).
# Match the request UA to the OS we ask the CDM for, so the Omaha call is coherent.
if sys.platform == "linux":
    _UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
else:
    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
HINT_FILE = "latest-component-updated-widevine-cdm"


def _cdm_platform():
    """(@os, os.platform, os.version, platform_subdir, cdm_filename) for the current OS. Linux ships
    libwidevinecdm.so under linux_x64 and registers via the hint file; Windows ships widevinecdm.dll
    under win_x64 and registers via the component-updater scan (see apply_widevine_launch)."""
    if sys.platform == "linux":
        return ("Linux", "Linux", "6.1.0", "linux_x64", "libwidevinecdm.so")
    return ("win", "Windows", "10.0.19045.0", "win_x64", "widevinecdm.dll")


def _cache_root():
    return os.environ.get("CLEARCOTE_WIDEVINE_DIR",
                          os.path.join(os.path.expanduser("~"), ".clearcote", "WidevineCdm"))


def _omaha_request_body():
    # Minimal Omaha v3.1 update check for the Windows x64 Widevine CDM. version=0.0.0.0 forces the
    # server to return the latest. (No requestid randomness — keep it deterministic + simple.)
    at_os, os_platform, os_version, _sub, _fn = _cdm_platform()
    return {
        "request": {
            "@os": at_os, "@updater": "clearcote",
            "acceptformat": "crx3", "protocol": "3.1",
            "arch": "x64", "nacl_arch": "x86-64", "prodversion": "149.0.0.0",
            "updaterversion": "149.0.0.0", "dedup": "cr",
            "os": {"arch": "x86_64", "platform": os_platform, "version": os_version},
            "app": [{
                "appid": WIDEVINE_APP_ID, "version": "0.0.0.0",
                "updatecheck": {}, "ping": {"r": -2},
            }],
        }
    }


def _post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
    # strip the XSSI guard prefix Google prepends
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[4:]
    return json.loads(raw)


def _parse_update(resp):
    """Pull (download_url, sha256, version) from an Omaha JSON update response. Handles both the
    newer `pipelines` shape and the classic `urls`+`manifest.packages` shape."""
    app = resp["response"]["app"][0]
    uc = app["updatecheck"]
    if uc.get("status") != "ok":
        raise RuntimeError("Widevine update check status: %r" % uc.get("status"))
    # newer pipelines format (what the live server returns)
    for pl in uc.get("pipelines", []):
        for op in pl.get("operations", []):
            urls = op.get("urls") or []
            out = op.get("out") or {}
            if urls and out.get("sha256"):
                return urls[0]["url"], out["sha256"], app.get("nextversion") or uc.get("nextversion", "")
    # classic format
    base = (uc.get("urls", {}).get("url") or [{}])[0].get("codebase")
    pkg = (uc.get("manifest", {}).get("packages", {}).get("package") or [{}])[0]
    ver = uc.get("manifest", {}).get("version", "")
    if base and pkg.get("name"):
        return base.rstrip("/") + "/" + pkg["name"], pkg.get("hash_sha256", ""), ver
    raise RuntimeError("could not find a CDM download URL in the update response")


def _crx3_to_zip(data):
    """A CRX3 file is 'Cr24' + u32 version + u32 header_len + header + zip. Return the zip bytes."""
    if data[:4] != b"Cr24":
        return data  # already a plain zip
    if len(data) < 12:
        raise RuntimeError("malformed CRX3 (truncated header)")
    header_len = struct.unpack_from("<I", data, 8)[0]
    if 12 + header_len > len(data):
        raise RuntimeError("malformed CRX3 (header overruns buffer)")
    return data[12 + header_len:]


def fetch_widevine(dest=None, quiet=False):
    """Download + verify the Windows x64 Widevine CDM into ``dest`` (default ~/.clearcote/WidevineCdm/
    <version>). Returns the versioned CDM directory (contains manifest.json +
    _platform_specific/win_x64/widevinecdm.dll). Re-fetch is skipped if already present."""
    resp = _post_json(OMAHA_URL, _omaha_request_body())
    url, sha256, version = _parse_update(resp)
    root = dest or _cache_root()
    ver_dir = os.path.join(root, version or "current")
    _at, _p, _v, sub, fn = _cdm_platform()
    dll = os.path.join(ver_dir, "_platform_specific", sub, fn)
    if os.path.isfile(dll) and os.path.isfile(os.path.join(ver_dir, "manifest.json")):
        if not quiet:
            print("[widevine] already present:", ver_dir)
        return ver_dir
    if not quiet:
        print("[widevine] fetching CDM %s" % (version or "(latest)"))
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _UA}),
                                timeout=120) as r:
        blob = r.read()
    # The CDM is a NATIVE DLL loaded into the browser process — never install it unverified. A
    # missing hash in the update response is treated as a hard failure, not a skip.
    if not sha256:
        raise RuntimeError("Widevine update response had no sha256 — refusing to install an "
                           "unverified CDM")
    if hashlib.sha256(blob).hexdigest() != sha256.lower():
        raise RuntimeError("Widevine CDM sha256 mismatch — refusing to install")
    os.makedirs(ver_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(_crx3_to_zip(blob))) as zf:
        zf.extractall(ver_dir)
    if not os.path.isfile(dll):
        # some CRX layouts nest the platform dir differently; surface a clear error
        raise RuntimeError("extracted CDM but the CDM binary was not at %s" % dll)
    if not quiet:
        print("[widevine] installed:", ver_dir)
    return ver_dir


def apply_widevine_launch(user_data_dir, kwargs, quiet=False):
    """Make ``widevine=True`` work end-to-end on a persistent context: seed the CDM into the
    profile AND fix the launch args so the engine actually registers it.

    The component updater registers the sideloaded CDM, but (1) Playwright disables it by default
    (``--disable-component-update``) and (2) it won't scan an already-installed component without
    ``--component-updater=fast-update``. So we un-suppress it and force the startup scan. Verified:
    with these, ``requestMediaKeySystemAccess('com.widevine.alpha')`` + ``createMediaKeys()`` succeed.
    Mutates ``kwargs`` in place. DRM is best-effort: any failure here degrades to a normal (no-DRM)
    launch — it must never abort ``launch_persistent_context``."""
    try:
        seed_widevine(user_data_dir, quiet=quiet)
        # Un-suppress the component updater. Playwright's ``ignore_default_args`` may be a list, or
        # the bool form (True = ignore ALL defaults, incl. --disable-component-update, so the
        # updater is already un-suppressed). Only the list/unset forms need the flag added.
        ida = kwargs.get("ignore_default_args")
        if isinstance(ida, (list, tuple)):
            ida = list(ida)
            if "--disable-component-update" not in ida:
                ida.append("--disable-component-update")
            kwargs["ignore_default_args"] = ida
        elif ida is None:
            kwargs["ignore_default_args"] = ["--disable-component-update"]
        # Force the pre-installed-component scan. A user-supplied --component-updater mode wins, but
        # if it isn't fast-update the CDM may not register — surface that instead of silently failing.
        # Force the pre-installed-component scan on WINDOWS (the component-updater path). On Linux the
        # CDM hint file we seeded IS the registration mechanism — read at startup regardless — so no
        # fast-update scan is needed there (verified: hint file + un-suppressed updater is enough).
        args = list(kwargs.get("args") or [])
        if sys.platform != "linux":
            existing = [a for a in args if "component-updater" in a]
            if not existing:
                args.append("--component-updater=fast-update")
            elif not any("fast-update" in a for a in existing) and not quiet:
                print("[widevine] note: your --component-updater mode may not register the CDM; "
                      "--component-updater=fast-update is needed to scan the pre-installed component")
        kwargs["args"] = args
    except Exception as exc:  # noqa: BLE001 — DRM is best-effort; never abort the launch
        if not quiet:
            print("[widevine] setup failed (continuing without DRM): %r" % exc)


def seed_widevine(user_data_dir, cdm_dir=None, quiet=False):
    """Make a persistent profile load the fetched CDM: copy it under <user_data_dir>/WidevineCdm/
    <version>/ and write the component hint file the engine reads. Fetches the CDM first if needed.
    No-op-safe to call on every launch."""
    src = cdm_dir or fetch_widevine(quiet=quiet)
    version = os.path.basename(src.rstrip(os.sep))
    wv_root = os.path.join(user_data_dir, "WidevineCdm")
    target = os.path.join(wv_root, version)
    _at, _p, _v, sub, fn = _cdm_platform()
    if not os.path.isfile(os.path.join(target, "_platform_specific", sub, fn)):
        import shutil
        os.makedirs(wv_root, exist_ok=True)
        shutil.copytree(src, target, dirs_exist_ok=True)
    # hint file: tells the engine where the latest component-updated CDM lives
    try:
        with open(os.path.join(wv_root, HINT_FILE), "w", encoding="utf-8") as handle:
            json.dump({"Path": target}, handle)
    except OSError:
        pass
    if not quiet:
        print("[widevine] seeded into", wv_root)
    return target
