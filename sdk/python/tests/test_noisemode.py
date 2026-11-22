import json
import os

import pytest

from clearcote._noisemode import PROFILE_STATE_FILE, has_chrome_data, read_noise_mode, resolve_noise_per_profile

R60 = dict(supports=lambda e: True, quiet=True)
OLD = dict(supports=lambda e: False, quiet=True)


def _state(d):
    with open(os.path.join(d, PROFILE_STATE_FILE), encoding="utf-8") as f:
        return json.load(f)


def _chrome_profile(d):
    open(os.path.join(d, "Local State"), "w").write("{}")
    os.mkdir(os.path.join(d, "Default"))
    return str(d)


def test_new_empty_folder_gets_per_profile_and_remembers(tmp_path):
    assert resolve_noise_per_profile(None, str(tmp_path), **R60) is True
    assert _state(tmp_path) == {"noise": "per-profile"}


def test_missing_folder_is_created_so_its_first_run_does_not_make_it_old(tmp_path):
    d = str(tmp_path / "new-profile")
    assert resolve_noise_per_profile(None, d, **R60) is True
    open(os.path.join(d, "Local State"), "w").write("{}")
    assert resolve_noise_per_profile(None, d, **R60) is True


def test_folder_made_before_this_release_keeps_per_site(tmp_path):
    d = _chrome_profile(tmp_path)
    assert resolve_noise_per_profile(None, d, **R60) is False
    assert _state(d) == {"noise": "per-site"}


def test_had_chrome_data_snapshot_wins_over_files_seeded_since(tmp_path):
    # The SDK seeds Default/Preferences before the engine is known: the snapshot says the folder was new.
    os.mkdir(tmp_path / "Default")
    assert resolve_noise_per_profile(None, str(tmp_path), had_chrome_data=False, **R60) is True
    other = tmp_path / "other"
    other.mkdir()
    assert resolve_noise_per_profile(None, str(other), had_chrome_data=True, **R60) is False


def test_has_chrome_data():
    assert has_chrome_data("/definitely/missing/folder") is False


def test_old_engine_new_folder_is_per_site_and_stays_so(tmp_path):
    assert resolve_noise_per_profile(None, str(tmp_path), **OLD) is False
    assert read_noise_mode(str(tmp_path)) == "per-site"
    assert resolve_noise_per_profile(None, str(tmp_path), **R60) is False


def test_explicit_value_wins_and_becomes_the_mode(tmp_path):
    legacy = _chrome_profile(tmp_path)
    assert resolve_noise_per_profile(True, legacy, **R60) is True
    assert resolve_noise_per_profile(None, legacy, **R60) is True
    d = tmp_path / "b"
    d.mkdir()
    assert resolve_noise_per_profile(False, str(d), **R60) is False
    assert resolve_noise_per_profile(None, str(d), **R60) is False


def test_no_folder_is_new_and_writes_nothing():
    assert resolve_noise_per_profile(None, None, **R60) is True
    assert resolve_noise_per_profile(None, None, **OLD) is False
    assert resolve_noise_per_profile(False, None, **R60) is False


def test_passthrough_left_alone(tmp_path):
    assert resolve_noise_per_profile(None, str(tmp_path), passthrough=True, **R60) is None
    assert not (tmp_path / PROFILE_STATE_FILE).exists()


def test_keeps_other_keys_and_ignores_bad_file(tmp_path):
    (tmp_path / PROFILE_STATE_FILE).write_text(json.dumps({"other": 1}))
    assert resolve_noise_per_profile(None, str(tmp_path), **R60) is True
    assert _state(tmp_path) == {"other": 1, "noise": "per-profile"}
    bad = tmp_path / "bad"
    bad.mkdir()
    _chrome_profile(bad)
    (bad / PROFILE_STATE_FILE).write_text("not json")
    assert resolve_noise_per_profile(None, str(bad), **R60) is False
    assert _state(bad) == {"noise": "per-site"}


def test_unwritable_never_fails_and_says_so_unless_quiet(tmp_path, capsys):
    (tmp_path / PROFILE_STATE_FILE).mkdir()  # a directory where the file should go
    assert resolve_noise_per_profile(None, str(tmp_path), supports=lambda e: True) is True
    assert "could not remember the noise mode" in capsys.readouterr().err
    assert resolve_noise_per_profile(None, str(tmp_path), **R60) is True
    assert capsys.readouterr().err == ""


def test_prepare_sets_the_option_from_the_folder(tmp_path, monkeypatch):
    """_prepare applies the folder's mode, and leaves the option alone when no caller says (_cc_noise_dir)."""
    import clearcote
    monkeypatch.setattr(clearcote, "resolve_noise_per_profile",
                        lambda req, d, **k: ("called", req, d, k.get("had_chrome_data")))
    seen = {}

    def fake_args(fp):
        seen["npp"] = fp.get("noise_per_profile")
        raise RuntimeError("stop")

    monkeypatch.setattr(clearcote, "fingerprint_args", fake_args)
    monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: "chrome")
    monkeypatch.setattr(clearcote, "_guard", lambda exe: None)
    for kw, want in ((dict(_cc_noise_dir=(str(tmp_path), True)), ("called", None, str(tmp_path), True)), ({}, None)):
        with pytest.raises(RuntimeError):
            clearcote._prepare(dict(fingerprint="s1", geoip=False, quiet=True, **kw))
        assert seen["npp"] == want
