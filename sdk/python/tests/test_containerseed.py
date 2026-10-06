import os

from clearcote._containerseed import LEGACY_SHARED_SEED, SEED_FILE, container_seed


def test_env_value_wins(tmp_path):
    assert container_seed("user-7423", str(tmp_path)) == ("user-7423", "env")
    assert not (tmp_path / SEED_FILE).exists()


def test_the_old_shared_seed_is_still_honoured_when_asked_for(tmp_path):
    assert container_seed(LEGACY_SHARED_SEED, str(tmp_path)) == ("clearcote-docker", "env")


def test_unset_generates_a_random_seed_and_keeps_it(tmp_path):
    # Two containers (two profile dirs) must not share an identity -- the old default made every
    # container without CC_FINGERPRINT the same device.
    a, src_a = container_seed(None, str(tmp_path / "a"))
    b, src_b = container_seed(None, str(tmp_path / "b"))
    assert src_a == src_b == "new"
    assert a != b
    assert a != LEGACY_SHARED_SEED and a.startswith("cc-")
    # ...and one container keeps its own across restarts.
    assert container_seed(None, str(tmp_path / "a")) == (a, "saved")


def test_an_empty_value_keeps_its_old_meaning(tmp_path):
    # Set-but-empty used to give no seed at all; compose files that rely on that keep working.
    assert container_seed("", str(tmp_path)) == ("", "env")
    assert not (tmp_path / SEED_FILE).exists()


def test_unwritable_profile_dir_still_returns_a_seed(tmp_path, monkeypatch):
    def deny(*_a, **_k):
        raise OSError("read-only")
    monkeypatch.setattr(os, "makedirs", deny)
    seed, src = container_seed(None, str(tmp_path / "ro" / "deeper"))
    assert src == "new" and seed.startswith("cc-")
