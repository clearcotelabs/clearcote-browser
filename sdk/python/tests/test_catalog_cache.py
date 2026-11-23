"""The version catalog is asked once per ~10 minutes per machine, not once per launch: a burst of launches from
one address was answered with a 403 challenge page by the site's host.

Mirrors sdk/node/test/catalog-cache.test.ts and sdk/dotnet/tests/Clearcote.Tests/CatalogCacheTests.cs.
"""
import importlib
import json
import time
import urllib.error

import pytest

from clearcote.release import CATALOG_FALLBACK, CATALOG_URL

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

LIVE = {"schema": 1, "builds": [{"major": 999, "version": "999.0.1.2", "tier": "free", "platforms": {}}]}


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setattr(download, "_catalog_memo", {})
    monkeypatch.setattr(download, "_catalog_spread", 0.0)
    return h


class Site:
    """Stands in for _http_get: counts the requests and answers the catalog, or fails like the challenge."""

    def __init__(self, monkeypatch, body=LIVE):
        self.hits = 0
        self.body = body
        self.fail = False
        monkeypatch.setattr(download, "_http_get", self)

    def __call__(self, url):
        assert url == CATALOG_URL
        self.hits += 1
        if self.fail:
            raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)
        return json.dumps(self.body).encode("utf-8")


def new_process(monkeypatch):
    monkeypatch.setattr(download, "_catalog_memo", {})  # only the file is left


def age_file(home, seconds):
    path = home / ".clearcote" / "catalog.json"
    entry = json.loads(path.read_text())
    entry["at"] -= seconds
    path.write_text(json.dumps(entry))


def test_many_launches_ask_once(home, monkeypatch):
    site = Site(monkeypatch)
    assert all(download._fetch_catalog(quiet=True) == LIVE for _ in range(25))
    assert site.hits == 1
    entry = json.loads((home / ".clearcote" / "catalog.json").read_text())
    assert entry["url"] == CATALOG_URL and entry["catalog"] == LIVE and abs(entry["at"] - time.time()) < 60


def test_another_process_uses_the_kept_file(home, monkeypatch):
    site = Site(monkeypatch)
    download._fetch_catalog(quiet=True)
    new_process(monkeypatch)
    assert download._fetch_catalog(quiet=True) == LIVE
    assert site.hits == 1


def test_an_old_answer_is_asked_again(home, monkeypatch):
    site = Site(monkeypatch)
    download._fetch_catalog(quiet=True)
    age_file(home, download._CATALOG_TTL + 1)
    new_process(monkeypatch)
    download._fetch_catalog(quiet=True)
    assert site.hits == 2


def test_the_spread_keeps_an_answer_longer_in_this_process(home, monkeypatch):
    site = Site(monkeypatch)
    download._fetch_catalog(quiet=True)
    age_file(home, download._CATALOG_TTL + 1)
    new_process(monkeypatch)
    monkeypatch.setattr(download, "_catalog_spread", 200.0)
    download._fetch_catalog(quiet=True)
    assert site.hits == 1


def test_a_403_uses_the_last_kept_answer_not_the_bundled_one(home, monkeypatch, capsys):
    site = Site(monkeypatch)
    download._fetch_catalog(quiet=True)
    age_file(home, 3600)
    new_process(monkeypatch)
    site.fail = True
    assert download._fetch_catalog(quiet=False) == LIVE
    assert site.hits == 2
    assert "last answer kept on this machine" in capsys.readouterr().err


def test_a_403_with_nothing_kept_uses_the_bundled_snapshot(home, monkeypatch):
    site = Site(monkeypatch)
    site.fail = True
    assert download._fetch_catalog(quiet=True) is CATALOG_FALLBACK
    assert not (home / ".clearcote" / "catalog.json").exists()  # a failure is never kept


def test_an_answer_older_than_a_week_is_not_used_on_failure(home, monkeypatch):
    site = Site(monkeypatch)
    download._fetch_catalog(quiet=True)
    age_file(home, download._CATALOG_STALE_MAX + 1)
    new_process(monkeypatch)
    site.fail = True
    assert download._fetch_catalog(quiet=True) is CATALOG_FALLBACK


@pytest.mark.parametrize("content", ["", "{", "[]", '{"at": "x"}', '{"at": 1, "url": "u", "catalog": {}}'])
def test_an_unreadable_file_is_a_miss(home, monkeypatch, content):
    site = Site(monkeypatch)
    (home / ".clearcote").mkdir(parents=True)
    (home / ".clearcote" / "catalog.json").write_text(content)
    assert download._fetch_catalog(quiet=True) == LIVE
    assert site.hits == 1


def test_a_file_for_another_url_or_from_the_future_is_a_miss(home, monkeypatch):
    site = Site(monkeypatch)
    (home / ".clearcote").mkdir(parents=True)
    path = home / ".clearcote" / "catalog.json"
    other = {"schema": 1, "builds": [{"major": 1, "version": "1.0.0.0", "platforms": {}}]}
    path.write_text(json.dumps({"at": time.time(), "url": "https://example.test/versions", "catalog": other}))
    assert download._fetch_catalog(quiet=True) == LIVE
    new_process(monkeypatch)
    path.write_text(json.dumps({"at": time.time() + 3600, "url": CATALOG_URL, "catalog": other}))
    assert download._fetch_catalog(quiet=True) == LIVE
    assert site.hits == 2


def test_an_answer_without_builds_is_not_kept(home, monkeypatch):
    site = Site(monkeypatch, body={"schema": 1, "builds": []})
    assert download._fetch_catalog(quiet=True) is CATALOG_FALLBACK
    assert not (home / ".clearcote" / "catalog.json").exists()
