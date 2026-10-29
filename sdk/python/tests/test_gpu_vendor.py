"""gpu_vendor: a short name went out verbatim ("intel" as UNMASKED_VENDOR_WEBGL, next to the persona's AMD renderer;
measured 2026-10-28, SC 295). It is now written the way Chrome reports it, and a vendor/renderer pair naming two GPU
makers is refused. Mirrors sdk/node/test/gpu-vendor.test.ts."""
import pytest

from clearcote._fingerprint import fingerprint_args, gpu_maker, gpu_vendor_for
from clearcote._warnings import coherence_warnings

AMD = "ANGLE (AMD, AMD Radeon(TM) Graphics (0x00001506) Direct3D11 vs_5_0 ps_5_0, D3D11)"
INTEL = "ANGLE (Intel, Intel(R) UHD Graphics 620 (0x00005917) Direct3D11 vs_5_0 ps_5_0, D3D11)"


@pytest.mark.parametrize("given,want", [
    ("intel", "Google Inc. (Intel)"), ("Intel Inc.", "Google Inc. (Intel)"), ("Intel Corporation", "Google Inc. (Intel)"),
    ("nvidia", "Google Inc. (NVIDIA)"), ("NVIDIA Corporation", "Google Inc. (NVIDIA)"), ("amd", "Google Inc. (AMD)"),
    ("ATI Technologies Inc.", "Google Inc. (AMD)"), ("Apple", "Google Inc. (Apple)"),
])
def test_written_as_chrome_reports_it(given, want):
    assert gpu_vendor_for(given, None, "windows") == want


def test_keeps_chrome_form_intel_mac_and_unknown():
    assert gpu_vendor_for("Google Inc. (NVIDIA)") == "Google Inc. (NVIDIA)"
    assert gpu_vendor_for("intel", None, "macos") == "Google Inc. (Intel Inc.)"
    assert gpu_vendor_for("Google Inc.", INTEL) == "Google Inc."
    assert gpu_vendor_for("Some Vendor") == "Some Vendor"


def test_refuses_two_makers():
    with pytest.raises(ValueError, match="names Intel but gpu_renderer names AMD"):
        gpu_vendor_for("intel", AMD)
    with pytest.raises(ValueError, match="matching pair"):
        fingerprint_args({"fingerprint": "1", "gpu_vendor": "intel", "gpu_renderer": AMD})
    assert gpu_vendor_for("intel", INTEL) == "Google Inc. (Intel)"


def test_reads_the_maker():
    assert gpu_maker(AMD) == "amd"
    assert gpu_maker("Google Inc. (Intel)") == "intel"
    assert gpu_maker("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)") == "google"
    assert gpu_maker("WebKit") is None


def test_normalized_on_the_command_line():
    args = fingerprint_args({"fingerprint": "1", "platform": "windows", "gpu_vendor": "intel"})
    assert "--fingerprint-gpu-vendor=Google Inc. (Intel)" in args


def test_warns_when_vendor_pinned_alone():
    codes = lambda o: [w["code"] for w in coherence_warnings(o)]  # noqa: E731
    assert "gpu-vendor-alone" in codes({"gpu_vendor": "intel"})
    assert "gpu-vendor-alone" not in codes({"gpu_vendor": "intel", "gpu_renderer": INTEL})
