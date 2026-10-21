using System.Diagnostics;
using System.Reflection;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// HumanizeOptions (lane A, SC 232): Attach(page, options), strict mode, the fallback report, presets and the
// seed -> persona guarantee. Mirrors python/tests/test_humanize_options.py and node/test/humanize-options.test.ts.
// .NET has no transparent patch, so per-call humanize=false is the stock method itself (ClickAsync beside
// HumanClickAsync) and needs no test here.
public class HumanizeOptionsTests
{
    // A locator stand-in: its ToString is what Playwright's prints ("Locator@<selector>"), so a frame locator can be
    // told apart; every call is logged and returns a completed task / default.
    public class FakeLocator : HumanizeShiftTests.Recorder
    {
        public string Selector { get; set; } = "#b";
        public object? PageObj { get; set; }
        public List<string> Calls { get; } = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method is null) return null;
            if (method.Name == "get_Page") return PageObj;
            Calls.Add(method.Name);
            if (method.Name == "SelectOptionAsync")
                return Task.FromResult<IReadOnlyList<string>>(new[] { "native" });
            return base.Invoke(method, args);
        }

        public override string ToString() => "Locator@" + Selector;
    }

    private static (IPage Page, HumanizeShiftTests.Recorder Kb) FakePage()
    {
        var keyboard = DispatchProxy.Create<IKeyboard, HumanizeShiftTests.Recorder>();
        var mouse = DispatchProxy.Create<IMouse, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        var rec = (HumanizeShiftTests.Recorder)(object)page;
        rec.Keyboard = keyboard;
        rec.Mouse = mouse;
        Humanize.TypoRateOverride.AddOrUpdate(page, new System.Runtime.CompilerServices.StrongBox<double>(0));
        return (page, (HumanizeShiftTests.Recorder)(object)keyboard);
    }

    private static (ILocator Loc, FakeLocator Rec) Locator(IPage page, string selector)
    {
        var loc = DispatchProxy.Create<ILocator, FakeLocator>();
        var rec = (FakeLocator)(object)loc;
        rec.Selector = selector;
        rec.PageObj = page;
        return (loc, rec);
    }

    private const string FrameSel = "iframe >> internal:control=enter-frame >> #b";

    [Fact]
    public void Validates_like_the_other_sdks()
    {
        new HumanizeOptions().Validate();
        new HumanizeOptions { Preset = "quick", Engine = "off", Ambient = "idle", TypoRate = 0.1, Handedness = "left", DeviceHz = 250, InputDevice = "trackpad" }.Validate();
        foreach (var bad in new[]
        {
            new HumanizeOptions { Preset = "fast" }, new HumanizeOptions { Engine = "maybe" }, new HumanizeOptions { Ambient = "always" },
            new HumanizeOptions { TypoRate = 2 }, new HumanizeOptions { Handedness = "both" }, new HumanizeOptions { DeviceHz = 5 },
            new HumanizeOptions { InputDevice = "pen" },
        })
            Assert.Throws<ArgumentException>(bad.Validate);
        Assert.Equal(0.7, new HumanizeOptions { Preset = "quick" }.Tempo);
        Assert.Equal(1.4, new HumanizeOptions { Preset = "careful" }.Tempo);
        Assert.True(HumanizeOptions.StrictMode.Strict);
        var (page, _) = FakePage();
        Assert.Throws<ArgumentException>(() => Humanize.Attach(page, new HumanizeOptions { Preset = "fast" }));
    }

    [Fact]
    public void Strict_exception_is_a_playwright_exception()
    {
        var e = new HumanizeStrictException("HumanClickAsync", "covered", "#b");
        Assert.IsAssignableFrom<PlaywrightException>(e);
        Assert.StartsWith("HumanClickAsync: humanize strict", e.Message);
        Assert.Equal(("HumanClickAsync", "covered", "#b"), (e.Api, e.Reason, e.Selector));
    }

    [Fact]
    public async Task Strict_throws_on_a_frame_locator_and_runs_nothing()
    {
        var (page, _) = FakePage();
        Humanize.Attach(page, HumanizeOptions.StrictMode, "opts");
        var (loc, rec) = Locator(page, FrameSel);
        var e = await Assert.ThrowsAsync<HumanizeStrictException>(() => loc.HumanClickAsync());
        Assert.Equal("HumanClickAsync", e.Api);
        Assert.StartsWith("frame: ", e.Reason);   // the frame route (Frames.cs): this stand-in has no box
        e = await Assert.ThrowsAsync<HumanizeStrictException>(() => loc.HumanFillAsync("x"));
        Assert.Equal("HumanFillAsync", e.Api);
        Assert.DoesNotContain("ClickAsync", rec.Calls);
        Assert.DoesNotContain("FillAsync", rec.Calls);
    }

    [Fact]
    public async Task OnFallback_hears_a_frame_locator_then_the_stock_call_runs()
    {
        var (page, _) = FakePage();
        var events = new List<HumanizeFallbackEvent>();
        Humanize.Attach(page, new HumanizeOptions { OnFallback = events.Add }, "opts");
        var (loc, rec) = Locator(page, FrameSel);
        await loc.HumanClickAsync();
        await loc.HumanHoverAsync();
        Assert.Equal(new[] { "HumanClickAsync", "HumanHoverAsync" }, events.Select(e => e.Api).ToArray());
        Assert.All(events, e => Assert.StartsWith("frame: ", e.Reason));   // reported by the frame route
        Assert.Equal("Locator@" + FrameSel, events[0].Selector);
        Assert.Equal(new[] { "ClickAsync", "HoverAsync" }, rec.Calls.Where(c => c is "ClickAsync" or "HoverAsync").ToArray());
    }

    [Fact]
    public async Task Strict_throws_on_an_option_the_humane_route_cannot_match()
    {
        var (page, _) = FakePage();
        Humanize.Attach(page, new HumanizeOptions { Strict = true });
        var (loc, rec) = Locator(page, "#s");
        var e = await Assert.ThrowsAsync<HumanizeStrictException>(
            () => loc.HumanSelectOptionAsync(new SelectOptionValue { Value = "a", Label = "Apple" }));
        Assert.Equal("HumanSelectOptionAsync", e.Api);
        Assert.DoesNotContain("SelectOptionAsync", rec.Calls);
    }

    [Fact]
    public async Task OnFallback_hears_a_type_into_a_field_that_is_not_visible()
    {
        var (page, _) = FakePage();
        var events = new List<HumanizeFallbackEvent>();
        Humanize.Attach(page, new HumanizeOptions { OnFallback = events.Add });
        var (loc, rec) = Locator(page, "#t");   // IsVisibleAsync answers false: no text field to click into
        await loc.HumanTypeAsync("hi");
        Assert.Single(events);
        Assert.Equal(("HumanTypeAsync", "not a visible text field"), (events[0].Api, events[0].Reason));
        Assert.Contains("PressSequentiallyAsync", rec.Calls);
    }

    [Fact]
    public async Task A_page_without_options_reports_to_nobody_and_still_falls_back()
    {
        var (page, _) = FakePage();
        Humanize.Attach(page, "plain");
        var (loc, rec) = Locator(page, FrameSel);
        await loc.HumanClickAsync();
        Assert.Contains("ClickAsync", rec.Calls);
    }

    private static readonly string[] Base =
        { "Seed", "DeviceHz", "TremorHz", "TremorAmp", "DriftAmp", "DriftTau", "Jitter", "FittsA", "FittsB", "PrimaryFrac", "Overshoot", "MaxCorrections", "ApproachBias" };
    private static readonly string[] Py =
        { "seed", "device_hz", "tremor_hz", "tremor_amp", "drift_amp", "drift_tau", "jitter", "fitts_a", "fitts_b", "primary_frac", "overshoot", "max_corrections", "approach_bias" };

    private static double[] Pick(Persona p) => Base.Select(k => Convert.ToDouble(typeof(Persona).GetProperty(k)!.GetValue(p))).ToArray();

    [Fact]
    public void Pins_the_first_20_rng_draws_and_the_seed_to_persona_map()
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(SharedTestData.PathOf("humanize_persona_pins.json")));
        var root = doc.RootElement;
        var seed = root.GetProperty("rng_seed").GetString()!;
        Assert.Equal(root.GetProperty("hash").GetUInt32(), Motion.HashSeed(seed));
        var r = Motion.Mulberry32(Motion.HashSeed(seed));
        var want = root.GetProperty("draws").EnumerateArray().Select(d => d.GetDouble()).ToArray();
        Assert.Equal(want, Enumerable.Range(0, 20).Select(_ => r()).ToArray());
        foreach (var pin in root.GetProperty("personas").EnumerateArray())
        {
            var si = pin.GetProperty("seed_input");
            object s = si.ValueKind == JsonValueKind.Number ? si.GetInt32() : si.GetString()!;
            var f = pin.GetProperty("fields");
            Assert.Equal(Py.Select(k => f.GetProperty(k).GetDouble()).ToArray(), Pick(Motion.MakePersona(s)));
        }
    }

    [Fact]
    public void Options_never_change_the_persona_draws_and_scale_only_the_timing_windows()
    {
        var o = new HumanizeOptions { Preset = "quick", Handedness = "right", TypoRate = 0.1, Ambient = "idle", Engine = "all" };
        var (page, _) = FakePage();
        Humanize.Attach(page, o, "opts");
        var p = Humanize.PersonaFor(page);
        var plain = Motion.MakePersona("opts");
        // handedness fixes the SIGN of the drawn approach bias (integration of lanes A + B); the draw is unchanged
        static double[] Abs(double[] v) => v.Select((x, i) => Base[i] == nameof(Persona.ApproachBias) ? Math.Abs(x) : x).ToArray();
        Assert.Equal(Abs(Pick(plain)), Abs(Pick(p)));
        Assert.True(p.ApproachBias >= 0, "right-handed: a positive approach bias");
        Assert.Equal(plain.KeyDwellMaxMs * 0.7, p.KeyDwellMaxMs, 9);
        Assert.Equal(plain.ClickHoldMinMs * 0.7, p.ClickHoldMinMs, 9);
        Assert.Same(o, Humanize.OptionsFor(page));
        var (page2, _) = FakePage();
        Humanize.Attach(page2, new HumanizeOptions { DeviceHz = 250 }, "opts");
        Assert.Equal(250, Humanize.PersonaFor(page2).DeviceHz);
        Assert.Equal(plain.KeyDwellMaxMs, Humanize.PersonaFor(page2).KeyDwellMaxMs);
        // the old overload and a null options object: the default persona for the seed
        var (page3, _) = FakePage();
        Humanize.Attach(page3, (HumanizeOptions?)null, "opts");
        Assert.Equal(Pick(plain), Pick(Humanize.PersonaFor(page3)));
    }

    // The typing time of one page: the pointer is placed first (that ambient placement is not part of the measure),
    // then a sentence is typed. Random.Shared is not seedable, so several pages per preset are summed; they run
    // concurrently (one gate per page), so the test takes as long as the slowest page.
    private static async Task<double> TypedMs(string preset)
    {
        var (page, _) = FakePage();
        Humanize.Attach(page, new HumanizeOptions { Preset = preset }, "opts");
        await page.HumanTypeAsync("");
        var sw = Stopwatch.StartNew();
        await page.HumanTypeAsync("the quick brown fox jumps over the lazy dog and runs back home again, twice over");
        return sw.Elapsed.TotalMilliseconds;
    }

    [Fact]
    public async Task Presets_scale_the_typing_time()
    {
        var runs = new[] { "default", "quick", "careful" }
            .SelectMany(p => Enumerable.Range(0, 6).Select(_ => (p, t: TypedMs(p)))).ToList();
        await Task.WhenAll(runs.Select(r => r.t));
        double Sum(string p) => runs.Where(r => r.p == p).Sum(r => r.t.Result);
        double b = Sum("default"), q = Sum("quick"), c = Sum("careful");
        Assert.True(b > 3000, $"default {b} ms");
        Assert.InRange(q / b, 0.7 * 0.85, 0.7 * 1.15);
        Assert.InRange(c / b, 1.4 * 0.85, 1.4 * 1.15);
    }
}

public class HumanizePresetPlannerTests
{
    // Integration (lanes A x B/C/E; mirrors test_presets_scale_the_timing_medians_and_the_after_action_pauses): the
    // preset's tempo scales the persona's timing MEDIANS (click hold, key dwell, grab/release, double-click gap,
    // deliberation pauses) and the after-action pauses (move-off delay, read pause), not only the glide samples. The
    // same draw (rng 0.5 = the median) on the default and on the careful / quick copies gives an exact ratio.
    [Fact]
    public void Presets_scale_the_timing_medians_and_the_after_action_pauses()
    {
        Rng mid = () => 0.5;
        static double[] Planned(HumanizeOptions o, Rng mid)
        {
            var p = Humanize.ApplyOptions(Motion.MakePersona("tempo"), o);
            var (grab, release) = Motion.DragDwell(p, mid);
            return new[]
            {
                Motion.ClickHold(p, mid), Motion.KeyDwell(p, mid), grab, release, Motion.DblclickGap(p, mid),
                Motion.PauseMs(p, 200, 400, mid), Motion.MoveOffDelayMs(p, mid), Attention.ReadPauseMs(5000, p, mid),
            };
        }
        var b = Planned(new HumanizeOptions(), mid);
        Assert.True(b[0] > 0 && b[7] > 0);
        foreach (var (preset, t) in new[] { ("quick", 0.7), ("careful", 1.4) })
        {
            var got = Planned(new HumanizeOptions { Preset = preset }, mid);
            for (int i = 0; i < b.Length; i++)
                Assert.True(Math.Abs(got[i] - b[i] * t) <= 1e-9 * Math.Max(1, b[i]), $"{preset} #{i}: {got[i]} vs {b[i] * t}");
        }
    }
}
