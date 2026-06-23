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

from code_sandbox_mcp.tools.file import write_file_sandbox


def _exec_run_for(
    content_bytes: bytes,
    uid: int = 1000,
    gid: int = 1000,
    mode: int = 0o644,
):
    """Build an ``exec_run`` side effect for the put_archive-based writer.

    Serves ``cat`` reads with *content_bytes* and mimics the ``stat`` probes
    that :func:`write_file` issues via ``_owner_for_write`` before streaming
    the file via ``put_archive``:

    - ``stat -c '%u %g %a'`` (existing file) -> ``uid gid mode``
    - ``stat -c '%u %g'`` (parent dir) -> ``uid gid``

    Returning real ``stat`` output exercises the ownership-preservation path
    instead of letting every write fall back to ``0, 0, 0o644``.  Defaults keep
    existing callers working without changes.
    """
    def _side_effect(cmd, **kwargs):  # noqa: ANN001, ANN202
        shell = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) > 2 else ""
        if shell.startswith("cat "):
            return (0, (content_bytes, b""))
        if shell.startswith("stat -c") and "%a" in shell:
            return (0, (f"{uid} {gid} {mode:o}\n".encode(), b""))
        if shell.startswith("stat -c"):
            return (0, (f"{uid} {gid}\n".encode(), b""))
        return (0, (b"", b""))

    return _side_effect


def _get_written_content(mock_container: MagicMock) -> str:
    """Extract the written file content from the put_archive tar stream."""
    call = mock_container.put_archive.call_args
    assert call is not None, "put_archive was not called"
    data = call.args[1]
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        member = tar.getmembers()[0]
        extracted = tar.extractfile(member)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def _get_written_member(mock_container: MagicMock) -> tarfile.TarInfo:
    """Return the ``TarInfo`` of the file streamed via put_archive.

    Exposes the tar entry's ``uid`` / ``gid`` / ``mode`` so tests can assert
    that :func:`write_file` carried the resolved ownership into the archive.
    """
    call = mock_container.put_archive.call_args
    assert call is not None, "put_archive was not called"
    data = call.args[1]
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        return tar.getmembers()[0]


class TestWriteFileSandboxFullOverwrite:
    """Tests for the original full-overwrite behaviour (backward compatibility)."""

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_full_overwrite(self, mock_docker: MagicMock) -> None:
        """Existing full overwrite still works."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
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
        assert _get_written_content(mock_container) == "new content"

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_full_overwrite_exec_run_fails(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error returned when exec_run fails."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b"write failed"))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" in result

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_full_overwrite_default_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Default dest_dir is /home/sandbox."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" not in result
        assert "/home/sandbox" in result
        # Verify exec_run was called
        mock_container.put_archive.assert_called_once()


    @patch("code_sandbox_mcp.tools.file._docker")
    def test_absolute_file_name_ignores_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Absolute file_name is used as-is, dest_dir is ignored."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="/tmp/repo/src/foo.py",
            file_contents="data",
            dest_dir="/home/sandbox",
        )
        assert "Error" not in result
        assert "/tmp/repo/src/foo.py" in result
        assert "/home/sandbox" not in result

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_relative_subpath_file_name_joined_with_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Relative file_name with subdirs is joined with dest_dir correctly."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="src/pkg/module.py",
            file_contents="data",
            dest_dir="/tmp/repo",
        )
        assert "Error" not in result
        assert "/tmp/repo/src/pkg/module.py" in result


class TestWriteFileSandboxLineRange:
    """Tests for start_line / end_line line-range replacement."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        """Set up a mock container whose exec_run returns *content*."""
        content_bytes = content.encode("utf-8") if content else b""
        mock_container = MagicMock()
        # exec_run sequence: test -f (success), cat (content), write (success)
        mock_container.exec_run.side_effect = _exec_run_for(content_bytes)
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_replace_middle_lines(self, mock_docker: MagicMock) -> None:
        """Replacing middle lines preserves surrounding lines."""
        existing = "line1\nline2\nline3\nline4\nline5\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="REPLACED\n",
            dest_dir="/root",
            start_line=2,
            end_line=4,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "line1\nREPLACED\nline5\n"

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_replace_from_start(self, mock_docker: MagicMock) -> None:
        """Omitting start_line defaults to line 1."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="NEWSTART\n",
            dest_dir="/root",
            end_line=2,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "NEWSTART\nline3\n"

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_replace_to_end(self, mock_docker: MagicMock) -> None:
        """Omitting end_line defaults to last line."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="END\n",
            dest_dir="/root",
            start_line=2,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "line1\nEND\n"

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
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
        assert "greater than" in result

    @patch("code_sandbox_mcp.tools.file._docker")
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
        assert "must be >= 1" in result

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_replace_single_line(self, mock_docker: MagicMock) -> None:
        """Replacing a single line with start_line == end_line."""
        existing = "keep\nreplace_me\nkeep\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="replaced\n",
            dest_dir="/root",
            start_line=2,
            end_line=2,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "keep\nreplaced\nkeep\n"


class TestWriteFileSandboxAppend:
    """Tests for append mode."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        content_bytes = content.encode("utf-8") if content else b""
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(content_bytes)
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("code_sandbox_mcp.tools.file._docker")
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
        assert "Written" in result
        assert _get_written_content(mock_container) == "line1\nline2\nappended\n"

    @patch("code_sandbox_mcp.tools.file._docker")
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
        assert "Written" in result
        assert _get_written_content(mock_container) == "content"


class TestWriteFileSandboxReplace:
    """Tests for old_str replacement mode."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        content_bytes = content.encode("utf-8") if content else b""
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(content_bytes)
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_replace_first_occurrence(self, mock_docker: MagicMock) -> None:
        """old_str replaces a unique occurrence (exact match)."""
        existing = "hello world, hello universe\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="GOODBYE",
            dest_dir="/root",
            old_str="hello world",
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "GOODBYE, hello universe\n"

    @patch("code_sandbox_mcp.tools.file._docker")
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
        assert "Written" in result
        assert _get_written_content(mock_container) == "before\nNEW\nCONTENT\nafter\n"

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
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


class TestWriteFileSandboxReplaceEnhanced:
    """Tests for enhanced old_str mode (issue #90)."""

    def _mock_container_with_file(
        self, mock_docker: MagicMock, content: str,
    ) -> MagicMock:
        content_bytes = content.encode("utf-8") if content else b""
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(content_bytes)
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    # --- uniqueness check ---

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_multiple_exact_matches_error(self, mock_docker: MagicMock) -> None:
        """Multiple exact matches are rejected with line numbers."""
        existing = "hello\nworld\nhello\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="HI",
            dest_dir="/root",
            old_str="hello",
        )
        assert "Error" in result
        assert "matches at 2 locations" in result
        assert "lines 1, 3" in result
        assert "unique" in result.lower()

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_single_exact_match_still_works(self, mock_docker: MagicMock) -> None:
        """A single exact match succeeds (backward compat)."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="REPLACED",
            dest_dir="/root",
            old_str="line2",
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "line1\nREPLACED\nline3\n"

    # --- whitespace-flexible fallback ---

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_whitespace_mismatch_fallback(self, mock_docker: MagicMock) -> None:
        """Whitespace difference is tolerated via fallback."""
        existing = "    def foo():\n        pass\n"
        # old_str has fewer leading spaces
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="def bar():",
            dest_dir="/root",
            old_str="def foo():",
        )
        assert "Error" not in result
        assert "Written" in result
        # new_str should be re-indented to match file's indentation
        assert _get_written_content(mock_container) == "    def bar():\n        pass\n"

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_whitespace_flexible_multiple_matches(
        self, mock_docker: MagicMock,
    ) -> None:
        """Whitespace-flexible match that finds duplicates is rejected."""
        # Use different indentation so exact match fails but
        # whitespace-flexible succeeds (and finds duplicates).
        existing = "  hello\n  world\n\thello\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="HI",
            dest_dir="/root",
            old_str="    hello",
        )
        assert "Error" in result
        assert "matches at 2 locations" in result
        assert "whitespace normalization" in result.lower()

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_whitespace_flexible_trailing(self, mock_docker: MagicMock) -> None:
        """Trailing whitespace is ignored in flexible match."""
        existing = "  hello world  \n  next line\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="goodbye",
            dest_dir="/root",
            old_str="hello world  ",
        )
        assert "Error" not in result
        assert "Written" in result
        # Trailing whitespace is stripped for matching; replacement uses
        # file's leading indentation only (trailing spaces are not preserved).
        assert _get_written_content(mock_container) == "  goodbye\n  next line\n"

    # --- near-miss echo ---

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_near_miss_shows_context(self, mock_docker: MagicMock) -> None:
        """When old_str is not found, near-miss context is returned."""
        existing = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        self._mock_container_with_file(mock_docker, existing)

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.txt",
            file_contents="new",
            dest_dir="/root",
            old_str="def baz():",
        )
        assert "Error" in result
        assert "not found" in result
        # Should show most similar area
        assert "Most relevant file area:" in result
        assert "def foo" in result or "def bar" in result


class TestWriteFileSandboxMutualExclusivity:
    """Tests that partial-update modes are mutually exclusive."""

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
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

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_file_not_found_for_line_range(
        self, mock_docker: MagicMock,
    ) -> None:
        """Partial update returns a dedicated 'file not found' message.

        Verifies the error message specifically matches the
        file-not-found path (test -f returns non-zero).
        """
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b"file not found"))
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
        # Expect the specific file-not-found path message
        expected_pattern = "file /root/nonexistent.txt not found"
        assert expected_pattern in result, (
            f"Expected '{expected_pattern}' in result, got: {result}"
        )


class TestWriteFileSandboxJournal:
    """Tests that write_file_sandbox records journal entries via write_file (Issue #96)."""

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.edit_verify.record_file_write")
    def test_full_overwrite_records_journal(
        self, mock_record: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.py",
            file_contents="print('hello')",
            dest_dir="/root",
        )
        assert "Error" not in result
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        assert args[0] == "abc123"  # container_id
        assert args[1] == "test.py"  # file_name
        assert "/root" in args[2]  # dest_dir
        assert args[3] > 0  # byte_count
        assert kwargs.get("is_test") is False  # not a test file


class TestWriteFileLargeFile:
    """Regression tests for Issue #144.

    Large files must not hit the Linux ``MAX_ARG_STRLEN`` limit (128 KiB per
    single argv string).  The old transport embedded the base64-encoded
    content in ``echo <b64> | base64 -d``, so files over ~96 KiB failed with
    ``argument list too long``.  Content is now streamed via ``put_archive``.
    """

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_large_overwrite_uses_put_archive_not_argv(
        self, mock_docker: MagicMock,
    ) -> None:
        # ~300 KiB; base64 of this would be ~400 KiB, far over the 128 KiB
        # single-argv limit that broke the old transport.
        big = ("x" * 200 + "\n") * 1500
        assert len(big.encode("utf-8")) > 256 * 1024

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="big.py",
            file_contents=big,
            dest_dir="/root",
        )
        assert "Error" not in result
        assert "Written" in result

        # Content went through put_archive (tar stream), not the shell argv.
        mock_container.put_archive.assert_called_once()
        assert _get_written_content(mock_container) == big

        # No exec_run command embedded the file content as an argv string.
        for call in mock_container.exec_run.call_args_list:
            cmd = call.args[0]
            joined = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            assert len(joined) < 128 * 1024, "file content leaked into argv"

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_large_old_str_edit(self, mock_docker: MagicMock) -> None:
        """A line-range/old_str edit of a large file also succeeds."""
        existing = ("y" * 100 + "\n") * 2000 + "TARGET\n"
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(
            existing.encode("utf-8")
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="big.py",
            file_contents="REPLACED",
            dest_dir="/root",
            old_str="TARGET",
        )
        assert "Error" not in result
        assert "Written" in result
        written = _get_written_content(mock_container)
        assert written.endswith("REPLACED\n")
        assert "TARGET" not in written


class TestWriteFileOwnership:
    """Issue #144: put_archive must not leave files owned by root."""

    def test_owner_for_write_preserves_existing(self) -> None:
        from code_sandbox_mcp.edit_verify import _owner_for_write

        container = MagicMock()
        # stat of the existing file: uid=1000 gid=1000 mode=644 (octal).
        container.exec_run.return_value = (0, (b"1000 1000 644\n", b""))
        uid, gid, mode = _owner_for_write(
            container, "/home/sandbox/f.py", "/home/sandbox"
        )
        assert (uid, gid, mode) == (1000, 1000, 0o644)

    def test_owner_for_write_inherits_parent_for_new_file(self) -> None:
        from code_sandbox_mcp.edit_verify import _owner_for_write

        container = MagicMock()

        def _side_effect(cmd, **kwargs):  # noqa: ANN001, ANN202
            shell = cmd[2]
            if "%a" in shell:  # stat of (missing) target file
                return (1, (b"", b"No such file"))
            return (0, (b"1000 1000\n", b""))  # stat of parent dir

        container.exec_run.side_effect = _side_effect
        uid, gid, mode = _owner_for_write(
            container, "/home/sandbox/new.py", "/home/sandbox"
        )
        assert (uid, gid, mode) == (1000, 1000, 0o644)

    def test_owner_for_write_falls_back_when_stat_unavailable(self) -> None:
        from code_sandbox_mcp.edit_verify import _owner_for_write

        container = MagicMock()
        container.exec_run.return_value = (127, (b"", b"stat: not found"))
        uid, gid, mode = _owner_for_write(container, "/x/f", "/x")
        assert (uid, gid, mode) == (0, 0, 0o644)

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_write_carries_existing_owner_into_archive(
        self, mock_docker: MagicMock
    ) -> None:
        """End-to-end: write_file streams the resolved owner into the tar.

        Drives the full ``write_file_sandbox`` path through the realistic
        ``_exec_run_for`` mock (which answers the ``stat`` probes) and asserts
        the put_archive tar entry carries the existing file's uid/gid/mode —
        guarding against a regression that would leave files owned by root.
        """
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(
            b"line1\nline2\n", uid=1000, gid=1000, mode=0o600
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        write_file_sandbox(
            "abc123abc123",
            "f.py",
            "appended\n",
            dest_dir="/home/sandbox",
            append=True,
        )

        member = _get_written_member(mock_container)
        assert (member.uid, member.gid, member.mode) == (1000, 1000, 0o600)


class TestWriteFileIsTestDetection:
    """Tests that write_file correctly classifies test files (Issue #96)."""

    def test_regular_file_is_not_test(self) -> None:
        from code_sandbox_mcp.edit_verify import _is_test_file
        assert _is_test_file("/root/main.py") is False
        assert _is_test_file("/home/src/app.py") is False
        assert _is_test_file("/root/utils.js") is False

    def test_test_prefix_is_test(self) -> None:
        from code_sandbox_mcp.edit_verify import _is_test_file
        assert _is_test_file("/root/test_main.py") is True
        assert _is_test_file("/home/tests/test_utils.py") is True

    def test_test_suffix_is_test(self) -> None:
        from code_sandbox_mcp.edit_verify import _is_test_file
        assert _is_test_file("/root/main_test.py") is True
        assert _is_test_file("/root/utils_test.go") is True

    def test_test_variant_suffix_is_test(self) -> None:
        from code_sandbox_mcp.edit_verify import _is_test_file
        assert _is_test_file("/root/utils_test_v2.go") is True
        assert _is_test_file("/root/model_test_v3.py") is True

    def test_dot_test_dot_is_test(self) -> None:
        from code_sandbox_mcp.edit_verify import _is_test_file
        assert _is_test_file("/root/app.test.js") is True
        assert _is_test_file("/root/component.spec.ts") is True

    def test_tests_directory_is_test(self) -> None:
        from code_sandbox_mcp.edit_verify import _is_test_file
        assert _is_test_file("/app/tests/test_main.py") is True
        assert _is_test_file("/app/test/test_main.py") is True
        assert _is_test_file("/app/__tests__/test_main.js") is True

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.edit_verify.record_file_write")
    def test_write_file_sandbox_detects_test_file(
        self, mock_record: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        write_file_sandbox(
            container_id="abc123",
            file_name="test_foo.py",
            file_contents="def test_foo(): pass",
            dest_dir="/tests",
        )
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs.get("is_test") is True

    @patch("code_sandbox_mcp.edit_verify.record_file_write")
    def test_write_file_uses_is_test(
        self, mock_record: MagicMock,
    ) -> None:
        from code_sandbox_mcp.edit_verify import write_file

        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))

        write_file(container, "abc123", "/tests/test_main.py", "def test_main(): pass")
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs.get("is_test") is True

        mock_record.reset_mock()
        write_file(container, "abc123", "/root/main.py", "def main(): pass")
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs.get("is_test") is False


class TestPosixpathFix:
    """Tests for posixpath usage in container path operations (Issue #219).

    On Windows hosts, os.path.join uses backslash separators which are invalid
    in Linux container paths. All container-side path operations must use
    posixpath to produce forward-slash paths regardless of the host OS.
    """

    def test_is_test_file_preserves_forward_slash(self) -> None:
        """_is_test_file must work with POSIX paths on any host OS."""
        from code_sandbox_mcp.edit_verify import _is_test_file

        assert _is_test_file("/home/sandbox/test_main.py") is True
        assert _is_test_file("/home/sandbox/main.py") is False
        assert _is_test_file("/tmp/repo/tests/test_foo.py") is True
        assert _is_test_file("/tmp/repo/src/bar.py") is False

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_write_file_sandbox_dest_path_uses_forward_slash(
        self, mock_docker: MagicMock,
    ) -> None:
        """dest_path constructed by write_file_sandbox must use forward slashes.

        Regression test: os.path.join would produce backslash-separated paths
        on Windows, causing "file not found" in Linux containers (Issue #219).
        """
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(b"existing content\n")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.py",
            file_contents="new content",
            dest_dir="/home/sandbox",
            old_str="existing content",
        )
        assert "Error" not in result

        # Verify the cat command uses forward-slash path (not backslash)
        cat_calls = [
            args[0][2] if isinstance(args[0], list) and len(args[0]) > 2 else ""
            for args, _ in mock_container.exec_run.call_args_list
            if isinstance(args[0], list) and len(args[0]) > 2
            and args[0][2].startswith("cat ")
        ]
        assert any(
            "/home/sandbox/test.py" in call
            for call in cat_calls
        ), f"Expected forward-slash path in cat calls: {cat_calls}"

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_write_file_sandbox_full_overwrite_path(
        self, mock_docker: MagicMock,
    ) -> None:
        """Full overwrite mode also uses correct path (not os.path.join behavior)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="output.txt",
            file_contents="data",
            dest_dir="/tmp/repo/src",
        )
        assert "Error" not in result
        parent_dir = mock_container.put_archive.call_args[0][0]
        assert parent_dir == "/tmp/repo/src", (
            f"put_archive parent_dir should be forward-slash path, got: {parent_dir!r}"
        )
