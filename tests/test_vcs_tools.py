"""Tests for the External VCS tools (issue_view, submit) — Issue #55."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import issue_view, submit


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


def _decode(result: str) -> dict:
    return json.loads(result)


# ---------------------------------------------------------------------------
# issue_view tests
# ---------------------------------------------------------------------------


class TestIssueView:
    """Tests for issue_view."""

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
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
# submit tests
# ---------------------------------------------------------------------------


class TestSubmit:
    """Tests for submit."""

    # -- dry_run=True tests --

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.generate_token")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
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
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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
        assert call_kwargs["operation"] == "submit"

        # Verify pending boundary crossing was recorded
        mock_record.assert_called_once()
        record_kwargs = mock_record.call_args[1]
        assert record_kwargs["approved"] is None
        assert record_kwargs["token"] == "tok_abc123"

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
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
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/issue-55",
            message="Fix issue #55",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        assert "no changes" in result["diff_summary"].lower()

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.generate_token")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
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
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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

    @patch("code_sandbox_mcp.server._docker")
    def test_execute_without_token(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=False without token should return error."""
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(submit(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="",
        ))

        assert result["status"] == "error"
        assert "token" in result["error"].lower()

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_invalid_token(
        self,
        mock_run_id: MagicMock,
        mock_gate: MagicMock,
        mock_verify: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """dry_run=False with invalid token should return error."""
        mock_run_id.return_value = "run123"
        mock_gate.return_value = {"gate_passed": True}
        mock_verify.return_value = None  # token invalid
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(submit(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="bad_token",
        ))

        assert result["status"] == "error"
        assert "invalid" in result["error"].lower()

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_verify_gate_fails(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Execute with verify gate failure should reject."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "failed",
            "gate_passed": False,
            "gate_fail_reasons": ["lint: 3 error(s)"],
        }
        mock_docker.return_value = _make_client_mock(MagicMock())

        result = _decode(submit(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
            working_dir="/root/repo",
        ))

        assert result["status"] == "rejected"
        assert result["reason"] == "verify_gate_failed"
        assert result["verify_result"]["gate_passed"] is False

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_successful_push(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Successful push should return pushed status with sha."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "ok",
            "gate_passed": True,
        }

        # Mock: git add (0), git commit (0), git push (0), git rev-parse (0)
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (0, b"[fix/x abc1234] Fix issue\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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
        assert result["verify_result"]["gate_passed"] is True

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_successful_push_with_pr(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Successful push + PR creation should include pr_url."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "ok",
            "gate_passed": True,
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"pushed", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
            (0, b"https://github.com/owner/repo/pull/99", b""),  # gh pr create
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_commit_nothing_to_commit_is_ok(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """git commit with 'nothing to commit' should proceed to push."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "ok",
            "gate_passed": True,
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (0, b"nothing to commit, working tree clean", b""),  # git commit (no changes)
            (0, b"Everything up-to-date", b""),  # git push (already up to date)
            (0, b"abc1234def5678", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
            working_dir="/root/repo",
        ))

        assert result["status"] == "pushed"

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_push_failure(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Push failure should return error status."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "ok",
            "gate_passed": True,
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (0, b"[fix/x abc1234] Fix", b""),  # git commit
            (1, b"", b"remote rejected: permission denied"),  # git push (fail)
            (0, b"abc1234def5678", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.generate_token")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
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
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_uses_default_identity(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Default identity should be used when author_name/email are None."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "ok",
            "gate_passed": True,
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"

        # Verify the git commit command includes default identity
        commit_call = container.exec_run.call_args_list[2]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "code-sandbox-mcp[bot]" in commit_cmd
        assert "code-sandbox-mcp[bot]@users.noreply.github.com" in commit_cmd
        assert "'code-sandbox-mcp[bot]'" in commit_cmd

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.verify_and_consume")
    @patch("code_sandbox_mcp.server.run_verify")
    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server.get_or_create_run_id")
    def test_execute_with_custom_identity(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_verify_fn: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Custom author_name/email should override the defaults."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "submit",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_verify_fn.return_value = {
            "status": "ok",
            "gate_passed": True,
        }

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(submit(
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
        commit_call = container.exec_run.call_args_list[2]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "'Custom User'" in commit_cmd
        assert "custom@example.com" in commit_cmd

# ---------------------------------------------------------------------------
# Token flow integration tests
# ---------------------------------------------------------------------------


class TestSubmitTokenFlow:
    """Integration tests for the dry_run → approve → execute flow."""

    @patch("code_sandbox_mcp.server._docker")
    def test_dry_run_generates_usable_token(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Token from dry_run should be usable for execute."""
        container = _make_container_mock([
            (0, b"M file.py\n---DIFF---\n 1 file changed", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        # Step 1: dry_run to get token
        dry_result = _decode(submit(
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

from code_sandbox_mcp.server import sandbox_create_pr


class TestSandboxCreatePr:
    """Tests for sandbox_create_pr (Issue #152)."""

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
    def test_api_push_failure_returns_error(self, mock_docker: MagicMock) -> None:
        """If the python script returns non-zero, status=error is returned."""
        # exec_run sequence:
        # 1. write script (success)
        # 2. run script (fail)
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
            )
        )
        assert result["status"] == "error"
        assert result["step"] == "api_push"

    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server._docker")
    def test_success_returns_pr_url(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        """Happy path: API push succeeds and gh pr create returns a URL."""
        push_json = json.dumps(
            {"sha": "a" * 40, "tree_sha": "b" * 40, "parent_sha": "c" * 40}
        ).encode()
        pr_output = b"https://github.com/owner/repo/pull/99\n"

        container = _make_container_mock([
            (0, b"", b""),   # write script
            (0, push_json, b""),  # run script
            (0, pr_output, b""),  # gh pr create
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
            )
        )
        assert result["status"] == "ok"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/99"
        assert result["sha"] == "a" * 7
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.server.record_boundary_crossing")
    @patch("code_sandbox_mcp.server._docker")
    def test_push_success_pr_fail_returns_pushed_no_pr(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        """If push succeeds but gh pr create fails, return pushed_no_pr."""
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
            )
        )
        assert result["status"] == "pushed_no_pr"
        assert "pr_create_error" in result

    @patch("code_sandbox_mcp.server._docker")
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

