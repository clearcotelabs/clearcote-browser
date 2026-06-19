from clearcote.geoip import accept_language_for_country, resolve_geo


def test_accept_language_for_country():
    assert accept_language_for_country("US") == "en-US,en"
    assert accept_language_for_country("de") == "de-DE,de,en"  # case-insensitive
    assert accept_language_for_country("BR") == "pt-BR,pt,en"
    assert accept_language_for_country("JP") == "ja-JP,ja,en"


def test_accept_language_fallback():
    assert accept_language_for_country("ZZ") == "en-US,en"
    assert accept_language_for_country("") == "en-US,en"
    assert accept_language_for_country(None) == "en-US,en"


def test_accept_language_has_no_q_weights():
    # A ';q=' in --accept-lang trips a Chromium DCHECK; the map must never contain one.
    for cc in ("US", "DE", "FR", "CA", "BR", "JP", "ZZ"):
        assert ";" not in accept_language_for_country(cc)


def test_resolve_geo_socks_returns_none():
    # SOCKS can't be used for the geo lookup and we must not fall back to the local IP
    # under a proxy (wrong region) — returns None without any network call.
    assert resolve_geo({"server": "socks5://127.0.0.1:9050"}, quiet=True) is None
