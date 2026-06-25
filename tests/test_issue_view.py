"""Tests for issue_view tool."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tests.conftest import _make_container_mock, _make_client_mock, _decode

from code_sandbox_mcp.tools.vcs import issue_view


class TestIssueView:
    """Tests for issue_view."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_successful_fetch(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Successful issue fetch returns number, title, summary, file, size."""
        issue_json = json.dumps({
            "number": 55,
            "title": "Implement VCS tools",
            "body": "This is the issue body.\n\nIt has multiple paragraphs.\n\n"
                    "And more content here for testing the 100 char limit.\n",
        })
        container = _make_container_mock([
            (0, issue_json.encode(), b""),  # gh issue view
            (0, b"", b""),  # write body to file
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

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

        mock_record.assert_called_once()
        call_args = mock_record.call_args
        assert call_args[0][1] == "issue_view"
        assert call_args[1]["approved"] is None

    @patch("code_sandbox_mcp.tools.vcs._docker")
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

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_gh_command_failure(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """gh command failure should return error with stderr."""
        container = _make_container_mock([
            (1, b"", b"could not find issue"),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(issue_view(
            container_id="abc123def456",
            repo="owner/repo",
            issue_number=999,
        ))

        assert "error" in result
        assert "could not find issue" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_invalid_json_from_gh(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Invalid JSON from gh should return parse error."""
        container = _make_container_mock([
            (0, b"not valid json", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(issue_view(
            container_id="abc123def456",
            repo="owner/repo",
            issue_number=55,
        ))

        assert "error" in result
        assert "parse" in result["error"].lower()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_custom_save_path(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Custom save_to path should be reflected in result."""
        issue_json = json.dumps({
            "number": 1,
            "title": "Test",
            "body": "Simple body",
        })
        container = _make_container_mock([
            (0, issue_json.encode(), b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(issue_view(
            container_id="abc123def456",
            repo="owner/repo",
            issue_number=1,
            save_to="/home/sandbox/issue.md",
        ))

        assert result["file"] == "/home/sandbox/issue.md"
        assert result["size_bytes"] == len("Simple body".encode())

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_empty_body(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Issue with empty body should return (empty body) summary."""
        issue_json = json.dumps({
            "number": 1,
            "title": "No content",
            "body": "",
        })
        container = _make_container_mock([
            (0, issue_json.encode(), b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(issue_view(
            container_id="abc123def456",
            repo="owner/repo",
            issue_number=1,
        ))

        assert result["summary"] == "(empty body)"
        assert result["size_bytes"] == 0
