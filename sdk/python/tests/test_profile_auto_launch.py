"""The launch-path wiring for ``profile="auto"``.

Covers the decisions _apply_auto_profile makes BEFORE any browser starts, by stubbing the two
expensive collaborators (measure_host launches a browser; resolve_auto hits the network).
"""

import clearcote
import pytest

HOST = {
    "os_family": "windows", "browser_major": 150, "gpu_vendor": "intel",
    "screen_width": 3440, "screen_height": 1440, "device_pixel_ratio": 1,
    "hardware_concurrency": 16, "device_memory": 16,
}

SERVICE_PROFILE = {"navigator": {"user_agent": "ua"}, "screen": {"width": 1920}}


@pytest.fixture
def stubbed(monkeypatch):
    calls = {"measure": 0, "resolve": 0, "select": None}

    def fake_measure(launch_fn, exe, major):
        calls["measure"] += 1
        calls["major"] = major
        return HOST

    def fake_resolve(host, **kw):
        calls["resolve"] += 1
        calls["select"] = kw
        return {"profile": SERVICE_PROFILE, "selection": {"entry": {"id": "svc-1"}}, "source": "service"}

    monkeypatch.setattr(clearcote, "measure_host", fake_measure)
    monkeypatch.setattr(clearcote, "resolve_auto", fake_resolve)
    return calls


class TestApplyAutoProfile:
    def test_sets_fingerprint_profile_from_the_resolver(self, stubbed):
        fp = {}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True)
        assert fp["fingerprint_profile"] == SERVICE_PROFILE
        assert stubbed["resolve"] == 1

    def test_never_sets_a_seed(self, stubbed):
        # The seed is what engages farbling; "auto" exists to avoid it.
        fp = {}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True)
        assert "fingerprint" not in fp

    def test_explicit_profile_wins_and_skips_all_work(self, stubbed):
        # If the caller already named a profile there is nothing to decide, and we must not pay
        # for a browser launch or a network call to decide it anyway.
        fp = {"fingerprint_profile": {"navigator": {"user_agent": "mine"}}}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True)
        assert fp["fingerprint_profile"]["navigator"]["user_agent"] == "mine"
        assert stubbed["measure"] == 0
        assert stubbed["resolve"] == 0

    def test_passes_the_engine_major_to_host_measurement(self, stubbed):
        # A profile captured on another major contradicts the engine, so the major that gets
        # filtered on must be the one the binary actually is.
        fp = {}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True)
        assert stubbed["major"] == int(str(clearcote.RELEASE["version"]).split(".")[0])

    def test_forwards_selection_options(self, stubbed):
        fp = {}
        clearcote._apply_auto_profile(fp, "/path/chrome", {"key": "acct-9", "mode": "best"}, quiet=True)
        assert stubbed["select"]["key"] == "acct-9"
        assert stubbed["select"]["mode"] == "best"

    def test_warns_when_a_seed_is_also_set(self, stubbed, capsys):
        # Silently ignoring the seed, or silently keeping it, would both be worse than saying so.
        fp = {"fingerprint": "seed-1"}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=False)
        err = capsys.readouterr().err
        assert "fingerprint seed" in err
        assert "farbling" in err

    def test_quiet_suppresses_the_warning(self, stubbed, capsys):
        fp = {"fingerprint": "seed-1"}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True)
        assert capsys.readouterr().err == ""

    def test_license_is_forwarded_from_the_pro_tuple(self, stubbed):
        fp = {}
        clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True,
                                      pro=("cc_lic_x", "https://api.example"))
        assert stubbed["select"]["license_key"] == "cc_lic_x"
        assert stubbed["select"]["api_base"] == "https://api.example"


class TestPrepareRouting:
    def test_auto_is_not_treated_as_a_saved_profile_name(self, monkeypatch):
        # resolve_profile_options would raise or look up a file called "auto"; the auto path must
        # never reach it, and must reach the auto resolver instead.
        def boom(*a, **k):
            raise AssertionError("resolve_profile_options must not be called for profile='auto'")

        routed = []
        monkeypatch.setattr(clearcote, "resolve_profile_options", boom)
        # Hermetic. This used to run _prepare for real: on a machine with a cached engine and a
        # cold host cache it ran the real host probe (a browser launch through Playwright's SYNC
        # API, with this machine's licence), which left an event loop running on this thread and
        # broke every async test that ran after it.
        monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: "/path/chrome")
        monkeypatch.setattr(clearcote, "_apply_auto_profile",
                            lambda fp, exe, select, quiet=False, pro=None, lease=None: routed.append(exe))
        clearcote._prepare({"profile": "auto", "quiet": True})
        assert routed == ["/path/chrome"]


class TestNestedProbeLaunchIsLicensed:
    """The host probe launches the SAME binary the session will use.

    On PRO that is the gated build, so a probe launched without a run-token is killed by the
    engine gate and surfaces as `TargetClosedError: Target page, context or browser has been
    closed` — a message naming neither licensing nor any call the caller wrote. It worked only
    when the key happened to be in CLEARCOTE_LICENSE_KEY; passing license_key= as a kwarg, the
    documented way, failed. Reproduced live against 150-r10 before the fix.
    """

    def test_probe_launch_receives_the_license(self, stubbed, monkeypatch):
        seen = {}

        def fake_measure(launch_fn, exe, major):
            # Call it the way _profilesource.measure_host does, and record what reaches launch().
            launch_fn(executable_path=exe, headless=True, quiet=True)
            return HOST

        def fake_launch(**kw):
            seen.update(kw)
            raise RuntimeError("stop here — the kwargs are the assertion")

        monkeypatch.setattr(clearcote, "measure_host", fake_measure)
        monkeypatch.setattr(clearcote, "launch", fake_launch)

        with pytest.raises(RuntimeError):
            clearcote._apply_auto_profile({}, "/path/chrome", {}, quiet=True,
                                          pro=("cc_lic_probe", "https://api.example"))

        assert seen["license_key"] == "cc_lic_probe", "the gated probe launch must carry the key"
        assert seen["license_api_base"] == "https://api.example"
        # The probe reads GPU/display off about:blank and needs no profile: it must not pay for a
        # throwaway profile directory on every "auto" resolution.
        assert seen["ephemeral_profile"] is False

    def test_probe_launch_borrows_the_callers_lease(self, stubbed, monkeypatch):
        """The probe runs INSIDE a launch that already holds a concurrency slot. Checking out a
        second one deadlocks that launch against itself on a per-browser plan (see
        tests/test_license_per_browser.py), so the caller's lease is handed down."""
        seen = {}
        sentinel = object()

        def fake_measure(launch_fn, exe, major):
            launch_fn(executable_path=exe, headless=True, quiet=True)
            return HOST

        def fake_launch(**kw):
            seen.update(kw)
            raise RuntimeError("stop here - the kwargs are the assertion")

        monkeypatch.setattr(clearcote, "measure_host", fake_measure)
        monkeypatch.setattr(clearcote, "launch", fake_launch)

        with pytest.raises(RuntimeError):
            clearcote._apply_auto_profile({}, "/path/chrome", {}, quiet=True, lease=sentinel)

        assert seen["_cc_lease"] is sentinel


class TestPrivacySandboxDefault:
    """Default flipped in 0.23.0: Privacy Sandbox stays ON unless explicitly disabled.

    The default persona is brand="chrome", so the surface follows what Google Chrome has. Since
    0.70.2 the parts Google's own servers switch off in every Chrome after its first session
    (Topics, Attribution Reporting, Fenced Frames; CHROME_SEED_DISABLED_FEATURES) are off by
    default too; the rest of the old list (FLEDGE, Shared Storage, ...) is still opt-in.
    """

    SANDBOX = "Fledge"
    SEED = "BrowsingTopics,ConversionMeasurement,FencedFrames,"

    def _args(self, monkeypatch, **kwargs):
        monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: "/path/chrome")
        _exe, args, *_rest = clearcote._prepare(dict(kwargs))
        return args

    def test_privacy_sandbox_is_not_disabled_by_default(self, monkeypatch):
        assert not any(self.SANDBOX in a for a in self._args(monkeypatch))

    def test_what_chromes_servers_switch_off_is_off_by_default_in_one_switch(self, monkeypatch):
        args = self._args(monkeypatch)
        off = [a for a in args if a.startswith("--disable-features=")]
        assert len(off) == 1 and self.SEED in off[0]

    def test_a_feature_the_caller_enables_is_not_switched_off(self, monkeypatch):
        args = self._args(monkeypatch, args=["--enable-features=BrowsingTopics"])
        off = [a for a in args if a.startswith("--disable-features=")]
        assert len(off) == 1 and "BrowsingTopics" not in off[0] and "ConversionMeasurement" in off[0]
        assert any(a.startswith("--enable-features=") and "BrowsingTopics" in a for a in args)

    def test_opt_in_still_disables_it(self, monkeypatch):
        args = self._args(monkeypatch, disable_privacy_sandbox=True)
        assert any(self.SANDBOX in a for a in args)
