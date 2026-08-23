"""agentpatch — parse and apply coding-agent patches reliably.

Zero-dependency engine that turns model-emitted edits (OpenAI V4A patches
today; SEARCH/REPLACE blocks and unified diffs on the roadmap) into file
changes, with a layered fuzzy-matching cascade and structured per-hunk
diagnostics designed to be fed back to the model.
"""

from .applier import ApplyError, FileResult, HunkResult, PatchResult, apply_patch
from .matcher import DEFAULT_THRESHOLD
from .v4a import (
    ADD,
    DELETE,
    UPDATE,
    FileOp,
    Hunk,
    InvalidPath,
    ParseError,
    Patch,
    detect_format,
    parse_patch,
)

VERSION = "0.1.0"

__all__ = [
    "ADD",
    "DELETE",
    "UPDATE",
    "ApplyError",
    "DEFAULT_THRESHOLD",
    "FileOp",
    "FileResult",
    "Hunk",
    "HunkResult",
    "InvalidPath",
    "ParseError",
    "Patch",
    "PatchResult",
    "VERSION",
    "apply_patch",
    "detect_format",
    "parse_patch",
]
