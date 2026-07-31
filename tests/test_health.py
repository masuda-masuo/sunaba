"""Tests for rule-based run-health classification (Issue #775).

Every health state is reproduced from journal-entry fixtures fed through
``aggregate_run_phases`` -- the fixtures are journal entries, never
hand-built aggregation states.  All tests are deterministic: ``now`` is
passed explicitly and no wall clock or sleeping is involved.
"""

from __future__ import annotations

import pytest

from sunaba.health import (
    HEALTH_ORDER,
    classify_all_runs,
    classify_run_health,
    verify_pass_count,
)
from sunaba.phase import aggregate_run_phases

_LOOP_ENV = "SUNABA_HEALTH_LOOP_THRESHOLD"
_STALL_ENV = "SUNABA_HEALTH_STALL_MINUTES"
_GRACE_ENV = "SUNABA_HEALTH_INFLIGHT_GRACE_MINUTES"


def _seq(n: int) -> str:
    """Deterministic ascending ISO timestamp at second *n* of the day."""
    return f"2026-01-01T00:00:{n:02d}Z"


def _entry(op: str, ts: str, **kwargs) -> dict:
    """Build a minimal journal entry dict."""
    e: dict = {
        "ts": ts,
        "run_id": kwargs.pop("run_id", "test-run"),
        "container_id": kwargs.pop("container_id", "abc123"),
        "operation": op,
    }
    e.update(kwargs)
    return e


def _tool(name: str, ts: str, **kwargs) -> dict:
    return _entry("tool_use", ts, tool_name=name, **kwargs)


def _boundary(sub: str, ts: str, **kwargs) -> dict:
    return _entry("boundary_crossing", ts, sub_operation=sub, **kwargs)


def _pytest(ts: str, exit_code: int = 1, **kwargs) -> dict:
    return _entry("exec", ts, commands=["pytest tests/"], exit_code=exit_code, **kwargs)


def _verify_pending(ts: str, **kwargs) -> dict:
    """A verify_in_container call recorded before execution (no result)."""
    return _tool("verify_in_container", ts, **kwargs)


def _verify_outcome(ts: str, gate_passed: bool = True, passes: int = 1, **kwargs) -> dict:
    """The outcome-bearing verify entry written after the run completes."""
    return _tool(
        "verify_in_container",
        ts,
        params={"result": {
            "gate_passed": gate_passed,
            "passes": passes,
            "fails": 0,
            "collected": passes,
            "status": "ok" if gate_passed else "failed",
        }},
        **kwargs,
    )


def _looping_journal(n: int, run_id: str = "test-run") -> list[dict]:
    """``n`` consecutive edit -> verify-fail -> edit roundtrips as journal entries."""
    entries = [_entry("initialize", _seq(0), run_id=run_id)]
    t = 1
    for _ in range(n):
        entries.append(_tool("edit_file", _seq(t), run_id=run_id, params={"file_path": "a.py"}))
        t += 1
        entries.append(_verify_pending(_seq(t), run_id=run_id))
        t += 1
        entries.append(_pytest(_seq(t), exit_code=1, run_id=run_id))
        t += 1
    entries.append(_tool("edit_file", _seq(t), run_id=run_id, params={"file_path": "a.py"}))
    return entries


def _clone_journal(run_id: str, ts: str) -> dict:
    return _boundary(
        "clone_repo", ts, run_id=run_id,
        details="repo=owner/app dest=/workspace",
    )


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


class TestDone:
    """done: the run has published, or its container/session was stopped."""

    def test_published_run_is_done(self):
        entries = [
            _entry("initialize", _seq(0)),
            _boundary("publish", _seq(1), details="https://github.com/o/r/pull/1"),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", _seq(2)) == "done"

    def test_stopped_run_is_done(self):
        entries = [
            _entry("initialize", _seq(0)),
            _entry("stop", _seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", _seq(2)) == "done"


# ---------------------------------------------------------------------------
# looping
# ---------------------------------------------------------------------------


class TestLooping:
    """looping: >= N consecutive edit -> verify-failure roundtrips (N=3)."""

    def test_three_roundtrips_is_looping(self):
        entries = _looping_journal(3)
        state = aggregate_run_phases(None, entries)
        assert state["test-run"]["edit_verify_roundtrips"] == 3
        assert classify_run_health(state, "test-run", _seq(20)) == "looping"

    def test_two_roundtrips_is_not_looping(self):
        entries = _looping_journal(2)
        state = aggregate_run_phases(None, entries)
        assert state["test-run"]["edit_verify_roundtrips"] == 2
        assert classify_run_health(state, "test-run", _seq(20)) == "progressing"

    def test_loop_threshold_argument_override(self):
        entries = _looping_journal(3)
        state = aggregate_run_phases(None, entries)
        assert (
            classify_run_health(state, "test-run", _seq(20), loop_threshold=4)
            == "progressing"
        )
        assert (
            classify_run_health(state, "test-run", _seq(20), loop_threshold=2)
            == "looping"
        )

    def test_loop_threshold_env_override(self, monkeypatch):
        entries = _looping_journal(2)
        state = aggregate_run_phases(None, entries)
        monkeypatch.setenv(_LOOP_ENV, "2")
        assert classify_run_health(state, "test-run", _seq(20)) == "looping"
        monkeypatch.delenv(_LOOP_ENV)
        assert classify_run_health(state, "test-run", _seq(20)) == "progressing"

    def test_invalid_loop_env_falls_back_to_default(self, monkeypatch):
        entries = _looping_journal(2)
        state = aggregate_run_phases(None, entries)
        monkeypatch.setenv(_LOOP_ENV, "not-a-number")
        assert classify_run_health(state, "test-run", _seq(20)) == "progressing"


# ---------------------------------------------------------------------------
# stalled
# ---------------------------------------------------------------------------


class TestStalled:
    """stalled: >= M minutes idle since last_ts (M=10), unless the most
    recent operation is a long-running one still in flight."""

    def _journal(self) -> list[dict]:
        return [
            _entry("initialize", _seq(0)),
            _tool("edit_file", _seq(1), params={"file_path": "a.py"}),
        ]

    def test_stalled_past_idle_threshold(self):
        state = aggregate_run_phases(None, self._journal())
        # 10m59s of silence >= M=10m
        assert classify_run_health(state, "test-run", "2026-01-01T00:11:00Z") == "stalled"

    def test_not_stalled_below_idle_threshold(self):
        state = aggregate_run_phases(None, self._journal())
        # 9m58s of silence < M=10m
        assert classify_run_health(state, "test-run", "2026-01-01T00:09:59Z") == "progressing"

    def test_stall_minutes_argument_override(self):
        state = aggregate_run_phases(None, self._journal())
        now = "2026-01-01T00:02:00Z"
        assert classify_run_health(state, "test-run", now) == "progressing"
        assert classify_run_health(state, "test-run", now, stall_minutes=1) == "stalled"

    def test_stall_minutes_env_override(self, monkeypatch):
        state = aggregate_run_phases(None, self._journal())
        now = "2026-01-01T00:02:00Z"
        monkeypatch.setenv(_STALL_ENV, "1")
        assert classify_run_health(state, "test-run", now) == "stalled"
        monkeypatch.delenv(_STALL_ENV)
        assert classify_run_health(state, "test-run", now) == "progressing"

    def test_invalid_stall_env_falls_back_to_default(self, monkeypatch):
        state = aggregate_run_phases(None, self._journal())
        monkeypatch.setenv(_STALL_ENV, "later")
        assert classify_run_health(state, "test-run", "2026-01-01T00:02:00Z") == "progressing"

    def test_verify_in_flight_is_not_stalled(self):
        """A pending verify call (no outcome entry yet) may still be running.

        15 minutes of silence is past the 10-minute stall threshold but
        within the 30-minute in-flight grace window.
        """
        entries = [
            _entry("initialize", _seq(0)),
            _verify_pending(_seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T00:15:00Z") == "progressing"

    def test_verify_in_flight_exemption_lapses_after_grace(self):
        """A pending verify with no outcome for hours means the session died
        or the tool errored without journaling -- stalled, not busy."""
        entries = [
            _entry("initialize", _seq(0)),
            _verify_pending(_seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T03:00:00Z") == "stalled"

    def test_inflight_grace_argument_override(self):
        entries = [
            _entry("initialize", _seq(0)),
            _verify_pending(_seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        now = "2026-01-01T00:15:00Z"
        assert classify_run_health(state, "test-run", now) == "progressing"
        assert (
            classify_run_health(state, "test-run", now, inflight_grace_minutes=12)
            == "stalled"
        )

    def test_inflight_grace_env_override(self, monkeypatch):
        entries = [
            _entry("initialize", _seq(0)),
            _verify_pending(_seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        now = "2026-01-01T00:15:00Z"
        monkeypatch.setenv(_GRACE_ENV, "12")
        assert classify_run_health(state, "test-run", now) == "stalled"
        monkeypatch.delenv(_GRACE_ENV)
        assert classify_run_health(state, "test-run", now) == "progressing"

    def test_inflight_grace_zero_disables_exemption(self):
        entries = [
            _entry("initialize", _seq(0)),
            _verify_pending(_seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert (
            classify_run_health(
                state, "test-run", "2026-01-01T00:11:00Z", inflight_grace_minutes=0
            )
            == "stalled"
        )

    def test_stalled_applies_after_verify_outcome(self):
        """Once the outcome entry is written the verify finished; silence
        since then is real idle time."""
        entries = [
            _entry("initialize", _seq(0)),
            _verify_outcome(_seq(1), passes=4),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T03:00:00Z") == "stalled"

    def test_lint_last_op_is_not_stalled_within_grace(self):
        entries = [
            _entry("initialize", _seq(0)),
            _tool("lint_in_container", _seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T00:15:00Z") == "progressing"

    def test_lint_last_op_is_stalled_beyond_grace(self):
        """lint/type_check journal only a pre-execution entry; hours of
        silence after one means the session died -- it must surface."""
        entries = [
            _entry("initialize", _seq(0)),
            _tool("lint_in_container", _seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T03:00:00Z") == "stalled"

    def test_no_timestamp_run_is_not_stalled(self):
        state = aggregate_run_phases(None, [_entry("initialize", "")])
        assert classify_run_health(state, "test-run", "2026-01-01T03:00:00Z") == "progressing"


class TestThresholdClamping:
    """Zero/negative thresholds are clamped instead of flagging every run."""

    def test_zero_loop_threshold_clamps_to_one(self):
        # No roundtrips at all: even a clamped threshold of 1 must not fire.
        entries = [_entry("initialize", _seq(0))]
        state = aggregate_run_phases(None, entries)
        assert (
            classify_run_health(state, "test-run", _seq(1), loop_threshold=0)
            == "progressing"
        )
        # One roundtrip: fires under the clamped threshold of 1.
        state = aggregate_run_phases(None, _looping_journal(1))
        assert (
            classify_run_health(state, "test-run", _seq(20), loop_threshold=0)
            == "looping"
        )

    def test_zero_stall_minutes_clamps_to_one(self):
        entries = [
            _entry("initialize", _seq(0)),
            _tool("edit_file", _seq(1), params={"file_path": "a.py"}),
        ]
        state = aggregate_run_phases(None, entries)
        # 30 seconds of silence: clamped threshold of 1 minute must not fire.
        assert (
            classify_run_health(
                state, "test-run", "2026-01-01T00:00:31Z", stall_minutes=0
            )
            == "progressing"
        )
        # 2 minutes of silence: fires under the clamped threshold.
        assert (
            classify_run_health(
                state, "test-run", "2026-01-01T00:02:01Z", stall_minutes=0
            )
            == "stalled"
        )


# ---------------------------------------------------------------------------
# regression
# ---------------------------------------------------------------------------


class TestRegression:
    """regression: verify pass count decreased vs the most recent prior run
    on the same repo (skipped when repo is None or there is no prior run)."""

    def _journal(self) -> list[dict]:
        """run-a (earlier): 2 passes; run-b (later): 1 pass; same repo."""
        return [
            _entry("initialize", _seq(0), run_id="run-a"),
            _clone_journal("run-a", _seq(1)),
            _pytest(_seq(2), exit_code=0, run_id="run-a"),
            _pytest(_seq(3), exit_code=0, run_id="run-a"),
            _entry("initialize", _seq(4), run_id="run-b"),
            _clone_journal("run-b", _seq(5)),
            _pytest(_seq(6), exit_code=0, run_id="run-b"),
        ]

    def test_pass_count_decrease_is_regression(self):
        state = aggregate_run_phases(None, self._journal())
        assert classify_run_health(state, "run-b", _seq(7)) == "regression"

    def test_prior_run_without_decrease_is_not_regression(self):
        state = aggregate_run_phases(None, self._journal())
        assert classify_run_health(state, "run-a", _seq(7)) == "progressing"

    def test_uses_most_recent_prior_run(self):
        """run-c's most recent prior is run-b (0 passes), not run-a (2)."""
        entries = [
            _entry("initialize", _seq(0), run_id="run-a"),
            _clone_journal("run-a", _seq(1)),
            _pytest(_seq(2), exit_code=0, run_id="run-a"),
            _pytest(_seq(3), exit_code=0, run_id="run-a"),
            _entry("initialize", _seq(4), run_id="run-b"),
            _clone_journal("run-b", _seq(5)),
            _entry("initialize", _seq(6), run_id="run-c"),
            _clone_journal("run-c", _seq(7)),
            _pytest(_seq(8), exit_code=0, run_id="run-c"),
        ]
        state = aggregate_run_phases(None, entries)
        # run-c: 1 pass vs prior run-b's 0 -> not decreased -> progressing
        assert classify_run_health(state, "run-c", _seq(9)) == "progressing"
        # run-b: 0 passes vs prior run-a's 2 -> decreased -> regression
        assert classify_run_health(state, "run-b", _seq(9)) == "regression"

    def test_skipped_without_repo(self):
        entries = [
            _entry("initialize", _seq(0), run_id="run-a"),
            _pytest(_seq(1), exit_code=0, run_id="run-a"),
            _entry("initialize", _seq(2), run_id="run-b"),
            _pytest(_seq(3), exit_code=0, run_id="run-b"),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "run-b", _seq(4)) == "progressing"

    def test_skipped_without_prior_run(self):
        entries = [
            _entry("initialize", _seq(0)),
            _clone_journal("test-run", _seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", _seq(2)) == "progressing"

    def test_equal_pass_count_not_regression(self):
        entries = [
            _entry("initialize", _seq(0), run_id="run-a"),
            _clone_journal("run-a", _seq(1)),
            _pytest(_seq(2), exit_code=0, run_id="run-a"),
            _entry("initialize", _seq(3), run_id="run-b"),
            _clone_journal("run-b", _seq(4)),
            _pytest(_seq(5), exit_code=0, run_id="run-b"),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "run-b", _seq(6)) == "progressing"


# ---------------------------------------------------------------------------
# progressing fallback, precedence, API
# ---------------------------------------------------------------------------


class TestProgressing:
    def test_fallback_for_recent_healthy_run(self):
        entries = [
            _entry("initialize", _seq(0)),
            _tool("read_file_range", _seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", _seq(2)) == "progressing"

    def test_unknown_run_id_is_progressing(self):
        state = aggregate_run_phases(None, [_entry("initialize", _seq(0))])
        assert classify_run_health(state, "ghost-run", _seq(1)) == "progressing"

    def test_invalid_now_raises(self):
        state = aggregate_run_phases(None, [_entry("initialize", _seq(0))])
        with pytest.raises(ValueError):
            classify_run_health(state, "test-run", "not-a-timestamp")

    def test_classify_all_runs(self):
        entries = [
            _entry("initialize", _seq(0), run_id="run-a"),
            _entry("stop", _seq(1), run_id="run-a"),
            _entry("initialize", _seq(2), run_id="run-b"),
            _tool("edit_file", _seq(3), run_id="run-b", params={"file_path": "a.py"}),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_all_runs(state, _seq(4)) == {
            "run-a": "done",
            "run-b": "progressing",
        }

    def test_classification_does_not_mutate_state(self):
        entries = _looping_journal(3)
        state = aggregate_run_phases(None, entries)
        snapshot = {k: dict(v) for k, v in state.items()}
        classify_run_health(state, "test-run", _seq(20))
        assert state == snapshot

    def test_health_order_precedence_listing(self):
        assert HEALTH_ORDER == ("done", "looping", "stalled", "regression", "progressing")


class TestPrecedence:
    """First match wins, top to bottom: done > looping > stalled > regression."""

    def test_done_beats_looping(self):
        entries = _looping_journal(3) + [_boundary("publish", _seq(12))]
        state = aggregate_run_phases(None, entries)
        assert state["test-run"]["edit_verify_roundtrips"] == 3
        assert classify_run_health(state, "test-run", _seq(20)) == "done"

    def test_done_beats_stalled(self):
        entries = [
            _entry("initialize", _seq(0)),
            _entry("stop", _seq(1)),
        ]
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T03:00:00Z") == "done"

    def test_looping_beats_stalled(self):
        entries = _looping_journal(3)
        state = aggregate_run_phases(None, entries)
        assert classify_run_health(state, "test-run", "2026-01-01T03:00:00Z") == "looping"

    def test_stalled_beats_regression(self):
        entries = [
            _entry("initialize", _seq(0), run_id="run-a"),
            _clone_journal("run-a", _seq(1)),
            _pytest(_seq(2), exit_code=0, run_id="run-a"),
            _pytest(_seq(3), exit_code=0, run_id="run-a"),
            _entry("initialize", _seq(4), run_id="run-b"),
            _clone_journal("run-b", _seq(5)),
            _pytest(_seq(6), exit_code=0, run_id="run-b"),
        ]
        state = aggregate_run_phases(None, entries)
        # run-b is both stalled (idle > M) and a regression (1 < 2 passes).
        assert classify_run_health(state, "run-b", "2026-01-01T00:30:00Z") == "stalled"


# ---------------------------------------------------------------------------
# verify pass count
# ---------------------------------------------------------------------------


class TestVerifyPassCount:
    def test_counts_passing_outcomes_only(self):
        entries = [
            _pytest(_seq(0), exit_code=0),
            _pytest(_seq(1), exit_code=1),
            _verify_outcome(_seq(2), passes=9),
            _tool("lint_in_container", _seq(3)),
        ]
        state = aggregate_run_phases(None, entries)
        assert verify_pass_count(state["test-run"]) == 2
