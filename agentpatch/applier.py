"""Apply parsed patches to files with per-hunk diagnostics.

Per-file atomicity: if any hunk in an update fails, the file on disk is left
untouched and failures are reported structurally.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .matcher import (
    DEFAULT_THRESHOLD,
    _find_all,
    count_matches,
    count_substr,
    diagnose_substr,
    has_ellipsis,
    locate,
    locate_ellipsis,
    nearest_window,
)
from .v4a import DELETE, SUBSTR, Patch

HINT_FLOOR = 0.40


class ApplyError(Exception):
    """Usage-level problems (bad root directory)."""


@dataclass
class HunkResult:
    index: int
    status: str                     # applied | failed
    strategy: str | None = None
    similarity: float | None = None
    line_start: int | None = None   # 1-based position in the original file
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "status": self.status,
            "strategy": self.strategy,
            "similarity": self.similarity,
            "line_start": self.line_start,
            "message": self.message,
        }


@dataclass
class FileResult:
    path: str
    kind: str
    status: str                     # ok | failed  ("ok" = done or would-do)
    renamed_to: str | None = None
    hunks: list[HunkResult] = field(default_factory=list)
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "status": self.status,
            "renamed_to": self.renamed_to,
            "hunks": [h.to_dict() for h in self.hunks],
            "message": self.message,
        }


@dataclass
class PatchResult:
    format: str
    dry_run: bool
    files: list[FileResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(f.status != "failed" for f in self.files)

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "dry_run": self.dry_run,
            "files": [f.to_dict() for f in self.files],
            "summary": {
                "files_applied": sum(1 for f in self.files if f.status == "ok"),
                "files_failed": sum(1 for f in self.files if f.status == "failed"),
                "hunks_applied": sum(
                    1 for f in self.files for h in f.hunks if h.status == "applied"
                ),
                "hunks_failed": sum(
                    1 for f in self.files for h in f.hunks if h.status == "failed"
                ),
            },
        }


def _read_text(path: str) -> tuple[list[str], bool, bool]:
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    if "\r\n" in raw[:4096]:
        # CRLF file: split on the full ending; output is normalized to CRLF.
        lines = raw.split("\r\n")
        trailing_nl = bool(lines) and lines[-1] == ""
        if trailing_nl:
            lines.pop()
        elif lines == [""]:
            lines = []
        return lines, trailing_nl, True
    trailing_nl = raw.endswith("\n")
    lines = raw.split("\n")
    if trailing_nl:
        lines.pop()
    elif lines == [""]:
        lines = []
    return lines, trailing_nl, False


def _write_text(path: str, lines: list[str], trailing_nl: bool, crlf: bool) -> None:
    sep = "\r\n" if crlf else "\n"
    body = sep.join(lines)
    if lines and trailing_nl:
        body += sep
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)


@dataclass
class _Placed:
    start: int      # 0-based index of the matched old block
    old_len: int
    replacement: list[str]
    hunk_index: int


def _anchor_region(file_lines: list[str], anchor: str | None) -> int:
    """Soft @@ anchors restrict the search to the region at/after their first
    occurrence; unknown anchors fall back to a whole-file search."""
    if not anchor:
        return 0
    hits = _find_all(file_lines, [anchor], lambda l: l)
    if not hits:
        hits = _find_all(file_lines, [anchor], str.rstrip)
    return hits[0] if hits else 0


def _locate_all(
    file_lines: list[str], op_hunks, threshold: float
) -> tuple[list[_Placed], list[HunkResult]]:
    placed: list[_Placed] = []
    results: list[HunkResult] = []
    for idx, hunk in enumerate(op_hunks):
        if hunk.mode == SUBSTR:
            continue  # handled sequentially by _apply_substrings
        old, new = hunk.old_lines, hunk.new_lines
        if not old:
            if hunk.line_hint is not None and new:
                # udiff pure insertion ("@@ -5,0 +6,2 @@"): no context to
                # match; the @@ header position is authoritative.
                start = min(hunk.line_hint, len(file_lines))
                placed.append(_Placed(start, 0, list(new), idx))
                results.append(
                    HunkResult(idx, "applied", strategy="position", similarity=1.0,
                               line_start=start + 1)
                )
            else:
                results.append(
                    HunkResult(idx, "failed",
                               message="hunk has no context to anchor its position")
                )
            continue
        start = _anchor_region(file_lines, hunk.anchor)
        if has_ellipsis(old):
            m = locate_ellipsis(file_lines, old, new, search_from=start)
        else:
            m = locate(file_lines, old, new, threshold,
                       search_from=start, hint=hunk.line_hint)
        if m is None:
            if has_ellipsis(old):
                msg = (
                    "'...' pattern is ambiguous or unmatched; "
                    "add more unique context lines"
                )
            else:
                n = count_matches(file_lines[start:], old)
                if n > 1:
                    msg = (
                        f"hunk matches {n} locations (ambiguous; add more unique context)"
                    )
                else:
                    msg = "could not find the target lines in this file"
                    hint = nearest_window(file_lines[start:], old)
                    if hint and hint[1] >= HINT_FLOOR:
                        msg += (
                            f"; nearest similar text at line {start + hint[0] + 1} "
                            f"({hint[1]:.2f} similar)"
                        )
            results.append(HunkResult(idx, "failed", message=msg))
            continue
        span = m.span_len if m.span_len is not None else len(old)
        placed.append(_Placed(m.line_start, span, m.replacement, idx))
        results.append(
            HunkResult(idx, "applied", strategy=m.strategy, similarity=m.similarity,
                       line_start=m.line_start + 1)
        )
    placed.sort(key=lambda p: p.start)
    keep: list[_Placed] = []
    for p in placed:
        if keep and p.start < keep[-1].start + keep[-1].old_len:
            for hit in (p.hunk_index, keep[-1].hunk_index):
                results[hit].status = "failed"
                results[hit].message = "overlaps another hunk"
            continue
        keep.append(p)
    return keep, results


def _apply_substrings(
    lines: list[str], indexed: list[tuple[int, object]]
) -> tuple[list[str] | None, list[HunkResult], bool | None]:
    """Apply substr-mode hunks SEQUENTIALLY against the evolving text.

    `indexed` carries (original op.hunks position, hunk) pairs so result
    indexes stay aligned with the op's hunk list. Each hunk searches the
    result of the previous one (tool-call semantics). Exact matching only:
    0 occurrences fails (with a near-miss hint when one exists), >1 fails as
    ambiguous unless replace_all. Returns (new_lines_or_None_on_failure,
    results, trailing_newline_override) where a trailing override of None
    means "keep the file's original".
    """
    text = "\n".join(lines)
    results: list[HunkResult] = []
    failed = False
    for idx, hunk in indexed:
        if failed:
            results.append(
                HunkResult(idx, "failed", message="skipped: an earlier hunk failed")
            )
            continue
        search = "\n".join(hunk.old_lines)
        replace = "\n".join(hunk.new_lines)
        occurrences = count_substr(text, search)
        if occurrences == 0:
            msg = "exact text not found in this file"
            hint = diagnose_substr(text, search)
            if hint:
                msg += f"; {hint}"
            results.append(HunkResult(idx, "failed", message=msg))
            failed = True
            continue
        if occurrences > 1 and not hunk.replace_all:
            results.append(
                HunkResult(
                    idx,
                    "failed",
                    message=(
                        f"exact text occurs {occurrences} times (ambiguous); "
                        "add surrounding context or set replace_all"
                    ),
                )
            )
            failed = True
            continue
        pos = text.find(search)
        line_start = text.count("\n", 0, pos) + 1
        text = (
            text.replace(search, replace)
            if hunk.replace_all
            else text[:pos] + replace + text[pos + len(search):]
        )
        results.append(
            HunkResult(idx, "applied", strategy=SUBSTR, similarity=1.0,
                       line_start=line_start)
        )
    if failed:
        return None, results, None

    trailing: bool | None = None
    if not text:
        out = []
    elif text.endswith("\n"):
        # A splice extended a newline to EOF; carry that into the write.
        out = text[:-1].split("\n")
        trailing = True
    else:
        out = text.split("\n")
    return out, results, trailing


def apply_patch(
    patch: Patch,
    root: str,
    dry_run: bool = False,
    threshold: float = DEFAULT_THRESHOLD,
) -> PatchResult:
    root_real = os.path.realpath(root)
    if not os.path.isdir(root_real):
        raise ApplyError(f"root directory does not exist: {root}")
    result = PatchResult(patch.format, dry_run)

    def inside(p: str) -> bool:
        rp = os.path.realpath(p)
        return rp == root_real or rp.startswith(root_real + os.sep)

    for op in patch.ops:
        target = os.path.join(root_real, op.path)
        fr = FileResult(op.path, op.kind, "ok")

        if not inside(target):
            fr.status = "failed"
            fr.message = "path escapes the root directory"
            result.files.append(fr)
            continue

        if op.kind == DELETE:
            if not os.path.isfile(target):
                fr.status = "failed"
                fr.message = "file to delete does not exist"
            elif not dry_run:
                os.remove(target)
            else:
                fr.message = "would delete"
            result.files.append(fr)
            continue

        if op.kind == "add":
            if os.path.lexists(target):
                fr.status = "failed"
                fr.message = "file already exists"
            else:
                fr.message = "would create" if dry_run else None
                if not dry_run:
                    _write_text(target, list(op.add_content), True, False)
            result.files.append(fr)
            continue

        if not os.path.isfile(target):
            fr.status = "failed"
            fr.message = "file does not exist"
            result.files.append(fr)
            continue
        try:
            lines, trailing_nl, crlf = _read_text(target)
        except UnicodeDecodeError:
            fr.status = "failed"
            fr.message = "file is not valid UTF-8"
            result.files.append(fr)
            continue

        placed, hunks_res = _locate_all(lines, op.hunks, threshold)
        fr.hunks = hunks_res
        if any(h.status == "failed" for h in hunks_res):
            fr.status = "failed"
            result.files.append(fr)
            continue

        out: list[str] = list(lines)
        for p in reversed(placed):
            out[p.start : p.start + p.old_len] = p.replacement

        substr_pairs = [
            (i, h) for i, h in enumerate(op.hunks) if h.mode == SUBSTR
        ]
        sub_trailing: bool | None = None
        if substr_pairs:
            sub_lines, sub_results, sub_trailing = _apply_substrings(
                out, substr_pairs
            )
            hunks_res.extend(sub_results)
            fr.hunks = hunks_res
            if sub_lines is None:
                fr.status = "failed"
                result.files.append(fr)
                continue
            out = sub_lines

        dest = target
        if op.rename_to is not None:
            dest = os.path.join(root_real, op.rename_to)
            fr.renamed_to = op.rename_to
            if not inside(dest):
                fr.status = "failed"
                fr.message = "rename target escapes the root directory"
                result.files.append(fr)
                continue
            fr.message = "would rename" if dry_run else None

        if not dry_run:
            _write_text(
                dest, out,
                trailing_nl if sub_trailing is None else sub_trailing,
                crlf,
            )
            if dest != target:
                os.remove(target)
        result.files.append(fr)
    return result
