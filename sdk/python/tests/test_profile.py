import json

import pytest

import clearcote._profile as profile_mod
from clearcote._fingerprint import fingerprint_args
from clearcote._profile import Profile, list_profiles, resolve_profile_options


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "acct-1.json"
    Profile("acct-1", {
        "fingerprint": "acct-1",
        "gpu_renderer": "ANGLE (Intel)",
        "canvas_bridge": {"url": "ws://127.0.0.1:9099", "auth": "user:secret"},
    }).save(str(path))
    loaded = Profile.load(str(path))
    assert loaded.name == "acct-1"
    assert loaded.options == {
        "fingerprint": "acct-1",
        "gpu_renderer": "ANGLE (Intel)",
        "canvas_bridge": {"url": "ws://127.0.0.1:9099", "auth": "user:secret"},
    }


def test_set_merges_and_chains():
    prof = Profile("p").set(fingerprint="s").set(brand="Edge")
    assert prof.options == {"fingerprint": "s", "brand": "Edge"}


def test_resolve_profile_options_accepts_instance_or_path(tmp_path):
    prof = Profile("p", {"gpu_vendor": "X"})
    assert resolve_profile_options(prof) == {"gpu_vendor": "X"}
    path = tmp_path / "p.json"
    prof.save(str(path))
    assert resolve_profile_options(str(path)) == {"gpu_vendor": "X"}


def test_list_profiles_reads_profile_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(profile_mod, "PROFILE_DIR", str(tmp_path))
    Profile("alpha", {}).save()
    Profile("beta", {}).save()
    assert list_profiles() == ["alpha", "beta"]


def test_loads_camelcase_profile_from_node_sdk(tmp_path):
    # a profile written by the Node SDK uses camelCase keys; load() normalizes them to snake_case
    # so the persona still maps to its --fingerprint-* switches here.
    path = tmp_path / "node.json"
    path.write_text(json.dumps({"name": "node", "options": {
        "fingerprint": "s", "gpuRenderer": "ANGLE (Intel)", "canvasBridge": {"url": "ws://h:1"},
    }}), encoding="utf-8")
    prof = Profile.load(str(path))
    assert prof.options == {
        "fingerprint": "s", "gpu_renderer": "ANGLE (Intel)", "canvas_bridge": {"url": "ws://h:1"},
    }
    args = fingerprint_args(prof.options)
    assert "--fingerprint-gpu-renderer=ANGLE (Intel)" in args
    assert "--canvas-bridge-url=ws://h:1" in args


def test_rejects_unsafe_profile_name():
    with pytest.raises(ValueError):
        Profile("..").save()


def test_save_writes_private_file(tmp_path):
    import os
    import stat
    path = Profile("p", {"canvas_bridge": {"auth": "u:s"}}).save(str(tmp_path / "p.json"))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    # 0600 on POSIX; on Windows the mode bits are not enforced, so only assert there.
    if os.name == "posix":
        assert mode == 0o600
