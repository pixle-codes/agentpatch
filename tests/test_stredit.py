"""Tests for the stredit (str_replace pair) format: parsing + applying."""

import json
import os
import tempfile
import unittest

from agentpatch import (
    SUBSTR,
    ParseError,
    Patch,
    apply_patch,
    detect,
    parse_patch,
)
from agentpatch import stredit


def j(obj) -> str:
    return json.dumps(obj)


class DetectTests(unittest.TestCase):
    def test_single_object_detected(self):
        self.assertEqual(detect(j({"path": "f.py", "old_str": "a", "new_str": "b"})), stredit.STREDIT)

    def test_array_detected(self):
        self.assertEqual(
            detect(j([{"file_path": "f.py", "old_string": "a", "new_string": "b"}])),
            stredit.STREDIT,
        )

    def test_rfc6902_not_stolen(self):
        self.assertIsNone(detect(j([{"op": "add", "path": "/x", "value": "y"}])))

    def test_empty_array_claimed_for_precise_error(self):
        self.assertEqual(detect(j([])), stredit.STREDIT)

    def test_broken_edit_object_claimed_for_precise_error(self):
        # new_str alone is edit-shaped; parser then reports the missing old_str
        self.assertEqual(detect(j({"path": "f.py", "new_str": "b"})), stredit.STREDIT)

    def test_plain_json_object_not_stolen(self):
        self.assertIsNone(detect(j({"hello": "world"})))

    def test_invalid_json_undetected(self):
        self.assertIsNone(detect("{not json"))

    def test_other_formats_unaffected(self):
        self.assertEqual(detect("*** Begin Patch\n*** End Patch"), "v4a")
        self.assertEqual(detect("--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@"), "udiff")


class ParseTests(unittest.TestCase):
    def test_single_edit(self):
        p = parse_patch(j({"path": "a.py", "old_str": "one\ntwo", "new_str": "ONE"}))
        self.assertIsInstance(p, Patch)
        self.assertEqual(p.format, stredit.STREDIT)
        self.assertEqual(len(p.ops), 1)
        op = p.ops[0]
        self.assertEqual(op.kind, "update")
        self.assertEqual(op.path, "a.py")
        self.assertEqual(op.hunks[0].mode, SUBSTR)
        self.assertEqual(op.hunks[0].old_lines, ["one", "two"])
        self.assertEqual(op.hunks[0].new_lines, ["ONE"])
        self.assertFalse(op.hunks[0].replace_all)

    def test_same_path_groups_into_one_op_in_order(self):
        p = parse_patch(
            j(
                [
                    {"path": "a.py", "old_str": "1", "new_str": "2"},
                    {"path": "b.py", "old_str": "3", "new_str": "4"},
                    {"path": "a.py", "old_str": "5", "new_str": "6"},
                ]
            )
        )
        self.assertEqual([op.path for op in p.ops], ["a.py", "b.py"])
        self.assertEqual(len(p.ops[0].hunks), 2)
        self.assertEqual(p.ops[0].hunks[1].old_lines, ["5"])

    def test_aliases_accepted(self):
        p = parse_patch(j({"file_path": "a.py", "old_string": "x", "new_string": "y"}))
        self.assertEqual(p.ops[0].path, "a.py")

    def test_replace_all_parsed(self):
        p = parse_patch(j({"path": "a.py", "old_str": "x", "replace_all": True}))
        self.assertTrue(p.ops[0].hunks[0].replace_all)

    def test_missing_new_str_means_delete(self):
        p = parse_patch(j({"path": "a.py", "old_str": "x"}))
        self.assertEqual(p.ops[0].hunks[0].new_lines, [""])

    def _err(self, text, frag):
        with self.assertRaises(ParseError) as cm:
            parse_patch(text, fmt=stredit.STREDIT)
        self.assertIn(frag, str(cm.exception))

    def test_error_invalid_json(self):
        self._err("{oops", "invalid JSON")

    def test_dispatched_invalid_json_gives_generic_error(self):
        with self.assertRaises(ParseError) as cm:
            parse_patch("{oops")
        self.assertIn("unrecognized patch format", str(cm.exception))

    def test_error_empty_array(self):
        self._err(j([]), "non-empty array")

    def test_error_non_object_entry(self):
        self._err(j(["nope"]), "JSON edit object")

    def test_error_missing_path(self):
        self._err(j({"old_str": "a"}), "missing 'path'")

    def test_error_missing_old_str(self):
        self._err(j({"path": "a.py", "new_str": "b"}), "missing 'old_str'")

    def test_error_empty_old_str(self):
        self._err(j({"path": "a.py", "old_str": ""}), "non-empty string")

    def test_error_bad_replace_all_type(self):
        self._err(j({"path": "a.py", "old_str": "a", "replace_all": "yes"}), "boolean")

    def test_error_absolute_path(self):
        self._err(j({"path": "/etc/passwd", "old_str": "a"}), "absolute")

    def test_error_traversal_path(self):
        self._err(j({"path": "../esc", "old_str": "a"}), "traversal")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def write(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if isinstance(content, bytes):
            with open(path, "wb") as fh:
                fh.write(content)
        else:
            with open(path, "w", newline="") as fh:
                fh.write(content)
        return path

    def read(self, rel, binary=False):
        mode = "rb" if binary else "r"
        with open(os.path.join(self.root, rel), mode, **({} if binary else {"newline": ""})) as fh:
            return fh.read()

    def apply(self, payload, dry_run=False):
        if not isinstance(payload, str):
            payload = j(payload)
        return apply_patch(parse_patch(payload, fmt=stredit.STREDIT), root=self.root, dry_run=dry_run)

    def test_partial_line_splice(self):
        self.write("f.py", 'color = "red"\nsize = 10\n')
        res = self.apply({"path": "f.py", "old_str": '"red"', "new_str": '"blue"'})
        self.assertTrue(res.ok)
        self.assertEqual(self.read("f.py"), 'color = "blue"\nsize = 10\n')
        h = res.files[0].hunks[0]
        self.assertEqual(h.strategy, SUBSTR)
        self.assertEqual(h.line_start, 1)

    def test_multiline_replacement_reports_line(self):
        self.write("g.py", "a = 1\nb = 2\nc = 3\n")
        res = self.apply({"path": "g.py", "old_str": "b = 2\nc = 3", "new_str": "B"})
        self.assertTrue(res.ok)
        self.assertEqual(res.files[0].hunks[0].line_start, 2)
        self.assertEqual(self.read("g.py"), "a = 1\nB\n")

    def test_delete_substring(self):
        self.write("d.py", "keep\nbye\n")
        res = self.apply({"path": "d.py", "old_str": "\nbye"})
        self.assertTrue(res.ok)
        self.assertEqual(self.read("d.py"), "keep\n")

    def test_search_to_eof_without_trailing_newline(self):
        self.write("e.txt", "head\ntail")
        res = self.apply({"path": "e.txt", "old_str": "tail", "new_str": "TAIL!"})
        self.assertTrue(res.ok)
        self.assertEqual(self.read("e.txt"), "head\nTAIL!")

    def test_zero_occurrences_fails_atomically_with_hint(self):
        self.write("h.py", "hello world\n")
        before = self.read("h.py")
        res = self.apply({"path": "h.py", "old_str": "banana", "new_str": "x"})
        self.assertFalse(res.ok)
        msg = res.files[0].hunks[0].message or ""
        self.assertIn("exact text not found", msg)
        self.assertNotIn("ignoring", msg)  # no near-miss to report
        self.assertEqual(self.read("h.py"), before)

    def test_near_miss_hint_trailing_whitespace(self):
        self.write("t.py", "x = 1\n")
        res = self.apply({"path": "t.py", "old_str": "x = 1 ", "new_str": "x = 2"})
        msg = res.files[0].hunks[0].message or ""
        self.assertIn("ignoring trailing whitespace", msg)
        self.assertIn("line 1", msg)

    def test_near_miss_hint_indentation(self):
        self.write("i.py", "if x:\n    go()\n")
        res = self.apply(
            {"path": "i.py", "old_str": "if x:\n        go()", "new_str": ""}
        )
        msg = res.files[0].hunks[0].message or ""
        self.assertIn("ignoring leading indentation", msg)

    def test_multiple_occurrences_ambiguous_without_flag(self):
        self.write("m.py", "x + x\n")
        res = self.apply({"path": "m.py", "old_str": "x", "new_str": "y"})
        self.assertFalse(res.ok)
        self.assertIn("occurs 2 times", res.files[0].hunks[0].message)
        self.assertIn("replace_all", res.files[0].hunks[0].message)
        self.assertEqual(self.read("m.py"), "x + x\n")

    def test_replace_all_replaces_every_non_overlapping_occurrence(self):
        self.write("ra.py", "aaaa\n")
        res = self.apply({"path": "ra.py", "old_str": "aa", "new_str": "b", "replace_all": True})
        self.assertTrue(res.ok)
        self.assertEqual(self.read("ra.py"), "bb\n")

    def test_replace_all_on_single_occurrence_is_fine(self):
        self.write("r1.py", "solo\n")
        res = self.apply({"path": "r1.py", "old_str": "solo", "new_str": "", "replace_all": True})
        self.assertTrue(res.ok)
        self.assertEqual(self.read("r1.py"), "")

    def test_sequential_edits_see_each_others_results(self):
        self.write("s.py", "val = 1\nname = q\n")
        res = self.apply(
            [
                {"path": "s.py", "old_str": "= q", "new_str": "= val"},
                {"path": "s.py", "old_str": "name = val", "new_str": "name = r"},
            ]
        )
        self.assertTrue(res.ok)
        self.assertEqual(self.read("s.py"), "val = 1\nname = r\n")

    def test_later_hunk_skipped_after_failure_and_file_intact(self):
        self.write("k.py", "aaa\n")
        res = self.apply(
            [
                {"path": "k.py", "old_str": "nope", "new_str": ""},
                {"path": "k.py", "old_str": "a", "new_str": "b"},
            ]
        )
        self.assertFalse(res.ok)
        hs = res.files[0].hunks
        self.assertIn("not found", hs[0].message)
        self.assertIn("skipped", hs[1].message)
        self.assertEqual(self.read("k.py"), "aaa\n")

    def test_crlf_file_stays_crlf(self):
        self.write("c.txt", b"one\r\ntwo\r\n")
        res = self.apply({"path": "c.txt", "old_str": "two", "new_str": "TWO"})
        self.assertTrue(res.ok)
        self.assertEqual(self.read("c.txt", binary=True), b"one\r\nTWO\r\n")

    def test_dry_run_leaves_file_untouched(self):
        self.write("dr.txt", "before\n")
        res = self.apply({"path": "dr.txt", "old_str": "before", "new_str": "after"}, dry_run=True)
        self.assertTrue(res.ok)
        self.assertTrue(res.dry_run)
        self.assertEqual(self.read("dr.txt"), "before\n")

    def test_missing_file_fails(self):
        res = self.apply({"path": "ghost.py", "old_str": "a", "new_str": "b"})
        self.assertFalse(res.ok)
        self.assertEqual(res.files[0].message, "file does not exist")

    def test_symlink_escape_blocked(self):
        outside = os.path.join(tempfile.mkdtemp(), "out.txt")
        with open(outside, "w") as fh:
            fh.write("secret\n")
        link = os.path.join(self.root, "lurk.py")
        os.symlink(outside, link)
        res = self.apply({"path": "lurk.py", "old_str": "secret", "new_str": "x"})
        self.assertFalse(res.ok)
        self.assertIn("escapes", res.files[0].message)
        with open(outside) as fh:
            self.assertEqual(fh.read(), "secret\n")

    def test_multiple_files_one_failure_keeps_other_applied(self):
        self.write("ok.txt", "good\n")
        self.write("bad.txt", "nothing here\n")
        res = self.apply(
            [
                {"path": "ok.txt", "old_str": "good", "new_str": "GREAT"},
                {"path": "bad.txt", "old_str": "absent", "new_str": ""},
            ]
        )
        d = res.to_dict()
        self.assertEqual(d["summary"]["files_applied"], 1)
        self.assertEqual(d["summary"]["files_failed"], 1)
        self.assertEqual(self.read("ok.txt"), "GREAT\n")


if __name__ == "__main__":
    unittest.main()
