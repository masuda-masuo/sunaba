"""Affected-test selection for ``verify_in_container`` (Issue #781).

Selects the tests impacted by a change set so the edit loop can run only
those, fast, without weakening the publish gate (an affected run never
reports ``gate_passed``; the full suite is still required before publish).

**Stdlib-only, import-light, standalone.** This module is copied into
arbitrary target containers at verify time and executed with *their*
``python``, so it must not import sunaba or any third-party package.  The
importable API (``select_affected_tests``) is used by the unit tests; the
CLI is the contract used by ``verify_in_container``.

CLI contract::

    python affected_tests.py --root <repo_root> [--deleted PATH]... <changed_path>...

prints exactly one JSON object to stdout and exits 0::

    {"selected": ["tests/test_x.py", ...], "widen_reason": null}

A non-null ``widen_reason`` means "run the full suite instead" -- widening
is expressed in JSON, never via the exit code.

Algorithm
---------

* Build a module-level import graph over all ``*.py`` under the repo root
  by walking the FULL AST (``ast.walk``), so imports inside function
  bodies (deferred imports) are captured.  Relative imports
  (``from . import x``, ``from ..pkg import y``) are resolved to dotted
  module names.
* Map changed ``.py`` files to modules and compute the reverse transitive
  closure: every module that directly or transitively imports a changed
  module is affected.
* Select every test file whose module is in the affected set.  A file
  counts as a test file only inside a known test root: a path component
  named ``tests``/``test``, or a directory (at any depth) containing
  ``conftest.py`` -- mirroring where pytest actually collects.  A
  ``test_*.py``-named *source* module (e.g. ``src/.../test_runners.py``)
  is not executed by an affected run.  A changed test file is always
  selected.  A naming-convention safety net additionally selects
  ``test_y*.py`` for a changed ``.../y.py`` even when the import graph
  missed the connection.
* Widen to full (``widen_reason`` set, always fail open) whenever: the
  change set includes ``conftest.py`` / ``pyproject.toml`` /
  ``setup.cfg`` / ``pytest.ini`` / ``tox.ini``, or any non-``.py`` file;
  a changed ``.py`` file cannot be mapped to a module under the root; the
  change set includes deleted or renamed files (``--deleted``); the
  selection is empty while the change set is non-empty; or a CHANGED file
  fails to parse.  An unrelated unparseable file elsewhere in the tree
  (a deliberately-broken fixture, scratch file) is skipped -- its module
  simply contributes no edges -- and never widens the run, so one broken
  file cannot permanently disable narrowing.  A failure never crashes
  the verify call.

Selection contract (reviewed for Issue #781)
--------------------------------------------

Selection is the TRUE reverse transitive closure over the import graph:
when a changed module is reached by a hub module (a package
``__init__.py`` re-exporting its submodules, a widely-imported module
like a verify entry point), the hub's whole importer population is
selected.  That is deliberate and kept: dropping package-``__init__``
re-export edges would make changes to submodules of re-exporting
packages select nothing (e.g. ``edit_verify/gate.py`` is imported only
by ``edit_verify/__init__.py``, so its changes would always widen),
which is a worse contract.  On hub-dense repositories the affected set
for a hub-adjacent change can therefore be a substantial share of the
suite; for a genuinely leaf change it stays a small fraction.  An
affected run never reports ``gate_passed``, so the mandatory full
verify still guards correctness either way.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from typing import Iterable, Optional

#: Directories never walked when building the import graph.  Copies and
#: vendored trees (``dist/``, ``build/``) would otherwise collide with real
#: modules under the same dotted name (e.g. a stale ``dist/sunaba/x.py``
#: shadowing ``src/sunaba/x.py`` in the module map).
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", "vendor",
    ".tox", "build", "dist", ".eggs", "egg-info", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "htmlcov", "target",
})

#: Basenames whose change forces the full suite: test fixtures and pytest
#: configuration affect collection itself, not individual tests.
_WIDEN_BASENAMES = frozenset({
    "conftest.py", "pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini",
})

#: Directory components that mark a test root (mirrors pytest's common
#: collection layout).  A ``test_*.py`` file outside any test root -- e.g.
#: ``src/pkg/test_runners.py`` -- is a source module, not a test file, and
#: an affected run must not execute it (Issue #781 review).
_TEST_ROOT_DIRS = frozenset({"tests", "test"})


def _is_test_file(root: str, rel_path: str) -> bool:
    """True for ``test_*.py`` / ``*_test.py`` files under a test root.

    A file counts only when it sits under a path component named
    ``tests``/``test``, or under a directory (at any depth, including the
    root) that contains ``conftest.py`` -- the places pytest actually
    collects from.  Basename-only matching would also classify
    ``src/.../test_runners.py`` helper modules as tests and hand them to
    pytest, which imports and collects them (Issue #781 review).
    """
    base = os.path.basename(rel_path)
    if not base.endswith(".py"):
        return False
    if not (base.startswith("test_") or base.endswith("_test.py")):
        return False
    parts = rel_path.split(os.sep)
    if any(p in _TEST_ROOT_DIRS for p in parts[:-1]):
        return True
    directory = os.path.dirname(rel_path)
    while True:
        if os.path.exists(os.path.join(root, directory, "conftest.py")):
            return True
        if not directory:
            break
        directory = os.path.dirname(directory)
    return False


def _iter_py_files(root: str) -> Iterable[tuple[str, str]]:
    """Yield ``(abs_path, root_relative_path)`` for every ``*.py`` under root.

    Walk order is deterministic (sorted) so selection output is stable.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                abs_path = os.path.join(dirpath, fn)
                rel = os.path.relpath(abs_path, root)
                yield abs_path, rel


def _package_root_dirs(root: str) -> set[str]:
    """Importable names of the top-level packages under *root*.

    A directory is a top-level package when it contains ``__init__.py`` and
    no ancestor directory between it and the root does (src-layout
    ``src/`` has no ``__init__.py``, so ``src/sunaba/`` is the top package
    ``sunaba``).  If the root itself is a package (root ``__init__.py``),
    ``{"."}`` is returned and no stripping happens (module names are
    root-relative, which matches absolute imports in that layout).
    """
    if os.path.exists(os.path.join(root, "__init__.py")):
        return {"."}
    tops: set[str] = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        if "__init__.py" not in filenames:
            continue
        rel = os.path.relpath(dirpath, root)
        if rel == ".":
            continue
        parts = rel.split(os.sep)
        for i in range(1, len(parts)):
            if os.path.exists(os.path.join(root, *parts[:i], "__init__.py")):
                break
        else:
            tops.add(parts[-1])
    return tops


def _module_for_file(root: str, rel_path: str, pkg_roots: set[str]) -> Optional[str]:
    """Map a root-relative ``.py`` path to its dotted module name.

    A file under a top-level package (see :func:`_package_root_dirs`) is
    named by its path relative to that package (``src/sunaba/x.py`` ->
    ``sunaba.x``); any other file is named root-relative (``scripts/f.py``
    -> ``scripts.f``).  Returns ``None`` when the file cannot be mapped
    (root-level ``__init__.py``, non-``.py``) -- a changed unmappable file
    must widen.
    """
    if not rel_path.endswith(".py"):
        return None
    stem = rel_path[:-3]
    rel_dir = os.path.dirname(rel_path)
    base = os.path.basename(stem)
    dir_parts = rel_dir.split(os.sep) if rel_dir else []
    if "." in pkg_roots:
        # The root itself is the package: names are root-relative, which
        # matches absolute imports in that layout.  root/__init__.py has no
        # importable name at all.
        mod_parts = list(dir_parts)
        if base != "__init__":
            mod_parts.append(base)
        return ".".join(mod_parts) if mod_parts else None
    for pkg_root in sorted(pkg_roots, key=lambda p: len(p.split(os.sep)), reverse=True):
        pkg_parts = pkg_root.split(os.sep)
        for i in range(len(dir_parts) - len(pkg_parts) + 1):
            if dir_parts[i:i + len(pkg_parts)] == pkg_parts:
                mod_parts = list(pkg_parts) + dir_parts[i + len(pkg_parts):]
                if base != "__init__":
                    mod_parts.append(base)
                return ".".join(mod_parts)
    # Not under any package (root-level script, tests/ without __init__):
    # root-relative dotted name, consistent for graph-internal edges.
    mod_parts = list(dir_parts)
    if base != "__init__":
        mod_parts.append(base)
    return ".".join(mod_parts) if mod_parts else None


def _with_prefixes(dotted: str) -> list[str]:
    """``a.b.c`` -> ``[a.b.c, a.b, a]``.

    ``import a.b.c`` executes ``a``, ``a.b`` and ``a.b.c``, so all three
    are imported by the importing module.
    """
    parts = dotted.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def _resolve_relative(module_name: str, level: int, imported: Optional[str], is_init: bool) -> list[str]:
    """Resolve ``from {'.' * level}[imported] import ...`` to dotted names."""
    parts = module_name.split(".")
    if is_init:
        base = parts
    else:
        base = parts[:-1] if len(parts) > 1 else []
    if level > 1:
        if level - 1 > len(base):
            return []
        base = base[: -(level - 1)]
    if not base and not imported:
        return []
    name = ".".join(base + ([imported] if imported else []))
    return _with_prefixes(name) if name else []


def _module_deps(module_name: str, rel_path: str, tree: ast.AST) -> set[str]:
    """All dotted module names *module_name* imports (full AST walk)."""
    deps: set[str] = set()
    is_init = os.path.basename(rel_path) == "__init__.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps.update(_with_prefixes(alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                # Absolute import: resolve against the module namespace
                # directly (e.g. ``from sunaba.tools import verify``).
                if node.module:
                    base_names = _with_prefixes(node.module)
                    for base in base_names:
                        deps.add(base)
                        for alias in node.names:
                            deps.add(f"{base}.{alias.name}")
                continue
            resolved = _resolve_relative(module_name, node.level, node.module, is_init)
            for base in resolved:
                deps.add(base)
                for alias in node.names:
                    deps.add(f"{base}.{alias.name}")
    return deps


def _build_graph(root: str) -> tuple[dict[str, set[str]], dict[str, str], list[tuple[str, str]]]:
    """Return ``(imports, module_to_file, parse_errors)``.

    ``imports[mod]`` is the set of dotted module names ``mod`` imports;
    ``module_to_file[mod]`` is the module's root-relative path.
    ``parse_errors`` is a list of ``(rel_path, message)`` for files that
    could not be parsed.  Such a file is skipped (it contributes no
    edges) rather than raised; only a *changed* file failing to parse
    widens to full (see :func:`select_affected_tests`), so one broken
    unrelated file cannot permanently disable narrowing.
    """
    imports: dict[str, set[str]] = {}
    module_to_file: dict[str, str] = {}
    parse_errors: list[tuple[str, str]] = []
    pkg_roots = _package_root_dirs(root)
    for abs_path, rel in _iter_py_files(root):
        mod = _module_for_file(root, rel, pkg_roots)
        if mod is None:
            continue
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                tree = ast.parse(fh.read(), filename=abs_path)
        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            parse_errors.append((rel, str(e)))
            continue
        imports[mod] = _module_deps(mod, rel, tree)
        module_to_file[mod] = rel
    return imports, module_to_file, parse_errors


def _reverse_closure(changed_modules: list[str], imports: dict[str, set[str]]) -> set[str]:
    """All modules that directly or transitively import a changed module."""
    affected = set(changed_modules)
    grew = True
    while grew:
        grew = False
        for mod, deps in imports.items():
            if mod not in affected and deps & affected:
                affected.add(mod)
                grew = True
    return affected


def select_affected_tests(
    root: str,
    changed_paths: list[str],
    deleted_paths: Optional[list[str]] = None,
) -> dict:
    """Select tests affected by *changed_paths* under *root*.

    Returns ``{"selected": [root-relative test paths], "widen_reason": str|None}``.
    A non-null ``widen_reason`` means the caller should run the full suite
    (fail-open); ``selected`` is then empty.
    """
    changed = [os.path.normpath(p) for p in changed_paths]
    deleted = [os.path.normpath(p) for p in (deleted_paths or [])]

    # --- Widening triggers that need no analysis ---------------------------
    if deleted:
        return {"selected": [], "widen_reason": "change set includes deleted or renamed files"}
    if not changed:
        return {"selected": [], "widen_reason": "change set is empty"}
    for p in changed:
        base = os.path.basename(p)
        if base in _WIDEN_BASENAMES:
            return {"selected": [], "widen_reason": f"change set includes {p}"}
        if not p.endswith(".py"):
            return {"selected": [], "widen_reason": f"change set includes non-.py file {p}"}
    for p in changed:
        full = p if os.path.isabs(p) else os.path.join(root, p)
        if not os.path.exists(full):
            return {"selected": [], "widen_reason": f"changed file not found on disk (deleted?): {p}"}
        if os.path.commonpath([os.path.realpath(full), os.path.realpath(root)]) != os.path.realpath(root):
            return {"selected": [], "widen_reason": f"changed file outside repo root: {p}"}

    # --- Import graph ------------------------------------------------------
    imports, module_to_file, parse_errors = _build_graph(root)
    changed_rels = set()
    for p in changed:
        rel = p if os.path.isabs(p) else os.path.relpath(p, root)
        changed_rels.add(os.path.normpath(rel))
    for rel, msg in parse_errors:
        if os.path.normpath(rel) in changed_rels:
            return {
                "selected": [],
                "widen_reason": f"analysis failure in changed file {rel}: {msg}",
            }
    # Unrelated unparseable files (broken fixtures, scratch files) were
    # skipped by _build_graph and do NOT widen: narrowing must stay usable
    # even when the tree contains intentionally-broken files.
    pkg_roots = _package_root_dirs(root)

    changed_modules: list[str] = []
    for p in changed:
        rel = p if os.path.isabs(p) else os.path.relpath(p, root)
        mod = _module_for_file(root, rel, pkg_roots)
        if mod is None or mod not in imports:
            return {"selected": [], "widen_reason": f"cannot map changed file {p} to a module"}
        changed_modules.append(mod)

    # --- Reverse transitive closure ----------------------------------------
    affected = _reverse_closure(changed_modules, imports)

    # --- Select test files ---------------------------------------------------
    selected: list[str] = []
    for mod in sorted(affected):
        rel = module_to_file.get(mod)
        if rel and _is_test_file(root, rel):
            selected.append(rel)

    # A changed test file is always selected, even when the import graph
    # lost the connection (e.g. it imports nothing at all).
    for mod in changed_modules:
        rel = module_to_file.get(mod)
        if rel and _is_test_file(root, rel) and rel not in selected:
            selected.append(rel)

    # Naming-convention safety net: a changed ``.../y.py`` also selects any
    # ``test_y*.py``, even when the graph missed the relationship.
    for p in changed:
        stem = os.path.splitext(os.path.basename(p))[0]
        for rel in module_to_file.values():
            if _is_test_file(root, rel) and os.path.basename(rel).startswith(f"test_{stem}"):
                if rel not in selected:
                    selected.append(rel)

    selected = sorted(selected)
    if not selected:
        return {"selected": [], "widen_reason": "no tests selected for the change set"}
    return {"selected": selected, "widen_reason": None}


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: prints one JSON object and exits 0."""
    parser = argparse.ArgumentParser(
        prog="affected_tests",
        description="Select tests affected by a change set (Issue #781).",
    )
    parser.add_argument("--root", required=True, help="repo root to analyze")
    parser.add_argument(
        "--deleted", action="append", default=[], metavar="PATH",
        help="deleted or renamed path (repeatable); forces widen-to-full",
    )
    parser.add_argument("changed", nargs="*", metavar="PATH", help="changed paths")
    args = parser.parse_args(argv)
    result = select_affected_tests(args.root, args.changed, args.deleted)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
