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
     chroot("/proc/self/fdinfo/") (Credentials::DropFileSystemAccess) and clone(CLONE_NEWPID) -- each renderer.
It runs in its own short-lived Python process, takes a fraction of a second, and needs neither the engine nor
a licence. A trial launch of Chrome would test the same thing more slowly, and a licensed engine does not start
at all before serve.py has leased its run token. The probe meets what Chrome would meet: the seccomp profile,
the container's capabilities, the host's user-namespace settings and AppArmor policy, and an emulator that
refuses namespace flags.

Run as a script, it prints "ok" or "fail <step> <errno>", and exits 0 when the sandbox can run.
"""
import ctypes
import errno
import os
import platform
import subprocess
import sys

CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000
_SIGCHLD = 17
_SYS_CLONE = {"x86_64": 56, "aarch64": 220}  # raw clone(2): Chrome calls it directly, not through glibc
_CAP_VERSION_3 = 0x20080522

PROFILE_IN_IMAGE = "/etc/clearcote/seccomp.json"
HOW_PROFILE = ("start the container with --security-opt seccomp=<profile>; the profile is in this image at %s "
               "(save it with: docker run --rm --entrypoint cat teamflatearth/clearcote %s > clearcote-seccomp.json), "
               "and the SDKs' launch() passes it for you" % (PROFILE_IN_IMAGE, PROFILE_IN_IMAGE))

# The steps, as the probe reports them and the log line names them.
STEPS = {
    "clone-user": "clone(CLONE_NEWUSER)",
    "uid-map": "writing a uid/gid map",
    "unshare-user": "unshare(CLONE_NEWUSER)",
    "clone-zygote": "clone(CLONE_NEWUSER|CLONE_NEWPID|CLONE_NEWNET)",
    "chroot": 'chroot("/proc/self/fdinfo/")',
    "clone-pid": "clone(CLONE_NEWPID)",
}
_NAMESPACE_STEPS = ("clone-user", "unshare-user", "clone-zygote", "clone-pid")


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

    def zygote():  # 2. the zygote: it chroots, then forks each renderer into a new PID namespace
        _map_ids(uid, gid)
        try:
            os.chroot("/proc/self/fdinfo/")
        except OSError as exc:
            raise _Failed("chroot", exc.errno) from None
        renderer = _clone(libc, CLONE_NEWPID, "clone-pid")
        if renderer == 0:
            os._exit(0)
        _wait(renderer, "clone-pid")

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
        step, err = exc.step, exc.err
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
    """(why, how) for a failed probe, in words for the log line."""
    if step == "probe" and err == errno.ETIMEDOUT:
        what = "the sandbox probe did not finish"
    else:
        what = "%s failed (%s)" % (STEPS.get(step, "the sandbox probe"), os.strerror(err) if err else "unknown error")
    if read("/proc/sys/kernel/unprivileged_userns_clone") == "0":
        return (what + ": the host does not let users without privileges create user namespaces "
                "(kernel.unprivileged_userns_clone=0)",
                "set kernel.unprivileged_userns_clone=1 on the host, and " + HOW_PROFILE)
    if read("/proc/sys/user/max_user_namespaces") == "0":
        return (what + ": the host allows no user namespaces (user.max_user_namespaces=0)",
                "raise user.max_user_namespaces on the host, and " + HOW_PROFILE)
    if err == errno.EINVAL and step in _NAMESPACE_STEPS:
        return (what + ": namespace flags are refused here, as they are where an amd64 image runs emulated on "
                "another CPU", "run the image on an amd64 machine")
    if err == errno.EPERM and step in _NAMESPACE_STEPS and _seccomp_filtered(read):
        return (what + ": the container's seccomp profile blocks the namespaces Chrome's sandbox needs "
                "(Docker's default profile does)", HOW_PROFILE)
    if err == errno.EPERM and step == "chroot":
        return (what + ": the container lacks CAP_SYS_CHROOT (dropped with --cap-drop), which Chrome's sandbox "
                "needs", "keep CAP_SYS_CHROOT (--cap-add SYS_CHROOT), and " + HOW_PROFILE)
    if read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") == "1":
        return (what + ": the host's AppArmor policy restricts user namespaces "
                "(kernel.apparmor_restrict_unprivileged_userns=1)",
                "allow user namespaces for the container, and " + HOW_PROFILE)
    return what, HOW_PROFILE


def decide(chrome_args, euid, run_probe=probe, read=_read):
    """Whether serve.py runs Chrome with its sandbox -> (True or False, the log line that says so and why).
    ``chrome_args``: the switches serve.py already has for Chrome (CC_EXTRA_ARGS among them)."""
    if "--no-sandbox" in chrome_args:
        return False, "[clearcote] sandbox: off (--no-sandbox was asked for)"
    if any(str(a).startswith("--canvas-bridge-url=") for a in chrome_args):
        # the bridge opens its socket from the renderer, which the sandbox does not allow
        return False, "[clearcote] sandbox: off (the canvas bridge needs --no-sandbox)"
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
