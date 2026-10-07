using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// r32: leave prefers-color-scheme to the engine's persona (Playwright emulates light by default).
public class ColorSchemeDefaultTests : IDisposable
{
    private readonly string _dir = Path.Combine(Path.GetTempPath(), "cc-colorscheme-" + Guid.NewGuid().ToString("N")[..8]);

    public ColorSchemeDefaultTests() => Directory.CreateDirectory(_dir);

    public void Dispose()
    {
        try { Directory.Delete(_dir, recursive: true); } catch (IOException) { }
    }

    private string Engine(bool r32)
    {
        var exe = Path.Combine(_dir, r32 ? "chrome-r32" : "chrome-r31");
        var body = "\0fingerprint-platform\0" + (r32 ? "\0fingerprint-color-scheme\0" : "");
        File.WriteAllBytes(exe, System.Text.Encoding.Latin1.GetBytes(body));
        return exe;
    }

    [Fact]
    public void An_r32_engine_gets_no_emulation_by_default()
        => Assert.Equal(ColorScheme.Null, ColorSchemeDefault.Resolve(null, Engine(r32: true)));

    [Fact]
    public void An_older_engine_keeps_Playwrights_default()
        => Assert.Null(ColorSchemeDefault.Resolve(null, Engine(r32: false)));

    [Fact]
    public void The_callers_choice_wins()
    {
        Assert.Equal(ColorScheme.Dark, ColorSchemeDefault.Resolve(ColorScheme.Dark, Engine(r32: true)));
        Assert.Equal(ColorScheme.Light, ColorSchemeDefault.Resolve(ColorScheme.Light, Engine(r32: false)));
    }

    [Fact]
    public void No_engine_path_means_no_default()
        => Assert.Null(ColorSchemeDefault.Resolve(null, null));

    [Fact]
    public void The_switch_name_is_the_engines()
        => Assert.Equal("fingerprint-color-scheme", ColorSchemeDefault.Switch);
}
