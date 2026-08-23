# PLAN.md — agentpatch

One-line: **A portable, dependency-free engine that turns coding agents' messy edits
(V4A patches, SEARCH/REPLACE blocks, unified diffs) into reliable file changes — with
layered fuzzy matching and machine-readable per-hunk diagnostics.**

## Problem

Every coding-agent harness must turn model-emitted edits into file writes, and every
one hand-rolls its own fragile implementation of the same three steps: parse an edit
format, locate the target region in the file, apply with tolerance for model sloppiness.

Evidence of grief (all within the last 8 months):
- anthropics/claude-code #3471 "Too many edit file errors" (open, 37 reactions);
  #26996 "Edit tool silently converts tabs to spaces, causing repeated match
  failures" (open, 30 reactions).
- Warp engineering (Dec 2025): reimplemented Codex's V4A `apply_patch` across their
  whole stack and *found a bug in OpenAI's own parser* (multiple `@@` contexts per
  file section mishandled) — warp.dev/blog/codex-models-in-warp.
- inputsystems.ai source-read (Jun 2026) of aider vs pi: robustness lives entirely in
  bespoke harness code ("The reliability you feel is invested there, not in the
  weights"); aider's famous fuzzy fallback is unreachable dead code on its apply path.
- openai/codex #28147: indentation drift on added lines breaks patches.
- fabianhertwig.com survey: Codex/aider/OpenHands/RooCode/Cursor each implement
  different formats and matching cascades; Cursor built an entire ML "apply model"
  because matching is the hard part.
- OpenAI community forum (Jun 2026): custom-harness authors struggle to give models
  actionable patch-failure feedback.

Who hurts: builders of agent harnesses and eval/benchmark pipelines (SWE-bench-style
harnesses included), plus anyone scripting codex/aider outputs. The population of
people writing these harnesses exploded in 2025-2026 and each new entrant re-solves
(or fails to solve) the same layer.

## Why existing solutions fail

- **OpenAI's reference `apply_patch.py`** (GPT-4.1 cookbook): single format (V4A),
  exact-match only, no fuzziness, no diagnostics beyond a bare exception.
- **Embedded implementations** (aider `editblock_coder.py`, pi `edit-diff.ts`,
  RooCode `MultiSearchReplaceDiffStrategy`, codex-rs): coupled to their hosts,
  different feature sets, none reusable; aider's edit-distance fallback is literally
  unreachable; Warp proved even the canonical V4A parser has bugs.
- **java-diff-utils / bsdiff / python-json-patch etc.**: operate on unified diffs,
  binaries, or RFC6902 JSON — nobody speaks the *agent* wire formats.
- **Morph & friends**: commercial ML fast-apply APIs; network dependency, cost,
  nondeterminism. Different axis from a deterministic local library.
- **Cursor apply-model**: closed, product-internal, ML-based.

## Your edge

1. **Multi-format, auto-detected**: V4A (`*** Begin Patch`), aider-style
   SEARCH/REPLACE blocks, unified diffs, str_replace pairs — one engine.
2. **Layered matching cascade** with explicit strategies: exact → trailing-
   whitespace/EOL-tolerant → indentation-flexible (re-indent insertions like
   RooCode's middle-out) → bounded fuzzy with similarity scores.
3. **Structured diagnostics**: per-hunk status, strategy used, 1-based location,
   similarity, ambiguity counts, "did you mean" nearest-context hints — designed to
   be fed straight back to a model so it can self-correct next turn.
4. **Zero dependencies**, stdlib-only, deterministic, offline; CLI with --json and
   0/1/2 exit codes so any harness can shell out to it today.
5. **Correctness on the cases vendors got wrong**: multiple `@@` hunks per file
   section is a pinned, tested feature (Warp's finding), not an accident.

## Architecture

```
model output ──▶ formats.detect() ──▶ parser (v4a | editblock | udiff)
                                        │ produces Patch{ops}
              ┌─────────────────────────┘
              ▼
        applier.apply(patch, files, threshold)
              │ per op: read file → matcher.locate(hunk.old_lines)
              │ cascade: EXACT → EOL/WS-TOLERANT → INDENT-FLEX → FUZZY≥t
              ▼
        PatchResult{per-hunk status/strategy/similarity/location/hints}
              ▼
        writers: rewritten files (atomic) · JSON report · exit codes
```

Components: `formats.py` (detect/dispatch), `v4a.py`, `editblock.py`, `udiff.py`,
`matcher.py` (cascade + uniqueness contract), `applier.py` (orchestration, overlap
checks, back-to-front application, atomic writes), `cli.py`.
Storage: none (pure transformation library). No servers, no network.

Key decisions: stdlib-only (difflib for fuzziness); paths validated relative, no
absolute-path or `..` traversal (matches V4A security posture); multi-edit overlap
detection before any write; dry-run by default in `check` mode.

## Milestones

- **M1 (s23, SHIPPED)**: V4A format end-to-end — parser (Update/Add/Delete/Rename,
  multiple hunks incl. the Warp edge case), matcher cascade levels 1–3, CLI
  `parse`/`apply` with `--json --dry-run --root --fuzzy-threshold`, exit-code
  contract, full test suite, README, publish.
- **M2 (this session, s25)**: SEARCH/REPLACE block format (aider/Cline/Roo
  dialect incl. filename-above-fence rule, ellipsis elision), auto-detection
  between formats. ✅ DONE — editblock.py parser + formats.py dispatch +
  ellipsis strategy in matcher + `--format` CLI flag; 95 tests green.
- **M3**: Unified diff ingestion; str_replace pair mode; "did you mean" hint
  generation (nearest-window SequenceMatcher reporting, aider-style feedback block).
- **M4**: Indent-flex insertion re-indentation polish, CRLF policy knobs, benchmark
  corpus (replay real-world failed patches from public issues as fixtures),
  ARCHITECTURE.md, v1.0 tag.

## Gotchas / decisions log

- V4A `@@` anchor line: treat same-line text after `@@` as an extra required context
  line (that is how codex-rs consumes it); hunks without anchors match on body alone.
- Add File bodies: strip exactly one leading `+` per line; empty line may be bare ``.
- Never flag or rewrite content outside ops; delete-file requires exact path match.
- Exit codes: 0 = all clean, 1 = any hunk/file failure (incl. dry-run findings),
  2 = usage/IO errors. Pinned in tests.
- s25 editblock decisions: markers accept 5–9 chars + trailing annotations but the
  keywords are literal so `<<<<<<< HEAD` merge markers never parse as blocks;
  filename resolution = backward scan (skip blanks/fences, strip decorations,
  require extension-ish pathiness) with fallback forward scan inside the fence
  (Cline style); a whitespace-only SEARCH maps to an ADD op (fails if file exists —
  deliberate divergence from aider's overwrite); empty REPLACE = deletion hunk;
  both sides empty = ParseError; undecorated prose directly above a block stops the
  backward filename scan (ParseError beats grabbing a stale earlier filename);
  ellipsis matching is line-based head/tail like aider's try_dotdotdots — middle is
  preserved only when the replacement also contains `...`; Match.span_len carries
  ellipsis consumed spans so overlap rejection stays correct.
- s25 lesson: exact-match on an UNINDENTED search against an INDENTED file line is
  indent_flex by design (leading ws ignored) — tests must expect that, and the
  reindent applies to insertions at that level.
