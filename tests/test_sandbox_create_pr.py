"""Tests for sandbox_create_pr (Issue #152, dry_run flow Issue #169)."""
from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import sandbox_create_pr


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


class TestSandboxCreatePr:
    """Tests for sandbox_create_pr."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_invalid_repo_format(self, mock_docker: MagicMock) -> None:
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="invalid-repo",
                branch="feat/x",
                pr_title="Test PR",
            )
        )
        assert result["status"] == "error"
        assert "owner/repo" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        from docker.errors import NotFound

        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
            )
        )
        assert "error" in result

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.generate_token")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_returns_token(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_gen_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=True returns a HEAD preview + token without pushing."""
        mock_run_id.return_value = "run123"
        mock_gen_token.return_value = "tok_create_pr"

        container = _make_container_mock([
            (0, b"abc1234 Add feature", b""),                # git log -1
            (0, b" file.py | 2 ++\n 1 file changed", b""),   # git show --stat
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                dry_run=True,
            )
        )
        assert result["status"] == "dry_run"
        assert result["confirmation_token"] == "tok_create_pr"
        assert result["branch"] == "feat/x"
        assert result["pr_title"] == "Test PR"
        assert "Add feature" in result["diff_summary"]

        mock_gen_token.assert_called_once()
        assert mock_gen_token.call_args[1]["operation"] == "sandbox_create_pr"
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["approved"] is None
        assert mock_record.call_args[1]["token"] == "tok_create_pr"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_execute_without_token(self, mock_docker: MagicMock) -> None:
        """dry_run=False without token returns an error before pushing."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
            )
        )
        assert result["status"] == "error"
        assert "token" in result["error"].lower()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_invalid_token(
        self,
        mock_run_id: MagicMock,
        mock_consume: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=False with an invalid/expired token returns an error."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = None  # token invalid
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                token="bad_token",
            )
        )
        assert result["status"] == "error"
        assert "invalid" in result["error"].lower()

    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_api_push_failure_returns_error(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_run_id: MagicMock,
    ) -> None:
        """If the python script returns non-zero, status=error is returned."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good", "operation": "sandbox_create_pr"}
        container = _make_container_mock([
            (0, b"", b""),
            (1, b'{"error": "gh api failed"}', b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                token="tok_good",
            )
        )
        assert result["status"] == "error"
        assert result["step"] == "api_push"
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_success_returns_pr_url(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_run_id: MagicMock,
    ) -> None:
        """Happy path: API push succeeds and gh pr create returns a URL."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good", "operation": "sandbox_create_pr"}
        push_json = json.dumps(
            {"sha": "a" * 40, "tree_sha": "b" * 40, "parent_sha": "c" * 40}
        ).encode()
        pr_output = b"https://github.com/owner/repo/pull/99\n"

        container = _make_container_mock([
            (0, b"", b""),         # write script
            (0, push_json, b""),   # run script
            (0, pr_output, b""),   # gh pr create
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                pr_body="Body text",
                token="tok_good",
            )
        )
        assert result["status"] == "ok"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/99"
        assert result["sha"] == "a" * 7
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_push_success_pr_fail_returns_pushed_no_pr(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_run_id: MagicMock,
    ) -> None:
        """If push succeeds but gh pr create fails, return pushed_no_pr."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good", "operation": "sandbox_create_pr"}
        push_json = json.dumps(
            {"sha": "a" * 40, "tree_sha": "b" * 40, "parent_sha": None}
        ).encode()

        container = _make_container_mock([
            (0, b"", b""),
            (0, push_json, b""),
            (1, b"PR already exists", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                token="tok_good",
            )
        )
        assert result["status"] == "pushed_no_pr"
        assert "pr_create_error" in result
        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args[1]
        assert call_kwargs["approved"] is True

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_invalid_branch_name(self, mock_docker: MagicMock) -> None:
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat;rm -rf /",
                pr_title="Test PR",
            )
        )
        assert result["status"] == "error"
        assert "invalid" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_invalid_base_branch_name(self, mock_docker: MagicMock) -> None:
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                base_branch="feat;rm -rf /",
            )
        )
        assert result["status"] == "error"
        assert "base_branch" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_json_parse_error_returns_error(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_run_id: MagicMock,
    ) -> None:
        """If the push script exits 0 but outputs invalid JSON, status=error."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good", "operation": "sandbox_create_pr"}
        container = _make_container_mock([
            (0, b"", b""),
            (0, b"not-json", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                token="tok_good",
            )
        )
        assert result["status"] == "error"
        assert result["step"] == "api_push"
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["approved"] is False

    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_push_result_error_key_returns_error(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_run_id: MagicMock,
    ) -> None:
        """If the push script exits 0 but JSON contains 'error', status=error."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good", "operation": "sandbox_create_pr"}
        container = _make_container_mock([
            (0, b"", b""),
            (0, b'{"error": "blob creation failed"}', b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(
            sandbox_create_pr(
                container_id="abc123def456",
                repo="owner/repo",
                branch="feat/x",
                pr_title="Test PR",
                token="tok_good",
            )
        )
        assert result["status"] == "error"
        assert "blob creation failed" in result.get("error", "")
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["approved"] is False
