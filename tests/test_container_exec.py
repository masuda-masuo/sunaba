"""Tests for run_container_and_exec with clone_repo, pip_extras, and timeout."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from code_sandbox_mcp.tools.container import run_container_and_exec


class TestRunContainerAndExecCloneRepo:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
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
            allow_network=True,
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
            allow_network=True,
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
            allow_network=True,
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
            allow_network=True,
        ))

        assert result["status"] == "ok"
        assert "clone_warning" not in result
        mock_net_clone.assert_called_once()


class TestRunContainerAndExecPipExtras:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for pip_extras with clone_repo in run_container_and_exec (Issue #245)."""

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_extras_none_skips_install(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            pip_extras=None,
        ))

        assert result["status"] == "ok"
        assert mock_container.exec_run.call_count == 1

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_default_pip_extras_installs_dev(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"output", b"")),
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=True,
        ))

        assert result["status"] == "ok"
        assert mock_container.exec_run.call_count == 2
        first_cmd = mock_container.exec_run.call_args_list[0][0][0][-1]
        assert "pip install -e '.[dev]' -q" in first_cmd

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_extras_skipped_without_network(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        """without network access pip can't reach PyPI, so the
        install must be skipped rather than hang until pip's own timeout."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=False,
        ))

        assert result["status"] == "ok"
        # Only the user command runs — no pip install exec call.
        assert mock_container.exec_run.call_count == 1

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_custom_pip_extras(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"output", b"")),
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            pip_extras="[test]",
            allow_network=True,
        ))

        first_cmd = mock_container.exec_run.call_args_list[0][0][0][-1]
        assert "pip install -e '.[test]' -q" in first_cmd

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_install_failure_non_fatal(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (1, (b"", b"ERROR")),
            (0, (b"output", b"")),
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=True,
        ))

        assert result["status"] == "ok"
        assert "clone_warning" not in result

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_clone_failure_skips_pip_install(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.side_effect = ValueError("clone not found")

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=True,
        ))

        assert result["status"] == "ok"
        assert result["clone_warning"] == "clone not found"
        assert mock_container.exec_run.call_count == 1


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


class TestPipArgs:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for pip_args propagation through _run_pip_install."""

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._try_clone_into_container", return_value=("cloned", None))
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_args_passed_to_exec_run(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        container = MagicMock()
        container.id = "abc123def456"
        container.exec_run.return_value = (0, (b"output", b""))
        client = MagicMock()
        client.containers.run.return_value = container
        mock_docker.return_value = client

        run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=True,
            pip_args="--index-url https://example.com",
        )

        # Find the exec_run call that contains the pip install command
        pip_calls = [
            call for call in container.exec_run.call_args_list
            if "pip install" in str(call)
        ]
        assert pip_calls, "pip install should be called via exec_run"
        call_str = str(pip_calls[0])
        assert "--index-url" in call_str
        assert "https://example.com" in call_str

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._try_clone_into_container", return_value=("cloned", None))
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pip_args_none_omitted(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_preclone_exists: MagicMock,
    ) -> None:
        container = MagicMock()
        container.id = "abc123def456"
        container.exec_run.return_value = (0, (b"output", b""))
        client = MagicMock()
        client.containers.run.return_value = container
        mock_docker.return_value = client

        run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=True,
        )

        pip_calls = [
            call for call in container.exec_run.call_args_list
            if "pip install" in str(call)
        ]
        assert pip_calls, "pip install should be called via exec_run"
        call_str = str(pip_calls[0])
        # No extra --index-url or double-space artifacts
        assert "--index-url" not in call_str
        assert "  -q" not in call_str
