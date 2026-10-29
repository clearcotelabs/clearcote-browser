using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LIVE: a humanized select whose change handler stalls the page keeps to its timeout, as stock SelectOption
/// does. Mirrors sdk/python/tests/test_humanize_select_commit_live.py. Skipped (a no-op, as in
/// HumanizeLiveTests) unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. Strict humanize: a fallback to
/// stock Playwright would pass the timing checks too, so it fails the test.
///
/// - A sync XHR that is never answered blocks the renderer, so the Enter that commits the pick is never
///   acknowledged: it used to wait forever, stock throws at the timeout.
/// - A change handler that navigates to a server that never answers leaves a document that cannot answer
///   the read-back: it used to throw at the page default, stock returns the value at once.
public class HumanizeSelectCommitLiveTests
{
    private const string Opts = "<option value=\"a\">Alpha</option><option value=\"b\">Bravo</option></select>";
    private static readonly Dictionary<string, string> Pages = new()
    {
        ["/xhr"] = "<select id=\"s\" onchange=\"var x=new XMLHttpRequest();x.open('GET','/hang',false);try{x.send()}catch(e){};window.__changed=this.value\">" + Opts,
        ["/nav"] = "<select id=\"s\" onchange=\"window.__changed=this.value;location.href='/hang'\">" + Opts,
        ["/fxhr"] = "<iframe src=\"/xhr\" width=\"300\" height=\"80\"></iframe>",
        ["/fnav"] = "<iframe src=\"/nav\" width=\"300\" height=\"80\"></iframe>",
        ["/ok"] = "<select id=\"s\" onchange=\"window.__changed=this.value\">" + Opts,
    };

    /// A local server whose /hang never answers while the test runs.
    private sealed class Server : IDisposable
    {
        private readonly TcpListener _l = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new();
        public string Base { get; }

        public Server()
        {
            _l.Start();
            Base = $"http://127.0.0.1:{((IPEndPoint)_l.LocalEndpoint).Port}";
            _ = Task.Run(AcceptAsync);
        }

        private async Task AcceptAsync()
        {
            while (!_stop.IsCancellationRequested)
            {
                TcpClient c;
                try { c = await _l.AcceptTcpClientAsync(_stop.Token); }
                catch (Exception) { return; }
                _ = Task.Run(() => ServeAsync(c));
            }
        }

        private async Task ServeAsync(TcpClient c)
        {
            using var _ = c;
            try
            {
                var s = c.GetStream();
                var buf = new byte[8192];
                int n = await s.ReadAsync(buf, _stop.Token);
                var line = Encoding.ASCII.GetString(buf, 0, n).Split('\n')[0];
                var path = line.Split(' ').ElementAtOrDefault(1)?.Split('?')[0] ?? "/";
                if (path == "/hang") { await Task.Delay(Timeout.Infinite, _stop.Token); return; }
                var body = Encoding.UTF8.GetBytes("<!doctype html><html><body>" + Pages.GetValueOrDefault(path, "") + "</body></html>");
                var head = Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {body.Length}\r\nConnection: close\r\n\r\n");
                await s.WriteAsync(head, _stop.Token);
                await s.WriteAsync(body, _stop.Token);
            }
            catch (Exception) { /* stopped, or the browser went away */ }
        }

        public void Dispose()
        {
            _stop.Cancel();
            _l.Stop();
        }
    }

    /// One browser per test: a page left blocked by a sync XHR must not hold up the next one. Null without a live engine.
    private static async Task<(IBrowserContext Ctx, string Dir)?> LaunchAsync()
    {
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe)) return null;
        var dir = TestTemp.Create("cc-live-select-commit-");
        var ctx = await Clearcote.LaunchPersistentContextAsync(dir, new LaunchOptions
        {
            ExecutablePath = exe, Headless = true, Args = new[] { "--no-sandbox" }, Quiet = true, Fingerprint = "select-commit-1",
        });
        return (ctx, dir);
    }

    private static async Task<IPage> PageAsync(IBrowserContext ctx, string url)
    {
        var page = await ctx.NewPageAsync();
        Humanize.Attach(page, new HumanizeOptions { Strict = true }, "select-commit-1");
        page.SetDefaultTimeout(15_000);
        await page.GotoAsync(url);
        return page;
    }

    private static async Task RunAsync(Func<IBrowserContext, string, Task> body)
    {
        var b = await LaunchAsync();
        if (b is null) return;
        using var srv = new Server();
        try
        {
            await body(b.Value.Ctx, srv.Base);
        }
        finally
        {
            await b.Value.Ctx.CloseAsync();
            TestTemp.Remove(b.Value.Dir);
        }
    }

    [Theory]
    [InlineData("/xhr", false)]
    [InlineData("/fxhr", true)]
    public Task SyncXhrChangeHandler_ThrowsAtTheTimeout(string path, bool inFrame) => RunAsync(async (ctx, url) =>
    {
        var page = await PageAsync(ctx, url + path);
        var sel = inFrame ? page.FrameLocator("iframe").Locator("#s") : page.Locator("#s");
        await sel.WaitForAsync();
        var sw = Stopwatch.StartNew();
        var e = await Assert.ThrowsAnyAsync<System.TimeoutException>(
            () => sel.HumanSelectOptionAsync(new[] { "b" }, new LocatorSelectOptionOptions { Timeout = 5000 }));
        Assert.True(sw.ElapsedMilliseconds < 9000, $"threw after {sw.ElapsedMilliseconds} ms: the 5 s timeout was not kept");
        Assert.Contains("5000ms", e.Message);
    });

    [Theory]
    [InlineData("/nav", false)]
    [InlineData("/fnav", true)]
    public Task NavigatingChangeHandler_ReturnsTheValue(string path, bool inFrame) => RunAsync(async (ctx, url) =>
    {
        var page = await PageAsync(ctx, url + path);
        var sel = inFrame ? page.FrameLocator("iframe").Locator("#s") : page.Locator("#s");
        await sel.WaitForAsync();
        var sw = Stopwatch.StartNew();
        var got = await sel.HumanSelectOptionAsync(new[] { "b" }, new LocatorSelectOptionOptions { Timeout = 5000 });
        Assert.Equal(new[] { "b" }, got);
        Assert.True(sw.ElapsedMilliseconds < 5000, $"took {sw.ElapsedMilliseconds} ms: waited on a document that is navigating away");
    });

    [Fact]
    public Task HealthyPage_WithAShortTimeout_StillSelects() => RunAsync(async (ctx, url) =>
    {
        var page = await PageAsync(ctx, url + "/ok");
        Assert.Equal(new[] { "b" },
            await page.Locator("#s").HumanSelectOptionAsync(new[] { "b" }, new LocatorSelectOptionOptions { Timeout = 300 }));
        Assert.Equal("b", await page.EvaluateAsync<string>("window.__changed"));
    });
}
