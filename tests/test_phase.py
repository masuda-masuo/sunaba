"""Tests for the phase aggregation module (Issue #774)."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sunaba.phase import (
    _PHASE_MAP,
    _entry_failed,
    _is_outcome_entry,
    aggregate_run_phases,
    phase_for_entry,
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def _entry(op: str, **kwargs) -> dict:
    """Build a minimal journal entry dict."""
    e: dict = {
        "ts": kwargs.pop("ts", "2026-01-01T00:00:00Z"),
        "run_id": kwargs.pop("run_id", "test-run"),
        "container_id": kwargs.pop("container_id", "abc123"),
        "operation": op,
    }
    e.update(kwargs)
    return e


def _tool_entry(tool_name: str, **kwargs) -> dict:
    return _entry("tool_use", tool_name=tool_name, **kwargs)


def _boundary_entry(sub_operation: str, **kwargs) -> dict:
    return _entry("boundary_crossing", sub_operation=sub_operation, **kwargs)


# ── Dynamic operation discovery ─────────────────────────────────────────────
#
# Instead of maintaining a manual enumeration of every tool_name and
# boundary sub_operation, we scan the source tree for call sites so
# that adding a new tool without updating ``_PHASE_MAP`` fails the
# coverage test (acceptance criterion 3).

_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"


def _discover_call_sites(func_name: str) -> set[str]:
    """Find all string-literal second arguments to calls of *func_name*
    in Python files under ``src/sunaba/``."""
    found: set[str] = set()
    tools_dir = _SRC_ROOT / "sunaba" / "tools"
    if not tools_dir.is_dir():
        return found

    for py_file in sorted(tools_dir.rglob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match func_name(...) — both direct and module-qualified calls.
            name = _call_func_name(node)
            if name != func_name:
                continue
            # Second positional arg should be the tool_name / sub_operation.
            if len(node.args) < 2:
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
    return found


def _call_func_name(node: ast.Call) -> str | None:
    """Extract the unqualified function name from a Call node, e.g.
    ``record_tool_use(...)`` → ``"record_tool_use"``."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _discover_journal_operations() -> set[str]:
    """Find all ``"operation": "xxx"`` string values in journal.py."""
    ops: set[str] = set()
    journal_py = _SRC_ROOT / "sunaba" / "journal.py"
    if not journal_py.is_file():
        return ops
    text = journal_py.read_text(encoding="utf-8")
    for m in re.finditer(r'"operation"\s*:\s*"([a-z_]+)"', text):
        ops.add(m.group(1))
    return ops


# ── Acceptance criterion 3: every operation is covered ──────────────────────


class TestPhaseMappingCoverage:
    """Acceptance criterion 3: the mapping covers every known operation.

    These tests **dynamically discover** the operation names that the
    journal-writing code can produce.  If a developer adds a new MCP
    tool and writes journal entries with its name but does not update
    ``_PHASE_MAP``, the corresponding test will fail — the operation
    is discovered from the source but has no mapping entry.
    """

    def test_direct_operations_covered(self):
        """Every direct operation in journal.py has an explicit map entry.

        Runtime falls back to 'other' for unknown keys, but the coverage
        gate requires an *explicit* entry — otherwise a new operation
        would silently pass as 'other' and this test would be vacuous.
        """
        ops = _discover_journal_operations()
        assert ops, "AST discovery found no direct operations — discovery broken"
        # tool_use / boundary_crossing are resolver-internal: the resolver
        # rewrites them to 'tool:*' / 'boundary:*' keys, so the bare names
        # are never looked up in _PHASE_MAP.
        for op_name in sorted(ops - {"tool_use", "boundary_crossing"}):
            assert op_name in _PHASE_MAP, (
                f"direct operation {op_name!r} has no explicit _PHASE_MAP entry"
            )

    def test_tool_names_covered(self):
        """Every tool_name from ``record_tool_use`` calls has an explicit entry."""
        tool_names = _discover_call_sites("record_tool_use")
        assert tool_names, "AST discovery found no record_tool_use call sites — discovery broken"
        for tool_name in sorted(tool_names):
            key = f"tool:{tool_name}"
            assert key in _PHASE_MAP, (
                f"tool {tool_name!r} has no explicit entry (add {key!r} to _PHASE_MAP)"
            )

    def test_boundary_sub_ops_covered(self):
        """Every sub_operation from ``record_boundary_crossing`` calls has an explicit entry."""
        sub_ops = _discover_call_sites("record_boundary_crossing")
        assert sub_ops, "AST discovery found no record_boundary_crossing call sites — discovery broken"
        for sub_op in sorted(sub_ops):
            key = f"boundary:{sub_op}"
            assert key in _PHASE_MAP, (
                f"boundary {sub_op!r} has no explicit entry (add {key!r} to _PHASE_MAP)"
            )

    def test_unknown_operation_does_not_crash(self):
        """Unknown/future operations resolve to 'other'."""
        assert phase_for_entry(_entry("future_tool_xyz")) == "other"
        assert phase_for_entry(_tool_entry("future_tool_abc")) == "other"
        assert phase_for_entry(_boundary_entry("future_boundary_op")) == "other"

# ── Acceptance criterion 1: incremental contract ────────────────────────────


class TestIncrementalContract:
    """The aggregation function must satisfy:
       ``aggregate(all) == aggregate(tail, aggregate(head))``
    """

    def _full_journal(self) -> list[dict]:
        """A complete journal for a single run through all phases."""
        ts = 0
        def nxt(op, **kw):
            nonlocal ts
            ts += 1
            t = f"2026-01-01T00:00:{ts:02d}Z"
            if op == "tool_use":
                return _tool_entry(kw.pop("tool_name"), ts=t, **kw)
            if op == "boundary_crossing":
                return _boundary_entry(kw.pop("sub_operation"), ts=t, **kw)
            return _entry(op, ts=t, **kw)

        return [
            nxt("initialize", image="python:3.12"),
            nxt("initialize_complete"),
            nxt("boundary_crossing", sub_operation="clone_repo"),
            # explore
            nxt("tool_use", tool_name="list_files"),
            nxt("tool_use", tool_name="read_file_range"),
            nxt("tool_use", tool_name="search_in_container"),
            # edit
            nxt("tool_use", tool_name="edit_file", params={"file_path": "src/foo.py"}),
            nxt("write_file", file_name="bar.py", dest_dir="/workspace/src"),
            nxt("tool_use", tool_name="transform_file", params={"file_path": "src/foo.py"}),
            # verify (fail)
            nxt("tool_use", tool_name="lint_in_container"),
            nxt("tool_use", tool_name="verify_in_container"),
            nxt("exec", commands=["pytest tests/ -x"], exit_code=1),
            # edit again
            nxt("tool_use", tool_name="edit_file", params={"file_path": "src/foo.py"}),
            # verify (pass)
            nxt("tool_use", tool_name="verify_in_container"),
            nxt("exec", commands=["pytest tests/ -x"], exit_code=0),
            # publish
            nxt("boundary_crossing", sub_operation="checkpoint"),
            nxt("boundary_crossing", sub_operation="publish"),
            # stop
            nxt("stop"),
        ]

    def test_full_vs_chunked_equivalence(self):
        """Aggregating the whole journal at once == splitting into chunks
        and aggregating sequentially."""
        entries = self._full_journal()

        # All at once
        full_state = aggregate_run_phases(None, entries)

        # Split into chunks and apply incrementally
        inc_state: dict | None = None
        chunk_size = 5
        for i in range(0, len(entries), chunk_size):
            chunk = entries[i : i + chunk_size]
            inc_state = aggregate_run_phases(inc_state, chunk)

        assert inc_state is not None

        # Compare the single run state
        full_run = full_state["test-run"]
        inc_run = inc_state["test-run"]

        assert full_run["run_id"] == inc_run["run_id"]
        assert full_run["image"] == inc_run["image"]
        assert full_run["session_label"] == inc_run["session_label"]
        assert full_run["start_ts"] == inc_run["start_ts"]
        assert full_run["last_ts"] == inc_run["last_ts"]
        assert full_run["touched_files"] == inc_run["touched_files"]
        assert full_run["edit_verify_roundtrips"] == inc_run["edit_verify_roundtrips"]
        # Issue #777 fields must also be chunk-invariant.
        assert full_run["op_calls"] == inc_run["op_calls"]
        assert full_run["op_failures"] == inc_run["op_failures"]
        assert full_run["failure_recovery"] == inc_run["failure_recovery"]
        assert full_run["pending_failure_ops"] == inc_run["pending_failure_ops"]
        assert len(full_run["phases"]) == len(inc_run["phases"])

        for i, (fs, is_) in enumerate(
            zip(full_run["phases"], inc_run["phases"])
        ):
            assert fs["phase"] == is_["phase"], f"phase mismatch at segment {i}"
            assert fs["op_count"] == is_["op_count"], f"op_count mismatch at segment {i}"
            assert fs["breakdown"] == is_["breakdown"], f"breakdown mismatch at segment {i}"

        assert len(full_run["verify_timeline"]) == len(inc_run["verify_timeline"])

    def test_empty_entries_preserves_state(self):
        """Aggregating an empty list leaves state unchanged."""
        entries = self._full_journal()
        s1 = aggregate_run_phases(None, entries)
        s2 = aggregate_run_phases(s1, [])
        assert s2 is not s1  # new dict
        assert s2 == s1       # but equal content

    def test_none_state_treated_as_empty(self):
        """Passing None as state is equivalent to an empty dict."""
        entries = self._full_journal()
        from_none = aggregate_run_phases(None, entries)
        from_empty = aggregate_run_phases({}, entries)
        assert from_none == from_empty


# ── Phase classification tests ──────────────────────────────────────────────


class TestPhaseClassification:
    """Unit tests for phase_for_entry."""

    def test_init_phase(self):
        assert phase_for_entry(_entry("initialize")) == "init"
        assert phase_for_entry(_entry("initialize_complete")) == "init"
        assert phase_for_entry(_boundary_entry("clone_repo")) == "init"
        assert phase_for_entry(_boundary_entry("setup_pr_branch")) == "init"

    def test_explore_phase(self):
        assert phase_for_entry(_tool_entry("read_file_range")) == "explore"
        assert phase_for_entry(_tool_entry("list_files")) == "explore"
        assert phase_for_entry(_tool_entry("search_in_container")) == "explore"
        assert phase_for_entry(_boundary_entry("issue_view")) == "explore"

    def test_edit_phase(self):
        assert phase_for_entry(_entry("write_file")) == "edit"
        assert phase_for_entry(_entry("copy_project")) == "edit"
        assert phase_for_entry(_entry("copy_file")) == "edit"
        assert phase_for_entry(_tool_entry("write_file")) == "edit"
        assert phase_for_entry(_tool_entry("edit_file")) == "edit"
        assert phase_for_entry(_tool_entry("transform_file")) == "edit"
        assert phase_for_entry(_tool_entry("undo_file_edit")) == "edit"

    def test_verify_phase(self):
        assert phase_for_entry(_tool_entry("verify_in_container")) == "verify"
        assert phase_for_entry(_tool_entry("lint_in_container")) == "verify"
        assert phase_for_entry(_tool_entry("type_check_in_container")) == "verify"
        assert phase_for_entry(_tool_entry("diff_in_container")) == "verify"

    def test_publish_phase(self):
        assert phase_for_entry(_boundary_entry("publish")) == "publish"
        assert phase_for_entry(_boundary_entry("pr_review_write")) == "publish"
        assert phase_for_entry(_boundary_entry("issue_write")) == "publish"
        assert phase_for_entry(_boundary_entry("checkpoint")) == "publish"
        assert phase_for_entry(_boundary_entry("checkpoint_restore")) == "publish"
        assert phase_for_entry(_tool_entry("checkpoint_list")) == "publish"
        assert phase_for_entry(_boundary_entry("merge_base_fetch")) == "publish"
        assert phase_for_entry(_boundary_entry("merge_complete")) == "publish"
        assert phase_for_entry(_boundary_entry("merge_abort")) == "publish"

    def test_other_phase(self):
        assert phase_for_entry(_entry("exec")) == "other"
        assert phase_for_entry(_entry("stop")) == "other"
        assert phase_for_entry(_tool_entry("run_python")) == "other"
        assert phase_for_entry(_tool_entry("sandbox_exec_check")) == "other"
        assert phase_for_entry(_entry("test_environment")) == "other"

    def test_session_management_phases(self):
        """Session (re)establishment → init; publish-flow guards → publish."""
        assert phase_for_entry(_tool_entry("sandbox_attach")) == "init"
        assert phase_for_entry(_boundary_entry("run_container_and_exec")) == "init"
        assert phase_for_entry(_tool_entry("secret_scan_override")) == "publish"
        assert phase_for_entry(_boundary_entry("secret_scan_override")) == "publish"


# ── Aggregation detail tests ────────────────────────────────────────────────


class TestAggregationDetails:
    """Test that the aggregation captures the right details."""

    def test_repo_from_clone_boundary(self):
        """The repo slug is extracted from the boundary:clone_repo details."""
        entries = [
            _boundary_entry(
                "clone_repo",
                details="repo=owner/name dest=/workspace proxy_read_grant=True",
                ts="2026-01-01T00:00:01Z",
            ),
        ]
        state = aggregate_run_phases(None, entries)
        assert state["test-run"]["repo"] == "owner/name"

    def test_repo_default_is_none(self):
        entries = [_entry("initialize", image="python:3.12", ts="2026-01-01T00:00:01Z")]
        state = aggregate_run_phases(None, entries)
        assert state["test-run"]["repo"] is None

    def test_touched_files_from_write_file(self):
        entries = [
            _entry("write_file", file_name="foo.py", dest_dir="/workspace/src",
                   ts="2026-01-01T00:00:01Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert "src/foo.py" in run["touched_files"] or "/workspace/src/foo.py" in run["touched_files"]

    def test_touched_files_from_edit_tool(self):
        entries = [
            _tool_entry("edit_file", params={"file_path": "src/bar.py"},
                        ts="2026-01-01T00:00:01Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert "src/bar.py" in run["touched_files"]

    def test_touched_files_deduplicated(self):
        entries = [
            _tool_entry("edit_file", params={"file_path": "src/foo.py"},
                        ts="2026-01-01T00:00:01Z"),
            _tool_entry("edit_file", params={"file_path": "src/foo.py"},
                        ts="2026-01-01T00:00:02Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        # Should only appear once
        assert run["touched_files"].count("src/foo.py") == 1

    def test_phase_ordering_preserved(self):
        """Phases appear in journal-chronological order."""
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:01Z"),
            _tool_entry("read_file_range", ts="2026-01-01T00:00:02Z"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:03Z"),
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:04Z"),
            _boundary_entry("publish", ts="2026-01-01T00:00:05Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        phase_names = [seg["phase"] for seg in run["phases"]]
        assert phase_names == ["init", "explore", "edit", "verify", "publish"]

    def test_consecutive_same_phase_merged(self):
        """Two edit operations in a row should produce one edit segment."""
        entries = [
            _tool_entry("edit_file", params={"file_path": "a.py"},
                        ts="2026-01-01T00:00:01Z"),
            _tool_entry("edit_file", params={"file_path": "b.py"},
                        ts="2026-01-01T00:00:02Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert len(run["phases"]) == 1
        assert run["phases"][0]["phase"] == "edit"
        assert run["phases"][0]["op_count"] == 2

    def test_edit_verify_roundtrip_detected(self):
        """edit → verify-fail → edit should increment roundtrip count."""
        entries = [
            _tool_entry("edit_file", params={"file_path": "a.py"},
                        ts="2026-01-01T00:00:01Z"),
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:02Z"),
            _entry("exec", commands=["pytest tests/"], exit_code=1,
                   ts="2026-01-01T00:00:03Z"),
            _tool_entry("edit_file", params={"file_path": "a.py"},
                        ts="2026-01-01T00:00:04Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["edit_verify_roundtrips"] >= 1

    def test_verify_pass_no_roundtrip(self):
        """edit → verify-pass → edit should NOT increment roundtrip count."""
        entries = [
            _tool_entry("edit_file", params={"file_path": "a.py"},
                        ts="2026-01-01T00:00:01Z"),
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:02Z"),
            _entry("exec", commands=["pytest tests/"], exit_code=0,
                   ts="2026-01-01T00:00:03Z"),
            _tool_entry("edit_file", params={"file_path": "a.py"},
                        ts="2026-01-01T00:00:04Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["edit_verify_roundtrips"] == 0

    def test_verify_timeline_tracks_pytest(self):
        """Pytest exec entries should appear in verify_timeline."""
        entries = [
            _entry("exec", commands=["pytest tests/"], exit_code=0,
                   ts="2026-01-01T00:00:01Z"),
            _entry("exec", commands=["pytest tests/test_x.py"], exit_code=1,
                   ts="2026-01-01T00:00:02Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        pytest_entries = [
            v for v in run["verify_timeline"] if v["type"] == "pytest_run"
        ]
        assert len(pytest_entries) == 2
        assert pytest_entries[0]["passed"] is True
        assert pytest_entries[1]["passed"] is False

    def test_multiple_runs(self):
        """Entries for different run_ids are separated."""
        entries = [
            _entry("initialize", run_id="run-a", ts="2026-01-01T00:00:01Z"),
            _tool_entry("edit_file", run_id="run-a", ts="2026-01-01T00:00:02Z"),
            _entry("initialize", run_id="run-b", ts="2026-01-01T00:00:03Z"),
            _tool_entry("read_file_range", run_id="run-b", ts="2026-01-01T00:00:04Z"),
        ]
        state = aggregate_run_phases(None, entries)
        assert "run-a" in state
        assert "run-b" in state
        assert state["run-a"]["phases"][0]["phase"] == "init"
        assert state["run-b"]["phases"][0]["phase"] == "init"


# ── Failure / outcome signal helpers (Issue #777) ─────────────────


class TestFailureSignals:
    """Unit tests for _is_outcome_entry and _entry_failed."""

    def test_is_outcome_entry(self):
        """Outcome entries: tool_use with a ``result``, and exec completions.

        Issue #789: exec journals a START entry (no ``exit_code``) before
        running and the completion (``exit_code`` present) after; only the
        completion is an outcome.
        """
        assert _is_outcome_entry(
            _tool_entry("verify_in_container",
                        params={"result": {"gate_passed": True}})
        )
        assert not _is_outcome_entry(
            _tool_entry("verify_in_container", params={"path": "tests/"})
        )
        assert _is_outcome_entry(
            _entry("exec", commands=["pytest"], exit_code=0)
        )
        assert not _is_outcome_entry(
            _entry("exec", commands=["pytest"])  # start entry, no outcome
        )
        # Background-exec dispatch sentinel: -1 means "launched, outcome
        # not yet known" -- not an outcome (Issue #789 leaves its
        # semantics unchanged).
        assert not _is_outcome_entry(
            _entry("exec", commands=["pytest"], exit_code=-1)
        )
        assert not _is_outcome_entry(
            _tool_entry("verify_in_container")  # no params at all
        )

    def test_entry_failed_exec(self):
        """exec fails when exit_code is nonzero; None counts as 0."""
        assert _entry_failed(_entry("exec", commands=["pytest"], exit_code=1)) is True
        assert _entry_failed(_entry("exec", commands=["pytest"], exit_code=0)) is False
        assert _entry_failed(_entry("exec", commands=["pytest"])) is False

    def test_entry_failed_tool_outcome(self):
        """tool_use outcome entries fail when gate_passed is False."""
        assert _entry_failed(
            _tool_entry("verify_in_container",
                        params={"result": {"gate_passed": False}})
        ) is True
        assert _entry_failed(
            _tool_entry("verify_in_container",
                        params={"result": {"gate_passed": True}})
        ) is False
        # A plain call entry (no result) carries no failure signal.
        assert _entry_failed(
            _tool_entry("edit_file", params={"file_path": "a.py"})
        ) is False

    def test_entry_failed_other_ops(self):
        """Operations without an outcome never count as failed."""
        assert _entry_failed(_entry("initialize", image="python:3.12")) is False
        assert _entry_failed(_boundary_entry("publish")) is False
        assert _entry_failed(_entry("stop")) is False


# ── Op metrics / failure-recovery state (Issue #777) ───────────────


class TestOpMetrics:
    """Aggregation of op_calls / op_failures / failure_recovery."""

    def test_calls_and_failures_counted(self):
        # Issue #789: each exec journals a start (no exit_code -- the call)
        # followed by the completion (the outcome); calls must not
        # double-count, so two execs are two calls, one failed.
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:01Z", image="python:3.12"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:02Z"),
            _entry("exec", commands=["pytest"], ts="2026-01-01T00:00:03Z"),
            _entry("exec", commands=["pytest"], exit_code=1, ts="2026-01-01T00:00:04Z"),
            _entry("exec", commands=["pytest"], ts="2026-01-01T00:00:05Z"),
            _entry("exec", commands=["pytest"], exit_code=0, ts="2026-01-01T00:00:06Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["op_calls"]["tool:edit_file"] == 1
        assert run["op_calls"]["exec"] == 2
        assert run["op_failures"]["exec"] == 1
        assert run["pending_failure_ops"] == []

    def test_outcome_entry_not_counted_as_call_but_counts_as_failure(self):
        """Verify outcome entries are not calls, but failures signal them."""
        entries = [
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:01Z"),
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:02Z",
                        params={"result": {"gate_passed": False}}),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["op_calls"]["tool:verify_in_container"] == 1
        assert run["op_failures"]["tool:verify_in_container"] == 1

    def test_verify_outcome_failure_recovery_recorded(self):
        """A failed verify outcome is queued, so the next call is its recovery."""
        entries = [
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:01Z"),
            _tool_entry("verify_in_container", ts="2026-01-01T00:00:02Z",
                        params={"result": {"gate_passed": False}}),
            _tool_entry("edit_file", ts="2026-01-01T00:00:03Z",
                        params={"file_path": "a.py"}),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        rec = run["failure_recovery"]
        assert rec.get("tool:verify_in_container", {}).get("tool:edit_file") == 1
        assert run["pending_failure_ops"] == []

    def test_consecutive_failures_each_get_recovery(self):
        """exec → exec-fail → edit: the second exec is the first failure's
        immediately-following action, edit recovers the second."""
        # Issue #789: each exec journals a start (the call) then the
        # completion (the outcome); the second exec's start is the first
        # failure's immediately-following action.
        entries = [
            _entry("exec", commands=["a"], ts="2026-01-01T00:00:01Z"),
            _entry("exec", commands=["a"], exit_code=1, ts="2026-01-01T00:00:02Z"),
            _entry("exec", commands=["b"], ts="2026-01-01T00:00:03Z"),
            _entry("exec", commands=["b"], exit_code=1, ts="2026-01-01T00:00:04Z"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:05Z",
                        params={"file_path": "a.py"}),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["op_calls"]["exec"] == 2
        assert run["op_failures"]["exec"] == 2
        rec = run["failure_recovery"]["exec"]
        assert rec.get("exec") == 1
        assert rec.get("tool:edit_file") == 1
        assert run["pending_failure_ops"] == []

    def test_background_dispatch_counts_as_call_and_failure(self):
        """The background-exec dispatch sentinel (exit_code=-1) keeps its
        pre-#789 counting: one call, one failure (its -1 semantics are
        relied upon elsewhere and are left unchanged)."""
        entries = [
            _entry("exec", commands=["long job"], exit_code=-1,
                   verbose="background", ts="2026-01-01T00:00:01Z"),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        assert run["op_calls"]["exec"] == 1
        assert run["op_failures"]["exec"] == 1
        assert run["pending_failure_ops"] == ["exec"]

    def test_failure_then_passing_exec_then_edit(self):
        """A passing exec recovers the failure; the later edit is not
        double-counted as a recovery."""
        # Issue #789: each exec journals a start (the call) then the
        # completion (the outcome); the passing exec's start is the
        # failure's immediately-following action.
        entries = [
            _entry("exec", commands=["bad"], ts="2026-01-01T00:00:01Z"),
            _entry("exec", commands=["bad"], exit_code=1, ts="2026-01-01T00:00:02Z"),
            _entry("exec", commands=["good"], ts="2026-01-01T00:00:03Z"),
            _entry("exec", commands=["good"], exit_code=0, ts="2026-01-01T00:00:04Z"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:05Z",
                        params={"file_path": "a.py"}),
        ]
        state = aggregate_run_phases(None, entries)
        run = state["test-run"]
        rec = run["failure_recovery"]["exec"]
        assert rec.get("exec") == 1  # the passing exec call
        assert rec.get("tool:edit_file", 0) == 0


# ---------------------------------------------------------------------------
# Health markers (Issue #775)
# ---------------------------------------------------------------------------


class TestHealthMarkers:
    """The aggregation state carries the markers the health classifier needs:
    ``published``, ``stopped`` and ``last_op``."""

    def test_defaults(self):
        state = aggregate_run_phases(None, [_entry("initialize", image="python:3.12")])
        run = state["test-run"]
        assert run["published"] is False
        assert run["stopped"] is False
        assert run["last_op"] == "initialize"

    def test_published_set_by_publish_boundary(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:01Z"),
            _boundary_entry("publish", ts="2026-01-01T00:00:02Z"),
        ]
        run = aggregate_run_phases(None, entries)["test-run"]
        assert run["published"] is True
        assert run["stopped"] is False

    def test_stopped_set_by_stop_entry(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:01Z"),
            _entry("stop", ts="2026-01-01T00:00:02Z"),
        ]
        run = aggregate_run_phases(None, entries)["test-run"]
        assert run["stopped"] is True
        assert run["published"] is False

    def test_last_op_tracks_most_recent_entry(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:01Z"),
            _tool_entry("read_file_range", ts="2026-01-01T00:00:02Z"),
            _tool_entry("edit_file", ts="2026-01-01T00:00:03Z"),
        ]
        run = aggregate_run_phases(None, entries)["test-run"]
        assert run["last_op"] == "tool:edit_file"

    def test_markers_survive_incremental_chunking(self):
        entries = [
            _entry("initialize", ts="2026-01-01T00:00:01Z"),
            _boundary_entry("publish", ts="2026-01-01T00:00:02Z"),
            _entry("stop", ts="2026-01-01T00:00:03Z"),
        ]
        full = aggregate_run_phases(None, entries)["test-run"]
        inc = aggregate_run_phases(
            aggregate_run_phases(None, entries[:1]), entries[1:]
        )["test-run"]
        assert full["published"] is True and inc["published"] is True
        assert full["stopped"] is True and inc["stopped"] is True
        assert full["last_op"] == "stop" and inc["last_op"] == "stop"
