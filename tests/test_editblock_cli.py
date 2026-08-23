import json
import os
import unittest

from agentpatch.cli import main

from .test_editblock_apply import ApplyBase


class EditBlockCliTests(ApplyBase):
    def run_cli(self, argv):
        import io
        import sys
        from contextlib import redirect_stderr, redirect_stdout

        err, out = sys.stderr, sys.stdout
        buf_err, buf_out = io.StringIO(), io.StringIO()
        sys.stderr, sys.stdout = buf_err, buf_out
        try:
            code = main(argv)
        finally:
            sys.stderr, sys.stdout = err, out
        return code, buf_out.getvalue(), buf_err.getvalue()

    def wpatch(self, text: str) -> str:
        p = os.path.join(self.root, "p.patch")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    BLOCK = (
        "app.py\n"
        "```python\n"
        "<<<<<<< SEARCH\n"
        "print('old')\n"
        "=======\n"
        "print('new')\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )

    def test_parse_reports_ops(self):
        code, out, _ = self.run_cli(["parse", self.wpatch(self.BLOCK)])
        self.assertEqual(code, 0)
        self.assertIn("update: app.py (1 hunk(s))", out)

    def test_parse_json_format_field(self):
        p = self.wpatch(self.BLOCK)
        code, out, _ = self.run_cli(["parse", p, "--json"])
        data = json.loads(out)
        self.assertEqual(data["format"], "editblock")
        self.assertEqual(data["ops"][0]["path"], "app.py")

    def test_apply_end_to_end_with_json_diagnostics(self):
        self.wfile("app.py", "print('old')\n")
        code, out, _ = self.run_cli(
            ["apply", self.wpatch(self.BLOCK), "-C", self.root, "--json"]
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["format"], "editblock")
        self.assertEqual(data["files"][0]["status"], "ok")
        self.assertEqual(data["files"][0]["hunks"][0]["strategy"], "exact")
        self.assertEqual(self.rfile("app.py"), "print('new')\n")

    def test_auto_detection_picks_editblock_in_apply(self):
        self.wfile("app.py", "print('old')\n")
        code, _, _ = self.run_cli(["apply", self.wpatch(self.BLOCK), "-C", self.root])
        self.assertEqual(code, 0)

    def test_format_override_forces_wrong_parser_to_exit_2(self):
        v4a_text = "*** Begin Patch\n*** Update File: app.py\n*** End Patch\n"
        code, _, err = self.run_cli(
            ["apply", self.wpatch(v4a_text), "-C", self.root, "--format", "editblock"]
        )
        self.assertEqual(code, 2)
        self.assertIn("no SEARCH/REPLACE blocks found", err)

    def test_unrecognized_format_exits_2(self):
        code, _, err = self.run_cli(["apply", self.wpatch("hello world\n"), "-C", self.root])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized patch format", err)

    def test_failed_hunk_exit_1(self):
        self.wfile("app.py", "something else\n")
        code, out, _ = self.run_cli(["apply", self.wpatch(self.BLOCK), "-C", self.root])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
