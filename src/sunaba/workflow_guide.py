"""Load and slice the workflow guide (Issue #728).

The guide ships inside the wheel as ``workflow_guide.md``, version-locked
to the running server.  This module loads it at call time and extracts
sections by phase name.
"""

from __future__ import annotations

import re

_GUIDE_RESOURCE = "workflow_guide.md"


def _load_guide() -> str:
    """Return the full text of the workflow guide."""
    import importlib.resources
    import pathlib

    # Try the installed package resource first.  Only "the resource is
    # genuinely not there" is caught: anything else (a permission error,
    # a corrupt wheel, an unreadable zip) means the packaging is broken,
    # and swallowing it here would surface later as a bare
    # FileNotFoundError from the source-tree fallback with the real cause
    # already discarded.
    try:
        return (
            importlib.resources.files("sunaba")
            / _GUIDE_RESOURCE
        ).read_text("utf-8")
    except (FileNotFoundError, NotADirectoryError, ModuleNotFoundError):
        pass

    # Fallback: running from the source tree (e.g. tests).  If this
    # also fails, let the exception propagate -- there is no further
    # recovery path.
    return (
        pathlib.Path(__file__).resolve().parent / _GUIDE_RESOURCE
    ).read_text("utf-8")


def _parse_phases(text: str) -> dict[str, tuple[int, int]]:
    """Parse ``## phase: <name>`` headers out of *text*.

    Returns ``{name: (start_line, end_line)}`` where *start_line* and
    *end_line* are 0-indexed line numbers in ``text.split("\\n")`` and
    *end_line* is exclusive.
    """
    phases: dict[str, tuple[int, int]] = {}
    lines = text.split("\n")
    current: str | None = None
    for i, line in enumerate(lines):
        m = re.match(r"^## phase:\s+(\S+)", line)
        if m:
            name: str = m.group(1)  # match succeeded -> group exists
            if current is not None:
                phases[current] = (phases[current][0], i)
            current = name
            phases[current] = (i, len(lines))
    return phases


def get_guide(phase: str | None = None) -> str:
    """Return the version-locked workflow guide, filtered by phase when given."""
    text = _load_guide()
    if phase is None:
        return text

    phases = _parse_phases(text)
    if phase not in phases:
        valid = ", ".join(sorted(phases))
        return (
            f"Error: unknown phase {phase!r}. "
            f"Valid phases: {valid}"
        )

    lines = text.split("\n")
    start, end = phases[phase]
    # Always include the preamble (everything before the first phase
    # header) so the "this guide wins over client-side documents"
    # provision stays visible even to callers who filter by phase.
    first_phase_line = min(s for s, _ in phases.values()) if phases else 0
    if first_phase_line > 0:
        preamble = "\n".join(lines[:first_phase_line])
        body = "\n".join(lines[start:end])
        return preamble + "\n\n" + body
    return "\n".join(lines[start:end])
