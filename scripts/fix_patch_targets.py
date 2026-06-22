#!/usr/bin/env python3
"""Rewrite ``patch("a.b.c")`` targets in tests when a symbol moves modules.

Background (issue #164): the #154 refactor moved ``sandbox_exec`` helpers from
``code_sandbox_mcp.server`` to ``code_sandbox_mcp.tools.exec``.  Every test that
patched ``code_sandbox_mcp.server._docker`` then had to be hand-edited to
``code_sandbox_mcp.tools.exec._docker``.  ``unittest.mock.patch`` resolves its
target lazily, so the stale patches only surfaced at run time.

This codemod is the companion to ``check_patch_targets.py`` (issue #166): rather
than *detecting* drift after a move, it *prevents* it by rewriting the patch
targets as part of the move.  Given one or more ``OLD NEW`` symbol renames it
finds every string ``patch(...)`` target that names ``OLD`` (or an attribute
underneath it) and rewrites the dotted prefix to ``NEW``.

Usage::

    # Preview (default): show what would change, touch nothing.
    python scripts/fix_patch_targets.py \\
        --move code_sandbox_mcp.server._docker code_sandbox_mcp.tools.exec._docker

    # Apply the rewrite in place.
    python scripts/fix_patch_targets.py -w \\
        --move code_sandbox_mcp.server._docker code_sandbox_mcp.tools.exec._docker

Multiple ``--move OLD NEW`` pairs may be given.  Positional paths restrict the
scan (default: ``tests``).  Exit code is non-zero only on error (e.g. a bad
argument); a no-op preview exits 0.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# ``check_patch_targets`` lives next to this script; reuse its AST walker so the
# two tools agree exactly on what counts as a string patch target.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_patch_targets as cpt  # noqa: E402


class Rename:
    """A single ``OLD -> NEW`` dotted-path rename rule."""

    def __init__(self, old: str, new: str) -> None:
        self.old = old
        self.new = new

    def apply(self, target: str) -> str | None:
        """Return the rewritten target, or None when this rule does not match.

        A rule matches ``target`` when it equals ``OLD`` exactly or names an
        attribute underneath it (``OLD.attr``).  The latter handles moving a
        class or module whose members are patched, e.g. moving ``a.b.C`` also
        rewrites ``patch("a.b.C.method")``.  Matching is on whole dotted
        components, so ``a.bc`` is never matched by a rule for ``a.b``.
        """
        if target == self.old:
            return self.new
        if target.startswith(self.old + "."):
            return self.new + target[len(self.old):]
        return None


class Replacement:
    """A pending edit to one string literal in a source file."""

    def __init__(self, lineno: int, old_target: str, new_target: str) -> None:
        self.lineno = lineno
        self.old_target = old_target
        self.new_target = new_target

    def __str__(self) -> str:
        return f"  line {self.lineno}: {self.old_target!r} -> {self.new_target!r}"


def _first_match(target: str, renames: list[Rename]) -> str | None:
    """Return the first rename that applies to ``target`` (in rule order)."""
    for rename in renames:
        rewritten = rename.apply(target)
        if rewritten is not None:
            return rewritten
    return None


def _string_literal_span(node: ast.Constant) -> tuple[int, int, int]:
    """Return ``(lineno, col_offset, end_col_offset)`` of a string literal.

    The span covers the literal *including* its quotes.  Only single-line
    literals are supported; patch targets are always plain one-line strings.
    """
    return node.lineno, node.col_offset, node.end_col_offset


def rewrite_source(source: str, renames: list[Rename]) -> tuple[str, list[Replacement]]:
    """Apply ``renames`` to ``source``, returning ``(new_source, replacements)``.

    The rewrite replaces only the matched string literals; it never reformats
    surrounding code.  The original quote character of each literal is kept.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    # Collect span + new value for each literal to change.
    edits: list[tuple[int, int, int, str, str]] = []
    replacements: list[Replacement] = []
    for target_node in cpt.iter_patch_target_nodes(tree):
        rewritten = _first_match(target_node.value, renames)
        if rewritten is None or rewritten == target_node.value:
            continue
        # Patch targets are always single-line plain strings; skip the
        # pathological multi-line literal so the line-based splice stays safe.
        if target_node.lineno != target_node.end_lineno:
            continue
        lineno, col, end_col = _string_literal_span(target_node)
        edits.append((lineno, col, end_col, target_node.value, rewritten))
        replacements.append(Replacement(lineno, target_node.value, rewritten))

    # Apply edits right-to-left within each line so earlier column offsets stay
    # valid after a length-changing replacement.
    for lineno, col, end_col, old_val, new_val in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        line = lines[lineno - 1]
        literal = line[col:end_col]
        # Detect the quote character(s): triple quotes (''' / """) take priority.
        # Implicit string concatenation ('a.' 'b') is rewritten as a single
        # quoted literal — the concatenation is normalised away.
        if literal.startswith('"""') or literal.startswith("'''"):
            quote = literal[:3]
        elif literal[:1] in ("'", '"'):
            quote = literal[0]
        else:
            quote = '"'
        lines[lineno - 1] = line[:col] + f"{quote}{new_val}{quote}" + line[end_col:]
    return "".join(lines), replacements


def rewrite_file(path: Path, renames: list[Rename], *, write: bool) -> list[Replacement]:
    """Plan (and optionally apply) the rewrite for a single file."""
    source = path.read_text(encoding="utf-8")
    new_source, replacements = rewrite_source(source, renames)
    if write and replacements:
        path.write_text(new_source, encoding="utf-8")
    return replacements


def rewrite_paths(paths: list[Path], renames: list[Rename], *, write: bool) -> dict[Path, list[Replacement]]:
    """Rewrite every ``*.py`` under ``paths``; return per-file replacements."""
    result: dict[Path, list[Replacement]] = {}
    for file in cpt._iter_python_files(paths):
        replacements = rewrite_file(file, renames, write=write)
        if replacements:
            result[file] = replacements
    return result


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--move",
        nargs=2,
        action="append",
        metavar=("OLD", "NEW"),
        dest="renames",
        required=True,
        help="A dotted-path symbol rename, e.g. --move a.b.c x.y.c. Repeatable.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["tests"],
        help="Files or directories to scan (default: tests).",
    )
    parser.add_argument(
        "-w",
        "--write",
        action="store_true",
        help="Apply the rewrite in place (default: preview only).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Warn when a rewritten target does not resolve (uses check_patch_targets).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; rewrite stale patch targets in place."""
    args = _parse_args(argv)
    cpt.ensure_src_importable()

    renames = [Rename(old, new) for old, new in args.renames]
    paths = [Path(p) for p in (args.paths or ["tests"])]
    results = rewrite_paths(paths, renames, write=args.write)

    total = sum(len(r) for r in results.values())
    verb = "Rewrote" if args.write else "Would rewrite"
    for file, replacements in sorted(results.items(), key=lambda kv: str(kv[0])):
        print(f"{file}:")
        for replacement in replacements:
            print(replacement)

    if not total:
        print("No matching patch targets found.")
        return 0

    print(f"\n{verb} {total} patch target(s) across {len(results)} file(s).")
    if not args.write:
        print("Re-run with -w/--write to apply.")

    if args.verify:
        unresolved = [
            (new, reason)
            for replacements in results.values()
            for new in {r.new_target for r in replacements}
            if (reason := cpt.resolve_patch_target(new)) is not None
        ]
        for new, reason in unresolved:
            print(f"warning: rewritten target {new!r} {reason}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
