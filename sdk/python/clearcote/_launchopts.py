"""Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
proxy resolution. Kept pure (input -> switches / cleaned options) so they're unit-testable and
mirror the Node SDK exactly."""

import re
import sys
import warnings

_SOCKS5 = re.compile(r"^socks5", re.I)
_SOCKS = re.compile(r"^socks", re.IGNORECASE)

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


def resolve_proxy(proxy):
    """Return ``(extra_args, proxy_for_playwright)`` for a Playwright proxy descriptor.

    Playwright rejects credentials in its proxy descriptor for SOCKS schemes, so a
    ``socks5://user:pass@host:port`` proxy (the most common residential-proxy shape) makes
    ``launch()`` fail outright. Route such a proxy through the ``--proxy-server`` engine switch so
    the launch proceeds, and drop it from the Playwright options.

    The credentials are forwarded to the engine as ``--socks5-credentials``: clearcote implements
    RFC 1929 username/password authentication, which stock Chromium does not, so no local relay is
    needed. Everything else (http/https proxies, or SOCKS without credentials) is left to
    Playwright unchanged."""
    if not isinstance(proxy, dict):
        return [], proxy
    server = (proxy.get("server") or "").strip()
    has_creds = bool(proxy.get("username") or proxy.get("password"))
    if server and _SOCKS.match(server) and has_creds:
        # Strip any userinfo already in the URL; the engine takes it via its own switch.
        bare = re.sub(r"^([a-zA-Z0-9+.-]+://)[^/@]*@", r"\1", server)
        creds = "%s:%s" % (proxy.get("username") or "", proxy.get("password") or "")
        # drop the proxy from Playwright (it would reject a credentialed SOCKS descriptor)
        return ["--proxy-server=" + bare, "--socks5-credentials=" + creds], None
    return [], proxy
