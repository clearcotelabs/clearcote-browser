using System.Text;

namespace Clearcote;

/// <summary>
/// Keep the persona off the browser's own command line (engine patch 1021, "env mode").
/// </summary>
/// <remarks>
/// <para>A browser's command line is readable by any local user (<c>/proc/&lt;pid&gt;/cmdline</c> on Linux,
/// the process environment block on Windows), and Clearcote's carries the seed, every persona override,
/// proxy credentials (<c>--proxy-auth</c>, <c>--socks5-credentials</c>) and the canvas-bridge token. An
/// engine with patch 1021 already moves those switches off every CHILD process's command line: they
/// travel in the <c>CLEARCOTE_PERSONA_ARGS</c> environment variable, which the children inherit. With
/// <c>--persona-from-env</c> the browser process reads them from that variable too, so the launcher can
/// keep them off the browser's own command line as well. That is what this class does.</para>
///
/// <para>It acts only when the engine implements <c>--persona-from-env</c> (probed in the binary, like
/// every other new engine switch: <see cref="LaunchOpts.EngineSupportsSwitch"/>). An older engine gets
/// the switches on its command line exactly as before.</para>
///
/// <para>The payload format and the switch list mirror <c>components/ungoogled/persona_transport.cc</c>:
/// base64 of <c>name\0value\0name\0value\0</c> with switch names that engine accepts. The engine stops
/// the launch on a payload it cannot decode or on a name outside its list, so the list here must never
/// be wider than the engine's. Chromium keeps the LAST copy of a repeated switch on a command line,
/// while the engine adopts the FIRST matching entry of the payload, so each name goes in once, with its
/// last value.</para>
///
/// <para>Turn it off with <see cref="LaunchOptions.PersonaEnv"/> = false or <c>CLEARCOTE_PERSONA_ENV=0</c>.
/// Mirrors the Python SDK's <c>_personaenv.py</c>.</para>
/// </remarks>
internal static class PersonaEnv
{
    /// The variable the engine reads the persona switches from.
    internal const string EnvVar = "CLEARCOTE_PERSONA_ARGS";
    internal const string FromEnvSwitch = "persona-from-env";
    /// The engine's own switch back to the pre-1021 behaviour.
    internal const string KillSwitch = "disable-persona-env-transport";
    internal const string OptOutEnv = "CLEARCOTE_PERSONA_ENV";
    /// The engine's cap for the variable it republishes for its children (Windows caps one environment
    /// variable at 32,767 characters). Above it the switches simply stay on the command line.
    internal const int MaxPayload = 30000;

    private static readonly HashSet<string> Names = new(StringComparer.Ordinal)
    {
        "disable-canvas-noise", "disable-fingerprint-noise", "disable-fingerprint-voices",
        "disable-gpu-fingerprint", "disable-gpu-string-spoof", "proxy-auth", "socks5-credentials",
        "webrtc-ip",
    };

    private static readonly string[] Off = { "0", "false", "off", "no" };

    // Decode is strict (a payload that is not UTF-8 is malformed). Encode is not, so a launch never
    // fails here, after its lease is taken: a lone surrogate becomes U+FFFD, as it does on its way to a
    // command line too.
    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    /// Whether the engine carries switch <paramref name="name"/> (no leading dashes) in the environment.
    internal static bool IsTransported(string name)
        => name == "fingerprint" || name.StartsWith("fingerprint-", StringComparison.Ordinal)
           || name.StartsWith("canvas-bridge-", StringComparison.Ordinal) || Names.Contains(name);

    /// (name, value) of a <c>--name[=value]</c> argument, else null (a URL or a non-switch).
    private static (string Name, string Value)? Switch(string? arg)
    {
        if (arg is null || !arg.StartsWith("--", StringComparison.Ordinal) || arg.Length == 2) return null;
        var body = arg[2..];
        var eq = body.IndexOf('=');
        var name = eq < 0 ? body : body[..eq];
        return name.Length == 0 ? null : (name, eq < 0 ? "" : body[(eq + 1)..]);
    }

    /// The <c>CLEARCOTE_PERSONA_ARGS</c> value for <paramref name="entries"/>.
    internal static string Encode(IEnumerable<(string Name, string Value)> entries)
    {
        var raw = new List<byte>();
        foreach (var (n, v) in entries)
        {
            raw.AddRange(Encoding.UTF8.GetBytes(n)); raw.Add(0);
            raw.AddRange(Encoding.UTF8.GetBytes(v)); raw.Add(0);
        }
        return Convert.ToBase64String(raw.ToArray());
    }

    /// The inverse of <see cref="Encode"/> (tests and diagnostics). Throws <see cref="FormatException"/>
    /// on a malformed payload.
    internal static List<(string Name, string Value)> Decode(string payload)
    {
        var raw = Convert.FromBase64String(payload);
        // Python's bytes.split(b"\0"): the pieces between NULs, the one after the last included.
        var parts = new List<byte[]>();
        var start = 0;
        for (var i = 0; i <= raw.Length; i++)
        {
            if (i < raw.Length && raw[i] != 0) continue;
            parts.Add(raw[start..i]);
            start = i + 1;
        }
        if (parts.Count == 0 || parts[^1].Length != 0 || parts.Count % 2 != 1)
            throw new FormatException("malformed persona payload");
        var entries = new List<(string, string)>();
        for (var i = 0; i < parts.Count - 1; i += 2) entries.Add((StrictUtf8.GetString(parts[i]), StrictUtf8.GetString(parts[i + 1])));
        return entries;
    }

    /// <see cref="LaunchOptions.PersonaEnv"/>: null follows <c>CLEARCOTE_PERSONA_ENV</c> (on unless it
    /// says 0/false/off/no, in any case); an explicit value wins over it.
    internal static bool Wanted(bool? enabled = null)
    {
        if (enabled is bool b) return b;
        var env = (Environment.GetEnvironmentVariable(OptOutEnv) ?? "").Trim().ToLowerInvariant();
        return Array.IndexOf(Off, env) < 0;
    }

    /// <summary>The transport for one launch: the browser's args, and the payload its environment
    /// carries (null: none, the args are the caller's unchanged).</summary>
    internal sealed record Transport(List<string> Args, string? Payload)
    {
        /// <paramref name="env"/> with <c>CLEARCOTE_PERSONA_ARGS</c> added, as a new dictionary;
        /// <paramref name="env"/> itself when there is no payload. A null env is the current process
        /// environment: Playwright REPLACES the child env when Env is set, so the parent's has to come
        /// along or the browser loses PATH. The caller's dictionary is never changed.
        internal IDictionary<string, string>? Env(IDictionary<string, string>? env)
        {
            if (Payload is null) return env;
            var outEnv = new Dictionary<string, string>();
            if (env is not null)
            {
                foreach (var (k, v) in env) if (v is not null) outEnv[k] = v;
            }
            else
            {
                foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables())
                    if (e.Value is string sv) outEnv[(string)e.Key] = sv;
            }
            outEnv[EnvVar] = Payload;
            return outEnv;
        }
    }

    /// <summary>
    /// The args (and payload) for a launch of <paramref name="exe"/>: when the engine implements
    /// <c>--persona-from-env</c> and env mode is wanted, the persona switches leave the args for the
    /// payload and <c>--persona-from-env</c> is appended; otherwise the args come back unchanged, with no
    /// payload.
    /// </summary>
    /// <remarks>
    /// Split from <see cref="Apply"/> for the Playwright launches, whose env is rebuilt per attempt (a
    /// launch retried after a stale-token refusal carries the lease's fresh token): the binary is probed
    /// once, and <see cref="Transport.Env"/> adds the payload to each attempt's env. Leaves everything as
    /// it is when the caller already chose a transport (either switch in the args, with or without a
    /// value), when there is no persona switch to move (the binary is then not even probed), on an engine
    /// without the switch, and when the payload would exceed <see cref="MaxPayload"/>.
    /// <paramref name="supports"/> is the engine probe (tests pass a stub).
    /// </remarks>
    internal static Transport Plan(string? exe, IEnumerable<string>? args, bool? enabled = null,
        Func<string?, string, bool>? supports = null)
    {
        var list = args?.ToList() ?? new List<string>();
        var unchanged = new Transport(list, null);
        if (!Wanted(enabled)) return unchanged;
        var switches = list.Select(Switch).ToList();
        if (switches.Any(s => s is { } sw && (sw.Name == FromEnvSwitch || sw.Name == KillSwitch)))
            return unchanged;  // the caller already chose a transport, or asked the engine for the old one
        var last = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var i = 0; i < switches.Count; i++)
            if (switches[i] is { } sw && IsTransported(sw.Name)) last[sw.Name] = i;
        if (last.Count == 0) return unchanged;
        if (!(supports ?? LaunchOpts.EngineSupportsSwitch)(exe, FromEnvSwitch)) return unchanged;
        var payload = Encode(last.Values.Order().Select(i => switches[i]!.Value));
        if (payload.Length > MaxPayload) return unchanged;
        var kept = list.Where((_, i) => !(switches[i] is { } sw && IsTransported(sw.Name))).ToList();
        kept.Add("--" + FromEnvSwitch);
        return new Transport(kept, payload);
    }

    /// <summary>
    /// Return (args, env) for a launch of <paramref name="exe"/> (see <see cref="Plan"/>).
    /// </summary>
    /// <remarks>
    /// When the persona moves, its switches leave <paramref name="args"/> and travel in
    /// <c>env[CLEARCOTE_PERSONA_ARGS]</c>, a new dictionary based on <paramref name="env"/> (null: the
    /// process environment). Otherwise the args come back unchanged and <paramref name="env"/> is returned
    /// as is, null included.
    /// </remarks>
    internal static (List<string> Args, IDictionary<string, string>? Env) Apply(string? exe, IEnumerable<string>? args,
        IDictionary<string, string>? env = null, bool? enabled = null, Func<string?, string, bool>? supports = null)
    {
        var t = Plan(exe, args, enabled, supports);
        return (t.Args, t.Env(env));
    }
}
