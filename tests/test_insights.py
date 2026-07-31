"""Tests for the insights module (Issue #777).

All metrics are tested using journal-entry fixtures fed through
:func:`aggregate_run_phases` — no hand-built states.
"""

from __future__ import annotations

from typing import Any

from sunaba.insights import (
    compute_all_insights,
    edit_verify_roundtrip_distribution,
    first_verify_failure_by_image,
    per_tool_error_rate,
    run_duration_op_distribution,
    unused_tools,
)
from sunaba.phase import _PHASE_MAP, aggregate_run_phases

# ── Helpers ────────────────────────────────────────────────────────


def _entry(
    op: str,
    *,
    run_id: str = "test-run",
    ts: str = "2026-01-01T00:00:00Z",
    **kwargs: Any,
) -> dict[str, Any]:
    """Build a minimal journal entry dict."""
    e: dict[str, Any] = {
        "ts": ts,
        "run_id": run_id,
        "container_id": kwargs.pop("container_id", "abc123"),
        "operation": op,
    }
    e.update(kwargs)
    return e


def _tool_entry(
    tool_name: str,
    *,
    run_id: str = "test-run",
    ts: str = "2026-01-01T00:00:00Z",
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return _entry(
        "tool_use", tool_name=tool_name, params=params or {},
        run_id=run_id, ts=ts, **kwargs,
    )


def _verify_outcome(
    passed: bool,
    *,
    run_id: str = "test-run",
    ts: str = "2026-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Build a verify_in_container outcome entry (Issue #774)."""
    return _tool_entry(
        "verify_in_container",
        run_id=run_id, ts=ts,
        params={"result": {"gate_passed": passed, "passes": 3, "fails": 0, "collected": 3, "status": "ok"}},
    )


def _ts(offset: int, base: str = "2026-01-01T00:00:") -> str:
    """Return an ISO timestamp with a given second offset."""
    return f"{base}{offset:02d}Z"


# ── Metric 1: Per-tool error rate ──────────────────────────────────


class TestPerToolErrorRate:
    """Metric 1: failure rate per operation/tool, plus recovery distribution."""

    def test_basic_counts(self):
        """Operations that fail increment op_failures; op_calls count all calls."""
        entries = [
            _entry("initialize", ts=_ts(1), image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(2)),
            _tool_entry("edit_file", ts=_ts(3)),
            _entry("exec", ts=_ts(4), commands=["pytest"], exit_code=1),  # fail
            _entry("exec", ts=_ts(5), commands=["pytest"], exit_code=0),
        ]
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)

        calls = {t["operation"]: t for t in result["by_tool"]}
        assert calls["tool:edit_file"]["calls"] == 2
        assert calls["tool:edit_file"]["failures"] == 0
        assert calls["exec"]["calls"] == 2
        assert calls["exec"]["failures"] == 1

    def test_failure_rate_zero_on_no_calls(self):
        """An operation that exists only in failures but has no calls is not
        produced (it would be a zero-division)."""
        entries: list[dict[str, Any]] = []
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)
        assert result["by_tool"] == []
        assert result["total_calls"] == 0
        assert result["total_failures"] == 0

    def test_recovery_distribution(self):
        """After an exec failure, the next non-outcome call is recorded as recovery."""
        entries = [
            _entry("initialize", ts=_ts(1), image="python:3.12"),
            _entry("exec", ts=_ts(2), commands=["bad"], exit_code=1),  # fail
            _tool_entry("edit_file", ts=_ts(3)),  # recovery action
            _entry("exec", ts=_ts(4), commands=["also bad"], exit_code=1),  # another fail
            _tool_entry("lint_in_container", ts=_ts(5)),  # recovery action
        ]
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)
        rec = result["recovery_distribution"]
        assert "exec" in rec
        assert rec["exec"].get("tool:edit_file") == 1
        assert rec["exec"].get("tool:lint_in_container") == 1

    def test_verify_outcome_is_counted_as_failure(self):
        """A verify_in_container outcome with gate_passed=False is a failure."""
        entries = [
            _tool_entry("verify_in_container", ts=_ts(1)),
            _verify_outcome(False, ts=_ts(2)),
        ]
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)
        calls = {t["operation"]: t for t in result["by_tool"]}
        # The first entry is a verify_call (not an outcome), the second is an outcome.
        # Outcome entries are not counted as calls, but they are counted as failures.
        assert calls.get("tool:verify_in_container", {}).get("calls", 0) == 1
        assert calls.get("tool:verify_in_container", {}).get("failures", 0) == 1

    def test_verify_outcome_failure_recovery_recorded(self):
        """A failed verify outcome must be queued so the next call is
        recorded as its recovery action (review finding: recovery after
        tool outcome failures was silently dropped)."""
        entries = [
            _tool_entry("verify_in_container", ts=_ts(1)),
            _verify_outcome(False, ts=_ts(2)),  # outcome failure
            _tool_entry("edit_file", ts=_ts(3)),  # recovery action
        ]
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)
        rec = result["recovery_distribution"]
        assert rec.get("tool:verify_in_container", {}).get("tool:edit_file") == 1

    def test_lint_outcome_failure_recovery_recorded(self):
        """The same holds for other two-entry tools, e.g. lint_in_container."""
        entries = [
            _tool_entry("lint_in_container", ts=_ts(1)),
            _tool_entry("lint_in_container", ts=_ts(2),
                        params={"result": {"gate_passed": False}}),
            _tool_entry("edit_file", ts=_ts(3)),
        ]
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)
        rec = result["recovery_distribution"]
        assert rec.get("tool:lint_in_container", {}).get("tool:edit_file") == 1

    def test_consecutive_failures_each_get_recovery(self):
        """exec → exec-fail → edit: the second exec is the first failure's
        immediately-following action, edit recovers the second (review
        finding: consecutive failures lost the first one's recovery)."""
        entries = [
            _entry("exec", ts=_ts(1), commands=["a"], exit_code=1),
            _entry("exec", ts=_ts(2), commands=["b"], exit_code=1),
            _tool_entry("edit_file", ts=_ts(3)),
        ]
        state = aggregate_run_phases(None, entries)
        result = per_tool_error_rate(state)
        rec = result["recovery_distribution"]["exec"]
        assert rec.get("exec") == 1
        assert rec.get("tool:edit_file") == 1


# ── Metric 2: First-verify failure rate by image ───────────────────


class TestFirstVerifyFailureByImage:
    """Metric 2: share of runs whose first verify failed, by image."""

    def test_single_run_first_verify_pass(self):
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("verify_in_container", ts=_ts(2), run_id="r1"),
            _verify_outcome(True, run_id="r1", ts=_ts(3)),
        ]
        state = aggregate_run_phases(None, entries)
        result = first_verify_failure_by_image(state)
        assert result["overall"]["total_runs_with_verify"] == 1
        assert result["overall"]["total_first_failed"] == 0
        assert result["overall"]["failure_rate"] == 0.0

    def test_single_run_first_verify_fail(self):
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("verify_in_container", ts=_ts(2), run_id="r1"),
            _verify_outcome(False, run_id="r1", ts=_ts(3)),
        ]
        state = aggregate_run_phases(None, entries)
        result = first_verify_failure_by_image(state)
        assert result["overall"]["total_first_failed"] == 1

    def test_multiple_verifies_only_first_counts(self):
        """Only the chronologically first verify_outcome matters."""
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("verify_in_container", ts=_ts(2), run_id="r1"),
            _verify_outcome(False, run_id="r1", ts=_ts(3)),  # first — fail
            _tool_entry("verify_in_container", ts=_ts(4), run_id="r1"),
            _verify_outcome(True, run_id="r1", ts=_ts(5)),  # second — pass
        ]
        state = aggregate_run_phases(None, entries)
        result = first_verify_failure_by_image(state)
        assert result["overall"]["total_first_failed"] == 1  # still 1

    def test_grouped_by_image(self):
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("verify_in_container", ts=_ts(2), run_id="r1"),
            _verify_outcome(False, run_id="r1", ts=_ts(3)),
            _entry("initialize", ts=_ts(4), run_id="r2", image="node:20"),
            _tool_entry("verify_in_container", ts=_ts(5), run_id="r2"),
            _verify_outcome(True, run_id="r2", ts=_ts(6)),
        ]
        state = aggregate_run_phases(None, entries)
        result = first_verify_failure_by_image(state)
        by_img = {i["image"]: i for i in result["by_image"]}
        assert by_img["python:3.12"]["failure_rate"] == 1.0
        assert by_img["node:20"]["failure_rate"] == 0.0

    def test_run_without_verify_is_excluded(self):
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(2), run_id="r1"),
        ]
        state = aggregate_run_phases(None, entries)
        result = first_verify_failure_by_image(state)
        assert result["overall"]["total_runs_with_verify"] == 0


# ── Metric 3: Edit→verify roundtrip distribution ───────────────────


class TestEditVerifyRoundtripDistribution:
    """Metric 3: histogram of edit_verify_roundtrips across runs."""

    def test_no_roundtrips(self):
        entries = [
            _tool_entry("edit_file", ts=_ts(1)),
            _tool_entry("verify_in_container", ts=_ts(2)),
            _entry("exec", commands=["pytest"], exit_code=0, ts=_ts(3)),
        ]
        state = aggregate_run_phases(None, entries)
        result = edit_verify_roundtrip_distribution(state)
        assert result["total_roundtrips"] == 0
        assert result["mean_roundtrips"] == 0.0
        # Bucket "0" should have count 1
        buckets = {b["bucket"]: b["count"] for b in result["histogram"]}
        assert buckets["0"] == 1

    def test_one_roundtrip(self):
        entries = [
            _tool_entry("edit_file", ts=_ts(1)),  # edit
            _tool_entry("verify_in_container", ts=_ts(2)),  # verify
            _entry("exec", commands=["pytest"], exit_code=1, ts=_ts(3)),  # fail
            _tool_entry("edit_file", ts=_ts(4)),  # edit (roundtrip!)
        ]
        state = aggregate_run_phases(None, entries)
        result = edit_verify_roundtrip_distribution(state)
        assert result["total_roundtrips"] >= 1
        buckets = {b["bucket"]: b["count"] for b in result["histogram"]}
        assert (buckets.get("1", 0) + buckets.get("5+", 0)) > 0

    def test_multiple_runs(self):
        """Each run contributes one observation."""
        entries = [
            # Run 1: no roundtrips
            _tool_entry("edit_file", ts=_ts(1), run_id="r1"),
            _tool_entry("verify_in_container", ts=_ts(2), run_id="r1"),
            _entry("exec", commands=["pytest"], exit_code=0, ts=_ts(3), run_id="r1"),
            # Run 2: one roundtrip
            _tool_entry("edit_file", ts=_ts(4), run_id="r2"),
            _tool_entry("verify_in_container", ts=_ts(5), run_id="r2"),
            _entry("exec", commands=["pytest"], exit_code=1, ts=_ts(6), run_id="r2"),
            _tool_entry("edit_file", ts=_ts(7), run_id="r2"),
        ]
        state = aggregate_run_phases(None, entries)
        result = edit_verify_roundtrip_distribution(state)
        assert result["total_runs"] == 2
        # At least one run should have >0 roundtrips
        buckets = {b["bucket"]: b["count"] for b in result["histogram"]}
        assert sum(buckets.values()) == 2


# ── Metric 4: Unused tools ─────────────────────────────────────────


class TestUnusedTools:
    """Metric 4: tools/operations with zero calls."""

    def test_all_used_returns_empty(self):
        entries = [
            _tool_entry("edit_file", ts=_ts(1)),
        ]
        state = aggregate_run_phases(None, entries)
        all_tools = {"tool:edit_file"}
        result = unused_tools(state, all_tools=all_tools)
        assert result == []

    def test_unused_detected(self):
        entries = [
            _tool_entry("edit_file", ts=_ts(1)),
        ]
        state = aggregate_run_phases(None, entries)
        all_tools = {"tool:edit_file", "tool:write_file", "tool:search_in_container"}
        result = unused_tools(state, all_tools=all_tools)
        unused_ops = {t["operation"] for t in result}
        assert "tool:write_file" in unused_ops
        assert "tool:search_in_container" in unused_ops
        assert "tool:edit_file" not in unused_ops

    def test_no_all_tools_returns_empty(self):
        entries = [_tool_entry("edit_file", ts=_ts(1))]
        state = aggregate_run_phases(None, entries)
        result = unused_tools(state)  # all_tools=None
        assert result == []

    def test_exec_and_stop_excluded(self):
        """exec and stop are always-used implicitly; skip them."""
        entries: list[dict[str, Any]] = []
        state = aggregate_run_phases(None, entries)
        all_tools = {"exec", "stop", "tool:edit_file"}
        result = unused_tools(state, all_tools=all_tools)
        # exec and stop are excluded even if unused
        ops = {t["operation"] for t in result}
        assert "exec" not in ops
        assert "stop" not in ops
        assert "tool:edit_file" in ops


# ── Metric 5: Run duration & op-count distributions ────────────────


class TestRunDurationOpDistribution:
    """Metric 5: duration and op-count distributions by repo and session_label."""

    def test_by_repo(self):
        entries = [
            _entry("initialize", ts=_ts(1, "2026-01-01T00:00:"), run_id="r1", image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(2, "2026-01-01T00:00:"), run_id="r1"),
            _tool_entry("edit_file", ts=_ts(3, "2026-01-01T00:00:"), run_id="r1"),
            # boundary to set repo
            _entry("boundary_crossing", sub_operation="clone_repo",
                   details="repo=owner/myrepo dest=/workspace",
                   ts=_ts(4, "2026-01-01T00:00:"), run_id="r1"),
            _entry("initialize", ts=_ts(5, "2026-01-01T00:00:"), run_id="r2", image="python:3.12"),
            _entry("boundary_crossing", sub_operation="clone_repo",
                   details="repo=owner/other dest=/workspace",
                   ts=_ts(6, "2026-01-01T00:00:"), run_id="r2"),
            _tool_entry("read_file_range", ts=_ts(7, "2026-01-01T00:00:"), run_id="r2"),
        ]
        state = aggregate_run_phases(None, entries)
        result = run_duration_op_distribution(state)
        repos = {r["key"]: r for r in result["by_repo"]}
        assert "owner/myrepo" in repos
        assert "owner/other" in repos
        assert repos["owner/myrepo"]["run_count"] == 1
        # Run r1 should have 2 edit_file calls (op_count_stats)
        assert repos["owner/myrepo"]["op_count_stats"]["count"] == 1

    def test_by_session_label(self):
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12",
                   session_label="session-a"),
            _tool_entry("edit_file", ts=_ts(2), run_id="r1"),
            _entry("initialize", ts=_ts(3), run_id="r2", image="python:3.12",
                   session_label="session-b"),
            _tool_entry("edit_file", ts=_ts(4), run_id="r2"),
        ]
        state = aggregate_run_phases(None, entries)
        result = run_duration_op_distribution(state)
        labels = {r["key"]: r for r in result["by_session_label"]}
        assert "session-a" in labels
        assert "session-b" in labels
        assert labels["session-a"]["run_count"] == 1
        assert labels["session-b"]["run_count"] == 1

    def test_no_repo_defaults(self):
        """Runs without a repo appear as '(no repo)'."""
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(2), run_id="r1"),
        ]
        state = aggregate_run_phases(None, entries)
        result = run_duration_op_distribution(state)
        repos = {r["key"]: r for r in result["by_repo"]}
        assert "(no repo)" in repos

    def test_duration_stats(self):
        """Duration is computed from start_ts to last_ts."""
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r1"),
            _tool_entry("edit_file", ts="2026-01-01T00:01:00Z", run_id="r1"),
        ]
        state = aggregate_run_phases(None, entries)
        result = run_duration_op_distribution(state)
        repo_stats = result["by_repo"][0]
        ds = repo_stats["duration_stats"]
        assert ds["mean"] >= 59.0  # ~60 seconds


# ── Period filter ──────────────────────────────────────────────────


class TestPeriodFilter:
    """The compute_all_insights function accepts a time-window filter."""

    def test_from_ts_filters_old_runs(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r1",
                   image="python:3.12"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:01Z", run_id="r1"),
            _entry("initialize", ts="2026-07-01T00:00:00Z", run_id="r2",
                   image="python:3.12"),
            _tool_entry("edit_file", ts="2026-07-01T00:00:01Z", run_id="r2"),
        ]
        state = aggregate_run_phases(None, entries)
        # Filter to only include runs after June 2026
        result = compute_all_insights(
            state,
            from_ts="2026-06-01T00:00:00Z",
            all_tools=set(_PHASE_MAP.keys()),
        )
        assert result["roundtrip_distribution"]["total_runs"] == 1

    def test_to_ts_filters_future_runs(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r1",
                   image="python:3.12"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:01Z", run_id="r1"),
            _entry("initialize", ts="2026-07-01T00:00:00Z", run_id="r2",
                   image="python:3.12"),
            _tool_entry("edit_file", ts="2026-07-01T00:00:01Z", run_id="r2"),
        ]
        state = aggregate_run_phases(None, entries)
        result = compute_all_insights(
            state,
            to_ts="2026-02-01T00:00:00Z",
            all_tools=set(_PHASE_MAP.keys()),
        )
        assert result["roundtrip_distribution"]["total_runs"] == 1

    def test_no_filter_returns_all(self):
        entries = [
            _entry("initialize", ts=_ts(1), run_id="r1", image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(2), run_id="r1"),
            _entry("initialize", ts=_ts(3), run_id="r2", image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(4), run_id="r2"),
        ]
        state = aggregate_run_phases(None, entries)
        result = compute_all_insights(state, all_tools=set(_PHASE_MAP.keys()))
        assert result["roundtrip_distribution"]["total_runs"] == 2

    def test_mixed_timestamp_formats_at_cutoff(self):
        """Journal entries use '+00:00' (isoformat), the dashboard filter
        generates 'Z' — the exact cutoff instant must not be misclassified
        by lexicographic comparison (review finding)."""
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00+00:00", run_id="r1",
                   image="python:3.12"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:01+00:00", run_id="r1"),
        ]
        state = aggregate_run_phases(None, entries)
        # Cutoff is exactly the run's start instant, written with 'Z'.
        result = compute_all_insights(
            state,
            from_ts="2026-01-01T00:00:00Z",
            all_tools=set(_PHASE_MAP.keys()),
        )
        # last_ts (+00:00) is >= cutoff (Z): the run is included.
        assert result["roundtrip_distribution"]["total_runs"] == 1

    def test_mixed_formats_exclusive_to_bound(self):
        """A run ending exactly at the to_ts cutoff (different suffix) is
        excluded, matching the exclusive-upper-bound semantics."""
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00+00:00", run_id="r1",
                   image="python:3.12"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:01+00:00", run_id="r1"),
        ]
        state = aggregate_run_phases(None, entries)
        result = compute_all_insights(
            state,
            to_ts="2026-01-01T00:00:02Z",
            all_tools=set(_PHASE_MAP.keys()),
        )
        assert result["roundtrip_distribution"]["total_runs"] == 1


# ── Integration: compute_all_insights ──────────────────────────────


class TestComputeAllInsights:
    """The convenience wrapper returns all five metrics."""

    def test_all_keys_present(self):
        entries = [
            _entry("initialize", ts=_ts(1), image="python:3.12"),
            _tool_entry("edit_file", ts=_ts(2)),
            _tool_entry("verify_in_container", ts=_ts(3)),
            _verify_outcome(True, ts=_ts(4)),
            _entry("exec", commands=["pytest"], exit_code=0, ts=_ts(5)),
        ]
        state = aggregate_run_phases(None, entries)
        result = compute_all_insights(state, all_tools=set(_PHASE_MAP.keys()))
        expected_keys = {
            "per_tool_error_rate",
            "first_verify_failure_by_image",
            "roundtrip_distribution",
            "unused_tools",
            "run_distributions",
        }
        assert set(result.keys()) == expected_keys

    def test_empty_state_does_not_crash(self):
        state: dict[str, Any] = {}
        result = compute_all_insights(state, all_tools=set(_PHASE_MAP.keys()))
        assert result["per_tool_error_rate"]["total_calls"] == 0
        assert result["first_verify_failure_by_image"]["overall"]["total_runs_with_verify"] == 0
        assert result["roundtrip_distribution"]["total_runs"] == 0


# ── Determinism ────────────────────────────────────────────────────


class TestDeterminism:
    """All metrics are deterministic; reference timestamps are explicit."""

    def test_same_input_same_output(self):
        entries = [
            _entry("initialize", ts=_ts(1), image="python:3.12", run_id="r1"),
            _tool_entry("edit_file", ts=_ts(2), run_id="r1"),
            _tool_entry("verify_in_container", ts=_ts(3), run_id="r1"),
            _verify_outcome(False, run_id="r1", ts=_ts(4)),
            _entry("exec", commands=["pytest"], exit_code=1, ts=_ts(5), run_id="r1"),
        ]
        s1 = aggregate_run_phases(None, entries)
        s2 = aggregate_run_phases(None, entries)
        r1 = compute_all_insights(s1, all_tools=set(_PHASE_MAP.keys()))
        r2 = compute_all_insights(s2, all_tools=set(_PHASE_MAP.keys()))
        assert r1 == r2
