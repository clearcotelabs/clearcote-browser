using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

// What a support question needs to know about the machine, and what N browsers cost on it (ports
// bench.ts / _bench.py; the .NET SDK has no CLI, so this is an API: ClearcoteHost.Info() and
// ClearcoteBench.RunAsync()).
//
// Why: "100 % CPU with 5 threads and pages never finish" has three usual causes -- too many browsers for
// the CPU, no hardware GPU (Chromium then draws every page on the CPU), or a wait condition ('load',
// 'networkidle') that a slow proxy or a chatty page never satisfies. The bench measures all three on the
// user's own machine, page and proxy.
//
// The cost per browser is measured from the browser's own process tree (the browser process, found by the
// profile directory the bench gives it, and every process below it), not from whole-machine counters, so
// whatever else runs on the machine does not end up in the figure.
//
// Output is part of the contract: the Node and Python SDKs print the same lines from the same report
// (BenchTests reads the same fixture and expected lines as test/bench.test.ts and tests/test_bench.py).

public sealed class HostCpu { public string Model { get; set; } = "unknown"; public int Threads { get; set; } }
public sealed class HostMemory { public int TotalMB { get; set; } public int FreeMB { get; set; } }
public sealed class HostGpu { public List<string> Adapters { get; set; } = new(); public bool Software { get; set; } }

/// The machine: CPU, RAM, OS, VM or not, and whether there is a GPU Chromium can draw with.
public sealed class HostReport
{
    public HostCpu Cpu { get; set; } = new();
    public HostMemory Memory { get; set; } = new();
    public string Os { get; set; } = "";
    /// "virtual machine (VMware, Inc. VMware7,1)", "container", or null on a physical machine.
    public string? Virtual { get; set; }
    /// Windows: this is a Remote Desktop session (no GPU acceleration by default).
    public bool RemoteSession { get; set; }
    /// The display adapters the OS reports; Software when none of them can draw 3D.
    public HostGpu Gpu { get; set; } = new();
}

public static class ClearcoteHost
{
    // Adapters that cannot accelerate Chromium: Windows' fallback drivers, hypervisor and BMC display
    // chips, Mesa's CPU renderers.
    internal static readonly Regex SoftwareGpu = new(
        "basic display|basic render|remote display|hyper-v video|vmware svga|virtualbox graphics|qxl|cirrus|bochs|llvmpipe|softpipe|swiftshader|virtio|standard vga|aspeed|matrox|parsec virtual|idd",
        RegexOptions.IgnoreCase);
    internal static readonly Regex VirtualHw = new(
        "vmware|virtualbox|qemu|kvm|xen|virtual machine|hvm domu|amazon ec2|google compute|droplet|openstack|bochs|parallels|hetzner|vultr|linode",
        RegexOptions.IgnoreCase);

    internal const string WindowsDisplayClass = @"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}";

    internal static string Run(string file, IEnumerable<string> args, int timeoutMs = 15000)
    {
        try
        {
            var psi = new ProcessStartInfo(file) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
            foreach (var a in args) psi.ArgumentList.Add(a);
            using var p = Process.Start(psi)!;
            var stdout = p.StandardOutput.ReadToEndAsync();
            _ = p.StandardError.ReadToEndAsync();
            if (!p.WaitForExit(timeoutMs)) { try { p.Kill(true); } catch { } return ""; }
            return p.ExitCode == 0 ? stdout.Result : "";
        }
        catch { return ""; }
    }

    private static string Read(string path) { try { return File.ReadAllText(path); } catch { return ""; } }

    /// `reg query` values as (name, data). The registry, not WMI: PowerShell's Get-CimInstance takes
    /// 1-2 s to start, reg.exe answers in ~50 ms.
    private static List<(string Name, string Value)> RegValues(string key, params string[] args)
        => Run("reg", new[] { "query", key }.Concat(args)).Split('\n')
            .Select(l => Regex.Match(l.TrimEnd('\r'), @"^\s+(\S+)\s+REG_\w+\s+(.*)$"))
            .Where(m => m.Success).Select(m => (m.Groups[1].Value, m.Groups[2].Value.Trim())).ToList();

    /// A REG_DWORD as reg.exe prints it ("0xa") in decimal, or "" when missing.
    private static string Dword(string? v)
        => v is not null && Regex.IsMatch(v, "^0x[0-9a-fA-F]+$") ? Convert.ToInt64(v[2..], 16).ToString(CultureInfo.InvariantCulture) : "";

    /// "Windows 11 Pro (10.0.26200)": ProductName still says "Windows 10" on Windows 11, whose builds
    /// start at 22000.
    public static string WindowsName(string product, string major, string minor, string build)
    {
        var name = int.TryParse(build, out var b) && b >= 22000 ? new Regex("Windows 10").Replace(product, "Windows 11", 1) : product;
        return $"{(name.Length > 0 ? name : "Windows")} ({(major.Length > 0 ? major : "10")}.{(minor.Length > 0 ? minor : "0")}.{(build.Length > 0 ? build : "?")})";
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MemoryStatusEx
    {
        public uint Length, MemoryLoad;
        public ulong TotalPhys, AvailPhys, TotalPageFile, AvailPageFile, TotalVirtual, AvailVirtual, AvailExtendedVirtual;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GlobalMemoryStatusEx(ref MemoryStatusEx status);

    internal static int HalfUp(double x) => (int)Math.Floor(x + 0.5);

    private static HostReport Windows()
    {
        Dictionary<string, string> Dict(List<(string Name, string Value)> v) => v.GroupBy(x => x.Name).ToDictionary(g => g.Key, g => g.First().Value);
        var nt = Dict(RegValues(@"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion"));
        var bios = Dict(RegValues(@"HKLM\HARDWARE\DESCRIPTION\System\BIOS"));
        var cpu = Dict(RegValues(@"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0"));
        var gpu = RegValues(WindowsDisplayClass, "/s", "/v", "DriverDesc").Select(x => x.Value).Where(v => v.Length > 0).Distinct().ToList();
        var hw = $"{bios.GetValueOrDefault("SystemManufacturer", "")} {bios.GetValueOrDefault("SystemProductName", "")}".Trim();
        // GlobalMemoryStatusEx's total and available physical memory (what Node's os.totalmem/freemem report).
        var m = new MemoryStatusEx { Length = (uint)Marshal.SizeOf<MemoryStatusEx>() };
        if (!GlobalMemoryStatusEx(ref m)) m = default;
        return new HostReport
        {
            Cpu = { Model = (cpu.GetValueOrDefault("ProcessorNameString") ?? "unknown").Trim(), Threads = Environment.ProcessorCount },
            Memory = { TotalMB = HalfUp(m.TotalPhys / 1048576.0), FreeMB = HalfUp(m.AvailPhys / 1048576.0) },
            Os = WindowsName(nt.GetValueOrDefault("ProductName", ""), Dword(nt.GetValueOrDefault("CurrentMajorVersionNumber")),
                Dword(nt.GetValueOrDefault("CurrentMinorVersionNumber")), nt.GetValueOrDefault("CurrentBuild", "")),
            Virtual = VirtualHw.IsMatch(hw) ? $"virtual machine ({hw})" : null,
            RemoteSession = (Environment.GetEnvironmentVariable("SESSIONNAME") ?? "").StartsWith("rdp-", StringComparison.OrdinalIgnoreCase),
            Gpu = { Adapters = gpu, Software = !gpu.Any(g => !SoftwareGpu.IsMatch(g)) },
        };
    }

    private static HostReport Linux()
    {
        var mem = Read("/proc/meminfo");
        long Kb(string k) { var m = Regex.Match(mem, $@"^{k}:\s+(\d+)", RegexOptions.Multiline); return m.Success ? long.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture) : 0; }
        var model = Regex.Match(Read("/proc/cpuinfo"), @"^model name\s*:\s*(.+)$", RegexOptions.Multiline);
        var pretty = Regex.Match(Read("/etc/os-release"), "^PRETTY_NAME=\"?([^\"\\n]*)\"?", RegexOptions.Multiline);
        var hw = $"{Read("/sys/class/dmi/id/sys_vendor").Trim()} {Read("/sys/class/dmi/id/product_name").Trim()}".Trim();
        var container = File.Exists("/.dockerenv") || File.Exists("/run/.containerenv");
        var adapters = Run("lspci", Array.Empty<string>()).Split('\n')
            .Where(l => Regex.IsMatch(l, "vga|3d controller|display controller", RegexOptions.IgnoreCase))
            .Select(l => Regex.Replace(l, @"^\S+\s+[^:]+:\s*", "").Trim()).ToList();
        bool dri;
        try { dri = Directory.Exists("/dev/dri") && Directory.GetFileSystemEntries("/dev/dri").Any(n => Path.GetFileName(n).StartsWith("renderD")); }
        catch { dri = false; }
        if (adapters.Count == 0 && dri) adapters.Add("render node in /dev/dri");
        return new HostReport
        {
            Cpu = { Model = model.Success ? model.Groups[1].Value.Trim() : "unknown", Threads = Environment.ProcessorCount },
            Memory = { TotalMB = HalfUp(Kb("MemTotal") / 1024.0), FreeMB = HalfUp(Kb("MemAvailable") / 1024.0) },
            Os = $"{(pretty.Success ? pretty.Groups[1].Value : "Linux")} (kernel {Read("/proc/sys/kernel/osrelease").Trim()})",
            Virtual = container ? "container" : VirtualHw.IsMatch(hw) ? $"virtual machine ({hw})" : null,
            Gpu = { Adapters = adapters, Software = !dri || !adapters.Any(g => !SoftwareGpu.IsMatch(g)) },
        };
    }

    private static HostReport Mac()
    {
        string Sysctl(string k) => Run("sysctl", new[] { "-n", k }).Trim();
        var adapters = Regex.Matches(Run("system_profiler", new[] { "SPDisplaysDataType" }, 30000), @"Chipset Model:\s*(.+)")
            .Select(m => m.Groups[1].Value.Trim()).ToList();
        // Inactive and speculative pages are free for the taking too, not only "Pages free".
        var vm = Run("vm_stat", Array.Empty<string>());
        var page = Regex.Match(vm, @"page size of (\d+)");
        long free = 0;
        foreach (var k in new[] { "Pages free", "Pages inactive", "Pages speculative" })
        {
            var m = Regex.Match(vm, $@"{k}:\s+(\d+)");
            if (m.Success) free += long.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture);
        }
        long.TryParse(Sysctl("hw.memsize"), out var total);
        var ver = Run("sw_vers", new[] { "-productVersion" }).Trim();
        return new HostReport
        {
            Cpu = { Model = Sysctl("machdep.cpu.brand_string") is { Length: > 0 } b ? b : "unknown", Threads = Environment.ProcessorCount },
            Memory = { TotalMB = HalfUp(total / 1048576.0), FreeMB = HalfUp(free * (page.Success ? long.Parse(page.Groups[1].Value) : 4096) / 1048576.0) },
            Os = $"macOS {(ver.Length > 0 ? ver : Environment.OSVersion.Version.ToString())}",
            Virtual = Sysctl("kern.hv_vmm_present") == "1" ? "virtual machine" : null,
            Gpu = { Adapters = adapters, Software = !adapters.Any(g => !SoftwareGpu.IsMatch(g)) },
        };
    }

    /// The machine: CPU, RAM, OS, VM or not, and whether there is a GPU Chromium can draw with. Local only.
    public static HostReport Info()
    {
        // The CPU, OS, VM and GPU do not change while a process runs, and reading them spawns reg / lspci /
        // system_profiler: read them once. Free memory changes, and is read again on every call.
        var first = _once is null;
        var h = _once ??= OperatingSystem.IsWindows() ? Windows() : OperatingSystem.IsMacOS() ? Mac() : Linux();
        return new HostReport
        {
            Cpu = { Model = h.Cpu.Model, Threads = h.Cpu.Threads },
            Memory = { TotalMB = h.Memory.TotalMB, FreeMB = first ? h.Memory.FreeMB : FreeMB() },
            Os = h.Os, Virtual = h.Virtual, RemoteSession = h.RemoteSession,
            Gpu = { Adapters = new List<string>(h.Gpu.Adapters), Software = h.Gpu.Software },
        };
    }

    private static HostReport? _once;

    private static int FreeMB()
    {
        if (OperatingSystem.IsWindows())
        {
            var m = new MemoryStatusEx { Length = (uint)Marshal.SizeOf<MemoryStatusEx>() };
            return GlobalMemoryStatusEx(ref m) ? HalfUp(m.AvailPhys / 1048576.0) : 0;
        }
        if (OperatingSystem.IsMacOS()) return Mac().Memory.FreeMB;
        var kb = Regex.Match(Read("/proc/meminfo"), @"^MemAvailable:\s+(\d+)", RegexOptions.Multiline);
        return kb.Success ? HalfUp(long.Parse(kb.Groups[1].Value, CultureInfo.InvariantCulture) / 1024.0) : 0;
    }

    // Every figure is rounded half-up before it is formatted, as the Node and Python SDKs do (their
    // formatters round ties differently, so all three round first).
    internal static string Tenths(double x) => (Math.Floor(x * 10 + 0.5) / 10).ToString("0.0", CultureInfo.InvariantCulture);
    private static string Gb(int mb) => Tenths(mb / 1024.0);

    /// The Host lines (the same text the Node and Python `clearcote info` print).
    public static List<string> Lines(HostReport h)
    {
        var lines = new List<string>
        {
            $"Host            {h.Cpu.Model}, {h.Cpu.Threads} threads, {Gb(h.Memory.TotalMB)} GB RAM ({Gb(h.Memory.FreeMB)} GB free)",
            $"                {h.Os}, {h.Virtual ?? "physical machine"}{(h.RemoteSession ? ", Remote Desktop session" : "")}",
        };
        if (h.Gpu.Adapters.Count == 0) lines.Add("GPU             none found: Chromium draws every page on the CPU");
        else if (h.Gpu.Software) lines.Add($"GPU             {string.Join(", ", h.Gpu.Adapters)} (no 3D acceleration: Chromium draws every page on the CPU)");
        else lines.Add($"GPU             {string.Join(", ", h.Gpu.Adapters)}");
        return lines;
    }
}

// ── the run ─────────────────────────────────────────────────────────────────────────────────────────

public sealed class BenchOptions
{
    /// Browsers at once (capped by free RAM unless <see cref="Force"/>).
    public int Concurrency { get; set; } = 3;
    /// The page every browser loads over and over.
    public string Url { get; set; } = "https://en.wikipedia.org/wiki/Web_browser";
    /// How long to keep loading, in seconds.
    public int DurationSec { get; set; } = 30;
    public bool Headless { get; set; } = true;
    /// A proxy URL such as http://user:pass@host:8080 (credentials are never printed).
    public string? Proxy { get; set; }
    /// Run more browsers than free RAM looks able to hold.
    public bool Force { get; set; }
}

/// What one browser did: every navigation, what it cost.
public sealed class BrowserRun
{
    public long? LaunchMs { get; set; }
    public string? LaunchError { get; set; }
    public List<long> DclMs { get; set; } = new();
    public List<long> LoadMs { get; set; } = new();
    public int LoadTimeouts { get; set; }
    public int IdleReached { get; set; }
    public int IdleMissed { get; set; }
    public List<string> Errors { get; set; } = new();
    public double CpuMs { get; set; }
    public double PeakMemMB { get; set; }
}

/// Everything the bench measured, before any summary (also what the tests feed Summarize).
public sealed class BenchRaw
{
    public HostReport Host { get; set; } = new();
    public string Url { get; set; } = "";
    public string? Proxy { get; set; }
    public bool Headless { get; set; }
    public int Requested { get; set; }
    public int Concurrency { get; set; }
    public int DurationSec { get; set; }
    /// Wall time the browsers were measured over.
    public double MeasuredSec { get; set; }
    public List<BrowserRun> Browsers { get; set; } = new();
    /// Whole-machine CPU busy % in the 3 s before launching and during the measured window (null where
    /// the platform does not say).
    public double? MachineBefore { get; set; }
    public double? MachineDuring { get; set; }
}

public sealed class BenchLaunch { public int Started { get; set; } public long? MedianMs { get; set; } public List<string> Errors { get; set; } = new(); }
public sealed class BenchError { public int Count { get; set; } public string Error { get; set; } = ""; }
public sealed class BenchPages
{
    public int Loads { get; set; }
    public long? DclMedianMs { get; set; }
    public long? DclP95Ms { get; set; }
    public long? LoadMedianMs { get; set; }
    public long? LoadP95Ms { get; set; }
    public int LoadTimeouts { get; set; }
    public int IdleReached { get; set; }
    public int IdleMissed { get; set; }
    public List<BenchError> Errors { get; set; } = new();
}
public sealed class BenchPerBrowser { public double CpuThreads { get; set; } public double CpuPercentOfMachine { get; set; } public int MemMB { get; set; } }
public sealed class BenchMachine { public int? CpuBefore { get; set; } public int? CpuDuring { get; set; } public bool Saturated { get; set; } }
public sealed class BenchCapacity { public int Browsers { get; set; } public string Limit { get; set; } = "cpu"; public int ByCpu { get; set; } public int ByRam { get; set; } }

/// The figures and advice of a bench run (the same JSON shape as the Node and Python reports).
public sealed class BenchReport
{
    public HostReport Host { get; set; } = new();
    public string Url { get; set; } = "";
    public string? Proxy { get; set; }
    public bool Headless { get; set; }
    public int Requested { get; set; }
    public int Concurrency { get; set; }
    public int DurationSec { get; set; }
    public BenchLaunch Launch { get; set; } = new();
    public BenchPages Pages { get; set; } = new();
    public BenchPerBrowser? PerBrowser { get; set; }
    public BenchMachine Machine { get; set; } = new();
    public BenchCapacity? Capacity { get; set; }
    public List<string> Advice { get; set; } = new();

    /// What the Node/Python `clearcote bench` prints.
    public List<string> Lines() => ClearcoteBench.Lines(this);

    /// The report as JSON (camelCase keys, as the other SDKs' --json).
    public string ToJson() => JsonSerializer.Serialize(this, ClearcoteBench.Json);
}

public static class ClearcoteBench
{
    internal static readonly JsonSerializerOptions Json = new() { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, WriteIndented = true };

    internal sealed record Proc(int Pid, int Ppid, string Cmd, double CpuMs, double MemMB);

    /// Every process of the browser executable (Windows) or every process (elsewhere): pid, parent,
    /// command line, CPU time used so far (ms), and private memory (MB; Windows commit, Linux PSS, macOS RSS).
    internal static List<Proc> ProcessTable(string exeName = "chrome.exe")
    {
        var output = new List<Proc>();
        if (OperatingSystem.IsWindows())
        {
            var ps = $"Get-CimInstance Win32_Process -Filter \"Name='{exeName.Replace("'", "")}'\"|ForEach-Object{{" +
                     "\"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.UserModeTime+$_.KernelModeTime)`t$($_.PrivatePageCount)`t$($_.CommandLine)\"}";
            foreach (var line in ClearcoteHost.Run("powershell", new[] { "-NoProfile", "-NonInteractive", "-Command", ps }).Split('\n'))
            {
                var f = line.TrimEnd('\r').Split('\t', 5);
                if (f.Length < 5 || !int.TryParse(f[0], out var pid) || !int.TryParse(f[1], out var ppid)) continue;
                double.TryParse(f[2], NumberStyles.Float, CultureInfo.InvariantCulture, out var t100ns);
                double.TryParse(f[3], NumberStyles.Float, CultureInfo.InvariantCulture, out var bytes);
                output.Add(new Proc(pid, ppid, f[4], t100ns / 1e4, bytes / 1048576.0));
            }
            return output;
        }
        if (OperatingSystem.IsMacOS())
        {
            foreach (var line in ClearcoteHost.Run("ps", new[] { "-axo", "pid=,ppid=,time=,rss=,command=" }).Split('\n'))
            {
                var m = Regex.Match(line, @"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(.*)$");
                if (!m.Success) continue;
                // [[dd-]hh:]mm:ss.ss
                var t = m.Groups[3].Value.Split('-', ':').Select(x => double.Parse(x, CultureInfo.InvariantCulture)).Reverse().ToList();
                double At(int i) => i < t.Count ? t[i] : 0;
                var secs = At(0) + At(1) * 60 + At(2) * 3600 + At(3) * 86400;
                output.Add(new Proc(int.Parse(m.Groups[1].Value), int.Parse(m.Groups[2].Value), m.Groups[5].Value, secs * 1000,
                    double.Parse(m.Groups[4].Value, CultureInfo.InvariantCulture) / 1024));
            }
            return output;
        }
        foreach (var dir in Directory.EnumerateDirectories("/proc"))
        {
            var name = Path.GetFileName(dir);
            if (!int.TryParse(name, out var pid)) continue;
            string stat;
            try { stat = File.ReadAllText($"/proc/{name}/stat"); } catch { continue; }
            var f = stat[(stat.LastIndexOf(')') + 2)..].Split(' ');
            if (f.Length < 13) continue;
            string Read(string p) { try { return File.ReadAllText(p); } catch { return ""; } }
            var pss = Regex.Match(Read($"/proc/{name}/smaps_rollup"), @"^Pss:\s+(\d+)", RegexOptions.Multiline);
            var statm = Read($"/proc/{name}/statm").Split(' ');
            double kb = pss.Success ? double.Parse(pss.Groups[1].Value, CultureInfo.InvariantCulture)
                : statm.Length > 1 && double.TryParse(statm[1], out var pages) ? pages * 4 : 0;
            output.Add(new Proc(pid, int.Parse(f[1]), Read($"/proc/{name}/cmdline").Replace('\0', ' '),
                (double.Parse(f[11]) + double.Parse(f[12])) * 10, kb / 1024));
        }
        return output;
    }

    /// The pids of the browser started on <paramref name="profileDir"/> and of every process below it.
    internal static HashSet<int> BrowserTree(IReadOnlyList<Proc> table, string profileDir)
    {
        var tree = table.Where(p => p.Cmd.Contains(profileDir) && !p.Cmd.Contains("--type=")).Select(p => p.Pid).ToHashSet();
        for (var grew = true; grew;)
        {
            grew = false;
            foreach (var p in table) if (!tree.Contains(p.Pid) && tree.Contains(p.Ppid)) { tree.Add(p.Pid); grew = true; }
        }
        return tree;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetSystemTimes(out long idle, out long kernel, out long user);

    /// (idle, total) CPU time of the whole machine so far, or null where the platform does not say.
    private static (double Idle, double Total)? MachineTimes()
    {
        if (OperatingSystem.IsWindows())
            return GetSystemTimes(out var idle, out var kernel, out var user) ? (idle, kernel + user) : null; // kernel includes idle
        try
        {
            var f = File.ReadLines("/proc/stat").First().Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (f[0] != "cpu") return null;
            var v = f.Skip(1).Take(7).Select(x => double.Parse(x, CultureInfo.InvariantCulture)).ToArray();
            return (v[3], v[0] + v[1] + v[2] + v[3] + v[5] + v[6]); // user nice system idle (iowait left out) irq softirq
        }
        catch { return null; }
    }

    private static double? Busy((double Idle, double Total)? a, (double Idle, double Total)? b)
        => a is null || b is null || b.Value.Total == a.Value.Total ? null : 100 * (1 - (b.Value.Idle - a.Value.Idle) / (b.Value.Total - a.Value.Total));

    /// The browsers free RAM looks able to hold before anything is measured (450 MB each, 15 % kept back).
    public static int RamSafeCount(int freeMB) => Math.Max(1, (int)Math.Floor(freeMB * 0.85 / 450));

    /// Strip credentials: http://host:8080.
    public static string ProxyLabel(string server) => Regex.Replace(server, "//[^@/]*@", "//");

    private static string FirstLine(Exception e)
    {
        var msg = (e.Message ?? e.GetType().Name).Split('\n')[0].TrimEnd('\r');
        return Regex.Replace(msg, @"\s+=+\s*logs\s*=+.*$", "", RegexOptions.IgnoreCase).Trim();
    }

    /// Start the browsers, load the page in each for the duration, and summarise what they cost on this
    /// machine. Every browser holds a licence seat while it runs; profiles are temporary and deleted
    /// afterwards. <paramref name="progress"/> gets one line per stage.
    public static async Task<BenchReport> RunAsync(BenchOptions? options = null, Action<string>? progress = null)
        => Summarize(await MeasureAsync(options ?? new BenchOptions(), progress));

    /// The raw measurements behind <see cref="RunAsync"/>.
    public static async Task<BenchRaw> MeasureAsync(BenchOptions o, Action<string>? progress = null,
        Func<string, LaunchOptions, Task<IBrowserContext>>? launch = null)
    {
        progress ??= _ => { };
        launch ??= Clearcote.LaunchPersistentContextAsync;
        if (o.Concurrency < 1) throw new ArgumentException("Concurrency must be at least 1");
        if (o.DurationSec < 1) throw new ArgumentException("DurationSec must be at least 1");
        var proxy = ProxySpec.From(o.Proxy);
        var host = ClearcoteHost.Info();
        var n = o.Force ? o.Concurrency : Math.Min(o.Concurrency, RamSafeCount(host.Memory.FreeMB));
        if (n < o.Concurrency) progress($"only {host.Memory.FreeMB} MB RAM free: running {n} browsers instead of {o.Concurrency} (Force to run them all)");

        var t0 = MachineTimes();
        await Task.Delay(3000);
        var machineBefore = Busy(t0, MachineTimes());

        var exe = Path.GetFileName(Environment.GetEnvironmentVariable("CLEARCOTE_BINARY") ?? "") is { Length: > 0 } e ? e
            : OperatingSystem.IsWindows() ? "chrome.exe" : "chrome";
        var dirs = Enumerable.Range(0, n).Select(_ => Directory.CreateTempSubdirectory("clearcote-bench-").FullName).ToList();
        var runs = dirs.Select(_ => new BrowserRun()).ToList();
        var contexts = new IBrowserContext?[n];
        progress($"starting {n} browser{(n == 1 ? "" : "s")}...");
        await Task.WhenAll(Enumerable.Range(0, n).Select(async i =>
        {
            var sw = Stopwatch.StartNew();
            try
            {
                contexts[i] = await launch(dirs[i], new LaunchOptions
                {
                    Headless = o.Headless, Quiet = true, Fingerprint = $"clearcote-bench-{i}",
                    // Cloud = false: this measures THIS machine, whatever CLEARCOTE_CLOUD says.
                    Cloud = false,
                    Proxy = proxy is null ? null : new ProxyOptions { Server = proxy.ServerString, Username = proxy.Username, Password = proxy.Password },
                });
                runs[i].LaunchMs = sw.ElapsedMilliseconds;
            }
            catch (Exception ex) { runs[i].LaunchError = FirstLine(ex); }
        }));

        // CPU time per pid, first and latest seen: a renderer that exits mid-run keeps the time it used.
        var first = new Dictionary<int, double>();
        var latest = new Dictionary<int, double>();
        var owner = new Dictionary<int, int>();
        var gate = new object();
        void Sample(bool initial)
        {
            var table = ProcessTable(exe);
            var byPid = table.GroupBy(p => p.Pid).ToDictionary(g => g.Key, g => g.First());
            lock (gate)
            {
                for (var i = 0; i < n; i++)
                {
                    if (contexts[i] is null) continue;
                    double mem = 0;
                    foreach (var pid in BrowserTree(table, dirs[i]))
                    {
                        var p = byPid[pid];
                        owner[pid] = i;
                        if (!first.ContainsKey(pid)) first[pid] = initial ? p.CpuMs : 0;
                        latest[pid] = p.CpuMs;
                        mem += p.MemMB;
                    }
                    runs[i].PeakMemMB = Math.Max(runs[i].PeakMemMB, mem);
                }
            }
        }

        progress($"loading {o.Url} for {o.DurationSec} s...");
        await Task.Run(() => Sample(true));
        var m0 = MachineTimes();
        var clock = Stopwatch.StartNew();
        var end = TimeSpan.FromSeconds(o.DurationSec);
        using var stop = new CancellationTokenSource();
        var sampler = Task.Run(async () =>
        {
            while (!stop.IsCancellationRequested)
            {
                try { await Task.Delay(4000, stop.Token); } catch (TaskCanceledException) { break; }
                Sample(false);
            }
        });
        await Task.WhenAll(Enumerable.Range(0, n).Select(async i =>
        {
            var ctx = contexts[i];
            var r = runs[i];
            if (ctx is null) return;
            IPage page;
            try { page = ctx.Pages.Count > 0 ? ctx.Pages[0] : await ctx.NewPageAsync(); }
            catch (Exception ex) { r.Errors.Add(FirstLine(ex)); return; }
            while (clock.Elapsed < end)
            {
                var t = Stopwatch.StartNew();
                try
                {
                    await page.GotoAsync(o.Url, new() { WaitUntil = WaitUntilState.DOMContentLoaded, Timeout = 60000 });
                    r.DclMs.Add(t.ElapsedMilliseconds);
                }
                catch (Exception ex)
                {
                    r.Errors.Add(FirstLine(ex));
                    // A dead proxy fails in milliseconds: retrying at once would measure a busy loop, not a browser.
                    await Task.Delay(1000);
                    continue;
                }
                try { await page.WaitForLoadStateAsync(LoadState.Load, new() { Timeout = 30000 }); r.LoadMs.Add(t.ElapsedMilliseconds); }
                catch { r.LoadTimeouts++; }
                try { await page.WaitForLoadStateAsync(LoadState.NetworkIdle, new() { Timeout = 5000 }); r.IdleReached++; }
                catch { r.IdleMissed++; }
            }
        }));
        var measured = clock.Elapsed.TotalSeconds;
        Sample(false);
        var m1 = MachineTimes();
        stop.Cancel();
        await sampler;
        foreach (var (pid, i) in owner) runs[i].CpuMs += latest.GetValueOrDefault(pid) - first.GetValueOrDefault(pid);

        foreach (var c in contexts) if (c is not null) { try { await c.CloseAsync(); } catch { } }
        foreach (var d in dirs)
            for (var k = 0; k < 10 && Directory.Exists(d); k++)
            {
                try { Directory.Delete(d, true); } catch { await Task.Delay(500); }
            }

        return new BenchRaw
        {
            Host = host, Url = o.Url, Proxy = proxy is null ? null : ProxyLabel(proxy.ServerString), Headless = o.Headless,
            Requested = o.Concurrency, Concurrency = n, DurationSec = o.DurationSec, MeasuredSec = measured, Browsers = runs,
            MachineBefore = machineBefore, MachineDuring = Busy(m0, m1),
        };
    }

    // ── the summary ─────────────────────────────────────────────────────────────────────────────────

    private static long? Pct(List<long> xs, double p)
    {
        if (xs.Count == 0) return null;
        var s = xs.OrderBy(x => x).ToList();
        return s[Math.Min(s.Count - 1, (int)Math.Floor(p * s.Count))];
    }

    private static double Round1(double x) => Math.Floor(x * 10 + 0.5) / 10;

    /// The figures and advice of a bench run. Pure: the same raw run gives the same report in all three SDKs.
    public static BenchReport Summarize(BenchRaw raw)
    {
        var ok = raw.Browsers.Where(b => b.LaunchMs is not null).ToList();
        var dcl = ok.SelectMany(b => b.DclMs).ToList();
        var load = ok.SelectMany(b => b.LoadMs).ToList();
        var counts = new Dictionary<string, int>();
        foreach (var e in ok.SelectMany(b => b.Errors)) counts[e] = counts.GetValueOrDefault(e) + 1;
        var pages = new BenchPages
        {
            Loads = dcl.Count,
            DclMedianMs = Pct(dcl, 0.5), DclP95Ms = Pct(dcl, 0.95),
            LoadMedianMs = Pct(load, 0.5), LoadP95Ms = Pct(load, 0.95),
            LoadTimeouts = ok.Sum(b => b.LoadTimeouts), IdleReached = ok.Sum(b => b.IdleReached), IdleMissed = ok.Sum(b => b.IdleMissed),
            Errors = counts.OrderByDescending(kv => kv.Value).ThenBy(kv => kv.Key, StringComparer.Ordinal)
                .Select(kv => new BenchError { Count = kv.Value, Error = kv.Key }).ToList(),
        };
        var threads = raw.Host.Cpu.Threads;
        BenchPerBrowser? perBrowser = null;
        BenchCapacity? capacity = null;
        if (ok.Count > 0 && raw.MeasuredSec > 0 && dcl.Count > 0)
        {
            var cores = ok.Sum(b => b.CpuMs) / ok.Count / 1000 / raw.MeasuredSec;
            var mem = ok.Sum(b => b.PeakMemMB) / ok.Count;
            perBrowser = new BenchPerBrowser { CpuThreads = Round1(cores), CpuPercentOfMachine = Round1(100 * cores / threads), MemMB = ClearcoteHost.HalfUp(mem) };
            // 80 % of the CPU and 85 % of the RAM that was free before the bench started: headroom for the
            // program driving the browsers and for the machine itself.
            var byCpu = cores > 0 ? Math.Max(1, (int)Math.Floor(0.8 * threads / cores)) : 999;
            var byRam = mem > 0 ? Math.Max(1, (int)Math.Floor(0.85 * raw.Host.Memory.FreeMB / mem)) : 999;
            capacity = new BenchCapacity { Browsers = Math.Min(byCpu, byRam), Limit = byCpu <= byRam ? "cpu" : "ram", ByCpu = byCpu, ByRam = byRam };
        }
        var saturated = (raw.MachineDuring ?? 0) >= 90;
        var advice = new List<string>();
        if (ok.Count > 0 && pages.Loads == 0)
        {
            // Nothing to measure: what a browser costs means nothing until a page loads.
            var firstError = pages.Errors.Count > 0 ? pages.Errors[0].Error : "no answer";
            advice.Add(raw.Proxy is not null
                ? $"No page loaded through the proxy ({firstError}): check the proxy URL and credentials, and that it answers from this machine. Run bench again once it does."
                : $"No page loaded ({firstError}): check the URL opens from this machine. Run bench again once it does.");
        }
        if (saturated)
        {
            // Whose load filled it: the browsers' own trees, or something else that started during the test?
            var browsersPct = perBrowser is not null ? perBrowser.CpuPercentOfMachine * ok.Count : 0;
            var rise = (raw.MachineDuring ?? 0) - (raw.MachineBefore ?? 0);
            advice.Add(browsersPct >= 0.5 * rise
                ? $"The CPU was full during the test: {raw.Concurrency} browsers at once is more than this machine runs smoothly; pages slow down and time out once it is."
                : $"The CPU was full during the test, but these browsers used only {ClearcoteHost.Tenths(browsersPct)} % of it: other programs on this machine took the rest, and every browser slows down once it is full.");
        }
        if (raw.Host.Gpu.Software)
            advice.Add("No GPU Chromium can use: every page is drawn on the CPU, so each browser costs more CPU. Run headless and block images/media you do not need.");
        if (raw.Host.RemoteSession) advice.Add("This is a Remote Desktop session: headed browsers here get no GPU acceleration.");
        if (!raw.Headless) advice.Add("Headed browsers draw every frame on screen and cost more CPU than headless ones.");
        if (pages.Loads > 0 && (double)pages.LoadTimeouts / pages.Loads > 0.1)
            advice.Add($"{pages.LoadTimeouts} of {pages.Loads} pages never fired 'load' within 30 s: wait with waitUntil 'domcontentloaded' and then for the element you need, not for 'load'.");
        var idleTotal = pages.IdleReached + pages.IdleMissed;
        if (idleTotal > 0 && (double)pages.IdleMissed / idleTotal > 0.5)
            advice.Add("This page rarely goes network-idle: never wait for 'networkidle' on it; wait for the element you need.");
        if (pages.Errors.Count > 0 && raw.Proxy is not null && pages.Loads > 0)
            advice.Add("Some navigations failed through the proxy: check it answers from this machine, and use sticky sessions (one exit IP per browser) rather than a new IP per request.");
        if (capacity is not null)
        {
            var s = capacity.Browsers == 1 ? "" : "s";
            advice.Add(capacity.Limit == "cpu"
                ? $"At this pace this machine runs about {capacity.Browsers} browser{s} at once (CPU is the limit). One browser with several contexts costs less than one browser per thread."
                : $"At this pace this machine runs about {capacity.Browsers} browser{s} at once (free RAM is the limit).");
        }
        return new BenchReport
        {
            Host = raw.Host, Url = raw.Url, Proxy = raw.Proxy, Headless = raw.Headless, Requested = raw.Requested,
            Concurrency = raw.Concurrency, DurationSec = raw.DurationSec,
            Launch = new BenchLaunch
            {
                Started = ok.Count, MedianMs = Pct(ok.Select(b => b.LaunchMs!.Value).ToList(), 0.5),
                Errors = raw.Browsers.Where(b => b.LaunchError is not null).Select(b => b.LaunchError!).ToList(),
            },
            Pages = pages, PerBrowser = perBrowser,
            Machine = new BenchMachine
            {
                CpuBefore = raw.MachineBefore is { } mb ? ClearcoteHost.HalfUp(mb) : null,
                CpuDuring = raw.MachineDuring is { } md ? ClearcoteHost.HalfUp(md) : null,
                Saturated = saturated,
            },
            Capacity = capacity, Advice = advice,
        };
    }

    private static string Secs(long? ms) => ms is null ? "-" : $"{ClearcoteHost.Tenths(ms.Value / 1000.0)} s";

    /// What the Node/Python `clearcote bench` prints.
    public static List<string> Lines(BenchReport r)
    {
        var lines = ClearcoteHost.Lines(r.Host);
        lines.Add($"Bench           {r.Concurrency} browser{(r.Concurrency == 1 ? "" : "s")} for {r.DurationSec} s on {r.Url} ({(r.Headless ? "headless" : "headed")}{(r.Proxy is not null ? $", proxy {r.Proxy}" : ", no proxy")})");
        if (r.Concurrency < r.Requested) lines.Add($"                {r.Requested} asked for; capped by free RAM (--force runs them all)");
        lines.Add($"Launch          {r.Launch.Started} of {r.Concurrency} started{(r.Launch.MedianMs is null ? "" : $", median {Secs(r.Launch.MedianMs)}")}");
        foreach (var e in r.Launch.Errors.Distinct()) lines.Add($"                FAILED: {e}");
        var p = r.Pages;
        lines.Add($"Pages           {p.Loads} loaded: DOMContentLoaded median {Secs(p.DclMedianMs)} (p95 {Secs(p.DclP95Ms)}), load median {Secs(p.LoadMedianMs)} (p95 {Secs(p.LoadP95Ms)})");
        if (p.LoadTimeouts > 0) lines.Add($"                'load' not reached within 30 s: {p.LoadTimeouts}");
        if (p.IdleReached + p.IdleMissed > 0) lines.Add($"                network idle within 5 s: {p.IdleReached} of {p.IdleReached + p.IdleMissed}");
        foreach (var e in p.Errors.Take(5)) lines.Add($"Errors          {e.Count} x {e.Error}");
        if (r.PerBrowser is { } pb)
            lines.Add($"Per browser     {ClearcoteHost.Tenths(pb.CpuThreads)} CPU threads ({ClearcoteHost.Tenths(pb.CpuPercentOfMachine)} % of this machine), {pb.MemMB} MB RAM");
        var m = r.Machine;
        if (m.CpuDuring is not null) lines.Add($"Machine CPU     {m.CpuDuring} % busy during the test ({m.CpuBefore} % before it){(m.Saturated ? " -- FULL" : "")}");
        if (r.Capacity is { } c)
            lines.Add($"Capacity        about {c.Browsers} browser{(c.Browsers == 1 ? "" : "s")} at once at this pace (CPU allows {c.ByCpu}, free RAM {c.ByRam})");
        foreach (var a in r.Advice) lines.Add($"Advice          {a}");
        return lines;
    }
}
