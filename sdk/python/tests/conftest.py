"""Shared pytest setup.

geoip is on by default (0.66.0): suites that launch must not look this machine's IP up for real.
setdefault, so a test (or a caller's own environment) can still turn it on; tests/test_geoip_default.py
covers the default with a stubbed resolver.
"""
import os

import pytest

os.environ.setdefault("CLEARCOTE_GEOIP", "0")


@pytest.fixture
def pro_test_key(monkeypatch):
    """Pin the TEST release key (sdk/testdata/pro_manifest_vectors.json) for one test, so the manifests the
    fake download route signs (tests/_fake_build.py, tests/_manifest_signer.py) verify. The SDK's own map is
    put back afterwards; the test key is never in it."""
    from clearcote import release

    from _manifest_signer import TEST_KEYS

    monkeypatch.setattr(release, "PRO_RELEASE_KEYS", dict(TEST_KEYS))
    return TEST_KEYS
