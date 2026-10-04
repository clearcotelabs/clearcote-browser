using Xunit;

namespace Clearcote.Tests;

public class LaunchOptsTests
{
    [Fact]
    public void MergeFeatureFlags_collapses_to_one_of_each()
    {
        var merged = LaunchOpts.MergeFeatureFlags(new[]
        {
            "--enable-features=A,B", "--mute-audio", "--disable-features=D", "--enable-features=C", "--disable-features=E",
        });
        Assert.Contains("--mute-audio", merged);
        Assert.Single(merged, a => a.StartsWith("--enable-features="));
        Assert.Single(merged, a => a.StartsWith("--disable-features="));
        Assert.Contains("--enable-features=A,B,C", merged);
        Assert.Contains("--disable-features=D,E", merged);
    }

    [Fact]
    public void PrivacySandboxArgs_is_exact()
        => Assert.Equal(
            new[] { "--disable-features=BrowsingTopics,BrowsingTopicsDocumentAPI,Fledge,InterestGroupStorage,PrivateAggregationApi,SharedStorageAPI,FencedFrames" },
            LaunchOpts.PrivacySandboxArgs());

    [Fact]
    public void WebrtcDefaultDeny()
    {
        Assert.Equal(new[] { "--webrtc-ip-handling-policy=disable_non_proxied_udp" },
            LaunchOpts.WebrtcDefaultDenyArgs(Array.Empty<string>(), null));
        // Regression: this used to be Assert.Empty when a webrtcIp was set, on the theory that the
        // engine's srflx fabrication covered WebRTC. It does not. A page using
        // iceTransportPolicy:"relay" forces TURN; TURN prefers UDP; an HTTP/SOCKS proxy carries only
        // TCP — so the UDP left on the host's own path and the TURN server read the real public IP
        // off the packet, with no candidate involved for the fabrication to rewrite. geoip sets
        // WebrtcIp for you, so the coherent configurations were the exposed ones.
        Assert.Equal(new[] { "--webrtc-ip-handling-policy=disable_non_proxied_udp" },
            LaunchOpts.WebrtcDefaultDenyArgs(Array.Empty<string>(), "1.2.3.4"));
        // An explicit caller policy still wins — with or without a webrtcIp.
        Assert.Empty(LaunchOpts.WebrtcDefaultDenyArgs(new[] { "--webrtc-ip-handling-policy=default" }, null));
        Assert.Empty(LaunchOpts.WebrtcDefaultDenyArgs(new[] { "--force-webrtc-ip-handling-policy=default" }, "1.2.3.4"));
    }

    [Fact]
    public void QuicArgs()
    {
        Assert.Equal(new[] { "--disable-quic" }, LaunchOpts.QuicArgs(new ProxyOptions { Server = "socks5://host:1080" }));
        Assert.Equal(new[] { "--disable-quic" }, LaunchOpts.QuicArgs(new ProxyOptions { Server = "http://host:8080" }));
        Assert.Empty(LaunchOpts.QuicArgs(null));
        Assert.Empty(LaunchOpts.QuicArgs(new ProxyOptions()));
    }

    [Fact]
    public void Socks5UdpArgs_emitted_only_when_opted_in()
    {
        var socks5 = new ProxyOptions { Server = "socks5://gw.example.com:1080", Username = "u", Password = "p" };
        Assert.Equal(new[] { "--socks5-udp" }, LaunchOpts.Socks5UdpArgs(true, socks5));
        Assert.Empty(LaunchOpts.Socks5UdpArgs(false, socks5));
    }

    // UDP ASSOCIATE is a SOCKS5 command. Emitting the switch for a transport that cannot carry a
    // datagram would be accepted and silently do nothing — the failure mode this guards against.
    [Fact]
    public void Socks5UdpArgs_silent_for_transports_that_cannot_relay_udp()
    {
        foreach (var server in new[] { "http://p:8080", "https://p:8443", "socks4://p:1080" })
            Assert.Empty(LaunchOpts.Socks5UdpArgs(true, new ProxyOptions { Server = server }));
    }

    [Fact]
    public void Socks5UdpArgs_silent_without_a_proxy()
    {
        Assert.Empty(LaunchOpts.Socks5UdpArgs(true, null));
        Assert.Empty(LaunchOpts.Socks5UdpArgs(true, new ProxyOptions()));
        Assert.Empty(LaunchOpts.Socks5UdpArgs(true, new ProxyOptions { Server = "" }));
    }

    [Fact]
    public void Socks5UdpArgs_accepts_socks5h_and_is_case_insensitive()
    {
        Assert.Equal(new[] { "--socks5-udp" }, LaunchOpts.Socks5UdpArgs(true, new ProxyOptions { Server = "socks5h://p:1080" }));
        Assert.Equal(new[] { "--socks5-udp" }, LaunchOpts.Socks5UdpArgs(true, new ProxyOptions { Server = "SOCKS5://p:1080" }));
    }

    // Verified against the proxy's own log: the association is established with the deny policy in
    // force, so enabling UDP does not mean weakening the leak default.
    [Fact]
    public void Socks5UdpArgs_composes_with_the_webrtc_deny_default()
    {
        var socks5 = new ProxyOptions { Server = "socks5://gw.example.com:1080" };
        var args = LaunchOpts.Socks5UdpArgs(true, socks5);
        var combined = args.Concat(LaunchOpts.WebrtcDefaultDenyArgs(args, null)).ToList();
        Assert.Contains("--socks5-udp", combined);
        Assert.Contains("--webrtc-ip-handling-policy=disable_non_proxied_udp", combined);
    }

    [Fact]
    public void ExtensionArgs()
    {
        Assert.Empty(LaunchOpts.ExtensionArgs(null));
        Assert.Empty(LaunchOpts.ExtensionArgs(Array.Empty<string>()));
        Assert.Equal(new[] { "--load-extension=/a,/b", "--disable-extensions-except=/a,/b" },
            LaunchOpts.ExtensionArgs(new[] { "/a", "/b" }));
    }

    [Fact]
    public void ResolveProxy_reroutes_credentialed_socks5_only()
    {
        var (a0, p0) = LaunchOpts.ResolveProxy(null);
        Assert.Empty(a0); Assert.Null(p0);

        // clearcote implements RFC 1929, so the credentials go to the engine rather than being
        // dropped: bare server on --proxy-server, user:pass on --socks5-credentials.
        var (a1, p1) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://h:1080", Username = "u", Password = "p" });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080", "--socks5-credentials=u:p" }, a1);
        Assert.Null(p1); // Playwright rejects a credentialed SOCKS descriptor

        // userinfo already in the URL is stripped -- it must not reach --proxy-server
        var (a1b, _) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://u:p@h:1080", Username = "u", Password = "p" });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080", "--socks5-credentials=u:p" }, a1b);

        var socksNoCreds = new ProxyOptions { Server = "socks5://h:1080" };
        var (a2, p2) = LaunchOpts.ResolveProxy(socksNoCreds);
        Assert.Empty(a2); Assert.Same(socksNoCreds, p2);

        var httpAuth = new ProxyOptions { Server = "http://h:8080", Username = "u", Password = "p" };
        var (a3, p3) = LaunchOpts.ResolveProxy(httpAuth);
        Assert.Empty(a3); Assert.Same(httpAuth, p3);
    }

    // Regression (found 2026-09-24, proven at runtime on r27): credentials written INTO the URL were
    // ignored, because only the fields were read. The proxy went to Playwright, which rebuilds the
    // server as scheme://host:port, and the browser's SOCKS5 greeting offered only "no auth".
    [Fact]
    public void ResolveProxy_routes_socks5_url_credentials_to_the_engine()
    {
        var (args, pw) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://user:pass@h:1080" });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080", "--socks5-credentials=user:pass" }, args);
        Assert.Null(pw);
    }

    [Theory]
    [InlineData("socks5://us%40er:p%3Ass%2Fw@h:1080", "us@er:p:ss/w")] // percent-decoded
    [InlineData("socks5://user:p@ss@h:1080", "user:p@ss")]             // split at the LAST '@'
    [InlineData("socks5://user:100%@h:1080", "user:100%")]             // a malformed escape stays literal
    public void ResolveProxy_decodes_url_credentials(string server, string creds)
    {
        var (args, _) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = server });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080", $"--socks5-credentials={creds}" }, args);
    }

    [Fact]
    public void ResolveProxy_hands_url_credentials_to_playwright_as_fields()
    {
        // Playwright drops userinfo from Server: left there, every request would 407.
        var (args, pw) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "http://u:p%21@h:3128", Bypass = "*.internal" });
        Assert.Empty(args);
        Assert.NotNull(pw);
        Assert.Equal("http://h:3128", pw!.Server);
        Assert.Equal("u", pw.Username);
        Assert.Equal("p!", pw.Password);
        Assert.Equal("*.internal", pw.Bypass);
    }

    [Fact]
    public void ResolveProxy_lets_the_fields_win_over_userinfo_per_field()
    {
        var (args, _) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://urluser:urlpass@h:1080", Password = "field" });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080", "--socks5-credentials=urluser:field" }, args);
    }

    [Fact]
    public void ResolveProxy_keeps_the_bypass_list_when_it_owns_a_socks5_proxy()
    {
        // Parity with the Node and Python SDKs: the proxy no longer reaches Playwright, so its bypass
        // list has to travel with the engine switches or it is silently dropped.
        var (args, _) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://h:1080", Username = "u", Password = "p", Bypass = " *.internal " });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080", "--socks5-credentials=u:p", "--proxy-bypass-list=*.internal" }, args);
    }

    [Fact]
    public void PortableArgs_covers_both_modes()
    {
        Assert.Empty(LaunchOpts.PortableArgs());
        Assert.Equal(new[] { "--portable-profile" }, LaunchOpts.PortableArgs(portableProfile: true));
        Assert.Equal(new[] { "--profile-encryption-key=s3cret" }, LaunchOpts.PortableArgs(encryptionKey: "s3cret"));
        // an explicit key wins over the generated-file mode
        Assert.Equal(new[] { "--profile-encryption-key=s3cret" },
                     LaunchOpts.PortableArgs(portableProfile: true, encryptionKey: "s3cret"));
    }

    // ----------------------------------------------------------------- web bluetooth
    // Web Bluetooth is compiled in but its DEFAULT follows the build platform, so the same persona
    // is a tell in opposite directions depending on which build is running: a Linux build serving
    // a Windows persona exposed navigator.usb/serial/hid but not navigator.bluetooth (a
    // combination no real Windows Chrome produces), while a Windows build under a LINUX claim
    // exposes navigator.bluetooth, which genuine Chrome 154 on Linux does not have (measured
    // 2026-10-04). The switch is derived from the CLAIM alone, so unlike the earlier version of
    // these tests the HOST no longer enters into it and the assertions are unconditional.
    [Fact]
    public void WebBluetoothArgs_EnabledForADesktopClaimOnEveryHost()
    {
        foreach (var claimed in new[] { "windows", "macos", "android", "chromeos" })
            Assert.Equal(new[] { "--enable-features=WebBluetooth" },
                         LaunchOpts.WebBluetoothArgs(claimed));
    }

    [Fact]
    public void WebBluetoothArgs_DisabledUnderALinuxClaimOnEveryHost()
    {
        // Genuine Chrome 154 on Linux exposes no navigator.bluetooth at all. Withholding the
        // ENABLE switch is not enough off Linux: measured on the r30 Windows build,
        // 'bluetooth' in navigator was still true under a Linux claim until --disable-features
        // landed, because that build ships the feature stable.
        Assert.Equal(new[] { "--disable-features=WebBluetooth" },
                     LaunchOpts.WebBluetoothArgs("linux"));
    }

    [Fact]
    public void WebBluetoothArgs_BareCallFollowsTheHost()
    {
        // No claim means no persona switches, so the page sees the real host OS and the switch has
        // to agree with THAT. Assert against the host this test actually runs on.
        var expected = System.Runtime.InteropServices.RuntimeInformation.IsOSPlatform(
                           System.Runtime.InteropServices.OSPlatform.Linux)
            ? "--disable-features=WebBluetooth"
            : "--enable-features=WebBluetooth";
        Assert.Equal(new[] { expected }, LaunchOpts.WebBluetoothArgs());
    }

    [Fact]
    public void WebBluetoothArgs_FoldIntoSingleEnableFeatures()
    {
        // Chromium honours only the LAST --enable-features, so a second occurrence would
        // silently drop WebBluetooth.
        var input = new List<string>(LaunchOpts.WebBluetoothArgs("windows"))
        {
            "--enable-features=SomethingElse",
        };
        var merged = LaunchOpts.MergeFeatureFlags(input);
        var enables = merged.FindAll(a => a.StartsWith("--enable-features="));
        Assert.Single(enables);
        Assert.Contains("SomethingElse", enables[0]);
        Assert.Contains("WebBluetooth", enables[0]);
    }

    [Fact]
    public void WebBluetoothArgs_FoldIntoSingleDisableFeatures()
    {
        // Same hazard on the other side: only the LAST --disable-features is honoured.
        var input = new List<string>(LaunchOpts.WebBluetoothArgs("linux"))
        {
            "--disable-features=SomethingElse",
        };
        var merged = LaunchOpts.MergeFeatureFlags(input);
        var disables = merged.FindAll(a => a.StartsWith("--disable-features="));
        Assert.Single(disables);
        Assert.Contains("SomethingElse", disables[0]);
        Assert.Contains("WebBluetooth", disables[0]);
    }
}
