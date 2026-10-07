using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.ExceptionServices;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// <see cref="Clearcote.LaunchAsync"/> needs Docker on this host (macOS has no native Clearcote build) and
/// it is missing or not running.
/// </summary>
public sealed class DockerUnavailableException : Exception
{
    public DockerUnavailableException(string message) : base(message) { }
}

/// <summary>The container behind a browser that <see cref="Clearcote.LaunchAsync"/> started in Docker.</summary>
/// <param name="Id">The container id.</param>
/// <param name="Image">The image it runs.</param>
/// <param name="Endpoint">The CDP endpoint the browser was connected through.</param>
/// <param name="ServeProtocol">The image's serve protocol: 3 also runs Chrome's sandbox when the container allows it
/// (it is started with the seccomp profile); 2 takes the licence key and proxy as a file and stops on its own; 0 is an
/// image older than sdk-0.40.0 (plain variables, no idle exit).</param>
public sealed record DockerContainer(string Id, string Image, string Endpoint, int ServeProtocol);

/// LaunchAsync on macOS: run the Clearcote Docker image and connect to it.
///
/// There is no native Clearcote build for macOS. Rather than fail there, LaunchAsync starts the published
/// Clearcote image (teamflatearth/clearcote:sdk-&lt;this SDK's version&gt;, a linux/amd64 image Docker Desktop
/// runs on Intel and Apple silicon alike), waits for its CDP endpoint, connects with Playwright and
/// returns the same Playwright IBrowser a local launch does. CloseAsync disconnects and stops the
/// container; nothing is left running.
///
/// When it applies: <see cref="LaunchOptions.Docker"/> false (or CLEARCOTE_DOCKER=0) turns it off; true
/// (or CLEARCOTE_DOCKER=1) turns it on on any OS with Docker; unset means macOS only, unless the caller
/// named a binary (ExecutablePath / CLEARCOTE_BINARY). CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 treats the host
/// as macOS so the real Docker path can be exercised end to end elsewhere; it is not an option.
///
/// The container is configured through the image's own CC_* variables (docker/serve.py), so only the
/// options the image understands are accepted; any other option throws naming it. Values travel in the
/// docker CLI's environment (`-e NAME` without a value), never on its command line. The licence key and
/// the proxy URL are not passed as variables at all: they are copied in as a file serve.py reads once and
/// deletes, so `docker inspect` does not show them (anyone who can reach the Docker daemon can still read a
/// running container's files and memory). The CDP port is published on 127.0.0.1 only.
///
/// What an image understands is read from its serve-protocol label before a container is created. An image older
/// than sdk-0.40.0 has none: it gets the key and proxy as plain variables, with a warning. Whatever the image,
/// LaunchAsync checks the container's log once the browser answers and refuses -- stopping and removing the
/// container -- unless it runs the licensed engine when a key was given and the proxy when one was given.
///
/// Nothing outlives its owner: the container is created with --rm, stops itself once no CDP client has been
/// connected for 30 s after its CDP first answers (CLEARCOTE_DOCKER_IDLE_EXIT), and the next launch removes any
/// whose owner process is certainly gone (SweepStaleAsync). CloseAsync stops it at once and returns once Docker has
/// removed it (waiting up to 15 s for that).
///
/// Chrome's sandbox: an image of serve protocol 3 runs Chrome with its sandbox when the container allows the user,
/// PID and network namespaces the sandbox makes, and Docker's default seccomp profile does not. Such an image is
/// started with --security-opt seccomp=&lt;profile&gt; (embedded in this assembly and written once to
/// ~/.clearcote/: Docker's default profile of moby/profiles seccomp v0.2.3 plus those two calls; docker/seccomp.json in
/// the repository) on an x86_64 Docker. An older image is not, since its Chrome runs with --no-sandbox whatever it
/// gets, and neither is a Docker on another CPU, where the image runs emulated (SeccompArgs). A Docker that refuses the
/// profile, at create or at start, gets the container again without it.
/// Mirrors _docker.py (Python) and docker.ts (Node).
internal static class DockerLaunch
{
    internal const string DefaultRepository = "teamflatearth/clearcote";
    internal const string TestOnlyAssumeMacos = "CLEARCOTE_TEST_ONLY_ASSUME_MACOS";
    private const string Label = "com.clearcotelabs.sdk-launch=1";
    // Who started a container: SweepStaleAsync removes the ones whose owner process is gone.
    private const string OwnerHostLabel = "com.clearcotelabs.owner-host";
    private const string OwnerTokenLabel = "com.clearcotelabs.owner-token";
    // The image's serve protocol (docker/serve.py's SERVE_PROTOCOL, carried as this image label). 2: takes
    // CC_SECRETS_FILE and CC_IDLE_EXIT_SECONDS and logs a serve-state line. An image without the label is older.
    private const string ProtocolLabel = "com.clearcotelabs.serve-protocol";
    internal const int ServeProtocol = 2;
    private const string FirstProtocolTag = "sdk-0.40.0";   // the first published image that speaks it
    // A container stops itself once no CDP client has been connected for this long (serve.py's
    // CC_IDLE_EXIT_SECONDS). CLEARCOTE_DOCKER_IDLE_EXIT overrides it; 0 turns it off.
    private const int DefaultIdleExitSeconds = 30;
    private const string DefaultCacheVolume = "clearcote-cache";
    // The licence key and the proxy URL (it may carry a password) reach the container as this file, copied
    // in with `docker cp` and deleted by serve.py once read, not as -e variables `docker inspect` would show.
    private static readonly string[] SecretEnv = { "CLEARCOTE_LICENSE_KEY", "CC_PROXY" };
    internal const string SecretsFile = "/tmp/clearcote-secrets.json";
    private const int ImageUid = 10001;   // the image's user (cc)
    // An image of this serve protocol runs Chrome's sandbox when the container allows it, so it is started with this
    // seccomp profile: Docker's default plus the namespace calls the sandbox makes (docker/seccomp.json).
    internal const int SandboxProtocol = 3;
    internal const string SeccompResource = "Clearcote.docker-seccomp.json";
    private static readonly System.Text.RegularExpressions.Regex SeccompRefused = new("seccomp", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    private static readonly string[] X86_64 = { "x86_64", "amd64" };   // the daemon architectures that run the linux/amd64 image natively
    private static readonly string[] Truthy = { "1", "true", "yes", "on" };
    private static readonly string[] Falsy = { "0", "false", "no", "off" };
    private const int ReadyTimeoutMs = 180_000;
    private const float ConnectTimeoutMs = 120_000;
    private const string InstallUrl = "https://docs.docker.com/desktop/setup/install/mac-install/";
    private const string OffHint = "To launch without Docker, set Docker = false (or CLEARCOTE_DOCKER=0) and ExecutablePath to a compatible Clearcote binary.";

    internal sealed record CliResult(int Code, string Stdout, string Stderr);

    /// Test seams: the docker executable lookup, the CLI runner, and Playwright's ConnectOverCDPAsync.
    internal static Func<string?> Which { get; set; } = () => FindOnPath("docker");
    internal static Func<IReadOnlyList<string>, IDictionary<string, string>?, int, byte[]?, Task<CliResult>> Run { get; set; } = RunCliAsync;
    internal static Func<string, BrowserTypeConnectOverCDPOptions, Task<IBrowser>>? ConnectOverride { get; set; }

    /// The container behind a browser (real or proxied); see <see cref="Clearcote.DockerContainerOf"/>.
    internal static readonly ConditionalWeakTable<object, DockerContainer> Containers = new();

    // LaunchOptions property -> the image's CC_* variable.
    private static readonly Dictionary<string, string> StrEnv = new()
    {
        ["Fingerprint"] = "CC_FINGERPRINT", ["Platform"] = "CC_PLATFORM", ["PlatformVersion"] = "CC_PLATFORM_VERSION",
        ["Brand"] = "CC_BRAND", ["BrandVersion"] = "CC_BRAND_VERSION", ["GpuVendor"] = "CC_GPU_VENDOR",
        ["GpuRenderer"] = "CC_GPU_RENDERER", ["Timezone"] = "CC_TIMEZONE", ["AcceptLanguage"] = "CC_ACCEPT_LANGUAGE",
        ["WebrtcIp"] = "CC_WEBRTC_IP", ["WebrtcMdns"] = "CC_WEBRTC_MDNS", ["TlsProfile"] = "CC_TLS_PROFILE",
        ["Version"] = "CC_VERSION", ["Location"] = "CC_LOCATION",
    };
    private static readonly Dictionary<string, string> IntEnv = new()
    {
        ["HardwareConcurrency"] = "CC_HARDWARE_CONCURRENCY", ["DeviceMemory"] = "CC_DEVICE_MEMORY",
        ["ColorDepth"] = "CC_COLOR_DEPTH", ["MaxTouchPoints"] = "CC_MAX_TOUCH_POINTS", ["StorageQuota"] = "CC_STORAGE_QUOTA",
    };
    private static readonly Dictionary<string, string> BoolEnv = new()
    {
        ["LightStealth"] = "CC_LIGHT_STEALTH", ["DisableGpuFingerprint"] = "CC_DISABLE_GPU_FINGERPRINT",
        ["FingerprintNoise"] = "CC_FINGERPRINT_NOISE", ["CanvasNoise"] = "CC_CANVAS_NOISE", ["GpuStringSpoof"] = "CC_GPU_STRING_SPOOF",
    };
    private static readonly string[] Special =
        { "DevicePixelRatio", "FingerprintProfile", "Headless", "Proxy", "Args", "LicenseKey", "LicenseApiBase", "PersonaEnv", "StockRuntime" };
    private static readonly string[] SdkSide = { "Timeout", "SlowMo", "Quiet", "Docker", "DockerImage" };

    /// Everything else LaunchOptions has is refused when set: a Docker launch takes the image's options,
    /// and (like any local launch) ignores the cloud section. Derived from the type, so an option added to
    /// LaunchOptions later is refused here until it is mapped.
    internal static readonly IReadOnlyList<PropertyInfo> RefusedOptions = typeof(LaunchOptions)
        .GetProperties(BindingFlags.Public | BindingFlags.Instance)
        .Where(p => !StrEnv.ContainsKey(p.Name) && !IntEnv.ContainsKey(p.Name) && !BoolEnv.ContainsKey(p.Name)
                    && !Special.Contains(p.Name) && !SdkSide.Contains(p.Name)
                    && (!CloudLaunch.SessionOptions.Contains(p.Name) || p.Name == "Geoip")
                    && !CloudLaunch.SdkSideOptions.Contains(p.Name))
        .OrderBy(p => p.Name, StringComparer.Ordinal)
        .ToList();

    private static string Env(string name) => (Environment.GetEnvironmentVariable(name) ?? "").Trim();

    internal static bool HostIsMacos() => Native.OsTag == "macos" || Env(TestOnlyAssumeMacos) == "1";

    /// Whether this launch runs in the Clearcote Docker image (see the class remarks).
    internal static bool Requested(LaunchOptions? o)
    {
        if (o?.Docker is bool b) return b;
        if (!string.IsNullOrEmpty(o?.ExecutablePath)) return false;   // the caller named a binary: theirs to run
        var env = Env("CLEARCOTE_DOCKER").ToLowerInvariant();
        if (Falsy.Contains(env)) return false;
        if (Truthy.Contains(env)) return true;
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_BINARY"))) return false;
        return HostIsMacos();
    }

    internal static string DefaultImage() =>
        Environment.GetEnvironmentVariable("CLEARCOTE_DOCKER_IMAGE") is { Length: > 0 } img ? img : $"{DefaultRepository}:sdk-{Clearcote.Version}";

    private static string ProxyUrl(ProxyOptions p)
    {
        if (string.IsNullOrEmpty(p.Server)) throw new ArgumentException("Proxy.Server is required");
        var i = p.Server.IndexOf("://", StringComparison.Ordinal);
        var scheme = i < 0 ? "http" : p.Server[..i];
        var rest = i < 0 ? p.Server : p.Server[(i + 3)..];
        if (!string.IsNullOrEmpty(p.Username) || !string.IsNullOrEmpty(p.Password))
            rest = $"{Uri.EscapeDataString(p.Username ?? "")}:{Uri.EscapeDataString(p.Password ?? "")}@{rest}";
        return $"{scheme}://{rest}";
    }

    /// The CC_* (and licence) variables the image is started with. Throws naming the first option it cannot take.
    internal static Dictionary<string, string> ContainerEnv(LaunchOptions o)
    {
        var defaults = new LaunchOptions();
        foreach (var p in RefusedOptions)
        {
            var value = p.GetValue(o);
            if (value is not null && !Equals(value, p.GetValue(defaults)))
                throw new ArgumentException(
                    $"{p.Name} is not available when LaunchAsync runs Clearcote in Docker (there is no native macOS build). {OffHint}");
        }
        var env = new Dictionary<string, string>(StringComparer.Ordinal);
        var t = typeof(LaunchOptions);
        foreach (var (prop, name) in StrEnv)
            if (t.GetProperty(prop)!.GetValue(o) is string s) env[name] = s;
        foreach (var (prop, name) in IntEnv)
            if (t.GetProperty(prop)!.GetValue(o) is int n) env[name] = n.ToString(CultureInfo.InvariantCulture);
        foreach (var (prop, name) in BoolEnv)
            if (t.GetProperty(prop)!.GetValue(o) is bool v) env[name] = v ? "1" : "0";   // false is a value
        if (o.DevicePixelRatio is double dpr) env["CC_DEVICE_PIXEL_RATIO"] = dpr.ToString("R", CultureInfo.InvariantCulture);
        if (o.FingerprintProfile is not null)
        {
            env["CC_FINGERPRINT_PROFILE"] = o.FingerprintProfile switch
            {
                string path when File.Exists(path) => File.ReadAllText(path),   // a path on THIS machine: send its content
                string json => json,
                var obj => JsonSerializer.Serialize(obj),
            };
        }
        if (o.Headless == true) env["CC_HEADLESS"] = "1";   // unset or false: the image's default, headed on its own display
        if (o.Proxy is { } proxy) env["CC_PROXY"] = ProxyUrl(proxy);
        var args = (o.Args ?? Array.Empty<string>()).ToList();
        var bad = args.FirstOrDefault(a => string.IsNullOrEmpty(a) || a.Any(char.IsWhiteSpace));
        if (bad is not null) throw new ArgumentException($"Args \"{bad}\": an argument with whitespace cannot be passed to the Docker image");
        // StockRuntime (engine r32+) goes in with the other engine switches; the image's entrypoint keeps it only on an
        // engine that has it, as a launch here does (docker/serve.py).
        if (LaunchOpts.StockRuntimeWanted(o.StockRuntime) && !args.Contains(LaunchOpts.StockRuntimeSwitch)) args.Add(LaunchOpts.StockRuntimeSwitch);
        if (args.Count > 0) env["CC_EXTRA_ARGS"] = string.Join(' ', args);
        if (License.ResolveLicenseKey(o.LicenseKey) is { } key) env["CLEARCOTE_LICENSE_KEY"] = key;   // as a local launch: option > env > saved key
        if (!string.IsNullOrEmpty(o.LicenseApiBase)) env["CLEARCOTE_LICENSE_API"] = o.LicenseApiBase;
        // Engine patch 1021 (PersonaEnv): the image's entrypoint takes the persona off its chrome's command line by
        // itself, on an engine that has the switch, and reads the same opt-out a launch here does.
        if (o.PersonaEnv is bool personaEnv) env["CLEARCOTE_PERSONA_ENV"] = personaEnv ? "1" : "0";
        return env;
    }

    // ── the docker CLI ─────────────────────────────────────────────────────────────────────────────

    private static string? FindOnPath(string name)
    {
        var exts = Native.IsWindows
            ? (Environment.GetEnvironmentVariable("PATHEXT") ?? ".EXE;.CMD;.BAT").Split(';', StringSplitOptions.RemoveEmptyEntries)
            : new[] { "" };
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
            foreach (var ext in exts)
            {
                var p = Path.Combine(dir, name + ext.ToLowerInvariant());
                if (File.Exists(p)) return p;
            }
        return null;
    }

    private static async Task<CliResult> RunCliAsync(IReadOnlyList<string> argv, IDictionary<string, string>? env, int timeoutMs, byte[]? input)
    {
        var psi = new ProcessStartInfo(argv[0])
        {
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = input is not null,
            UseShellExecute = false, CreateNoWindow = true,
        };
        foreach (var a in argv.Skip(1)) psi.ArgumentList.Add(a);
        if (env is not null) foreach (var (k, v) in env) psi.Environment[k] = v;
        Process proc;
        try { proc = Process.Start(psi)!; }
        catch (Exception e) { return new CliResult(127, "", e.Message); }
        using (proc)
        {
            var stdout = proc.StandardOutput.ReadToEndAsync();
            var stderr = proc.StandardError.ReadToEndAsync();
            if (input is not null)
            {
                await proc.StandardInput.BaseStream.WriteAsync(input).ConfigureAwait(false);
                proc.StandardInput.Close();
            }
            using var cts = new CancellationTokenSource(timeoutMs);
            try { await proc.WaitForExitAsync(cts.Token).ConfigureAwait(false); }
            catch (OperationCanceledException)
            {
                try { proc.Kill(entireProcessTree: true); } catch { }
                return new CliResult(124, "", $"`{string.Join(' ', argv.Take(2))}` timed out after {timeoutMs / 1000} s");
            }
            return new CliResult(proc.ExitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false));
        }
    }

    private static string FirstLine(string? text) =>
        (text ?? "").Split('\n').Select(l => l.Trim()).FirstOrDefault(l => l.Length > 0) ?? "";

    /// The docker executable, or DockerUnavailableException saying what to do.
    internal static async Task<string> CheckDockerAsync() => (await DockerInfoAsync().ConfigureAwait(false)).Exe;

    /// The docker executable and the daemon's CPU architecture as `docker info` reports it ("x86_64", "aarch64", or ""
    /// when it does not say), from one `docker info` (it takes about a second on Docker Desktop), or
    /// DockerUnavailableException saying what to do.
    internal static async Task<(string Exe, string Arch)> DockerInfoAsync()
    {
        var exe = Which() ?? throw new DockerUnavailableException(
            $"Clearcote has no native macOS build, so on macOS LaunchAsync runs it in Docker, but the `docker` command was not found. Install Docker Desktop ({InstallUrl}), start it, and try again. {OffHint}");
        var r = await Run(new[] { exe, "info", "--format", "{{.ServerVersion}} {{.Architecture}}" }, null, 30_000, null).ConfigureAwait(false);
        if (r.Code != 0)
            throw new DockerUnavailableException(
                $"Clearcote has no native macOS build, so on macOS LaunchAsync runs it in Docker, but Docker is not running (`docker info`: {(FirstLine(r.Stderr) is { Length: > 0 } l ? l : $"exit {r.Code}")}). Start Docker Desktop and try again. {OffHint}");
        var parts = r.Stdout.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        return (exe, parts.Length > 1 ? parts[1] : "");
    }

    private static readonly HashSet<string> Warned = new(StringComparer.Ordinal);   // the warnings given once already

    private static void WarnOnce(string key, string message, bool quiet)
    {
        if (quiet) return;
        lock (Warned)
            if (!Warned.Add(key)) return;
        Console.Error.WriteLine(message);
    }

    /// Forget the warnings given once (for tests).
    internal static void ResetWarnings() { lock (Warned) Warned.Clear(); }

    /// How long a launched container waits with no CDP client before it stops itself:
    /// CLEARCOTE_DOCKER_IDLE_EXIT (seconds, 0 = never), default 30.
    internal static int IdleExitSeconds()
    {
        var raw = Env("CLEARCOTE_DOCKER_IDLE_EXIT");
        if (raw.Length == 0) return DefaultIdleExitSeconds;
        if (!int.TryParse(raw, NumberStyles.None, CultureInfo.InvariantCulture, out var n))
            throw new ArgumentException($"CLEARCOTE_DOCKER_IDLE_EXIT=\"{raw}\" is not a whole number of seconds");
        return n;
    }

    /// The named volume a licensed container keeps its engine in (shared, so it downloads once):
    /// CLEARCOTE_DOCKER_CACHE_VOLUME, default clearcote-cache.
    internal static string CacheVolume() => Env("CLEARCOTE_DOCKER_CACHE_VOLUME") is { Length: > 0 } v ? v : DefaultCacheVolume;

    internal static bool PidAlive(int pid)
    {
        if (pid <= 0) return false;
        try
        {
            using var p = Process.GetProcessById(pid);
            try { return !p.HasExited; }
            catch (System.ComponentModel.Win32Exception) { return true; }   // exists, but not ours to query
        }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }

    // ── who owns a container ────────────────────────────────────────────────────────────────────────
    // A container carries a random per-process token (OwnerTokenLabel); the owner writes a record under
    // ~/.clearcote/docker-owners/<token>.json saying which process it is: pid, a start marker that changes when
    // the pid is reused, its host name and (Linux) its PID namespace and boot id. A sweeper only judges a record
    // it can verify completely: one in its own home, written on this host, from this boot and PID namespace --
    // every one of those fields as this process would write it, so a record written on another OS into a shared
    // home (no namespace or boot id), in a container given the Docker socket (another namespace), next to Windows
    // in WSL2, or on another machine is left alone, and so is a pid that is not a positive 32-bit number. When in
    // doubt, nothing is removed: the container's idle exit still stops it. The record format is shared by the
    // Python, Node and .NET SDKs, so any of them can sweep any other's containers.

    private static string? ReadText(string path)
    {
        try { return File.ReadAllText(path).Trim(); } catch { return null; }
    }

    // What this host says about processes: /proc where there is one, `ps` elsewhere. Test seams.
    internal static Func<string, string?> ProcText = ReadText;
    internal static Func<bool> IsWindowsHost = OperatingSystem.IsWindows;
    internal static Func<string?> BootIdProbe = () => ProcText("/proc/sys/kernel/random/boot_id");
    internal static Func<string?> PidNamespaceProbe = () =>
    {
        try { return new FileInfo("/proc/self/ns/pid").LinkTarget; } catch { return null; }
    };
    /// Runs `ps` as the start info says: its standard output, or null when it failed.
    internal static Func<ProcessStartInfo, string?> RunPs = psi =>
    {
        using var ps = Process.Start(psi)!;
        var output = ps.StandardOutput.ReadToEnd();
        ps.WaitForExit(10_000);
        return ps.ExitCode == 0 ? output : null;
    };

    private static string? BootId() => BootIdProbe();
    private static string? PidNamespace() => PidNamespaceProbe();

    /// `ps -o lstart=` prints local time, in the locale's words. Pinned, so an owner and a sweeper that run with
    /// different TZ / LANG settings (or one program that changes TZ between launches) read the same string for the
    /// same process. Without it a sweeper in another time zone took a live owner for a reused pid.
    internal static readonly IReadOnlyDictionary<string, string> PsEnv = new Dictionary<string, string> { ["TZ"] = "UTC0", ["LC_ALL"] = "C" };

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, int pid);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);

    /// The creation time Windows keeps for a process, as the FILETIME it stores (UTC). Process.StartTime goes
    /// through local time, and a start inside the hour a clock change repeats came back an hour off.
    private static long? WindowsCreationTime(int pid)
    {
        var h = OpenProcess(0x1000 /* PROCESS_QUERY_LIMITED_INFORMATION */, false, pid);
        if (h == IntPtr.Zero) return null;
        try { return GetProcessTimes(h, out var created, out _, out _, out _) ? created : null; }
        finally { CloseHandle(h); }
    }

    /// A marker that differs once <paramref name="pid"/> belongs to another process (it was reused), or null when
    /// it cannot be read here. Linux: the start time from /proc; Windows: the creation time (FILETIME, UTC);
    /// elsewhere (macOS): `ps -o lstart=` in UTC and the C locale. The same strings in all three SDKs.
    internal static string? ProcessStart(int pid)
    {
        if (ProcText($"/proc/{pid}/stat") is { } stat)
        {
            var f = stat[(stat.LastIndexOf(')') + 2)..].Split(' ', StringSplitOptions.RemoveEmptyEntries);
            return f.Length > 19 ? $"linux:{f[19]}" : null;
        }
        if (IsWindowsHost())
        {
            try { return WindowsCreationTime(pid) is { } created ? $"win:{created}" : null; }
            catch { return null; }
        }
        try
        {
            var psi = new ProcessStartInfo("ps") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
            foreach (var a in new[] { "-o", "lstart=", "-p", pid.ToString(CultureInfo.InvariantCulture) }) psi.ArgumentList.Add(a);
            foreach (var (k, v) in PsEnv) psi.Environment[k] = v;
            var output = RunPs(psi);
            var text = output is null ? "" : string.Join(' ', output.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
            return text.Length > 0 ? $"ps-utc:{text}" : null;
        }
        catch { return null; }
    }

    private static string OwnersDir => Path.Combine(Native.ClearcoteDir, "docker-owners");
    private static string? _ownerToken;
    private static readonly object OwnerGate = new();

    /// Test seam: forget this process's token (a new HOME gets a new record).
    internal static void ResetOwnerToken() { lock (OwnerGate) _ownerToken = null; }

    /// This process's owner token, recording it (once) under ~/.clearcote/docker-owners/.
    internal static string OwnerToken()
    {
        lock (OwnerGate)
        {
            if (_ownerToken is not null) return _ownerToken;
            var token = Convert.ToHexString(System.Security.Cryptography.RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
            var record = new Dictionary<string, object?>
            {
                ["token"] = token, ["pid"] = Environment.ProcessId, ["start"] = ProcessStart(Environment.ProcessId),
                ["pidns"] = PidNamespace(), ["boot"] = BootId(), ["host"] = System.Net.Dns.GetHostName(), ["sdk"] = "dotnet",
            };
            var path = Path.Combine(OwnersDir, token + ".json");
            try
            {
                Directory.CreateDirectory(OwnersDir);
                File.WriteAllText(path, JsonSerializer.Serialize(record));
                AppDomain.CurrentDomain.ProcessExit += (_, _) => { try { File.Delete(path); } catch { } };
            }
            catch { /* no record: other launches leave its containers alone, and idle exit still ends them */ }
            return _ownerToken = token;
        }
    }

    /// The fields an owner record from this process would carry besides its pid and start marker.
    internal static (string? Host, string? Boot, string? PidNs) OwnerHere() => (System.Net.Dns.GetHostName(), BootId(), PidNamespace());

    private static string? MarkerKind(string marker) => marker.IndexOf(':') is var i and > 0 ? marker[..i] : null;

    /// True or false for the owner a record describes, or null when that cannot be told for certain here: a record
    /// whose host, boot id or PID namespace is not exactly what this process would record (missing ones included),
    /// or whose pid is not a positive 32-bit integer (int.MaxValue at most, as TryGetInt32 reads it). A pid that is
    /// alive with a start marker of the same kind but another value was reused (false); one whose marker cannot be
    /// compared counts as alive.
    internal static bool? OwnerAlive(JsonElement rec)
    {
        string? Str(string name) => rec.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        var here = OwnerHere();
        if (string.IsNullOrEmpty(here.Host) || Str("host") is not { } host || !string.Equals(host, here.Host, StringComparison.OrdinalIgnoreCase)) return null;
        if (Str("boot") != here.Boot || Str("pidns") != here.PidNs) return null;
        if (!rec.TryGetProperty("pid", out var p) || p.ValueKind != JsonValueKind.Number || !p.TryGetInt32(out var pid) || pid <= 0) return null;
        if (!PidAlive(pid)) return false;
        if (Str("start") is { Length: > 0 } start && ProcessStart(pid) is { } now && MarkerKind(now) == MarkerKind(start) && now != start)
            return false;   // the pid now belongs to another process
        return true;
    }

    private static JsonElement? LoadRecord(string path)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            return doc.RootElement.ValueKind == JsonValueKind.Object ? doc.RootElement.Clone() : null;
        }
        catch { return null; }
    }

    /// Stop and remove containers an earlier launch left behind whose owner process is certainly gone (killed,
    /// crashed): their token's record is in this home, from this boot and PID namespace, and its process no longer
    /// exists. They would stop on their own after the idle period; this frees their licence seat now. Anything else
    /// -- live owners, owners in another namespace or on another machine, containers without a token -- is left
    /// alone. Returns the ids removed.
    internal static async Task<IReadOnlyList<string>> SweepStaleAsync(string exe)
    {
        var format = $"{{{{.ID}}}}\t{{{{.Label \"{OwnerTokenLabel}\"}}}}";
        var r = await Run(new[] { exe, "ps", "-a", "--filter", $"label={Label}", "--format", format }, null, 30_000, null).ConfigureAwait(false);
        var stale = new List<string>();
        var dead = new HashSet<string>(StringComparer.Ordinal);
        foreach (var line in r.Code == 0 ? r.Stdout.Split('\n') : Array.Empty<string>())
        {
            var parts = line.Trim().Split('\t');
            if (parts.Length != 2 || !System.Text.RegularExpressions.Regex.IsMatch(parts[1], "^[0-9a-f]{32}$")) continue;   // no owner token (an older SDK): not ours to judge
            var path = Path.Combine(OwnersDir, parts[1] + ".json");
            if (LoadRecord(path) is { } rec && OwnerAlive(rec) == false)
            {
                stale.Add(parts[0]);
                dead.Add(path);
            }
        }
        if (stale.Count > 0)
        {
            await Run(new[] { exe, "stop", "--time", "10" }.Concat(stale).ToList(), null, 120_000, null).ConfigureAwait(false);
            await Run(new[] { exe, "rm", "-f", "-v" }.Concat(stale).ToList(), null, 60_000, null).ConfigureAwait(false);
        }
        try   // the records of owners that are certainly gone
        {
            foreach (var path in Directory.EnumerateFiles(OwnersDir, "*.json"))
                if (dead.Contains(path) || (LoadRecord(path) is { } rec && OwnerAlive(rec) == false))
                    try { File.Delete(path); } catch { }
        }
        catch { }
        return stale;
    }

    private static readonly Dictionary<string, string> Live = new();   // container id -> docker executable
    private static int _sweepInstalled;

    private static void InstallSweep()
    {
        if (Interlocked.Exchange(ref _sweepInstalled, 1) == 1) return;
        // A normal exit removes what a caller never closed. Ctrl-C, a kill or a crash may not run this: the
        // container's idle exit and the next launch's SweepStaleAsync cover those.
        AppDomain.CurrentDomain.ProcessExit += (_, _) =>
        {
            KeyValuePair<string, string>[] left;
            lock (Live) left = Live.ToArray();
            foreach (var (id, exe) in left)
                try { Run(new[] { exe, "rm", "-f", "-v", id }, null, 30_000, null).Wait(TimeSpan.FromSeconds(30)); } catch { }
        };
    }

    /// How long RemoveAsync waits for the container to be gone, and how often it looks. Seams: tests shorten them.
    internal static TimeSpan RemovalWait { get; set; } = TimeSpan.FromSeconds(15);
    internal static TimeSpan RemovalPoll { get; set; } = TimeSpan.FromMilliseconds(250);

    /// Waits, for up to RemovalWait, until `docker ps -a` no longer lists the container. False only when it is still
    /// listed once the wait is up; a docker CLI that does not answer ends the wait. Never throws.
    private static async Task<bool> WaitGoneAsync(string exe, string id)
    {
        var deadline = DateTime.UtcNow + RemovalWait;
        while (true)
        {
            var left = (int)Math.Max((deadline - DateTime.UtcNow).TotalMilliseconds, 1000);
            var r = await Run(new[] { exe, "ps", "-a", "-q", "--no-trunc", "--filter", $"id={id}" }, null, left, null).ConfigureAwait(false);
            if (r.Code != 0 || r.Stdout.Trim().Length == 0) return true;
            if (DateTime.UtcNow >= deadline) return false;
            await Task.Delay(RemovalPoll).ConfigureAwait(false);
        }
    }

    internal static async Task RemoveAsync(string exe, string id)
    {
        // stop first: SIGTERM lets the image release a licence seat. The container was created with --rm, so
        // stopping it removes it and its anonymous volume (the image declares VOLUME /opt/xdg-cache: ~0.5 GB);
        // rm -f -v is for a container that did not go away. A named volume is never removed.
        await Run(new[] { exe, "stop", "--time", "10", id }, null, 60_000, null).ConfigureAwait(false);
        await Run(new[] { exe, "rm", "-f", "-v", id }, null, 60_000, null).ConfigureAwait(false);
        // The daemon's own --rm removal starts once the container has stopped and runs on after `docker stop`
        // returns; `docker rm` meanwhile answers "removal ... already in progress" at once. Wait for it to finish,
        // so that CloseAsync returning means the container is gone. A container still there after the wait (its
        // removal failed: Docker marks it Dead) gets one more rm -f -v, which takes the volume too.
        if (!await WaitGoneAsync(exe, id).ConfigureAwait(false))
            await Run(new[] { exe, "rm", "-f", "-v", id }, null, 60_000, null).ConfigureAwait(false);
        lock (Live) Live.Remove(id);
    }

    /// A starting container's log, kept in memory: it is created with --rm, so once it has stopped its log
    /// cannot be asked for any more.
    internal interface ILogTail
    {
        Task<string> TextAsync(int waitMs = 3000);
        /// The lines saying what the entrypoint applied (serve-state, engine, proxy), once one matching
        /// <paramref name="pattern"/> is among them (or the wait is up). Kept apart from the other lines.
        Task<IReadOnlyList<string>> WaitMarksAsync(System.Text.RegularExpressions.Regex pattern, int waitMs = 10_000);
        void Stop();
    }

    private static readonly System.Text.RegularExpressions.Regex Mark = new(@"\[clearcote\] (serve-state |engine: |proxy: )");

    private sealed class LogTail : ILogTail
    {
        private readonly Process? _proc;
        private readonly Queue<string> _lines = new();
        private readonly List<string> _marks = new();
        private readonly Task _done = Task.CompletedTask;

        public LogTail(string exe, string id)
        {
            try
            {
                var psi = new ProcessStartInfo(exe) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
                foreach (var a in new[] { "logs", "--follow", id }) psi.ArgumentList.Add(a);
                _proc = Process.Start(psi);
                if (_proc is not null) _done = Task.WhenAll(Pump(_proc.StandardOutput), Pump(_proc.StandardError));
            }
            catch { _proc = null; }
        }

        private async Task Pump(StreamReader reader)
        {
            while (await reader.ReadLineAsync().ConfigureAwait(false) is { } line)
                lock (_lines)
                {
                    _lines.Enqueue(line);
                    if (Mark.IsMatch(line)) _marks.Add(line);
                    while (_lines.Count > 40) _lines.Dequeue();
                }
        }

        public async Task<IReadOnlyList<string>> WaitMarksAsync(System.Text.RegularExpressions.Regex pattern, int waitMs = 10_000)
        {
            var deadline = DateTime.UtcNow.AddMilliseconds(waitMs);
            while (true)
            {
                lock (_lines) if (_marks.Any(pattern.IsMatch) || DateTime.UtcNow >= deadline) return _marks.ToArray();
                await Task.Delay(50).ConfigureAwait(false);
            }
        }

        public async Task<string> TextAsync(int waitMs = 3000)
        {
            await Task.WhenAny(_done, Task.Delay(waitMs)).ConfigureAwait(false);   // the container is gone: the follower ends
            lock (_lines) return string.Join("\n", _lines);
        }

        public void Stop()
        {
            try { if (_proc is { HasExited: false }) _proc.Kill(); } catch { }
            _proc?.Dispose();
        }
    }

    /// Test seam: the log follower.
    internal static Func<string, string, ILogTail> FollowLogs { get; set; } = (exe, id) => new LogTail(exe, id);

    private static async Task<bool> CdpReadyAsync(int port)
    {
        try
        {
            using var http = new HttpClient(new SocketsHttpHandler { UseProxy = false }) { Timeout = TimeSpan.FromSeconds(2) };
            using var res = await http.GetAsync($"http://127.0.0.1:{port}/json/version").ConfigureAwait(false);
            return res.IsSuccessStatusCode && (await res.Content.ReadAsStringAsync().ConfigureAwait(false)).Contains("webSocketDebuggerUrl");
        }
        catch { return false; }
    }

    private static async Task<int> PublishedPortAsync(string exe, string id)
    {
        var r = await Run(new[] { exe, "port", id, "9222/tcp" }, null, 30_000, null).ConfigureAwait(false);
        foreach (var line in r.Stdout.Split('\n'))
        {
            var m = System.Text.RegularExpressions.Regex.Match(line.Trim(), @":(\d+)$");
            if (m.Success) return int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture);
        }
        throw new InvalidOperationException($"could not read the container's published CDP port ({(FirstLine(r.Stderr) is { Length: > 0 } l ? l : r.Stdout)})");
    }

    private static async Task WaitReadyAsync(string exe, string id, int port, int budgetMs, ILogTail logs)
    {
        var deadline = DateTime.UtcNow.AddMilliseconds(budgetMs);
        while (true)
        {
            if (await CdpReadyAsync(port).ConfigureAwait(false)) return;
            var st = await Run(new[] { exe, "inspect", "-f", "{{.State.Running}}", id }, null, 30_000, null).ConfigureAwait(false);
            if (st.Code != 0 || st.Stdout.Trim() != "true")   // stopped (and, with --rm, already gone)
            {
                var text = (await logs.TextAsync().ConfigureAwait(false)).Trim();
                throw new InvalidOperationException($"the Clearcote container stopped before its browser came up:\n{(text.Length > 0 ? text : "(it left no log)")}");
            }
            if (DateTime.UtcNow >= deadline)
                throw new TimeoutException($"the Clearcote container's CDP endpoint did not answer within {budgetMs / 1000} s");
            await Task.Delay(250).ConfigureAwait(false);
        }
    }

    private static readonly System.Text.RegularExpressions.Regex VolumeInitRace = new(
        @"volumes/[^/\s]+/_data\S*: (?:file exists|no such file)", System.Text.RegularExpressions.RegexOptions.IgnoreCase);

    // A tag that is not there: Docker 29 says `failed to resolve reference "docker.io/...:tag": ...: not found`;
    // older ones `manifest unknown` / `manifest for ... not found`. Docker 29 reports a registry it cannot reach with
    // the same "failed to resolve reference" opening, so that phrase alone proves nothing: the network wordings are
    // looked for first.
    private static readonly System.Text.RegularExpressions.Regex NotPublished = new(
        @"manifest unknown|manifest for .* not found|not found: manifest|pull access denied|repository does not exist|: not found\s*$",
        System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.Multiline);
    private static readonly System.Text.RegularExpressions.Regex RegistryUnreachable = new(
        @"dial tcp|no such host|i/o timeout|connection refused|connection reset|network is unreachable|no route to host|TLS handshake timeout|context deadline exceeded|failed to do request|Client\.Timeout|temporary failure in name resolution|server misbehaving",
        System.Text.RegularExpressions.RegexOptions.IgnoreCase);

    private static Exception ImageFailed(string image, int code, string stderr, string what = "docker pull")
    {
        var detail = stderr.Trim() is { Length: > 0 } e ? e : $"exit {code}";
        if (RegistryUnreachable.IsMatch(detail))
            return new InvalidOperationException($"could not download the Clearcote image {image}: the registry did not answer ({FirstLine(detail)}). Check this machine's network (and any proxy Docker itself needs), or set CLEARCOTE_DOCKER_IMAGE / DockerImage to an image that is already here (`docker images {DefaultRepository}`).");
        if (NotPublished.IsMatch(detail))
        {
            var why = image == $"{DefaultRepository}:sdk-{Clearcote.Version}"
                ? $"Each image is published a few minutes after its SDK release (it is built from the PyPI package of the same version), so a brand-new SDK can be ahead of its image: try again in a few minutes. To launch meanwhile, set CLEARCOTE_DOCKER_IMAGE to an earlier tag of {DefaultRepository} ({DefaultRepository}:latest is the newest published one). An image older than {FirstProtocolTag} still works, but the licence key and the proxy reach it as container variables that `docker inspect` shows, and it does not stop on its own if this program dies; LaunchAsync says so when that happens."
                : "Check the name, or unset CLEARCOTE_DOCKER_IMAGE / DockerImage to use the default.";
            return new InvalidOperationException($"the Clearcote image {image} is not available ({FirstLine(detail)}). {why}");
        }
        return new InvalidOperationException($"`{what} {image}` failed: {detail}\n(set CLEARCOTE_DOCKER_IMAGE or DockerImage to use another image)");
    }

    /// The serve protocol <paramref name="image"/> speaks (its ProtocolLabel; 0 for an image without it), pulling
    /// the image first when it is not here. Known before a container exists, so the settings a container gets are
    /// always ones its entrypoint understands.
    internal static async Task<int> ImageProtocolAsync(string exe, string image, bool quiet)
    {
        const string fmt = "{{json .Config.Labels}}";
        var r = await Run(new[] { exe, "image", "inspect", "--format", fmt, image }, null, 60_000, null).ConfigureAwait(false);
        if (r.Code != 0)
        {
            if (!quiet) Console.Error.WriteLine($"[clearcote] downloading the Clearcote Docker image {image}");
            var pull = await Run(new[] { exe, "pull", "--platform", "linux/amd64", image }, null, 1_800_000, null).ConfigureAwait(false);
            if (pull.Code != 0) throw ImageFailed(image, pull.Code, pull.Stderr);
            r = await Run(new[] { exe, "image", "inspect", "--format", fmt, image }, null, 60_000, null).ConfigureAwait(false);
            if (r.Code != 0) throw ImageFailed(image, r.Code, r.Stderr, "docker image inspect");
        }
        try
        {
            using var doc = JsonDocument.Parse(r.Stdout.Trim() is { Length: > 0 } t ? t : "null");
            if (doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty(ProtocolLabel, out var v)
                && int.TryParse(v.GetString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var n))
                return n;
        }
        catch (JsonException) { }
        return 0;
    }

    /// A proxy URL split as written, the way the Python SDK splits it: the scheme ("http" without one), the userinfo
    /// before the authority's LAST '@' ("" without one) and the host and port after it. (Uri ends the login at the
    /// first '@' of a password written without escapes, and escapes some characters itself.)
    private static (string Scheme, string Info, string HostPort) SplitProxy(string url)
    {
        var i = url.IndexOf("://", StringComparison.Ordinal);
        var rest = i < 0 ? url : url[(i + 3)..];
        var end = rest.IndexOfAny(new[] { '/', '?', '#' });
        var authority = end < 0 ? rest : rest[..end];
        var at = authority.LastIndexOf('@');
        return (i < 0 ? "http" : url[..i].ToLowerInvariant(), at < 0 ? "" : authority[..at], authority[(at + 1)..]);
    }

    private static (string? Host, int? Port) ProxyHostPort(string? url)
    {
        if (string.IsNullOrEmpty(url)) return (null, null);
        var (scheme, _, hostPort) = SplitProxy(url);
        return Uri.TryCreate($"{scheme}://{hostPort}", UriKind.Absolute, out var u)
            ? (u.Host.Trim('[', ']').ToLowerInvariant(), u.IsDefaultPort && !hostPort.Contains($":{u.Port}") ? null : u.Port)
            : (null, null);
    }

    /// The scheme of a proxy URL, whether it carries a username or password (http://:@host has neither, though
    /// Uri.UserInfo is ":"), and whether they are percent-escaped.
    private static (string Scheme, bool Creds, bool Escaped) ProxyLogin(string url)
    {
        var (scheme, info, _) = SplitProxy(url);
        return (scheme, info.Length > 0 && info != ":", info.Contains('%'));
    }

    /// Why an image older than FirstProtocolTag cannot take this proxy, or null. Its entrypoint drops the password
    /// of an http(s) proxy (Chrome is then challenged and nothing answers: every request fails), and hands a SOCKS5
    /// password to an engine switch only the licensed engine has -- as written in the URL, without decoding it, so a
    /// username or password with characters that have to be escaped there arrives wrong.
    internal static string? LegacyProxyRefusal(string image, string? proxyUrl, bool licensed)
    {
        if (string.IsNullOrEmpty(proxyUrl)) return null;
        var (scheme, creds, escaped) = ProxyLogin(proxyUrl);
        if (!creds || (scheme.StartsWith("socks5", StringComparison.Ordinal) && licensed && !escaped)) return null;
        var what = scheme.StartsWith("http", StringComparison.Ordinal) ? "an HTTP proxy that needs a password"
            : !licensed ? "a SOCKS5 proxy that needs a password without a licence key (its open engine cannot log in)"
            : "a SOCKS5 proxy that needs a password with characters a URL has to escape (it would send them escaped)";
        return $"the Clearcote image {image} predates {FirstProtocolTag} and cannot use {what}: every request through it would fail. Use {DefaultRepository}:{FirstProtocolTag} or newer (the default image is), or a proxy that does not need a password.";
    }

    /// What the container did not apply that was asked for (empty when all is well), from the lines its entrypoint
    /// logged: serve-state (protocol 2) or, for an older image, its engine and proxy lines.
    internal static IReadOnlyList<string> VerifyApplied(IReadOnlyList<string> marks, int protocol, bool licensed, string? proxyWanted)
    {
        var problems = new List<string>();
        string? engine, proxy, proxyAuth = null;
        if (protocol >= 2)
        {
            JsonElement? state = null;
            foreach (var line in marks)
            {
                var i = line.IndexOf("serve-state ", StringComparison.Ordinal);
                if (i < 0) continue;
                try { using var doc = JsonDocument.Parse(line[(i + "serve-state ".Length)..]); state = doc.RootElement.Clone(); }
                catch (JsonException) { state = null; }
            }
            if (state is not { ValueKind: JsonValueKind.Object } st) return new[] { "it did not report what it applied (no serve-state line)" };
            engine = st.TryGetProperty("engine", out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
            proxy = st.TryGetProperty("proxy", out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;
            proxyAuth = st.TryGetProperty("proxy_auth", out var pa) && pa.ValueKind == JsonValueKind.String ? pa.GetString() : null;
        }
        else
        {
            var engineLine = marks.FirstOrDefault(m => m.Contains("] engine: ", StringComparison.Ordinal)) ?? "";
            engine = engineLine.Contains("(licensed)", StringComparison.Ordinal) && engineLine.Contains("/pro-", StringComparison.Ordinal) ? "licensed" : engineLine.Length > 0 ? "open" : null;
            var proxyLine = marks.FirstOrDefault(m => m.Contains("] proxy: ", StringComparison.Ordinal));
            proxy = proxyLine?[(proxyLine.IndexOf("] proxy: ", StringComparison.Ordinal) + "] proxy: ".Length)..].Trim();
        }
        if (licensed && engine != "licensed")
            problems.Add(engine is not null ? "a licence key was given, but it runs the open engine" : "a licence key was given, but it did not say which engine it runs");
        if (!string.IsNullOrEmpty(proxyWanted))
        {
            var (wh, wp) = ProxyHostPort(proxyWanted);
            var (gh, gp) = ProxyHostPort(proxy);
            if (proxy is null || gh != wh || (wp is not null && gp is not null && wp != gp))
                problems.Add(proxy is null ? "a proxy was given, but it did not apply it (its traffic would leave from the container's own address)" : $"it applied a different proxy ({proxy})");
            else if (protocol >= 2 && ProxyLogin(proxyWanted).Creds && proxyAuth is not ("engine" or "relay"))
                problems.Add("the proxy needs a password, but it did not say it can log in to it (every request through it would fail)");
        }
        return problems;
    }

    /// A tar holding the secrets file's JSON, owned by the image's user (uid 10001) and readable by it only,
    /// for `docker cp -`: serve.py reads it once and deletes it.
    internal static byte[] SecretsTar(IReadOnlyDictionary<string, string> secrets)
    {
        var data = System.Text.Encoding.UTF8.GetBytes(JsonSerializer.Serialize(secrets));
        using var ms = new MemoryStream();
        using (var writer = new System.Formats.Tar.TarWriter(ms, System.Formats.Tar.TarEntryFormat.Ustar, leaveOpen: true))
        {
            writer.WriteEntry(new System.Formats.Tar.UstarTarEntry(System.Formats.Tar.TarEntryType.RegularFile, Path.GetFileName(SecretsFile))
            {
                Uid = ImageUid, Gid = ImageUid, Mode = UnixFileMode.UserRead | UnixFileMode.UserWrite,
                ModificationTime = DateTimeOffset.UtcNow, DataStream = new MemoryStream(data),
            });
        }
        return ms.ToArray();
    }

    /// The seccomp profile embedded in this assembly (docker/seccomp.json).
    internal static byte[] SeccompProfile()
    {
        using var s = typeof(DockerLaunch).Assembly.GetManifestResourceStream(SeccompResource)
            ?? throw new InvalidOperationException($"the {SeccompResource} resource is missing from this assembly");
        using var ms = new MemoryStream();
        s.CopyTo(ms);
        return ms.ToArray();
    }

    private static int _profileWarned;

    /// The embedded profile as a file, which is what the docker CLI reads: ~/.clearcote/docker-seccomp-&lt;hash&gt;.json,
    /// written once for each version of the profile. One already there with the right content is used as it is. Null,
    /// after one warning per process (unless quiet), when it can be neither found nor written: the container then runs
    /// Chrome without its sandbox.
    internal static string? SeccompProfilePath(bool quiet = true)
    {
        var data = SeccompProfile();
        var hash = Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(data))[..12].ToLowerInvariant();
        var path = Path.Combine(Native.ClearcoteDir, $"docker-seccomp-{hash}.json");
        bool Written()
        {
            try { return File.Exists(path) && File.ReadAllBytes(path).AsSpan().SequenceEqual(data); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return false; }
        }
        if (Written()) return path;
        var tmp = $"{path}.{Guid.NewGuid():N}.tmp";
        try
        {
            Directory.CreateDirectory(Native.ClearcoteDir);
            File.WriteAllBytes(tmp, data);
            File.Move(tmp, path, overwrite: true);   // whole or not at all, for a launch reading it at the same time
            return path;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            try { File.Delete(tmp); } catch (Exception) { }
            if (Written()) return path;   // another launch wrote it meanwhile (a Windows File.Move race)
            if (!quiet && Interlocked.Exchange(ref _profileWarned, 1) == 0)
                Console.Error.WriteLine($"[clearcote] warning: could not write the seccomp profile to {path} ({e.Message}); the container runs Chrome without its sandbox.");
            return null;
        }
    }

    /// The docker create options that let Chrome's sandbox run in an image of <paramref name="protocol"/> on a daemon of
    /// CPU <paramref name="arch"/>: the seccomp profile for an image that uses it (SandboxProtocol), nothing for an older
    /// one, whose Chrome runs with --no-sandbox anyway and so keeps Docker's stricter default.
    ///
    /// Nothing either on a daemon that is not x86_64. The image is linux/amd64, so there it runs emulated (Docker Desktop
    /// on Apple silicon: aarch64). Under qemu the namespace calls fail and the container falls back; under Rosetta they
    /// may succeed while Chrome's x86_64 seccomp-bpf filter meets the emulator's own syscalls, and tabs could crash
    /// instead. Untested: run the live sandbox tests on Apple silicon with Rosetta on and with it off before passing the
    /// profile there.
    internal static IReadOnlyList<string> SeccompArgs(int protocol, string arch, bool quiet = true) =>
        protocol >= SandboxProtocol && X86_64.Contains(arch) && SeccompProfilePath(quiet) is { } path
            ? new[] { "--security-opt", $"seccomp={path}" } : Array.Empty<string>();

    /// Docker would not run the container with the seccomp profile: at `docker create`, or at `docker start`, which is
    /// where Docker 29 first loads it (a profile its runtime cannot apply, a kernel without seccomp).
    private sealed class ProfileRefusedException : Exception
    {
        public ProfileRefusedException(string message) : base(message) { }
    }

    /// `docker create` (again while Docker's own volume initialisation races), the secrets copied in, `docker start` ->
    /// the running container's id. Leaves nothing behind when it throws; throws ProfileRefusedException when the seccomp
    /// options <paramref name="profile"/> (in <paramref name="argv"/>) are what Docker refused, at create or at start.
    private static async Task<string> CreateStartedAsync(string exe, IReadOnlyList<string> argv, IDictionary<string, string> env,
        IReadOnlyDictionary<string, string> secrets, string image, IReadOnlyList<string> profile)
    {
        var r = await Run(argv, env, 1_800_000, null).ConfigureAwait(false);
        // Two launches creating their first container on a NEW shared volume at once collide in Docker's own
        // volume initialisation ("failed to mkdir .../volumes/<name>/_data/...: file exists"); the loser
        // succeeds a moment later, once the volume has been filled.
        for (var attempt = 1; attempt < 4 && r.Code != 0 && VolumeInitRace.IsMatch(r.Stderr); attempt++)
        {
            await Task.Delay(1000 * attempt).ConfigureAwait(false);
            r = await Run(argv, env, 1_800_000, null).ConfigureAwait(false);
        }
        var id = r.Stdout.Trim().Split('\n').LastOrDefault()?.Trim() ?? "";
        if (r.Code != 0 || id.Length == 0)
        {
            if (profile.Count > 0 && SeccompRefused.IsMatch(r.Stderr)) throw new ProfileRefusedException(FirstLine(r.Stderr));
            throw ImageFailed(image, r.Code, r.Stderr, "docker create");
        }
        lock (Live) Live[id] = exe;
        InstallSweep();
        try
        {
            if (secrets.Count > 0)
            {
                var cp = await Run(new[] { exe, "cp", "-", $"{id}:{SecretsFile[..SecretsFile.LastIndexOf('/')]}" }, null, 60_000, SecretsTar(secrets)).ConfigureAwait(false);
                if (cp.Code != 0) throw new InvalidOperationException($"could not copy the licence/proxy settings into the container: {FirstLine(cp.Stderr)}");
            }
            var st = await Run(new[] { exe, "start", id }, null, 120_000, null).ConfigureAwait(false);
            if (st.Code != 0 && profile.Count > 0 && SeccompRefused.IsMatch(st.Stderr)) throw new ProfileRefusedException(FirstLine(st.Stderr));
            if (st.Code != 0) throw new InvalidOperationException($"`docker start` failed: {(st.Stderr.Trim() is { Length: > 0 } e ? e : $"exit {st.Code}")}");
        }
        catch
        {
            await RemoveAsync(exe, id).ConfigureAwait(false);
            throw;
        }
        return id;
    }

    /// Start the image and wait for its CDP endpoint. Nothing is left running when this throws.
    ///
    /// What the image does with its settings is known first (ImageProtocolAsync). An image of this SDK's protocol
    /// gets --rm, an idle exit (it stops once no CDP client has been connected for IdleExitSeconds(), counted from
    /// when its CDP answers) and the licence key and proxy URL as a file copied in, not as -e variables
    /// `docker inspect` would show. An older image gets plain variables -- they still work -- and a warning that
    /// they are visible and that it will not stop on its own -- and is refused, before it starts, a proxy password
    /// it cannot answer (LegacyProxyRefusal). Either way, once the browser answers, the container's own log must
    /// show the licensed engine when a key was given and the proxy when one was given, logged in to when it needs
    /// a password (VerifyApplied); otherwise it is stopped and removed and LaunchAsync throws: a browser on the
    /// open engine, one sending traffic direct, or one whose every request the proxy turns away is never handed
    /// back in their place. The container carries this process's
    /// owner token for SweepStaleAsync.
    internal static async Task<(DockerContainer Container, string Exe)> StartContainerAsync(LaunchOptions o)
    {
        var env = ContainerEnv(o);
        var idle = IdleExitSeconds();
        var (exe, arch) = await DockerInfoAsync().ConfigureAwait(false);
        await SweepStaleAsync(exe).ConfigureAwait(false);
        var image = string.IsNullOrEmpty(o.DockerImage) ? DefaultImage() : o.DockerImage;
        var protocol = await ImageProtocolAsync(exe, image, o.Quiet).ConfigureAwait(false);
        var secrets = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var k in SecretEnv)
            if (env.Remove(k, out var v)) secrets[k] = v;
        var licensed = secrets.ContainsKey("CLEARCOTE_LICENSE_KEY");
        secrets.TryGetValue("CC_PROXY", out var proxyWanted);
        if (protocol >= ServeProtocol)
        {
            if (idle > 0) env["CC_IDLE_EXIT_SECONDS"] = idle.ToString(CultureInfo.InvariantCulture);
            if (secrets.Count > 0) env["CC_SECRETS_FILE"] = SecretsFile;
        }
        else
        {
            if (LegacyProxyRefusal(image, proxyWanted, licensed) is { } refusal) throw new InvalidOperationException(refusal);
            if (!o.Quiet)
                Console.Error.WriteLine($"[clearcote] warning: the image {image} predates {FirstProtocolTag}: "
                    + (secrets.Count > 0 ? "its licence key and proxy are passed as container variables, which `docker inspect` shows, and " : "")
                    + $"it will not stop on its own if this program dies (CloseAsync still stops it). Use {DefaultRepository}:{FirstProtocolTag} or newer.");
            foreach (var (k, v) in secrets) env[k] = v;   // plain variables: names on the command line, values in the CLI's environment
            secrets.Clear();
        }
        var argv = new List<string>
        {
            exe, "create", "--rm", "--platform", "linux/amd64", "--shm-size", "1g", "-p", "127.0.0.1::9222",
            "--label", Label, "--label", $"{OwnerHostLabel}={System.Net.Dns.GetHostName()}", "--label", $"{OwnerTokenLabel}={OwnerToken()}",
        };
        var seccomp = SeccompArgs(protocol, arch, o.Quiet);
        if (protocol >= SandboxProtocol && arch.Length == 0)
            WarnOnce("arch", "[clearcote] warning: `docker info` did not say which CPU Docker runs on, so the container runs Chrome without its sandbox.", o.Quiet);
        argv.AddRange(seccomp);
        foreach (var name in env.Keys.OrderBy(k => k, StringComparer.Ordinal)) argv.AddRange(new[] { "-e", name });   // value via the CLI's environment
        if (licensed) argv.AddRange(new[] { "-v", $"{CacheVolume()}:/opt/xdg-cache" });   // the licensed engine downloads once
        argv.Add(image);
        if (!o.Quiet) Console.Error.WriteLine($"[clearcote] no native macOS build: starting the Clearcote Docker image {image}");
        string id;
        try
        {
            id = await CreateStartedAsync(exe, argv, env, secrets, image, seccomp).ConfigureAwait(false);
        }
        catch (ProfileRefusedException refused)
        {
            // A Docker that cannot use the seccomp profile: the container again, without it. Its Chrome then runs with
            // --no-sandbox, as it did before the profile existed.
            if (!o.Quiet) Console.Error.WriteLine($"[clearcote] warning: Docker refused the seccomp profile ({refused.Message}); the container runs Chrome without its sandbox.");
            id = await CreateStartedAsync(exe, argv.Where(a => !seccomp.Contains(a)).ToList(), env, secrets, image, Array.Empty<string>()).ConfigureAwait(false);
        }
        ILogTail? logs = null;
        try
        {
            logs = FollowLogs(exe, id);
            var port = await PublishedPortAsync(exe, id).ConfigureAwait(false);
            await WaitReadyAsync(exe, id, port, o.Timeout is float ms && ms > 0 ? (int)Math.Max(ms, 1000) : ReadyTimeoutMs, logs).ConfigureAwait(false);
            if (protocol >= ServeProtocol || licensed || proxyWanted is not null)
            {
                var wanted = new System.Text.RegularExpressions.Regex(protocol >= ServeProtocol ? "serve-state " : proxyWanted is not null ? @"\] proxy: " : @"\] engine: ");
                var problems = VerifyApplied(await logs.WaitMarksAsync(wanted).ConfigureAwait(false), protocol, licensed, proxyWanted);
                if (problems.Count > 0)
                    throw new InvalidOperationException($"the Clearcote container ({image}) did not apply what LaunchAsync asked for: {string.Join("; ", problems)}. It was stopped and removed.");
            }
            return (new DockerContainer(id, image, $"http://127.0.0.1:{port}", protocol), exe);
        }
        catch
        {
            await RemoveAsync(exe, id).ConfigureAwait(false);
            throw;
        }
        finally
        {
            logs?.Stop();
        }
    }

    /// LaunchAsync on macOS: the Playwright IBrowser of a Clearcote container. CloseAsync/DisposeAsync
    /// disconnect and stop the container; a lost connection stops it too.
    internal static async Task<IBrowser> LaunchBrowserAsync(LaunchOptions o)
    {
        var (container, exe) = await StartContainerAsync(o).ConfigureAwait(false);
        var state = new ContainerState(exe, container.Id);
        IBrowser? real = null;
        try
        {
            var connect = ConnectOverride ?? (async (url, opts) =>
                await (await Clearcote.PlaywrightInstanceAsync().ConfigureAwait(false)).Chromium.ConnectOverCDPAsync(url, opts).ConfigureAwait(false));
            real = await connect(container.Endpoint, new BrowserTypeConnectOverCDPOptions
            {
                SlowMo = o.SlowMo, Timeout = o.Timeout ?? ConnectTimeoutMs,
            }).ConfigureAwait(false);
            real.Disconnected += (_, _) => _ = state.EndAsync();
            var browser = BrowserProxy.Wrap(real, state);
            Containers.AddOrUpdate(real, container);
            Containers.AddOrUpdate(browser, container);
            return browser;
        }
        catch
        {
            if (real is not null) { try { await real.CloseAsync().ConfigureAwait(false); } catch { } }
            await state.EndAsync().ConfigureAwait(false);
            throw;
        }
    }

    /// Stops the container at most once.
    internal sealed class ContainerState
    {
        private readonly string _exe;
        private readonly string _id;
        private readonly object _gate = new();
        private Task? _stop;

        public ContainerState(string exe, string id) { _exe = exe; _id = id; }

        public Task EndAsync()
        {
            lock (_gate) return _stop ??= RemoveQuietlyAsync();
        }

        private async Task RemoveQuietlyAsync()
        {
            try { await RemoveAsync(_exe, _id).ConfigureAwait(false); } catch { /* best-effort */ }
        }
    }

    private static object? Forward(object target, MethodInfo method, object?[]? args)
    {
        try { return method.Invoke(target, args); }
        catch (TargetInvocationException e) when (e.InnerException is not null)
        {
            ExceptionDispatchInfo.Capture(e.InnerException).Throw();
            throw;
        }
    }

    /// A container's browser: Playwright's own IBrowser, except that closing it also stops the container and
    /// a new page or context gets no emulated viewport unless one is asked for (the window is real, on the
    /// image's virtual display; an emulated 1280x720 on top of it is the impossible-window tell).
    public class BrowserProxy : DispatchProxy
    {
        internal IBrowser Target = null!;
        internal ContainerState State = null!;

        internal static IBrowser Wrap(IBrowser target, ContainerState state)
        {
            var p = Create<IBrowser, BrowserProxy>();
            var bp = (BrowserProxy)(object)p;
            bp.Target = target;
            bp.State = state;
            return p;
        }

        private async Task CloseAsync(MethodInfo? close, object?[]? args)
        {
            try
            {
                if (close is not null) await ((Task)Forward(Target, close, args)!).ConfigureAwait(false);
                else await Target.CloseAsync().ConfigureAwait(false);
            }
            finally
            {
                await State.EndAsync().ConfigureAwait(false);
            }
        }

        protected override object? Invoke(MethodInfo? targetMethod, object?[]? args)
        {
            ArgumentNullException.ThrowIfNull(targetMethod);
            switch (targetMethod.Name)
            {
                case "CloseAsync":
                    return CloseAsync(targetMethod, args);
                case "DisposeAsync":
                    return new ValueTask(CloseAsync(null, null));
                case "NewPageAsync" when args is { Length: 1 }:
                    var page = args[0] is BrowserNewPageOptions po ? new BrowserNewPageOptions(po) : new BrowserNewPageOptions();
                    page.ViewportSize ??= ViewportSize.NoViewport;
                    args[0] = page;
                    break;
                case "NewContextAsync" when args is { Length: 1 }:
                    var ctx = args[0] is BrowserNewContextOptions co ? new BrowserNewContextOptions(co) : new BrowserNewContextOptions();
                    ctx.ViewportSize ??= ViewportSize.NoViewport;
                    args[0] = ctx;
                    break;
            }
            return Forward(Target, targetMethod, args);
        }
    }
}
