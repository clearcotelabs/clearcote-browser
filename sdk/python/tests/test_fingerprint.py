import base64
import gzip
import json

from clearcote import _fingerprint
from clearcote._fingerprint import (
    FINGERPRINT_KEYS,
    clean_accept_language,
    encode_profile,
    fingerprint_args,
)


def test_default_persona_on_windows_host(monkeypatch):
    # On a Windows host the default persona is Windows + Google Chrome; the defaults keep
    # navigator.platform and the UA-CH brand coherent. A coherent Accept-Language is always emitted
    # (en-US,en) and --lang pins the UI/ICU locale so Intl matches navigator.language.
    monkeypatch.setattr(_fingerprint.sys, "platform", "win32")
    assert fingerprint_args({}) == [
        "--fingerprint-platform=windows",
        "--fingerprint-brand=chrome",
        "--accept-lang=en-US,en",
        "--lang=en-US",
    ]


def test_default_persona_on_linux_host(monkeypatch):
    # On a Linux host the default persona is Linux — coherent with the Linux binary the SDK ships
    # (its GPU/voices/audio-device values are Linux-native under --fingerprint-platform=linux).
    monkeypatch.setattr(_fingerprint.sys, "platform", "linux")
    assert fingerprint_args({}) == [
        "--fingerprint-platform=linux",
        "--fingerprint-brand=chrome",
        "--accept-lang=en-US,en",
        "--lang=en-US",
    ]


def test_lang_derived_from_primary_accept_language():
    # Intl/locale coherence: --lang = the primary Accept-Language tag.
    assert "--lang=fr-FR" in fingerprint_args({"accept_language": "fr-FR,fr"})
    assert "--lang=de-DE" in fingerprint_args({"accept_language": "de-DE,de;q=0.7,en;q=0.3"})


def test_maps_every_option():
    args = fingerprint_args({
        "fingerprint": "seed-1",
        "platform": "windows",
        "platform_version": "10.0.0",
        "brand": "Edge",
        "brand_version": "149",
        "gpu_vendor": "Google Inc.",
        "gpu_renderer": "ANGLE (Intel)",
        "hardware_concurrency": 8,
        "location": "40.7,-74.0",
        "timezone": "America/New_York",
        "webrtc_ip": "1.2.3.4",
    })
    for expected in (
        "--fingerprint=seed-1",
        "--fingerprint-platform=windows",
        "--fingerprint-platform-version=10.0.0",
        "--fingerprint-brand=Edge",
        "--fingerprint-brand-version=149",
        "--fingerprint-gpu-vendor=Google Inc.",
        "--fingerprint-gpu-renderer=ANGLE (Intel)",
        "--fingerprint-hardware-concurrency=8",
        "--fingerprint-location=40.7,-74.0",
        "--timezone=America/New_York",
        "--webrtc-ip=1.2.3.4",
    ):
        assert expected in args


def test_clean_accept_language():
    assert clean_accept_language("en-US, en ;q=0.8, , fr") == "en-US,en,fr"
    assert clean_accept_language("de-DE,de;q=0.7,en;q=0.3") == "de-DE,de,en"
    assert clean_accept_language("") == ""
    assert "--accept-lang=en-US,en" in fingerprint_args({"accept_language": "en-US,en;q=0.9"})


def test_disable_gpu_only_when_true():
    assert "--disable-gpu-fingerprint" in fingerprint_args({"disable_gpu_fingerprint": True})
    assert "--disable-gpu-fingerprint" not in fingerprint_args({"disable_gpu_fingerprint": False})


def test_noise_disabled_only_when_false():
    assert "--disable-fingerprint-noise" in fingerprint_args({"fingerprint_noise": False})
    assert "--disable-fingerprint-noise" not in fingerprint_args({"fingerprint_noise": True})
    assert "--disable-fingerprint-noise" not in fingerprint_args({})


def test_skips_empty_and_none():
    args = fingerprint_args({"fingerprint": "", "timezone": None, "gpu_vendor": ""})
    assert not any(a.startswith("--fingerprint=") for a in args)
    assert not any(a.startswith("--timezone=") for a in args)
    assert not any(a.startswith("--fingerprint-gpu-vendor=") for a in args)


def test_profile_roundtrip_dict_and_string():
    profile = {"navigator": {"userAgent": "Mozilla/5.0 …"}, "screen": {"width": 1920}}
    flag = next(a for a in fingerprint_args({"fingerprint_profile": profile}) if a.startswith("--fingerprint-profile="))
    b64 = flag.split("=", 1)[1]
    assert json.loads(gzip.decompress(base64.b64decode(b64))) == profile

    raw = '{"a":1}'
    assert gzip.decompress(base64.b64decode(encode_profile(raw))).decode() == raw


def test_fingerprint_keys_cover_known_options():
    for k in ("fingerprint", "platform", "brand", "timezone", "webrtc_ip",
              "fingerprint_noise", "fingerprint_profile"):
        assert k in FINGERPRINT_KEYS
