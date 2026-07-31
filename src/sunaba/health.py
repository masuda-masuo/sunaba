"""Rule-based run-health classification (Issue #775).

Derives a per-run ``health`` value from the #774 aggregation state built by
:func:`sunaba.phase.aggregate_run_phases` (the run being classified plus, for
the ``regression`` rule, the other runs in the same state).  Purely
rule-based -- no LLM calls, no journal/filesystem reads, no clock reads:
callers pass ``now`` explicitly.

Health states, in precedence order (first match wins; ``progressing`` is
the fallback):

===============  ============================================================
health           rule
===============  ============================================================
done             the run has published (a ``boundary:publish`` journal
                 entry) or its container/session was stopped (a ``stop``
                 entry)
looping          >= N consecutive edit -> verify-failure roundtrips
                 (``edit_verify_roundtrips`` from the phase aggregation)
stalled          >= M minutes with no journal activity since ``last_ts``;
                 a run whose most recent operation is a known long-running
                 one still in flight (e.g. a pending ``verify_in_container``
                 call, or an exec START -- Issue #789) is NOT flagged -- but
                 that exemption lapses after a grace window (see below): no
                 tool legitimately runs silent forever, and a session that
                 dies right after such a call must still surface as stalled
                 eventually.  Host-scoped runs (``host-`` run_ids, #778)
                 are never ``stalled``: they have no session-end lifecycle,
                 so idle time is expected for them
regression       the run's verify pass count decreased vs the most recent
                 prior run for the same ``repo`` (skipped when ``repo`` is
                 None or there is no prior run)
progressing      none of the above
===============  ============================================================

Thresholds N and M are configurable per call via ``loop_threshold`` and
``stall_minutes``, and via environment variables when those arguments are
left at ``None``:

* ``SUNABA_HEALTH_LOOP_THRESHOLD`` -- minimum roundtrips to flag
  ``looping`` (default ``3``)
* ``SUNABA_HEALTH_STALL_MINUTES``  -- idle minutes to flag ``stalled``
  (default ``10``)
* ``SUNABA_HEALTH_INFLIGHT_GRACE_MINUTES`` -- how long the in-flight
  exemption holds after ``last_ts`` (default ``30``; ``0`` disables the
  exemption entirely)

``loop_threshold`` is clamped to >= 1 and ``stall_minutes`` to >= 1 --
a zero or negative threshold would flag every run.

The module is pure: given the same state, ``now`` and thresholds it always
returns the same classification.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

#: Health states in precedence order.
HEALTH_ORDER: tuple[str, ...] = (
    "done",
    "looping",
    "stalled",
    "regression",
    "progressing",
)

#: Journal operations that can legitimately take minutes while the journal
#: shows no new activity: the tools record their ``tool_use`` entry *before*
#: executing.  A run whose most recent operation is one of these is treated
#: as busy, not stalled (Issue #775).
#:
#: ``exec`` (Issue #789) journals twice per foreground call -- a START entry
#: before running and the completion after -- so a run ending on an exec
#: START may still be busy (the classifier checks the ``exec_in_flight``
#: marker from the phase aggregation for that); a run ending on an exec
#: completion stalls normally.  Background-exec dispatch sentinels carry
#: ``exit_code=-1`` and are completion-shaped, so they get no exemption.
_LONG_RUNNING_OPS: frozenset[str] = frozenset({
    "exec",
    "tool:lint_in_container",
    "tool:type_check_in_container",
    "tool:verify_in_container",
})

_LOOP_THRESHOLD_ENV = "SUNABA_HEALTH_LOOP_THRESHOLD"
_STALL_MINUTES_ENV = "SUNABA_HEALTH_STALL_MINUTES"
_INFLIGHT_GRACE_ENV = "SUNABA_HEALTH_INFLIGHT_GRACE_MINUTES"

_DEFAULT_LOOP_THRESHOLD = 3
_DEFAULT_STALL_MINUTES = 10.0
_DEFAULT_INFLIGHT_GRACE_MINUTES = 30.0


def _parse_iso(ts: str | None) -> float | None:
    """Parse an ISO-8601 timestamp to Unix seconds, or ``None`` on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _env_threshold(name: str, default: float) -> float:
    """Read a numeric threshold from the environment, falling back to
    *default* when unset or unparseable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolved_thresholds(
    loop_threshold: int | None,
    stall_minutes: float | None,
    inflight_grace_minutes: float | None,
) -> tuple[int, float, float]:
    """Resolve thresholds: explicit arguments win, environment next,
    module defaults last.

    ``loop_threshold`` and ``stall_minutes`` are clamped to >= 1 (zero or
    negative values would flag every run).  ``inflight_grace_minutes`` is
    clamped to >= 0; exactly 0 disables the in-flight exemption.
    """
    if loop_threshold is None:
        loop_threshold = int(
            _env_threshold(_LOOP_THRESHOLD_ENV, _DEFAULT_LOOP_THRESHOLD)
        )
    if stall_minutes is None:
        stall_minutes = _env_threshold(_STALL_MINUTES_ENV, _DEFAULT_STALL_MINUTES)
    if inflight_grace_minutes is None:
        inflight_grace_minutes = _env_threshold(
            _INFLIGHT_GRACE_ENV, _DEFAULT_INFLIGHT_GRACE_MINUTES
        )
    return (
        max(1, loop_threshold),
        max(1.0, stall_minutes),
        max(0.0, inflight_grace_minutes),
    )


def verify_pass_count(run: dict[str, Any]) -> int:
    """Return the number of passing verify outcomes for *run*.

    Counts ``pytest_run`` and ``verify_outcome`` timeline entries whose
    ``passed`` is True; ``gate_check`` entries carry ``passed=None`` and
    never count.
    """
    return sum(1 for v in run.get("verify_timeline", []) if v.get("passed") is True)


def _last_op_in_flight(
    run: dict[str, Any],
    now_epoch: float,
    grace_minutes: float,
) -> bool:
    """Whether the run's most recent journal entry is a long-running
    operation that may still be executing.

    ``lint``/``type_check`` entries are written before the tool runs, so a
    run ending on one is busy.  ``verify_in_container`` is written twice:
    a pending call before the run and an outcome-bearing entry after it --
    a run ending on a pending call is still verifying, while one ending on
    the outcome has finished.  ``exec`` (Issue #789) also journals twice: a
    START entry before running and the completion after -- a run ending on
    a START (``exec_in_flight``) is still executing, while one ending on a
    completion has finished.

    The exemption is bounded: it holds only while ``now`` is within
    *grace_minutes* of ``last_ts``.  Beyond that, silence means the tool
    finished without journaling an outcome (error path) or the session
    died mid-call -- both are stall-worthy.  ``grace_minutes == 0``
    disables the exemption entirely.
    """
    last_op = run.get("last_op", "")
    if last_op not in _LONG_RUNNING_OPS:
        return False
    last_epoch = _parse_iso(run.get("last_ts"))
    if last_epoch is None or now_epoch - last_epoch >= grace_minutes * 60:
        return False
    if last_op == "exec":
        # exec journals a START entry before running and the completion
        # after (Issue #789): only a run whose last entry is a START is
        # still busy.  The ``exec_in_flight`` marker comes from the phase
        # aggregation; background-exec dispatch sentinels (exit_code=-1)
        # are completion-shaped and never set it.
        return bool(run.get("exec_in_flight"))
    if last_op != "tool:verify_in_container":
        return True
    timeline = run.get("verify_timeline", [])
    return not timeline or timeline[-1].get("type") != "verify_outcome"


def _is_stalled(run: dict[str, Any], now_epoch: float, stall_minutes: float) -> bool:
    """Whether *run* has had no journal activity for >= *stall_minutes*."""
    last = run.get("last_ts")
    if not last:
        return False
    last_epoch = _parse_iso(last)
    if last_epoch is None:
        return False
    return now_epoch - last_epoch >= stall_minutes * 60


def _prior_run_for_repo(
    state: dict[str, dict[str, Any]],
    run: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the most recent prior run for *run*'s repo, or ``None``.

    Prior means a different run sharing ``repo`` whose ``start_ts`` is
    strictly before *run*'s; among those, the latest start wins.  Skipped
    when ``repo`` is None or either start timestamp is missing/unparseable.
    """
    repo = run.get("repo")
    own_start = run.get("start_ts")
    if not repo or not own_start:
        return None
    own_epoch = _parse_iso(own_start)
    if own_epoch is None:
        return None
    best: dict[str, Any] | None = None
    best_epoch: float | None = None
    for other in state.values():
        if other.get("repo") != repo:
            continue
        ost = other.get("start_ts")
        if not ost:
            continue
        o_epoch = _parse_iso(ost)
        if o_epoch is None or o_epoch >= own_epoch:
            continue
        if best_epoch is None or o_epoch > best_epoch:
            best = other
            best_epoch = o_epoch
    return best


def classify_run_health(
    state: dict[str, dict[str, Any]],
    run_id: str,
    now: str,
    loop_threshold: int | None = None,
    stall_minutes: float | None = None,
    inflight_grace_minutes: float | None = None,
) -> str:
    """Classify a single run from the #774 aggregation *state*.

    Args:
        state: The ``{run_id: run_state}`` mapping from
            :func:`sunaba.phase.aggregate_run_phases`.
        run_id: The run to classify.
        now: Explicit ISO-8601 timestamp supplied by the caller -- this
            function never reads the clock itself.
        loop_threshold: N for the ``looping`` rule; ``None`` falls back to
            the ``SUNABA_HEALTH_LOOP_THRESHOLD`` environment variable, then
            to 3.
        stall_minutes: M for the ``stalled`` rule; ``None`` falls back to
            the ``SUNABA_HEALTH_STALL_MINUTES`` environment variable, then
            to 10.
        inflight_grace_minutes: how long the long-running-op exemption
            holds after ``last_ts``; ``None`` falls back to the
            ``SUNABA_HEALTH_INFLIGHT_GRACE_MINUTES`` environment variable,
            then to 30.  ``0`` disables the exemption.

    Returns one of :data:`HEALTH_ORDER`.  An unknown ``run_id`` classifies
    as ``progressing``.
    """
    run = state.get(run_id)
    if run is None:
        return "progressing"

    loop_threshold, stall_minutes, inflight_grace_minutes = _resolved_thresholds(
        loop_threshold, stall_minutes, inflight_grace_minutes
    )

    # done -- the run has published, or its container/session was stopped.
    if run.get("published") or run.get("stopped"):
        return "done"

    # looping -- >= N consecutive edit -> verify-failure roundtrips.
    if run.get("edit_verify_roundtrips", 0) >= loop_threshold:
        return "looping"

    # stalled -- >= M minutes idle, unless the most recent operation is a
    # long-running one still in flight.  Host-scoped runs (run_id prefixed
    # ``host-``, #778) have no session-end lifecycle -- they never receive a
    # stop entry -- so idle time is expected for them and never reads as a
    # dead session (Issue #789).
    now_epoch = _parse_iso(now)
    if now_epoch is None:
        raise ValueError(f"now must be an ISO-8601 timestamp, got {now!r}")
    if (
        not run.get("host")
        and _is_stalled(run, now_epoch, stall_minutes)
        and not _last_op_in_flight(run, now_epoch, inflight_grace_minutes)
    ):
        return "stalled"

    # regression -- verify pass count decreased vs the most recent prior
    # run on the same repo.
    prior = _prior_run_for_repo(state, run)
    if prior is not None and verify_pass_count(run) < verify_pass_count(prior):
        return "regression"

    return "progressing"


def classify_all_runs(
    state: dict[str, dict[str, Any]],
    now: str,
    loop_threshold: int | None = None,
    stall_minutes: float | None = None,
    inflight_grace_minutes: float | None = None,
) -> dict[str, str]:
    """Classify every run in *state*.

    Returns a ``{run_id: health}`` mapping.
    """
    return {
        rid: classify_run_health(
            state, rid, now, loop_threshold, stall_minutes, inflight_grace_minutes
        )
        for rid in state
    }
