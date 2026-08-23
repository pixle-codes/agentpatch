import contextlib
import io
import json
import os
import tempfile
import unittest

from agentpatch.applier import apply_patch
from agentpatch.editblock import ParseError
from agentpatch.formats import UDIFF, detect
from agentpatch.matcher import locate
from agentpatch.udiff import detect_format, parse_patch
from agentpatch.v4a import ADD, DELETE, UPDATE

GIT_DIFF = """\
diff --git a/app.py b/app.py
index 3e7a1b2..9c0f4d5 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 def main():
-    print("hello")
+    print("world")
     return 0
"""


class DetectTests(unittest.TestCase):
    def test_detects_git_diff(self):
        self.assertEqual(detect_format(GIT_DIFF), "udiff")
        self.assertEqual(detect(GIT_DIFF), UDIFF)

    def test_detects_plain_header(self):
        text = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(detect_format(text), "udiff")

    def test_rejects_prose(self):
        self.assertIsNone(detect_format("just some prose\n"))
        self.assertIsNone(detect_format(""))

    def test_editblock_still_wins_when_checked_first(self):
        # formats.detect tries v4a then editblock before udiff, so an
        # editblock payload containing '--- ' lines is not stolen by us.
        text = (
            "f.py\n```python\n<<<<<<< SEARCH\n--- x\n=======\n--- y\n"
            ">>>>>>> REPLACE\n```\n"
        )
        self.assertEqual(detect(text), "editblock")


class ParseUpdateTests(unittest.TestCase):
    def test_basic_update(self):
        p = parse_patch(GIT_DIFF)
        self.assertEqual(p.format, "udiff")
        self.assertEqual(len(p.ops), 1)
        op = p.ops[0]
        self.assertEqual((op.kind, op.path), (UPDATE, "app.py"))
        h = op.hunks[0]
        self.assertEqual(h.old_lines, ["def main():", '    print("hello")', "    return 0"])
        self.assertEqual(h.new_lines, ["def main():", '    print("world")', "    return 0"])
        self.assertEqual(h.line_hint, 0)

    def test_prefixes_and_timestamps_stripped(self):
        p = parse_patch(
            "--- a/src/x.py\t2026-08-23 10:00:00.000000000 +0000\n"
            "+++ b/src/x.py\t2026-08-23 10:00:01.000000000 +0000\n"
            "@@ -2,2 +2,3 @@\n"
            " a\n"
            "-b\n"
            "+B\n"
            "+c\n"
        )
        op = p.ops[0]
        self.assertEqual(op.path, "src/x.py")
        self.assertEqual(op.hunks[0].old_lines, ["a", "b"])
        self.assertEqual(op.hunks[0].new_lines, ["a", "B", "c"])
        self.assertEqual(op.hunks[0].line_hint, 1)

    def test_counts_optional_default_one(self):
        p = parse_patch("--- f.py\n+++ f.py\n@@ -3 +3 @@\n-old\n+new\n")
        h = p.ops[0].hunks[0]
        self.assertEqual((h.old_lines, h.new_lines), (["old"], ["new"]))
        self.assertEqual(h.line_hint, 2)

    def test_zero_context_pure_insertion(self):
        # position hint = where the new lines begin (new_start - 1)
        p = parse_patch("--- f.py\n+++ f.py\n@@ -5,0 +6,2 @@\n+x\n+y\n")
        h = p.ops[0].hunks[0]
        self.assertEqual((h.old_lines, h.new_lines, h.line_hint), ([], ["x", "y"], 5))

    def test_removed_line_starting_with_dashes_is_not_a_header(self):
        # Count-driven consumption: a '-' removal whose CONTENT begins with
        # '--' renders as '--- ...' and must not end the hunk or section.
        text = (
            "--- f.py\n+++ f.py\n@@ -1,4 +1,4 @@\n"
            " c1\n c2\n-keep\n--- looks like a header\n+KEEP\n+added\n"
            "--- g.py\n+++ g.py\n@@ -1,1 +1,1 @@\n-x\n+X\n"
        )
        p = parse_patch(text)
        self.assertEqual([op.path for op in p.ops], ["f.py", "g.py"])
        self.assertEqual(
            p.ops[0].hunks[0].old_lines,
            ["c1", "c2", "keep", "-- looks like a header"],
        )
        self.assertEqual(
            p.ops[0].hunks[0].new_lines,
            ["c1", "c2", "KEEP", "added"],
        )

    def test_no_newline_marker_skipped(self):
        text = (
            "--- f.py\n+++ f.py\n@@ -1,1 +1,1 @@\n-old\n"
            "\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
        )
        h = parse_patch(text).ops[0].hunks[0]
        self.assertEqual((h.old_lines, h.new_lines), (["old"], ["new"]))

    def test_multiple_hunks_accumulate_context_counts(self):
        p = parse_patch(
            "--- f.py\n+++ f.py\n@@ -1,2 +1,2 @@\n a\n-b\n+B\n@@ -10,1 +10,1 @@\n-z\n+Z\n"
        )
        hunks = p.ops[0].hunks
        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0].old_lines, ["a", "b"])
        self.assertEqual(hunks[0].line_hint, 0)
        self.assertEqual(hunks[1].old_lines, ["z"])

    def test_bare_empty_context_line_allowed(self):
        # mailers/editors strip the trailing space off empty context lines
        p = parse_patch("--- f.py\n+++ f.py\n@@ -1,3 +1,3 @@\n a\n\n c\n")
        h = p.ops[0].hunks[0]
        self.assertEqual(h.old_lines, ["a", "", "c"])


class ParseAddDeleteTests(unittest.TestCase):
    def test_new_file(self):
        p = parse_patch(
            "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+import os\n+print(os)\n"
        )
        op = p.ops[0]
        self.assertEqual((op.kind, op.path), (ADD, "new.py"))
        self.assertEqual(op.add_content, ["import os", "print(os)"])

    def test_deleted_file(self):
        p = parse_patch("--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n")
        op = p.ops[0]
        self.assertEqual((op.kind, op.path), (DELETE, "old.py"))


class ParseErrorTests(unittest.TestCase):
    def test_headers_without_hunks_raise(self):
        with self.assertRaisesRegex(ParseError, "no hunk bodies"):
            parse_patch("--- a\n+++ b\n")

    def test_missing_plus_header(self):
        with self.assertRaisesRegex(ParseError, "lacks a '\\+\\+\\+' header"):
            parse_patch("--- a/f.py\n@@ -1 +1 @@\n-x\n")

    def test_both_devnull(self):
        with self.assertRaisesRegex(ParseError, "/dev/null on both sides"):
            parse_patch("--- /dev/null\n+++ /dev/null\n@@ -0,0 +0,0 @@")

    def test_truncated_hunk(self):
        with self.assertRaisesRegex(ParseError, "truncated hunk"):
            parse_patch("--- f.py\n+++ f.py\n@@ -1,3 +1,3 @@\n a\n-b\n")

    def test_bad_body_prefix(self):
        with self.assertRaisesRegex(ParseError, "prefix"):
            parse_patch("--- f.py\n+++ f.py\n@@ -1,1 +1,1 @@\nJUNK\n")

    def test_empty_input(self):
        with self.assertRaisesRegex(ParseError, "expected '--- ' file header"):
            parse_patch("hello world\n")

    def test_whitespace_only_input(self):
        with self.assertRaisesRegex(ParseError, "file sections"):
            parse_patch("\n\n")


class MatcherHintTests(unittest.TestCase):
    LINES = ["dup", "mid1", "dup", "mid2", "dup"]

    def test_ambiguous_without_hint(self):
        self.assertIsNone(locate(self.LINES, ["dup"], ["NEW"]))

    def test_nearest_hint_wins(self):
        m = locate(self.LINES, ["dup"], ["NEW"], hint=3)
        self.assertEqual(m.line_start, 2)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def wfile(self, rel: str, content: str) -> str:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)
        return path

    def rfile(self, rel: str) -> str:
        with open(os.path.join(self.root, rel)) as fh:
            return fh.read()

    def test_end_to_end_git_diff(self):
        self.wfile("app.py", 'def main():\n    print("hello")\n    return 0\n')
        res = apply_patch(parse_patch(GIT_DIFF), self.root)
        self.assertTrue(res.ok)
        self.assertEqual(
            self.rfile("app.py"), 'def main():\n    print("world")\n    return 0\n'
        )

    def test_pure_insertion_placed_by_position(self):
        self.wfile("f.py", "one\ntwo\nthree\n")
        p = parse_patch("--- f.py\n+++ f.py\n@@ -1,0 +2,1 @@\n+zero-five\n")
        res = apply_patch(p, self.root)
        self.assertTrue(res.ok)
        hr = res.files[0].hunks[0]
        self.assertEqual(hr.strategy, "position")
        self.assertEqual(hr.similarity, 1.0)
        self.assertEqual(self.rfile("f.py"), "one\nzero-five\ntwo\nthree\n")

    def test_insertion_clamped_to_eof(self):
        self.wfile("f.py", "one\n")
        p = parse_patch("--- f.py\n+++ f.py\n@@ -99,0 +100,1 @@\n+tail\n")
        res = apply_patch(p, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("f.py"), "one\ntail\n")

    def test_new_file_end_to_end(self):
        p = parse_patch(
            "--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1,1 @@\n+print('hi')\n"
        )
        res = apply_patch(p, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("pkg/new.py"), "print('hi')\n")


class CliTests(unittest.TestCase):
    def run_cli(self, args):
        from agentpatch import cli

        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(args)
        except SystemExit as e:
            code = e.code
        return code or 0, out, err

    def test_parse_json_reports_udiff(self):
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
            fh.write(GIT_DIFF)
            name = fh.name
        try:
            code, out, _ = self.run_cli(["parse", name, "--json"])
        finally:
            os.unlink(name)
        self.assertEqual(code, 0)
        d = json.loads(out.getvalue())
        self.assertEqual(d["format"], "udiff")


if __name__ == "__main__":
    unittest.main()
