"""Tests for publish advanced features: token flow, squash, force push, API fallback."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests.conftest import _make_container_mock, _make_client_mock, _decode

from code_sandbox_mcp.tools.vcs import publish


class TestPublishTokenFlow:
    """Integration tests for the dry_run → approve → execute flow."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_dry_run_generates_usable_token(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Token from dry_run should be usable for execute."""
        container = _make_container_mock([
            (0, b"M file.py\n---DIFF---\n 1 file changed", b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        dry_result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
        ))
        assert dry_result["status"] == "dry_run"
        token = dry_result["confirmation_token"]
        assert len(token) > 0

        from code_sandbox_mcp.token import verify_and_consume
        approval = verify_and_consume(token)
        assert approval is not None

        second_consume = verify_and_consume(token)
        assert second_consume is None


class TestPublishSquashCheckpoints:
    """Tests for publish with automatic checkpoint squash."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_squash_checkpoints(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Submit should squash unpushed checkpoints with reset --soft."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (0, b"main\n", b""),
            (0, b"abc1234 First checkpoint\n", b""),
            (0, b"", b""),
            (0, b"", b""),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"
        reset_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "reset --soft" in c[0][0][2]
        ]
        assert len(reset_calls) == 1

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_squash_checkpoints_no_tracking(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Submit with no tracking branch should skip squash."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"


class TestPublishAllowForcePush:
    """Tests for publish with allow_force_push=True."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_allow_force_push(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """allow_force_push=True should include --force in push command."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
            allow_force_push=True,
        ))

        assert result["status"] == "pushed"
        push_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "push origin" in c[0][0][2]
        ]
        assert len(push_calls) == 1
        assert "--force" in push_calls[0]


class TestPublishApiPushFallback:
    """Tests for publish when git push fails and falls back to _try_api_push."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_api_push_fallback_succeeds(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When git push fails, _try_api_push should be used as fallback."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        push_json = json.dumps({"sha": "b" * 40}).encode()
        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (1, b"", b"remote rejected: permission denied"),
            (0, b"abc1234def5678", b""),
            (0, b"", b""),
            (0, push_json, b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"
        assert result["sha"] == "bbbbbbb"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_api_push_fallback_fails(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When both git push and API push fail, return error."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (1, b"", b"remote rejected"),
            (0, b"abc1234def5678", b""),
            (0, b"", b""),
            (1, b"", b"API push failed too"),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_with_squash_and_force(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Dry run should show checkpoint info when unpushed commits exist."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good"}

        container = _make_container_mock([
            (0, b"M file.py\n---DIFF---\n 1 file changed", b""),
            (0, b"abc1234 First checkpoint\ndef5678 Second checkpoint\n", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
            allow_force_push=True,
        ))

        assert result["status"] == "dry_run"
        assert "Checkpoints to squash" in result["diff_summary"]
        assert "2 commit(s)" in result["diff_summary"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    def test_dry_run_only_checkpoints(
        self,
        mock_consume: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Dry run should work with no working tree changes but unpushed commits."""
        mock_consume.return_value = {"token": "tok_good"}

        container = _make_container_mock([
            (0, b"---DIFF---", b""),
            (0, b"abc1234 Only checkpoint\n", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        assert "unpushed checkpoints" in result["diff_summary"]
        assert "Checkpoints to squash: 1 commit(s)" in result["diff_summary"]
