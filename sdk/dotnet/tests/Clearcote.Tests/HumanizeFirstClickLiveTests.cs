using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LIVE: the FIRST humanized click on a fresh headless page, and the first after a navigation, walks and presses
/// (mirrors sdk/node/test/humanize-first-click.live.test.ts and sdk/python/tests/test_humanize_first_click_live.py).
/// Skipped (a no-op, as in HumanizeLiveTests) unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
///
/// WHY. The Node and Python engine route (Browser.dispatchPointerPath) walks in the browser process without waiting
/// for the page, and a fresh headless browser's first page dropped that walk (SC 216, r38 and r40: 2-4 moves, some
/// with no press); the route now waits for two animation frames before it walks (IsolatedWorld.Frames). The .NET
/// engine route takes coordinate clicks (and, with Engine "all", targeted ones); the locator clicks below stay on the
/// SDK planner by default, where every leg is a Playwright mouse move (Input.dispatchMouseEvent, which the renderer
/// acknowledges), so their first click walked in full (measured 2026-10-13). This keeps it that way.
public class HumanizeFirstClickLiveTests
{
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
    private const int Pages = 6;

    private const string Page = @"<!doctype html><html><body style=""margin:0;height:1200px"">
<button id=b style=""position:absolute;left:600px;top:400px;width:120px;height:40px"">target</button>
<button id=c style=""position:absolute;left:150px;top:120px;width:120px;height:40px"">other</button>
<script>window.ev = [];
addEventListener('pointermove', e => {
  const cs = (e.getCoalescedEvents && e.getCoalescedEvents().length) ? e.getCoalescedEvents() : [e];
  for (const c of cs) ev.push({ t: 'move', tr: e.isTrusted });
}, true);
for (const t of ['mousedown', 'mouseup', 'click'])
  addEventListener(t, e => ev.push({ t, tr: e.isTrusted, id: e.target.id }), true);</script></body></html>";

    private sealed record Summary(int Moves, int Down, int Up, int Click, bool Trusted);

    // b, or c: consecutive clicks on one page alternate, so each walk covers ~500 px.
    private static async Task<Summary> ClickAndReadAsync(IPage page, string id = "b")
    {
        await page.Locator("#" + id).HumanClickAsync();
        try { await page.WaitForFunctionAsync("() => window.ev.some(e => e.t === 'mouseup')", null, new() { Timeout = 3000 }); }
        catch (TimeoutException) { } // no mouseup is what the summary reports
        var ev = JsonDocument.Parse(await page.EvaluateAsync<string>("JSON.stringify(window.ev.splice(0))")).RootElement
            .EnumerateArray().ToList();
        string T(JsonElement e) => e.GetProperty("t").GetString()!;
        return new Summary(
            ev.Count(e => T(e) == "move"), ev.Count(e => T(e) == "mousedown"), ev.Count(e => T(e) == "mouseup"),
            ev.Count(e => T(e) == "click" && e.TryGetProperty("id", out var i) && i.GetString() is "b" or "c"),
            ev.All(e => e.GetProperty("tr").GetBoolean()));
    }

    // A walk of 300+ px reaches the page as tens of moves once frames flow; the broken first walks delivered 1-9.
    private static void Check(List<Summary> got)
    {
        var all = string.Join(", ", got);
        foreach (var g in got)
        {
            Assert.True(g is { Down: 1, Up: 1, Click: 1, Trusted: true }, all);
            Assert.True(g.Moves >= 10, all);
        }
    }

    [Fact]
    public async Task First_clicks_walk_and_press_on_fresh_pages_and_after_navigations()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var fcCache = Path.Join(Path.GetTempPath().TrimEnd(Path.DirectorySeparatorChar), "cc-fc-cache");
        var fcCacheExisted = Directory.Exists(fcCache);
        var browser = await Clearcote.LaunchAsync(new LaunchOptions
        {
            ExecutablePath = LiveExe, Headless = true, Args = new[] { "--no-sandbox" }, Quiet = true,
            Fingerprint = "first-click-live-net",
        });
        try
        {
            var fresh = new List<Summary>();
            for (var i = 0; i < Pages; i++)
            {
                var page = await browser.NewPageAsync();
                Humanize.Attach(page, "first-click-live-net");
                await page.SetContentAsync(Page);
                await page.WaitForTimeoutAsync(200);
                fresh.Add(await ClickAndReadAsync(page));
                await page.CloseAsync();
            }

            var p = await browser.NewPageAsync();
            Humanize.Attach(p, "first-click-live-net");
            await p.SetContentAsync(Page);
            await p.WaitForTimeoutAsync(200);
            await ClickAndReadAsync(p);
            var nav = new List<Summary>();
            for (var i = 0; i < 4; i++)
            {
                await p.GotoAsync("data:text/html," + Uri.EscapeDataString(Page + $"<!-- {i} -->"));
                await p.WaitForTimeoutAsync(200);
                nav.Add(await ClickAndReadAsync(p, i % 2 == 1 ? "b" : "c"));
            }
            await p.CloseAsync();

            Check(fresh);
            Check(nav);
        }
        finally
        {
            await browser.CloseAsync();
            if (!fcCacheExisted) TestTemp.Remove(fcCache);
        }
    }
}
