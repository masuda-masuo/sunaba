"""Tests for the append-only execution journal (Issue #44)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from code_sandbox_mcp.journal import (
    generate_run_id,
    get_journal_path,
    get_or_create_run_id,
    get_runs,
    read_journal,
    record_boundary_crossing,
    record_copy,
    record_exec,
    record_file_write,
    record_initialize,
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
        assert e["inject_vcs_token"] is False

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
            record_initialize("abc123", "python@sha256:abcd", allow_network=True, inject_vcs_token=True)
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


class TestSandboxRejectRecording:
    """sandbox_reject must resolve the pending approval it rejects (Issue #359).

    Previously sandbox_reject removed the token from the store but never
    wrote a resolving journal entry, so the original ``approved=None``
    boundary crossing stayed unresolved and get_pending_approvals()
    reported the token as pending forever (asymmetry with sandbox_approve).
    """

    def test_reject_resolves_pending_approval(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.journal import get_pending_approvals
        from code_sandbox_mcp.token import generate_token
        from code_sandbox_mcp.tools.approval import sandbox_reject

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            token = generate_token("git_push", "push to main", "abc123def456", "run1")
            # The requesting tool records the pending crossing (approved=None).
            record_boundary_crossing(
                "abc123def456", "git_push", "push to main",
                approved=None, token=token,
            )
            assert token in {p["token"] for p in get_pending_approvals()}

            result = json.loads(sandbox_reject(token))
            assert result["status"] == "ok"

            # The pending entry must now be resolved (the Issue #359 bug).
            assert token not in {p.get("token") for p in get_pending_approvals()}
            rejects = [
                e for e in read_journal()
                if e.get("operation") == "boundary_crossing"
                and e.get("approved") is False
                and e.get("token") == token
            ]
            assert len(rejects) == 1
            assert rejects[0]["sub_operation"] == "git_push"

    def test_reject_unknown_token_records_nothing(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.tools.approval import sandbox_reject

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            result = json.loads(sandbox_reject("deadbeefdeadbeef"))
            assert result["status"] == "error"
            assert read_journal() == []


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

    def test_end_to_end_via_sandbox_cache_invalidate(self, tmp_path: Path) -> None:
        """Integration test: call a real tool and verify the journal entry."""
        from code_sandbox_mcp.journal import get_tool_usage, read_journal

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            from code_sandbox_mcp.server import sandbox_cache_invalidate
            result = json.loads(sandbox_cache_invalidate(key="e2e_test_key"))

            assert result == {"invalidated": 0}

            entries = read_journal()
            tool_entries = [e for e in entries if e.get("operation") == "tool_use"]
            assert len(tool_entries) == 1
            e = tool_entries[0]
            assert e["tool_name"] == "sandbox_cache_invalidate"
            assert e["container_id"] == "system"
            assert e["params"] == {"key": "e2e_test_key"}

            # Verify get_tool_usage() aggregates correctly
            usage = get_tool_usage()
            assert usage["structured_ops"].get("sandbox_cache_invalidate") == 1
            assert usage["total_ops"] >= 1

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
