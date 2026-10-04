"""Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
proxy resolution. Kept pure (input -> switches / cleaned options) so they're unit-testable and
mirror the Node SDK exactly."""

import re
import sys
import warnings
from urllib.parse import unquote

_SOCKS5 = re.compile(r"^socks5", re.I)
_SOCKS = re.compile(r"^socks", re.IGNORECASE)
_HTTP_PROXY = re.compile(r"^https?://", re.IGNORECASE)

# Privacy Sandbox + intrusive web APIs a de-Googled stealth build should not expose (a build that
# claims to be de-Googled while still answering document.browsingTopics()/navigator.runAdAuction
# is a self-contradictory, pivotable fingerprint). All are runtime base::Features, so disabling
# needs no rebuild. Verified present in the 149 source.
#
# WebUSB is deliberately NOT in this list. It is not a Privacy Sandbox feature - it is a device
# API that ships alongside Web Serial, WebHID and Web Bluetooth under identical secure-context
# gating. Disabling only WebUSB left navigator.usb absent while serial/hid/bluetooth stayed
# present, a combination no real Chromium produces; measured against stock Chrome on the same
# host, that split was the single flagged difference in the device-API family. Presence leaks
# nothing on its own - the API is permission-gated and enumerates no device without a user
# gesture - so exposing it costs no privacy and removes a hard coherence tell.
PRIVACY_SANDBOX_FEATURES = (
    "BrowsingTopics", "BrowsingTopicsDocumentAPI", "Fledge", "InterestGroupStorage",
    "PrivateAggregationApi", "SharedStorageAPI", "FencedFrames",
)


def merge_feature_flags(args):
    """Chromium honors only the LAST ``--enable-features`` / ``--disable-features`` on the command
    line (they do NOT concatenate), so multiple occurrences clobber each other. Collapse all of each
    into a single flag (order-preserving, de-duped) so defaults from different layers + the user's
    own flags coexist."""
    enabled, disabled, rest = [], [], []
    for a in args:
        if a.startswith("--enable-features="):
            enabled += [f for f in a.split("=", 1)[1].split(",") if f]
        elif a.startswith("--disable-features="):
            disabled += [f for f in a.split("=", 1)[1].split(",") if f]
        else:
            rest.append(a)

    def _dedupe(xs):
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    if enabled:
        rest.append("--enable-features=" + ",".join(_dedupe(enabled)))
    if disabled:
        rest.append("--disable-features=" + ",".join(_dedupe(disabled)))
    return rest


def privacy_sandbox_args():
    """Disable Privacy Sandbox + intrusive APIs (runtime, no rebuild)."""
    return ["--disable-features=" + ",".join(PRIVACY_SANDBOX_FEATURES)]


def quic_args(proxy):
    """Behind a proxy, real Chrome cannot use QUIC/HTTP3 — a SOCKS5/HTTP proxy carries only TCP, so
    Chrome falls back to TCP for proxied requests. Disable QUIC when a proxy is configured so no
    HTTP/3 UDP is even attempted: coherent with proxied Chrome, and a belt-and-suspenders guarantee
    that no UDP egresses *around* the proxy (the #9 leak). No proxy -> leave QUIC on (real Chrome
    uses it, so disabling it everywhere would itself be a tell)."""
    return ["--disable-quic"] if (isinstance(proxy, dict) and proxy.get("server")) else []


def socks5_udp_args(socks5_udp, proxy):
    """Carry WebRTC's UDP through the SOCKS5 proxy with UDP ASSOCIATE (RFC 1928 section 7) instead
    of letting it egress on the host's own path.

    This is the transport ``webrtc_default_deny_args`` asks for. That default sets
    ``disable_non_proxied_udp``, which on stock Chromium means "no UDP at all" because stock
    Chromium cannot proxy a datagram -- so peer connections that genuinely need UDP simply fail.
    With this option the engine opens a UDP association through the proxy and relays every datagram
    over it, so UDP works AND still leaves from the proxy's address. The two compose: measured
    against the proxy's own log, the association is established with the deny policy in force, so
    enabling this does not require weakening the policy.

    Emitted only for a ``socks5://`` proxy. UDP ASSOCIATE is a SOCKS5 command -- SOCKS4 has no
    equivalent and an HTTP proxy carries only TCP -- so with any other scheme the switch would be
    accepted and silently do nothing, which is worse than not sending it.

    Needs a PRO engine 151 r17+; older binaries ignore the switch."""
    if socks5_udp is not True:
        return []
    server = ((proxy or {}).get("server") or "").strip() if isinstance(proxy, dict) else ""
    return ["--socks5-udp"] if _SOCKS5.match(server) else []


#: Platforms whose stable Chrome ships Web Bluetooth. Chromium's runtime_enabled_features.json5
#: gives WebBluetooth status "stable" on Win/Mac/Android/ChromeOS/iOS and lets Linux fall through to
#: "default": "experimental"; content_features.cc declares kWebBluetooth FEATURE_DISABLED_BY_DEFAULT.
WEB_BLUETOOTH_PLATFORMS = frozenset({"windows", "macos", "mac", "android", "chromeos"})


def web_bluetooth_args(claimed_platform=None):
    """Switches to expose ``navigator.bluetooth``, matching the platform the page is TOLD it is.

    Web Bluetooth is compiled into the engine but runtime-disabled on Linux only (see
    WEB_BLUETOOTH_PLATFORMS). Two different tells fall out of that, in opposite directions:

    * A Linux host serving a WINDOWS (or macOS/Android) persona reports navigator.usb, serial and
      hid but NOT navigator.bluetooth - a combination no real Windows Chrome produces. The flag
      restores it on the shipped binary; no rebuild is involved.
    * A Linux host serving a LINUX claim must NOT have it. Measured 2026-10-04 against genuine
      Chrome 154 on Linux: ``'bluetooth' in navigator`` is false, and false again for our own engine
      launched without the SDK - so adding the flag there invented an API genuine Linux Chrome does
      not expose, and ours answered ``getAvailability() === false``, i.e. "API present, no adapter",
      which genuine Linux Chrome cannot produce at all.

    Hence the gate is the CLAIMED platform, not ``sys.platform``: passing the host here was the bug.
    ``claimed_platform`` defaults to the host's own name, which keeps a bare call conservative.

    Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
    and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
    real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
    this flag produces. getAvailability() resolves false and requestDevice() rejects NotFoundError
    on a machine with no adapter, matching a real desktop without Bluetooth hardware.
    """
    if not sys.platform.startswith("linux"):
        return []  # Win/Mac builds ship it stable; the flag would be a no-op
    if claimed_platform is None:
        from ._fingerprint import host_persona_platform
        claimed_platform = host_persona_platform()
    if str(claimed_platform).strip().lower() not in WEB_BLUETOOTH_PLATFORMS:
        return []  # a Linux claim has no navigator.bluetooth on genuine Chrome either
    return ["--enable-features=WebBluetooth"]


def webrtc_default_deny_args(args, webrtc_ip=None):
    """Default WebRTC to disable_non_proxied_udp, so no UDP can egress around the proxy.

    This used to be skipped whenever ``webrtc_ip`` was set, on the theory that the engine's srflx
    fabrication already covered WebRTC. It does not -- the two defend different things:

      * fabrication rewrites what the browser *reports*, which beats a page reading the candidate;
      * this policy stops UDP *leaving the machine*, which beats a server watching where packets
        arrive from.

    A page that sets ``iceTransportPolicy: "relay"`` forces the browser to talk to its own TURN
    server. TURN prefers UDP and an HTTP/SOCKS proxy carries only TCP, so that UDP left on the
    host's own path and the TURN server read the real public address straight off the packet -- no
    candidate involved, so fabricating one changed nothing. Reported by a customer whose session was
    flagged for location spoofing with an otherwise perfectly coherent persona.

    Worse, ``geoip=True`` sets ``webrtc_ip`` for you, so the more carefully a caller configured for
    coherence the more likely they had silently lost this. Now only an explicit policy from the
    caller suppresses it.

    Note this is a real trade-off, not a free win: denying non-proxied UDP means peer connections
    that genuinely need UDP will not establish. Callers who need working WebRTC through a proxy want
    a transport that actually carries UDP (SOCKS5 with UDP ASSOCIATE, or a full tunnel) and can set
    their own policy to opt out. ``webrtc_ip`` is accepted and ignored, for call-site compatibility.
    """
    if any(a.startswith("--webrtc-ip-handling-policy") or a.startswith("--force-webrtc-ip-handling-policy")
           for a in args):
        return []
    return ["--webrtc-ip-handling-policy=disable_non_proxied_udp"]


def extension_args(paths):
    """Switches to load unpacked extensions. Chromium needs BOTH --load-extension=<dirs> and
    --disable-extensions-except=<dirs> (the latter keeps the listed extensions enabled while
    everything else stays off). ``paths`` is a list of unpacked-extension directories."""
    if not paths:
        return []
    joined = ",".join(str(p) for p in paths)
    return ["--load-extension=" + joined, "--disable-extensions-except=" + joined]


def portable_args(portable_profile=False, encryption_key=None):
    """Switches that keep the cookie encryption key with the PROFILE rather than the OS keystore,
    so the whole user data directory can be copied to another machine and still decrypt.

    ``encryption_key`` derives the key from a caller-supplied secret and writes nothing to disk --
    prefer it when the profile is synced to shared storage. ``portable_profile`` generates a key and
    stores it in the profile, which is convenient but means the cookie database is effectively
    unencrypted at rest (inherent to portability, not a flaw in it)."""
    if encryption_key:
        return ["--profile-encryption-key=" + str(encryption_key)]
    if portable_profile:
        return ["--portable-profile"]
    return []


_SWITCH_CACHE = {}


def engine_supports_switch(exe, name):
    """Whether the engine binary that will run implements the command-line switch ``name``.

    Chromium switch names are NUL-terminated C-string literals in the binary, so a NUL-delimited
    search for ``name`` is a capability probe that cannot collide with header names (HPACK's
    ``proxy-authenticate`` is not ``\\0proxy-auth\\0``). On Windows the switches live in
    ``chrome.dll`` next to the launcher; elsewhere in the executable itself. Cached per
    (path, size, mtime). Any failure answers False, which selects the legacy (Playwright) path —
    slower, never silently broken.
    """
    try:
        import os
        exe = str(exe or "")
        if not exe:
            return False
        path = exe
        if sys.platform.startswith("win"):
            dll = os.path.join(os.path.dirname(exe), "chrome.dll")
            if os.path.exists(dll):
                path = dll
        st = os.stat(path)
        key = (path, name, st.st_size, int(st.st_mtime))
        if key in _SWITCH_CACHE:
            return _SWITCH_CACHE[key]
        needle = b"\x00" + name.encode("ascii") + b"\x00"
        found = False
        with open(path, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(8 * 1024 * 1024)
                if not chunk:
                    break
                if needle in tail + chunk:
                    found = True
                    break
                tail = chunk[-len(needle):]
        _SWITCH_CACHE[key] = found
        return found
    except Exception:  # noqa: BLE001
        return False


# Options that only exist from a given engine revision. An unknown switch is ignored by Chromium,
# so an older engine launches fine -- but silently, which is worse than a warning.
_ENGINE_OPTION_SWITCHES = (
    ("persona_schema", "fingerprint-schema", "persona_schema=2 (engine r19+)"),
    ("real_gpu_host", "fingerprint-gpu-backend-real", "real_gpu_host (engine r19+)"),
)


def warn_unsupported_engine_options(exe, fp, proxy, quiet=False):
    """Warn once per launch for each option the resolved engine cannot honour. Never raises.

    Silenced by ``quiet=True`` or ``CLEARCOTE_NO_WARN``, like the coherence warnings."""
    import os
    if quiet or os.environ.get("CLEARCOTE_NO_WARN"):
        return
    try:
        for key, switch, label in _ENGINE_OPTION_SWITCHES:
            v = (fp or {}).get(key)
            # persona_schema matters only at 2; real_gpu_host only when truthy (note True == 1 in
            # Python, so the two are tested separately rather than through one membership check)
            wanted = (str(v) == "2") if key == "persona_schema" else bool(v)
            if not wanted:
                continue
            if not engine_supports_switch(exe, switch):
                warnings.warn(f"clearcote: {label} is not supported by this engine build and is ignored; "
                              "upgrade the engine to use it.", stacklevel=3)
        server, username, password = proxy_credentials(proxy) if isinstance(proxy, dict) else ("", "", "")
        has_creds = bool(username or password)
        if server and has_creds and _SOCKS.match(server) and not engine_supports_switch(exe, "socks5-credentials"):
            warnings.warn("clearcote: this engine build cannot authenticate to a SOCKS5 proxy "
                          "(needs r17+); the proxy will reject the connection.", stacklevel=3)
    except Exception:  # noqa: BLE001
        pass


_URL_AUTHORITY = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/?#]*)(.*)$", re.DOTALL)


def proxy_credentials(proxy):
    """``(server, username, password)`` for a Playwright proxy dict, ``server`` without userinfo.

    ``socks5://user:pass@host:1080`` is the shape most proxy providers hand out, so credentials
    written into the URL count exactly like the ``username``/``password`` keys (which win when both
    are given, as in ``to_proxy_spec``). Userinfo is percent-decoded, like a browser reads it, and
    split at the LAST '@' of the authority, like a URL parser, so an unescaped '@' in a password
    survives."""
    raw = (proxy.get("server") or "").strip()
    server, url_user, url_pass = raw, "", ""
    m = _URL_AUTHORITY.match(raw)
    if m and "@" in m.group(2):
        info, _, hostport = m.group(2).rpartition("@")
        user, sep, pw = info.partition(":")
        url_user, url_pass = unquote(user), unquote(pw) if sep else ""
        server = m.group(1) + hostport + m.group(3)
    return server, proxy.get("username") or url_user, proxy.get("password") or url_pass


def resolve_proxy(proxy, engine_supports_proxy_auth=False):
    """Return ``(extra_args, proxy_for_playwright)`` for a Playwright proxy descriptor.

    Playwright rejects credentials in its proxy descriptor for SOCKS schemes, so a
    ``socks5://user:pass@host:port`` proxy (the most common residential-proxy shape) makes
    ``launch()`` fail outright. Route such a proxy through the ``--proxy-server`` engine switch so
    the launch proceeds, and drop it from the Playwright options.

    The credentials are forwarded to the engine as ``--socks5-credentials``: clearcote implements
    RFC 1929 username/password authentication, which stock Chromium does not, so no local relay is
    needed.

    An ``http://`` / ``https://`` proxy WITH credentials takes the same route, via ``--proxy-auth``:
    the engine answers the proxy's 407 itself (clearcote r19+). Handing the credentials to
    Playwright instead makes its driver enable Fetch interception and ``Network.setCacheDisabled``
    for the whole context -- every request then bypasses the cache and carries the interception's
    side effects, a transport tell that has nothing to do with the persona. Proxies without
    credentials are left to Playwright unchanged.

    Credentials count wherever they were written (see ``proxy_credentials``). Reading only the
    keys used to hand a ``socks5://user:pass@host`` proxy to Playwright, which rebuilds the server
    as scheme://host:port: the browser then offered the proxy no authentication at all."""
    if not isinstance(proxy, dict):
        return [], proxy
    server, username, password = proxy_credentials(proxy)
    has_creds = bool(username or password)
    # http(s) credentials go to the engine only when it implements --proxy-auth (r19+). Older
    # engines keep Playwright's handling: it works, at the cost of the interception side effects.
    http_to_engine = bool(_HTTP_PROXY.match(server)) and engine_supports_proxy_auth
    if server and has_creds and (_SOCKS.match(server) or http_to_engine):
        # `server` has no userinfo; the engine takes it via its own switch. Userinfo left in
        # --proxy-server is rejected by Chromium's proxy parser and the entry is dropped: every
        # request then fails with ERR_NO_SUPPORTED_PROXIES (measured on r27) -- never emit it.
        creds = "%s:%s" % (username, password)
        switch = "--socks5-credentials=" if _SOCKS.match(server) else "--proxy-auth="
        args = ["--proxy-server=" + server, switch + creds]
        bypass = (proxy.get("bypass") or "").strip()
        if bypass:
            args.append("--proxy-bypass-list=" + bypass)
        # drop the proxy from Playwright: it would reject a credentialed SOCKS descriptor, and for
        # http(s) it would turn on interception + cache-disable for the credentials we now own
        return args, None
    # Left to Playwright. It drops userinfo from the server, so credentials written there are
    # handed over as the keys it reads (an http proxy on an engine without --proxy-auth).
    if server != (proxy.get("server") or "").strip():
        out = {**proxy, "server": server}
        if username:
            out["username"] = username
        if password:
            out["password"] = password
        return [], out
    return [], proxy


# -- GPU launch defaults ------------------------------------------------------------------------

# Playwright launch defaults the SDK removes.
#
# ``--enable-automation`` keeps the engine's AutomationControlled feature off.
# ``--enable-unsafe-swiftshader`` is added by Playwright (1.49+) to every Chromium launch; it lets
# WebGL fall back to SwiftShader software rendering, which real Chrome no longer does for WebGL.
# Stripping it on its own is NOT safe: measured on a GPU-less Linux host, a HEADED launch then has no
# WebGL at all. It is only removed together with gpu_blocklist_args(), which restores WebGL through
# the normal GPU path. A caller's own ``ignore_default_args`` always wins.
# ``--hide-scrollbars`` is added by Playwright to every HEADLESS launch; with it the page measures
# 0 px scrollbars, while genuine Chrome shows 15 px on Windows -- headed OR plain ``--headless=new``
# (measured on Chrome 154). Stripping it only restores Chrome's own default; it is a no-op headed.
DEFAULT_IGNORED_ARGS = ("--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars")


def gpu_blocklist_args(headed, platform=None, user_args=()):
    """``--ignore-gpu-blocklist`` for headed launches and for every launch on Windows.

    Headed on a host without a usable GPU (a VPS under Xvfb), Chromium's blocklist disables WebGL
    outright once the SwiftShader fallback flag is gone; this flag lets WebGL run anyway. On Windows
    the blocklist also refuses WebGPU on the Microsoft Basic Render Driver found on GPU-less VMs.
    Headless Linux already renders WebGL through SwiftShader regardless (measured), so nothing is
    added there. Not added when the caller already passes it."""
    platform = sys.platform if platform is None else platform
    if not headed and not str(platform).startswith("win"):
        return []
    if "--ignore-gpu-blocklist" in (user_args or ()):
        return []
    return ["--ignore-gpu-blocklist"]


# -- new engine switches (152 r22+) -------------------------------------------------------------

# Switches introduced in engine 152 r22, with what the caller asked for. Gated per binary.
GATED_ENGINE_SWITCHES = {
    "--fingerprint-passthrough": 'fingerprint="off" (pass-through debug mode)',
    "--disable-fingerprint-voices": "fingerprint_voices=False",
    "--allow-third-party-cookies": "allow_third_party_cookies=True",
    "--transparent-proxy": "transparent_proxy=True",
}


def gate_engine_switches(exe, args, quiet=False):
    """Drop any 152 r22+ switch the engine that will run does not implement, with a warning.

    Chromium ignores unknown switches silently, so an older engine would launch without the feature
    and without saying so. Detection is the same NUL-delimited literal probe as
    :func:`engine_supports_switch`. Returns ``(args, warnings)``; the warnings are emitted through
    :mod:`warnings` unless ``quiet``."""
    out, notes = [], []
    for a in args:
        name = str(a).split("=", 1)[0]
        what = GATED_ENGINE_SWITCHES.get(name)
        if what is None or engine_supports_switch(exe, name[2:]):
            out.append(a)
            continue
        notes.append(f"clearcote: {what} needs engine 152 r22 or newer; this engine ignores it, "
                     "so it was not applied.")
    if not quiet:
        for n in notes:
            warnings.warn(n, stacklevel=3)
    return out, notes


def engine_extras_args(allow_third_party_cookies=None, transparent_proxy=None, proxy=None, quiet=False):
    """Launch switches for the non-fingerprint engine options.

    ``allow_third_party_cookies=True`` allows third-party cookies as stock Chrome does (the
    de-Googled base blocks them, breaking reCAPTCHA / SSO / payment challenge iframes).
    ``transparent_proxy=True`` removes what an origin or page can observe about the proxy (the
    ``Proxy-Connection`` header on plain-HTTP requests, and proxy-shaped DNS/connect/TLS timing); it
    is only meaningful with a proxy, so without one it is dropped with a note."""
    args = []
    if allow_third_party_cookies is True:
        args.append("--allow-third-party-cookies")
    if transparent_proxy is True:
        server = (proxy or {}).get("server") if isinstance(proxy, dict) else proxy
        if server:
            args.append("--transparent-proxy")
        elif not quiet:
            warnings.warn("clearcote: transparentProxy has no effect without a proxy; ignored.",
                          stacklevel=3)
    return args


def serve_needs_no_sandbox(platform=None, uid=None, args=()):
    """serve() as root on Linux needs --no-sandbox (unless the caller already passed it).

    Chromium refuses to start as root without it, and serve spawns the binary itself, so
    Playwright's own --no-sandbox is missing: ``clearcote serve`` in a root container just timed out."""
    platform = sys.platform if platform is None else platform
    return str(platform).startswith("linux") and uid == 0 and "--no-sandbox" not in (args or ())
