"""Per-container verify-success tracking for state-conditioned nudges (Issue #550).

The server knows runtime state the agent cannot see -- e.g. whether
``verify_in_container`` ever completed with the full gate passing for a
container in this server session.  This module keeps that state in a
module-level in-memory map (same pattern as ``journal._run_map``) so tools
can attach *advisory* nudge fields to their results when an action
contradicts the recorded state.  Journal analysis behind Issue #550 showed
unconditional nudges are mostly noise, so nudges fire only on
contradiction -- and they never block.

The maps are intentionally process-local and lost on a server restart.
Verification success remains process-local; uv preparation state is a
separate hard prerequisite for verify and publish.
"""

from __future__ import annotations

import threading
from typing import Any

#: Maps container ID prefixes -> True once ``verify_in_container`` has
#: completed with ``gate_passed=True`` (same keying as ``journal._run_map``).
_verify_map: dict[str, bool] = {}
_verify_map_lock: threading.Lock = threading.Lock()

#: Per-container Python environment preparation for locked uv projects.
_environment_state: dict[str, dict[str, Any]] = {}
_environment_state_lock: threading.Lock = threading.Lock()


def record_environment_state(
    container_id: str,
    status: str,
    *,
    reason: str = "",
    command: str = "",
    exit_code: int | None = None,
    fingerprint: dict[str, Any] | None = None,
    project_root: str | None = None,
) -> None:
    """Record uv preparation state and invalidate verify on failure."""
    if status not in {"not_prepared", "ready", "failed"}:
        raise ValueError(f"invalid environment state: {status}")
    try:
        from sunaba.tools.common import _docker

        client = _docker()
        container = client.containers.get(container_id)
        container_id = str(container.id)
    except Exception:
        pass
    key = container_id
    with _environment_state_lock:
        record: dict[str, Any] = {
            "status": status,
            "reason": reason[:240],
            "command": command[:240],
            "exit_code": exit_code,
        }
        if fingerprint is not None:
            record["fingerprint"] = fingerprint
        if project_root is not None:
            record["project_root"] = project_root
        _environment_state[key] = record

    try:
        from sunaba.environment_state_store import save_environment_state

        save_environment_state(container_id, record)
    except Exception:
        pass

    if status != "ready":
        invalidate_verify_success(container_id)


def clear_environment_state(container_id: str, *, invalidate_verify: bool = False) -> None:
    """Forget stale uv preparation after a confirmed transition to a non-uv root."""
    try:
        from sunaba.tools.common import _docker

        client = _docker()
        container = client.containers.get(container_id)
        container_id = str(container.id)
    except Exception:
        pass
    with _environment_state_lock:
        _environment_state.pop(container_id, None)
    try:
        from sunaba.environment_state_store import remove_environment_state

        remove_environment_state(container_id)
    except Exception:
        pass
    if invalidate_verify:
        invalidate_verify_success(container_id)


def get_environment_state(container_id: str) -> dict[str, Any] | None:
    """Return a copy of preparation state, or None when never recorded."""
    with _environment_state_lock:
        value = _environment_state.get(container_id)
        return dict(value) if value is not None else None

def invalidate_verify_success(container_id: str) -> None:
    """Forget a previous verify success after environment preparation fails."""
    with _verify_map_lock:
        _verify_map.pop(container_id[:12], None)



def record_verify_success(container_id: str) -> None:
    """Record that the full verify gate passed for *container_id*."""
    with _verify_map_lock:
        _verify_map[container_id[:12]] = True


def has_verify_success(container_id: str) -> bool:
    """Return whether a full-gate verify success is recorded for *container_id*."""
    with _verify_map_lock:
        return _verify_map.get(container_id[:12], False)


# ---------------------------------------------------------------------------
# Per-container in-flight guard (issue #910)
# ---------------------------------------------------------------------------
#
# At most one verify may run per container at a time.  A concurrent
# identical call observes ``status: "in_progress"`` without starting a
# second command -- duplicating a full gate is exactly how #910's retained
# test trees exhausted the container's PID capacity.  The bookkeeping is a
# plain process-local map guarded by a lock: the server is single-process,
# and the guard must be released on every terminal outcome (success, test
# failure, timeout, raised exception), which the verify call does in a
# ``finally``.

#: Container ID prefixes currently running a verify.
_inflight: dict[str, bool] = {}
_inflight_lock: threading.Lock = threading.Lock()


def verify_guard_acquire(container_id: str) -> bool:
    """Claim the verify slot for *container_id*.

    Returns ``False`` when another verify is already running for the same
    container (the caller must return ``status: "in_progress"`` without
    starting any command), ``True`` when the slot is now held.
    """
    key = container_id[:12]
    with _inflight_lock:
        if _inflight.get(key, False):
            return False
        _inflight[key] = True
        return True


def verify_guard_release(container_id: str) -> None:
    """Release the verify slot for *container_id* (idempotent)."""
    key = container_id[:12]
    with _inflight_lock:
        _inflight.pop(key, None)


def verify_in_flight(container_id: str) -> bool:
    """Return whether a verify is currently running for *container_id*."""
    key = container_id[:12]
    with _inflight_lock:
        return _inflight.get(key, False)
