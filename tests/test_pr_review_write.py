"""Tests for sandbox_pr_review_write tool (#477)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import sandbox_pr_review_write
from tests.conftest import _decode, _make_client_mock


class TestSandboxPrReviewWriteValidation:
    """Input validation, before any docker interaction."""

    def test_invalid_event(self) -> None:
        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1, event="INVALID",
        ))
        assert "error" in result
        assert "INVALID" in result["error"]

    def test_invalid_repo_format(self) -> None:
        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="not-a-repo", pr=1, event="APPROVE",
        ))
        assert "error" in result
        assert "repo format" in result["error"]

    def test_invalid_pr_number(self) -> None:
        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=0, event="APPROVE",
        ))
        assert "error" in result
        assert "PR number" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())
        from docker.errors import NotFound as DockerNotFound
        mock_docker.return_value.containers.get.side_effect = DockerNotFound("no container")

        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1, event="COMMENT",
        ))
        assert "error" in result
        assert "not found" in result["error"]


class TestSandboxPrReviewWriteExecute:
    """One-shot PR review via the host-side REST API."""

    _PR_DATA = {
        "head": {"sha": "abc123def4567890"},
        "html_url": "https://github.com/owner/repo/pull/1",
    }

    _REVIEW_RESULT = {
        "id": 98765,
        "html_url": "https://github.com/owner/repo/pull/1#pullrequestreview-98765",
    }

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_no_host_token_is_error(
        self,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1, event="APPROVE",
        ))
        assert result["status"] == "error"
        assert "token" in result["error"].lower() or "host-side" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_execute_approve_success(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with (
            patch(
                "code_sandbox_mcp.tools.vcs._github_api_request",
                side_effect=[self._PR_DATA, self._REVIEW_RESULT],
            ) as mock_api,
        ):
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=1,
                event="APPROVE", body="Looks good!",
            ))

        assert result["status"] == "ok"
        assert result["review_id"] == 98765
        assert "pullrequestreview" in result["html_url"]

        # First call: fetch PR data
        assert mock_api.call_args_list[0][0] == ("/repos/owner/repo/pulls/1", "ghs_tok")
        # Second call: create review with comments
        assert mock_api.call_args_list[1][0] == ("/repos/owner/repo/pulls/1/reviews", "ghs_tok")
        assert mock_api.call_args_list[1][1] == {
            "method": "POST",
            "payload": {
                "body": "Looks good!",
                "event": "APPROVE",
                "commit_id": "abc123def4567890",
            },
        }
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["approved"] is True

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_execute_with_inline_comments(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        comments = [
            {"path": "src/main.py", "line": 42, "body": "Consider using Optional[str]"},
            {"path": "src/utils.py", "line": 10, "side": "LEFT", "body": "Remove unused import"},
        ]

        with (
            patch(
                "code_sandbox_mcp.tools.vcs._github_api_request",
                side_effect=[self._PR_DATA, self._REVIEW_RESULT],
            ) as mock_api,
        ):
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=1,
                event="REQUEST_CHANGES", body="Please fix", comments=comments,
            ))

        assert result["status"] == "ok"
        assert result["review_id"] == 98765

        payload = mock_api.call_args_list[1][1]["payload"]
        assert payload["event"] == "REQUEST_CHANGES"
        assert payload["body"] == "Please fix"
        assert payload["commit_id"] == "abc123def4567890"
        assert payload["comments"] == comments
        assert mock_record.call_args.kwargs["approved"] is True

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_api_failure_records_denied_and_reports_error(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "code_sandbox_mcp.tools.vcs._github_api_request",
            side_effect=[
                self._PR_DATA,
                RuntimeError("GitHub API POST ... returned HTTP 422: validation error"),
            ],
        ):
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=1,
                event="APPROVE",
            ))

        assert result["status"] == "error"
        assert "422" in result["error"] or "validation" in result["error"]
        assert mock_record.call_args.kwargs["approved"] is False

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_pr_fetch_failure(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "code_sandbox_mcp.tools.vcs._github_api_request",
            side_effect=RuntimeError("GitHub API GET /repos/owner/repo/pulls/1 returned HTTP 404: Not Found"),
        ):
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=999,
                event="COMMENT",
            ))

        assert result["status"] == "error"
        assert "404" in result["error"] or "Not Found" in result["error"]
        assert mock_record.call_args.kwargs["approved"] is False
