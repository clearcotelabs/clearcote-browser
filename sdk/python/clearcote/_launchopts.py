"""Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
proxy resolution. Kept pure (input -> switches / cleaned options) so they're unit-testable and
mirror the Node SDK exactly."""

import re
import sys
import warnings

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


def web_bluetooth_args():
    """Switches to expose ``navigator.bluetooth`` on Linux hosts (empty elsewhere).

    Web Bluetooth is compiled into the engine but runtime-disabled on Linux only:
    Chromium's runtime_enabled_features.json5 gives WebBluetooth status "stable" on Win/Mac/Android/
    ChromeOS and lets Linux fall through to "default": "experimental", and content_features.cc
    declares kWebBluetooth FEATURE_DISABLED_BY_DEFAULT. So a Linux host serving a Windows persona
    reports navigator.usb, navigator.serial and navigator.hid but NOT navigator.bluetooth - a
    combination no real Windows Chrome produces, and an OS-origin tell that survives every string
    spoof. One flag restores it on the shipped binary; no rebuild is involved.
    
    Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
    and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
    real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
    this flag produces. getAvailability() resolves false and requestDevice() rejects NotFoundError
    on a machine with no adapter, matching a real desktop without Bluetooth hardware.
    """
    if not sys.platform.startswith("linux"):
        return []  # Win/Mac builds ship it stable; the flag would be a no-op
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
        server = str((proxy or {}).get("server") or "") if isinstance(proxy, dict) else ""
        has_creds = isinstance(proxy, dict) and bool(proxy.get("username") or proxy.get("password"))
        if server and has_creds and _SOCKS.match(server) and not engine_supports_switch(exe, "socks5-credentials"):
            warnings.warn("clearcote: this engine build cannot authenticate to a SOCKS5 proxy "
                          "(needs r17+); the proxy will reject the connection.", stacklevel=3)
    except Exception:  # noqa: BLE001
        pass


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
    credentials are left to Playwright unchanged."""
    if not isinstance(proxy, dict):
        return [], proxy
    server = (proxy.get("server") or "").strip()
    has_creds = bool(proxy.get("username") or proxy.get("password"))
    # http(s) credentials go to the engine only when it implements --proxy-auth (r19+). Older
    # engines keep Playwright's handling: it works, at the cost of the interception side effects.
    http_to_engine = bool(_HTTP_PROXY.match(server)) and engine_supports_proxy_auth
    if server and has_creds and (_SOCKS.match(server) or http_to_engine):
        # Strip any userinfo already in the URL; the engine takes it via its own switch. Userinfo
        # left in --proxy-server is rejected by Chromium's proxy parser and the entry is dropped,
        # i.e. the browser would go DIRECT -- never emit it.
        bare = re.sub(r"^([a-zA-Z0-9+.-]+://)[^/@]*@", r"\1", server)
        creds = "%s:%s" % (proxy.get("username") or "", proxy.get("password") or "")
        switch = "--socks5-credentials=" if _SOCKS.match(server) else "--proxy-auth="
        args = ["--proxy-server=" + bare, switch + creds]
        bypass = (proxy.get("bypass") or "").strip()
        if bypass:
            args.append("--proxy-bypass-list=" + bypass)
        # drop the proxy from Playwright: it would reject a credentialed SOCKS descriptor, and for
        # http(s) it would turn on interception + cache-disable for the credentials we now own
        return args, None
    return [], proxy
