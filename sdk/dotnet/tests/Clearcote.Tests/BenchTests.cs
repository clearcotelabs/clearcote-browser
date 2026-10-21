using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// ClearcoteBench and ClearcoteHost: the summary and the printed lines are a contract shared with the Node
// and Python SDKs (test/bench.test.ts and tests/test_bench.py read the same fixture and expected lines).
public class BenchTests
{
    private static string Data(string name) => Path.Combine(AppContext.BaseDirectory, "data", name);

    private static BenchRaw Raw() => JsonSerializer.Deserialize<BenchRaw>(File.ReadAllText(Data("bench_raw.json")), ClearcoteBench.Json)!;

    private static List<string> Expected()
        => File.ReadAllText(Data("bench_lines.txt")).Replace("\r\n", "\n").TrimEnd('\n').Split('\n').ToList();

    private static HostReport Host(Action<HostReport>? edit = null)
    {
        var h = new HostReport
        {
            Cpu = { Model = "AMD Ryzen 7 5700G", Threads = 16 }, Memory = { TotalMB = 16180, FreeMB = 4096 },
            Os = "Microsoft Windows 11 Pro (10.0.26200)", Gpu = { Adapters = { "NVIDIA GeForce RTX 3070" } },
        };
        edit?.Invoke(h);
        return h;
    }

    [Fact]
    public void PrintsTheSharedFixtureLinesExactly()
        => Assert.Equal(Expected(), ClearcoteBench.Summarize(Raw()).Lines());

    [Fact]
    public void MeasuresPerBrowserFromTheBrowsersThatStarted()
    {
        var r = ClearcoteBench.Summarize(Raw());
        Assert.Equal(4, r.Launch.Started);
        Assert.Equal(5200, r.Launch.MedianMs);
        Assert.Equal(new[] { "browserType.launchPersistentContext: licence: all 4 seats are in use" }, r.Launch.Errors);
        // (46875+40000+52000+38000)/4 ms over 31.25 s = 1.415 threads of 8
        Assert.Equal(1.4, r.PerBrowser!.CpuThreads);
        Assert.Equal(17.7, r.PerBrowser.CpuPercentOfMachine);
        Assert.Equal(611, r.PerBrowser.MemMB);
        Assert.Equal(("ram", 3, 4, 3), (r.Capacity!.Limit, r.Capacity.Browsers, r.Capacity.ByCpu, r.Capacity.ByRam));
        Assert.Equal((61, 100, true), (r.Machine.CpuBefore, r.Machine.CpuDuring, r.Machine.Saturated));
    }

    [Fact]
    public void JsonUsesTheSameKeysAsTheOtherSdks()
    {
        using var doc = JsonDocument.Parse(ClearcoteBench.Summarize(Raw()).ToJson());
        Assert.Equal(new[] { "host", "url", "proxy", "headless", "requested", "concurrency", "durationSec", "launch", "pages",
            "perBrowser", "machine", "capacity", "advice" }, doc.RootElement.EnumerateObject().Select(p => p.Name));
        Assert.True(doc.RootElement.GetProperty("perBrowser").TryGetProperty("cpuPercentOfMachine", out _));
        Assert.True(doc.RootElement.GetProperty("host").GetProperty("memory").TryGetProperty("freeMB", out _));
    }

    [Fact]
    public void BlamesTheBrowsersOnlyWhenTheyCausedMostOfTheRise()
    {
        Assert.StartsWith("The CPU was full during the test: 5 browsers at once", ClearcoteBench.Summarize(Raw()).Advice[0]);
        var idle = Raw();
        foreach (var b in idle.Browsers) b.CpuMs /= 10;
        Assert.Contains("these browsers used only 7.2 % of it: other programs", ClearcoteBench.Summarize(idle).Advice[0]);
        var calm = Raw();
        calm.MachineDuring = 70;
        Assert.False(ClearcoteBench.Summarize(calm).Machine.Saturated);
    }

    [Fact]
    public void NoPageLoadedGivesNoCapacityAndSaysWhatToFix()
    {
        var raw = Raw();
        foreach (var b in raw.Browsers) { b.DclMs.Clear(); b.LoadMs.Clear(); }
        var r = ClearcoteBench.Summarize(raw);
        Assert.Null(r.Capacity);
        Assert.Null(r.PerBrowser);
        Assert.StartsWith("No page loaded through the proxy (page.goto: net::ERR_TUNNEL_CONNECTION_FAILED", r.Advice[0]);
    }

    [Fact]
    public void RoundsTiesLikeTheOtherSdks()
    {
        Assert.Equal("0.3", ClearcoteHost.Tenths(0.25));
        Assert.Equal("1.3", ClearcoteHost.Tenths(1.25));
    }

    [Fact]
    public void CapsByFreeRam()
    {
        Assert.Equal(2, ClearcoteBench.RamSafeCount(1386));
        Assert.Equal(1, ClearcoteBench.RamSafeCount(100));
        Assert.Equal(30, ClearcoteBench.RamSafeCount(16000));
    }

    [Fact]
    public void NeverPrintsProxyCredentials()
    {
        Assert.Equal("http://gw.example:7777", ClearcoteBench.ProxyLabel("http://user:pa%40ss@gw.example:7777"));
        Assert.Equal("socks5://gw.example:1080", ClearcoteBench.ProxyLabel("socks5://gw.example:1080"));
    }

    [Fact]
    public void BrowserTreeFollowsTheProfileDir()
    {
        const string d = @"C:\Temp\clearcote-bench-abc";
        var table = new List<ClearcoteBench.Proc>
        {
            new(1, 0, $"chrome.exe --user-data-dir={d} --headless", 10, 100),
            new(2, 1, "chrome.exe --type=renderer", 20, 200),
            new(3, 2, "chrome.exe --type=utility", 1, 5),
            new(4, 1, $"chrome.exe --type=gpu-process --user-data-dir={d}", 5, 50),
            new(9, 0, @"chrome.exe --user-data-dir=C:\Other", 99, 999),
            new(10, 9, "chrome.exe --type=renderer", 99, 999),
        };
        Assert.Equal(new[] { 1, 2, 3, 4 }, ClearcoteBench.BrowserTree(table, d).OrderBy(x => x));
    }

    [Fact]
    public void HostLines()
    {
        Assert.Equal(new[]
        {
            "Host            AMD Ryzen 7 5700G, 16 threads, 15.8 GB RAM (4.0 GB free)",
            "                Microsoft Windows 11 Pro (10.0.26200), physical machine",
            "GPU             NVIDIA GeForce RTX 3070",
        }, ClearcoteHost.Lines(Host()));
        Assert.Equal(new[]
        {
            "Host            AMD Ryzen 7 5700G, 16 threads, 15.8 GB RAM (4.0 GB free)",
            "                Microsoft Windows 11 Pro (10.0.26200), container",
            "GPU             none found: Chromium draws every page on the CPU",
        }, ClearcoteHost.Lines(Host(h => { h.Virtual = "container"; h.Gpu = new HostGpu { Software = true }; })));
        Assert.Equal("GPU             ASPEED Graphics Family (no 3D acceleration: Chromium draws every page on the CPU)",
            ClearcoteHost.Lines(Host(h => h.Gpu = new HostGpu { Adapters = { "ASPEED Graphics Family" }, Software = true }))[2]);
    }

    [Fact]
    public void NamesWindows11AlthoughItsProductNameSays10()
    {
        Assert.Equal("Windows 11 Pro (10.0.26200)", ClearcoteHost.WindowsName("Windows 10 Pro", "10", "0", "26200"));
        Assert.Equal("Windows 10 Home (10.0.19045)", ClearcoteHost.WindowsName("Windows 10 Home", "10", "0", "19045"));
        Assert.Equal("Windows Server 2022 Datacenter (10.0.20348)", ClearcoteHost.WindowsName("Windows Server 2022 Datacenter", "10", "0", "20348"));
        Assert.Equal("Windows (10.0.?)", ClearcoteHost.WindowsName("", "", "", ""));
    }

    [Fact]
    public void HostInfoDescribesThisMachine()
    {
        var h = ClearcoteHost.Info();
        Assert.True(h.Cpu.Threads >= 1);
        Assert.True(h.Memory.TotalMB > 0);
        Assert.False(string.IsNullOrEmpty(h.Os));
        Assert.Equal(3, ClearcoteHost.Lines(h).Count);
    }

    [Fact]
    public void HostIsReadOncePerProcessAndHandedOutAsCopies()
    {
        var a = ClearcoteHost.Info();
        a.Gpu.Adapters.Add("changed by the caller");
        a.Cpu.Model = "changed";
        var b = ClearcoteHost.Info();
        Assert.NotEqual("changed", b.Cpu.Model);
        Assert.DoesNotContain("changed by the caller", b.Gpu.Adapters);
        Assert.Equal(a.Memory.TotalMB, b.Memory.TotalMB);
        Assert.True(b.Memory.FreeMB > 0);
    }

    [Fact]
    public async Task RejectsABadConcurrency()
        => await Assert.ThrowsAsync<ArgumentException>(() => ClearcoteBench.MeasureAsync(new BenchOptions { Concurrency = 0 }));
}
