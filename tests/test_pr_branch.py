"""Tests for _setup_pr_branch and sandbox_initialize/run_container_and_exec with pr parameter (Issue #136)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.tools.container import (
    _setup_pr_branch,
    run_container_and_exec,
    sandbox_initialize,
)


def _make_container_mock(exec_returns: list[tuple[int, tuple[bytes, bytes]]]):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, (stdout, stderr) in exec_returns
    ]
    return container


_PR_INFO_JSON = json.dumps({
    "headRefName": "feature-branch",
})


class TestSetupPrBranch:
    """Tests for _setup_pr_branch."""

    def test_success(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned repo\n", b"")),
            (0, (b"Switched to branch\n", b"")),
            (0, (b"Installed\n", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

        assert "PR #136" in result
        assert "feature-branch" in result
        assert "/tmp/repo" in result
        assert "abc123" in result

    def test_gh_view_failure(self):
        container = _make_container_mock([
            (1, (b"", b"not found")),
        ])

        with pytest.raises(RuntimeError, match="Failed to fetch PR"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 999, "/tmp/repo",
            )

    def test_clone_failure(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (1, (b"", b"Repository not found")),
        ])

        with pytest.raises(RuntimeError, match="Failed to clone"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

    def test_checkout_failure(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (1, (b"", b"checkout failed")),
        ])

        with pytest.raises(RuntimeError, match="Failed to checkout"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

    def test_install_failure_non_fatal(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (0, (b"Switched\n", b"")),
            (1, (b"", b"install failed")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger") as mock_logger:
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

        assert "PR #136" in result
        mock_logger.warning.assert_called_once()
        assert "pip install deps failed" in mock_logger.warning.call_args[0][0]

    def test_invalid_json_from_gh(self):
        container = _make_container_mock([
            (0, (b"not valid json", b"")),
        ])

        with pytest.raises(RuntimeError, match="Failed to parse PR info JSON"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

    def test_incomplete_pr_info(self):
        incomplete = json.dumps({"other_field": "value"})
        container = _make_container_mock([
            (0, (incomplete.encode(), b"")),
        ])

        with pytest.raises(RuntimeError, match="Incomplete PR info"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )


    def test_repo_name_in_path(self):
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = [
            (0, (b'{"headRefName":"feature-branch"}', b"")),
            (0, (b"Cloning into '/tmp/repo/myrepo'...", b"")),
            (0, (b"Switched to branch 'feature-branch'", b"")),
            (0, (b"Installed", b"")),
        ]
        with patch("code_sandbox_mcp.tools.container.record_copy"):
            result = _setup_pr_branch(
                mock_container,
                "abc123def456",
                "owner/myrepo",
                42,
                "/tmp/repo",
            )
        assert "PR #42" in result
        assert "/tmp/repo/myrepo" in result


class TestSandboxInitializePrParam:
    """Tests for sandbox_initialize with pr parameter."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._container_env")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_calls_setup(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_container_env: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.return_value = "PR #136 (feature) \u2192 /tmp/repo/repo in container abc123"
        mock_container_env.return_value = {"GITHUB_TOKEN": "fake-token"}

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            repo="owner/repo",
            pr=136,
            allow_network=False,
            inject_vcs_token=False,
        )

        assert "abc123def456" in result
        assert "PR #136" in result
        mock_setup.assert_called_once()
        args = mock_setup.call_args[0]
        assert args[2] == "owner/repo"
        assert args[3] == 136
        run_kwargs = mock_client.containers.run.call_args[1]
        assert run_kwargs.get("environment", {}).get("GITHUB_TOKEN") == "fake-token"

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pr_without_repo_returns_warning(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            pr=136,
        )

        assert "abc123def456" in result
        assert "pr setup failed" in result
        assert "repo is required" in result

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_setup_failure_non_fatal(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.side_effect = RuntimeError("network error")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            repo="owner/repo",
            pr=136,
        )

        assert result.startswith("abc123def456")
        assert "pr setup failed" in result

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_without_pr_works_normally(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )

        assert result == "abc123def456"


class TestRunContainerAndExecPrParam:
    """Tests for run_container_and_exec with pr parameter."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_calls_setup(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            repo="owner/repo",
            pr=136,
        ))

        assert result["status"] == "ok"
        mock_setup.assert_called_once_with(
            mock_container, "abc123def456", "owner/repo", 136, "/tmp/repo", "[dev]",
        )

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pr_without_repo_returns_warning(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            pr=136,
        ))

        assert result["status"] == "ok"
        assert result["pr_warning"] == "repo is required when pr is specified"

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_error_reported(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.side_effect = RuntimeError("network error")

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            repo="owner/repo",
            pr=136,
        ))

        assert result["status"] == "ok"
        assert result["pr_warning"] == "network error"



class TestPipExtrasParam:
    """Tests for pip_extras parameter customization."""

    def test_custom_pip_extras(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (0, (b"Switched\n", b"")),
            (0, (b"Installed\n", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                pip_extras="[testing]",
            )

        assert "PR #136" in result
        # The editable install must target the repo dir ("."), i.e.
        # `pip install -e '.[testing]'` -- NOT `pip install -e '[testing]'`,
        # which is an invalid spec that silently no-ops so the dev install
        # never takes effect.
        install_cmd = container.exec_run.call_args_list[3][0][0][2]
        assert "pip install -e '.[testing]'" in install_cmd

    def test_pip_extras_none_skips_install(self):
        # 3 exec calls: gh view, clone, checkout. No pip install.
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (0, (b"Switched\n", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                pip_extras=None,
            )

        assert "PR #136" in result
        # Should be exactly 3 exec calls (no pip install)
        assert container.exec_run.call_count == 3


class TestCloneRepoPrInteraction:
    """Tests for clone_repo + pr interaction."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._container_env")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    def test_clone_repo_skipped_when_pr_set(
        self,
        mock_clone_shiori: MagicMock,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_container_env: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.return_value = "PR #136 setup done"
        mock_container_env.return_value = {}

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            repo="owner/repo",
            pr=136,
        )

        # _clone_shiori_repo_to_container should NOT be called
        mock_clone_shiori.assert_not_called()
        # _setup_pr_branch SHOULD be called
        mock_setup.assert_called_once()

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._container_env")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    def test_clone_repo_called_when_pr_not_set(
        self,
        mock_clone_shiori: MagicMock,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_container_env: MagicMock,
        mock_docker: MagicMock,
        mock_preclone_exists: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container_env.return_value = {}

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        # _clone_shiori_repo_to_container SHOULD be called
        mock_clone_shiori.assert_called_once()
        # _setup_pr_branch should NOT be called
        mock_setup.assert_not_called()
