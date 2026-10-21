using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// LIVE twins of the context attach, the launch's Humanize / ShowCursor, the pointer primitives, the tap and the
/// SC 232 / SC 239 triage cases, against a real Clearcote build (skipped, as a no-op, unless CLEARCOTE_LIVE_ENGINE
/// names one; add CLEARCOTE_LICENSE_KEY for a PRO build). Mirrors node/test/humanize-shape.live.test.ts (the slider
/// drag), python/tests/test_humanize_triage_sync.py, test_humanize_keyboard.py and test_humanize_forms_async.py
/// (the label and Tab cases there run on simulators; here a real page answers the hit tests).
public class HumanizeAttachLiveTests : IClassFixture<FormsLiveBrowser>
{
    private readonly FormsLiveBrowser _b;
    public HumanizeAttachLiveTests(FormsLiveBrowser b) => _b = b;

    private static string? Exe => FormsLiveBrowser.LiveExe;

    private const string Recorder = @"<script>
window.__ev = [];
for (const t of ['mousedown','mouseup','click','keydown','pointermove','pointerdown','pointerup'])
  document.addEventListener(t, (e) => window.__ev.push({ t, id: (e.target && e.target.id) || (e.target && e.target.tagName) || '',
    tr: e.isTrusted, x: e.clientX || 0, y: e.clientY || 0, b: e.buttons || 0, key: e.key || '' }), true);
</script>";

    private static async Task<List<JsonElement>> Ev(IPage page, string t)
    {
        var all = await page.EvaluateAsync<JsonElement>("window.__ev");
        return all.EnumerateArray().Where(e => e.GetProperty("t").GetString() == t).Select(e => e.Clone()).ToList();
    }

    private static Task Clear(IPage page) => page.EvaluateAsync("window.__ev.length = 0");
    private static string Id(JsonElement e) => e.GetProperty("id").GetString()!;
    private static double X(JsonElement e) => e.GetProperty("x").GetDouble();
    private static double Y(JsonElement e) => e.GetProperty("y").GetDouble();

    private async Task Live(string html, Func<IPage, List<HumanizeFallbackEvent>, Task> body, string seed = "attach-live")
    {
        if (_b.Context is null) return;
        var page = await _b.Context.NewPageAsync();
        var events = new List<HumanizeFallbackEvent>();
        try
        {
            Humanize.Attach(page, new HumanizeOptions { OnFallback = events.Add }, seed);
            Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
            await page.SetContentAsync("<!doctype html><html><head><meta charset=utf-8>" + Recorder + "</head><body style='margin:20px;font:15px sans-serif'>"
                + html + "</body></html>");
            await body(page, events);
        }
        finally
        {
            await page.CloseAsync();
        }
    }

    // ------------------------------------------------------------------ SC 232 / SC 239 triage (label, Tab)
    private const string Labels = @"<input type=checkbox id=cb><label for=cb id=lab style='display:block;width:420px;padding:4px 0'>Accept the terms</label><br>
<label id=wrap1><input type=checkbox id=cb2></label><br><br>
<label id=wrap2><input type=checkbox id=cb3> Subscribe to the newsletter</label>";

    [Fact]
    public Task Visible_label_click_aims_at_the_label_text_and_hit_tests_the_label_element() => Live(Labels, async (page, _) =>
    {
        Humanize.LabelDrawOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<bool>(true));
        var text = await page.EvaluateAsync<double[]>(@"(() => { const r = document.createRange(); r.selectNodeContents(document.getElementById('lab'));
            const b = r.getBoundingClientRect(); return [b.x, b.y, b.width, b.height]; })()");
        await page.Locator("#cb").HumanCheckAsync(new LocatorCheckOptions { Timeout = 5000 });
        Assert.True(await page.Locator("#cb").IsCheckedAsync());
        var down = Assert.Single(await Ev(page, "mousedown"));
        Assert.True(down.GetProperty("tr").GetBoolean());
        Assert.Equal("lab", Id(down));
        // inside the TEXT (the label element is 420 px wide; the click is aimed at its words)
        Assert.InRange(X(down), text[0] - 1, text[0] + text[2] + 1);
        Assert.InRange(Y(down), text[1] - 1, text[1] + text[3] + 1);
    });

    [Fact]
    public Task A_wrapping_label_holding_only_its_control_is_no_label_aim() => Live(Labels, async (page, _) =>
    {
        Humanize.LabelDrawOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<bool>(true));
        await page.Locator("#cb2").HumanCheckAsync(new LocatorCheckOptions { Timeout = 5000 });
        Assert.True(await page.Locator("#cb2").IsCheckedAsync());
        var down = Assert.Single(await Ev(page, "mousedown"));
        Assert.Equal("cb2", Id(down));                    // the input itself: the label has no text of its own
    });

    [Fact]
    public Task A_wrapping_labels_text_beside_the_input_is_aimed_at_and_hit_tested_on_the_label() => Live(Labels, async (page, _) =>
    {
        Humanize.LabelDrawOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<bool>(true));
        var input = (await page.Locator("#cb3").BoundingBoxAsync())!;
        await page.Locator("#cb3").HumanCheckAsync(new LocatorCheckOptions { Timeout = 5000 });
        Assert.True(await page.Locator("#cb3").IsCheckedAsync());
        var down = Assert.Single(await Ev(page, "mousedown"));
        Assert.Equal("wrap2", Id(down));
        Assert.True(X(down) > input.X + input.Width, $"x {X(down)}: on the input's box ({input.X}+{input.Width})");
    });

    private const string Buttons = @"<input id=far><br><button id=b1>b1</button> <button id=b2>b2</button> <button id=b3>b3</button><br><br>
<input id=q> <button id=go style='width:160px;height:40px'>Go</button>";

    [Fact]
    public Task Press_on_a_button_out_of_Tab_reach_clicks_the_field_before_it_and_Tabs() => Live(Buttons, async (page, events) =>
    {
        await page.Locator("#far").HumanFillAsync("x");   // the focus sits 5 stops before #go
        await Clear(page);
        events.Clear();
        await page.Locator("#go").HumanPressAsync("Enter");
        Assert.Empty(events);                             // no Playwright focus
        var down = Assert.Single(await Ev(page, "mousedown"));
        Assert.Equal("q", Id(down));                      // one click, into the field before the button
        var keys = (await Ev(page, "keydown")).Select(e => e.GetProperty("key").GetString()).ToList();
        Assert.Equal(new[] { "Tab", "Enter" }, keys.TakeLast(2));
        Assert.Equal("go", await page.EvaluateAsync<string>("document.activeElement.id"));
    }, seed: "ha-1");

    [Fact]
    public Task Press_on_a_button_with_nothing_focused_Tabs_in_from_the_top() => Live(
        "<button id=b1>b1</button> <button id=go style='width:160px;height:40px'>Go</button>", async (page, events) =>
    {
        Assert.Equal("BODY", await page.EvaluateAsync<string>("document.activeElement.tagName"));
        await page.Locator("#go").HumanPressAsync("Enter");
        Assert.Empty(events);
        Assert.Empty(await Ev(page, "mousedown"));
        Assert.Equal(new[] { "Tab", "Tab", "Enter" }, (await Ev(page, "keydown")).Select(e => e.GetProperty("key").GetString()));
        Assert.Equal("go", await page.EvaluateAsync<string>("document.activeElement.id"));
    }, seed: "ha-2");

    [Fact]
    public Task Page_focus_gives_a_field_the_focus_the_humane_way() => Live(Buttons, async (page, events) =>
    {
        await page.HumanFocusAsync("#q");
        Assert.Empty(events);
        Assert.Equal("q", await page.EvaluateAsync<string>("document.activeElement.id"));
        var trusted = (await Ev(page, "mousedown")).Concat(await Ev(page, "keydown")).ToList();
        Assert.NotEmpty(trusted);                         // a click or Tab, not Playwright's 0 ms focus
        Assert.All(trusted, e => Assert.True(e.GetProperty("tr").GetBoolean()));
    });

    // ------------------------------------------------------------------ pointer primitives (shape live test)
    private const string Slider = @"<div id=track style='position:absolute;left:60px;top:300px;width:420px;height:44px;background:#ddd'>
<div id=knob style='position:absolute;left:2px;top:2px;width:40px;height:40px;border-radius:20px;background:#16a34a;touch-action:none'></div></div>
<script>(() => { const k = document.getElementById('knob'); let sx = null, x0 = 2;
  k.addEventListener('pointerdown', e => { sx = e.clientX; x0 = k.offsetLeft; k.setPointerCapture(e.pointerId); });
  k.addEventListener('pointermove', e => { if (sx === null) return; k.style.left = Math.max(2, Math.min(378, x0 + e.clientX - sx)) + 'px'; });
  k.addEventListener('pointerup', () => { sx = null; }); })();</script>";

    [Fact]
    public Task A_slider_drag_stays_on_its_track_and_lands() => Live(Slider, async (page, _) =>
    {
        var kb = (await page.Locator("#knob").BoundingBoxAsync())!;
        double x = kb.X + kb.Width / 2, y = kb.Y + kb.Height / 2;
        await page.HumanMoveAsync(x, y);
        await Clear(page);
        await page.HumanMouseDownAsync();
        await page.HumanMoveAsync(x + 330, y + 2);
        await page.HumanMouseUpAsync();
        var all = await page.EvaluateAsync<JsonElement>("window.__ev");
        var ev = all.EnumerateArray().Select(e => e.Clone()).ToList();
        var held = ev.Where(e => e.GetProperty("t").GetString() == "pointermove" && e.GetProperty("b").GetInt32() != 0).ToList();
        Assert.True(held.Count >= 10, $"{held.Count} held samples");
        Assert.All(ev, e => Assert.True(e.GetProperty("tr").GetBoolean()));
        double x1 = x + 330, y1 = y + 2, d = Math.Sqrt((x1 - x) * (x1 - x) + (y1 - y) * (y1 - y));
        double side = held.Max(e => Math.Abs((X(e) - x) * (y1 - y) / d - (Y(e) - y) * (x1 - x) / d));
        Assert.True(side <= 9, $"the held leg strayed {side:F1} px off its line");
        var left = await page.EvaluateAsync<double>("document.getElementById('knob').offsetLeft");
        Assert.True(Math.Abs(left - 332) <= 3, $"the knob landed at {left}");
    });

    [Fact]
    public Task Coordinate_dblclick_is_two_trusted_held_presses() => Live(
        "<button id=d style='position:absolute;left:300px;top:200px;width:160px;height:40px' ondblclick='window.dbl=1'>dbl</button>", async (page, _) =>
    {
        await page.HumanDblClickAsync(380, 220);
        var downs = await Ev(page, "mousedown");
        Assert.Equal(2, downs.Count);
        Assert.All(downs, e => Assert.Equal("d", Id(e)));
        Assert.True(await page.EvaluateAsync<bool>("window.dbl === 1"));
    });

    // ------------------------------------------------------------------ launch options, popups, cursor, tap
    private static LaunchOptions Opts(string fp) => new()
    {
        ExecutablePath = Exe, Headless = true, Args = new[] { "--no-sandbox" }, Quiet = true, Fingerprint = fp,
    };

    [Fact]
    public async Task Launch_Humanize_attaches_every_page_context_and_popup_with_the_fingerprint_persona()
    {
        if (string.IsNullOrEmpty(Exe)) return;
        var events = new List<HumanizeFallbackEvent>();
        var o = Opts("hz-launch-1");
        o.HumanizeOptions = new HumanizeOptions { OnFallback = events.Add };
        o.ShowCursor = true;
        var browser = await Clearcote.LaunchAsync(o);
        try
        {
            uint seed = Motion.MakePersona("hz-launch-1").Seed;
            var page = await browser.NewPageAsync();
            Assert.Same(o.HumanizeOptions, Humanize.OptionsFor(page));
            Assert.Equal(seed, Humanize.PersonaFor(page).Seed);
            Assert.False(Humanize.HasTouch(page));
            await page.GotoAsync("data:text/html,<button id=t style='width:120px;height:40px'>t</button>");
            Assert.True(await page.EvaluateAsync<bool>("!!document.getElementById('__clearcote_cursor')"));
            var popup = await page.RunAndWaitForPopupAsync(() => page.EvaluateAsync("window.open('about:blank')"));
            Assert.Same(o.HumanizeOptions, Humanize.OptionsFor(popup));
            Assert.Equal(seed, Humanize.PersonaFor(popup).Seed);
            // a new context, with touch: its pages too; the tap goes out as a reported humanized mouse click
            var touch = await browser.NewContextAsync(new BrowserNewContextOptions { HasTouch = true, ViewportSize = ViewportSize.NoViewport });
            var tp = await touch.NewPageAsync();
            Assert.Equal(seed, Humanize.PersonaFor(tp).Seed);
            Assert.True(Humanize.HasTouch(tp));
            await tp.SetContentAsync("<button id=t style='width:120px;height:40px' onclick='window.tapped=1'>t</button>");
            await tp.Locator("#t").HumanTapAsync();
            Assert.True(await tp.EvaluateAsync<bool>("window.tapped === 1"));
            Assert.Contains(events, e => e.Api == "HumanTapAsync" && e.Reason.StartsWith(Humanize.TapNotHumanized, StringComparison.Ordinal));
            // no touch: Playwright's own tap, which refuses
            await Assert.ThrowsAnyAsync<PlaywrightException>(() => page.Locator("#t").HumanTapAsync(new LocatorTapOptions { Timeout = 3000 }));
        }
        finally
        {
            await browser.CloseAsync();
        }
    }

    [Fact]
    public async Task Launch_without_Humanize_attaches_nothing_and_a_persistent_launch_attaches_its_pages()
    {
        if (string.IsNullOrEmpty(Exe)) return;
        var browser = await Clearcote.LaunchAsync(Opts("hz-launch-2"));
        try
        {
            var page = await browser.NewPageAsync();
            Assert.Null(Humanize.OptionsFor(page));       // off by default for a local launch, as in Node and Python
        }
        finally
        {
            await browser.CloseAsync();
        }
        var o = Opts("hz-launch-3");
        o.Humanize = true;
        var ctx = await Clearcote.LaunchEphemeralProfileAsync(o);
        try
        {
            var page = ctx.Pages.Count > 0 ? ctx.Pages[0] : await ctx.NewPageAsync();
            Assert.NotNull(Humanize.OptionsFor(page));
            Assert.Equal(Motion.MakePersona("hz-launch-3").Seed, Humanize.PersonaFor(page).Seed);
            var p2 = await ctx.NewPageAsync();
            Assert.Equal(Motion.MakePersona("hz-launch-3").Seed, Humanize.PersonaFor(p2).Seed);
            Assert.False(Humanize.HasTouch(p2));          // Playwright's options are read on a persistent context too
        }
        finally
        {
            await ctx.CloseAsync();
        }
    }
}
