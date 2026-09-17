"""geoip: resolve the egress IP's geo (timezone + lat/lon + language) so the browser matches the
proxy actually in use — the way Camoufox does it. Primary source is daijro's offline
"geoip-all-in-one" MaxMind DB (more accurate than a single online API; merges IP2Location +
GeoLite2 + DB-IP, timezone computed from coordinates). Flow: discover the exit IP via a small
IP-echo *through the proxy*, then look that IP up in the cached .mmdb. Falls back to ip-api.com
(direct geo through the proxy) if the DB can't be fetched/opened.

The .mmdb (GPL-3.0 data) is downloaded + cached on first use (~52 MB zip -> ~120 MB), NOT bundled.
The exit-IP and ip-api lookups go THROUGH the proxy (HTTP or SOCKS5, see ``_net``); we never fall
back to the local IP under a proxy, which would give the wrong region. Needs the `maxminddb`
package (a dependency); if it's missing, falls back to ip-api.com.

Budget: CLEARCOTE_GEOIP_TIMEOUT_SECONDS (default 20) bounds the whole resolution. A lookup that
runs out of budget fails rather than hanging a launch.
"""

import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile

from ._net import proxied_request, to_proxy_spec

try:
    import maxminddb  # type: ignore
    _HAVE_MAXMINDDB = True
except ImportError:
    _HAVE_MAXMINDDB = False

MMDB_URL = "https://github.com/daijro/geoip-all-in-one/releases/latest/download/geoip-aio-all.mmdb.zip"
MMDB_MAX_AGE_DAYS = 30
IPECHO_URLS = ("http://icanhazip.com", "http://api.ipify.org", "http://ip-api.com/line/?fields=query")
IPAPI_URL = "http://ip-api.com/json/?fields=status,message,countryCode,timezone,lat,lon,query"
# dotted record paths for the geoip-all-in-one schema (GeoLite2-City shaped)
_PATHS = {"iso_code": "country.iso_code", "longitude": "location.longitude",
          "latitude": "location.latitude", "timezone": "location.time_zone"}


def _log(quiet, msg):
    if not quiet:
        sys.stderr.write(f"[clearcote] {msg}\n")
        sys.stderr.flush()


def _geo_cache_root():
    """Same location the Node SDK uses, so a downloaded DB is shared."""
    env = os.environ.get("CLEARCOTE_CACHE")
    if env:
        return os.path.join(env, "geoip")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "clearcote", "geoip")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), "clearcote", "geoip")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clearcote", "geoip")


def geo_cache_root():
    """Where the geoip database is cached (CLEARCOTE_CACHE/geoip when set)."""
    return _geo_cache_root()


def geoip_timeout_seconds(env=None):
    """Whole-resolution budget in seconds: CLEARCOTE_GEOIP_TIMEOUT_SECONDS (> 0), default 20."""
    env = os.environ if env is None else env
    raw = str(env.get("CLEARCOTE_GEOIP_TIMEOUT_SECONDS") or "").strip()
    try:
        n = float(raw)
    except ValueError:
        return 20.0
    if not math.isfinite(n) or n <= 0:
        return 20.0
    return n


class GeoipError(RuntimeError):
    """Raised by launch() when ``geoip=True`` was requested and the region could not be resolved.

    Failing closed is the point: continuing would launch with the host's clock and a default
    language -- UTC + en-US on most servers -- which is exactly the mismatch geoip exists to prevent.
    Pass an explicit ``timezone`` AND ``accept_language`` to launch anyway when the lookup is
    unavailable.
    """

    code = "GEOIP_UNRESOLVED"


def _find_in(data, dotted):
    for part in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
        if data is None:
            return None
    return data


def _get_text(url, spec, timeout):
    res = proxied_request(url, timeout=max(0.001, timeout), proxy=spec)
    if not res.ok:
        raise RuntimeError(f"HTTP {res.status}")
    return res.text().strip()


def _looks_like_ip(s):
    if s.count(".") == 3 and all(p.isdigit() for p in s.split(".")):
        return True
    return ":" in s and all(c in "0123456789abcdefABCDEF:" for c in s)


def _exit_ip(spec, deadline, quiet):
    for url in IPECHO_URLS:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            text = _get_text(url, spec, min(left, 8.0))
            ip = text.split()[0].strip() if text else ""
            if _looks_like_ip(ip):
                return ip
        except Exception:  # noqa: BLE001
            continue
    return None


def _ensure_mmdb(quiet):
    if not _HAVE_MAXMINDDB:
        return None
    d = _geo_cache_root()
    mmdb = os.path.join(d, "geoip-aio-all.mmdb")
    if os.path.exists(mmdb):
        age_days = (time.time() - os.path.getmtime(mmdb)) / 86400.0
        if age_days < MMDB_MAX_AGE_DAYS:
            return mmdb
    try:
        os.makedirs(d, exist_ok=True)
        _log(quiet, "geoip: downloading the geoip-all-in-one database (~52 MB, first run only)")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            zpath = tmp.name
        part = mmdb + ".part-%d" % os.getpid()
        try:
            with urllib.request.urlopen(  # noqa: S310
                urllib.request.Request(MMDB_URL, headers={"User-Agent": "clearcote-sdk"}), timeout=120
            ) as resp, open(zpath, "wb") as out:
                shutil.copyfileobj(resp, out)
            with zipfile.ZipFile(zpath) as z:
                member = next((n for n in z.namelist() if n.lower().endswith(".mmdb")), None)
                if not member:
                    raise RuntimeError("no .mmdb in archive")
                with z.open(member) as src, open(part, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            os.replace(part, mmdb)  # atomic: a timed-out waiter never opens a half-written file
        finally:
            for f in (zpath, part):
                try:
                    os.remove(f)
                except OSError:
                    pass
        _log(quiet, "geoip: database ready")
        return mmdb
    except Exception as e:  # noqa: BLE001
        _log(quiet, f"geoip: database fetch failed ({type(e).__name__}: {e}) — falling back to ip-api")
        return None


_MMDB_LOCK = threading.Lock()
_MMDB_THREAD = None


def _ensure_mmdb_within(quiet, left):
    """_ensure_mmdb bounded by ``left`` seconds. The first-run database download (~52 MB) may
    outlast the budget: it keeps going in a background thread and caches for the next launch,
    while this launch falls back to ip-api instead of waiting."""
    global _MMDB_THREAD
    result = {}
    with _MMDB_LOCK:
        if _MMDB_THREAD is None or not _MMDB_THREAD.is_alive():
            def run():
                result["path"] = _ensure_mmdb(quiet)
            _MMDB_THREAD = threading.Thread(target=run, name="clearcote-geoip-db", daemon=True)
            _MMDB_THREAD.start()
        thread = _MMDB_THREAD  # possibly a download already in flight from an earlier launch
    thread.join(max(0.0, left))
    if thread.is_alive():
        return None
    if "path" in result:
        return result["path"]
    mmdb = os.path.join(_geo_cache_root(), "geoip-aio-all.mmdb")
    return mmdb if os.path.exists(mmdb) else None


def _mmdb_lookup(ip, deadline, quiet):
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    mmdb = _ensure_mmdb_within(quiet, left)
    if not mmdb:
        return None
    try:
        with maxminddb.open_database(mmdb) as reader:
            rec = reader.get(ip)
        if not rec:
            return None
        country = _find_in(rec, _PATHS["iso_code"])
        lat = _find_in(rec, _PATHS["latitude"])
        lon = _find_in(rec, _PATHS["longitude"])
        tz = _find_in(rec, _PATHS["timezone"])
        if not tz and lat is None:
            return None
        return {
            "ip": ip,
            "country": (str(country).upper() if country else None),
            "timezone": (str(tz) if tz else None),
            "accept_language": accept_language_for_country(country),
            "location": (f"{lat},{lon}" if lat is not None and lon is not None else None),
        }
    except Exception as e:  # noqa: BLE001
        _log(quiet, f"geoip: mmdb read failed ({type(e).__name__}: {e})")
        return None


def _ip_api_fallback(spec, deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    try:
        j = json.loads(_get_text(IPAPI_URL, spec, min(left, 8.0)))
        if j.get("status") != "success":
            return None
        lat, lon = j.get("lat"), j.get("lon")
        return {
            "ip": j.get("query"),
            "country": j.get("countryCode"),
            "timezone": j.get("timezone"),
            "accept_language": accept_language_for_country(j.get("countryCode")),
            "location": (f"{lat},{lon}" if lat is not None and lon is not None else None),
        }
    except Exception:  # noqa: BLE001
        return None


def resolve_geo_detailed(proxy=None, quiet=False, timeout=None):
    """Resolve geo for the egress (through ``proxy`` if given -- HTTP or SOCKS5, a Playwright dict
    or a URL string -- else direct), reporting why it failed.

    Returns ``(geo, reason, elapsed_ms)``: ``geo`` is the dict (or None), ``reason`` a human-readable
    failure reason when there is no geo/timezone (else None). Never raises. Bounded by ``timeout``
    seconds (default :func:`geoip_timeout_seconds`)."""
    started = time.monotonic()
    budget = float(timeout) if timeout is not None else geoip_timeout_seconds()
    deadline = started + budget

    def elapsed():
        return int(round((time.monotonic() - started) * 1000))

    try:
        spec = to_proxy_spec(proxy) if proxy else None
    except Exception as e:  # noqa: BLE001
        return None, f"invalid proxy ({e})", elapsed()
    ip = _exit_ip(spec, deadline, quiet)
    geo = _mmdb_lookup(ip, deadline, quiet) if ip else None
    if not geo:
        geo = _ip_api_fallback(spec, deadline)
    if geo and geo.get("timezone"):
        _log(quiet, f"geoip: {geo['ip']} -> {geo['country']} "
                    f"tz={geo['timezone']} lang={geo['accept_language']}")
        return geo, None, elapsed()
    if time.monotonic() >= deadline:
        secs = "%g" % round(budget, 1)
        reason = f"timed out after {secs}s (CLEARCOTE_GEOIP_TIMEOUT_SECONDS)"
    elif not ip:
        reason = "could not determine the exit IP" + (" through the proxy" if spec else "")
    elif geo:
        reason = f"no timezone for exit IP {ip}"
    else:
        reason = f"no geo data for exit IP {ip}"
    return geo, reason, elapsed()


def resolve_geo(proxy=None, quiet=False, timeout=None):
    """Resolve geo for the egress (through ``proxy`` if given, else direct). Never raises — returns
    None on failure. geoip-all-in-one offline DB first, ip-api.com fallback. Returns a dict
    {ip, country, timezone, accept_language, location} or None. ``timeout`` bounds the whole
    resolution in seconds (default CLEARCOTE_GEOIP_TIMEOUT_SECONDS, else 20)."""
    return resolve_geo_detailed(proxy, quiet=quiet, timeout=timeout)[0]


# country (ISO-3166 alpha-2) -> Accept-Language. Plain comma list (NO ;q= weights). The geoip DB
# has no language data, so this maps the resolved country. Falls back to en-US,en.
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
    return COUNTRY_LANG.get(str(cc).upper(), "en-US,en")
