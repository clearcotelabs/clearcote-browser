using System.Runtime.CompilerServices;

namespace Clearcote.Tests;

/// The SDK keeps one Playwright driver for the whole process (<c>Clearcote._pw</c>), started on first use with the
/// environment of that moment. Some tests point TMPDIR / TMP / TEMP at a folder of their own for a while
/// (<see cref="Sandbox.Env"/>). When one of them happened to be the first to need Playwright, the driver kept that
/// folder after the test removed it, and every later Playwright call in the run failed
/// (ENOENT, mkdtemp '&lt;that folder&gt;/playwright-artifacts-*'): which test is first depends only on the order the
/// suites run in, so adding a test class elsewhere could break the cloud suites. Start it once, before any test,
/// with the run's own environment.
internal static class SharedPlaywrightFirst
{
    [ModuleInitializer]
    internal static void Start()
    {
        try { Clearcote.PlaywrightInstanceAsync().GetAwaiter().GetResult(); }
        catch { /* no Playwright driver here: the tests that need one report that themselves */ }
    }
}
