"""Journal/trace tools: sandbox_read_journal, sandbox_trace, sandbox_list_runs, sandbox_journal_path, sandbox_trace_dir."""

from __future__ import annotations

import json

from sunaba.journal import get_journal_path, get_runs, read_journal
from sunaba.trace import generate_html_trace, generate_json_trace, get_trace_dir


def sandbox_read_journal(
    run_id: str | None = None,
    max_entries: int = 100,
    session_label: str | None = None,
) -> str:
    """Read the append-only execution journal.

    Returns JSON array of journal entries, optionally filtered by
    *run_id*.  The journal records every container lifecycle event
    (initialize, exec, stop) and boundary-crossing operation.

    Args:
        run_id: If provided, only return entries for this run.
            Omit to see all journal entries.
        max_entries: Maximum number of entries to return
            (most recent first, default 100).
        session_label: If provided, only return entries with this
            session label (Issue #479).

    Returns:
        JSON string with a list of journal entry objects, each
        containing ``ts``, ``run_id``, ``container_id``,
        ``operation``, and operation-specific fields.
    """
    entries = read_journal(run_id=run_id, max_entries=max_entries, session_label=session_label)
    return json.dumps(entries, ensure_ascii=False)


def sandbox_trace(
    run_id: str,
    output_format: str = "json",
) -> str:
    """Generate a replay trace for a specific run.

    Creates an HTML or JSON trace file from journal entries for
    *run_id*, enabling post-hoc review of "why did it do that?".

    Args:
        run_id: The run identifier to generate a trace for.
        output_format: Output format - ``"json"`` or ``"html"``
            (default ``"json"``).

    Returns:
        Path to the generated trace file, or an error message
        beginning with ``"Error:"``.
    """
    if output_format not in ("json", "html"):
        return "Error: format must be 'json' or 'html'"

    if output_format == "json":
        path = generate_json_trace(run_id)
    else:
        path = generate_html_trace(run_id)

    if not path:
        return f"Error: run_id {run_id} not found in journal"
    return path


def sandbox_list_runs() -> str:
    """List all runs recorded in the execution journal.

    Returns a JSON array of run summaries, each with ``run_id``,
    ``started``, ``image``, ``operations``, ``boundary_crossings``,
    ``status``, and ``last_ts``.

    Returns:
        JSON string with a list of run summary objects.
    """
    runs = get_runs()
    return json.dumps(runs, ensure_ascii=False)


def sandbox_journal_path() -> str:
    """Return the filesystem path to the execution journal file.

    Returns:
        Absolute path to ``~/.sunaba/journal.log``.
    """
    return get_journal_path()


def sandbox_trace_dir() -> str:
    """Return the filesystem path to the trace output directory.

    Returns:
        Absolute path to ``~/.sunaba/traces/``.
    """
    return get_trace_dir()
