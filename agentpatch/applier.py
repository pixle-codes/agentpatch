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
    has_ellipsis,
    locate,
    locate_ellipsis,
    nearest_window,
)
from .v4a import DELETE, Patch

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
        old, new = hunk.old_lines, hunk.new_lines
        if not old:
            results.append(
                HunkResult(idx, "failed",
                           message="hunk has no context to anchor its position")
            )
            continue
        start = _anchor_region(file_lines, hunk.anchor)
        if has_ellipsis(old):
            m = locate_ellipsis(file_lines, old, new, search_from=start)
        else:
            m = locate(file_lines, old, new, threshold, search_from=start)
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
            _write_text(dest, out, trailing_nl, crlf)
            if dest != target:
                os.remove(target)
        result.files.append(fr)
    return result
