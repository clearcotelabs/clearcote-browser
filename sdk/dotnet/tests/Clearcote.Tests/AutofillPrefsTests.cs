using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Autofill on, as in Chrome (<see cref="AutofillPrefs"/>): <c>autofill.profile_enabled</c> /
/// <c>credit_card_enabled</c> go into a profile's Default/Preferences only where they are missing, before every
/// launch on a profile directory. Mirrors sdk/node/test/autofillpref.test.ts (+ the autofill block of
/// launch-entrypoints.test.ts) and sdk/python/tests/test_autofillpref.py.
/// </summary>
/// <remarks>The launch-wiring tests run a stand-in engine (a shell script, so POSIX-only, as in CrlsetTests) that
/// copies the Preferences it finds next to itself. The live test needs CLEARCOTE_LIVE_ENGINE (a Clearcote build, and a
/// licence for a PRO one) and Node 22+ on PATH (it reads Web Data with Node's built-in SQLite).</remarks>
public class AutofillPrefsTests : IDisposable
{
    private readonly string _dir = TestTemp.Create("cc-autofill-");

    public void Dispose() => TestTemp.Remove(_dir);

    private string File_(string udd) => Path.Combine(udd, "Default", "Preferences");

    private void Write(string udd, string text)
    {
        Directory.CreateDirectory(Path.Combine(udd, "Default"));
        File.WriteAllText(File_(udd), text);
    }

    private JsonNode Read(string udd) => JsonNode.Parse(File.ReadAllText(File_(udd)))!;

    [Fact]
    public void A_new_profile_gets_both_on()
    {
        var udd = Path.Combine(_dir, "not-yet");
        Assert.Equal(new[] { "profile_enabled", "credit_card_enabled" }, AutofillPrefs.Seed(udd));
        Assert.Equal("""{"autofill":{"profile_enabled":true,"credit_card_enabled":true}}""", Read(udd).ToJsonString());
    }

    [Fact]
    public void Keeps_an_explicit_off_and_every_other_preference()
    {
        Write(_dir, """{"autofill":{"profile_enabled":false,"other":1},"browser":{"x":2},"name":"Zoë"}""");
        Assert.Equal(new[] { "credit_card_enabled" }, AutofillPrefs.Seed(_dir));
        var p = Read(_dir);
        Assert.False(p["autofill"]!["profile_enabled"]!.GetValue<bool>());
        Assert.True(p["autofill"]!["credit_card_enabled"]!.GetValue<bool>());
        Assert.Equal(1, p["autofill"]!["other"]!.GetValue<int>());
        Assert.Equal(2, p["browser"]!["x"]!.GetValue<int>());
        Assert.Equal("Zoë", p["name"]!.GetValue<string>());
        Assert.Contains("Zoë", File.ReadAllText(File_(_dir)));   // written as text, not \u escapes
    }

    [Fact]
    public void Does_nothing_when_both_are_set()
    {
        const string text = "{\n \"autofill\": {\"profile_enabled\": false, \"credit_card_enabled\": false}\n}";
        Write(_dir, text);
        Assert.Empty(AutofillPrefs.Seed(_dir));
        Assert.Equal(text, File.ReadAllText(File_(_dir)));
    }

    [Theory]
    [InlineData("{not json")]
    [InlineData("[1,2]")]
    [InlineData("null")]
    [InlineData("")]
    public void Never_replaces_a_file_it_cannot_read(string bad)
    {
        Write(_dir, bad);
        Assert.Empty(AutofillPrefs.Seed(_dir));
        Assert.Equal(bad, File.ReadAllText(File_(_dir)));
    }

    [Fact]
    public void Apply_never_throws()
    {
        var notADir = Path.Combine(_dir, "file");
        File.WriteAllText(notADir, "x");
        AutofillPrefs.Apply(notADir, quiet: true);
        Assert.False(Directory.Exists(Path.Combine(notADir, "Default")));
    }

    // ── launch wiring (POSIX: a shell-script stand-in engine) ──────────────────────────────────────

    /// A stand-in engine: copies the profile's Preferences (as the browser would find them at start) next to
    /// itself, writes its arguments there, then exits (the launch then fails).
    private string Standin()
    {
        var exe = Path.Combine(TestTemp.Create("cc-autofill-standin-"), "chrome");
        File.WriteAllText(exe, "#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\n" +
            "for a in \"$@\"; do case \"$a\" in --user-data-dir=*) udd=\"${a#--user-data-dir=}\" ;; esac; done\n" +
            "[ -n \"$udd\" ] && cp \"$udd/Default/Preferences\" \"$d/prefs\" 2>/dev/null\nexit 1\n");
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private static Sandbox Isolated() => new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_CLOUD", null)
        .Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null)
        .Env("CLEARCOTE_WIDEVINE", null).Env("CLEARCOTE_OFFLINE_KEY", null).Env("CLEARCOTE_NO_WARN", "1");

    [Fact]
    public async Task A_persistent_launch_seeds_the_profile_before_the_browser_starts()
    {
        if (OperatingSystem.IsWindows()) return;
        using var sb = Isolated();
        sb.TempHome();
        var exe = Standin();
        try
        {
            var udd = Path.Combine(_dir, "udd");
            Write(udd, """{"autofill":{"profile_enabled":false},"browser":{"x":1}}""");
            await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(udd, new LaunchOptions
            {
                ExecutablePath = exe, Headless = true, Quiet = true, Timeout = 20000,
            }));
            var seen = Path.Combine(Path.GetDirectoryName(exe)!, "prefs");
            Assert.True(File.Exists(seen), "the stand-in engine never ran, or found no Preferences");
            var p = JsonNode.Parse(File.ReadAllText(seen))!;
            Assert.False(p["autofill"]!["profile_enabled"]!.GetValue<bool>());   // the profile's own off is kept
            Assert.True(p["autofill"]!["credit_card_enabled"]!.GetValue<bool>());
            Assert.Equal(1, p["browser"]!["x"]!.GetValue<int>());
        }
        finally { TestTemp.Remove(Path.GetDirectoryName(exe)!); }
    }

    [Fact]
    public async Task Serve_seeds_the_profile_before_the_browser_starts()
    {
        if (OperatingSystem.IsWindows()) return;
        using var sb = Isolated();
        sb.TempHome();
        var exe = Standin();
        try
        {
            var udd = Path.Combine(_dir, "served");
            await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync(new ServeOptions
            {
                ExecutablePath = exe, UserDataDir = udd, Quiet = true, ReadyTimeoutMs = 5000,
            }));
            var seen = Path.Combine(Path.GetDirectoryName(exe)!, "prefs");
            Assert.True(File.Exists(seen), "the stand-in engine never ran, or found no Preferences");
            var p = JsonNode.Parse(File.ReadAllText(seen))!;
            Assert.True(p["autofill"]!["profile_enabled"]!.GetValue<bool>());
            Assert.True(p["autofill"]!["credit_card_enabled"]!.GetValue<bool>());
        }
        finally { TestTemp.Remove(Path.GetDirectoryName(exe)!); }
    }

    // ── live ──────────────────────────────────────────────────────────────────────────────────────

    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");

    private const string Form = """
        <!doctype html><form method="post" action="/done">
        <input name="name" autocomplete="name"><input name="email" type="email" autocomplete="email">
        <input name="city" autocomplete="address-level2"><button>Send</button></form>
        """;

    /// A local page: the form at /, "ok" at /done (GET or POST).
    private sealed class FormSite : IDisposable
    {
        private readonly TcpListener _l = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new();
        public string Url { get; }

        public FormSite()
        {
            _l.Start();
            Url = $"http://127.0.0.1:{((IPEndPoint)_l.LocalEndpoint).Port}/";
            _ = Task.Run(async () =>
            {
                while (!_stop.IsCancellationRequested)
                {
                    TcpClient c;
                    try { c = await _l.AcceptTcpClientAsync(_stop.Token); }
                    catch (Exception) { return; }
                    _ = Task.Run(() => AnswerAsync(c));
                }
            });
        }

        private static async Task AnswerAsync(TcpClient c)
        {
            using var _ = c;
            try
            {
                var s = c.GetStream();
                var buf = new byte[65536];
                var head = new StringBuilder();
                int n;
                while ((n = await s.ReadAsync(buf)) > 0)
                {
                    head.Append(Encoding.ASCII.GetString(buf, 0, n));
                    var end = head.ToString().IndexOf("\r\n\r\n", StringComparison.Ordinal);
                    if (end < 0) continue;
                    var h = head.ToString();
                    var len = 0;
                    foreach (var line in h[..end].Split("\r\n"))
                        if (line.StartsWith("content-length:", StringComparison.OrdinalIgnoreCase)) len = int.Parse(line[15..].Trim());
                    var have = h.Length - end - 4;
                    while (have < len && (n = await s.ReadAsync(buf)) > 0) have += n;
                    var path = h.Split(' ')[1];
                    var body = Encoding.UTF8.GetBytes(path == "/done" ? "<p>ok</p>" : Form);
                    var resp = Encoding.ASCII.GetBytes($"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\ncontent-length: {body.Length}\r\nconnection: close\r\n\r\n");
                    await s.WriteAsync(resp);
                    await s.WriteAsync(body);
                    return;
                }
            }
            catch (Exception) { }
        }

        public void Dispose()
        {
            _stop.Cancel();
            _l.Stop();
        }
    }

    /// Rows in the autofill table of Web Data, read with Node's built-in SQLite (Node 22+: behind a flag).
    private static int AutofillRows(string webData)
    {
        var psi = new ProcessStartInfo("node") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        foreach (var a in new[] { "--experimental-sqlite", "--no-warnings", "-e",
                     "const {DatabaseSync}=require('node:sqlite');const db=new DatabaseSync(process.argv[1],{readOnly:true});console.log(db.prepare('select count(*) n from autofill').get().n)",
                     webData })
            psi.ArgumentList.Add(a);
        using var p = Process.Start(psi)!;
        var output = p.StandardOutput.ReadToEnd();
        var err = p.StandardError.ReadToEnd();
        p.WaitForExit();
        Assert.True(p.ExitCode == 0, $"node could not read {webData}: {err}");
        return int.Parse(output.Trim());
    }

    private async Task<int> SubmitAndCount(string udd)
    {
        using var site = new FormSite();
        var context = await Clearcote.LaunchPersistentContextAsync(udd, new LaunchOptions
        {
            ExecutablePath = LiveExe, Headless = true, Quiet = true, Geoip = false, Fingerprint = "autofill-live-1",
        });
        try
        {
            var page = context.Pages.Count > 0 ? context.Pages[0] : await context.NewPageAsync();
            await page.GotoAsync(site.Url);
            await page.FillAsync("[name=\"name\"]", "Ada Lovelace");
            await page.FillAsync("[name=\"email\"]", "ada@example.com");
            await page.FillAsync("[name=\"city\"]", "London");
            await page.RunAndWaitForNavigationAsync(() => page.ClickAsync("button"), new() { Url = "**/done" });
            await page.WaitForTimeoutAsync(1500);
        }
        finally
        {
            await context.CloseAsync();   // graceful: Chrome writes Web Data on the way out
        }
        return AutofillRows(Path.Combine(udd, "Default", "Web Data"));
    }

    [Fact]
    public async Task Live_a_submitted_form_is_remembered_and_an_explicit_off_is_kept()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var on = Path.Combine(_dir, "on");
        var rowsOn = await SubmitAndCount(on);
        Console.WriteLine($"autofill rows (seeded on): {rowsOn}");
        Assert.True(Read(on)["autofill"]!["profile_enabled"]!.GetValue<bool>());
        Assert.True(rowsOn > 0, $"autofill rows: {rowsOn}");

        var off = Path.Combine(_dir, "off");
        Write(off, """{"autofill":{"profile_enabled":false}}""");
        var rowsOff = await SubmitAndCount(off);
        Console.WriteLine($"autofill rows (explicit off): {rowsOff}");
        Assert.False(Read(off)["autofill"]!["profile_enabled"]!.GetValue<bool>());
        Assert.Equal(0, rowsOff);
    }
}
