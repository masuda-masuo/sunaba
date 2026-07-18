"""Embedded Python scripts that execute inside sandbox containers.

Each module-level ``r'''...'''`` constant is a self-contained script
parameterised via ``__PARAMS_B64__`` / ``__DIFF_B64__`` / ``__CODE_B64__`` /
``__FILE_PATH_REPR__`` / ``__MARK_A__`` / ``__MARK_B__`` placeholders that
are substituted on the host before the script is executed in the container.
These scripts are **never executed on the host**.
"""

from __future__ import annotations

# Generated transform applied by :func:`apply_patch_to_file`.  Runs inside the
# container via :func:`transform_file_in_container`: writes the file's text and
# the (normalized) diff to a temp dir and lets ``git apply --recount`` apply it
# — tolerating off-by-one ``@@`` counts that break a strict parser.
# ``__DIFF_B64__`` is substituted on the host.
_GIT_APPLY_TRANSFORM = r'''
import base64, os, subprocess, tempfile

DIFF = base64.b64decode("__DIFF_B64__").decode("utf-8")

def transform(text):
    d = tempfile.mkdtemp()
    target = os.path.join(d, "target")
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    patch = os.path.join(d, "patch.diff")
    with open(patch, "w", encoding="utf-8", newline="") as fh:
        fh.write(DIFF)
    errors = []
    for extra in ([], ["--ignore-whitespace"]):
        try:
            proc = subprocess.run(
                ["git", "apply", "--recount", "-p1", *extra, patch],
                cwd=d, capture_output=True, text=True,
            )
        except FileNotFoundError:
            raise RuntimeError("git is not available in this container")
        if proc.returncode == 0:
            with open(target, "r", encoding="utf-8") as fh:
                return fh.read()
        msg = (proc.stderr or proc.stdout).strip()
        if msg:
            errors.append(msg)
    raise RuntimeError("git apply could not apply the diff: " + " | ".join(errors))
'''


# In-container runner for ``transform_file``.  Reads the target file, runs the
# caller's ``transform(text) -> text`` against it, writes the result back, and
# emits a unified diff wrapped in per-call sentinels so stray prints from the
# caller's code cannot corrupt the result envelope.
# ``__FILE_PATH_REPR__`` / ``__CODE_B64__`` / ``__MARK_A__`` / ``__MARK_B__``
# are substituted on the host.  This script runs inside the sandbox container,
# **never on the host**.
_TRANSFORM_RUNNER = r'''
import sys, json, base64, difflib, traceback

FILE_PATH = __FILE_PATH_REPR__
USER_CODE_B64 = "__CODE_B64__"
MARK_A = "__MARK_A__"
MARK_B = "__MARK_B__"

def emit(obj):
    sys.stdout.write(MARK_A + json.dumps(obj) + MARK_B)
    sys.stdout.flush()
    sys.exit(0)

try:
    with open(FILE_PATH, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()
except FileNotFoundError:
    emit({"status": "error", "error": "file not found: " + FILE_PATH})
except Exception as e:
    emit({"status": "error", "error": "read failed: " + repr(e)})

try:
    user_code = base64.b64decode(USER_CODE_B64).decode("utf-8")
except Exception as e:
    emit({"status": "error", "error": "could not decode code: " + repr(e)})

ns = {}
try:
    exec(user_code, ns)
except Exception as e:
    emit({"status": "error",
          "error": "code failed at definition time: " + type(e).__name__ + ": " + str(e),
          "traceback": traceback.format_exc()})

transform = ns.get("transform")
if not callable(transform):
    emit({"status": "error",
          "error": "code must define a callable `transform(text: str) -> str`"})

try:
    new = transform(original)
except Exception as e:
    emit({"status": "error",
          "error": "transform() raised " + type(e).__name__ + ": " + str(e),
          "traceback": traceback.format_exc()})

if not isinstance(new, str):
    emit({"status": "error",
          "error": "transform() must return str, got " + type(new).__name__})

if new == original:
    orig_lines = original.count("\n") + (1 if original and not original.endswith("\n") else 0)
    emit({"status": "ok", "changed": False, "diff": "", "new_size": len(original), "new_lines": orig_lines})

try:
    with open(FILE_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
except Exception as e:
    emit({"status": "error", "error": "write failed: " + repr(e)})

diff = "\n".join(difflib.unified_diff(
    original.splitlines(), new.splitlines(),
    fromfile=FILE_PATH, tofile=FILE_PATH, lineterm=""))
new_lines = new.count("\n") + (1 if new and not new.endswith("\n") else 0)
emit({"status": "ok", "changed": True, "diff": diff, "new_size": len(new), "new_lines": new_lines})
'''


# In-container driver for ``edit_symbol_in_container``.  Unlike the transform
# runner it never executes caller-supplied code: the resolve / edit / verify
# logic is this fixed script, parameterized via a base64-encoded JSON blob, so
# every error message shape stays under host control.
# ``__PARAMS_B64__`` / ``__MARK_A__`` / ``__MARK_B__`` are substituted on the
# host.  This script runs inside the sandbox container, **never on the host**.
_EDIT_SYMBOL_DRIVER = r'''
import ast, base64, difflib, json, sys

PARAMS = json.loads(base64.b64decode("__PARAMS_B64__").decode("utf-8"))
MARK_A = "__MARK_A__"
MARK_B = "__MARK_B__"

FILE_PATH = PARAMS["file_path"]
SYMBOL = PARAMS["symbol"]
NEW_CODE = PARAMS["new_code"]
LINE = PARAMS["line"]
PRESERVE = PARAMS.get("preserve", "decorators+docstring")


def emit(obj):
    sys.stdout.write(MARK_A + json.dumps(obj) + MARK_B)
    sys.stdout.flush()
    sys.exit(0)


def fail(msg):
    emit({"status": "error", "error": msg})


if NEW_CODE and not NEW_CODE.strip():
    fail('Error: new_code is whitespace-only; use new_code="" to delete the symbol')

try:
    with open(FILE_PATH, "r", encoding="utf-8", newline="") as fh:
        original = fh.read()
except FileNotFoundError:
    fail("Error: file not found: " + FILE_PATH)
except Exception as e:
    fail("Error: read failed: " + repr(e))

if "\r\n" in original:
    fail(
        "Error: " + FILE_PATH + " contains CRLF line endings; edit_symbol"
        " supports LF files only. Use edit_file with a complete old_str,"
        " or transform_file."
    )

try:
    tree = ast.parse(original)
except SyntaxError as e:
    fail(
        "Error: " + FILE_PATH + " has a syntax error at line " + str(e.lineno)
        + ": " + str(e.msg) + ". Fix it with edit_file (complete old_str) or transform_file first."
    )

lines = original.splitlines()
had_final_nl = original.endswith("\n")
DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def dec_name(d):
    target = d.func if isinstance(d, ast.Call) else d
    parts = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
        return "@" + ".".join(reversed(parts))
    return lines[d.lineno - 1].strip()


candidates = []


def collect(node, scope):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, DEF_NODES):
            starts = [child.lineno] + [d.lineno for d in child.decorator_list]
            candidates.append({
                "qualname": ".".join(scope + [child.name]),
                "kind": "class" if isinstance(child, ast.ClassDef) else "function",
                "def_line": child.lineno,
                "start": min(starts),
                "end": child.end_lineno,
                "decorators": [dec_name(d) for d in child.decorator_list],
            })
            collect(child, scope + [child.name])
        else:
            collect(child, scope)


collect(tree, [])

matches = [
    c for c in candidates
    if c["qualname"] == SYMBOL or c["qualname"].endswith("." + SYMBOL)
]

if not matches:
    pool = {}
    for c in candidates:
        pool.setdefault(c["qualname"], []).append(c)
        pool.setdefault(c["qualname"].rsplit(".", 1)[-1], []).append(c)
    close = difflib.get_close_matches(SYMBOL, sorted(pool), n=5, cutoff=0.6)
    hints = []
    for name in close:
        for c in pool[name]:
            if c not in hints:
                hints.append(c)
    msg = "Error: symbol '" + SYMBOL + "' not found in " + FILE_PATH + "."
    if hints:
        hints.sort(key=lambda c: c["start"])
        msg += " Did you mean: " + ", ".join(
            c["qualname"] + " (line " + str(c["start"]) + ")" for c in hints[:5]
        ) + "?"
    fail(msg)


def describe(cands):
    mixed = len(set(c["qualname"] for c in cands)) > 1
    out = []
    for c in cands:
        text = lines[c["def_line"] - 1].strip()[:80]
        desc = " / ".join(c["decorators"] + [text])
        if mixed:
            desc = c["qualname"] + ": " + desc
        out.append("  lines " + str(c["start"]) + "-" + str(c["end"]) + ":  " + desc)
    return "\n".join(out)


if LINE is not None:
    containing = [c for c in matches if c["start"] <= LINE <= c["end"]]
    if not containing:
        fail(
            "Error: line=" + str(LINE) + " does not fall within any definition of '"
            + SYMBOL + "' in " + FILE_PATH + ":\n" + describe(matches)
            + "\nRetry with line=<start line of the intended definition>."
        )
    containing.sort(key=lambda c: (c["end"] - c["start"], -c["start"]))
    target = containing[0]
elif len(matches) > 1:
    fail(
        "Error: '" + SYMBOL + "' is ambiguous in " + FILE_PATH + ":\n"
        + describe(matches)
        + "\nRetry with line=<start line of the intended definition>."
    )
else:
    target = matches[0]

start, end = target["start"], target["end"]
def_text = lines[target["def_line"] - 1]
def_indent = len(def_text) - len(def_text.lstrip())

if NEW_CODE == "":
    before = lines[:start - 1]
    after = lines[end:]
    n_blank = 0
    while before and not before[-1].strip():
        before.pop()
        n_blank += 1
    while after and not after[0].strip():
        after.pop(0)
        n_blank += 1
    if not after or not before:
        new_lines = before + after
    else:
        max_blank = 2 if def_indent == 0 else 1
        new_lines = before + [""] * min(n_blank, max_blank) + after
    new = "\n".join(new_lines)
    if new_lines and had_final_nl:
        new += "\n"
else:
    code_lines = NEW_CODE.splitlines()
    n_lead_stripped = 0
    while code_lines and not code_lines[0].strip():
        code_lines.pop(0)
        n_lead_stripped += 1
    while code_lines and not code_lines[-1].strip():
        code_lines.pop()
    first = code_lines[0]
    delta = def_indent - (len(first) - len(first.lstrip()))
    reindented = []
    for ln in code_lines:
        if not ln.strip():
            reindented.append("")
        elif delta >= 0:
            reindented.append(" " * delta + ln)
        else:
            reindented.append(ln[min(-delta, len(ln) - len(ln.lstrip())):])

    if PRESERVE != "none":
        old_node = None
        for n in ast.walk(tree):
            if isinstance(n, DEF_NODES) and n.lineno == target["def_line"]:
                old_node = n
                break
        if old_node is not None:
            try:
                new_tree = ast.parse(NEW_CODE)
            except SyntaxError:
                new_tree = None
            if new_tree is not None:
                new_node = None
                for n in ast.walk(new_tree):
                    if isinstance(n, DEF_NODES):
                        new_node = n
                        break

                if new_node is not None:
                    def _has_docstring(node):
                        return (node.body
                                and isinstance(node.body[0], ast.Expr)
                                and isinstance(node.body[0].value, ast.Constant)
                                and isinstance(node.body[0].value.value, str))

                    preserve_decs = PRESERVE in ("decorators", "decorators+docstring")
                    preserve_docs = PRESERVE in ("docstring", "decorators+docstring")

                    dec_offset = 0
                    if preserve_decs and old_node.decorator_list and not new_node.decorator_list:
                        dec_lines = []
                        for d in old_node.decorator_list:
                            for ln in range(d.lineno, d.end_lineno + 1):
                                dec_lines.append(lines[ln - 1])
                        reindented = dec_lines + reindented
                        dec_offset = len(dec_lines)

                    if preserve_docs and not _has_docstring(new_node) and _has_docstring(old_node):
                        ds = old_node.body[0]
                        new_body = new_node.body[0]
                        # Locate the new body's first statement in code_lines
                        # coordinates (AST linenos count the leading blank
                        # lines that were stripped above).  Insertion is only
                        # possible when that statement starts its own line:
                        # a one-liner (def f(): return 1) has nowhere to put
                        # a docstring line.  Likewise skip when the OLD
                        # docstring shares the def line.
                        body_idx = new_body.lineno - 1 - n_lead_stripped
                        body_on_own_line = (
                            0 <= body_idx < len(code_lines)
                            and not code_lines[body_idx][: new_body.col_offset].strip()
                        )
                        if ds.lineno != target["def_line"] and body_on_own_line:
                            new_body_indent = max(0, new_body.col_offset + delta)
                            # Shift the docstring block as a whole so nested
                            # lines keep their relative indentation.
                            shift = new_body_indent - ds.col_offset
                            ds_lines = []
                            for ln in range(ds.lineno, ds.end_lineno + 1):
                                raw = lines[ln - 1]
                                if not raw.strip():
                                    ds_lines.append("")
                                elif shift >= 0:
                                    ds_lines.append(" " * shift + raw)
                                else:
                                    cut = min(-shift, len(raw) - len(raw.lstrip()))
                                    ds_lines.append(raw[cut:])
                            ins = body_idx + dec_offset
                            reindented = reindented[:ins] + ds_lines + reindented[ins:]

    new_lines = lines[:start - 1] + reindented + lines[end:]
    new = "\n".join(new_lines)
    if had_final_nl and new and not new.endswith("\n"):
        new += "\n"

try:
    ast.parse(new)
except SyntaxError as e:
    fail(
        "Error: the edited file would have a syntax error at line " + str(e.lineno)
        + ": " + str(e.msg) + "; nothing was written. Fix new_code and retry."
    )

resolved = {
    "qualname": target["qualname"],
    "kind": target["kind"],
    "start_line": start,
    "end_line": end,
}

if new == original:
    n_lines = original.count("\n") + (1 if original and not original.endswith("\n") else 0)
    emit({"status": "ok", "resolved": resolved, "changed": False, "diff": "",
          "new_size": len(original), "new_lines": n_lines})

try:
    with open(FILE_PATH, "w", encoding="utf-8", newline="") as fh:
        fh.write(new)
except Exception as e:
    fail("Error: write failed: " + repr(e))

diff = "\n".join(difflib.unified_diff(
    original.splitlines(), new.splitlines(),
    fromfile=FILE_PATH, tofile=FILE_PATH, lineterm=""))
n_lines = new.count("\n") + (1 if new and not new.endswith("\n") else 0)
emit({"status": "ok", "resolved": resolved, "changed": True, "diff": diff,
      "new_size": len(new), "new_lines": n_lines})
'''
