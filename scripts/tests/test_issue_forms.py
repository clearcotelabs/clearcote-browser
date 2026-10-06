"""Validate .github/ISSUE_TEMPLATE/*.yml against GitHub's issue-form syntax rules.

GitHub only reports a broken form after it is on the default branch (the template silently disappears from
the "New issue" chooser), so the rules are checked here instead: required top-level keys, permitted keys,
element types and their attributes, unique ids and labels, id characters, and dropdown/checkbox options.
Source: GitHub Docs, "Syntax for issue forms", "Syntax for GitHub's form schema" and "Common validation
errors when creating issue forms".

    python -m unittest discover -s scripts/tests -v
"""

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - CI installs it
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
FINGERPRINT_FORM = TEMPLATES / "fingerprint_difference.yml"

TOP_KEYS = {"name", "description", "title", "labels", "assignees", "projects", "type", "body"}
ELEMENT_KEYS = {"type", "id", "attributes", "validations"}
ATTRIBUTES = {
    "markdown": {"value"},
    "input": {"label", "description", "placeholder", "value"},
    "textarea": {"label", "description", "placeholder", "value", "render"},
    "dropdown": {"label", "description", "multiple", "options", "default"},
    "checkboxes": {"label", "description", "options"},
}
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _nonempty_str(v):
    return isinstance(v, str) and v.strip() != ""


def validate_form(form):
    """Return a list of problems (empty = valid) for one parsed issue form."""
    errs = []
    if not isinstance(form, dict):
        return ["the form must be a YAML mapping"]
    for key in ("name", "description", "body"):
        if key not in form:
            errs.append(f"required top level key {key} is missing")
    for key in form:
        if key not in TOP_KEYS:
            errs.append(f"{key!r} is not a permitted key")
    for key in ("name", "description"):
        if key in form and not _nonempty_str(form[key]):
            errs.append(f"{key} must be a non-empty string")
    if "title" in form and not isinstance(form["title"], str):
        errs.append("title must be a string")
    for key in ("labels", "assignees", "projects"):
        if key in form and not (isinstance(form[key], str)
                                or (isinstance(form[key], list) and all(isinstance(x, str) for x in form[key]))):
            errs.append(f"{key} must be a string or a list of strings")
    body = form.get("body")
    if "body" in form and (not isinstance(body, list) or not body):
        errs.append("body must be a non-empty array")
        body = []
    body = body or []

    ids, labels, fields = set(), set(), 0
    for i, el in enumerate(body):
        where = f"body[{i}]"
        if not isinstance(el, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        for key in el:
            if key not in ELEMENT_KEYS:
                errs.append(f"{where}: {key!r} is not a permitted key")
        typ = el.get("type")
        if typ is None:
            errs.append(f"{where}: required key type is missing")
            continue
        if typ not in ATTRIBUTES:
            errs.append(f"{where}: {typ!r} is not a valid input type")
            continue
        attrs = el.get("attributes")
        if not isinstance(attrs, dict):
            errs.append(f"{where}: required key attributes is missing")
            continue
        for key in attrs:
            if key not in ATTRIBUTES[typ]:
                errs.append(f"{where}: attribute {key!r} is not permitted for {typ}")
        if "id" in el:
            if typ == "markdown":
                errs.append(f"{where}: markdown elements cannot have an id")
            elif not isinstance(el["id"], str) or not ID_RE.match(el["id"]):
                errs.append(f"{where}: id can only contain numbers, letters, -, _")
            elif el["id"] in ids:
                errs.append(f"{where}: body must have unique ids ({el['id']!r} repeats)")
            else:
                ids.add(el["id"])
        if "validations" in el:
            v = el["validations"]
            if typ in ("markdown", "checkboxes"):
                errs.append(f"{where}: validations are not supported for {typ}")
            elif not isinstance(v, dict) or set(v) - {"required"} or not isinstance(v.get("required", False), bool):
                errs.append(f"{where}: validations may only hold required: true|false")
        if typ == "markdown":
            if not _nonempty_str(attrs.get("value")):
                errs.append(f"{where}: required attribute key value is missing")
            continue
        fields += 1
        label = attrs.get("label")
        if not _nonempty_str(label):
            errs.append(f"{where}: label must not be empty")
        elif label in labels:
            errs.append(f"{where}: body must have unique labels ({label!r} repeats)")
        else:
            labels.add(label)
        if typ == "textarea" and "render" in attrs and not _nonempty_str(attrs["render"]):
            errs.append(f"{where}: render must be a language name")
        if typ == "dropdown":
            opts = attrs.get("options")
            if not isinstance(opts, list) or not opts:
                errs.append(f"{where}: options must not be empty")
            else:
                if any(isinstance(o, bool) for o in opts):
                    errs.append(f"{where}: options must not include booleans (quote values such as 'yes')")
                if any(not _nonempty_str(o) for o in opts if not isinstance(o, bool)):
                    errs.append(f"{where}: options must be non-empty strings")
                if len(set(map(str, opts))) != len(opts):
                    errs.append(f"{where}: options must be unique")
                if any(isinstance(o, str) and o.strip().lower() == "none" for o in opts):
                    errs.append(f"{where}: options must not include the reserved word, none")
                d = attrs.get("default")
                if d is not None and (not isinstance(d, int) or isinstance(d, bool) or not 0 <= d < len(opts)):
                    errs.append(f"{where}: default must be an index into options")
            if "multiple" in attrs and not isinstance(attrs["multiple"], bool):
                errs.append(f"{where}: multiple must be true or false")
        if typ == "checkboxes":
            opts = attrs.get("options")
            if not isinstance(opts, list) or not opts:
                errs.append(f"{where}: options must not be empty")
            else:
                for j, o in enumerate(opts):
                    if not isinstance(o, dict) or not _nonempty_str(o.get("label")):
                        errs.append(f"{where}.options[{j}]: label is required")
                    elif set(o) - {"label", "required"} or not isinstance(o.get("required", False), bool):
                        errs.append(f"{where}.options[{j}]: only label and required: true|false are permitted")
    if body and fields == 0:
        errs.append("body must contain at least one non-markdown field")
    return errs


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@unittest.skipIf(yaml is None, "PyYAML not installed")
class IssueFormsInRepoTest(unittest.TestCase):
    def test_every_issue_form_is_valid(self):
        forms = sorted(TEMPLATES.glob("*.yml")) + sorted(TEMPLATES.glob("*.yaml"))
        forms = [f for f in forms if f.name != "config.yml"]
        self.assertTrue(forms, "no issue forms found")
        for f in forms:
            with self.subTest(form=f.name):
                self.assertEqual(validate_form(load(f)), [])

    def test_fingerprint_difference_form_collects_what_triage_needs(self):
        form = load(FINGERPRINT_FORM)
        self.assertEqual(validate_form(form), [])
        by_id = {el["id"]: el for el in form["body"] if "id" in el}
        for needed in ("repro", "clearcote-value", "chrome-value", "chrome-version", "launch-options",
                       "engine-version", "sdk", "sdk-version", "os", "checks"):
            self.assertIn(needed, by_id, f"the form has no {needed!r} field")
        for required in ("repro", "clearcote-value", "chrome-value", "launch-options", "engine-version", "os"):
            self.assertTrue(by_id[required].get("validations", {}).get("required"), f"{required} must be required")
        same_machine = [o for o in by_id["checks"]["attributes"]["options"] if "same machine" in o["label"]]
        self.assertEqual(len(same_machine), 1, "needs the 'compared on the same machine' checkbox")
        self.assertIs(same_machine[0].get("required"), True)

    def test_fingerprint_difference_form_wording_is_neutral(self):
        text = FINGERPRINT_FORM.read_text(encoding="utf-8").lower()
        for word in ("bypass", "evade", "evasion", "undetect", "anti-bot", "antibot", "bot detection", "stealth"):
            self.assertNotIn(word, text)


@unittest.skipIf(yaml is None, "PyYAML not installed")
class ValidatorRejectsBrokenFormsTest(unittest.TestCase):
    """The validator has to fail on the mistakes GitHub rejects, or a green run above proves nothing."""

    def good(self):
        return yaml.safe_load("""
name: Report
description: A report
body:
  - type: markdown
    attributes:
      value: Hello
  - type: input
    id: a
    attributes:
      label: A
    validations:
      required: true
  - type: dropdown
    id: b
    attributes:
      label: B
      options: [x, y]
  - type: checkboxes
    id: c
    attributes:
      label: C
      options:
        - label: ok
          required: true
""")

    def assertRejects(self, form, fragment):
        errs = validate_form(form)
        self.assertTrue(any(fragment in e for e in errs), f"expected {fragment!r} in {errs}")

    def test_good_form_passes(self):
        self.assertEqual(validate_form(self.good()), [])

    def test_missing_required_top_level_keys(self):
        for key in ("name", "description", "body"):
            f = self.good()
            del f[key]
            self.assertRejects(f, f"required top level key {key} is missing")

    def test_unknown_top_level_key(self):
        f = self.good()
        f["about"] = "legacy markdown-template key"
        self.assertRejects(f, "is not a permitted key")

    def test_duplicate_ids(self):
        f = self.good()
        f["body"][2]["id"] = "a"
        self.assertRejects(f, "unique ids")

    def test_duplicate_labels(self):
        f = self.good()
        f["body"][2]["attributes"]["label"] = "A"
        self.assertRejects(f, "unique labels")

    def test_bad_id_characters(self):
        f = self.good()
        f["body"][1]["id"] = "has space"
        self.assertRejects(f, "id can only contain")

    def test_markdown_without_value(self):
        f = self.good()
        f["body"][0]["attributes"] = {}
        self.assertRejects(f, "value is missing")

    def test_unknown_element_type(self):
        f = self.good()
        f["body"][1]["type"] = "text"
        self.assertRejects(f, "not a valid input type")

    def test_field_without_label(self):
        f = self.good()
        del f["body"][1]["attributes"]["label"]
        self.assertRejects(f, "label must not be empty")

    def test_dropdown_reserved_none_and_booleans(self):
        f = self.good()
        f["body"][2]["attributes"]["options"] = ["None", True]
        self.assertRejects(f, "reserved word, none")
        self.assertRejects(f, "booleans")

    def test_dropdown_duplicate_options(self):
        f = self.good()
        f["body"][2]["attributes"]["options"] = ["x", "x"]
        self.assertRejects(f, "options must be unique")

    def test_checkbox_option_without_label(self):
        f = self.good()
        f["body"][3]["attributes"]["options"] = [{"required": True}]
        self.assertRejects(f, "label is required")

    def test_attribute_not_permitted_for_type(self):
        f = self.good()
        f["body"][1]["attributes"]["render"] = "shell"  # render is textarea-only
        self.assertRejects(f, "not permitted for input")

    def test_only_markdown(self):
        f = self.good()
        f["body"] = f["body"][:1]
        self.assertRejects(f, "at least one non-markdown field")


if __name__ == "__main__":
    unittest.main()
