"""Deterministic phase aggregation for the execution journal.

Maps every journal operation into one of six fixed phases
(``init``, ``explore``, ``edit``, ``verify``, ``publish``, ``other``)
via a single lookup table.  The core function :func:`aggregate_run_phases`
has the incremental contract ``(state, new_entries) → state`` so live-update
consumers can push tail deltas through the same function.

Each per-run state also carries health-relevant markers for the rule-based
classifier in :mod:`sunaba.health` (Issue #775): ``published`` (a
``boundary:publish`` crossing was seen), ``stopped`` (a ``stop`` entry was
seen) and ``last_op`` (the effective operation of the most recent entry).
The markers are monotonic and idempotent, so the incremental contract is
preserved.

No LLM calls, no new journal record types, no agent-facing MCP tools.
"""

from __future__ import annotations

import re
from typing import Any

#: Extracts the repo slug from a ``boundary:clone_repo`` details string,
#: e.g. ``"repo=owner/name dest=/workspace proxy_read_grant=True"``.
_REPO_RE = re.compile(r"repo=(\S+)")

# ── Phase mapping ───────────────────────────────────────────────────────────
#
# Single source of truth.  Each key is the *effective* operation identifier
# resolved from a journal entry:
#
#   * direct        → ``"op"``          (e.g. ``"initialize"``)
#   * tool_use      → ``"tool:TNAME"``  (e.g. ``"tool:read_file_range"``)
#   * boundary      → ``"boundary:SUB"``(e.g. ``"boundary:publish"``)
#
# Unknown / future keys are silently mapped to ``"other"`` by the resolver;
# they never crash.

_PHASE_MAP: dict[str, str] = {
    # ── init ────────────────────────────────────────────────────────
    "initialize":          "init",
    "initialize_complete": "init",
    "boundary:clone_repo": "init",
    "boundary:setup_pr_branch": "init",
    "boundary:setup_branch": "init",
    "boundary:run_container_and_exec": "init",
    "tool:sandbox_attach":    "init",

    # ── explore ─────────────────────────────────────────────────────
    "tool:read_file_range":     "explore",
    "tool:list_files":          "explore",
    "tool:search_in_container": "explore",
    "boundary:issue_view":      "explore",

    # ── edit ────────────────────────────────────────────────────────
    "write_file":               "edit",
    "copy_project":             "edit",
    "copy_file":                "edit",
    "tool:write_file":          "edit",
    "tool:edit_file":           "edit",
    "tool:transform_file":      "edit",
    "tool:undo_file_edit":      "edit",

    # ── verify ──────────────────────────────────────────────────────
    "tool:verify_in_container":   "verify",
    "tool:lint_in_container":     "verify",
    "tool:type_check_in_container":"verify",
    "tool:diff_in_container":     "verify",

    # ── publish ─────────────────────────────────────────────────────
    "boundary:publish":           "publish",
    "boundary:pr_review_write":   "publish",
    "boundary:issue_write":       "publish",
    "boundary:checkpoint":        "publish",
    "boundary:checkpoint_restore":"publish",
    "tool:checkpoint_list":       "publish",
    "boundary:merge_base_fetch":  "publish",
    "boundary:merge_base":        "publish",
    "boundary:merge_complete":    "publish",
    "boundary:merge_abort":       "publish",
    "tool:secret_scan_override":  "publish",
    "boundary:secret_scan_override": "publish",

    # ── other (explicit — deliberately unclassified, not a fallback) ──
    # exec is never classified by command content (no cleverness);
    # run_python / sandbox_exec_check are the same free-form execution
    # surface, and stop is session teardown.
    "exec":                    "other",
    "stop":                    "other",
    "tool:run_python":         "other",
    "tool:sandbox_exec_check": "other",
}


def _effective_operation(entry: dict[str, Any]) -> str:
    """Return the *effective* operation key for *entry*.

    For ``tool_use`` entries the ``tool_name`` field is used;
    for ``boundary_crossing`` entries the ``sub_operation`` field is used.
    Every other entry uses ``operation`` as-is.
    """
    op = entry.get("operation", "")
    if op == "tool_use":
        return f"tool:{entry.get('tool_name', '')}"
    if op == "boundary_crossing":
        return f"boundary:{entry.get('sub_operation', '')}"
    return op


def phase_for_entry(entry: dict[str, Any]) -> str:
    """Return the phase (one of ``init/explore/edit/verify/publish/other``)
    for a single journal entry.  Unknown operations → ``"other"``."""
    return _PHASE_MAP.get(_effective_operation(entry), "other")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_iso(ts: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to a Unix timestamp (float seconds).
    Returns ``None`` on failure."""
    if not ts:
        return None
    try:
        from datetime import datetime, timezone
        # Handle both 'Z' and '+00:00' suffixes
        ts_clean = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _file_path_from_entry(entry: dict[str, Any]) -> str | None:
    """Extract a touched-file path from an edit-phase entry, or ``None``."""
    op = entry.get("operation", "")
    if op == "write_file":
        fn = entry.get("file_name", "")
        dd = entry.get("dest_dir", "")
        if fn and dd:
            return f"{dd.rstrip('/')}/{fn.lstrip('/')}"
        return fn or dd or None
    if op in ("copy_project", "copy_file"):
        dd = entry.get("dest_dir", "")
        return dd or None
    if op == "tool_use" and entry.get("tool_name") in (
        "write_file", "edit_file", "transform_file", "undo_file_edit",
    ):
        params = entry.get("params", {})
        if isinstance(params, dict):
            return params.get("file_path") or None
    return None


def _is_pytest_pass(entry: dict[str, Any]) -> bool | None:
    """Return ``True`` if *entry* is a pytest exec that passed,
    ``False`` if it failed, or ``None`` if it is not a pytest exec.

    An exec START entry (no ``exit_code`` -- Issue #789) is never a pytest
    outcome: its result is not known yet, so it returns ``None``.
    """
    if entry.get("operation") != "exec":
        return None
    cmds = entry.get("commands", [])
    if not cmds or not isinstance(cmds, list):
        return None
    first = cmds[0].strip() if cmds else ""
    # Heuristic: the first command starts with pytest
    if not (first.startswith("pytest") or first.startswith("python") and "pytest" in first):
        return None
    if "exit_code" not in entry:
        return None  # exec start entry: outcome not yet known
    return entry["exit_code"] == 0


def _is_outcome_entry(entry: dict[str, Any]) -> bool:
    """Return True for entries that report a *result* rather than a call.

    Two journaling patterns pair a call entry with a separate outcome
    entry:

    * ``verify_in_container`` (Issue #774): the call entry carries the
      request params; the outcome entry's params hold the ``result`` dict.
    * ``exec`` (Issue #789): the start entry is recorded before execution
      and has no ``exit_code``; the completion entry carries ``exit_code``
      and ``output_size``.

    An exec entry with ``exit_code == -1`` is the background-exec dispatch
    sentinel (Issue #359): ``-1`` means "launched, outcome not yet known",
    so it is *not* an outcome -- it keeps counting as a call (and as a
    failure via :func:`_entry_failed`), exactly as before the exec
    double-entry.

    Outcome entries are not calls -- they never increment ``op_calls`` --
    but they are the ones that carry pass/fail signals.
    """
    if entry.get("operation") == "exec":
        ec = entry.get("exit_code")
        return ec is not None and ec != -1
    if entry.get("operation") != "tool_use":
        return False
    params = entry.get("params")
    return isinstance(params, dict) and "result" in params


def _entry_failed(entry: dict[str, Any]) -> bool:
    """Return True when *entry* carries a failure signal.

    Failure is defined where the journal records an outcome:

    * ``exec`` completion -- ``exit_code`` is not 0 (``None`` counts as 0).
      An exec START entry (no ``exit_code`` -- Issue #789) has no outcome
      yet and is never failed.
    * ``tool_use`` outcome entry -- ``result.gate_passed`` is ``False``.

    Every other operation type has no failure signal and is never failed.
    """
    op = entry.get("operation", "")
    if op == "exec":
        if "exit_code" not in entry:
            return False  # exec start entry: outcome not yet known
        ec = entry.get("exit_code")
        return ec not in (None, 0)
    if op == "tool_use":
        params = entry.get("params")
        if isinstance(params, dict) and isinstance(params.get("result"), dict):
            return params["result"].get("gate_passed") is False
    return False


def _phase_order(phase: str) -> int:
    """Return a sort key so phases appear in workflow order."""
    _order = {"init": 0, "explore": 1, "edit": 2, "verify": 3, "publish": 4, "other": 5}
    return _order.get(phase, 99)


# ── State shapes ────────────────────────────────────────────────────────────


def _new_run_state(run_id: str) -> dict[str, Any]:
    """Return a fresh per-run aggregation state."""
    return {
        "run_id": run_id,
        "session_label": None,
        "image": "unknown",
        "repo": None,
        "start_ts": None,
        "last_ts": None,
        "phases": [],
        "touched_files": [],
        "verify_timeline": [],
        "edit_verify_roundtrips": 0,
        # Issue #777 (insights page): per-operation call/failure counters and
        # the distribution of the action that immediately follows a failure.
        # ``pending_failure_ops`` is the FIFO of failures that have not yet
        # seen a follow-up action; drained by :func:`_track_op_metrics`.
        "op_calls": {},
        "op_failures": {},
        "failure_recovery": {},
        "pending_failure_ops": [],
        # Issue #789 (dashboard): per-run summary counters mirroring
        # journal.get_runs, so the per-poll dashboard fragments render run
        # rows from the incremental aggregation state instead of
        # full-parsing the journal.  Monotonic and idempotent -- safe under
        # the incremental contract.
        "entry_count": 0,
        "boundary_crossings": 0,
        "vcs_operations": 0,
        # Issue #775 (health badges): monotonic markers for the rule-based
        # classifier in sunaba.health.
        "published": False,
        "stopped": False,
        "last_op": "",
        # Issue #789: ``host`` is True for host-scoped runs -- the
        # process-lifetime run_id for container-less operations, always
        # prefixed ``host-`` (journal.get_host_run_id, #778).  The prefix is
        # the detection source because it is the journal's own contract and
        # cannot collide with container run_ids (uuid4 hex, ``get_runs``
        # already keys host status off it).  ``exec_in_flight`` is True while
        # the run's most recent exec entry is a START (no ``exit_code``),
        # i.e. the exec is still running; the completion entry clears it.
        "host": run_id.startswith("host-"),
        "exec_in_flight": False,
    }


# ── Incremental aggregation ─────────────────────────────────────────────────


def aggregate_run_phases(
    state: dict[str, dict[str, Any]] | None,
    entries: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Aggregate journal entries into per-run phase summaries.

    **Contract:** ``(state, new_entries) → state``.

    ``state`` may be ``None`` for the initial call (equivalent to an empty
    state).  *entries* are the journal entries to incorporate; they are
    assumed to be sorted by ``ts`` ascending for correct timeline ordering.

    Returns a ``{run_id: run_state}`` mapping.  The returned dict is a
    **new** object; the caller's *state* dict is not mutated.

    This is the *only* public function in this module.  Every other symbol is
    an implementation detail.
    """
    # Shallow-copy the outer dict so the caller's reference is safe.
    if state is None:
        new_state: dict[str, dict[str, Any]] = {}
    else:
        new_state = dict(state)

    for entry in entries:
        run_id = entry.get("run_id", "")
        if not run_id:
            continue

        run = new_state.get(run_id)
        if run is None:
            run = _new_run_state(run_id)
            new_state[run_id] = run

        _incorporate(run, entry)

    return new_state


# ── Internal incorporation ──────────────────────────────────────────────────


def _incorporate(run: dict[str, Any], entry: dict[str, Any]) -> None:
    """Fold a single journal entry into the running state *run* (mutated)."""
    ts = entry.get("ts", "")
    phase = phase_for_entry(entry)
    op = entry.get("operation", "")
    eff = _effective_operation(entry)

    # ── metadata ──────────────────────────────────────────────
    if not run["start_ts"]:
        run["start_ts"] = ts
    if ts:
        run["last_ts"] = ts

    sl = entry.get("session_label")
    if sl:
        run["session_label"] = sl

    if op == "initialize":
        img = entry.get("image")
        if img:
            run["image"] = img

    if eff == "boundary:clone_repo" and not run["repo"]:
        details = entry.get("details", "")
        if isinstance(details, str):
            m = _REPO_RE.search(details)
            if m:
                run["repo"] = m.group(1)

    # \u2500\u2500 health markers (Issue #775) \u2500\u2500
    # Monotonic, idempotent flags consumed by ``sunaba.health``: ``published``
    # (saw a boundary:publish crossing), ``stopped`` (saw a stop entry) and
    # ``last_op`` (effective operation of the most recent entry, used by the
    # stalled rule's long-running-operation exemption).
    run["last_op"] = eff
    if eff == "boundary:publish":
        run["published"] = True
    if op == "stop":
        run["stopped"] = True
    if eff == "exec":
        # Issue #789: an exec START entry (no ``exit_code``) means the exec
        # is still running; the completion entry clears the flag.  The
        # health classifier exempts a run ending on an in-flight exec from
        # the stalled rule, bounded by the grace window.  Background-exec
        # dispatch sentinels carry ``exit_code=-1`` and count as
        # completions, so they never set the flag.
        run["exec_in_flight"] = "exit_code" not in entry

    # ── per-run summary counters (Issue #789, mirrors journal.get_runs) ──
    run["entry_count"] += 1
    if entry.get("boundary_crossing") or op == "boundary_crossing":
        run["boundary_crossings"] += 1
        if eff in ("boundary:issue_view", "boundary:publish"):
            run["vcs_operations"] += 1

    # ── touched files ─────────────────────────────────────────
    fp = _file_path_from_entry(entry)
    if fp and fp not in run["touched_files"]:
        run["touched_files"].append(fp)

    # ── verify timeline ───────────────────────────────────────
    if eff == "tool:verify_in_container":
        params = entry.get("params", {})
        result_info = params.get("result") if isinstance(params, dict) else None
        if result_info and isinstance(result_info, dict):
            # Outcome-bearing entry (Issue #774): record actual pass/fail.
            run["verify_timeline"].append({
                "ts": ts,
                "type": "verify_outcome",
                "passed": result_info.get("gate_passed", False),
                "passes": result_info.get("passes", 0),
                "fails": result_info.get("fails", 0),
                "collected": result_info.get("collected", 0),
                "status": result_info.get("status", "unknown"),
            })
        else:
            run["verify_timeline"].append({
                "ts": ts,
                "type": "verify_call",
                "passed": None,  # pending — outcome entry follows later
            })
    elif eff in ("tool:lint_in_container", "tool:type_check_in_container"):
        run["verify_timeline"].append({
            "ts": ts,
            "type": "gate_check",
            "tool": entry.get("tool_name", ""),
            "passed": None,
        })
    else:
        pytest_outcome = _is_pytest_pass(entry)
        if pytest_outcome is not None:
            run["verify_timeline"].append({
                "ts": ts,
                "type": "pytest_run",
                "passed": pytest_outcome,
            })

    # ── phases (merge consecutive same-phase segments) ───────
    phases: list[dict[str, Any]] = run["phases"]
    if phases and phases[-1]["phase"] == phase:
        seg = phases[-1]
    else:
        seg = {
            "phase": phase,
            "op_count": 0,
            "breakdown": {},
            "start_ts": ts,
            "end_ts": ts,
            "duration_s": None,
        }
        phases.append(seg)

    seg["op_count"] += 1
    seg["breakdown"][eff] = seg["breakdown"].get(eff, 0) + 1
    seg["end_ts"] = ts

    # Recompute phase durations (cheap — at most one segment per phase merge)
    _recompute_durations(run)

    # ── edit → verify-fail → edit roundtrips ──────────────────
    _update_roundtrips(run)

    # ── per-operation call / failure / recovery tracking (#777) ──
    _track_op_metrics(run, entry)


def _track_op_metrics(run: dict[str, Any], entry: dict[str, Any]) -> None:
    """Update per-operation call counts, failure counts, and failure-recovery
    distribution in *run* (mutated).

    Called from :func:`_incorporate` on every journal entry.  Outcome entries
    (``verify_in_container`` result records and ``exec`` completions --
    Issue #789) are never counted as calls, but they carry pass/fail signals
    used by :func:`_entry_failed`.

    Failure-recovery tracking keeps a FIFO (``pending_failure_ops``) of the
    operations that failed and have not yet seen a follow-up action.  The
    *immediately-following action* of a failure is the next non-outcome call
    -- which may itself fail (a second failed ``exec`` is still the first
    failure's next action).  When that call arrives, every pending failure
    records it as its recovery action and leaves the queue, so consecutive
    failures each get their own recovery entry instead of the last one
    overwriting the rest.
    """
    eff = _effective_operation(entry)
    is_outcome = _is_outcome_entry(entry)

    # ── call counting ──
    if not is_outcome:
        run["op_calls"][eff] = run["op_calls"].get(eff, 0) + 1

    # ── failure counting ──
    if _entry_failed(entry):
        run["op_failures"][eff] = run["op_failures"].get(eff, 0) + 1
        if not is_outcome:
            # A failing call is still an action: it is the immediately-
            # following action of any previously pending failures, so flush
            # the queue with it before enqueuing itself.
            _flush_recovery(run, eff)
        run["pending_failure_ops"].append(eff)
    elif not is_outcome and run["pending_failure_ops"]:
        # First non-outcome call after one or more failures → recovery action.
        _flush_recovery(run, eff)


def _flush_recovery(run: dict[str, Any], action: str) -> None:
    """Record *action* as the recovery action of every pending failure and
    clear the pending queue (mutates *run*)."""
    rec = run["failure_recovery"]
    for prev in run["pending_failure_ops"]:
        if prev not in rec:
            rec[prev] = {}
        rec[prev][action] = rec[prev].get(action, 0) + 1
    run["pending_failure_ops"] = []


def _recompute_durations(run: dict[str, Any]) -> None:
    """Update ``duration_s`` on every phase segment in *run*."""
    for seg in run["phases"]:
        s = _parse_iso(seg["start_ts"])
        e = _parse_iso(seg["end_ts"])
        seg["duration_s"] = round(e - s, 1) if (s is not None and e is not None and e >= s) else None


def _update_roundtrips(run: dict[str, Any]) -> None:
    """Recompute the edit → verify-fail → edit roundtrip count idempotently.

    Called on every ``_incorporate``; because it scans the *full* semantic
    phase list and sets ``edit_verify_roundtrips`` to the computed total
    (rather than incrementing), it is naturally idempotent — the same
    journal entries always produce the same count regardless of
    chunking or call frequency.

    A roundtrip is counted for each consecutive triple of semantic
    segments (edit, verify, edit) where the time window spanning the
    verify segment contains **no** passing ``pytest_run`` or
    ``verify_outcome`` entry.
    """
    semantic = _build_semantic(run["phases"])
    timeline = run["verify_timeline"]

    if len(semantic) < 3:
        run["edit_verify_roundtrips"] = 0
        return

    count = 0
    for i in range(len(semantic) - 2):
        a, b, c = semantic[i], semantic[i + 1], semantic[i + 2]
        if a["phase"] != "edit" or b["phase"] != "verify" or c["phase"] != "edit":
            continue

        # Time window: from verify segment start to second edit segment start.
        window_start = b.get("start_ts", "")
        window_end = c.get("start_ts", "")

        # A passing outcome anywhere in this window defeats the roundtrip.
        passed = False
        for vt in timeline:
            vts = vt.get("ts", "")
            if not (window_start <= vts <= window_end):
                continue
            if vt["type"] == "pytest_run" and vt.get("passed") is True:
                passed = True
                break
            if vt["type"] == "verify_outcome" and vt.get("passed") is True:
                passed = True
                break

        if not passed:
            count += 1

    run["edit_verify_roundtrips"] = count


def _build_semantic(phases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop ``"other"`` segments and merge consecutive same-phase segments.

    Returns a new list; does not mutate *phases*.
    """
    semantic: list[dict[str, Any]] = []
    for seg in phases:
        p = seg["phase"]
        if p == "other":
            continue
        if semantic and semantic[-1]["phase"] == p:
            prev = semantic[-1]
            prev["end_ts"] = seg.get("end_ts", prev.get("end_ts", ""))
            prev["op_count"] += seg.get("op_count", 0)
            for k, v in seg.get("breakdown", {}).items():
                prev["breakdown"][k] = prev["breakdown"].get(k, 0) + v
        else:
            semantic.append(dict(seg))
    return semantic


# ── Public helpers for rendering ────────────────────────────────────────────


def phase_state_for_run(
    state: dict[str, dict[str, Any]],
    run_id: str,
) -> dict[str, Any] | None:
    """Return the aggregated state for a single *run_id*, or ``None``."""
    return state.get(run_id)


def sorted_phase_names(phases: list[dict[str, Any]]) -> list[str]:
    """Return the distinct phase names in workflow order."""
    seen: set[str] = set()
    result: list[str] = []
    for seg in phases:
        p = seg["phase"]
        if p not in seen:
            seen.add(p)
            result.append(p)
    # Sort by workflow order to handle interleaved phases
    result.sort(key=_phase_order)
    return result
