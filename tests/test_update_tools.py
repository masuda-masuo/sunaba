"""Tests for the update MCP tools (sandbox_update_start / sandbox_update_check)."""
from __future__ import annotations

import pytest

from code_sandbox_mcp.server import (
    _CURRENT_UPDATE_LOG_PATH,
    _UPDATE_SPEC,
    sandbox_update_check,
    sandbox_update_start,
)


@pytest.fixture(autouse=True)
def _reset_update_state():
    """Reset global update state before and after each test."""
    import code_sandbox_mcp.server as srv
    with srv._UPDATE_LOCK:
        was = srv._CURRENT_UPDATE_LOG_PATH
        srv._CURRENT_UPDATE_LOG_PATH = None
    yield
    with srv._UPDATE_LOCK:
        srv._CURRENT_UPDATE_LOG_PATH = was


class TestSandboxUpdateStart:
    """Tests for sandbox_update_start()."""

    def test_returns_job_id(self) -> None:
        result = sandbox_update_start()
        assert "Update started in background" in result
        assert "Log:" in result

    def test_concurrent_update_returns_error(self) -> None:
        import code_sandbox_mcp.server as srv
        with srv._UPDATE_LOCK:
            srv._CURRENT_UPDATE_LOG_PATH = "/tmp/update.log"
        result = sandbox_update_start()
        assert "already in progress" in result
        with srv._UPDATE_LOCK:
            srv._CURRENT_UPDATE_LOG_PATH = None




@pytest.fixture
def srv_module():
    import code_sandbox_mcp.server as srv
    return srv


class TestSandboxUpdateCheck:
    """Tests for sandbox_update_check()."""

    def test_no_job_returns_error(self, monkeypatch, srv_module) -> None:
        monkeypatch.setattr(srv_module, "_CURRENT_UPDATE_LOG_PATH", None)
        result = sandbox_update_check()
        assert "no update job found" in result

    def test_log_not_found_returns_error(self, monkeypatch, tmp_path, srv_module) -> None:
        nonexistent = str(tmp_path / "nonexistent" / "update.log")
        monkeypatch.setattr(srv_module, "_CURRENT_UPDATE_LOG_PATH", nonexistent)
        result = sandbox_update_check()
        assert "update log not found" in result

    def test_running_status(self, monkeypatch, tmp_path, srv_module) -> None:
        log_path = tmp_path / "update.log"
        log_path.write_text(
            "=== Update started (spec: test) @ 2026-01-01T00:00:00 ===\n"
            "Collecting package...\n"
        )
        monkeypatch.setattr(srv_module, "_CURRENT_UPDATE_LOG_PATH", str(log_path))
        result = sandbox_update_check()
        assert "Status: running" in result

    def test_done_status(self, monkeypatch, tmp_path, srv_module) -> None:
        log_path = tmp_path / "update.log"
        log_path.write_text(
            "=== Update started (spec: test) @ 2026-01-01T00:00:00 ===\n"
            "Installing...\n"
            "=== Update succeeded ===\n"
        )
        monkeypatch.setattr(srv_module, "_CURRENT_UPDATE_LOG_PATH", str(log_path))
        result = sandbox_update_check()
        assert "Status: done" in result

    def test_error_status(self, monkeypatch, tmp_path, srv_module) -> None:
        log_path = tmp_path / "update.log"
        log_path.write_text(
            "=== Update started (spec: test) @ 2026-01-01T00:00:00 ===\n"
            "ERROR: Something went wrong\n"
            "=== Update failed (exit code: 1) ===\n"
        )
        monkeypatch.setattr(srv_module, "_CURRENT_UPDATE_LOG_PATH", str(log_path))
        result = sandbox_update_check()
        assert "Status: error" in result
        assert "exit code: 1" in result


class TestUpdateSpecDefault:
    """Tests for the update spec default value."""

    def test_default_update_spec_is_absolute_path(self) -> None:
        from pathlib import Path
        p = Path(_UPDATE_SPEC)
        assert p.is_absolute()
        assert p.is_dir()
