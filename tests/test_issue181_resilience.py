"""Tests for Issue #181: resilience against unhealthy/wedged containers.

Covers:
- ``_docker()`` per-request timeout plumbing.
- ``sandbox_stop`` force-kill + force-remove recovery path.
- ``sandbox_initialize`` ``mem_limit`` / ``cpus`` resource overrides.
- ``sandbox_exec_check`` using the short recovery timeout.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from docker.errors import APIError, NotFound

from code_sandbox_mcp.tools.common import RECOVERY_DOCKER_TIMEOUT, _docker
from code_sandbox_mcp.tools.container import sandbox_initialize, sandbox_stop
from code_sandbox_mcp.tools.exec import sandbox_exec_check

_IMG = "python@sha256:" + "0" * 64


class TestDockerTimeout:
    """_docker() forwards the per-request timeout only when given."""

    def test_default_no_timeout(self) -> None:
        with patch("docker.from_env") as mock_from_env:
            _docker()
        mock_from_env.assert_called_once_with()

    def test_explicit_timeout(self) -> None:
        with patch("docker.from_env") as mock_from_env:
            _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
        mock_from_env.assert_called_once_with(timeout=RECOVERY_DOCKER_TIMEOUT)


class TestSandboxStopForceKill:
    """sandbox_stop kills + force-removes instead of graceful stop."""

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_kill_then_force_remove(self, mock_docker: MagicMock, mock_record: MagicMock) -> None:
        container = MagicMock()
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        result = sandbox_stop("abc123def456")

        mock_docker.assert_called_once_with(timeout=RECOVERY_DOCKER_TIMEOUT)
        container.kill.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        container.stop.assert_not_called()
        mock_record.assert_called_once()
        assert "stopped and removed" in result

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_kill_apierror_still_removes(self, mock_docker: MagicMock, mock_record: MagicMock) -> None:
        container = MagicMock()
        container.kill.side_effect = APIError("not running")
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        result = sandbox_stop("abc123def456")

        container.remove.assert_called_once_with(force=True)
        assert "stopped and removed" in result

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_not_found(self, mock_docker: MagicMock) -> None:
        client = MagicMock()
        client.containers.get.side_effect = NotFound("nope")
        mock_docker.return_value = client

        result = sandbox_stop("abc123def456")
        assert result.startswith("Error: container")


class TestSandboxInitializeResources:
    """sandbox_initialize honours mem_limit / cpus overrides."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_mem_and_cpus_passed(
        self,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        container = MagicMock()
        container.id = "abc123def456"
        client = MagicMock()
        client.containers.run.return_value = container
        mock_docker.return_value = client

        result = sandbox_initialize(image=_IMG, mem_limit="2g", cpus=2.0)

        assert result == "abc123def456"
        kwargs = client.containers.run.call_args.kwargs
        assert kwargs["mem_limit"] == "2g"
        assert kwargs["memswap_limit"] == "2g"
        # 2.0 cores * default cpu_period (100000us)
        assert kwargs["cpu_quota"] == 200000

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_defaults_unchanged(
        self,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        container = MagicMock()
        container.id = "abc123def456"
        client = MagicMock()
        client.containers.run.return_value = container
        mock_docker.return_value = client

        sandbox_initialize(image=_IMG)

        # These expected values mirror DEFAULT_SECURITY_PROFILE in
        # container.py (mem_limit="512m", cpu_period=100000 → 0.5
        # cores = 50000).  If the profile defaults change, update
        # these assertions accordingly.
        kwargs = client.containers.run.call_args.kwargs
        assert kwargs["mem_limit"] == "512m"
        assert kwargs["memswap_limit"] == "512m"
        assert kwargs["cpu_quota"] == 50000

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_invalid_cpus_rejected(
        self,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        client = MagicMock()
        mock_docker.return_value = client

        result = sandbox_initialize(image=_IMG, cpus=0)

        assert result.startswith("Error: cpus must be > 0")
        client.containers.run.assert_not_called()


class TestExecCheckTimeout:
    """sandbox_exec_check polls with the short recovery timeout."""

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_uses_recovery_timeout(self, mock_docker: MagicMock) -> None:
        container = MagicMock()
        # Exit-code file absent -> job still running, no further calls.
        container.exec_run.return_value = (0, b"not_found")
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        result = sandbox_exec_check("abc123def456", "job-1")

        mock_docker.assert_called_once_with(timeout=RECOVERY_DOCKER_TIMEOUT)
        assert result == "running"
