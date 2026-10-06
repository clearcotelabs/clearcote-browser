"""The MCP Registry entry (mcp/server.json) agrees with the packages it lists: the same version as the Python package
(and so the npm launcher, see test_packaging.py), the same names, and the ownership markers the registry checks
(`mcpName` in package.json for npm, `mcp-name: <name>` in the README that becomes the PyPI description)."""
import json
import pathlib
import re
import urllib.request

import pytest

import clearcote_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def server_json() -> dict:
    return json.loads((ROOT / "server.json").read_text(encoding="utf-8"))


def project(key: str) -> str:
    return re.search(rf'^{key}\s*=\s*"([^"]+)"', PYPROJECT, re.M).group(1)


def test_server_json_version_matches_the_python_package():
    entry = server_json()
    assert entry["version"] == project("version") == clearcote_mcp.__version__
    assert {p["registryType"]: p["version"] for p in entry["packages"]} == {"pypi": entry["version"],
                                                                          "npm": entry["version"]}


def test_the_listed_packages_carry_the_registry_name():
    entry = server_json()
    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    assert {p["registryType"]: p["identifier"] for p in entry["packages"]} == {"pypi": project("name"),
                                                                             "npm": npm["name"]}
    assert npm["mcpName"] == entry["name"]
    readme = (ROOT / project("readme")).read_text(encoding="utf-8")
    # The registry wants the token followed by a boundary: whitespace, a tag or the end of a comment.
    assert re.search(rf"mcp-name: {re.escape(entry['name'])}(\s|<|-->)", readme)


def test_server_json_matches_the_schema_it_names():
    """Fetches the schema its $schema names; skipped offline."""
    jsonschema = pytest.importorskip("jsonschema")
    entry = server_json()
    try:
        with urllib.request.urlopen(entry["$schema"], timeout=20) as r:
            schema = json.load(r)
    except OSError as exc:
        pytest.skip(f"schema not reachable: {exc}")
    cls = jsonschema.validators.validator_for(schema)
    errors = sorted(cls(schema, format_checker=cls.FORMAT_CHECKER).iter_errors(entry), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)
