// launch()'s macOS path against the REAL Clearcote image: docker run, CDP, a page that loads, the persona
// options reaching the engine, and no container left after close().
//
// Off by default. Set CLEARCOTE_TEST_DOCKER_IMAGE to the image to run (and have a working docker); the test
// sets CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 so the macOS decision itself is what sends launch() to Docker.
// Mirrors sdk/python/tests/test_docker_launch_live.py and DockerLaunchLiveTests.cs.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { execFileSync } from "node:child_process";
import type { Browser } from "playwright-core";
import { launch } from "../src/index.js";
import { dockerCli, imageProtocol, TEST_ONLY_ASSUME_MACOS } from "../src/docker.js";
import { tempDir } from "./helpers/temp.js";

const IMAGE = process.env.CLEARCOTE_TEST_DOCKER_IMAGE;
const ENV = ["HOME", "USERPROFILE", "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER_IMAGE", TEST_ONLY_ASSUME_MACOS];
const saved: Record<string, string | undefined> = {};

const containerExists = (id: string) =>
  execFileSync("docker", ["ps", "-a", "-q", "--no-trunc", "--filter", `id=${id}`], { encoding: "utf8" }).trim().length > 0;
// The image declares VOLUME /opt/xdg-cache: every container gets an anonymous volume holding a copy of the
// engine (~0.5 GB). It must go when the container does.
const anonymousVolumes = (id: string) =>
  execFileSync("docker", ["inspect", "-f", '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}} {{end}}{{end}}', id], { encoding: "utf8" })
    .trim().split(" ").filter(Boolean);
const volumeExists = (name: string) => {
  try { execFileSync("docker", ["volume", "inspect", name], { stdio: "ignore" }); return true; } catch { return false; }
};
const containerOf = (b: Browser) => (b as unknown as { dockerContainer: { id: string } }).dockerContainer;

// A proxy the test can see into: a small CONNECT proxy run from the same image (it has Python) on Docker's default
// network, which the browser's container reaches by address. It logs every tunnel it opens, and with a username and
// password it turns away (407) whatever does not log in -- so these tests check for themselves that the traffic
// went through the proxy and logged in, instead of trusting what launch() checked. Same script as the Python test.
const PROXY_SCRIPT = String.raw`
import base64, socket, sys, threading
need = "Basic " + base64.b64encode(("%s:%s" % (sys.argv[1], sys.argv[2])).encode()).decode() if len(sys.argv) > 2 else None
def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    for s in (a, b):
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
def handle(c):
    f = c.makefile("rb")
    while True:
        line = f.readline()
        if not line:
            return
        head = []
        while True:
            h = f.readline()
            if not h or h in (b"\r\n", b"\n"):
                break
            head.append(h)
        method, target = line.decode("latin-1").split()[:2]
        auth = [h.split(b":", 1)[1].strip().decode() for h in head if h.lower().startswith(b"proxy-authorization:")]
        if need and auth != [need]:
            print("REFUSED", method, target, flush=True)
            c.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="cc"\r\nContent-Length: 0\r\n\r\n')
            continue
        print("TUNNEL", method, target, "logged-in" if need else "open", flush=True)
        if method != "CONNECT":
            c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            return
        host, port = target.rsplit(":", 1)
        u = socket.create_connection((host, int(port)), timeout=20)
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        threading.Thread(target=pipe, args=(u, c), daemon=True).start()
        pipe(c, u)
        return
srv = socket.create_server(("0.0.0.0", 3128))
print("READY", flush=True)
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
`;

const dk = (...args: string[]) => {
  try { return execFileSync("docker", args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }); } catch { return ""; }
};

/** The test proxy: its server URL, its log, and stop(). */
async function startProxy(...creds: string[]) {
  const id = execFileSync("docker", ["run", "-d", "--rm", "--entrypoint", "python", IMAGE!, "-u", "-c", PROXY_SCRIPT, ...creds], { encoding: "utf8" }).trim();
  for (let i = 0; i < 100 && !dk("logs", id).includes("READY"); i++) await new Promise((r) => setTimeout(r, 100));
  const ip = dk("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", id).trim();
  return { server: `http://${ip}:3128`, log: () => dk("logs", id), stop: () => { dk("rm", "-f", "-v", id); } };
}

/** The browser binary the container runs, read from its process list (not from its own log). */
function engineOf(id: string): string | null {
  for (const line of dk("top", id, "-eo", "pid,args").split("\n").slice(1)) { // docker top wants a pid column
    const args = line.trim().replace(/^\d+\s+/, "");
    const exe = args.split(/\s+/)[0] ?? "";
    if (exe.endsWith("/chrome") && !args.includes("--type=")) return exe;
  }
  return null;
}

// Every chrome process in a container: its user namespace and its command line, read from the container's /proc.
const CHROME_PROCESSES = String.raw`for p in /proc/[0-9]*; do a=$(tr '\0' ' ' < $p/cmdline 2>/dev/null); case "$a" in */chrome\ *) echo "$(readlink $p/ns/user) $a" ;; esac; done`;
const chromeProcesses = (id: string) =>
  dk("exec", id, "sh", "-c", CHROME_PROCESSES).split("\n").filter((l) => l.includes(" ")).map((l) => [l.slice(0, l.indexOf(" ")), l.slice(l.indexOf(" ") + 1)]);

async function probe(browser: Browser) {
  const page = await browser.newPage();
  await page.goto("data:text/html,<title>loaded in docker</title><p>hi</p>");
  const got = await page.evaluate(() => ({
    title: document.title, tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
    platform: navigator.platform, ua: navigator.userAgent, w: innerWidth,
  }));
  return { page, got };
}

describe.skipIf(!IMAGE || !dockerCli.which())("launch() on macOS against the real Clearcote image", () => {
  beforeEach(() => {
    for (const k of ENV) saved[k] = process.env[k];
    for (const k of ENV) delete process.env[k];
    process.env[TEST_ONLY_ASSUME_MACOS] = "1";
    process.env.CLEARCOTE_DOCKER_IMAGE = IMAGE;
    const home = tempDir("cc-docker-live-home-"); // no saved licence key: the image's open engine
    process.env.HOME = home;
    process.env.USERPROFILE = home;
  });
  afterEach(() => {
    for (const [k, v] of Object.entries(saved)) { if (v === undefined) delete process.env[k]; else process.env[k] = v; }
  });

  it("runs the image, loads a page, carries the persona, and leaves no container", async () => {
    // control: the image's own defaults
    const plain = await launch({ quiet: true });
    const id0 = containerOf(plain).id;
    let base;
    let vols0: string[] = [];
    try {
      expect(containerExists(id0)).toBe(true);
      vols0 = anonymousVolumes(id0);
      expect(vols0.length).toBeGreaterThan(0);
      expect(vols0.every(volumeExists)).toBe(true);
      base = (await probe(plain)).got;
      expect(base.title).toBe("loaded in docker");
    } finally {
      await plain.close();
    }
    expect(containerExists(id0)).toBe(false);
    expect(vols0.some(volumeExists)).toBe(false);

    // treatment: the persona options reach the engine in the container
    const b = await launch({ fingerprint: "docker-e2e-seed", platform: "windows", timezone: "Asia/Tokyo", quiet: true });
    const id1 = containerOf(b).id;
    const vols1 = anonymousVolumes(id1);
    try {
      const { page, got } = await probe(b);
      expect(got.title).toBe("loaded in docker");
      expect(got.tz).toBe("Asia/Tokyo");
      expect(got.tz).not.toBe(base.tz);
      expect(got.platform).toBe("Win32");
      expect(base.platform).not.toBe("Win32");
      expect(got.ua).toContain("Windows NT");
      expect(page.viewportSize()).toBeNull();
      await page.goto("https://example.com/", { waitUntil: "domcontentloaded", timeout: 60_000 });
      expect(await page.title()).toBe("Example Domain");
    } finally {
      await b.close();
    }
    expect(containerExists(id1)).toBe(false);
    expect(vols1.length).toBeGreaterThan(0);
    expect(vols1.some(volumeExists)).toBe(false);
  }, 600_000);

  it("a proxy with a password carries the traffic", async () => {
    // Before sdk-0.40.0's image, the password of an http(s) proxy was dropped: the browser was challenged, nothing
    // answered, every navigation failed. An image that old is refused it before it starts.
    const tp = await startProxy("cc-user", "p@ss:w rd");
    try {
      const proxy = { server: tp.server, username: "cc-user", password: "p@ss:w rd" };
      if ((await imageProtocol("docker", IMAGE!, true)) < 2) {
        await expect(launch({ proxy, quiet: true })).rejects.toThrow(/predates sdk-0\.40\.0 and cannot use an HTTP proxy/);
        return;
      }
      const b = await launch({ proxy, quiet: true });
      const { id } = containerOf(b);
      try {
        const page = await b.newPage();
        await page.goto("https://example.com/", { waitUntil: "domcontentloaded", timeout: 60_000 });
        expect(await page.title()).toBe("Example Domain");
        expect(tp.log()).toContain("TUNNEL CONNECT example.com:443 logged-in");
        const exe = engineOf(id);
        expect(exe).toBeTruthy();
        expect(exe).not.toContain("/pro-"); // no key: the open engine
      } finally {
        await b.close();
      }
      expect(containerExists(id)).toBe(false);
    } finally {
      tp.stop();
    }
  }, 300_000);

  // Licensed and proxied, against whatever image CLEARCOTE_TEST_DOCKER_IMAGE names (one from before sdk-0.40.0 too):
  // launch() must come back on the licensed engine with the proxy applied -- or refuse -- never on the open engine
  // with traffic going direct. The test checks both itself: the engine from the container's process list, the
  // traffic from the test proxy's log. The proxy wants a password from an image that can log in to one (sdk-0.40.0
  // and newer); an older image gets one without. Needs CLEARCOTE_TEST_DOCKER_KEY (a real key) and, optionally,
  // CLEARCOTE_TEST_DOCKER_CACHE_VOLUME (a scratch volume for the licensed engine).
  const KEY = process.env.CLEARCOTE_TEST_DOCKER_KEY;
  it.skipIf(!KEY)("a licensed, proxied launch is never downgraded", async () => {
    if (saved.CLEARCOTE_DOCKER_CACHE_VOLUME === undefined && process.env.CLEARCOTE_TEST_DOCKER_CACHE_VOLUME) {
      process.env.CLEARCOTE_DOCKER_CACHE_VOLUME = process.env.CLEARCOTE_TEST_DOCKER_CACHE_VOLUME;
    }
    const login = (await imageProtocol("docker", IMAGE!, true)) >= 2 ? ["cc-user", "p@ss:w rd"] : [];
    const tp = await startProxy(...login);
    try {
      const proxy = { server: tp.server, ...(login.length ? { username: login[0], password: login[1] } : {}) };
      const b = await launch({ licenseKey: KEY, proxy, quiet: true, timeout: 600_000 });
      const info = (b as unknown as { dockerContainer: { id: string; serveProtocol: number } }).dockerContainer;
      try {
        const env = execFileSync("docker", ["inspect", "-f", "{{json .Config.Env}}", info.id], { encoding: "utf8" });
        // protocol 2 keeps the key out of the container's configuration; an older image gets it as a variable
        expect(env.includes(KEY!)).toBe(info.serveProtocol < 2);
        expect(engineOf(info.id) ?? "").toContain("/pro-"); // the licensed engine is what runs
        const page = await b.newPage();
        await page.goto("https://example.com/", { waitUntil: "domcontentloaded", timeout: 60_000 });
        expect(await page.title()).toBe("Example Domain");
        expect(tp.log()).toContain(`TUNNEL CONNECT example.com:443 ${login.length ? "logged-in" : "open"}`);
      } finally {
        await b.close();
        delete process.env.CLEARCOTE_DOCKER_CACHE_VOLUME;
      }
      expect(containerExists(info.id)).toBe(false);
    } finally {
      tp.stop();
    }
  }, 900_000);

  it("Chrome runs with its sandbox", async () => {
    // launch() starts the image with the seccomp profile, and its Chrome runs sandboxed: no --no-sandbox on any chrome
    // process, renderers in user namespaces of their own, and Chrome's own sandbox page agrees. An image older than
    // serve protocol 3 runs Chrome with --no-sandbox whatever it gets: there this fails.
    const b = await launch({ quiet: true });
    const id = containerOf(b).id;
    try {
      const procs = chromeProcesses(id);
      expect(procs.length).toBeGreaterThan(0);
      expect(procs.filter(([, args]) => args.includes("--no-sandbox"))).toEqual([]);
      const browserNs = procs.find(([, args]) => !args.includes("--type="))![0];
      const renderers = procs.filter(([, args]) => args.includes("--type=renderer")).map(([ns]) => ns);
      expect(renderers.length).toBeGreaterThan(0);
      expect(renderers.filter((ns) => ns === browserNs)).toEqual([]);
      const page = await b.newPage();
      await page.goto("chrome://sandbox");
      await page.waitForFunction(() => /adequately sandboxed/.test(document.body.innerText), null, { timeout: 30_000 });
      const text = await page.evaluate(() => document.body.innerText);
      expect(text).toContain("You are adequately sandboxed.");
      expect(text).toContain("Layer 1 Sandbox\tNamespace");
    } finally {
      await b.close();
    }
    expect(containerExists(id)).toBe(false);
  }, 600_000);
});
