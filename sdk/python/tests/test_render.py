from clearcote._render import evaluate_render_info


def test_coherent_nvidia_persona():
    v = evaluate_render_info({
        "webgl": True, "webgl2": True,
        "vendor": "Google Inc. (NVIDIA)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)",
        "maxTextureSize": 16384,
    })
    assert v["coherent"] is True
    assert v["software_suspected"] is False
    assert v["warnings"] == []


def test_software_rasterizer_is_a_fatal_tell():
    v = evaluate_render_info({
        "webgl": True,
        "vendor": "Google Inc. (Google)",
        "renderer": "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 16.0.0)), SwiftShader driver)",
    })
    assert v["software_suspected"] is True
    assert v["coherent"] is False
    assert any("software rasterizer" in w for w in v["warnings"])


def test_incoherent_vendor_renderer_family():
    v = evaluate_render_info({
        "webgl": True,
        "vendor": "Google Inc. (Apple)",
        "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080, D3D11)",
    })
    assert v["coherent"] is False
    assert any("disagree on GPU family" in w for w in v["warnings"])


def test_no_webgl_is_a_tell():
    v = evaluate_render_info({"webgl": False})
    assert v["coherent"] is False
    assert any("WebGL is unavailable" in w for w in v["warnings"])


def test_claimed_gpu_mismatch():
    v = evaluate_render_info(
        {"webgl": True, "vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 770, D3D11)"},
        claimed_gpu="NVIDIA GeForce RTX 4090",
    )
    assert v["coherent"] is False
    assert any("does not match" in w for w in v["warnings"])


def test_unmasked_pair_preferred_over_masked():
    v = evaluate_render_info({
        "webgl": True,
        "vendor": "WebKit", "renderer": "WebKit WebGL",
        "unmaskedVendor": "Google Inc. (Intel)",
        "unmaskedRenderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, D3D11)",
    })
    assert v["renderer"].startswith("ANGLE (Intel")
    assert v["coherent"] is True
