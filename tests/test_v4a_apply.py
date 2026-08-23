import json
import os
import tempfile
import unittest

from agentpatch.applier import ApplyError, apply_patch
from agentpatch.v4a import InvalidPath, ParseError, detect_format, parse_patch


def apply_text(text: str, root: str, **kw):
    return apply_patch(parse_patch(text), root, **kw)


class DetectTests(unittest.TestCase):
    def test_detects_envelope(self):
        self.assertEqual(detect_format("*** Begin Patch\n*** End Patch"), "v4a")

    def test_rejects_other(self):
        self.assertIsNone(detect_format("--- a/f\n+++ b/f\n"))
        self.assertIsNone(detect_format(""))


class ParseUpdateTests(unittest.TestCase):
    def test_simple_update_with_anchor(self):
        p = parse_patch(
            "*** Begin Patch\n"
            "*** Update File: src/app.py\n"
            "@@ def main():\n"
            " print('hi')\n"
            "-print('world')\n"
            "+print('there')\n"
            "*** End Patch\n"
        )
        self.assertEqual(len(p.ops), 1)
        op = p.ops[0]
        self.assertEqual((op.kind, op.path), ("update", "src/app.py"))
        h = op.hunks[0]
        self.assertEqual(h.anchor, "def main():")
        self.assertEqual(h.old_lines, ["print('hi')", "print('world')"])
        self.assertEqual(h.new_lines, ["print('hi')", "print('there')"])

    def test_multiple_hunks_per_section(self):
        # The exact shape Codex CLI mishandles (Warp finding); we must parse
        # AND apply it correctly.
        p = parse_patch(
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@ one\n"
            "-a\n"
            "+A\n"
            "@@ two\n"
            "-b\n"
            "+B\n"
            "*** End Patch"
        )
        self.assertEqual([h.anchor for h in p.ops[0].hunks], ["one", "two"])

    def test_bare_anchor_allowed(self):
        p = parse_patch(
            "*** Begin Patch\n*** Update File: f.py\n@@\n-x\n+y\n*** End Patch"
        )
        self.assertIsNone(p.ops[0].hunks[0].anchor)

    def test_move_to_rename(self):
        p = parse_patch(
            "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n*** End Patch"
        )
        self.assertEqual((p.ops[0].rename_to), "new.py")

    def test_add_file_strips_plus(self):
        p = parse_patch(
            "*** Begin Patch\n*** Add File: n.md\n+hello\n+\n+world\n*** End Patch"
        )
        self.assertEqual(p.ops[0].add_content, ["hello", "", "world"])

    def test_delete_file(self):
        p = parse_patch("*** Begin Patch\n*** Delete File: t/x.py\n*** End Patch")
        self.assertEqual(p.ops[0].kind, "delete")

    def test_rejects_absolute_path(self):
        with self.assertRaises(InvalidPath):
            parse_patch("*** Begin Patch\n*** Update File: /etc/passwd\n*** End Patch")

    def test_rejects_traversal(self):
        with self.assertRaises(InvalidPath):
            parse_patch("*** Begin Patch\n*** Delete File: ../secrets\n*** End Patch")

    def test_missing_end_marker_tolerated(self):
        p = parse_patch("*** Begin Patch\n*** Update File: f.py\n@@\n old\n+new\n")
        self.assertEqual(len(p.ops), 1)
        self.assertEqual(p.ops[0].hunks[0].old_lines, ["old"])
        self.assertEqual(p.ops[0].hunks[0].new_lines, ["old", "new"])

    def test_missing_begin_marker(self):
        with self.assertRaises(ParseError):
            parse_patch("just some text\n")

    def test_unprefixed_body_line_rejected(self):
        with self.assertRaises(ParseError):
            parse_patch(
                "*** Begin Patch\n*** Update File: f.py\n@@\nnaked line\n*** End Patch"
            )


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


PATCH_SIMPLE = (
    "*** Begin Patch\n"
    "*** Update File: app.py\n"
    "@@ def main():\n"
    "     print('hi')\n"
    "-    print('world')\n"
    "+    print('there')\n"
    "*** End Patch\n"
)


class ApplyUpdateTests(ApplyBase):
    def test_exact_apply(self):
        self.wfile("app.py", "def main():\n    print('hi')\n    print('world')\n")
        res = apply_text(PATCH_SIMPLE, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.files[0].hunks[0].strategy, "exact")
        self.assertEqual(
            self.rfile("app.py"),
            "def main():\n    print('hi')\n    print('there')\n",
        )

    def test_eol_tolerant_trailing_whitespace(self):
        self.wfile("app.py", "def main():\n    print('hi')  \n    print('world')\t\n")
        res = apply_text(PATCH_SIMPLE, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.files[0].hunks[0].strategy, "eol_tolerant")
        self.assertIn("print('there')", self.rfile("app.py"))

    def test_indent_flex_tabs_vs_spaces(self):
        self.wfile("app.py", "def main():\n\tprint('hi')\n\tprint('world')\n")
        res = apply_text(PATCH_SIMPLE, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.files[0].hunks[0].strategy, "indent_flex")
        self.assertEqual(
            self.rfile("app.py"),
            "def main():\n\tprint('hi')\n\tprint('there')\n",
        )

    def test_indent_flex_reindents_insertion_to_file_depth(self):
        patch = (
            "*** Begin Patch\n"
            "*** Update File: app.py\n"
            "@@ if x:\n"
            "-    pass\n"
            "+    return 1\n"
            "*** End Patch\n"
        )
        self.wfile("app.py", "if x:\n        pass\n")
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        # insertion adopts the file's deeper indentation base (8 spaces)
        self.assertEqual(self.rfile("app.py"), "if x:\n        return 1\n")

    def test_ambiguity_fails_with_count_message(self):
        self.wfile("app.py", "x = 1\ny = 2\nx = 1\n")
        patch = (
            "*** Begin Patch\n*** Update File: app.py\n@@\n x = 1\n*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertFalse(res.ok)
        msg = res.files[0].hunks[0].message or ""
        self.assertIn("ambiguous", msg)
        self.assertIn("2 locations", msg)
        # atomicity: file untouched
        self.assertEqual(self.rfile("app.py"), "x = 1\ny = 2\nx = 1\n")

    def test_no_match_fails_and_leaves_file_untouched(self):
        self.wfile("app.py", "nothing here\n")
        res = apply_text(PATCH_SIMPLE, self.root)
        self.assertFalse(res.ok)
        self.assertEqual(res.files[0].hunks[0].message,
                         "could not find the target lines in this file")
        self.assertEqual(self.rfile("app.py"), "nothing here\n")

    def test_missing_file_fails(self):
        res = apply_text(PATCH_SIMPLE, self.root)
        self.assertFalse(res.ok)
        self.assertIn("does not exist", res.files[0].message)


class MultiHunkTests(ApplyBase):
    def test_two_hunks_both_applied_with_offset_safety(self):
        self.wfile("f.py", "a = 1\nb = 2\nc = 3\nd = 4\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@ a\n"
            "-a = 1\n"
            "+a = 10\n"
            "@@ c\n"
            "-c = 3\n"
            "+c = 30\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.to_dict()["summary"]["hunks_applied"], 2)
        self.assertEqual(self.rfile("f.py"), "a = 10\nb = 2\nc = 30\nd = 4\n")

    def test_overlapping_hunks_both_fail_atomically(self):
        self.wfile("f.py", "l1\nl2\nl3\nl4\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@ l1\n"
            "-l1\n"
            "-l2\n"
            "+L12\n"
            "@@ l2\n"
            "-l2\n"
            "-l3\n"
            "+L23\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertFalse(res.ok)
        for h in res.files[0].hunks:
            self.assertEqual(h.status, "failed")
            self.assertIn("overlap", h.message)
        self.assertEqual(self.rfile("f.py"), "l1\nl2\nl3\nl4\n")

    def test_one_bad_hunk_blocks_whole_file(self):
        self.wfile("f.py", "good = 1\nbad = ?\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@ good\n"
            "-good = 1\n"
            "+good = 2\n"
            "@@ nope\n"
            "-missing line\n"
            "+x\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertFalse(res.ok)
        self.assertEqual(res.to_dict()["summary"]["hunks_failed"], 1)
        self.assertEqual(self.rfile("f.py"), "good = 1\nbad = ?\n")


class FuzzyTests(ApplyBase):
    def test_fuzzy_match_within_threshold(self):
        self.wfile("f.py", "def handle(req):\n    r = request_get(req)\n"
                             "    return respons(r)\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@\n"
            "-    return response(r)\n"
            "+    return respond(r)\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        h = res.files[0].hunks[0]
        self.assertEqual(h.strategy, "fuzzy")
        self.assertGreaterEqual(h.similarity, 0.85)

    def test_below_threshold_rejected(self):
        self.wfile("f.py", "totally different content lines here\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: f.py\n"
            "@@\n"
            "-alpha beta gamma delta epsilon\n"
            "+zeta\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root, threshold=0.99)
        self.assertFalse(res.ok)


class AddDeleteRenameTests(ApplyBase):
    def test_add_creates_parents(self):
        patch = (
            "*** Begin Patch\n*** Add File: deep/dir/new.txt\n+content\n*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile(os.path.join("deep", "dir", "new.txt")),
                         "content\n")

    def test_add_existing_fails(self):
        self.wfile("exists.txt", "x\n")
        patch = "*** Begin Patch\n*** Add File: exists.txt\n+y\n*** End Patch"
        res = apply_text(patch, self.root)
        self.assertFalse(res.ok)
        self.assertEqual(self.rfile("exists.txt"), "x\n")

    def test_delete_removes(self):
        self.wfile("gone.txt", "bye\n")
        res = apply_text(
            "*** Begin Patch\n*** Delete File: gone.txt\n*** End Patch", self.root
        )
        self.assertTrue(res.ok)
        self.assertFalse(os.path.exists(os.path.join(self.root, "gone.txt")))

    def test_delete_missing_fails(self):
        res = apply_text(
            "*** Begin Patch\n*** Delete File: never.txt\n*** End Patch", self.root
        )
        self.assertFalse(res.ok)

    def test_rename_updates_content_and_moves(self):
        self.wfile("old.py", "v = 1\n")
        patch = (
            "*** Begin Patch\n"
            "*** Update File: old.py\n"
            "*** Move to: renamed.py\n"
            "-v = 1\n"
            "+v = 2\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(res.files[0].renamed_to, "renamed.py")
        self.assertFalse(os.path.exists(os.path.join(self.root, "old.py")))
        self.assertEqual(self.rfile("renamed.py"), "v = 2\n")


class EncodingTests(ApplyBase):
    def test_crlf_preserved(self):
        self.wfile("win.py", "def f():\r\n    return 1\r\n")
        patch = (
            "*** Begin Patch\n*** Update File: win.py\n@@\n-    return 1\n"
            "+    return 2\n*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        with open(os.path.join(self.root, "win.py"), newline="") as fh:
            raw = fh.read()
        self.assertEqual(raw, "def f():\r\n    return 2\r\n")

    def test_no_trailing_newline_preserved(self):
        self.wfile("f.txt", "one\ntwo")
        patch = (
            "*** Begin Patch\n*** Update File: f.txt\n@@\n-two\n+TWO\n*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertTrue(res.ok)
        self.assertEqual(self.rfile("f.txt"), "one\nTWO")

    def test_binary_file_reported_not_crash(self):
        with open(os.path.join(self.root, "blob.bin"), "wb") as fh:
            fh.write(b"\xff\xfe\x00\x01")
        patch = (
            "*** Begin Patch\n*** Update File: blob.bin\n@@\n-x\n+y\n*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertFalse(res.ok)
        self.assertIn("UTF-8", res.files[0].message)


class DryRunTests(ApplyBase):
    def test_dry_run_reports_without_writing(self):
        self.wfile("app.py", "def main():\n    print('hi')\n    print('world')\n")
        before = self.rfile("app.py")
        res = apply_text(PATCH_SIMPLE, self.root, dry_run=True)
        self.assertTrue(res.ok)
        d = res.to_dict()
        self.assertTrue(d["dry_run"])
        self.assertEqual(d["summary"]["hunks_applied"], 1)
        self.assertEqual(self.rfile("app.py"), before)

    def test_dry_run_delete(self):
        self.wfile("f.txt", "x\n")
        res = apply_text(
            "*** Begin Patch\n*** Delete File: f.txt\n*** End Patch",
            self.root, dry_run=True,
        )
        self.assertTrue(res.ok)
        self.assertTrue(os.path.exists(os.path.join(self.root, "f.txt")))


class RootSafetyTests(ApplyBase):
    def test_root_must_exist(self):
        with self.assertRaises(ApplyError):
            apply_text(PATCH_SIMPLE, os.path.join(self.root, "nope"))

    def test_symlink_escape_blocked(self):
        outside = tempfile.mkdtemp()
        link = os.path.join(self.root, "link")
        os.symlink(outside, link)
        with open(os.path.join(outside, "victim.txt"), "w") as fh:
            fh.write("data\n")
        patch = (
            "*** Begin Patch\n*** Update File: link/victim.txt\n@@\n-data\n+owned\n"
            "*** End Patch"
        )
        res = apply_text(patch, self.root)
        self.assertFalse(res.ok)


class ResultShapeTests(ApplyBase):
    def test_json_dict_schema(self):
        self.wfile("app.py", "def main():\n    print('hi')\n    print('world')\n")
        d = apply_text(PATCH_SIMPLE, self.root).to_dict()
        self.assertEqual(d["format"], "v4a")
        self.assertIn("summary", d)
        f = d["files"][0]
        for key in ("path", "kind", "status", "renamed_to", "hunks", "message"):
            self.assertIn(key, f)
        h = f["hunks"][0]
        for key in ("index", "status", "strategy", "similarity", "line_start",
                    "message"):
            self.assertIn(key, h)
        self.assertEqual(h["line_start"], 2)

    def test_parse_json_roundtrip(self):
        from agentpatch.cli import _patch_dict

        p = parse_patch(PATCH_SIMPLE)
        s = json.dumps(_patch_dict(p))
        self.assertIn("app.py", s)


if __name__ == "__main__":
    unittest.main()
