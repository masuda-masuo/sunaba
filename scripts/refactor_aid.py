#!/usr/bin/env python3
"""Refactoring aid: function dependency graph + extraction risk checks (issue #165).

Static (``ast``-based) analysis of the ``code_sandbox_mcp`` package, written to
support the ``server.py`` split series (issue #153).  Given a function it shows
what that function depends on and -- the part off-the-shelf tools don't give --
whether each dependency should be *moved together* with the function or
*imported as-is* when the function is extracted into another module.

Subcommands::

    refactor_aid.py graph <function> [--reverse] [--depth N] [--json]
    refactor_aid.py extract-check <function> [--json]

``graph``
    Print the dependency tree, or with ``--reverse`` the callers of the
    function (useful to judge whether moving it is safe).

``extract-check``
    Print an extraction plan plus the risks that recurring refactor reviews
    keep catching by hand: ``__file__`` relative paths that break when the file
    moves deeper (cf. PR #197 ``_UPDATE_SPEC``), shared-helper references that
    would break on a move (cf. PR #163 ``_docker``), and missing docstrings.

Scope / limitations:

* Module-level functions only; methods and dynamic dispatch (``getattr`` /
  string-based calls) are not resolved.
* Re-exports are reported at the imported-from module, not chased to the
  original definition.
* Import hygiene and blank-line style are ruff's job, not this tool's.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Definition:
    """A top-level ``def``/``class`` discovered in the package."""

    name: str
    module: str
    file: Path
    lineno: int
    kind: str  # "func" | "class"
    has_docstring: bool


@dataclass
class FuncInfo:
    """Per-function analysis: bare-name calls and ``__file__`` usage."""

    name: str
    module: str
    file: Path
    lineno: int
    has_docstring: bool
    calls: set[str]
    dunder_file_lines: list[int]


@dataclass
class Model:
    """The whole-package symbol/reference model built in a single AST pass."""

    top_pkg: str
    src_root: Path
    defs: dict[tuple[str, str], Definition]
    by_name: dict[str, list[Definition]]
    funcs: dict[tuple[str, str], FuncInfo]
    import_maps: dict[Path, dict[str, tuple[str, str]]]
    referrers: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    callers: dict[tuple[str, str], set[tuple[str, str]]] = field(default_factory=dict)


def module_name_for(path: Path, src_root: Path) -> tuple[str, bool]:
    """Return ``(dotted_module, is_package)`` for *path* under *src_root*."""
    rel = path.relative_to(src_root)
    parts = list(rel.with_suffix("").parts)
    is_pkg = rel.name == "__init__.py"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


def resolve_relative(curr_module: str, is_pkg: bool, level: int, modname: str) -> str:
    """Resolve a (possibly relative) ``from ... import`` target to a dotted module."""
    if level == 0:
        return modname
    base = curr_module.split(".")
    if not is_pkg:
        base = base[:-1]
    if level > 1:
        base = base[: -(level - 1)]
    if modname:
        return ".".join(base + modname.split("."))
    return ".".join(base)


def _scan_function(node: ast.AST) -> tuple[set[str], list[int]]:
    """Collect bare-name call targets and ``__file__`` line numbers in *node*."""
    calls: set[str] = set()
    dunder: list[int] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            calls.add(sub.func.id)
        if isinstance(sub, ast.Name) and sub.id == "__file__":
            dunder.append(sub.lineno)
    return calls, dunder


def build_model(src_root: Path) -> Model:
    """Walk every ``*.py`` under *src_root* and build the reference model."""
    pkgs = sorted(p.parent.name for p in src_root.glob("*/__init__.py"))
    top_pkg = pkgs[0] if pkgs else ""

    defs: dict[tuple[str, str], Definition] = {}
    by_name: dict[str, list[Definition]] = {}
    funcs: dict[tuple[str, str], FuncInfo] = {}
    import_maps: dict[Path, dict[str, tuple[str, str]]] = {}

    for path in sorted(src_root.rglob("*.py")):
        module, is_pkg = module_name_for(path, src_root)
        tree = ast.parse(path.read_text(encoding="utf-8"))

        imap: dict[str, tuple[str, str]] = {}
        for n in tree.body:
            if isinstance(n, ast.Import):
                for a in n.names:
                    local = a.asname or a.name.split(".")[0]
                    imap[local] = (a.name, "")
            elif isinstance(n, ast.ImportFrom):
                tmod = resolve_relative(module, is_pkg, n.level, n.module or "")
                for a in n.names:
                    local = a.asname or a.name
                    imap[local] = (tmod, a.name)
        import_maps[path] = imap

        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                has_doc = ast.get_docstring(n) is not None
                defs[(module, n.name)] = Definition(
                    n.name, module, path, n.lineno, "func", has_doc
                )
                by_name.setdefault(n.name, []).append(defs[(module, n.name)])
                calls, dunder = _scan_function(n)
                funcs[(module, n.name)] = FuncInfo(
                    n.name, module, path, n.lineno, has_doc, calls, dunder
                )
            elif isinstance(n, ast.ClassDef):
                has_doc = ast.get_docstring(n) is not None
                defs[(module, n.name)] = Definition(
                    n.name, module, path, n.lineno, "class", has_doc
                )
                by_name.setdefault(n.name, []).append(defs[(module, n.name)])

    model = Model(top_pkg, src_root, defs, by_name, funcs, import_maps)
    _build_refs(model)
    return model


def _internal(model: Model, module: str) -> bool:
    """True when *module* belongs to the analysed top-level package."""
    return module.split(".")[0] == model.top_pkg


def resolve_dep(model: Model, fi: FuncInfo, name: str) -> tuple[str, str, str] | None:
    """Resolve a referenced *name* to ``(kind, module, symbol)`` or ``None``.

    *kind* is ``"import"`` (defined in another package module, reached via an
    import) or ``"local"`` (defined in the same module).  External names
    (stdlib / third-party / unresolved) return ``None``.
    """
    imap = model.import_maps[fi.file]
    if name in imap:
        tmod, tsym = imap[name]
        if _internal(model, tmod):
            return ("import", tmod, tsym or name)
        return None
    if (fi.module, name) in model.defs:
        return ("local", fi.module, name)
    return None


def _build_refs(model: Model) -> None:
    """Populate referrer and caller indexes from every function's calls."""
    for (mod, name), fi in model.funcs.items():
        for call in fi.calls:
            if call == name:
                continue
            r = resolve_dep(model, fi, call)
            if r is None:
                continue
            _, dmod, dname = r
            model.referrers.setdefault((dmod, dname), set()).add(mod)
            model.callers.setdefault((dmod, dname), set()).add((mod, name))


def annotate(model: Model, src_module: str, dep_module: str, dep_name: str) -> str:
    """Return the move/import annotation for a dependency of a function."""
    if dep_module == src_module:
        others = model.referrers.get((dep_module, dep_name), set()) - {src_module}
        if others:
            return "shared - careful (also used by: " + ", ".join(sorted(others)) + ")"
        return "move together"
    return f"import as-is ({dep_module})"


def deps_of(model: Model, mod: str, name: str) -> list[tuple[str, str, str]]:
    """Return ``(dep_module, dep_name, annotation)`` for a function's deps."""
    fi = model.funcs.get((mod, name))
    if fi is None:
        return []
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for call in sorted(fi.calls):
        if call == name:
            continue
        r = resolve_dep(model, fi, call)
        if r is None:
            continue
        _, dmod, dname = r
        if (dmod, dname) in seen:
            continue
        seen.add((dmod, dname))
        out.append((dmod, dname, annotate(model, mod, dmod, dname)))
    return out


def find_func(model: Model, name: str, package: str | None = None) -> tuple[str, str]:
    """Locate a function by *name*, optionally filtered by *package* substring."""
    cands = [k for k in model.funcs if k[1] == name]
    if package:
        cands = [k for k in cands if package in k[0]]
    if not cands:
        raise SystemExit(f"error: no function named {name!r} found")
    if len(cands) > 1:
        mods = ", ".join(sorted(m for m, _ in cands))
        print(
            f"note: {name!r} defined in multiple modules ({mods}); "
            f"using {cands[0][0]}. Use --package to disambiguate.",
            file=sys.stderr,
        )
    return cands[0]


def _loc(model: Model, module: str, name: str) -> str:
    """Human-readable location: repo-relative file path if known, else module."""
    d = model.defs.get((module, name))
    if d is not None:
        try:
            return str(d.file.relative_to(model.src_root.parent))
        except ValueError:
            return str(d.file)
    return module


# --- rendering -------------------------------------------------------------


def render_graph(model: Model, mod: str, name: str, depth: int) -> list[str]:
    """Render the forward dependency tree as a list of lines."""
    lines = [f"{name}  ({_loc(model, mod, name)})"]

    def walk(cur_mod: str, cur_name: str, prefix: str, level: int, stack: set) -> None:
        if level > depth:
            return
        deps = deps_of(model, cur_mod, cur_name)
        for i, (dmod, dname, ann) in enumerate(deps):
            last = i == len(deps) - 1
            branch = "\\_ " if last else "|_ "
            lines.append(f"{prefix}{branch}{dname}()  {dmod}  [{ann}]")
            key = (dmod, dname)
            if key not in stack and (dmod, dname) in model.funcs:
                ext = "   " if last else "|  "
                walk(dmod, dname, prefix + ext, level + 1, stack | {key})

    walk(mod, name, "  ", 1, {(mod, name)})
    return lines


def render_reverse(model: Model, mod: str, name: str) -> list[str]:
    """Render the callers of a function (who would break if it moved)."""
    callers = sorted(model.callers.get((mod, name), set()))
    head = f"callers of {name}  ({_loc(model, mod, name)})"
    if not callers:
        return [head, "  (none - no in-package callers found)"]
    lines = [head]
    for cmod, cname in callers:
        lines.append(f"  <- {cname}()  {cmod}")
    return lines


def render_extract_check(model: Model, mod: str, name: str) -> list[str]:
    """Render the extraction plan and risk findings for a function."""
    fi = model.funcs[(mod, name)]
    deps = deps_of(model, mod, name)

    move = [(m, n) for m, n, a in deps if a == "move together"]
    imp = [(m, n) for m, n, a in deps if a.startswith("import as-is")]
    shared = [(m, n, a) for m, n, a in deps if a.startswith("shared")]

    lines = [f"extract-check: {name}  ({_loc(model, mod, name)})", "", "dependencies to carry:"]
    lines.append("  move together:  " + (", ".join(n for _, n in move) or "(none)"))
    lines.append(
        "  import as-is:   " + (", ".join(f"{n} ({m})" for m, n in imp) or "(none)")
    )

    risks: list[str] = []
    if fi.dunder_file_lines:
        locs = ", ".join(f"L{n}" for n in fi.dunder_file_lines)
        risks.append(
            f"WARN __file__ referenced ({locs}) - path base changes on move; recheck Path depth"
        )
    for dmod, dname, ann in shared:
        risks.append(f"WARN shared dep: {dname}() is referenced elsewhere - {ann}")
    if not fi.has_docstring:
        risks.append(f"WARN missing docstring: {name}()")
    for dmod, dname in move:
        d = model.defs.get((dmod, dname))
        if d is not None and not d.has_docstring:
            risks.append(f"WARN missing docstring (move target): {dname}()")

    lines.append("")
    lines.append("risks:")
    lines.extend("  " + r for r in (risks or ["(none)"]))
    return lines


# --- JSON ------------------------------------------------------------------


def graph_json(model: Model, mod: str, name: str) -> dict:
    """Machine-readable dependency list for a function."""
    return {
        "function": name,
        "module": mod,
        "location": _loc(model, mod, name),
        "dependencies": [
            {"module": m, "name": n, "annotation": a}
            for m, n, a in deps_of(model, mod, name)
        ],
    }


def extract_check_json(model: Model, mod: str, name: str) -> dict:
    """Machine-readable extraction plan + risks for a function."""
    fi = model.funcs[(mod, name)]
    deps = deps_of(model, mod, name)
    move = [n for m, n, a in deps if a == "move together"]
    shared = [n for m, n, a in deps if a.startswith("shared")]
    missing_doc = [name] if not fi.has_docstring else []
    for m, n, a in deps:
        if a == "move together":
            d = model.defs.get((m, n))
            if d is not None and not d.has_docstring:
                missing_doc.append(n)
    return {
        "function": name,
        "module": mod,
        "move_together": move,
        "import_as_is": [
            {"module": m, "name": n} for m, n, a in deps if a.startswith("import as-is")
        ],
        "risks": {
            "dunder_file_lines": fi.dunder_file_lines,
            "shared_dependencies": shared,
            "missing_docstrings": missing_doc,
        },
    }


def _default_src_root() -> Path:
    """Locate ``src`` relative to this script's repository root."""
    root = Path(__file__).resolve().parent.parent
    return root / "src"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the refactoring-aid tool."""
    parser = argparse.ArgumentParser(
        description="Function dependency graph / extraction aid."
    )
    parser.add_argument(
        "--src", type=Path, default=_default_src_root(), help="Package src root."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pg = sub.add_parser("graph", help="Show a function's dependency tree.")
    pg.add_argument("function")
    pg.add_argument("--reverse", action="store_true", help="Show callers instead.")
    pg.add_argument("--depth", type=int, default=1, help="Transitive depth (default 1).")
    pg.add_argument("--package", help="Disambiguate by module substring.")
    pg.add_argument("--json", action="store_true")

    pe = sub.add_parser("extract-check", help="Extraction plan + risks.")
    pe.add_argument("function")
    pe.add_argument("--package", help="Disambiguate by module substring.")
    pe.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    model = build_model(args.src)
    mod, name = find_func(model, args.function, args.package)

    if args.cmd == "graph":
        if args.json:
            print(json.dumps(graph_json(model, mod, name), ensure_ascii=False, indent=2))
        elif args.reverse:
            print("\n".join(render_reverse(model, mod, name)))
        else:
            print("\n".join(render_graph(model, mod, name, args.depth)))
    elif args.cmd == "extract-check":
        if args.json:
            print(
                json.dumps(
                    extract_check_json(model, mod, name), ensure_ascii=False, indent=2
                )
            )
        else:
            print("\n".join(render_extract_check(model, mod, name)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
