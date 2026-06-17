"""Tests for the update MCP tools (sandbox_update_start / sandbox_update_check)."""
from __future__ import annotations

from code_sandbox_mcp.server import (
    _UPDATE_SPEC,
    sandbox_update_check,
    sandbox_update_start,
)


class TestSandboxUpdateStart:
    """Tests for sandbox_update_start()."""

    def test_returns_job_id(self) -> None:
        result = sandbox_update_start()
        assert "Update job started:" in result
        assert "sandbox_update_check" in result


class TestUpdateSpecDefault:
    """Tests for the update spec default value."""

    def test_default_update_spec_is_dot(self) -> None:
        assert _UPDATE_SPEC == "."
