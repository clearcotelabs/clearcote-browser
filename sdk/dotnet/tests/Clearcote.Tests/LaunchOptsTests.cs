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
            new[] { "--disable-features=BrowsingTopics,BrowsingTopicsDocumentAPI,Fledge,InterestGroupStorage,PrivateAggregationApi,SharedStorageAPI,FencedFrames,WebUSB" },
            LaunchOpts.PrivacySandboxArgs());

    [Fact]
    public void WebrtcDefaultDeny()
    {
        Assert.Equal(new[] { "--webrtc-ip-handling-policy=disable_non_proxied_udp" },
            LaunchOpts.WebrtcDefaultDenyArgs(Array.Empty<string>(), null));
        Assert.Empty(LaunchOpts.WebrtcDefaultDenyArgs(Array.Empty<string>(), "1.2.3.4"));
        Assert.Empty(LaunchOpts.WebrtcDefaultDenyArgs(new[] { "--webrtc-ip-handling-policy=default" }, null));
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

        var (a1, p1) = LaunchOpts.ResolveProxy(new ProxyOptions { Server = "socks5://h:1080", Username = "u", Password = "p" });
        Assert.Equal(new[] { "--proxy-server=socks5://h:1080" }, a1);
        Assert.Null(p1); // credentials can't be authenticated by Chromium -> dropped

        var socksNoCreds = new ProxyOptions { Server = "socks5://h:1080" };
        var (a2, p2) = LaunchOpts.ResolveProxy(socksNoCreds);
        Assert.Empty(a2); Assert.Same(socksNoCreds, p2);

        var httpAuth = new ProxyOptions { Server = "http://h:8080", Username = "u", Password = "p" };
        var (a3, p3) = LaunchOpts.ResolveProxy(httpAuth);
        Assert.Empty(a3); Assert.Same(httpAuth, p3);
    }
}
