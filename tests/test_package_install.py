"""Tests for the package_install MCP tool."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from sunaba.tools.package import package_install


class TestPackageInstall:
    """Tests for the package_install tool."""

    @patch("sunaba.tools.package._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "error"
        assert "not found" in result["stderr"]

    @patch("sunaba.tools.package._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
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

    @patch("sunaba.tools.package._docker")
    def test_successful_install(self, mock_docker: MagicMock) -> None:
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


    @patch("sunaba.tools.package._docker")
    def test_successful_editable_install(self, mock_docker: MagicMock) -> None:
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

    @patch("sunaba.tools.package._docker")
    def test_install_with_upgrade(self, mock_docker: MagicMock) -> None:
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

    @patch("sunaba.tools.package._docker")
    def test_install_failure(self, mock_docker: MagicMock) -> None:
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

    @patch("sunaba.tools.package._docker")
    def test_install_list_packages(self, mock_docker: MagicMock) -> None:
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

    @patch("sunaba.tools.package._docker")
    def test_runtime_installer_selection(self, mock_docker: MagicMock) -> None:
        """#390: the installer is chosen at runtime inside the container —
        ``uv pip`` when ``$VIRTUAL_ENV`` is set (venv-baked images, PR #388),
        plain ``pip`` otherwise (venv-less images, the #380 constraint)."""
        container = MagicMock()
        seen_cmds = []

        def exec_run_side_effect(cmd, **kwargs):
            seen_cmds.append(cmd)
            if isinstance(cmd, list) and cmd[:2] == ["pip", "list"]:
                return (0, (b"[]", b""))
            return (0, (b"Successfully installed requests-2.31.0", b""))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(
            container_id="abc123",
            packages="requests",
        ))
        assert result["status"] == "ok"
        install_cmds = [
            c for c in seen_cmds if isinstance(c, list) and c[:2] == ["sh", "-c"]
        ]
        assert install_cmds, "no install command was executed"
        script = install_cmds[0][2]
        assert '[ -n "$VIRTUAL_ENV" ]' in script
        assert "then exec uv pip install requests;" in script
        assert "else exec pip install requests;" in script
        # package snapshots must stay plain pip (they parse pip's JSON)
        assert all(
            c[:2] == ["pip", "list"]
            for c in seen_cmds
            if isinstance(c, list) and c[:2] != ["sh", "-c"]
        )
