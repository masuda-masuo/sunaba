"""Append-only execution journal for post-hoc audit (§9).

Writes JSON-lines records to ``~/.sunaba/journal.log``.
Every container lifecycle event (initialize, exec, stop) and
boundary-crossing operation is recorded with timestamp, run_id,
and operational metadata.

Thread-safe via a module-level lock.  The journal is append-only
by design — no record is ever deleted or overwritten.  When the
journal exceeds :data:`_MAX_JOURNAL_SIZE` the current file is
rotated to ``journal.log.1`` before new writes continue, so the
on-disk footprint is bounded by approximately twice the max size
(one active file + one backup).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sunaba.output_control import mask_tokens

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JOURNAL_DIR: Path = Path.home() / ".sunaba"
_JOURNAL_PATH: Path = _JOURNAL_DIR / "journal.log"
_JOURNAL_BACKUP_PATH: Path = _JOURNAL_DIR / "journal.log.1"

#: Max journal file size before rotation (100 MB).
_MAX_JOURNAL_SIZE: int = 100 * 1024 * 1024


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
# Run ID mapping / Session label
# ---------------------------------------------------------------------------

#: Maps container ID prefixes → run IDs so that all operations on the
#: same container share a run_id.
_run_map: dict[str, str] = {}
_run_map_lock: threading.Lock = threading.Lock()

#: Maps container ID prefixes → session labels so that all operations
#: on the same container (for the current attach session) share a label.
_session_map: dict[str, str] = {}
_session_map_lock: threading.Lock = threading.Lock()


def set_session_label(container_id: str, label: str | None) -> None:
    """Set the session label for *container_id*.

    Pass ``None`` to clear.  When set, all subsequent journal entries
    for this container include ``session_label``.
    """
    with _session_map_lock:
        if label is None:
            _session_map.pop(container_id, None)
        else:
            _session_map[container_id] = label


def get_session_label(container_id: str) -> str | None:
    """Return the current session label for *container_id*, or ``None``."""
    with _session_map_lock:
        return _session_map.get(container_id)


def generate_run_id() -> str:
    """Generate a new unique run identifier."""
    return uuid.uuid4().hex[:12]


def get_or_create_run_id(container_id: str) -> str:
    """Return the run_id for *container_id*, creating one if needed."""
    with _run_map_lock:
        if container_id not in _run_map:
            _run_map[container_id] = generate_run_id()
        return _run_map[container_id]


#: Process-lifetime run id for host-scoped (container-less) operations.
_host_run_id: str | None = None
_host_run_id_lock: threading.Lock = threading.Lock()


def get_host_run_id() -> str:
    """Return the run_id shared by all container-less operations (#778).

    Host-scoped boundary crossings (e.g. ``sandbox_issue_write`` without a
    container) are grouped under one run per server process, so the audit
    trail stays attributable without a throwaway container.

    Process-local, like every run-id map here: assumes the single-process
    FastMCP server.  A forked child would inherit and reuse the parent's id.
    """
    global _host_run_id
    with _host_run_id_lock:
        if _host_run_id is None:
            _host_run_id = "host-" + generate_run_id()
        return _host_run_id


def remove_run_id(container_id: str) -> None:
    """Remove the run_id mapping when a container is stopped."""
    with _run_map_lock:
        _run_map.pop(container_id, None)
    set_session_label(container_id, None)


# ---------------------------------------------------------------------------
# Core write
# ---------------------------------------------------------------------------


def _rotate_if_needed_unlocked() -> None:
    """Rename journal.log to journal.log.1 when size exceeds limit.

    Called under ``_lock``.  If the backup already exists it is
    silently overwritten (oldest history is discarded first).
    """
    if _JOURNAL_PATH.exists() and _JOURNAL_PATH.stat().st_size >= _MAX_JOURNAL_SIZE:
        _JOURNAL_PATH.replace(_JOURNAL_BACKUP_PATH)


def _mask_entry(value: Any) -> Any:
    """Mask credential values anywhere inside a journal entry.

    ``record_exec`` stores the shell commands verbatim, so a command that
    carries a token (``export GITHUB_TOKEN=...``) used to land in
    ``journal.log`` in clear text -- measured, not theorised: a probe token
    written through ``sandbox_exec`` was recoverable from the file
    afterwards.  ``mask_tokens`` already existed for this and its docstring
    even claims it protects "journal records", but nothing on the journal
    path called it.

    Masking here rather than at the four ``record_exec`` call sites is
    deliberate: a per-call-site fix is the shape that already failed once,
    and it would leave every future field unprotected.  ``_append_json`` is
    the single door every entry goes through.

    Masking runs on the values *before* serialisation, never on the JSON
    text: the token pattern ends with an optional quote, so applying it to
    a serialised line can eat the quote that terminates a JSON string and
    corrupt the record.
    """
    if isinstance(value, str):
        return mask_tokens(value)
    if isinstance(value, dict):
        return {k: _mask_entry(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_entry(v) for v in value]
    return value


def _append_json(entry: dict[str, Any]) -> None:
    """Append a single JSON-lines record to the journal."""
    _ensure_dir()
    with _lock:
        _rotate_if_needed_unlocked()
        with open(_JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(_mask_entry(entry), ensure_ascii=False) + "\n")


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
    """Atomically write the sidecar states (caller must hold ``_lock``).

    Uses a PID-suffixed tmp file so that concurrent processes (e.g. xdist
    workers) do not race on the same ``.tmp`` name (Issue #590).
    """
    _ensure_dir()
    tmp = _get_state_path().with_suffix(f".tmp.{os.getpid()}")
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
            {
                "complete": False,
                "used": False,
                "stopped": False,
                "init_ts": None,
                "progress_ts": None,
            },
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
        if op not in ("initialize", "initialize_complete", "initialize_progress", "exec"):
            continue
        s = states.setdefault(
            cid,
            {
                "complete": False,
                "used": False,
                "stopped": False,
                "init_ts": None,
                "progress_ts": None,
            },
        )
        if op == "initialize":
            s["init_ts"] = entry.get("ts")
        elif op == "initialize_complete":
            s["complete"] = True
        elif op == "initialize_progress":
            # Journal entries are read in append order, so the last write
            # wins: this tracks the NEWEST progress timestamp (Issue #806).
            s["progress_ts"] = entry.get("ts")
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
    session_label: str | None = None,
) -> None:
    """Record a container initialization event.

    Args:
        container_id: 12-character container ID prefix.
        image: Docker image used.
        allow_network: Whether network access was granted.
        mem_limit: Override mem_limit if specified (Issue #201).
        cpus: Override cpus if specified (Issue #201).
        session_label: Optional session identifier (e.g. model name,
            task name) for this session (Issue #479).
    """
    if session_label is not None:
        set_session_label(container_id, session_label)
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "initialize",
        "image": image,
        "allow_network": allow_network,
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
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
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "initialize_complete",
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    _append_json(entry)
    _update_container_state(container_id, complete=True)


def record_initialize_progress(container_id: str, note: str) -> None:
    """Record an in-progress marker during ``sandbox_initialize`` (Issue #806).

    Init-time dependency installs (``_install_repo_deps``) run ``exec_run``
    directly and journal nothing of their own, so during a long pip/npm step
    the container looks journal-idle and a concurrent session's opportunistic
    orphan GC could reap it mid-install.  Each installer step writes this
    marker *before* it starts; the orphan reaper computes the container's age
    from the newest of ``init_ts`` / ``progress_ts``, so an init whose last
    marker is inside the grace window is never reaped.

    *note* is a short human-readable step description (e.g. ``"deps: pip
    install"``).  The marker is best-effort at the call sites: a journal
    write failure must never break the install.
    """
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "initialize_progress",
        "note": note,
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    _append_json(entry)
    _update_container_state(container_id, progress_ts=entry["ts"])


def record_exec(
    container_id: str,
    commands: list[str],
    exit_code: int,
    verbose: str = "summary",
    allow_network: bool = False,
    output_size: int = 0,
    max_output_tokens: int | None = None,
    session_label: str | None = None,
) -> None:
    """Append an ``exec`` operation entry to the run journal.

    Records the executed commands, exit code, and metadata (output
    size, boundary crossing) under the run id resolved from
    *container_id*.
    """
    if session_label is not None:
        set_session_label(container_id, session_label)
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
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    if max_output_tokens is not None:
        entry["max_output_tokens"] = max_output_tokens
    _append_json(entry)
    _update_container_state(container_id, used=True)


def record_exec_start(
    container_id: str,
    commands: list[str],
    verbose: str = "summary",
    allow_network: bool = False,
    session_label: str | None = None,
) -> None:
    """Append an ``exec`` START entry to the run journal (Issue #789).

    A foreground ``sandbox_exec`` can run for minutes without any other
    journal activity, so the completion-only record made the run look idle
    (and, on the dashboard, ``stalled``) while it was busy.  This call is
    recorded *before* execution begins; the completion entry
    (:func:`record_exec`, unchanged) follows when the exec finishes.

    The two entries are distinguished by the presence of ``exit_code``:
    the start entry has none (and no ``output_size`` / ``max_output_tokens``
    -- nothing outcome-shaped), the completion entry carries them.  Readers
    that count exec *calls* count the start entry; readers that need the
    outcome read the completion entry.  Background-exec dispatch sentinels
    are written via :func:`record_exec` with ``exit_code=-1`` and are
    untouched (Issue #359).
    """
    if session_label is not None:
        set_session_label(container_id, session_label)
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "exec",
        "commands": commands,
        "verbose": verbose,
        "boundary_crossing": allow_network,
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    _append_json(entry)
    _update_container_state(container_id, used=True)


def record_busy_refusal(
    pool: str,
    limit: str,
    cap: int,
    *,
    tool: str | None = None,
    container_id: str | None = None,
) -> None:
    """Record a docker/recovery concurrency-cap refusal (Issue #783).

    Written when :func:`sunaba.tools.common.docker_bound` /
    ``recovery_bound`` refuse a call because a pool is saturated (Issue
    #784 busy JSON).  Refusals are the observable form of "initialize
    wait" in the post-#784 world: acquisition is non-blocking, so a
    saturated pool *refuses* instead of queueing, and per-pool refusal
    counts are surfaced on the /insights page as a proxy metric.

    The entry is attributed to the refused call's container run when that
    run already exists (its per-container cap may be the reason);
    otherwise -- no container named, or the container is unknown to the
    process-local run map (e.g. it survived a server restart) -- it goes
    to the host run.  A refusal must never MINT a run: a refusal-only run
    has zero operations, shows up as a phantom "running" row and turns
    into a false ``stalled`` badge (#783 review), while host runs are
    exempt from stalled classification (#792) and the /insights refusal
    metric is keyed by pool, not by run, so nothing is lost.  The
    ``container_id`` field still records which container was concerned.
    Best-effort by design: the refusal path must never crash because
    observation failed, so any journaling error is swallowed here (the
    busy JSON is still returned to the caller).
    """
    try:
        run_id = None
        if container_id:
            with _run_map_lock:
                run_id = _run_map.get(container_id)
        if run_id is None:
            run_id = get_host_run_id()
        entry: dict[str, Any] = {
            "ts": _utcnow_iso(),
            "run_id": run_id,
            "container_id": container_id,
            "operation": "busy_refusal",
            "pool": pool,
            "limit": limit,
            "cap": cap,
        }
        if tool:
            entry["tool"] = tool
        _append_json(entry)
    except Exception:
        pass  # best-effort observation (see docstring)


def record_stop(container_id: str) -> None:
    """Record a container stop event."""
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "stop",
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    _append_json(entry)
    _update_container_state(container_id, stopped=True)
    remove_run_id(container_id)
    # A stopped container's files are gone; hooking here (the single
    # funnel every stop/reap path goes through) keeps the invariant
    # "stopped container => no stale undo snapshots" in one place.
    from sunaba import undo
    undo.clear_history(container_id)


def record_boundary_crossing(
    container_id: str | None,
    operation: str,
    details: str,
    approved: bool | None = None,
    session_label: str | None = None,
) -> None:
    """Record a boundary-crossing operation.

    *approved* is ``None`` when no approval was required (e.g. read-only
    VCS access that only needs journal recording).

    *container_id* is ``None`` for host-scoped operations that involve no
    container (#778); they share the process-lifetime host run_id and the
    sidecar container state is untouched.
    """
    if container_id is None:
        entry: dict[str, Any] = {
            "ts": _utcnow_iso(),
            "run_id": get_host_run_id(),
            "container_id": None,
            "operation": "boundary_crossing",
            "sub_operation": operation,
            "details": details,
            "approved": approved,
        }
        if session_label is not None:
            entry["session_label"] = session_label
        _append_json(entry)
        return
    if session_label is not None:
        set_session_label(container_id, session_label)
    run_id = get_or_create_run_id(container_id)
    entry = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "boundary_crossing",
        "sub_operation": operation,
        "details": details,
        "approved": approved,
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
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
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": "write_file",
        "file_name": file_name,
        "dest_dir": dest_dir,
        "byte_count": byte_count,
        "is_test": is_test,
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    _append_json(entry)


def record_copy(
    container_id: str,
    operation: str,  # "copy_project" | "copy_file"
    local_src: str,
    dest_dir: str,
) -> None:
    """Record a file/directory copy into the container."""
    run_id = get_or_create_run_id(container_id)
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "container_id": container_id,
        "operation": operation,
        "local_src": local_src,
        "dest_dir": dest_dir,
    }
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    _append_json(entry)



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
    label = get_session_label(container_id)
    if label is not None:
        entry["session_label"] = label
    if params:
        entry["params"] = params
    _append_json(entry)


def record_capture_health(
    container_id: str | None,
    state: str,
    *,
    consecutive_empty: int,
    canary_nonce_found: bool | None = None,
    canary_error: str | None = None,
) -> None:
    """Record a capture-health guard state change (issue #852).

    Written when the guard trips (``state="broken"``) or clears
    (``state="recovered"``), carrying the consecutive-empty counter value
    at the transition and the canary result, so post-incident forensics
    does not depend on client-side logs.  ``canary_nonce_found`` is
    ``None`` when no canary ran (e.g. recovery observed from a non-empty
    decode rather than a re-probe).

    The entry is attributed to the triggering call's container run when
    one exists; a guard call without container attribution falls back to
    the host run (mirroring :func:`record_busy_refusal`).
    """
    run_id = get_or_create_run_id(container_id) if container_id else get_host_run_id()
    entry: dict[str, Any] = {
        "ts": _utcnow_iso(),
        "run_id": run_id,
        "operation": "capture_health",
        "state": state,
        "consecutive_empty": consecutive_empty,
        "canary_nonce_found": canary_nonce_found,
    }
    if container_id:
        entry["container_id"] = container_id
    if canary_error is not None:
        entry["canary_error"] = canary_error
    _append_json(entry)


# ---------------------------------------------------------------------------
# Journal reading
# ---------------------------------------------------------------------------


def _read_journal_unlocked() -> list[dict[str, Any]]:
    """Parse every journal line into dicts (caller must hold ``_lock``).

    Reads from both ``journal.log.1`` (backup, if present) and
    ``journal.log`` (active), concatenating in chronological order.
    """
    entries: list[dict[str, Any]] = []

    def _load(path: Path) -> None:
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    _load(_JOURNAL_BACKUP_PATH)
    _load(_JOURNAL_PATH)
    return entries


def read_journal(
    run_id: str | None = None,
    max_entries: int | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
    session_label: str | None = None,
) -> list[dict[str, Any]]:
    """Read journal entries, optionally filtered by *run_id* and/or time range.

    Args:
        run_id: If provided, only return entries for this run.
        max_entries: Maximum number of entries to return (most recent
            first when specified).
        from_ts: Inclusive lower bound for ``ts`` (ISO format).
        to_ts: Exclusive upper bound for ``ts`` (ISO format).
        session_label: If provided, only return entries with this
            session label (Issue #479).

    Returns:
        List of journal entry dicts, oldest first.
    """
    with _lock:
        raw = _read_journal_unlocked()

    entries: list[dict[str, Any]] = []
    for entry in raw:
        if run_id is not None and entry.get("run_id") != run_id:
            continue
        if session_label is not None and entry.get("session_label") != session_label:
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


def read_journal_tail(
    offset: int = 0,
    generation: int | None = None,
) -> tuple[list[dict[str, Any]], int, bool, int]:
    """Read complete journal lines from a byte *offset* in the live file.

    Tail-following reader for the dashboard's diff-polling endpoint (#776).
    Unlike :func:`read_journal` it never touches ``journal.log.1``: the
    caller keeps a byte offset into the live file and we hand back only the
    lines that became visible after it, so a poll costs O(new bytes).

    Args:
        offset: Byte position in the live ``journal.log`` to read from.
            0 (the default) means "from the beginning of the live file".
        generation: The file-identity token the caller received with its
            previous read (``None`` on the first poll).  Rotation replaces
            the live file, so a size check alone misses the case where the
            *new* file has already grown past the caller's offset; the
            generation (the file's inode number) catches it regardless of
            sizes.  On platforms whose ``st_ino`` is always 0 the check
            degrades to the size-based detection.

    Returns:
        ``(entries, next_offset, rotated, generation)``:

        * *entries*: parsed JSON-lines covering ``[offset, next_offset)``,
          oldest first.
        * *next_offset*: the byte position the caller should poll from next.
        * *rotated*: ``True`` when the live file was replaced (its
          generation changed) or is *smaller* than *offset*.  The caller
          must reset to offset 0 and fully re-draw; the backup file is
          deliberately never spliced into the delta.
        * *generation*: the live file's current identity token; echo it back
          on the next call.

    A partial trailing line (a record still being written, so no newline
    yet) is never returned: *next_offset* stays at the start of that line
    and the next poll re-reads it once it is complete.
    """
    with _lock:
        if not _JOURNAL_PATH.exists():
            return [], 0, offset > 0, 0
        st = _JOURNAL_PATH.stat()
        size = st.st_size
        current_gen = st.st_ino
        if generation is not None and current_gen and generation != current_gen:
            return [], 0, True, current_gen
        if offset > size:
            return [], size, True, current_gen
        if offset < 0:
            offset = 0
        try:
            with open(_JOURNAL_PATH, "rb") as f:
                f.seek(offset)
                data = f.read()
        except (OSError, OverflowError, ValueError):
            # File vanished/replaced between stat and open, or an offset the
            # seek cannot represent: nothing deliverable this round.  The
            # next poll re-checks (rotation detection or fresh data), so the
            # connection is never left hanging on an exception.
            return [], offset, False, current_gen

    # Byte-based slicing: the offset must stay a byte position even though
    # entries are UTF-8 (ensure_ascii=False), so each line is decoded on its
    # own after splitting the raw bytes.
    last_nl = data.rfind(b"\n")
    if last_nl < 0:
        # Nothing complete yet (or offset == size): poll again at the same
        # position once the line is finished.
        return [], offset, False, current_gen
    complete = data[: last_nl + 1]
    next_offset = offset + len(complete)

    entries: list[dict[str, Any]] = []
    for line in complete.split(b"\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    return entries, next_offset, False, current_gen


def read_journal_snapshot(
    run_id: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Read the whole journal once; return ``(entries, live_offset)``.

    One atomic snapshot for live-update consumers (dashboard trace page,
    #776): *entries* is the backup file's records followed by the live
    file's complete lines (chronological order, optionally filtered by
    *run_id*), and *live_offset* is the byte position just after the live
    file's last complete line -- exactly where a diff poll should resume.

    Rows rendered from *entries* and polls started at *live_offset*
    partition the live file without gap or overlap: a record appended
    between the render and the first poll is delivered by the poller, and
    nothing already rendered is re-delivered.  A partial trailing line is
    excluded (as in :func:`read_journal_tail`) and its start lies below
    *live_offset*, so the next poll picks it up once the newline arrives.
    """
    with _lock:
        entries: list[dict[str, Any]] = []

        def _load(path: Path) -> int:
            """Parse *path* into entries; return the byte offset after its
            last complete line (a partial trailing line excluded)."""
            if not path.exists():
                return 0
            with open(path, "rb") as f:
                data = f.read()
            last_nl = data.rfind(b"\n")
            if last_nl < 0:
                return 0
            complete = data[: last_nl + 1]
            for line in complete.split(b"\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line.decode("utf-8")))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
            return len(complete)

        _load(_JOURNAL_BACKUP_PATH)
        live_offset = _load(_JOURNAL_PATH)

    if run_id is not None:
        entries = [e for e in entries if e.get("run_id") == run_id]
    return entries, live_offset


def get_journal_live_size() -> int:
    """Return the current byte size of the live ``journal.log`` (0 if absent)."""
    with _lock:
        if not _JOURNAL_PATH.exists():
            return 0
        return _JOURNAL_PATH.stat().st_size


def get_journal_path() -> str:
    """Return the absolute path to the journal log file."""
    return str(_JOURNAL_PATH)


def get_journal_dir() -> str:
    """Return the absolute path of the journal directory (``~/.sunaba``).

    The directory also holds the trace files (``~/.sunaba/traces``,
    Issue #783 disk observation) and the container-state sidecar, so its
    size is the "journal + trace" component of the dashboard's disk
    usage card.
    """
    return str(_JOURNAL_DIR)


def get_runs() -> list[dict[str, Any]]:
    """Return a summary of all runs found in the journal."""
    if not _JOURNAL_PATH.exists() and not _JOURNAL_BACKUP_PATH.exists():
        return []

    runs: dict[str, dict[str, Any]] = {}
    for entry in read_journal():
        rid = entry.get("run_id", "")
        if rid not in runs:
            runs[rid] = {
                "run_id": rid,
                "started": entry.get("ts"),
                "image": entry.get("image", "unknown"),
                "session_labels": set(),
                "operations": 0,
                "boundary_crossings": 0,
                "vcs_operations": 0,
                "last_ts": entry.get("ts"),
                # Host-scoped runs (#778) have no lifecycle: never "running",
                # never stopped -- a distinct status keeps them out of the
                # dashboard's live-container semantics.
                "status": "host" if rid.startswith("host-") else "running",
            }
        run = runs[rid]
        run["operations"] += 1
        run["last_ts"] = entry.get("ts")
        sl = entry.get("session_label")
        if sl:
            run["session_labels"].add(sl)
        if entry.get("operation") == "stop":
            run["status"] = "stopped"
        if entry.get("boundary_crossing") or entry.get("operation") == "boundary_crossing":
            run["boundary_crossings"] += 1
            sub_op = entry.get("sub_operation", "")
            if sub_op in ("issue_view", "publish"):
                run["vcs_operations"] += 1

    result = sorted(runs.values(), key=lambda r: r.get("started", ""), reverse=True)
    for r in result:
        labels = r.get("session_labels", set())
        r["session_labels"] = sorted(labels) if labels else []
    return result


def get_active_environments() -> list[dict[str, Any]]:
    """Return currently active test environments from journal entries.

    Returns a list of environments with status ``"starting"`` or
    ``"ready"`` that have no corresponding ``"stopped"`` entry.

    Reads from both ``journal.log.1`` (backup, if present) and
    ``journal.log`` (active) via :func:`_read_journal_unlocked`,
    so entries survive journal rotation transparently.
    """
    env_entries: list[dict[str, Any]] = []
    with _lock:
        for entry in _read_journal_unlocked():
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
    "write_file_sandbox": "2026-06-07",  # retired 2026-07-17 (#630 split)
    "write_file": "2026-07-17",
    "edit_file": "2026-07-17",
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


#: Shell operators that make a command *compound* rather than simple.
#  A dedicated tool can only stand in for a single program invocation;
#  once a command pipes, chains, substitutes or redirects it is an exec
#  mini-program with no structured equivalent (issue #846).
_COMPOUND_SHELL_OPERATORS: tuple[str, ...] = (
    "|", ";", "&&", "||", "$(", "`", ">", ">>", "<",
)


def _is_simple_command(cmd: str) -> bool:
    """True when *cmd* carries none of the compound shell operators.

    A substring test, deliberately not a shell parse: an operator hidden
    inside quotes (``grep 'a|b' f``) reads as compound and so drops out
    of the bypass count.  The metric wants precision over recall -- a
    missed bypass costs less than a legitimate mini-program counted as
    one -- and even a trailing ``| head -20`` is enough to make the
    command something no single tool call replaces.
    """
    return not any(op in cmd for op in _COMPOUND_SHELL_OPERATORS)


# The container's default working directory (= the clone destination,
# see docs/design_filesystem_layout.md).  A ``cd`` here is a no-op, which
# is what separates the ``cd-redundant`` bucket from a real relocation
# (issue #845).  Compared literally: the journal records no per-container
# clone_dest, and plumbing container state into the report for the rare
# non-default case is not worth it -- the approximation over-counts
# ``cd`` (real) at worst, never ``cd-redundant``.
_DEFAULT_CWD = "/workspace"


def _is_default_cwd(target: str) -> bool:
    """True when *target* names the default working directory.

    Tolerates a trailing slash and surrounding quotes, both of which the
    shell strips before ``cd`` sees the path.
    """
    return target.strip().strip("\"'").rstrip("/") == _DEFAULT_CWD


def classify_exec_command(cmd: str) -> str:
    """Classify a single shell command string into a bucket category.

    The first token (after stripping leading whitespace) determines the
    bucket.  Special-case detection for piped / chained commands is
    handled by checking for ``&&`` and ``;`` separators — in that case
    only the first sub-command is used for classification.

    ``cd`` splits in two: ``cd-redundant`` when the target is the default
    working directory (a no-op), plain ``cd`` for a real relocation.
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
        if len(tokens) == 2 and _is_default_cwd(tokens[1]):
            return "cd-redundant"
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
          (every cd, redundant or not — unchanged since the metric shipped)
        - ``cd_rate_pct`` — cd entries as % of exec entries
        - ``cd_redundant_count`` — the subset of ``cd_count`` that cd's to the
          default working directory (``/workspace``), i.e. a no-op
        - ``cd_redundant_rate_pct`` — redundant cds as % of exec entries
        - ``structured_ops`` — count of each structured tool operation
        - ``bypass_count`` — exec commands that could have used a dedicated
          tool: the first word maps to one that already existed, *and* the
          command is simple (issue #846).  Simple means it contains none of
          ``|  ;  &&  ||  $(  `  >  >>  <`` — a trailing ``| head -20`` is
          not tolerated, since no single tool call replaces a pipeline.
        - ``bypass_rate_pct`` — bypass as % of (dedicated + bypass)
        - ``bypass_detail`` — ``{shell_command: count}`` breakdown of bypassed commands
        - ``compound_shell_count`` — first commands that map to a dedicated
          tool but are compound: the legitimate-exec baseline.  Counted here
          *instead of* in ``bypass_count`` / ``bypass_detail``, never both.
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
    cd_redundant_count = 0
    structured_ops: dict[str, int] = {}
    bypass_count = 0
    bypass_detail: dict[str, int] = {}
    compound_shell_count = 0

    for entry in entries:

        op = entry.get("operation", "")
        if op in (
            "initialize",
            "initialize_complete",
            "initialize_progress",
            "stop",
            "test_environment",
            # Issue #783: concurrency-cap refusals are resource events, not
            # tool usage -- they are aggregated on the /insights page.
            "busy_refusal",
        ):
            continue

        if op == "exec":
            # Issue #789: every foreground exec journals a start entry
            # (no ``exit_code``) before running and the completion after;
            # count each exec once, from its completion.
            if "exit_code" not in entry:
                continue
            exec_ops += 1
            exec_entry_count += 1
            commands = entry.get("commands", [])

            if commands and isinstance(commands, list):
                first_cmd = commands[0] if commands else ""
                bucket = classify_exec_command(first_cmd)
                command_buckets[bucket] = command_buckets.get(bucket, 0) + 1

                # cd_count stays "every leading cd" so the historical
                # rate remains comparable; the redundant share is the
                # new signal (issue #845).
                if bucket in ("cd", "cd-redundant"):
                    cd_count += 1
                    if bucket == "cd-redundant":
                        cd_redundant_count += 1

                # Bypass detection: does a structured tool exist for this command?
                first_word = first_cmd.strip().split()[0] if first_cmd.strip() else ""
                first_word = first_word.rstrip(";")
                tool = _SHELL_TO_TOOL.get(first_word)
                if tool:
                    tool_intro = _TOOL_INTRO_DATES.get(tool, "")
                    # Only count as bypass if the tool existed at the time
                    if tool_intro and entry.get("ts", "")[:10] >= tool_intro:
                        # ...and only when the tool could actually have
                        # stood in for the command.  A pipeline or chain
                        # is an exec mini-program, so it lands in the
                        # legitimate-exec baseline instead (issue #846).
                        if _is_simple_command(first_cmd):
                            bypass_count += 1
                            bypass_detail[first_word] = bypass_detail.get(first_word, 0) + 1
                        else:
                            compound_shell_count += 1

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
    cd_redundant_rate_pct = (
        round(cd_redundant_count / exec_entry_count * 100, 1) if exec_entry_count else 0.0
    )

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
        "cd_redundant_count": cd_redundant_count,
        "cd_redundant_rate_pct": cd_redundant_rate_pct,
        "structured_ops": dict(sorted(structured_ops.items(), key=lambda x: -x[1])),
        "bypass_count": bypass_count,
        "bypass_rate_pct": bypass_rate_pct,
        "bypass_detail": dict(sorted(bypass_detail.items(), key=lambda x: -x[1])),
        "compound_shell_count": compound_shell_count,
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


def get_last_activity_per_container() -> dict[str, str]:
    """Return the most recent activity timestamp for each container.

    Scans the full journal for ``ts`` fields grouped by ``container_id``.
    Containers with a ``stop`` entry are excluded (they are no longer
    active).  The result is a ``{container_id: last_ts}`` mapping where
    *last_ts* is the ISO-8601 timestamp of the most recent journal entry
    for that container.
    """
    entries = read_journal()
    last_ts: dict[str, str] = {}
    seen_stopped: set[str] = set()
    for entry in entries:
        cid = entry.get("container_id")
        if not cid:
            continue
        if entry.get("operation") == "stop":
            seen_stopped.add(cid)
            last_ts.pop(cid, None)
            continue
        ts = entry.get("ts", "")
        if ts:
            last_ts[cid] = ts
    for cid in seen_stopped:
        last_ts.pop(cid, None)
    return last_ts


def get_run_id_per_container() -> dict[str, str]:
    """Return the most recent ``run_id`` for each container (Issue #527).

    Read from the journal rather than the in-memory ``_run_map``, which is
    lost on server restart -- the containers the dashboard most wants to show
    a trace link for are precisely the ones that outlived the process that
    created them.  Containers with a ``stop`` entry are excluded, matching
    :func:`get_last_activity_per_container`.
    """
    entries = read_journal()
    run_ids: dict[str, str] = {}
    for entry in entries:
        cid = entry.get("container_id")
        if not cid:
            continue
        if entry.get("operation") == "stop":
            run_ids.pop(cid, None)
            continue
        run_id = entry.get("run_id")
        if run_id:
            run_ids[cid] = run_id
    return run_ids


# ---------------------------------------------------------------------------
# Concurrent-container timeline (Issue #783, observation 1)
# ---------------------------------------------------------------------------


def timeline_from_lifecycle(lifecycle: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Convert a container-lifecycle map into the concurrency timeline.

    *lifecycle* maps ``container_id`` to
    ``{"init_ts": str|None, "stop_ts": str|None}``.  Each container whose
    lifetime was journaled contributes an ``initialize`` event (count +1)
    and, when a stop is recorded, a ``stop`` event (count -1); events are
    sorted by timestamp and the running count is sampled after every
    event.  A ``stop`` without a journaled ``initialize`` is outside the
    timeline contract and contributes nothing (it cannot close a lifetime
    that was never observed open).

    Returns ``{"current": int, "peak": int, "series": [...]}``:

    * ``current`` -- the count after the last event (0 when the journal
      holds no lifecycle events),
    * ``peak`` -- the maximum concurrently-running count ever observed,
    * ``series`` -- oldest-first ``{"ts": str, "count": int}`` samples,
      one per event.

    Pure function -- the single source of truth shared by the journal
    full-scan (:func:`container_concurrency_timeline`) and the
    dashboard's incremental cache (Issue #789), which maintains the
    lifecycle map from journal deltas.
    """
    events: list[tuple[str, int]] = []
    for lc in lifecycle.values():
        if not lc.get("init_ts"):
            continue  # stop without a journaled initialize: skip
        events.append((lc["init_ts"], 1))
        if lc.get("stop_ts"):
            events.append((lc["stop_ts"], -1))
    events.sort(key=lambda e: e[0])

    count = 0
    peak = 0
    series: list[dict[str, Any]] = []
    for ts, delta in events:
        count += delta
        if count > peak:
            peak = count
        series.append({"ts": ts, "count": count})
    return {"current": count, "peak": peak, "series": series}


def container_concurrency_timeline(
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconstruct the concurrent-container timeline from journal entries.

    Builds the lifecycle map from *entries* (default: the whole journal)
    and feeds it through :func:`timeline_from_lifecycle`: an
    ``initialize`` entry opens a container's lifetime, a ``stop`` entry
    closes it, and a re-``initialize`` after a stop reopens it (matching
    the sidecar's reset-on-init semantics in
    :func:`_rebuild_states_unlocked`).  Host-run entries (no
    ``container_id``) are ignored.

    The result is the raw material for the dashboard's "Concurrent
    Containers" observation (Issue #783): the current count, the all-time
    peak, and the trend (a timestamped series of running counts).
    """
    if entries is None:
        with _lock:
            entries = _read_journal_unlocked()
    lifecycle: dict[str, dict[str, Any]] = {}
    for entry in entries:
        cid = entry.get("container_id")
        if not cid:
            continue
        op = entry.get("operation")
        if op == "initialize":
            lifecycle[cid] = {"init_ts": entry.get("ts"), "stop_ts": None}
        elif op == "stop":
            lc = lifecycle.setdefault(cid, {"init_ts": None, "stop_ts": None})
            lc["stop_ts"] = entry.get("ts")
    return timeline_from_lifecycle(lifecycle)
