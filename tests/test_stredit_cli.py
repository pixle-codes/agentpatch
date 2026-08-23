"""CLI-level tests for the stredit format (exit codes, --json, --format)."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from agentpatch import cli


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


class StreditCliTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.patch = os.path.join(tempfile.mkdtemp(), "p.json")
        self.target = os.path.join(self.root, "f.py")
        with open(self.target, "w") as fh:
            fh.write("value = 1\n")

    def write_patch(self, obj):
        with open(self.patch, "w") as fh:
            json.dump(obj, fh)

    def test_apply_ok_exit0(self):
        self.write_patch({"path": "f.py", "old_str": "= 1", "new_str": "= 2"})
        code, out, _ = run(["apply", self.patch, "-C", self.root])
        self.assertEqual(code, 0)
        self.assertIn("via substr", out)
        with open(self.target) as fh:
            self.assertEqual(fh.read(), "value = 2\n")

    def test_apply_json_report(self):
        self.write_patch({"path": "f.py", "old_str": "= 1", "new_str": "= 2"})
        code, out, _ = run(["apply", self.patch, "-C", self.root, "--json"])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["format"], "stredit")
        h = report["files"][0]["hunks"][0]
        self.assertEqual(h["strategy"], "substr")
        self.assertEqual(h["line_start"], 1)

    def test_apply_failure_exit1_file_untouched(self):
        self.write_patch({"path": "f.py", "old_str": "absent", "new_str": ""})
        code, _, _ = run(["apply", self.patch, "-C", self.root])
        self.assertEqual(code, 1)
        with open(self.target) as fh:
            self.assertEqual(fh.read(), "value = 1\n")

    def test_apply_undetectable_exit2_without_forcing(self):
        with open(self.patch, "w") as fh:
            fh.write("{not json at all}")
        code, _, err = run(["apply", self.patch, "-C", self.root])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized patch format", err)

    def test_apply_forced_format_surfaces_json_error_as_exit2(self):
        with open(self.patch, "w") as fh:
            fh.write("{oops")
        code, _, err = run(
            ["apply", self.patch, "-C", self.root, "--format", "stredit"]
        )
        self.assertEqual(code, 2)
        self.assertIn("invalid JSON", err)

    def test_parse_json_shows_mode_and_replace_all(self):
        self.write_patch([{"path": "f.py", "old_str": "a", "new_str": "b",
                           "replace_all": True}])
        code, out, _ = run(["parse", self.patch, "--json"])
        self.assertEqual(code, 0)
        hunk = json.loads(out)["ops"][0]["hunks"][0]
        self.assertEqual(hunk["mode"], "substr")
        self.assertTrue(hunk["replace_all"])

    def test_version_bumped(self):
        code, out, _ = run(["--version"])
        self.assertIn("agentpatch 0.3.0", out)


if __name__ == "__main__":
    unittest.main()
