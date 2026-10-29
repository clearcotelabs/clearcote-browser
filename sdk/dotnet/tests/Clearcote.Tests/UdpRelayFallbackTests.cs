using System.Net;
using System.Net.Sockets;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// Socks5Udp: a proxy that refuses UDP ASSOCIATE gets no relay (LaunchOpts.UdpRelayOrFallbackAsync), so the browser
/// runs as without Socks5Udp instead of with a relay that closes every UDP socket.
public class UdpRelayFallbackTests
{
    /// A local SOCKS5 server answering the greeting, an optional login, and UDP ASSOCIATE with <paramref name="rep"/>.
    private static (string Server, List<string> Seen, Task Done) FakeSocks5(byte rep, bool login = false)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var seen = new List<string>();
        var done = Task.Run(async () =>
        {
            using var c = await listener.AcceptTcpClientAsync();
            var s = c.GetStream();
            var buf = new byte[512];
            await s.ReadAsync(buf);
            await s.WriteAsync(new byte[] { 5, (byte)(login ? 2 : 0) });
            if (login)
            {
                var n = await s.ReadAsync(buf);
                int ul = buf[1];
                seen.Add($"login {System.Text.Encoding.UTF8.GetString(buf, 2, ul)}:{System.Text.Encoding.UTF8.GetString(buf, 3 + ul, buf[2 + ul])}");
                await s.WriteAsync(new byte[] { 1, 0 });
            }
            await s.ReadAsync(buf);
            seen.Add($"cmd {buf[1]}");
            await s.WriteAsync(new byte[] { 5, rep, 0, 1, 127, 0, 0, 1, 0x30, 0x39 });
            listener.Stop();
        });
        return ($"socks5://127.0.0.1:{((IPEndPoint)listener.LocalEndpoint).Port}", seen, done);
    }

    [Fact]
    public async Task Names_the_refusal()
    {
        var (server, seen, done) = FakeSocks5(2);
        Assert.Equal("reply 2, not allowed by the proxy's rules", await LaunchOpts.Socks5UdpRefusedAsync(new ProxyOptions { Server = server }));
        await done;
        Assert.Equal(new[] { "cmd 3" }, seen);
    }

    [Fact]
    public async Task A_granted_association_logs_in_with_the_proxy_credentials()
    {
        var (server, seen, done) = FakeSocks5(0, login: true);
        Assert.Null(await LaunchOpts.Socks5UdpRefusedAsync(new ProxyOptions { Server = server, Username = "u-1", Password = "p:2" }));
        await done;
        Assert.Equal(new[] { "login u-1:p:2", "cmd 3" }, seen);
    }

    [Fact]
    public async Task No_answer_is_not_a_refusal()
        => Assert.Null(await LaunchOpts.Socks5UdpRefusedAsync(new ProxyOptions { Server = "socks5://127.0.0.1:9" }, 2000));

    [Fact]
    public async Task Keeps_the_relay_when_not_refused()
        => Assert.True(await LaunchOpts.UdpRelayOrFallbackAsync(new ProxyOptions { Server = "socks5://203.0.113.5:1080" }, true, true,
            _ => Task.FromResult<string?>(null)));

    [Fact]
    public async Task Drops_the_relay_when_refused()
        => Assert.False(await LaunchOpts.UdpRelayOrFallbackAsync(new ProxyOptions { Server = "socks5://203.0.113.5:1080" }, true, true,
            _ => Task.FromResult<string?>("reply 2, not allowed")));

    [Fact]
    public async Task Asks_nothing_without_Socks5Udp_or_a_socks5_proxy()
    {
        var calls = 0;
        Task<string?> Probe(ProxyOptions? _) { calls++; return Task.FromResult<string?>("refused"); }
        Assert.False(await LaunchOpts.UdpRelayOrFallbackAsync(new ProxyOptions { Server = "socks5://203.0.113.5:1080" }, false, true, Probe));
        Assert.True(await LaunchOpts.UdpRelayOrFallbackAsync(new ProxyOptions { Server = "http://203.0.113.5:8080" }, true, true, Probe));
        Assert.True(await LaunchOpts.UdpRelayOrFallbackAsync(null, true, true, Probe));
        Assert.Equal(0, calls);
    }
}
