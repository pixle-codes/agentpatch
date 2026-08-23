import json
import os
import tempfile
import unittest

from agentpatch.applier import apply_patch
from agentpatch.editblock import parse_patch


class ApplyBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)

    def wfile(self, rel: str, content: str) -> None:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="") as fh:
            fh.write(content)

    def rfile(self, rel: str) -> str:
        with open(os.path.join(self.root, rel), newline="") as fh:
            return fh.read()

    def apply(self, text: str, **kw):
        return apply_patch(parse_patch(text), self.root, **kw)


B = (
    "{path}\n"
    "```python\n"
    "<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"
    "```\n"
)


def block(path: str, search: str, replace: str) -> str:
    return B.format(path=path, search=search, replace=replace)


class EditBlockApplyTests(ApplyBase):
    def test_basic_update(self):
        self.wfile("app.py", "def main():\n    print('old')\n")
        text = block(
            "app.py",
            "def main():\n    print('old')\n",
            "def main():\n    print('new')\n",
        )
        res = self.apply(text)
        self.assertTrue(res.ok)
        self.assertEqual(
            self.rfile("app.py"), "def main():\n    print('new')\n"
        )
        h = res.files[0].hunks[0]
        self.assertEqual(h.status, "applied")
        self.assertEqual(h.strategy, "exact")

    def test_unindented_search_reindents_insertion(self):
        # model searched without leading indentation; cascade escalates to
        # indent_flex and must carry the replacement to the file's base
        self.wfile("app.py", "def main():\n    print('old')\n")
        res = self.apply(block("app.py", "print('old')\n", "print('new')\n"))
        h = res.files[0].hunks[0]
        self.assertTrue(res.ok)
        self.assertEqual(h.strategy, "indent_flex")
        self.assertEqual(self.rfile("app.py"), "def main():\n    print('new')\n")

    def test_eol_tolerant_strategy_used(self):
        self.wfile("app.py", "x = 1   \n")
        res = self.apply(block("app.py", "x = 1\n", "x = 2\n"))
        self.assertTrue(res.ok)
        self.assertEqual(res.files[0].hunks[0].strategy, "eol_tolerant")

    def test_indent_flex_reindents_insertion(self):
        # whole hunk written two spaces deeper than the file's real base
        self.wfile("app.py", "if a:\n    if b:\n        old()\n")
        text = block(
            "app.py",
            "      if b:\n          old()\n",
            "      if b:\n          old()\n          new()\n",
        )
        res = self.apply(text)
        self.assertTrue(res.ok)
        h = res.files[0].hunks[0]
        self.assertEqual(h.strategy, "indent_flex")
        self.assertEqual(
            self.rfile("app.py"), "if a:\n    if b:\n        old()\n        new()\n"
        )

    def test_add_creates_file_fails_if_exists(self):
        res = self.apply(block("brand/new.txt", "", "hello\n"))
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("brand/new.txt"), "hello\n")
        res2 = self.apply(block("brand/new.txt", "", "other\n"))
        self.assertFalse(res2.ok)
        self.assertIn("already exists", res2.files[0].message)

    def test_empty_replace_deletes_matched_lines(self):
        self.wfile("a.py", "keep\nstale1\nstale2\nkeep2\n")
        res = self.apply(block("a.py", "stale1\nstale2\n", ""))
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("a.py"), "keep\nkeep2\n")

    def test_multi_hunk_same_file_atomic_on_failure(self):
        self.wfile("a.py", "one\ntwo\nthree\n")
        text = block("a.py", "one\n", "ONE\n") + block("a.py", "missing\n", "X\n")
        res = self.apply(text)
        self.assertFalse(res.ok)
        self.assertEqual(len(res.files[0].hunks), 2)
        self.assertEqual(self.rfile("a.py"), "one\ntwo\nthree\n")

    def test_overlap_between_blocks_rejected(self):
        self.wfile("a.py", "ctx\ntarget\nctx2\n")
        text = block("a.py", "ctx\ntarget\n", "A\n") + block("a.py", "target\nctx2\n", "B\n")
        res = self.apply(text)
        self.assertFalse(res.ok)

    def test_crlf_file_preserved(self):
        self.wfile("win.py", "alpha\r\nbeta\r\n")
        res = self.apply(block("win.py", "beta\r\n", "BETA\r\n"))
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("win.py"), "alpha\r\nBETA\r\n")


class EllipsisApplyTests(ApplyBase):
    def test_ellipsis_preserves_middle(self):
        self.wfile("a.py", "start\njunk1\njunk2\nend\n")
        res = self.apply(block("a.py", "start\n...\nend\n", "start\nKEPT-MARKER\n...\nend-x\n"))
        self.assertTrue(res.ok)
        self.assertEqual(
            self.rfile("a.py"), "start\nKEPT-MARKER\njunk1\njunk2\nend-x\n"
        )
        self.assertEqual(res.files[0].hunks[0].strategy, "ellipsis")

    def test_ellipsis_without_marker_in_replace_drops_middle(self):
        self.wfile("a.py", "head\nA\nB\nfoot\n")
        res = self.apply(block("a.py", "head\n...\nfoot\n", "head\nZ\nfoot\n"))
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("a.py"), "head\nZ\nfoot\n")

    def test_ellipsis_leading_and_trailing_segments(self):
        self.wfile("a.py", "top\nmid\nbottom\n")
        res = self.apply(block("a.py", "top\n...\nbottom\n", "TOP\n...\nBOTTOM\n"))
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("a.py"), "TOP\nmid\nBOTTOM\n")

    def test_ellipsis_ambiguous_fails(self):
        self.wfile("a.py", "x\nm1\ny\nm2\ny\n")
        res = self.apply(block("a.py", "x\n...\ny\n", "q\n"))
        self.assertFalse(res.ok)
        self.assertIn("'...'", res.files[0].hunks[0].message)

    def test_ellipsis_unmatched_fails(self):
        self.wfile("a.py", "nothing here\n")
        res = self.apply(block("a.py", "x\n...\ny\n", "q\n"))
        self.assertFalse(res.ok)

    def test_ellipsis_span_counts_for_overlap(self):
        self.wfile("a.py", "s\n1\n2\ne\nT\n")
        text = block("a.py", "s\n...\ne\n", "S\n") + block("a.py", "e\nT\n", "ET\n")
        res = self.apply(text)
        self.assertFalse(res.ok, "second block touches lines consumed by the first")


if __name__ == "__main__":
    unittest.main()
