"""Tests for sandbox_issue_write tool (#414)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from sunaba.tools.vcs import sandbox_issue_write
from tests.conftest import _decode, _make_client_mock


class TestSandboxIssueWriteValidation:
    """Input validation, before any docker interaction."""

    def test_invalid_method(self) -> None:
        result = _decode(sandbox_issue_write(
            container_id="abc123def456", repo="owner/repo", method="delete",
        ))
        assert "error" in result
        assert "method" in result["error"]

    def test_invalid_repo_format(self) -> None:
        result = _decode(sandbox_issue_write(
            container_id="abc123def456", repo="not-a-repo", method="create", title="T",
        ))
        assert "error" in result
        assert "repo format" in result["error"]

    def test_create_requires_title(self) -> None:
        result = _decode(sandbox_issue_write(
            container_id="abc123def456", repo="owner/repo", method="create",
        ))
        assert "error" in result
        assert "title" in result["error"]

    def test_comment_requires_issue_number(self) -> None:
        result = _decode(sandbox_issue_write(
            container_id="abc123def456", repo="owner/repo", method="comment", body="hi",
        ))
        assert "error" in result
        assert "issue_number" in result["error"]

    @patch("sunaba.tools.vcs._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())
        from docker.errors import NotFound as DockerNotFound
        mock_docker.return_value.containers.get.side_effect = DockerNotFound("no container")

        result = _decode(sandbox_issue_write(
            container_id="abc123def456", repo="owner/repo", method="create", title="T",
        ))
        assert "error" in result
        assert "not found" in result["error"]


class TestSandboxIssueWriteExecute:
    """One-shot execute via the host-side REST API (no dry_run/token step)."""

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    def test_no_host_token_is_error(
        self,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(sandbox_issue_write(
            container_id="abc123def456", repo="owner/repo", method="create",
            title="T",
        ))
        assert result["status"] == "error"
        assert "host-side" in result["error"] or "token" in result["error"].lower()

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_create_success(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={"number": 99, "html_url": "https://github.com/owner/repo/issues/99"},
        ) as mock_api:
            result = _decode(sandbox_issue_write(
                container_id="abc123def456", repo="owner/repo", method="create",
                title="Bug found", body="details here",
            ))

        assert result["status"] == "ok"
        assert result["number"] == 99
        assert result["html_url"] == "https://github.com/owner/repo/issues/99"
        mock_api.assert_called_once_with(
            "/repos/owner/repo/issues", "ghs_tok", method="POST",
            payload={"title": "Bug found", "body": "details here"},
        )
        mock_record.assert_called_once()
        assert mock_record.call_args.kwargs["approved"] is True

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_comment_success(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={"html_url": "https://github.com/owner/repo/issues/42#issuecomment-1"},
        ) as mock_api:
            result = _decode(sandbox_issue_write(
                container_id="abc123def456", repo="owner/repo", method="comment",
                issue_number=42, body="thanks!",
            ))

        assert result["status"] == "ok"
        assert "number" not in result
        mock_api.assert_called_once_with(
            "/repos/owner/repo/issues/42/comments", "ghs_tok", method="POST",
            payload={"body": "thanks!"},
        )

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_api_failure_records_denied_and_reports_error(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            side_effect=RuntimeError("GitHub API POST /repos/owner/repo/issues returned HTTP 403: rate limited"),
        ):
            result = _decode(sandbox_issue_write(
                container_id="abc123def456", repo="owner/repo", method="create",
                title="T",
            ))

        assert result["status"] == "error"
        assert "403" in result["error"]
        assert mock_record.call_args.kwargs["approved"] is False
