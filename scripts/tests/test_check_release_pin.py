"""Tests for scripts/check_release_pin.py (the release-pin gate).

Offline: the GitHub API and SHA256SUMS.txt are faked from the pins themselves, and the three pin files are
copied into a temporary directory and edited there, so every check can be driven to pass and to fail.

    python -m unittest discover -s scripts/tests -v
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release_pin.py"
PY_RELEASE = ROOT / "sdk" / "python" / "clearcote" / "release.py"
TS_RELEASE = ROOT / "sdk" / "node" / "src" / "release.ts"
CS_RELEASE = ROOT / "sdk" / "dotnet" / "src" / "Clearcote" / "Release.cs"
WORKFLOW = ROOT / ".github" / "workflows" / "sdk-ci.yml"

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it; the workflow test needs a real YAML parser
    yaml = None


def load_gate():
    spec = importlib.util.spec_from_file_location("check_release_pin_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def python_pins():
    import runpy
    ns = runpy.run_path(str(PY_RELEASE))
    return ns["PLATFORMS"], ns["REPO"]


class FakeGitHub:
    """Serves /releases/tags/<tag> and SHA256SUMS.txt built from the Python pins (which the real gate
    checks against the other SDKs). `digest` controls the asset's digest field: a callable(pin) -> value,
    or None to leave the field out, as GitHub did for assets uploaded before it computed digests."""

    def __init__(self, digest=lambda pin: "sha256:" + pin["sha256"], extra_assets=()):
        self.platforms, self.repo = python_pins()
        self.digest = digest
        self.extra_assets = list(extra_assets)  # more asset names on every release, after the pinned ones
        self.urls = []

    def __call__(self, url, token=None):
        self.urls.append(url)
        m = re.search(r"/releases/tags/([^/]+)$", url)
        if m:
            tag = m.group(1)
            assets = []
            for pin in self.platforms.values():
                if pin["tag"] != tag:
                    continue
                a = {"name": pin["asset"], "browser_download_url": pin["url"], "size": pin["size"]}
                if self.digest is not None:
                    a["digest"] = self.digest(pin)
                assets.append(a)
            for name in self.extra_assets:
                assets.append({"name": name, "browser_download_url": f"https://github.com/{self.repo}/releases/download/{tag}/{name}"})
            assets.append({"name": "SHA256SUMS.txt",
                           "browser_download_url": f"https://github.com/{self.repo}/releases/download/{tag}/SHA256SUMS.txt"})
            return json.dumps({"tag_name": tag, "assets": assets})
        if url.endswith("/SHA256SUMS.txt"):
            lines = []
            for pin in self.platforms.values():
                lines.append(f"{pin['sha256']}  {pin['asset']}")
                lines.append(f"{pin['exe_sha256']}  {pin['binary']}")
            return "\n".join(lines) + "\n"
        raise AssertionError(f"unexpected URL {url}")


class GateCase(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self._tmp = tempfile.TemporaryDirectory(prefix="cc-pin-test-")
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.py, self.ts, self.cs = tmp / "release.py", tmp / "release.ts", tmp / "Release.cs"
        for src, dst in ((PY_RELEASE, self.py), (TS_RELEASE, self.ts), (CS_RELEASE, self.cs)):
            shutil.copyfile(src, dst)

    def edit(self, path, old, new, count=1):
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"fixture edit target not found in {path.name}")
        path.write_text(text.replace(old, new, count), encoding="utf-8")

    def edit_all(self, py, ts, cs):
        """The same change in all three SDKs (each an (old, new) pair), so the pins still agree."""
        for path, (old, new) in ((self.py, py), (self.ts, ts), (self.cs, cs)):
            self.edit(path, old, new)

    def run_gate(self, fake=None):
        """Run main() against the temp pin files and a fake GitHub. Returns (exit code, stdout)."""
        fake = fake or FakeGitHub()
        out = io.StringIO()
        code = 0
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(self.gate, "PY_RELEASE", self.py))
            st.enter_context(mock.patch.object(self.gate, "TS_RELEASE", self.ts))
            st.enter_context(mock.patch.object(self.gate, "CS_RELEASE", self.cs, create=True))
            st.enter_context(mock.patch.object(self.gate, "http", fake))
            st.enter_context(mock.patch.object(self.gate.time, "sleep", lambda s: None))
            st.enter_context(mock.patch.dict(os.environ, {"RELEASE_PIN_WAIT_SECS": "0"}))  # restored on exit
            os.environ.pop("GITHUB_ACTIONS", None)
            st.enter_context(contextlib.redirect_stdout(out))
            try:
                self.gate.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue()


class BaselineTest(GateCase):
    def test_consistent_pins_pass(self):
        code, out = self.run_gate()
        self.assertEqual(code, 0, out)
        self.assertIn("OK: all platform pins verified", out)


class DotnetPinTest(GateCase):
    """The .NET SDK carries its own copy of the pin (sdk/dotnet/src/Clearcote/Release.cs)."""

    def test_real_release_cs_parses_and_matches_python(self):
        pins = self.gate.load_dotnet()
        py, _ = python_pins()
        self.assertEqual(set(pins), set(py))
        for oskey, p in py.items():
            d = pins[oskey]
            self.assertEqual(d["tag"], p["tag"])
            self.assertEqual(d["sha256"], p["sha256"])
            self.assertEqual(d["exeSha256"], p["exe_sha256"])
            self.assertEqual(d["size"], int(p["size"]))
            self.assertEqual(d["os"], p["os"])  # "windows" in C# is normalised to "win32"

    def test_dotnet_archive_hash_drift_fails(self):
        py, _ = python_pins()
        good = py["win32"]["sha256"]
        self.edit(self.cs, f'Sha256 = "{good}"', f'Sha256 = "{"0" * 64}"')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("sha256", out)
        self.assertIn("dotnet", out)

    def test_dotnet_tag_drift_fails(self):
        py, _ = python_pins()
        tag = py["linux"]["tag"]
        text = self.cs.read_text(encoding="utf-8")
        linux_block = text[text.index("ReleaseInfo Linux"):]
        self.edit(self.cs, linux_block, linux_block.replace(f'Tag = "{tag}"', 'Tag = "v0.0.0-drifted"', 1))
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("tag", out)
        self.assertIn("dotnet", out)

    def test_dotnet_missing_platform_fails(self):
        self.edit(self.cs, ', ["linux"] = Linux', "")
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("platform", out)

    def test_dotnet_unparsable_pin_fails(self):
        self.edit(self.cs, "ExeSha256 = ", "ExeSha256Renamed = ")
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("Release.cs", out)


class CommentedOutCodeTest(GateCase):
    """Commented-out pin code is not a pin: C# and TypeScript comments are removed before parsing."""

    def test_strip_comments_keeps_strings_and_line_structure(self):
        src = ('var u = "https://x/y"; // gone 1\n'
               '/* gone 2\n   gone 3 */ var c = \'"\'; var v = @"C:\\dir\\"; var s = "a /* b */ c // d";\n'
               'var r = """raw // kept""";  /** gone 4 */\n'
               'const t = `tpl // kept`; const e = "esc \\" // kept";\n')
        out = self.gate.strip_comments(src)
        self.assertEqual(out.count("\n"), src.count("\n"))
        for kept in ('"https://x/y"', "'\"'", '@"C:\\dir\\"', '"a /* b */ c // d"', '"""raw // kept"""',
                     "`tpl // kept`", '"esc \\" // kept"'):
            self.assertIn(kept, out)
        self.assertNotIn("gone", out)

    def test_dotnet_platform_in_a_block_comment_is_not_pinned(self):
        self.edit(self.cs, ', ["linux"] = Linux', ' /* , ["linux"] = Linux */')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("platform sets differ", out)

    def test_dotnet_platform_in_a_line_comment_is_not_pinned(self):
        self.edit(self.cs, ', ["linux"] = Linux };', ', // ["linux"] = Linux\n        };')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("platform sets differ", out)

    def test_dotnet_field_in_a_line_comment_is_ignored(self):
        tag = python_pins()[0]["win32"]["tag"]
        self.edit(self.cs, f'Tag = "{tag}",', f'// Tag = "{tag}",\n        Tag = "v0.0.0-drifted",')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("v0.0.0-drifted", out)

    def test_dotnet_field_in_a_block_comment_is_ignored(self):
        tag = python_pins()[0]["win32"]["tag"]
        self.edit(self.cs, f'Tag = "{tag}",', f'/* Tag = "{tag}", */ Tag = "v0.0.0-drifted",')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("v0.0.0-drifted", out)

    def test_node_field_in_a_line_comment_is_ignored(self):
        tag = python_pins()[0]["win32"]["tag"]
        self.edit(self.ts, f'tag: "{tag}",', f'// tag: "{tag}",\n  tag: "v0.0.0-drifted",')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("v0.0.0-drifted", out)


class ArchiveAndAssetGlobTest(GateCase):
    """`archive` picks how the SDKs unpack the asset; `assetGlob` is the marker their auto-update uses to pick a
    release's asset (`^clearcote-.*-<glob>\\.(zip|tar.xz)$`). Both must agree across the SDKs and with the
    release's real asset names."""

    WIN_GLOB = ('"asset_glob": "windows-x64",', 'assetGlob: "windows-x64",', 'AssetGlob = "windows-x64",')
    LINUX_ARCHIVE = ('"archive": "tar.xz",', 'archive: "tar.xz",', 'Archive = "tar.xz",')

    def change(self, triple, old, new):
        self.edit_all(*[(t, t.replace(old, new)) for t in triple])

    def test_every_sdk_pins_archive_and_glob(self):
        py, _ = python_pins()
        for sdk, load in (("node", self.gate.load_node), ("python", self.gate.load_python),
                          ("dotnet", self.gate.load_dotnet)):
            pins = load()
            for oskey, p in py.items():
                self.assertEqual(pins[oskey]["archive"], p["archive"], f"{sdk} {oskey}")
                self.assertEqual(pins[oskey]["assetGlob"], p["asset_glob"], f"{sdk} {oskey}")

    def test_dotnet_archive_drift_fails(self):
        self.edit(self.cs, 'Archive = "tar.xz",', 'Archive = "zip",')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("archive", out)
        self.assertIn("dotnet", out)

    def test_dotnet_asset_glob_drift_fails(self):
        self.edit(self.cs, 'AssetGlob = "linux-x64",', 'AssetGlob = "linux-arm64",')
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("assetGlob", out)
        self.assertIn("dotnet", out)

    def test_archive_that_is_not_the_assets_format_fails(self):
        self.change(self.LINUX_ARCHIVE, "tar.xz", "zip")
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("[linux]", out)
        self.assertIn("archive", out)

    def test_unknown_archive_format_fails(self):
        self.change(self.LINUX_ARCHIVE, "tar.xz", "xz")
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("archive", out)

    def test_glob_that_does_not_pick_the_pinned_asset_fails(self):
        self.change(self.WIN_GLOB, "windows-x64", "windows-arm64")
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("[win32]", out)
        self.assertIn("assetGlob", out)

    def test_glob_that_also_picks_another_platforms_asset_fails(self):
        self.change(self.WIN_GLOB, "windows-x64", "x64")
        code, out = self.run_gate()
        self.assertEqual(code, 1, out)
        self.assertIn("assetGlob", out)
        self.assertIn(python_pins()[0]["linux"]["asset"], out)

    def test_second_asset_matching_the_glob_in_the_release_fails(self):
        win = python_pins()[0]["win32"]["asset"]
        fake = FakeGitHub(extra_assets=[win[:-len(".zip")] + ".tar.xz"])
        code, out = self.run_gate(fake)
        self.assertEqual(code, 1, out)
        self.assertIn("assetGlob", out)

    def test_real_release_side_files_do_not_match_the_glob(self):
        # the published release also carries per-asset .sha256 / .sha256.asc files and the signing key
        pins = python_pins()[0].values()
        extra = [p["asset"] + s for p in pins for s in (".sha256", ".sha256.asc")]
        code, out = self.run_gate(FakeGitHub(extra_assets=extra + ["clearcote-signing-key.asc", "SHA256SUMS.txt.asc"]))
        self.assertEqual(code, 0, out)


class AssetDigestTest(GateCase):
    """GitHub release assets carry `digest: "sha256:<hex>"`; it must equal the pinned archive hash."""

    def test_digest_mismatch_fails(self):
        fake = FakeGitHub(digest=lambda pin: "sha256:" + "f" * 64)
        code, out = self.run_gate(fake)
        self.assertEqual(code, 1, out)
        self.assertIn("digest", out)

    def test_digest_mismatch_on_one_platform_fails(self):
        fake = FakeGitHub(digest=lambda pin: "sha256:" + ("0" * 64 if pin["os"] == "linux" else pin["sha256"]))
        code, out = self.run_gate(fake)
        self.assertEqual(code, 1, out)
        self.assertIn("[linux]", out)
        self.assertIn("digest", out)

    def test_matching_digest_is_reported(self):
        code, out = self.run_gate()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"digest matches")

    def test_missing_digest_is_tolerated(self):
        code, out = self.run_gate(FakeGitHub(digest=None))
        self.assertEqual(code, 0, out)
        self.assertIn("no digest", out)

    def test_uppercase_sha256_digest_matches(self):
        code, out = self.run_gate(FakeGitHub(digest=lambda pin: "sha256:" + pin["sha256"].upper()))
        self.assertEqual(code, 0, out)

    def test_other_digest_algorithm_is_not_compared(self):
        code, out = self.run_gate(FakeGitHub(digest=lambda pin: "sha512:" + "a" * 128))
        self.assertEqual(code, 0, out)
        self.assertIn("sha512", out)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class WorkflowTriggerTest(unittest.TestCase):
    def setUp(self):
        self.wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        # YAML 1.1 reads a bare `on:` key as boolean True
        self.on = self.wf.get("on", self.wf.get(True))

    def test_gate_runs_when_a_release_is_published(self):
        release = self.on.get("release")
        self.assertIsInstance(release, dict, "sdk-ci.yml has no `release:` trigger")
        self.assertIn("published", release.get("types", []))
        job = self.wf["jobs"]["release-pin"]
        self.assertNotIn("release", str(job.get("if", "")), "the release-pin job must not skip release events")
        self.assertIn("check_release_pin.py", json.dumps(job["steps"]))

    def test_existing_triggers_kept(self):
        for key in ("pull_request", "push", "workflow_call", "workflow_dispatch"):
            self.assertIn(key, self.on)


if __name__ == "__main__":
    unittest.main()
