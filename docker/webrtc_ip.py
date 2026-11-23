"""CC_WEBRTC_IP for docker/serve.py: the address WebRTC reports as this browser's public one.

With CC_WEBRTC_IP unset, the engine gathers no ICE candidates at all (not even a .local host candidate), with or
without a proxy, which a regular Chrome never does. So when it is unset this fills it the way the SDKs' geoip fills
``webrtc_ip`` for a launch of their own: with the egress IP, looked up through CC_PROXY when one is set (the
proxy's exit), else the container's own public address. The lookup is the SDK's own (clearcote.geoip: the same
IP-echo services, CLEARCOTE_GEOIP_ECHO_URL and the same proxy handling), only the IP, not the region.

* An explicit address is used as given.
* ``off`` (also ``0``/``false``/``no``/``none``) or an explicit empty value keeps it unset: today's behaviour,
  no lookup. The SDKs pass ``off`` when their own geoip is off.
* Behind a proxy (CC_PROXY) it is not filled: the image has no UDP relay, so no packet leaves from the proxy's exit,
  and a WebRTC address there is one sites that run their own STUN servers can prove made up. Unset, the browser
  gathers no candidates behind the proxy, as Chrome does under its disable_non_proxied_udp policy. The SDKs leave
  it unset behind a proxy too. An explicit address behind a proxy is used, with a warning.
* The open engine (no licence key) is not filled either: without a proxy, measured in this image, it never finishes
  ICE gathering with a WebRTC IP, so it is left as it was.
* A failed lookup warns and starts without it; it never stops the container."""
import os
import time

OFF = ("off", "0", "false", "no", "none")
DEFAULT_TIMEOUT_S = 10.0  # the SDKs' default geoip budget; CLEARCOTE_GEOIP_TIMEOUT_SECONDS overrides it


def plan(raw, proxy, licensed):
    """What to do for CC_WEBRTC_IP=``raw`` (None when the variable is not set at all) -> ``(action, value)``:
    ``("use", ip)`` an explicit address, ``("off", why)`` leave it unset, ``("lookup", None)`` look the egress up."""
    if raw is not None:
        text = raw.strip()
        if not text:
            return "off", "CC_WEBRTC_IP is set but empty"
        if text.lower() in OFF:
            return "off", "CC_WEBRTC_IP=%s" % text
        return "use", text
    if proxy:
        return "off", ("behind the proxy: no UDP leaves from its exit, so a WebRTC address there would be one no "
                       "packet comes from")
    if not licensed:
        return "off", "the open engine: set CC_WEBRTC_IP to choose one"
    return "lookup", None


def lookup_timeout(environ=None):
    environ = os.environ if environ is None else environ
    try:
        value = float(str(environ.get("CLEARCOTE_GEOIP_TIMEOUT_SECONDS") or "").strip())
        return value if value > 0 else DEFAULT_TIMEOUT_S
    except ValueError:
        return DEFAULT_TIMEOUT_S


def egress_ip(proxy, timeout):
    """The egress IP through ``proxy`` (a URL with its login, or None for this container's own address), by the
    SDK's geoip lookup, or None. Never raises."""
    try:
        from clearcote import geoip
        from clearcote._net import to_proxy_spec
        spec = to_proxy_spec(proxy) if proxy else None
        return geoip._exit_ip(spec, time.monotonic() + timeout, True)
    except Exception:  # noqa: BLE001 -- an older SDK in the image, a bad proxy URL, no network: start without it
        return None


def resolve(raw, proxy, licensed, lookup=egress_ip, environ=None):
    """``(ip or None, source, log line)`` for serve.py. ``source`` goes into the serve-state line: "env", "proxy
    exit", "own address", "off", or "lookup failed"."""
    action, value = plan(raw, proxy, licensed)
    if action == "use":
        if proxy:
            return value, "env", ("[clearcote] WARNING: WebRTC IP: %s (CC_WEBRTC_IP) behind CC_PROXY: this image has "
                                  "no UDP relay, so no packet leaves from that address, and sites that run their own "
                                  "STUN servers can prove it fake. Leave CC_WEBRTC_IP unset behind a proxy." % value)
        return value, "env", "[clearcote] WebRTC IP: %s (CC_WEBRTC_IP)" % value
    if action == "off":
        return None, "off", "[clearcote] WebRTC IP: none (%s); the browser reports no ICE candidates" % value
    timeout = lookup_timeout(environ)
    ip = lookup(proxy, timeout)
    if not ip:
        return None, "lookup failed", ("[clearcote] WARNING: WebRTC IP: could not look up the %s within %g s; "
                                       "starting without one (the browser reports no ICE candidates). Set "
                                       "CC_WEBRTC_IP to choose it." % ("proxy's exit IP" if proxy else
                                                                       "container's public IP", timeout))
    source = "proxy exit" if proxy else "own address"
    return ip, source, "[clearcote] WebRTC IP: %s (%s, looked up at start; set CC_WEBRTC_IP to choose it)" % (
        ip, "the proxy's exit" if proxy else "this container's public address")
