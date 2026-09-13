"""Per-container verify-success tracking for state-conditioned nudges (Issue #550).

The server knows runtime state the agent cannot see -- e.g. whether
``verify_in_container`` ever completed with the full gate passing for a
container in this server session.  This module keeps that state in a
module-level in-memory map (same pattern as ``journal._run_map``) so tools
can attach *advisory* nudge fields to their results when an action
contradicts the recorded state.  Journal analysis behind Issue #550 showed
unconditional nudges are mostly noise, so nudges fire only on
contradiction -- and they never block.

The map is intentionally process-local and lost on a server restart:
every consumer is advisory (a missing record produces a warning, never a
block).  A record means "the full verify gate passed for this container
at least once in this server session"; it is not invalidated by later
edits (kept deliberately simple).
"""

from __future__ import annotations

import threading

#: Maps container ID prefixes -> True once ``verify_in_container`` has
#: completed with ``gate_passed=True`` (same keying as ``journal._run_map``).
_verify_map: dict[str, bool] = {}
_verify_map_lock: threading.Lock = threading.Lock()


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
