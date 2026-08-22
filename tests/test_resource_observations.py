"""Tests for the Issue #783 phase-1 resource observations.

Covers the three observations (concurrent-container timeline, disk usage,
initialize duration + busy-refusal proxy) at every layer: journal records,
phase aggregation, insights metrics, the disk-usage probe/cache, the
concurrency-bound refusal journaling, and the dashboard fragments.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sunaba import resources
from sunaba.dashboard import get_dashboard_url, start_dashboard, stop_dashboard
from sunaba.insights import (
    busy_refusal_counts,
    compute_all_insights,
    initialize_duration_distribution,
)
from sunaba.journal import (
    container_concurrency_timeline,
    get_host_run_id,
    get_or_create_run_id,
    read_journal,
    record_busy_refusal,
    timeline_from_lifecycle,
)
from sunaba.phase import (
    _PHASE_MAP,
    aggregate_run_phases,
    phase_for_entry,
)


def _entry(op: str, **kwargs: Any) -> dict[str, Any]:
    """Build a minimal journal entry dict."""
    e: dict[str, Any] = {
        "ts": kwargs.pop("ts", "2026-01-01T00:00:00Z"),
        "run_id": kwargs.pop("run_id", "test-run"),
        "container_id": kwargs.pop("container_id", "abc123"),
        "operation": op,
    }
    e.update(kwargs)
    return e


# ---------------------------------------------------------------------------
# Observation 1: concurrent-container timeline (journal layer)
# ---------------------------------------------------------------------------


class TestContainerConcurrencyTimeline:
    """journal.container_concurrency_timeline / timeline_from_lifecycle."""

    def test_empty_journal_is_zero(self):
        result = container_concurrency_timeline([])
        assert result == {"current": 0, "peak": 0, "series": []}

    def test_initialize_stop_sequence(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", container_id="aaa111"),
            _entry("initialize", ts="2026-01-01T00:00:10Z", container_id="bbb222"),
            _entry("initialize", ts="2026-01-01T00:00:20Z", container_id="ccc333"),
            _entry("stop", ts="2026-01-01T00:00:30Z", container_id="aaa111"),
            _entry("stop", ts="2026-01-01T00:01:00Z", container_id="bbb222"),
        ]
        result = container_concurrency_timeline(entries)
        assert result["current"] == 1  # only ccc333 still running
        assert result["peak"] == 3
        assert [s["count"] for s in result["series"]] == [1, 2, 3, 2, 1]
        assert result["series"][0]["ts"] == "2026-01-01T00:00:00Z"

    def test_stop_without_initialize_is_ignored(self):
        """A stop with no journaled initialize cannot close a lifetime."""
        result = container_concurrency_timeline(
            [_entry("stop", container_id="orphan99")]
        )
        assert result == {"current": 0, "peak": 0, "series": []}

    def test_reinitialize_reopens_lifetime(self):
        """A re-init after a stop reopens the container's lifetime.

        The lifecycle map tracks each container's *latest* lifetime
        (matching the sidecar's reset-on-init semantics), so the series
        contains only that lifetime's events.
        """
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", container_id="aaa111"),
            _entry("stop", ts="2026-01-01T00:00:10Z", container_id="aaa111"),
            _entry("initialize", ts="2026-01-01T00:00:20Z", container_id="aaa111"),
        ]
        result = container_concurrency_timeline(entries)
        assert result["current"] == 1
        assert [s["count"] for s in result["series"]] == [1]

    def test_host_entries_ignored(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", container_id="aaa111"),
            _entry("boundary_crossing", sub_operation="issue_view", container_id=None),
            _entry("busy_refusal", container_id=None, pool="docker"),
        ]
        result = container_concurrency_timeline(entries)
        assert result["current"] == 1
        assert len(result["series"]) == 1

    def test_timeline_from_lifecycle_matches_journal_scan(self):
        lifecycle = {
            "aaa111": {"init_ts": "2026-01-01T00:00:00Z", "stop_ts": "2026-01-01T00:00:30Z"},
            "bbb222": {"init_ts": "2026-01-01T00:00:10Z", "stop_ts": None},
            "orphan99": {"init_ts": None, "stop_ts": "2026-01-01T00:00:40Z"},
        }
        result = timeline_from_lifecycle(lifecycle)
        assert result["current"] == 1
        assert result["peak"] == 2
        assert [s["count"] for s in result["series"]] == [1, 2, 1]


# ---------------------------------------------------------------------------
# Observation 3: initialize duration + busy refusals (journal records)
# ---------------------------------------------------------------------------


class TestBusyRefusalJournal:
    """record_busy_refusal writes attributed journal entries."""

    def test_record_with_container(self):
        # The container's run must already exist for the refusal to be
        # attributed to it (a refusal never mints a run — #783 review).
        run_id = get_or_create_run_id("abc123")
        record_busy_refusal("docker", "global", 24, tool="sandbox_initialize", container_id="abc123")
        entries = read_journal()
        assert len(entries) == 1
        e = entries[0]
        assert e["operation"] == "busy_refusal"
        assert e["pool"] == "docker"
        assert e["limit"] == "global"
        assert e["cap"] == 24
        assert e["tool"] == "sandbox_initialize"
        assert e["container_id"] == "abc123"
        assert e["run_id"] == run_id

    def test_record_with_unknown_container_never_mints_a_run(self):
        """A refusal for a container the run map does not know (e.g. after a
        server restart) attributes to the host run instead of minting a
        refusal-only run — which would surface as a phantom "running" row
        and a false stalled badge (#783 review).
        """
        from sunaba import journal as journal_mod

        record_busy_refusal("docker", "per_container", 6, container_id="unknown99")
        entries = read_journal()
        assert len(entries) == 1
        e = entries[0]
        assert e["container_id"] == "unknown99"  # concerned container still recorded
        assert e["run_id"] == get_host_run_id()  # ... but attributed to the host run
        with journal_mod._run_map_lock:
            assert "unknown99" not in journal_mod._run_map  # no run minted

    def test_record_without_container_uses_host_run(self):
        record_busy_refusal("recovery", "recovery", 4)
        entries = read_journal()
        assert len(entries) == 1
        e = entries[0]
        assert e["container_id"] is None
        assert e["run_id"] == get_host_run_id()

    def test_busy_refusal_op_is_mapped_to_other_phase(self):
        assert "busy_refusal" in _PHASE_MAP
        assert _PHASE_MAP["busy_refusal"] == "other"
        assert phase_for_entry(_entry("busy_refusal", pool="docker")) == "other"


class TestInitializeTimingAggregation:
    """Phase aggregation folds init timing and per-pool refusal counts."""

    def test_aggregation_captures_timing_and_refusals(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", image="python:3.12"),
            _entry("initialize_complete", ts="2026-01-01T00:00:10Z"),
            _entry("busy_refusal", ts="2026-01-01T00:00:11Z", pool="docker", run_id="host-1", container_id=None),
            _entry("busy_refusal", ts="2026-01-01T00:00:12Z", pool="docker", run_id="host-1", container_id=None),
            _entry("busy_refusal", ts="2026-01-01T00:00:13Z", pool="recovery", run_id="host-1", container_id=None),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["init_start_ts"] == "2026-01-01T00:00:00Z"
        assert run["init_complete_ts"] == "2026-01-01T00:00:10Z"
        host = state["host-1"]
        assert host["busy_refusals"] == {"docker": 2, "recovery": 1}

    def test_incremental_contract_holds_for_new_fields(self):
        """aggregate(all) == aggregate(tail, aggregate(head)) for the new fields."""
        head = [
            _entry("initialize", ts="2026-01-01T00:00:00Z"),
            _entry("busy_refusal", ts="2026-01-01T00:00:01Z", pool="docker", run_id="host-1", container_id=None),
        ]
        tail = [
            _entry("initialize_complete", ts="2026-01-01T00:00:10Z"),
            _entry("busy_refusal", ts="2026-01-01T00:00:11Z", pool="recovery", run_id="host-1", container_id=None),
        ]
        all_at_once = aggregate_run_phases(None, head + tail)
        incremental = aggregate_run_phases(aggregate_run_phases(None, head), tail)
        assert all_at_once == incremental


# ---------------------------------------------------------------------------
# Observation 3: insights metrics
# ---------------------------------------------------------------------------


class TestInitializeDurationInsight:
    """initialize_duration_distribution: stats + histogram + abandoned/in_flight."""

    def _state(self) -> dict[str, Any]:
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r1"),
            _entry("initialize_complete", ts="2026-01-01T00:00:10Z", run_id="r1"),
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r2"),
            _entry("initialize_complete", ts="2026-01-01T00:00:40Z", run_id="r2"),
            # r3: stopped without completing init -> positively abandoned
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r3"),
            _entry("stop", ts="2026-01-01T00:01:00Z", run_id="r3"),
            # r4: no completion and no stop -> in flight, NOT abandoned
            _entry("initialize", ts="2026-01-01T00:00:00Z", run_id="r4"),
        ]
        return aggregate_run_phases(None, entries)

    def test_distribution_stats_and_abandoned_vs_in_flight(self):
        result = initialize_duration_distribution(self._state())
        assert result["total_runs"] == 4
        assert result["abandoned"] == 1  # r3 (stop without completion)
        assert result["in_flight"] == 1  # r4 (still running as far as the journal knows)
        stats = result["stats"]
        assert stats["count"] == 2
        assert stats["min"] == 10.0
        assert stats["max"] == 40.0
        assert stats["mean"] == 25.0
        assert stats["median"] == 25.0

    def test_histogram_buckets(self):
        result = initialize_duration_distribution(self._state())
        by_bucket = {item["bucket"]: item["count"] for item in result["histogram"]}
        assert by_bucket["0-5"] == 0
        assert by_bucket["5-15"] == 1  # 10s
        assert by_bucket["30-60"] == 1  # 40s
        assert sum(by_bucket.values()) == 2

    def test_empty_state(self):
        result = initialize_duration_distribution({})
        assert result["stats"]["count"] == 0
        assert result["abandoned"] == 0
        assert result["in_flight"] == 0
        assert sum(i["count"] for i in result["histogram"]) == 0

    def test_refusals_do_not_leak_into_phase_segments_or_counters(self):
        """A busy_refusal is counted and nothing else (issue #783 review).

        It must not open/extend phase segments, bump ``entry_count`` or
        overwrite ``last_op`` (which would perturb the health classifier's
        long-running-operation exemption).
        """
        base = [_entry("initialize", ts="2026-01-01T00:00:00Z")]
        refusals = [
            _entry("busy_refusal", ts="2026-01-01T00:00:01Z", pool="docker"),
            _entry("busy_refusal", ts="2026-01-01T00:00:02Z", pool="docker"),
        ]
        baseline = aggregate_run_phases(None, base)["test-run"]
        run = aggregate_run_phases(None, base + refusals)["test-run"]
        assert run["busy_refusals"] == {"docker": 2}
        assert run["entry_count"] == baseline["entry_count"]
        assert run["phases"] == baseline["phases"]
        assert run["last_op"] == baseline["last_op"]


class TestBusyRefusalInsight:
    """busy_refusal_counts: per-pool totals across runs."""

    def test_counts_by_pool(self):
        entries = [
            _entry("busy_refusal", pool="docker", run_id="host-1", container_id=None),
            _entry("busy_refusal", pool="docker", run_id="host-1", container_id=None),
            _entry("busy_refusal", pool="recovery", run_id="host-1", container_id=None),
            _entry("busy_refusal", pool="docker", run_id="run-x", container_id="abc123"),
        ]
        state = aggregate_run_phases(None, entries)
        result = busy_refusal_counts(state)
        assert result["total"] == 4
        by_pool = {item["pool"]: item["count"] for item in result["by_pool"]}
        assert by_pool == {"docker": 3, "recovery": 1}

    def test_empty_state(self):
        assert busy_refusal_counts({}) == {"by_pool": [], "total": 0}

    def test_compute_all_insights_contract_is_unchanged(self):
        """The five-key contract of compute_all_insights stays fixed (#777);
        the Issue #783 metrics are separate public functions."""
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:00Z", image="python:3.12"),
            _entry("initialize_complete", ts="2026-01-01T00:00:05Z"),
            _entry("busy_refusal", pool="docker", run_id="host-1", container_id=None),
        ]
        state = aggregate_run_phases(None, entries)
        insights = compute_all_insights(state, all_tools=set(_PHASE_MAP.keys()))
        assert set(insights.keys()) == {
            "per_tool_error_rate",
            "first_verify_failure_by_image",
            "roundtrip_distribution",
            "unused_tools",
            "run_distributions",
            "verify_failure_reasons",
            "initialize_distributions",
            "busy_refusals",
        }
        # The new metrics compute from the same period-filtered runs.
        from sunaba.insights import filter_runs_by_period

        filtered = filter_runs_by_period(state, None, None)
        assert initialize_duration_distribution(filtered)["stats"]["count"] == 1
        assert busy_refusal_counts(filtered)["total"] == 1
        # busy_refusal is a resource event, never an "unused tool" candidate.
        ops = [item["operation"] for item in insights["unused_tools"]]
        assert "busy_refusal" not in ops

    def test_refusals_do_not_pollute_the_five_777_metrics(self):
        """A busy_refusal between a failure and its recovery must not appear
        as a fake tool row, inflate op counts, or be reported as the
        recovery action of a pending failure (#783 review finding)."""
        from sunaba.insights import per_tool_error_rate, run_duration_op_distribution

        base = [
            # run-a: exec fails (START then completion, Issue #789), then a
            # refused call, then a successful exec.
            _entry("exec", ts="2026-01-01T00:00:00Z", run_id="run-a", commands=["pytest"]),
            _entry("exec", ts="2026-01-01T00:00:01Z", run_id="run-a", commands=["pytest"], exit_code=1),
            _entry("busy_refusal", ts="2026-01-01T00:00:02Z", run_id="run-a", container_id="abc123", pool="docker"),
            _entry("exec", ts="2026-01-01T00:00:03Z", run_id="run-a", commands=["pytest"]),
            _entry("exec", ts="2026-01-01T00:00:04Z", run_id="run-a", commands=["pytest"], exit_code=0),
            # run-b: only a refusal (host-attributed).
            _entry("busy_refusal", ts="2026-01-01T00:00:05Z", run_id="host-1", container_id=None, pool="recovery"),
        ]
        state = aggregate_run_phases(None, base)

        # (a) per_tool_error_rate: no busy_refusal row, totals unchanged.
        err = per_tool_error_rate(state)
        ops = {t["operation"]: t for t in err["by_tool"]}
        assert "busy_refusal" not in ops
        # The failed exec is the only failure and "exec" is its only call
        # (both exec START + completion entries carry the same op key).
        assert ops["exec"]["calls"] == 2
        assert ops["exec"]["failures"] == 1
        assert err["total_calls"] == 2
        assert err["total_failures"] == 1

        # (b) failure recovery: the refused call is NOT the recovery action;
        # the successful exec is.
        rec = err["recovery_distribution"]
        assert rec.get("exec") == {"exec": 1}
        assert "busy_refusal" not in rec

        # (c) run op counts are not inflated by refusals.
        dist = run_duration_op_distribution(state)
        by_repo = {item["key"]: item for item in dist["by_repo"]}
        run_a = by_repo["(no repo)"]  # single repo bucket
        assert run_a["op_count_stats"]["max"] == 2  # only the two execs

        # The new #783 metrics still see the refusals (they are the point).
        assert busy_refusal_counts(state)["total"] == 2
        assert state["run-a"]["busy_refusals"] == {"docker": 1}


# ---------------------------------------------------------------------------
# Observation 2: disk usage (probe + cache)
# ---------------------------------------------------------------------------


class TestDiskUsage:
    """resources.measure_disk_usage / cached_disk_usage / _dir_bytes."""

    @pytest.fixture(autouse=True)
    def _fresh_disk_cache(self):
        """Isolate the module-level cache and wait out any refresh worker."""
        self._drain_refresh_worker()
        with resources._disk_cache_lock:
            resources._disk_cache.update({"ts": 0.0, "value": None, "probing": False})
        yield
        self._drain_refresh_worker()

    @staticmethod
    def _drain_refresh_worker(timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with resources._disk_cache_lock:
                if not resources._disk_cache["probing"]:
                    return
            time.sleep(0.01)
        raise AssertionError("background disk probe did not finish")

    def test_measure_with_fake_probe(self):
        fake = lambda: {"ok": True, "total_bytes": 42}  # noqa: E731
        assert resources.measure_disk_usage(probe=fake) == {"ok": True, "total_bytes": 42}

    def test_cached_probe_called_once_within_interval(self):
        calls: list[int] = []

        def fake_probe() -> dict[str, Any]:
            calls.append(1)
            return {"total_bytes": len(calls)}

        first = resources.cached_disk_usage(interval_s=60.0, force=True, probe=fake_probe)
        second = resources.cached_disk_usage(interval_s=60.0, probe=fake_probe)
        assert first == second == {"total_bytes": 1}
        assert len(calls) == 1  # cached: probe not re-run

    def test_force_reprobes_synchronously(self):
        calls: list[int] = []

        def fake_probe() -> dict[str, Any]:
            calls.append(1)
            return {"total_bytes": len(calls)}

        resources.cached_disk_usage(interval_s=60.0, force=True, probe=fake_probe)
        second = resources.cached_disk_usage(interval_s=60.0, force=True, probe=fake_probe)
        assert second == {"total_bytes": 2}
        assert len(calls) == 2

    def test_stale_cache_served_immediately_and_refreshed_in_background(self):
        """A stale render never waits for the probe (issue #783 review).

        The previous value is returned while a background worker runs the
        probe; the cache is replaced when it finishes.
        """
        import threading

        release = threading.Event()
        calls: list[int] = []

        def gated_probe() -> dict[str, Any]:
            calls.append(1)
            if len(calls) > 1:
                release.wait(5.0)  # later probes block until released
            return {"total_bytes": len(calls)}

        first = resources.cached_disk_usage(interval_s=0.0, probe=gated_probe)
        assert first == {"total_bytes": 1}  # first-ever call probes synchronously

        stale = resources.cached_disk_usage(interval_s=0.0, probe=gated_probe)
        # Served the previous measurement without waiting on the gated probe.
        assert stale == {"total_bytes": 1}

        release.set()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            with resources._disk_cache_lock:
                if resources._disk_cache["value"] == {"total_bytes": 2}:
                    break
            time.sleep(0.01)
        with resources._disk_cache_lock:
            assert resources._disk_cache["value"] == {"total_bytes": 2}

    def test_stale_refresh_is_single_flight(self):
        """Concurrent stale renders spawn exactly one background probe."""
        import threading

        release = threading.Event()
        started = threading.Event()
        calls: list[int] = []

        def slow_probe() -> dict[str, Any]:
            calls.append(1)
            started.set()
            release.wait(5.0)
            return {"total_bytes": 99}

        resources.cached_disk_usage(
            interval_s=60.0, force=True, probe=lambda: {"total_bytes": 1}
        )
        # All stale: the first kicks the worker, the rest see probing=True.
        resources.cached_disk_usage(interval_s=0.0, probe=slow_probe)
        assert started.wait(5.0)
        resources.cached_disk_usage(interval_s=0.0, probe=slow_probe)
        resources.cached_disk_usage(interval_s=0.0, probe=slow_probe)
        assert len(calls) == 1
        release.set()

    def test_dir_bytes_counts_files_recursively(self, tmp_path: Path):
        (tmp_path / "journal.log").write_text("x" * 100)
        sub = tmp_path / "traces"
        sub.mkdir()
        (sub / "run.json").write_text("y" * 50)
        assert resources._dir_bytes(tmp_path) == 150
        assert resources._dir_bytes(tmp_path / "missing") == 0

    def test_default_probe_shape(self):
        with patch(
            "sunaba.resources._measure_docker_disk",
            return_value={"images_bytes": 1024, "containers_bytes": 512, "error": None},
        ):
            result = resources.measure_disk_usage()
        assert result["docker"]["images_bytes"] == 1024
        assert result["docker"]["containers_bytes"] == 512
        assert result["total_bytes"] == 1536
        assert "journal_dir" in result
        assert isinstance(result["journal_dir"]["bytes"], int)

    def test_default_probe_degrades_when_docker_unavailable(self):
        with patch(
            "sunaba.resources._measure_docker_disk",
            return_value={"images_bytes": None, "containers_bytes": None, "error": "nope"},
        ):
            result = resources.measure_disk_usage()
        assert result["total_bytes"] is None  # only journal component known
        assert result["docker"]["error"] == "nope"
        assert isinstance(result["journal_dir"]["bytes"], int)


# ---------------------------------------------------------------------------
# Observation 3: docker_bound / recovery_bound journal refusals (common.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_docker_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop cached docker/recovery-cap state so env knobs take effect."""
    import sunaba.tools.common as common_mod

    monkeypatch.setattr(common_mod, "_DOCKER_GLOBAL_SEMAPHORE", None, raising=False)
    monkeypatch.setattr(common_mod, "_DOCKER_PER_CONTAINER_SEMAPHORES", {}, raising=False)
    monkeypatch.setattr(common_mod, "_RECOVERY_SEMAPHORE", None, raising=False)
    yield


def _plain_tool(container_id: str = "abc123") -> str:
    """Docker-free dummy tool used only to exercise the wrappers."""
    return "ok"


class TestBusyRefusalJournaling:
    """Refused calls write a busy_refusal journal entry (Issue #783)."""

    def test_docker_bound_refusal_is_journaled(self, monkeypatch: pytest.MonkeyPatch):
        import sunaba.tools.common as common_mod

        monkeypatch.setenv("SUNABA_DOCKER_GLOBAL_CONCURRENCY", "1")
        wrapped = common_mod.docker_bound(_plain_tool)
        # Occupy the single global permit so the next call is refused.
        sem = common_mod._get_global_docker_semaphore()
        assert sem.acquire(blocking=False)
        try:
            result = wrapped("abc123")
        finally:
            sem.release()
        payload = json.loads(result)
        assert payload["busy"] is True
        assert payload["pool"] == "docker"
        # The limit field is part of the payload (not a dead field) and is
        # carried into the journal entry (review finding).
        assert payload["limit"] == "global"
        entries = read_journal()
        refusals = [e for e in entries if e.get("operation") == "busy_refusal"]
        assert len(refusals) == 1
        assert refusals[0]["pool"] == "docker"
        assert refusals[0]["limit"] == "global"
        assert refusals[0]["cap"] == 1
        assert refusals[0]["tool"] == "_plain_tool"
        assert refusals[0]["container_id"] == "abc123"

    def test_recovery_refusal_is_journaled(self, monkeypatch: pytest.MonkeyPatch):
        import sunaba.tools.common as common_mod

        monkeypatch.setenv("SUNABA_RECOVERY_CONCURRENCY", "1")
        wrapped = common_mod.recovery_bound(_plain_tool)
        sem = common_mod._get_recovery_semaphore()
        assert sem.acquire(blocking=False)
        try:
            result = wrapped("abc123")
        finally:
            sem.release()
        payload = json.loads(result)
        assert payload["pool"] == "recovery"
        assert payload["limit"] == "recovery"
        refusals = [e for e in read_journal() if e.get("operation") == "busy_refusal"]
        assert len(refusals) == 1
        assert refusals[0]["pool"] == "recovery"
        assert refusals[0]["limit"] == "recovery"
        assert refusals[0]["cap"] == 1

    def test_successful_call_journals_nothing(self, monkeypatch: pytest.MonkeyPatch):
        import sunaba.tools.common as common_mod

        wrapped = common_mod.docker_bound(_plain_tool)
        assert wrapped("abc123") == "ok"
        assert read_journal() == []


# ---------------------------------------------------------------------------
# Dashboard fragments (observation 1 + 2 on "/", observation 3 on /insights)
# ---------------------------------------------------------------------------


class TestDashboardResourceCards:
    """The dashboard's resource cards render from the journal/cache."""

    def _serve(self, path: str) -> str:
        url = get_dashboard_url()
        assert url is not None
        with urllib.request.urlopen(url + path) as resp:
            assert resp.status == 200
            return resp.read().decode("utf-8")

    def test_dashboard_shows_concurrency_and_disk_cards(self):
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "r1", "container_id": "aaa111", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:10Z", "run_id": "r2", "container_id": "bbb222", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:20Z", "run_id": "r3", "container_id": "ccc333", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:30Z", "run_id": "r1", "container_id": "aaa111", "operation": "stop"},
        ]
        disk = {
            "measured_at": "2026-01-01T00:00:00Z",
            "docker": {"images_bytes": 5 * (1 << 30), "containers_bytes": 1 * (1 << 30), "error": None},
            "journal_dir": {"path": "/tmp/.sunaba", "bytes": 2048},
            "total_bytes": 6 * (1 << 30) + 2048,
        }
        live = [
            {"container_id": "bbb222", "kind": "sandbox", "status": "running"},
            {"container_id": "ccc333", "kind": "sandbox", "status": "running"},
        ]
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch("sunaba.dashboard.read_journal_snapshot", return_value=(entries, 0)),
                patch("sunaba.dashboard.cached_disk_usage", return_value=disk),
                patch("sunaba.dashboard.list_managed_containers", return_value=(live, None)),
            ):
                html = self._serve("/")
                # Observation 1: journal current=2 (ccc + bbb), peak=3, and
                # the live-docker reconciliation (2 sandboxes) shown separately.
                assert "Concurrent Containers" in html
                assert "Current (journal)" in html
                assert "Live now (docker)" in html
                assert "Peak (journal)" in html
                assert ">3<" in html
                assert ">2<" in html
                # Trend bar labels are HH:MM:SS UTC of the events.
                assert "00:00:30" in html
                # The limitation note qualifies the journal-derived numbers.
                assert "without a journaled stop" in html
                # Observation 2: disk card with formatted sizes.
                assert "Disk Usage" in html
                assert "5.0 GB" in html
                assert "1.0 GB" in html
                assert "2.0 KB" in html
        finally:
            stop_dashboard()

    def test_concurrency_card_degrades_when_docker_unreachable(self):
        """A failed docker listing shows a dash for 'Live now', not a crash."""
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "r1", "container_id": "aaa111", "operation": "initialize"},
        ]
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch("sunaba.dashboard.read_journal_snapshot", return_value=(entries, 0)),
                patch("sunaba.dashboard.list_managed_containers", return_value=([], "daemon down")),
            ):
                html = self._serve("/")
                assert "Live now (docker)" in html
                assert "daemon down" in html
                assert "Current (journal)" in html
        finally:
            stop_dashboard()

    def test_insights_shows_initialize_duration_and_busy_refusals(self):
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run-a", "container_id": "aaa111", "operation": "initialize", "image": "python:3.12"},
            {"ts": "2026-01-01T00:00:10Z", "run_id": "run-a", "container_id": "aaa111", "operation": "initialize_complete"},
            {"ts": "2026-01-01T00:00:11Z", "run_id": "host-1", "container_id": None, "operation": "busy_refusal", "pool": "docker", "limit": "global", "cap": 24},
            {"ts": "2026-01-01T00:00:12Z", "run_id": "host-1", "container_id": None, "operation": "busy_refusal", "pool": "docker", "limit": "global", "cap": 24},
            {"ts": "2026-01-01T00:00:13Z", "run_id": "host-1", "container_id": None, "operation": "busy_refusal", "pool": "recovery", "limit": "recovery", "cap": 4},
        ]
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with patch("sunaba.dashboard.read_journal_snapshot", return_value=(entries, 0)):
                html = self._serve("/insights")
                # Observation 3: initialize-duration panel (10s mean).
                assert "6. Initialize Duration" in html
                assert "mean 10s" in html
                # Busy-refusal proxy panel: total 3 across pools.
                assert "7. Busy Refusals by Pool" in html
                assert "(total 3)" in html
                assert ">docker<" in html
                assert ">recovery<" in html
        finally:
            stop_dashboard()
