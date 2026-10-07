"""Resolve the Clearcote browser binary: download a release, verify it, extract it to a
per-version cache, and return the chrome.exe path.

Two modes:
  * pinned (default)    — download the exact release baked into this SDK (release.py) and verify
                          its zip + chrome.exe against the SHA-256 hashes there. The hash IS the
                          trust anchor: you audit it once, in the package you installed.
  * auto_update (opt-in)— resolve the NEWEST GitHub release at runtime, verify the zip against
                          that release's published SHA256SUMS.txt, and — when a ``gpg`` binary is
                          available — verify SHA256SUMS.txt.asc against the pinned signing-key
                          fingerprint. Stay current without bumping the SDK. Falls back to the
                          pinned release if GitHub is unreachable.
A hash mismatch (either mode) is always a hard failure and the partial file is removed.

Uses only the standard library (urllib + hashlib + zipfile); GPG verification is best-effort and
only runs if a ``gpg`` executable is on PATH.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess  # noqa: S404
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile

from .release import RELEASE, REPO, SIGNING_KEY_FPR, platform_release


def _log(quiet, msg):
    if not quiet:
        sys.stderr.write(f"[clearcote] {msg}\n")
        sys.stderr.flush()


def _auto_update_requested(opt):
    if opt is not None:
        return bool(opt)
    return os.environ.get("CLEARCOTE_AUTO_UPDATE") in ("1", "true")


def _cache_root():
    env = os.environ.get("CLEARCOTE_CACHE")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "clearcote", "Cache")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), "clearcote")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clearcote")


def default_cache_root():
    """Per-OS cache root for downloaded browsers (CLEARCOTE_CACHE overrides)."""
    return _cache_root()


def _find(dirpath, name):
    name = name.lower()
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            if f.lower() == name:
                return os.path.join(root, f)
    return None


MANIFEST = ".manifest.json"

# Files Chromium cannot start without. A tree missing any of these does not fail at launch with a
# usable error — it CHECK-crashes inside the browser process before Playwright can attach, e.g. a
# missing icudtl.dat dies with "Invalid file descriptor to ICU data received" / "Check failed:
# result" and exit code 0xC0000003. Cheap to stat, so this list is checked on EVERY resolve; the
# full manifest (below) is checked once per process.
CRITICAL_FILES = {
    "win32": ["chrome.exe", "chrome.dll", "chrome_elf.dll", "icudtl.dat",
              "snapshot_blob.bin", "resources.pak"],
    "other": ["icudtl.dat", "snapshot_blob.bin", "resources.pak"],
}

_scanned = set()  # browser dirs whose full manifest already verified in this process


def _critical_names():
    return CRITICAL_FILES["win32" if sys.platform == "win32" else "other"]


def _write_manifest(base, browser_dir):
    """Snapshot every extracted file's size next to the tree, so a LATER launch can tell a healthy
    install from one that antivirus, a full disk, or an interrupted copy has since eaten."""
    files = {}
    for root, _dirs, names in os.walk(browser_dir):
        for name in names:
            path = os.path.join(root, name)
            try:
                files[os.path.relpath(path, browser_dir).replace("\\", "/")] = os.path.getsize(path)
            except OSError:
                pass
    try:
        with open(os.path.join(base, MANIFEST), "w", encoding="utf-8") as f:
            json.dump({"files": files}, f)
    except OSError:  # a manifest is an optimisation, never a reason to fail an install
        pass


def _check_names(browser_dir, names, sizes=None):
    """Return a list of "<file> — <problem>" strings for missing / truncated entries."""
    problems = []
    for name in names:
        path = os.path.join(browser_dir, name.replace("/", os.sep))
        try:
            got = os.path.getsize(path)
        except OSError:
            problems.append(f"{name} — missing")
            continue
        want = (sizes or {}).get(name)
        if want is not None and got != want:
            problems.append(f"{name} — {got:,} bytes, expected {want:,}")
        elif want is None and got == 0:
            problems.append(f"{name} — empty (0 bytes)")
    return problems


def verify_install(browser_dir, base=None, full=None):
    """Check an extracted browser tree and return a list of problems ([] when healthy).

    Always checks CRITICAL_FILES (a few stats). When ``base`` holds a manifest written at install
    time, also checks every recorded file's exact size — once per process per tree by default,
    since a full tree is ~700 files; pass ``full=True`` to force it."""
    problems = _check_names(browser_dir, _critical_names())
    if problems:
        return problems

    manifest = os.path.join(base or os.path.dirname(browser_dir), MANIFEST)
    key = os.path.normcase(os.path.abspath(browser_dir))
    if full is False or (full is None and key in _scanned) or not os.path.exists(manifest):
        return problems
    try:
        with open(manifest, encoding="utf-8") as f:
            sizes = (json.load(f) or {}).get("files") or {}
    except (OSError, ValueError):
        return problems
    problems = _check_names(browser_dir, list(sizes), sizes)
    if not problems:
        _scanned.add(key)
    return problems


def broken_install_error(browser_dir, problems, repairable=True):
    """The message a user actually needs when the tree is damaged: what is wrong, and how to fix
    it — instead of the browser CHECK-crashing during startup with an ICU stack trace."""
    shown = "\n".join(f"    {p}" for p in problems[:8])
    more = f"\n    ... and {len(problems) - 8} more" if len(problems) > 8 else ""
    fix = (
        f"Delete the folder and let Clearcote re-download it:\n"
        f"    {os.path.dirname(browser_dir)}\n"
        if repairable else
        "Re-create this browser directory from a complete copy (or unset CLEARCOTE_BINARY /\n"
        "executable_path and let Clearcote download and verify its own build).\n"
    )
    return RuntimeError(
        f"Clearcote browser install is incomplete or corrupted:\n    {browser_dir}\n"
        f"{shown}{more}\n\n"
        f"The browser cannot start without these files — it would crash during startup with an\n"
        f"ICU / 'Check failed' error before Playwright can attach.\n\n"
        f"{fix}\n"
        "Common causes: antivirus quarantined a file, the disk filled up mid-install, or the\n"
        "directory was copied by something that did not finish (e.g. a bundled app copying it out\n"
        "of a PyInstaller temp dir). Excluding the Clearcote cache directory from real-time\n"
        "antivirus scanning prevents a repeat."
    )


def check_install(exe):
    """Validate the tree around a caller-supplied binary (``executable_path`` / ``CLEARCOTE_BINARY``
    — e.g. a browser bundled into a packaged app). Raises with a clear message when it is damaged.

    Deliberately lenient about LAYOUT, strict about COMPLETENESS: a caller may point at something
    that is not a flat Chromium tree at all (installed Google Chrome keeps its DLLs in a versioned
    subfolder), and refusing to launch that would be a false alarm. So the check only engages once
    the directory looks flat — at least one non-binary payload file sits next to the exe — and then
    every other payload file is required. That is exactly the damaged-copy case: most of the tree
    present, one or two files eaten."""
    if not exe or not os.path.exists(exe):
        return  # the launcher reports a missing binary better than we can
    browser_dir = os.path.dirname(os.path.abspath(exe))
    payload = [n for n in _critical_names() if not n.lower().startswith("chrome.ex")]
    if not any(os.path.exists(os.path.join(browser_dir, n)) for n in payload):
        return  # not a flat Chromium tree (or not ours) — nothing we can meaningfully assert
    problems = verify_install(browser_dir)
    if problems:
        raise broken_install_error(browser_dir, problems, repairable=False)


def _cached(base, binary, quiet=False, repair=True):
    """The cached chrome path for an install base, or None when absent or damaged.

    A damaged tree returns None so the caller re-downloads: the ``.verified`` marker only records
    that the archive hashed correctly AT INSTALL TIME, and the files can be eaten afterwards.
    ``repair`` also moves the damaged tree out of the way; pass False when not holding the install
    lock, so only the process that will re-install touches it."""
    if not os.path.exists(os.path.join(base, ".verified")):
        return None
    browser_dir = os.path.join(base, "browser")
    cached = _find(browser_dir, binary)
    if not cached:
        return None
    problems = verify_install(browser_dir, base)
    if problems:
        if not repair:
            return None
        _log(quiet, f"cached browser is damaged ({problems[0]}) — re-downloading")
        for marker in (".verified", MANIFEST):
            try:
                os.remove(os.path.join(base, marker))
            except OSError:
                pass
        _move_aside(base, browser_dir)
        return None
    return cached


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def warm_files(dirpath):
    """Read every file in ``dirpath`` once so on-access antivirus finishes scanning the
    freshly-extracted, unsigned binaries BEFORE the browser is launched.

    Windows-only concern: launching a just-extracted ``chrome.exe`` can race the real-time AV
    scan of ``chrome_elf.dll`` (the SxS assembly member the exe's manifest depends on). If the DLL
    is still locked / being scanned at launch, Windows reports "spawn UNKNOWN" / "side-by-side
    configuration is incorrect" AND caches that negative activation context against the path, so
    every later launch from that path keeps failing. Forcing a sequential read here makes the AV
    scan happen up front and closes the race. Cheap, best-effort, and safe to call anywhere."""
    for root, _dirs, files in os.walk(dirpath):
        for name in files:
            try:
                with open(os.path.join(root, name), "rb") as fh:
                    while fh.read(1 << 20):
                        pass
            except OSError:
                pass


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "clearcote-sdk"})
    # timeout is the socket idle timeout (max wait per read), so a stalled connection fails fast
    # instead of hanging first-run launch forever. 30s for small API/text fetches.
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _download(url, dest, expected_size, quiet):
    """Stream the download to dest, hashing as we go; return the hex digest."""
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "clearcote-sdk"})
    # 60s socket idle timeout: aborts a stalled stream (no bytes for 60s) without capping the
    # total time of the large (~242 MB) binary download, since the timeout resets each read.
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        seen = 0
        last = -1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            seen += len(chunk)
            if not quiet and total:
                pct = int(seen * 100 / total)
                if pct != last and pct % 5 == 0:
                    last = pct
                    sys.stderr.write(
                        f"\r[clearcote] downloading {pct}% "
                        f"({seen // 10**6}/{total // 10**6} MB)"
                    )
                    sys.stderr.flush()
    if not quiet:
        sys.stderr.write("\n")
        sys.stderr.flush()
    return h.hexdigest()


def _parse_sums(text, asset_name, binary):
    """Pull the archive + inner-binary hashes out of a SHA256SUMS.txt body."""
    out = {}
    for raw in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+[*]?(.+)$", raw.strip())
        if not m:
            continue
        base = re.split(r"[\\/]", m.group(2))[-1]
        if base == asset_name:
            out["archive"] = m.group(1).lower()
        elif base == binary:
            out["bin"] = m.group(1).lower()
    return out


def _resolve_latest(quiet):
    """Resolve the newest non-draft GitHub release with THIS platform's asset + SHA256SUMS.txt.

    Returns a release dict (with ``unpinned=True`` and ``asc_url``/``key_url``) or None.
    """
    pin = platform_release()
    if pin is None:  # unsupported OS -> nothing to auto-resolve
        return None
    glob, binary = pin["asset_glob"], pin["binary"]
    asset_re = re.compile(rf"^clearcote-.*-{re.escape(glob)}\.(?:zip|tar\.xz)$")
    ver_re = re.compile(rf"^clearcote-(.+)-{re.escape(glob)}\.(?:zip|tar\.xz)$")
    try:
        data = json.loads(_http_get(f"https://api.github.com/repos/{REPO}/releases?per_page=30"))
    except Exception as exc:  # noqa: BLE001
        _log(quiet, f"auto-update: couldn't reach GitHub ({exc}); using pinned {RELEASE['tag']}")
        return None
    releases = sorted(
        (r for r in data if r and not r.get("draft")),
        key=lambda r: r.get("published_at") or "",
        reverse=True,
    )
    for r in releases:
        assets = r.get("assets") or []
        asset = next((a for a in assets if asset_re.match(a["name"])), None)
        sums_asset = next((a for a in assets if a["name"] == "SHA256SUMS.txt"), None)
        if not asset or not sums_asset:
            continue
        try:
            parsed = _parse_sums(
                _http_get(sums_asset["browser_download_url"]).decode("utf-8", "replace"),
                asset["name"], binary)
        except Exception:  # noqa: BLE001
            continue
        if not parsed.get("archive"):
            continue
        m = ver_re.match(asset["name"])
        asc = next((a for a in assets if a["name"] == "SHA256SUMS.txt.asc"), None)
        key = next((a for a in assets if a["name"] == "clearcote-signing-key.asc"), None)
        return {
            "tag": r["tag_name"],
            "version": m.group(1) if m else r["tag_name"],
            "asset": asset["name"],
            "url": asset["browser_download_url"],
            "sha256": parsed["archive"],
            "exe_sha256": parsed.get("bin", ""),
            "size": asset.get("size") or 0,
            "os": pin["os"],
            "archive": pin["archive"],
            "binary": binary,
            "unpinned": True,
            "asc_url": asc["browser_download_url"] if asc else None,
            "key_url": key["browser_download_url"] if key else None,
        }
    return None


def _has_gpg():
    try:
        return subprocess.run(["gpg", "--version"], capture_output=True).returncode == 0  # noqa: S603,S607
    except Exception:  # noqa: BLE001
        return False


def _gpg_verify(rel, sums_body, quiet):
    """Best-effort: import the published key into a throwaway keyring, confirm its fingerprint
    matches the pinned one, then verify SHA256SUMS.txt.asc. Returns "ok" | "skipped" | "failed".
    """
    if not rel.get("asc_url") or not rel.get("key_url"):
        return "skipped"
    if not _has_gpg():
        _log(quiet, "auto-update: gpg not found — skipping signature check (zip is still SHA-256-verified)")
        return "skipped"
    home = tempfile.mkdtemp(prefix="ccgpg-")
    key_path = os.path.join(home, "key.asc")
    sums_path = os.path.join(home, "SHA256SUMS.txt")
    asc_path = os.path.join(home, "SHA256SUMS.txt.asc")

    def gpg(*args):
        return subprocess.run(  # noqa: S603,S607
            ["gpg", "--homedir", home, "--batch", *args], capture_output=True, text=True
        )

    try:
        with open(sums_path, "w", encoding="utf-8") as f:
            f.write(sums_body)
        with open(key_path, "wb") as f:
            f.write(_http_get(rel["key_url"]))
        with open(asc_path, "wb") as f:
            f.write(_http_get(rel["asc_url"]))
        if gpg("--import", key_path).returncode != 0:
            return "failed"
        shown = gpg("--with-colons", "--fingerprint")
        fprs = [ln.split(":")[9] for ln in shown.stdout.splitlines() if ln.startswith("fpr:")]
        if SIGNING_KEY_FPR not in fprs:
            _log(quiet, f"auto-update: signing key fingerprint mismatch (expected {SIGNING_KEY_FPR})")
            return "failed"
        return "ok" if gpg("--verify", asc_path, sums_path).returncode == 0 else "failed"
    except Exception:  # noqa: BLE001
        return "failed"
    finally:
        shutil.rmtree(home, ignore_errors=True)


# Install lock. One protocol, the same in Python (download.py), Node (download.ts) and .NET
# (Download.cs) -- change all three together. Any mix of processes installing one build into
# <cache>/<tag> takes turns:
#  1. The lock is the file <tag>/.install-lock, created with O_CREAT|O_EXCL (atomic on every OS and in
#     all three languages; Node has no flock). It holds JSON: pid, host, boot, pidns, nonce, sdk, created.
#  2. The holder touches the file (mtime) every 5 s while it works.
#  3. Waiters poll every 0.25 s. The lock is stale when its record is from this machine (same host, boot
#     id and PID namespace) and that pid is gone, when its mtime is over 120 s old, or when it is still
#     not valid JSON 10 s after it was written. A stale lock is broken while holding
#     <tag>/.install-lock.break (also O_EXCL; one older than 30 s is removed): re-check that it is still
#     stale, delete it, delete the breaker.
#  4. The holder re-checks the cache and uses a verified build if one is there now. Otherwise it downloads
#     and extracts into <tag>/.tmp-<nonce>/, moves the tree to <tag>/browser, writes .manifest.json then
#     .verified, and removes the temp dir. A verified tree is never deleted or written over; a damaged
#     one is moved aside (<tag>/.trash-*) by the holder only.
#  5. Release deletes the lock only while it still holds our nonce. Waiting gives up after 30 min with an
#     error that names the holder and the lock file.
INSTALL_LOCK = ".install-lock"
_LOCK_POLL = 0.25
_LOCK_HEARTBEAT = 5.0
_LOCK_STALE = 120.0
_LOCK_UNREADABLE = 10.0
_LOCK_BREAKER_STALE = 30.0
INSTALL_WAIT = 30 * 60.0


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _pid_namespace():
    try:
        return os.readlink("/proc/self/ns/pid")
    except (OSError, AttributeError, NotImplementedError, ValueError):
        return None


def _pid_alive(pid):
    if not 0 < pid < 2**31:
        return False
    if os.name == "nt":  # os.kill(pid, 0) would TERMINATE the process on Windows
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock(path):
    """("gone" | "busy" | "ok" | "invalid", record or None, mtime). "busy": cannot be read right now."""
    try:
        mtime = os.stat(path).st_mtime
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "gone", None, None
    except OSError:
        return "busy", None, None
    try:
        rec = json.loads(raw.decode("utf-8"))
    except ValueError:
        return "invalid", None, mtime
    return ("ok", rec, mtime) if isinstance(rec, dict) else ("invalid", None, mtime)


def _lock_stale(rec, mtime):
    age = time.time() - mtime
    if rec is None:
        return age > _LOCK_UNREADABLE
    pid = rec.get("pid")
    host = rec.get("host")
    if (isinstance(host, str) and host.lower() == socket.gethostname().lower()
            and rec.get("boot") == _boot_id() and rec.get("pidns") == _pid_namespace()
            and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            and not _pid_alive(pid)):
        return True
    return age > _LOCK_STALE


def _break_stale_lock(path):
    """Delete a stale lock, under the breaker file. True when the lock is gone afterwards."""
    breaker = path + ".break"
    try:
        os.close(os.open(breaker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except OSError:
        try:
            if time.time() - os.stat(breaker).st_mtime > _LOCK_BREAKER_STALE:
                os.remove(breaker)  # its owner died between taking and removing it
        except OSError:
            pass
        return False
    try:
        state, rec, mtime = _read_lock(path)
        if state in ("ok", "invalid") and _lock_stale(rec, mtime):
            os.remove(path)
            state = "gone"
        return state == "gone"
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.remove(breaker)


_SDK_NAMES = {"python": "Python", "node": "Node", "dotnet": ".NET"}


def _lock_holder(rec):
    if not rec or not rec.get("pid"):
        return "a process that left no details"
    sdk = _SDK_NAMES.get(rec.get("sdk"), str(rec.get("sdk") or "unknown"))
    return f"process {rec.get('pid')} on {rec.get('host') or 'an unknown machine'} (Clearcote {sdk} SDK)"


def _wait_words(seconds):
    n, unit = (max(1, round(seconds)), "second") if seconds < 120 else (round(seconds / 60), "minute")
    return f"{n} {unit}{'' if n == 1 else 's'}"


class _HeldLock:
    """The install lock while this process holds it: heartbeat thread, ownership check, release."""

    def __init__(self, path, nonce):
        self.path, self.nonce = path, nonce
        self._lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat_loop, name="clearcote-install-lock", daemon=True)
        self._thread.start()

    def held(self):
        """False once the lock file no longer carries our nonce (another process judged it stale)."""
        if self._lost:
            return False
        state, rec, _ = _read_lock(self.path)
        if state != "busy" and (rec or {}).get("nonce") != self.nonce:
            self._lost = True
        return not self._lost

    def beat(self):
        if self.held():
            with contextlib.suppress(OSError):
                os.utime(self.path, None)

    def _beat_loop(self):
        while not self._stop.wait(_LOCK_HEARTBEAT):
            self.beat()

    def release(self):
        self._stop.set()
        self._thread.join(5)
        for _ in range(40):  # another process may be reading the file this instant (Windows refuses the delete)
            state, rec, _ = _read_lock(self.path)
            if state == "gone" or (state != "busy" and (rec or {}).get("nonce") != self.nonce):
                return
            if state == "ok":
                try:
                    os.remove(self.path)
                    return
                except FileNotFoundError:
                    return
                except OSError:
                    pass
            time.sleep(0.05)


def _acquire_install_lock(base, timeout=None, quiet=True):
    """Wait for, then take, the install lock of build directory ``base`` (see the protocol above)."""
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, INSTALL_LOCK)
    nonce = uuid.uuid4().hex
    body = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "boot": _boot_id(),
                       "pidns": _pid_namespace(), "nonce": nonce, "sdk": "python",
                       "created": int(time.time() * 1000)}).encode("utf-8")
    wait = INSTALL_WAIT if timeout is None else timeout
    deadline = time.monotonic() + wait
    told = False
    refused = 0
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
        except FileExistsError:
            fd = None
        except PermissionError:  # Windows: the previous lock file is still being deleted
            if os.name != "nt":
                raise
            fd = None
            refused += 1
        if fd is not None:
            try:
                os.write(fd, body)
            except OSError:
                os.close(fd)
                with contextlib.suppress(OSError):
                    os.remove(path)
                raise
            os.close(fd)
            return _HeldLock(path, nonce)
        state, rec, mtime = _read_lock(path)
        if state != "gone":
            refused = 0
        elif refused > 40:  # no lock file, yet we may not create one: not a race, a real error
            raise PermissionError(f"Clearcote cannot create {path}: permission denied")
        if state in ("ok", "invalid") and _lock_stale(rec, mtime) and _break_stale_lock(path):
            _log(quiet, f"removed an abandoned install lock ({_lock_holder(rec)})")
            continue
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Gave up after {_wait_words(wait)} waiting for another program to finish installing the "
                f"Clearcote browser in\n    {base}\n"
                f"The other installer is {_lock_holder(rec)}. If nothing else is installing it, delete this "
                f"file and try again:\n    {path}")
        if not told and state == "ok":
            _log(quiet, f"another program is installing this browser build ({_lock_holder(rec)}); waiting for it")
            told = True
        time.sleep(_LOCK_POLL)


@contextlib.contextmanager
def _install_lock(base, quiet=True, timeout=None):
    """One installer per build directory at a time, across processes, SDKs and containers that share a
    cache volume (the macOS Docker launch mounts one named volume into every licensed container). Without
    it, two first launches both downloaded into ``base`` and one deleted files the other was running."""
    lock = _acquire_install_lock(base, timeout, quiet)
    try:
        yield lock
    finally:
        lock.release()


class _LockLost(Exception):
    """Another process judged our install lock stale and took over."""


def _remove_tree(path):
    shutil.rmtree(path, ignore_errors=True)


def _rename_dir(src, dst):
    for attempt in range(10):
        try:
            os.rename(src, dst)
            return
        except PermissionError:  # Windows: a scanner can hold a file in the fresh tree for a moment
            if os.name != "nt" or attempt == 9:
                raise
            time.sleep(0.2)


def _move_aside(base, browser_dir):
    """Move a tree that must be replaced out of the way, then delete it if nothing is using it."""
    trash = os.path.join(base, ".trash-" + uuid.uuid4().hex[:12])
    try:
        _rename_dir(browser_dir, trash)
    except FileNotFoundError:
        return
    except OSError as e:
        raise RuntimeError(
            f"Clearcote cannot replace the browser files in\n    {browser_dir}\n"
            f"because another program is still using them ({e}).\n"
            "Close every program that uses this Clearcote browser, then try again.") from None
    _remove_tree(trash)  # what is still in use stays until the next install sweeps it


def _sweep_leftovers(base, asset):
    """Remove what an install that stopped part-way left behind. Only called by the lock holder."""
    with contextlib.suppress(OSError):
        for name in os.listdir(base):
            if name == ".incoming" or name.startswith((".tmp-", ".trash-")):
                _remove_tree(os.path.join(base, name))
    with contextlib.suppress(OSError):
        os.remove(os.path.join(base, asset))  # where earlier versions downloaded the archive


def _fetch_and_verify(rel, base, quiet):
    """Download + verify a resolved release into ``base``; return the extracted chrome.exe path.

    Holds ``base``'s install lock: a second process that missed the cache at the same moment waits, then
    uses the tree the first one finished instead of downloading over it."""
    binary = rel.get("binary", "chrome.exe")
    for _ in range(3):
        with _install_lock(base, quiet) as lock:
            cached = _cached(base, binary, quiet)
            if cached:
                _log(quiet, f"installed by another process meanwhile: {cached}")
                return cached
            try:
                return _fetch_and_verify_unlocked(rel, base, quiet, lock)
            except Exception:
                if lock.held():
                    raise
            _log(quiet, "another process took over this install; waiting for it")
    raise RuntimeError(f"Clearcote could not install {rel['tag']}: other processes kept taking over the install in {base}")


def _fetch_and_verify_unlocked(rel, base, quiet, lock=None):
    """Download + verify a resolved release into ``base``; return the extracted chrome.exe path.

    The caller holds the install lock (``lock``). Everything is written under ``base/.tmp-<nonce>``
    first, and the finished tree is moved into place only while the lock is still ours."""
    os.makedirs(base, exist_ok=True)
    _sweep_leftovers(base, rel["asset"])
    tmp = os.path.join(base, ".tmp-" + (lock.nonce if lock else uuid.uuid4().hex))
    os.makedirs(tmp)
    try:
        return _install_into(rel, base, tmp, quiet, lock)
    finally:
        _remove_tree(tmp)


def _install_into(rel, base, tmp, quiet, lock):
    browser_dir = os.path.join(base, "browser")
    zip_path = os.path.join(tmp, rel["asset"])

    tail = ", latest" if rel.get("unpinned") else ""
    _log(quiet, f"fetching Clearcote {rel['version']} ({rel['tag']}{tail}, "
                f"~{(rel.get('size') or 0) // 10**6} MB)")
    got = _download(rel["url"], zip_path, rel.get("size"), quiet)

    _log(quiet, "verifying SHA-256")
    if got.lower() != rel["sha256"].lower():
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise RuntimeError(
            "Clearcote archive SHA-256 mismatch — refusing to use it.\n"
            f"  expected {rel['sha256']}\n  got      {got}"
        )

    # For an auto-resolved (un-pinned) release, also confirm authenticity via the signed checksum file.
    if rel.get("unpinned") and rel.get("asc_url"):
        try:
            sums_body = _http_get(
                f"https://github.com/{REPO}/releases/download/{rel['tag']}/SHA256SUMS.txt"
            ).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            sums_body = ""
        if sums_body:
            verdict = _gpg_verify(rel, sums_body, quiet)
            if verdict == "failed":
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Clearcote {rel['tag']}: GPG signature verification FAILED against the pinned "
                    f"key {SIGNING_KEY_FPR} — refusing to use it."
                )
            if verdict == "ok":
                _log(quiet, f"auto-update: GPG signature OK (key {SIGNING_KEY_FPR})")

    _log(quiet, "extracting")
    # Extract next to the target, then move the finished tree into place, so `browser/` only ever
    # appears once fully written (no partial tree a concurrent launch could pick up), and — on
    # Windows — we can force an on-access AV scan of the finished tree before any launch (below).
    incoming = os.path.join(tmp, "browser")
    if rel["asset"].endswith(".tar.xz") or rel.get("archive") == "tar.xz":
        import tarfile
        with tarfile.open(zip_path) as t:
            t.extractall(incoming)
    else:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(incoming)
    with contextlib.suppress(OSError):
        os.remove(zip_path)  # reclaim disk; keep only the extracted tree

    binary = rel.get("binary", "chrome.exe")
    exe = _find(incoming, binary)
    if not exe:
        raise RuntimeError(f"Clearcote archive verified but {binary} was not found inside it.")

    if rel.get("exe_sha256"):
        exe_hash = _sha256_file(exe)
        if exe_hash.lower() != rel["exe_sha256"].lower():
            raise RuntimeError(
                f"Clearcote {binary} SHA-256 mismatch — refusing to use it.\n"
                f"  expected {rel['exe_sha256']}\n  got      {exe_hash}"
            )

    if sys.platform != "win32":
        # Make the launcher executable (tar preserves 0755, but be defensive) + a best-effort setuid
        # on the sandbox helper. The setuid bit only takes effect if chrome-sandbox is root-owned; in
        # containers/non-root, pass --no-sandbox (see docs). We never require root here.
        try:
            os.chmod(exe, 0o755)
        except OSError:
            pass
        sandbox = os.path.join(os.path.dirname(exe), "chrome-sandbox")
        if os.path.exists(sandbox):
            try:
                os.chmod(sandbox, 0o4755)
            except OSError:
                pass

    # Move the tree into place only while the lock is still ours. The caller found no verified tree
    # under the lock, so whatever sits at browser/ is an install that stopped part-way.
    if lock is not None:
        lock.beat()
        if not lock.held():
            raise _LockLost()
    for marker in (".verified", MANIFEST):
        with contextlib.suppress(OSError):
            os.remove(os.path.join(base, marker))
    if os.path.lexists(browser_dir):
        _move_aside(base, browser_dir)
    _rename_dir(incoming, browser_dir)
    exe = os.path.join(browser_dir, os.path.relpath(exe, incoming))

    if sys.platform == "win32":
        # Pre-scan the whole tree so real-time AV finishes with the freshly-extracted binaries
        # before the first launch — closes the chrome_elf.dll scan race that otherwise poisons the
        # path (see warm_files). One-time cost on install; later cached launches skip it.
        warm_files(browser_dir)

    # Record the finished tree BEFORE the .verified marker, so a launch never sees "verified" with
    # no manifest to check it against.
    _write_manifest(base, browser_dir)
    with open(os.path.join(base, ".verified"), "w", encoding="utf-8") as f:
        f.write(rel["sha256"] + "\n")
    _log(quiet, f"ready: {exe}")
    return exe


def _plat_key():
    """Catalog platform key for this OS (matches the /download/pro `platform` param)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return None


def _ver_key(v):
    """Sortable tuple from a version string, e.g. '150.0.7871.115' -> (150, 0, 7871, 115)."""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _fetch_catalog(quiet=False):
    """The public version catalog (source of truth for which majors exist + each build's tier).
    Falls back to the bundled snapshot when the live catalog is unreachable."""
    from .release import CATALOG_FALLBACK, CATALOG_URL

    try:
        data = json.loads(_http_get(CATALOG_URL).decode("utf-8"))
        if isinstance(data, dict) and data.get("builds"):
            return data
    except Exception as exc:  # noqa: BLE001
        _log(quiet, f"version catalog unreachable ({exc}); using the bundled snapshot")
    return CATALOG_FALLBACK


def _catalog_available(catalog, plat):
    return ", ".join(
        f"{b.get('version')} ({b.get('tier', 'free')})"
        for b in catalog.get("builds", [])
        if plat in (b.get("platforms") or {})
    ) or "none"


def is_pro_revision_selector(selector):
    """True when the selector pins a specific PRO REBUILD, e.g. "r7" or "150.0.7871.114-r7".

    Revisions are the same Chromium version rebuilt, so they never appear in the public version
    catalog — resolve_version would reject them. They are PRO-only (the free build has no
    revisions). The download route (which knows PRO_CATALOG_JSON) does the real resolution; the
    SDK just recognises the shape and routes straight to the licensed PRO download."""
    return bool(re.search(r"(?:^|-)r\d+$", str(selector or "").strip(), re.IGNORECASE))


def resolve_version(selector, has_license=False, quiet=False, catalog=None):
    """Resolve a version selector against the public catalog, VALIDATING that it exists (and is
    reachable) BEFORE any download, so a bad request fails fast with a helpful message instead of
    getting stuck.

    ``selector`` may be a bare major ("150"), an exact version ("150.0.7871.115"), or "latest".
    Returns ``("free", rel_dict)`` (download via ``_fetch_and_verify``) or ``("pro", version_str)``
    (download via ``pro_ensure_binary(version=...)``). Raises ``ValueError`` when the version does
    not exist for this OS, or when it is a PRO build and ``has_license`` is False.
    """
    plat = _plat_key()
    if plat is None:
        raise RuntimeError("Clearcote ships Windows x64 and Linux x64 only.")
    catalog = catalog or _fetch_catalog(quiet)  # a given catalog (the bundled one): no network
    builds = [b for b in catalog.get("builds", []) if plat in (b.get("platforms") or {})]
    sel = str(selector or "").strip()

    if sel.lower() in ("latest", "newest"):
        # newest build the caller can actually use (free always; pro only when licensed)
        cands = [b for b in builds if b.get("tier", "free") == "free" or has_license]
    elif re.fullmatch(r"\d+", sel):  # bare major -> newest of that major
        cands = [b for b in builds if str(b.get("major")) == sel]
    else:  # exact version
        cands = [b for b in builds if b.get("version") == sel]

    if not cands:
        raise ValueError(
            f"No Clearcote build matches version {selector!r} for {plat}. "
            f"Available: {_catalog_available(catalog, plat)}."
        )
    pick = max(cands, key=lambda b: _ver_key(b.get("version", "0")))
    tier = pick.get("tier", "free")

    if tier == "pro" and not has_license:
        free = [b["version"] for b in builds if b.get("tier", "free") == "free"]
        raise ValueError(
            f"Clearcote {pick['version']} is a PRO build and isn't public yet — set a license key "
            f"(CLEARCOTE_LICENSE_KEY, or pass license_key=...) to use it.\n"
            f"  Free versions you can use without a key: {', '.join(free) or 'none'}."
        )
    if tier == "pro":
        return ("pro", pick["version"])

    p = pick["platforms"][plat]
    if not p.get("url") or not p.get("sha256"):
        raise ValueError(
            f"Clearcote {pick['version']} is marked free but the catalog has no download for {plat}."
        )
    rel = {
        "tag": pick.get("tag") or f"v-{pick['version']}",
        "version": pick["version"],
        "asset": p["asset"],
        "url": p["url"],
        "sha256": p["sha256"],
        "exe_sha256": p.get("exe_sha256"),
        "size": p.get("size"),
        "archive": p.get("archive"),
        "binary": p.get("binary", "chrome"),
        "unpinned": False,  # catalog sha256 is the trust anchor -> sha256-only verify, like a pin
    }
    return ("free", rel)


def resolved_engine_version(selector, has_license=False, quiet=True):
    """Best-effort: the browser build this launch will actually run, as a version string, for
    lease TELEMETRY only. Never raises (a launch must never fail over telemetry).

    An exact "X.Y.Z.W" selector is returned as-is (no network). A bare major / "latest" / None is
    resolved against the public catalog — None maps to the newest build the caller can use, which
    is the same default the binary path picks (newest free, or newest pro when licensed). Any
    failure (catalog unreachable, unknown selector) falls back to the pinned ``RELEASE`` version."""
    try:
        sel = str(selector or "").strip()
        if re.fullmatch(r"\d+(?:\.\d+){3}", sel):  # exact build -> no catalog round-trip
            return sel
        if is_pro_revision_selector(sel):
            # "150.0.7871.114-r7" -> the version; bare "r7" -> the pinned baseline version.
            m = re.match(r"^(\d+(?:\.\d+){3})-r\d+$", sel, re.IGNORECASE)
            return m.group(1) if m else str(RELEASE["version"])
        kind, payload = resolve_version(sel or "latest", has_license=has_license, quiet=quiet)
        return payload if kind == "pro" else payload.get("version")
    except Exception:  # noqa: BLE001
        return str(RELEASE["version"])


def ensure_binary(cache_dir=None, quiet=False, auto_update=None):
    """Ensure the Clearcote binary is present and verified; return the chrome.exe path.

    Cached per release tag, so later calls are instant. Set ``auto_update=True`` (or the env var
    ``CLEARCOTE_AUTO_UPDATE=1``) to resolve and download the latest GitHub release instead of the
    version pinned into this SDK.
    """
    cache_root = cache_dir or _cache_root()

    rel = None
    if _auto_update_requested(auto_update):
        latest = _resolve_latest(quiet)
        if latest and latest["tag"] == RELEASE["tag"]:
            rel = dict(RELEASE, unpinned=False)  # newest IS pinned — use the audited baked-in hashes
        else:
            rel = latest or dict(RELEASE, unpinned=False)
    else:
        rel = dict(RELEASE, unpinned=False)

    base = os.path.join(cache_root, rel["tag"])
    cached = _cached(base, rel.get("binary", "chrome.exe"), quiet, repair=False)
    if cached:
        return cached
    return _fetch_and_verify(rel, base, quiet)


RELEASE_CHANNELS = ("stable", "preview")


def resolve_release_channel(explicit=None, env=None):
    """Resolve the PRO release channel: explicit option > CLEARCOTE_RELEASE_CHANNEL > "stable".

    Unknown values raise ValueError, so a typo never silently selects a different build."""
    env = os.environ if env is None else env
    source = explicit if explicit is not None else env.get("CLEARCOTE_RELEASE_CHANNEL")
    raw = str(source or "").strip().lower()
    if not raw or raw == "stable":
        return "stable"
    if raw == "preview":
        return "preview"
    raise ValueError(f"Unknown release channel '{source}'. Use \"stable\" or \"preview\".")


def pro_download_url(base_url, plat, version=None, channel="stable"):
    """The PRO download URL for a platform, version selector and channel."""
    import urllib.parse

    url = f"{str(base_url).rstrip('/')}/api/v1/download/pro?platform={plat}"
    if version:  # request a specific PRO major/version; server returns the newest match
        url += f"&version={urllib.parse.quote(str(version), safe='')}"
    # An exact pin overrides the channel on the server; sending it anyway lets the server report
    # which channel resolved. Omitted for stable so older servers see an unchanged request.
    if channel == "preview":
        url += "&channel=preview"
    return url


def list_cached_builds(cache_dir=None):
    """Verified browser builds already in the cache, newest first (by directory mtime).

    Returns ``[{"tag": ..., "path": ...}]``. Never downloads and never deletes: unlike the resolve
    path, a damaged entry is simply left out of the list."""
    root = cache_dir or _cache_root()
    if not os.path.isdir(root):
        return []
    binary = "chrome.exe" if sys.platform == "win32" else "chrome"
    out = []
    for name in os.listdir(root):
        base = os.path.join(root, name)
        if not os.path.isdir(base) or not os.path.exists(os.path.join(base, ".verified")):
            continue
        browser_dir = os.path.join(base, "browser")
        exe = _find(browser_dir, binary) if os.path.isdir(browser_dir) else None
        if not exe:
            continue
        out.append((os.path.getmtime(base), name, exe))
    out.sort(key=lambda t: t[0], reverse=True)
    return [{"tag": name, "path": exe} for _m, name, exe in out]


def pro_ensure_binary(license_key, api_base=None, cache_dir=None, quiet=False, version=None,
                      release_channel=None):
    """Download + verify the PRO (license-gated) browser and return its chrome path.

    The PRO build is not on a public releases page: the SDK asks the site for it via
    ``GET /api/v1/download/pro`` with the license key, gets back an unguessable blob
    URL + sha256, then reuses the SAME verify+extract path as the free binary
    (``_fetch_and_verify``, sha256-only — no GPG). Cached per PRO tag. Raises on any
    failure — a licensed caller must get the PRO build, never a silent free fall-back.

    ``release_channel="preview"`` (or CLEARCOTE_RELEASE_CHANNEL=preview) gets the newest build for
    this platform, preview or stable. An exact version pin overrides the channel.
    """
    import urllib.error

    base_url = (api_base or os.environ.get("CLEARCOTE_LICENSE_API")
                or "https://www.clearcotelabs.com").rstrip("/")
    plat = ("windows" if sys.platform.startswith("win")
            else "linux" if sys.platform.startswith("linux") else None)
    if plat is None:
        raise RuntimeError("Clearcote PRO ships Windows x64 and Linux x64 only.")

    channel = resolve_release_channel(release_channel)
    url = pro_download_url(base_url, plat, version, channel)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {license_key}", "User-Agent": "clearcote-sdk"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            meta = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(
            f"Clearcote PRO download not authorized (HTTP {e.code}): {body}\n"
            "Check your license key and that your plan is active.") from None

    resolved = meta.get("resolved_channel")
    if channel == "preview" and resolved and resolved != "preview":
        _log(quiet, f"release channel: preview requested, no newer preview for {plat} -> "
                    f"{resolved} {meta.get('tag') or ''}".strip())
    if not meta.get("url") or not meta.get("sha256"):
        raise RuntimeError(
            f"Clearcote PRO build is not currently available for {plat} "
            "(the server returned no download).")

    rel = {
        "tag": meta.get("tag") or f"pro-{meta.get('version', '')}",
        "version": meta.get("version", ""),
        "url": meta["url"],
        "sha256": meta["sha256"],
        "exe_sha256": meta.get("exe_sha256"),
        "asset": meta["asset"],
        "archive": meta.get("archive"),
        "binary": meta.get("binary", "chrome.exe"),
        "size": meta.get("size"),
        "unpinned": False,  # pinned -> sha256-only verify (no GPG), like the free pin
    }
    dst = os.path.join(cache_dir or _cache_root(), rel["tag"])
    cached = _cached(dst, rel["binary"], quiet, repair=False)
    if cached:
        return cached
    return _fetch_and_verify(rel, dst, quiet)
