"""Tests for clone_repo: validation, Shiori pre-clone, network fallback, and tool endpoint."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.tools.container import (
    _clone_repo_via_network,
    _clone_shiori_repo_to_container,
    _validate_clone_repo,
)


class TestValidateCloneRepo:
    """Tests for _validate_clone_repo."""

    def test_valid_owner_name(self) -> None:
        owner, name = _validate_clone_repo("masuda-masuo/shiori")
        assert owner == "masuda-masuo"
        assert name == "shiori"

    def test_valid_with_dots(self) -> None:
        owner, name = _validate_clone_repo("my.org/my-repo_v2")
        assert owner == "my.org"
        assert name == "my-repo_v2"

    def test_empty_string(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            _validate_clone_repo("")

    def test_no_slash(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("masuda-masuo")

    def test_too_many_slashes(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("a/b/c")

    def test_empty_owner(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("/shiori")

    def test_empty_name(self) -> None:
        with pytest.raises(ValueError, match="owner/name"):
            _validate_clone_repo("masuda-masuo/")

    def test_invalid_characters(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_clone_repo("bad@owner/repo")

    def test_invalid_characters_in_name(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric"):
            _validate_clone_repo("owner/rep o")


class TestCloneShioriRepoToContainer:
    """Tests for _clone_shiori_repo_to_container."""

    def test_invalid_clone_dest(self) -> None:
        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", "/data/repos"
        ):
            with pytest.raises(ValueError, match="must start with /tmp/"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/etc/repo"
                )

    def test_no_shiori_repos_path_configured(self) -> None:
        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", None
        ):
            with pytest.raises(ValueError, match="not configured"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/tmp/repo"
                )

    def test_repos_root_not_found(self) -> None:
        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", "/nonexistent/path"
        ):
            with pytest.raises(ValueError, match="not found"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/tmp/repo"
                )

    def test_path_traversal_prevented_by_validate(self) -> None:
        """Path traversal via '../' is caught by _validate_clone_repo format check."""
        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", "/data/repos"
        ):
            with pytest.raises(ValueError, match="owner/name"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "../escape/repo", "/tmp/repo"
                )

    def test_clone_not_found(self, tmp_path: Path) -> None:
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            with pytest.raises(ValueError, match="not found"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/tmp/repo"
                )

    def test_no_git_directory(self, tmp_path: Path) -> None:
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        clone_dir = repos_root / "owner" / "repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / "README.md").write_text("hello")
        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            with pytest.raises(ValueError, match="no .git directory"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/tmp/repo"
                )

    def test_successful_copy_and_unshallow(self, tmp_path: Path) -> None:
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        clone_dir = repos_root / "owner" / "repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()
        (clone_dir / "README.md").write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.return_value = (
            0,
            (b"remote: Enumerating objects: 42, done.\n", b""),
        )

        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            result = _clone_shiori_repo_to_container(
                mock_container, "abc123", "owner/repo", "/tmp/repo"
            )

        assert "Copied Shiori clone" in result
        assert "/tmp/repo/repo" in result
        mock_container.put_archive.assert_called_once()
        assert mock_container.exec_run.call_count == 2

    def test_unshallow_fails_but_copy_succeeds(
            self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        caplog.set_level(logging.WARNING)
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        clone_dir = repos_root / "owner" / "repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()
        (clone_dir / "README.md").write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.return_value = (
            1,
            (b"fatal: --unshallow on a complete repository does not make sense\n", b""),
        )

        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            result = _clone_shiori_repo_to_container(
                mock_container, "abc123", "owner/repo", "/tmp/repo"
            )

        assert "Copied Shiori clone" in result
        assert "git fetch --unshallow failed" in caplog.text
        mock_container.put_archive.assert_called_once()

    def test_unshallow_error_caught(self, tmp_path: Path) -> None:
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        clone_dir = repos_root / "owner" / "repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.side_effect = Exception("network error")

        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            result = _clone_shiori_repo_to_container(
                mock_container, "abc123", "owner/repo", "/tmp/repo"
            )

        assert "Copied Shiori clone" in result

    def test_put_archive_failure(self, tmp_path: Path) -> None:
        from unittest.mock import Mock

        from docker.errors import APIError

        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        clone_dir = repos_root / "owner" / "repo"
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.reason = "Internal Server Error"

        mock_container = MagicMock()
        mock_container.put_archive.side_effect = APIError(
            "500 Server Error", mock_response, explanation="failed"
        )

        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            with pytest.raises(RuntimeError, match="Failed to copy repo"):
                _clone_shiori_repo_to_container(
                    mock_container, "abc123", "owner/repo", "/tmp/repo"
                )

    def test_repo_name_in_path(self, tmp_path: Path) -> None:
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        clone_dir = repos_root / "owner" / "myrepo"
        clone_dir.mkdir(parents=True)
        (clone_dir / ".git").mkdir()
        (clone_dir / "README.md").write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_container.exec_run.return_value = (
            0,
            (b"remote: Enumerating objects: 42, done.\n", b""),
        )

        with patch(
            "code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", str(repos_root)
        ):
            result = _clone_shiori_repo_to_container(
                mock_container, "abc123", "owner/myrepo", "/tmp/repo"
            )

        assert "Copied Shiori clone" in result
        assert "/tmp/repo/myrepo" in result
        mock_container.put_archive.assert_called_once()


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
    """Tests for clone_repo tool endpoint."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_successful_clone(self, mock_record, mock_docker):
        """Successful clone returns ok with clone_path."""
        container = _make_container([
            (0, b"", b""),  # gh auth setup-git
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
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
            (0, b"", b""),
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
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
            (0, b"", b""),
            (1, b"", b"fatal: repository not found\n"),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        result = json.loads(clone_repo("abc123def456", "owner/nonexistent"))
        assert result["status"] == "error"
        assert "repository not found" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_with_custom_dest(self, mock_docker):
        """Clone with custom dest_dir computes correct clone_path."""
        container = _make_container([
            (0, b"", b""),
            (0, b"", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        result = json.loads(
            clone_repo("abc123def456", "owner/mytool", dest_dir="/tmp/work")
        )
        assert result["clone_path"] == "/tmp/work/mytool"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_targets_repo_subdir(self, mock_docker):
        """Issue #131: gh clones into {dest_dir}/{repo_name}, not dest_dir."""
        container = _make_container([
            (0, b"", b""),
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        clone_repo("abc123def456", "owner/mytool")

        cmd = container.exec_run.call_args[0][0][-1]
        assert "/home/sandbox/mytool" in cmd
        assert "gh repo clone 'owner/mytool' '/home/sandbox'" not in cmd

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_clone_existing_dir_adds_hint(self, mock_docker):
        """Issue #131: 'already exists' failures get an actionable hint."""
        container = _make_container([
            (0, b"", b""),
            (
                1,
                b"",
                b"fatal: destination path '/home/sandbox/mytool' already "
                b"exists and is not an empty directory.\n",
            ),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "error"
        assert "Hint:" in result["error"]
        assert "dest_dir" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_clone_succeeds_when_auth_setup_fails(self, mock_record, mock_docker):
        """gh auth setup-git failure is ignored; clone still proceeds."""
        container = _make_container([
            (1, b"", b"gh: not logged in\n"),
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "ok"
        assert result["clone_path"] == "/home/sandbox/mytool"


class TestCloneRepoViaNetwork:
    """Tests for _clone_repo_via_network (Issue #146, PR #170 review)."""

    def _container(self, exit_code: int, output: bytes) -> MagicMock:
        c = MagicMock()
        c.id = "abc123def456"
        c.exec_run.return_value = (exit_code, output)
        return c

    def test_success_returns_message(self) -> None:
        c = self._container(0, b"")
        msg = _clone_repo_via_network(c, "abc123def456", "owner/repo", "/tmp/repo")
        assert "owner/repo" in msg
        assert "/tmp/repo/repo" in msg

    def test_failure_without_token_hints_inject_vcs_token(self) -> None:
        c = self._container(1, b"gh: Could not resolve to a Repository")
        with pytest.raises(RuntimeError) as exc:
            _clone_repo_via_network(c, "abc123def456", "owner/private", "/tmp/repo")
        assert "inject_vcs_token=True" in str(exc.value)

    def test_failure_with_token_omits_hint(self) -> None:
        c = self._container(1, b"some other gh error")
        with pytest.raises(RuntimeError) as exc:
            _clone_repo_via_network(
                c, "abc123def456", "owner/private", "/tmp/repo",
                inject_vcs_token=True,
            )
        assert "inject_vcs_token=True" not in str(exc.value)

    def test_anonymous_git_clone_without_token(self) -> None:
        """Issue #333: no token -> anonymous git clone (public works)."""
        c = self._container(0, b"")
        _clone_repo_via_network(c, "abc123def456", "owner/repo", "/tmp/repo")
        cmd = c.exec_run.call_args_list[0][0][0][-1]
        assert "git clone" in cmd
        assert "https://github.com/owner/repo.git" in cmd
        assert "GIT_TERMINAL_PROMPT=0" in cmd
        assert "gh repo clone" not in cmd

    def test_gh_clone_with_token(self) -> None:
        """Issue #333: inject_vcs_token=True keeps gh repo clone (private)."""
        c = self._container(0, b"")
        _clone_repo_via_network(
            c, "abc123def456", "owner/repo", "/tmp/repo",
            inject_vcs_token=True,
        )
        cmd = c.exec_run.call_args_list[0][0][0][-1]
        assert "gh repo clone owner/repo" in cmd


class TestCloneRepoTransportSelection:
    """Issue #333: clone_repo picks gh vs anonymous git by token presence."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_anonymous_when_setup_git_fails(self, mock_record, mock_docker) -> None:
        container = _make_container([
            (1, b"", b"gh: not logged in\n"),
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "ok"
        cmd = container.exec_run.call_args[0][0][-1]
        assert "GIT_TERMINAL_PROMPT=0 git clone" in cmd
        assert "https://github.com/owner/mytool.git" in cmd
        assert "gh repo clone" not in cmd
        assert "inject_vcs_token=True" in result["warning"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_gh_when_setup_git_succeeds(self, mock_record, mock_docker) -> None:
        container = _make_container([
            (0, b"", b""),
            (0, b"Cloning into 'mytool'...\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        from code_sandbox_mcp.server import clone_repo
        result = json.loads(clone_repo("abc123def456", "owner/mytool"))
        assert result["status"] == "ok"
        cmd = container.exec_run.call_args[0][0][-1]
        assert "gh repo clone owner/mytool" in cmd
        assert "warning" not in result


class TestCloneWarnsWithoutToken:
    """Issue #333 follow-up: warn at clone time when no token (push fails)."""

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists",
           return_value=False)
    def test_network_clone_without_token_warns(self, _mock_pre) -> None:
        from code_sandbox_mcp.tools.container import _try_clone_into_container
        c = MagicMock()
        c.exec_run.return_value = (0, b"")
        res = _try_clone_into_container(
            c, "abc123def456", "owner/repo", "/tmp/repo"
        )
        assert res.error is None
        assert "WARNING" in res.msg
        assert "inject_vcs_token=True" in res.msg

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists",
           return_value=False)
    def test_network_clone_with_token_no_warning(self, _mock_pre) -> None:
        from code_sandbox_mcp.tools.container import _try_clone_into_container
        c = MagicMock()
        c.exec_run.return_value = (0, b"")
        res = _try_clone_into_container(
            c, "abc123def456", "owner/repo", "/tmp/repo",
            inject_vcs_token=True,
        )
        assert res.error is None
        assert "WARNING" not in res.msg
