"""The MCPB bundle (mcp/mcpb) agrees with the package it runs: the same version, a pin on exactly that release,
the server's tools, and an env entry for every variable the registry entry lists."""
import asyncio
import json
import pathlib
import re

import clearcote_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "mcpb"


def manifest() -> dict:
    return json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))


def test_bundle_pins_this_release():
    version = clearcote_mcp.__version__
    assert manifest()["version"] == version
    pyproject = (BUNDLE / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(rf'"clearcote-mcp=={re.escape(version)}"', pyproject), "pin clearcote-mcp to this release"


def test_bundle_lists_the_servers_tools():
    from clearcote_mcp.server import mcp
    tools = asyncio.run(mcp.list_tools())
    assert sorted(t["name"] for t in manifest()["tools"]) == sorted(t.name for t in tools)


def test_bundle_passes_every_registry_variable():
    registry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    names = {e["name"] for e in registry["packages"][0]["environmentVariables"]}
    server = manifest()["server"]
    assert set(server["mcp_config"]["env"]) == names
    config = manifest()["user_config"]
    for value in server["mcp_config"]["env"].values():
        key = re.fullmatch(r"\$\{user_config\.(\w+)\}", value).group(1)
        assert key in config and config[key]["required"] is False
