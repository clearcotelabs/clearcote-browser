"""Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
proxy resolution. Kept pure (input -> switches / cleaned options) so they're unit-testable and
mirror the Node SDK exactly."""

import re
import warnings

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


def webrtc_default_deny_args(args, webrtc_ip):
    """When no persona WebRTC IP is configured, default WebRTC to disable_non_proxied_udp so the real
    local IP can't leak via srflx (stock Chromium leaks it). Skipped if the caller already set a
    handling policy or a webrtc_ip (the engine owns coherent fabrication in that case)."""
    if webrtc_ip:
        return []
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


def resolve_proxy(proxy):
    """Return ``(extra_args, proxy_for_playwright)`` for a Playwright proxy descriptor.

    Playwright rejects credentials in its proxy descriptor for SOCKS schemes, so a
    ``socks5://user:pass@host:port`` proxy (the most common residential-proxy shape) makes
    ``launch()`` fail outright. Route such a proxy through the ``--proxy-server`` engine switch so
    the launch proceeds, and drop it from the Playwright options. NOTE: Chromium has no SOCKS5
    authentication, so the credentials can't be honored either way — we warn the caller to put the
    auth on a local relay. Everything else (http/https proxies, or SOCKS without credentials) is
    left to Playwright unchanged."""
    if not isinstance(proxy, dict):
        return [], proxy
    server = (proxy.get("server") or "").strip()
    has_creds = bool(proxy.get("username") or proxy.get("password"))
    if server and _SOCKS.match(server) and has_creds:
        warnings.warn(
            "clearcote: routed a credentialed SOCKS5 proxy via --proxy-server so the launch can "
            "proceed, but Chromium cannot authenticate SOCKS5 — the credentials are dropped. Put "
            "the authentication on a local relay (a local SOCKS->authenticated-SOCKS bridge).",
            stacklevel=2,
        )
        return ["--proxy-server=" + server], None  # drop the proxy from Playwright (it would reject it)
    return [], proxy
