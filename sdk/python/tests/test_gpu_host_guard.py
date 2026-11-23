"""The GPU vendor guard: the GPU a persona declares against the one this machine renders on (_persona.py), and the
vendor filter of profile="auto" (_profilelib.same_gpu_vendor). The same cases as sdk/node/test/gpu-host-guard.test.ts."""
import base64
import gzip
import json
import time

import pytest

from clearcote import _persona, _profilelib, _profilesource, _warnings
from clearcote._persona import (GPU_CHECK_ENV, gpu_host_mismatch, gpu_source, persona_gpu_from_token,
                                warn_gpu_host_mismatch)

AMD = {"vendor": "Google Inc. (AMD)",
       "renderer": "ANGLE (AMD, AMD Radeon RX 6600 (0x000073FF) Direct3D11 vs_5_0 ps_5_0, D3D11)"}
NV = {"vendor": "Google Inc. (NVIDIA)",
      "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 (0x00002184) Direct3D11 vs_5_0 ps_5_0, D3D11)"}
INTEL = {"vendor": "Google Inc. (Intel)",
         "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x00004680) Direct3D11 vs_5_0 ps_5_0, D3D11)"}
SWIFT = {"vendor": "Google Inc. (Google)",
         "renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)"}


def host(v):
    return {"gpu_vendor": v}


def token(gpu):
    """A persona token shaped like the service's: base64url(payload JSON) + "." + signature; payload.p = codec JSON."""
    now = int(time.time())
    payload = {"v": 1, "iat": now, "nbf": now - 30, "exp": now + 3600,
               "p": {"codec": 1, "persona": {"webgl_vendor": gpu["vendor"], "webgl_renderer": gpu["renderer"]}}
               if gpu else {}}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".c2ln"


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    monkeypatch.delenv(GPU_CHECK_ENV, raising=False)
    _persona.clear_caches()
    yield
    _persona.clear_caches()


# ── reading the token ─────────────────────────────────────────────────────────────────────────────────────────────
def test_persona_gpu_from_token_reads_the_webgl_vendor_and_renderer():
    assert persona_gpu_from_token(token(AMD)) == AMD


@pytest.mark.parametrize("bad", [token(None), "tok-abc", ""])
def test_persona_gpu_from_token_is_none_without_a_persona_or_for_a_token_it_cannot_read(bad):
    assert persona_gpu_from_token(bad) is None


def test_gpu_source_names_the_options_an_imported_profile_or_the_engine():
    assert gpu_source({"fingerprint-gpu-vendor": "Google Inc. (AMD)"}, None) == "options"
    assert gpu_source({"fingerprint-gpu-renderer": "x"}, None) == "options"
    assert gpu_source({"fingerprint-profile-id": "p1"}, None) == "profile"
    prof = json.dumps({"webgl": {"webgl1": {"debug": {"UNMASKED_RENDERER_WEBGL": AMD["renderer"]}}}})
    assert gpu_source({}, prof) == "profile"
    # a profile without GPU strings (fonts and screen only) leaves the GPU to the engine, as the engine does
    assert gpu_source({}, json.dumps({"screen": {"width": 1920}})) == "engine"
    assert gpu_source({"fingerprint": "7"}, None) == "engine"


# ── the comparison ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_declared_vendor_that_is_not_the_hosts_is_flagged_with_its_source_and_fix():
    m = gpu_host_mismatch(AMD, host("nvidia"), ["--fingerprint=1"], "options")
    assert m["code"] == "gpu-vendor-host:amd:nvidia"
    for part in ("gpu_vendor / gpu_renderer declare a AMD GPU", "this machine renders on NVIDIA", "card that computes",
                 "Remove gpu_vendor / gpu_renderer", GPU_CHECK_ENV + "=1"):
        assert part in m["message"]
    assert "Use a profile captured on a machine with the same GPU vendor" in \
        gpu_host_mismatch(INTEL, host("nvidia"), [], "profile")["message"]
    auto = gpu_host_mismatch(INTEL, host("amd"), [], "engine")  # the engine's own choice on an AMD host
    assert auto["code"] == "gpu-vendor-host:intel:amd" and "gpu_string_spoof=False" in auto["message"]


@pytest.mark.parametrize("gpu, vendor, source", [(NV, "nvidia", "options"), (INTEL, "intel", "engine"),
                                                 (AMD, "amd", "profile")])
def test_the_same_vendor_passes_whatever_the_model(gpu, vendor, source):
    assert gpu_host_mismatch(gpu, host(vendor), [], source) is None


def test_a_graphics_cards_name_on_a_software_host_is_flagged_and_a_software_renderer_is_not():
    m = gpu_host_mismatch(INTEL, host("none"), [], "engine")
    assert m["code"] == "gpu-software-host:intel"
    assert "renders WebGL in software" in m["message"] and "gpu_string_spoof=False" in m["message"]
    assert "gpu_vendor / gpu_renderer declare a NVIDIA GPU" in gpu_host_mismatch(NV, host("none"), [], "options")["message"]
    assert gpu_host_mismatch(SWIFT, host("none"), [], "engine") is None
    assert gpu_host_mismatch(SWIFT, host("nvidia"), [], "options") is None


@pytest.mark.parametrize("sw", ["--canvas-bridge-url=ws://x:1", "--use-adapter-luid=1,2", "--disable-gpu-string-spoof",
                                "--disable-gpu-fingerprint", "--fingerprint-passthrough"])
def test_launches_that_show_the_real_gpu_or_render_elsewhere_are_skipped(sw):
    assert gpu_host_mismatch(AMD, host("nvidia"), [sw], "options") is None
    assert gpu_host_mismatch(INTEL, host("none"), [sw], "engine") is None


def test_an_unknown_host_vendor_or_an_unreadable_persona_says_nothing():
    assert gpu_host_mismatch(AMD, host(""), [], "options") is None
    assert gpu_host_mismatch(AMD, host("virtio"), [], "options") is None
    assert gpu_host_mismatch(None, host("nvidia"), [], "options") is None


SEL = {"fingerprint-gpu-vendor": AMD["vendor"], "fingerprint-gpu-renderer": AMD["renderer"]}


def test_warn_once_per_process(fresh, capsys):
    warn_gpu_host_mismatch(token(AMD), host("nvidia"), [], SEL, None)
    warn_gpu_host_mismatch(token(AMD), host("nvidia"), [], SEL, None)
    assert capsys.readouterr().err.count("clearcote: warning: gpu_vendor / gpu_renderer declare a AMD GPU") == 1


def test_quiet_no_warn_and_the_opt_out_silence_it(fresh, capsys, monkeypatch):
    warn_gpu_host_mismatch(token(AMD), host("nvidia"), [], SEL, None, quiet=True)
    monkeypatch.setenv(GPU_CHECK_ENV, "1")
    warn_gpu_host_mismatch(token(AMD), host("nvidia"), [], SEL, None)
    monkeypatch.delenv(GPU_CHECK_ENV)
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    warn_gpu_host_mismatch(token(AMD), host("nvidia"), [], SEL, None)
    assert capsys.readouterr().err == ""


# ── wired into apply(): the token it fetched, against the engine's host facts ─────────────────────────────────────
class _Lease:
    token = "hdr.sig"
    lease_id = "L1"


def _r51(_exe, name):
    return name == "cc-persona-host-facts"


def _apply(tmp_path, args, vendor, gpu, quiet=False):
    exe = tmp_path / "chrome"
    exe.write_text("x")
    return _persona.apply(str(exe), args, {}, _Lease(), supports=_r51,
                          runner=lambda cmd, env: '{"gpu_vendor": "%s"}\n' % vendor,
                          fetch=lambda *a, **k: (token(gpu), time.time() + 3600), quiet=quiet)


def test_apply_warns_for_gpu_vendor_amd_on_an_nvidia_host_and_not_for_the_hosts_vendor(fresh, capsys, tmp_path):
    _apply(tmp_path, ["--fingerprint=1", "--fingerprint-gpu-vendor=" + AMD["vendor"],
                      "--fingerprint-gpu-renderer=" + AMD["renderer"]], "nvidia", AMD)
    _apply(tmp_path, ["--fingerprint=2"], "nvidia", NV)
    err = capsys.readouterr().err
    assert "gpu_vendor / gpu_renderer declare a AMD GPU" in err and "this machine renders on NVIDIA" in err
    assert err.count("clearcote: warning:") == 1


def test_apply_warns_for_an_imported_profile_of_another_vendor(fresh, capsys, tmp_path):
    prof = json.dumps({"webgl": {"webgl1": {"debug": {"UNMASKED_VENDOR_WEBGL": INTEL["vendor"],
                                                      "UNMASKED_RENDERER_WEBGL": INTEL["renderer"]}}}})
    b64 = base64.b64encode(gzip.compress(prof.encode())).decode()
    _apply(tmp_path, ["--fingerprint-profile=" + b64], "nvidia", INTEL)
    assert "the imported profile declares a Intel GPU" in capsys.readouterr().err


def test_apply_says_nothing_when_quiet(fresh, capsys, tmp_path):
    _apply(tmp_path, ["--fingerprint=1", "--fingerprint-gpu-vendor=" + AMD["vendor"]], "nvidia", AMD, quiet=True)
    assert capsys.readouterr().err == ""


# ── profile="auto" picks the host's GPU vendor ────────────────────────────────────────────────────────────────────
def _entry(id_, gpu_vendor, screen=1920):
    return {"id": id_, "os_family": "windows", "browser_major": 158, "gpu_vendor": gpu_vendor,
            "screen_width": screen, "screen_height": 1080}


def _host(gpu_vendor):
    return {"os_family": "windows", "browser_major": 158, "gpu_vendor": gpu_vendor, "screen_width": 1920,
            "screen_height": 1080}


def test_same_gpu_vendor_keeps_only_the_host_vendors_candidates_when_there_are_any():
    pool = [_entry("a", "amd"), _entry("n1", "nvidia"), _entry("n2", "nvidia"), _entry("i", "intel")]
    assert [e["id"] for e in _profilelib.same_gpu_vendor(pool, _host("nvidia"))] == ["n1", "n2"]
    assert [e["id"] for e in _profilelib.same_gpu_vendor(pool, _host("amd"))] == ["a"]
    assert [e["id"] for e in _profilelib.same_gpu_vendor([_entry("i", "intel")], _host("amd"))] == ["i"]
    assert len(_profilelib.same_gpu_vendor(pool, _host("software"))) == 4
    assert len(_profilelib.same_gpu_vendor(pool, _host("unknown"))) == 4


def test_select_profile_never_picks_another_vendor_when_the_index_has_the_hosts():
    pool = [_entry("n", "nvidia", 1280)] + [_entry("i%d" % i, "intel") for i in range(30)]
    for key in ("k1", "k2", "k3", "k4", "k5"):
        assert _profilelib.select_profile(pool, _host("nvidia"), key=key)["entry"]["id"] == "n"


# ── host.json: the GPU read with the real strings ─────────────────────────────────────────────────────────────────
def test_host_cache_entry_without_the_real_gpu_marker_is_re_measured(tmp_path, monkeypatch):
    monkeypatch.setattr(_profilesource, "_host_cache_path", lambda: str(tmp_path / "host.json"))
    exe = str(tmp_path / "chrome")
    _profilesource.write_cached_host(exe, {"os_family": "windows", "browser_major": 158, "gpu_vendor": "nvidia"})
    with open(tmp_path / "host.json", encoding="utf-8") as f:
        old = json.load(f)
    assert old["_gpu"] == "real" and _profilesource.read_cached_host(exe) is not None
    del old["_gpu"]
    (tmp_path / "host.json").write_text(json.dumps(old), encoding="utf-8")
    assert _profilesource.read_cached_host(exe) is None


def test_measure_host_reads_the_real_gpu_strings(tmp_path, monkeypatch):
    monkeypatch.setattr(_profilesource, "_host_cache_path", lambda: str(tmp_path / "host.json"))
    seen = {}

    def launch_fn(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    with pytest.raises(RuntimeError):
        _profilesource.measure_host(launch_fn, str(tmp_path / "chrome"), 158)
    assert seen["gpu_string_spoof"] is False
