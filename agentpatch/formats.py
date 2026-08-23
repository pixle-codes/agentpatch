"""Format detection and parser dispatch.

detect() inspects the first meaningful lines of a patch and returns one of
the registered format names; parse_patch() turns text into a Patch via the
right parser. Adding a format = a parser module exposing parse_patch() +
detect_format(), plus one entry in _PARSERS.
"""

from __future__ import annotations

from . import editblock, udiff, v4a
from .v4a import ParseError, Patch

V4A = "v4a"
EDITBLOCK = "editblock"
UDIFF = "udiff"

_PARSERS = {
    V4A: v4a.parse_patch,
    EDITBLOCK: editblock.parse_patch,
    UDIFF: udiff.parse_patch,
}
_DETECTORS = {
    V4A: v4a.detect_format,
    EDITBLOCK: editblock.detect_format,
    UDIFF: udiff.detect_format,
}


def detect(text: str) -> str | None:
    for name in (V4A, EDITBLOCK, UDIFF):
        fmt = _DETECTORS[name](text)
        if fmt:
            return fmt
    return None


def parse_patch(text: str, fmt: str | None = None) -> Patch:
    """Parse with an explicit format or auto-detection."""
    if fmt is None:
        fmt = detect(text)
    if fmt is None:
        raise ParseError("unrecognized patch format")
    parser = _PARSERS.get(fmt)
    if parser is None:
        raise ParseError(f"unknown format {fmt!r}")
    return parser(text)
