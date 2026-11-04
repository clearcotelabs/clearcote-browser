"""The persona host-facts probe says why it got no answer (CR-2VFYCD)."""
import subprocess
import sys

import pytest

from clearcote import _persona

REFUSAL = "[clearcote] no valid license lease for this run. Set a license key (CLEARCOTE_LICENSE_KEY) via the SDK and check your plan's concurrency limit.\r\n"


def test_returns_stdout_of_a_clean_run():
    assert "nvidia" in _persona.describe_probe('{"gpu_vendor":"nvidia"}\n', "", 0)


def test_names_the_engines_refusal():
    with pytest.raises(RuntimeError, match="it exited with code 1: no valid license lease for this run"):
        _persona.describe_probe("", REFUSAL, 1)


def test_names_a_clean_exit_without_output():
    with pytest.raises(RuntimeError, match="it exited without output"):
        _persona.describe_probe(" \n", "", 0)


def test_names_a_signal():
    with pytest.raises(RuntimeError, match="stopped by signal 9"):
        _persona.describe_probe("", "", -9)


def test_names_a_binary_that_could_not_start(tmp_path):
    with pytest.raises(RuntimeError, match="it could not be started"):
        _persona._run_probe([str(tmp_path / "missing-chrome"), "--cc-persona-host-facts"], {})


def test_names_a_timeout(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired("chrome", 30)
    monkeypatch.setattr(_persona.subprocess, "run", slow)
    with pytest.raises(RuntimeError, match="did not answer within 30 s"):
        _persona._run_probe(["chrome", "--cc-persona-host-facts"], {})


def test_reads_a_real_process():
    cmd = [sys.executable, "-c", "import sys; print('log'); print('{\"gpu_vendor\": \"amd\"}')"]
    assert _persona._run_probe(cmd, None).strip().endswith('{"gpu_vendor": "amd"}')


def test_reports_stderr_of_a_real_refusing_process():
    cmd = [sys.executable, "-c", "import sys; print('[clearcote] refused here', file=sys.stderr); sys.exit(1)"]
    with pytest.raises(RuntimeError, match="it exited with code 1: refused here"):
        _persona._run_probe(cmd, None)


def test_retries_from_a_recovered_copy_on_the_windows_race(monkeypatch):
    monkeypatch.setattr(_persona.sys, "platform", "win32")
    import clearcote._winlaunch as wl
    monkeypatch.setattr(wl, "recovered_exe", lambda exe: None)
    monkeypatch.setattr(wl, "make_recovered_copy", lambda exe, warm=None: "C:/recovered/chrome.exe")
    seen = []

    def run(exe):
        seen.append(exe)
        if exe == "C:/cache/chrome.exe":
            e = OSError("The application has failed to start because its side-by-side configuration is incorrect")
            e.winerror = 14001
            raise e
        return "ok"
    assert _persona._spawn_probe("C:/cache/chrome.exe", run) == "ok"
    assert seen == ["C:/cache/chrome.exe", "C:/recovered/chrome.exe"]
