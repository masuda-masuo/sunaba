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
from datetime import date, datetime, timedelta, timezone
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
    mem_limit: str | None = None,
    cpus: float | None = None,
) -> None:
    """Record a container initialization event.

    Args:
        container_id: 12-character container ID prefix.
        image: Docker image used.
        allow_network: Whether network access was granted.
        inject_vcs_token: Whether VCS tokens were injected.
        mem_limit: Override mem_limit if specified (Issue #201).
        cpus: Override cpus if specified (Issue #201).
    """
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "initialize",
        "image": image,
        "allow_network": allow_network,
        "inject_vcs_token": inject_vcs_token,
    }
    if mem_limit is not None:
        entry["mem_limit"] = mem_limit
    if cpus is not None:
        entry["cpus"] = cpus
    _append_json(entry)


def record_initialize_complete(container_id: str) -> None:
    """Record that ``sandbox_initialize`` finished all setup phases.

    Written only after clone / pip install / PR setup have returned, so a
    container that has this event is a usable, intentional container — never
    an orphan from a mid-init timeout.  The orphan reaper (Issue #298) treats
    the *absence* of this event (together with no ``exec`` and no ``stop``) as
    the signal that an ``initialize`` was abandoned partway through.
    """
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "initialize_complete",
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


# ---------------------------------------------------------------------------
# Command classification (for tool usage dashboard — Issue #229)
# ---------------------------------------------------------------------------

#: Tool introduction dates for time-windowed bias correction.
#  Each key is a tool name and its value is the ISO date when it became available.
_TOOL_INTRO_DATES: dict[str, str] = {
    "search_in_container": "2026-06-07",
    "read_file_range": "2026-06-07",
    "list_files": "2026-06-07",
    "verify_in_container": "2026-06-07",
    "transform_file": "2026-06-07",
    "lint_in_container": "2026-06-07",
    "type_check_in_container": "2026-06-07",
    "write_file_sandbox": "2026-06-07",
    "copy_project": "2026-06-07",
    "copy_file": "2026-06-07",
}

#: Shell-program → tool-name mapping for structured-tool bypass detection.
#  When a shell command's first word matches a key below, the dedicated
#  tool in the value existed and could have been used instead.
_SHELL_TO_TOOL: dict[str, str] = {
    "grep": "search_in_container",
    "rg": "search_in_container",
    "ag": "search_in_container",
    "cat": "read_file_range",
    "head": "read_file_range",
    "tail": "read_file_range",
    "less": "read_file_range",
    "find": "list_files",
    "ls": "list_files",
    "sed": "transform_file",
    "awk": "transform_file",
    "ruff": "lint_in_container",
    "pyright": "type_check_in_container",
    "pytest": "verify_in_container",
}


def classify_exec_command(cmd: str) -> str:
    """Classify a single shell command string into a bucket category.

    The first token (after stripping leading whitespace) determines the
    bucket.  Special-case detection for piped / chained commands is
    handled by checking for ``&&`` and ``;`` separators — in that case
    only the first sub-command is used for classification.
    """
    cmd = cmd.strip()
    if not cmd:
        return "empty"

    # Extract the first sub-command before && or ;
    for sep in ("&&", ";", "||", "|"):
        idx = cmd.find(sep)
        if idx > 0:
            # Only split on separators that are outside quotes
            quoted = False
            for i, ch in enumerate(cmd[:idx]):
                if ch in ("'", '"'):
                    quoted = not quoted
            if not quoted:
                cmd = cmd[:idx].strip()
                break

    tokens = cmd.split()
    if not tokens:
        return "empty"

    first = tokens[0].rstrip(";")

    # SCM
    if first == "git":
        return "git"
    if first in ("gh", "hub"):
        return "gh"

    # Testing / linting / type checking
    if first in ("pytest", "tox", "coverage"):
        return "pytest"
    if first == "ruff":
        return "lint"
    if first == "pyright" or first == "mypy":
        return "type_check"

    # Search
    if first in ("grep", "rg", "ag", "ack"):
        return "search"

    # Read
    if first in ("cat", "head", "tail", "less", "more"):
        return "read"

    # Edit / transform
    if first in ("sed", "awk", "cut", "tr", "sort", "uniq", "wc"):
        return "edit"

    # Package management
    if first in ("pip", "pip3", "uv", "npm", "yarn", "apt", "apt-get",
                 "yum", "dnf", "gem", "cargo", "brew"):
        return "install"

    # File listing
    if first in ("find", "ls", "locate", "tree"):
        return "list"

    # Python interpreter
    if first in ("python", "python3", "pypy"):
        # Check if it's pytest invocation
        for tok in tokens[1:3]:
            if tok in ("-m", "pytest") or "pytest" in tok:
                return "pytest"
        return "python"

    # Echo / print
    if first in ("echo", "printf"):
        return "echo"

    # cd
    if first == "cd":
        return "cd"

    # File operations
    if first in ("cp", "mv", "rm", "mkdir", "rmdir", "touch",
                 "chmod", "chown", "ln", "stat", "dd", "tee"):
        return "file_ops"

    # Shell builtins / control
    if first in ("export", "source", ".", "set", "unset", "env",
                 "alias", "unalias", "type", "which", "command"):
        return "shell"

    # Container / Docker
    if first in ("docker", "podman", "nerdctl"):
        return "container"

    # System
    if first in ("curl", "wget", "tar", "gzip", "gunzip", "zip", "unzip",
                 "ssh", "scp", "rsync", "ps", "kill", "sleep", "timeout",
                 "date", "df", "du", "free", "uptime", "hostname", "whoami"):
        return "system"

    return "other"


def get_tool_usage(
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Aggregate tool usage statistics from journal entries within a time range.

    Args:
        from_date: Inclusive start date in ``"YYYY-MM-DD"`` format.
            Defaults to 7 days ago.
        to_date: Inclusive end date in ``"YYYY-MM-DD"`` format.
            Defaults to today.

    Returns:
        A dict with keys:

        - ``time_range`` — ``{from, to}`` ISO dates used
        - ``total_ops`` — count of all operations (excl. init/stop)
        - ``exec_ops`` — count of ``exec`` operations
        - ``exec_share_pct`` — exec as % of total
        - ``non_exec_ops`` — count of non-exec tool operations
        - ``command_buckets`` — ``{bucket: count}`` for exec commands
        - ``cd_count`` — count of exec entries whose first command is ``cd``
        - ``cd_rate_pct`` — cd entries as % of exec entries
        - ``structured_ops`` — count of each structured tool operation
        - ``bypass_count`` — exec commands that could have used a dedicated tool
        - ``bypass_rate_pct`` — bypass as % of (dedicated + bypass)
        - ``exec_entry_count`` — total number of exec *entries* (not sub-commands)
    """
    if to_date is None:
        to_dt = date.today()
    else:
        to_dt = date.fromisoformat(to_date)

    if from_date is None:
        from_dt = to_dt - timedelta(days=7)
    else:
        from_dt = date.fromisoformat(from_date)

    from_iso = from_dt.isoformat()
    to_iso = (to_dt + timedelta(days=1)).isoformat()  # exclusive upper bound

    entries = read_journal()

    total_ops = 0
    exec_ops = 0
    exec_entry_count = 0
    command_buckets: dict[str, int] = {}
    cd_count = 0
    structured_ops: dict[str, int] = {}
    bypass_count = 0
    bypass_detail: dict[str, int] = {}

    for entry in entries:
        ts = entry.get("ts", "")
        if ts < from_iso or ts >= to_iso:
            continue

        op = entry.get("operation", "")
        if op in ("initialize", "initialize_complete", "stop", "test_environment"):
            continue

        if op == "exec":
            exec_ops += 1
            exec_entry_count += 1
            commands = entry.get("commands", [])

            if commands and isinstance(commands, list):
                first_cmd = commands[0] if commands else ""
                bucket = classify_exec_command(first_cmd)
                command_buckets[bucket] = command_buckets.get(bucket, 0) + 1

                if bucket == "cd":
                    cd_count += 1

                # Bypass detection: does a structured tool exist for this command?
                first_word = first_cmd.strip().split()[0] if first_cmd.strip() else ""
                first_word = first_word.rstrip(";")
                tool = _SHELL_TO_TOOL.get(first_word)
                if tool:
                    tool_intro = _TOOL_INTRO_DATES.get(tool, "")
                    # Only count as bypass if the tool existed at the time
                    if tool_intro and ts[:10] >= tool_intro:
                        bypass_count += 1
                        bypass_detail[first_word] = bypass_detail.get(first_word, 0) + 1

        elif op == "boundary_crossing":
            sub_op = entry.get("sub_operation", "")
            key = f"boundary:{sub_op}" if sub_op else "boundary:unknown"
            structured_ops[key] = structured_ops.get(key, 0) + 1
            total_ops += 1

        else:
            structured_ops[op] = structured_ops.get(op, 0) + 1
            total_ops += 1

    total_ops += exec_ops

    exec_share_pct = round(exec_ops / total_ops * 100, 1) if total_ops else 0.0
    cd_rate_pct = round(cd_count / exec_entry_count * 100, 1) if exec_entry_count else 0.0

    # Bypass rate: bypass_count / (bypass_count + dedicated_usage)
    dedicated_usage = struct_tool_ops_from_journal(structured_ops)
    bypass_denom = bypass_count + dedicated_usage
    bypass_rate_pct = round(bypass_count / bypass_denom * 100, 1) if bypass_denom else 0.0

    return {
        "time_range": {"from": from_iso, "to": to_iso},
        "total_ops": total_ops,
        "exec_ops": exec_ops,
        "exec_share_pct": exec_share_pct,
        "non_exec_ops": total_ops - exec_ops,
        "command_buckets": dict(sorted(command_buckets.items(), key=lambda x: -x[1])),
        "cd_count": cd_count,
        "cd_rate_pct": cd_rate_pct,
        "structured_ops": dict(sorted(structured_ops.items(), key=lambda x: -x[1])),
        "bypass_count": bypass_count,
        "bypass_rate_pct": bypass_rate_pct,
        "bypass_detail": dict(sorted(bypass_detail.items(), key=lambda x: -x[1])),
        "exec_entry_count": exec_entry_count,
        "_tool_intro_dates": _TOOL_INTRO_DATES,
    }


def struct_tool_ops_from_journal(structured_ops: dict[str, int]) -> int:
    """Count structured tool operations that have shell equivalents.

    Only counts operations that map to tools in ``_SHELL_TO_TOOL.values()``.
    ``boundary:*`` entries are excluded.
    """
    tool_values = set(_SHELL_TO_TOOL.values())
    op_to_tool: dict[str, str] = {
        "write_file": "write_file_sandbox",
        "lint_in_container": "lint_in_container",
        "type_check_in_container": "type_check_in_container",
    }
    count = 0
    for key, n in structured_ops.items():
        if key.startswith("boundary:"):
            continue
        tool = op_to_tool.get(key, key)
        if tool in tool_values:
            count += n
    return count
