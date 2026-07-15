"""Tests for sandbox_pr_review_write tool (#477)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from sunaba.tools.vcs import _github_api_request, sandbox_pr_review_write
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

    @patch("sunaba.tools.vcs._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())
        from docker.errors import NotFound as DockerNotFound
        mock_docker.return_value.containers.get.side_effect = DockerNotFound("no container")

        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1, event="COMMENT",
        ))
        assert "error" in result
        assert "not found" in result["error"]

    def test_invalid_comment_not_a_dict(self) -> None:
        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1,
            event="COMMENT", comments=["not a dict"],
        ))
        assert "error" in result
        assert "expected a dict" in result["error"]

    def test_comment_missing_path(self) -> None:
        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1,
            event="COMMENT", comments=[{"body": "look here"}],
        ))
        assert "error" in result
        assert "path" in result["error"]

    def test_comment_missing_body(self) -> None:
        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1,
            event="COMMENT", comments=[{"path": "src/main.py"}],
        ))
        assert "error" in result
        assert "body" in result["error"]

    @patch("sunaba.tools.vcs._docker")
    def test_empty_comments_list_is_accepted(self, mock_docker: MagicMock) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())
        from docker.errors import NotFound as DockerNotFound
        mock_docker.return_value.containers.get.side_effect = DockerNotFound("no container")

        result = _decode(sandbox_pr_review_write(
            container_id="abc123def456", repo="owner/repo", pr=1,
            event="COMMENT", comments=[],
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

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
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

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_approve_success(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with (
            patch(
                "sunaba.tools.vcs._github_api_request",
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

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
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
                "sunaba.tools.vcs._github_api_request",
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

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_own_pr_request_changes_auto_downgrades(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            side_effect=[
                self._PR_DATA,
                RuntimeError(
                    "GitHub API POST /repos/owner/repo/pulls/1/reviews "
                    "returned HTTP 422: Can not request changes on your own pull request"
                ),
                self._REVIEW_RESULT,
            ],
        ) as mock_api:
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=1,
                event="REQUEST_CHANGES",
            ))

        assert result["status"] == "ok"
        assert result["review_id"] == 98765
        assert result["original_event"] == "REQUEST_CHANGES"
        assert result["downgraded_to"] == "COMMENT"
        # Third call should have event=COMMENT
        assert mock_api.call_args_list[2][1]["payload"]["event"] == "COMMENT"

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_own_pr_approve_auto_downgrades(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            side_effect=[
                self._PR_DATA,
                RuntimeError(
                    "GitHub API POST /repos/owner/repo/pulls/1/reviews "
                    "returned HTTP 422: Can not approve your own pull request"
                ),
                self._REVIEW_RESULT,
            ],
        ) as mock_api:
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=1,
                event="APPROVE",
            ))

        assert result["status"] == "ok"
        assert result["review_id"] == 98765
        assert result["original_event"] == "APPROVE"
        assert result["downgraded_to"] == "COMMENT"
        # Third call should have event=COMMENT
        assert mock_api.call_args_list[2][1]["payload"]["event"] == "COMMENT"

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_own_pr_comment_passes_through(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            side_effect=[
                self._PR_DATA,
                RuntimeError(
                    "GitHub API POST /repos/owner/repo/pulls/1/reviews "
                    "returned HTTP 422: some other error"
                ),
            ],
        ):
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=1,
                event="COMMENT",
            ))

        assert result["status"] == "error"
        assert "422" in result["error"]
        assert "COMMENT" not in result["error"]

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_pr_fetch_failure(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_docker.return_value = _make_client_mock(MagicMock())

        with patch(
            "sunaba.tools.vcs._github_api_request",
            side_effect=RuntimeError("GitHub API GET /repos/owner/repo/pulls/1 returned HTTP 404: Not Found"),
        ):
            result = _decode(sandbox_pr_review_write(
                container_id="abc123def456", repo="owner/repo", pr=999,
                event="COMMENT",
            ))

        assert result["status"] == "error"
        assert "404" in result["error"] or "Not Found" in result["error"]
        assert mock_record.call_args.kwargs["approved"] is False


class TestGithubApiRequest:
    """Direct tests for _github_api_request error handling."""

    def test_raw_body_fallback_on_non_json_response(self) -> None:
        """Non-JSON error body should include raw body in RuntimeError."""
        import http.client
        import io
        import urllib.error

        fp = io.BytesIO(b"<html>rate limit exceeded</html>")
        exc = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo",
            code=403,
            msg="Forbidden",
            hdrs=http.client.HTTPMessage(),
            fp=fp,
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=exc,
        ):
            try:
                _github_api_request("/repos/owner/repo", "tok")
            except RuntimeError as e:
                err = str(e)
                assert "raw body: <html>rate limit exceeded</html>" in err
            else:
                msg = "Expected RuntimeError"
                raise AssertionError(msg)

    def test_parsed_message_included(self) -> None:
        """JSON error body with 'message' should include it in RuntimeError."""
        import http.client
        import io
        import json
        import urllib.error

        body = json.dumps({"message": "Not Found", "documentation_url": "..."})
        fp = io.BytesIO(body.encode("utf-8"))
        exc = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo",
            code=404,
            msg="Not Found",
            hdrs=http.client.HTTPMessage(),
            fp=fp,
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=exc,
        ):
            try:
                _github_api_request("/repos/owner/repo", "tok")
            except RuntimeError as e:
                assert "Not Found" in str(e)
                assert "documentation_url" not in str(e)
            else:
                msg = "Expected RuntimeError"
                raise AssertionError(msg)

    def test_parsed_errors_included(self) -> None:
        """JSON error body with 'errors' array should include each message."""
        import http.client
        import io
        import json
        import urllib.error

        body = json.dumps({
            "message": "Validation Failed",
            "errors": [
                {"resource": "PullRequestReview", "code": "custom",
                 "message": "Can not request changes on your own pull request"},
            ],
        })
        fp = io.BytesIO(body.encode("utf-8"))
        exc = urllib.error.HTTPError(
            url="https://api.github.com/repos/owner/repo/pulls/1/reviews",
            code=422,
            msg="Unprocessable Entity",
            hdrs=http.client.HTTPMessage(),
            fp=fp,
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=exc,
        ):
            try:
                _github_api_request("/repos/owner/repo/pulls/1/reviews", "tok",
                                    method="POST", payload={"event": "APPROVE"})
            except RuntimeError as e:
                err = str(e)
                assert "Validation Failed" in err
                assert "Can not request changes" in err
            else:
                msg = "Expected RuntimeError"
                raise AssertionError(msg)
