"""Layered matching cascade: locate a hunk's old lines inside file lines.

Strategies, tried in order (first level with >=1 hit decides):
  exact          byte-for-byte line equality
  eol_tolerant   trailing whitespace / line endings ignored
  indent_flex    leading whitespace ignored; insertions re-indented to match
                 the file's indentation unit
  fuzzy          difflib.SequenceMatcher over sliding windows, >= threshold

Uniqueness contract: exactly one match at the deciding level succeeds; more
than one is an ambiguity failure (looser levels are supersets of stricter
ones, so escalating can never disambiguate).
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

EXACT = "exact"
EOL_TOLERANT = "eol_tolerant"
INDENT_FLEX = "indent_flex"
FUZZY = "fuzzy"

DEFAULT_THRESHOLD = 0.85


@dataclass
class Match:
    strategy: str
    line_start: int          # 0-based index into file lines
    similarity: float        # 1.0 for non-fuzzy strategies
    replacement: list[str]   # new lines, possibly re-indented


def leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _find_all(
    file_lines: list[str], old: list[str], key, start: int = 0
) -> list[int]:
    n = len(old)
    if n == 0 or len(file_lines) - start < n:
        return []
    keyed_old = [key(l) for l in old]
    return [
        i
        for i in range(start, len(file_lines) - n + 1)
        if [key(l) for l in file_lines[i : i + n]] == keyed_old
    ]


def _reindent(old: list[str], new: list[str], matched_first: str) -> list[str]:
    """Shift new-line indentation from the patch's base to the file's base."""
    base = leading_ws(old[0])
    target = leading_ws(matched_first)
    out = []
    for nl in new:
        if not nl.strip():
            out.append(nl)
            continue
        ws = leading_ws(nl)
        if ws.startswith(base):
            out.append(target + nl[len(base):])
        else:
            out.append(nl)
    return out


def _fuzzy_locate(
    file_lines: list[str], old: list[str], new: list[str],
    threshold: float, offset: int = 0,
) -> Match | None:
    n = len(old)
    target = "\n".join(old)
    best_i, best_score = -1, 0.0
    for i in range(len(file_lines) - n + 1):
        score = SequenceMatcher(None, target, "\n".join(file_lines[i : i + n])).ratio()
        if score > best_score:
            best_i, best_score = i, score
    if best_i < 0 or best_score < threshold:
        return None
    return Match(FUZZY, offset + best_i, round(best_score, 4), list(new))


def locate(
    file_lines: list[str],
    old: list[str],
    new: list[str],
    threshold: float = DEFAULT_THRESHOLD,
    search_from: int = 0,
) -> Match | None:
    """Return the winning Match, or None on zero hits or ambiguity.

    search_from restricts matching to lines at/after that index (used for
    soft @@ anchors); returned line_start stays an absolute file index.
    """
    if not old:
        return None
    for key, strategy in (
        (lambda l: l, EXACT),
        (str.rstrip, EOL_TOLERANT),
        (str.lstrip, INDENT_FLEX),
    ):
        hits = [i for i in _find_all(file_lines, old, key) if i >= search_from]
        if len(hits) == 1:
            i = hits[0]
            repl = new
            if strategy == INDENT_FLEX:
                repl = _reindent(old, new, file_lines[i])
            return Match(strategy, i, 1.0, repl)
        if len(hits) > 1:
            return None  # ambiguous; looser levels cannot fix it
    return _fuzzy_locate(file_lines[search_from:], old, new, threshold, search_from)


def count_matches(file_lines: list[str], old: list[str]) -> int:
    """Exact-match count, falling back to eol-tolerant count."""
    hits = _find_all(file_lines, old, lambda l: l)
    if hits:
        return len(hits)
    return len(_find_all(file_lines, old, str.rstrip))
