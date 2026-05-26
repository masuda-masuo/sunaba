"""Tests for the update MCP tools (sandbox_update_start / sandbox_update_check)."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from code_sandbox_mcp.server import (
    _UPDATE_SPEC,
    _jobs,
    _jobs_lock,
    _run_update_background,
    sandbox_exec_check,
    sandbox_update_check,
    sandbox_update_start,
)


class TestSandboxUpdateStart:
    """Tests for sandbox_update_start()."""

    def test_returns_job_id(self) -> None:
        result = sandbox_update_start()
        assert "Update job started:" in result
        assert "sandbox_update_check" in result

    def test_starts_background_thread(self) -> None:
        # Clear jobs
        with _jobs_lock:
            _jobs.clear()

        result = sandbox_update_start()
        # Extract job_id from result
        job_id_line = [line for line in result.split("\n") if "Update job started:" in line][0]
        job_id = job_id_line.split(":")[1].strip()

        # Give the thread a moment to start
        time.sleep(0.1)

        with _jobs_lock:
            assert job_id in _jobs
            assert _jobs[job_id]["status"] in ("running", "error")


class TestSandboxUpdateCheck:
    """Tests for sandbox_update_check()."""

    def setup_method(self) -> None:
        with _jobs_lock:
            _jobs.clear()

    def test_job_not_found(self) -> None:
        result = sandbox_update_check("nonexistent")
        assert "not found" in result

    def test_job_running(self) -> None:
        with _jobs_lock:
            _jobs["test-job-1"] = {
                "status": "running",
                "started_at": time.time(),
            }
        result = sandbox_update_check("test-job-1")
        assert "running" in result
        assert "elapsed" in result

    def test_job_done(self) -> None:
        with _jobs_lock:
            _jobs["test-job-2"] = {
                "status": "done",
                "started_at": time.time() - 5,
                "finished_at": time.time(),
                "elapsed": 5.0,
                "output": "Successfully installed package",
            }
        result = sandbox_update_check("test-job-2")
        assert "done" in result
        assert "Successfully installed package" in result

    def test_job_error(self) -> None:
        with _jobs_lock:
            _jobs["test-job-3"] = {
                "status": "error",
                "started_at": time.time() - 2,
                "finished_at": time.time(),
                "elapsed": 2.0,
                "error": "pip install failed",
            }
        result = sandbox_update_check("test-job-3")
        assert "error" in result.lower()
        assert "pip install failed" in result


class TestRunUpdateBackground:
    """Tests for the background update runner."""

    def setup_method(self) -> None:
        with _jobs_lock:
            _jobs.clear()

    @patch("code_sandbox_mcp.server.subprocess.run")
    def test_successful_update(self, mock_run) -> None:
        # Mock pip install success
        mock_result = type("Result", (), {"returncode": 0, "stdout": "Installed", "stderr": ""})()
        mock_run.return_value = mock_result

        job_id = "test-update-ok"

        # Run in a thread to allow sys.exit to be caught
        with pytest.raises(SystemExit) as exc_info:
            _run_update_background(job_id)

        assert exc_info.value.code == 42

        # Job should be marked as done
        with _jobs_lock:
            assert _jobs[job_id]["status"] == "done"
            assert _jobs[job_id]["output"] == "Installed"

    @patch("code_sandbox_mcp.server.subprocess.run")
    def test_failed_update(self, mock_run) -> None:
        # Mock pip install failure
        mock_result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": "ERROR: Could not install"})()
        mock_run.return_value = mock_result

        job_id = "test-update-fail"
        _run_update_background(job_id)

        with _jobs_lock:
            assert _jobs[job_id]["status"] == "error"
            assert "ERROR: Could not install" in _jobs[job_id]["error"]

    @patch("code_sandbox_mcp.server.subprocess.run")
    def test_update_exception(self, mock_run) -> None:
        # Mock an exception during pip install
        mock_run.side_effect = FileNotFoundError("pip not found")

        job_id = "test-update-exc"
        _run_update_background(job_id)

        with _jobs_lock:
            assert _jobs[job_id]["status"] == "error"
            assert "pip not found" in _jobs[job_id]["error"]


class TestUpdateSpecDefault:
    """Tests for the update spec default value."""

    def test_default_update_spec_is_dot(self) -> None:
        assert _UPDATE_SPEC == "."


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
