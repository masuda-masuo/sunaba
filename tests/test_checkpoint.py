"""Tests for checkpoint, checkpoint_list, checkpoint_restore."""
from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import (
    checkpoint,
    checkpoint_list,
    checkpoint_restore,
)


def _make_container_mock(exec_returns: list[tuple[int, bytes, bytes]]):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client_mock(container: MagicMock):
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _decode(result):
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return json.loads(result)


class TestCheckpoint:
    """Tests for checkpoint."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_success(self, mock_docker: MagicMock) -> None:
        """Happy path: git add + commit succeeds, returns sha."""
        container = _make_container_mock([
            (0, b"", b""),
            (0, b"abc1234\n", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint(
            container_id="abc123def456",
            message="my checkpoint",
        ))

        assert result["status"] == "ok"
        assert result["sha"] == "abc1234"
        assert result["message"] == "my checkpoint"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_git_failure(self, mock_docker: MagicMock) -> None:
        """Git commit failure should return error."""
        container = _make_container_mock([
            (1, b"fatal: not a git repository", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint(
            container_id="abc123def456",
            message="my checkpoint",
        ))

        assert result["status"] == "error"
        assert result["step"] == "checkpoint"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_container_not_found(self, mock_docker: MagicMock) -> None:
        """Container not found should return error."""
        from docker.errors import NotFound
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = _decode(checkpoint(
            container_id="abc123def456",
            message="test",
        ))

        assert "error" in result


class TestCheckpointList:
    """Tests for checkpoint_list."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_list_success(self, mock_docker: MagicMock) -> None:
        """Happy path: returns list of checkpoints."""
        log_output = (
            b"abc1234 2026-06-24T10:00:00+00:00 First checkpoint\n"
            b"def5678 2026-06-24T10:05:00+00:00 Second checkpoint\n"
        )
        container = _make_container_mock([
            (0, log_output, b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint_list(
            container_id="abc123def456",
        ))

        assert "checkpoints" in result
        assert len(result["checkpoints"]) == 2
        assert result["checkpoints"][0]["sha"] == "abc1234"
        assert result["checkpoints"][1]["message"] == "Second checkpoint"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_list_empty(self, mock_docker: MagicMock) -> None:
        """Empty git log should return empty list."""
        container = _make_container_mock([
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint_list(
            container_id="abc123def456",
        ))

        assert result["checkpoints"] == []

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_list_container_not_found(self, mock_docker: MagicMock) -> None:
        """Container not found should return error."""
        from docker.errors import NotFound
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = _decode(checkpoint_list(
            container_id="abc123def456",
        ))

        assert "error" in result


class TestCheckpointRestore:
    """Tests for checkpoint_restore."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_restore_success(self, mock_docker: MagicMock) -> None:
        """Happy path: git reset --hard succeeds."""
        container = _make_container_mock([
            (0, b"HEAD is now at abc1234", b""),
            (0, b"abc1234\n", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint_restore(
            container_id="abc123def456",
            sha="abc1234",
        ))

        assert result["status"] == "ok"
        assert result["restored_to"] == "abc1234"
        assert "warning" in result

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_restore_failure(self, mock_docker: MagicMock) -> None:
        """Git reset failure should return error."""
        container = _make_container_mock([
            (1, b"fatal: ambiguous argument", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint_restore(
            container_id="abc123def456",
            sha="badsha",
        ))

        assert result["status"] == "error"
        assert result["step"] == "checkpoint_restore"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_checkpoint_restore_container_not_found(self, mock_docker: MagicMock) -> None:
        """Container not found should return error."""
        from docker.errors import NotFound
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = _decode(checkpoint_restore(
            container_id="abc123def456",
            sha="abc1234",
        ))

        assert "error" in result
