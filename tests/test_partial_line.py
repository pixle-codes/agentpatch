"""Partial-line fallback cascade level (aider #4716) + diagnostics.

Regression corpus for real-world failure shapes mined from public
issues (see tests/corpus/ for the raw fixtures):

- aider #4716: models summarize long prose lines; SEARCH text that is a
  verbatim fragment of a longer line must still apply when unique.
- openai/codex #26297 + #35361: truncated V4A envelopes missing
  "*** End Patch" used to hard-fail and sent agents into repair loops.
"""

import os
import tempfile
import unittest

from agentpatch.applier import apply_patch
from agentpatch.editblock import parse_patch as parse_eb
from agentpatch.matcher import MIN_PARTIAL_LINE, _partial_line_locate, locate
from agentpatch.v4a import parse_patch


SENTENCE = (
    "All popular browsers support defining custom keywords so that what "
    "you type in the address bar can take you where you are going even faster."
)
PARAGRAPH_LINE = (
    "When we surveyed power users about their browsing habits, the picture "
    "was remarkably consistent. " + SENTENCE + " Every major engine ships "
    "its own flavor of keyword shortcuts these days, and the feature has "
    "quietly become table stakes for anyone who lives in the keyboard."
)


def eb_patch(body: str):
    return parse_eb("post.md\n<<<<<<< SEARCH\n" + body + "\n>>>>>>> REPLACE")


class PartialLineUnitTests(unittest.TestCase):
    def test_fragment_inside_longer_line_matches(self):
        lines = [PARAGRAPH_LINE, "tail"]
        old = [SENTENCE]
        new = ["Replaced sentence entirely."]
        m = locate(lines, old, new)
        self.assertIsNotNone(m)
        self.assertEqual(m.strategy, "partial_line")
        self.assertEqual(m.line_start, 0)
        self.assertEqual(
            m.replacement,
            [
                PARAGRAPH_LINE.replace(SENTENCE, "Replaced sentence entirely."),
            ],
        )

    def test_short_fragment_rejected_by_floor(self):
        lines = [PARAGRAPH_LINE, "tail"]
        m = _partial_line_locate(lines, ["even faster."], ["slower"])
        self.assertIsNone(m)

    def test_ambiguous_fragment_rejected(self):
        twice = [PARAGRAPH_LINE, PARAGRAPH_LINE + " again", "tail"]
        self.assertIsNone(_partial_line_locate(twice, [SENTENCE], ["x"]))

    def test_multiline_span_preserves_boundaries(self):
        long1 = "word " * 60 + "and here the first section reaches its natural END-OF-ONE marker"
        long2 = "START-TWO follows immediately " + "second " * 60 + "fin."
        lines = [long1, long2, "zz"]
        old = [
            "reaches its natural END-OF-ONE marker",
            "START-TWO follows immediately",
        ]
        new = ["ends the first section", "opens the second section"]
        m = locate(lines, old, new)
        self.assertIsNotNone(m)
        self.assertEqual(m.strategy, "partial_line")
        self.assertEqual(m.line_start, 0)
        self.assertEqual(m.span_len, 2)
        self.assertEqual(
            m.replacement,
            [
                "word " * 60 + "and here the first section ends the first section",
                "opens the second section " + "second " * 60 + "fin.",
            ],
        )

    def test_fuzzy_wins_over_partial_when_close(self):
        full_line = SENTENCE + ", but slightly longer overall"
        old = [full_line[:-12]]
        new = ["replacement"]
        lines = [full_line, "tail"]
        m = locate(lines, old, new)
        self.assertEqual(m.strategy, "fuzzy")


class PartialLineApplyTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, "post.md"), "w") as fh:
            fh.write(PARAGRAPH_LINE + "\n")

    def test_apply_via_editblock(self):
        patch = eb_patch(SENTENCE + "\n=======\nCustom keywords fixed here.")
        r = apply_patch(patch, self.d)
        self.assertTrue(r.ok)
        h = r.files[0].hunks[0]
        self.assertEqual(h.strategy, "partial_line")
        self.assertEqual(
            h.line_start, 1
        )
        with open(os.path.join(self.d, "post.md")) as fh:
            self.assertEqual(
                fh.read(),
                PARAGRAPH_LINE.replace(SENTENCE, "Custom keywords fixed here.") + "\n",
            )

    def test_apply_via_v4a(self):
        patch = parse_patch(
            f"*** Begin Patch\n*** Update File: post.md\n@@\n {SENTENCE}\n"
            "+A brand-new opening line.\n*** End Patch\n"
        )
        r = apply_patch(patch, self.d)
        self.assertTrue(r.ok)
        self.assertEqual(r.files[0].hunks[0].strategy, "partial_line")

    def test_dry_run_reports_strategy_without_write(self):
        patch = eb_patch(SENTENCE + "\n=======\nX.")
        before = open(os.path.join(self.d, "post.md")).read()
        r = apply_patch(patch, self.d, dry_run=True)
        self.assertTrue(r.ok)
        self.assertEqual(open(os.path.join(self.d, "post.md")).read(), before)

    def test_two_occurrences_fail_with_hint(self):
        with open(os.path.join(self.d, "post.md"), "a") as fh:
            fh.write(
                "In a follow-up survey conducted several months later we asked "
                "the very same panel of power users to revisit these questions "
                "and, unsurprisingly perhaps, " + SENTENCE + " remained the single "
                "most-agreed-with statement in the entire questionnaire.\n"
            )
        patch = eb_patch(SENTENCE + "\n=======\nX.")
        r = apply_patch(patch, self.d)
        self.assertFalse(r.ok)
        msg = r.files[0].hunks[0].message or ""
        self.assertIn("partial-line fragment", msg)
        self.assertIn("ambiguous", msg)


class TruncatedV4ATests(unittest.TestCase):
    def test_missing_end_marker_parses_and_applies(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "app.py"), "w") as fh:
            fh.write('def greet(name):\n    print("hi")\n')
        patch = parse_patch(
            '*** Begin Patch\n*** Update File: app.py\n@@\n-def greet(name):\n'
            '-    print("hi")\n+def greet(name):\n+    print(f"hello {name}")\n'
        )
        r = apply_patch(patch, d)
        self.assertTrue(r.ok)
        with open(os.path.join(d, "app.py")) as fh:
            self.assertIn('print(f"hello {name}")', fh.read())

    def test_truncated_mid_hunk_still_fails_visibly(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "app.py"), "w") as fh:
            fh.write("a\nb\nc\n")
        patch = parse_patch(
            "*** Begin Patch\n*** Update File: app.py\n@@\n-a\n-zombie line\n+b\n"
        )
        r = apply_patch(patch, d)
        self.assertFalse(r.ok)
        self.assertIn("could not find", r.files[0].hunks[0].message)


if __name__ == "__main__":
    unittest.main()
