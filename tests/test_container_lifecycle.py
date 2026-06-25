"""Tests for container lifecycle: sandbox_initialize + run_container_and_exec with clone_repo, pip_extras, timeout."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.container import (
    run_container_and_exec,
    sandbox_initialize,
)


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
        mock_container.exec_run.return_value = (0, (b"", b""))
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
        assert "pip install" not in result
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
        mock_container.exec_run.return_value = (0, (b"", b""))
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
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"", b""))
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


class TestSandboxInitializeCloneRepoPipExtras:
    """Tests for pip_extras with clone_repo (Issue #245)."""

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_extras_none_skips_install(
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
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            pip_extras=None,
        )

        assert "abc123def456" in result
        assert mock_container.exec_run.call_count == 0

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_extras_default_installs_dev(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"Installed", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert "abc123def456" in result
        assert mock_container.exec_run.call_count == 1
        call_cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "pip install -e '.[dev]' -q" in call_cmd

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_extras_custom_value(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            pip_extras="[test]",
        )

        call_cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "pip install -e '.[test]' -q" in call_cmd

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_install_failure_non_fatal(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (1, (b"", b"ERROR"))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert "abc123def456" in result
        assert "clone_repo failed" not in result
        assert "pip install" not in result

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_clone_failure_skips_pip_install(
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
        mock_clone.side_effect = ValueError("clone not found")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert "clone_repo failed" in result
        assert mock_container.exec_run.call_count == 0
