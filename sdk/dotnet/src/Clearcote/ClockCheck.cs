// The engine refuses a run token (and a persona token) that, by THIS computer's clock, is not valid yet or already
// expired. A clock far behind or ahead then shows up as a licence refusal ("no valid license lease for this run")
// that no licence key fixes (CR-2VFYCD: a Windows clock a minute slow refused a free key and a Pro key alike). So
// before a launch, check the token the same way and, when the engine would refuse, say the clock is off, by how
// much, and how to fix it.
//
// The servers backdate nbf by 15 minutes, so only a clock off by more than that is caught. iat is the server's time
// when it minted the token, so iat - now is a lower bound on how far behind this clock runs ("at least").
//
// Mirrors the Node SDK's clockcheck.ts, the Python SDK's _clockcheck.py and the Profile Manager's clockcheck.ts.
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Clearcote;

/// The engine refuses the token because this computer's clock is off. <see cref="SkewSeconds"/> is negative when the clock runs behind.
public sealed class ClockSkewError : LicenseError
{
    public double SkewSeconds { get; }
    public ClockSkewError(string message, double skewSeconds) : base(message, "CLOCK_SKEW") => SkewSeconds = skewSeconds;
}

public static class ClockCheck
{
    private const string Fix = "Set the clock to update automatically (Windows: Settings > Time & language > Date & time > "
        + "\"Set time automatically\", then \"Sync now\"; macOS: System Settings > General > Date & Time; "
        + "Linux: `timedatectl set-ntp true`) and launch again.";

    private static (double? Nbf, double? Iat, double? Exp)? Times(string token)
    {
        try
        {
            var body = token.Split('.')[0].Replace('-', '+').Replace('_', '/');
            body += new string('=', (4 - body.Length % 4) % 4);
            using var doc = JsonDocument.Parse(Encoding.UTF8.GetString(Convert.FromBase64String(body)));
            if (doc.RootElement.ValueKind != JsonValueKind.Object) return null;
            double? Num(string name) => doc.RootElement.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : null;
            return (Num("nbf"), Num("iat"), Num("exp"));
        }
        catch (Exception) { return null; }
    }

    /// "3 min 12 s", "2 h 5 min", "1 day 4 h".
    public static string FormatDuration(double sec)
    {
        var s = Math.Max(1L, (long)Math.Round(Math.Abs(sec), MidpointRounding.AwayFromZero));
        long d = s / 86400, h = s % 86400 / 3600, m = s % 3600 / 60, r = s % 60;
        if (d > 0) return $"{d} day{(d == 1 ? "" : "s")}" + (h > 0 ? $" {h} h" : "");
        if (h > 0) return $"{h} h" + (m > 0 ? $" {m} min" : "");
        if (m > 0) return $"{m} min" + (r > 0 ? $" {r} s" : "");
        return $"{r} s";
    }

    private static string Utc(double sec) =>
        DateTimeOffset.FromUnixTimeSeconds((long)sec).UtcDateTime.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture) + " UTC";

    private static double NowSec() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    /// The message and skew for a token the engine would refuse on this clock, or null when the clock is fine for it.
    public static (string Message, double SkewSeconds)? Problem(string? token, double? now = null, string what = "licence token")
    {
        if (string.IsNullOrEmpty(token) || Times(token) is not { } t) return null;
        var n = now ?? NowSec();
        if (t.Nbf is { } nbf && n < nbf)
        {
            var issued = t.Iat ?? nbf;
            var behind = issued - n;
            return ($"This computer's clock is at least {FormatDuration(behind)} behind (it reads {Utc(n)}; the server "
                + $"issued this {what} at {Utc(issued)}). The browser checks the {what} against this computer's clock "
                + $"and refuses one that is not valid yet, whatever the licence key. {Fix}", -behind);
        }
        if (t.Exp is { } exp && n >= exp)
        {
            var ahead = n - (t.Iat ?? exp);
            var iss = t.Iat is { } iat ? $", although the server issued it at {Utc(iat)}" : "";
            return ($"This computer's clock looks wrong: it reads {Utc(n)}, after the moment this {what} expires "
                + $"({Utc(exp)}){iss}. The browser refuses an expired {what}, whatever the licence key. {Fix}", ahead);
        }
        return null;
    }

    /// Throw <see cref="ClockSkewError"/> when the engine would refuse <paramref name="token"/> on this computer's clock.
    public static void AssertFor(string? token, string what = "licence token", double? now = null)
    {
        if (Problem(token, now, what) is { } p) throw new ClockSkewError(p.Message, p.SkewSeconds);
    }
}
