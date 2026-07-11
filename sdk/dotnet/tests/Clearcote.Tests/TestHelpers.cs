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

/// Snapshots + restores env vars and the (internal) platform/HTTP test seams on Dispose, so tests
/// stay hermetic. Mirrors the Node OLD-env save/restore + Python monkeypatch auto-undo.
internal sealed class Sandbox : IDisposable
{
    private readonly Dictionary<string, string?> _env = new();

    public Sandbox Env(string key, string? value)
    {
        if (!_env.ContainsKey(key)) _env[key] = Environment.GetEnvironmentVariable(key);
        Environment.SetEnvironmentVariable(key, value);
        return this;
    }

    /// Redirect HOME + USERPROFILE to a fresh temp dir (like the license suite's temp HOME).
    public string TempHome()
    {
        var dir = Directory.CreateTempSubdirectory("cc-home-").FullName;
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
    }
}
