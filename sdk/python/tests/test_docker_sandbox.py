"""The Docker image runs Chrome WITH its sandbox whenever the container allows it.

docker/sandbox_check.py decides it for docker/serve.py: --no-sandbox only when the probe says Chrome's namespace
sandbox cannot run, with one log line saying why and how to turn it on. docker/seccomp.json is the profile that
lets it run: Docker's default plus exactly the two namespace calls the sandbox makes. Loaded from the
repository's docker/ directory; skipped where the tree has none (an installed package). The container itself is
exercised end to end in test_docker_launch_live.py."""
import errno
import hashlib
import importlib.util
import json
import os
import platform
import signal
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
DOCKER = os.path.normpath(os.path.join(HERE, "..", "..", "..", "docker"))
pytestmark = pytest.mark.skipif(not os.path.isdir(DOCKER), reason="no docker/ directory in this tree")

# Docker's default seccomp profile, moby/profiles seccomp v0.2.3 (the one Docker 29.5 to 29.8 build in), as
# sha256 of json.dumps(profile, sort_keys=True, separators=(",", ":")).
DOCKER_DEFAULT_V0_2_3 = "9da637d2ab0a204fcbd91bd88f1be9e004a3acab61c571a9f5b8870e588a17d2"
CLONE_NEWUSER, CLONE_NEWPID, CLONE_NEWNET = 0x10000000, 0x20000000, 0x40000000
CLONE_NEWNS, CLONE_NEWCGROUP, CLONE_NEWUTS, CLONE_NEWIPC = 0x00020000, 0x02000000, 0x04000000, 0x08000000


def _check():
    spec = importlib.util.spec_from_file_location("sandbox_check", os.path.join(DOCKER, "sandbox_check.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _profile():
    with open(os.path.join(DOCKER, "seccomp.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _no_probe():
    raise AssertionError("the probe must not run")


# ── the decision ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_sandbox_is_on_when_the_probe_passes():
    on, line = _check().decide([], 10001, run_probe=lambda: (True, None))
    assert on is True and line == "[clearcote] sandbox: on (Chrome's user, PID and network namespace sandbox)"


def test_without_the_profile_it_falls_back_and_says_why_and_how_in_one_line():
    sc = _check()
    status = {"/proc/self/status": "Name:\tpython\nSeccomp:\t2\nSeccomp_filters:\t1\n"}
    on, line = sc.decide([], 10001, run_probe=lambda: (False, ("clone-user", errno.EPERM)), read=status.get)
    assert on is False and "\n" not in line
    assert line.startswith("[clearcote] sandbox: OFF, Chrome runs with --no-sandbox: clone(CLONE_NEWUSER) failed")
    assert "seccomp profile blocks" in line  # why
    assert "--security-opt seccomp=" in line and sc.PROFILE_IN_IMAGE in line  # how
    assert sc.PROFILE_IN_IMAGE == "/etc/clearcote/seccomp.json"


def test_a_no_sandbox_the_caller_passed_is_honoured_without_probing():
    on, line = _check().decide(["--lang=de", "--no-sandbox"], 10001, run_probe=_no_probe)
    assert on is False and line == "[clearcote] sandbox: off (--no-sandbox was asked for)"


def test_the_canvas_bridge_keeps_no_sandbox():
    # the bridge opens its socket from the renderer, which the sandbox (its own network namespace) does not allow
    on, line = _check().decide(["--canvas-bridge-url=ws://gpu-host:9099"], 10001, run_probe=_no_probe)
    assert on is False and line == "[clearcote] sandbox: off (the canvas bridge needs --no-sandbox)"


@pytest.mark.parametrize("args,why", [
    # Chrome: "Zygote cannot be disabled if sandbox is enabled", exit 1
    (["--no-zygote"], "--no-zygote was asked for"),
    # Chrome falls back to the setuid helper, which the image does not install, and aborts
    (["--disable-namespace-sandbox"], "--disable-namespace-sandbox was asked for"),
    (["--lang=de", "--no-zygote=1"], "--no-zygote was asked for"),
])
def test_switches_chrome_cannot_run_its_sandbox_with_turn_it_off_without_probing(args, why):
    on, line = _check().decide(args, 10001, run_probe=_no_probe)
    assert on is False and line.startswith("[clearcote] sandbox: off (" + why)


def test_root_runs_without_the_sandbox_because_chrome_refuses_it_there():
    on, line = _check().decide([], 0, run_probe=_no_probe)
    assert on is False and "runs as root" in line and "--security-opt seccomp=" in line


def test_a_host_that_restricts_user_namespaces_is_named_before_the_seccomp_profile():
    # Every container has a seccomp filter, the profile included: with the profile on an AppArmor-restricted host
    # the line must name the host's policy, not tell the user to pass the profile they passed.
    files = {"/proc/self/status": "Seccomp:\t2\n", "/proc/sys/kernel/apparmor_restrict_unprivileged_userns": "1"}
    on, line = _check().decide([], 10001, run_probe=lambda: (False, ("uid-map", errno.EPERM)), read=files.get)
    assert on is False and "AppArmor policy restricts user namespaces" in line and "seccomp profile blocks" not in line
    assert "kernel.apparmor_restrict_unprivileged_userns=0" in line


def test_the_profile_is_saved_without_copying_the_engine_volume():
    sc = _check()
    assert "--tmpfs /opt/xdg-cache" in sc.SAVE_PROFILE and sc.SAVE_PROFILE in sc.HOW_PROFILE


@pytest.mark.parametrize("failure,files,said", [
    (("clone-user", errno.EPERM), {"/proc/sys/kernel/unprivileged_userns_clone": "0"},
     "kernel.unprivileged_userns_clone=0"),
    (("clone-user", errno.ENOSPC), {"/proc/sys/user/max_user_namespaces": "0"}, "user.max_user_namespaces=0"),
    (("clone-zygote", errno.EINVAL), {}, "emulated"),
    (("chroot", errno.EPERM), {}, "CAP_SYS_CHROOT"),
    (("clone-user", errno.EACCES), {"/proc/sys/kernel/apparmor_restrict_unprivileged_userns": "1"}, "AppArmor"),
    (("probe", errno.ETIMEDOUT), {}, "the sandbox probe did not finish"),
    (("probe", errno.EIO), {}, "the sandbox probe failed"),
    (("seccomp-arch", 0), {}, "runs emulated on another CPU"),
    (("seccomp-bpf", errno.EINVAL), {}, "installing a seccomp-bpf filter failed"),
])
def test_each_reason_is_named(failure, files, said):
    on, line = _check().decide([], 10001, run_probe=lambda: (False, failure), read=files.get)
    assert on is False and said in line and "To turn the sandbox on, " in line


@pytest.mark.parametrize("stdout,code,result", [
    ("ok\n", 0, (True, None)),
    ("fail unshare-user 1\n", 1, (False, ("unshare-user", 1))),
    ("fail seccomp-arch 0\n", 1, (False, ("seccomp-arch", 0))),
    ("Traceback (most recent call last):\n", 1, (False, ("probe", errno.EIO))),
    ("ok\n", 1, (False, ("probe", errno.EIO))),
])
def test_the_probe_process_answer_is_read(monkeypatch, stdout, code, result):
    sc = _check()
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return type("R", (), {"returncode": code, "stdout": stdout})()

    monkeypatch.setattr(sc.subprocess, "run", run)
    assert sc.probe() == result
    assert seen == [[sys.executable, os.path.join(DOCKER, "sandbox_check.py")]]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux namespaces")
def test_the_probe_runs_here():
    # Whatever this machine allows, the probe answers in the form serve.py reads (the container behaviour, on and
    # off, is test_docker_launch_live.py's).
    ok, failure = _check().probe()
    assert (ok, failure) == (True, None) or (ok is False and failure[0] in _check().STEPS)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux seccomp-bpf")
@pytest.mark.parametrize("arch,code", [("own", 0), ("other", -getattr(signal, "SIGSYS", 31))])
def test_the_filter_kills_a_process_whose_syscalls_are_another_cpus(arch, code):
    # What an emulated amd64 image meets under Chrome's renderer filter, simulated the other way round: a filter
    # for another CPU kills the process at its next syscall; one for its own CPU lets it run.
    own = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}[platform.machine()]
    other = 0xC00000B7 if own == 0xC000003E else 0xC000003E
    script = ("import ctypes, os, sandbox_check as s\n"
              "s.arch_filter(ctypes.PyDLL(None, use_errno=True), %d)\n"
              "os.getppid()\nos._exit(0)\n" % (own if arch == "own" else other))
    r = subprocess.run([sys.executable, "-c", script], cwd=DOCKER, capture_output=True, timeout=30)
    assert r.returncode == code, r.stderr


# ── the profile ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_profile_is_dockers_default_plus_only_the_namespace_calls_chromes_sandbox_makes():
    profile = _profile()
    added = [s for s in profile["syscalls"] if str(s.get("comment", "")).startswith("clearcote:")]
    rest = dict(profile, syscalls=[s for s in profile["syscalls"] if s not in added])
    canon = json.dumps(rest, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canon).hexdigest() == DOCKER_DEFAULT_V0_2_3  # the rest is Docker's, unchanged
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    clone, unshare = added
    assert clone["names"] == ["clone"] and clone["action"] == "SCMP_ACT_ALLOW"
    [arg] = clone["args"]
    assert (arg["index"], arg["op"], arg.get("valueTwo", 0)) == (0, "SCMP_CMP_MASKED_EQ", 0)
    # clone may now create user, PID and network namespaces (the calls Chrome makes), and still no other kind
    assert arg["value"] == CLONE_NEWNS | CLONE_NEWCGROUP | CLONE_NEWUTS | CLONE_NEWIPC
    assert not arg["value"] & (CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET)
    assert clone["excludes"] == {"arches": ["s390", "s390x"]}  # where clone() takes its flags second
    # unshare: exactly CLONE_NEWUSER (Chrome's own check), nothing else
    assert unshare["names"] == ["unshare"] and unshare["action"] == "SCMP_ACT_ALLOW"
    assert unshare["args"] == [{"index": 0, "value": CLONE_NEWUSER, "op": "SCMP_CMP_EQ"}]
    assert "includes" not in unshare and "excludes" not in unshare


def test_the_image_ships_the_profile_and_the_probe():
    with open(os.path.join(DOCKER, "Dockerfile"), encoding="utf-8") as fh:
        dockerfile = fh.read()
    assert "COPY seccomp.json /etc/clearcote/seccomp.json" in dockerfile
    copy = next(ln for ln in dockerfile.splitlines() if ln.startswith("COPY serve.py"))
    assert "sandbox_check.py" in copy.split()
    assert 'LABEL com.clearcotelabs.serve-protocol="3"' in dockerfile


def test_serve_py_asks_the_probe_and_passes_no_sandbox_only_when_it_says_no():
    with open(os.path.join(DOCKER, "serve.py"), encoding="utf-8") as fh:
        serve = fh.read()
    assert "sandbox_check.decide(args + extra, os.geteuid())" in serve
    assert '(["--no-sandbox"] if not sandbox and "--no-sandbox" not in args + extra else [])' in serve
    assert serve.count('"--no-sandbox"') == 2  # that line, and nowhere else
    assert '"sandbox": sandbox,' in serve and "SERVE_PROTOCOL = 3" in serve
