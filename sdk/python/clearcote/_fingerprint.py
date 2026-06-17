"""Map Clearcote fingerprint kwargs to Chromium command-line switches.

Switch names mirror components/ungoogled/ungoogled_switches.cc
(see patches/000-fingerprint-switches.patch).
"""

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
    "webrtc_ip",
    "disable_gpu_fingerprint",
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


def fingerprint_args(opts):
    """Build the Chromium switches for a dict of fingerprint options."""
    args = []
    for key, flag in _FLAGS.items():
        value = opts.get(key)
        if value is not None and value != "":
            args.append(f"--{flag}={value}")
    if opts.get("disable_gpu_fingerprint"):
        args.append("--disable-gpu-fingerprint")
    return args
