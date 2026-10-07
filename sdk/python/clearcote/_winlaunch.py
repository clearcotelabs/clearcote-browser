"""Windows launch helpers: a no-run spawn probe, one reusable recovered copy per build, and removal
of the temp directories a launch that never started leaves behind.

Why a cached build can refuse to start in place ("spawn UNKNOWN", WinError 14001: "the side-by-side
configuration is incorrect"): chrome.exe's manifest names a private assembly, the
``<version>.manifest`` beside it, and Windows resolves that assembly against the file system as the
system sees it. A process running inside an MSIX-packaged app (and everything it starts) has its
writes to %LOCALAPPDATA% redirected into that package's private store, so a build it downloaded into
the cache is visible to the process but not to that check, and every launch from the cache fails.
%TEMP% and the home directory are not redirected. Real-time antivirus still scanning a freshly
extracted chrome_elf.dll can produce the same error for a while.

What used to happen: three in-place launches through Playwright, then a fresh ~400 MB copy of the
browser in %TEMP%\\clearcote-recover-<random> that nothing deleted. Each failed Playwright launch also
leaked its two temp directories (``playwright_chromiumdev_profile-*`` and ``playwright-artifacts-*``):
Node's ``spawn`` throws synchronously on that error, before Playwright registers their cleanup.

Now: the copy lives at ``~/.clearcote/recovered/<build>-<hash>/browser`` and is reused by every later
launch of the same build (the Node and Python SDKs share it), the in-place attempts are probed with a
suspended CreateProcess instead of a Playwright launch, and an attempt that still fails has exactly
its own temp directories removed. ``clearcote clear-cache`` deletes the recovered copies too.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

MARKER = ".clearcote-recovered.json"
_CREATE_SUSPENDED = 0x00000004
_CREATE_NO_WINDOW = 0x08000000
_ERROR_SXS_CANT_GEN_ACTCTX = 14001
_STALE_PARTIAL_S = 3600  # an unfinished copy older than this was abandoned (a killed launch)
_PROFILE_IN_LOG = re.compile(r"--user-data-dir=(.*?playwright_chromiumdev_profile-[A-Za-z0-9]{6})")


def spawn_error(exe):
    """``None`` if Windows can create a process from ``exe``, else the ``OSError`` it raised.

    The process is created suspended and terminated at once, so none of its code runs: no window,
    no profile, no licence check. CreateProcess builds the activation context before that, which is
    the step that fails, so this answers exactly what Playwright's spawn would hit."""
    try:
        proc = subprocess.Popen([exe], creationflags=_CREATE_SUSPENDED | _CREATE_NO_WINDOW,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        return exc
    try:
        proc.kill()
    finally:
        proc.wait()
    return None


def is_sxs_error(exc):
    return getattr(exc, "winerror", None) == _ERROR_SXS_CANT_GEN_ACTCTX or "side-by-side" in str(exc).lower()


def recover_root():
    return os.path.join(os.path.expanduser("~"), ".clearcote", "recovered")


def recovery_key(exe):
    """``<build>-<hash>``: the cache build directory's name, and a hash of the source directory and
    the exe's size and modification time, so a rebuilt or re-downloaded build gets a fresh copy.
    Node computes the same key (winlaunch.ts), so both SDKs share one copy. Raises OSError if the
    exe is gone."""
    src = os.path.dirname(os.path.abspath(exe))
    st = os.stat(exe)
    parent = os.path.dirname(src) if os.path.basename(src).lower() == "browser" else src
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(parent))[:60] or "browser"
    ident = "%s|%d|%d" % (os.path.normcase(src), st.st_size, st.st_mtime_ns // 1000000)
    return "%s-%s" % (name, hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12])


def recovered_exe(exe):
    """The exe inside this build's recovered copy when one is complete, else ``None``."""
    try:
        d = os.path.join(recover_root(), recovery_key(exe))
    except OSError:
        return None
    cand = os.path.join(d, "browser", os.path.basename(exe))
    return cand if os.path.isfile(os.path.join(d, MARKER)) and os.path.isfile(cand) else None


def discard_copy(path):
    """Delete a browser copy unless a browser is running from it. Returns True once it is gone.

    Windows refuses to delete an exe while any process runs from it, so the main exe goes first:
    if that fails the copy is in use and is left alone; if it succeeds nothing can start from the
    copy any more and the rest is removed. (Renaming the directory is no test: Windows allows it
    while a program inside is running.)"""
    browser = os.path.join(path, "browser")
    try:
        names = [n for n in os.listdir(browser) if n.lower().endswith(".exe")]
    except OSError:
        names = []
    for n in sorted(names, key=lambda n: n.lower() != "chrome.exe"):
        try:
            os.remove(os.path.join(browser, n))
        except FileNotFoundError:
            pass
        except OSError:
            return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)


def _age_s(path):
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return 0.0


def sweep_recovered(root, keep=None):
    """Remove recovered copies nothing can use any more: their source build is gone or has changed
    (its key no longer matches), plus copies abandoned half-way. Copies in use are skipped."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if name == keep:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            with open(os.path.join(path, MARKER), encoding="utf-8") as f:
                meta = json.load(f)
            source_exe = os.path.join(meta["source"], meta["exe"])
        except (OSError, ValueError, KeyError, TypeError):
            # no readable marker: a copy still being made, or abandoned when its launch was killed
            stale = _age_s(path) > _STALE_PARTIAL_S
        else:
            try:
                stale = recovery_key(source_exe) != name
            except OSError:
                stale = True  # the build it was copied from is gone (clear-cache, or deleted by hand)
        if stale:
            discard_copy(path)


def clear_recovered():
    """``clearcote clear-cache``: delete every recovered copy. Returns (removed, bytes, in_use)."""
    root = recover_root()
    removed, size, in_use = 0, 0, 0
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return removed, size, in_use
    for name in names:
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        n = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)
        if discard_copy(path):
            removed, size = removed + 1, size + n
        else:
            in_use += 1
    try:
        os.rmdir(root)  # only when empty
    except OSError:
        pass
    return removed, size, in_use


def sweep_temp_recover_dirs(keep_s=60):
    """Remove ``clearcote-recover-*`` copies older SDK versions left in the temp directory (one
    ~400 MB browser per launch, never deleted). Recent ones and ones in use are left alone."""
    if os.environ.get("CLEARCOTE_KEEP_RECOVER"):
        return
    tmp = tempfile.gettempdir()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        path = os.path.join(tmp, name)
        if name.startswith("clearcote-recover-") and os.path.isdir(path) and _age_s(path) > keep_s:
            discard_copy(path)


def make_recovered_copy(exe, warm=None):
    """Copy the build into ``recover_root()`` once and return the copy's exe; later launches reuse
    it. Concurrent launches each copy into a ``.partial-`` directory and the first to finish wins
    the name; the others discard theirs."""
    src = os.path.dirname(os.path.abspath(exe))
    root = recover_root()
    os.makedirs(root, exist_ok=True)
    key = recovery_key(exe)
    sweep_recovered(root, keep=key)
    sweep_temp_recover_dirs()
    ready = recovered_exe(exe)
    if ready:
        return ready
    final = os.path.join(root, key)
    part = tempfile.mkdtemp(prefix=key + ".partial-", dir=root)
    keep_part = False
    try:
        shutil.copytree(src, os.path.join(part, "browser"))
        if warm:
            warm(os.path.join(part, "browser"))
        st = os.stat(exe)
        with open(os.path.join(part, MARKER), "w", encoding="utf-8") as f:
            json.dump({"source": src, "exe": os.path.basename(exe), "size": st.st_size,
                       "mtimeMs": st.st_mtime_ns // 1000000}, f)
        try:
            os.rename(part, final)
        except OSError:
            ready = recovered_exe(exe)  # another launch finished its copy first
            if ready:
                return ready
            if os.path.isdir(final) and discard_copy(final):  # an unmarked leftover held the name
                try:
                    os.rename(part, final)
                except OSError:
                    pass
            if os.path.isdir(part):
                keep_part = True  # still blocked: launch from our own complete copy
                return os.path.join(part, "browser", os.path.basename(exe))
        return os.path.join(final, "browser", os.path.basename(exe))
    finally:
        if not keep_part and os.path.isdir(part):
            shutil.rmtree(part, ignore_errors=True)


def _birth(path):
    st = os.stat(path)
    return getattr(st, "st_birthtime", st.st_ctime)  # st_ctime is the creation time on Windows


def _driver_tmpdir():
    """os.tmpdir() of the Playwright driver (Node) on Windows: TEMP, then TMP."""
    return (os.environ.get("TEMP") or os.environ.get("TMP")
            or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "temp"))


def remove_failed_launch_dirs(exc, started):
    """Remove the two temp directories a Playwright launch leaves when its process never started.

    Node's spawn throws synchronously on "spawn UNKNOWN", before Playwright sets up the cleanup of
    the directories it has just made, so every failed attempt leaked a ``playwright-artifacts-*``
    and (for launch()) a ``playwright_chromiumdev_profile-*``. The profile is named in the error's
    call log. The artifacts directory is not: it is the empty one made by this attempt (after
    ``started``) just before that profile, or, for a persistent launch, the only one made by this
    attempt. Only empty directories are removed, and nothing when the choice is ambiguous."""
    msg = str(exc)
    if "<launching>" not in msg:  # Playwright never got as far as starting the process
        return
    tmp, anchor = None, None
    m = _PROFILE_IN_LOG.search(msg)
    if m:
        prof = m.group(1)
        tmp = os.path.dirname(prof)
        try:
            anchor = _birth(prof)
            os.rmdir(prof)  # only succeeds while empty: the browser never wrote to it
        except OSError:
            pass
    tmp = tmp or _driver_tmpdir()
    now = time.time()
    found = []
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        if not name.startswith("playwright-artifacts-"):
            continue
        path = os.path.join(tmp, name)
        try:
            born = _birth(path)
            if started - 0.05 <= born <= now + 0.05 and not os.listdir(path):
                found.append((born, path))
        except OSError:
            continue
    if anchor is not None:
        found = [f for f in found if f[0] <= anchor + 0.001]
        pick = max(found)[1] if found else None
    else:
        pick = found[0][1] if len(found) == 1 else None
    if pick:
        try:
            os.rmdir(pick)
        except OSError:
            pass
