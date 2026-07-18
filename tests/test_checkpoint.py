"""Tests for checkpoint, checkpoint_list, checkpoint_restore."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from sunaba.tools.vcs import (
    checkpoint,
    checkpoint_list,
    checkpoint_restore,
)
from tests.conftest import _decode, _make_client_mock, _make_container_mock


class TestCheckpoint:
    """Tests for checkpoint."""

    @patch("sunaba.tools.vcs._docker")
    def test_checkpoint_success(self, mock_docker: MagicMock) -> None:
        """Happy path: git add + commit succeeds, returns sha."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"", b""),  # git add -A && git commit
            (0, b"abc1234\n", b""),  # git rev-parse --short HEAD
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
        assert result["swept_untracked"] == []

    @patch("sunaba.tools.vcs._docker")
    def test_checkpoint_swept_untracked_empty(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """swept_untracked is [] when no untracked files exist."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files (empty)
            (0, b"", b""),  # git add -A && git commit
            (0, b"abc1234\n", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint(
            container_id="abc123def456",
            message="cp",
        ))
        assert result["swept_untracked"] == []

    @patch("sunaba.tools.vcs._docker")
    def test_checkpoint_swept_untracked_with_files(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """swept_untracked lists untracked files when they exist."""
        container = _make_container_mock([
            (0, b"newfile.py\nunused.md\n", b""),  # git ls-files
            (0, b"", b""),  # git add -A && git commit
            (0, b"def5678\n", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint(
            container_id="abc123def456",
            message="cp",
        ))
        assert result["swept_untracked"] == ["newfile.py", "unused.md"]

    @patch("sunaba.tools.vcs._docker")
    def test_checkpoint_git_failure(self, mock_docker: MagicMock) -> None:
        """Git commit failure should return error."""
        container = _make_container_mock([
            (0, b"", b""),  # git ls-files succeeds
            (1, b"fatal: not a git repository", b""),  # git add+commit fails
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(checkpoint(
            container_id="abc123def456",
            message="my checkpoint",
        ))

        assert result["status"] == "error"
        assert result["step"] == "checkpoint"

    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._docker")
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
