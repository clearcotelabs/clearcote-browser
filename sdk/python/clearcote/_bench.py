"""``clearcote info``'s Host section and ``clearcote bench``: what a support question needs to know about
the machine, and what N browsers cost on it.

Why: "100 % CPU with 5 threads and pages never finish" has three usual causes -- too many browsers for
the CPU, no hardware GPU (Chromium then draws every page on the CPU), or a wait condition ('load',
'networkidle') that a slow proxy or a chatty page never satisfies. ``bench`` measures all three on the
user's own machine, page and proxy, and prints one block they can paste.

The cost per browser is measured from the browser's own process tree (the browser process, found by
the profile directory bench gives it, and every process below it: renderers, GPU, network), not from
whole-machine counters, so whatever else runs on the machine does not end up in the figure.

Output is part of the contract: the Node SDK's bench.ts prints the same lines from the same report
(tests/test_bench.py and test/bench.test.ts feed both the same fixture). Change a line here, change it
there too.
"""
from __future__ import annotations

import asyncio
import math
import os
import platform as _platform
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Adapters that cannot accelerate Chromium: Windows' fallback drivers, hypervisor and BMC display chips,
# Mesa's CPU renderers.
SOFTWARE_GPU = re.compile(r"basic display|basic render|remote display|hyper-v video|vmware svga|virtualbox graphics|"
                          r"qxl|cirrus|bochs|llvmpipe|softpipe|swiftshader|virtio|standard vga|aspeed|matrox|"
                          r"parsec virtual|idd", re.I)
VIRTUAL_HW = re.compile(r"vmware|virtualbox|qemu|kvm|xen|virtual machine|hvm domu|amazon ec2|google compute|droplet|"
                        r"openstack|bochs|parallels|hetzner|vultr|linode", re.I)

WINDOWS_DISPLAY_CLASS = r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def _run(cmd, timeout=15):
    try:
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _read(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _threads():
    return os.cpu_count() or 1


def _reg_values(key, *args):
    """``reg query`` values as ``[(name, data)]`` (every value of every subkey with ``/s``). The registry,
    not WMI: PowerShell's Get-CimInstance takes 1-2 s to start, reg.exe answers in ~50 ms."""
    out = []
    for line in _run(["reg", "query", key, *args]).splitlines():
        m = re.match(r"^\s+(\S+)\s+REG_\w+\s+(.*)$", line)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def _dword(v):
    """A REG_DWORD as reg.exe prints it ("0xa") in decimal, or "" when missing."""
    return str(int(v, 16)) if v and re.fullmatch(r"0x[0-9a-fA-F]+", v) else ""


def windows_name(product, major, minor, build):
    """"Windows 11 Pro (10.0.26200)": ProductName still says "Windows 10" on Windows 11, whose builds
    start at 22000."""
    name = product.replace("Windows 10", "Windows 11", 1) if build.isdigit() and int(build) >= 22000 else product
    return f"{name or 'Windows'} ({major or '10'}.{minor or '0'}.{build or '?'})"


def _windows_memory():
    """GlobalMemoryStatusEx's total and available physical memory in bytes (what Node's os.totalmem and
    os.freemem report)."""
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    s = MEMORYSTATUSEX()
    s.dwLength = ctypes.sizeof(s)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s)):
        return 0, 0
    return s.ullTotalPhys, s.ullAvailPhys


def _windows_host():
    nt = dict(_reg_values(r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
    bios = dict(_reg_values(r"HKLM\HARDWARE\DESCRIPTION\System\BIOS"))
    cpu = dict(_reg_values(r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0"))
    gpu = list(dict.fromkeys(v for _k, v in _reg_values(WINDOWS_DISPLAY_CLASS, "/s", "/v", "DriverDesc") if v))
    hw = f"{bios.get('SystemManufacturer', '')} {bios.get('SystemProductName', '')}".strip()
    total, free = _windows_memory()
    return {
        "cpu": {"model": (cpu.get("ProcessorNameString") or _platform.processor() or "unknown").strip(),
                "threads": _threads()},
        "memory": {"totalMB": _half_up(total / 2 ** 20), "freeMB": _half_up(free / 2 ** 20)},
        "os": windows_name(nt.get("ProductName", ""), _dword(nt.get("CurrentMajorVersionNumber")),
                           _dword(nt.get("CurrentMinorVersionNumber")), nt.get("CurrentBuild", "")),
        "virtual": f"virtual machine ({hw})" if VIRTUAL_HW.search(hw) else None,
        "remoteSession": os.environ.get("SESSIONNAME", "").lower().startswith("rdp-"),
        "gpu": {"adapters": gpu, "software": not any(not SOFTWARE_GPU.search(x) for x in gpu)},
    }


def _linux_host():
    mem = _read("/proc/meminfo")

    def kb(k):
        m = re.search(rf"^{k}:\s+(\d+)", mem, re.M)
        return int(m.group(1)) if m else 0

    m = re.search(r"^model name\s*:\s*(.+)$", _read("/proc/cpuinfo"), re.M)
    model = m.group(1).strip() if m else (_platform.processor() or "unknown")
    m = re.search(r'^PRETTY_NAME="?([^"\n]*)"?', _read("/etc/os-release"), re.M)
    pretty = m.group(1) if m else "Linux"
    hw = f"{_read('/sys/class/dmi/id/sys_vendor').strip()} {_read('/sys/class/dmi/id/product_name').strip()}".strip()
    container = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    adapters = [re.sub(r"^\S+\s+[^:]+:\s*", "", line).strip()
                for line in _run(["lspci"]).split("\n") if re.search(r"vga|3d controller|display controller", line, re.I)]
    try:
        dri = any(n.startswith("renderD") for n in os.listdir("/dev/dri"))
    except OSError:
        dri = False
    if not adapters and dri:
        adapters = ["render node in /dev/dri"]
    return {
        "cpu": {"model": model, "threads": _threads()},
        "memory": {"totalMB": _half_up(kb("MemTotal") / 1024), "freeMB": _half_up(kb("MemAvailable") / 1024)},
        "os": f"{pretty} (kernel {_platform.release()})",
        "virtual": "container" if container else (f"virtual machine ({hw})" if VIRTUAL_HW.search(hw) else None),
        "remoteSession": False,
        "gpu": {"adapters": adapters, "software": not dri or not any(not SOFTWARE_GPU.search(x) for x in adapters)},
    }


def _mac_host():
    def sysctl(k):
        return _run(["sysctl", "-n", k]).strip()

    adapters = [m.group(1).strip() for m in re.finditer(r"Chipset Model:\s*(.+)", _run(["system_profiler", "SPDisplaysDataType"], 30))]
    free = 0
    vm = _run(["vm_stat"])
    page = re.search(r"page size of (\d+)", vm)
    for k in ("Pages free", "Pages inactive", "Pages speculative"):
        m = re.search(rf"{k}:\s+(\d+)", vm)
        free += int(m.group(1)) if m else 0
    return {
        "cpu": {"model": sysctl("machdep.cpu.brand_string") or _platform.processor() or "unknown", "threads": _threads()},
        "memory": {"totalMB": _half_up(int(sysctl("hw.memsize") or 0) / 2 ** 20),
                   "freeMB": _half_up(free * int(page.group(1) if page else 4096) / 2 ** 20)},
        "os": f"macOS {_run(['sw_vers', '-productVersion']).strip() or _platform.release()}",
        "virtual": "virtual machine" if sysctl("kern.hv_vmm_present") == "1" else None,
        "remoteSession": False,
        "gpu": {"adapters": adapters, "software": not any(not SOFTWARE_GPU.search(x) for x in adapters)},
    }


_HOST_ONCE = []  # [report]: the CPU, OS, VM and GPU, read once per process


def _free_mb():
    if sys.platform == "win32":
        return _half_up(_windows_memory()[1] / 2 ** 20)
    if sys.platform == "darwin":
        return _mac_host()["memory"]["freeMB"]
    m = re.search(r"^MemAvailable:\s+(\d+)", _read("/proc/meminfo"), re.M)
    return _half_up(int(m.group(1)) / 1024) if m else 0


def host_info():
    """The machine: CPU, RAM, OS, VM or not, and whether there is a GPU Chromium can draw with. Local only.

    The CPU, OS, VM and GPU do not change while a process runs, and reading them spawns reg / lspci /
    system_profiler: they are read once. Free memory changes, and is read again on every call."""
    import copy
    first = not _HOST_ONCE
    if first:
        _HOST_ONCE.append(_windows_host() if sys.platform == "win32" else _mac_host() if sys.platform == "darwin"
                          else _linux_host())
    h = copy.deepcopy(_HOST_ONCE[0])
    if not first:
        h["memory"]["freeMB"] = _free_mb()
    return h


# Every figure is rounded half-up before it is formatted: Python's format and JS toFixed round ties
# differently (0.25 -> "0.2" vs "0.3"), and both SDKs must print the same text.
def _half_up(x):
    return int(math.floor(x + 0.5))


def _round1(x):
    return math.floor(x * 10 + 0.5) / 10


def _tenths(x):
    return f"{math.floor(x * 10 + 0.5) / 10:.1f}"


def _gb(mb):
    return _tenths(mb / 1024)


def host_lines(h):
    """The Host lines of ``clearcote info`` (and the top of ``clearcote bench``)."""
    lines = [
        f"Host            {h['cpu']['model']}, {h['cpu']['threads']} threads, {_gb(h['memory']['totalMB'])} GB RAM "
        f"({_gb(h['memory']['freeMB'])} GB free)",
        f"                {h['os']}, {h['virtual'] or 'physical machine'}"
        + (", Remote Desktop session" if h["remoteSession"] else ""),
    ]
    g = h["gpu"]
    if not g["adapters"]:
        lines.append("GPU             none found: Chromium draws every page on the CPU")
    elif g["software"]:
        lines.append(f"GPU             {', '.join(g['adapters'])} (no 3D acceleration: Chromium draws every page on the CPU)")
    else:
        lines.append(f"GPU             {', '.join(g['adapters'])}")
    return lines


# ── process table ─────────────────────────────────────────────────────────────────────────────────────

def process_table(exe_name="chrome.exe"):
    """Every process of the browser executable (Windows) or every process (elsewhere): pid, parent,
    command line, CPU time used so far (ms), and private memory (MB; Windows commit, Linux PSS, macOS RSS)."""
    out = []
    if sys.platform == "win32":
        ps = (f"Get-CimInstance Win32_Process -Filter \"Name='{exe_name.replace(chr(39), '')}'\"|ForEach-Object{{"
              "\"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.UserModeTime+$_.KernelModeTime)`t$($_.PrivatePageCount)`t"
              "$($_.CommandLine)\"}")
        for line in _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]).splitlines():
            f = line.split("\t", 4)
            if len(f) < 5:
                continue
            try:
                out.append({"pid": int(f[0]), "ppid": int(f[1]), "cpuMs": int(f[2] or 0) / 1e4,
                            "memMB": int(f[3] or 0) / 2 ** 20, "cmd": f[4]})
            except ValueError:
                continue
        return out
    if sys.platform == "darwin":
        for line in _run(["ps", "-axo", "pid=,ppid=,time=,rss=,command="]).splitlines():
            m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(.*)$", line)
            if not m:
                continue
            # [[dd-]hh:]mm:ss.ss
            t = [float(x) for x in re.split(r"[-:]", m.group(3))][::-1] + [0, 0, 0, 0]
            secs = t[0] + t[1] * 60 + t[2] * 3600 + t[3] * 86400
            out.append({"pid": int(m.group(1)), "ppid": int(m.group(2)), "cpuMs": secs * 1000,
                        "memMB": int(m.group(4)) / 1024, "cmd": m.group(5)})
        return out
    tick = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        stat = _read(f"/proc/{name}/stat")
        f = stat[stat.rfind(")") + 2:].split(" ")
        if len(f) < 13:
            continue
        pss = re.search(r"^Pss:\s+(\d+)", _read(f"/proc/{name}/smaps_rollup"), re.M)
        statm = _read(f"/proc/{name}/statm").split(" ")
        kb = int(pss.group(1)) if pss else (int(statm[1]) * 4 if len(statm) > 1 and statm[1].isdigit() else 0)
        out.append({"pid": int(name), "ppid": int(f[1]), "cpuMs": (int(f[11]) + int(f[12])) * 1000 / tick,
                    "memMB": kb / 1024, "cmd": _read(f"/proc/{name}/cmdline").replace("\0", " ")})
    return out


def browser_tree(table, profile_dir):
    """The pids of the browser started on ``profile_dir`` and of every process below it."""
    tree = {p["pid"] for p in table if profile_dir in p["cmd"] and "--type=" not in p["cmd"]}
    grew = True
    while grew:
        grew = False
        for p in table:
            if p["pid"] not in tree and p["ppid"] in tree:
                tree.add(p["pid"])
                grew = True
    return tree


def _machine_times():
    """``(idle, total)`` CPU time of the whole machine so far, or None where the platform does not say."""
    if sys.platform == "win32":
        import ctypes
        idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return idle.value, kernel.value + user.value  # kernel time includes idle time
    line = _read("/proc/stat").split("\n", 1)[0].split()
    if line and line[0] == "cpu":
        v = [int(x) for x in line[1:8]]
        return v[3], sum(v[:3]) + v[3] + sum(v[5:7])  # user nice system idle (iowait left out) irq softirq
    return None


def _busy(a, b):
    if a is None or b is None or b[1] == a[1]:
        return None
    return 100 * (1 - (b[0] - a[0]) / (b[1] - a[1]))


# ── the run ───────────────────────────────────────────────────────────────────────────────────────────

def ram_safe_count(free_mb):
    """The browsers free RAM looks able to hold before anything is measured (450 MB each, 15 % kept back)."""
    return max(1, math.floor(free_mb * 0.85 / 450))


def _first_line(e):
    msg = (str(e).splitlines() or [type(e).__name__])[0]
    return re.sub(r"\s+=+\s*logs\s*=+.*$", "", msg, flags=re.I).strip()


def proxy_label(server):
    """Strip credentials: ``http://host:8080``."""
    return re.sub(r"//[^@/]*@", "//", server)


async def run_bench(concurrency, url, duration_sec, headless, launch_fn, proxy=None, force=False,
                    exe_name="chrome.exe", progress=None):
    """Start the browsers, load ``url`` in each for ``duration_sec``, and return the raw measurements
    (the same keys as the Node SDK's BenchRaw). ``launch_fn(dir, **opts)`` is an async persistent launch."""
    progress = progress or (lambda _line: None)
    loop = asyncio.get_running_loop()
    host = await loop.run_in_executor(None, host_info)
    n = concurrency if force else min(concurrency, ram_safe_count(host["memory"]["freeMB"]))
    if n < concurrency:
        progress(f"only {host['memory']['freeMB']} MB RAM free: running {n} browsers instead of {concurrency} "
                 "(--force to run them all)")

    t0 = _machine_times()
    await asyncio.sleep(3)
    machine_before = _busy(t0, _machine_times())

    dirs = [tempfile.mkdtemp(prefix="clearcote-bench-") for _ in range(n)]
    runs = [{"dclMs": [], "loadMs": [], "loadTimeouts": 0, "idleReached": 0, "idleMissed": 0, "errors": [],
             "cpuMs": 0, "peakMemMB": 0} for _ in dirs]
    contexts = [None] * n
    progress(f"starting {n} browser{'' if n == 1 else 's'}...")

    async def start(i):
        t = time.monotonic()
        opts = {"headless": headless, "quiet": True, "fingerprint": f"clearcote-bench-{i}"}
        if proxy:
            opts["proxy"] = proxy
        try:
            contexts[i] = await launch_fn(dirs[i], **opts)
            runs[i]["launchMs"] = _half_up((time.monotonic() - t) * 1000)
        except Exception as e:  # noqa: BLE001
            runs[i]["launchError"] = _first_line(e)

    await asyncio.gather(*(start(i) for i in range(n)))

    # CPU time per pid, first and latest seen: a renderer that exits mid-run keeps the time it used.
    first, latest, owner = {}, {}, {}

    def sample(initial):
        table = process_table(exe_name)
        by_pid = {p["pid"]: p for p in table}
        for i, d in enumerate(dirs):
            if contexts[i] is None:
                continue
            mem = 0
            for pid in browser_tree(table, d):
                p = by_pid[pid]
                owner[pid] = i
                first.setdefault(pid, p["cpuMs"] if initial else 0)
                latest[pid] = p["cpuMs"]
                mem += p["memMB"]
            runs[i]["peakMemMB"] = max(runs[i]["peakMemMB"], mem)

    progress(f"loading {url} for {duration_sec} s...")
    await loop.run_in_executor(None, sample, True)
    m0 = _machine_times()
    start_t = time.monotonic()
    end = start_t + duration_sec
    sampling = True

    async def sampler():
        while sampling:
            await asyncio.sleep(4)
            if sampling:
                await loop.run_in_executor(None, sample, False)

    sampler_task = asyncio.ensure_future(sampler())

    async def drive(i):
        ctx, r = contexts[i], runs[i]
        if ctx is None:
            return
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        except Exception as e:  # noqa: BLE001
            r["errors"].append(_first_line(e))
            return
        while time.monotonic() < end:
            t = time.monotonic()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                r["dclMs"].append(_half_up((time.monotonic() - t) * 1000))
            except Exception as e:  # noqa: BLE001
                r["errors"].append(_first_line(e))
                # A dead proxy fails in milliseconds: retrying at once would measure a busy loop, not a browser.
                await asyncio.sleep(1)
                continue
            try:
                await page.wait_for_load_state("load", timeout=30000)
                r["loadMs"].append(_half_up((time.monotonic() - t) * 1000))
            except Exception:  # noqa: BLE001
                r["loadTimeouts"] += 1
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
                r["idleReached"] += 1
            except Exception:  # noqa: BLE001
                r["idleMissed"] += 1

    await asyncio.gather(*(drive(i) for i in range(n)))
    measured = time.monotonic() - start_t
    await loop.run_in_executor(None, sample, False)
    m1 = _machine_times()
    sampling = False
    await sampler_task
    for pid, i in owner.items():
        runs[i]["cpuMs"] += latest.get(pid, 0) - first.get(pid, 0)

    for c in contexts:
        if c is not None:
            try:
                await c.close()
            except Exception:  # noqa: BLE001
                pass
    for d in dirs:
        for _ in range(10):
            if not os.path.exists(d):
                break
            shutil.rmtree(d, ignore_errors=True)
            if os.path.exists(d):
                await asyncio.sleep(0.5)

    return {
        "host": host, "url": url, "proxy": proxy_label(proxy["server"]) if proxy else None, "headless": headless,
        "requested": concurrency, "concurrency": n, "durationSec": duration_sec, "measuredSec": measured,
        "browsers": runs, "machineBefore": machine_before, "machineDuring": _busy(m0, m1),
    }


# ── the summary ───────────────────────────────────────────────────────────────────────────────────────

def _pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, math.floor(p * len(s)))]


def summarize_bench(raw):
    """The figures and advice of a bench run. Pure: the same raw run gives the same report in both SDKs."""
    ok = [b for b in raw["browsers"] if b.get("launchMs") is not None]
    dcl = [x for b in ok for x in b["dclMs"]]
    load = [x for b in ok for x in b["loadMs"]]
    counts = {}
    for e in (e for b in ok for e in b["errors"]):
        counts[e] = counts.get(e, 0) + 1
    pages = {
        "loads": len(dcl),
        "dclMedianMs": _pct(dcl, 0.5), "dclP95Ms": _pct(dcl, 0.95),
        "loadMedianMs": _pct(load, 0.5), "loadP95Ms": _pct(load, 0.95),
        "loadTimeouts": sum(b["loadTimeouts"] for b in ok),
        "idleReached": sum(b["idleReached"] for b in ok),
        "idleMissed": sum(b["idleMissed"] for b in ok),
        "errors": [{"count": c, "error": e} for e, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))],
    }
    threads = raw["host"]["cpu"]["threads"]
    per_browser = capacity = None
    if ok and raw["measuredSec"] > 0 and dcl:
        cores = sum(b["cpuMs"] for b in ok) / len(ok) / 1000 / raw["measuredSec"]
        mem = sum(b["peakMemMB"] for b in ok) / len(ok)
        per_browser = {"cpuThreads": _round1(cores), "cpuPercentOfMachine": _round1(100 * cores / threads),
                       "memMB": _half_up(mem)}
        # 80 % of the CPU and 85 % of the RAM that was free before bench started: headroom for the
        # script driving the browsers and for the machine itself.
        by_cpu = max(1, math.floor(0.8 * threads / cores)) if cores > 0 else 999
        by_ram = max(1, math.floor(0.85 * raw["host"]["memory"]["freeMB"] / mem)) if mem > 0 else 999
        capacity = {"browsers": min(by_cpu, by_ram), "limit": "cpu" if by_cpu <= by_ram else "ram",
                    "byCpu": by_cpu, "byRam": by_ram}
    saturated = (raw["machineDuring"] or 0) >= 90
    advice = []
    if ok and not pages["loads"]:
        # Nothing to measure: what a browser costs means nothing until a page loads.
        first = pages["errors"][0]["error"] if pages["errors"] else "no answer"
        advice.append(
            f"No page loaded through the proxy ({first}): check the proxy URL and credentials, and that it answers "
            "from this machine. Run bench again once it does."
            if raw["proxy"] else
            f"No page loaded ({first}): check the URL opens from this machine. Run bench again once it does.")
    if saturated:
        # Whose load filled it: the browsers' own trees, or something else that started during the test?
        browsers_pct = per_browser["cpuPercentOfMachine"] * len(ok) if per_browser else 0
        rise = (raw["machineDuring"] or 0) - (raw["machineBefore"] or 0)
        advice.append(
            f"The CPU was full during the test: {raw['concurrency']} browsers at once is more than this "
            "machine runs smoothly; pages slow down and time out once it is."
            if browsers_pct >= 0.5 * rise else
            f"The CPU was full during the test, but these browsers used only {_tenths(browsers_pct)} % of it: other "
            "programs on this machine took the rest, and every browser slows down once it is full.")
    if raw["host"]["gpu"]["software"]:
        advice.append("No GPU Chromium can use: every page is drawn on the CPU, so each browser costs more CPU. "
                      "Run headless and block images/media you do not need.")
    if raw["host"]["remoteSession"]:
        advice.append("This is a Remote Desktop session: headed browsers here get no GPU acceleration.")
    if not raw["headless"]:
        advice.append("Headed browsers draw every frame on screen and cost more CPU than headless ones.")
    if pages["loads"] and pages["loadTimeouts"] / pages["loads"] > 0.1:
        advice.append(f"{pages['loadTimeouts']} of {pages['loads']} pages never fired 'load' within 30 s: wait with "
                      "waitUntil 'domcontentloaded' and then for the element you need, not for 'load'.")
    idle_total = pages["idleReached"] + pages["idleMissed"]
    if idle_total and pages["idleMissed"] / idle_total > 0.5:
        advice.append("This page rarely goes network-idle: never wait for 'networkidle' on it; wait for the element "
                      "you need.")
    if pages["errors"] and raw["proxy"] and pages["loads"]:
        advice.append("Some navigations failed through the proxy: check it answers from this machine, and use sticky "
                      "sessions (one exit IP per browser) rather than a new IP per request.")
    if capacity:
        s = "" if capacity["browsers"] == 1 else "s"
        advice.append(
            f"At this pace this machine runs about {capacity['browsers']} browser{s} at once (CPU is the limit). "
            "One browser with several contexts costs less than one browser per thread."
            if capacity["limit"] == "cpu" else
            f"At this pace this machine runs about {capacity['browsers']} browser{s} at once (free RAM is the limit).")
    mb, md = raw["machineBefore"], raw["machineDuring"]
    return {
        "host": raw["host"], "url": raw["url"], "proxy": raw["proxy"], "headless": raw["headless"],
        "requested": raw["requested"], "concurrency": raw["concurrency"], "durationSec": raw["durationSec"],
        "launch": {"started": len(ok), "medianMs": _pct([b["launchMs"] for b in ok], 0.5),
                   "errors": [b["launchError"] for b in raw["browsers"] if b.get("launchError")]},
        "pages": pages, "perBrowser": per_browser,
        "machine": {"cpuBefore": None if mb is None else _half_up(mb),
                    "cpuDuring": None if md is None else _half_up(md), "saturated": saturated},
        "capacity": capacity, "advice": advice,
    }


def _secs(ms):
    return "-" if ms is None else f"{_tenths(ms / 1000)} s"


def bench_lines(r):
    """What ``clearcote bench`` prints."""
    lines = host_lines(r["host"])
    n = r["concurrency"]
    lines.append(f"Bench           {n} browser{'' if n == 1 else 's'} for {r['durationSec']} s on {r['url']} "
                 f"({'headless' if r['headless'] else 'headed'}{', proxy ' + r['proxy'] if r['proxy'] else ', no proxy'})")
    if n < r["requested"]:
        lines.append(f"                {r['requested']} asked for; capped by free RAM (--force runs them all)")
    la = r["launch"]
    lines.append(f"Launch          {la['started']} of {n} started"
                 + ("" if la["medianMs"] is None else f", median {_secs(la['medianMs'])}"))
    for e in dict.fromkeys(la["errors"]):
        lines.append(f"                FAILED: {e}")
    p = r["pages"]
    lines.append(f"Pages           {p['loads']} loaded: DOMContentLoaded median {_secs(p['dclMedianMs'])} "
                 f"(p95 {_secs(p['dclP95Ms'])}), load median {_secs(p['loadMedianMs'])} (p95 {_secs(p['loadP95Ms'])})")
    if p["loadTimeouts"]:
        lines.append(f"                'load' not reached within 30 s: {p['loadTimeouts']}")
    if p["idleReached"] + p["idleMissed"]:
        lines.append(f"                network idle within 5 s: {p['idleReached']} of {p['idleReached'] + p['idleMissed']}")
    for e in p["errors"][:5]:
        lines.append(f"Errors          {e['count']} x {e['error']}")
    pb = r["perBrowser"]
    if pb:
        lines.append(f"Per browser     {_tenths(pb['cpuThreads'])} CPU threads ({_tenths(pb['cpuPercentOfMachine'])} % of "
                     f"this machine), {pb['memMB']} MB RAM")
    m = r["machine"]
    if m["cpuDuring"] is not None:
        lines.append(f"Machine CPU     {m['cpuDuring']} % busy during the test ({m['cpuBefore']} % before it)"
                     + (" -- FULL" if m["saturated"] else ""))
    c = r["capacity"]
    if c:
        lines.append(f"Capacity        about {c['browsers']} browser{'' if c['browsers'] == 1 else 's'} at once at this pace "
                     f"(CPU allows {c['byCpu']}, free RAM {c['byRam']})")
    for a in r["advice"]:
        lines.append(f"Advice          {a}")
    return lines
