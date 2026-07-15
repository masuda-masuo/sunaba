"""Tests for issue_view tool."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from sunaba.tools.vcs import issue_view
from tests.conftest import _decode, _make_client_mock, _make_container_mock


def _make_comment(user: str, ts: str, body: str) -> dict:
    return {
        "user": {"login": user},
        "created_at": ts,
        "body": body,
    }


class TestIssueView:
    """Tests for issue_view."""

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_successful_fetch(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """Successful issue fetch returns number, title, summary, file, size."""
        container = _make_container_mock([
            (0, b"", b""),  # write body to file
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={
                "number": 55,
                "title": "Implement VCS tools",
                "body": "This is the issue body.\n\nIt has multiple paragraphs.\n\n"
                        "And more content here for testing the 100 char limit.\n",
            },
        ) as mock_api:
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=55,
            ))

        assert result["number"] == 55
        assert result["title"] == "Implement VCS tools"
        assert result["summary"].startswith("This is the issue body.")
        assert len(result["summary"]) <= 100
        assert result["file"] == "/home/sandbox/issue.md"
        assert result["size_bytes"] > 0

        # Fetched host-side (#360): the container never runs gh, and this
        # path works with no host token (anonymous, public-repo read).
        mock_api.assert_called_once_with("/repos/owner/repo/issues/55", "")

        mock_record.assert_called_once()
        call_args = mock_record.call_args
        assert call_args[0][1] == "issue_view"
        assert call_args[1]["approved"] is None

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="ghs_tok")
    @patch("sunaba.tools.vcs._docker")
    def test_uses_host_token_when_available(
        self,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """A host-resolved token is passed through to raise the rate limit."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={"number": 1, "title": "T", "body": "B"},
        ) as mock_api:
            issue_view(container_id="abc123def456", repo="owner/repo", issue_number=1)

        mock_api.assert_called_once_with("/repos/owner/repo/issues/1", "ghs_tok")

    @patch("sunaba.tools.vcs._docker")
    def test_container_not_found(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Container not found should return error."""
        mock_docker.return_value = _make_client_mock(MagicMock())
        from docker.errors import NotFound as DockerNotFound
        mock_docker.return_value.containers.get.side_effect = DockerNotFound(
            "No such container"
        )

        result = _decode(issue_view(
            container_id="abc123def456",
            repo="owner/repo",
            issue_number=55,
        ))

        assert "error" in result
        assert "not found" in result["error"]

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    def test_api_error_is_reported(
        self,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """A GitHub API failure should return an error mentioning the issue."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._github_api_request",
            side_effect=RuntimeError(
                "GitHub API GET /repos/owner/repo/issues/999 returned HTTP 404: Not Found"
            ),
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=999,
            ))

        assert "error" in result
        assert "404" in result["error"]

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_custom_save_path(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """Custom save_to path should be reflected in result."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={"number": 1, "title": "Test", "body": "Simple body"},
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=1,
                save_to="/home/sandbox/issue.md",
            ))

        assert result["file"] == "/home/sandbox/issue.md"
        assert result["size_bytes"] == len("Simple body".encode())

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_empty_body(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """Issue with empty body should return (empty body) summary."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={"number": 1, "title": "No content", "body": ""},
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=1,
            ))

        assert result["summary"] == "(empty body)"
        assert result["size_bytes"] == 0

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    def test_write_failure_is_reported(
        self,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """A failed in-container write should return an error."""
        container = _make_container_mock([
            (1, b"", b"no space left on device"),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._github_api_request",
            return_value={"number": 1, "title": "T", "body": "B"},
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=1,
            ))

        assert "error" in result
        assert "Failed to write" in result["error"]


class TestIssueViewWithComments:
    """Tests for issue_view with include_comments=True."""

    def _make_issue_response(self) -> dict:
        return {
            "number": 42,
            "title": "Test issue",
            "body": "This is the issue body.",
        }

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_include_comments_appends_to_body(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """include_comments=True fetches comments and appends them."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        comments = [
            _make_comment("alice", "2026-07-10T10:00:00Z", "First comment."),
            _make_comment("bob", "2026-07-11T12:00:00Z", "Second comment."),
        ]

        with (
            patch(
                "sunaba.tools.vcs._github_api_request",
                return_value=self._make_issue_response(),
            ),
            patch(
                "sunaba.tools.vcs._github_api_request_list",
                return_value=comments,
            ),
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=42,
                include_comments=True,
            ))

        assert result["number"] == 42
        assert result["comments"] == 2

        # Verify the file content includes both body and comments
        write_cmd = container.exec_run.call_args[0][0][-1]
        import base64
        encoded = write_cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'")
        written = base64.b64decode(encoded).decode("utf-8")

        assert "This is the issue body." in written
        assert "## Comments" in written
        assert "@alice" in written
        assert "@bob" in written
        assert "2026-07-10T10:00:00Z" in written
        assert "First comment." in written
        assert "Second comment." in written
        # Chronological order: alice before bob
        assert written.index("@alice") < written.index("@bob")

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_include_comments_default_false(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """include_comments=False (default) does not fetch comments."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with (
            patch(
                "sunaba.tools.vcs._github_api_request",
                return_value=self._make_issue_response(),
            ),
            patch(
                "sunaba.tools.vcs._github_api_request_list",
            ) as mock_comments_api,
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=42,
            ))

        assert result["comments"] is None
        mock_comments_api.assert_not_called()

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_max_comments_trims_oldest(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """max_comments trims oldest comments, keeping newest."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        comments = [
            _make_comment("old1", "2026-07-01T00:00:00Z", "Old comment 1"),
            _make_comment("old2", "2026-07-02T00:00:00Z", "Old comment 2"),
            _make_comment("new1", "2026-07-03T00:00:00Z", "New comment 1"),
        ]

        with (
            patch(
                "sunaba.tools.vcs._github_api_request",
                return_value=self._make_issue_response(),
            ),
            patch(
                "sunaba.tools.vcs._github_api_request_list",
                return_value=comments,
            ),
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=42,
                include_comments=True,
                max_comments=2,
            ))

        assert result["comments"] == 2

        write_cmd = container.exec_run.call_args[0][0][-1]
        import base64
        encoded = write_cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'")
        written = base64.b64decode(encoded).decode("utf-8")

        assert "old1" not in written
        assert "Old comment 1" not in written
        assert "new1" in written
        assert "old2" in written

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_comments_api_error(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """A comments API failure should return an error."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with (
            patch(
                "sunaba.tools.vcs._github_api_request",
                return_value=self._make_issue_response(),
            ),
            patch(
                "sunaba.tools.vcs._github_api_request_list",
                side_effect=RuntimeError(
                    "GitHub API GET /repos/owner/repo/issues/42/comments returned HTTP 403"
                ),
            ),
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=42,
                include_comments=True,
            ))

        assert "error" in result
        assert "403" in result["error"]

    @patch("sunaba.tools.vcs._resolve_vcs_token", return_value="")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_no_comments_returns_body_only(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_token: MagicMock,
    ) -> None:
        """include_comments=True with no comments still writes just the body."""
        container = _make_container_mock([(0, b"", b"")])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with (
            patch(
                "sunaba.tools.vcs._github_api_request",
                return_value=self._make_issue_response(),
            ),
            patch(
                "sunaba.tools.vcs._github_api_request_list",
                return_value=[],
            ),
        ):
            result = _decode(issue_view(
                container_id="abc123def456",
                repo="owner/repo",
                issue_number=42,
                include_comments=True,
            ))

        assert result["comments"] == 0

        write_cmd = container.exec_run.call_args[0][0][-1]
        import base64
        encoded = write_cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'")
        written = base64.b64decode(encoded).decode("utf-8")

        assert written == "This is the issue body."
        assert "## Comments" not in written
