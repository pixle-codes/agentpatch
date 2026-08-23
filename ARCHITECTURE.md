# ARCHITECTURE.md

agentpatch turns model-emitted edit text into file changes through four
stages: **detect → parse → locate/match → apply**. Everything is stdlib-only,
deterministic and offline. This document explains the moving parts and the
invariants that make them trustworthy.

```
patch text ──▶ formats.detect()            first match wins, fixed priority
                   │
                   ▼
             parser.parse_patch()          one module per wire format
                   │  produces Patch{ops:[FileOp{hunks:[Hunk]}]}
                   ▼
             applier.apply_patch()
                   ├─ read file (newline="" → LF/CRLF aware line list)
                   ├─ line hunks   ─▶ matcher.locate() cascade
                   ├─ substr hunks ─▶ sequential text-space splices
                   ├─ overlap rejection + back-to-front application
                   └─ atomic write (all-or-nothing per file)
                   │
                   ▼
             PatchResult {files:[FileResult{hunks:[HunkResult]}]}
             → text report / --json / exit code 0|1|2
```

## Data model (`v4a.py`)

Every format compiles into the same three structures, so the applier and CLI
are format-agnostic:

- **Patch** — `format` name + ordered `ops`.
- **FileOp** — one path per operation: `update`, `add`, `delete`, optional
  `rename_to`. Update ops carry hunks; add ops carry full content.
- **Hunk** — the universal unit of edit:
  - `old_lines` / `new_lines`: the matched and replacement content, split by
    `\n`. For `mode="substr"` hunks these are joined back into exact strings;
    for `mode="lines"` they are whole-line lists.
  - `anchor`: soft context hint from V4A `@@` lines (narrows the search
    region, never required).
  - `line_hint`: authoritative position from udiff `@@ -l,c +l,c @@` headers.
    Breaks exact-level ties by nearest hit and places zero-context insertions.
  - `replace_all`: substr-only flag allowing multiple occurrences.
  - `mode`: `"lines"` (cascade matching) or `"substr"` (byte-exact).

## Format modules

Each parser module exposes `parse_patch(text) -> Patch` and
`detect_format(text) -> str | None`; registering both in `formats.py` is the
entire extension recipe.

- **v4a.py** — `*** Begin Patch` envelopes. Multiple `@@` hunks per section
  are a pinned feature (Codex mishandles them; Warp found this).
- **editblock.py** — fenced SEARCH/REPLACE blocks. Filename resolution scans
  backward past fences/decorations with an inside-the-fence fallback; marker
  keywords are literal so git conflict markers never parse. Whitespace-only
  SEARCH ⇒ add op.
- **udiff.py** — count-driven hunk consumption (GNU patch semantics): a hunk
  ends exactly when both side tallies are filled, which makes removed lines
  starting with `--` safe without lookahead. `/dev/null` sections become
  add/delete ops.
- **stredit.py** — JSON str_replace pairs (Claude Code Edit / OpenHands
  shape, aliases accepted). Detection claims only edit-shaped JSON: an object
  with an edit key, or arrays whose dicts have no RFC6902 `op` key. Empty
  arrays are claimed deliberately — a precise "non-empty array" parse error
  beats a generic unrecognized-format exit.

Detection priority v4a → editblock → udiff → stredit exists because earlier
formats' false-positive surfaces are narrower: a fenced SEARCH block may
contain `--- `, but JSON never looks like V4A.

## Matching (`matcher.py`)

### Line mode cascade

`locate()` tries strategies strictly in order; the FIRST level with hits
decides:

1. `exact` — byte equality
2. `eol_tolerant` — trailing whitespace ignored
3. `indent_flex` — leading whitespace ignored; insertions re-indented from
   the patch's base indentation to the file's
4. `ellipsis` — head/tail around `...` markers, middle preserved iff the
   replacement also contains `...`
5. `fuzzy` — best sliding-window SequenceMatcher ≥ threshold (default 0.85)

Looser levels are supersets of stricter ones, so escalating can never
disambiguate what a stricter level found ambiguous — that asymmetry is why
the uniqueness contract works:

> **Uniqueness contract** — exactly one match at the deciding level applies;
> more than one fails loudly ("matches N locations"). Formats carrying
> positional metadata may relax this via `hint=`: the hit nearest the hint
> wins (udiff), or the hunk applies purely positionally (zero-context
> insertions).

Failure diagnostics are designed for model self-correction: ambiguity
reports the count; misses report the best nearby window's line and
similarity when above `HINT_FLOOR`.

### Substr mode

`count_substr()` counts occurrences INCLUDING overlaps (honest ambiguity),
while `replace_all` splicing uses non-overlapping left-to-right replacement.
`diagnose_substr()` reports which relaxation *would* match (trailing
whitespace, leading indentation) and at which line — precise enough for the
model to fix its own string next turn.

## Applying (`applier.py`)

Per FileOp, against the real file:

1. **Safety**: paths resolve under the root via `realpath` (symlink escapes
   blocked); add refuses existing files; delete requires existence.
2. **Line hunks** locate against the ORIGINAL lines; placements are checked
   pairwise (ellipsis spans use their consumed span) and applied
   back-to-front so indices stay valid.
3. **Substr hunks** then apply SEQUENTIALLY against the evolving text —
   later edits see earlier results, matching tool-call semantics. A splice
   extending `\n` to EOF flips trailing-newline on for the write.
4. **Atomicity**: any hunk failure marks the whole file failed and nothing
   is written; later hunks are reported as skipped rather than evaluated
   against a hypothetical state.
5. **Writes**: files are read and written with `newline=""` (no silent
   translation); CRLF files keep CRLF; a missing final newline survives
   unless an edit explicitly added one at EOF.

Mixed line+substr ops (only constructible programmatically) run line hunks
first, then substr on the intermediate result.

## Exit codes

`0` everything applied (or would apply under `--dry-run`) · `1` any
file/hunk failure (dry-run findings included) · `2` usage, IO, or parse
errors. Parse errors never touch the filesystem.

## Testing

172 stdlib unittest cases (`python3 -m unittest discover -s tests -t .`),
one file per format plus CLI contracts. Fixtures pin dialect edges (marker
tolerances, count arithmetic, alias precedence) and safety invariants
(traversal, symlink escape, atomicity after mid-file failure).
