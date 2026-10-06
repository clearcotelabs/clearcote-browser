using System.Text;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Engine patch 1021 (the persona in the environment, "env mode"), SDK side: <see cref="PersonaEnv"/>
/// and the launches that use it. Mirrors the Python suite's tests/test_personaenv.py.
/// </summary>
/// <remarks>
/// Gated on the engine: a launch changes only when the binary implements --persona-from-env, so an
/// r30/r31 engine launches exactly as before. Hermetic: the "engine" is a small file carrying (or not)
/// the switch name the probe looks for. The launch tests run the real LaunchAsync,
/// LaunchPersistentContextAsync, LaunchEphemeralProfileAsync and ServeAsync against a stand-in engine
/// that writes down its command line and CLEARCOTE_PERSONA_ARGS and exits. The launch then fails, which
/// is all these tests need. The stand-in is a shell script, so those tests are POSIX-only.
/// </remarks>
public class PersonaEnvTests : IDisposable
{
    private static readonly Func<string?, string, bool> On = (_, _) => true;   // probe stubs
    private static readonly Func<string?, string, bool> Off = (_, _) => false;
    private static readonly byte[] Engine1021 = Encoding.Latin1.GetBytes("\0persona-from-env\0");

    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();

    public PersonaEnvTests()
    {
        // No licence (a launch would take a real lease), no opt-out, no payload inherited from the shell.
        _sb.TempHome();
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env(PersonaEnv.OptOutEnv, null).Env(PersonaEnv.EnvVar, null)
            .Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null)
            .Env("CLEARCOTE_BROWSER_VERSION", null).Env("CLEARCOTE_NO_WARN", "1");
    }

    public void Dispose()
    {
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    /// A fake engine binary, with or without the switch literal the probe looks for.
    private string FakeEngine(bool with1021)
    {
        var exe = Path.Combine(Dir("cc-pe-engine-"), "chrome");
        var body = new List<byte>(Encoding.Latin1.GetBytes("\x7f" + "ELF"));
        if (with1021) body.AddRange(Engine1021);
        body.AddRange(Encoding.Latin1.GetBytes("\0--remote-debugging-port\0"));
        File.WriteAllBytes(exe, body.ToArray());
        return exe;
    }

    private static List<(string Name, string Value)> Entries(params (string Name, string Value)[] e) => e.ToList();

    // ── the payload and the switch list ──────────────────────────────────────

    [Fact]
    public void The_payload_is_the_engines_format()
    {
        var entries = Entries(("fingerprint", "17"), ("disable-canvas-noise", ""), ("fingerprint-timezone", "Europe/Zürich"));
        var raw = Encoding.ASCII.GetBytes("fingerprint\u000017\u0000disable-canvas-noise\u0000\u0000fingerprint-timezone\u0000Europe/Z")
            .Concat(new byte[] { 0xC3, 0xBC }).Concat(Encoding.ASCII.GetBytes("rich\u0000")).ToArray();
        Assert.Equal(Convert.ToBase64String(raw), PersonaEnv.Encode(entries));
        Assert.Equal(entries, PersonaEnv.Decode(PersonaEnv.Encode(entries)));
        Assert.Empty(PersonaEnv.Decode(""));
        // no closing NUL / a name without its value
        Assert.Throws<FormatException>(() => PersonaEnv.Decode(Convert.ToBase64String(Encoding.ASCII.GetBytes("fingerprint\u000017"))));
        Assert.Throws<FormatException>(() => PersonaEnv.Decode(Convert.ToBase64String(Encoding.ASCII.GetBytes("fingerprint\u0000"))));
    }

    [Theory]
    [InlineData("fingerprint")]
    [InlineData("fingerprint-platform")]
    [InlineData("fingerprint-profile")]
    [InlineData("canvas-bridge-url")]
    [InlineData("canvas-bridge-auth")]
    [InlineData("disable-canvas-noise")]
    [InlineData("disable-fingerprint-noise")]
    [InlineData("disable-fingerprint-voices")]
    [InlineData("disable-gpu-fingerprint")]
    [InlineData("disable-gpu-string-spoof")]
    [InlineData("proxy-auth")]
    [InlineData("socks5-credentials")]
    [InlineData("webrtc-ip")]
    public void The_engines_transported_switches(string name) => Assert.True(PersonaEnv.IsTransported(name));

    // The engine stops the launch on a name outside its list: the SDK's list must never be wider.
    [Theory]
    [InlineData("proxy-server")]
    [InlineData("lang")]
    [InlineData("user-data-dir")]
    [InlineData("fingerprinting")]
    [InlineData("disable-features")]
    [InlineData("persona-from-env")]
    [InlineData("disable-persona-env-transport")]
    [InlineData("headless")]
    [InlineData("canvas-bridge")]
    public void Everything_else_stays_on_the_command_line(string name) => Assert.False(PersonaEnv.IsTransported(name));

    // ── Apply ────────────────────────────────────────────────────────────────

    [Fact]
    public void Persona_switches_move_into_the_environment()
    {
        var args = new[] { "--fingerprint=17", "--lang=en-US", "--fingerprint-platform=windows", "--socks5-credentials=u:p",
            "--no-first-run", "https://example.com/" };
        var baseEnv = new Dictionary<string, string> { ["KEEP"] = "1" };
        var (newArgs, env) = PersonaEnv.Apply("chrome", args, baseEnv, supports: On);
        Assert.Equal(new[] { "--lang=en-US", "--no-first-run", "https://example.com/", "--persona-from-env" }, newArgs);
        Assert.Equal(Entries(("fingerprint", "17"), ("fingerprint-platform", "windows"), ("socks5-credentials", "u:p")),
            PersonaEnv.Decode(env![PersonaEnv.EnvVar]));
        Assert.Equal("1", env["KEEP"]);
        Assert.False(baseEnv.ContainsKey(PersonaEnv.EnvVar));   // the caller's dictionary is not touched
        Assert.Equal("--fingerprint=17", args[0]);
    }

    [Fact]
    public void The_process_environment_is_the_base()
    {
        _sb.Env("CC_TEST_MARKER", "1");
        var (_, env) = PersonaEnv.Apply("chrome", new[] { "--fingerprint=1" }, null, supports: On);
        Assert.Equal("1", env!["CC_TEST_MARKER"]);
        Assert.True(env.ContainsKey(PersonaEnv.EnvVar));
    }

    [Fact]
    public void The_last_copy_of_a_repeated_switch_wins()
    {
        // On a command line Chromium keeps the LAST copy; the engine adopts the FIRST payload entry.
        var args = new[] { "--fingerprint-hardware-concurrency=8", "--fingerprint=1", "--fingerprint-hardware-concurrency=4" };
        var (_, env) = PersonaEnv.Apply("chrome", args, new Dictionary<string, string>(), supports: On);
        Assert.Equal(Entries(("fingerprint", "1"), ("fingerprint-hardware-concurrency", "4")), PersonaEnv.Decode(env![PersonaEnv.EnvVar]));
    }

    [Fact]
    public void Inert_on_an_engine_without_the_switch()
    {
        var args = new[] { "--fingerprint=17", "--lang=en-US" };
        var baseEnv = new Dictionary<string, string> { ["KEEP"] = "1" };
        var (newArgs, env) = PersonaEnv.Apply("chrome", args, baseEnv, supports: Off);
        Assert.Equal(args, newArgs);
        Assert.Same(baseEnv, env);
    }

    [Theory]
    [InlineData(false, null)]
    [InlineData(null, "0")]
    [InlineData(null, "false")]
    [InlineData(null, "off")]
    [InlineData(null, "no")]
    [InlineData(null, " OFF ")]
    [InlineData(null, "False")]
    public void Turned_off(bool? enabled, string? envValue)
    {
        if (envValue is not null) _sb.Env(PersonaEnv.OptOutEnv, envValue);
        var args = new[] { "--fingerprint=17" };
        var (newArgs, env) = PersonaEnv.Apply("chrome", args, null, enabled, supports: On);
        Assert.Equal(args, newArgs);
        Assert.Null(env);
    }

    [Theory]
    [InlineData("1")]
    [InlineData("yes")]
    [InlineData("")]
    public void Any_other_environment_value_leaves_it_on(string envValue)
    {
        _sb.Env(PersonaEnv.OptOutEnv, envValue);
        Assert.Equal(new[] { "--persona-from-env" }, PersonaEnv.Apply("chrome", new[] { "--fingerprint=17" }, null, supports: On).Args);
    }

    [Fact]
    public void The_option_beats_the_environment_variable()
    {
        _sb.Env(PersonaEnv.OptOutEnv, "0");
        var (newArgs, env) = PersonaEnv.Apply("chrome", new[] { "--fingerprint=17" }, new Dictionary<string, string>(), enabled: true, supports: On);
        Assert.Equal(new[] { "--persona-from-env" }, newArgs);
        Assert.True(env!.ContainsKey(PersonaEnv.EnvVar));
    }

    [Theory]
    [InlineData("--disable-persona-env-transport")]
    [InlineData("--persona-from-env")]
    [InlineData("--disable-persona-env-transport=1")]
    [InlineData("--persona-from-env=1")]
    public void A_transport_the_caller_chose_is_left_alone(string chosen)
    {
        var args = new[] { "--fingerprint=17", chosen };
        var (newArgs, env) = PersonaEnv.Apply("chrome", args, null, supports: On);
        Assert.Equal(args, newArgs);
        Assert.Null(env);
    }

    [Fact]
    public void Nothing_to_move_does_not_even_probe()
    {
        Func<string?, string, bool> probe = (_, _) => throw new Xunit.Sdk.XunitException("probed without a persona switch to move");
        var args = new[] { "--lang=en-US", "--no-first-run" };
        var (newArgs, env) = PersonaEnv.Apply("chrome", args, null, supports: probe);
        Assert.Equal(args, newArgs);
        Assert.Null(env);
    }

    [Fact]
    public void An_oversized_persona_stays_on_the_command_line()
    {
        var args = new[] { "--fingerprint=17", "--fingerprint-profile=" + new string('A', 40000) };
        var (newArgs, env) = PersonaEnv.Apply("chrome", args, null, supports: On);
        Assert.Equal(args, newArgs);
        Assert.Null(env);
    }

    [Fact]
    public void A_persona_right_at_the_cap_still_moves()
    {
        // "fingerprint-profile\0" + value + "\0": 22,500 bytes is 30,000 base64 characters, one more byte is 30,004.
        var atCap = new[] { "--fingerprint-profile=" + new string('A', 22479) };
        var (newArgs, env) = PersonaEnv.Apply("chrome", atCap, new Dictionary<string, string>(), supports: On);
        Assert.Equal(new[] { "--persona-from-env" }, newArgs);
        Assert.Equal(PersonaEnv.MaxPayload, env![PersonaEnv.EnvVar].Length);
        var over = new[] { "--fingerprint-profile=" + new string('A', 22480) };
        Assert.Equal(over, PersonaEnv.Apply("chrome", over, null, supports: On).Args);
    }

    [Fact]
    public void The_real_probe_reads_the_engine()
    {
        string with1021 = FakeEngine(with1021: true), without = FakeEngine(with1021: false);
        Assert.Equal(new[] { "--persona-from-env" }, PersonaEnv.Apply(with1021, new[] { "--fingerprint=17" }, new Dictionary<string, string>()).Args);
        Assert.Equal(new[] { "--fingerprint=17" }, PersonaEnv.Apply(without, new[] { "--fingerprint=17" }, new Dictionary<string, string>()).Args);
    }

    // ── where the option goes ────────────────────────────────────────────────

    [Fact]
    public void Docker_accepts_and_ignores_it_and_a_cloud_launch_refuses_it()
    {
        // The image's own launch picks the transport; as in Python, persona_env is not a cloud option.
        Assert.DoesNotContain(DockerLaunch.RefusedOptions, p => p.Name == "PersonaEnv");
        Assert.Equal(DockerLaunch.ContainerEnv(new LaunchOptions { Fingerprint = "17" }),
            DockerLaunch.ContainerEnv(new LaunchOptions { Fingerprint = "17", PersonaEnv = false }));
        Assert.Equal("PersonaEnv is not available for cloud browsers",
            Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { PersonaEnv = true })).Message);
    }

    // ── the launch entry points ──────────────────────────────────────────────

    /// A stand-in engine: writes its arguments and CLEARCOTE_PERSONA_ARGS next to itself, then exits.
    /// The probe's literal sits after the exit, where the shell never reads.
    private string Standin(bool with1021)
    {
        var exe = Path.Combine(Dir("cc-pe-standin-"), "chrome");
        const string script = "#!/bin/sh\n" +
            "d=$(dirname \"$0\")\n" +
            "printf '%s\\n' \"$@\" > \"$d/argv\"\n" +
            "printf '%s' \"${CLEARCOTE_PERSONA_ARGS-unset}\" > \"$d/persona\"\n" +
            "exit 1\n";
        var body = new List<byte>(Encoding.ASCII.GetBytes(script));
        if (with1021) body.AddRange(Engine1021);
        body.Add((byte)'\n');
        File.WriteAllBytes(exe, body.ToArray());
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    /// What the stand-in was started with: its arguments and its decoded persona payload (null: none).
    private static (List<string> Argv, List<(string Name, string Value)>? Persona) Seen(string exe, Exception launchError)
    {
        var dir = Path.GetDirectoryName(exe)!;
        Assert.True(File.Exists(Path.Combine(dir, "argv")), $"the stand-in engine never ran: {launchError}");
        var persona = File.ReadAllText(Path.Combine(dir, "persona"));
        return (File.ReadAllLines(Path.Combine(dir, "argv")).ToList(), persona == "unset" ? null : PersonaEnv.Decode(persona));
    }

    private static LaunchOptions Opts(string exe, bool? personaEnv = null) => new()
    {
        ExecutablePath = exe, Fingerprint = "17", Platform = "windows", Headless = true, Quiet = true, Timeout = 20000,
        PersonaEnv = personaEnv,
    };

    private static void AssertMoved((List<string> Argv, List<(string Name, string Value)>? Persona) seen)
    {
        Assert.Contains("--persona-from-env", seen.Argv);
        Assert.DoesNotContain(seen.Argv, a => a.StartsWith("--fingerprint", StringComparison.Ordinal));
        Assert.NotNull(seen.Persona);
        Assert.Contains(("fingerprint", "17"), seen.Persona!);
        Assert.Contains(("fingerprint-platform", "windows"), seen.Persona!);
    }

    private static void AssertUnchanged((List<string> Argv, List<(string Name, string Value)>? Persona) seen)
    {
        Assert.Contains("--fingerprint=17", seen.Argv);
        Assert.DoesNotContain("--persona-from-env", seen.Argv);
        Assert.Null(seen.Persona);
    }

    [Fact]
    public async Task Persistent_context_on_a_1021_engine()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-pe-udd-"), Opts(exe)));
        AssertMoved(Seen(exe, e));
    }

    [Fact]
    public async Task Persistent_context_on_an_older_engine_is_unchanged()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: false);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-pe-udd-"), Opts(exe)));
        AssertUnchanged(Seen(exe, e));
    }

    [Fact]
    public async Task Plain_launch_on_a_1021_engine()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(Opts(exe)));
        AssertMoved(Seen(exe, e));
    }

    [Fact]
    public async Task PersonaEnv_false_keeps_the_command_line()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: true);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-pe-udd-"), Opts(exe, personaEnv: false)));
        AssertUnchanged(Seen(exe, e));
    }

    [Fact]
    public async Task Ephemeral_profile_on_a_1021_engine_and_the_failed_launch_leaves_nothing()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: true);
        var temp = Dir("cc-pe-tmp-");
        _sb.Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);   // where the throwaway profile goes
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(Opts(exe)));
        AssertMoved(Seen(exe, e));
        Assert.Empty(Directory.EnumerateFileSystemEntries(temp));
    }

    [Fact]
    public async Task Serve_on_a_1021_engine()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: true);
        var temp = Dir("cc-pe-tmp-");
        _sb.Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);   // where ServeAsync's own profile goes
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync(new ServeOptions
        {
            ExecutablePath = exe, Fingerprint = "17", Platform = "windows", Quiet = true, ReadyTimeoutMs = 20000,
        }));
        var seen = Seen(exe, e);
        AssertMoved(seen);
        Assert.Contains(seen.Argv, a => a.StartsWith("--remote-debugging-port=", StringComparison.Ordinal));   // the CDP switches stay
        Assert.Empty(Directory.EnumerateFileSystemEntries(temp));
    }

    [Fact]
    public async Task Serve_on_an_older_engine_is_unchanged()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(with1021: false);
        var temp = Dir("cc-pe-tmp-");
        _sb.Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);
        var e = await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync(new ServeOptions
        {
            ExecutablePath = exe, Fingerprint = "17", Platform = "windows", Quiet = true, ReadyTimeoutMs = 20000,
        }));
        AssertUnchanged(Seen(exe, e));
    }
}
