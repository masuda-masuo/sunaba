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


def _get_state_path() -> Path:
    """Return the sidecar state file path (derived from _JOURNAL_DIR)."""
    return _JOURNAL_DIR / "container_state.json"

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
# Container state sidecar (Issue #305)
# ---------------------------------------------------------------------------


#: Whether this process has re-synced the sidecar from the journal.  A crash
#: between the journal append and the sidecar update in ``record_*`` loses the
#: sidecar update, and a later ``record_*`` makes the sidecar look newer than
#: the journal, permanently masking the loss.  The journal is always written
#: first (a superset of the sidecar), so one unconditional rebuild per process
#: closes that window — a crash implies a process restart.
_state_synced: bool = False


def _load_states_unlocked() -> dict[str, dict[str, Any]]:
    """Load the sidecar states (caller must hold ``_lock``).

    Raises :class:`FileNotFoundError` when the sidecar does not exist;
    callers decide whether that means "empty" or "rebuild".
    """
    with open(_get_state_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def _save_states_unlocked(states: dict[str, dict[str, Any]]) -> None:
    """Atomically write the sidecar states (caller must hold ``_lock``)."""
    _ensure_dir()
    tmp = _get_state_path().with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(states, f, ensure_ascii=False)
    tmp.replace(_get_state_path())


def _update_container_state(container_id: str, **updates: Any) -> None:
    """Update a single container's state entry (thread-safe, atomic).

    A ``stopped=True`` update removes the entry instead of flagging it: the
    sidecar only tracks containers that may still be alive, so it stays
    bounded by the number of active containers instead of growing with
    history.
    """
    with _lock:
        try:
            states = _load_states_unlocked()
        except FileNotFoundError:
            states = {}
        s = states.setdefault(
            container_id,
            {"complete": False, "used": False, "stopped": False, "init_ts": None},
        )
        for k, v in updates.items():
            if v is not None:
                s[k] = v
        if s.get("stopped"):
            states.pop(container_id, None)
        _save_states_unlocked(states)


def _rebuild_states_unlocked() -> dict[str, dict[str, Any]]:
    """Rebuild container states from the journal (caller must hold ``_lock``).

    Stopped containers are dropped, mirroring the pruning in
    :func:`_update_container_state`.
    """
    states: dict[str, dict[str, Any]] = {}
    for entry in _read_journal_unlocked():
        cid = entry.get("container_id")
        if not cid:
            continue
        op = entry.get("operation")
        if op == "stop":
            states.pop(cid, None)
            continue
        if op not in ("initialize", "initialize_complete", "exec"):
            continue
        s = states.setdefault(
            cid,
            {"complete": False, "used": False, "stopped": False, "init_ts": None},
        )
        if op == "initialize":
            s["init_ts"] = entry.get("ts")
        elif op == "initialize_complete":
            s["complete"] = True
        else:
            s["used"] = True
    return states


def read_container_states() -> dict[str, dict[str, Any]]:
    """Return per-container lifecycle state summary (sidecar fast path).

    Normally reads the sidecar, which is bounded by the number of active
    containers.  Falls back to a full journal scan (rewriting the sidecar)
    whenever the sidecar cannot be trusted: on the first read in each
    process (see ``_state_synced``), when the journal is newer than the
    sidecar (crash between journal append and sidecar update), or when the
    sidecar vanished between the stat and the read.

    Containers with a recorded ``stop`` have no entry; ``stopped`` is kept
    in the value shape for interface stability but is never ``True``.
    """
    global _state_synced
    with _lock:
        if _state_synced:
            state_path = _get_state_path()
            journal_mtime = (
                _JOURNAL_PATH.stat().st_mtime_ns if _JOURNAL_PATH.exists() else 0
            )
            state_mtime = (
                state_path.stat().st_mtime_ns if state_path.exists() else 0
            )
            if journal_mtime <= state_mtime:
                try:
                    return _load_states_unlocked()
                except FileNotFoundError:
                    pass  # vanished after the stat — rebuild below
        states = _rebuild_states_unlocked()
        _save_states_unlocked(states)
        _state_synced = True
        return states


# ---------------------------------------------------------------------------
# Convenience recorders
# ---------------------------------------------------------------------------


def record_initialize(
    container_id: str,
    image: str,
    allow_network: bool = False,
    mem_limit: str | None = None,
    cpus: float | None = None,
) -> None:
    """Record a container initialization event.

    Args:
        container_id: 12-character container ID prefix.
        image: Docker image used.
        allow_network: Whether network access was granted.
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
    }
    if mem_limit is not None:
        entry["mem_limit"] = mem_limit
    if cpus is not None:
        entry["cpus"] = cpus
    _append_json(entry)
    _update_container_state(container_id, init_ts=entry["ts"])


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
    _update_container_state(container_id, complete=True)


def record_exec(
    container_id: str,
    commands: list[str],
    exit_code: int,
    verbose: str = "summary",
    allow_network: bool = False,
    output_size: int = 0,
    max_output_tokens: int | None = None,
) -> None:
    """Append an ``exec`` operation entry to the run journal.

    Records the executed commands, exit code, and metadata (output
    size, boundary crossing) under the run id resolved from
    *container_id*.
    """
    run_id = get_or_create_run_id(container_id)
    boundary = allow_network
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "exec",
        "commands": commands,
        "exit_code": exit_code,
        "verbose": verbose,
        "boundary_crossing": boundary,
        "output_size": output_size,
    }
    if max_output_tokens is not None:
        entry["max_output_tokens"] = max_output_tokens
    _append_json(entry)
    _update_container_state(container_id, used=True)


def record_stop(container_id: str) -> None:
    """Record a container stop event."""
    run_id = get_or_create_run_id(container_id)
    _append_json({
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "stop",
    })
    _update_container_state(container_id, stopped=True)
    remove_run_id(container_id)


def record_boundary_crossing(
    container_id: str,
    operation: str,
    details: str,
    approved: bool | None = None,
) -> None:
    """Record a boundary-crossing operation.

    *approved* is ``None`` when no approval was required (e.g. read-only
    VCS access that only needs journal recording).
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



def record_tool_use(
    container_id: str,
    tool_name: str,
    params: dict[str, Any] | None = None,
) -> None:
    """Record a structured-tool usage event (read / verify / lint / type).

    Lightweight record for tools that don't run arbitrary shell commands
    (e.g. ``read_file_range``, ``list_files``, ``search_in_container``,
    ``lint_in_container``, ``type_check_in_container``,
    ``verify_in_container``).  Fixes the bypass-rate overcount on the
    #229 tool-usage dashboard by adding dedicated-tool entries to the
    journal alongside the ``exec`` entries they replace.

    *params* is an optional dict of tool-specific parameters (file path,
    search pattern, language, etc.) for audit context.
    """
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "tool_use",
        "tool_name": tool_name,
    }
    if params:
        entry["params"] = params
    _append_json(entry)


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------


def _read_journal_unlocked() -> list[dict[str, Any]]:
    """Parse every journal line into dicts (caller must hold ``_lock``)."""
    if not _JOURNAL_PATH.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(_JOURNAL_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def read_journal(
    run_id: str | None = None,
    max_entries: int | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> list[dict[str, Any]]:
    """Read journal entries, optionally filtered by *run_id* and/or time range.

    Args:
        run_id: If provided, only return entries for this run.
        max_entries: Maximum number of entries to return (most recent
            first when specified).
        from_ts: Inclusive lower bound for ``ts`` (ISO format).
        to_ts: Exclusive upper bound for ``ts`` (ISO format).

    Returns:
        List of journal entry dicts, oldest first.
    """
    with _lock:
        raw = _read_journal_unlocked()

    entries: list[dict[str, Any]] = []
    for entry in raw:
        if run_id is not None and entry.get("run_id") != run_id:
            continue
        ts = entry.get("ts", "")
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts >= to_ts:
            continue
        entries.append(entry)

    if max_entries is not None and len(entries) > max_entries:
        entries = entries[-max_entries:]

    return entries


def get_journal_path() -> str:
    """Return the absolute path to the journal log file."""
    return str(_JOURNAL_PATH)


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
            in_single = False
            in_double = False
            i = 0
            while i < idx:
                ch = cmd[i]
                if ch == '\\' and i + 1 < idx:
                    i += 2
                    continue
                if ch == "'" and not in_double:
                    in_single = not in_single
                elif ch == '"' and not in_single:
                    in_double = not in_double
                i += 1
            if not in_single and not in_double:
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
        - ``bypass_detail`` — ``{shell_command: count}`` breakdown of bypassed commands
        - ``exec_entry_count`` — total number of exec *entries* (not sub-commands)
        - ``_tool_intro_dates`` — ``{tool: intro_date}`` mapping for bias correction
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

    entries = read_journal(from_ts=from_iso, to_ts=to_iso)

    total_ops = 0
    exec_ops = 0
    exec_entry_count = 0
    command_buckets: dict[str, int] = {}
    cd_count = 0
    structured_ops: dict[str, int] = {}
    bypass_count = 0
    bypass_detail: dict[str, int] = {}

    for entry in entries:

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
                    if tool_intro and entry.get("ts", "")[:10] >= tool_intro:
                        bypass_count += 1
                        bypass_detail[first_word] = bypass_detail.get(first_word, 0) + 1

        elif op == "tool_use":
            tool_name = entry.get("tool_name", "")
            key = tool_name if tool_name else "tool_use:unknown"
            structured_ops[key] = structured_ops.get(key, 0) + 1
            total_ops += 1

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
