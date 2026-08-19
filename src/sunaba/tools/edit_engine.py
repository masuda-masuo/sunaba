"""Pure-string edit engine: AST resolution, whitespace-flexible matching, and
echo formatting for the ``edit_file`` tool.

This module has **zero** dependencies on docker, containers, or any I/O: it
works on plain Python strings, making every function directly unit-testable
without mocks.
"""

from __future__ import annotations

import ast
import difflib
import re
import textwrap
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

_DEF_RE = re.compile(r'^\s*(?:async\s+)?def\s+(\w+)')
_CLASS_RE = re.compile(r'^\s*class\s+(\w+)')


# ---------------------------------------------------------------------------
# Symbol extraction & parsing predicates
# ---------------------------------------------------------------------------


def _extract_symbol_from_old_str(old_str: str) -> str | None:
    """Extract a function/class name from *old_str* if it looks like a definition.

    Skips blank lines, comments, and decorator lines.  Returns the symbol
    name (``"foo"``, ``"Bar"``) of the first ``def`` / ``async def`` /
    ``class`` line, or ``None`` when *old_str* does not start with a
    Python definition.
    """
    for line in old_str.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        m = _DEF_RE.match(stripped)
        if m:
            return m.group(1)
        m = _CLASS_RE.match(stripped)
        if m:
            return m.group(1)
        if stripped.startswith('@'):
            continue
        break
    return None


def _parses_as_definition(text: str) -> bool:
    """True when *text* parses standalone as code containing a def/class."""
    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        return False
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for n in ast.walk(tree)
    )


def _is_bare_signature(old_str: str) -> bool:
    """True when *old_str* has no content beyond a single def/class signature.

    Blank lines, comments, and decorators (including multi-line
    decorators and multi-line signatures) around the definition are
    allowed; an unfinished signature start like ``def foo(`` counts as
    bare too.  Used to decide whether the exact-string fallback is safe
    after a failed AST resolution: string-replacing a bare signature
    with a complete definition would splice the new body in front of
    the old one and leave the old body orphaned in the file (issue
    #599 follow-up).  old_str blocks that carry any body line -- even a
    mis-indented one the whitespace-flexible matcher handles -- are NOT
    bare and keep the fallback.
    """
    src = textwrap.dedent(old_str).rstrip()
    # AST probe: a complete signature block (decorators + def/class
    # line, however many physical lines) plus an appended probe body
    # parses to exactly one definition whose body is that probe.
    try:
        tree = ast.parse(src + "\n    pass")
    except SyntaxError:
        # The probe also fails on a complete ONE-LINER definition
        # (``def f(): pass``, overload stubs ``def f(): ...``) because
        # the appended body is an unexpected indent after the inline
        # body.  Those are complete definitions -- string-replacing
        # them orphans nothing -- so they are never bare.
        if _parses_as_definition(src):
            return False
    else:
        if len(tree.body) == 1 and isinstance(
            tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = tree.body[0].body
            return len(body) == 1 and isinstance(body[0], ast.Pass)
        return False
    # Unparseable: line scan for signature *fragments* (e.g. the first
    # line of a multi-line signature).  Continuation lines of an
    # unfinished multi-line decorator or signature are not recognized
    # here and fall through to False -- the fallback then relies on the
    # exact-string match semantics.
    seen_def = False
    for line in old_str.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if seen_def:
            return False
        if stripped.startswith("@"):
            continue
        if _DEF_RE.match(stripped) or _CLASS_RE.match(stripped):
            seen_def = True
            continue
        return False
    return seen_def


# ---------------------------------------------------------------------------
# Body-loss guard
# ---------------------------------------------------------------------------

#: The AST node types ``old_str`` AST resolution can target.
_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _sole_definition(text: str) -> _DefNode | None:
    """Return the single def/class node *text* consists of, else ``None``.

    Text that carries anything besides one definition (two defs, a def
    plus a module-level assignment, a bare expression) is not a
    candidate for the body-loss guard: it is plainly a real
    replacement.
    """
    try:
        tree = ast.parse(textwrap.dedent(text))
    except SyntaxError:
        return None
    if len(tree.body) != 1:
        return None
    node = tree.body[0]
    return node if isinstance(node, _DEF_NODES) else None


def _body_statements(node: _DefNode) -> list[ast.stmt]:
    """Return *node*'s body with a leading docstring removed."""
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _is_body_free(node: _DefNode) -> bool:
    """True when *node*'s body is a docstring and nothing else.

    A docstring is a legal body, so a ``def`` line plus a docstring
    parses and re-verifies like any other definition -- which is
    exactly why losing the real body that way is silent.

    ``pass`` and ``...`` are NOT body-free here.  They are bodies the
    caller wrote on purpose: replacing a definition with an overload
    stub is a real edit, pinned by
    ``test_one_liner_stub_old_str_keeps_string_fallback``, and blocking
    it would trade a silent loss for a false refusal.
    """
    return not _body_statements(node)


@dataclass
class _ResolvedDefinition:
    """One definition as the in-container driver sees it."""

    #: The AST node, for inspecting the body.
    node: _DefNode

    #: Dotted name built from the enclosing definitions, driver-style.
    qualname: str

    #: ``"class"`` or ``"function"``.
    kind: str

    #: First line of the definition, **decorators included**.
    start: int

    #: Last line of the definition.
    end: int


def _collect_definitions(tree: ast.AST) -> list[_ResolvedDefinition]:
    """Collect every definition in *tree* the way the driver's collect() does.

    Scope is the chain of *enclosing definitions* only -- other blocks
    (``if``, ``try``, ``with``) are walked through without extending the
    qualname -- and a definition's span starts at its first decorator,
    not at its ``def`` line.
    """
    found: list[_ResolvedDefinition] = []

    def collect(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _DEF_NODES):
                starts = [child.lineno, *(d.lineno for d in child.decorator_list)]
                found.append(_ResolvedDefinition(
                    node=child,
                    qualname=".".join([*scope, child.name]),
                    kind="class" if isinstance(child, ast.ClassDef) else "function",
                    start=min(starts),
                    end=child.end_lineno or child.lineno,
                ))
                collect(child, [*scope, child.name])
            else:
                collect(child, scope)

    collect(tree, [])
    return found


def _resolve_symbol(
    source: str,
    symbol: str,
    line: int | None = None,
) -> _ResolvedDefinition | None:
    """Resolve *symbol* exactly as ``_EDIT_SYMBOL_DRIVER`` resolves it.

    This is a mirror of the driver's resolution, and it has to stay one:
    a pre-flight that resolves a different definition than the driver
    guards the wrong code.  Matching is on the qualname (exact, or a
    ``.``-suffix), the *line* window spans decorators too -- which is
    what the driver's own ambiguity error tells callers to pass -- and
    several definitions containing that line are broken apart by
    smallest span, then latest start, as the driver does.

    Returns ``None`` when the driver would resolve nothing (symbol not
    found, *line* outside every match, or an ambiguous symbol with no
    *line*).  In each of those branches the driver fails before editing
    anything, so there is no AST edit for the caller's guard to make a
    judgement about.  ``tests/test_edit_symbol.py`` pins this function
    and the driver against a shared table of inputs so the two cannot
    drift apart unnoticed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # The driver parses the same text and fails the same way; it
        # writes nothing, so nothing needs guarding.
        return None

    matches = [
        c for c in _collect_definitions(tree)
        if c.qualname == symbol or c.qualname.endswith("." + symbol)
    ]
    if not matches:
        return None

    if line is not None:
        containing = [c for c in matches if c.start <= line <= c.end]
        if not containing:
            return None
        containing.sort(key=lambda c: (c.end - c.start, -c.start))
        return containing[0]

    if len(matches) > 1:
        return None
    return matches[0]


def _body_loss_error(
    existing: str,
    symbol: str,
    file_contents: str,
    dest_path: str,
    line: int | None = None,
) -> str | None:
    """Return an error when the AST edit would silently delete a body.

    AST resolution replaces the *whole* definition with *file_contents*
    under ``preserve="decorators+docstring"``.  When *file_contents* is
    a signature plus a docstring and nothing else, the decorators and
    the docstring survive, the body does not, and the result re-parses
    cleanly -- so nothing downstream objects (sunaba PR #822: an
    ``edit_file`` meant to update a docstring deleted ``done()``'s body,
    including its ``_control.stop()`` call, and the gate stayed green
    because the code was behind ``pragma: no cover``).

    The judgement is on the observable content of the old and new
    definitions, never on the shape of ``old_str``: an ``old_str`` that
    happens to include a ``def`` line is not by itself a problem, and a
    genuinely shorter new body is not either.  The definition judged is
    the one :func:`_resolve_symbol` picks, which is the one the driver
    will replace.

    Returns ``None`` when the edit is safe.
    """
    new_def = _sole_definition(file_contents)
    if new_def is None or not _is_body_free(new_def):
        return None

    try:
        target = _resolve_symbol(existing, symbol, line)
    except Exception as e:  # pragma: no cover - defensive
        # Unable to say which definition the driver will replace, so
        # unable to say the edit is safe.  Refusing costs the caller one
        # ast=False retry; passing costs them their code.
        return (
            f"Error: refusing to replace '{symbol}' in {dest_path}: "
            f"file_contents has no body beyond its docstring, and the "
            f"definition this edit would replace could not be resolved "
            f"here ({e}), so the body cannot be shown to survive. Put "
            "the full body in file_contents, or pass ast=False for a "
            "literal string replacement of old_str."
        )

    if target is None or _is_body_free(target.node):
        return None

    dropped = len(_body_statements(target.node))
    plural = "" if dropped == 1 else "s"
    return (
        f"Error: refusing to replace {target.kind} '{symbol}' in "
        f"{dest_path} (resolved to '{target.qualname}', lines "
        f"{target.start}-{target.end}): file_contents has no body beyond "
        f"its docstring, so the AST edit would silently delete the "
        f"{dropped} statement{plural} in the current body. Put the full "
        "body in file_contents, or pass ast=False for a literal string "
        "replacement of old_str."
    )


# ---------------------------------------------------------------------------
# String matching utilities
# ---------------------------------------------------------------------------


def _find_all_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    """Find all non-overlapping occurrences of *pattern* in *text*.

    Returns a list of ``(offset, line_number)`` tuples.  An empty
    pattern matches at every position; the scan still terminates.
    """
    matches: list[tuple[int, int]] = []
    idx = 0
    while True:
        idx = text.find(pattern, idx)
        if idx == -1:
            break
        line_no = text[:idx].count("\n") + 1
        matches.append((idx, line_no))
        # Advance past the match so occurrences never overlap.  An
        # empty pattern has len 0, so advance by at least 1 or the
        # scan would never terminate.
        idx += max(len(pattern), 1)
    return matches


def _get_line_indent(line: str) -> int:
    """Return the leading whitespace length of *line*."""
    return len(line) - len(line.lstrip())


def _reindent_lines(lines: list[str], delta: int) -> list[str]:
    """Apply an indentation *delta* (number of spaces) to each line.

    Empty/whitespace-only lines are passed through unchanged.
    A positive *delta* adds leading spaces; a negative *delta* removes them.
    """
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue
        if delta >= 0:
            result.append(" " * delta + line)
        else:
            remove = min(-delta, _get_line_indent(line))
            result.append(line[remove:])
    return result


def _try_whitespace_flexible(
    existing: str, old_str: str, new_str: str,
) -> tuple[str, int, int] | str | None:
    """Attempt whitespace-flexible matching.

    Strips leading/trailing whitespace from each line of *old_str* and
    slides over the file looking for a block whose stripped lines match.
    When found the file's original indentation is preserved and *new_str*
    is re-indented to fit.

    Returns ``(new_content, replaced_start_line, replaced_end_line)`` on
    success (1-indexed lines in the *new* content), an ``"Error: ..."``
    string when the match is ambiguous, or ``None`` if no match was found.
    """
    existing_lines = existing.splitlines()
    old_lines = old_str.splitlines()
    old_stripped = [line.strip() for line in old_lines]

    if len(old_lines) > len(existing_lines):
        return None

    matches: list[int] = []
    for i in range(len(existing_lines) - len(old_lines) + 1):
        chunk = existing_lines[i : i + len(old_lines)]
        if [line.strip() for line in chunk] == old_stripped:
            matches.append(i)

    if not matches:
        return None

    if len(matches) > 1:
        line_nos = ", ".join(str(m + 1) for m in matches[:10])
        suffix = "..." if len(matches) > 10 else ""
        return (
            f"Error: old_str matches at {len(matches)} locations "
            f"(lines {line_nos}{suffix}) after whitespace normalization. "
            "Add more surrounding context to make it unique, or use "
            "transform_file to edit several occurrences in one call."
        )

    i = matches[0]
    chunk = existing_lines[i : i + len(old_lines)]
    file_first_indent = _get_line_indent(chunk[0])
    old_first_indent = _get_line_indent(old_lines[0])
    delta = file_first_indent - old_first_indent
    reindented = _reindent_lines(new_str.splitlines(), delta)
    new_content = "\n".join(reindented)

    # Build character offsets to do a string-level replacement
    # (preserves trailing whitespace and file structure).
    pos = 0
    line_starts: list[int] = []
    for line in existing_lines:
        line_starts.append(pos)
        pos += len(line) + 1  # +1 for newline
    # offset right after the last matched line
    start_offset = line_starts[i]
    end_idx = i + len(old_lines)
    if end_idx < len(line_starts):
        end_offset = line_starts[end_idx]
    else:
        end_offset = len(existing)

    result = existing[:start_offset] + new_content + existing[end_offset:]
    if existing.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    replaced_start = i + 1
    replaced_end = i + max(len(reindented), 1)
    return result, replaced_start, replaced_end


# ---------------------------------------------------------------------------
# Near-miss reporting
# ---------------------------------------------------------------------------

# Unified diff display limits for the near-miss echo: old_str blocks of up
# to _NEAR_MISS_FULL_DIFF_MAX_LINES lines get an untruncated diff; longer
# ones are capped at _NEAR_MISS_DIFF_CAP diff lines.
_NEAR_MISS_FULL_DIFF_MAX_LINES = 50
_NEAR_MISS_DIFF_CAP = 30


def _build_first_mismatch_report(
    old_lines: list[str], matched_lines: list[str], best_start: int,
) -> str:
    """Report the first line where *old_lines* and *matched_lines* diverge.

    Compares the whitespace-stripped lines with
    :meth:`difflib.SequenceMatcher.get_opcodes` so that inserted or
    missing lines (e.g. a duplicated line in old_str) still point at the
    first real divergence instead of shifting every subsequent line.
    Lines are shown with ``repr()`` to make tabs, spaces, and other
    invisible characters visible.  *best_start* is the 0-indexed file
    line of ``matched_lines[0]`` used to report real file line numbers.

    Returns an empty string when the stripped lines are identical
    (whitespace-only mismatch, normally handled by the flexible matcher).
    """
    old_stripped = [line.strip() for line in old_lines]
    matched_stripped = [line.strip() for line in matched_lines]
    sm = difflib.SequenceMatcher(None, old_stripped, matched_stripped)
    for tag, i1, _i2, j1, _j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            return (
                f"First mismatch: old_str line {i1 + 1} "
                f"vs file line {best_start + j1 + 1}\n"
                f"  old_str: {old_lines[i1]!r}\n"
                f"  file:    {matched_lines[j1]!r}"
            )
        if tag == "delete":
            return (
                f"First mismatch: old_str line {i1 + 1}: "
                f"{old_lines[i1]!r} has no counterpart in the file region"
            )
        # tag == "insert"
        return (
            f"First mismatch: file line {best_start + j1 + 1}: "
            f"{matched_lines[j1]!r} has no counterpart in old_str"
        )
    return ""


def _build_near_miss_echo(existing: str, old_str: str, dest_path: str) -> str:
    """Build a near-miss error message with diff, context, and first mismatch.

    Uses a sliding-window line match to find the most similar region,
    shows a unified diff (full for old_str blocks of up to 50 lines,
    capped at 30 diff lines beyond that), 3 lines of surrounding
    context, and pinpoints the first mismatching line (issue #580).
    """
    existing_lines = existing.splitlines()
    old_lines = old_str.splitlines()
    n_old = len(old_lines)
    n_existing = len(existing_lines)

    # --- find best-matching block via sliding window ---
    best_ratio = 0.0
    best_start = 0  # line index in existing_lines
    best_end = 0

    if n_old <= n_existing:
        for i in range(n_existing - n_old + 1):
            block = "\n".join(existing_lines[i:i + n_old])
            sm = difflib.SequenceMatcher(None, old_str, block)
            # quick_ratio() is an upper bound on ratio(); skip windows
            # that cannot beat the current best (issue #580).
            if sm.quick_ratio() <= best_ratio:
                continue
            r = sm.ratio()
            if r > best_ratio:
                best_ratio = r
                best_start = i
                best_end = i + n_old
    elif n_existing > 0:
        # old_str longer than file -- compare with whole file
        sm = difflib.SequenceMatcher(None, old_str, existing)
        best_ratio = sm.ratio()
        best_start = 0
        best_end = n_existing

    # --- build context (3 lines before / after) ---
    ctx_start = max(0, best_start - 3)
    ctx_end = min(n_existing, best_end + 3)
    context_lines: list[str] = []
    for i in range(ctx_start, ctx_end):
        prefix = ">>>" if best_start <= i < best_end else "   "
        context_lines.append(f"{prefix} {i + 1:4d} | {existing_lines[i]}")
    context_block = "\n".join(context_lines)

    # --- unified diff (limited to 6 lines) ---
    matched_lines = existing_lines[best_start:best_end] if best_end > best_start else []
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            matched_lines,
            fromfile="old_str (provided)",
            tofile=f"{dest_path} (file)",
            lineterm="",
        )
    )
    # Show the full diff for old_str blocks of up to 50 lines; beyond
    # that cap the diff at 30 lines so the interesting part survives
    # (issue #580 -- the old 6-line cap hid the actual difference).
    if (
        len(old_lines) > _NEAR_MISS_FULL_DIFF_MAX_LINES
        and len(diff_lines) > _NEAR_MISS_DIFF_CAP
    ):
        remaining = len(diff_lines) - _NEAR_MISS_DIFF_CAP
        diff_lines = diff_lines[:_NEAR_MISS_DIFF_CAP] + [
            f"... (truncated, {remaining} more lines)"
        ]
    diff_block = "\n".join(diff_lines) if diff_lines else "(identical content, whitespace differs)"

    # --- first mismatching line (replaces the old indentation hint) ---
    mismatch_report = _build_first_mismatch_report(
        old_lines, matched_lines, best_start,
    )
    mismatch_section = f"\n{mismatch_report}" if mismatch_report else ""

    return (
        f"Error: old_str not found in {dest_path}.\n"
        f"Best matching region (similarity={best_ratio:.0%}):\n"
        f"{context_block}\n"
        f"Unified diff (old_str vs file region):\n"
        f"{diff_block}"
        f"{mismatch_section}\n"
        "Tip: Use read_file_range first to confirm the exact content "
        "(including whitespace). If exact matching keeps failing, switch "
        "to transform_file -- it edits by pattern (e.g. re.sub) and does "
        "not need the exact text."
    )


# ---------------------------------------------------------------------------
# Success-echo formatting
# ---------------------------------------------------------------------------

# Success echo limits: +-2 context lines around the replaced region and a
# 30-row overall cap with the middle elided.
_SUCCESS_ECHO_CONTEXT = 2
_SUCCESS_ECHO_MAX_ROWS = 30

# ---------------------------------------------------------------------------
# Transaction-based multi-replacement (sunaba#875)
# ---------------------------------------------------------------------------


def _validate_and_apply_replacements(
    original: str,
    replacements: list[dict],
    dest_path: str,
) -> tuple[str, str | None]:
    """Validate and apply a series of replacements transactionally.

    All matching and counting is done against *original*.  If every
    replacement's expected_count (when declared) matches the actual
    count, and matched regions do not overlap, all replacements are
    applied at once.  Otherwise nothing is written.

    *replacements* is a list of dicts, each with:
    - ``old_str``: the literal text to find
    - ``new_str``: the replacement text
    - ``expected_count`` (optional): how many occurrences are expected

    Returns ``(new_content, None)`` on success, or
    ``("", error_message)`` on failure.
    """
    if not replacements:
        return "", "Error: edits list must not be empty"

    # Phase 1: validate every replacement against the original text.
    # Collect all match regions (offset, length, replacement_index).
    all_regions: list[tuple[int, int, int]] = []
    for i, rep in enumerate(replacements):
        # edits is an untyped list[dict], so a malformed entry (a non-dict,
        # a missing old_str/new_str, a non-string key) must fail with the
        # tool's structured 'edits[i]' error rather than a raw KeyError or
        # TypeError leaking out of the validation phase.
        if not isinstance(rep, dict):
            return "", (
                f"Error: edits[{i}] must be a dict with old_str and "
                "new_str. Nothing was written."
            )
        old = rep.get("old_str")
        new = rep.get("new_str")
        expected = rep.get("expected_count")

        if old is None:
            return "", (
                f"Error: edits[{i}].old_str is required. "
                "Nothing was written."
            )
        if not isinstance(old, str):
            return "", (
                f"Error: edits[{i}].old_str must be a string. "
                "Nothing was written."
            )
        if not old:
            return "", f"Error: edits[{i}].old_str must not be empty"
        if new is None:
            return "", (
                f"Error: edits[{i}].new_str is required. "
                "Nothing was written."
            )
        if not isinstance(new, str):
            return "", (
                f"Error: edits[{i}].new_str must be a string. "
                "Nothing was written."
            )

        matches = _find_all_matches(original, old)
        actual_count = len(matches)

        if expected is not None:
            if actual_count != expected:
                near_miss = _build_near_miss_echo(original, old, dest_path) if actual_count == 0 else ""
                hint = f"\n{near_miss}" if near_miss else ""
                return "", (
                    f"Error: edits[{i}].old_str has {actual_count} "
                    f"occurrence(s) but expected_count is {expected}. "
                    f"Nothing was written."
                    f"{hint}"
                )
            # All occurrences are replaced
            for offset, _line_no in matches:
                all_regions.append((offset, len(old), i))
        else:
            # No expected_count: use legacy behaviour -- unique match required
            if actual_count > 1:
                line_nos = ", ".join(str(m[1]) for m in matches[:10])
                suffix = "..." if len(matches) > 10 else ""
                return "", (
                    f"Error: edits[{i}].old_str matches at {actual_count} "
                    f"locations (lines {line_nos}{suffix}). Declare "
                    f"expected_count={actual_count} to replace all, or add "
                    f"more context to make it unique. Nothing was written."
                )
            if actual_count == 0:
                near_miss = _build_near_miss_echo(original, old, dest_path)
                return "", (
                    f"Error: edits[{i}].old_str not found in {dest_path}. "
                    f"Nothing was written.\n{near_miss}"
                )
            offset, _line_no = matches[0]
            all_regions.append((offset, len(old), i))

    # Phase 2: check for overlapping regions.
    # Sort by offset, then check adjacent pairs.
    all_regions.sort()
    for j in range(len(all_regions) - 1):
        r1_offset, r1_len, r1_idx = all_regions[j]
        r2_offset, _r2_len, r2_idx = all_regions[j + 1]
        if r1_offset + r1_len > r2_offset:
            return "", (
                f"Error: edits[{r1_idx}].old_str and edits[{r2_idx}].old_str "
                f"have overlapping matches. Nothing was written."
            )

    # Phase 3: apply all replacements in reverse offset order so that
    # earlier offsets remain valid.
    result = original
    for offset, length, rep_idx in reversed(all_regions):
        new = replacements[rep_idx]["new_str"]
        result = result[:offset] + new + result[offset + length:]

    return result, None


# Minimum (stripped) file_contents length for the "already applied" hint on
# a failed old_str match -- short snippets appear coincidentally too often
# to be evidence of a retried edit.
_ALREADY_APPLIED_MIN_CHARS = 8


def _build_success_echo(
    content: str, dest_path: str, rep_start: int, rep_end: int,
) -> str:
    """Echo the post-edit region after a successful old_str replacement.

    Shows the replaced lines (marked ``>>>``) with line numbers and
    +-2 lines of context so the model keeps a ground-truth image of the
    file right after the edit instead of drifting across batch edits
    (issue #580).  The echo is capped at 30 rows; the middle is elided.
    """
    lines = content.splitlines()
    if not lines:
        return f"Written {len(content)} bytes to {dest_path}"
    rep_start = max(1, min(rep_start, len(lines)))
    rep_end = max(rep_start, min(rep_end, len(lines)))
    if rep_start == rep_end:
        span = f"replaced line {rep_start}"
    else:
        span = f"replaced lines {rep_start}-{rep_end}"

    ctx_start = max(1, rep_start - _SUCCESS_ECHO_CONTEXT)
    ctx_end = min(len(lines), rep_end + _SUCCESS_ECHO_CONTEXT)
    rows: list[str] = []
    for ln in range(ctx_start, ctx_end + 1):
        prefix = ">>>" if rep_start <= ln <= rep_end else "   "
        rows.append(f"{prefix} {ln:4d} | {lines[ln - 1]}")

    if len(rows) > _SUCCESS_ECHO_MAX_ROWS:
        keep = (_SUCCESS_ECHO_MAX_ROWS - 1) // 2
        omitted = len(rows) - 2 * keep
        rows = rows[:keep] + [f"... ({omitted} lines)"] + rows[-keep:]

    return (
        f"Written {len(content)} bytes to {dest_path} ({span})\n"
        + "\n".join(rows)
    )


# ---------------------------------------------------------------------------
# Syntax note (only for .py files)
# ---------------------------------------------------------------------------


def _python_syntax_note(dest_path: str, content: str) -> str:
    """Warning suffix when *content* for a .py file does not parse.

    A .py file that stops parsing right after a write is almost always
    an escaping or matching mistake -- say so in the success echo, so
    the caller can repair it immediately instead of discovering it at
    verify time (issue #599).  Warning only: multi-step edits may pass
    through broken intermediate states on purpose.
    """
    if not dest_path.endswith(".py"):
        return ""
    try:
        ast.parse(content)
    except SyntaxError as e:
        return (
            f"\nWarning: {dest_path} does not parse as Python after "
            f"this edit (line {e.lineno}: {e.msg}). If unintended, "
            "call undo_file_edit to restore the pre-edit file (do "
            "NOT try to repair the broken text in place), check "
            "file_contents for escaping artifacts (stray \\n, "
            '\\" or unbalanced quotes), and re-apply the edit.'
        )
    return ""
