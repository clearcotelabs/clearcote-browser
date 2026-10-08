"""Launch-time option helpers that are NOT fingerprint switches: unpacked-extension loading and
proxy resolution. Kept pure (input -> switches / cleaned options) so they're unit-testable and
mirror the Node SDK exactly."""

import glob
import os
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


#: Playwright puts its own ``--disable-features`` list on every launch, BEFORE the caller's args, and
#: Chromium keeps only the last ``--disable-features`` on the line -- so the SDK re-emits that list
#: itself, merged into its own single switch, minus PAGE_VISIBLE_PLAYWRIGHT_FEATURES (see
#: playwright_feature_override_args). The list differs between Playwright releases (1.49 also disables
#: LazyFrameLoading and PlzDedicatedWorker; 1.61 drops AcceptCHFrame and adds
#: BoundaryEventDispatchTracksNodeRemoval), so the installed driver's own list is read at launch
#: (installed_playwright_disabled_features); this copy (1.57) is only the fallback.
PLAYWRIGHT_DISABLED_FEATURES = (
    "AcceptCHFrame", "AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose",
    "DialMediaRouteProvider", "GlobalMediaControls", "HttpsUpgrades", "LensOverlay", "MediaRouter",
    "PaintHolding", "ThirdPartyStoragePartitioning", "Translate", "AutoDeElevate", "RenderDocument",
    "OptimizationHints",
)

#: Entries of Playwright's list that a web page or server can observe, kept ENABLED as in genuine
#: Chrome 154 (all four default on). The rest stay off: they keep Playwright's automation stable
#: (beforeunload, frame tracking, paint timing for screenshots) or only touch browser UI and
#: background services.
#:
#: * ThirdPartyStoragePartitioning: every Chrome since 115 partitions third-party storage, whatever
#:   the user's cookie settings. With it off, a cross-site iframe reads the storage its site wrote as
#:   a top-level page, or -- with third-party cookies blocked, the engine default -- gets a
#:   SecurityError from localStorage. Measured 2026-10-06 (r30, genuine Chrome 154.0.8037.98 on the
#:   same host): genuine gives the iframe an empty partition (null, no error) with third-party
#:   cookies allowed AND blocked; genuine launched with Playwright's defaults reproduces both
#:   clearcote results exactly.
#: * AcceptCHFrame: client hints requested in the TLS/HTTP2 ACCEPT_CH frame reach the server on the
#:   first request.
#: * HttpsUpgrades: an http:// navigation is tried over https first -- the server sees which. Note
#:   for automation: a page.route("http://...") handler can now see the https:// attempt, and an
#:   http-only site pays the upgrade-and-fallback; args=["--disable-features=HttpsUpgrades"] (or
#:   AcceptCHFrame) turns one back off, as the merge keeps the caller's entries.
#: * LazyFrameLoading (Playwright <= 1.49): loading="lazy" iframes load lazily, as the page expects.
#:   It no longer exists in Chromium 154, so re-enabling it is a no-op there (as are 1.49's
#:   PlzDedicatedWorker, AutoExpandDetailsElement and ImprovedCookieControls, which stay listed).
#:
#: Deliberately left off although a page could observe it: BoundaryEventDispatchTracksNodeRemoval
#: (Playwright 1.61, microsoft/playwright#38568), which Playwright's own pointer actions rely on.
PAGE_VISIBLE_PLAYWRIGHT_FEATURES = frozenset(
    {"ThirdPartyStoragePartitioning", "AcceptCHFrame", "HttpsUpgrades", "LazyFrameLoading"})

_PW_ASSIGN = re.compile(r"\bdisabledFeatures\s*=")
_PW_ARRAY = re.compile(r"^\s*(?:\([^)]*\)\s*=>\s*)?\[(.*?)\]", re.S)
#: A parse must contain one of these to be trusted -- a reshaped source then falls back to the
#: copy instead of silently re-enabling whatever a truncated parse cut off.
_PW_KNOWN = ("MediaRouter", "Translate", "ThirdPartyStoragePartitioning", "DestroyProfileOnBrowserClose")
_PW_LITERAL = re.compile(r"""["'`]--disable-features=([A-Za-z0-9_,]+)["'`]""")
_PW_TERNARY = re.compile(r"""\w+\s*\?\s*["'][^"']*["']\s*:\s*["'][^"']*["']""")
_PW_LINE_COMMENT = re.compile(r"//[^\n]*")
_PW_NAME = re.compile(r"""["']([A-Za-z0-9_]+)["']""")


def parse_playwright_disabled_features(source):
    """The ``--disable-features`` list in a Playwright chromiumSwitches source (1.5x/1.6x array form,
    bundled or not, or the older literal-string form), as a tuple; None when it is not there.
    A conditional entry (``assistantMode ? "AutomationControlled" : ""``) is not part of the default."""
    if not source:
        return None
    names = ()
    assign = _PW_ASSIGN.search(source)
    if assign:
        # strip comments BEFORE looking for the closing bracket: a "]" in a comment must not end it
        tail = _PW_LINE_COMMENT.sub("", source[assign.end():assign.end() + 6000])
        m = _PW_ARRAY.match(tail)
        if m:
            names = tuple(_PW_NAME.findall(_PW_TERNARY.sub("", m.group(1))))
    if not names:
        m = _PW_LITERAL.search(source)
        if m:
            names = tuple(n for n in m.group(1).split(",") if n)
    return names if any(k in names for k in _PW_KNOWN) else None


_SCREENSHOT_SWITCH = "--enable-features=CDPScreenshotNewSurface"
_installed_pw = []  # [(disabled features or None, source mentions CDPScreenshotNewSurface)] once read


def _installed_playwright_switches_source():
    try:
        import os
        import playwright
        lib = os.path.join(os.path.dirname(playwright.__file__), "driver", "package", "lib")
        for rel in (("server", "chromium", "chromiumSwitches.js"), ("coreBundle.js",)):
            path = os.path.join(lib, *rel)
            if os.path.isfile(path):
                with open(path, encoding="utf-8", errors="replace") as handle:
                    return handle.read()
    except Exception:
        pass
    return None


def _installed_playwright():
    if not _installed_pw:
        src = _installed_playwright_switches_source()
        _installed_pw.append((parse_playwright_disabled_features(src),
                              bool(src) and "CDPScreenshotNewSurface" in src))
    return _installed_pw[0]


def installed_playwright_disabled_features():
    """The list the installed Playwright driver puts on a Chromium launch (read once per process),
    or None when it cannot be read."""
    return _installed_playwright()[0]


def playwright_feature_override_args(ignore_default_args=None, playwright_features=None,
                                     screenshot_surface=None):
    """Switches that replace Playwright's own ``--disable-features`` without the page-visible entries.

    Only for launches Playwright starts (launch / launch_persistent_context); serve() and the Docker
    entrypoint start Chromium themselves and never carry Playwright's list. merge_feature_flags then
    folds this into the SDK's single ``--disable-features``, which Playwright places after its own.

    ``playwright_features`` defaults to the installed driver's list (the 1.57 copy when unreadable).
    Nothing is re-emitted for a switch Playwright did not put on the line: ``ignore_default_args=True``
    drops them all, and a list drops exactly the switches it names (Playwright matches exactly, so
    any other value leaves its list in place and it still needs replacing).

    Also re-emits Playwright's ``--enable-features=CDPScreenshotNewSurface`` (unless
    PLAYWRIGHT_LEGACY_SCREENSHOT is set), because the SDK's own ``--enable-features`` -- e.g.
    WebBluetooth for a Windows claim -- would otherwise replace it the same way."""
    if ignore_default_args is True:
        return []
    ignored = list(ignore_default_args) if isinstance(ignore_default_args, (list, tuple)) else []
    features = tuple(playwright_features if playwright_features is not None
                     else (installed_playwright_disabled_features() or PLAYWRIGHT_DISABLED_FEATURES))
    out = []
    if "--disable-features=" + ",".join(features) not in ignored:
        keep = [f for f in features if f not in PAGE_VISIBLE_PLAYWRIGHT_FEATURES]
        if keep:
            out.append("--disable-features=" + ",".join(keep))
    if screenshot_surface is None:
        import os
        screenshot_surface = (_installed_playwright()[1]
                              and not os.environ.get("PLAYWRIGHT_LEGACY_SCREENSHOT"))
    if screenshot_surface and _SCREENSHOT_SWITCH not in ignored:
        out.append(_SCREENSHOT_SWITCH)
    return out


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
    """Switch to make ``navigator.bluetooth`` match the platform the page is TOLD it is.

    Web Bluetooth is compiled into the engine but its *default* follows the build platform (see
    WEB_BLUETOOTH_PLATFORMS). So the same persona is a tell in opposite directions depending on
    which build you happen to be running, and neither direction is what the page should see:

    * A Linux build serving a WINDOWS (or macOS/Android) persona reports navigator.usb, serial and
      hid but NOT navigator.bluetooth - a combination no real Windows Chrome produces.
    * A Windows build serving a LINUX claim reports navigator.bluetooth, which genuine Chrome on
      Linux does not have. Measured 2026-10-04 against genuine Chrome 154 on Linux:
      ``'bluetooth' in navigator`` is false, and false again for our own engine launched without
      the SDK; ours answered ``getAvailability() === false``, i.e. "API present, no adapter", which
      genuine Linux Chrome cannot produce at all.

    Hence the switch is derived from the CLAIM alone and the host is never consulted: whichever way
    the build's default falls, one of these two switches lands the page on the right answer, and the
    one that agrees with the default is a no-op. Reading ``sys.platform`` here was the original bug,
    and reading it to decide whether to bother was the half-fix: skipping the disable off Linux left
    a Windows host serving a Linux persona exposing the API (measured on the r30 Windows build).
    ``claimed_platform`` defaults to the host's own name, which keeps a bare call conservative.

    Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
    and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
    real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
    the enable switch produces. getAvailability() resolves false and requestDevice() rejects
    NotFoundError on a machine with no adapter, matching a real desktop without Bluetooth hardware.
    """
    if claimed_platform is None:
        from ._fingerprint import host_persona_platform
        claimed_platform = host_persona_platform()
    if str(claimed_platform).strip().lower() in WEB_BLUETOOTH_PLATFORMS:
        return ["--enable-features=WebBluetooth"]
    return ["--disable-features=WebBluetooth"]


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


def x_display_available(environ=None):
    """Whether ``DISPLAY`` names an X server this process can reach.

    ANGLE's OpenGL backend opens an X display even for a headless browser: measured on a GPU-less
    Linux host, ``--use-angle=gl`` without one leaves WebGL disabled ("Could not open the default X
    display"); with one (an Xvfb) it renders through Mesa. A local ``:N`` display is checked for its
    socket; a ``host:N`` display cannot be probed cheaply, so it is trusted."""
    env = os.environ if environ is None else environ
    disp = (env.get("DISPLAY") or "").strip()
    if not disp:
        return False
    if disp.startswith(":"):
        num = disp[1:].split(".")[0]
        return num.isdigit() and os.path.exists("/tmp/.X11-unix/X" + num)
    return True


# Library directories searched for Mesa's EGL (Debian/Ubuntu multiarch, Fedora/RHEL lib64, Arch).
EGL_LIB_DIRS = (
    "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu",
    "/usr/lib64", "/lib64", "/usr/lib", "/lib",
)


def mesa_egl_available(lib_dirs=EGL_LIB_DIRS):
    """Whether Mesa can render WebGL over EGL with no X display (``--use-angle=gl-egl``).

    Measured on a GPU-less Linux host: with no display, ``gl-egl`` renders through
    Mesa's llvmpipe with the same limits as the X display path (16384 textures, 1024 vertex uniform
    vectors), and genuine Chrome on that path reports the same. It needs the system ``libEGL.so.1``,
    Mesa's EGL vendor library and a software rasterizer driver. Without libEGL the browser has no WebGL
    at all, not even the SwiftShader fallback, so this answers True only when all three are present."""
    def has(name):
        return any(os.path.exists(os.path.join(d, name)) for d in lib_dirs)

    if not (has("libEGL.so.1") and has("libEGL_mesa.so.0")):
        return False
    return has("dri/swrast_dri.so") or has("dri/kms_swrast_dri.so") or any(
        glob.glob(os.path.join(d, "libgallium-*.so")) for d in lib_dirs)


def gpu_backend_args(claimed_platform, headed, platform=None, user_args=(), environ=None, mesa_egl=None):
    """The ANGLE backend whose WebGL surface matches the platform the page is TOLD it is, on a Linux host.

    A persona's renderer string names a GPU and an API; the limits, the API suffix and the extension
    list the page reads next to it come from whichever backend actually renders (the engine only ever
    lowers limits). Measured 2026-10-06 and 2026-10-08 on a GPU-less Linux host against real captures:

    * A **Windows** claim names Direct3D11. ANGLE's OpenGL backend (Mesa) clamps
      MAX_VERTEX_UNIFORM_VECTORS to 1024 -- no real Direct3D11 Intel machine reports that (0 of 129
      captures; they report 4096) -- and translates shaders to GLSL. SwiftShader reports 4096, and
      the HLSL shader dialect (see shader_dialect) makes its translations match. So a Windows claim
      gets ``--use-angle=swiftshader-webgl``, the engine's own headless default.
    * A **Linux** claim names Mesa. Real Linux Chrome shows the ``OpenGL ES 3.2`` form of the
      renderer string four to one over desktop ``OpenGL 4.x`` (63 vs 16 decontaminated captures,
      SC 180), and Mesa over EGL (``--use-angle=gl-egl``) reproduces that form, its extension set
      (an exact match for 60 of 63 captures) and its limits -- with or without an X display, headed
      or headless (measured under Xvfb). So a Linux claim gets ``gl-egl`` whenever the host has
      Mesa's EGL (mesa_egl_available), in BOTH modes, so headless and headed show one WebGL surface.
      Without EGL but with an X display, ``--use-angle=gl`` (desktop GL, the rarer real form); with
      neither, SwiftShader stays.
    * Mesa needs ``--ignore-gpu-blocklist`` on a GPU-less host or Chromium disables WebGL outright.
      Headed launches already carry it (gpu_blocklist_args); headless ones get it here. That holds
      for a caller's OWN ``--use-angle=`` / ``--use-gl=`` too: measured, a headless launch with the
      caller's ``gl-egl`` or ``gl`` and no blocklist override had NO WebGL context at all, a louder
      tell than any backend. Their backend choice is kept; only the override is added.

    ``claimed_platform`` None (pass-through: no persona) adds nothing, nor does any host other than
    Linux. ``mesa_egl`` None probes this host; tests pass True/False."""
    platform = sys.platform if platform is None else platform
    if claimed_platform is None or not str(platform).startswith("linux"):
        return []
    user = list(user_args or ())
    blocklist = [] if (headed or "--ignore-gpu-blocklist" in user) else ["--ignore-gpu-blocklist"]
    if any(a.startswith(("--use-angle=", "--use-gl=")) for a in user):
        return blocklist  # the caller chose the backend; Mesa still needs the blocklist override
    claim = str(claimed_platform).strip().lower()
    if claim == "windows":
        return ["--use-angle=swiftshader-webgl"]
    if claim == "linux":
        if mesa_egl_available() if mesa_egl is None else mesa_egl:
            backend = "--use-angle=gl-egl"
        elif x_display_available(environ):
            backend = "--use-angle=gl"
        else:
            return []
        return [backend] + blocklist
    return []


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


# -- stock DevTools Runtime behaviour (engine r32+) -----------------------------------------------

# The engine holds back part of what V8 reports to a DevTools client (patch 110), so with Playwright
# set_content() times out, page.on("console") / page.on("pageerror") receive nothing, and expose_function() /
# expose_binding() stop working after a navigation. Engine r32 (patch 1043) restores stock Chromium for these
# with this switch. Off by default: pages can observe some of the restored Runtime behaviour.
STOCK_RUNTIME_SWITCH = "--disable-runtime-suppression"
STOCK_RUNTIME_ENV = "CLEARCOTE_STOCK_RUNTIME"
_STOCK_RUNTIME_ON = ("1", "true", "yes", "on")


def stock_runtime_wanted(value=None):
    """The ``stock_runtime`` option: an explicit value wins; None follows ``CLEARCOTE_STOCK_RUNTIME`` (on for
    1/true/yes/on, off otherwise)."""
    if value is None:
        return os.environ.get(STOCK_RUNTIME_ENV, "").strip().lower() in _STOCK_RUNTIME_ON
    if isinstance(value, str):  # "0"/"false" from a config file must not mean on
        return value.strip().lower() in _STOCK_RUNTIME_ON
    return bool(value)


def stock_runtime_args(exe, enabled=None, user_args=(), quiet=False):
    """``[--disable-runtime-suppression]`` when ``stock_runtime`` is on and the engine that will run has the
    switch; else nothing. It is a normal browser argument, not a persona switch (_personaenv leaves it on the
    command line). An engine without it (r31, open builds) would ignore it without a word, so it is not passed
    and the SDK says so once per process (quiet and CLEARCOTE_NO_WARN silence it without using it up). A caller
    who already put it in ``args`` gets it once."""
    if not stock_runtime_wanted(enabled):
        return []
    if not engine_supports_switch(exe, STOCK_RUNTIME_SWITCH[2:]):
        from ._warnings import warn_once
        warn_once("stock-runtime-unsupported",
                  "stock_runtime (CLEARCOTE_STOCK_RUNTIME) is on, but this engine does not support it (it needs an "
                  "engine from r32 on), so the browser starts without it.", quiet)
        return []
    return [] if STOCK_RUNTIME_SWITCH in (user_args or ()) else [STOCK_RUNTIME_SWITCH]


def warn_stock_runtime_cloud(enabled=None, quiet=False):
    """A cloud browser does not take ``stock_runtime``: say so once per process when it is on."""
    if stock_runtime_wanted(enabled):
        from ._warnings import warn_once
        warn_once("stock-runtime-cloud",
                  "stock_runtime (CLEARCOTE_STOCK_RUNTIME) only applies to local and Docker launches; a cloud "
                  "browser does not take it, so it was not applied.", quiet)


def serve_needs_no_sandbox(platform=None, uid=None, args=()):
    """serve() as root on Linux needs --no-sandbox (unless the caller already passed it).

    Chromium refuses to start as root without it, and serve spawns the binary itself, so
    Playwright's own --no-sandbox is missing: ``clearcote serve`` in a root container just timed out."""
    platform = sys.platform if platform is None else platform
    return str(platform).startswith("linux") and uid == 0 and "--no-sandbox" not in (args or ())


def serve_infobar_args(headless, args=()):
    """The switch that keeps the engine's "unsupported command-line flag" warning bar off a served
    browser.

    Any flag on Chromium's list raises it, --no-sandbox among them (which serve adds as root on
    Linux), and it lands on the first tab: 56px off that tab's innerHeight, a frame (outer - inner)
    no other tab and no real Chrome has. launch() never shows it: Playwright starts that browser
    without a startup window, and passes --disable-infobars to a persistent context.

    - Headless: --disable-infobars, the same switch. Chromium honours it only in headless, where it
      suppresses infobars and nothing else, so every headless serve gets it.
    - Headed: Chromium ignores --disable-infobars. The one switch that drops the warning is
      --test-type, which also turns on test-harness behaviour (chrome.test in extension pages, no
      component extensions with background pages, no OS integration for installed web apps), none
      of it visible to a page. So only with --no-sandbox, the flag root on Linux cannot run without,
      and bare: --test-type=webdriver would also waive Payment Request's user-interaction check."""
    def has(switch):
        return any(a == switch or str(a).startswith(switch + "=") for a in (args or ()))
    if headless:
        return [] if has("--disable-infobars") else ["--disable-infobars"]
    return ["--test-type"] if has("--no-sandbox") and not has("--test-type") else []
