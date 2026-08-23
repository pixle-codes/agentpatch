"""Parser for str_replace edit pairs (Claude Code / OpenHands tool-call shape).

Wire shape — a JSON object or an array of them:

    {"path": "f.py",
     "old_str": "exact text to find",
     "new_str": "replacement text",     <- optional (default: delete)
     "replace_all": false}              <- optional (default: false)

Aliases accepted, first key wins: path|file_path, old_str|old_string,
new_str|new_string. This is the argument shape of the Claude Code Edit tool
and OpenHands' str_replace_editor, so transcript replays and MCP payloads
parse without translation.

Semantics (enforced by the applier): old_str must appear EXACTLY once in the
file unless replace_all is set; there is NO fuzzy cascade — the format's
whole contract is byte-exact matching. Multi-line strings are carried in the
Hunk's line lists joined by "\\n"; partial-line splices are supported.
"""

from __future__ import annotations

import json

from .v4a import UPDATE, SUBSTR, FileOp, Hunk, ParseError, Patch, validate_path

STREDIT = "stredit"

_PATH_KEYS = ("path", "file_path")
_OLD_KEYS = ("old_str", "old_string")
_NEW_KEYS = ("new_str", "new_string")


def _pick(entry: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in entry:
            return entry[k]
    return None


def parse_patch(text: str) -> Patch:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(
            f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from None

    entries = data if isinstance(data, list) else [data]
    if not entries or not all(isinstance(e, dict) for e in entries):
        raise ParseError("expected a JSON edit object or a non-empty array of them")

    ops: dict[str, FileOp] = {}
    order: list[str] = []
    for i, entry in enumerate(entries):
        label = f"edit {i}"
        path = _pick(entry, _PATH_KEYS)
        old = _pick(entry, _OLD_KEYS)
        new = _pick(entry, _NEW_KEYS)
        replace_all = entry.get("replace_all", False)

        if path is None:
            raise ParseError(f"{label}: missing 'path' (or 'file_path')")
        if not isinstance(path, str):
            raise ParseError(f"{label}: 'path' must be a string")
        if old is None:
            raise ParseError(f"{label}: missing 'old_str' (or 'old_string')")
        if not isinstance(old, str) or not old:
            raise ParseError(f"{label}: 'old_str' must be a non-empty string")
        if new is None:
            new = ""
        if not isinstance(new, str):
            raise ParseError(f"{label}: 'new_str' must be a string")
        if not isinstance(replace_all, bool):
            raise ParseError(f"{label}: 'replace_all' must be a boolean")

        try:
            path = validate_path(path)
        except ParseError as exc:
            raise ParseError(f"{label}: {exc}") from None

        if path not in ops:
            ops[path] = FileOp(path=path, kind=UPDATE)
            order.append(path)
        ops[path].hunks.append(
            Hunk(
                anchor=None,
                old_lines=old.split("\n"),
                new_lines=new.split("\n"),
                mode=SUBSTR,
                line_hint=None,
                replace_all=replace_all,
            )
        )
    return Patch(format=STREDIT, ops=[ops[p] for p in order])


def detect_format(text: str) -> str | None:
    """Return 'stredit' when text is JSON shaped like str_replace edits.

    Claims: a JSON object carrying an edit-ish key, or an array whose
    entries are all objects, none of which has an RFC6902 "op" key, and at
    least one of which carries an edit-ish key (plus the empty array, which
    nothing else owns — parsing it yields a precise error). RFC6902-style
    patches therefore stay unclaimed.
    """
    stripped = text.lstrip()
    if stripped[:1] not in ("{", "["):
        return None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None

    def _editish(e: dict) -> bool:
        return any(
            k in e for k in ("old_str", "old_string", "new_str", "new_string")
        )

    if isinstance(data, dict):
        claimed = _editish(data)
    elif isinstance(data, list):
        claimed = not data or (
            all(isinstance(e, dict) for e in data)
            and not any("op" in e for e in data)
            and any(_editish(e) for e in data)
        )
    else:
        claimed = False
    return STREDIT if claimed else None
