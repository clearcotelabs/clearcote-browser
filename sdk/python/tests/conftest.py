"""Shared pytest setup.

geoip is on by default (0.66.0): suites that launch must not look this machine's IP up for real.
setdefault, so a test (or a caller's own environment) can still turn it on; tests/test_geoip_default.py
covers the default with a stubbed resolver.
"""
import os

os.environ.setdefault("CLEARCOTE_GEOIP", "0")
