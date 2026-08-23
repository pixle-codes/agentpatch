"""Corpus of real-world failure shapes mined from public issues.

Each JSON file pins a shape models actually emit in the wild (provenance
in its "source" field): the raw patch text plus before/after file states.
The loader runs every case through detect -> parse -> apply exactly as the
CLI would, so regressions against reality fail loudly.
"""

import json
import os
import tempfile
import unittest

from agentpatch.applier import apply_patch
from agentpatch.formats import parse_patch


CORPUS_DIR = os.path.join(os.path.dirname(__file__), "corpus")


def load_cases():
    cases = []
    for name in sorted(os.listdir(CORPUS_DIR)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(CORPUS_DIR, name), encoding="utf-8") as fh:
            cases.append((name, json.load(fh)))
    return cases


class CorpusTests(unittest.TestCase):
    def test_corpus_nonempty(self):
        self.assertGreaterEqual(len(load_cases()), 4)

    def _run_case(self, name, case):
        d = tempfile.mkdtemp()
        for f in case["files"]:
            path = os.path.join(d, f["path"])
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline="") as fh:
                fh.write(f["before"])
        patch = parse_patch(case["patch"])
        result = apply_patch(patch, d)
        if "expect_failure_message_contains" in case:
            flat = [h for f in result.files for h in f.hunks]
            self.assertFalse(
                result.ok, f"{name}: expected failure, got success"
            )
            msgs = " ".join(h.message or "" for h in flat)
            for needle in case["expect_failure_message_contains"]:
                self.assertIn(needle, msgs)
            return
        self.assertTrue(result.ok, f"{name}: {result.to_dict()}")
        for f in case["files"]:
            with open(os.path.join(d, f["path"]), newline="") as fh:
                self.assertEqual(fh.read(), f["after"], f"{name}:{f['path']}")

    def test_every_case(self):
        for name, case in load_cases():
            with self.subTest(case=name):
                self._run_case(name, case)

    def test_every_case_documents_provenance(self):
        for name, case in load_cases():
            with self.subTest(case=name):
                self.assertTrue(case["source"].startswith("https://"))
                self.assertTrue(case["note"].strip())


if __name__ == "__main__":
    unittest.main()
