"""Map Clearcote fingerprint kwargs to Chromium command-line switches.

Switch names mirror components/ungoogled/ungoogled_switches.cc
(see patches/000-fingerprint-switches.patch).
"""

import base64
import gzip
import json
import os

# kwargs accepted by launch()/launch_persistent_context() that are fingerprint options
# (everything else is passed straight through to Playwright).
FINGERPRINT_KEYS = (
    "fingerprint",
    "platform",
    "platform_version",
    "brand",
    "brand_version",
    "gpu_vendor",
    "gpu_renderer",
    "hardware_concurrency",
    "location",
    "timezone",
    "accept_language",
    "webrtc_ip",
    "disable_gpu_fingerprint",
    "fingerprint_noise",
    "fingerprint_profile",
)

# kwarg -> switch name (without leading "--"). disable_gpu_fingerprint is a boolean flag,
# handled separately below.
_FLAGS = {
    "fingerprint": "fingerprint",
    "platform": "fingerprint-platform",
    "platform_version": "fingerprint-platform-version",
    "brand": "fingerprint-brand",
    "brand_version": "fingerprint-brand-version",
    "gpu_vendor": "fingerprint-gpu-vendor",
    "gpu_renderer": "fingerprint-gpu-renderer",
    "hardware_concurrency": "fingerprint-hardware-concurrency",
    "location": "fingerprint-location",
    "timezone": "timezone",
    "webrtc_ip": "webrtc-ip",
}


def clean_accept_language(value):
    """Normalize an Accept-Language value for Chromium's ``--accept-lang``: a plain comma-separated
    tag list with NO ``;q=`` weights or spaces (Chromium adds the q-weights to the header itself; a
    ``;`` in the switch value trips a DCHECK and crashes the renderer)."""
    tags = [t.split(";")[0].strip() for t in str(value).split(",")]
    return ",".join(t for t in tags if t)


def encode_profile(value):
    """Encode a captured clearcote-profile for the ``--fingerprint-profile`` switch.

    ``value`` may be a path to a ``.json`` file, a ``dict``, or a JSON string. The profile is
    gzip+base64 encoded so the full capture (≈40 KB) stays well within Chromium's command-line
    length limit (gzip ~6x). The engine base64-decodes + gunzips + parses it and overrides the
    seed-derived persona with the imported values."""
    if isinstance(value, dict):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str) and os.path.isfile(value):
        with open(value, "rb") as handle:
            raw = handle.read()
    else:
        raw = str(value).encode("utf-8")  # assume a JSON string
    return base64.b64encode(gzip.compress(raw, 9)).decode("ascii")


def _profile_accept_language(value):
    """Best-effort: derive an Accept-Language from an imported profile's navigator.languages
    (path / dict / JSON string), so an imported identity keeps the donor's language order."""
    try:
        if isinstance(value, dict):
            obj = value
        elif isinstance(value, (bytes, bytearray)):
            obj = json.loads(value)
        elif isinstance(value, str) and os.path.isfile(value):
            with open(value, encoding="utf-8") as handle:
                obj = json.load(handle)
        else:
            obj = json.loads(value)
    except (ValueError, OSError):
        return None
    langs = (obj.get("navigator") or {}).get("languages") if isinstance(obj, dict) else None
    if isinstance(langs, list) and langs:
        return ",".join(str(tag) for tag in langs)
    return None


def fingerprint_args(opts):
    """Build the Chromium switches for a dict of fingerprint options."""
    args = []
    for key, flag in _FLAGS.items():
        value = opts.get(key)
        if value is not None and value != "":
            args.append(f"--{flag}={value}")
    # clearcote presents as Google Chrome (its UA string says "Chrome/<v>"), so default the
    # UA-CH brand to "chrome" — otherwise navigator.userAgentData advertises only "Chromium",
    # a UA/UA-CH mismatch some bot detectors flag. Override via brand="edge" etc.
    if not opts.get("brand"):
        args.append("--fingerprint-brand=chrome")
    accept_language = opts.get("accept_language")
    if not accept_language and opts.get("fingerprint_profile"):
        accept_language = _profile_accept_language(opts["fingerprint_profile"])
    if not accept_language:
        # Always send a coherent Accept-Language. Without --accept-lang Chromium falls back to the
        # build/OS locale, which can leak a language that mismatches the proxy's country/timezone
        # (e.g. en-GB on a US IP) — a geo-inconsistency tell. en-US,en is the common Chrome default;
        # set accept_language (or geoip) to match the proxy region.
        accept_language = "en-US,en"
    args.append(f"--accept-lang={clean_accept_language(accept_language)}")
    if opts.get("disable_gpu_fingerprint"):
        args.append("--disable-gpu-fingerprint")
    # fingerprint_noise=False turns OFF the per-eTLD+1 farbling noise (canvas/WebGL/audio/
    # client-rects) so those surfaces return natural, unperturbed values — useful when a site's
    # anti-bot ML scores the noise pattern as "tampered". Identity spoofs
    # (UA/screen/GPU/persona) stay on. Default (unset/True) keeps the noise.
    if opts.get("fingerprint_noise") is False:
        args.append("--disable-fingerprint-noise")
    # fingerprint_profile imports a real captured fingerprint (path/dict/JSON) — see
    # tools/fingerprint-collect. Its fields override the seed-derived persona; absent fields
    # fall back to the seed, so partial profiles stay coherent.
    profile = opts.get("fingerprint_profile")
    if profile:
        args.append(f"--fingerprint-profile={encode_profile(profile)}")
    return args
