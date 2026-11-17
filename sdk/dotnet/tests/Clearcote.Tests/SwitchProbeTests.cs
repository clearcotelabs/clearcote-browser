using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// EngineSupportsSwitch remembers where it found a name.
///
/// A launch asks the engine binary for about seven switch names. Each search read the binary from its start, and
/// the names sit together near the end of a 290 MB chrome.dll. The probe now keeps the offset of every name it
/// found (in memory, and in ~/.clearcote/engine-switches.json for the next process), checks a known name where it
/// was found, and searches for a new one from just before the known ones. The answers must stay exactly what a
/// search of the whole file gives, whatever the positions file says.
///
/// Mirrors sdk/node/test/switch-probe.test.ts and sdk/python/tests/test_switch_probe.py.
public sealed class SwitchProbeTests : IDisposable
{
    private const int MB = 1024 * 1024;
    private const int Chunk = 8 * MB;
    private const int Size = 20 * MB;

    private static byte[] Needle(string name) => Encoding.Latin1.GetBytes($"\0{name}\0");

    // Where each name sits in the stand-in binary. "near-end" is found first in most tests; the others are before
    // it, across a read boundary, or across the place a later search starts from.
    private static readonly (string Name, long At)[] At =
    {
        ("near-end", 18L * MB),
        ("also-near-end", 19L * MB + 123),
        ("early", 1L * MB + 7),
        ("across-a-chunk", Chunk - 5),
        ("across-the-start", 2L * Chunk - 3),
        ("last-bytes", Size - Needle("last-bytes").Length),
    };

    private static long AtOf(string name) => At.First(a => a.Name == name).At;

    private readonly string _dir = TestTemp.Create("cc-switch-probe-");
    private readonly string _positions;
    private readonly string _exe;
    private readonly Func<string> _file = LaunchOpts.SwitchPositions.FilePath;
    private readonly long _minSize = LaunchOpts.SwitchPositions.MinSize;
    private readonly long _lookBack = LaunchOpts.SwitchPositions.LookBack;
    private readonly int _maxFiles = LaunchOpts.SwitchPositions.MaxFiles;

    public SwitchProbeTests()
    {
        _positions = Path.Combine(_dir, "positions", "engine-switches.json");
        var body = new byte[Size];
        Array.Fill(body, (byte)'A');
        foreach (var (name, at) in At) Needle(name).CopyTo(body, at);
        // a longer name that contains a shorter one, and a header-like string that is not NUL-delimited
        Needle("near-end-extended").CopyTo(body, 3L * MB);
        Encoding.Latin1.GetBytes("proxy-authenticate").CopyTo(body, 4L * MB);
        _exe = WriteEngine("engine", body);
        LaunchOpts.SwitchPositions.FilePath = () => _positions;
        LaunchOpts.SwitchPositions.LookBack = 0;
        NewProcess();
    }

    public void Dispose()
    {
        LaunchOpts.SwitchPositions.FilePath = _file;
        LaunchOpts.SwitchPositions.MinSize = _minSize;
        LaunchOpts.SwitchPositions.LookBack = _lookBack;
        LaunchOpts.SwitchPositions.MaxFiles = _maxFiles;
        LaunchOpts.ResetSwitchProbe();
        TestTemp.Remove(_dir);
    }

    /// The file the probe reads: chrome.dll next to the executable on Windows when there is one, else the
    /// executable itself (as here).
    private string WriteEngine(string sub, byte[] body)
    {
        var d = Path.Combine(_dir, sub);
        Directory.CreateDirectory(d);
        var p = Path.Combine(d, "chrome");
        File.WriteAllBytes(p, body);
        return p;
    }

    private static string KeyOf(string path)
    {
        var fi = new FileInfo(path);
        return $"{fi.FullName}|{fi.Length}|{new DateTimeOffset(fi.LastWriteTimeUtc).ToUnixTimeSeconds()}";
    }

    /// A new process: nothing in memory, the positions file as it is.
    private static void NewProcess()
    {
        LaunchOpts.ResetSwitchProbe();
        LaunchOpts.ProbeBytesRead = 0;
    }

    private JsonObject Stored() => (JsonObject)JsonNode.Parse(File.ReadAllText(_positions))!["files"]!;

    private Dictionary<string, long> StoredFor(string exe)
        => ((JsonObject)Stored()[KeyOf(exe)]!).ToDictionary(kv => kv.Key, kv => kv.Value!.GetValue<long>());

    private void WritePositions(string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(_positions)!);
        File.WriteAllText(_positions, text);
    }

    private void WritePositions(string exe, params (string Name, long At)[] at)
    {
        var mine = new JsonObject();
        foreach (var (n, a) in at) mine[n] = a;
        WritePositions(new JsonObject { ["v"] = 1, ["files"] = new JsonObject { [KeyOf(exe)] = mine } }.ToJsonString());
    }

    private static Dictionary<string, long> Dict(params (string Name, long At)[] at) => at.ToDictionary(a => a.Name, a => a.At);

    // -- answers ---------------------------------------------------------------------------------------

    public static IEnumerable<object[]> Orders() => new[]
    {
        new object[] { At.Select(a => a.Name).ToArray() },
        new object[] { At.Select(a => a.Name).Reverse().ToArray() },
        new object[] { new[] { "early", "near-end", "across-the-start", "across-a-chunk", "last-bytes", "also-near-end" } },
    };

    [Theory]
    [MemberData(nameof(Orders))]
    public void Finds_every_name_wherever_it_sits_in_any_order_and_no_name_that_is_not_there(string[] order)
    {
        foreach (var name in order) Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, name), name);
        foreach (var name in new[] { "missing", "near-en", "ear-end", "proxy-auth", "proxy-authenticate", "" })
            Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, name), name);
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "near-end-extended"));
    }

    [Fact]
    public void A_missing_file_no_path_and_a_directory_answer_false()
    {
        Assert.False(LaunchOpts.EngineSupportsSwitch(Path.Combine(_dir, "nope"), "near-end"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(null, "near-end"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(_dir, "near-end"));
        Assert.False(File.Exists(_positions));
    }

    // -- what is remembered ----------------------------------------------------------------------------

    [Fact]
    public void The_offset_of_each_name_found_per_binary_and_nothing_for_a_name_that_is_not_there()
    {
        foreach (var (name, _) in At) LaunchOpts.EngineSupportsSwitch(_exe, name);
        LaunchOpts.EngineSupportsSwitch(_exe, "missing");
        Assert.Equal(new[] { KeyOf(_exe) }, Stored().Select(kv => kv.Key).ToArray());
        Assert.Equal(Dict(At), StoredFor(_exe));
        // written whole, then renamed
        Assert.Equal(new[] { "engine-switches.json" }, Directory.GetFileSystemEntries(Path.GetDirectoryName(_positions)!).Select(Path.GetFileName).ToArray());
    }

    [Fact]
    public void Nothing_for_a_small_binary_it_is_searched_whole_every_time()
    {
        var small = WriteEngine("small", Encoding.Latin1.GetBytes("\u007fELF\0proxy-auth\0--remote-debugging-port\0"));
        Assert.True(LaunchOpts.EngineSupportsSwitch(small, "proxy-auth"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(small, "missing"));
        Assert.False(File.Exists(_positions));
    }

    [Fact]
    public void Only_the_newest_binaries_are_kept()
    {
        LaunchOpts.SwitchPositions.MinSize = 0;
        LaunchOpts.SwitchPositions.MaxFiles = 2;
        var keys = new List<string>();
        foreach (var n in new[] { "a", "b", "c" })
        {
            var p = WriteEngine("engine-" + n, Encoding.Latin1.GetBytes($"\u007fELF\0switch-{n}\0"));
            Assert.True(LaunchOpts.EngineSupportsSwitch(p, "switch-" + n));
            keys.Add(KeyOf(p));
        }
        Assert.Equal(keys.Skip(1).ToArray(), Stored().Select(kv => kv.Key).ToArray());
    }

    // -- the next process ------------------------------------------------------------------------------

    [Fact]
    public void Checks_a_known_name_where_it_was_found_instead_of_searching()
    {
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "near-end"));
        Assert.True(LaunchOpts.ProbeBytesRead >= 18L * MB); // the first search reads up to the name
        NewProcess();
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "near-end"));
        Assert.Equal(Needle("near-end").Length, LaunchOpts.ProbeBytesRead);
    }

    [Fact]
    public void Searches_for_a_new_name_from_the_known_ones_on_then_in_the_rest_of_the_file()
    {
        LaunchOpts.EngineSupportsSwitch(_exe, "near-end");
        NewProcess();
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "also-near-end"));
        Assert.True(LaunchOpts.ProbeBytesRead <= 4L * MB + 64); // from 16 MB to the end
        NewProcess();
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "early")); // before the known ones: the rest of the file
        Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, "missing"));
        Assert.Equal(Dict(("near-end", AtOf("near-end")), ("also-near-end", AtOf("also-near-end")), ("early", AtOf("early"))), StoredFor(_exe));
    }

    [Fact]
    public void A_name_the_binary_lacks_is_searched_for_in_the_whole_file_once_per_process()
    {
        LaunchOpts.EngineSupportsSwitch(_exe, "near-end");
        NewProcess();
        Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, "missing"));
        Assert.True(LaunchOpts.ProbeBytesRead >= Size);
        LaunchOpts.ProbeBytesRead = 0;
        Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, "missing"));
        Assert.Equal(0, LaunchOpts.ProbeBytesRead);
    }

    // -- the positions file is never the answer --------------------------------------------------------

    [Fact]
    public void A_wrong_offset_for_a_name_that_is_there_is_found_by_searching_and_corrected()
    {
        WritePositions(_exe, ("near-end", 5L * MB), ("early", AtOf("near-end")));
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "near-end"));
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "early"));
        Assert.Equal(Dict(("near-end", AtOf("near-end")), ("early", AtOf("early"))), StoredFor(_exe));
    }

    [Fact]
    public void An_offset_for_a_name_that_is_not_there_is_still_false()
    {
        WritePositions(_exe, ("missing", AtOf("near-end")), ("near-en", AtOf("near-end")), ("gone", Size - 2));
        foreach (var name in new[] { "missing", "near-en", "gone" })
            Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, name), name);
    }

    [Theory]
    [InlineData("not-json")]
    [InlineData("another-shape")]
    [InlineData("files-is-a-list")]
    [InlineData("no-offsets")]
    [InlineData("an-entry-that-is-no-object")]
    public void A_damaged_positions_file_gives_the_same_answers(string kind)
    {
        var key = System.Text.Json.JsonSerializer.Serialize(KeyOf(_exe));
        WritePositions(kind switch
        {
            "not-json" => "{{{",
            "another-shape" => "[1,2,3]",
            "files-is-a-list" => "{\"v\":1,\"files\":[]}",
            "an-entry-that-is-no-object" => "{\"v\":1,\"files\":{" + key + ":7}}",
            // offsets that are no offsets
            _ => "{\"v\":1,\"files\":{" + key + ":{\"near-end\":-1,\"early\":1000000000000,\"across-a-chunk\":\"12\","
                 + "\"last-bytes\":1.5,\"also-near-end\":null,\"across-the-start\":true}}}",
        });
        foreach (var (name, _) in At) Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, name), name);
        Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, "missing"));
        Assert.Equal(Dict(At), StoredFor(_exe));
    }

    [Fact]
    public void A_binary_that_changed_starts_again()
    {
        LaunchOpts.EngineSupportsSwitch(_exe, "near-end");
        var body = new byte[Size];
        Array.Fill(body, (byte)'A');
        Needle("near-end").CopyTo(body, 2L * MB);
        var other = WriteEngine("changed", body);
        // the old binary's positions under the new binary's key would point at the wrong place: checked, then searched
        WritePositions(other, ("near-end", AtOf("near-end")));
        NewProcess();
        Assert.True(LaunchOpts.EngineSupportsSwitch(other, "near-end"));
        Assert.False(LaunchOpts.EngineSupportsSwitch(other, "early"));
        Assert.Equal(Dict(("near-end", 2L * MB)), StoredFor(other));
        // same path and size, a later modification time: another key
        var before = KeyOf(other);
        File.SetLastWriteTimeUtc(other, File.GetLastWriteTimeUtc(other).AddSeconds(10));
        Assert.NotEqual(before, KeyOf(other));
        NewProcess();
        Assert.True(LaunchOpts.EngineSupportsSwitch(other, "near-end"));
        Assert.Contains(KeyOf(other), Stored().Select(kv => kv.Key));
    }

    [Fact]
    public void A_positions_file_that_cannot_be_written_changes_nothing()
    {
        LaunchOpts.SwitchPositions.FilePath = () => Path.Combine(_exe, "cannot", "engine-switches.json"); // under a FILE
        foreach (var (name, _) in At) Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, name), name);
        Assert.False(LaunchOpts.EngineSupportsSwitch(_exe, "missing"));
    }

    /// The Node and Python SDKs read and write the same file: what they wrote must be understood here, and what
    /// is written here must have their shape ({"v":1,"files":{"path|size|mtime seconds":{"name":offset}}}).
    [Fact]
    public void The_file_has_the_shape_the_Node_and_Python_SDKs_write()
    {
        // as json.dump / JSON.stringify write it
        WritePositions("{\"v\":1,\"files\":{" + System.Text.Json.JsonSerializer.Serialize(KeyOf(_exe)) + ":{\"near-end\":" + AtOf("near-end") + "}}}");
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "near-end"));
        Assert.Equal(Needle("near-end").Length, LaunchOpts.ProbeBytesRead);
        Assert.True(LaunchOpts.EngineSupportsSwitch(_exe, "also-near-end"));
        var root = (JsonObject)JsonNode.Parse(File.ReadAllText(_positions))!;
        Assert.Equal(new[] { "v", "files" }, root.Select(kv => kv.Key).ToArray());
        Assert.Equal(1, root["v"]!.GetValue<int>());
        Assert.Equal(Dict(("near-end", AtOf("near-end")), ("also-near-end", AtOf("also-near-end"))), StoredFor(_exe));
    }
}
