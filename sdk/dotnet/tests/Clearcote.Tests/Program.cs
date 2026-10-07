using System.Text.Json;

namespace Clearcote.Tests;

/// Entry point for the child processes some tests start (<c>dotnet Clearcote.Tests.dll install-child ...</c>).
/// The test runner never calls it; it replaces the empty one the test SDK would generate
/// (GenerateProgramFile=false in the project file).
public static class Program
{
    public static async Task<int> Main(string[] args)
    {
        if (args.Length == 3 && args[0] == "install-child")
        {
            var path = await Download.ProEnsureBinaryAsync("test-key",
                new ProDownloadOptions { ApiBase = args[1], CacheDir = args[2], Quiet = true });
            Console.WriteLine(JsonSerializer.Serialize(new { path }));
            return 0;
        }
        Console.Error.WriteLine("usage: Clearcote.Tests install-child <api base> <cache dir>");
        return 2;
    }
}
