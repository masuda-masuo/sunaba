"""Tests for publish tool (dry_run → execute → squash → force push → API fallback)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import publish
from tests.conftest import _decode, _make_client_mock, _make_container_mock


class TestPublish:
    """Tests for publish."""

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

        mock_gen_token.assert_called_once()
        call_kwargs = mock_gen_token.call_args[1]
        assert call_kwargs["operation"] == "publish"

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
        mock_verify.return_value = None
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
            (0, b"nothing to commit, working tree clean", b""),  # git commit
            (0, b"Everything up-to-date", b""),  # git push
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
            (1, b"", b"remote rejected: permission denied"),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse
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

    @patch("code_sandbox_mcp.tools.vcs.resolve_git_root")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.generate_token")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_auto_resolves_from_meta(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_gen_token: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """Default working_dir auto-resolves from .sandbox-meta.json."""
        mock_resolve.return_value = "/tmp/repo/code-sandbox-mcp"
        mock_run_id.return_value = "run123"
        mock_gen_token.return_value = "tok_abc123"

        container = _make_container_mock([
            (0, b"M modified.py\n---DIFF---\n 1 file changed", b""),
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
        mock_resolve.assert_called_once()
        for call in container.exec_run.call_args_list:
            args, kwargs = call
            cmd = args[0][2]
            if "cd " not in cmd:
                continue
            assert "/tmp/repo/code-sandbox-mcp" in cmd, f"Expected resolved path in: {cmd}"

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

        commit_call = container.exec_run.call_args_list[3]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "'Custom User'" in commit_cmd
        assert "custom@example.com" in commit_cmd
