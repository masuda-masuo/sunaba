"""Tests for copy_project and copy_file tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.tools.file import copy_file, copy_project


class TestCopyProject:
    """Tests for copy_project tool."""

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_copy_project_with_dot(
        self,
        mock_docker: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """local_src_dir="." should resolve to the actual directory name as arcname."""
        src_dir = tmp_path / "myproject"
        src_dir.mkdir()
        (src_dir / "hello.txt").write_text("hello")
        (src_dir / "subdir").mkdir()
        (src_dir / "subdir" / "nested.txt").write_text("nested")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        with monkeypatch.context() as m:
            m.chdir(str(src_dir))
            result = copy_project(
                container_id="abc123",
                local_src_dir=".",
                dest_dir="/root/shiori",
            )

        assert "Error" not in result
        assert "/root/shiori/myproject" in result
        assert "/root/shiori/." not in result

        mock_container.put_archive.assert_called_once()
        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/root/shiori"

        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert all(
            name.startswith("myproject/") or name == "myproject"
            for name in names
        ), f"Entries should be under 'myproject/', got: {names}"
        assert "myproject/hello.txt" in names
        assert "myproject/subdir/nested.txt" in names

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_copy_project_with_absolute_path(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Absolute paths should use the directory basename as arcname."""
        src_dir = tmp_path / "myapp"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("print('hello')")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/opt",
        )

        assert "Error" not in result
        assert "/opt/myapp" in result

        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/opt"

        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert "myapp/app.py" in names

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_copy_project_container_not_found(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Should return error when container is not found."""
        from docker.errors import NotFound
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=".",
            dest_dir="/root",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_copy_project_src_not_a_directory(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Should return error when local_src_dir is not a directory."""
        src_file = tmp_path / "file.txt"
        src_file.write_text("not a directory")
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_file),
            dest_dir="/root",
        )
        assert "Error" in result
        assert "not a directory" in result

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_copy_project_put_archive_fails(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Should return error when put_archive raises an APIError."""
        from unittest.mock import Mock

        from docker.errors import APIError
        src_dir = tmp_path / "testproj"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("data")

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.reason = "Not Found"

        mock_container = MagicMock()
        mock_container.put_archive.side_effect = APIError(
            "404 Client Error: Not Found",
            mock_response,
            explanation="No such directory",
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/nonexistent",
        )
        assert "Error" in result

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.tools.file.record_copy")
    def test_copy_file_default_dest_path(
        self,
        mock_record_copy: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Default dest_path is /home/sandbox."""
        src_file = tmp_path / "hello.txt"
        src_file.write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_file(
            container_id="abc123",
            local_src_file=str(src_file),
        )

        assert "Error" not in result
        assert "/home/sandbox" in result
