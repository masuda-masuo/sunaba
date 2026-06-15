"""Tests for write_file_sandbox tool (including partial update support).

Tests cover:
- Full overwrite (backward compatible)
- start_line / end_line line-range replacement
- append mode
- old_str → file_contents replacement
- Mutual exclusivity validation
- Error cases (out-of-range, not found, etc.)
"""
from __future__ import annotations

import io
import tarfile
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import write_file_sandbox


def _make_tar_bytes(content: str, name: str = "test.txt") -> bytes:
    """Create a tar archive in memory containing *content* as *name*."""
    encoded = content.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(encoded)
        tar.addfile(info, io.BytesIO(encoded))
    buf.seek(0)
    return buf.read()


class TestWriteFileSandboxFullOverwrite:
    """Tests for the original full-overwrite behaviour (backward compatibility)."""

    @patch("code_sandbox_mcp.server._docker")
    def test_full_overwrite(self, mock_docker: MagicMock) -> None:
        """Existing full overwrite still works."""
        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="hello.txt",
            file_contents="new content",
            dest_dir="/root",
        )
        assert "Error" not in result
        assert "Written" in result
        assert "hello.txt" in result
        mock_container.put_archive.assert_called_once()

    @patch("code_sandbox_mcp.server._docker")
    def test_full_overwrite_container_not_found(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error returned when container is not found."""
        from docker.errors import NotFound
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_full_overwrite_put_archive_fails(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error returned when put_archive raises APIError."""
        from docker.errors import APIError
        from unittest.mock import Mock
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.reason = "Internal Server Error"

        mock_container = MagicMock()
        mock_container.put_archive.side_effect = APIError(
            "500 Server Error",
            mock_response,
            explanation="write failed",
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_full_overwrite_default_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Default dest_dir is /root."""
        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" not in result
        assert "/root" in result
        # Verify put_archive was called with /root
        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/root"


class TestWriteFileSandboxLineRange:
    """Tests for start_line / end_line line-range replacement."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        """Set up a mock container whose get_archive returns *content*."""
        tar_bytes = _make_tar_bytes(content)
        mock_container = MagicMock()
        # get_archive returns (generator_of_bytes, stat)
        mock_container.get_archive.return_value = (
            [tar_bytes],
            {"size": len(tar_bytes)},
        )
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_middle_lines(self, mock_docker: MagicMock) -> None:
        """Replacing middle lines preserves surrounding lines."""
        existing = "line1\nline2\nline3\nline4\nline5\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="REPLACED\n",
            dest_dir="/root",
            start_line=2,
            end_line=4,
        )
        assert "Error" not in result

        # Capture what was written
        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            assert member is not None
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "line1\nREPLACED\nline5\n"

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_from_start(self, mock_docker: MagicMock) -> None:
        """Omitting start_line defaults to line 1."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="NEWSTART\n",
            dest_dir="/root",
            end_line=2,
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "NEWSTART\nline3\n"

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_to_end(self, mock_docker: MagicMock) -> None:
        """Omitting end_line defaults to last line."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="END\n",
            dest_dir="/root",
            start_line=2,
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "line1\nEND\n"

    @patch("code_sandbox_mcp.server._docker")
    def test_start_line_exceeds_length(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error when start_line exceeds file length."""
        existing = "line1\nline2\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=10,
        )
        assert "Error" in result
        assert "exceeds file length" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_end_line_exceeds_length(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error when end_line exceeds file length."""
        existing = "line1\nline2\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=1,
            end_line=99,
        )
        assert "Error" in result
        assert "exceeds file length" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_start_line_greater_than_end_line(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error when start_line > end_line."""
        existing = "line1\nline2\nline3\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=3,
            end_line=1,
        )
        assert "Error" in result
        assert "greater than" in result or "exceeds file length" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_start_line_zero_error(self, mock_docker: MagicMock) -> None:
        """start_line=0 returns an error (must be >= 1)."""
        existing = "line1\nline2\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=0,
        )
        assert "Error" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_single_line(self, mock_docker: MagicMock) -> None:
        """Replacing a single line with start_line == end_line."""
        existing = "keep\nreplace_me\nkeep\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="replaced\n",
            dest_dir="/root",
            start_line=2,
            end_line=2,
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "keep\nreplaced\nkeep\n"


class TestWriteFileSandboxAppend:
    """Tests for append mode."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        tar_bytes = _make_tar_bytes(content)
        mock_container = MagicMock()
        mock_container.get_archive.return_value = (
            [tar_bytes],
            {"size": len(tar_bytes)},
        )
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("code_sandbox_mcp.server._docker")
    def test_append_to_existing_file(self, mock_docker: MagicMock) -> None:
        """append=True appends file_contents to end of file."""
        existing = "line1\nline2\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="appended\n",
            dest_dir="/root",
            append=True,
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "line1\nline2\nappended\n"

    @patch("code_sandbox_mcp.server._docker")
    def test_append_to_empty_file(self, mock_docker: MagicMock) -> None:
        """Appending to an empty file works."""
        existing = ""
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="content",
            dest_dir="/root",
            append=True,
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "content"


class TestWriteFileSandboxReplace:
    """Tests for old_str replacement mode."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        tar_bytes = _make_tar_bytes(content)
        mock_container = MagicMock()
        mock_container.get_archive.return_value = (
            [tar_bytes],
            {"size": len(tar_bytes)},
        )
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_first_occurrence(self, mock_docker: MagicMock) -> None:
        """old_str replaces the first occurrence only."""
        existing = "hello world, hello universe\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="GOODBYE",
            dest_dir="/root",
            old_str="hello",
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "GOODBYE world, hello universe\n"

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_multi_line(self, mock_docker: MagicMock) -> None:
        """old_str can span multiple lines."""
        existing = "before\nOLD\nBLOCK\nafter\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="NEW\nCONTENT",
            dest_dir="/root",
            old_str="OLD\nBLOCK",
        )
        assert "Error" not in result

        call_args = mock_container.put_archive.call_args
        tar_data = call_args[0][1]
        tar_data.seek(0)
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            member = tar.next()
            written = tar.extractfile(member).read().decode("utf-8")

        assert written == "before\nNEW\nCONTENT\nafter\n"

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_old_str_not_found(self, mock_docker: MagicMock) -> None:
        """Error when old_str is not found in the file."""
        existing = "some content\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="replacement",
            dest_dir="/root",
            old_str="nonexistent",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_replace_old_str_empty(self, mock_docker: MagicMock) -> None:
        """Empty old_str returns an error."""
        existing = "some content\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="replacement",
            dest_dir="/root",
            old_str="",
        )
        assert "Error" in result
        assert "must not be empty" in result


class TestWriteFileSandboxMutualExclusivity:
    """Tests that partial-update modes are mutually exclusive."""

    @patch("code_sandbox_mcp.server._docker")
    def test_start_line_and_append(
        self, mock_docker: MagicMock,
    ) -> None:
        """start_line + append raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            start_line=1,
            append=True,
        )
        assert "Error" in result
        assert "mutually exclusive" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_start_line_and_old_str(
        self, mock_docker: MagicMock,
    ) -> None:
        """start_line + old_str raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            start_line=1,
            old_str="foo",
        )
        assert "Error" in result
        assert "mutually exclusive" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_append_and_old_str(
        self, mock_docker: MagicMock,
    ) -> None:
        """append + old_str raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            append=True,
            old_str="foo",
        )
        assert "Error" in result
        assert "mutually exclusive" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_all_together(
        self, mock_docker: MagicMock,
    ) -> None:
        """start_line + append + old_str all together raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            start_line=1,
            end_line=2,
            append=True,
            old_str="foo",
        )
        assert "Error" in result
        assert "mutually exclusive" in result


class TestWriteFileSandboxFileNotFound:
    """Tests for file-not-found errors in partial-update modes."""

    @patch("code_sandbox_mcp.server._docker")
    def test_file_not_found_for_line_range(
        self, mock_docker: MagicMock,
    ) -> None:
        """Partial update returns a dedicated 'file not found' message.

        Verifies the error message specifically matches the
        FileNotFoundError path (not the generic Exception fallback).
        """
        from docker.errors import NotFound as DockerNotFound

        mock_container = MagicMock()
        mock_container.get_archive.side_effect = DockerNotFound(
            "file not found"
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="nonexistent.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=1,
        )
        # Expect the specific FileNotFoundError path message
        expected_pattern = "file /root/nonexistent.txt not found"
        assert expected_pattern in result, (
            f"Expected '{expected_pattern}' in result, got: {result}"
        )
