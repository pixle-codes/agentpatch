"""Parser for unified diffs (git-style and plain `diff -ru` output).

Grammar handled:

    diff --git a/f.py b/f.py      <- optional preamble lines, ignored
    index abc..def 100644         <-
    --- a/f.py [TAB timestamp]    <- or --- /dev/null  (new file)
    +++ b/f.py [TAB timestamp]    <- or +++ /dev/null  (deleted file)
    @@ -l,c +l,c @@ heading       <- counts optional ("@@ -1 +1 @@"), c may be 0
     context line                 <- ' ' prefix
    -removed line
    +inserted line
    \ No newline at end of file   <- tolerated, skipped

Dialect notes encoded here:
- The leading `a/`/`b/` component is stripped only when both sides carry the
  same prefix style; unprefixed (git apply -p0, hg) paths pass through.
- Hunk bodies are consumed COUNT-DRIVEN: exactly old_count context/deleted
  plus new_count context/inserted lines. This is what lets a removed line
  whose content starts with `--` (rendered as `--- ...`) never be mistaken
  for the next file header — GNU patch semantics.
- The @@ header's old-file start line is kept as hunk.line_hint: the applier
  uses it to disambiguate repeated context (nearest hit wins) and to place
  zero-context pure-insertion hunks positionally.
"""

from __future__ import annotations

import re

from .v4a import ADD, DELETE, UPDATE, FileOp, Hunk, ParseError, Patch, validate_path

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @")

DEV_NULL = "/dev/null"


def _strip_prefix(path: str) -> str:
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _parse_header(line: str, tag: str) -> str | None:
    """Parse a '--- path' / '+++ path' line; None means /dev/null."""
    rest = line[len(tag):].strip()
    if not rest:
        raise ParseError(f"{tag!r} header without a path")
    path = rest.split("\t")[0].strip()
    if path == DEV_NULL:
        return None
    return validate_path(_strip_prefix(path))


def parse_patch(text: str) -> Patch:
    lines = text.splitlines()
    patch = Patch(format="udiff")
    i, n = 0, len(lines)
    seen_file = False

    while i < n:
        raw = lines[i]

        if raw.startswith("diff ") or raw.startswith("index "):
            i += 1
            continue

        if not raw.startswith("--- "):
            if not raw.strip():
                i += 1
                continue
            raise ParseError(
                f"expected '--- ' file header at line {i + 1}, got {raw.strip()[:40]!r}"
            )

        old_path = _parse_header(raw, "---")
        i += 1
        if i >= n or not lines[i].startswith("+++ "):
            raise ParseError(f"'---' header at line {i} lacks a '+++' header")
        new_path = _parse_header(lines[i], "+++")
        i += 1

        if old_path is None and new_path is None:
            raise ParseError("file section has /dev/null on both sides")

        hunks: list[Hunk] = []
        add_content: list[str] = []

        while i < n:
            m = HUNK_RE.match(lines[i])
            if m is None:
                break
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            i += 1

            # Pure insertions (old_count == 0) have no old region to match;
            # their authoritative position is where the new lines begin
            # ("@@ -5,0 +6,2 @@" inserts before new line 6, i.e. index 5).
            hunk = Hunk(
                anchor=None,
                mode="lines",
                line_hint=(
                    old_start - 1 if old_count > 0
                    else max(new_start - 1, 0)
                ),
            )
            # Count-driven consumption: a hunk ends exactly when both sides
            # have their promised line tallies (context lines land in both).
            while len(hunk.old_lines) < old_count or (
                len(hunk.new_lines) < new_count
            ):
                if i >= n:
                    raise ParseError(
                        f"truncated hunk at line {i}: header promised "
                        f"-{old_count},+{new_count} but the patch ends early"
                    )
                body = lines[i]
                i += 1
                if body.startswith("\\"):
                    continue  # "\ No newline at end of file"
                prefix, content = body[:1], body[1:]
                if prefix in (" ", ""):
                    # git renders an empty context line as a bare "" when the
                    # trailing space was stripped by a mailer/editor
                    if prefix == "" and content:
                        raise ParseError(
                            f"hunk body line {body.strip()[:40]!r} at line {i} "
                            "lacks a ' ', '-', or '+' prefix"
                        )
                    hunk.old_lines.append(content)
                    hunk.new_lines.append(content)
                elif prefix == "-":
                    hunk.old_lines.append(content)
                elif prefix == "+":
                    hunk.new_lines.append(content)
                else:
                    raise ParseError(
                        f"hunk body line {body.strip()[:40]!r} at line {i} "
                        "lacks a ' ', '-', or '+' prefix"
                    )
            while i < n and lines[i].startswith("\\"):
                i += 1  # markers trailing the finished side of a hunk
            if old_path is None:
                add_content.extend(hunk.new_lines)
            elif new_path is None or (hunk.old_lines or hunk.new_lines):
                hunks.append(hunk)

        guard = (
            f"file section {new_path or old_path!r} has headers but no hunk "
            "bodies"
        )
        if old_path is None:
            op = FileOp(path=new_path, kind=ADD, add_content=add_content)
        elif new_path is None:
            op = FileOp(path=old_path, kind=DELETE)
        else:
            # A real update always carries hunks; bare headers mean malformed
            # input or prose that merely looks like a header. GNU patch
            # rejects these; so do we (CLI surfaces it as exit 2).
            if not hunks:
                raise ParseError(guard)
            op = FileOp(path=old_path, kind=UPDATE, hunks=hunks)
        patch.ops.append(op)
        seen_file = True

    if not seen_file:
        raise ParseError("no '--- /+++' file sections found")
    return patch


def detect_format(text: str) -> str | None:
    """Return 'udiff' when the first meaningful line opens a unified diff."""
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(("diff --git ", "diff -")) or line.startswith("--- "):
            return "udiff"
        return None
    return None
