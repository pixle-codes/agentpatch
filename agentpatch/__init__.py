"""agentpatch — parse and apply coding-agent patches reliably.

Zero-dependency engine that turns model-emitted edits (OpenAI V4A patches,
aider/Cline/Roo SEARCH/REPLACE blocks) into file changes, with a layered
fuzzy-matching cascade and structured per-hunk diagnostics designed to be
fed back to the model.
"""

from .applier import ApplyError, FileResult, HunkResult, PatchResult, apply_patch
from .formats import EDITBLOCK, STREDIT, V4A, detect, parse_patch
from .matcher import DEFAULT_THRESHOLD
from .v4a import (
    ADD,
    DELETE,
    SUBSTR,
    UPDATE,
    FileOp,
    Hunk,
    InvalidPath,
    ParseError,
    Patch,
)

VERSION = "1.0.0"

__all__ = [
    "ADD",
    "DELETE",
    "EDITBLOCK",
    "STREDIT",
    "SUBSTR",
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
    "V4A",
    "VERSION",
    "apply_patch",
    "detect",
    "parse_patch",
]
