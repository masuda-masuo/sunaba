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
    _forget_terminal,
    _jobs,
    _jobs_lock,
    _open_terminal_with_logs,
    _terminals_lock,
    _terminals_opened,
    sandbox_exec,
    sandbox_exec_check,
)


class TestSandboxExecTerminal:
    """Tests for sandbox_exec terminal window behavior."""

    def setup_method(self) -> None:
        with _jobs_lock:
            _jobs.clear()
        with _terminals_lock:
            _terminals_opened.clear()

    @patch("code_sandbox_mcp.server._docker")
    def test_terminal_note_included_when_terminal_set(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """When _TERMINAL is set, sandbox_exec returns a terminal notification."""
        # Setup mock container - exec_run returns (0, output_bytes) for all calls
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
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    def test_terminal_note_omitted_when_terminal_none(
        self,
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
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    def test_container_not_found_with_terminal(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """When container not found, error returned without terminal notification."""
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
            assert "A terminal window has been opened" not in result
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    def test_terminal_with_multiple_commands(
        self,
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
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server._docker")
    def test_command_failure_with_terminal(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Terminal notification is included even when a command fails.

        sandbox_exec now calls exec_run multiple times per command:
          1. truncate -s 0 /tmp/mcp.log
          2. echo '$ echo first' >> /tmp/mcp.log   (header for cmd 1)
          3. (echo first) 2>&1 | tee -a /tmp/mcp.log (cmd 1)
          4. echo '$ false' >> /tmp/mcp.log         (header for cmd 2)
          5. (false) 2>&1 | tee -a /tmp/mcp.log     (cmd 2, fails)
          6. echo "Command exited with code 1" >> /tmp/mcp.log (error msg)
        """
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = [
            (0, b""),            # 1. truncate
            (0, b""),            # 2. header 1
            (0, b"first output"),  # 3. cmd 1 success
            (0, b""),            # 4. header 2
            (1, b"error msg"),   # 5. cmd 2 fails
            (0, b""),            # 6. error msg to log
        ]
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            result = sandbox_exec(
                container_id="abc123def456",
                commands=["echo first", "false"],
            )
            assert "$ echo first" in result
            assert "Command exited with code 1" in result
            assert "A terminal window has been opened" in result
            # Second command should NOT appear in output (stopped at failure)
            assert "$ false" in result
        finally:
            server._TERMINAL = original_terminal


class TestSandboxExecCheck:
    """Tests for sandbox_exec_check()."""

    def setup_method(self) -> None:
        with _jobs_lock:
            _jobs.clear()
        with _terminals_lock:
            _terminals_opened.clear()

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


class TestOpenTerminalWithLogs:
    """Tests for _open_terminal_with_logs deduplication."""

    def setup_method(self) -> None:
        with _terminals_lock:
            _terminals_opened.clear()

    @patch("code_sandbox_mcp.server.subprocess.Popen")
    def test_first_call_opens_terminal(
        self,
        mock_popen: MagicMock,
    ) -> None:
        """First call for a container_id should open a terminal window."""
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            _open_terminal_with_logs("abc123def456")
            mock_popen.assert_called_once()
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server.subprocess.Popen")
    def test_second_call_skips_terminal(
        self,
        mock_popen: MagicMock,
    ) -> None:
        """Second call for the same container_id should NOT open a new terminal."""
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            _open_terminal_with_logs("abc123def456")
            _open_terminal_with_logs("abc123def456")
            # Popen should have been called only once
            mock_popen.assert_called_once()
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server.subprocess.Popen")
    def test_different_containers_open_separate_windows(
        self,
        mock_popen: MagicMock,
    ) -> None:
        """Different container_ids should each open their own terminal."""
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            _open_terminal_with_logs("abc123def456")
            _open_terminal_with_logs("xyz789ghi012")
            assert mock_popen.call_count == 2
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server.subprocess.Popen")
    def test_skip_when_terminal_none(
        self,
        mock_popen: MagicMock,
    ) -> None:
        """When _TERMINAL is None, no terminal is opened even on first call."""
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = None

        try:
            _open_terminal_with_logs("abc123def456")
            mock_popen.assert_not_called()
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server.subprocess.Popen")
    def test_forget_terminal_allows_reopen(
        self,
        mock_popen: MagicMock,
    ) -> None:
        """After _forget_terminal(), the same container_id can open a new window."""
        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        server._TERMINAL = "xterm"

        try:
            _open_terminal_with_logs("abc123def456")
            _forget_terminal("abc123def456")
            _open_terminal_with_logs("abc123def456")
            assert mock_popen.call_count == 2
        finally:
            server._TERMINAL = original_terminal

    @patch("code_sandbox_mcp.server.subprocess.Popen")
    def test_sandbox_exec_multiple_calls_reuse_terminal(
        self,
        mock_popen: MagicMock,
    ) -> None:
        """Multiple sandbox_exec calls for the same container should open only one terminal.

        This integration-style test verifies that the full tool path
        respects the deduplication.
        """
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, b"output")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container

        import code_sandbox_mcp.server as server
        original_terminal = server._TERMINAL
        original_docker = server._docker
        server._TERMINAL = "xterm"
        server._docker = lambda: mock_client

        try:
            # First call should open terminal
            sandbox_exec("abc123def456", ["echo hello"])
            first_call_count = mock_popen.call_count

            # Second call should NOT open new terminal
            sandbox_exec("abc123def456", ["echo world"])
            assert mock_popen.call_count == first_call_count
        finally:
            server._TERMINAL = original_terminal
            server._docker = original_docker
