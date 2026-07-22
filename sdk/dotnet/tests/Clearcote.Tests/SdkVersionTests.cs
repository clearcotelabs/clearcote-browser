using System.Linq;
using System.Reflection;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Guards the SDK version constant against the csproj.
///
/// WHY THIS EXISTS. PUBLISHING.md tells you to bump BOTH the csproj &lt;Version&gt; and
/// <see cref="Clearcote.Version"/>, and nothing enforced it — so the constant was missed on 0.18.0
/// and again on 0.19.x and sat at "0.17.1" while the shipped package said 0.19.2. Anything reading
/// the constant at runtime (a user agent string, a bug report, a support diagnostic) reported a
/// version that had not existed for two releases, and no test noticed. VersionTests.cs sounds like
/// it would cover this and does not: it is about the browser-version CATALOG selector.
///
/// The assembly's informational version is generated from the csproj &lt;Version&gt;, which makes it
/// the one value that cannot drift from the package. Comparing the constant against it turns a
/// forgotten bump into a red test instead of a wrong number in the field.
/// </summary>
public class SdkVersionTests
{
    [Fact]
    public void Version_constant_matches_the_assembly_version_from_the_csproj()
    {
        var asm = typeof(Clearcote).Assembly;
        var info = asm.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion
                   ?? asm.GetName().Version?.ToString()
                   ?? "";
        // SourceLink appends "+<commit sha>" to the informational version; the package version is
        // everything before it.
        var packageVersion = info.Split('+')[0];
        Assert.Equal(Clearcote.Version, packageVersion);
    }

    [Fact]
    public void Version_constant_is_a_plain_three_part_version()
    {
        var parts = Clearcote.Version.Split('.');
        Assert.Equal(3, parts.Length);
        Assert.All(parts, p => Assert.True(int.TryParse(p, out _), $"'{p}' is not numeric"));
    }
}
