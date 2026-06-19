import re

from clearcote.release import RELEASE, REPO, SIGNING_KEY_FPR


def test_release_shape():
    for k in ("tag", "version", "asset", "url", "sha256", "exe_sha256", "size", "os"):
        assert k in RELEASE and RELEASE[k] not in (None, ""), k
    assert re.fullmatch(r"[0-9a-f]{64}", RELEASE["sha256"])
    assert re.fullmatch(r"[0-9a-f]{64}", RELEASE["exe_sha256"])
    assert RELEASE["size"] > 0
    assert RELEASE["os"] == "win32"


def test_release_internally_consistent():
    assert RELEASE["version"] in RELEASE["asset"]
    assert RELEASE["asset"].endswith(".zip")
    assert RELEASE["url"] == (
        f"https://github.com/{REPO}/releases/download/{RELEASE['tag']}/{RELEASE['asset']}"
    )
    assert re.match(r"^v\d+\.\d+\.\d+", RELEASE["tag"])
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", RELEASE["version"])


def test_signing_key_fpr():
    assert re.fullmatch(r"[0-9A-F]{40}", SIGNING_KEY_FPR)
