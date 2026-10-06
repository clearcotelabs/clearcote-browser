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
public sealed record DockerContainer(string Id, string Image, string Endpoint);

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
/// Nothing outlives its owner: the container is created with --rm, stops itself once no CDP client has been
/// connected for 30 s (CLEARCOTE_DOCKER_IDLE_EXIT), and the next launch on the machine removes any whose
/// owner process is gone. CloseAsync stops it at once.
/// Mirrors _docker.py (Python) and docker.ts (Node).
internal static class DockerLaunch
{
    internal const string DefaultRepository = "teamflatearth/clearcote";
    internal const string TestOnlyAssumeMacos = "CLEARCOTE_TEST_ONLY_ASSUME_MACOS";
    private const string Label = "com.clearcotelabs.sdk-launch=1";
    // Who started a container: SweepStaleAsync removes the ones whose owner process is gone.
    private const string OwnerHostLabel = "com.clearcotelabs.owner-host";
    private const string OwnerPidLabel = "com.clearcotelabs.owner-pid";
    // A container stops itself once no CDP client has been connected for this long (serve.py's
    // CC_IDLE_EXIT_SECONDS). CLEARCOTE_DOCKER_IDLE_EXIT overrides it; 0 turns it off.
    private const int DefaultIdleExitSeconds = 30;
    private const string DefaultCacheVolume = "clearcote-cache";
    // The licence key and the proxy URL (it may carry a password) reach the container as this file, copied
    // in with `docker cp` and deleted by serve.py once read, not as -e variables `docker inspect` would show.
    private static readonly string[] SecretEnv = { "CLEARCOTE_LICENSE_KEY", "CC_PROXY" };
    internal const string SecretsFile = "/tmp/clearcote-secrets.json";
    private const int ImageUid = 10001;   // the image's user (cc)
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
        { "DevicePixelRatio", "FingerprintProfile", "Headless", "Proxy", "Args", "LicenseKey", "LicenseApiBase" };
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
        if (o.Args is { Count: > 0 } args)
        {
            var bad = args.FirstOrDefault(a => string.IsNullOrEmpty(a) || a.Any(char.IsWhiteSpace));
            if (bad is not null) throw new ArgumentException($"Args \"{bad}\": an argument with whitespace cannot be passed to the Docker image");
            env["CC_EXTRA_ARGS"] = string.Join(' ', args);
        }
        if (License.ResolveLicenseKey(o.LicenseKey) is { } key) env["CLEARCOTE_LICENSE_KEY"] = key;   // as a local launch: option > env > saved key
        if (!string.IsNullOrEmpty(o.LicenseApiBase)) env["CLEARCOTE_LICENSE_API"] = o.LicenseApiBase;
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
    internal static async Task<string> CheckDockerAsync()
    {
        var exe = Which() ?? throw new DockerUnavailableException(
            $"Clearcote has no native macOS build, so on macOS LaunchAsync runs it in Docker, but the `docker` command was not found. Install Docker Desktop ({InstallUrl}), start it, and try again. {OffHint}");
        var r = await Run(new[] { exe, "info", "--format", "{{.ServerVersion}}" }, null, 30_000, null).ConfigureAwait(false);
        if (r.Code != 0)
            throw new DockerUnavailableException(
                $"Clearcote has no native macOS build, so on macOS LaunchAsync runs it in Docker, but Docker is not running (`docker info`: {(FirstLine(r.Stderr) is { Length: > 0 } l ? l : $"exit {r.Code}")}). Start Docker Desktop and try again. {OffHint}");
        return exe;
    }

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

    /// Stop and remove containers an earlier launch on this machine left behind: labelled as ours, with an
    /// owner process that no longer exists (killed, crashed). They would stop on their own after the idle
    /// period; this frees their licence seat now. Containers of live processes, and of other machines sharing
    /// the Docker daemon, are left alone. Returns the ids removed.
    internal static async Task<IReadOnlyList<string>> SweepStaleAsync(string exe)
    {
        var format = $"{{{{.ID}}}}\t{{{{.Label \"{OwnerHostLabel}\"}}}}\t{{{{.Label \"{OwnerPidLabel}\"}}}}";
        var r = await Run(new[] { exe, "ps", "-a", "--filter", $"label={Label}", "--format", format }, null, 30_000, null).ConfigureAwait(false);
        if (r.Code != 0) return Array.Empty<string>();
        var here = System.Net.Dns.GetHostName();
        var stale = r.Stdout.Split('\n')
            .Select(l => l.Trim().Split('\t'))
            .Where(p => p.Length == 3 && p[1] == here && int.TryParse(p[2], NumberStyles.None, CultureInfo.InvariantCulture, out var pid) && !PidAlive(pid))
            .Select(p => p[0]).ToList();
        if (stale.Count > 0)
        {
            await Run(new[] { exe, "stop", "--time", "10" }.Concat(stale).ToList(), null, 120_000, null).ConfigureAwait(false);
            await Run(new[] { exe, "rm", "-f", "-v" }.Concat(stale).ToList(), null, 60_000, null).ConfigureAwait(false);
        }
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

    internal static async Task RemoveAsync(string exe, string id)
    {
        // stop first: SIGTERM lets the image release a licence seat. The container was created with --rm, so
        // stopping it removes it and its anonymous volume (the image declares VOLUME /opt/xdg-cache: ~0.5 GB);
        // rm -f -v is for a container that did not go away. A named volume is never removed.
        await Run(new[] { exe, "stop", "--time", "10", id }, null, 60_000, null).ConfigureAwait(false);
        await Run(new[] { exe, "rm", "-f", "-v", id }, null, 60_000, null).ConfigureAwait(false);
        lock (Live) Live.Remove(id);
    }

    /// A starting container's log, kept in memory: it is created with --rm, so once it has stopped its log
    /// cannot be asked for any more.
    internal interface ILogTail
    {
        Task<string> TextAsync(int waitMs = 3000);
        void Stop();
    }

    private sealed class LogTail : ILogTail
    {
        private readonly Process? _proc;
        private readonly Queue<string> _lines = new();
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
                    while (_lines.Count > 40) _lines.Dequeue();
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

    private static readonly System.Text.RegularExpressions.Regex NotPublished = new(
        "manifest unknown|manifest for .* not found|not found: manifest|pull access denied|repository does not exist",
        System.Text.RegularExpressions.RegexOptions.IgnoreCase);

    private static Exception CreateFailed(string image, int code, string stderr)
    {
        var detail = stderr.Trim() is { Length: > 0 } e ? e : $"exit {code}";
        if (NotPublished.IsMatch(detail))
        {
            var why = image == $"{DefaultRepository}:sdk-{Clearcote.Version}"
                ? $"Images are published a few minutes after each SDK release (after the PyPI package they are built from), so a brand-new SDK can be ahead of its image: try again shortly, or set CLEARCOTE_DOCKER_IMAGE={DefaultRepository}:latest to run the newest published image."
                : "Check the name, or unset CLEARCOTE_DOCKER_IMAGE / DockerImage to use the default.";
            return new InvalidOperationException($"the Clearcote image {image} is not available ({FirstLine(detail)}). {why}");
        }
        return new InvalidOperationException($"`docker create {image}` failed: {detail}\n(set CLEARCOTE_DOCKER_IMAGE or DockerImage to use another image)");
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

    /// Start the image and wait for its CDP endpoint. Nothing is left running when this throws.
    ///
    /// The container is created with --rm and stops itself once no CDP client has been connected for
    /// IdleExitSeconds() (serve.py's CC_IDLE_EXIT_SECONDS), so one whose owner died (Ctrl-C, a kill, a crash)
    /// gives its licence seat back and disappears on its own; the next launch on this machine also sweeps any
    /// still there (SweepStaleAsync). It carries the owner's host name and pid as labels for that sweep. The
    /// licence key and the proxy URL are copied in as a file rather than passed with -e, so they are not part
    /// of the container's configuration (`docker inspect`).
    internal static async Task<(DockerContainer Container, string Exe)> StartContainerAsync(LaunchOptions o)
    {
        var env = ContainerEnv(o);
        var idle = IdleExitSeconds();
        var exe = await CheckDockerAsync().ConfigureAwait(false);
        await SweepStaleAsync(exe).ConfigureAwait(false);
        var image = string.IsNullOrEmpty(o.DockerImage) ? DefaultImage() : o.DockerImage;
        var secrets = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var k in SecretEnv)
            if (env.Remove(k, out var v)) secrets[k] = v;
        if (idle > 0) env["CC_IDLE_EXIT_SECONDS"] = idle.ToString(CultureInfo.InvariantCulture);
        if (secrets.Count > 0) env["CC_SECRETS_FILE"] = SecretsFile;
        var argv = new List<string>
        {
            exe, "create", "--rm", "--platform", "linux/amd64", "--shm-size", "1g", "-p", "127.0.0.1::9222",
            "--label", Label, "--label", $"{OwnerHostLabel}={System.Net.Dns.GetHostName()}", "--label", $"{OwnerPidLabel}={Environment.ProcessId}",
        };
        foreach (var name in env.Keys.OrderBy(k => k, StringComparer.Ordinal)) argv.AddRange(new[] { "-e", name });   // value via the CLI's environment
        if (secrets.ContainsKey("CLEARCOTE_LICENSE_KEY")) argv.AddRange(new[] { "-v", $"{CacheVolume()}:/opt/xdg-cache" });   // the licensed engine downloads once
        argv.Add(image);
        if (!o.Quiet) Console.Error.WriteLine($"[clearcote] no native macOS build: starting the Clearcote Docker image {image} (the first run downloads it)");
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
        if (r.Code != 0 || id.Length == 0) throw CreateFailed(image, r.Code, r.Stderr);
        lock (Live) Live[id] = exe;
        InstallSweep();
        ILogTail? logs = null;
        try
        {
            if (secrets.Count > 0)
            {
                var cp = await Run(new[] { exe, "cp", "-", $"{id}:{SecretsFile[..SecretsFile.LastIndexOf('/')]}" }, null, 60_000, SecretsTar(secrets)).ConfigureAwait(false);
                if (cp.Code != 0) throw new InvalidOperationException($"could not copy the licence/proxy settings into the container: {FirstLine(cp.Stderr)}");
            }
            var st = await Run(new[] { exe, "start", id }, null, 120_000, null).ConfigureAwait(false);
            if (st.Code != 0) throw new InvalidOperationException($"`docker start` failed: {(st.Stderr.Trim() is { Length: > 0 } e ? e : $"exit {st.Code}")}");
            logs = FollowLogs(exe, id);
            var port = await PublishedPortAsync(exe, id).ConfigureAwait(false);
            await WaitReadyAsync(exe, id, port, o.Timeout is float ms && ms > 0 ? (int)Math.Max(ms, 1000) : ReadyTimeoutMs, logs).ConfigureAwait(false);
            return (new DockerContainer(id, image, $"http://127.0.0.1:{port}"), exe);
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
