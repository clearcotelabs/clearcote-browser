import json
import os

import pytest

from clearcote._noisemode import PROFILE_STATE_FILE, resolve_noise_per_profile
from clearcote._schemamode import read_schema, resolve_persona_schema, schema_in_args

R51 = dict(supports=lambda e: True, quiet=True)
OLD = dict(supports=lambda e: False, quiet=True)


def _state(d):
    with open(os.path.join(d, PROFILE_STATE_FILE), encoding="utf-8") as f:
        return json.load(f)


def _chrome_profile(d):
    open(os.path.join(d, "Local State"), "w").write("{}")
    os.mkdir(os.path.join(d, "Default"))
    return str(d)


def test_new_empty_folder_gets_schema_8_and_remembers(tmp_path):
    assert resolve_persona_schema(None, str(tmp_path), **R51) == "8"
    assert _state(tmp_path) == {"schema": "8"}
    # later launches keep it, even once the browser has written its profile
    _chrome_profile(tmp_path)
    assert resolve_persona_schema(None, str(tmp_path), **R51) == "8"


def test_missing_folder_is_created_and_remembered(tmp_path):
    d = str(tmp_path / "new-profile")
    assert resolve_persona_schema(None, d, **R51) == "8"
    assert read_schema(d) == "8"


def test_folder_made_before_this_release_keeps_7_and_adds_nothing(tmp_path):
    d = _chrome_profile(tmp_path)
    assert resolve_persona_schema(None, d, **R51) is None  # no switch: byte-identical to the earlier SDK
    assert _state(d) == {"schema": "7"}
    assert resolve_persona_schema(None, d, **R51) is None


def test_had_chrome_data_snapshot_wins(tmp_path):
    os.mkdir(tmp_path / "Default")  # seeded by the SDK this launch
    assert resolve_persona_schema(None, str(tmp_path), had_chrome_data=False, **R51) == "8"
    other = tmp_path / "other"
    other.mkdir()
    assert resolve_persona_schema(None, str(other), had_chrome_data=True, **R51) is None


def test_engine_without_the_persona_service_gets_7_and_keeps_it(tmp_path):
    assert resolve_persona_schema(None, str(tmp_path), **OLD) is None
    assert read_schema(str(tmp_path)) == "7"
    assert resolve_persona_schema(None, str(tmp_path), **R51) is None


def test_explicit_value_wins_and_is_remembered(tmp_path):
    d = _chrome_profile(tmp_path)
    assert resolve_persona_schema(8, d, **R51) == 8  # returned as given
    assert read_schema(d) == "8"
    assert resolve_persona_schema(None, d, **R51) == "8"
    assert resolve_persona_schema("5", d, **R51) == "5"
    assert read_schema(d) == "5"


def test_schema_pinned_in_args_wins_adds_nothing_and_is_remembered(tmp_path):
    d = str(tmp_path)
    assert resolve_persona_schema(None, d, args=["--x", "--fingerprint-schema=4"], **R51) is None
    assert read_schema(d) == "4"
    assert schema_in_args(["--fingerprint-schema=5", "--fingerprint-schema=7"]) == "7"
    assert schema_in_args(["--fingerprint-schema=bad"]) is None


def test_no_folder_is_new_and_writes_nothing():
    assert resolve_persona_schema(None, None, **R51) == "8"
    assert resolve_persona_schema(None, None, **OLD) is None


def test_passthrough_left_alone(tmp_path):
    assert resolve_persona_schema(None, str(tmp_path), passthrough=True, **R51) is None
    assert not os.path.exists(os.path.join(tmp_path, PROFILE_STATE_FILE))


def test_shares_the_file_with_the_noise_mode(tmp_path):
    d = str(tmp_path)
    resolve_noise_per_profile(None, d, supports=lambda e: True, quiet=True)
    resolve_persona_schema(None, d, **R51)
    assert _state(d) == {"noise": "per-profile", "schema": "8"}
    # a bad value in the file is ignored, the rest kept
    with open(os.path.join(d, PROFILE_STATE_FILE), "w", encoding="utf-8") as f:
        json.dump({"noise": "per-site", "schema": "x"}, f)
    _chrome_profile(tmp_path)
    assert resolve_persona_schema(None, d, **R51) is None
    assert _state(d) == {"noise": "per-site", "schema": "7"}


def test_unwritable_never_fails_and_says_so_unless_quiet(tmp_path, capsys):
    (tmp_path / PROFILE_STATE_FILE).mkdir()
    assert resolve_persona_schema(None, str(tmp_path), supports=lambda e: True) == "8"
    assert "could not remember the persona schema" in capsys.readouterr().err
    assert resolve_persona_schema(None, str(tmp_path), **R51) == "8"
    assert capsys.readouterr().err == ""


def test_prepare_sets_the_option_from_the_folder(tmp_path, monkeypatch):
    """_prepare applies the folder's schema, and leaves the option alone when no caller says (_cc_noise_dir)."""
    import clearcote
    monkeypatch.setattr(clearcote, "resolve_noise_per_profile", lambda req, d, **k: None)
    monkeypatch.setattr(clearcote, "resolve_persona_schema",
                        lambda req, d, **k: "8" if k.get("args") == ["--a"] and not k.get("had_chrome_data") else "x")
    seen = {}

    def fake_args(fp):
        seen["ps"] = fp.get("persona_schema")
        raise RuntimeError("stop")

    monkeypatch.setattr(clearcote, "fingerprint_args", fake_args)
    monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: "chrome")
    monkeypatch.setattr(clearcote, "_guard", lambda exe: None)
    for kw, want in ((dict(_cc_noise_dir=(str(tmp_path), False)), "8"), ({}, None)):
        with pytest.raises(RuntimeError):
            clearcote._prepare(dict(fingerprint="s1", geoip=False, quiet=True, args=["--a"], **kw))
        assert seen["ps"] == want


def test_engine_option_row_for_8_exists():
    from clearcote._launchopts import _ENGINE_OPTION_SWITCHES
    assert ("persona_schema", "8", "cc-persona-host-facts") in [r[:3] for r in _ENGINE_OPTION_SWITCHES]
