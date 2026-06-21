"""Tests for Issue #86: clone_repo and list_files tools."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import clone_repo


def _make_container(exec_returns):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client(container):
    client = MagicMock()
    client.containers.get.return_value = container
    return client


class TestCloneRepo:
    """Tests for clone_repo tool."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_successful_clone(self, mock_record, mock_docker):
        """Successful clone returns ok with clone_path."""
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "ok"
        assert result["repo"] == "owner/mytool"
        assert result["clone_path"] == "/home/sandbox/mytool"
        assert result["branch"] == "default"
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_clone_with_branch(self, mock_record, mock_docker):
        """Clone with branch specified."""
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            clone_repo("abc123def456", "owner/mytool", branch="develop")
        )
        assert result["status"] == "ok"
        assert result["branch"] == "develop"
        mock_record.assert_called_once()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_failure(self, mock_docker):
        """Clone failure returns error status."""
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (1, b"", b"fatal: repository not found\n"),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/nonexistent"))
        assert result["status"] == "error"
        assert "repository not found" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_with_custom_dest(self, mock_docker):
        """Clone with custom dest_dir computes correct clone_path."""
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (0, b"", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            clone_repo("abc123def456", "owner/mytool", dest_dir="/tmp/work")
        )
        assert result["clone_path"] == "/tmp/work/mytool"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_targets_repo_subdir(self, mock_docker):
        """Issue #131: gh clones into {dest_dir}/{repo_name}, not dest_dir.

        ``gh repo clone`` treats its second argument as the clone target
        itself, so cloning into the default ``/home/sandbox`` (an existing
        non-empty home) fails.  The command must target the repo subdir.
        """
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        clone_repo("abc123def456", "owner/mytool")

        cmd = container.exec_run.call_args[0][0][-1]
        assert "/home/sandbox/mytool" in cmd
        # The bare parent must not be the clone target.
        assert "gh repo clone 'owner/mytool' '/home/sandbox'" not in cmd

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_existing_dir_adds_hint(self, mock_docker):
        """Issue #131: 'already exists' failures get an actionable hint."""
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (
                1,
                b"",
                b"fatal: destination path '/home/sandbox/mytool' already "
                b"exists and is not an empty directory.\n",
            ),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "error"
        assert "Hint:" in result["error"]
        assert "dest_dir" in result["error"]


    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_clone_succeeds_when_auth_setup_fails(self, mock_record, mock_docker):
        """gh auth setup-git failure is ignored; clone still proceeds."""
        container = _make_container([
            (1, b"", b"gh: not logged in\n"),  # gh auth setup-git fails
            (0, b"Cloning into 'mytool'...\n", b""),  # clone succeeds
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "ok"
        assert result["clone_path"] == "/home/sandbox/mytool"

