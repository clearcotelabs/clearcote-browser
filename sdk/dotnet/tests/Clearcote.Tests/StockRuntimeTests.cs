using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// <see cref="LaunchOptions.StockRuntime"/> / CLEARCOTE_STOCK_RUNTIME: Chromium's own DevTools Runtime behaviour,
/// back on request. The engine holds back part of what V8 reports to a DevTools client (patch 110); engine r32
/// adds --disable-runtime-suppression, which restores stock Chromium for it (Playwright's Console and PageError
/// events, SetContentAsync, ExposeFunctionAsync across navigations). Off by default and, like every new engine
/// switch, gated on the binary: an engine without the switch launches without it, with one warning per process.
/// A normal browser argument, never part of the persona payload; a Docker launch hands it to the image's
/// entrypoint; a cloud browser does not take it. Mirrors the Python suite's tests/test_stock_runtime.py.
/// </summary>
/// <remarks>
/// The launch tests run the real LaunchAsync, LaunchPersistentContextAsync, LaunchEphemeralProfileAsync and
/// ServeAsync against a stand-in engine that writes down its command line and CLEARCOTE_PERSONA_ARGS and exits.
/// The launch then fails, which is all these tests need. The stand-in is a shell script, so those tests are
/// POSIX-only.
/// </remarks>
public class StockRuntimeTests : IDisposable
{
    private const string Switch = "--disable-runtime-suppression";
    private const string EnvName = "CLEARCOTE_STOCK_RUNTIME";
    private const string Unsupported = "this engine does not support it";   // in the one-time warning
    private const string CloudOnlyLocal = "only applies to local and Docker launches";   // in the cloud's one-time warning
    private static readonly byte[] EngineR32 = Encoding.Latin1.GetBytes("\0disable-runtime-suppression\0");
    private static readonly byte[] Engine1021 = Encoding.Latin1.GetBytes("\0persona-from-env\0");

    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();
    private readonly TextWriter _savedErr = Console.Error;
    private readonly StringWriter _err = new();

    public StockRuntimeTests()
    {
        // No licence (a launch would take a real lease), nothing inherited from the shell, warnings on, and a
        // temp directory of our own for the profiles the launches make.
        _sb.TempHome();
        var temp = Dir("cc-sr-tmp-");
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env(EnvName, null).Env("CLEARCOTE_NO_WARN", null)
            .Env(PersonaEnv.OptOutEnv, null).Env(PersonaEnv.EnvVar, null).Env("CLEARCOTE_CLOUD", null)
            .Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null)
            .Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);
        LaunchWarnings.ForgetSaid();
        Console.SetError(TextWriter.Synchronized(_err));
    }

    public void Dispose()
    {
        Console.SetError(_savedErr);
        LaunchWarnings.ForgetSaid();
        CloudLaunch.ConnectOverride = null;
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    private int Said(string text) => _err.ToString().Split(text).Length - 1;

    private string Line(string text) => _err.ToString().Split('\n').First(l => l.Contains(text, StringComparison.Ordinal));

    private static int Count(IEnumerable<string> argv) => argv.Count(a => a == Switch);

    /// A stand-in engine: writes its arguments and CLEARCOTE_PERSONA_ARGS next to itself, then exits. The probe's
    /// literals sit after the exit, where the shell never reads.
    private string Standin(bool r32, bool with1021 = false)
    {
        var exe = Path.Combine(Dir("cc-sr-engine-"), "chrome");
        const string script = "#!/bin/sh\n" +
            "d=$(dirname \"$0\")\n" +
            "printf '%s\\n' \"$@\" > \"$d/argv\"\n" +
            "printf '%s' \"${CLEARCOTE_PERSONA_ARGS-unset}\" > \"$d/persona\"\n" +
            "exit 1\n";
        var body = new List<byte>(Encoding.ASCII.GetBytes(script));
        if (r32) body.AddRange(EngineR32);
        if (with1021) body.AddRange(Engine1021);
        body.Add((byte)'\n');
        File.WriteAllBytes(exe, body.ToArray());
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    /// Launch the stand-in through <paramref name="entry"/>; returns its arguments and decoded persona payload.
    private async Task<(List<string> Argv, List<(string Name, string Value)>? Persona)> Launch(
        string entry, string exe, Action<LaunchOptions>? set = null)
    {
        LaunchOptions o = entry == "serve" ? new ServeOptions { ReadyTimeoutMs = 20000 } : new LaunchOptions();
        o.ExecutablePath = exe; o.Headless = true; o.Quiet = true; o.Timeout = 20000;
        set?.Invoke(o);
        var argvFile = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
        File.Delete(argvFile);
        var e = entry switch
        {
            "launch" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(o)),
            "persistent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-sr-udd-"), o)),
            "ephemeral" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(o)),
            "serve" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync((ServeOptions)o)),
            _ => throw new ArgumentException(entry),
        };
        Assert.True(File.Exists(argvFile), $"the stand-in engine never ran: {e}");
        var persona = File.ReadAllText(Path.Combine(Path.GetDirectoryName(exe)!, "persona"));
        return (File.ReadAllLines(argvFile).ToList(), persona == "unset" ? null : PersonaEnv.Decode(persona));
    }

    // ── on, off, and the environment variable ────────────────────────────────

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("ephemeral")]
    [InlineData("serve")]
    public async Task On_with_an_r32_engine_the_switch_is_there_exactly_once(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(r32: true);
        Assert.Equal(1, Count((await Launch(entry, exe, o => o.StockRuntime = true)).Argv));
        // the caller passed it too: still once
        var seen = await Launch(entry, exe, o => { o.StockRuntime = true; o.Args = new[] { Switch, "--lang=de-DE" }; });
        Assert.Equal(1, Count(seen.Argv));
        Assert.Contains("--lang=de-DE", seen.Argv);
    }

    [Fact]
    public async Task Off_unless_asked()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(r32: true);
        Assert.Equal(0, Count((await Launch("persistent", exe)).Argv));
        Assert.Equal(0, Count((await Launch("persistent", exe, o => o.StockRuntime = false)).Argv));
        _sb.Env(EnvName, "0");
        Assert.Equal(0, Count((await Launch("persistent", exe)).Argv));
        _sb.Env(EnvName, "1");
        Assert.Equal(1, Count((await Launch("persistent", exe)).Argv));
    }

    [Fact]
    public async Task The_environment_variable_and_an_explicit_value_wins_over_it()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(r32: true);
        foreach (var (value, on) in new[] { ("1", true), ("true", true), ("YES", true), (" on ", true),
                     ("0", false), ("", false), ("no", false), ("off", false), ("2", false) })
        {
            _sb.Env(EnvName, value);
            Assert.True((on ? 1 : 0) == Count((await Launch("serve", exe)).Argv), $"CLEARCOTE_STOCK_RUNTIME={value}");
        }
        _sb.Env(EnvName, "1");
        Assert.Equal(0, Count((await Launch("serve", exe, o => o.StockRuntime = false)).Argv));
        _sb.Env(EnvName, "0");
        Assert.Equal(1, Count((await Launch("serve", exe, o => o.StockRuntime = true)).Argv));
    }

    // ── an engine without the switch ─────────────────────────────────────────

    [Fact]
    public async Task An_engine_without_it_launches_without_it_and_says_so_once()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(r32: false);   // r31 / an open build: no such switch
        Assert.Equal(0, Count((await Launch("persistent", exe, o => { o.StockRuntime = true; o.Quiet = false; })).Argv));
        Assert.Equal(0, Count((await Launch("serve", exe, o => { o.StockRuntime = true; o.Quiet = false; })).Argv));
        Assert.Equal(1, Said(Unsupported));
        Assert.StartsWith("clearcote: warning: StockRuntime", Line(Unsupported));
        Assert.Contains("r32", Line(Unsupported));
    }

    [Fact]
    public async Task Quiet_and_no_warn_silence_it_without_using_it_up()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(r32: false);
        await Launch("serve", exe, o => o.StockRuntime = true);   // Quiet
        _sb.Env("CLEARCOTE_NO_WARN", "1");
        await Launch("serve", exe, o => { o.StockRuntime = true; o.Quiet = false; });
        Assert.Equal(0, Said(Unsupported));
        _sb.Env("CLEARCOTE_NO_WARN", null);
        await Launch("serve", exe, o => { o.StockRuntime = true; o.Quiet = false; });
        Assert.Equal(1, Said(Unsupported));
    }

    // ── not a persona switch ─────────────────────────────────────────────────

    [Fact]
    public async Task It_stays_on_the_command_line_when_the_persona_moves_to_the_environment()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(r32: true, with1021: true);
        var seen = await Launch("persistent", exe, o => { o.Fingerprint = "17"; o.Platform = "windows"; o.StockRuntime = true; });
        Assert.Contains("--persona-from-env", seen.Argv);   // the persona did move...
        Assert.Equal(1, Count(seen.Argv));                  // ...and this switch did not
        Assert.NotNull(seen.Persona);
        Assert.Contains(("fingerprint", "17"), seen.Persona!);
        Assert.DoesNotContain(seen.Persona!, e => e.Name == "disable-runtime-suppression");
        Assert.False(PersonaEnv.IsTransported("disable-runtime-suppression"));
    }

    // ── Docker ───────────────────────────────────────────────────────────────

    [Fact]
    public void Docker_hands_the_switch_to_the_images_entrypoint_with_the_other_engine_switches()
    {
        // The container's browser is started by the image's entrypoint (docker/serve.py), which keeps the switch
        // only on an engine that has it.
        Assert.DoesNotContain(DockerLaunch.RefusedOptions, p => p.Name == "StockRuntime");
        Assert.False(DockerLaunch.ContainerEnv(new LaunchOptions { Fingerprint = "17" }).ContainsKey("CC_EXTRA_ARGS"));
        Assert.False(DockerLaunch.ContainerEnv(new LaunchOptions { Fingerprint = "17", StockRuntime = false }).ContainsKey("CC_EXTRA_ARGS"));
        Assert.Equal(Switch, DockerLaunch.ContainerEnv(new LaunchOptions { StockRuntime = true })["CC_EXTRA_ARGS"]);
        Assert.Equal($"--lang=de-DE {Switch}",
            DockerLaunch.ContainerEnv(new LaunchOptions { StockRuntime = true, Args = new[] { "--lang=de-DE" } })["CC_EXTRA_ARGS"]);
        Assert.Equal($"{Switch} --lang=de-DE",   // once
            DockerLaunch.ContainerEnv(new LaunchOptions { StockRuntime = true, Args = new[] { Switch, "--lang=de-DE" } })["CC_EXTRA_ARGS"]);
        _sb.Env(EnvName, "1");
        Assert.Equal(Switch, DockerLaunch.ContainerEnv(new LaunchOptions())["CC_EXTRA_ARGS"]);
        Assert.False(DockerLaunch.ContainerEnv(new LaunchOptions { StockRuntime = false }).ContainsKey("CC_EXTRA_ARGS"));   // the option wins
    }

    // ── cloud ────────────────────────────────────────────────────────────────

    [Fact]
    public async Task A_cloud_launch_does_not_take_it_and_says_so_once()
    {
        await using var api = new FakeCloud();
        _sb.Env("CLEARCOTE_API_KEY", FakeCloud.ApiKey).Env("CLEARCOTE_API_URL", api.Url);
        CloudLaunch.ConnectOverride = (_, _) => Task.FromResult(CloudTests.FakeBrowser.Make().Browser);
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, StockRuntime = true, Country = "us" })).CloseAsync();
        await Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "acct-1", StockRuntime = true });
        var bodies = api.Requests("POST", "/api/v1/browsers").Select(r => (JsonObject)r.Body!).ToList();
        Assert.Equal(2, bodies.Count);
        Assert.Equal(new[] { "country" }, bodies[0].Select(p => p.Key).ToArray());   // nothing about it reaches the API
        Assert.DoesNotContain(bodies.SelectMany(b => b.Select(p => p.Key)), k => k.Contains("runtime", StringComparison.OrdinalIgnoreCase));
        Assert.Equal(1, Said(CloudOnlyLocal));
        Assert.StartsWith("clearcote: warning: StockRuntime", Line(CloudOnlyLocal));
        // the environment variable too
        LaunchWarnings.ForgetSaid();
        _sb.Env(EnvName, "1");
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true })).CloseAsync();
        Assert.Equal(2, Said(CloudOnlyLocal));
        LaunchWarnings.ForgetSaid();
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Quiet = true, StockRuntime = true })).CloseAsync();   // quiet: not said
        Assert.Equal(2, Said(CloudOnlyLocal));
    }
}
