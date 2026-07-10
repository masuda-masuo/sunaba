"""Tests for journal/trace MCP tool handlers (tools/journal.py)."""

from __future__ import annotations

import json
from unittest.mock import patch

from sunaba.tools.journal import (
    sandbox_journal_path,
    sandbox_list_runs,
    sandbox_read_journal,
    sandbox_trace,
    sandbox_trace_dir,
)


class TestSandboxReadJournal:
    """Tests for sandbox_read_journal."""

    def test_returns_json_list(self) -> None:
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run1", "operation": "exec"},
        ]
        with patch("sunaba.tools.journal.read_journal", return_value=entries):
            result = sandbox_read_journal()
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["run_id"] == "run1"

    def test_filtered_by_run_id(self) -> None:
        entries = [{"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "operation": "initialize"}]
        with patch("sunaba.tools.journal.read_journal", return_value=entries):
            result = sandbox_read_journal(run_id="run1", max_entries=10)
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["run_id"] == "run1"

    def test_empty_journal(self) -> None:
        with patch("sunaba.tools.journal.read_journal", return_value=[]):
            result = sandbox_read_journal()
        assert json.loads(result) == []


class TestSandboxTrace:
    """Tests for sandbox_trace."""

    def test_json_format(self) -> None:
        with patch("sunaba.tools.journal.generate_json_trace", return_value="/tmp/trace/run1.json"):
            result = sandbox_trace("run1", output_format="json")
        assert result == "/tmp/trace/run1.json"

    def test_html_format(self) -> None:
        with patch("sunaba.tools.journal.generate_html_trace", return_value="/tmp/trace/run1.html"):
            result = sandbox_trace("run1", output_format="html")
        assert result == "/tmp/trace/run1.html"

    def test_invalid_format_returns_error(self) -> None:
        result = sandbox_trace("run1", output_format="xml")
        assert result.startswith("Error:")

    def test_not_found_returns_error(self) -> None:
        with patch("sunaba.tools.journal.generate_json_trace", return_value=""):
            result = sandbox_trace("nonexistent")
        assert result.startswith("Error:")

    def test_default_format_is_json(self) -> None:
        with patch("sunaba.tools.journal.generate_json_trace", return_value="/tmp/trace/run1.json"):
            result = sandbox_trace("run1")
        assert result == "/tmp/trace/run1.json"


class TestSandboxListRuns:
    """Tests for sandbox_list_runs."""

    def test_returns_json_list(self) -> None:
        runs = [
            {"run_id": "run1", "status": "running", "operations": 3},
            {"run_id": "run2", "status": "stopped", "operations": 5},
        ]
        with patch("sunaba.tools.journal.get_runs", return_value=runs):
            result = sandbox_list_runs()
        data = json.loads(result)
        assert len(data) == 2
        assert data[0]["run_id"] == "run1"

    def test_empty(self) -> None:
        with patch("sunaba.tools.journal.get_runs", return_value=[]):
            result = sandbox_list_runs()
        assert json.loads(result) == []


class TestSandboxJournalPath:
    """Tests for sandbox_journal_path."""

    def test_returns_path(self) -> None:
        with patch("sunaba.tools.journal.get_journal_path", return_value="/home/user/.sunaba/journal.log"):
            result = sandbox_journal_path()
        assert result == "/home/user/.sunaba/journal.log"
        assert "journal.log" in result


class TestSandboxTraceDir:
    """Tests for sandbox_trace_dir."""

    def test_returns_path(self) -> None:
        with patch("sunaba.tools.journal.get_trace_dir", return_value="/home/user/.sunaba/traces"):
            result = sandbox_trace_dir()
        assert result == "/home/user/.sunaba/traces"
        assert "traces" in result
