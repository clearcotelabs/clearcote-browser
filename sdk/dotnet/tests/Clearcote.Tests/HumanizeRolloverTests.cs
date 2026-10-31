using System.Reflection;
using System.Runtime.CompilerServices;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// Key rollover in the human type loop (mirrors the Python and Node tests): on some pairs of plain keys
// the next key goes down before the previous one comes up. r32/r33 typed 0 overlapping keydowns in 32
// keys, because every character was one press. SC 299: the chance per pair is the rate x the pair's bigram class
// factor (Keys.RolloverPairRate: same finger 0, same hand 0.65, alternating hands 1.35, a space 1.0, at most 1), so
// with the rate forced to 1 only alternating-hand pairs and pairs with a space roll every time.
public class HumanizeRolloverTests
{
    // A keyboard whose DownAsync fails for one key, for the failure path.
    public class FailingKeyboard : HumanizeShiftTests.Recorder
    {
        public string? FailDown { get; set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "DownAsync" && Equals(args?[0], FailDown))
                return Task.FromException(new PlaywrightException("Target page, context or browser has been closed"));
            return base.Invoke(method, args);
        }
    }

    private static (IPage Page, HumanizeShiftTests.Recorder Keyboard) FakePage<TKeyboard>(double rate)
        where TKeyboard : HumanizeShiftTests.Recorder
    {
        var keyboard = DispatchProxy.Create<IKeyboard, TKeyboard>();
        var mouse = DispatchProxy.Create<IMouse, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        var pageRec = (HumanizeShiftTests.Recorder)(object)page;
        pageRec.Keyboard = keyboard;
        pageRec.Mouse = mouse;
        Humanize.RolloverOverride.AddOrUpdate(page, new StrongBox<double>(rate));
        // exact key sequences: no fat-finger slips here (FormsTests and the live suite cover those)
        Humanize.TypoRateOverride.AddOrUpdate(page, new StrongBox<double>(0));
        return (page, (HumanizeShiftTests.Recorder)(object)keyboard);
    }

    private static (IPage Page, HumanizeShiftTests.Recorder Keyboard) FakePage(double rate)
        => FakePage<HumanizeShiftTests.Recorder>(rate);

    // Walk the log: every down has exactly one up, and count the downs that came while another key
    // (not Shift) was still held.
    private static int Overlaps(List<(string Op, string Arg)> log)
    {
        var held = new HashSet<string>();
        int overlaps = 0;
        foreach (var (op, key) in log.Where(e => e.Arg != "Shift"))
        {
            if (op == "down")
            {
                if (held.Count > 0) overlaps++;
                Assert.True(held.Add(key), $"'{key}' went down twice without an up");
            }
            else if (op == "up") Assert.True(held.Remove(key), $"'{key}' came up without a down");
        }
        Assert.Empty(held);
        return overlaps;
    }

    // What the field receives: DownAsync and PressAsync each insert their character; TypeAsync inserts text.
    private static string Typed(List<(string Op, string Arg)> log)
        => string.Concat(log.Where(e => e.Arg != "Shift" && e.Op is "down" or "press" or "type").Select(e => e.Arg));

    [Fact]
    public async Task Rolls_pairs_of_plain_keys_and_types_the_exact_text()
    {
        var (page, kb) = FakePage(1.0);
        await page.HumanTypeAsync("asdf jkl");   // same-hand pairs roll at 0.65; " j" (a space) always
        Assert.True(Overlaps(kb.Log) >= 1, "no keydown while the previous key was still held");
        Assert.Equal("asdf jkl", Typed(kb.Log));
        // alternating hands and a space: every pair rolls at rate 1
        (page, kb) = FakePage(1.0);
        await page.HumanTypeAsync("aksl djf");
        Assert.Equal("aksl djf", Typed(kb.Log));
        Assert.Equal(new (string, string)[]
        {
            ("down", "a"), ("down", "k"), ("up", "a"), ("up", "k"),
            ("down", "s"), ("down", "l"), ("up", "s"), ("up", "l"),
            ("down", " "), ("down", "d"), ("up", " "), ("up", "d"),
            ("down", "j"), ("down", "f"), ("up", "j"), ("up", "f"),
        }, kb.Log.ToArray());
    }

    [Fact]
    public async Task Never_rolls_a_key_into_itself_and_picks_up_after_a_pair()
    {
        var (page, kb) = FakePage(1.0);
        await page.HumanTypeAsync("hello");
        // h-e alternates (rolls); l-l is one key; l-o is the same finger (SC 299: never rolls)
        Assert.Equal(new (string, string)[]
        {
            ("down", "h"), ("down", "e"), ("up", "h"), ("up", "e"),
            ("press", "l"),
            ("press", "l"),
            ("press", "o"),
        }, kb.Log.ToArray());
        Assert.Equal("hello", Typed(kb.Log));
    }

    [Fact]
    public async Task Same_finger_pairs_never_roll()
    {
        // SC 299 (r54 / 0.67.1): the class-free rollover typed same-finger pairs as overlaps, a keydown interval of one
        // dwell less the overlap -- a same finger cannot press the next key before it lifts.
        for (int k = 0; k < 20; k++)
        {
            var (page, kb) = FakePage(1.0);
            await page.HumanTypeAsync("dedede lolo");   // d-e, e-d, l-o, o-l: same finger; e-' ', ' '-l: a space
            var log = kb.Log.ToList();
            var held = new HashSet<string>();
            foreach (var (op, key) in log)
            {
                if (op == "down")
                {
                    if (held.Count > 0)
                        Assert.True(held.Contains(" ") || key == " ", $"run {k}: '{key}' went down while {string.Join(",", held)} was held");
                    held.Add(key);
                }
                else if (op == "up") held.Remove(key);
            }
            Assert.Equal("dedede lolo", Typed(log));
        }
        Assert.Equal(0.0, Keys.RolloverPairRate(1.0, "d", "e"));
        Assert.Equal(0.0, Keys.RolloverPairRate(0.38, "l", "o"));
        Assert.Equal(0.38 * 0.65, Keys.RolloverPairRate(0.38, "a", "s"), 12);
        Assert.Equal(0.30 * 1.35, Keys.RolloverPairRate(0.30, "a", "k"), 12);
        Assert.Equal(1.0, Keys.RolloverPairRate(0.9, "a", "k"));   // capped
        Assert.Equal(0.25, Keys.RolloverPairRate(0.25, "a", " "), 12);
    }

    [Fact]
    public async Task Rate_zero_types_one_press_per_key_as_before()
    {
        var (page, kb) = FakePage(0.0);
        await page.HumanTypeAsync("asdf jkl");
        Assert.Equal("asdf jkl".Select(c => ("press", c.ToString())).ToArray(), kb.Log.ToArray());
        Assert.Equal(0, Overlaps(kb.Log));
    }

    [Fact]
    public async Task Shifted_keys_keep_the_shift_sequence_and_never_roll()
    {
        var (page, kb) = FakePage(1.0);
        await page.HumanTypeAsync("Hi!");
        Assert.Equal(new (string, string)[]
        {
            ("down", "Shift"), ("press", "H"), ("up", "Shift"),
            ("press", "i"),
            ("down", "Shift"), ("press", "!"), ("up", "Shift"),
        }, kb.Log.ToArray());
    }

    [Fact]
    public async Task A_failure_inside_a_roll_releases_both_keys_and_stops_typing()
    {
        var (page, kb) = FakePage<FailingKeyboard>(1.0);
        ((FailingKeyboard)kb).FailDown = "k";
        await Assert.ThrowsAsync<PlaywrightException>(() => page.HumanTypeAsync("akdl"));   // a-k alternates: always rolls
        Assert.Equal(new (string, string)[] { ("down", "a"), ("up", "a"), ("up", "k") }, kb.Log.ToArray());
    }
}
