"""Packaging guards: the three version fields agree, and the `mcp` dependency is capped at the next
major (an uncapped `mcp[cli]>=1.2` is what let pip resolve the incompatible mcp 2.x for 0.1.0)."""
import json
import pathlib
import re

import clearcote_mcp

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _dependency(name: str) -> str:
    deps = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", PYPROJECT, re.M | re.S).group(1)
    for spec in re.findall(r'"([^"]+)"', deps):
        if re.match(rf"{re.escape(name)}(\[|[<>=!~ ]|$)", spec):
            return spec
    raise AssertionError(f"{name} is not a dependency")


def test_versions_agree():
    py = re.search(r'^version\s*=\s*"([^"]+)"', PYPROJECT, re.M).group(1)
    npm = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))["version"]
    assert clearcote_mcp.__version__ == py == npm


def test_mcp_dependency_has_an_upper_bound():
    assert "<" in _dependency("mcp"), "cap mcp at the next major: an unreleased major can break the import"


def test_clearcote_floor_supports_free_keys():
    floor = re.search(r">=\s*([\d.]+)", _dependency("clearcote")).group(1)
    assert tuple(int(p) for p in floor.split(".")) >= (0, 30), "free-tier keys need clearcote 0.30.0+"


def _floor(name: str) -> str:
    return re.search(r">=\s*([\d.]+)", _dependency(name)).group(1)


def test_mcp_floor_passes_tool_images_through():
    # mcp before 1.19 turns a CallToolResult a tool returns (screenshot_page's inline image) into one JSON text block.
    assert tuple(int(p) for p in _floor("mcp").split(".")) >= (1, 19)


def test_ci_floor_job_pins_the_declared_floors():
    ci = (ROOT.parent / ".github" / "workflows" / "mcp-ci.yml").read_text(encoding="utf-8")
    pins = dict(re.findall(r"(mcp|clearcote)==([\d.]+)", ci))  # inside printf 'mcp==..\nclearcote==..'

    def norm(v):
        parts = [int(p) for p in v.split(".")]
        while parts and parts[-1] == 0:
            parts.pop()
        return parts
    assert {k: norm(v) for k, v in pins.items()} == {"mcp": norm(_floor("mcp")), "clearcote": norm(_floor("clearcote"))}


def test_launcher_requires_the_packages_python():
    needed = re.search(r'^requires-python\s*=\s*">=\s*([\d.]+)"', PYPROJECT, re.M).group(1)
    cli = (ROOT / "npm" / "cli.js").read_text(encoding="utf-8")
    found = re.search(r"MIN_PYTHON\s*=\s*\[(\d+),\s*(\d+)\]", cli)
    assert found and ".".join(found.groups()) == needed


def test_the_changelog_describes_this_version():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {clearcote_mcp.__version__}" in changelog
