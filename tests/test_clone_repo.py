"""Tests for Shiori clone_repo functionality (Issue #84)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.server import (
    _clone_shiori_repo_to_container,
    _validate_clone_repo,
    run_container_and_exec,
    sandbox_initialize,
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
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", "/data/repos"
        ):
            with pytest.raises(ValueError, match="must start with /tmp/"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/etc/repo"
                )

    def test_no_shiori_repos_path_configured(self) -> None:
        with patch(
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", None
        ):
            with pytest.raises(ValueError, match="not configured"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/tmp/repo"
                )

    def test_repos_root_not_found(self) -> None:
        with patch(
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", "/nonexistent/path"
        ):
            with pytest.raises(ValueError, match="not found"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "owner/repo", "/tmp/repo"
                )

    def test_path_traversal_prevented_by_validate(self) -> None:
        """Path traversal via '../' is caught by _validate_clone_repo format check."""
        with patch(
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", "/data/repos"
        ):
            with pytest.raises(ValueError, match="owner/name"):
                _clone_shiori_repo_to_container(
                    MagicMock(), "abc123", "../escape/repo", "/tmp/repo"
                )

    def test_clone_not_found(self, tmp_path: Path) -> None:
        repos_root = tmp_path / "repos"
        repos_root.mkdir()
        with patch(
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", str(repos_root)
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
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", str(repos_root)
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
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", str(repos_root)
        ):
            result = _clone_shiori_repo_to_container(
                mock_container, "abc123", "owner/repo", "/tmp/repo"
            )

        assert "Copied Shiori clone" in result
        assert "/tmp/repo/repo" in result
        mock_container.put_archive.assert_called_once()
        mock_container.exec_run.assert_called_once()

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
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", str(repos_root)
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
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", str(repos_root)
        ):
            result = _clone_shiori_repo_to_container(
                mock_container, "abc123", "owner/repo", "/tmp/repo"
            )

        assert "Copied Shiori clone" in result

    def test_put_archive_failure(self, tmp_path: Path) -> None:
        from docker.errors import APIError
        from unittest.mock import Mock

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
            "code_sandbox_mcp.server._SHIORI_REPOS_PATH", str(repos_root)
        ):
            with pytest.raises(RuntimeError, match="Failed to copy repo"):
                _clone_shiori_repo_to_container(
                    mock_container, "abc123", "owner/repo", "/tmp/repo"
                )


class TestSandboxInitializeCloneRepo:
    """Tests for sandbox_initialize with clone_repo."""

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._ensure_image")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_clone_repo_calls_helper(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Copied Shiori clone of owner/repo → /tmp/repo/repo in container abc123def456"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert "abc123def456" in result
        assert "Copied Shiori clone" in result
        mock_clone.assert_called_once_with(
            mock_container, "abc123def456", "owner/repo", "/tmp/repo",
        )

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._ensure_image")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_clone_repo_failure_non_fatal(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.side_effect = ValueError("clone not found")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert result.startswith("abc123def456")
        assert "clone_repo failed" in result

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._ensure_image")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_without_clone_repo_works_normally(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )

        assert result == "abc123def456"
        mock_clone.assert_not_called()

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._ensure_image")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_clone_dest_custom(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Copied Shiori clone..."

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            clone_dest="/tmp/proj",
        )

        mock_clone.assert_called_once_with(
            mock_container, "abc123def456", "owner/repo", "/tmp/proj",
        )


class TestRunContainerAndExecCloneRepo:
    """Tests for run_container_and_exec with clone_repo."""

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_clone_repo_called_before_exec(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        parent_mock = MagicMock()
        parent_mock.attach_mock(mock_clone, "clone")
        parent_mock.attach_mock(mock_container.exec_run, "exec_run")

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
        ))

        assert result["status"] == "ok"
        mock_clone.assert_called_once()

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_clone_error_reported_in_result(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.side_effect = ValueError("path not found")

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
        ))

        assert result["status"] == "ok"
        assert result["clone_warning"] == "path not found"

    @patch("code_sandbox_mcp.server._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.validate_image_ref")
    def test_without_clone_repo_normally(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
        ))

        assert result["status"] == "ok"
        mock_clone.assert_not_called()
        assert "clone_warning" not in result


class TestShioriReposPathArg:
    """Tests for --shiori-repos-path CLI argument."""

    def test_default_is_none(self) -> None:
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args([])
        assert args.shiori_repos_path is None

    def test_explicit_arg(self) -> None:
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args(["--shiori-repos-path", "/data/repos"])
        assert args.shiori_repos_path == "/data/repos"

    def test_env_var_default(self) -> None:
        import os
        from code_sandbox_mcp.server import _build_arg_parser
        with patch.dict(os.environ, {"SHIORI_REPOS_PATH": "/custom/repos"}):
            parser = _build_arg_parser()
            args = parser.parse_args([])
            assert args.shiori_repos_path == "/custom/repos"
