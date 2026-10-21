/**
 * `clearcote info`'s Host section and `clearcote bench`: what a support question needs to know about the
 * machine, and what N browsers cost on it.
 *
 * Why: "100 % CPU with 5 threads and pages never finish" has three usual causes -- too many browsers for
 * the CPU, no hardware GPU (Chromium then draws every page on the CPU), or a wait condition ('load',
 * 'networkidle') that a slow proxy or a chatty page never satisfies. `bench` measures all three on the
 * user's own machine, page and proxy, and prints one block they can paste.
 *
 * The cost per browser is measured from the browser's own process tree (the browser process, found by
 * the profile directory bench gives it, and every process below it: renderers, GPU, network), not from
 * whole-machine counters, so whatever else runs on the machine does not end up in the figure.
 *
 * Output is part of the contract: the Python SDK's _bench.py prints the same lines from the same report
 * (test/bench.test.ts and tests/test_bench.py feed both the same fixture). Change a line here, change it
 * there too.
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import { join } from "node:path";
import type { BrowserContext } from "playwright-core";

export interface HostReport {
  cpu: { model: string; threads: number };
  memory: { totalMB: number; freeMB: number };
  os: string;
  /** "virtual machine (VMware, Inc. VMware7,1)", "container", or null on a physical machine. */
  virtual: string | null;
  /** Windows: this is a Remote Desktop session (no GPU acceleration by default). */
  remoteSession: boolean;
  /** The display adapters the OS reports; `software` when none of them can draw 3D. */
  gpu: { adapters: string[]; software: boolean };
}

// Adapters that cannot accelerate Chromium: Windows' fallback drivers, hypervisor and BMC display chips,
// Mesa's CPU renderers.
const SOFTWARE_GPU = /basic display|basic render|remote display|hyper-v video|vmware svga|virtualbox graphics|qxl|cirrus|bochs|llvmpipe|softpipe|swiftshader|virtio|standard vga|aspeed|matrox|parsec virtual|idd/i;
const VIRTUAL_HW = /vmware|virtualbox|qemu|kvm|xen|virtual machine|hvm domu|amazon ec2|google compute|droplet|openstack|bochs|parallels|hetzner|vultr|linode/i;

function run(cmd: string, args: string[], timeout = 15000): string {
  const r = spawnSync(cmd, args, { encoding: "utf8", timeout, windowsHide: true, maxBuffer: 64 * 1024 * 1024 });
  return r.status === 0 ? r.stdout : "";
}

function readText(p: string): string {
  try { return readFileSync(p, "utf8"); } catch { return ""; }
}

/** `reg query` values as name -> data (every value of every subkey with `/s`). The registry, not WMI:
 * PowerShell's Get-CimInstance takes 1-2 s to start, reg.exe answers in ~50 ms. */
function regValues(key: string, ...args: string[]): Array<[string, string]> {
  return run("reg", ["query", key, ...args]).split(/\r?\n/)
    .map((l) => /^\s+(\S+)\s+REG_\w+\s+(.*)$/.exec(l))
    .filter((m): m is RegExpExecArray => !!m)
    .map((m) => [m[1], m[2].trim()] as [string, string]);
}

/** A REG_DWORD as reg.exe prints it ("0xa") in decimal, or "" when missing. */
const dword = (v: string | undefined) => (v && /^0x[0-9a-f]+$/i.test(v) ? String(parseInt(v, 16)) : "");

export const WINDOWS_DISPLAY_CLASS = "HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}";

/** "Windows 11 Pro (10.0.26200)": ProductName still says "Windows 10" on Windows 11, whose builds start at 22000. */
export function windowsName(product: string, major: string, minor: string, build: string): string {
  const name = Number(build) >= 22000 ? product.replace(/Windows 10/, "Windows 11") : product;
  return `${name || "Windows"} (${major || "10"}.${minor || "0"}.${build || "?"})`;
}

function windowsHost(): HostReport {
  const nt = Object.fromEntries(regValues("HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"));
  const bios = Object.fromEntries(regValues("HKLM\\HARDWARE\\DESCRIPTION\\System\\BIOS"));
  const cpu = Object.fromEntries(regValues("HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"));
  const gpu = [...new Set(regValues(WINDOWS_DISPLAY_CLASS, "/s", "/v", "DriverDesc").map(([, v]) => v).filter(Boolean))];
  const hw = `${bios.SystemManufacturer ?? ""} ${bios.SystemProductName ?? ""}`.trim();
  return {
    cpu: { model: (cpu.ProcessorNameString || os.cpus()[0]?.model || "unknown").trim(), threads: os.cpus().length },
    // os.totalmem/freemem are GlobalMemoryStatusEx's total and available physical memory.
    memory: { totalMB: Math.floor(os.totalmem() / 2 ** 20 + 0.5), freeMB: Math.floor(os.freemem() / 2 ** 20 + 0.5) },
    os: windowsName(nt.ProductName ?? "", dword(nt.CurrentMajorVersionNumber), dword(nt.CurrentMinorVersionNumber), nt.CurrentBuild ?? ""),
    virtual: VIRTUAL_HW.test(hw) ? `virtual machine (${hw})` : null,
    remoteSession: /^rdp-/i.test(process.env.SESSIONNAME ?? ""),
    gpu: { adapters: gpu, software: !gpu.some((g) => !SOFTWARE_GPU.test(g)) },
  };
}

function linuxHost(): HostReport {
  const mem = readText("/proc/meminfo");
  const kb = (k: string) => Number(new RegExp(`^${k}:\\s+(\\d+)`, "m").exec(mem)?.[1] ?? 0);
  const model = /^model name\s*:\s*(.+)$/m.exec(readText("/proc/cpuinfo"))?.[1]?.trim() ?? os.cpus()[0]?.model ?? "unknown";
  const pretty = /^PRETTY_NAME="?([^"\n]*)"?/m.exec(readText("/etc/os-release"))?.[1] ?? "Linux";
  const hw = `${readText("/sys/class/dmi/id/sys_vendor").trim()} ${readText("/sys/class/dmi/id/product_name").trim()}`.trim();
  const container = existsSync("/.dockerenv") || existsSync("/run/.containerenv");
  let adapters = run("lspci", []).split("\n").filter((l) => /vga|3d controller|display controller/i.test(l))
    .map((l) => l.replace(/^\S+\s+[^:]+:\s*/, "").trim());
  const dri = existsSync("/dev/dri") && readdirSync("/dev/dri").some((n) => n.startsWith("renderD"));
  if (!adapters.length && dri) adapters = ["render node in /dev/dri"];
  return {
    cpu: { model, threads: os.cpus().length },
    memory: { totalMB: Math.floor(kb("MemTotal") / 1024 + 0.5), freeMB: Math.floor(kb("MemAvailable") / 1024 + 0.5) },
    os: `${pretty} (kernel ${os.release()})`,
    virtual: container ? "container" : VIRTUAL_HW.test(hw) ? `virtual machine (${hw})` : null,
    remoteSession: false,
    gpu: { adapters, software: !dri || !adapters.some((g) => !SOFTWARE_GPU.test(g)) },
  };
}

function macHost(): HostReport {
  const sysctl = (k: string) => run("sysctl", ["-n", k]).trim();
  const adapters = run("system_profiler", ["SPDisplaysDataType"], 30000).split("\n")
    .map((l) => /Chipset Model:\s*(.+)/.exec(l)?.[1]?.trim()).filter((x): x is string => !!x);
  const vm = sysctl("kern.hv_vmm_present") === "1";
  // os.freemem() counts only pages nothing holds; inactive and speculative pages are free for the taking too.
  const vmStat = run("vm_stat", []);
  const pageSize = Number(/page size of (\d+)/.exec(vmStat)?.[1] ?? 4096);
  const freePages = ["Pages free", "Pages inactive", "Pages speculative"]
    .reduce((a, k) => a + Number(new RegExp(`${k}:\\s+(\\d+)`).exec(vmStat)?.[1] ?? 0), 0);
  return {
    cpu: { model: sysctl("machdep.cpu.brand_string") || os.cpus()[0]?.model || "unknown", threads: os.cpus().length },
    memory: { totalMB: Math.floor(Number(sysctl("hw.memsize") || os.totalmem()) / 2 ** 20 + 0.5), freeMB: Math.floor((freePages * pageSize) / 2 ** 20 + 0.5) },
    os: `macOS ${run("sw_vers", ["-productVersion"]).trim() || os.release()}`,
    virtual: vm ? "virtual machine" : null,
    remoteSession: false,
    gpu: { adapters, software: !adapters.some((g) => !SOFTWARE_GPU.test(g)) },
  };
}

/** The machine: CPU, RAM, OS, VM or not, and whether there is a GPU Chromium can draw with. Local only. */
export function hostInfo(): HostReport {
  // The CPU, OS, VM and GPU do not change while a process runs, and reading them spawns reg / lspci /
  // system_profiler: read them once. Free memory changes, and is cheap to read again.
  hostOnce ??= process.platform === "win32" ? windowsHost() : process.platform === "darwin" ? macHost() : linuxHost();
  const h = structuredClone(hostOnce);
  if (hostReads++) h.memory.freeMB = freeMB();
  return h;
}

let hostOnce: HostReport | undefined;
let hostReads = 0;

function freeMB(): number {
  if (process.platform === "linux") {
    const kb = Number(/^MemAvailable:\s+(\d+)/m.exec(readText("/proc/meminfo"))?.[1] ?? 0);
    return Math.floor(kb / 1024 + 0.5);
  }
  if (process.platform === "darwin") return macHost().memory.freeMB;
  return Math.floor(os.freemem() / 2 ** 20 + 0.5);
}

// Every figure is rounded half-up before it is formatted: toFixed and Python's format round ties
// differently (0.25 -> "0.3" vs "0.2"), and both SDKs must print the same text.
const tenths = (x: number) => (Math.floor(x * 10 + 0.5) / 10).toFixed(1);
const gb = (mb: number) => tenths(mb / 1024);

/** The Host lines of `clearcote info` (and the top of `clearcote bench`). */
export function hostLines(h: HostReport): string[] {
  const lines = [
    `Host            ${h.cpu.model}, ${h.cpu.threads} threads, ${gb(h.memory.totalMB)} GB RAM (${gb(h.memory.freeMB)} GB free)`,
    `                ${h.os}, ${h.virtual ?? "physical machine"}${h.remoteSession ? ", Remote Desktop session" : ""}`,
  ];
  if (!h.gpu.adapters.length) lines.push("GPU             none found: Chromium draws every page on the CPU");
  else if (h.gpu.software) lines.push(`GPU             ${h.gpu.adapters.join(", ")} (no 3D acceleration: Chromium draws every page on the CPU)`);
  else lines.push(`GPU             ${h.gpu.adapters.join(", ")}`);
  return lines;
}

// ── process table ─────────────────────────────────────────────────────────────────────────────────────

export interface Proc { pid: number; ppid: number; cmd: string; cpuMs: number; memMB: number }

/** Every process of the browser executable (Windows) or every process (elsewhere): pid, parent, command
 * line, CPU time used so far, and private memory (Windows commit, Linux PSS, macOS RSS). */
export function processTable(exeName = "chrome.exe"): Proc[] {
  if (process.platform === "win32") {
    const ps = `Get-CimInstance Win32_Process -Filter "Name='${exeName.replace(/'/g, "")}'"|ForEach-Object{` +
      "\"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.UserModeTime+$_.KernelModeTime)`t$($_.PrivatePageCount)`t$($_.CommandLine)\"}";
    return run("powershell", ["-NoProfile", "-NonInteractive", "-Command", ps]).split(/\r?\n/).filter(Boolean).map((l) => {
      const [pid, ppid, t100ns, bytes, ...cmd] = l.split("\t");
      return { pid: Number(pid), ppid: Number(ppid), cpuMs: Number(t100ns) / 1e4, memMB: Number(bytes) / 2 ** 20, cmd: cmd.join("\t") };
    });
  }
  if (process.platform === "darwin") {
    return run("ps", ["-axo", "pid=,ppid=,time=,rss=,command="]).split("\n").filter(Boolean).map((l) => {
      const m = /^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(.*)$/.exec(l);
      if (!m) return null;
      // [[dd-]hh:]mm:ss.ss
      const t = m[3].split(/[-:]/).map(Number).reverse();
      const secs = (t[0] ?? 0) + (t[1] ?? 0) * 60 + (t[2] ?? 0) * 3600 + (t[3] ?? 0) * 86400;
      return { pid: Number(m[1]), ppid: Number(m[2]), cpuMs: secs * 1000, memMB: Number(m[4]) / 1024, cmd: m[5] };
    }).filter((p): p is Proc => !!p);
  }
  const out: Proc[] = [];
  for (const name of readdirSync("/proc")) {
    if (!/^\d+$/.test(name)) continue;
    const stat = readText(`/proc/${name}/stat`);
    const f = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
    if (f.length < 13) continue;
    const pss = /^Pss:\s+(\d+)/m.exec(readText(`/proc/${name}/smaps_rollup`))?.[1];
    const rss = Number(readText(`/proc/${name}/statm`).split(" ")[1] ?? 0) * 4;
    out.push({
      pid: Number(name), ppid: Number(f[1]), cpuMs: (Number(f[11]) + Number(f[12])) * 10,
      memMB: Number(pss ?? rss) / 1024, cmd: readText(`/proc/${name}/cmdline`).replace(/\0/g, " "),
    });
  }
  return out;
}

/** The pids of the browser started on `profileDir` and of every process below it. */
export function browserTree(table: Proc[], profileDir: string): Set<number> {
  const roots = table.filter((p) => p.cmd.includes(profileDir) && !p.cmd.includes("--type="));
  const tree = new Set(roots.map((p) => p.pid));
  for (let grew = true; grew;) {
    grew = false;
    for (const p of table) if (!tree.has(p.pid) && tree.has(p.ppid)) { tree.add(p.pid); grew = true; }
  }
  return tree;
}

function machineTimes(): { idle: number; total: number } {
  return os.cpus().reduce((a, c) => {
    const t = c.times;
    return { idle: a.idle + t.idle, total: a.total + t.user + t.nice + t.sys + t.irq + t.idle };
  }, { idle: 0, total: 0 });
}

// ── the run ───────────────────────────────────────────────────────────────────────────────────────────

export interface BenchOptions {
  concurrency: number;
  url: string;
  durationSec: number;
  headless: boolean;
  proxy?: { server: string; username?: string; password?: string };
  /** Run more browsers than free RAM looks able to hold. */
  force?: boolean;
}

/** What one browser did: every navigation, what it cost. */
export interface BrowserRun {
  launchMs?: number;
  launchError?: string;
  dclMs: number[];
  loadMs: number[];
  loadTimeouts: number;
  idleReached: number;
  idleMissed: number;
  errors: string[];
  cpuMs: number;
  peakMemMB: number;
}

/** Everything bench measured, before any summary: also what the tests feed summarizeBench(). */
export interface BenchRaw {
  host: HostReport;
  url: string;
  proxy: string | null;
  headless: boolean;
  requested: number;
  concurrency: number;
  durationSec: number;
  /** Wall time the browsers were measured over. */
  measuredSec: number;
  browsers: BrowserRun[];
  /** Whole-machine CPU busy % in the 3 s before launching and during the measured window (null where
   * the platform does not say). */
  machineBefore: number | null;
  machineDuring: number | null;
}

/** The browsers free RAM looks able to hold before anything is measured (450 MB each, 15 % kept back). */
export function ramSafeCount(freeMB: number): number {
  return Math.max(1, Math.floor((freeMB * 0.85) / 450));
}

const firstLine = (e: unknown) => String((e as Error)?.message ?? e).split("\n")[0].replace(/\s+=+\s*logs\s*=+.*$/i, "").trim();

/** Strip credentials: `http://host:8080`. */
export function proxyLabel(server: string): string {
  return server.replace(/\/\/[^@/]*@/, "//");
}

export async function runBench(
  o: BenchOptions,
  launchFn: (dir: string, opts: Record<string, unknown>) => Promise<BrowserContext>,
  exeName = "chrome.exe",
  progress: (line: string) => void = () => {},
): Promise<BenchRaw> {
  const host = hostInfo();
  const safe = ramSafeCount(host.memory.freeMB);
  const n = o.force ? o.concurrency : Math.min(o.concurrency, safe);
  if (n < o.concurrency) progress(`only ${host.memory.freeMB} MB RAM free: running ${n} browsers instead of ${o.concurrency} (--force to run them all)`);

  const t0 = machineTimes();
  await new Promise((r) => setTimeout(r, 3000));
  const t1 = machineTimes();
  const machineBefore = 100 * (1 - (t1.idle - t0.idle) / (t1.total - t0.total));

  const dirs = Array.from({ length: n }, () => mkdtempSync(join(os.tmpdir(), "clearcote-bench-")));
  const runs: BrowserRun[] = dirs.map(() => ({ dclMs: [], loadMs: [], loadTimeouts: 0, idleReached: 0, idleMissed: 0, errors: [], cpuMs: 0, peakMemMB: 0 }));
  const contexts: Array<BrowserContext | undefined> = [];
  progress(`starting ${n} browser${n === 1 ? "" : "s"}...`);
  await Promise.all(dirs.map(async (dir, i) => {
    const t = Date.now();
    try {
      contexts[i] = await launchFn(dir, {
        headless: o.headless, quiet: true, fingerprint: `clearcote-bench-${i}`, ...(o.proxy ? { proxy: o.proxy } : {}),
      });
      runs[i].launchMs = Date.now() - t;
    } catch (e) {
      runs[i].launchError = firstLine(e);
    }
  }));

  // CPU time per pid, first and latest seen: a renderer that exits mid-run keeps the time it used.
  const first = new Map<number, number>();
  const latest = new Map<number, number>();
  const owner = new Map<number, number>();
  const sample = (initial: boolean) => {
    const table = processTable(exeName);
    dirs.forEach((dir, i) => {
      if (!contexts[i]) return;
      let mem = 0;
      for (const pid of browserTree(table, dir)) {
        const p = table.find((x) => x.pid === pid)!;
        owner.set(pid, i);
        if (!first.has(pid)) first.set(pid, initial ? p.cpuMs : 0);
        latest.set(pid, p.cpuMs);
        mem += p.memMB;
      }
      runs[i].peakMemMB = Math.max(runs[i].peakMemMB, mem);
    });
  };

  progress(`loading ${o.url} for ${o.durationSec} s...`);
  sample(true);
  const m0 = machineTimes();
  const start = Date.now();
  const end = start + o.durationSec * 1000;
  let sampling = true;
  const sampler = (async () => {
    while (sampling) {
      await new Promise((r) => setTimeout(r, 4000));
      if (sampling) sample(false);
    }
  })();
  await Promise.all(contexts.map(async (ctx, i) => {
    if (!ctx) return;
    const r = runs[i];
    let page;
    try { page = ctx.pages()[0] ?? await ctx.newPage(); } catch (e) { r.errors.push(firstLine(e)); return; }
    while (Date.now() < end) {
      const t = Date.now();
      try {
        await page.goto(o.url, { waitUntil: "domcontentloaded", timeout: 60000 });
        r.dclMs.push(Date.now() - t);
      } catch (e) {
        r.errors.push(firstLine(e));
        // A dead proxy fails in milliseconds: retrying at once would measure a busy loop, not a browser.
        await new Promise((res) => setTimeout(res, 1000));
        continue;
      }
      try {
        await page.waitForLoadState("load", { timeout: 30000 });
        r.loadMs.push(Date.now() - t);
      } catch { r.loadTimeouts++; }
      try {
        await page.waitForLoadState("networkidle", { timeout: 5000 });
        r.idleReached++;
      } catch { r.idleMissed++; }
    }
  }));
  const measuredSec = (Date.now() - start) / 1000;
  sample(false);
  const m1 = machineTimes();
  sampling = false;
  await sampler;
  for (const [pid, i] of owner) runs[i].cpuMs += (latest.get(pid) ?? 0) - (first.get(pid) ?? 0);

  await Promise.all(contexts.map((c) => c?.close().catch(() => {})));
  for (const dir of dirs) {
    for (let k = 0; k < 10 && existsSync(dir); k++) {
      try { rmSync(dir, { recursive: true, force: true }); } catch { await new Promise((r) => setTimeout(r, 500)); }
    }
  }

  return {
    host, url: o.url, proxy: o.proxy ? proxyLabel(o.proxy.server) : null, headless: o.headless,
    requested: o.concurrency, concurrency: n, durationSec: o.durationSec, measuredSec, browsers: runs,
    machineBefore, machineDuring: 100 * (1 - (m1.idle - m0.idle) / (m1.total - m0.total)),
  };
}

// ── the summary ───────────────────────────────────────────────────────────────────────────────────────

export interface BenchReport {
  host: HostReport;
  url: string;
  proxy: string | null;
  headless: boolean;
  requested: number;
  concurrency: number;
  durationSec: number;
  launch: { started: number; medianMs: number | null; errors: string[] };
  pages: {
    loads: number;
    dclMedianMs: number | null; dclP95Ms: number | null;
    loadMedianMs: number | null; loadP95Ms: number | null;
    loadTimeouts: number; idleReached: number; idleMissed: number;
    errors: Array<{ count: number; error: string }>;
  };
  perBrowser: { cpuThreads: number; cpuPercentOfMachine: number; memMB: number } | null;
  machine: { cpuBefore: number | null; cpuDuring: number | null; saturated: boolean };
  capacity: { browsers: number; limit: "cpu" | "ram"; byCpu: number; byRam: number } | null;
  advice: string[];
}

function pct(xs: number[], p: number): number | null {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
}

const round1 = (x: number) => Math.floor(x * 10 + 0.5) / 10;
const round0 = (x: number) => Math.floor(x + 0.5);

/** The figures and advice of a bench run. Pure: the same raw run gives the same report in both SDKs. */
export function summarizeBench(raw: BenchRaw): BenchReport {
  const ok = raw.browsers.filter((b) => b.launchMs !== undefined);
  const dcl = ok.flatMap((b) => b.dclMs);
  const load = ok.flatMap((b) => b.loadMs);
  const counts = new Map<string, number>();
  for (const e of ok.flatMap((b) => b.errors)) counts.set(e, (counts.get(e) ?? 0) + 1);
  const pages: BenchReport["pages"] = {
    loads: dcl.length,
    dclMedianMs: pct(dcl, 0.5), dclP95Ms: pct(dcl, 0.95),
    loadMedianMs: pct(load, 0.5), loadP95Ms: pct(load, 0.95),
    loadTimeouts: ok.reduce((a, b) => a + b.loadTimeouts, 0),
    idleReached: ok.reduce((a, b) => a + b.idleReached, 0),
    idleMissed: ok.reduce((a, b) => a + b.idleMissed, 0),
    errors: [...counts].map(([error, count]) => ({ count, error })).sort((a, b) => b.count - a.count || (a.error < b.error ? -1 : 1)),
  };
  const threads = raw.host.cpu.threads;
  let perBrowser: BenchReport["perBrowser"] = null;
  let capacity: BenchReport["capacity"] = null;
  if (ok.length && raw.measuredSec > 0 && dcl.length) {
    const cores = ok.reduce((a, b) => a + b.cpuMs, 0) / ok.length / 1000 / raw.measuredSec;
    const mem = ok.reduce((a, b) => a + b.peakMemMB, 0) / ok.length;
    perBrowser = { cpuThreads: round1(cores), cpuPercentOfMachine: round1((100 * cores) / threads), memMB: round0(mem) };
    // 80 % of the CPU and 85 % of the RAM that was free before bench started: headroom for the
    // script driving the browsers and for the machine itself.
    const byCpu = cores > 0 ? Math.max(1, Math.floor((0.8 * threads) / cores)) : 999;
    const byRam = mem > 0 ? Math.max(1, Math.floor((0.85 * raw.host.memory.freeMB) / mem)) : 999;
    capacity = { browsers: Math.min(byCpu, byRam), limit: byCpu <= byRam ? "cpu" : "ram", byCpu, byRam };
  }
  const saturated = (raw.machineDuring ?? 0) >= 90;
  const advice: string[] = [];
  if (ok.length && !pages.loads) {
    // Nothing to measure: what a browser costs means nothing until a page loads.
    advice.push(raw.proxy
      ? `No page loaded through the proxy (${pages.errors[0]?.error ?? "no answer"}): check the proxy URL and credentials, and that it answers from this machine. Run bench again once it does.`
      : `No page loaded (${pages.errors[0]?.error ?? "no answer"}): check the URL opens from this machine. Run bench again once it does.`);
  }
  if (saturated) {
    // Whose load filled it: the browsers' own trees, or something else that started during the test?
    const browsersPct = perBrowser ? perBrowser.cpuPercentOfMachine * ok.length : 0;
    const rise = (raw.machineDuring ?? 0) - (raw.machineBefore ?? 0);
    advice.push(browsersPct >= 0.5 * rise
      ? `The CPU was full during the test: ${raw.concurrency} browsers at once is more than this machine runs smoothly; pages slow down and time out once it is.`
      : `The CPU was full during the test, but these browsers used only ${tenths(browsersPct)} % of it: other programs on this machine took the rest, and every browser slows down once it is full.`);
  }
  if (raw.host.gpu.software) {
    advice.push("No GPU Chromium can use: every page is drawn on the CPU, so each browser costs more CPU. Run headless and block images/media you do not need.");
  }
  if (raw.host.remoteSession) advice.push("This is a Remote Desktop session: headed browsers here get no GPU acceleration.");
  if (!raw.headless) advice.push("Headed browsers draw every frame on screen and cost more CPU than headless ones.");
  if (pages.loads && pages.loadTimeouts / pages.loads > 0.1) {
    advice.push(`${pages.loadTimeouts} of ${pages.loads} pages never fired 'load' within 30 s: wait with waitUntil 'domcontentloaded' and then for the element you need, not for 'load'.`);
  }
  if (pages.idleReached + pages.idleMissed > 0 && pages.idleMissed / (pages.idleReached + pages.idleMissed) > 0.5) {
    advice.push("This page rarely goes network-idle: never wait for 'networkidle' on it; wait for the element you need.");
  }
  if (pages.errors.length && raw.proxy && pages.loads) {
    advice.push("Some navigations failed through the proxy: check it answers from this machine, and use sticky sessions (one exit IP per browser) rather than a new IP per request.");
  }
  if (capacity) {
    advice.push(capacity.limit === "cpu"
      ? `At this pace this machine runs about ${capacity.browsers} browser${capacity.browsers === 1 ? "" : "s"} at once (CPU is the limit). One browser with several contexts costs less than one browser per thread.`
      : `At this pace this machine runs about ${capacity.browsers} browser${capacity.browsers === 1 ? "" : "s"} at once (free RAM is the limit).`);
  }
  return {
    host: raw.host, url: raw.url, proxy: raw.proxy, headless: raw.headless, requested: raw.requested,
    concurrency: raw.concurrency, durationSec: raw.durationSec,
    launch: {
      started: ok.length,
      medianMs: pct(ok.map((b) => b.launchMs!), 0.5),
      errors: raw.browsers.filter((b) => b.launchError).map((b) => b.launchError!),
    },
    pages, perBrowser,
    machine: {
      cpuBefore: raw.machineBefore === null ? null : round0(raw.machineBefore),
      cpuDuring: raw.machineDuring === null ? null : round0(raw.machineDuring),
      saturated,
    },
    capacity, advice,
  };
}

const secs = (ms: number | null) => (ms === null ? "-" : `${tenths(ms / 1000)} s`);

/** What `clearcote bench` prints. */
export function benchLines(r: BenchReport): string[] {
  const lines = hostLines(r.host);
  lines.push(`Bench           ${r.concurrency} browser${r.concurrency === 1 ? "" : "s"} for ${r.durationSec} s on ${r.url} (${r.headless ? "headless" : "headed"}${r.proxy ? `, proxy ${r.proxy}` : ", no proxy"})`);
  if (r.concurrency < r.requested) lines.push(`                ${r.requested} asked for; capped by free RAM (--force runs them all)`);
  lines.push(`Launch          ${r.launch.started} of ${r.concurrency} started${r.launch.medianMs === null ? "" : `, median ${secs(r.launch.medianMs)}`}`);
  for (const e of [...new Set(r.launch.errors)]) lines.push(`                FAILED: ${e}`);
  const p = r.pages;
  lines.push(`Pages           ${p.loads} loaded: DOMContentLoaded median ${secs(p.dclMedianMs)} (p95 ${secs(p.dclP95Ms)}), load median ${secs(p.loadMedianMs)} (p95 ${secs(p.loadP95Ms)})`);
  if (p.loadTimeouts) lines.push(`                'load' not reached within 30 s: ${p.loadTimeouts}`);
  if (p.idleReached + p.idleMissed) lines.push(`                network idle within 5 s: ${p.idleReached} of ${p.idleReached + p.idleMissed}`);
  for (const e of p.errors.slice(0, 5)) lines.push(`Errors          ${e.count} x ${e.error}`);
  if (r.perBrowser) {
    lines.push(`Per browser     ${tenths(r.perBrowser.cpuThreads)} CPU threads (${tenths(r.perBrowser.cpuPercentOfMachine)} % of this machine), ${r.perBrowser.memMB} MB RAM`);
  }
  const m = r.machine;
  if (m.cpuDuring !== null) lines.push(`Machine CPU     ${m.cpuDuring} % busy during the test (${m.cpuBefore} % before it)${m.saturated ? " -- FULL" : ""}`);
  if (r.capacity) {
    lines.push(`Capacity        about ${r.capacity.browsers} browser${r.capacity.browsers === 1 ? "" : "s"} at once at this pace (CPU allows ${r.capacity.byCpu}, free RAM ${r.capacity.byRam})`);
  }
  for (const a of r.advice) lines.push(`Advice          ${a}`);
  return lines;
}
