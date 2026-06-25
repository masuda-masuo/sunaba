"""Append-only execution journal for post-hoc audit (§9).

Writes JSON-lines records to ``~/.code-sandbox-mcp/journal.log``.
Every container lifecycle event (initialize, exec, stop) and
boundary-crossing operation is recorded with timestamp, run_id,
and operational metadata.

Thread-safe via a module-level lock.  The journal is append-only
by design — no record is ever deleted or overwritten.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOURNAL_DIR: Path = Path.home() / ".code-sandbox-mcp"
_JOURNAL_PATH: Path = _JOURNAL_DIR / "journal.log"

#: Module-level lock for thread-safe journal writes.
_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def _ensure_dir() -> None:
    """Create the journal directory if it does not exist."""
    _JOURNAL_DIR.mkdir(parents=True, exist_ok=True)


def _utcnow_iso() -> str:
    """Return current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Run ID mapping
# ---------------------------------------------------------------------------

#: Maps container ID prefixes → run IDs so that all operations on the
#: same container share a run_id.
_run_map: dict[str, str] = {}
_run_map_lock: threading.Lock = threading.Lock()


def generate_run_id() -> str:
    """Generate a new unique run identifier."""
    return uuid.uuid4().hex[:12]


def get_or_create_run_id(container_id: str) -> str:
    """Return the run_id for *container_id*, creating one if needed."""
    with _run_map_lock:
        if container_id not in _run_map:
            _run_map[container_id] = generate_run_id()
        return _run_map[container_id]


def remove_run_id(container_id: str) -> None:
    """Remove the run_id mapping when a container is stopped."""
    with _run_map_lock:
        _run_map.pop(container_id, None)


# ---------------------------------------------------------------------------
# Core write
# ---------------------------------------------------------------------------


def _append_json(entry: dict[str, Any]) -> None:
    """Append a single JSON-lines record to the journal."""
    _ensure_dir()
    with _lock:
        with open(_JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Convenience recorders
# ---------------------------------------------------------------------------


def record_initialize(
    container_id: str,
    image: str,
    allow_network: bool = False,
    inject_vcs_token: bool = False,
) -> None:
    """Record a container initialization event."""
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "initialize",
        "image": image,
        "allow_network": allow_network,
        "inject_vcs_token": inject_vcs_token,
    })


def record_exec(
    container_id: str,
    commands: list[str],
    exit_code: int,
    verbose: str = "summary",
    allow_network: bool = False,
    inject_vcs_token: bool = False,
    cached: bool = False,
    output_size: int = 0,
    max_output_tokens: int | None = None,
    input_hash: str = "",
) -> None:
    """Append an ``exec`` operation entry to the run journal.

    Records the executed commands, exit code, and metadata (cache hit,
    output size, boundary crossing) under the run id resolved from
    *container_id*.
    """
    run_id = get_or_create_run_id(container_id)
    boundary = allow_network or inject_vcs_token
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "exec",
        "commands": commands,
        "exit_code": exit_code,
        "verbose": verbose,
        "boundary_crossing": boundary,
        "cached": cached,
        "output_size": output_size,
    }
    if max_output_tokens is not None:
        entry["max_output_tokens"] = max_output_tokens
    if input_hash:
        entry["input_hash"] = input_hash
    _append_json(entry)


def record_stop(container_id: str) -> None:
    """Record a container stop event."""
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "stop",
    })
    remove_run_id(container_id)


def record_boundary_crossing(
    container_id: str,
    operation: str,
    details: str,
    approved: bool | None = None,
    token: str | None = None,
) -> None:
    """Record a boundary-crossing operation.

    *approved* is ``None`` when no approval was required (e.g. read-only
    VCS access that only needs journal recording, not token approval).

    *token* is set when the operation enters a pending-approval state
    and is referenced by subsequent approve/reject entries.
    """
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "boundary_crossing",
        "sub_operation": operation,
        "details": details,
        "approved": approved,
    }
    if token is not None:
        entry["token"] = token
    _append_json(entry)


def record_file_write(
    container_id: str,
    file_name: str,
    dest_dir: str,
    byte_count: int,
    is_test: bool = False,
) -> None:
    """Record a file write event into the container.

    *is_test* indicates whether the written file is a test file
    (based on path conventions such as ``test_`` prefix or
    ``tests/`` directory).  This enables the publish flow to
    flag "test changes" as a first-class signal (Issue #96).
    """
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "write_file",
        "file_name": file_name,
        "dest_dir": dest_dir,
        "byte_count": byte_count,
        "is_test": is_test,
    })


def record_copy(
    container_id: str,
    operation: str,  # "copy_project" | "copy_file"
    local_src: str,
    dest_dir: str,
) -> None:
    """Record a file/directory copy into the container."""
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": operation,
        "local_src": local_src,
        "dest_dir": dest_dir,
    })


def record_test_environment(
    container_id: str,
    services: list[dict[str, str]],
    status: str,  # "starting" | "ready" | "stopped"
) -> None:
    """Record a test environment lifecycle event.

    *services* is a list of dicts with keys ``name``, ``image``,
    ``access_url`` (if available).

    *status* is one of ``"starting"``, ``"ready"``, or ``"stopped"``.
    """
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "test_environment",
        "services": services,
        "environment_status": status,
    })


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------


def read_journal(
    run_id: str | None = None,
    max_entries: int | None = None,
) -> list[dict[str, Any]]:
    """Read journal entries, optionally filtered by *run_id*.

    Args:
        run_id: If provided, only return entries for this run.
        max_entries: Maximum number of entries to return (most recent
            first when specified).

    Returns:
        List of journal entry dicts, oldest first.
    """
    if not _JOURNAL_PATH.exists():
        return []

    entries: list[dict[str, Any]] = []
    with _lock:
        with open(_JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if run_id is None or entry.get("run_id") == run_id:
                    entries.append(entry)

    if max_entries is not None and len(entries) > max_entries:
        entries = entries[-max_entries:]

    return entries


def get_journal_path() -> str:
    """Return the absolute path to the journal log file."""
    return str(_JOURNAL_PATH)


def get_pending_approvals() -> list[dict[str, Any]]:
    """Return boundary-crossing entries that are awaiting approval.

    An entry with ``approved=None`` is considered pending unless a later
    entry with the same ``token`` has ``approved`` set to ``True`` or
    ``False`` (i.e. the approval has already been resolved).

    Returns:
        List of pending boundary_crossing entries, oldest first.
    """
    if not _JOURNAL_PATH.exists():
        return []

    all_entries: list[dict[str, Any]] = []
    with _lock:
        with open(_JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("operation") == "boundary_crossing":
                    all_entries.append(entry)

    resolved_tokens: set[str] = set()
    for entry in all_entries:
        if entry.get("approved") is not None:
            token = entry.get("token")
            if token:
                resolved_tokens.add(token)

    pending: list[dict[str, Any]] = []
    for entry in all_entries:
        if entry.get("approved") is not None:
            continue
        token = entry.get("token")
        if token and token in resolved_tokens:
            continue
        pending.append(entry)

    return pending


def get_runs() -> list[dict[str, Any]]:
    """Return a summary of all runs found in the journal."""
    if not _JOURNAL_PATH.exists():
        return []

    runs: dict[str, dict[str, Any]] = {}
    for entry in read_journal():
        rid = entry.get("run_id", "")
        if rid not in runs:
            runs[rid] = {
                "run_id": rid,
                "started": entry.get("ts"),
                "image": entry.get("image", "unknown"),
                "operations": 0,
                "boundary_crossings": 0,
                "vcs_operations": 0,
                "last_ts": entry.get("ts"),
                "status": "running",
            }
        run = runs[rid]
        run["operations"] += 1
        run["last_ts"] = entry.get("ts")
        if entry.get("operation") == "stop":
            run["status"] = "stopped"
        if entry.get("boundary_crossing") or entry.get("operation") == "boundary_crossing":
            run["boundary_crossings"] += 1
            sub_op = entry.get("sub_operation", "")
            if sub_op in ("issue_view", "publish"):
                run["vcs_operations"] += 1

    return sorted(runs.values(), key=lambda r: r.get("started", ""), reverse=True)


def get_active_environments() -> list[dict[str, Any]]:
    """Return currently active test environments from journal entries.

    Returns a list of environments with status ``"starting"`` or
    ``"ready"`` that have no corresponding ``"stopped"`` entry.
    """
    if not _JOURNAL_PATH.exists():
        return []

    env_entries: list[dict[str, Any]] = []
    with _lock:
        with open(_JOURNAL_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("operation") == "test_environment":
                    env_entries.append(entry)

    active: dict[str, dict[str, Any]] = {}
    for entry in env_entries:
        cid = entry.get("container_id", "")
        status = entry.get("environment_status", "")
        if status == "stopped":
            active.pop(cid, None)
        else:
            active[cid] = entry

    return list(active.values())
