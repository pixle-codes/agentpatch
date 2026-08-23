# agentpatch

**Parse and apply coding agents' patches reliably.** A zero-dependency engine
that turns model-emitted edits into file changes — with layered fuzzy matching,
ambiguity detection, and structured per-hunk diagnostics designed to be fed back
to the model so it can self-correct.

Today every coding-agent harness hand-rolls the same fragile layer between
"the model emitted an edit" and "the file changed": parse a patch format, find
the target region, apply with tolerance for sloppiness. aider, Cline, RooCode,
pi and Codex each embed their own; Warp reimplemented OpenAI's V4A parser and
found bugs *in it*. agentpatch extracts that layer into one portable,
deterministic, offline library + CLI. Full evidence trail in [PLAN.md](PLAN.md).

## Features

- **Three formats, auto-detected** (or forced with `--format`):
  - **V4A** (`*** Begin Patch` envelopes): Update / Add / Delete /
    Move-to rename, multiple `@@` hunks per file section — including the
    multi-hunk case Codex CLI itself mishandles.
  - **SEARCH/REPLACE blocks** (the aider/Cline/Roo dialect): filename above
    the fence or inside it, markdown fences stripped, 5–9 marker chars,
    trailing annotations on markers tolerated — while git merge-conflict
    markers (`<<<<<<< HEAD`) never parse as blocks. A whitespace-only
    SEARCH creates the file; an empty REPLACE deletes the matched lines.
  - **Unified diffs** (git-style and plain `diff -ru` output): count-driven
    hunk bodies (a removed line starting with `--` never reads as the next
    header), `a/`/`b/` prefix stripping, `/dev/null` new/deleted files,
    `\ No newline at end of file`, optional counts. `@@` start lines act as
    positional hints: repeated context resolves to the hit nearest the
    declared position instead of failing as ambiguous, and zero-context
    insertion hunks (`@@ -5,0 +6,2 @@`) apply positionally.
- **Ellipsis elision**: a lone `...` line in a SEARCH/REPLACE block stands
  for any run of lines; keeping `...` in the replacement preserves the
  omitted middle, dropping it removes it (aider's try-dot-dot-dots
  semantics, line-based).
- **Layered matching cascade**, first unique hit wins:
  1. `exact`
  2. `eol_tolerant` — trailing whitespace / line endings ignored
  3. `indent_flex` — leading whitespace ignored, insertions re-indented to the
     file's indentation base (tab-indented files stop failing)
  4. `ellipsis` — head/tail segments around `...` markers
  5. `fuzzy` — best sliding-window similarity >= threshold (default 0.85)
- **Uniqueness contract**: ambiguous matches fail loudly with a location count
  instead of silently editing the wrong block.
- **"Did you mean" diagnostics**: when a hunk can't be located, the failure
  message reports the nearest similar text's line number and similarity, ready
  to feed back to the model for self-correction next turn.
- **Per-file atomicity**: one bad hunk leaves the whole file untouched.
- **Overlap detection**: overlapping hunks are both rejected (ellipsis hunks
  count their full consumed span).
- **Safety**: relative paths only; `..`, absolute paths, and symlink escapes
  out of the root are blocked; binary files reported, not mangled; CRLF and
  missing-final-newline preserved.
- **Agent-readable CLI**: `--json` reports with per-hunk strategy / similarity /
  line numbers, stdin via `-`, exit codes `0` clean / `1` failures / `2`
  usage-or-parse errors.
- **Zero dependencies**, Python 3.9+ stdlib only.

## Install

```bash
git clone https://github.com/pixle-codes/agentpatch.git
cd agentpatch
python3 -m agentpatch.cli --help
```

## Usage

```bash
agentpatch apply fix.patch -C myrepo --dry-run   # verdicts only, no writes
agentpatch apply fix.patch -C myrepo             # writes files
echo "$PATCH" | agentpatch apply - --json        # stdin + machine report
agentpatch parse fix.patch --json                # inspect ops without touching fs
```

Text output:

```
[ok] update: src/app.py (APPLIED)
  hunk 0: applied at line 2 via exact
summary: 1 file(s) ok, 0 failed; 1 hunk(s) applied, 0 failed
```

JSON report (truncated):

```json
{"format": "v4a", "dry_run": false,
 "files": [{"path": "src/app.py", "kind": "update", "status": "ok",
   "hunks": [{"index": 0, "status": "applied", "strategy": "exact",
              "similarity": 1.0, "line_start": 2, "message": null}]}],
 "summary": {"files_applied": 1, "files_failed": 0,
             "hunks_applied": 1, "hunks_failed": 0}}
```

### SEARCH/REPLACE blocks

```text
src/app.py
```python
<<<<<<< SEARCH
def old():
    return 1
=======
def new():
    return 2
>>>>>>> REPLACE
```
```

### Unified diff

```bash
git diff > fix.diff
agentpatch apply fix.diff -C myrepo      # detected automatically
```

Auto-detection picks the format; `--format v4a|editblock|udiff` forces one.

### Library

```python
from agentpatch import parse_patch, apply_patch

res = apply_patch(parse_patch(text), root="myrepo", dry_run=False)
if not res.ok:
    for f in res.files:
        for h in f.hunks:
            if h.status == "failed":
                print(f.path, h.index, h.message)  # feed back to the model
```

## Roadmap

- M3 (remaining): str_replace pair mode (exact string replacement, not line lists)
- M4: real-world failure-corpus benchmarks, ARCHITECTURE.md, v1.0

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

## License

MIT — see [LICENSE](LICENSE).
