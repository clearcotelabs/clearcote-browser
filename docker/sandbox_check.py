#!/usr/bin/env python3
"""Can Chrome's sandbox run in this container? docker/serve.py asks before it starts Chrome.

Chrome's Linux sandbox puts its zygote and every renderer in new user, PID and network namespaces (the
namespace sandbox), then confines them with seccomp-bpf. Docker's default seccomp profile refuses the
namespace calls to a container without CAP_SYS_ADMIN, and Chrome then aborts on its setuid helper, which this
image does not install. So serve.py used to pass --no-sandbox always. Now it runs this probe first: the
sandbox is on when the probe passes, and --no-sandbox, with one log line saying why and how to turn the
sandbox on, when it does not.

The probe makes the calls Chrome's namespace sandbox makes, in the same order, as the same user (measured with
strace on the engine this image ships):
  1. clone(CLONE_NEWUSER), then in the child: map its own uid and gid, drop its capabilities and
     unshare(CLONE_NEWUSER) -- Chrome's check that unprivileged user namespaces work
     (sandbox::Credentials::CanCreateProcessInNewUserNS);
  2. clone(CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET) -- the zygote -- and in it: map the ids,
     chroot("/proc/self/fdinfo/") (Credentials::DropFileSystemAccess) and clone(CLONE_NEWPID) -- a renderer;
  3. in that renderer: a seccomp-bpf filter that, as every Chrome policy does first, kills a process whose
     syscalls arrive as another CPU's, then a syscall under it. Where an emulator translates the image's
     amd64 syscalls into another CPU's, the namespace calls may work and this is what fails.
It runs in its own short-lived Python process, takes a fraction of a second, and needs neither the engine nor
a licence. A trial launch of Chrome would test the same thing more slowly, and a licensed engine does not start
at all before serve.py has leased its run token. The probe meets what Chrome would meet: the seccomp profile,
the container's capabilities, the host's user-namespace settings and AppArmor policy, and an emulator.

Run as a script, it prints "ok" or "fail <step> <errno>", and exits 0 when the sandbox can run.
"""
import ctypes
import errno
import os
import platform
import signal
import subprocess
import sys

CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
_SIGCHLD = 17
_SYS_CLONE = {"x86_64": 56, "aarch64": 220}  # raw clone(2): Chrome calls it directly, not through glibc
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}  # seccomp_data.arch of this CPU's syscalls
_CAP_VERSION_3 = 0x20080522
_PR_SET_SECCOMP, _PR_SET_NO_NEW_PRIVS, _SECCOMP_MODE_FILTER = 22, 38, 2

PROFILE_IN_IMAGE = "/etc/clearcote/seccomp.json"
# --tmpfs: without it `docker run` would first copy the engine (~0.5 GB) into a volume, to read one file
SAVE_PROFILE = ("docker run --rm --tmpfs /opt/xdg-cache --entrypoint cat teamflatearth/clearcote %s "
                "> clearcote-seccomp.json" % PROFILE_IN_IMAGE)
HOW_PROFILE = ("start the container with --security-opt seccomp=<profile>; the profile is in this image at %s "
               "(save it with: %s), and the SDKs' launch() passes it for you" % (PROFILE_IN_IMAGE, SAVE_PROFILE))
_WITH_PROFILE = "run the container with --security-opt seccomp=<profile> (in this image at %s)" % PROFILE_IN_IMAGE

# The steps, as the probe reports them and the log line names them.
STEPS = {
    "clone-user": "clone(CLONE_NEWUSER)",
    "uid-map": "writing a uid/gid map",
    "unshare-user": "unshare(CLONE_NEWUSER)",
    "clone-zygote": "clone(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET)",
    "chroot": 'chroot("/proc/self/fdinfo/")',
    "clone-pid": "clone(CLONE_NEWPID)",
    "seccomp-bpf": "installing a seccomp-bpf filter",
    "seccomp-arch": "a syscall under a seccomp-bpf filter for this CPU",
}
_NAMESPACE_STEPS = ("clone-user", "unshare-user", "clone-zygote", "clone-pid")

# Switches with which Chrome cannot run its namespace sandbox here: it then has to have --no-sandbox.
SANDBOX_OFF_SWITCHES = {
    "--no-sandbox": "--no-sandbox was asked for",
    # "Zygote cannot be disabled if sandbox is enabled": Chrome exits 1 at start
    "--no-zygote": "--no-zygote was asked for, and Chrome runs without its zygote only with --no-sandbox",
    # Chrome then reaches for its setuid helper, which this image does not install, and aborts
    "--disable-namespace-sandbox": "--disable-namespace-sandbox was asked for, and this image has no setuid sandbox",
    # the bridge opens its socket from the renderer, which the sandbox does not allow
    "--canvas-bridge-url": "the canvas bridge needs --no-sandbox",
}


class _Failed(Exception):
    def __init__(self, step, err):
        super().__init__(step, err)
        self.step, self.err = step, err


_report_fd = None  # the pipe a child writes "<step> <errno>" to, before it exits non-zero


def _clone(libc, flags, step):
    """clone(2) with ``flags`` and no new stack, which is fork() as far as the caller can tell -> the
    child's pid in the parent, 0 in the child."""
    nr = _SYS_CLONE.get(platform.machine())
    if nr is None:
        raise _Failed(step, errno.ENOSYS)
    libc.syscall.restype = ctypes.c_long
    pid = libc.syscall(ctypes.c_long(nr), ctypes.c_ulong(flags | _SIGCHLD), ctypes.c_long(0), ctypes.c_long(0),
                       ctypes.c_long(0), ctypes.c_long(0))
    if pid < 0:
        raise _Failed(step, ctypes.get_errno())
    return pid


def _write(path, text):
    fd = os.open(path, os.O_WRONLY)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _map_ids(uid, gid):
    """What Chrome writes in a new user namespace: its own uid and gid, mapped to themselves."""
    try:
        _write("/proc/self/setgroups", "deny")
        _write("/proc/self/uid_map", "%d %d 1\n" % (uid, uid))
        _write("/proc/self/gid_map", "%d %d 1\n" % (gid, gid))
    except OSError as exc:
        raise _Failed("uid-map", exc.errno) from None


def _drop_capabilities(libc):
    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

    if libc.capset(ctypes.byref(Header(_CAP_VERSION_3, 0)), (Data * 2)()) != 0:
        raise _Failed("unshare-user", ctypes.get_errno())


def arch_filter(libc, audit_arch):
    """Install, for this process, the check every Chrome seccomp-bpf policy starts with: a syscall whose
    architecture is not ``audit_arch`` kills the process (SIGSYS); everything else is allowed."""
    class Insn(ctypes.Structure):
        _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]

    class Prog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Insn))]

    insns = (Insn * 4)(Insn(0x20, 0, 0, 4),            # A = seccomp_data.arch
                       Insn(0x15, 0, 1, audit_arch),   # A == audit_arch ? next : skip one
                       Insn(0x06, 0, 0, 0x7FFF0000),   # SECCOMP_RET_ALLOW
                       Insn(0x06, 0, 0, 0x80000000))   # SECCOMP_RET_KILL_PROCESS
    prog = Prog(len(insns), ctypes.cast(insns, ctypes.POINTER(Insn)))
    if libc.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0),
                  ctypes.c_ulong(0)) != 0 \
            or libc.prctl(ctypes.c_int(_PR_SET_SECCOMP), ctypes.c_ulong(_SECCOMP_MODE_FILTER), ctypes.byref(prog),
                          ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
        raise _Failed("seccomp-bpf", ctypes.get_errno())


def _child(body):
    """Run ``body`` in a child clone() just made, and exit: 0, or 1 after reporting the call that failed.
    It never returns into the caller's code."""
    code = 1
    try:
        body()
        code = 0
    except _Failed as exc:
        try:
            os.write(_report_fd, ("%s %d\n" % (exc.step, exc.err)).encode())
        except (OSError, TypeError):
            pass
    except BaseException:  # noqa: BLE001, S110 -- reported as a failure of the step that cloned it
        pass
    os._exit(code)


def _wait(pid, step):
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGSYS:
        raise _Failed("seccomp-arch", 0)  # killed by the filter: its syscalls are not this CPU's
    if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0):
        raise _Failed(step, errno.EPERM)  # the child reported the call that failed in it, if it could


def _run_probe():
    # PyDLL: the GIL stays held across clone(), so a child never has to take it back from another thread.
    libc = ctypes.PyDLL(None, use_errno=True)
    uid, gid = os.geteuid(), os.getegid()

    def check():  # 1. Chrome's check that a user without privileges can use user namespaces
        _map_ids(uid, gid)
        _drop_capabilities(libc)
        if libc.unshare(ctypes.c_int(CLONE_NEWUSER)) != 0:
            raise _Failed("unshare-user", ctypes.get_errno())

    pid = _clone(libc, CLONE_NEWUSER, "clone-user")
    if pid == 0:
        _child(check)
    _wait(pid, "clone-user")

    def renderer():  # 3. the seccomp-bpf filter, and a syscall under it
        arch_filter(libc, _AUDIT_ARCH.get(platform.machine(), 0))
        os.getppid()

    def zygote():  # 2. the zygote: it chroots, then forks each renderer into a new PID namespace
        _map_ids(uid, gid)
        try:
            os.chroot("/proc/self/fdinfo/")
        except OSError as exc:
            raise _Failed("chroot", exc.errno) from None
        child = _clone(libc, CLONE_NEWPID, "clone-pid")
        if child == 0:
            _child(renderer)
        _wait(child, "clone-pid")

    pid = _clone(libc, CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET, "clone-zygote")
    if pid == 0:
        _child(zygote)
    _wait(pid, "clone-zygote")


def _main():
    """The probe's own process: print "ok", or "fail <step> <errno>" for the first call refused."""
    global _report_fd
    read_fd, _report_fd = os.pipe()
    try:
        _run_probe()
        print("ok", flush=True)
        return 0
    except _Failed as exc:
        os.close(_report_fd)  # every child has exited: the read below ends at what they wrote
        words = os.read(read_fd, 4096).decode(errors="replace").split()
        step, err = exc.step, exc.err  # the deepest child's report comes first
        if len(words) >= 2 and words[0] in STEPS and words[1].isdigit():
            step, err = words[0], int(words[1])
        print("fail %s %d" % (step, err), flush=True)
        return 1


def probe(timeout=10.0):
    """-> (True, None) when Chrome's sandbox can run here, else (False, (step, errno)). The probe runs in a
    Python process of its own, so nothing it does can touch the caller."""
    try:
        r = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, ("probe", errno.ETIMEDOUT)
    except OSError as exc:
        return False, ("probe", exc.errno or errno.EIO)
    words = (r.stdout or "").split()
    if r.returncode == 0 and words == ["ok"]:
        return True, None
    if len(words) == 3 and words[0] == "fail" and words[2].isdigit():
        return False, (words[1], int(words[2]))
    return False, ("probe", errno.EIO)


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _seccomp_filtered(read=_read):
    for line in (read("/proc/self/status") or "").splitlines():
        if line.startswith("Seccomp:"):
            return line.split(":", 1)[1].strip() == "2"
    return False


def explain(step, err, read=_read):
    """(why, how) for a failed probe, in words for the log line. A setting of the host comes first: it refuses
    the namespaces whatever seccomp profile the container has (and every container has one)."""
    if step == "probe" and err == errno.ETIMEDOUT:
        what = "the sandbox probe did not finish"
    elif step == "seccomp-arch":
        what = "%s killed the probe" % STEPS[step]
    else:
        what = "%s failed (%s)" % (STEPS.get(step, "the sandbox probe"), os.strerror(err) if err else "unknown error")
    if step in _NAMESPACE_STEPS + ("uid-map", "chroot"):
        if read("/proc/sys/kernel/unprivileged_userns_clone") == "0":
            return (what + ": the host does not let users without privileges create user namespaces "
                    "(kernel.unprivileged_userns_clone=0)",
                    "set kernel.unprivileged_userns_clone=1 on the host, and " + _WITH_PROFILE)
        if read("/proc/sys/user/max_user_namespaces") == "0":
            return (what + ": the host allows no user namespaces (user.max_user_namespaces=0)",
                    "raise user.max_user_namespaces on the host, and " + _WITH_PROFILE)
        if read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") == "1":
            return (what + ": the host's AppArmor policy restricts user namespaces to programs it names "
                    "(kernel.apparmor_restrict_unprivileged_userns=1), which no seccomp profile changes",
                    "let the container create them (kernel.apparmor_restrict_unprivileged_userns=0 on the host, "
                    "or an AppArmor profile for the container that allows userns), and " + _WITH_PROFILE)
    if step.startswith("seccomp-"):
        return (what + ": this container's syscalls are not this CPU's, as where an amd64 image runs emulated on "
                "another CPU, and Chrome's sandbox filter would kill its renderers",
                "run the image on an amd64 machine")
    if err == errno.EINVAL and step in _NAMESPACE_STEPS:
        return (what + ": namespace flags are refused here, as they are where an amd64 image runs emulated on "
                "another CPU", "run the image on an amd64 machine")
    if err == errno.EPERM and step == "chroot":
        return (what + ": the container lacks CAP_SYS_CHROOT (dropped with --cap-drop), which Chrome's sandbox "
                "needs", "keep CAP_SYS_CHROOT (--cap-add SYS_CHROOT), and " + _WITH_PROFILE)
    if err == errno.EPERM and step in _NAMESPACE_STEPS and _seccomp_filtered(read):
        return (what + ": the container's seccomp profile blocks the namespaces Chrome's sandbox needs "
                "(Docker's default profile does)", HOW_PROFILE)
    return what, HOW_PROFILE


def decide(chrome_args, euid, run_probe=probe, read=_read):
    """Whether serve.py runs Chrome with its sandbox -> (True or False, the log line that says so and why).
    ``chrome_args``: the switches serve.py already has for Chrome (CC_EXTRA_ARGS among them)."""
    names = {str(a).split("=", 1)[0] for a in chrome_args}
    for switch, why in SANDBOX_OFF_SWITCHES.items():
        if switch in names:
            return False, "[clearcote] sandbox: off (%s)" % why
    if euid == 0:
        return False, ("[clearcote] sandbox: OFF, Chrome runs with --no-sandbox: the container runs as root, and "
                       "Chrome's sandbox does not run as root. To turn the sandbox on, run the container as the "
                       "image's own user (leave out --user 0), and " + HOW_PROFILE + ".")
    ok, failure = run_probe()
    if ok:
        return True, "[clearcote] sandbox: on (Chrome's user, PID and network namespace sandbox)"
    why, how = explain(*failure, read=read)
    return False, "[clearcote] sandbox: OFF, Chrome runs with --no-sandbox: %s. To turn the sandbox on, %s." % (why, how)


if __name__ == "__main__":
    sys.exit(_main())
