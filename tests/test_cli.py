import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agentpatch.cli import main


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.stderr = io.StringIO()
        self.stdout = io.StringIO()

    def run_cli(self, argv):
        err, out = sys.stderr, sys.stdout
        sys.stderr, sys.stdout = self.stderr, self.stdout
        try:
            code = main(argv)
        finally:
            sys.stderr, sys.stdout = err, out
        return code

    def wfile(self, rel: str, content: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as fh:
            fh.write(content)

    def rfile(self, rel: str) -> str:
        with open(os.path.join(self.root, rel), newline="") as fh:
            return fh.read()

    def wpatch(self, text: str) -> str:
        p = os.path.join(self.root, "p.patch")
        with open(p, "w") as fh:
            fh.write(text)
        return p


GOOD_PATCH = (
    "*** Begin Patch\n"
    "*** Update File: app.py\n"
    "@@ def main():\n"
    "-    print('world')\n"
    "+    print('there')\n"
    "*** End Patch\n"
)


class ApplyExitCodesTests(CliBase):
    def test_success_exit_0_and_writes(self):
        self.wfile("app.py", "def main():\n    print('world')\n")
        code = self.run_cli(["apply", self.wpatch(GOOD_PATCH), "-C", self.root])
        self.assertEqual(code, 0)
        self.assertIn("print('there')", self.rfile("app.py"))

    def test_failure_exit_1_json_report(self):
        self.wfile("app.py", "unrelated\n")
        code = self.run_cli(
            ["apply", self.wpatch(GOOD_PATCH), "-C", self.root, "--json"]
        )
        self.assertEqual(code, 1)
        d = json.loads(self.stdout.getvalue())
        self.assertEqual(d["summary"]["hunks_failed"], 1)
        self.assertEqual(d["files"][0]["status"], "failed")

    def test_dry_run_clean_exit_0_no_write(self):
        self.wfile("app.py", "def main():\n    print('world')\n")
        before = self.rfile("app.py")
        code = self.run_cli(
            ["apply", self.wpatch(GOOD_PATCH), "-C", self.root, "--dry-run"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.rfile("app.py"), before)
        self.assertIn("[ok] update: app.py (DRY RUN)", self.stdout.getvalue())

    def test_missing_root_exit_2(self):
        code = self.run_cli(
            ["apply", self.wpatch(GOOD_PATCH),
             "-C", os.path.join(self.root, "void")]
        )
        self.assertEqual(code, 2)

    def test_missing_patch_file_exit_2(self):
        code = self.run_cli(["apply", os.path.join(self.root, "no.patch"),
                             "-C", self.root])
        self.assertEqual(code, 2)

    def test_garbage_format_exit_2(self):
        code = self.run_cli(["apply", self.wpatch("hello world\n"), "-C", self.root])
        self.assertEqual(code, 2)

    def test_bad_envelope_exit_2(self):
        code = self.run_cli(
            ["apply", self.wpatch("*** Begin Patch\nnothing\n"), "-C", self.root]
        )
        self.assertEqual(code, 2)


class StdinTests(CliBase):
    def test_apply_from_stdin_dash(self):
        self.wfile("app.py", "def main():\n    print('world')\n")
        with mock.patch("sys.stdin", io.StringIO(GOOD_PATCH)):
            code = self.run_cli(["apply", "-", "-C", self.root])
        self.assertEqual(code, 0)
        self.assertIn("there", self.rfile("app.py"))


class ParseCommandTests(CliBase):
    def test_parse_text_output(self):
        code = self.run_cli(["parse", self.wpatch(GOOD_PATCH)])
        self.assertEqual(code, 0)
        self.assertIn("update: app.py (1 hunk(s))", self.stdout.getvalue())

    def test_parse_json_schema(self):
        code = self.run_cli(["parse", self.wpatch(GOOD_PATCH), "--json"])
        self.assertEqual(code, 0)
        d = json.loads(self.stdout.getvalue())
        self.assertEqual(d["format"], "v4a")
        op = d["ops"][0]
        for key in ("path", "kind", "rename_to", "hunks", "add_content"):
            self.assertIn(key, op)
        self.assertEqual(op["hunks"][0]["anchor"], "def main():")

    def test_parse_unrecognized_exit_2(self):
        code = self.run_cli(["parse", self.wpatch("--- a\n+++ b\n"), "--json"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
