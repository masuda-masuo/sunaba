#!/usr/bin/env python3
"""Check that patch targets actually bind at runtime by temporarily sabotaging definitions.

For each patch target relevant to a change:
1. Resolve the target to its actual definition site (file + line of the function)
2. Temporarily inject ``raise AssertionError("sabotage-check")`` at the top
3. Run only the tests that patch that symbol
4. If the tests pass, the patch binds — OK
5. If any test fails, the real implementation is being called — report a defect
6. Always restore the mutated file to its original content

Tests that are **subjects** (they call the symbol directly) are detected and
reported as excluded; they are never run under mutation.  Tests that both patch
a symbol and call it are classified as patchers and run.

Patches found only at module level (autouse fixtures, ``setUp``) cannot be
attributed to a specific test.  When test-level patchers exist they are run by
nodeid (fixtures in the file still apply); when no test-level patcher exists
the target is skipped with a reason rather than running the file wholesale —
running the file would include subject tests, which fail by definition under
mutation and would produce false-positive defects.

If the process is terminated while an injection is in flight (SIGTERM or
SIGINT), a signal handler restores the mutated file before the process exits.
SIGKILL cannot be covered -- the kernel terminates the process without running
any Python code -- which is exactly why the real-repo test runs against a
disposable checkout of HEAD rather than the live working tree.

Usage::

    # Check targets relevant to the current diff against origin/main:
    python scripts/check_patch_sabotage.py

    # Check against an explicit base ref (HEAD -> nothing to check):
    python scripts/check_patch_sabotage.py --base HEAD

    # Explicit file list (no git dependency):
    python scripts/check_patch_sabotage.py --files impl.py,tests/test_impl.py

    # Point at a fixture mini-project:
    python scripts/check_patch_sabotage.py --root /tmp/fixture --files impl.py,test.py

    # Machine-readable output:
    python scripts/check_patch_sabotage.py --json
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

# Reuse the walker/resolution from the sibling checker (same pattern as
# fix_patch_targets.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_patch_targets as cpt  # noqa: E402

SABOTAGE_MESSAGE = "sabotage-check"
SABOTAGE_LINE = f"raise AssertionError({SABOTAGE_MESSAGE!r})"

# The currently-injected source file and its pre-injection content, or None
# when no mutation is in flight.  The SIGTERM/SIGINT handler restores from
# this so an interrupted run cannot leave a sabotage raise behind.
_ACTIVE_INJECTION: tuple[Path, str] | None = None


# ── Patch-target extraction (extends cpt to also cover monkeypatch.setattr) ──

def _is_monkeypatch_setattr_call(func: ast.expr) -> bool:
    """Return True for ``monkeypatch.setattr(...)`` calls."""
    if isinstance(func, ast.Attribute) and func.attr == "setattr":
        if isinstance(func.value, ast.Name) and func.value.id == "monkeypatch":
            return True
    return False


def _extract_string_first_arg(node: ast.Call) -> str | None:
    """Extract first positional argument if it is a string constant."""
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _extract_patch_target_string(node: ast.Call, *, func_check) -> str | None:
    """Extract the target string from a ``patch(...)`` or ``monkeypatch.setattr`` call.

    Returns the string literal from the first positional arg or the ``target``
    keyword arg, whichever is a plain string.
    """
    if not isinstance(node, ast.Call) or not func_check(node.func):
        return None
    if node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    for kw in node.keywords:
        if kw.arg == "target":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
    return None


def iter_all_patch_targets(
    tree: ast.AST,
) -> list[tuple[int, str]]:
    """Return ``(lineno, target)`` for every string patch/setattr target in *tree*.

    Covers ``patch("dotted.path")`` (decorator, context-manager, inline) and
    ``monkeypatch.setattr("dotted.path", ...)``.
    """
    results: list[tuple[int, str]] = [
        (lineno, str(target)) for lineno, target in cpt.iter_patch_targets(tree)
    ]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _extract_patch_target_string(node, func_check=_is_monkeypatch_setattr_call)
        if target is not None:
            results.append((node.lineno, target))
    return results


# ── Definition-site discovery ────────────────────────────────────────────────

def get_target_object(target: str) -> object | None:
    """Resolve a dotted patch target to its actual Python object, or *None*."""
    module_path, _, attribute = target.rpartition(".")
    if not module_path:
        return None
    try:
        owner = cpt._import_target_owner(module_path)
        return getattr(owner, attribute)
    except (ImportError, AttributeError):
        return None


def find_definition_file(obj: object) -> Path | None:
    """Return the source file defining *obj*, or *None* for builtins / C extensions."""
    try:
        return Path(inspect.getfile(cast(Any, obj))).resolve()
    except (TypeError, OSError):
        return None


def is_patchable_callable(obj: object) -> bool:
    """Return True when *obj* is a plain function or method (not class / builtin / module)."""
    if not callable(obj):
        return False
    if inspect.isclass(obj) or inspect.isbuiltin(obj) or inspect.ismodule(obj):
        return False
    return True


def find_func_node_in_source(
    source: str, name: str, source_lineno: int
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Locate the AST ``FunctionDef`` / ``AsyncFunctionDef`` node for *name*.

    *source_lineno* is the first line from ``inspect.getsourcelines``, which
    starts at the first decorator (if any).  The node's effective start is
    ``decorator_list[0].lineno`` or ``node.lineno``.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                effective = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                if effective == source_lineno:
                    return node
    return None


# ── Sabotage injection ───────────────────────────────────────────────────────

def inject_sabotage(
    source: str, func_node: ast.FunctionDef | ast.AsyncFunctionDef
) -> str:
    """Return *source* with a sabotage ``raise`` at the top of *func_node*'s body.

    The raise is inserted after any docstring.
    """
    lines = source.splitlines(keepends=True)

    if not func_node.body:
        return source

    first_stmt = func_node.body[0]
    insert_lineno = first_stmt.lineno  # 1-based

    # Indent from the first body statement
    first_line = lines[insert_lineno - 1]
    indent_len = len(first_line) - len(first_line.lstrip())
    indent = first_line[:indent_len]

    # Skip a docstring (string expression as first statement)
    if (
        isinstance(first_stmt, ast.Expr)
        and isinstance(first_stmt.value, ast.Constant)
        and isinstance(first_stmt.value.value, str)
    ):
        insert_lineno = (first_stmt.end_lineno or first_stmt.lineno) + 1

    lines.insert(insert_lineno - 1, f"{indent}{SABOTAGE_LINE}\n")
    return "".join(lines)


def _handle_termination_signal(signum: int, frame: Any) -> None:
    """Restore a currently-injected file, then die with the default action.

    Best-effort: the ``finally`` restore in :func:`check_target` is the
    reliable primary path; this handler only covers the window where the
    process is killed mid-injection.  SIGKILL cannot be covered -- the kernel
    terminates the process without running any Python code -- which is exactly
    why the real-repo test runs against a disposable checkout of HEAD rather
    than the live working tree.
    """
    active = _ACTIVE_INJECTION
    if active is not None:
        path, original = active
        try:
            path.write_text(original, encoding="utf-8")
        except Exception as restore_exc:
            # The main thread is suspended mid-injection, so this write is the
            # only restore that will happen -- the ``finally`` path in
            # check_target never runs.  Nothing is left to retry, but a failed
            # restore leaves the sabotage raise behind and must be loud.
            print(
                f"CRITICAL: failed to restore {path} after sabotage: {restore_exc}",
                file=sys.stderr,
            )
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# ── Test classification ──────────────────────────────────────────────────────

def _enclosing_test_names(tree: ast.AST, node: ast.expr) -> list[str]:
    """Return the qualified test-function names that enclose *node* (innermost first).

    Qualified names are ``ClassName.test_name`` for methods, ``test_name``
    otherwise.
    """
    result: list[str] = []
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not parent.name.startswith("test_"):
            continue
        if _func_effective_start(parent) <= node.lineno <= (parent.end_lineno or 999_999):
            # Walk up to check for enclosing class
            enclosing_class: str | None = None
            for cls_node in ast.walk(tree):
                if isinstance(cls_node, ast.ClassDef):
                    if cls_node.lineno <= parent.lineno <= (cls_node.end_lineno or 999_999):
                        enclosing_class = cls_node.name
            if enclosing_class:
                result.append(f"{enclosing_class}.{parent.name}")
            else:
                result.append(parent.name)
    # Innermost first
    result.sort(key=_test_func_scope_size(tree))
    return result


def _test_func_scope_size(tree: ast.AST):
    """Return a sort key: the span (end_lineno - lineno) of the enclosing test function."""
    _cache: dict[str, int] = {}

    def key(name: str) -> int:
        if name in _cache:
            return _cache[name]
        func_name = name.split(".")[-1]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == func_name:
                    _cache[name] = (node.end_lineno or 0) - node.lineno
                    return _cache[name]
        _cache[name] = 0
        return 0

    return key


def _func_effective_start(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Return the start line including decorators."""
    if func_node.decorator_list:
        return func_node.decorator_list[0].lineno
    return func_node.lineno


def _has_module_level_patch_for_target(tree: ast.AST, target: str) -> bool:
    """Return True when there is a module-level (non-function-scoped) patch for *target*."""
    test_funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    ]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Is it inside any test function (including decorator range)?
        inside = any(
            _func_effective_start(f) <= node.lineno <= (f.end_lineno or 999_999)
            for f in test_funcs
        )
        if inside:
            continue

        ts = _extract_patch_target_string(node, func_check=cpt._is_patch_call)
        if ts is None:
            ts = _extract_patch_target_string(node, func_check=_is_monkeypatch_setattr_call)
        if ts == target:
            return True

    return False


def _add_subject(
    tree: ast.AST, func_node: ast.FunctionDef | ast.AsyncFunctionDef, subjects: set[str]
) -> None:
    """Add the qualified name of *func_node* to *subjects*."""
    qualified = str(func_node.name)
    for cls_node in ast.walk(tree):
        if isinstance(cls_node, ast.ClassDef):
            if cls_node.lineno <= func_node.lineno <= (cls_node.end_lineno or 999_999):
                qualified = f"{cls_node.name}.{func_node.name}"
    subjects.add(qualified)


def classify_tests(test_file: Path, target: str) -> tuple[list[str], list[str]]:
    """Return ``(patcher_names, subject_names)`` for *test_file* and *target*.

    Names are qualified (``ClassName.test_name`` or ``test_name``).  Tests that
    are both patchers and subjects are classified as patchers.
    """
    source = test_file.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(test_file))

    patchers: set[str] = set()

    # Walk all Calls looking for patch/setattr for this target
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        ts = _extract_patch_target_string(node, func_check=cpt._is_patch_call)
        if ts is None:
            ts = _extract_patch_target_string(node, func_check=_is_monkeypatch_setattr_call)
        if ts != target:
            continue

        for name in _enclosing_test_names(tree, node):
            patchers.add(name)

    # Also check decorators on test functions
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            ts = _extract_patch_target_string(decorator, func_check=cpt._is_patch_call)
            if ts == target:
                patchers.add(str(node.name))

    # ── Subject detection ────────────────────────────────────────────────────
    symbol_name = target.rpartition(".")[2]
    symbol_module = target.rpartition(".")[0]

    subjects: set[str] = set()
    imports_symbol = False
    # Local names that may refer to the symbol: the bare name plus any import
    # aliases (e.g. ``from impl import compute as calc`` -> "calc",
    # ``import impl.compute as c`` -> "c").
    local_names: set[str] = {symbol_name}

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == symbol_module:
                for alias in node.names:
                    if alias.name == symbol_name or alias.name == "*":
                        imports_symbol = True
                        if alias.name != "*":
                            local_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == symbol_module or alias.name == target:
                    imports_symbol = True
                    if alias.name == target:
                        local_names.add(alias.asname or symbol_name)

    if imports_symbol:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("test_"):
                    continue
                for child in ast.walk(node):
                    # A subject *calls* the symbol.  A bare reference (e.g.
                    # ``assert compute is not None``) never reaches the
                    # sabotage raise, so only call position counts.
                    if not isinstance(child, ast.Call):
                        continue
                    func = child.func
                    if isinstance(func, ast.Name) and func.id in local_names:
                        _add_subject(tree, node, subjects)
                        break
                    # Also detect module.symbol calls (e.g. impl.compute())
                    if isinstance(func, ast.Attribute) and func.attr == symbol_name:
                        _add_subject(tree, node, subjects)
                        break

    # Patchers take priority over subjects
    subjects = subjects - patchers

    return sorted(patchers), sorted(subjects)


# ── Git helpers ───────────────────────────────────────────────────────────────

def _get_merge_base(root: Path, base_ref: str | None) -> str:
    """Determine the merge-base ref for ``git diff``."""
    if base_ref is not None:
        return base_ref

    for remote in ["origin/main", "main"]:
        try:
            result = subprocess.run(
                ["git", "merge-base", "HEAD", remote],
                capture_output=True, text=True, cwd=root, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            continue
    return "main"


def _get_changed_files(root: Path, base: str) -> list[Path] | None:
    """Return changed-file paths from ``git diff``, or *None* when git fails.

    A git failure must not look like "no changes" — this is a checker, and a
    silently empty change set would report green without checking anything.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", base],
            capture_output=True, text=True, cwd=root, timeout=10,
        )
    except Exception as exc:
        print(f"git diff --name-only {base} failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"git diff --name-only {base} failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    return [root / line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


# ── Main pipeline ────────────────────────────────────────────────────────────

def _ensure_root_importable(root: Path) -> None:
    """Add *root* to ``sys.path`` so that fixture modules can be imported."""
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    # Also ensure src is importable (cpt does this, but be explicit)
    cpt.ensure_src_importable()


def collect_candidates(
    root: Path, changed_files: list[Path]
) -> dict[str, list[Path]]:
    """Return ``{target: [test_file, ...]}`` for patch targets relevant to the change.

    A target is relevant when:
    1. It appears in a changed test file, or
    2. Its resolved definition file is among the changed files.
    """
    test_dir = root / "tests"
    if not test_dir.is_dir():
        return {}

    changed_set = set(changed_files)

    # target -> {test_file, ...}
    target_files: dict[str, set[Path]] = {}
    for py_file in sorted(test_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        for _, target in iter_all_patch_targets(tree):
            target_files.setdefault(target, set()).add(py_file)

    candidates: dict[str, list[Path]] = {}
    for target, files in target_files.items():
        # Category A: test file is among the changed files
        if any(f in changed_set for f in files):
            candidates[target] = sorted(files)
            continue

        # Category B: resolved definition file is a changed file
        obj = get_target_object(target)
        if obj is not None:
            def_file = find_definition_file(obj)
            if def_file is not None and def_file in changed_set:
                candidates[target] = sorted(files)

    return candidates


def check_target(
    root: Path, target: str, test_files: list[Path], timeout: int
) -> dict:
    """Sabotage *target*'s definition and run patcher tests.

    Returns a result dict with keys: target, defect, skipped, skipped_reason,
    patcher_tests, subject_tests, error, module_level.
    """
    global _ACTIVE_INJECTION
    result: dict = {
        "target": target,
        "defect": False,
        "skipped": False,
        "skipped_reason": None,
        "patcher_tests": [],
        "subject_tests": [],
        "module_level": False,
        "error": None,
    }

    # ── Resolve definition site ──────────────────────────────────────────
    obj = get_target_object(target)
    if obj is None:
        reason = cpt.resolve_patch_target(target) or "target does not resolve"
        result["skipped"] = True
        result["skipped_reason"] = reason
        return result

    if not is_patchable_callable(obj):
        kind = type(obj).__name__
        result["skipped"] = True
        result["skipped_reason"] = f"not a patchable callable ({kind})"
        return result

    def_file = find_definition_file(obj)
    if def_file is None:
        result["skipped"] = True
        result["skipped_reason"] = "built-in or C extension; cannot locate source"
        return result

    if not def_file.is_relative_to(root):
        result["skipped"] = True
        result["skipped_reason"] = f"definition outside project root ({def_file})"
        return result

    try:
        _, source_lineno = inspect.getsourcelines(cast(Any, obj))
    except (TypeError, OSError) as e:
        result["skipped"] = True
        result["skipped_reason"] = f"cannot read source: {e}"
        return result

    # ── Classify tests ───────────────────────────────────────────────────
    all_patchers: set[str] = set()
    all_subjects: set[str] = set()
    module_level_files: list[Path] = []
    file_patchers: dict[Path, set[str]] = {}

    for tf in test_files:
        source = tf.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(tf))
        if _has_module_level_patch_for_target(tree, target):
            module_level_files.append(tf)
        p, s = classify_tests(tf, target)
        file_patchers[tf] = set(p)
        all_patchers.update(p)
        all_subjects.update(s)

    all_subjects = all_subjects - all_patchers  # patchers take priority

    result["patcher_tests"] = sorted(all_patchers)
    result["subject_tests"] = sorted(all_subjects)

    result["module_level"] = bool(module_level_files)

    # ── Build pytest arguments ───────────────────────────────────────────
    pytest_args: list[str] = []

    if all_patchers:
        # Run only the attributable patcher tests by nodeid.  Even when a
        # module-level patch exists, nodeids are more precise: the patch is
        # verified through the tests that declare it, and any fixture in the
        # file still applies to the selected tests.  Nodeids are built per
        # file, so files that only host module-level patches (e.g. a conftest
        # with an autouse fixture) never produce bogus nodeids.
        for tf in test_files:
            rel = str(tf.relative_to(root))
            for name in sorted(file_patchers.get(tf, ())):
                pytest_args.append(rel + "::" + name.replace(".", "::"))
    else:
        # Only module-level (fixture/setUp) patches exist for this target.
        # The patch cannot be attributed to a specific test, and running the
        # file wholesale would run subject tests, which fail by definition
        # under mutation and would produce false-positive defects (measured
        # false-positive class 2).  With no patchers there is nothing we can
        # run as evidence, so the target is skipped.
        result["skipped"] = True
        result["skipped_reason"] = (
            "patch appears only at module level (fixture/setUp); no test-level "
            "patchers to run, and subject tests are never run as evidence"
        )
        return result

    # ── Mutate and run ───────────────────────────────────────────────────
    original_source = def_file.read_text(encoding="utf-8")

    try:
        # Parse and locate the function node
        tree = ast.parse(original_source, filename=str(def_file))
        func_node = find_func_node_in_source(
            original_source, target.rpartition(".")[2], source_lineno
        )
        if func_node is None:
            result["error"] = (
                f"Could not locate function definition for {target} in {def_file}"
            )
            return result

        # Inject sabotage
        mutated_source = inject_sabotage(original_source, func_node)
        _ACTIVE_INJECTION = (def_file, original_source)
        def_file.write_text(mutated_source, encoding="utf-8")

        # Run patcher tests
        cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "--tb=short"]
        cmd.extend(pytest_args)

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=root,
            timeout=timeout,
        )

        if proc.returncode != 0:
            result["defect"] = True
            result["error"] = (
                f"pytest exit={proc.returncode}\n"
                + proc.stdout + "\n" + proc.stderr
            )

        return result

    except subprocess.TimeoutExpired:
        result["error"] = f"pytest timed out after {timeout}s"
        result["defect"] = True
        return result
    except Exception as exc:
        result["error"] = str(exc)
        return result
    finally:
        # Restore — always, even on Ctrl-C / exception.  A failed restore
        # leaves the repo mutated, so it must be loud, never swallowed.
        try:
            def_file.write_text(original_source, encoding="utf-8")
        except Exception as restore_exc:
            print(
                f"CRITICAL: failed to restore {def_file} after sabotage: {restore_exc}",
                file=sys.stderr,
            )
            raise
        finally:
            _ACTIVE_INJECTION = None


# ── Reporting ────────────────────────────────────────────────────────────────

def _report_human(results: list[dict]) -> None:
    """Write a human-readable summary to stderr (and clean output to stdout)."""
    checked = [r for r in results if not r["skipped"] and not r["defect"]]
    skipped = [r for r in results if r["skipped"]]
    defects = [r for r in results if r["defect"]]

    for r in checked:
        parts = [f"\u2713 {r['target']}"]
        if r["patcher_tests"]:
            parts.append("patcher_tests=" + ",".join(r["patcher_tests"]))
        if r["subject_tests"]:
            parts.append("subjects_excluded=" + ",".join(r["subject_tests"]))
        if r.get("module_level"):
            parts.append("module_level_patch=yes")
        print(" — ".join(parts))

    for r in skipped:
        extra = " (module-level patch)" if r.get("module_level") else ""
        print(f"\u2205 {r['target']} — skipped: {r['skipped_reason']}{extra}", file=sys.stderr)

    for r in defects:
        print(f"\u2717 DEFECT {r['target']} — patch does not bind; real implementation is called\n"
              f"  Patcher tests: {r['patcher_tests']}\n"
              f"  Subjects excluded: {r['subject_tests']}", file=sys.stderr)

    if not defects:
        print(f"\nChecked {len(results)} target(s), no defects.", file=sys.stderr)
    else:
        print(f"\n{len(defects)} defect(s) found!", file=sys.stderr)


def _report_json(results: list[dict]) -> None:
    """Print a machine-readable JSON report to stdout."""
    defects = [r for r in results if r["defect"]]
    print(json.dumps({"targets": results, "defects": len(defects)}, indent=2))


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit non-zero when any defect is found."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Git ref to diff against (default: merge-base of HEAD and origin/main).  "
        "Use --base HEAD to diff against HEAD (no changes = nothing to check).",
    )
    parser.add_argument(
        "--files",
        default=None,
        help="Comma-separated paths to treat as the change set (bypasses git entirely).",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root directory (default: .).  Test fixtures can point this at a mini-project.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON to stdout.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=300,
        help="Per-target pytest timeout in seconds (default: 300).",
    )
    args = parser.parse_args(argv)

    # An in-flight injection must be restored if the process is terminated by
    # SIGTERM/SIGINT.  SIGKILL cannot be covered -- the kernel terminates the
    # process without running any Python code -- which is exactly why the
    # real-repo test runs against a disposable checkout of HEAD rather than
    # the live working tree.
    signal.signal(signal.SIGTERM, _handle_termination_signal)
    signal.signal(signal.SIGINT, _handle_termination_signal)

    root = Path(args.root).resolve()
    _ensure_root_importable(root)

    # Determine changed files
    if args.files:
        changed_files = [root / f.strip() for f in args.files.split(",") if f.strip()]
    else:
        base = _get_merge_base(root, args.base)
        maybe_changed = _get_changed_files(root, base)
        if maybe_changed is None:
            print(
                "Cannot determine the change set; refusing to report green.",
                file=sys.stderr,
            )
            return 2
        changed_files = maybe_changed

    # Find candidates
    candidates = collect_candidates(root, changed_files)

    if not candidates:
        print("Nothing to check.", file=sys.stderr)
        if args.json:
            print(json.dumps({"targets": [], "defects": 0}))
        return 0

    # Process each target
    all_results: list[dict] = []
    for target in sorted(candidates):
        result = check_target(
            root, target, candidates[target], args.timeout_seconds
        )
        all_results.append(result)

    # Report
    if args.json:
        _report_json(all_results)
    else:
        _report_human(all_results)

    return 1 if any(r["defect"] for r in all_results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
