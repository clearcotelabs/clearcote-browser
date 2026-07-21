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

    /// Default WebRTC to deny non-proxied UDP unless a webrtcIp is set or the caller already set a policy.
    public static List<string> WebrtcDefaultDenyArgs(IEnumerable<string> args, string? webrtcIp)
    {
        if (!string.IsNullOrEmpty(webrtcIp)) return new();
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

    /// A credentialed SOCKS5 proxy can't authenticate in Chromium, so re-route it via --proxy-server and
    /// drop it from the Playwright proxy (credentials are dropped with a warning). Everything else passes
    /// through unchanged. Returns the extra args + the (possibly nulled) proxy to hand to Playwright.
    public static (List<string> Args, ProxyOptions? Proxy) ResolveProxy(ProxyOptions? proxy)
    {
        if (proxy is null) return (new(), null);
        var isSocks = proxy.Server?.StartsWith("socks", StringComparison.OrdinalIgnoreCase) ?? false;
        var hasCreds = !string.IsNullOrEmpty(proxy.Username) || !string.IsNullOrEmpty(proxy.Password);
        if (isSocks && hasCreds && !string.IsNullOrEmpty(proxy.Server))
        {
            Console.Error.WriteLine(
                "[clearcote] SOCKS5 proxy credentials can't be authenticated by Chromium; " +
                "routing via --proxy-server and dropping the credentials. Put the auth on a local relay.");
            return (new() { $"--proxy-server={proxy.Server}" }, null);
        }
        return (new(), proxy);
    }
}
