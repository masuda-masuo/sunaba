"""Tests for the append-only execution journal (Issue #44)."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from code_sandbox_mcp.journal import (
    generate_run_id,
    get_journal_path,
    get_or_create_run_id,
    get_runs,
    read_container_states,
    read_journal,
    record_boundary_crossing,
    record_copy,
    record_exec,
    record_file_write,
    record_initialize,
    record_initialize_complete,
    record_stop,
    remove_run_id,
)


class TestJournalWrite:
    """Tests for journal write operations."""

    def test_generate_run_id_is_unique(self) -> None:
        ids = {generate_run_id() for _ in range(100)}
        assert len(ids) == 100

    def test_get_or_create_run_id_returns_same_for_same_container(self) -> None:
        rid1 = get_or_create_run_id("abc123")
        rid2 = get_or_create_run_id("abc123")
        assert rid1 == rid2

    def test_get_or_create_run_id_different_containers(self) -> None:
        rid1 = get_or_create_run_id("abc123")
        rid2 = get_or_create_run_id("def456")
        assert rid1 != rid2

    def test_remove_run_id(self) -> None:
        get_or_create_run_id("abc123")
        remove_run_id("abc123")
        # After removal, a new run_id should be generated
        rid_new = get_or_create_run_id("abc123")
        rid_another = get_or_create_run_id("abc123")
        assert rid_new == rid_another

    def test_record_initialize_creates_entry(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        # Temporarily override the journal path
        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_initialize("abc123", "python@sha256:abcd", allow_network=True)

        entries = _read_log(log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["operation"] == "initialize"
        assert e["container_id"] == "abc123"
        assert e["image"] == "python@sha256:abcd"
        assert e["allow_network"] is True

    def test_record_exec_creates_entry(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["echo hello"], exit_code=0, verbose="summary")

        entries = _read_log(log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["operation"] == "exec"
        assert e["container_id"] == "abc123"
        assert e["commands"] == ["echo hello"]
        assert e["exit_code"] == 0
        assert e["boundary_crossing"] is False

    def test_record_exec_with_failure(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["false"], exit_code=1)

        entries = _read_log(log_path)
        assert entries[0]["exit_code"] == 1

    def test_record_stop_creates_entry(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_initialize("abc123", "python@sha256:abcd")
            record_exec("abc123", ["echo hello"], exit_code=0)
            record_stop("abc123")

        entries = _read_log(log_path)
        assert len(entries) == 3
        assert entries[2]["operation"] == "stop"
        assert entries[2]["container_id"] == "abc123"

    def test_record_boundary_crossing(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing("abc123", "git_push", "pushed to main", approved=True)

        entries = _read_log(log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["operation"] == "boundary_crossing"
        assert e["sub_operation"] == "git_push"
        assert e["approved"] is True

    def test_record_boundary_crossing_no_approval(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_boundary_crossing("abc123", "gh_issue_view", "read issue #1", approved=None)

        entries = _read_log(log_path)
        assert entries[0]["approved"] is None

    def test_record_file_write(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_file_write("abc123", "test.py", "/root", byte_count=42)

        entries = _read_log(log_path)
        assert entries[0]["operation"] == "write_file"
        assert entries[0]["file_name"] == "test.py"
        assert entries[0]["byte_count"] == 42
        assert entries[0]["is_test"] is False

    def test_record_file_write_is_test(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_file_write("abc123", "test_foo.py", "/tests", byte_count=42, is_test=True)

        entries = _read_log(log_path)
        assert entries[0]["operation"] == "write_file"
        assert entries[0]["is_test"] is True

    def test_record_copy_project(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_copy("abc123", "copy_project", "/src/myproject", "/root/myproject")

        entries = _read_log(log_path)
        assert entries[0]["operation"] == "copy_project"

    def test_record_copy_file(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_copy("abc123", "copy_file", "/src/file.txt", "/root/file.txt")

        entries = _read_log(log_path)
        assert entries[0]["operation"] == "copy_file"

    def test_full_lifecycle_journal(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_initialize("abc123", "python@sha256:abcd", allow_network=True)
            record_exec("abc123", ["echo hello"], exit_code=0)
            record_file_write("abc123", "test.py", "/root", 100)
            record_boundary_crossing("abc123", "git_push", "pushed", approved=True)
            record_exec("abc123", ["pytest"], exit_code=1)
            record_stop("abc123")

        entries = _read_log(log_path)
        assert len(entries) == 6
        ops = [e["operation"] for e in entries]
        assert ops == ["initialize", "exec", "write_file", "boundary_crossing", "exec", "stop"]
        assert entries[2]["is_test"] is False


class TestJournalRead:
    """Tests for journal reading operations."""

    def test_read_empty_journal(self, tmp_path: Path):
        log_path = tmp_path / "nonexistent" / "journal.log"
        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path):
            entries = read_journal()
            assert entries == []

    def test_read_journal_all(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_initialize("abc123", "python@sha256:abcd")
            record_exec("abc123", ["echo hello"], exit_code=0)
            entries = read_journal()
            assert len(entries) == 2

    def test_read_journal_filtered_by_run_id(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_initialize("abc123", "python@sha256:abcd")
            entries_all = read_journal()
            run_id = entries_all[0]["run_id"]
            record_exec("abc123", ["echo hello"], exit_code=0)
            record_initialize("def456", "python@sha256:abcd")
            record_exec("def456", ["echo hello"], exit_code=0)
            entries_a = read_journal(run_id=run_id)
            assert len(entries_a) == 2
            assert all(e["run_id"] == run_id for e in entries_a)


class TestGetRuns:
    """Tests for get_runs summary."""

    def test_get_runs_empty(self, tmp_path: Path):
        log_path = tmp_path / "nonexistent" / "journal.log"
        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path):
            runs = get_runs()
            assert runs == []

    def test_get_runs_with_stopped_container(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir), \
             patch("code_sandbox_mcp.journal._run_map", {}), \
             patch("code_sandbox_mcp.journal._run_map_lock", type(journal_dir)):
            # Directly write entries to avoid run_map interference
            _append_json_test(log_path, {"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "container_id": "abc", "operation": "initialize", "image": "test"})
            _append_json_test(log_path, {"ts": "2026-01-01T00:00:01Z", "run_id": "run1", "container_id": "abc", "operation": "exec", "exit_code": 0})
            _append_json_test(log_path, {"ts": "2026-01-01T00:00:02Z", "run_id": "run1", "container_id": "abc", "operation": "stop"})

            runs = get_runs()
            assert len(runs) == 1
            assert runs[0]["run_id"] == "run1"
            assert runs[0]["status"] == "stopped"
            assert runs[0]["operations"] == 3

    def test_get_runs_with_boundary_crossing(self, tmp_path: Path):
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        _append_json_test(log_path, {"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "container_id": "abc", "operation": "initialize", "image": "test"})
        _append_json_test(log_path, {"ts": "2026-01-01T00:00:01Z", "run_id": "run1", "container_id": "abc", "operation": "boundary_crossing", "sub_operation": "git_push"})

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            runs = get_runs()
            assert runs[0]["boundary_crossings"] == 1


class TestJournalPath:
    """Tests for journal path helpers."""

    def test_get_journal_path(self) -> None:
        path = get_journal_path()
        assert path.endswith("journal.log")
        assert ".code-sandbox-mcp" in path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_log(path: Path) -> list[dict]:
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _append_json_test(path: Path, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class TestRecordToolUse:
    """Tests for record_tool_use (Issue #359 tier 3+4)."""

    def test_record_tool_use_creates_entry(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.journal import record_tool_use

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_tool_use(
                "abc123def456",
                "read_file_range",
                {"file_path": "/tmp/repo/repo/foo.py"},
            )

        entries = _read_log(log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["operation"] == "tool_use"
        assert e["tool_name"] == "read_file_range"
        assert e["container_id"] == "abc123def456"
        assert e["params"] == {"file_path": "/tmp/repo/repo/foo.py"}

    def test_record_tool_use_without_params(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.journal import record_tool_use

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_tool_use("abc123def456", "search_in_container")

        entries = _read_log(log_path)
        assert len(entries) == 1
        e = entries[0]
        assert e["operation"] == "tool_use"
        assert e["tool_name"] == "search_in_container"
        assert "params" not in e

    def test_get_tool_usage_counts_tool_use_entries(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.journal import get_tool_usage, record_tool_use

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_tool_use("abc123def456", "read_file_range")
            record_tool_use("abc123def456", "list_files")
            record_tool_use("abc123def456", "search_in_container")
            record_tool_use("abc123def456", "lint_in_container")
            record_tool_use("abc123def456", "type_check_in_container")
            record_tool_use("abc123def456", "verify_in_container")

            usage = get_tool_usage()

        structured = usage["structured_ops"]
        assert structured.get("read_file_range") == 1
        assert structured.get("list_files") == 1
        assert structured.get("search_in_container") == 1
        assert structured.get("lint_in_container") == 1
        assert structured.get("type_check_in_container") == 1
        assert structured.get("verify_in_container") == 1
        assert usage["total_ops"] == 6
        assert usage["exec_ops"] == 0

    def test_end_to_end_tool_use_with_exec_entries(self, tmp_path: Path) -> None:
        """Integration test: tool_use entries coexist with exec entries in get_tool_usage()."""
        from code_sandbox_mcp.journal import (
            get_or_create_run_id,
            get_tool_usage,
            record_exec,
            record_tool_use,
        )

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            get_or_create_run_id("cid-e2e")
            record_exec("cid-e2e", ["ls"], 0)
            record_exec("cid-e2e", ["cat foo"], 0)
            record_tool_use("cid-e2e", "read_file_range", {"file_path": "foo"})
            record_tool_use("cid-e2e", "lint_in_container", {"file_path": "bar.py"})

            usage = get_tool_usage()

            assert usage["exec_ops"] == 2
            assert usage["structured_ops"]["read_file_range"] == 1
            assert usage["structured_ops"]["lint_in_container"] == 1
            assert usage["total_ops"] >= 4
            # verify_in_container is NOT in this fixture, so it should not appear
            assert "verify_in_container" not in usage["structured_ops"]


class TestContainerStateSidecar:
    """Container state sidecar for O(1) status lookup (Issue #305)."""

    @contextmanager
    def _journal_at(self, tmp_path: Path) -> Iterator[Path]:
        journal_dir = tmp_path / "journal"
        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", journal_dir / "journal.log"), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir), \
             patch("code_sandbox_mcp.journal._state_synced", False):
            yield journal_dir

    def test_lifecycle_reflected_in_states(self, tmp_path: Path) -> None:
        with self._journal_at(tmp_path):
            record_initialize("aaa111", image="python:3.12")
            states = read_container_states()
            assert states["aaa111"]["complete"] is False
            assert states["aaa111"]["init_ts"]

            record_initialize_complete("aaa111")
            record_exec("aaa111", ["echo hi"], exit_code=0)
            # Second read takes the sidecar fast path (_state_synced armed).
            states = read_container_states()
            assert states["aaa111"]["complete"] is True
            assert states["aaa111"]["used"] is True

    def test_stop_prunes_entry(self, tmp_path: Path) -> None:
        with self._journal_at(tmp_path) as journal_dir:
            record_initialize("aaa111", image="python:3.12")
            record_initialize_complete("aaa111")
            record_stop("aaa111")
            assert "aaa111" not in read_container_states()
            # The sidecar file itself is pruned too, keeping it bounded by
            # active containers rather than all-time history.
            sidecar = json.loads((journal_dir / "container_state.json").read_text())
            assert "aaa111" not in sidecar

    def test_rebuild_from_journal_prunes_stopped(self, tmp_path: Path) -> None:
        with self._journal_at(tmp_path) as journal_dir:
            record_initialize("aaa111", image="python:3.12")
            record_stop("aaa111")
            record_initialize("bbb222", image="python:3.12")
            (journal_dir / "container_state.json").unlink()

            states = read_container_states()
            assert "aaa111" not in states
            assert states["bbb222"]["init_ts"]

    def test_sidecar_vanished_recovers_from_journal(self, tmp_path: Path) -> None:
        with self._journal_at(tmp_path) as journal_dir:
            record_initialize("aaa111", image="python:3.12")
            read_container_states()  # arm the fast path
            (journal_dir / "container_state.json").unlink()

            states = read_container_states()
            assert states["aaa111"]["init_ts"]
            assert (journal_dir / "container_state.json").exists()

    def test_first_read_rebuilds_despite_fresher_sidecar(self, tmp_path: Path) -> None:
        """A crash between journal append and sidecar update must not be masked.

        A later ``record_*`` makes the sidecar mtime fresher than the journal,
        so the mtime comparison alone would trust the stale sidecar forever.
        A crash implies a process restart, and the first read in each process
        re-syncs unconditionally from the journal.
        """
        with self._journal_at(tmp_path) as journal_dir:
            record_initialize("aaa111", image="python:3.12")
            record_initialize_complete("aaa111")
            read_container_states()  # arm the fast path

            # Simulate the lost update: complete=True reached the journal but
            # not the sidecar, and the sidecar mtime ended up fresher.
            sidecar = journal_dir / "container_state.json"
            sidecar.write_text(json.dumps({
                "aaa111": {"complete": False, "used": False,
                           "stopped": False, "init_ts": None},
            }))
            bump = (journal_dir / "journal.log").stat().st_mtime_ns + 10**9
            os.utime(sidecar, ns=(bump, bump))

            # Within the same process the fast path trusts the fresher sidecar…
            assert read_container_states()["aaa111"]["complete"] is False
            # …but a new process (crash implies restart) re-syncs from the journal.
            with patch("code_sandbox_mcp.journal._state_synced", False):
                assert read_container_states()["aaa111"]["complete"] is True
