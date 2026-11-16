"""engine_supports_switch remembers where it found a name.

A launch asks the engine binary for about seven switch names. Each search read the binary from its start, and the
names sit together near the end of a 290 MB chrome.dll: measured on r60 (2026-11-16), 7 probes, 1.2 of the 3.0 s
the first launch of a process took. The probe now keeps the offset of every name it found (in memory, and in
~/.clearcote/engine-switches.json for the next process), checks a known name where it was found, and searches for
a new one from just before the known ones. The answers must stay exactly what a search of the whole file gives,
whatever the positions file says.

Mirrors sdk/node/test/switch-probe.test.ts.
"""
import builtins
import json
import os

import pytest

from clearcote import _launchopts as L
from clearcote._launchopts import engine_supports_switch, reset_switch_probe, switch_positions

MB = 1024 * 1024
CHUNK = 8 * MB
SIZE = 20 * MB


def needle(name):
    return b"\x00" + name.encode("ascii") + b"\x00"


# Where each name sits in the stand-in binary. "near-end" is found first in most tests; the others are before it,
# across a read boundary, or across the place a later search starts from.
AT = {
    "near-end": 18 * MB,
    "also-near-end": 19 * MB + 123,
    "early": 1 * MB + 7,
    "across-a-chunk": CHUNK - 5,
    "across-the-start": 2 * CHUNK - 3,
    "last-bytes": SIZE - len(needle("last-bytes")),
}


def write_engine(directory, body):
    """The file the probe reads: chrome.dll next to the executable on Windows when there is one, else the
    executable itself (as here)."""
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / "chrome"
    p.write_bytes(bytes(body))
    return str(p)


def key_of(path):
    st = os.stat(path)
    return "%s|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime))


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    body = bytearray(b"A" * SIZE)
    for name, at in AT.items():
        n = needle(name)
        body[at:at + len(n)] = n
    # a longer name that contains a shorter one, and a header-like string that is not NUL-delimited
    ext = b"\x00near-end-extended\x00"
    body[3 * MB:3 * MB + len(ext)] = ext
    body[4 * MB:4 * MB + 18] = b"proxy-authenticate"
    return write_engine(tmp_path_factory.mktemp("switch-probe"), body)


class _Counted:
    """A binary file whose reads are counted, to tell a search from a check at a known offset."""

    def __init__(self, fh, io):
        self._fh, self._io = fh, io

    def read(self, n=-1):
        data = self._fh.read(n)
        self._io["bytes_read"] += len(data)
        return data

    def seek(self, *a):
        return self._fh.seek(*a)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return self._fh.__exit__(*a)


@pytest.fixture
def io(monkeypatch, tmp_path):
    """The positions file under tmp_path, a search for a new name starting at the chunk of the first known one,
    and a count of the bytes the probe reads."""
    state = {"bytes_read": 0, "positions": str(tmp_path / "engine-switches.json")}

    def counting_open(file, mode="r", *a, **k):
        fh = builtins.open(file, mode, *a, **k)
        return _Counted(fh, state) if mode == "rb" else fh

    monkeypatch.setattr(L, "open", counting_open, raising=False)
    monkeypatch.setattr(switch_positions, "file", lambda: state["positions"])
    monkeypatch.setattr(switch_positions, "look_back", 0)
    reset_switch_probe()
    yield state
    reset_switch_probe()


def stored(io):
    with open(io["positions"], encoding="utf-8") as fh:
        return json.load(fh)["files"]


def new_process(io):
    """A new process: nothing in memory, the positions file as it is."""
    reset_switch_probe()
    io["bytes_read"] = 0


def write_positions(io, files):
    with open(io["positions"], "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "files": files}, fh)


# -- answers ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("order", [
    list(AT), list(reversed(list(AT))),
    ["early", "near-end", "across-the-start", "across-a-chunk", "last-bytes", "also-near-end"],
])
def test_finds_every_name_wherever_it_sits_in_any_order_and_no_name_that_is_not_there(exe, io, order):
    for name in order:
        assert engine_supports_switch(exe, name) is True, name
    for name in ("missing", "near-en", "ear-end", "proxy-auth", "proxy-authenticate", ""):
        assert engine_supports_switch(exe, name) is False, name
    assert engine_supports_switch(exe, "near-end-extended") is True


def test_a_missing_file_no_path_and_a_directory_answer_false(exe, io, tmp_path):
    assert engine_supports_switch(str(tmp_path / "nope"), "near-end") is False
    assert engine_supports_switch(None, "near-end") is False
    assert engine_supports_switch(str(tmp_path), "near-end") is False
    assert not os.path.exists(io["positions"])


# -- what is remembered ----------------------------------------------------------------------------

def test_the_offset_of_each_name_found_per_binary_and_nothing_for_a_name_that_is_not_there(exe, io):
    for name in AT:
        engine_supports_switch(exe, name)
    engine_supports_switch(exe, "missing")
    assert stored(io) == {key_of(exe): AT}
    assert os.listdir(os.path.dirname(io["positions"])) == ["engine-switches.json"]  # written whole, then renamed


def test_nothing_for_a_small_binary_it_is_searched_whole_every_time(io, tmp_path):
    small = write_engine(tmp_path / "small", b"\x7fELF\x00proxy-auth\x00--remote-debugging-port\x00")
    assert engine_supports_switch(small, "proxy-auth") is True
    assert engine_supports_switch(small, "missing") is False
    assert not os.path.exists(io["positions"])


def test_only_the_newest_binaries_are_kept(io, tmp_path, monkeypatch):
    monkeypatch.setattr(switch_positions, "min_size", 0)
    monkeypatch.setattr(switch_positions, "max_files", 2)
    keys = []
    for n in "abc":
        p = write_engine(tmp_path / ("engine-" + n), b"\x7fELF\x00switch-" + n.encode() + b"\x00")
        assert engine_supports_switch(p, "switch-" + n) is True
        keys.append(key_of(p))
    assert list(stored(io)) == keys[1:]


# -- the next process ------------------------------------------------------------------------------

def test_checks_a_known_name_where_it_was_found_instead_of_searching(exe, io):
    assert engine_supports_switch(exe, "near-end") is True
    assert io["bytes_read"] >= 18 * MB  # the first search reads up to the name
    new_process(io)
    assert engine_supports_switch(exe, "near-end") is True
    assert io["bytes_read"] == len(needle("near-end"))


def test_searches_for_a_new_name_from_the_known_ones_on_then_in_the_rest_of_the_file(exe, io):
    engine_supports_switch(exe, "near-end")
    new_process(io)
    assert engine_supports_switch(exe, "also-near-end") is True
    assert io["bytes_read"] <= 4 * MB + 64  # from 16 MB to the end
    new_process(io)
    assert engine_supports_switch(exe, "early") is True  # before the known ones: the rest of the file
    assert engine_supports_switch(exe, "missing") is False
    assert stored(io)[key_of(exe)] == {"near-end": AT["near-end"], "also-near-end": AT["also-near-end"],
                                       "early": AT["early"]}


def test_a_name_the_binary_lacks_is_searched_for_in_the_whole_file_once_per_process(exe, io):
    engine_supports_switch(exe, "near-end")
    new_process(io)
    assert engine_supports_switch(exe, "missing") is False
    assert io["bytes_read"] >= SIZE
    io["bytes_read"] = 0
    assert engine_supports_switch(exe, "missing") is False
    assert io["bytes_read"] == 0


# -- the positions file is never the answer --------------------------------------------------------

def test_a_wrong_offset_for_a_name_that_is_there_is_found_by_searching_and_corrected(exe, io):
    write_positions(io, {key_of(exe): {"near-end": 5 * MB, "early": AT["near-end"]}})
    assert engine_supports_switch(exe, "near-end") is True
    assert engine_supports_switch(exe, "early") is True
    assert stored(io)[key_of(exe)] == {"near-end": AT["near-end"], "early": AT["early"]}


def test_an_offset_for_a_name_that_is_not_there_is_still_false(exe, io):
    write_positions(io, {key_of(exe): {"missing": AT["near-end"], "near-en": AT["near-end"], "gone": SIZE - 2}})
    for name in ("missing", "near-en", "gone"):
        assert engine_supports_switch(exe, name) is False, name


@pytest.mark.parametrize("text", [
    "{{{",                                # not JSON
    json.dumps([1, 2, 3]),                # another shape
    json.dumps({"v": 1, "files": []}),    # files is a list
    None,                                 # offsets that are no offsets
], ids=["not-json", "another-shape", "files-is-a-list", "no-offsets"])
def test_a_damaged_positions_file_gives_the_same_answers(exe, io, text):
    if text is None:
        text = json.dumps({"v": 1, "files": {key_of(exe): {
            "near-end": -1, "early": 10 ** 12, "across-a-chunk": "12", "last-bytes": 1.5, "also-near-end": None,
            "across-the-start": True}}})
    with open(io["positions"], "w", encoding="utf-8") as fh:
        fh.write(text)
    for name in AT:
        assert engine_supports_switch(exe, name) is True, name
    assert engine_supports_switch(exe, "missing") is False
    assert stored(io)[key_of(exe)] == AT


def test_a_binary_that_changed_starts_again(exe, io, tmp_path):
    engine_supports_switch(exe, "near-end")
    body = bytearray(b"A" * SIZE)
    n = needle("near-end")
    body[2 * MB:2 * MB + len(n)] = n
    other = write_engine(tmp_path / "changed", body)
    # the old binary's positions under the new binary's key would point at the wrong place: checked, then searched
    write_positions(io, {key_of(other): {"near-end": AT["near-end"]}})
    new_process(io)
    assert engine_supports_switch(other, "near-end") is True
    assert engine_supports_switch(other, "early") is False
    assert stored(io)[key_of(other)] == {"near-end": 2 * MB}
    # same path and size, a later modification time: another key
    before = key_of(other)
    later = os.stat(other).st_mtime + 10
    os.utime(other, (later, later))
    assert key_of(other) != before
    new_process(io)
    assert engine_supports_switch(other, "near-end") is True
    assert key_of(other) in stored(io)


def test_a_positions_file_that_cannot_be_written_changes_nothing(exe, io, monkeypatch):
    monkeypatch.setattr(switch_positions, "file", lambda: os.path.join(exe, "cannot", "engine-switches.json"))  # under a FILE
    for name in AT:
        assert engine_supports_switch(exe, name) is True, name
    assert engine_supports_switch(exe, "missing") is False
