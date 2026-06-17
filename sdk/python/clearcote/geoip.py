"""geoip: resolve the egress IP's geo (timezone + language + lat/lon) so the browser's timezone /
Accept-Language / geolocation match the proxy actually in use — the way Camoufox and similar tools
do it. When a proxy is configured the lookup runs THROUGH that proxy (so we get the proxy's exit
geo, not the local machine's); with no proxy it uses the direct connection. Uses ip-api.com (free,
no key) + a country -> Accept-Language table. Stdlib only (urllib).

Supports http(s) proxies (the common case). A SOCKS proxy is skipped (we never fall back to the
local IP under a proxy, which would set the wrong region) — pass timezone/accept_language
explicitly in that case.
"""

import json
import sys
import urllib.request
from urllib.parse import quote, urlsplit, urlunsplit

GEO_URL = "http://ip-api.com/json/?fields=status,message,countryCode,timezone,lat,lon,query"


def _log(quiet, msg):
    if not quiet:
        sys.stderr.write(f"[clearcote] {msg}\n")
        sys.stderr.flush()


def _proxy_url(proxy):
    """Playwright proxy dict -> a proxy URL with embedded credentials."""
    if not proxy or not proxy.get("server"):
        return None
    server = proxy["server"]
    if "://" not in server:
        server = "http://" + server
    user = proxy.get("username")
    if not user:
        return server
    parts = urlsplit(server)
    cred = quote(user, safe="")
    if proxy.get("password"):
        cred += ":" + quote(proxy["password"], safe="")
    netloc = f"{cred}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


# country (ISO-3166 alpha-2) -> Accept-Language. Plain comma-separated tag list (NO ;q= weights —
# Chromium's --accept-lang adds those). Heuristic; users can override. Falls back to en-US,en.
COUNTRY_LANG = {
    "US": "en-US,en", "GB": "en-GB,en", "CA": "en-CA,en,fr-CA", "AU": "en-AU,en", "NZ": "en-NZ,en",
    "IE": "en-IE,en", "IN": "en-IN,en,hi", "ZA": "en-ZA,en", "SG": "en-SG,en",
    "DE": "de-DE,de,en", "AT": "de-AT,de,en", "CH": "de-CH,de,fr,en",
    "FR": "fr-FR,fr,en", "BE": "nl-BE,nl,fr,en", "NL": "nl-NL,nl,en",
    "ES": "es-ES,es,en", "MX": "es-MX,es,en", "AR": "es-AR,es,en", "CL": "es-CL,es,en",
    "CO": "es-CO,es,en", "PT": "pt-PT,pt,en", "BR": "pt-BR,pt,en",
    "IT": "it-IT,it,en", "PL": "pl-PL,pl,en", "RU": "ru-RU,ru,en", "UA": "uk-UA,uk,ru,en",
    "SE": "sv-SE,sv,en", "NO": "nb-NO,no,en", "DK": "da-DK,da,en", "FI": "fi-FI,fi,en",
    "CZ": "cs-CZ,cs,en", "RO": "ro-RO,ro,en", "HU": "hu-HU,hu,en", "GR": "el-GR,el,en",
    "TR": "tr-TR,tr,en", "IL": "he-IL,he,en", "SA": "ar-SA,ar,en", "AE": "ar-AE,ar,en",
    "EG": "ar-EG,ar,en", "JP": "ja-JP,ja,en", "KR": "ko-KR,ko,en",
    "CN": "zh-CN,zh,en", "HK": "zh-HK,zh,en", "TW": "zh-TW,zh,en",
    "TH": "th-TH,th,en", "VN": "vi-VN,vi,en", "ID": "id-ID,id,en",
    "MY": "ms-MY,ms,en", "PH": "en-PH,en,fil",
}


def accept_language_for_country(cc):
    if not cc:
        return "en-US,en"
    return COUNTRY_LANG.get(cc.upper(), "en-US,en")


def resolve_geo(proxy=None, quiet=False, timeout=8):
    """Resolve geo for the egress (through ``proxy`` if given, else direct). Never raises —
    returns None on failure so a transient lookup error can't break launch().
    Returns a dict {ip, country, timezone, accept_language, location} or None."""
    proxy_url = None
    if proxy and proxy.get("server"):
        server = proxy["server"]
        scheme = server.split("://", 1)[0].lower() if "://" in server else "http"
        if scheme.startswith("socks"):
            _log(quiet, "geoip: SOCKS proxy can't be used for the geo lookup — "
                        "set timezone/accept_language explicitly. Skipping.")
            return None
        proxy_url = _proxy_url(proxy)
    try:
        if proxy_url:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
            )
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(
            GEO_URL, headers={"User-Agent": "clearcote-sdk", "Accept": "application/json"}
        )
        with opener.open(req, timeout=timeout) as resp:
            j = json.loads(resp.read().decode("utf-8", "replace"))
        if j.get("status") != "success":
            _log(quiet, f"geoip: lookup failed ({j.get('message', 'unknown')})")
            return None
        lat, lon = j.get("lat"), j.get("lon")
        geo = {
            "ip": j.get("query"),
            "country": j.get("countryCode"),
            "timezone": j.get("timezone"),
            "accept_language": accept_language_for_country(j.get("countryCode")),
            "location": (f"{lat},{lon}" if lat is not None and lon is not None else None),
        }
        _log(quiet, f"geoip: {geo['ip']} -> {geo['country']} "
                    f"tz={geo['timezone']} lang={geo['accept_language']}")
        return geo
    except Exception as e:  # noqa: BLE001
        _log(quiet, f"geoip: {type(e).__name__}: {e}")
        return None
