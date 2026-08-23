"""agentpatch CLI: parse and apply coding-agent patches with JSON diagnostics.

Exit codes: 0 = everything applied (or would apply, under --dry-run),
1 = at least one hunk/file failed, 2 = usage, IO, or parse errors.
"""

from __future__ import annotations

import argparse
import json
import sys

from .applier import ApplyError, PatchResult, apply_patch
from .formats import EDITBLOCK, V4A, detect, parse_patch
from .v4a import ParseError

VERSION = "0.2.0"
_FORMAT_CHOICES = ("auto", V4A, EDITBLOCK)


def _read_patch(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _print_text(res: PatchResult) -> None:
    mode = "DRY RUN" if res.dry_run else "APPLIED"
    for f in res.files:
        head = f"{f.kind}: {f.path}"
        if f.renamed_to:
            head += f" -> {f.renamed_to}"
        mark = "ok" if f.status == "ok" else "FAIL"
        print(f"[{mark}] {head} ({mode})")
        if f.message:
            print(f"       {f.message}")
        for h in f.hunks:
            if h.status == "applied":
                extra = f" via {h.strategy}" + (
                    f" (similarity {h.similarity:.2f})" if h.strategy == "fuzzy" else ""
                )
                print(f"  hunk {h.index}: applied at line {h.line_start}{extra}")
            else:
                print(f"  hunk {h.index}: FAILED - {h.message}")
    s = res.to_dict()["summary"]
    print(
        f"summary: {s['files_applied']} file(s) ok, {s['files_failed']} failed; "
        f"{s['hunks_applied']} hunk(s) applied, {s['hunks_failed']} failed"
    )


def _resolve_format(text: str, requested: str) -> str | None:
    return detect(text) if requested == "auto" else (requested or None)


def cmd_parse(args: argparse.Namespace) -> int:
    try:
        text = _read_patch(args.patchfile)
    except OSError as exc:
        print(f"error: cannot read patch: {exc}", file=sys.stderr)
        return 2
    fmt = _resolve_format(text, args.format)
    if args.json:
        if fmt is None:
            print(json.dumps({"error": "unrecognized patch format"}))
            return 2
    if fmt is None:
        print("error: unrecognized patch format", file=sys.stderr)
        return 2
    try:
        patch = parse_patch(text, fmt)
    except ParseError as exc:
        if args.json:
            print(json.dumps({"format": fmt, "error": str(exc)}))
        else:
            print(f"parse error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(patch.to_dict() if hasattr(patch, "to_dict") else _patch_dict(patch), indent=2))
    else:
        for op in patch.ops:
            line = f"{op.kind}: {op.path}"
            if op.rename_to:
                line += f" -> {op.rename_to}"
            if op.hunks:
                line += f" ({len(op.hunks)} hunk(s))"
            print(line)
    return 0


def _patch_dict(patch) -> dict:
    return {
        "format": patch.format,
        "ops": [
            {
                "path": op.path,
                "kind": op.kind,
                "rename_to": op.rename_to,
                "hunks": [
                    {
                        "anchor": h.anchor,
                        "old_lines": h.old_lines,
                        "new_lines": h.new_lines,
                    }
                    for h in op.hunks
                ],
                "add_content": op.add_content,
            }
            for op in patch.ops
        ],
    }


def cmd_apply(args: argparse.Namespace) -> int:
    try:
        text = _read_patch(args.patchfile)
    except OSError as exc:
        print(f"error: cannot read patch: {exc}", file=sys.stderr)
        return 2
    fmt = _resolve_format(text, args.format)
    if fmt is None:
        print("error: unrecognized patch format", file=sys.stderr)
        return 2
    try:
        patch = parse_patch(text, fmt)
    except ParseError as exc:
        print(f"parse error: {exc}", file=sys.stderr)
        return 2
    try:
        res = apply_patch(
            patch, root=args.root, dry_run=args.dry_run, threshold=args.fuzzy_threshold
        )
    except ApplyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        _print_text(res)
    return 0 if res.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentpatch",
        description="Parse and apply coding-agent patches (V4A, SEARCH/REPLACE "
        "blocks) with layered fuzzy matching and structured diagnostics.",
    )
    p.add_argument("--version", action="version", version=f"agentpatch {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_format_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--format",
            choices=_FORMAT_CHOICES,
            default="auto",
            help="force a patch format instead of auto-detection (default: auto)",
        )

    pp = sub.add_parser("parse", help="parse a patch and show its operations")
    pp.add_argument("patchfile", help="path to the patch file, or '-' for stdin")
    pp.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    add_format_arg(pp)
    pp.set_defaults(func=cmd_parse)

    pa = sub.add_parser("apply", help="apply a patch to files under --root")
    pa.add_argument("patchfile", help="path to the patch file, or '-' for stdin")
    pa.add_argument("-C", "--root", default=".", help="root directory (default: cwd)")
    pa.add_argument("--dry-run", action="store_true", help="report without writing")
    pa.add_argument("--fuzzy-threshold", type=float, default=0.85,
                    help="minimum similarity for fuzzy matches (default 0.85)")
    pa.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    add_format_arg(pa)
    pa.set_defaults(func=cmd_apply)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
