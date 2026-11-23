using System.Net;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// The version catalog is asked once per ~10 minutes per machine, not once per launch: a burst of launches from
// one address was answered with a 403 challenge page by the site's host. The answer is kept per process and in
// ~/.clearcote/catalog.json; when the catalog can't be reached, the last kept answer (up to a week old) is used
// before the bundled snapshot. HOME is a temp dir and the site is an HttpMessageHandler.
// Mirrors sdk/python/tests/test_catalog_cache.py and sdk/node/test/catalog-cache.test.ts.
[Collection("Console")]
public class CatalogCacheTests : IDisposable
{
    private const string Live = "{\"schema\":1,\"builds\":[{\"major\":999,\"version\":\"999.0.1.2\",\"tier\":\"free\",\"platforms\":{}}]}";

    private sealed class Site : HttpMessageHandler
    {
        public int Hits;
        public HttpStatusCode Status = HttpStatusCode.OK;
        public string Body = Live;

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
        {
            Assert.Equal(Release.CatalogUrl, request.RequestUri!.ToString());
            Hits++;
            return Task.FromResult(new HttpResponseMessage(Status)
            {
                Content = new StringContent(Status == HttpStatusCode.OK ? Body : "<html>challenge</html>"),
            });
        }
    }

    private readonly Sandbox _sandbox = new();
    private readonly Site _site = new();
    private readonly string _home;
    private readonly TextWriter _stderr = Console.Error;
    private readonly StringWriter _err = new();

    public CatalogCacheTests()
    {
        _home = _sandbox.TempHome();
        _sandbox.Http(_site);
        Download.ForgetCatalog();
        Download.CatalogSpreadSeconds = 0;
        Console.SetError(_err);
    }

    public void Dispose()
    {
        Console.SetError(_stderr);
        _sandbox.Dispose();
        Download.ForgetCatalog();
        Download.CatalogSpreadSeconds = null;
    }

    private string Kept => Path.Combine(_home, ".clearcote", "catalog.json");

    private static void NewProcess() => Download.ForgetCatalog(); // only the file is left

    private void Age(double seconds)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(Kept));
        var at = doc.RootElement.GetProperty("at").GetDouble() - seconds;
        Write(at, doc.RootElement.GetProperty("url").GetString()!, doc.RootElement.GetProperty("catalog").GetRawText());
    }

    private void Write(double at, string url, string catalogJson)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Kept)!);
        File.WriteAllText(Kept, $"{{\"at\":{at.ToString("R", System.Globalization.CultureInfo.InvariantCulture)},\"url\":{JsonSerializer.Serialize(url)},\"catalog\":{catalogJson}}}");
    }

    private static double Now => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    private static void AssertLive(Catalog cat) => Assert.Equal("999.0.1.2", Assert.Single(cat.Builds).Version);

    [Fact]
    public async Task Many_launches_ask_once_and_the_answer_is_kept_in_the_home_dir()
    {
        for (var i = 0; i < 25; i++) AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(1, _site.Hits);
        using var doc = JsonDocument.Parse(File.ReadAllText(Kept));
        Assert.Equal(Release.CatalogUrl, doc.RootElement.GetProperty("url").GetString());
        Assert.Equal("999.0.1.2", doc.RootElement.GetProperty("catalog").GetProperty("builds")[0].GetProperty("version").GetString());
        Assert.InRange(doc.RootElement.GetProperty("at").GetDouble(), Now - 60, Now + 1);
    }

    [Fact]
    public async Task Another_process_uses_the_kept_file()
    {
        await Download.FetchCatalogAsync(true);
        NewProcess();
        AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(1, _site.Hits);
    }

    [Fact]
    public async Task An_answer_older_than_the_ttl_is_asked_again()
    {
        await Download.FetchCatalogAsync(true);
        Age(601);
        NewProcess();
        await Download.FetchCatalogAsync(true);
        Assert.Equal(2, _site.Hits);
    }

    [Fact]
    public async Task The_spread_of_a_process_keeps_the_answer_a_little_longer()
    {
        await Download.FetchCatalogAsync(true);
        Age(601);
        NewProcess();
        Download.CatalogSpreadSeconds = 200;
        await Download.FetchCatalogAsync(true);
        Assert.Equal(1, _site.Hits);
    }

    [Fact]
    public async Task A_403_uses_the_last_kept_answer_not_the_bundled_snapshot()
    {
        await Download.FetchCatalogAsync(true);
        Age(3600);
        NewProcess();
        _site.Status = HttpStatusCode.Forbidden;
        AssertLive(await Download.FetchCatalogAsync(false));
        Assert.Equal(2, _site.Hits);
        Assert.Contains("403", _err.ToString());
        Assert.Contains("last answer kept on this machine", _err.ToString());
    }

    [Fact]
    public async Task A_403_with_nothing_kept_uses_the_bundled_snapshot_and_keeps_nothing()
    {
        _site.Status = HttpStatusCode.Forbidden;
        Assert.Same(Release.CatalogFallback, await Download.FetchCatalogAsync(false));
        Assert.Contains("bundled snapshot", _err.ToString());
        Assert.False(File.Exists(Kept));
    }

    [Fact]
    public async Task An_answer_older_than_a_week_is_not_used_on_failure()
    {
        await Download.FetchCatalogAsync(true);
        Age(7 * 86400 + 1);
        NewProcess();
        _site.Status = HttpStatusCode.Forbidden;
        Assert.Same(Release.CatalogFallback, await Download.FetchCatalogAsync(true));
    }

    [Theory]
    [InlineData("")]
    [InlineData("{")]
    [InlineData("[]")]
    [InlineData("{\"at\":\"x\"}")]
    [InlineData("{\"at\":1,\"url\":\"u\",\"catalog\":{}}")]
    public async Task An_unreadable_file_is_a_miss(string content)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Kept)!);
        File.WriteAllText(Kept, content);
        AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(1, _site.Hits);
    }

    [Fact]
    public async Task A_file_for_another_url_or_with_a_time_ahead_of_this_clock_is_a_miss()
    {
        const string other = "{\"schema\":1,\"builds\":[{\"major\":1,\"version\":\"1.0.0.0\",\"platforms\":{}}]}";
        Write(Now, "https://example.test/versions", other);
        AssertLive(await Download.FetchCatalogAsync(true));
        NewProcess();
        Write(Now + 3600, Release.CatalogUrl, other);
        AssertLive(await Download.FetchCatalogAsync(true));
        Assert.Equal(2, _site.Hits);
    }

    [Fact]
    public async Task An_answer_without_builds_is_not_kept()
    {
        _site.Body = "{\"schema\":1,\"builds\":[]}";
        Assert.Same(Release.CatalogFallback, await Download.FetchCatalogAsync(true));
        Assert.False(File.Exists(Kept));
    }
}
