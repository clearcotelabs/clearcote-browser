using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LaunchAsync's macOS path against the REAL Clearcote image: docker run, CDP, a page that loads, the
/// persona options reaching the engine, and no container left after CloseAsync.
///
/// Off by default (a no-op, as GeometryLiveTests): set CLEARCOTE_TEST_DOCKER_IMAGE to the image to run, with
/// a working docker. CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 is set so the macOS decision itself sends LaunchAsync
/// to Docker. Mirrors sdk/node/test/docker-launch.live.test.ts and sdk/python/tests/test_docker_launch_live.py.
public sealed class DockerLaunchLiveTests : IDisposable
{
    private readonly Sandbox _sb = new();
    private readonly string? _image = Environment.GetEnvironmentVariable("CLEARCOTE_TEST_DOCKER_IMAGE");

    public DockerLaunchLiveTests()
    {
        foreach (var k in new[] { "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_CLOUD" }) _sb.Env(k, null);
        _sb.Env(DockerLaunch.TestOnlyAssumeMacos, "1").Env("CLEARCOTE_DOCKER_IMAGE", _image);
        _sb.TempHome();   // no saved licence key: the image's open engine
    }

    public void Dispose() => _sb.Dispose();

    private static (int Code, string Out) Docker(params string[] args)
    {
        var psi = new System.Diagnostics.ProcessStartInfo("docker") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        foreach (var a in args) psi.ArgumentList.Add(a);
        using var p = System.Diagnostics.Process.Start(psi)!;
        var output = p.StandardOutput.ReadToEnd();
        p.StandardError.ReadToEnd();
        p.WaitForExit();
        return (p.ExitCode, output);
    }

    private static bool ContainerExists(string id) => Docker("ps", "-a", "-q", "--no-trunc", "--filter", $"id={id}").Out.Trim().Length > 0;

    /// The image declares VOLUME /opt/xdg-cache: every container gets an anonymous volume holding a copy of
    /// the engine (~0.5 GB). It must go when the container does.
    private static string[] AnonymousVolumes(string id) =>
        Docker("inspect", "-f", "{{range .Mounts}}{{if eq .Type \"volume\"}}{{.Name}} {{end}}{{end}}", id).Out
            .Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    private static bool VolumeExists(string name) => Docker("volume", "inspect", name).Code == 0;

    private static async Task<(IPage Page, JsonElement Got)> ProbeAsync(IBrowser browser)
    {
        var page = await browser.NewPageAsync();
        await page.GotoAsync("data:text/html,<title>loaded in docker</title><p>hi</p>");
        var got = await page.EvaluateAsync<JsonElement>(
            "() => ({title: document.title, tz: Intl.DateTimeFormat().resolvedOptions().timeZone, platform: navigator.platform, ua: navigator.userAgent})");
        return (page, got);
    }

    [Fact]
    public async Task Runs_the_real_image_loads_a_page_carries_the_persona_and_leaves_no_container()
    {
        if (string.IsNullOrEmpty(_image) || DockerLaunch.Which() is null) return;

        // control: the image's own defaults
        var plain = await Clearcote.LaunchAsync(new LaunchOptions { Quiet = true });
        var id0 = Clearcote.DockerContainerOf(plain)!.Id;
        JsonElement baseline;
        string[] vols0;
        try
        {
            Assert.True(ContainerExists(id0));
            vols0 = AnonymousVolumes(id0);
            Assert.NotEmpty(vols0);
            Assert.All(vols0, v => Assert.True(VolumeExists(v)));
            baseline = (await ProbeAsync(plain)).Got;
            Assert.Equal("loaded in docker", baseline.GetProperty("title").GetString());
        }
        finally { await plain.CloseAsync(); }
        Assert.False(ContainerExists(id0));
        Assert.DoesNotContain(vols0, VolumeExists);

        // treatment: the persona options reach the engine in the container
        var b = await Clearcote.LaunchAsync(new LaunchOptions { Fingerprint = "docker-e2e-seed", Platform = "windows", Timezone = "Asia/Tokyo", Quiet = true });
        var id1 = Clearcote.DockerContainerOf(b)!.Id;
        var vols1 = AnonymousVolumes(id1);
        try
        {
            var (page, got) = await ProbeAsync(b);
            Assert.Equal("Asia/Tokyo", got.GetProperty("tz").GetString());
            Assert.NotEqual(baseline.GetProperty("tz").GetString(), got.GetProperty("tz").GetString());
            Assert.Equal("Win32", got.GetProperty("platform").GetString());
            Assert.NotEqual("Win32", baseline.GetProperty("platform").GetString());
            Assert.Contains("Windows NT", got.GetProperty("ua").GetString());
            Assert.Null(page.ViewportSize);
            await page.GotoAsync("https://example.com/", new() { WaitUntil = WaitUntilState.DOMContentLoaded, Timeout = 60_000 });
            Assert.Equal("Example Domain", await page.TitleAsync());
        }
        finally { await b.CloseAsync(); }
        Assert.False(ContainerExists(id1));
        Assert.NotEmpty(vols1);
        Assert.DoesNotContain(vols1, VolumeExists);
    }

    // A proxy the test can see into: a small CONNECT proxy run from the same image (it has Python) on Docker's default
    // network, which the browser's container reaches by address. It logs every tunnel it opens, and with a username and
    // password it turns away (407) whatever does not log in -- so these tests check for themselves that the traffic went
    // through the proxy and logged in, instead of trusting what LaunchAsync checked. Same script as the Python test.
    private const string ProxyScript = """

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

""";

    private sealed record TestProxy(string Server, string Id)
    {
        public string Log() => Docker("logs", Id).Out;
        public void Stop() => Docker("rm", "-f", "-v", Id);
    }

    private async Task<TestProxy> StartProxyAsync(params string[] creds)
    {
        var id = Docker(new[] { "run", "-d", "--rm", "--entrypoint", "python", _image!, "-u", "-c", ProxyScript }.Concat(creds).ToArray()).Out.Trim();
        for (var i = 0; i < 100 && !Docker("logs", id).Out.Contains("READY", StringComparison.Ordinal); i++) await Task.Delay(100);
        var ip = Docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", id).Out.Trim();
        return new TestProxy($"http://{ip}:3128", id);
    }

    /// The browser binary the container runs, read from its process list (not from its own log).
    private static string? EngineOf(string id)
    {
        foreach (var line in Docker("top", id, "-eo", "pid,args").Out.Split('\n').Skip(1))   // docker top wants a pid column
        {
            var parts = line.Trim().Split(' ', 2, StringSplitOptions.RemoveEmptyEntries);
            var args = parts.Length > 1 ? parts[1].Trim() : "";
            var exe = args.Split(' ', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault() ?? "";
            if (exe.EndsWith("/chrome", StringComparison.Ordinal) && !args.Contains("--type=", StringComparison.Ordinal)) return exe;
        }
        return null;
    }

    /// Before sdk-0.40.0's image, the password of an http(s) proxy was dropped: the browser was challenged, nothing
    /// answered, every navigation failed. An image that old is refused it before it starts.
    [Fact]
    public async Task A_proxy_with_a_password_carries_the_traffic()
    {
        if (string.IsNullOrEmpty(_image) || DockerLaunch.Which() is null) return;
        var tp = await StartProxyAsync("cc-user", "p@ss:w rd");
        try
        {
            var proxy = new ProxyOptions { Server = tp.Server, Username = "cc-user", Password = "p@ss:w rd" };
            if (await DockerLaunch.ImageProtocolAsync("docker", _image, quiet: true) < 2)
            {
                var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Proxy = proxy, Quiet = true }));
                Assert.Contains("predates sdk-0.40.0 and cannot use an HTTP proxy", e.Message);
                return;
            }
            var b = await Clearcote.LaunchAsync(new LaunchOptions { Proxy = proxy, Quiet = true });
            var id = Clearcote.DockerContainerOf(b)!.Id;
            try
            {
                var page = await b.NewPageAsync();
                await page.GotoAsync("https://example.com/", new() { WaitUntil = WaitUntilState.DOMContentLoaded, Timeout = 60_000 });
                Assert.Equal("Example Domain", await page.TitleAsync());
                Assert.Contains("TUNNEL CONNECT example.com:443 logged-in", tp.Log());
                var exe = EngineOf(id);
                Assert.NotNull(exe);
                Assert.DoesNotContain("/pro-", exe);   // no key: the open engine
            }
            finally { await b.CloseAsync(); }
            Assert.False(ContainerExists(id));
        }
        finally { tp.Stop(); }
    }

    /// Licensed and proxied, against whatever image CLEARCOTE_TEST_DOCKER_IMAGE names (one from before sdk-0.40.0
    /// too): LaunchAsync must come back on the licensed engine with the proxy applied -- or refuse -- never on the
    /// open engine with traffic going direct. The test checks both itself: the engine from the container's process
    /// list, the traffic from the test proxy's log. The proxy wants a password from an image that can log in to one
    /// (sdk-0.40.0 and newer); an older image gets one without. Needs CLEARCOTE_TEST_DOCKER_KEY (a real key) and,
    /// optionally, CLEARCOTE_TEST_DOCKER_CACHE_VOLUME (a scratch volume for the licensed engine).
    [Fact]
    public async Task A_licensed_proxied_launch_is_never_downgraded()
    {
        var key = Environment.GetEnvironmentVariable("CLEARCOTE_TEST_DOCKER_KEY");
        if (string.IsNullOrEmpty(_image) || string.IsNullOrEmpty(key) || DockerLaunch.Which() is null) return;
        _sb.Env("CLEARCOTE_DOCKER_CACHE_VOLUME", Environment.GetEnvironmentVariable("CLEARCOTE_TEST_DOCKER_CACHE_VOLUME"));
        var login = await DockerLaunch.ImageProtocolAsync("docker", _image, quiet: true) >= 2 ? new[] { "cc-user", "p@ss:w rd" } : Array.Empty<string>();
        var tp = await StartProxyAsync(login);
        try
        {
            var proxy = new ProxyOptions { Server = tp.Server, Username = login.Length > 0 ? login[0] : null, Password = login.Length > 0 ? login[1] : null };
            var b = await Clearcote.LaunchAsync(new LaunchOptions { LicenseKey = key, Proxy = proxy, Quiet = true, Timeout = 600_000 });
            var info = Clearcote.DockerContainerOf(b)!;
            try
            {
                var env = Docker("inspect", "-f", "{{json .Config.Env}}", info.Id).Out;
                // protocol 2 keeps the key out of the container's configuration; an older image gets it as a variable
                Assert.Equal(info.ServeProtocol < 2, env.Contains(key));
                Assert.Contains("/pro-", EngineOf(info.Id) ?? "");   // the licensed engine is what runs
                var page = await b.NewPageAsync();
                await page.GotoAsync("https://example.com/", new() { WaitUntil = WaitUntilState.DOMContentLoaded, Timeout = 60_000 });
                Assert.Equal("Example Domain", await page.TitleAsync());
                Assert.Contains($"TUNNEL CONNECT example.com:443 {(login.Length > 0 ? "logged-in" : "open")}", tp.Log());
            }
            finally { await b.CloseAsync(); }
            Assert.False(ContainerExists(info.Id));
        }
        finally { tp.Stop(); }
    }
}
