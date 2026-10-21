using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// The frame route LIVE (Frames.cs): a select inside a frame (a FrameLocator's locator, direct and nested) is ONE
/// trusted change; a fill inside a frame types trusted keys; a locator made on a child IFrame takes the frame route
/// too (it used to take the main-frame route); a cross-origin (out-of-process) frame falls back to Playwright with
/// the fallback reported. Mirrors node/test/humanize-coverage.live.test.ts and
/// python/tests/test_humanize_coverage_live.py. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.
public class FramesLiveTests : IClassFixture<FormsLiveBrowser>, IAsyncLifetime
{
    private const string Rec = @"<script>
window.__ev = [];
for (const t of ['mousedown','mouseup','click','input','change','keydown'])
  document.addEventListener(t, (e) => window.__ev.push({ t, id: (e.target && e.target.id) || '', tr: e.isTrusted }), true);
</script>";
    private static readonly string[] Names = { "Afghanistan", "Albania", "Germany", "Ghana", "Greece", "Netherlands", "Norway", "Peru", "Spain", "Uruguay" };
    private static readonly string Opts = string.Concat(Names.Select((n, i) => $"<option value='v{i}'>{n}</option>"));
    private static readonly string Child = $@"<!doctype html><html><head><meta charset=utf-8>{Rec}</head><body style='margin:8px'>
<select id=cs>{Opts}</select><input id=ct value='old'><input id=cc type=checkbox></body></html>";
    private static readonly string Outer = $@"<!doctype html><html><head>{Rec}</head><body style='margin:20px'>
<iframe id=inner src='/child.html' style='width:420px;height:200px;border:3px solid #888;padding:5px'></iframe></body></html>";
    private static string MainHtml(string cross) => $@"<!doctype html><html><head><meta charset=utf-8>{Rec}<style>
iframe{{display:block;margin:8px 0}}</style></head>
<body style='margin:10px'><select id=s>{Opts}</select>
<iframe id=same name=same src='/child.html' style='width:460px;height:200px;border:4px solid #444;padding:6px'></iframe>
<iframe id=nested src='/outer.html' style='width:520px;height:300px'></iframe>
<iframe id=cross src='{cross}/child.html' style='width:460px;height:200px'></iframe></body></html>";

    private readonly FormsLiveBrowser _b;
    private TestOrigin? _server;
    private string _base = "";

    public FramesLiveTests(FormsLiveBrowser b) => _b = b;

    public Task InitializeAsync()
    {
        if (_b.Context is null) return Task.CompletedTask;
        string? cross = null;
        _server = new TestOrigin(r => r.Url.Split('?')[0] switch
        {
            "/main.html" => (200, MainHtml(cross!)),
            "/child.html" => (200, Child),
            "/outer.html" => (200, Outer),
            _ => (404, ""),
        }, "text/html; charset=utf-8");
        _base = $"http://127.0.0.1:{_server.Port}";
        cross = $"http://localhost:{_server.Port}";   // another site: an out-of-process frame
        return Task.CompletedTask;
    }

    public async Task DisposeAsync()
    {
        if (_server is not null) await _server.DisposeAsync();
    }

    private async Task Live(Func<IPage, List<HumanizeFallbackEvent>, Task> body)
    {
        if (_b.Context is null) return;
        var page = await _b.Context.NewPageAsync();
        var events = new List<HumanizeFallbackEvent>();
        try
        {
            Humanize.Attach(page, new HumanizeOptions { OnFallback = events.Add }, "frames-live-1");
            await page.GotoAsync(_base + "/main.html");
            await page.WaitForLoadStateAsync(LoadState.Load);
            await body(page, events);
        }
        finally
        {
            await page.CloseAsync();
        }
    }

    private static async Task<List<JsonElement>> Ev(IFrame f, string t, string id)
    {
        var all = await f.EvaluateAsync<JsonElement>("window.__ev");
        return all.EnumerateArray().Where(e => e.GetProperty("t").GetString() == t && e.GetProperty("id").GetString() == id)
            .Select(e => e.Clone()).ToList();
    }

    private static Task Reset(IFrame f) => f.EvaluateAsync("window.__ev.length = 0");
    private static IFrame Nested(IPage page) => page.Frames.First(f => f.ParentFrame?.ParentFrame is not null);
    private static string Fallbacks(List<HumanizeFallbackEvent> ev) => string.Join("; ", ev.Select(e => e.Api + ": " + e.Reason));

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(2)]
    [InlineData(3)]
    [InlineData(4)]
    [InlineData(5)]
    public Task Frame_select_is_one_trusted_change(int run) => Live(async (page, events) =>
    {
        bool same = run % 2 == 0;
        var f = same ? page.Frame("same")! : Nested(page);
        var fl = same ? page.FrameLocator("#same") : page.FrameLocator("#nested").FrameLocator("#inner");
        var name = new[] { "Peru", "Ghana", "Spain", "Norway", "Greece", "Uruguay" }[run];
        await Reset(f);
        var got = await fl.Locator("#cs").HumanSelectOptionAsync(new SelectOptionValue { Label = name });
        Assert.Equal(new[] { $"v{Array.IndexOf(Names, name)}" }, got);
        var ch = await Ev(f, "change", "cs");
        Assert.Single(ch);
        Assert.True(ch[0].GetProperty("tr").GetBoolean());
        Assert.True(events.Count == 0, Fallbacks(events));
    });

    [Fact]
    public Task Frame_fill_types_trusted_keys() => Live(async (page, events) =>
    {
        var f = Nested(page);
        await Reset(f);
        await page.FrameLocator("#nested").FrameLocator("#inner").Locator("#ct").HumanFillAsync("new words");
        Assert.Equal("new words", await f.Locator("#ct").InputValueAsync());
        var keys = await Ev(f, "keydown", "ct");
        Assert.True(keys.Count >= 9, $"{keys.Count} keydowns");
        Assert.All(keys, k => Assert.True(k.GetProperty("tr").GetBoolean()));
        Assert.True(events.Count == 0, Fallbacks(events));
    });

    [Fact]
    public Task A_locator_made_on_a_child_frame_takes_the_frame_route() => Live(async (page, events) =>
    {
        // frame.Locator(...): no enter-frame selector, the child IFrame tells (it used to take the main-frame route)
        var f = page.Frame("same")!;
        var loc = f.Locator("#ct");
        Assert.False(Humanize.IsFrameLocator(loc));
        Assert.True(Humanize.InChildFrame(loc));
        Assert.False(Humanize.InChildFrame(page.Locator("#s")));
        await Reset(f);
        await loc.HumanFillAsync("typed here");
        Assert.Equal("typed here", await loc.InputValueAsync());
        var keys = await Ev(f, "keydown", "ct");
        Assert.True(keys.Count >= 10, $"{keys.Count} keydowns");
        Assert.All(keys, k => Assert.True(k.GetProperty("tr").GetBoolean()));
        await Reset(f);
        await f.Locator("#cc").HumanCheckAsync();
        Assert.True(await f.Locator("#cc").IsCheckedAsync());
        var downs = await Ev(f, "mousedown", "cc");
        Assert.Single(downs);
        Assert.True(downs[0].GetProperty("tr").GetBoolean());
        await f.Locator("#ct").HumanPressAsync("End");
        await f.Locator("#ct").HumanTypeAsync("!");
        Assert.Equal("typed here!", await loc.InputValueAsync());
        Assert.True(events.Count == 0, Fallbacks(events));
    });

    [Fact]
    public Task A_cross_origin_frame_falls_back_to_Playwright_reported() => Live(async (page, events) =>
    {
        var got = await page.FrameLocator("#cross").Locator("#cs").HumanSelectOptionAsync("Peru");
        Assert.Equal(new[] { "v7" }, got);
        var e = Assert.Single(events);
        Assert.Equal("HumanSelectOptionAsync", e.Api);
        Assert.Contains("cross-origin", e.Reason);
    });
}
