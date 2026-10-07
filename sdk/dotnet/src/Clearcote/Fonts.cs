using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace Clearcote;

/// <summary>
/// Linux font wiring (the same as the Python SDK's <c>_fonts.py</c> and the Node SDK's <c>fonts.ts</c>).
/// </summary>
/// <remarks>
/// <para>The Linux release bundles metric-compatible font clones (Segoe UI->Selawik, Arial->Arimo, Times New
/// Roman->Tinos, ...) under <c>&lt;binDir&gt;/fonts/</c> with a self-contained <c>fonts.conf.template</c>. On a
/// bare server or container the Windows families (and even the standard fontconfig metric-alias rules) are
/// absent, so a page asking for "Segoe UI" collapses to a single default: a detectable render and an
/// absent-font tell. At launch the template is filled in and FONTCONFIG_FILE points at the result, so the
/// clones resolve without the host's /etc/fonts. Nothing happens off Linux or for a build without
/// <c>fonts/</c>.</para>
/// <para>Because the template is self-contained, fonts installed on the host are invisible to the browser.
/// Two settings add directories to it: <see cref="LaunchOptions.FontDirs"/> / CLEARCOTE_FONT_DIRS (your own
/// fonts, typically a copy of a Windows machine's Fonts folder: listed ahead of the bundle; on an engine that
/// lets a genuine face win over its substitute, <see cref="GenuineFacesSwitch"/>, PRO r32+, every family
/// they provide is also used as itself instead of its lookalike, with Helvetica/Times/Courier and the CSS
/// generics following; an older engine keeps the rules, since it still draws listed families through its
/// own substitute), and
/// CLEARCOTE_FALLBACK_FONT_DIRS (fonts used only for characters nothing else covers: listed after the bundle,
/// no rule changes; the Docker image sets it). Both are lists separated by the path separator. Never point
/// either at all of /usr/share/fonts: the host's Latin families then compete with the clones.</para>
/// </remarks>
internal static class Fonts
{
    internal const string FontDirsEnv = "CLEARCOTE_FONT_DIRS";
    internal const string FallbackFontDirsEnv = "CLEARCOTE_FALLBACK_FONT_DIRS";

    /// Carried by an engine that lets a genuine Windows face installed here win over its substitute (PRO
    /// r32+). The SDK never passes it: its presence in the binary is the marker. Measured on an engine
    /// without it: with a genuine Arial in CLEARCOTE_FONT_DIRS and the rules changed, "Helvetica" reached
    /// the genuine file while "Arial" stayed on the engine's Arimo -- two widths no Windows machine has.
    internal const string GenuineFacesSwitch = "disable-genuine-font-faces";

    /// Whether the engine at <paramref name="exePath"/> lets a genuine face from your own fonts win.
    internal static bool EngineHonoursGenuineFaces(string exePath) => LaunchOpts.EngineSupportsSwitch(exePath, GenuineFacesSwitch);

    private static readonly string[] FontExts = { ".ttf", ".otf", ".ttc", ".otc" };

    /// The Windows family each non-Windows name in the template stands for (the registry's FontSubstitutes).
    private static readonly Dictionary<string, string> WindowsAliases = new(StringComparer.Ordinal)
    {
        ["helvetica"] = "Arial", ["times"] = "Times New Roman", ["courier"] = "Courier New",
    };

    /// What each CSS generic resolves to in Chrome on Windows (measured: see fonts.conf.template).
    private static readonly Dictionary<string, string> GenericWindows = new(StringComparer.Ordinal)
    {
        ["sans-serif"] = "Arial", ["serif"] = "Times New Roman", ["monospace"] = "Consolas", ["system-ui"] = "Segoe UI",
    };

    private static readonly string[] ColorTables = { "COLR", "CBDT", "sbix", "SVG " };

    // --- reading font files (just the name and cmap tables; no fontconfig needed) ---------------------

    private static byte[] ReadAt(FileStream fs, long offset, int length)
    {
        if (length <= 0 || offset < 0 || offset >= fs.Length) return Array.Empty<byte>();
        fs.Seek(offset, SeekOrigin.Begin);
        var buf = new byte[(int)Math.Min(length, fs.Length - offset)];
        var n = 0;
        while (n < buf.Length)
        {
            var got = fs.Read(buf, n, buf.Length - n);
            if (got == 0) break;
            n += got;
        }
        return n == buf.Length ? buf : buf[..n];
    }

    private static ushort U16(byte[] b, int at) => BinaryPrimitives.ReadUInt16BigEndian(b.AsSpan(at, 2));
    private static short I16(byte[] b, int at) => BinaryPrimitives.ReadInt16BigEndian(b.AsSpan(at, 2));
    private static uint U32(byte[] b, int at) => BinaryPrimitives.ReadUInt32BigEndian(b.AsSpan(at, 4));

    private static List<long> Faces(FileStream fs)
    {
        var head = ReadAt(fs, 0, 12);
        if (head.Length < 12) return new List<long>();
        if (Encoding.ASCII.GetString(head, 0, 4) == "ttcf")
        {
            var count = (int)Math.Min(U32(head, 8), 1024);
            var data = ReadAt(fs, 12, 4 * count);
            var faces = new List<long>();
            for (var i = 0; i + 4 <= data.Length; i += 4) faces.Add(U32(data, i));
            return faces;
        }
        return new List<long> { 0 };
    }

    private static Dictionary<string, (long Off, int Len)> Tables(FileStream fs, long offset)
    {
        var head = ReadAt(fs, offset, 12);
        var tables = new Dictionary<string, (long, int)>(StringComparer.Ordinal);
        if (head.Length < 12) return tables;
        var data = ReadAt(fs, offset + 12, 16 * U16(head, 4));
        for (var i = 0; i + 16 <= data.Length; i += 16)
            tables[Encoding.ASCII.GetString(data, i, 4)] = (U32(data, i + 8), (int)Math.Min(U32(data, i + 12), int.MaxValue));
        return tables;
    }

    private static HashSet<string> Families(FileStream fs, (long Off, int Len) table)
    {
        var data = ReadAt(fs, table.Off, table.Len);
        var names = new HashSet<string>(StringComparer.Ordinal);
        if (data.Length < 6) return names;
        int count = U16(data, 2), strings = U16(data, 4);
        for (var i = 0; i < count; i++)
        {
            var at = 6 + 12 * i;
            if (at + 12 > data.Length) break;
            int platform = U16(data, at), encoding = U16(data, at + 2), nameId = U16(data, at + 6);
            int n = U16(data, at + 8), o = U16(data, at + 10);
            if (nameId != 1 && nameId != 16) continue;
            if (strings + o + n > data.Length) continue;
            string text;
            if (platform is 0 or 3)
            {
                if (n % 2 != 0) continue;
                text = Encoding.BigEndianUnicode.GetString(data, strings + o, n);
            }
            else if (platform == 1 && encoding == 0)
            {
                text = Encoding.Latin1.GetString(data, strings + o, n);  // Mac Roman: the same for ASCII names
            }
            else continue;
            if (!string.IsNullOrWhiteSpace(text)) names.Add(text.Trim().ToLowerInvariant());
        }
        return names;
    }

    private static HashSet<int> CmapCovers(FileStream fs, (long Off, int Len) table, IReadOnlyCollection<int> codepoints)
    {
        var data = ReadAt(fs, table.Off, table.Len);
        if (data.Length < 4) return new HashSet<int>();
        var subtables = new Dictionary<(int, int, int), int>();
        int count = U16(data, 2);
        for (var i = 0; i < count; i++)
        {
            var at = 4 + 8 * i;
            if (at + 8 > data.Length) break;
            var sub = U32(data, at + 4);
            if (sub + 2 <= (uint)data.Length)
                subtables.TryAdd((U16(data, at), U16(data, at + 2), U16(data, (int)sub)), (int)sub);
        }
        foreach (var key in new[] { (3, 10, 12), (0, 6, 12), (0, 4, 12), (3, 1, 4), (0, 3, 4), (0, 2, 4), (0, 1, 4), (0, 0, 4) })
            if (subtables.TryGetValue(key, out var sub))
                return key.Item3 == 12 ? Fmt12(data, sub, codepoints) : Fmt4(data, sub, codepoints);
        return new HashSet<int>();
    }

    private static HashSet<int> Fmt12(byte[] data, int sub, IReadOnlyCollection<int> codepoints)
    {
        var covered = new HashSet<int>();
        if (sub + 16 > data.Length) return covered;
        var groups = Math.Min(U32(data, sub + 12), (uint)((data.Length - sub - 16) / 12));
        for (var i = 0; i < groups; i++)
        {
            var at = sub + 16 + 12 * i;
            long start = U32(data, at), end = U32(data, at + 4), glyph = U32(data, at + 8);
            foreach (var c in codepoints)
                if (c >= start && c <= end && glyph + (c - start) != 0) covered.Add(c);
        }
        return covered;
    }

    private static HashSet<int> Fmt4(byte[] data, int sub, IReadOnlyCollection<int> codepoints)
    {
        var covered = new HashSet<int>();
        if (sub + 14 > data.Length) return covered;
        int seg2 = U16(data, sub + 6);
        int endsAt = sub + 14, startsAt = endsAt + seg2 + 2, deltasAt = startsAt + seg2, rangesAt = deltasAt + seg2;
        if (rangesAt + seg2 > data.Length) return covered;
        foreach (var c in codepoints)
        {
            if (c > 0xFFFF) continue;
            for (var i = 0; i < seg2 / 2; i++)
            {
                if (c > U16(data, endsAt + 2 * i)) continue;
                int start = U16(data, startsAt + 2 * i);
                if (c < start) break;
                int delta = I16(data, deltasAt + 2 * i), rangeOff = U16(data, rangesAt + 2 * i);
                int glyph;
                if (rangeOff == 0) glyph = (c + delta) & 0xFFFF;
                else
                {
                    var at = rangesAt + 2 * i + rangeOff + 2 * (c - start);
                    if (at + 2 > data.Length) break;
                    glyph = U16(data, at);
                    if (glyph != 0) glyph = (glyph + delta) & 0xFFFF;
                }
                if (glyph != 0) covered.Add(c);
                break;
            }
        }
        return covered;
    }

    /// A font file's family names (lower-cased, every face of a collection), which of
    /// <paramref name="codepoints"/> it covers, and whether a face with a colour table covers any of them.
    /// Empty for a file that is not a readable font.
    internal static (HashSet<string> Families, HashSet<int> Covered, bool Color) ReadFont(string path, IReadOnlyCollection<int>? codepoints = null)
    {
        var families = new HashSet<string>(StringComparer.Ordinal);
        var covered = new HashSet<int>();
        var color = false;
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            foreach (var face in Faces(fs))
            {
                var t = Tables(fs, face);
                if (t.TryGetValue("name", out var name)) families.UnionWith(Families(fs, name));
                if (codepoints is { Count: > 0 } && t.TryGetValue("cmap", out var cmap))
                {
                    var got = CmapCovers(fs, cmap, codepoints);
                    covered.UnionWith(got);
                    if (got.Count > 0 && ColorTables.Any(t.ContainsKey)) color = true;
                }
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or ArgumentException)
        {
            return (new HashSet<string>(StringComparer.Ordinal), new HashSet<int>(), false);
        }
        return (families, covered, color);
    }

    /// Every font file under <paramref name="dirs"/> (recursively, as fontconfig reads a &lt;dir&gt;), sorted.
    internal static List<string> FontFiles(IEnumerable<string> dirs)
    {
        var found = new List<string>();
        foreach (var d in dirs)
        {
            try
            {
                found.AddRange(Directory.EnumerateFiles(d, "*", new EnumerationOptions { RecurseSubdirectories = true, IgnoreInaccessible = true })
                    .Where(f => FontExts.Any(x => f.EndsWith(x, StringComparison.OrdinalIgnoreCase))));
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
        }
        found.Sort(StringComparer.Ordinal);
        return found;
    }

    private static readonly Dictionary<(string, long, DateTime), HashSet<string>> FamilyCache = new();

    /// Lower-cased family names of every font under <paramref name="dirs"/> (cached per file size and mtime).
    internal static HashSet<string> DirFamilies(IEnumerable<string> dirs)
    {
        var fams = new HashSet<string>(StringComparer.Ordinal);
        foreach (var path in FontFiles(dirs))
        {
            FileInfo fi;
            try { fi = new FileInfo(path); _ = fi.Length; } catch (IOException) { continue; }
            var key = (path, fi.Length, fi.LastWriteTimeUtc);
            HashSet<string>? cached;
            lock (FamilyCache)
                if (!FamilyCache.TryGetValue(key, out cached)) FamilyCache[key] = cached = ReadFont(path).Families;
            fams.UnionWith(cached);
        }
        return fams;
    }

    // --- which directories ------------------------------------------------------------------------------

    private static string Expand(string p)
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (p == "~") p = home;
        else if (p.StartsWith("~/", StringComparison.Ordinal)) p = Path.Join(home, p[2..]);
        return Path.GetFullPath(p);
    }

    private static IEnumerable<string> Split(string? value) =>
        string.IsNullOrEmpty(value) ? Enumerable.Empty<string>()
            : value.Split(Path.PathSeparator).Select(p => p.Trim()).Where(p => p.Length > 0).Select(Expand);

    /// The <see cref="LaunchOptions.FontDirs"/> option as absolute paths. Throws ArgumentException on Linux for
    /// a path that is not a directory: a typo must not silently fall back to the lookalikes. (Elsewhere the
    /// option does nothing: Windows and macOS have their own fonts.)
    internal static List<string> CheckFontDirs(IEnumerable<string>? fontDirs, bool? isLinux = null)
    {
        if (fontDirs is null) return new List<string>();
        var dirs = new List<string>();
        foreach (var d in fontDirs)
        {
            if (string.IsNullOrWhiteSpace(d)) throw new ArgumentException("FontDirs: an empty path is not a directory");
            dirs.Add(Expand(d));
        }
        if (isLinux ?? OperatingSystem.IsLinux())
            foreach (var d in dirs)
                if (!Directory.Exists(d)) throw new ArgumentException($"FontDirs: {d} is not a directory");
        return dirs;
    }

    /// The option's directories then CLEARCOTE_FONT_DIRS (User), and CLEARCOTE_FALLBACK_FONT_DIRS
    /// (Fallback), each without duplicates; Ignored lists entries from the environment that are not
    /// directories (the option throws for those instead).
    internal static (List<string> User, List<string> Fallback, List<string> Ignored) ResolveFontDirs(
        IEnumerable<string>? fontDirs, Func<string, string?>? getEnv = null, bool? isLinux = null)
    {
        getEnv ??= Environment.GetEnvironmentVariable;
        var user = new List<string>();
        var fallback = new List<string>();
        var ignored = new List<string>();
        foreach (var d in CheckFontDirs(fontDirs, isLinux)) if (!user.Contains(d)) user.Add(d);
        foreach (var d in Split(getEnv(FontDirsEnv)))
        {
            if (!Directory.Exists(d)) ignored.Add(d);
            else if (!user.Contains(d)) user.Add(d);
        }
        foreach (var d in Split(getEnv(FallbackFontDirsEnv)))
        {
            if (!Directory.Exists(d)) ignored.Add(d);
            else if (!user.Contains(d) && !fallback.Contains(d)) fallback.Add(d);
        }
        return (user, fallback, ignored);
    }

    // --- the generated fontconfig file ----------------------------------------------------------------

    private static string Xml(string s) =>
        s.Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace("\"", "&quot;");

    private static string Unxml(string s) =>
        s.Replace("&lt;", "<").Replace("&gt;", ">").Replace("&quot;", "\"").Replace("&amp;", "&").Trim();

    private static readonly Regex Rule = new(
        "[ \\t]*<match\\b[^>]*>\\s*<test\\b[^>]*name=\"family\"[^>]*>\\s*<string>([^<]*)</string>\\s*</test>\\s*"
        + "<edit\\b[^>]*name=\"family\"[^>]*>\\s*<string>([^<]*)</string>(\\s*</edit>\\s*</match>)[ \\t]*\\n?", RegexOptions.CultureInvariant);
    private static readonly Regex Alias = new(
        "(<alias\\b[^>]*>\\s*<family>([^<]*)</family>\\s*<prefer>\\s*<family>)([^<]*)(</family>)", RegexOptions.CultureInvariant);
    private static readonly Regex BundleDir = new("<dir>\\s*@FONTS_DIR@\\s*</dir>", RegexOptions.CultureInvariant);

    /// <paramref name="template"/> with the lookalike rules and generics adjusted to the
    /// <paramref name="genuine"/> families (lower-cased names your own fonts provide).
    internal static string GenuineRules(string template, IReadOnlySet<string> genuine)
    {
        if (genuine.Count == 0) return template;
        var ruled = Rule.Replace(template, m =>
        {
            var family = Unxml(m.Groups[1].Value);
            if (genuine.Contains(family.ToLowerInvariant())) return "";
            if (WindowsAliases.TryGetValue(family.ToLowerInvariant(), out var target)
                && genuine.Contains(target.ToLowerInvariant()) && Unxml(m.Groups[2].Value) != target)
                return m.Value.Replace($"<string>{m.Groups[2].Value}</string>{m.Groups[3].Value}",
                    $"<string>{Xml(target)}</string>{m.Groups[3].Value}");
            return m.Value;
        });
        return Alias.Replace(ruled, m =>
            GenericWindows.TryGetValue(Unxml(m.Groups[2].Value).ToLowerInvariant(), out var target) && genuine.Contains(target.ToLowerInvariant())
                ? m.Groups[1].Value + Xml(target) + m.Groups[4].Value
                : m.Value);
    }

    /// The fontconfig file for one launch: your own directories ahead of the bundle, the fallback ones
    /// after it, and the rules adjusted to the families your own fonts genuinely provide.
    internal static string BuildConf(string template, string fontsDir, string cacheDir, IReadOnlyList<string>? userDirs = null,
        IReadOnlyList<string>? fallbackDirs = null, IReadOnlySet<string>? genuine = null)
    {
        userDirs ??= Array.Empty<string>();
        fallbackDirs ??= Array.Empty<string>();
        var conf = GenuineRules(template, genuine ?? new HashSet<string>());
        if (userDirs.Count > 0 || fallbackDirs.Count > 0)
        {
            var mine = userDirs.Select(d => $"<dir>{Xml(d)}</dir>").ToList();
            var after = fallbackDirs.Select(d => $"<dir>{Xml(d)}</dir>").ToList();
            conf = BundleDir.IsMatch(conf)
                ? BundleDir.Replace(conf, _ => string.Join("\n  ", mine.Append("<dir>@FONTS_DIR@</dir>").Concat(after)), 1)
                // a template without the usual line: still list everything, the bundle's place unknown
                : conf.Replace("</fontconfig>", "  " + string.Join("\n  ", mine.Concat(after)) + "\n</fontconfig>");
        }
        return conf.Replace("@FONTS_DIR@", fontsDir).Replace("@CACHE_DIR@", cacheDir);
    }

    /// The FONTCONFIG_FILE for a launch of <paramref name="exePath"/>, or null off Linux or for a build with
    /// no font bundle. Without extra directories the file is <c>fonts.generated.conf</c>, exactly as the Python
    /// and Node SDKs write it; with some, each distinct result gets its own <c>fonts.generated-&lt;hash&gt;.conf</c>.
    internal static string? LinuxFontConfig(string exePath, IEnumerable<string>? fontDirs, bool? isLinux = null)
    {
        if (!(isLinux ?? OperatingSystem.IsLinux())) return null;
        var (user, fallback, _) = ResolveFontDirs(fontDirs, null, isLinux);
        var fontsDir = Path.Join(Path.GetDirectoryName(exePath), "fonts");
        var template = Path.Join(fontsDir, "fonts.conf.template");
        if (!File.Exists(template)) return null;
        try
        {
            var cacheDir = Path.Join(Path.GetTempPath().TrimEnd(Path.DirectorySeparatorChar), "cc-fc-cache");
            Directory.CreateDirectory(cacheDir);
            // The rules follow your fonts only on an engine that draws them (GenuineFacesSwitch).
            var genuine = user.Count > 0 && EngineHonoursGenuineFaces(exePath) ? DirFamilies(user) : new HashSet<string>();
            var conf = BuildConf(File.ReadAllText(template), fontsDir, cacheDir, user, fallback, genuine);
            var name = user.Count > 0 || fallback.Count > 0
                ? $"fonts.generated-{Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(conf)))[..12].ToLowerInvariant()}.conf"
                : "fonts.generated.conf";
            var confPath = Path.Join(fontsDir, name);
            var tmp = $"{confPath}.{Environment.ProcessId}.tmp";
            File.WriteAllText(tmp, conf, new UTF8Encoding(false));
            File.Move(tmp, confPath, overwrite: true);
            return confPath;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return null;  // never block a launch on font wiring
        }
    }

    /// Add FONTCONFIG_FILE to the browser env. The caller's own FONTCONFIG_FILE (in
    /// <paramref name="callerEnv"/>, the launch option) always wins. Playwright REPLACES the child env when
    /// Env is set, so without an env the parent environment comes along (as Languages.ApplyLinuxLanguage does).
    internal static IDictionary<string, string>? ApplyLinuxFonts(string exePath, IEnumerable<string>? fontDirs,
        IDictionary<string, string>? env, IDictionary<string, string>? callerEnv, bool? isLinux = null)
    {
        if (callerEnv is not null && callerEnv.ContainsKey("FONTCONFIG_FILE")) return env;
        var conf = LinuxFontConfig(exePath, fontDirs, isLinux);
        if (conf is null) return env;
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
        outEnv["FONTCONFIG_FILE"] = conf;
        return outEnv;
    }
}
