using System.Text.RegularExpressions;
namespace Clearcote;

/// Proxy descriptor (mirrors the Playwright proxy shape the Node SDK reads).
public sealed class ProxyOptions
{
    public string? Server { get; set; }
    public string? Username { get; set; }
    public string? Password { get; set; }
    public string? Bypass { get; set; }
}

/// Default/always-on Chromium arg helpers (ports launchopts.ts).
public static class LaunchOpts
{
    /// Privacy-Sandbox features Clearcote disables by default (a stock, un-enrolled Chrome profile).
    /// WebUSB is deliberately excluded: it is a device API, not a Privacy Sandbox feature, and it
    /// ships alongside Web Serial/WebHID/Web Bluetooth under identical gating. Disabling only
    /// WebUSB produced a device-API family split no real Chromium exhibits.
    public static readonly string[] PrivacySandboxFeatures =
    {
        "BrowsingTopics", "BrowsingTopicsDocumentAPI", "Fledge", "InterestGroupStorage",
        "PrivateAggregationApi", "SharedStorageAPI", "FencedFrames",
    };

    public static List<string> PrivacySandboxArgs()
        => new() { $"--disable-features={string.Join(",", PrivacySandboxFeatures)}" };

    /// Chromium keeps only the LAST --enable-features / --disable-features; collapse all occurrences
    /// into one of each (order-preserving for the rest, de-duped values).
    public static List<string> MergeFeatureFlags(IEnumerable<string> args)
    {
        var enabled = new List<string>();
        var disabled = new List<string>();
        var rest = new List<string>();
        foreach (var a in args)
        {
            if (a.StartsWith("--enable-features="))
                enabled.AddRange(a["--enable-features=".Length..].Split(',', StringSplitOptions.RemoveEmptyEntries));
            else if (a.StartsWith("--disable-features="))
                disabled.AddRange(a["--disable-features=".Length..].Split(',', StringSplitOptions.RemoveEmptyEntries));
            else rest.Add(a);
        }
        if (enabled.Count > 0) rest.Add($"--enable-features={string.Join(",", enabled.Distinct())}");
        if (disabled.Count > 0) rest.Add($"--disable-features={string.Join(",", disabled.Distinct())}");
        return rest;
    }

    /// Disable QUIC/HTTP-3 when a proxy is set (a SOCKS5/HTTP proxy carries only TCP; no UDP around it).
    public static List<string> QuicArgs(ProxyOptions? proxy)
        => proxy is not null && !string.IsNullOrEmpty(proxy.Server) ? new() { "--disable-quic" } : new();

    /// Default WebRTC to deny non-proxied UDP, so no UDP can egress around the proxy.
    ///
    /// This used to be skipped whenever a webrtcIp was set, on the theory that the engine's srflx
    /// fabrication already covered WebRTC. It does not — fabrication rewrites what the browser
    /// reports (beating a page that reads the candidate), while this policy stops UDP leaving the
    /// machine (beating a server that watches where packets arrive from). A page using
    /// iceTransportPolicy: "relay" forces TURN, TURN prefers UDP, and an HTTP/SOCKS proxy carries
    /// only TCP — so the UDP left on the host's own path and the TURN server read the real public
    /// address off the packet. Only an explicit caller policy suppresses this now.
    ///
    /// Trade-off, not a free win: peer connections that genuinely need UDP will not establish.
    /// Callers who need working WebRTC through a proxy want a transport that carries UDP (SOCKS5
    /// with UDP ASSOCIATE, or a full tunnel) and can set their own policy to opt out.
    /// webrtcIp is accepted and ignored, for call-site compatibility.
    /// <summary>Switches to expose <c>navigator.bluetooth</c> on Linux hosts (empty elsewhere).</summary>
    /// <remarks>
    /// Web Bluetooth is compiled into the engine but runtime-disabled on Linux only:
    /// Chromium's runtime_enabled_features.json5 gives WebBluetooth status "stable" on Win/Mac/Android/
    /// ChromeOS and lets Linux fall through to "default": "experimental", and content_features.cc
    /// declares kWebBluetooth FEATURE_DISABLED_BY_DEFAULT. So a Linux host serving a Windows persona
    /// reports navigator.usb, navigator.serial and navigator.hid but NOT navigator.bluetooth - a
    /// combination no real Windows Chrome produces, and an OS-origin tell that survives every string
    /// spoof. One flag restores it on the shipped binary; no rebuild is involved.
    /// 
    /// Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
    /// and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
    /// real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
    /// this flag produces. getAvailability() resolves false and requestDevice() rejects NotFoundError
    /// on a machine with no adapter, matching a real desktop without Bluetooth hardware.
    /// </remarks>
    public static List<string> WebBluetoothArgs()
    {
        // Win/Mac builds ship WebBluetooth stable; the flag would be a no-op there.
        if (!System.Runtime.InteropServices.RuntimeInformation.IsOSPlatform(
                System.Runtime.InteropServices.OSPlatform.Linux))
            return new List<string>();
        return new List<string> { "--enable-features=WebBluetooth" };
    }

    public static List<string> WebrtcDefaultDenyArgs(IEnumerable<string> args, string? webrtcIp = null)
    {
        if (args.Any(a => a.StartsWith("--webrtc-ip-handling-policy")
                          || a.StartsWith("--force-webrtc-ip-handling-policy")))
            return new();
        return new() { "--webrtc-ip-handling-policy=disable_non_proxied_udp" };
    }

    /// --load-extension + --disable-extensions-except (both needed), only when paths are given.
    public static List<string> ExtensionArgs(IReadOnlyList<string>? paths)
    {
        if (paths is null || paths.Count == 0) return new();
        var joined = string.Join(",", paths);
        return new() { $"--load-extension={joined}", $"--disable-extensions-except={joined}" };
    }

    /// Playwright rejects credentials in its SOCKS proxy descriptor, so a
    /// socks5://user:pass@host:port proxy is routed through --proxy-server instead and the
    /// credentials handed to the engine via --socks5-credentials. Clearcote implements RFC 1929
    /// username/password authentication, which stock Chromium does not, so no local relay is
    /// needed. Everything else passes through unchanged. Returns the extra args + the (possibly
    /// nulled) proxy to hand to Playwright.
    public static (List<string> Args, ProxyOptions? Proxy) ResolveProxy(ProxyOptions? proxy)
    {
        if (proxy is null) return (new(), null);
        var isSocks = proxy.Server?.StartsWith("socks", StringComparison.OrdinalIgnoreCase) ?? false;
        var hasCreds = !string.IsNullOrEmpty(proxy.Username) || !string.IsNullOrEmpty(proxy.Password);
        if (isSocks && hasCreds && !string.IsNullOrEmpty(proxy.Server))
        {
            // Strip any userinfo already in the URL; the engine takes it via its own switch.
            var bare = Regex.Replace(proxy.Server!, "^([a-zA-Z0-9+.-]+://)[^/@]*@", "$1");
            var creds = $"{proxy.Username ?? string.Empty}:{proxy.Password ?? string.Empty}";
            return (new() { $"--proxy-server={bare}", $"--socks5-credentials={creds}" }, null);
        }
        return (new(), proxy);
    }

    /// Keep the profile's cookie encryption key with the profile instead of the OS keystore, so the
    /// whole user data directory can be copied to another machine. `encryptionKey` derives the key
    /// from a caller-supplied secret and writes nothing to disk; `portableProfile` generates one and
    /// stores it in the profile.
    public static List<string> PortableArgs(bool portableProfile = false, string? encryptionKey = null)
    {
        if (!string.IsNullOrEmpty(encryptionKey)) return new() { $"--profile-encryption-key={encryptionKey}" };
        return portableProfile ? new() { "--portable-profile" } : new();
    }
}
