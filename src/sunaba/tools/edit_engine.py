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
# String matching utilities
# ---------------------------------------------------------------------------


def _find_all_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    """Find all non-overlapping occurrences of *pattern* in *text*.

    Returns a list of ``(offset, line_number)`` tuples.
    """
    matches: list[tuple[int, int]] = []
    idx = 0
    while True:
        idx = text.find(pattern, idx)
        if idx == -1:
            break
        line_no = text[:idx].count("\n") + 1
        matches.append((idx, line_no))
        idx += 1
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
