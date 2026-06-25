"""Tests for the External VCS tools (issue_view, publish) — Issue #55."""
from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

from code_sandbox_mcp.tools.vcs import (
    checkpoint,
    checkpoint_list,
    checkpoint_restore,
    issue_view,
    sandbox_create_pr,
    publish,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_container_mock(exec_returns: list[tuple[int, bytes, bytes]]):
    """Build a mock Docker container with a sequence of exec_run results."""
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client_mock(container: MagicMock):
    """Build a mock Docker client that returns the given container."""
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _decode(result):
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return json.loads(result)


# ---------------------------------------------------------------------------
# issue_view tests
# ---------------------------------------------------------------------------


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

        # Verify boundary crossing was recorded
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


# ---------------------------------------------------------------------------
# publish tests
# ---------------------------------------------------------------------------


class TestPublish:
    """Tests for publish."""

    # -- dry_run=True tests --

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
        """dry_run=True should return diff summary and confirmation token."""
        mock_run_id.return_value = "run123"
        mock_gen_token.return_value = "tok_abc123"

        container = _make_container_mock([
            (0, b"M modified.py\n?? new.py\n---DIFF---\n 2 files changed", b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/issue-55",
            message="Fix issue #55",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        assert result["branch"] == "fix/issue-55"
        assert "modified.py" in result["diff_summary"]
        assert result["confirmation_token"] == "tok_abc123"
        assert result["create_pr"] is False

        # Verify token was generated for submit
        mock_gen_token.assert_called_once()
        call_kwargs = mock_gen_token.call_args[1]
        assert call_kwargs["operation"] == "publish"

        # Verify pending boundary crossing was recorded
        mock_record.assert_called_once()
        record_kwargs = mock_record.call_args[1]
        assert record_kwargs["approved"] is None
        assert record_kwargs["token"] == "tok_abc123"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_no_changes(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=True with no changes should return warning."""
        mock_run_id.return_value = "run123"

        container = _make_container_mock([
            (0, b"---DIFF---", b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/issue-55",
            message="Fix issue #55",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        assert "no changes" in result["diff_summary"].lower()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.generate_token")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_with_create_pr(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_gen_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=True with create_pr=True should include PR info."""
        mock_run_id.return_value = "run123"
        mock_gen_token.return_value = "tok_pr"

        container = _make_container_mock([
            (0, b"M file.py\n---DIFF---\n 1 file changed", b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="feat/new",
            message="Add feature",
            create_pr=True,
            pr_title="My PR",
            dry_run=True,
        ))

        assert result["create_pr"] is True
        assert result["pr_title"] == "My PR"

    # -- dry_run=False (execute) tests --

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_execute_without_token(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=False without token should return error."""
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="",
        ))

        assert result["status"] == "error"
        assert "token" in result["error"].lower()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_invalid_token(
        self,
        mock_run_id: MagicMock,
        mock_verify: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=False with invalid token should return error."""
        mock_run_id.return_value = "run123"
        mock_verify.return_value = None  # token invalid
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="bad_token",
        ))

        assert result["status"] == "error"
        assert "invalid" in result["error"].lower()


    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_successful_push(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Successful push should return pushed status with sha."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        # Mock: git add (0), git commit (0), git push (0), git rev-parse (0)
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix issue\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix issue",
            dry_run=False,
            token="tok_good",
            working_dir="/root/repo",
        ))

        assert result["status"] == "pushed"
        assert result["branch"] == "fix/x"
        assert result["sha"] == "abc1234"


    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_successful_push_with_pr(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Successful push + PR creation should include pr_url."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"pushed", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
            (0, b"https://github.com/owner/repo/pull/99", b""),  # gh pr create
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            create_pr=True,
            pr_title="My PR Title",
            pr_body="PR body here",
            dry_run=False,
            token="tok_good",
            working_dir="/root/repo",
        ))

        assert result["status"] == "pushed"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/99"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_commit_nothing_to_commit_is_ok(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """git commit with 'nothing to commit' should proceed to push."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"nothing to commit, working tree clean", b""),  # git commit (no changes)
            (0, b"Everything up-to-date", b""),  # git push (already up to date)
            (0, b"abc1234def5678", b""),  # git rev-parse
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
            working_dir="/root/repo",
        ))

        assert result["status"] == "pushed"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_push_failure(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Push failure should return error status."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix", b""),  # git commit
            (1, b"", b"remote rejected: permission denied"),  # git push (fail)
            (0, b"abc1234def5678", b""),  # git rev-parse
            # Transport fallback: _try_api_push
            (0, b"", b""),  # write API push script
            (1, b"", b"push failed"),  # API push also fails
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
            working_dir="/root/repo",
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"
        assert "permission denied" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.generate_token")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_default_working_dir(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_gen_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Default working_dir is /home/sandbox."""
        mock_run_id.return_value = "run123"
        mock_gen_token.return_value = "tok_abc123"

        container = _make_container_mock([
            (0, b"M modified.py\n?? new.py\n---DIFF---\n 2 files changed", b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/issue-55",
            message="Fix issue #55",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        call_args = container.exec_run.call_args[0][0]
        assert "cd /home/sandbox" in call_args[2]


    # -- Git identity tests --

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_uses_default_identity(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Default identity should be used when author_name/email are None."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
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

        # Verify the git commit command includes default identity
        commit_call = container.exec_run.call_args_list[3]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "code-sandbox-mcp[bot]" in commit_cmd
        assert "code-sandbox-mcp[bot]@users.noreply.github.com" in commit_cmd
        assert "'code-sandbox-mcp[bot]'" in commit_cmd

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_with_custom_identity(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Custom author_name/email should override the defaults."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
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
            author_name="Custom User",
            author_email="custom@example.com",
        ))

        assert result["status"] == "pushed"

        # Verify the git commit command includes custom identity
        commit_call = container.exec_run.call_args_list[3]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "'Custom User'" in commit_cmd
        assert "custom@example.com" in commit_cmd

# ---------------------------------------------------------------------------
# Token flow integration tests
# ---------------------------------------------------------------------------


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

        # Step 1: dry_run to get token
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

        # Step 2: call sandbox_approve (from token.py) to approve
        from code_sandbox_mcp.token import verify_and_consume
        approval = verify_and_consume(token)
        assert approval is not None

        # Step 3: Now attempt execute with the SAME token (should fail
        # because it was just consumed by verify_and_consume above)
        # In the real flow, approve acts on it, then submit consumes it.
        # Since we already consumed it, the next verify_and_consume will fail.
        second_consume = verify_and_consume(token)
        assert second_consume is None  # Already consumed

# ---------------------------------------------------------------------------
# sandbox_create_pr tests
# ---------------------------------------------------------------------------



class TestSandboxCreatePr:
    """Tests for sandbox_create_pr (Issue #152, dry_run flow Issue #169)."""

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

    # -- dry_run=True --

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

    # -- dry_run=False (execute) --

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

# ---------------------------------------------------------------------------
# checkpoint tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# checkpoint_list tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# checkpoint_restore tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Additional publish tests for issue #241 features
# ---------------------------------------------------------------------------


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
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
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
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
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
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
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


# ---------------------------------------------------------------------------
# publish async + progress notification tests (Issue #253)
# ---------------------------------------------------------------------------


