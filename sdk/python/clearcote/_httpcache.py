"""A shared HTTP cache for runs that start from a fresh profile (``http_cache_dir``).

A fresh profile starts with an empty disk cache, so every run downloads the site's scripts, styles and images
again through the proxy. Pointing Chromium's --disk-cache-dir at a folder that outlives the run keeps those
files while cookies and storage stay new. Measured on r56 (11-02, github.com + bbc.com/news through a counting
proxy): about 8 MB of proxy traffic per run fresh, 0.36-0.44 MB once the cache was warm.

Chromium locks its cache: of several browsers on one cache folder at once, only the first gets it, the others
run on an empty in-memory cache. So the folder is split into slots (<dir>/slot-0, slot-1, ...) and each launch
takes the first one no other live launch holds, marked by <dir>/slot-N.lock holding the owner's pid. A lock
whose process is gone is taken over. Node (httpcache.ts) and .NET (HttpCache.cs) use the same layout, so the
three can share one folder.

Only a launch with a profile has a disk cache: incognito keeps it in memory (measured: --disk-cache-dir on an
incognito launch left Cache_Data empty and saved nothing), so a launch without a profile refuses the option.
"""

import atexit
import os
import re

from ._warnings import warn_once
from .download import _pid_alive

HTTP_CACHE_SLOTS = 64  # the most browsers that can share one folder at once

NEEDS_PROFILE = "http_cache_dir needs a profile: a launch without one keeps its HTTP cache in memory. %s"

_DISK_CACHE_ARG = re.compile(r"^--disk-cache-dir(=|$)")


class HttpCacheSlot:
    """A slot held by this process: ``dir`` goes to --disk-cache-dir, ``release()`` frees it (idempotent)."""

    def __init__(self, dir, lock):
        self.dir = dir
        self._lock = lock
        self._done = False
        atexit.register(self.release)

    def release(self, *_):
        if self._done:
            return
        self._done = True
        try:
            atexit.unregister(self.release)
        except Exception:
            pass
        try:
            with open(self._lock, encoding="utf-8") as f:
                mine = f.read().strip() == str(os.getpid())
            if mine:
                os.remove(self._lock)
        except OSError:
            pass


def _try_lock(path):
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _owner(path):
    try:
        with open(path, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def acquire_http_cache_slot(root, quiet=False):
    """Hold the first free slot of ``root``, creating the folder. Raises when the folder cannot be created;
    returns None (with one warning) when every slot is held by a live process."""
    base = os.path.abspath(os.fspath(root))
    os.makedirs(base, exist_ok=True)
    for n in range(HTTP_CACHE_SLOTS):
        lock = os.path.join(base, "slot-%d.lock" % n)
        held = _try_lock(lock)
        if not held:
            if _pid_alive(_owner(lock)):
                continue
            try:
                os.remove(lock)  # a crashed run's lock
            except FileNotFoundError:
                pass
            held = _try_lock(lock)
            if not held:
                continue  # another launch took it over first
        slot = os.path.join(base, "slot-%d" % n)
        os.makedirs(slot, exist_ok=True)
        return HttpCacheSlot(slot, lock)
    warn_once("http-cache-full", "clearcote: http_cache_dir: all %d slots of %s are in use; this launch runs "
              "without the shared cache" % (HTTP_CACHE_SLOTS, base), quiet)
    return None


def check_http_cache_dir(http_cache_dir, args):
    """Validate ``http_cache_dir`` against the caller's own args (before anything starts)."""
    if http_cache_dir is None:
        return None
    if isinstance(http_cache_dir, os.PathLike):
        http_cache_dir = os.fspath(http_cache_dir)
    if not isinstance(http_cache_dir, str) or not http_cache_dir.strip():
        raise TypeError("http_cache_dir must be a folder path")
    if any(_DISK_CACHE_ARG.match(str(a)) for a in (args or [])):
        raise TypeError("http_cache_dir and an explicit --disk-cache-dir in args cannot be combined; pass one of them")
    return http_cache_dir


def refuse_without_profile(kwargs, hint):
    """A launch without a profile (incognito): ``http_cache_dir`` would do nothing, so it raises (``hint`` names
    the profile-backed call to use instead)."""
    if kwargs.get("http_cache_dir") is not None:
        raise TypeError(NEEDS_PROFILE % hint)


def take_http_cache(kwargs):
    """Pop ``http_cache_dir`` from launch kwargs and, when set, take a slot: --disk-cache-dir=<slot> is appended
    to ``kwargs["args"]``. Returns the release to call once the browser has exited, or None when no slot was
    taken."""
    root = check_http_cache_dir(kwargs.pop("http_cache_dir", None), kwargs.get("args"))
    if root is None:
        return None
    slot = acquire_http_cache_slot(root, kwargs.get("quiet", False))
    if slot is None:
        return None
    kwargs["args"] = list(kwargs.get("args") or []) + ["--disk-cache-dir=" + slot.dir]
    return slot.release


def with_http_cache(kwargs, start, event):
    """Run ``start()`` (a local launch) with the slot ``http_cache_dir`` asks for, released when the returned
    browser/context fires ``event`` ("disconnected" / "close") or the launch fails."""
    release = take_http_cache(kwargs)
    if release is None:
        return start()
    try:
        target = start()
    except BaseException:
        release()
        raise
    target.on(event, release)
    return target


async def with_http_cache_async(kwargs, start, event):
    """:func:`with_http_cache` for the async API: ``start()`` returns an awaitable."""
    release = take_http_cache(kwargs)
    if release is None:
        return await start()
    try:
        target = await start()
    except BaseException:
        release()
        raise
    target.on(event, release)
    return target
