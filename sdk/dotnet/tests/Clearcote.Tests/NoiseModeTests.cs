using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// NoiseMode: one noise per profile for a new profile folder, per-site noise kept for one made before 0.70.5.
/// Same cases as the Node suite (test/noisemode.test.ts) and the Python suite (tests/test_noisemode.py).
/// </summary>
[Collection("Console")]
public class NoiseModeTests : IDisposable
{
    private readonly List<string> _dirs = new();
    private readonly StringWriter _err = new();
    private readonly TextWriter _stderr = Console.Error;
    private static readonly Func<string?, bool> R60 = _ => true;
    private static readonly Func<string?, bool> Old = _ => false;

    public NoiseModeTests() => Console.SetError(_err);

    public void Dispose()
    {
        Console.SetError(_stderr);
        foreach (var d in _dirs) try { Directory.Delete(d, recursive: true); } catch (IOException) { }
    }

    private string Fresh()
    {
        var d = Directory.CreateTempSubdirectory("cc-noise-").FullName;
        _dirs.Add(d);
        return d;
    }

    private string ChromeProfile()
    {
        var d = Fresh();
        File.WriteAllText(Path.Combine(d, "Local State"), "{}");
        Directory.CreateDirectory(Path.Combine(d, "Default"));
        return d;
    }

    private static JsonObject State(string d) => (JsonObject)JsonNode.Parse(File.ReadAllText(Path.Combine(d, NoiseMode.ProfileStateFile)))!;

    [Fact]
    public void NewEmptyFolderGetsPerProfileAndRemembersIt()
    {
        var d = Fresh();
        Assert.True(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
        Assert.Equal("per-profile", (string?)State(d)["noise"]);
    }

    [Fact]
    public void MissingFolderIsCreatedSoItsFirstRunDoesNotMakeItOld()
    {
        var d = Path.Combine(Fresh(), "new-profile");
        Assert.True(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
        File.WriteAllText(Path.Combine(d, "Local State"), "{}");
        Assert.True(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
    }

    [Fact]
    public void FolderMadeBeforeThisReleaseKeepsPerSite()
    {
        var d = ChromeProfile();
        Assert.False(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
        Assert.Equal("per-site", (string?)State(d)["noise"]);
        Assert.False(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
    }

    [Fact]
    public void OldEngineNewFolderIsPerSiteAndStaysSo()
    {
        var d = Fresh();
        Assert.False(NoiseMode.Resolve(null, d, quiet: true, supports: Old));
        Assert.Equal("per-site", NoiseMode.Read(d));
        Assert.False(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
    }

    [Fact]
    public void ExplicitValueWinsAndBecomesTheMode()
    {
        var legacy = ChromeProfile();
        Assert.True(NoiseMode.Resolve(true, legacy, quiet: true, supports: R60));
        Assert.True(NoiseMode.Resolve(null, legacy, quiet: true, supports: R60));
        var d = Fresh();
        Assert.False(NoiseMode.Resolve(false, d, quiet: true, supports: R60));
        Assert.False(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
    }

    [Fact]
    public void NoFolderIsNewAndWritesNothing()
    {
        Assert.True(NoiseMode.Resolve(null, null, quiet: true, supports: R60));
        Assert.False(NoiseMode.Resolve(null, null, quiet: true, supports: Old));
        Assert.False(NoiseMode.Resolve(false, null, quiet: true, supports: R60));
    }

    [Fact]
    public void PassThroughIsLeftAlone()
    {
        var d = Fresh();
        Assert.Null(NoiseMode.Resolve(null, d, passthrough: true, quiet: true, supports: R60));
        Assert.False(File.Exists(Path.Combine(d, NoiseMode.ProfileStateFile)));
    }

    [Fact]
    public void KeepsOtherKeysAndIgnoresAFileItCannotRead()
    {
        var d = Fresh();
        File.WriteAllText(Path.Combine(d, NoiseMode.ProfileStateFile), "{\"other\":1}");
        Assert.True(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
        Assert.Equal(1, (int)State(d)["other"]!);
        Assert.Equal("per-profile", (string?)State(d)["noise"]);
        var bad = ChromeProfile();
        File.WriteAllText(Path.Combine(bad, NoiseMode.ProfileStateFile), "not json");
        Assert.False(NoiseMode.Resolve(null, bad, quiet: true, supports: R60));
        Assert.Equal("per-site", (string?)State(bad)["noise"]);
    }

    [Fact]
    public void UnwritableNeverFailsAndSaysSoUnlessQuiet()
    {
        var d = Fresh();
        Directory.CreateDirectory(Path.Combine(d, NoiseMode.ProfileStateFile));  // a directory where the file should go
        Assert.True(NoiseMode.Resolve(null, d, supports: R60));
        Assert.Contains("could not remember the noise mode", _err.ToString());
        _err.GetStringBuilder().Clear();
        Assert.True(NoiseMode.Resolve(null, d, quiet: true, supports: R60));
        Assert.Equal("", _err.ToString());
    }
}
