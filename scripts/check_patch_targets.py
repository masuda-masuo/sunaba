#!/usr/bin/env python3
"""Lint that every ``patch("a.b.c")`` target actually exists.

Background (issue #166): after the #154 refactor a test kept patching
``code_sandbox_mcp.server._docker`` even though ``_docker`` had moved to a
different module.  ``unittest.mock.patch`` resolves its target lazily, so the
stale target was only discovered when the test ran.  This checker resolves
every string ``patch(...)`` target statically (the same way ``mock`` does at
runtime) and fails when the attribute is missing, so the drift is caught in CI.

Usage::

    python scripts/check_patch_targets.py [PATH ...]

With no arguments it scans ``tests``.  Exit code is non-zero when any target
cannot be resolved.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import sys
from pathlib import Path


class PatchTargetError:
    """A single unresolved patch target."""

    def __init__(self, path: Path, lineno: int, target: str, reason: str) -> None:
        self.path = path
        self.lineno = lineno
        self.target = target
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: patch target {self.target!r} {self.reason}"


def _is_patch_call(func: ast.expr) -> bool:
    """Return True for ``patch(...)`` / ``mock.patch(...)`` style calls.

    ``patch.object(...)`` / ``patch.dict(...)`` / ``patch.multiple(...)`` are
    excluded automatically: their ``func`` node has ``attr="object"`` (etc.),
    not ``attr="patch"``, so the check below is False.
    """
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def iter_patch_target_nodes(tree: ast.AST):
    """Yield the ``ast.Constant`` node of every string ``patch(...)`` target.

    Callers that only need the value/lineno can use :func:`iter_patch_targets`;
    the codemod in ``fix_patch_targets.py`` needs the node itself for its source
    column span, so the node-level walk lives here as the single source of truth.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_patch_call(node.func):
            continue
        target_node: ast.expr | None = None
        if node.args:
            target_node = node.args[0]
        else:
            for kw in node.keywords:
                if kw.arg == "target":
                    target_node = kw.value
                    break
        if isinstance(target_node, ast.Constant) and isinstance(target_node.value, str):
            yield target_node


def iter_patch_targets(tree: ast.AST):
    """Yield ``(lineno, target)`` for every string ``patch(...)`` target."""
    for node in iter_patch_target_nodes(tree):
        yield node.lineno, node.value


def _dot_lookup(thing, comp: str, import_path: str):
    """Resolve ``comp`` on ``thing``, importing ``import_path`` if needed.

    Mirrors ``unittest.mock._dot_lookup`` so resolution matches runtime patch
    behaviour exactly (a submodule is imported only when attribute access fails).
    """
    try:
        return getattr(thing, comp)
    except AttributeError:
        importlib.import_module(import_path)
        return getattr(thing, comp)


def _import_target_owner(dotted: str):
    """Import and return the object that owns the final patched attribute.

    ``dotted`` is the patch target with its last component stripped, e.g. for
    ``a.b.C.method`` it is ``a.b.C`` and the returned object is class ``C``.
    Mirrors ``unittest.mock._importer``.
    """
    components = dotted.split(".")
    import_path = components.pop(0)
    thing = importlib.import_module(import_path)
    for comp in components:
        import_path += f".{comp}"
        thing = _dot_lookup(thing, comp, import_path)
    return thing


def resolve_patch_target(target: str) -> str | None:
    """Return None when ``target`` resolves, else a human-readable reason.

    .. note::
        Resolution imports the owner module (and its transitive dependencies),
        which executes module-level code.  Side effects such as DB connections
        or network calls in module scope will run.  In a typical CI environment
        this is acceptable; avoid scanning untrusted third-party code with
        side-effectful module initialisation.
    """
    module_path, _, attribute = target.rpartition(".")
    if not module_path:
        return "is not a dotted path (expected 'module.attr')"
    try:
        owner = _import_target_owner(module_path)
    except ImportError as exc:
        return f"could not import {module_path!r} ({exc})"
    except AttributeError as exc:
        return f"could not resolve {module_path!r} ({exc})"
    if not hasattr(owner, attribute):
        return f"-- {attribute!r} not found in {module_path!r}"
    return None


def check_file(path: Path) -> list[PatchTargetError]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    errors: list[PatchTargetError] = []
    for lineno, target in iter_patch_targets(tree):
        reason = resolve_patch_target(target)
        if reason is not None:
            errors.append(PatchTargetError(path, lineno, target, reason))
    return errors


def _iter_python_files(paths: list[Path]):
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.py"))
        elif path.suffix == ".py":
            yield path


def check_paths(paths: list[Path]) -> list[PatchTargetError]:
    errors: list[PatchTargetError] = []
    for file in _iter_python_files(paths):
        errors.extend(check_file(file))
    return errors


def _ensure_src_importable() -> None:
    """Make ``src`` importable when running from a checkout without install."""
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=["tests"],
        help="Files or directories to scan (default: tests).",
    )
    args = parser.parse_args(argv)
    _ensure_src_importable()

    paths = [Path(p) for p in (args.paths or ["tests"])]
    errors = check_paths(paths)
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} unresolved patch target(s) found.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
