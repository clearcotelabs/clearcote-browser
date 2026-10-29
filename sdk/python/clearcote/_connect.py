"""``clearcote connect [CODE]``: start the Clearcote Helper, so the web profile manager at
https://app.clearcotelabs.com can use this computer.

The helper is its own open-source, reproducible program (clearcote-profile-manager/helper). This does
not copy it: it downloads the release pinned below, checks the archive against the pinned SHA-256
(the same hashes as that release's SHA256SUMS), unpacks it once into the cache, and runs it with the
Node.js runtime that ships inside it (so nothing else is needed). The CODE is the one the web page
shows: started with it, the helper pairs that page without asking again (typing it here is the
approval).

Kept field-for-field in step with HELPER in the Node SDK's connect.ts (its test/connect.test.ts checks).
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import zipfile

# The helper release this SDK runs.
HELPER = {
    "version": "0.27.2",
    "base_url": "https://github.com/clearcotelabs/clearcote-profile-manager/releases/download/helper-v0.27.2/",
    "assets": {
        "win-x64": {"asset": "clearcote-helper-0.27.2-win-x64.zip", "sha256": "7266cebab53a00f0951b7be4dc2300281545aa85453a2b60babad1e3c3350d8c", "size": 36275012},
        "win-arm64": {"asset": "clearcote-helper-0.27.2-win-arm64.zip", "sha256": "3dc4aafb0fc855b327a5957c567c8e314509ae6c5327d5ed3fb0bcd1c037c396", "size": 32319625},
        "mac-x64": {"asset": "clearcote-helper-0.27.2-mac-x64.tar.gz", "sha256": "b2cbbc693dea7b5d75273832a8c93b07d46ec7af62c98d9b3b840af316fa48ec", "size": 41675370},
        "mac-arm64": {"asset": "clearcote-helper-0.27.2-mac-arm64.tar.gz", "sha256": "0090c7e662e454c0ba40f86df4ec731af29307091fea91d9459c09e21e2b2211", "size": 40573627},
        "linux-x64": {"asset": "clearcote-helper-0.27.2-linux-x64.tar.gz", "sha256": "5ff5086b424f17fd934b9756c4b90be5847ee6dbfef614d0bb537d087bd468f5", "size": 45835332},
        "linux-arm64": {"asset": "clearcote-helper-0.27.2-linux-arm64.tar.gz", "sha256": "04e4992f80dfda3fb57451c8272eb4532c11e0cba40b8713654986b2b6d42976", "size": 45780118},
    },
}

# Where the web profile manager lives.
APP_URL = "https://app.clearcotelabs.com"


def helper_platform(system=None, machine=None):
    """``"win-x64"`` etc. for this machine, or None where there is no helper build."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    os_name = "win" if system.startswith("win") else "mac" if system == "darwin" else "linux" if system.startswith("linux") else None
    cpu = "x64" if machine in ("x86_64", "amd64", "x64") else "arm64" if machine in ("arm64", "aarch64") else None
    return f"{os_name}-{cpu}" if os_name and cpu else None


def normalize_pair_code(code):
    """The code as the web page shows it (``"4F7-K2Q"``), or None when it cannot be one."""
    c = re.sub(r"[\s-]", "", str(code or "")).upper()
    if not re.fullmatch(r"[2-9A-HJKMNP-Z]{6}", c):
        return None
    return f"{c[:3]}-{c[3:]}"


def helper_files(dirpath, plat, version=None):
    """The unpacked helper's runtime and program: ``{"node": ..., "script": ...}``."""
    top = os.path.join(dirpath, f"clearcote-helper-{version or HELPER['version']}-{plat}")
    root = top if os.path.isdir(top) else dirpath  # CLEARCOTE_HELPER_DIR may point inside the folder already
    return {
        "node": os.path.join(root, "runtime", "node.exe" if plat.startswith("win") else "node"),
        "script": os.path.join(root, "lib", "clearcote-helper.js"),
    }


def ensure_helper(cache_root=None, quiet=False, helper=None, plat=None):
    """The helper on disk, downloading and checking it the first time. ``CLEARCOTE_HELPER_DIR`` uses
    an unpacked helper you already have instead (nothing is downloaded or checked)."""
    from .download import _download, default_cache_root

    plat = plat or helper_platform()
    if not plat:
        raise RuntimeError(f"There is no Clearcote Helper for {sys.platform}-{platform.machine()}.")
    own = os.environ.get("CLEARCOTE_HELPER_DIR")
    if own:
        f = helper_files(own, plat)
        if not os.path.isfile(f["script"]):
            raise RuntimeError(f"CLEARCOTE_HELPER_DIR: no lib/clearcote-helper.js in {own}")
        return f
    helper = helper or HELPER
    a = helper["assets"].get(plat)
    if not a or not a.get("sha256"):
        raise RuntimeError(f"This SDK has no Clearcote Helper pinned for {plat}.")
    dirpath = os.path.join(cache_root or default_cache_root(), "helper", f"{helper['version']}-{plat}")
    if os.path.isfile(os.path.join(dirpath, ".verified")):
        return helper_files(dirpath, plat, helper["version"])

    os.makedirs(os.path.dirname(dirpath), exist_ok=True)
    tmp = f"{dirpath}.tmp-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        archive = os.path.join(tmp, a["asset"])
        if not quiet:
            sys.stderr.write(f"[clearcote] downloading Clearcote Helper {helper['version']} ({a['asset']})\n")
        got = _download(helper["base_url"] + a["asset"], archive, a["size"], quiet)
        if got != a["sha256"]:
            raise RuntimeError(f"Clearcote Helper download does not match its pinned SHA-256 (got {got}, "
                               f"want {a['sha256']}). Nothing was run.")
        out = os.path.join(tmp, "x")
        os.makedirs(out)
        if a["asset"].endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                z.extractall(out)
        else:
            # tarfile keeps the runtime's exec bit, which zipfile would not.
            with tarfile.open(archive, "r:gz") as t:
                if hasattr(tarfile, "data_filter"):
                    t.extractall(out, filter="data")
                else:
                    t.extractall(out)  # noqa: S202 -- checked against the pinned SHA-256 first
        os.remove(archive)
        with open(os.path.join(out, ".verified"), "w", encoding="utf-8") as fh:
            fh.write(a["sha256"] + "\n")
        shutil.rmtree(dirpath, ignore_errors=True)
        os.replace(out, dirpath)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return helper_files(dirpath, plat, helper["version"])


def _open_url(url):
    """Open a URL in the default browser; best-effort."""
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 -- the person opens it themselves
        pass


def connect(code=None, port=None, allow_origins=(), no_open=False):
    """Run the helper in this terminal until it exits (Ctrl+C closes its browsers first). Returns its
    exit code."""
    if code is not None:
        c = normalize_pair_code(code)
        if not c:
            raise ValueError(f'"{code}" is not a code the profile manager shows (it looks like 4F7-K2Q).')
        code = c
    files = ensure_helper()
    args = [files["node"], files["script"]]
    if port:
        args += ["--port", str(port)]
    for o in allow_origins or ():
        args += ["--allow-origin", o]
    if no_open:
        args.append("--no-open")

    if code:
        sys.stderr.write(f"[clearcote] starting the helper with code {code}. "
                         "The profile manager tab showing it connects by itself.\n")
    else:
        sys.stderr.write(f'[clearcote] starting the helper. In {APP_URL} press "Connect this computer", '
                         "then approve the pairing.\n")
        if not no_open:
            _open_url(APP_URL)
    sys.stderr.flush()
    env = dict(os.environ)
    if code:
        env["CLEARCOTE_HELPER_PAIR_CODE"] = code
    else:
        env.pop("CLEARCOTE_HELPER_PAIR_CODE", None)

    child = subprocess.Popen(args, env=env)  # noqa: S603 -- our own pinned, checked helper
    # Ctrl+C reaches the helper too (same console): it closes its browsers and exits, and so do we.
    # A SIGTERM sent to us alone is passed on (POSIX; on Windows it would not let it stop cleanly).
    old = {}

    def _ignore(*_a):
        pass

    def _forward(signum, _frame):
        if sys.platform != "win32":
            child.send_signal(signum)

    for name, fn in (("SIGINT", _ignore), ("SIGBREAK", _ignore), ("SIGTERM", _forward), ("SIGHUP", _forward)):
        s = getattr(signal, name, None)
        if s is not None:
            try:
                old[s] = signal.signal(s, fn)
            except (OSError, ValueError):
                pass
    try:
        while True:
            try:
                return child.wait()
            except KeyboardInterrupt:
                continue
    finally:
        for s, h in old.items():
            try:
                signal.signal(s, h)
            except (OSError, ValueError):
                pass
