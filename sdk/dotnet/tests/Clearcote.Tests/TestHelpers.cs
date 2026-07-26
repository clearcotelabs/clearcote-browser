using System.Net;

// These suites mutate process-global state (env vars, the platform + HTTP test seams), so run serially.
[assembly: Xunit.CollectionBehavior(DisableTestParallelization = true)]

namespace Clearcote.Tests;

/// A captured HTTP handler: records outgoing requests and returns canned responses. The .NET
/// equivalent of the Node suite swapping global fetch / the Python suite monkeypatching urlopen.
internal sealed class FakeHandler : HttpMessageHandler
{
    public readonly List<HttpRequestMessage> Requests = new();
    private readonly Func<HttpRequestMessage, HttpResponseMessage> _responder;

    public FakeHandler(Func<HttpRequestMessage, HttpResponseMessage> responder) => _responder = responder;

    public static FakeHandler Json(HttpStatusCode status, string body) =>
        new(_ => new HttpResponseMessage(status) { Content = new StringContent(body) });

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        Requests.Add(request);
        return Task.FromResult(_responder(request));
    }
}

/// Temp directories for tests, created and removed in one place.
///
/// Every test tree must be removed when the test finishes: a few `dotnet test` runs leave hundreds of
/// stale trees behind otherwise, and leaked browser/profile directories have filled a test box's disk
/// before. The Node suite's geometry.live test holds the same convention with rmSync.
internal static class TestTemp
{
    public static string Create(string prefix) => Directory.CreateTempSubdirectory(prefix).FullName;

    /// Best-effort recursive removal. On Windows a process that touched the tree can hold handles
    /// under it for a moment after it exits, so losing that race must not fail an otherwise green
    /// test — same tolerance as GeometryLiveTests and the SDK's own ephemeral-profile cleanup.
    public static void Remove(string dir)
    {
        try { Directory.Delete(dir, recursive: true); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}

/// Snapshots + restores env vars and the (internal) platform/HTTP test seams on Dispose, so tests
/// stay hermetic. Mirrors the Node OLD-env save/restore + Python monkeypatch auto-undo.
internal sealed class Sandbox : IDisposable
{
    private readonly Dictionary<string, string?> _env = new();
    private readonly List<string> _temp = new();

    public Sandbox Env(string key, string? value)
    {
        if (!_env.ContainsKey(key)) _env[key] = Environment.GetEnvironmentVariable(key);
        Environment.SetEnvironmentVariable(key, value);
        return this;
    }

    /// Redirect HOME + USERPROFILE to a fresh temp dir (like the license suite's temp HOME).
    /// The directory is removed on Dispose — the license suites call this once per test, so leaving
    /// them behind leaked a home tree per test per run.
    public string TempHome()
    {
        var dir = TestTemp.Create("cc-home-");
        _temp.Add(dir);
        Env("HOME", dir);
        Env("USERPROFILE", dir);
        return dir;
    }

    public Sandbox Os(string? tag) { Native.OsTagOverride = tag; return this; }
    public Sandbox Http(HttpMessageHandler? handler) { SdkHttp.HandlerOverride = handler; return this; }

    public void Dispose()
    {
        foreach (var (k, v) in _env) Environment.SetEnvironmentVariable(k, v);
        Native.OsTagOverride = null;
        SdkHttp.HandlerOverride = null;
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }
}
