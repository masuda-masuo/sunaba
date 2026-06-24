"""Tests for the trace module (Issue #44)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from code_sandbox_mcp.trace import (
    generate_html_trace,
    generate_json_trace,
    get_trace_dir,
)


class TestGenerateJsonTrace:
    """Tests for JSON trace generation."""

    def test_generate_empty_trace(self, tmp_path: Path):
        with patch("code_sandbox_mcp.trace._TRACE_DIR", tmp_path), \
             patch("code_sandbox_mcp.trace.read_journal", return_value=[]):
            result = generate_json_trace("nonexistent")
            assert result == ""

    def test_generate_json_trace(self, tmp_path: Path):
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "container_id": "abc", "operation": "initialize", "image": "python@sha256:abcd"},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run1", "container_id": "abc", "operation": "exec", "commands": ["echo hello"], "exit_code": 0},
            {"ts": "2026-01-01T00:00:02Z", "run_id": "run1", "container_id": "abc", "operation": "stop"},
        ]
        with patch("code_sandbox_mcp.trace._TRACE_DIR", tmp_path), \
             patch("code_sandbox_mcp.trace.read_journal", return_value=entries):
            result = generate_json_trace("run1")

        assert result.endswith("run1.json")
        trace_file = Path(result)
        assert trace_file.exists()
        data = json.loads(trace_file.read_text())
        assert data["run_id"] == "run1"
        assert data["total_operations"] == 3
        assert len(data["entries"]) == 3

    def test_generate_json_trace_with_boundary_crossings(self, tmp_path: Path):
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "operation": "initialize", "image": "python@sha256:abcd"},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run1", "operation": "boundary_crossing", "boundary_crossing": True, "sub_operation": "git_push"},
        ]
        with patch("code_sandbox_mcp.trace._TRACE_DIR", tmp_path), \
             patch("code_sandbox_mcp.trace.read_journal", return_value=entries):
            result = generate_json_trace("run1")

        data = json.loads(Path(result).read_text())
        assert data["boundary_crossings"] == 1


class TestGenerateHtmlTrace:
    """Tests for HTML trace generation."""

    def test_generate_empty_html(self, tmp_path: Path):
        with patch("code_sandbox_mcp.trace._TRACE_DIR", tmp_path), \
             patch("code_sandbox_mcp.trace.read_journal", return_value=[]):
            result = generate_html_trace("nonexistent")
            assert result == ""

    def test_generate_html_trace(self, tmp_path: Path):
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run1", "container_id": "abc", "operation": "initialize", "image": "python@sha256:abcd", "allow_network": False, "inject_vcs_token": False},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run1", "container_id": "abc", "operation": "exec", "commands": ["echo hello"], "exit_code": 0},
            {"ts": "2026-01-01T00:00:02Z", "run_id": "run1", "container_id": "abc", "operation": "exec", "commands": ["false"], "exit_code": 1},
        ]
        with patch("code_sandbox_mcp.trace._TRACE_DIR", tmp_path), \
             patch("code_sandbox_mcp.trace.read_journal", return_value=entries):
            result = generate_html_trace("run1")

        assert result.endswith("run1.html")
        html_content = Path(result).read_text()
        assert "run1" in html_content
        assert "echo hello" in html_content
        assert "Operations" in html_content


class TestGetTraceDir:
    """Tests for trace directory helper."""

    def test_get_trace_dir(self) -> None:
        trace_dir = get_trace_dir()
        assert ".code-sandbox-mcp" in trace_dir
        assert "traces" in trace_dir
