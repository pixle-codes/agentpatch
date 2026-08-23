"""Parser for aider/Cline/Roo-style SEARCH/REPLACE blocks.

Grammar (tolerant superset of the dialects in the wild):

    path/to/file.py                 <- filename above the fence (or inside it)
    ```python                       <- optional markdown fence, stripped
    <<<<<<< SEARCH                  <- 5-9 '<' chars + SEARCH
    old lines (raw)
    =======                         <- 5-9 '=' chars alone
    new lines (raw)
    >>>>>>> REPLACE                 <- 5-9 '>' chars + REPLACE
    ```

Dialect notes encoded here:
- The filename lives ABOVE the opening fence (aider) or as the first line
  inside the fence (Cline/Roo); both are resolved, decorations like
  backticks/bold/"File:" prefixes are stripped.
- Marker lines tolerate 5-9 marker chars and trailing annotations
  ("<<<<<<< SEARCH (exact match)") but the keywords are literal, so git
  merge-conflict markers (<<<<<<< HEAD) never parse as blocks.
- A whitespace-only SEARCH means "create this file" -> an add op.
- An empty REPLACE means "delete the matched lines" -> a hunk with no new
  lines. Both sides empty is a ParseError.
- A lone `...` line inside SEARCH/REPLACE is an elision marker: it stands
  for any run of lines (matched by the applier's ellipsis strategy).
"""

from __future__ import annotations

import re

from .v4a import ADD, FileOp, Hunk, InvalidPath, ParseError, Patch, UPDATE, validate_path

HEAD_RE = re.compile(r"^<{5,9}\s*SEARCH\b.*$")
DIVIDER_RE = re.compile(r"^={5,9}\s*$")
REPLACE_RE = re.compile(r"^>{5,9}\s*REPLACE\b.*$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,}).*$")

_SKIP_CHARS = "#>*-"
_PREFIX_RE = re.compile(r"^(?:file|filename|path)\s*:\s*", re.IGNORECASE)


_BULLET_RE = re.compile(r"^[-*+]\s+")


def _clean_candidate(line: str) -> str | None:
    cand = line.strip("`* \t")
    if cand.startswith("#"):
        cand = cand.lstrip("#").strip()
    cand = _BULLET_RE.sub("", cand)
    cand = _PREFIX_RE.sub("", cand).strip()
    if cand.endswith(":"):
        cand = cand[:-1].strip()
    cand = cand.strip("`* \t")
    if not cand or " " in cand or not re.search(r"\.[A-Za-z0-9_-]{1,8}$", cand):
        return None
    try:
        return validate_path(cand)
    except InvalidPath:
        return None


def _find_filename(
    lines: list[str], head_idx: int, fence_idx: int | None
) -> str:
    for i in range(head_idx - 1, -1, -1):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            continue
        if FENCE_RE.match(raw):
            continue  # opening fence / language tag, never a filename
        cand = _clean_candidate(stripped)
        if cand:
            return cand
        if stripped[0] in _SKIP_CHARS:
            continue  # decorated prose; keep looking above
        break  # undecorated prose: the model forgot the filename
    if fence_idx is not None:
        for i in range(fence_idx + 1, head_idx):
            if lines[i].strip():
                cand = _clean_candidate(lines[i].strip())
                if cand:
                    return cand
    raise ParseError(
        f"no filename found above the SEARCH block at line {head_idx + 1}"
    )


def parse_patch(text: str) -> Patch:
    lines = text.splitlines()
    patch = Patch(format="editblock")

    state = "outside"          # outside | search | replace
    search: list[str] = []
    replace: list[str] = []
    cur_path: str | None = None
    head_idx = -1
    fence_idx: int | None = None

    def emit(path: str, old: list[str], new: list[str]) -> None:
        if not any(l.strip() for l in old) and not any(l.strip() for l in new):
            raise ParseError(
                f"SEARCH/REPLACE block for {path!r} has both sides empty"
            )
        if not any(l.strip() for l in old):
            op = FileOp(path=path, kind=ADD, add_content=list(new))
            patch.ops.append(op)
            return
        hunk = Hunk(anchor=None, old_lines=old, new_lines=new)
        last = patch.ops[-1] if patch.ops else None
        if (
            last is not None
            and last.kind == UPDATE
            and last.path == path
            and last.rename_to is None
        ):
            last.hunks.append(hunk)
        else:
            patch.ops.append(FileOp(path=path, kind=UPDATE, hunks=[hunk]))

    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n")
        if state == "search":
            if DIVIDER_RE.match(line):
                state = "replace"
                replace = []
            elif HEAD_RE.match(line):
                raise ParseError(
                    f"new SEARCH at line {idx + 1} before ======= divider"
                )
            elif REPLACE_RE.match(line):
                raise ParseError(
                    f"REPLACE marker at line {idx + 1} before ======= divider"
                )
            else:
                search.append(line)
            continue
        if state == "replace":
            if REPLACE_RE.match(line):
                emit(cur_path, search, replace)
                state = "outside"
                cur_path = None
            elif HEAD_RE.match(line):
                raise ParseError(
                    f"new SEARCH at line {idx + 1} without >>>>>>> REPLACE"
                )
            else:
                replace.append(line)
            continue
        # state == outside
        if HEAD_RE.match(line):
            head_idx = idx
            cur_path = _find_filename(lines, idx, fence_idx)
            search, replace = [], []
            state = "search"
            continue
        if FENCE_RE.match(line):
            fence_idx = idx

    if state == "search":
        raise ParseError("unterminated SEARCH section (no ======= divider)")
    if state == "replace":
        raise ParseError("unterminated REPLACE section (no >>>>>>> REPLACE)")
    if not patch.ops:
        raise ParseError("no SEARCH/REPLACE blocks found")
    return patch


def detect_format(text: str) -> str | None:
    """Return 'editblock' when text contains a SEARCH marker line."""
    for line in text.splitlines():
        if HEAD_RE.match(line):
            return "editblock"
    return None
