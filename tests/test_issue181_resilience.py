"""Tests for Issue #181: resilience against unhealthy/wedged containers.

Covers:
- ``_docker()`` per-request timeout plumbing.
- ``sandbox_stop`` force-kill + force-remove recovery path.
- ``sandbox_initialize`` ``mem_limit`` / ``cpus`` resource overrides.
- ``sandbox_exec_check`` using the short recovery timeout.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from docker.errors import APIError, NotFound

from code_sandbox_mcp.tools.common import RECOVERY_DOCKER_TIMEOUT, _docker, _recovery_timeout_from_env
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
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_kill_then_force_remove(
        self,
        mock_docker: MagicMock,
        mock_docker_vcs: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        # checkpoint_list container: unpushed checkpoints empty
        vcs_container = MagicMock()
        vcs_container.exec_run.return_value = (0, b"")
        vcs_client = MagicMock()
        vcs_client.containers.get.return_value = vcs_container
        mock_docker_vcs.return_value = vcs_client

        result = sandbox_stop("abc123def456")

        mock_docker.assert_called_once_with(timeout=RECOVERY_DOCKER_TIMEOUT)
        container.kill.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        container.stop.assert_not_called()
        mock_record.assert_called_once()
        assert "stopped and removed" in result

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_kill_apierror_still_removes(
        self,
        mock_docker: MagicMock,
        mock_docker_vcs: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        container.kill.side_effect = APIError("not running")
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        # checkpoint_list container: unpushed checkpoints empty
        vcs_container = MagicMock()
        vcs_container.exec_run.return_value = (0, b"")
        vcs_client = MagicMock()
        vcs_client.containers.get.return_value = vcs_container
        mock_docker_vcs.return_value = vcs_client

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
        # sandbox_exec_check makes multiple exec_run calls:
        #   1. date +%s          → epoch timestamp
        #   2. cat .start        → empty (no start file for old jobs)
        #   3. stat .out/.err    → "0" (no output files yet)
        #   4. cat .exit         → "not_found" (job still running)
        container.exec_run.side_effect = [
            (0, b"1700000000"),
            (0, b""),
            (0, b"0"),
            (0, b"not_found"),
        ]
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        result = sandbox_exec_check("abc123def456", "job-1")

        mock_docker.assert_called_once_with(timeout=RECOVERY_DOCKER_TIMEOUT)
        parsed = json.loads(result)
        assert parsed["status"] == "running"
        assert parsed["elapsed_seconds"] is None
        assert parsed["last_output_seconds_ago"] is None


class TestRecoveryTimeoutConfigurable:
    """RECOVERY_DOCKER_TIMEOUT honours the env override (Issue #181 follow-up).

    The merged #199 hard-coded 15s.  Operators on slow hosts may need to
    tune it, so it is now read from ``CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT``
    with a safe fallback.  See docs/issue-181-followup.md.
    """

    def test_default_when_unset(self, monkeypatch) -> None:
        monkeypatch.delenv("CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT", raising=False)
        assert _recovery_timeout_from_env() == 15.0

    def test_env_override(self, monkeypatch) -> None:
        monkeypatch.setenv("CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT", "3.5")
        assert _recovery_timeout_from_env() == 3.5

    def test_invalid_or_nonpositive_falls_back(self, monkeypatch) -> None:
        for bad in ("not-a-number", "0", "-5"):
            monkeypatch.setenv("CODE_SANDBOX_RECOVERY_DOCKER_TIMEOUT", bad)
            assert _recovery_timeout_from_env() == 15.0


class TestSandboxStopUnpushedCheckpoints:
    """sandbox_stop warns about unpushed checkpoints unless force=True."""

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_warns_without_force(
        self,
        mock_docker: MagicMock,
        mock_docker_vcs: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        """Early-return path: does not call record_stop."""
        container = MagicMock()
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        # checkpoint_list's container: return a checkpoint entry
        vcs_container = MagicMock()
        vcs_container.exec_run.return_value = (
            0, b"abc1234 2024-01-01T00:00:00+00:00 my checkpoint"
        )
        vcs_client = MagicMock()
        vcs_client.containers.get.return_value = vcs_container
        mock_docker_vcs.return_value = vcs_client

        result = sandbox_stop("abc123def456")

        assert "unpushed checkpoint" in result
        assert "force=True" in result
        container.kill.assert_not_called()
        container.remove.assert_not_called()
        mock_record.assert_not_called()

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_force_skips_warning(self, mock_docker: MagicMock, mock_record: MagicMock) -> None:
        container = MagicMock()
        container.exec_run.return_value = (
            0, b"abc1234 2024-01-01 my checkpoint"
        )
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        result = sandbox_stop("abc123def456", force=True)

        container.kill.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        assert "stopped and removed" in result

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_no_unpushed_proceeds(
        self,
        mock_docker: MagicMock,
        mock_docker_vcs: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        container = MagicMock()
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        vcs_container = MagicMock()
        vcs_container.exec_run.return_value = (0, b"")
        vcs_client = MagicMock()
        vcs_client.containers.get.return_value = vcs_container
        mock_docker_vcs.return_value = vcs_client

        result = sandbox_stop("abc123def456")

        container.kill.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        assert "stopped and removed" in result

    @patch("code_sandbox_mcp.tools.container.record_stop")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_no_git_proceeds(
        self,
        mock_docker: MagicMock,
        mock_docker_vcs: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        container = MagicMock()
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        vcs_container = MagicMock()
        vcs_container.exec_run.return_value = (128, b"fatal: not a git repository")
        vcs_client = MagicMock()
        vcs_client.containers.get.return_value = vcs_container
        mock_docker_vcs.return_value = vcs_client

        result = sandbox_stop("abc123def456")

        container.kill.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        assert "stopped and removed" in result
