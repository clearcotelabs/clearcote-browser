using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// <see cref="LaunchOptions.RuntimeBindings"/> / CLEARCOTE_RUNTIME_BINDINGS: Runtime.addBinding bindings across
/// navigations, on request. Patch 110 keeps CDP bindings out of documents created after the binding was added, so
/// Playwright's ExposeFunctionAsync / ExposeBindingAsync fail after GotoAsync (clearcote-browser#43). Engine r54
/// (pro-r54-04) adds --enable-runtime-bindings, which restores only that. Off by default, gated on the binary like
/// <see cref="LaunchOptions.StockRuntime"/>, never part of the persona payload; redundant next to StockRuntime, which
/// already restores it. Mirrors the Python suite's tests/test_runtime_bindings.py.
/// </summary>
/// <remarks>
/// The launch tests run the real LaunchAsync, LaunchPersistentContextAsync, LaunchEphemeralProfileAsync and
/// ServeAsync against a stand-in engine that writes down its command line and CLEARCOTE_PERSONA_ARGS and exits (as
/// StockRuntimeTests does). The launch then fails, which is all these tests need. The stand-in is a shell script, so
/// those tests are POSIX-only.
/// </remarks>
public class RuntimeBindingsTests : IDisposable
{
    private const string Switch = "--enable-runtime-bindings";
    private const string Stock = "--disable-runtime-suppression";
    private const string EnvName = "CLEARCOTE_RUNTIME_BINDINGS";
    private const string Unsupported = "RuntimeBindings (CLEARCOTE_RUNTIME_BINDINGS) is on, but this engine does not support it";
    private const string CloudOnlyLocal = "RuntimeBindings (CLEARCOTE_RUNTIME_BINDINGS) only applies to local and Docker launches";
    private const string BindingsNote = "page.ExposeFunctionAsync()";   // in the engine note about bindings
    private const string ConsoleNote = "the engine does not forward console or page-error events";
    private static readonly byte[] EngineR32 = Encoding.Latin1.GetBytes("\0disable-runtime-suppression\0");
    private static readonly byte[] EngineR54 = Encoding.Latin1.GetBytes("\0enable-runtime-bindings\0");
    private static readonly byte[] Engine1021 = Encoding.Latin1.GetBytes("\0persona-from-env\0");

    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();
    private readonly TextWriter _savedErr = Console.Error;
    private readonly StringWriter _err = new();

    public RuntimeBindingsTests()
    {
        // No licence (a launch would take a real lease), nothing inherited from the shell, warnings on, and a
        // temp directory of our own for the profiles the launches make.
        _sb.TempHome();
        var temp = Dir("cc-rb-tmp-");
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env(EnvName, null).Env("CLEARCOTE_STOCK_RUNTIME", null).Env("CLEARCOTE_NO_WARN", null)
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

    private string TakeErr()
    {
        var text = _err.ToString();
        _err.GetStringBuilder().Clear();
        return text;
    }

    private static int Count(IEnumerable<string> argv, string sw = Switch) => argv.Count(a => a == sw);

    /// A stand-in engine: writes its arguments and CLEARCOTE_PERSONA_ARGS next to itself, then exits. The probe's
    /// literals (the switches this engine claims) sit after the exit, where the shell never reads.
    private string Standin(params byte[][] literals)
    {
        var exe = Path.Combine(Dir("cc-rb-engine-"), "chrome");
        const string script = "#!/bin/sh\n" +
            "d=$(dirname \"$0\")\n" +
            "printf '%s\\n' \"$@\" > \"$d/argv\"\n" +
            "printf '%s' \"${CLEARCOTE_PERSONA_ARGS-unset}\" > \"$d/persona\"\n" +
            "exit 1\n";
        var body = new List<byte>(Encoding.ASCII.GetBytes(script));
        foreach (var l in literals) body.AddRange(l);
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
            "persistent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-rb-udd-"), o)),
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
    public async Task On_with_an_r54_engine_the_switch_is_there_exactly_once(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(EngineR32, EngineR54);
        var seen = await Launch(entry, exe, o => o.RuntimeBindings = true);
        Assert.Equal(1, Count(seen.Argv));
        Assert.Equal(0, Count(seen.Argv, Stock));
        // the caller passed it too: still once
        seen = await Launch(entry, exe, o => { o.RuntimeBindings = true; o.Args = new[] { Switch, "--lang=de-DE" }; });
        Assert.Equal(1, Count(seen.Argv));
        Assert.Contains("--lang=de-DE", seen.Argv);
    }

    [Fact]
    public async Task Off_by_default_and_the_environment_variable_and_an_explicit_value_wins_over_it()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(EngineR54);
        Assert.Equal(0, Count((await Launch("persistent", exe)).Argv));
        Assert.Equal(0, Count((await Launch("persistent", exe, o => o.RuntimeBindings = false)).Argv));
        foreach (var (value, on) in new[] { ("1", true), ("true", true), ("YES", true), (" on ", true),
                     ("0", false), ("", false), ("no", false), ("off", false), ("2", false) })
        {
            _sb.Env(EnvName, value);
            Assert.True((on ? 1 : 0) == Count((await Launch("persistent", exe)).Argv), $"CLEARCOTE_RUNTIME_BINDINGS={value}");
        }
        _sb.Env(EnvName, "1");
        Assert.Equal(0, Count((await Launch("persistent", exe, o => o.RuntimeBindings = false)).Argv));   // the option wins
        _sb.Env(EnvName, "0");
        Assert.Equal(1, Count((await Launch("persistent", exe, o => o.RuntimeBindings = true)).Argv));
    }

    [Fact]
    public async Task Not_added_next_to_the_stock_runtime_which_already_restores_it()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(EngineR32, EngineR54);
        var argv = (await Launch("persistent", exe, o => { o.RuntimeBindings = true; o.StockRuntime = true; })).Argv;
        Assert.Equal(1, Count(argv, Stock));
        Assert.Equal(0, Count(argv));
        // the caller's own --disable-runtime-suppression counts the same
        argv = (await Launch("persistent", exe, o => { o.RuntimeBindings = true; o.Args = new[] { Stock }; })).Argv;
        Assert.Equal(1, Count(argv, Stock));
        Assert.Equal(0, Count(argv));
    }

    // ── an engine without the switch ─────────────────────────────────────────

    [Fact]
    public async Task An_engine_without_it_launches_without_it_and_says_so_once()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(EngineR32);   // r32..r53, or an open build without the switch
        for (var i = 0; i < 2; i++)
            Assert.Equal(0, Count((await Launch("persistent", exe, o => { o.RuntimeBindings = true; o.Quiet = false; })).Argv));
        Assert.Equal(0, Count((await Launch("serve", exe, o => { o.RuntimeBindings = true; o.Quiet = false; })).Argv));
        Assert.Equal(1, Said(Unsupported));
        Assert.StartsWith("clearcote: warning: ", Line(Unsupported));
        Assert.Contains("r54", Line(Unsupported));
        Assert.Contains("StockRuntime = true", Line(Unsupported));
    }

    [Fact]
    public async Task Quiet_and_no_warn_silence_it_without_using_it_up()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin();
        await Launch("persistent", exe, o => o.RuntimeBindings = true);   // Quiet
        _sb.Env("CLEARCOTE_NO_WARN", "1");
        await Launch("persistent", exe, o => { o.RuntimeBindings = true; o.Quiet = false; });
        Assert.Equal(0, Said(Unsupported));
        _sb.Env("CLEARCOTE_NO_WARN", null);
        await Launch("persistent", exe, o => { o.RuntimeBindings = true; o.Quiet = false; });
        Assert.Equal(1, Said(Unsupported));
    }

    [Fact]
    public async Task Serve()
    {
        if (OperatingSystem.IsWindows()) return;
        Assert.Equal(1, Count((await Launch("serve", Standin(EngineR54), o => o.RuntimeBindings = true)).Argv));
        Assert.Equal(0, Count((await Launch("serve", Standin(EngineR54))).Argv));
        Assert.Equal(0, Count((await Launch("serve", Standin(), o => { o.RuntimeBindings = true; o.Quiet = false; })).Argv));
        Assert.Equal(1, Said(Unsupported));
    }

    // ── not a persona switch ─────────────────────────────────────────────────

    [Fact]
    public async Task It_stays_on_the_command_line_when_the_persona_moves_to_the_environment()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(Engine1021, EngineR54);
        var seen = await Launch("persistent", exe, o => { o.Fingerprint = "17"; o.Platform = "windows"; o.RuntimeBindings = true; });
        Assert.Contains("--persona-from-env", seen.Argv);   // the persona did move...
        Assert.Equal(1, Count(seen.Argv));                  // ...and this switch did not
        Assert.NotNull(seen.Persona);
        Assert.DoesNotContain(seen.Persona!, e => e.Name == "enable-runtime-bindings");
        Assert.False(PersonaEnv.IsTransported("enable-runtime-bindings"));
    }

    // ── Docker ───────────────────────────────────────────────────────────────

    [Fact]
    public void Docker_hands_the_switch_to_the_images_entrypoint()
    {
        // The container's browser is started by the image's entrypoint (docker/serve.py), which keeps the switch
        // only on an engine that has it and drops it where the stock runtime already restores bindings.
        Assert.DoesNotContain(DockerLaunch.RefusedOptions, p => p.Name == "RuntimeBindings");
        Assert.False(DockerLaunch.ContainerEnv(new LaunchOptions { RuntimeBindings = false }).ContainsKey("CC_EXTRA_ARGS"));
        Assert.Equal(Switch, DockerLaunch.ContainerEnv(new LaunchOptions { RuntimeBindings = true })["CC_EXTRA_ARGS"]);
        Assert.Equal(Switch,   // once
            DockerLaunch.ContainerEnv(new LaunchOptions { RuntimeBindings = true, Args = new[] { Switch } })["CC_EXTRA_ARGS"]);
        Assert.Equal($"{Stock} {Switch}",
            DockerLaunch.ContainerEnv(new LaunchOptions { RuntimeBindings = true, StockRuntime = true })["CC_EXTRA_ARGS"]);
        _sb.Env(EnvName, "1");
        Assert.Equal(Switch, DockerLaunch.ContainerEnv(new LaunchOptions())["CC_EXTRA_ARGS"]);
        Assert.False(DockerLaunch.ContainerEnv(new LaunchOptions { RuntimeBindings = false }).ContainsKey("CC_EXTRA_ARGS"));   // the option wins
    }

    // ── cloud ────────────────────────────────────────────────────────────────

    [Fact]
    public async Task A_cloud_launch_does_not_take_it_and_says_so_once()
    {
        await using var api = new FakeCloud();
        _sb.Env("CLEARCOTE_API_KEY", FakeCloud.ApiKey).Env("CLEARCOTE_API_URL", api.Url);
        CloudLaunch.ConnectOverride = (_, _) => Task.FromResult(CloudTests.FakeBrowser.Make().Browser);
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, RuntimeBindings = true, Country = "us" })).CloseAsync();
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, RuntimeBindings = true })).CloseAsync();
        await Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "acct-1", RuntimeBindings = true });
        var bodies = api.Requests("POST", "/api/v1/browsers").Select(r => (JsonObject)r.Body!).ToList();
        Assert.Equal(3, bodies.Count);
        Assert.DoesNotContain(bodies.SelectMany(b => b.Select(p => p.Key)),
            k => k.Contains("runtime", StringComparison.OrdinalIgnoreCase) || k.Contains("binding", StringComparison.OrdinalIgnoreCase));
        Assert.Equal(1, Said(CloudOnlyLocal));
        Assert.StartsWith("clearcote: warning: RuntimeBindings", Line(CloudOnlyLocal));
        // the environment variable too
        LaunchWarnings.ForgetSaid();
        _sb.Env(EnvName, "1");
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true })).CloseAsync();
        Assert.Equal(2, Said(CloudOnlyLocal));
        LaunchWarnings.ForgetSaid();
        await (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Quiet = true, RuntimeBindings = true })).CloseAsync();   // quiet: not said
        Assert.Equal(2, Said(CloudOnlyLocal));
    }

    // ── the engine note about bindings ───────────────────────────────────────

    [Fact]
    public async Task The_bindings_note_names_the_option_and_is_not_said_when_bindings_are_restored()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = Standin(EngineR32, EngineR54);
        await Launch("persistent", exe, o => { o.RuntimeBindings = true; o.Quiet = false; });
        var err = TakeErr();
        Assert.DoesNotContain(BindingsNote, err);
        Assert.Contains(ConsoleNote, err);   // console events stay suppressed
        await Launch("persistent", exe, o => { o.StockRuntime = true; o.Quiet = false; });
        Assert.DoesNotContain(BindingsNote, TakeErr());
        await Launch("persistent", exe, o => o.Quiet = false);
        var note = TakeErr().Split('\n').Where(l => l.Contains(BindingsNote, StringComparison.Ordinal)).ToList();
        Assert.Single(note);
        Assert.StartsWith("clearcote: note: ", note[0]);
        Assert.Contains("RuntimeBindings = true", note[0]);
        Assert.Contains("r54", note[0]);
    }

    [Fact]
    public void EmitCoherence_leaves_the_bindings_note_out_for_either_switch()
    {
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false, runtimeBindings: true);
        Assert.DoesNotContain(BindingsNote, _err.ToString());
        Assert.Contains(ConsoleNote, _err.ToString());
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false, stockRuntime: true);
        Assert.DoesNotContain(BindingsNote, _err.ToString());
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false);
        LaunchWarnings.EmitCoherence(Array.Empty<LaunchWarnings.Warning>(), quiet: false);
        Assert.Equal(1, Said(BindingsNote));   // once per process
    }
}
