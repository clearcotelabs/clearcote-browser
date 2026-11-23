using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// SchemaMode: persona schema 8 for a new profile folder, the remembered schema (7 for one made before 0.70.6) for a
/// known one. Same cases as the Node suite (test/schemamode.test.ts) and the Python suite (tests/test_schemamode.py).
/// </summary>
[Collection("Console")]
public class SchemaModeTests : IDisposable
{
    private readonly List<string> _dirs = new();
    private readonly StringWriter _err = new();
    private readonly TextWriter _stderr = Console.Error;
    private static readonly Func<string?, bool> R51 = _ => true;
    private static readonly Func<string?, bool> Old = _ => false;

    public SchemaModeTests() => Console.SetError(_err);

    public void Dispose()
    {
        Console.SetError(_stderr);
        foreach (var d in _dirs) try { Directory.Delete(d, recursive: true); } catch (IOException) { }
    }

    private string Fresh()
    {
        var d = Directory.CreateTempSubdirectory("cc-schema-").FullName;
        _dirs.Add(d);
        return d;
    }

    private static string ChromeProfile(string d)
    {
        File.WriteAllText(Path.Combine(d, "Local State"), "{}");
        Directory.CreateDirectory(Path.Combine(d, "Default"));
        return d;
    }

    private static JsonObject State(string d)
        => (JsonObject)JsonNode.Parse(File.ReadAllText(Path.Combine(d, NoiseMode.ProfileStateFile)))!;

    [Fact]
    public void NewFolderGets8AndKeepsIt()
    {
        var d = Fresh();
        Assert.Equal(8, SchemaMode.Resolve(null, d, quiet: true, supports: R51));
        Assert.Equal("8", (string?)State(d)["schema"]);
        ChromeProfile(d);
        Assert.Equal(8, SchemaMode.Resolve(null, d, quiet: true, supports: R51));
    }

    [Fact]
    public void MissingFolderIsCreatedAndRemembered()
    {
        var d = Path.Combine(Fresh(), "new-profile");
        Assert.Equal(8, SchemaMode.Resolve(null, d, quiet: true, supports: R51));
        Assert.Equal(8, SchemaMode.Read(d));
    }

    [Fact]
    public void FolderMadeEarlierKeeps7AndAddsNothing()
    {
        var d = ChromeProfile(Fresh());
        Assert.Null(SchemaMode.Resolve(null, d, quiet: true, supports: R51));
        Assert.Equal("7", (string?)State(d)["schema"]);
        Assert.Null(SchemaMode.Resolve(null, d, quiet: true, supports: R51));
    }

    [Fact]
    public void EngineWithoutThePersonaServiceGets7()
    {
        var d = Fresh();
        Assert.Null(SchemaMode.Resolve(null, d, quiet: true, supports: Old));
        Assert.Equal(7, SchemaMode.Read(d));
        Assert.Null(SchemaMode.Resolve(null, d, quiet: true, supports: R51));
    }

    [Fact]
    public void ExplicitValueWinsAndIsRemembered()
    {
        var d = ChromeProfile(Fresh());
        Assert.Equal(8, SchemaMode.Resolve(8, d, quiet: true, supports: R51));
        Assert.Equal(8, SchemaMode.Read(d));
        Assert.Equal(8, SchemaMode.Resolve(null, d, quiet: true, supports: R51));
        Assert.Equal(5, SchemaMode.Resolve(5, d, quiet: true, supports: R51));
        Assert.Equal(5, SchemaMode.Read(d));
    }

    [Fact]
    public void SchemaPinnedInArgsWinsAddsNothingAndIsRemembered()
    {
        var d = Fresh();
        Assert.Null(SchemaMode.Resolve(null, d, args: new[] { "--x", "--fingerprint-schema=4" }, quiet: true, supports: R51));
        Assert.Equal(4, SchemaMode.Read(d));
        Assert.Equal(7, SchemaMode.SchemaInArgs(new[] { "--fingerprint-schema=5", "--fingerprint-schema=7" }));
        Assert.Null(SchemaMode.SchemaInArgs(new[] { "--fingerprint-schema=bad" }));
    }

    [Fact]
    public void NoFolderIsNewAndPassthroughIsLeftAlone()
    {
        Assert.Equal(8, SchemaMode.Resolve(null, null, quiet: true, supports: R51));
        Assert.Null(SchemaMode.Resolve(null, null, quiet: true, supports: Old));
        var d = Fresh();
        Assert.Null(SchemaMode.Resolve(null, d, passthrough: true, quiet: true, supports: R51));
        Assert.False(File.Exists(Path.Combine(d, NoiseMode.ProfileStateFile)));
    }

    [Fact]
    public void SharesTheFileWithTheNoiseModeAndIgnoresABadValue()
    {
        var d = Fresh();
        NoiseMode.Resolve(null, d, quiet: true, supports: _ => true);
        SchemaMode.Resolve(null, d, quiet: true, supports: R51);
        Assert.Equal("per-profile", (string?)State(d)["noise"]);
        Assert.Equal("8", (string?)State(d)["schema"]);
        File.WriteAllText(Path.Combine(d, NoiseMode.ProfileStateFile), "{\"noise\":\"per-site\",\"schema\":\"x\"}");
        ChromeProfile(d);
        Assert.Null(SchemaMode.Resolve(null, d, quiet: true, supports: R51));
        Assert.Equal("per-site", (string?)State(d)["noise"]);
        Assert.Equal("7", (string?)State(d)["schema"]);
    }

    [Fact]
    public void UnwritableNeverFailsAndSaysSoUnlessQuiet()
    {
        var d = Fresh();
        Directory.CreateDirectory(Path.Combine(d, NoiseMode.ProfileStateFile));
        Assert.Equal(8, SchemaMode.Resolve(null, d, supports: R51));
        Assert.Contains("could not remember the persona schema", _err.ToString());
        _err.GetStringBuilder().Clear();
        Assert.Equal(8, SchemaMode.Resolve(null, d, quiet: true, supports: R51));
        Assert.Equal("", _err.ToString());
    }

    [Fact]
    public void Schema8IsAcceptedAndPassed()
        => Assert.Contains("--fingerprint-schema=8", Fingerprint.Args(new FingerprintOptions { PersonaSchema = 8 }));
}
