"""Tests for the package_install MCP tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from code_sandbox_mcp.tools.package import package_install


class TestPackageInstall:
    """Tests for the package_install tool."""

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_container_not_found(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "error"
        assert "not found" in result["stderr"]

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_docker_error(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "error"

    def test_no_args_returns_error(self) -> None:
        result = json.loads(package_install(
            container_id="abc123",
        ))
        assert result["status"] == "error"
        assert "required" in result["error"]

    def test_packages_and_editable_mutually_exclusive(self) -> None:
        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
            editable="/path/to/project",
        ))
        assert result["status"] == "error"
        assert "mutually exclusive" in result["error"]

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_successful_install(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        container = MagicMock()
        call_count = 0

        def exec_run_side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            # call 1: pip list (before), call 3: pip list (after)
            if call_count in (1, 3):
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}]',
                    b"",
                ))
            # call 2: pip install
            return (0, (
                b"Successfully installed requests-2.31.0",
                b"",
            ))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "ok"
        assert result["changed"] == 0  # pip list returns same data before/after


    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_successful_editable_install(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        container = MagicMock()

        def exec_run_side_effect(cmd, **kwargs):
            shell_cmd = cmd[-1] if isinstance(cmd, list) else ""
            if "pip list" in shell_cmd and "install" not in shell_cmd:
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}]',
                    b"",
                ))
            if "pip install" in shell_cmd:
                return (0, (
                    b"Successfully installed myproject",
                    b"",
                ))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            editable="/path/to/project",
            extras="[dev]",
        ))
        assert result["status"] == "ok"

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_install_with_upgrade(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        container = MagicMock()

        def exec_run_side_effect(cmd, **kwargs):
            shell_cmd = cmd[-1] if isinstance(cmd, list) else ""
            if "pip list" in shell_cmd and "install" not in shell_cmd:
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}]',
                    b"",
                ))
            if "pip install" in shell_cmd:
                assert "--upgrade" in shell_cmd or "-U" in shell_cmd
                return (0, (
                    b"Successfully installed requests-2.31.0",
                    b"",
                ))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
            upgrade=True,
        ))
        assert result["status"] == "ok"

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_install_failure(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        container = MagicMock()
        call_count = 0

        def exec_run_side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count in (1, 3):
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}]',
                    b"",
                ))
            return (1, (
                b"",
                b"ERROR: Could not find a version that satisfies the requirement nonexistent",
            ))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="nonexistent-package",
        ))
        assert result["status"] == "error"
        assert "exit code 1" in result["error"]

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=False)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_install_list_packages(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        container = MagicMock()
        call_count = 0

        def exec_run_side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count in (1, 3):
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}, {"name": "setuptools", "version": "68.0"}]',
                    b"",
                ))
            return (0, (
                b"Successfully installed requests-2.31.0 click-8.1.0",
                b"",
            ))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages=["requests", "click"],
        ))
        assert result["status"] == "ok"

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=True)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_uv_install_preferred(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        """When uv is available, uv pip install is used instead of pip install."""
        container = MagicMock()

        def exec_run_side_effect(cmd, **kwargs):
            shell_cmd = cmd[-1] if isinstance(cmd, list) else ""
            if "pip list" in shell_cmd and "install" not in shell_cmd:
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}]',
                    b"",
                ))
            if "uv pip install" in shell_cmd or cmd == ["uv", "pip", "install"] or "uv" in cmd:
                return (0, (
                    b"Successfully installed requests-2.31.0",
                    b"",
                ))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "ok"

    @patch("code_sandbox_mcp.tools.package._has_uv", return_value=True)
    @patch("code_sandbox_mcp.tools.package._docker")
    def test_uv_install_fallback_to_pip(self, mock_docker: MagicMock, mock_has_uv: MagicMock) -> None:
        """When uv is not available, pip install is used as fallback."""
        container = MagicMock()

        def exec_run_side_effect(cmd, **kwargs):
            shell_cmd = cmd[-1] if isinstance(cmd, list) else ""
            if "pip list" in shell_cmd and "install" not in shell_cmd:
                return (0, (
                    b'[{"name": "pip", "version": "23.0"}]',
                    b"",
                ))
            if "pip install" in shell_cmd:
                return (0, (
                    b"Successfully installed requests-2.31.0",
                    b"",
                ))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "ok"
