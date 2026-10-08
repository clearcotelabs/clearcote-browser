using System.Reflection;
using System.Runtime.CompilerServices;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// Key rollover in the human type loop (mirrors the Python and Node tests): on some pairs of plain keys
// the next key goes down before the previous one comes up. r32/r33 typed 0 overlapping keydowns in 32
// keys, because every character was one press.
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
        await page.HumanTypeAsync("asdf jkl");
        Assert.True(Overlaps(kb.Log) >= 1, "no keydown while the previous key was still held");
        Assert.Equal("asdf jkl", Typed(kb.Log));
        Assert.Equal(new (string, string)[]
        {
            ("down", "a"), ("down", "s"), ("up", "a"), ("up", "s"),
            ("down", "d"), ("down", "f"), ("up", "d"), ("up", "f"),
            ("down", " "), ("down", "j"), ("up", " "), ("up", "j"),
            ("down", "k"), ("down", "l"), ("up", "k"), ("up", "l"),
        }, kb.Log.ToArray());
    }

    [Fact]
    public async Task Never_rolls_a_key_into_itself_and_picks_up_after_a_pair()
    {
        var (page, kb) = FakePage(1.0);
        await page.HumanTypeAsync("hello");
        Assert.Equal(new (string, string)[]
        {
            ("down", "h"), ("down", "e"), ("up", "h"), ("up", "e"),
            ("press", "l"),
            ("down", "l"), ("down", "o"), ("up", "l"), ("up", "o"),
        }, kb.Log.ToArray());
        Assert.Equal("hello", Typed(kb.Log));
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
        ((FailingKeyboard)kb).FailDown = "s";
        await Assert.ThrowsAsync<PlaywrightException>(() => page.HumanTypeAsync("asdf"));
        Assert.Equal(new (string, string)[] { ("down", "a"), ("up", "a"), ("up", "s") }, kb.Log.ToArray());
    }
}
