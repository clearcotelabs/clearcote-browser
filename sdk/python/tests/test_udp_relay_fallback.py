"""socks5_udp: a proxy that refuses UDP ASSOCIATE gets no relay (udp_relay_or_fallback), so the browser runs as
without socks5_udp instead of with a relay that closes every UDP socket."""
import socket
import threading
import warnings

from clearcote._launchopts import socks5_udp_refused, udp_relay_or_fallback


def fake_socks5(rep, login=False):
    """A local SOCKS5 server answering the greeting, an optional login, and UDP ASSOCIATE with ``rep``."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    seen = []

    def serve():
        c, _ = srv.accept()
        with c:
            c.recv(16)
            c.sendall(bytes([5, 2 if login else 0]))
            if login:
                d = c.recv(512)
                ul = d[1]
                seen.append("login %s:%s" % (d[2:2 + ul].decode(), d[3 + ul:3 + ul + d[2 + ul]].decode()))
                c.sendall(bytes([1, 0]))
            d = c.recv(16)
            seen.append("cmd %d" % d[1])
            c.sendall(bytes([5, rep, 0, 1, 127, 0, 0, 1, 0x30, 0x39]))
        srv.close()
    threading.Thread(target=serve, daemon=True).start()
    return "socks5://127.0.0.1:%d" % srv.getsockname()[1], seen


def test_names_the_refusal():
    server, seen = fake_socks5(2)
    assert socks5_udp_refused({"server": server}) == "reply 2, not allowed by the proxy's rules"
    assert seen == ["cmd 3"]


def test_granted_association_logs_in_with_the_proxy_credentials():
    server, seen = fake_socks5(0, login=True)
    assert socks5_udp_refused({"server": server, "username": "u-1", "password": "p:2"}) is None
    assert seen == ["login u-1:p:2", "cmd 3"]


def test_no_answer_is_not_a_refusal():
    assert socks5_udp_refused({"server": "socks5://127.0.0.1:9"}, timeout=2) is None


def test_keeps_the_relay_when_not_refused():
    assert udp_relay_or_fallback({"server": "socks5://203.0.113.5:1080"}, True, True, probe=lambda p: None) is True


def test_drops_the_relay_with_a_warning_when_refused():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert udp_relay_or_fallback({"server": "socks5://203.0.113.5:1080"}, True, False,
                                     probe=lambda p: "reply 2, not allowed") is False
    assert any("refused a UDP association (reply 2, not allowed)" in str(x.message) for x in w)


def test_asks_nothing_without_socks5_udp_or_a_socks5_proxy():
    calls = []
    probe = lambda p: calls.append(p) or "refused"  # noqa: E731
    assert udp_relay_or_fallback({"server": "socks5://203.0.113.5:1080"}, False, True, probe=probe) is False
    assert udp_relay_or_fallback({"server": "http://203.0.113.5:8080"}, True, True, probe=probe) is True
    assert udp_relay_or_fallback(None, True, True, probe=probe) is True
    assert calls == []
