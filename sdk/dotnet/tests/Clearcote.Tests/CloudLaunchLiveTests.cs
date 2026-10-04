using System.Text.Json.Nodes;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// Cloud launch end to end against a REAL browser: the fake hosted API answers the create with the CDP
/// WebSocket of a local Chromium, so the SDK's connect, the proxy, close and DELETE run for real, and
/// profile sync reads cookies off the same browser over CDP. Which binary: CLEARCOTE_TEST_BINARY, else a
/// Chromium Playwright has downloaded; with neither each test is a no-op (no Skippable* package, see
/// GeometryLiveTests). Mirrors sdk/node/test/cloud-launch.test.ts and sdk/python/tests/test_cloud_launch.py.
public sealed class CloudLaunchLiveTests : IAsyncLifetime
{
    private const string Button = "<title>t</title><button id=b onclick=\"document.title='clicked'\">go</button>";

    private readonly Sandbox _sb = new();
    private readonly FakeCloud _api = new();
    private LocalChromium? _browser;

    private bool Off => _browser is null;

    public async Task InitializeAsync()
    {
        var exe = LocalChromium.Find();
        if (exe is null) return;
        _sb.Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_API_KEY", FakeCloud.ApiKey).Env("CLEARCOTE_API_URL", _api.Url);
        _browser = await LocalChromium.StartAsync(exe);
        _api.ConnectUrl = _browser.WsUrl;
    }

    public async Task DisposeAsync()
    {
        await _api.DisposeAsync();
        if (_browser is not null) await _browser.DisposeAsync();
        _sb.Dispose();
    }

    private async Task ClickButtonAsync(IPage page)
    {
        await page.SetContentAsync(Button);
        await page.Locator("#b").HumanClickAsync();   // humanize works on a cloud page as on a local one
        Assert.Equal("clicked", await page.TitleAsync());
    }

    [Fact]
    public async Task Is_a_working_Playwright_browser_and_CloseAsync_disconnects_and_ends_the_session()
    {
        if (Off) return;
        var browser = await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Country = "us", Identity = "acct-1" });
        try
        {
            Assert.True(browser.IsConnected);
            Assert.False(string.IsNullOrEmpty(browser.Version));
            var sent = _api.Requests("POST", "/api/v1/browsers")[0].Body!;
            Assert.Equal(("us", "acct-1", 2), (Cloud.Str(sent["country"]), Cloud.Str(sent["identity"]), sent.AsObject().Count));
            Assert.Equal("bs_1", Cloud.Str(Cloud.SessionOf(browser)!["id"]));
            var page = await browser.NewPageAsync();
            Assert.Null(page.ViewportSize);              // no emulated viewport on top of the real window
            await ClickButtonAsync(page);
            var ctx = browser.Contexts[0];               // the session's own context is reachable and usable
            await ClickButtonAsync(await ctx.NewPageAsync());
        }
        finally
        {
            await browser.CloseAsync();
        }
        Assert.False(browser.IsConnected);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
        Assert.True(_browser!.IsAlive);                  // CloseAsync only disconnects: ending the browser is the gateway's job
    }

    [Fact]
    public async Task Follows_CLEARCOTE_CLOUD()
    {
        if (Off) return;
        _sb.Env("CLEARCOTE_CLOUD", "true");
        var browser = await Clearcote.LaunchAsync(new LaunchOptions { Note = "from env" });
        Assert.True(browser.IsConnected);
        await browser.CloseAsync();
        Assert.Equal("""{"note":"from env"}""", _api.Requests("POST", "/api/v1/browsers")[0].Body!.ToJsonString());
    }

    [Fact]
    public async Task A_persistent_cloud_context()
    {
        if (Off) return;
        var ctx = await Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "acct-1" });
        Assert.Equal("""{"name":"acct-1","persist":true}""", _api.Requests("POST", "/api/v1/browsers")[0].Body!["profile"]!.ToJsonString());
        await ClickButtonAsync(await ctx.NewPageAsync());
        await ctx.CloseAsync();
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
        Assert.True(_browser!.IsAlive);
    }

    // ── profile sync off a real browser ────────────────────────────────────────────────────────────

    private async Task SetCookiesAsync()
    {
        var pw = await Clearcote.PlaywrightInstanceAsync();
        var b = await pw.Chromium.ConnectOverCDPAsync(_browser!.HttpUrl);
        var session = await b.NewBrowserCDPSessionAsync();
        await session.SendAsync("Storage.setCookies", new Dictionary<string, object>
        {
            ["cookies"] = new object[]
            {
                new { name = "sid", value = "a1", domain = ".example.com", path = "/", secure = true, httpOnly = true, sameSite = "Lax", expires = 1893456000 },
                new { name = "lang", value = "nl", domain = "shop.example.com", path = "/", secure = false, httpOnly = false },
                new { name = "trk", value = "zz", domain = ".tracker.net", path = "/", secure = false, httpOnly = false },
            },
        });
        await b.CloseAsync();
    }

    private CloudServed Served(List<object> calls) => new(_browser!.HttpUrl, () => _browser.IsAlive, () => { calls.Add("close"); return Task.CompletedTask; });

    [Theory]
    [InlineData("http")]
    [InlineData("ws")]
    public async Task Sync_from_a_CDP_endpoint(string which)
    {
        if (Off) return;
        await SetCookiesAsync();
        var endpoint = which == "http" ? _browser!.HttpUrl : _browser!.WsUrl;
        var res = await new Cloud().Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromCdp = endpoint, Domains = new[] { "example.com" } });
        var sent = _api.Requests("PUT").Last().Body!["cookies"]!.AsArray();
        Assert.Equal(new[] { "lang", "sid" }, sent.Select(c => Cloud.Str(c!["name"])).OrderBy(n => n, StringComparer.Ordinal).ToArray());
        var sid = sent.Single(c => Cloud.Str(c!["name"]) == "sid")!;
        // persistent (Chromium caps the lifetime at 400 days, so not the exact value set)
        Assert.Equal(".example.com", Cloud.Str(sid["domain"]));
        Assert.True(sid["httpOnly"]!.GetValue<bool>());
        Assert.True(sid["expires"]!.GetValue<double>() > DateTimeOffset.UtcNow.ToUnixTimeSeconds());
        Assert.Equal(-1, sent.Single(c => Cloud.Str(c!["name"]) == "lang")!["expires"]!.GetValue<double>());
        var allowed = new[] { "name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite" };
        Assert.All(((JsonObject)sid).Select(p => p.Key), k => Assert.Contains(k, allowed));
        Assert.Equal(2, res!["imported"]!.GetValue<int>());
        Assert.True(_browser.IsAlive);                   // reading cookies never closes the browser it reads from
    }

    [Fact]
    public async Task Sync_from_a_profile_directory_reads_a_headless_local_browser()
    {
        if (Off) return;
        await SetCookiesAsync();
        var calls = new List<object>();
        var dir = TestTemp.Create("cc-prof-");
        try
        {
            await new Cloud().Profiles.SyncAsync("acct-1", new ProfileSyncOptions
            {
                FromProfile = dir, Domains = new[] { "tracker.net" },
                Serve = o => { calls.Add((o.UserDataDir, o.Headless, o.Quiet)); return Task.FromResult(Served(calls)); },
            });
        }
        finally
        {
            TestTemp.Remove(dir);
        }
        Assert.Equal(((string?)dir, (bool?)true, true), calls[0]);
        Assert.Equal("close", calls[^1]);
        Assert.Equal(new[] { "trk" }, _api.Requests("PUT").Last().Body!["cookies"]!.AsArray().Select(c => Cloud.Str(c!["name"])).ToArray());
    }

    [Fact]
    public async Task Sync_by_login_opens_the_page_waits_then_reads_the_cookies()
    {
        if (Off) return;
        var calls = new List<object>();
        var res = await new Cloud().Profiles.SyncAsync("acct-1", new ProfileSyncOptions
        {
            LoginUrl = $"{_api.Url}/login-page", Domains = new[] { "127.0.0.1" },
            Serve = o => { calls.Add((o.UserDataDir, o.Headless, o.Quiet)); return Task.FromResult(Served(calls)); },
            Confirm = async () =>
            {
                var deadline = DateTime.UtcNow.AddSeconds(15);
                while (DateTime.UtcNow < deadline && _api.Requests("GET", "/login-page").Count == 0) await Task.Delay(50);
                await Task.Delay(300);
                calls.Add("confirmed");
            },
        });
        Assert.Equal(new object[] { ((string?)null, (bool?)false, true), "confirmed", "close" }, calls.ToArray());
        var sent = _api.Requests("PUT").Last().Body!["cookies"]!.AsArray();
        var c = Assert.Single(sent)!;
        Assert.Equal(("sid", "s3cret", "127.0.0.1"), (Cloud.Str(c["name"]), Cloud.Str(c["value"]), Cloud.Str(c["domain"])));
        Assert.Equal(1, res!["imported"]!.GetValue<int>());
    }
}

/// A real Chromium with a CDP endpoint, standing in for the hosted one. Mirrors the Node suite's
/// test/helpers/chromium.ts (same binary choice, same switches).
internal sealed class LocalChromium : IAsyncDisposable
{
    private readonly System.Diagnostics.Process _proc;
    private readonly string _udd;

    public string WsUrl { get; }
    public string HttpUrl { get; }
    public bool IsAlive { get { try { return !_proc.HasExited; } catch { return false; } } }

    private LocalChromium(System.Diagnostics.Process proc, string udd, string ws)
    {
        _proc = proc;
        _udd = udd;
        WsUrl = ws;
        HttpUrl = "http://127.0.0.1:" + System.Text.RegularExpressions.Regex.Match(ws, @":(\d+)/").Groups[1].Value;
    }

    public static string? Find()
    {
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_TEST_BINARY");
        if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;
        var root = Environment.GetEnvironmentVariable("PLAYWRIGHT_BROWSERS_PATH")
            ?? (OperatingSystem.IsWindows()
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ms-playwright")
                : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".cache", "ms-playwright"));
        if (!Directory.Exists(root)) return null;
        var exe = OperatingSystem.IsWindows() ? "chrome.exe" : "chrome";
        return Directory.GetDirectories(root, "chromium-*")
            .SelectMany(d => Directory.GetDirectories(d).Select(sub => Path.Combine(sub, exe)))
            .Where(File.Exists).OrderByDescending(p => p, StringComparer.Ordinal).FirstOrDefault();
    }

    public static async Task<LocalChromium> StartAsync(string exe)
    {
        var udd = TestTemp.Create("cc-cloud-test-");
        var psi = new System.Diagnostics.ProcessStartInfo(exe) { RedirectStandardError = true, UseShellExecute = false };
        if (OperatingSystem.IsLinux() && Environment.UserName == "root") psi.ArgumentList.Add("--no-sandbox");
        foreach (var a in new[] { "--headless=new", "--remote-debugging-port=0", $"--user-data-dir={udd}", "--no-first-run",
                                  "--no-default-browser-check", "--disable-gpu", "about:blank" })
            psi.ArgumentList.Add(a);
        var proc = System.Diagnostics.Process.Start(psi)!;
        var deadline = DateTime.UtcNow.AddSeconds(30);
        while (DateTime.UtcNow < deadline)
        {
            var line = await proc.StandardError.ReadLineAsync();
            if (line is null) break;
            var m = System.Text.RegularExpressions.Regex.Match(line, @"DevTools listening on (ws://\S+)");
            if (m.Success)
            {
                _ = proc.StandardError.ReadToEndAsync();   // keep draining so the browser never blocks on a full pipe
                return new LocalChromium(proc, udd, m.Groups[1].Value);
            }
        }
        try { proc.Kill(entireProcessTree: true); } catch { }
        TestTemp.Remove(udd);
        throw new InvalidOperationException($"{exe} did not open a CDP endpoint");
    }

    public async ValueTask DisposeAsync()
    {
        try { _proc.Kill(entireProcessTree: true); await _proc.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(10)); } catch { }
        // Chromium's helpers can still be writing under the profile for a moment after exit.
        for (var i = 0; i < 10 && Directory.Exists(_udd); i++)
        {
            try { Directory.Delete(_udd, recursive: true); } catch { await Task.Delay(200); }
        }
    }
}
