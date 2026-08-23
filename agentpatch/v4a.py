"""Parser for OpenAI's V4A patch format ("*** Begin Patch" envelopes).

Grammar handled (superset of the cookbook reference, including the
multiple-@@-hunks-per-file-section case that Codex CLI itself mishandles):

    *** Begin Patch
    *** Update File: path        (or Add File: / Delete File:)
    @@ optional anchor line      -- zero or more hunks per section
     context line                -- ' ' prefix: kept in old and new
    -removed line                -- '-' prefix: old only
    +inserted line               -- '+' prefix: new only
    *** Move to: new/path        -- ends an Update section (rename)
    *** End Patch

Paths must be relative and may not contain '..' segments.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ParseError(ValueError):
    pass


class InvalidPath(ParseError):
    pass


UPDATE = "update"
ADD = "add"
DELETE = "delete"
SUBSTR = "substr"               # Hunk.mode value: exact-string search/replace

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"


@dataclass
class Hunk:
    anchor: str | None
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    mode: str = "lines"           # lines | substr
    line_hint: int | None = None  # 0-based expected position (udiff @@ header)
    replace_all: bool = False     # substr mode only: replace every occurrence


@dataclass
class FileOp:
    path: str
    kind: str                      # update | add | delete
    rename_to: str | None = None   # update only
    hunks: list[Hunk] = field(default_factory=list)  # update only
    add_content: list[str] = field(default_factory=list)  # add only


@dataclass
class Patch:
    format: str = "v4a"
    ops: list[FileOp] = field(default_factory=list)


def validate_path(path: str) -> str:
    p = path.strip()
    if not p:
        raise InvalidPath("empty file path in patch")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        raise InvalidPath(f"absolute paths are not allowed: {p!r}")
    parts = p.replace("\\", "/").split("/")
    if ".." in parts:
        raise InvalidPath(f"path traversal is not allowed: {p!r}")
    return p


def _split_header(line: str, keyword: str) -> str:
    rest = line[len(keyword):].strip()
    if not rest:
        raise ParseError(f"{keyword!r} header without a path")
    return rest


def parse_patch(text: str) -> Patch:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0] != _BEGIN:
        raise ParseError('patch must start with "*** Begin Patch"')
    try:
        end = lines.index(_END)
    except ValueError:
        end = -1
    if end == -1:
        raise ParseError('patch must end with "*** End Patch"')

    patch = Patch()
    op: FileOp | None = None
    hunk: Hunk | None = None
    in_add = False

    for raw in lines[1:end]:
        if raw.startswith("*** Update File:"):
            op = FileOp(path=validate_path(_split_header(raw, "*** Update File:")), kind=UPDATE)
            patch.ops.append(op)
            hunk, in_add = None, False
        elif raw.startswith("*** Add File:"):
            op = FileOp(path=validate_path(_split_header(raw, "*** Add File:")), kind=ADD)
            patch.ops.append(op)
            hunk, in_add = None, True
        elif raw.startswith("*** Delete File:"):
            op = FileOp(path=validate_path(_split_header(raw, "*** Delete File:")), kind=DELETE)
            patch.ops.append(op)
            hunk, in_add = None, False
        elif raw.startswith("*** Move to:"):
            if op is None or op.kind != UPDATE:
                raise ParseError("'*** Move to:' outside an Update File section")
            op.rename_to = validate_path(_split_header(raw, "*** Move to:"))
        elif raw.startswith("@@"):
            if op is None or op.kind != UPDATE:
                raise ParseError("'@@' hunk outside an Update File section")
            hunk = Hunk(anchor=raw[2:].strip() or None)
            op.hunks.append(hunk)
        else:
            if op is None:
                raise ParseError(f"content before any file header: {raw.strip()[:40]!r}")
            if op.kind == ADD:
                op.add_content.append(raw[1:] if raw.startswith("+") else raw)
            elif raw.startswith((" ", "-", "+")):
                if hunk is None:
                    hunk = Hunk(anchor=None)
                    op.hunks.append(hunk)
                body = raw[1:]
                if raw.startswith("+"):
                    hunk.new_lines.append(body)
                elif raw.startswith("-"):
                    hunk.old_lines.append(body)
                else:
                    hunk.old_lines.append(body)
                    hunk.new_lines.append(body)
            else:
                raise ParseError(
                    f"line {raw.strip()[:40]!r} lacks a ' ', '-', or '+' prefix "
                    "inside an Update File section"
                )
    return patch


def detect_format(text: str) -> str | None:
    """Return 'v4a' when text looks like a V4A envelope, else None."""
    for line in text.splitlines():
        if not line.strip():
            continue
        return "v4a" if line == _BEGIN else None
    return None
