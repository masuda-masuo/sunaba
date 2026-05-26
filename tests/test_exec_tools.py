"""Tests for the exec MCP tools (sandbox_exec / sandbox_exec_background / sandbox_exec_check)."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.server import (
    _CONTAINER_LOG_PATH,
    _TERMINAL,
    _exec_run,
    _jobs,
    _jobs_lock,
    _open_terminal_with_logs,
    sandbox_exec,
    sandbox_exec_check,
)


class TestSandboxExecTerminal:
    """Tests for sandbox_exec terminal window behavior."""

    def setup_method(self) -> None:
        with _jobs_lock:
            _jobs.clear()

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._open_terminal_with_logs")
    def test_terminal_note_included_when_terminal_set(
        self,
        mock_open_terminal: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When _TERMINAL is set, sandbox_exec returns a terminal notification."""
        # Setup mock container
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, b"hello world")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Set _TERMINAL
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            result = sandbox_exec(
                container_id="abc123def456",
                commands=["echo hello"],
            )
            assert "A terminal window has been opened" in result
            assert _CONTAINER_LOG_PATH in result
            mock_open_terminal.assert_called_once_with("abc123def456")
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._open_terminal_with_logs")
    def test_terminal_note_omitted_when_terminal_none(
        self,
        mock_open_terminal: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When _TERMINAL is None, sandbox_exec does NOT include terminal notification."""
        # Setup mock container
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, b"hello world")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Ensure _TERMINAL is None
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = None

        try:
            result = sandbox_exec(
                container_id="abc123def456",
                commands=["echo hello"],
            )
            assert "A terminal window has been opened" not in result
            mock_open_terminal.assert_not_called()
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._open_terminal_with_logs")
    def test_container_not_found_with_terminal(
        self,
        mock_open_terminal: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When container not found, error returned and _open_terminal_with_logs not called."""
        from docker.errors import NotFound

        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            result = sandbox_exec(
                container_id="abc123def456",
                commands=["echo hello"],
            )
            assert "Error" in result
            assert "not found" in result
            # Terminal should NOT be opened when container doesn't exist
            mock_open_terminal.assert_not_called()
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._open_terminal_with_logs")
    def test_terminal_with_multiple_commands(
        self,
        mock_open_terminal: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Terminal notification is included even with multiple commands."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, b"output")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            result = sandbox_exec(
                container_id="abc123def456",
                commands=["echo first", "echo second"],
            )
            assert "$ echo first" in result
            assert "$ echo second" in result
            assert "A terminal window has been opened" in result
            mock_open_terminal.assert_called_once_with("abc123def456")
        finally:
            server._TERMINAL = original_terminal


class TestSandboxExecCheck:
    """Tests for sandbox_exec_check()."""

    def setup_method(self) -> None:
        with _jobs_lock:
            _jobs.clear()

    def test_job_not_found(self) -> None:
        result = sandbox_exec_check("nonexistent-container", "nonexistent")
        assert "not found" in result

    def test_job_running_default_no_partial(self) -> None:
        """Default show_partial=False should not include partial output."""
        with _jobs_lock:
            _jobs["exec-job-1"] = {
                "status": "running",
                "started_at": time.time() - 10,
                "output": "$ git clone\nCloning into...",
            }
        result = sandbox_exec_check("c1", "exec-job-1")
        assert "Status: running" in result
        assert "elapsed" in result
        assert "partial output" not in result
        assert "git clone" not in result

    def test_job_running_with_partial(self) -> None:
        """show_partial=True should include partial output."""
        with _jobs_lock:
            _jobs["exec-job-2"] = {
                "status": "running",
                "started_at": time.time() - 5,
                "output": "$ pip install\nCollecting...",
            }
        result = sandbox_exec_check("c2", "exec-job-2", show_partial=True)
        assert "Status: running" in result
        assert "--- partial output ---" in result
        assert "pip install" in result

    def test_job_done(self) -> None:
        """Done status should always include full output regardless of show_partial."""
        with _jobs_lock:
            _jobs["exec-job-3"] = {
                "status": "done",
                "started_at": time.time() - 10,
                "finished_at": time.time(),
                "elapsed": 10.0,
                "output": "$ echo hello\nhello",
            }
        result = sandbox_exec_check("c3", "exec-job-3")
        assert "done" in result
        assert "echo hello" in result

    def test_job_error(self) -> None:
        """Error status should always include error message."""
        with _jobs_lock:
            _jobs["exec-job-4"] = {
                "status": "error",
                "started_at": time.time() - 2,
                "finished_at": time.time(),
                "elapsed": 2.0,
                "error": "command not found",
            }
        result = sandbox_exec_check("c4", "exec-job-4")
        assert "error" in result.lower()
        assert "command not found" in result

    def test_backward_compatible(self) -> None:
        """Calling without show_partial should work (backward compatibility)."""
        with _jobs_lock:
            _jobs["exec-job-5"] = {
                "status": "done",
                "started_at": time.time() - 3,
                "finished_at": time.time(),
                "elapsed": 3.0,
                "output": "done",
            }
        # No show_partial argument - should still work
        result = sandbox_exec_check("c5", "exec-job-5")
        assert "done" in result
