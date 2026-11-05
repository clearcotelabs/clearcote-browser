"""The device id added to an old capture's WebGL renderer before it goes to the engine."""

import base64
import copy
import gzip
import json
import re

from clearcote._angle_device_id import capture_for_engine, fix_capture_renderers, with_angle_device_id
from clearcote._angle_device_ids import BY_ARCH, BY_NAME
from clearcote._fingerprint import encode_profile

OLD = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)"
NEW = "ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 Ti (0x00002782) Direct3D11 vs_5_0 ps_5_0, D3D11)"


def capture():
    return {
        "webgl": {
            "webgl1": {"debug": {"UNMASKED_VENDOR_WEBGL": "Google Inc. (NVIDIA)", "UNMASKED_RENDERER_WEBGL": OLD}},
            "webgl2": {"debug": {"UNMASKED_VENDOR_WEBGL": "Google Inc. (NVIDIA)", "UNMASKED_RENDERER_WEBGL": OLD}},
        },
        "webgpu": {"high_performance": {"info": {"vendor": "nvidia", "architecture": "lovelace"}}},
        "hardware_concurrency": 16,
    }


def decode(b64):
    return json.loads(gzip.decompress(base64.b64decode(b64)))


def test_adds_the_id_real_captures_print():
    assert with_angle_device_id(OLD) == NEW
    assert with_angle_device_id(OLD, "lovelace") == NEW
    assert with_angle_device_id(OLD.replace("Direct3D11 ", "Direct3D11on12 ")) == NEW.replace("Direct3D11 ", "Direct3D11on12 ")


def test_leaves_alone_what_it_should():
    assert with_angle_device_id(NEW) == NEW
    gl = "ANGLE (NVIDIA Corporation, NVIDIA GeForce RTX 4070 Ti/PCIe/SSE2, OpenGL 4.5.0)"
    assert with_angle_device_id(gl) == gl
    odd = "ANGLE (NVIDIA, NVIDIA GeForce Imaginary 9999 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    assert with_angle_device_id(odd) == odd


def test_generic_name_follows_the_chip_generation():
    amd = "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)"
    assert f"({BY_ARCH['AMD|AMD Radeon(TM) Graphics|gcn-5']}) Direct3D11" in with_angle_device_id(amd, "gcn-5")
    assert f"({BY_NAME['AMD|AMD Radeon(TM) Graphics']}) Direct3D11" in with_angle_device_id(amd, "none")


def test_every_id_has_angles_form():
    for dev in list(BY_ARCH.values()) + list(BY_NAME.values()):
        assert re.fullmatch(r"0x[0-9A-F]{8}", dev)


def test_encode_profile_from_dict_file_and_string(tmp_path):
    c = capture()
    before = copy.deepcopy(c)
    out = decode(encode_profile(c))
    assert out["webgl"]["webgl1"]["debug"]["UNMASKED_RENDERER_WEBGL"] == NEW
    assert out["webgl"]["webgl2"]["debug"]["UNMASKED_RENDERER_WEBGL"] == NEW
    assert out["hardware_concurrency"] == 16
    assert c == before  # the caller's dict is not touched
    f = tmp_path / "cap.json"
    f.write_text(json.dumps(capture(), indent=2), encoding="utf-8")
    assert decode(encode_profile(str(f)))["webgl"]["webgl2"]["debug"]["UNMASKED_RENDERER_WEBGL"] == NEW
    assert decode(encode_profile(json.dumps(capture())))["webgl"]["webgl1"]["debug"]["UNMASKED_RENDERER_WEBGL"] == NEW


def test_fine_capture_and_non_json_pass_through_byte_for_byte():
    good = capture()
    fix_capture_renderers(good)
    text = json.dumps(good, indent=2)
    assert gzip.decompress(base64.b64decode(encode_profile(text))).decode("utf-8") == text
    assert capture_for_engine(b"not json") == b"not json"
    assert fix_capture_renderers({}) is False
