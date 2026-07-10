"""Launch smoke tests — actually INVOKE every public launch entry point (sync + async)
through the real argument-assembly (`_prepare`) with a mocked Playwright driver and a
fake binary, asserting each returns without a wiring/unpack/signature error and threads
the effective (profile-aware) fingerprint seed into the humanizer.

WHY THIS FILE EXISTS: clearcote 0.15.0 shipped a broken async launch — it unpacked 5 of
`_prepare`'s 6 return values (`ValueError: too many values to unpack`). The prior tests
missed it because they only *imported* launch, never *called* it. These tests call the
real entry points (only the driver + humanizer are mocked, so `_prepare` runs for real),
so any future tuple/signature drift between `_prepare` and a caller — or between the sync
and async surfaces — fails HERE. Because the publish workflows gate on this suite
(sdk-ci.yml), a launch that can't launch can no longer be released.
"""
import inspect
import tempfile

import clearcote
from clearcote import async_api


def _fake_exe():
    """A real (empty) file so `_prepare` uses it as the explicit executable_path and
    never tries to download a binary."""
    f = tempfile.NamedTemporaryFile(prefix="fake-chrome-", suffix=".exe", delete=False)
    f.write(b"\x00")
    f.close()
    return f.name


class _FakeBrowser:
    def on(self, *a, **k):
        pass


# --------------------------------------------------------------------------- sync
def _patch_sync(monkeypatch, cap):
    class _SyncChromium:
        def launch(self, **kw):
            return _FakeBrowser()

        def launch_persistent_context(self, *a, **kw):
            return _FakeBrowser()

    class _SyncPW:
        chromium = _SyncChromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _SyncPW())
    monkeypatch.setattr(clearcote, "install_humanize",
                        lambda b, h, s, seed=None: cap.__setitem__("seed", seed))
    monkeypatch.setattr(clearcote, "install_humanize_on_context",
                        lambda c, h, s, seed=None: cap.__setitem__("seed", seed))


def test_sync_launch_invokes_and_threads_seed(monkeypatch):
    cap = {}
    _patch_sync(monkeypatch, cap)
    browser = clearcote.launch(executable_path=_fake_exe(), fingerprint="seed-SYNC")
    assert browser is not None
    assert cap["seed"] == "seed-SYNC"  # _prepare's effective seed reached the humanizer


def test_sync_launch_persistent_context_invokes_and_threads_seed(monkeypatch):
    cap = {}
    _patch_sync(monkeypatch, cap)
    ctx = clearcote.launch_persistent_context(
        tempfile.mkdtemp(), executable_path=_fake_exe(), fingerprint="seed-LPC")
    assert ctx is not None
    assert cap["seed"] == "seed-LPC"


# -------------------------------------------------------------------------- async
def _patch_async(monkeypatch, cap):
    class _AsyncChromium:
        async def launch(self, **kw):
            return _FakeBrowser()

        async def launch_persistent_context(self, *a, **kw):
            return _FakeBrowser()

    class _AsyncPW:
        chromium = _AsyncChromium()

        async def stop(self):
            pass

    async def _fake_start():
        return _AsyncPW()

    async def _fake_ih(b, h, s, seed=None):
        cap["seed"] = seed

    async def _fake_ihc(c, h, s, seed=None):
        cap["seed"] = seed

    monkeypatch.setattr(async_api, "_start_driver", _fake_start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda *a, **k: None)
    monkeypatch.setattr(async_api, "install_humanize", _fake_ih)
    monkeypatch.setattr(async_api, "install_humanize_on_context", _fake_ihc)


async def test_async_launch_invokes_and_threads_seed(monkeypatch):
    cap = {}
    _patch_async(monkeypatch, cap)
    browser = await async_api.launch(executable_path=_fake_exe(), fingerprint="seed-ASYNC")
    assert browser is not None
    assert cap["seed"] == "seed-ASYNC"


async def test_async_launch_persistent_context_invokes_and_threads_seed(monkeypatch):
    cap = {}
    _patch_async(monkeypatch, cap)
    ctx = await async_api.launch_persistent_context(
        tempfile.mkdtemp(), executable_path=_fake_exe(), fingerprint="seed-ALPC")
    assert ctx is not None
    assert cap["seed"] == "seed-ALPC"


# ------------------------------------------------------------------- sync<->async parity
def test_sync_async_launch_param_parity():
    """The sync and async surfaces must expose matching parameters for each launch
    entry point, so a positional/keyword added to one but not the other is caught."""
    for name in ("launch", "launch_persistent_context", "launch_agent"):
        sp = list(inspect.signature(getattr(clearcote, name)).parameters)
        ap = list(inspect.signature(getattr(async_api, name)).parameters)
        assert sp == ap, f"{name}: sync {sp} vs async {ap} parameter drift"
