"""Every SDK publish workflow must wait for the same gates before it publishes.

npm.yml and pypi.yml call sdk-ci.yml (unit tests + the release-pin guard, which also checks the .NET pin)
and stealth-coherence.yml, and their publish job needs both. A publish workflow that skips them can ship an
SDK whose pinned browser is missing or checksum-mismatched, so the rule is checked here instead of relying
on someone noticing in review.

    python -m unittest discover -s scripts/tests -v
"""

import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Workflow file -> the job that publishes (the one that must wait for the gates).
SDK_PUBLISHERS = {
    "npm.yml": "publish",
    "pypi.yml": "build",
    "nuget.yml": "publish",
}
REQUIRED_GATES = ("./.github/workflows/sdk-ci.yml", "./.github/workflows/stealth-coherence.yml")


def _load(name):
    with open(WORKFLOWS / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _needs(job):
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class PublishGates(unittest.TestCase):
    def test_sdk_ci_is_callable(self):
        for gate in REQUIRED_GATES:
            with self.subTest(gate=gate):
                doc = _load(Path(gate).name)
                on = doc.get(True) or doc.get("on")  # PyYAML reads the `on:` key as True
                self.assertIn("workflow_call", on)

    def test_sdk_ci_runs_the_release_pin_guard(self):
        steps = [s.get("run", "") for job in _load("sdk-ci.yml")["jobs"].values() for s in job.get("steps", [])]
        self.assertTrue(any("check_release_pin.py" in r for r in steps))

    def test_every_sdk_publisher_waits_for_the_gates(self):
        for name, publish_job in SDK_PUBLISHERS.items():
            with self.subTest(workflow=name):
                jobs = _load(name)["jobs"]
                self.assertIn(publish_job, jobs, f"{name} has no job '{publish_job}'")
                gate_jobs = {gate: [j for j, spec in jobs.items() if spec.get("uses") == gate]
                             for gate in REQUIRED_GATES}
                for gate, callers in gate_jobs.items():
                    self.assertTrue(callers, f"{name} never calls {gate}")
                    self.assertTrue(set(callers) & set(_needs(jobs[publish_job])),
                                    f"{name}: job '{publish_job}' does not wait for {gate}")


if __name__ == "__main__":
    unittest.main()
