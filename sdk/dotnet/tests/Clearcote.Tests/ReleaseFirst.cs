using System.Runtime.CompilerServices;

namespace Clearcote.Tests;

/// Release.Current is fixed the first time the Release type is used. Which test class runs first is not fixed, so a
/// test that fakes the host OS (Native.OsTagOverride) could be the first to use it: Current then held
/// the faked platform's release for the rest of the run, and ReleaseTests.PlatformRelease_selects_by_os failed on Linux
/// with the Windows release (the NuGet publish of 0.70.0, first attempt). Using it here, before any test, fixes it to
/// the real host as in a real process.
internal static class ReleaseFirst
{
    [ModuleInitializer]
    internal static void Touch() => _ = Release.Current;
}
