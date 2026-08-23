import unittest

from agentpatch.editblock import ParseError, detect_format, parse_patch
from agentpatch.formats import detect
from agentpatch.v4a import ADD, UPDATE


def block(path_line: str | None, search: str, replace: str, fenced=True) -> str:
    out = ""
    if path_line:
        out += path_line + "\n"
    if fenced:
        out += "```python\n"
    out += f"<<<<<<< SEARCH\n{search}=======\n{replace}>>>>>>> REPLACE\n"
    if fenced:
        out += "```\n"
    return out


class DetectTests(unittest.TestCase):
    def test_detect_editblock(self):
        self.assertEqual(detect_format(block("a.py", "x\n", "y\n")), "editblock")
        self.assertEqual(detect(block("a.py", "x\n", "y\n")), "editblock")

    def test_marker_length_tolerance(self):
        for n in range(5, 10):
            text = f"{'<' * n} SEARCH\nx\n======\ny\n{'>' * n} REPLACE\na.py\n"
            self.assertEqual(detect_format(text), "editblock", f"n={n}")
        self.assertIsNone(detect_format("<<<< SEARCH\nx\n"))       # too few
        self.assertIsNone(detect_format("<<<<<<<<<< SEARCH\nx\n"))  # too many

    def test_merge_conflict_markers_are_not_blocks(self):
        self.assertIsNone(detect_format("<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> other\n"))

    def test_v4a_wins_over_editblock(self):
        text = "*** Begin Patch\n*** Update File: a.py\n<<<<<<< SEARCH\n-x\n=======\n+y\n>>>>>>> REPLACE\n*** End Patch\n"
        self.assertEqual(detect(text), "v4a")

    def test_unknown(self):
        self.assertIsNone(detect("just some prose\n"))


class ParseTests(unittest.TestCase):
    def test_basic_fenced_block(self):
        p = parse_patch(block("src/app.py", "def old():\n    pass\n", "def new():\n    pass\n"))
        self.assertEqual(p.format, "editblock")
        self.assertEqual(len(p.ops), 1)
        op = p.ops[0]
        self.assertEqual(op.kind, UPDATE)
        self.assertEqual(op.path, "src/app.py")
        self.assertEqual(op.hunks[0].old_lines, ["def old():", "    pass"])
        self.assertEqual(op.hunks[0].new_lines, ["def new():", "    pass"])

    def test_fenceless_cline_style(self):
        # markers without any filename line are a parse error...
        with self.assertRaises(ParseError):
            parse_patch("<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        # ...but a filename directly above the markers is enough
        p2 = parse_patch("app.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
        self.assertEqual(p2.ops[0].path, "app.py")
        self.assertEqual(p2.ops[0].hunks[0].old_lines, ["old"])

    def test_filename_inside_fence(self):
        text = "```ts\nsrc/index.ts\n<<<<<<< SEARCH\nlet a = 1;\n=======\nlet a = 2;\n>>>>>>> REPLACE\n```\n"
        p = parse_patch(text)
        self.assertEqual(p.ops[0].path, "src/index.ts")

    def test_filename_decorations_stripped(self):
        for decorated in (
            "**src/app.py**",
            "`src/app.py`",
            "## src/app.py",
            "- src/app.py:",
            "File: `src/app.py`",
            "**File:** src/app.py",
        ):
            with self.subTest(decorated=decorated):
                p = parse_patch(block(decorated, "x\n", "y\n"))
                self.assertEqual(p.ops[0].path, "src/app.py")

    def test_consecutive_blocks_same_file_merged(self):
        text = (
            block("a.py", "one\n", "1\n")
            + block("a.py", "two\n", "2\n")
        )
        p = parse_patch(text)
        self.assertEqual(len(p.ops), 1)
        self.assertEqual(len(p.ops[0].hunks), 2)

    def test_multiple_files(self):
        text = block("a.py", "x\n", "y\n") + block("b.py", "p\n", "q\n")
        p = parse_patch(text)
        self.assertEqual([o.path for o in p.ops], ["a.py", "b.py"])

    def test_empty_search_means_add(self):
        p = parse_patch(block("new.txt", "", "hello\nworld\n"))
        self.assertEqual(p.ops[0].kind, ADD)
        self.assertEqual(p.ops[0].add_content, ["hello", "world"])

    def test_whitespace_only_search_means_add(self):
        p = parse_patch(block("new.txt", "\n", "hello\n"))
        self.assertEqual(p.ops[0].kind, ADD)

    def test_empty_replace_deletes_lines(self):
        p = parse_patch(block("a.py", "gone\n", ""))
        h = p.ops[0].hunks[0]
        self.assertEqual(h.old_lines, ["gone"])
        self.assertEqual(h.new_lines, [])

    def test_both_sides_empty_is_error(self):
        with self.assertRaises(ParseError):
            parse_patch(block("a.py", "", ""))

    def test_missing_divider(self):
        with self.assertRaises(ParseError):
            parse_patch("a.py\n<<<<<<< SEARCH\nx\n>>>>>>> REPLACE\n")

    def test_missing_replace(self):
        with self.assertRaises(ParseError):
            parse_patch("a.py\n<<<<<<< SEARCH\nx\n=======\ny\n")

    def test_new_search_before_divider(self):
        with self.assertRaises(ParseError):
            parse_patch("a.py\n<<<<<<< SEARCH\nx\n<<<<<<< SEARCH\n=======\ny\n>>>>>>> REPLACE\n")

    def test_missing_filename(self):
        with self.assertRaises(ParseError):
            parse_patch(block(None, "x\n", "y\n"))

    def test_trailing_annotations_on_markers(self):
        text = (
            "a.py\n"
            "<<<<<<< SEARCH (exact match)\n"
            "x\n"
            "=======\n"
            "y\n"
            ">>>>>>> REPLACE\n"
        )
        p = parse_patch(text)
        self.assertEqual(p.ops[0].hunks[0].old_lines, ["x"])

    def test_absolute_path_rejected(self):
        with self.assertRaises(ParseError):
            parse_patch(block("/etc/passwd", "x\n", "y\n"))

    def test_prose_above_block_skipped_to_filename(self):
        text = (
            "Here is how we fix the bug:\n\n"
            "src/app.py\n"
            "```python\n"
            "<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"
            "```\n"
        )
        p = parse_patch(text)
        self.assertEqual(p.ops[0].path, "src/app.py")

    def test_body_preserved_verbatim(self):
        body_old = "  indented  \n\ttabbed\n"
        p = parse_patch(block("a.py", body_old, "back\n"))
        self.assertEqual(p.ops[0].hunks[0].old_lines, ["  indented  ", "\ttabbed"])


if __name__ == "__main__":
    unittest.main()
