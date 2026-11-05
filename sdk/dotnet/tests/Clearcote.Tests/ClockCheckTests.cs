using System.Text;
using Xunit;

namespace Clearcote.Tests;

/// <summary>CR-2VFYCD: a computer clock far off makes the engine refuse every token; the SDK says so before launching. Mirrors tests/test_clockcheck.py.</summary>
public class ClockCheckTests
{
    private const double T = 1_800_000_000; // server time when the token was minted

    private static string Tok(string json) =>
        Convert.ToBase64String(Encoding.UTF8.GetBytes(json)).TrimEnd('=').Replace('+', '-').Replace('/', '_') + ".sig";

    private static readonly string Run = Tok("{\"lic\":\"l\",\"lease\":\"L1\",\"plan\":\"pro\",\"nbf\":1799999100,\"iat\":1800000000,\"exp\":1800000180}");

    [Fact]
    public void QuietInsideTheWindow()
    {
        Assert.Null(ClockCheck.Problem(Run, T));
        Assert.Null(ClockCheck.Problem(Run, T - 900));
        Assert.Null(ClockCheck.Problem(Run, T + 179));
    }

    [Fact]
    public void NamesAClockBehindWithAtLeastHowFar()
    {
        var p = ClockCheck.Problem(Run, T - 1200)!.Value;
        Assert.Equal(-1200, p.SkewSeconds);
        Assert.Contains("clock is at least 20 min behind", p.Message);
        Assert.Contains("it reads 2027-01-15 07:40:00 UTC; the server issued this licence token at 2027-01-15 08:00:00 UTC", p.Message);
        Assert.Contains("Set time automatically", p.Message);
    }

    [Fact]
    public void NamesAClockAheadPastExp()
    {
        var p = ClockCheck.Problem(Run, T + 600, "persona token")!.Value;
        Assert.Equal(600, p.SkewSeconds);
        Assert.Contains("after the moment this persona token expires", p.Message);
    }

    [Fact]
    public void SkipsUnreadableTokens()
    {
        Assert.Null(ClockCheck.Problem("hdr.sig", T));
        Assert.Null(ClockCheck.Problem(Tok("{\"lic\":\"l\"}"), T));
        ClockCheck.AssertFor(null);
    }

    [Fact]
    public void FormatsDurations()
    {
        Assert.Equal("42 s", ClockCheck.FormatDuration(42));
        Assert.Equal("3 min 12 s", ClockCheck.FormatDuration(192));
        Assert.Equal("2 h 5 min", ClockCheck.FormatDuration(7500));
        Assert.Equal("1 day 4 h", ClockCheck.FormatDuration(100800));
    }

    [Fact]
    public async Task PrepareRefusesBeforeRunningTheEngine()
    {
        var ran = 0;
        var lease = new LeaseSession(() => Run, "L1", () => Task.CompletedTask, () => throw new InvalidOperationException());
        var e = await Assert.ThrowsAsync<ClockSkewError>(() => PersonaToken.PrepareAsync("chrome", new[] { "--fingerprint=1" }, lease, null,
            (_, _) => true, (_, _) => { ran++; return "{\"gpu_vendor\":\"none\"}"; }, (_, _, _, _, _) => throw new InvalidOperationException(), now: T - 3600));
        Assert.Equal("CLOCK_SKEW", e.Code);
        Assert.Contains("at least 1 h behind", e.Message);
        Assert.Equal(0, ran);
    }
}
