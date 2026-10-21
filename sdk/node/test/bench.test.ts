// `clearcote bench` and the Host section of `clearcote info`: the summary and the printed lines are a
// contract shared with the Python SDK (tests/test_bench.py reads the same fixture and expected lines).
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  benchLines, browserTree, hostInfo, hostLines, proxyLabel, ramSafeCount, summarizeBench, windowsName,
  type BenchRaw, type HostReport, type Proc,
} from "../src/bench.js";
import { USAGE, commandUsage } from "../src/cli-commands.js";

const DATA = join(new URL(".", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"), "data");
const raw = JSON.parse(readFileSync(join(DATA, "bench_raw.json"), "utf8")) as BenchRaw;
const expected = readFileSync(join(DATA, "bench_lines.txt"), "utf8").replace(/\r\n/g, "\n").replace(/\n$/, "").split("\n");

const host = (over: Partial<HostReport> = {}): HostReport => ({
  cpu: { model: "AMD Ryzen 7 5700G", threads: 16 }, memory: { totalMB: 16180, freeMB: 4096 },
  os: "Microsoft Windows 11 Pro (10.0.26200)", virtual: null, remoteSession: false,
  gpu: { adapters: ["NVIDIA GeForce RTX 3070"], software: false }, ...over,
});

describe("clearcote bench", () => {
  it("prints the shared fixture's lines exactly (the Python SDK prints the same)", () => {
    expect(benchLines(summarizeBench(raw))).toEqual(expected);
  });

  it("measures per browser from the browsers that started, and finds the limit", () => {
    const r = summarizeBench(raw);
    expect(r.launch).toEqual({ started: 4, medianMs: 5200, errors: ["browserType.launchPersistentContext: licence: all 4 seats are in use"] });
    // (46875+40000+52000+38000)/4 ms over 31.25 s = 1.415 threads of 8
    expect(r.perBrowser).toEqual({ cpuThreads: 1.4, cpuPercentOfMachine: 17.7, memMB: 611 });
    expect(r.capacity).toEqual({ browsers: 3, limit: "ram", byCpu: 4, byRam: 3 });
    expect(r.pages.errors[0]).toEqual({ count: 3, error: "page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://shop.example/products" });
    expect(r.machine).toEqual({ cpuBefore: 61, cpuDuring: 100, saturated: true });
  });

  it("blames the browsers for a full CPU only when they caused most of the rise", () => {
    // 4 browsers x 17.7 % = 70.8 % of the machine against a rise of 38 points: theirs
    expect(summarizeBench(raw).advice[0]).toMatch(/^The CPU was full during the test: 5 browsers at once/);
    // a tenth of that CPU (1.8 % each, 7.2 % together): something else filled it
    const idle = { ...raw, browsers: raw.browsers.map((b) => ({ ...b, cpuMs: b.cpuMs / 10 })) };
    expect(summarizeBench(idle).advice[0]).toMatch(/these browsers used only 7\.2 % of it: other programs/);
    expect(summarizeBench({ ...raw, machineDuring: 70 }).machine.saturated).toBe(false);
  });

  it("caps the browsers by free RAM before measuring anything", () => {
    expect(ramSafeCount(1386)).toBe(2);
    expect(ramSafeCount(100)).toBe(1);
    expect(ramSafeCount(16000)).toBe(30);
  });

  it("never prints proxy credentials", () => {
    expect(proxyLabel("http://user:pa%40ss@gw.example:7777")).toBe("http://gw.example:7777");
    expect(proxyLabel("socks5://gw.example:1080")).toBe("socks5://gw.example:1080");
  });

  it("finds the browser by its profile directory and takes every process below it", () => {
    const dir = "C:\\Temp\\clearcote-bench-abc";
    const table: Proc[] = [
      { pid: 1, ppid: 0, cmd: `chrome.exe --user-data-dir=${dir} --headless`, cpuMs: 10, memMB: 100 },
      { pid: 2, ppid: 1, cmd: "chrome.exe --type=renderer", cpuMs: 20, memMB: 200 },
      { pid: 3, ppid: 2, cmd: "chrome.exe --type=utility", cpuMs: 1, memMB: 5 },
      { pid: 4, ppid: 1, cmd: `chrome.exe --type=gpu-process --user-data-dir=${dir}`, cpuMs: 5, memMB: 50 },
      { pid: 9, ppid: 0, cmd: "chrome.exe --user-data-dir=C:\\Other", cpuMs: 99, memMB: 999 },
      { pid: 10, ppid: 9, cmd: "chrome.exe --type=renderer", cpuMs: 99, memMB: 999 },
    ];
    expect([...browserTree(table, dir)].sort()).toEqual([1, 2, 3, 4]);
  });
});

describe("clearcote info: Host", () => {
  it("names the CPU, RAM, OS and GPU", () => {
    expect(hostLines(host())).toEqual([
      "Host            AMD Ryzen 7 5700G, 16 threads, 15.8 GB RAM (4.0 GB free)",
      "                Microsoft Windows 11 Pro (10.0.26200), physical machine",
      "GPU             NVIDIA GeForce RTX 3070",
    ]);
  });

  it("says when pages are drawn on the CPU, and when it is a VM", () => {
    expect(hostLines(host({ virtual: "container", gpu: { adapters: [], software: true } }))).toEqual([
      "Host            AMD Ryzen 7 5700G, 16 threads, 15.8 GB RAM (4.0 GB free)",
      "                Microsoft Windows 11 Pro (10.0.26200), container",
      "GPU             none found: Chromium draws every page on the CPU",
    ]);
    expect(hostLines(host({ gpu: { adapters: ["ASPEED Graphics Family"], software: true } }))[2])
      .toBe("GPU             ASPEED Graphics Family (no 3D acceleration: Chromium draws every page on the CPU)");
  });

  it("reads the machine once per process and hands out copies", () => {
    const a = hostInfo();
    a.gpu.adapters.push("changed by the caller");
    a.cpu.model = "changed";
    const b = hostInfo();
    expect(b.cpu.model).not.toBe("changed");
    expect(b.gpu.adapters).not.toContain("changed by the caller");
    expect(b.memory.totalMB).toBe(a.memory.totalMB);
    expect(b.memory.freeMB).toBeGreaterThan(0);
  });

  it("names Windows 11 although its ProductName says Windows 10", () => {
    expect(windowsName("Windows 10 Pro", "10", "0", "26200")).toBe("Windows 11 Pro (10.0.26200)");
    expect(windowsName("Windows 10 Home", "10", "0", "19045")).toBe("Windows 10 Home (10.0.19045)");
    expect(windowsName("Windows Server 2022 Datacenter", "10", "0", "20348")).toBe("Windows Server 2022 Datacenter (10.0.20348)");
    expect(windowsName("", "", "", "")).toBe("Windows (10.0.?)");
  });

  it("is documented, with bench's own flags", () => {
    for (const s of ["clearcote bench", "BENCH FLAGS", "--concurrency", "--duration"]) expect(USAGE).toContain(s);
    expect(commandUsage("bench")).toContain("--force");
    expect(commandUsage("bench")).toContain("seats");
  });
});
