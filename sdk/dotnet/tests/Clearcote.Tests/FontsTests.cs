using System.Buffers.Binary;
using System.Text;
using Xunit;

namespace Clearcote.Tests;

// Linux font wiring: the bundled clones, your own fonts (FontDirs / CLEARCOTE_FONT_DIRS) and fallback fonts
// (CLEARCOTE_FALLBACK_FONT_DIRS). Mirrors sdk/python/tests/test_font_dirs.py and sdk/node/test/font-dirs.test.ts.
public class FontsTests : IDisposable
{
    private readonly string _root = TestTemp.Create("ccfonts-");
    private readonly string? _savedDirs = Environment.GetEnvironmentVariable(Fonts.FontDirsEnv);
    private readonly string? _savedFallback = Environment.GetEnvironmentVariable(Fonts.FallbackFontDirsEnv);
    // LinuxFontConfig points fontconfig at <tmp>/cc-fc-cache, shared by every launch on the machine: remove
    // it afterwards only when this test created it, never a real one already in use.
    private readonly bool _cacheExisted = Directory.Exists(Cache);

    public FontsTests()
    {
        Environment.SetEnvironmentVariable(Fonts.FontDirsEnv, null);
        Environment.SetEnvironmentVariable(Fonts.FallbackFontDirsEnv, null);
    }

    public void Dispose()
    {
        Environment.SetEnvironmentVariable(Fonts.FontDirsEnv, _savedDirs);
        Environment.SetEnvironmentVariable(Fonts.FallbackFontDirsEnv, _savedFallback);
        TestTemp.Remove(_root);
        if (!_cacheExisted) TestTemp.Remove(Cache);
    }

    private static readonly string Assets = FindAssets();

    private static string FindAssets()
    {
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d is not null; d = d.Parent)
        {
            var candidate = Path.Join(d.FullName, "assets", "fonts");
            if (File.Exists(Path.Join(candidate, "fonts.conf.template"))) return candidate;
        }
        throw new InvalidOperationException("assets/fonts not found above the test binary");
    }

    private static string Template => File.ReadAllText(Path.Join(Assets, "fonts.conf.template"));
    private static readonly string Cache = Path.Join(Path.GetTempPath().TrimEnd(Path.DirectorySeparatorChar), "cc-fc-cache");

    private string Sub(string name) => Directory.CreateDirectory(Path.Join(_root, name)).FullName;

    /// A release directory: fonts/ with the real template, and a stand-in engine binary that carries the
    /// genuine-faces marker switch only when asked to (PRO r32+ does; older engines do not).
    private (string Exe, string Fonts) Bundle(bool genuineFaces = false, string name = "bin")
    {
        var fonts = Directory.CreateDirectory(Path.Join(_root, name, "fonts")).FullName;
        File.WriteAllText(Path.Join(fonts, "fonts.conf.template"), Template);
        var exe = Path.Join(_root, name, "chrome");
        var marker = genuineFaces ? $"\0{Fonts.GenuineFacesSwitch}\0" : "";
        File.WriteAllBytes(exe, Encoding.Latin1.GetBytes($"ELF\0proxy-auth\0{marker}"));
        return (exe, fonts);
    }

    // --- tiny synthetic fonts: a name table, a cmap (format 4 + 12) and optionally a colour table -------

    private static byte[] Be16(params int[] v) { var b = new byte[2 * v.Length]; for (var i = 0; i < v.Length; i++) BinaryPrimitives.WriteUInt16BigEndian(b.AsSpan(2 * i), (ushort)(v[i] & 0xFFFF)); return b; }
    private static byte[] Be32(params long[] v) { var b = new byte[4 * v.Length]; for (var i = 0; i < v.Length; i++) BinaryPrimitives.WriteUInt32BigEndian(b.AsSpan(4 * i), (uint)v[i]); return b; }
    private static byte[] Cat(params byte[][] parts) => parts.SelectMany(p => p).ToArray();

    private static byte[] NameTable(string[] families)
    {
        var records = new List<byte[]>();
        var strings = Array.Empty<byte>();
        for (var i = 0; i < Math.Min(2, families.Length); i++)
        {
            var raw = Encoding.BigEndianUnicode.GetBytes(families[i]);
            records.Add(Be16(3, 1, 0x409, i == 0 ? 1 : 16, raw.Length, strings.Length));
            strings = Cat(strings, raw);
        }
        return Cat(Be16(0, records.Count, 6 + 12 * records.Count), Cat(records.ToArray()), strings);
    }

    private static byte[] Cmap4(int[] cps, int[] viaGlyphArray)
    {
        var segs = cps.Where(c => c <= 0xFFFF).OrderBy(c => c).Select((c, i) => (C: c, G: i + 1)).Append((C: 0xFFFF, G: 0)).ToList();
        var n = segs.Count;
        var ends = Be16(segs.Select(s => s.C).ToArray());
        var deltas = new List<int>(); var offsets = new List<int>(); var glyphs = new List<int>();
        for (var i = 0; i < n; i++)
        {
            var (c, g) = segs[i];
            if (viaGlyphArray.Contains(c)) { deltas.Add(0); offsets.Add(2 * (n - i) + 2 * glyphs.Count); glyphs.Add(g); }
            else { deltas.Add(g != 0 ? (g - c) & 0xFFFF : 1); offsets.Add(0); }
        }
        var rest = Cat(Be16(n * 2, 0, 0, 0), ends, Be16(0), ends, Be16(deltas.ToArray()), Be16(offsets.ToArray()), Be16(glyphs.ToArray()));
        return Cat(Be16(4, 6 + rest.Length, 0), rest);
    }

    private static byte[] Cmap12(int[] cps)
    {
        var groups = Cat(cps.OrderBy(c => c).Select((c, i) => Be32(c, c, i + 1)).ToArray());
        return Cat(Be16(12, 0), Be32(16 + groups.Length, 0, cps.Length), groups);
    }

    private static byte[] Cmap(int[] cps, int[] viaGlyphArray)
    {
        var subs = new List<(int P, int E, byte[] Data)> { (3, 1, Cmap4(cps, viaGlyphArray)) };
        if (cps.Any(c => c > 0xFFFF)) subs.Add((3, 10, Cmap12(cps)));
        var off = 4 + 8 * subs.Count;
        var recs = new List<byte[]>(); var data = Array.Empty<byte>();
        foreach (var (p, e, d) in subs) { recs.Add(Cat(Be16(p, e), Be32(off + data.Length))); data = Cat(data, d); }
        return Cat(Be16(0, subs.Count), Cat(recs.ToArray()), data);
    }

    private static SortedDictionary<string, byte[]> TablesFor(string[] families, int[] cps, bool color, int[] viaGlyphArray)
    {
        var t = new SortedDictionary<string, byte[]>(StringComparer.Ordinal) { ["name"] = NameTable(families) };
        if (cps.Length > 0) t["cmap"] = Cmap(cps, viaGlyphArray);
        if (color) t["COLR"] = new byte[14];
        return t;
    }

    private static byte[] Sfnt(SortedDictionary<string, byte[]> tables, int @base = 0)
    {
        var head = Cat(Be32(0x00010000), Be16(tables.Count, 0, 0, 0));
        var off = @base + 12 + 16 * tables.Count;
        var recs = new List<byte[]>(); var data = new List<byte[]>();
        foreach (var (tag, blob) in tables)
        {
            recs.Add(Cat(Encoding.ASCII.GetBytes(tag), Be32(0, off, blob.Length)));
            var padded = Cat(blob, new byte[(4 - blob.Length % 4) % 4]);
            data.Add(padded);
            off += padded.Length;
        }
        return Cat(head, Cat(recs.ToArray()), Cat(data.ToArray()));
    }

    private static string MakeFont(string path, string[] families, int[]? cps = null, bool color = false, int[]? viaGlyphArray = null)
    {
        File.WriteAllBytes(path, Sfnt(TablesFor(families, cps ?? Array.Empty<int>(), color, viaGlyphArray ?? Array.Empty<int>())));
        return path;
    }

    private static string MakeCollection(string path, params (string[] Families, int[] Cps)[] faces)
    {
        var pos = 12 + 4 * faces.Length;
        var blobs = new List<byte[]>(); var offsets = new List<long>();
        foreach (var (families, cps) in faces)
        {
            var blob = Sfnt(TablesFor(families, cps, false, Array.Empty<int>()), pos);
            offsets.Add(pos); blobs.Add(blob); pos += blob.Length;
        }
        File.WriteAllBytes(path, Cat(Encoding.ASCII.GetBytes("ttcf"), Be32(0x00010000, faces.Length), Be32(offsets.ToArray()), Cat(blobs.ToArray())));
        return path;
    }

    // --- reading font files -------------------------------------------------------------------------

    [Fact]
    public void Reads_family_names_and_coverage_through_format_4_and_12()
    {
        var f = MakeFont(Path.Join(_root, "a.ttf"), new[] { "Segoe UI Semibold", "Segoe UI" }, new[] { 0x41, 0x995, 0x1F600 });
        var (families, covered, color) = Fonts.ReadFont(f, new[] { 0x41, 0x995, 0x1780, 0x1F600 });
        Assert.Equal(new[] { "segoe ui", "segoe ui semibold" }, families.OrderBy(x => x, StringComparer.Ordinal));
        Assert.Equal(new[] { 0x41, 0x995, 0x1F600 }, covered.OrderBy(x => x));
        Assert.False(color);
    }

    [Fact]
    public void Glyph_array_segments_count_and_glyph_zero_does_not()
    {
        var f = MakeFont(Path.Join(_root, "b.ttf"), new[] { "X" }, new[] { 0x41, 0x42, 0x995 }, viaGlyphArray: new[] { 0x42, 0x995 });
        Assert.Equal(new[] { 0x41, 0x42, 0x995 }, Fonts.ReadFont(f, new[] { 0x41, 0x42, 0x995, 0x43, 0xFFFF }).Covered.OrderBy(x => x));
    }

    [Fact]
    public void Colour_tables_and_collections()
    {
        var emoji = Fonts.ReadFont(MakeFont(Path.Join(_root, "e.ttf"), new[] { "Noto Color Emoji" }, new[] { 0x1F600 }, color: true), new[] { 0x1F600 });
        Assert.True(emoji.Color && emoji.Covered.Contains(0x1F600) && emoji.Families.Contains("noto color emoji"));
        var ttc = Fonts.ReadFont(MakeCollection(Path.Join(_root, "c.ttc"),
            (new[] { "WenQuanYi Zen Hei" }, new[] { 0x4E2D }), (new[] { "WenQuanYi Zen Hei Mono" }, new[] { 0xAC00 })), new[] { 0x4E2D, 0xAC00 });
        Assert.Equal(new[] { "wenquanyi zen hei", "wenquanyi zen hei mono" }, ttc.Families.OrderBy(x => x, StringComparer.Ordinal));
        Assert.Equal(2, ttc.Covered.Count);
    }

    [Theory]
    [InlineData("Arimo-Regular.ttf", "arimo", new[] { 0x41, 0x05D0 }, new[] { 0x0995 })]
    [InlineData("DejaVuSans.ttf", "dejavu sans", new[] { 0x0531, 0x0628, 0x0E81 }, new[] { 0x0995, 0x4E2D })]
    [InlineData("Inconsolata.otf", "inconsolata", new[] { 0x41 }, new[] { 0x0628 })]
    public void Reads_the_bundled_fonts_like_any_font(string name, string family, int[] has, int[] lacks)
    {
        var (families, covered, color) = Fonts.ReadFont(Path.Join(Assets, name), has.Concat(lacks).ToArray());
        Assert.Contains(family, families);
        Assert.Equal(has.OrderBy(x => x), covered.OrderBy(x => x));
        Assert.False(color);
    }

    [Fact]
    public void Ignores_what_is_not_a_font()
    {
        File.WriteAllText(Path.Join(_root, "junk.ttf"), "not a font at all");
        File.WriteAllBytes(Path.Join(_root, "empty.otf"), Array.Empty<byte>());
        File.WriteAllBytes(Path.Join(_root, "short.ttc"), Encoding.Latin1.GetBytes("ttcf\0\x01\0\0\0\0\0\x05\0\0\0\x40"));
        foreach (var n in new[] { "junk.ttf", "empty.otf", "short.ttc" })
        {
            var r = Fonts.ReadFont(Path.Join(_root, n), new[] { 0x41 });
            Assert.Empty(r.Families);
            Assert.Empty(r.Covered);
            Assert.False(r.Color);
        }
    }

    [Fact]
    public void Walks_subdirectories()
    {
        MakeFont(Path.Join(Sub("sub"), "x.TTF"), new[] { "X" });
        MakeFont(Path.Join(_root, "y.otf"), new[] { "Y" });
        File.WriteAllText(Path.Join(_root, "readme.txt"), "no");
        Assert.Equal(new[] { "x.TTF", "y.otf" }, Fonts.FontFiles(new[] { _root }).Select(Path.GetFileName));
    }

    // --- which directories ----------------------------------------------------------------------------

    [Fact]
    public void Option_first_then_the_variable_no_duplicates_and_fallback_keeps_out_your_own()
    {
        string a = Sub("a"), b = Sub("b"), c = Sub("c"), gone = Path.Join(_root, "gone");
        var env = new Dictionary<string, string>
        {
            [Fonts.FontDirsEnv] = string.Join(Path.PathSeparator, b, a, gone),
            [Fonts.FallbackFontDirsEnv] = string.Join(Path.PathSeparator, c, a, ""),
        };
        var (user, fallback, ignored) = Fonts.ResolveFontDirs(new[] { a }, k => env.GetValueOrDefault(k), isLinux: true);
        Assert.Equal(new[] { a, b }, user);
        Assert.Equal(new[] { c }, fallback);
        Assert.Equal(new[] { gone }, ignored);
    }

    [Fact]
    public void Checks_the_option()
    {
        var typo = Path.Join(_root, "typo");
        Assert.Throws<ArgumentException>(() => Fonts.CheckFontDirs(new[] { typo }, isLinux: true));
        Assert.Throws<ArgumentException>(() => Fonts.CheckFontDirs(new[] { "" }, isLinux: true));
        Assert.Equal(new[] { _root }, Fonts.CheckFontDirs(new[] { _root }, isLinux: true));
        Assert.Equal(new[] { typo }, Fonts.CheckFontDirs(new[] { typo }, isLinux: false));  // Windows has its own fonts
        Assert.Empty(Fonts.CheckFontDirs(null, isLinux: true));
    }

    // --- the generated fontconfig file ------------------------------------------------------------------

    [Fact]
    public void No_genuine_family_leaves_the_template_alone()
    {
        Assert.Equal(Template, Fonts.GenuineRules(Template, new HashSet<string>()));
        Assert.Equal(Template, Fonts.GenuineRules(Template, new HashSet<string> { "some other family" }));
    }

    [Fact]
    public void Drops_the_lookalike_and_retargets_aliases_and_generics()
    {
        var output = Fonts.GenuineRules(Template, new HashSet<string> { "arial", "segoe ui", "consolas" });
        foreach (var f in new[] { "Arial", "Segoe UI", "Consolas" }) Assert.DoesNotContain($"<string>{f}</string></test>", output);
        Assert.Contains("<test name=\"family\"><string>Helvetica</string></test><edit name=\"family\" mode=\"assign\" binding=\"strong\"><string>Arial</string></edit>", output);
        Assert.Contains("<alias><family>sans-serif</family><prefer><family>Arial</family></prefer></alias>", output);
        Assert.Contains("<alias><family>system-ui</family><prefer><family>Segoe UI</family></prefer></alias>", output);
        Assert.Contains("<alias><family>monospace</family><prefer><family>Consolas</family></prefer></alias>", output);
        Assert.Contains("<alias><family>serif</family><prefer><family>Tinos</family></prefer></alias>", output);
        Assert.Contains("<test name=\"family\"><string>Calibri</string></test><edit name=\"family\" mode=\"assign\" binding=\"strong\"><string>Carlito</string></edit>", output);
        Assert.Contains("<test name=\"family\"><string>Times</string></test><edit name=\"family\" mode=\"assign\" binding=\"strong\"><string>Tinos</string></edit>", output);
        var changed = Template.Split('\n').Except(output.Split('\n')).ToList();
        Assert.NotEmpty(changed);
        Assert.All(changed, line => Assert.True(line.Contains("<test name=\"family\">") || line.Contains("<alias>"), line));
        Assert.Equal(Template.Split("<match target=\"font\">").Length, output.Split("<match target=\"font\">").Length);
    }

    [Fact]
    public void Follows_the_template_it_is_given()
    {
        var t = Template.Replace("<string>Inconsolata</string></edit>", "<string>Cousine</string></edit>");
        var output = Fonts.GenuineRules(t, new HashSet<string> { "trebuchet ms" });
        Assert.DoesNotContain("<string>Trebuchet MS</string></test>", output);
        Assert.Contains("<test name=\"family\"><string>Consolas</string></test><edit name=\"family\" mode=\"assign\" binding=\"strong\"><string>Cousine</string></edit>", output);
    }

    [Fact]
    public void Lists_your_fonts_first_and_fallback_last_and_plain_substitution_without_them()
    {
        var conf = Fonts.BuildConf(Template, "/opt/cc/fonts", "/tmp/c", new[] { "/home/me/win & fonts" }, new[] { "/usr/local/fb" });
        var dirs = conf.Split('\n').Select(l => l.Trim()).Where(l => l.StartsWith("<dir>", StringComparison.Ordinal)).ToArray();
        Assert.Equal(new[] { "<dir>/home/me/win &amp; fonts</dir>", "<dir>/opt/cc/fonts</dir>", "<dir>/usr/local/fb</dir>" }, dirs);
        Assert.DoesNotContain("@FONTS_DIR@", conf);
        Assert.Contains("<cachedir>/tmp/c</cachedir>", conf);
        Assert.Equal(Template.Replace("@FONTS_DIR@", "/f").Replace("@CACHE_DIR@", "/c"), Fonts.BuildConf(Template, "/f", "/c"));
    }

    [Fact]
    public void Without_extra_directories_the_file_is_what_python_and_node_write()
    {
        var (exe, fonts) = Bundle();
        var path = Fonts.LinuxFontConfig(exe, null, isLinux: true);
        Assert.Equal(Path.Join(fonts, "fonts.generated.conf"), path);
        Assert.Equal(Template.Replace("@FONTS_DIR@", fonts).Replace("@CACHE_DIR@", Cache), File.ReadAllText(path!));
        Assert.Equal(new[] { "fonts.conf.template", "fonts.generated.conf" },
            Directory.GetFiles(fonts).Select(Path.GetFileName).OrderBy(x => x, StringComparer.Ordinal));
        Assert.Null(Fonts.LinuxFontConfig(exe, null, isLinux: false));
        Assert.Null(Fonts.LinuxFontConfig(Path.Join(_root, "nobundle", "chrome"), null, isLinux: true));
    }

    [Fact]
    public void Your_windows_fonts_are_listed_first_and_lose_their_lookalike()
    {
        var (exe, fonts) = Bundle(genuineFaces: true);
        var win = Sub("winfonts");
        MakeFont(Path.Join(win, "arial.ttf"), new[] { "Arial" }, new[] { 0x41 });
        Environment.SetEnvironmentVariable(Fonts.FontDirsEnv, win);
        var path = Fonts.LinuxFontConfig(exe, null, isLinux: true)!;
        Assert.StartsWith("fonts.generated-", Path.GetFileName(path));
        Assert.Equal(fonts, Path.GetDirectoryName(path));
        var conf = File.ReadAllText(path);
        Assert.True(conf.IndexOf($"<dir>{win}</dir>", StringComparison.Ordinal) < conf.IndexOf($"<dir>{fonts}</dir>", StringComparison.Ordinal));
        Assert.DoesNotContain("<string>Arial</string></test>", conf);
        Assert.Contains("<string>Segoe UI</string></test>", conf);
        var other = Sub("other");
        var path2 = Fonts.LinuxFontConfig(exe, new[] { other }, isLinux: true)!;
        Assert.NotEqual(path, path2);
        Assert.True(File.Exists(path));
        var conf2 = File.ReadAllText(path2);
        Assert.True(conf2.IndexOf($"<dir>{other}</dir>", StringComparison.Ordinal) < conf2.IndexOf($"<dir>{win}</dir>", StringComparison.Ordinal));
        Assert.Equal(path, Fonts.LinuxFontConfig(exe, null, isLinux: true));
    }

    [Fact]
    public void An_engine_without_genuine_faces_keeps_the_rules()
    {
        // Measured on such an engine: "Arial" still renders through its substitute, so retargeting Helvetica
        // to the genuine Arial would give the two different widths. The directory is still listed first.
        var (exe, fonts) = Bundle(genuineFaces: false);
        var win = Sub("winfonts");
        MakeFont(Path.Join(win, "arial.ttf"), new[] { "Arial" }, new[] { 0x41 });
        var conf = File.ReadAllText(Fonts.LinuxFontConfig(exe, new[] { win }, isLinux: true)!);
        Assert.True(conf.IndexOf($"<dir>{win}</dir>", StringComparison.Ordinal) < conf.IndexOf($"<dir>{fonts}</dir>", StringComparison.Ordinal));
        Assert.Contains("<string>Arial</string></test>", conf);
        Assert.Contains("<alias><family>sans-serif</family><prefer><family>Arimo</family></prefer></alias>", conf);
        Assert.Contains("<test name=\"family\"><string>Helvetica</string></test><edit name=\"family\" mode=\"assign\" binding=\"strong\"><string>Arimo</string></edit>", conf);
        Assert.False(Fonts.EngineHonoursGenuineFaces(exe));
        Assert.True(Fonts.EngineHonoursGenuineFaces(Bundle(genuineFaces: true, name: "r32").Exe));
    }

    [Fact]
    public void Fallback_fonts_come_after_the_bundle_and_change_no_rule()
    {
        var (exe, fonts) = Bundle();
        var fb = Sub("fallback");
        MakeFont(Path.Join(fb, "lookalike.ttf"), new[] { "Arial" }, new[] { 0x41 });
        Environment.SetEnvironmentVariable(Fonts.FallbackFontDirsEnv, fb);
        var conf = File.ReadAllText(Fonts.LinuxFontConfig(exe, null, isLinux: true)!);
        Assert.True(conf.IndexOf($"<dir>{fonts}</dir>", StringComparison.Ordinal) < conf.IndexOf($"<dir>{fb}</dir>", StringComparison.Ordinal));
        Assert.Contains("<string>Arial</string></test>", conf);
    }

    [Fact]
    public void ApplyLinuxFonts_sets_the_variable_but_the_callers_own_wins()
    {
        var (exe, fonts) = Bundle();
        var env = Fonts.ApplyLinuxFonts(exe, null, null, null, isLinux: true)!;
        Assert.Equal(Path.Join(fonts, "fonts.generated.conf"), env["FONTCONFIG_FILE"]);
        Assert.True(env.Count > 1);  // the parent environment comes along: Playwright replaces the child's
        var caller = new Dictionary<string, string> { ["FONTCONFIG_FILE"] = "/mine.conf" };
        Assert.Same(caller, Fonts.ApplyLinuxFonts(exe, null, caller, caller, isLinux: true));
        var withLang = new Dictionary<string, string> { ["LANGUAGE"] = "de" };
        var merged = Fonts.ApplyLinuxFonts(exe, null, withLang, null, isLinux: true)!;
        Assert.Equal("de", merged["LANGUAGE"]);
        Assert.Equal(Path.Join(fonts, "fonts.generated.conf"), merged["FONTCONFIG_FILE"]);
        Assert.Null(Fonts.ApplyLinuxFonts(exe, null, null, null, isLinux: false));
    }

    [Fact]
    public void FontDirs_is_a_local_only_option()
    {
        Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == nameof(LaunchOptions.FontDirs));
    }
}
