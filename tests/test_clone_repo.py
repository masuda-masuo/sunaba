"""Tests for Shiori clone_repo functionality (Issue #84)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.tools.container import (
    _clone_repo_via_network,
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
    
    

class TestSandboxInitializeCloneRepo:
    """Tests for sandbox_initialize with clone_repo."""

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_clone_repo_calls_helper(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
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

    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
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

    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
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

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_clone_dest_custom(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
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

    @patch("code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", None)
    @patch("code_sandbox_mcp.tools.container._clone_repo_via_network")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_network_fallback_when_shiori_not_configured(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_shiori_clone: MagicMock,
        mock_net_clone: MagicMock,
    ) -> None:
        # When Shiori is NOT configured and the Shiori copy raises
        # ValueError, sandbox_initialize should use the network fallback
        # too (mirrors the run_container_and_exec path, Issue #146).
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_shiori_clone.side_effect = ValueError(
            "Shiori repos path is not configured"
        )
        mock_net_clone.return_value = (
            "Cloned owner/repo via network into /tmp/repo/repo"
            " in container abc123def456"
        )

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert result.startswith("abc123def456")
        assert "clone_repo failed" not in result
        assert "via network" in result
        mock_net_clone.assert_called_once()


class TestRunContainerAndExecCloneRepo:
    """Tests for run_container_and_exec with clone_repo."""

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_clone_repo_called_before_exec(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
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

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", "/some/repos")
    def test_clone_error_reported_in_result(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        # When Shiori IS configured, pre-clone exists, but the copy step
        # fails with ValueError, the error is reported as clone_warning
        # (Issue #84).  _shiori_preclone_exists is stubbed True so that
        # _try_clone_into_container does not attempt the network fallback.
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

    @patch("code_sandbox_mcp.tools.container._SHIORI_REPOS_PATH", None)
    @patch("code_sandbox_mcp.tools.container._clone_repo_via_network")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_network_fallback_when_shiori_not_configured(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_shiori_clone: MagicMock,
        mock_net_clone: MagicMock,
    ) -> None:
        # When Shiori is NOT configured and _clone_shiori_repo_to_container
        # raises ValueError, the network fallback should be used (Issue #146).
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_shiori_clone.side_effect = ValueError("Shiori repos path is not configured")
        mock_net_clone.return_value = "Cloned owner/repo via network"

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
        ))

        assert result["status"] == "ok"
        assert "clone_warning" not in result
        mock_net_clone.assert_called_once()

    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
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


    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=False)
    @patch("code_sandbox_mcp.tools.container._clone_repo_via_network")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_preclone_absent_falls_back_to_network(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_shiori_clone: MagicMock,
        mock_net_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        # When _shiori_preclone_exists returns False (pre-clone absent),
        # _try_clone_into_container should fall back to network clone
        # (Issue #178).  The helper is mocked directly for robustness.
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_shiori_clone.side_effect = ValueError("Repository clone not found: /some/repos/owner/repo")
        mock_net_clone.return_value = "Cloned owner/repo via network into /tmp/repo/repo in container abc123def456"

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
        ))

        assert result["status"] == "ok"
        assert "clone_warning" not in result
        mock_net_clone.assert_called_once()

class TestRunContainerAndExecTimeout:
    """Tests for run_container_and_exec with timeout (Issue #138)."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_timeout_applied_in_cmd(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """timeout=N should wrap the script with timeout(1)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"ok", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo ok"],
            timeout=5,
        ))

        assert result["status"] == "ok"
        cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "timeout 5" in cmd

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_timeout_zero_not_applied(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """timeout=0 (default) does not wrap command with timeout(1)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"ok", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo ok"],
        ))

        assert result["status"] == "ok"
        cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "timeout" not in cmd

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_timeout_status_on_exit_124(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Issue #138: timeout=N returns status 'timeout' when exit_code is 124."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (124, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["sleep 60"],
            timeout=5,
        ))

        assert result["status"] == "timeout"
        assert result["exit_code"] == 124

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_exit_124_without_timeout_is_error(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """exit_code=124 without timeout set is status 'error', not 'timeout'."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (124, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["exit 124"],
        ))

        assert result["status"] == "error"

    def test_negative_timeout_returns_error(self) -> None:
        """timeout < 0 is rejected immediately with a clear error."""
        result = json.loads(run_container_and_exec(
            commands=["echo ok"],
            timeout=-1,
        ))

        assert result["status"] == "error"
        assert "timeout" in result["error"]


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
        # Private repos fail without a token; the error must guide the user
        # to opt in with inject_vcs_token=True (token is not auto-injected).
        c = self._container(1, b"gh: Could not resolve to a Repository")
        with pytest.raises(RuntimeError) as exc:
            _clone_repo_via_network(c, "abc123def456", "owner/private", "/tmp/repo")
        assert "inject_vcs_token=True" in str(exc.value)

    def test_failure_with_token_omits_hint(self) -> None:
        # When the token was already injected the hint would be misleading,
        # so it must be suppressed.
        c = self._container(1, b"some other gh error")
        with pytest.raises(RuntimeError) as exc:
            _clone_repo_via_network(
                c, "abc123def456", "owner/private", "/tmp/repo",
                inject_vcs_token=True,
            )
        assert "inject_vcs_token=True" not in str(exc.value)
