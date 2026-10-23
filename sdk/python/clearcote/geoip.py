"""geoip: resolve the egress IP's geo (timezone + lat/lon + language) so the browser matches the
proxy actually in use — the way Camoufox does it. Source: daijro's offline "geoip-all-in-one"
MaxMind DB (more accurate than a single online API; merges IP2Location + GeoLite2 + DB-IP,
timezone computed from coordinates). Flow: discover the exit IP via a small IP-echo *through the
proxy*, then look that IP up in the cached .mmdb.

The .mmdb (GPL-3.0 data) is downloaded + cached on first use (~52 MB zip -> ~120 MB), NOT bundled.
The exit-IP lookup goes THROUGH the proxy (HTTP or SOCKS5, see ``_net``); we never fall back to the
local IP under a proxy, which would give the wrong region. Needs the `maxminddb` package (a
dependency).

Settings:
* CLEARCOTE_GEOIP_ECHO_URL -- your own IP-echo endpoint(s), comma-separated, used instead of the
  public ones (api.ipify.org, icanhazip.com). The answer is the IP as plain text, or JSON with an
  ``ip`` (or ``query``) field.
* CLEARCOTE_GEOIP_IPAPI=1 -- also ask ip-api.com (exit IP + geo in one call) when the database has
  no answer, e.g. while it is still downloading on first use. Off by default: ip-api.com's free
  endpoint is for non-commercial use only and allows 45 requests a minute per IP.
* CLEARCOTE_GEOIP_TIMEOUT_SECONDS (default 20) bounds the whole resolution. A lookup that runs out
  of budget fails rather than hanging a launch.
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

from ._net import PROXIED_REQUEST_SCHEMES, proxied_request, to_proxy_spec

try:
    import maxminddb  # type: ignore
    _HAVE_MAXMINDDB = True
except ImportError:
    _HAVE_MAXMINDDB = False

MMDB_URL = "https://github.com/daijro/geoip-all-in-one/releases/latest/download/geoip-aio-all.mmdb.zip"
MMDB_MAX_AGE_DAYS = 30
# Public IP-echo services with no usage limit, tried in order. CLEARCOTE_GEOIP_ECHO_URL replaces them.
# https: some proxies answer every plain-http request with a 301 to https, so an http echo failed
# on a proxy that browses fine.
IPECHO_URLS = ("https://api.ipify.org", "https://icanhazip.com")
ECHO_URL_ENV = "CLEARCOTE_GEOIP_ECHO_URL"
IPAPI_ENV = "CLEARCOTE_GEOIP_IPAPI"
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


def echo_urls(environ=None):
    """The IP-echo endpoints, in order: CLEARCOTE_GEOIP_ECHO_URL (comma-separated) when set, else the
    public defaults."""
    environ = os.environ if environ is None else environ
    custom = [u.strip() for u in str(environ.get(ECHO_URL_ENV) or "").split(",") if u.strip()]
    return tuple(custom) or IPECHO_URLS


def ipapi_enabled(environ=None):
    """Whether CLEARCOTE_GEOIP_IPAPI opts in to the ip-api.com fallback (1/true/yes/on)."""
    environ = os.environ if environ is None else environ
    return str(environ.get(IPAPI_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _ip_from_echo(text):
    """The IP an echo answered: plain text (first word), or JSON with an ``ip`` or ``query`` field."""
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            j = json.loads(text)
            text = str(j.get("ip") or j.get("query") or "")
        except ValueError:
            return ""
    return text.split()[0].strip() if text else ""


def _exit_ip(spec, deadline, quiet):
    for url in echo_urls():
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            ip = _ip_from_echo(_get_text(url, spec, min(left, 8.0)))
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
        _log(quiet, f"geoip: database fetch failed ({type(e).__name__}: {e})")
        return None


_MMDB_LOCK = threading.Lock()
_MMDB_THREAD = None


def _ensure_mmdb_within(quiet, left, info=None):
    """_ensure_mmdb bounded by ``left`` seconds. The first-run database download (~52 MB) may
    outlast the budget: it keeps going in a background thread and caches for the next launch.
    ``info["db"]`` says why there is no database: "missing-module", "downloading" or "unavailable"."""
    global _MMDB_THREAD
    info = {} if info is None else info
    if not _HAVE_MAXMINDDB:
        info["db"] = "missing-module"
        return None
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
        info["db"] = "downloading"
        return None
    if result.get("path"):
        return result["path"]
    mmdb = os.path.join(_geo_cache_root(), "geoip-aio-all.mmdb")
    if os.path.exists(mmdb):
        return mmdb
    info["db"] = "unavailable"
    return None


def _mmdb_lookup(ip, deadline, quiet, info=None):
    left = deadline - time.monotonic()
    if left <= 0:
        return None
    mmdb = _ensure_mmdb_within(quiet, left, info)
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
    # Say which scheme, rather than the "could not determine the exit IP" every lookup would end in.
    scheme = spec["server"].split(":", 1)[0].lower() if spec else ""
    if spec and scheme not in PROXIED_REQUEST_SCHEMES:
        return None, f"{scheme}:// proxies are not supported; use http, https or socks5", elapsed()
    ip = _exit_ip(spec, deadline, quiet)
    info = {}
    geo = _mmdb_lookup(ip, deadline, quiet, info) if ip else None
    if (not geo or not geo.get("timezone")) and ipapi_enabled():
        geo = _ip_api_fallback(spec, deadline) or geo
    if geo and geo.get("timezone"):
        _log(quiet, f"geoip: {geo['ip']} -> {geo['country']} "
                    f"tz={geo['timezone']} lang={geo['accept_language']}")
        return geo, None, elapsed()
    db = info.get("db")
    if ip and db and not ipapi_enabled():
        reason = {
            "downloading": "the geoip database is still downloading (~52 MB, first use only); "
                           "launch again in a moment or raise CLEARCOTE_GEOIP_TIMEOUT_SECONDS",
            "missing-module": "the maxminddb package is not installed (pip install maxminddb)",
        }.get(db, "the geoip database could not be downloaded")
        reason += f" -- or set {IPAPI_ENV}=1 to ask ip-api.com (non-commercial use only)"
    elif time.monotonic() >= deadline:
        secs = "%g" % round(budget, 1)
        reason = f"timed out after {secs}s (CLEARCOTE_GEOIP_TIMEOUT_SECONDS)"
    elif not ip:
        reason = "could not determine the exit IP" + (" through the proxy" if spec else "")
    elif geo:
        reason = f"no timezone for exit IP {ip}"
    else:
        reason = f"no geo data for exit IP {ip}"
    return geo, reason, elapsed()


def _same_family(a, b):
    return (":" in a) == (":" in b)


def check_egress_drift(proxy, first_ip, timeout=8.0):
    """Look the proxy's exit IP up once more. Returns the new IP when it differs from ``first_ip``
    (same address family: an IPv4 answer after an IPv6 one is a different echo, not a rotation),
    else None. Never raises."""
    if not proxy or not first_ip:
        return None
    try:
        spec = to_proxy_spec(proxy)
        ip = _exit_ip(spec, time.monotonic() + timeout, True)
    except Exception:  # noqa: BLE001
        return None
    if ip and ip != first_ip and _same_family(ip, first_ip):
        return ip
    return None


def egress_drift_message(first_ip, new_ip):
    return ("the proxy's exit IP changed between two lookups at launch (%s -> %s): it rotates, so the "
        "timezone, language and WebRTC IP taken from %s will not match the exit sites see. Use a "
        "sticky session (most providers take a session id in the proxy username)." % (first_ip, new_ip, first_ip))


def warn_on_egress_drift(proxy, first_ip, quiet=False):
    """Check for a rotating exit in a daemon thread and print one warning if it moved. geoip resolves
    the exit once; a proxy that hands out a new exit per connection makes that answer stale at once.
    Never blocks or fails the launch. Returns the thread (or None when nothing is checked)."""
    if quiet or os.environ.get("CLEARCOTE_NO_WARN") or not proxy or not first_ip:
        return None

    def run():
        new_ip = check_egress_drift(proxy, first_ip)
        if new_ip:
            print("clearcote: warning: " + egress_drift_message(first_ip, new_ip),
                  file=sys.stderr, flush=True)

    thread = threading.Thread(target=run, name="clearcote-egress-drift", daemon=True)
    thread.start()
    return thread


def resolve_geo(proxy=None, quiet=False, timeout=None):
    """Resolve geo for the egress (through ``proxy`` if given, else direct). Never raises — returns
    None on failure. geoip-all-in-one offline DB (ip-api.com only with CLEARCOTE_GEOIP_IPAPI=1). Returns a dict
    {ip, country, timezone, accept_language, location} or None. ``timeout`` bounds the whole
    resolution in seconds (default CLEARCOTE_GEOIP_TIMEOUT_SECONDS, else 20)."""
    return resolve_geo_detailed(proxy, quiet=quiet, timeout=timeout)[0]


# country (ISO-3166 alpha-2) -> the OS locale a machine there most plausibly runs. The geoip DB has
# no language data, so this maps the resolved country. ONE tag, not a list: the launch expands it
# the way Chrome does (DE -> de-DE -> UI "de", languages de-DE,de,en-US,en; CA -> en-CA ->
# en-GB,en-US,en; MY -> ms-MY -> Intl "ms" with en-US,en). See _languages.resolve_languages.
# Multi-language countries take the majority language (BE nl, CH de, CA en, IN en).
COUNTRY_LANG = {
    "US": "en-US", "GB": "en-GB", "CA": "en-CA", "AU": "en-AU", "NZ": "en-NZ", "IE": "en-IE",
    "IN": "en-IN", "ZA": "en-ZA", "SG": "en-SG", "PH": "en-PH",
    "DE": "de-DE", "AT": "de-AT", "CH": "de-CH", "FR": "fr-FR", "BE": "nl-BE", "NL": "nl-NL",
    "ES": "es-ES", "MX": "es-MX", "AR": "es-AR", "CL": "es-CL", "CO": "es-CO", "PE": "es-PE",
    "VE": "es-VE", "EC": "es-EC", "UY": "es-UY", "PT": "pt-PT", "BR": "pt-BR",
    "IT": "it-IT", "PL": "pl-PL", "RU": "ru-RU", "UA": "uk-UA", "SE": "sv-SE", "NO": "nb-NO",
    "DK": "da-DK", "FI": "fi-FI", "CZ": "cs-CZ", "SK": "sk-SK", "RO": "ro-RO", "HU": "hu-HU",
    "GR": "el-GR", "BG": "bg-BG", "HR": "hr-HR", "SI": "sl-SI", "RS": "sr-RS", "LT": "lt-LT",
    "LV": "lv-LV", "EE": "et-EE", "TR": "tr-TR", "IL": "he-IL", "SA": "ar-SA", "AE": "ar-AE",
    "EG": "ar-EG", "JP": "ja-JP", "KR": "ko-KR", "CN": "zh-CN", "HK": "zh-HK", "TW": "zh-TW",
    "TH": "th-TH", "VN": "vi-VN", "ID": "id-ID", "MY": "ms-MY",
}


def accept_language_for_country(cc):
    """The OS-locale tag for a country code (``DE`` -> ``de-DE``), en-US when unknown. It is a
    valid one-tag Accept-Language; launch() turns it into Chrome's own language list."""
    if not cc:
        return "en-US"
    return COUNTRY_LANG.get(str(cc).upper(), "en-US")
