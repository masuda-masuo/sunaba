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
