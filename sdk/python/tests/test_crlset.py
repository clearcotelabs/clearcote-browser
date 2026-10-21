import hashlib
import http.client
import io
import json
import os
import ssl
import struct
import zipfile

import pytest

from clearcote import _crlset
from clearcote._crlset import (
    apply_crlset_launch,
    fetch_crlset,
    crlset_mode,
    find_crlset,
    install_crlset,
    parse_header,
    seed_crlset,
    unsuppress_component_updater,
)


def crlset_bytes(sequence, body=b""):
    header = json.dumps({"Version": 0, "ContentType": "CRLSet", "Sequence": sequence, "BlockedSPKIs": []}).encode()
    return struct.pack("<H", len(header)) + header + body


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_CRLSET_DIR", str(tmp_path / "cache"))
    for name in ("CLEARCOTE_CRLSET", "CLEARCOTE_CRLSET_FILE", "CLEARCOTE_CRLSET_TIMEOUT", "CLEARCOTE_CRLSET_URL"):
        monkeypatch.delenv(name, raising=False)
    _crlset._WARNED.clear()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_parse_header_reads_the_sequence_and_rejects_other_files():
    assert parse_header(crlset_bytes(10849, b"\x00" * 40))["Sequence"] == 10849
    for bad in (b"", b"\x05\x00hello", struct.pack("<H", 2) + b"{}"):
        with pytest.raises(ValueError):
            parse_header(bad)


def test_install_takes_a_file_or_chromes_component_directory_and_find_returns_the_newest(tmp_path):
    chrome = tmp_path / "CertificateRevocation"
    write(chrome / "10848" / "crl-set", crlset_bytes(10848))
    write(chrome / "10849" / "crl-set", crlset_bytes(10849))
    cached = install_crlset(str(chrome))
    assert cached.endswith(os.path.join("10849", "crl-set"))
    install_crlset(str(write(tmp_path / "old" / "crl-set", crlset_bytes(10700))))
    assert find_crlset() == cached


def test_find_prefers_the_explicit_path_then_the_env_file(tmp_path, monkeypatch):
    install_crlset(str(write(tmp_path / "a" / "crl-set", crlset_bytes(5))))
    env_file = write(tmp_path / "env" / "crl-set", crlset_bytes(6))
    monkeypatch.setenv("CLEARCOTE_CRLSET_FILE", str(env_file))
    explicit = write(tmp_path / "x" / "crl-set", crlset_bytes(7))
    assert find_crlset(str(explicit)) == str(explicit)
    assert find_crlset() == str(env_file)


def test_find_is_none_without_a_cache():
    assert find_crlset() is None


def test_seed_writes_the_component_layout_and_keeps_a_newer_one(tmp_path):
    profile = tmp_path / "profile"
    src = write(tmp_path / "s" / "crl-set", crlset_bytes(10849, b"body"))
    assert seed_crlset(str(profile), str(src)) == 10849
    version_dir = profile / "CertificateRevocation" / "10849"
    assert (version_dir / "crl-set").read_bytes() == src.read_bytes()
    assert json.loads((version_dir / "manifest.json").read_text())["version"] == "10849"
    write(profile / "CertificateRevocation" / "10900" / "crl-set", crlset_bytes(10900))
    assert seed_crlset(str(profile), str(src)) == 10900


@pytest.mark.parametrize("value, env, expected", [
    (None, None, ("off", None)),
    (None, "0", ("off", None)),
    (None, "1", ("wait", None)),
    (None, "nowait", ("nowait", None)),
    (True, "0", ("wait", None)),
    (False, "1", ("off", None)),
    ("nowait", None, ("nowait", None)),
    ("/x/crl-set", None, ("wait", "/x/crl-set")),
])
def test_mode(value, env, expected, monkeypatch):
    if env is not None:
        monkeypatch.setenv("CLEARCOTE_CRLSET", env)
    assert crlset_mode(value) == expected


def test_mode_rejects_other_values():
    with pytest.raises(ValueError):
        crlset_mode(3)


@pytest.mark.parametrize("given, expected", [
    (None, ["--disable-component-update"]),
    (["--enable-automation"], ["--enable-automation", "--disable-component-update"]),
    (["--disable-component-update"], ["--disable-component-update"]),
    (True, True),
])
def test_unsuppress_component_updater(given, expected):
    kwargs = {} if given is None else {"ignore_default_args": given}
    unsuppress_component_updater(kwargs)
    assert kwargs["ignore_default_args"] == expected


def test_apply_seeds_unsuppresses_and_pops_its_options(tmp_path):
    install_crlset(str(write(tmp_path / "s" / "crl-set", crlset_bytes(42))))
    kwargs = {"crlset": True, "crlset_timeout": 4, "ignore_default_args": ["--enable-automation"]}
    plan = apply_crlset_launch(str(tmp_path / "p"), kwargs, quiet=True)
    assert (plan.timeout, plan.sequence) == (4.0, 42)
    assert "crlset" not in kwargs and "crlset_timeout" not in kwargs
    assert "--disable-component-update" in kwargs["ignore_default_args"]
    assert (tmp_path / "p" / "CertificateRevocation" / "42" / "crl-set").is_file()


def test_apply_nowait_seeds_without_a_plan(tmp_path):
    install_crlset(str(write(tmp_path / "s" / "crl-set", crlset_bytes(42))))
    kwargs = {"crlset": "nowait"}
    assert apply_crlset_launch(str(tmp_path / "p"), kwargs, quiet=True) is None
    assert kwargs["ignore_default_args"] == ["--disable-component-update"]
    assert (tmp_path / "p" / "CertificateRevocation" / "42").is_dir()


def test_apply_is_silent_and_untouched_by_default_without_a_cache(tmp_path, capsys):
    kwargs = {}
    assert apply_crlset_launch(str(tmp_path / "p"), kwargs) is None
    assert kwargs == {}
    assert capsys.readouterr().err == ""


def test_apply_warns_once_when_the_first_fetch_fails(tmp_path, capsys, monkeypatch):
    def offline():
        raise OSError("firewall")
    monkeypatch.setattr(_crlset, "_download_from_google", offline)
    for _ in range(2):
        assert apply_crlset_launch(str(tmp_path / "p"), {"crlset": True}) is None
    assert capsys.readouterr().err.count("could not fetch a CRLSet") == 1


def test_apply_fetches_into_an_empty_cache_then_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(_crlset, "_download_from_google", lambda: crlset_bytes(10850))
    plan = apply_crlset_launch(str(tmp_path / "p"), {"crlset": True}, quiet=True)
    assert plan.sequence == 10850
    assert find_crlset().endswith(os.path.join("10850", "crl-set"))
    assert _crlset._stamp_fresh()


def test_a_stale_cache_is_used_and_refreshed_in_the_background(tmp_path, monkeypatch):
    cached = install_crlset(str(write(tmp_path / "s" / "crl-set", crlset_bytes(10849))))
    refreshes = []
    monkeypatch.setattr(_crlset, "_refresh_in_background", lambda: refreshes.append(1))
    assert _crlset.resolve_crlset() == cached and refreshes == [1]  # no stamp: stale
    _crlset._touch_stamp()
    assert _crlset.resolve_crlset() == cached and refreshes == [1]  # fresh: no refresh


def test_fetch_from_a_mirror_url(tmp_path, monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    seen = []
    monkeypatch.setattr(_crlset.urllib.request, "urlopen", lambda url, timeout: seen.append(url) or Resp(crlset_bytes(77)))
    monkeypatch.setenv("CLEARCOTE_CRLSET_URL", "https://mirror.example/crl-set")
    assert fetch_crlset().endswith(os.path.join("77", "crl-set"))
    assert seen == ["https://mirror.example/crl-set"]


def _crx(payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("crl-set", payload)
        zf.writestr("manifest.json", "{}")
    header = b"\x00" * 8
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + buf.getvalue()


@pytest.mark.parametrize("good_hash", [True, False])
def test_download_from_google_verifies_the_package_hash(monkeypatch, good_hash):
    from clearcote import _widevine
    blob = _crx(crlset_bytes(10850))
    digest = hashlib.sha256(blob).hexdigest() if good_hash else "00" * 32
    bodies = []

    def post(url, body):
        bodies.append(body)
        return {"response": {"app": [{"appid": _crlset.CRLSET_APP_ID, "updatecheck": {"status": "ok", "pipelines": [
            {"operations": [{"type": "download", "urls": [{"url": "http://dl/c.crx3"}], "out": {"sha256": digest}}]}]}}]}}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(_widevine, "_post_json", post)
    monkeypatch.setattr(_crlset.urllib.request, "urlopen", lambda req, timeout: Resp(blob))
    if good_hash:
        assert parse_header(_crlset._download_from_google())["Sequence"] == 10850
    else:
        with pytest.raises(RuntimeError, match="sha256"):
            _crlset._download_from_google()
    assert bodies[0]["request"]["app"][0]["appid"] == "hfnkpimlhhgieaddgfemjhofmfblmnib"


def test_apply_off_does_nothing(tmp_path):
    install_crlset(str(write(tmp_path / "s" / "crl-set", crlset_bytes(42))))
    kwargs = {"crlset": False}
    assert apply_crlset_launch(str(tmp_path / "p"), kwargs) is None
    assert "ignore_default_args" not in kwargs
    assert not (tmp_path / "p").exists()


def test_apply_never_raises_on_a_broken_path(tmp_path, capsys):
    assert apply_crlset_launch(str(tmp_path / "p"), {"crlset": str(tmp_path / "missing")}) is None
    assert "crlset" in capsys.readouterr().err


def test_probe_server_counts_connections_not_requests():
    server = _crlset._ProbeServer()
    try:
        host, port = server.url[len("https://"):].rstrip("/").split(":")
        ctx = ssl._create_unverified_context()
        conn = http.client.HTTPSConnection(host, int(port), context=ctx, timeout=5)
        for _ in range(3):  # keep-alive: one connection
            conn.request("GET", "/")
            assert conn.getresponse().read() == b""
        assert server.count() == 1
        conn.close()
        second = http.client.HTTPSConnection(host, int(port), context=ctx, timeout=5)
        second.request("GET", "/")
        second.getresponse().read()
        second.close()
        assert server.count() == 2
    finally:
        server.close()


@pytest.mark.parametrize("proxy, expected", [
    (None, []),
    ({"server": "http://p:1"}, ["--proxy-bypass-list=<-loopback>;127.0.0.1:9"]),
    ({"server": "http://p:1", "bypass": ".corp, intra"}, ["--proxy-bypass-list=*.corp;intra;<-loopback>;127.0.0.1:9"]),
    ({"server": "http://p:1", "bypass": "<-loopback>"}, ["--proxy-bypass-list=<-loopback>;127.0.0.1:9"]),
])
def test_the_probe_port_bypasses_a_playwright_proxy_last(proxy, expected, monkeypatch):
    monkeypatch.delenv("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK", raising=False)
    pw_kwargs = {} if proxy is None else {"proxy": proxy}
    assert _crlset.proxy_bypass_args(pw_kwargs, "127.0.0.1:9") == expected
    assert pw_kwargs == ({} if proxy is None else {"proxy": proxy})  # the proxy itself is left alone


def test_readiness_starts_the_probe_before_launch_and_closes_it(tmp_path):
    plan = _crlset.CRLSetReadiness(1.0, 42, quiet=True)
    args = plan.before_launch({"proxy": {"server": "http://p:1"}})
    assert args == ["--proxy-bypass-list=<-loopback>;" + plan.server.url[len("https://"):].rstrip("/")]
    plan.close()
    assert plan.server is None
    assert plan.wait(context=None) is False  # nothing to wait with once closed


def test_the_probe_key_pair_ships_with_the_package():
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(_crlset.PROBE_PEM)
