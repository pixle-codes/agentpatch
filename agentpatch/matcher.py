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
ELLIPSIS = "ellipsis"

DEFAULT_THRESHOLD = 0.85


@dataclass
class Match:
    strategy: str
    line_start: int          # 0-based index into file lines
    similarity: float        # 1.0 for non-fuzzy strategies
    replacement: list[str]   # new lines, possibly re-indented
    span_len: int | None = None  # consumed file lines; None -> caller uses len(old)


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


def is_ellipsis_marker(line: str) -> bool:
    return line.strip() == "..."


def has_ellipsis(lines: list[str]) -> bool:
    return any(is_ellipsis_marker(l) for l in lines)


def _split_ellipsis(lines: list[str]) -> tuple[list[str], list[str]]:
    """Head = lines before the first marker, tail = after the last one."""
    idxs = [i for i, l in enumerate(lines) if is_ellipsis_marker(l)]
    if not idxs:
        return list(lines), []
    return lines[: idxs[0]], lines[idxs[-1] + 1 :]


def locate_ellipsis(
    file_lines: list[str],
    old: list[str],
    new: list[str],
    search_from: int = 0,
) -> Match | None:
    """Match a SEARCH whose lone `...` line stands for any run of lines.

    Mirrors aider's try_dotdotdots: head/tail segments of the pattern are
    located in order; a `...` in the replacement preserves the omitted
    middle, its absence drops it. Exactly one valid head/tail placement
    must exist (uniqueness contract).
    """
    head, tail = _split_ellipsis(old)
    if not head and not tail:
        return None
    for key in (lambda l: l, str.rstrip):
        head_hits = [
            i
            for i in range(search_from, len(file_lines) - len(head) + 1)
            if head and [key(l) for l in file_lines[i : i + len(head)]] == [key(l) for l in head]
        ]
        tail_hits = [
            j
            for j in range(search_from, len(file_lines) - len(tail) + 1)
            if tail and [key(l) for l in file_lines[j : j + len(tail)]] == [key(l) for l in tail]
        ]
        if not head:
            pairs = [(j, j + len(tail)) for j in tail_hits]
        elif not tail:
            pairs = [(i, i + len(head)) for i in head_hits]
        else:
            pairs = [
                (i, j + len(tail))
                for i in head_hits
                for j in tail_hits
                if j >= i + len(head)
            ]
        if len(pairs) > 1 or not pairs:
            continue
        start, end = pairs[0]
        middle = file_lines[start + len(head) : end - len(tail)]
        new_head, new_tail = _split_ellipsis(new)
        repl = (
            list(new_head) + middle + list(new_tail)
            if has_ellipsis(new)
            else list(new_head) + list(new_tail)
        )
        return Match(ELLIPSIS, start, 1.0, repl, span_len=end - start)
    return None


def locate(
    file_lines: list[str],
    old: list[str],
    new: list[str],
    threshold: float = DEFAULT_THRESHOLD,
    search_from: int = 0,
    hint: int | None = None,
) -> Match | None:
    """Return the winning Match, or None on zero hits or ambiguity.

    search_from restricts matching to lines at/after that index (used for
    soft @@ anchors); returned line_start stays an absolute file index.
    hint (0-based expected position, from udiff @@ headers) breaks ties:
    with several hits at the deciding level the one nearest the hint wins
    instead of failing as ambiguous. Formats without positional metadata
    keep the strict uniqueness contract by leaving hint=None.
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
            if hint is None:
                return None  # ambiguous; looser levels cannot fix it
            i = min(hits, key=lambda h: abs(h - hint))
            repl = new
            if strategy == INDENT_FLEX:
                repl = _reindent(old, new, file_lines[i])
            return Match(strategy, i, 1.0, repl)
    return _fuzzy_locate(file_lines[search_from:], old, new, threshold, search_from)


def nearest_window(
    file_lines: list[str], old: list[str]
) -> tuple[int, float] | None:
    """Best sliding-window similarity for diagnostics ("did you mean").

    Returns (0-based line_start, ratio) or None when old is empty or the
    file is shorter than old.
    """
    n = len(old)
    if n == 0 or len(file_lines) < n:
        return None
    target = "\n".join(old)
    best_i, best = -1, 0.0
    for i in range(len(file_lines) - n + 1):
        score = SequenceMatcher(None, target, "\n".join(file_lines[i : i + n])).ratio()
        if score > best:
            best_i, best = i, score
    return (best_i, round(best, 4)) if best_i >= 0 else None


def count_matches(file_lines: list[str], old: list[str]) -> int:
    """Exact-match count, falling back to eol-tolerant count."""
    hits = _find_all(file_lines, old, lambda l: l)
    if hits:
        return len(hits)
    return len(_find_all(file_lines, old, str.rstrip))


def count_substr(text: str, needle: str) -> int:
    """Number of occurrences of needle in text, OVERLAPPING ones included.

    Overlapping counting is the honest ambiguity measure: "aaa" containing
    "aa" twice is genuinely ambiguous even though str.replace would only
    touch one.
    """
    start = hits = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return hits
        hits += 1
        start = i + 1


def diagnose_substr(text: str, needle: str) -> str | None:
    """Explain WHY an exact substring search failed, when a near miss exists.

    Returns a human/model-readable hint or None. Checks the two relaxations
    of the cascade in order (trailing whitespace, then leading indentation)
    and reports the line number of the unique near-miss — feedback precise
    enough for the model to fix its own edit next turn.
    """
    lines = text.split("\n")
    want = needle.split("\n")
    n = len(want)

    def _scan(key):
        keyed = [key(l) for l in want]
        return [
            i
            for i in range(len(lines) - n + 1)
            if [key(l) for l in lines[i : i + n]] == keyed
        ]

    for name, key in (
        ("ignoring trailing whitespace", str.rstrip),
        ("ignoring leading indentation", str.lstrip),
    ):
        hits = _scan(key)
        if len(hits) == 1:
            where = "" if n == 1 else f" (spanning {n} lines from there)"
            return (
                f"a match exists at line {hits[0] + 1} {name}{where}, "
                "but not byte-exactly"
            )
    return None
