"""Tests for the write_file / edit_file tools (issue #630 split).

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

from sunaba.tools.file import edit_file, write_file


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
    - ``id -u; id -g`` (running user, new file) -> ``uid`` / ``gid``

    Returning real ``stat`` output exercises the ownership-preservation path
    instead of letting every write fall back to ``999, 999, 0o644``.  Defaults keep
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
        if shell.startswith("id "):  # running-user probe (Issue #642)
            return (0, (f"{uid}\n{gid}\n".encode(), b""))
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

    @patch("sunaba.tools.file._docker")
    def test_full_overwrite(self, mock_docker: MagicMock) -> None:
        """Existing full overwrite still works."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
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

    @patch("sunaba.tools.file._docker")
    def test_full_overwrite_container_not_found(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error returned when container is not found."""
        from docker.errors import NotFound
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("sunaba.tools.file._docker")
    def test_full_overwrite_exec_run_fails(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error returned when exec_run fails."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b"write failed"))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" in result

    @patch("sunaba.tools.file._docker")
    def test_full_overwrite_default_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Default dest_dir is /home/sandbox."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert "Error" not in result
        assert "/workspace" in result
        # Verify exec_run was called
        mock_container.put_archive.assert_called_once()


    @patch("sunaba.tools.file._docker")
    def test_absolute_file_name_ignores_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Absolute file_name is used as-is, dest_dir is ignored."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="/tmp/repo/src/foo.py",
            file_contents="data",
            dest_dir="/home/sandbox",
        )
        assert "Error" not in result
        assert "/tmp/repo/src/foo.py" in result
        assert "/home/sandbox" not in result

    @patch("sunaba.tools.file._docker")
    def test_relative_subpath_file_name_joined_with_dest_dir(
        self, mock_docker: MagicMock,
    ) -> None:
        """Relative file_name with subdirs is joined with dest_dir correctly."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
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

    @patch("sunaba.tools.file._docker")
    def test_replace_middle_lines(self, mock_docker: MagicMock) -> None:
        """Replacing middle lines preserves surrounding lines."""
        existing = "line1\nline2\nline3\nline4\nline5\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_replace_from_start(self, mock_docker: MagicMock) -> None:
        """Omitting start_line defaults to line 1."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="NEWSTART\n",
            dest_dir="/root",
            end_line=2,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "NEWSTART\nline3\n"

    @patch("sunaba.tools.file._docker")
    def test_replace_to_end(self, mock_docker: MagicMock) -> None:
        """Omitting end_line defaults to last line."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="END\n",
            dest_dir="/root",
            start_line=2,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "line1\nEND\n"

    @patch("sunaba.tools.file._docker")
    def test_start_line_exceeds_length(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error when start_line exceeds file length."""
        existing = "line1\nline2\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=10,
        )
        assert "Error" in result
        assert "exceeds file length" in result

    @patch("sunaba.tools.file._docker")
    def test_end_line_exceeds_length(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error when end_line exceeds file length."""
        existing = "line1\nline2\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=1,
            end_line=99,
        )
        assert "Error" in result
        assert "exceeds file length" in result

    @patch("sunaba.tools.file._docker")
    def test_start_line_greater_than_end_line(
        self, mock_docker: MagicMock,
    ) -> None:
        """Error when start_line > end_line."""
        existing = "line1\nline2\nline3\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=3,
            end_line=1,
        )
        assert "Error" in result
        assert "greater than" in result

    @patch("sunaba.tools.file._docker")
    def test_start_line_zero_error(self, mock_docker: MagicMock) -> None:
        """start_line=0 returns an error (must be >= 1)."""
        existing = "line1\nline2\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="data",
            dest_dir="/root",
            start_line=0,
        )
        assert "Error" in result
        assert "must be >= 1" in result

    @patch("sunaba.tools.file._docker")
    def test_replace_single_line(self, mock_docker: MagicMock) -> None:
        """Replacing a single line with start_line == end_line."""
        existing = "keep\nreplace_me\nkeep\n"
        mock_container = self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_append_to_existing_file(self, mock_docker: MagicMock) -> None:
        """append=True appends file_contents to end of file."""
        existing = "line1\nline2\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="appended\n",
            dest_dir="/root",
            append=True,
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "line1\nline2\nappended\n"

    @patch("sunaba.tools.file._docker")
    def test_append_to_empty_file(self, mock_docker: MagicMock) -> None:
        """Appending to an empty file works."""
        existing = ""
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_replace_first_occurrence(self, mock_docker: MagicMock) -> None:
        """old_str replaces a unique occurrence (exact match)."""
        existing = "hello world, hello universe\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="GOODBYE",
            dest_dir="/root",
            old_str="hello world",
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "GOODBYE, hello universe\n"

    @patch("sunaba.tools.file._docker")
    def test_replace_multi_line(self, mock_docker: MagicMock) -> None:
        """old_str can span multiple lines."""
        existing = "before\nOLD\nBLOCK\nafter\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="NEW\nCONTENT",
            dest_dir="/root",
            old_str="OLD\nBLOCK",
        )
        assert "Error" not in result
        assert "Written" in result
        assert _get_written_content(mock_container) == "before\nNEW\nCONTENT\nafter\n"

    @patch("sunaba.tools.file._docker")
    def test_replace_old_str_not_found(self, mock_docker: MagicMock) -> None:
        """Error when old_str is not found in the file."""
        existing = "some content\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="replacement",
            dest_dir="/root",
            old_str="nonexistent",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("sunaba.tools.file._docker")
    def test_replace_old_str_empty(self, mock_docker: MagicMock) -> None:
        """Empty old_str returns an error."""
        existing = "some content\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_multiple_exact_matches_error(self, mock_docker: MagicMock) -> None:
        """Multiple exact matches are rejected with line numbers."""
        existing = "hello\nworld\nhello\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_single_exact_match_still_works(self, mock_docker: MagicMock) -> None:
        """A single exact match succeeds (backward compat)."""
        existing = "line1\nline2\nline3\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_whitespace_mismatch_fallback(self, mock_docker: MagicMock) -> None:
        """Whitespace difference is tolerated via fallback."""
        existing = "    def foo():\n        pass\n"
        # old_str has fewer leading spaces
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_whitespace_flexible_multiple_matches(
        self, mock_docker: MagicMock,
    ) -> None:
        """Whitespace-flexible match that finds duplicates is rejected."""
        # Use different indentation so exact match fails but
        # whitespace-flexible succeeds (and finds duplicates).
        existing = "  hello\n  world\n\thello\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="HI",
            dest_dir="/root",
            old_str="    hello",
        )
        assert "Error" in result
        assert "matches at 2 locations" in result
        assert "whitespace normalization" in result.lower()

    @patch("sunaba.tools.file._docker")
    def test_whitespace_flexible_trailing(self, mock_docker: MagicMock) -> None:
        """Trailing whitespace is ignored in flexible match."""
        existing = "  hello world  \n  next line\n"
        mock_container = self._mock_container_with_file(
            mock_docker, existing,
        )

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_near_miss_shows_context(self, mock_docker: MagicMock) -> None:
        """When old_str is not found, near-miss context is returned."""
        existing = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="new",
            dest_dir="/root",
            old_str="def baz():",
        )
        assert "Error" in result
        assert "not found" in result
        # Should show best matching region
        assert "Best matching region" in result
        assert "Unified diff" in result
        assert "def foo" in result or "def bar" in result

    @patch("sunaba.tools.file._docker")
    def test_near_miss_first_mismatch(self, mock_docker: MagicMock) -> None:
        """Near-miss pinpoints the first mismatching line (issue #580)."""
        # old_str has indent=2 AND different content ("fox" vs "foo")
        # so whitespace-flexible won't match — near-miss fires.
        existing = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="new",
            dest_dir="/root",
            old_str="  def fox():",
        )
        assert "Error" in result
        # The misleading indentation hint is gone (issue #580).
        assert "Indentation mismatch" not in result
        assert "First mismatch: old_str line 1 vs file line 1" in result
        assert "'  def fox():'" in result
        assert "'def foo():'" in result

    @patch("sunaba.tools.file._docker")
    def test_near_miss_shows_diff(self, mock_docker: MagicMock) -> None:
        """Near-miss shows unified diff including ---/+++ headers."""
        existing = "def foo():\n    pass\n\ndef bar():\n    pass\n"
        self._mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="new",
            dest_dir="/root",
            old_str="def baz():",
        )
        assert "Error" in result
        assert "--- old_str (provided)" in result
        assert "+++ /root/test.txt (file)" in result
        assert "-def baz():" in result
        assert "+def bar():" in result


def _mock_container_with_file(mock_docker: MagicMock, content: str) -> MagicMock:
    """Wire a mock container that serves *content* for cat reads."""
    content_bytes = content.encode("utf-8") if content else b""
    mock_container = MagicMock()
    mock_container.exec_run.side_effect = _exec_run_for(content_bytes)
    mock_client = MagicMock()
    mock_client.containers.get.return_value = mock_container
    mock_docker.return_value = mock_client
    return mock_container


class TestNearMissFirstMismatch:
    """Issue #580: the near-miss echo pinpoints the first mismatching line."""

    @patch("sunaba.tools.file._docker")
    def test_leading_blank_line_no_bogus_indent_report(
        self, mock_docker: MagicMock,
    ) -> None:
        """old_str starting with a blank line reports the real mismatch.

        Reproduces the issue #580 scenario: the old indentation hint
        compared the blank first line (indent=0) against the file and
        produced a bogus "indent=0 vs N" report.
        """
        existing = "def m():\n    a()\n\n    x = 1\n    y = 2\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="new",
            dest_dir="/root",
            old_str="\n    x = 1\n    y = 3",
        )
        assert "Error" in result
        assert "Indentation mismatch" not in result
        assert "First mismatch: old_str line 3 vs file line 5" in result
        assert "'    y = 3'" in result
        assert "'    y = 2'" in result

    @patch("sunaba.tools.file._docker")
    def test_tab_vs_space_shown_via_repr(self, mock_docker: MagicMock) -> None:
        """Tabs and spaces are visualized with repr() on the mismatch line."""
        existing = "def f():\n\tresult = foobar()\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="new",
            dest_dir="/root",
            old_str="def f():\n    result = foo()",
        )
        assert "Error" in result
        assert "First mismatch: old_str line 2 vs file line 2" in result
        # repr() makes the tab visible as \t and preserves the spaces.
        assert "'\\tresult = foobar()'" in result
        assert "'    result = foo()'" in result

    @patch("sunaba.tools.file._docker")
    def test_duplicated_line_in_old_str(self, mock_docker: MagicMock) -> None:
        """A duplicated line in old_str is reported with the right line number.

        Position-wise comparison (line i vs line i) would blame every
        line after the duplicate; opcode-based comparison points at the
        extra line itself.
        """
        existing = "a = 1\nb = 2\nc = 3\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="new",
            dest_dir="/root",
            old_str="a = 1\na = 1\nb = 2",
        )
        assert "Error" in result
        assert (
            "First mismatch: old_str line 1: 'a = 1' "
            "has no counterpart in the file region"
        ) in result

    @patch("sunaba.tools.file._docker")
    def test_small_old_str_gets_full_diff(self, mock_docker: MagicMock) -> None:
        """old_str of <= 50 lines shows the unified diff untruncated."""
        existing = "".join(f"actual_{i} = {i}\n" for i in range(1, 11))
        _mock_container_with_file(mock_docker, existing)

        old_str = "\n".join(f"wanted_{i} = {i}" for i in range(1, 11))
        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="new",
            dest_dir="/root",
            old_str=old_str,
        )
        assert "Error" in result
        assert "truncated" not in result
        # The old 6-line cap would have hidden these later diff lines.
        assert "-wanted_10 = 10" in result
        assert "+actual_10 = 10" in result

    @patch("sunaba.tools.file._docker")
    def test_large_old_str_diff_is_capped(self, mock_docker: MagicMock) -> None:
        """old_str of > 50 lines caps the diff at 30 lines with a marker."""
        existing = "".join(f"actual_{i} = {i}\n" for i in range(1, 61))
        _mock_container_with_file(mock_docker, existing)

        old_str = "\n".join(f"wanted_{i} = {i}" for i in range(1, 61))
        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="new",
            dest_dir="/root",
            old_str=old_str,
        )
        assert "Error" in result
        assert "... (truncated," in result
        assert "more lines)" in result
        # Diff lines past the 30-line cap are not shown.
        assert "+actual_60 = 60" not in result


class TestOldStrSuccessEcho:
    """Issue #580: successful old_str edits echo the post-edit region."""

    @patch("sunaba.tools.file._docker")
    def test_exact_match_echoes_replaced_region(
        self, mock_docker: MagicMock,
    ) -> None:
        """The exact-match path echoes the new lines with context."""
        existing = "line1\nline2\nline3\nline4\nline5\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="REPLACED",
            dest_dir="/root",
            old_str="line3",
        )
        assert "Error" not in result
        assert "Written" in result
        assert "(replaced line 3)" in result
        assert ">>>" in result
        assert "| REPLACED" in result
        # +-2 lines of context, with line numbers.
        assert "| line1" in result
        assert "| line5" in result

    @patch("sunaba.tools.file._docker")
    def test_exact_match_multiline_span(self, mock_docker: MagicMock) -> None:
        """A multi-line replacement reports the full replaced span."""
        existing = "line1\nline2\nline3\nline4\nline5\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="X\nY\nZ",
            dest_dir="/root",
            old_str="line2\nline3",
        )
        assert "Error" not in result
        assert "(replaced lines 2-4)" in result
        assert "| X" in result
        assert "| Z" in result

    @patch("sunaba.tools.file._docker")
    def test_whitespace_flexible_echoes_reindented_lines(
        self, mock_docker: MagicMock,
    ) -> None:
        """The flexible path echoes the re-indented lines (ground truth)."""
        existing = "    def foo():\n        pass\n"
        _mock_container_with_file(mock_docker, existing)

        # Multi-line old_str with no indentation: not an exact substring,
        # so the whitespace-flexible fallback (re-indent) kicks in.
        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="def bar():\n    changed()",
            dest_dir="/root",
            old_str="def foo():\npass",
        )
        assert "Error" not in result
        assert "(replaced lines 1-2)" in result
        # The echoed lines carry the file's indentation, not old_str's.
        assert "|     def bar():" in result
        assert "|         changed()" in result

    @patch("sunaba.tools.file._docker")
    def test_long_replacement_elides_middle(
        self, mock_docker: MagicMock,
    ) -> None:
        """An echo of more than 30 rows elides the middle."""
        existing = "a\nb\nc\n"
        _mock_container_with_file(mock_docker, existing)

        new_block = "\n".join(f"n{i:02d}" for i in range(1, 41))
        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents=new_block,
            dest_dir="/root",
            old_str="b",
        )
        assert "Error" not in result
        assert "(replaced lines 2-41)" in result
        # 42 echo rows total: 14 head + marker + 14 tail.
        assert "... (14 lines)" in result
        assert "| n01" in result
        assert "| n40" in result
        assert "| n20" not in result

    @patch("sunaba.tools.file._docker")
    def test_full_overwrite_keeps_plain_message(
        self, mock_docker: MagicMock,
    ) -> None:
        """Non-old_str modes keep the plain 'Written N bytes' message."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="hello",
            dest_dir="/root",
        )
        assert result == "Written 5 bytes to /root/test.txt"


class TestWriteFileSandboxMutualExclusivity:
    """Tests that partial-update modes are mutually exclusive."""

    @patch("sunaba.tools.file._docker")
    def test_start_line_and_append(
        self, mock_docker: MagicMock,
    ) -> None:
        """start_line + append raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = edit_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            start_line=1,
            append=True,
        )
        assert "Error" in result
        assert "mutually exclusive" in result

    @patch("sunaba.tools.file._docker")
    def test_start_line_and_old_str(
        self, mock_docker: MagicMock,
    ) -> None:
        """start_line + old_str raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = edit_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            start_line=1,
            old_str="foo",
        )
        assert "Error" in result
        assert "mutually exclusive" in result

    @patch("sunaba.tools.file._docker")
    def test_append_and_old_str(
        self, mock_docker: MagicMock,
    ) -> None:
        """append + old_str raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = edit_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
            append=True,
            old_str="foo",
        )
        assert "Error" in result
        assert "mutually exclusive" in result

    @patch("sunaba.tools.file._docker")
    def test_all_together(
        self, mock_docker: MagicMock,
    ) -> None:
        """start_line + append + old_str all together raises error."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
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

        result = edit_file(
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


class TestEditFileSplitContract:
    """Issue #630 split: edit_file needs a mode and an existing file."""

    @patch("sunaba.tools.file._docker")
    def test_no_mode_rejected_with_write_file_pointer(
        self, mock_docker: MagicMock,
    ) -> None:
        """edit_file without any edit mode is rejected, pointing at write_file."""
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = edit_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert result.startswith("Error")
        assert "requires one edit mode" in result
        assert "write_file" in result

    @patch("sunaba.tools.file._docker")
    def test_missing_file_points_at_write_file(
        self, mock_docker: MagicMock,
    ) -> None:
        """edit_file on a missing file names write_file as the creation path."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"", b"no such file"))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = edit_file(
            container_id="abc123",
            file_name="new.txt",
            file_contents="data",
            dest_dir="/root",
            old_str="foo",
        )
        assert "Error: file /root/new.txt not found" in result
        assert "use write_file to create it" in result

    @patch("sunaba.tools.file.record_tool_use")
    @patch("sunaba.tools.file._docker")
    def test_write_file_journals_overwrote_existing_true(
        self, mock_docker: MagicMock, mock_record: MagicMock,
    ) -> None:
        """Overwriting an existing file logs overwrote_existing=True (#630 metric)."""
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(b"old content")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="new content",
            dest_dir="/root",
        )
        assert "Error" not in result
        mock_record.assert_called_once_with(
            "abc123",
            "write_file",
            {"file_path": "/root/f.txt", "overwrote_existing": True},
        )

    @patch("sunaba.tools.file.record_tool_use")
    @patch("sunaba.tools.file._docker")
    def test_write_file_journals_overwrote_existing_false(
        self, mock_docker: MagicMock, mock_record: MagicMock,
    ) -> None:
        """Creating a new file logs overwrote_existing=False (#630 metric)."""
        def _cat_fails(cmd, **kwargs):  # noqa: ANN001, ANN202
            shell = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) > 2 else ""
            if shell.startswith("cat "):
                return (1, (b"", b"no such file"))
            return (0, (b"", b""))

        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _cat_fails
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
            container_id="abc123",
            file_name="f.txt",
            file_contents="new content",
            dest_dir="/root",
        )
        assert "Error" not in result
        mock_record.assert_called_once_with(
            "abc123",
            "write_file",
            {"file_path": "/root/f.txt", "overwrote_existing": False},
        )


class TestWriteFileSandboxJournal:
    """Tests that write_file/edit_file record journal entries (Issue #96)."""

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.edit_verify.record_file_write")
    def test_full_overwrite_records_journal(
        self, mock_record: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
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

    @patch("sunaba.tools.file._docker")
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

        result = write_file(
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

    @patch("sunaba.tools.file._docker")
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

        result = edit_file(
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
        from sunaba.edit_verify import _owner_for_write

        container = MagicMock()
        # stat of the existing file: uid=1000 gid=1000 mode=644 (octal).
        container.exec_run.return_value = (0, (b"1000 1000 644\n", b""))
        uid, gid, mode = _owner_for_write(
            container, "/home/sandbox/f.py"
        )
        assert (uid, gid, mode) == (1000, 1000, 0o644)

    def test_owner_for_write_uses_running_user_for_new_file(self) -> None:
        from sunaba.edit_verify import _owner_for_write

        container = MagicMock()

        def _side_effect(cmd, **kwargs):  # noqa: ANN001, ANN202
            shell = cmd[2]
            if "%a" in shell:  # stat of (missing) target file
                return (1, (b"", b"No such file"))
            return (0, (b"999\n999\n", b""))  # id -u; id -g (running user)

        container.exec_run.side_effect = _side_effect
        uid, gid, mode = _owner_for_write(
            container, "/tmp/new.py"
        )
        assert (uid, gid, mode) == (999, 999, 0o644)

    def test_owner_for_write_new_file_does_not_stat_proc_self_symlink(self) -> None:
        """Issue #642: the running-user probe must dereference, not stat the
        root-owned ``/proc/self`` symlink.

        ``stat -c '%u %g' /proc/self`` (no ``-L``) reports the symlink's owner
        (root, 0:0), which left new files unwritable by the sandbox user.  The
        probe must read the real uid/gid -- via ``id`` -- so a new file is owned
        by the running user and stays writable by other uid-999 tools.
        """
        from sunaba.edit_verify import _owner_for_write

        container = MagicMock()

        def _side_effect(cmd, **kwargs):  # noqa: ANN001, ANN202
            shell = cmd[2]
            if "%a" in shell:  # stat of (missing) target file -> new file
                return (1, (b"", b"No such file"))
            # Regression guard: never stat the bare /proc/self symlink, which
            # would return the root owner and re-introduce the bug.
            assert not ("stat" in shell and "/proc/self" in shell and " -L" not in shell), (
                f"running-user probe must dereference, got: {shell!r}"
            )
            return (0, (b"999\n999\n", b""))  # id -u; id -g

        container.exec_run.side_effect = _side_effect
        uid, gid, mode = _owner_for_write(container, "/tmp/new.py")
        assert (uid, gid) == (999, 999)

    def test_owner_for_write_falls_back_when_stat_unavailable(self) -> None:
        from sunaba.edit_verify import _owner_for_write

        container = MagicMock()
        container.exec_run.return_value = (127, (b"", b"stat: not found"))
        uid, gid, mode = _owner_for_write(container, "/x/f")
        assert (uid, gid, mode) == (999, 999, 0o644)

    @patch("sunaba.tools.file._docker")
    def test_write_carries_existing_owner_into_archive(
        self, mock_docker: MagicMock
    ) -> None:
        """End-to-end: write_file streams the resolved owner into the tar.

        Drives the full ``edit_file`` path through the realistic
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

        edit_file(
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
        from sunaba.edit_verify import _is_test_file
        assert _is_test_file("/root/main.py") is False
        assert _is_test_file("/home/src/app.py") is False
        assert _is_test_file("/root/utils.js") is False

    def test_test_prefix_is_test(self) -> None:
        from sunaba.edit_verify import _is_test_file
        assert _is_test_file("/root/test_main.py") is True
        assert _is_test_file("/home/tests/test_utils.py") is True

    def test_test_suffix_is_test(self) -> None:
        from sunaba.edit_verify import _is_test_file
        assert _is_test_file("/root/main_test.py") is True
        assert _is_test_file("/root/utils_test.go") is True

    def test_test_variant_suffix_is_test(self) -> None:
        from sunaba.edit_verify import _is_test_file
        assert _is_test_file("/root/utils_test_v2.go") is True
        assert _is_test_file("/root/model_test_v3.py") is True

    def test_dot_test_dot_is_test(self) -> None:
        from sunaba.edit_verify import _is_test_file
        assert _is_test_file("/root/app.test.js") is True
        assert _is_test_file("/root/component.spec.ts") is True

    def test_tests_directory_is_test(self) -> None:
        from sunaba.edit_verify import _is_test_file
        assert _is_test_file("/app/tests/test_main.py") is True
        assert _is_test_file("/app/test/test_main.py") is True
        assert _is_test_file("/app/__tests__/test_main.js") is True

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.edit_verify.record_file_write")
    def test_write_file_detects_test_file(
        self, mock_record: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        write_file(
            container_id="abc123",
            file_name="test_foo.py",
            file_contents="def test_foo(): pass",
            dest_dir="/tests",
        )
        mock_record.assert_called_once()
        _, kwargs = mock_record.call_args
        assert kwargs.get("is_test") is True

    @patch("sunaba.edit_verify.record_file_write")
    def test_write_file_uses_is_test(
        self, mock_record: MagicMock,
    ) -> None:
        from sunaba.edit_verify import write_file

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
        from sunaba.edit_verify import _is_test_file

        assert _is_test_file("/home/sandbox/test_main.py") is True
        assert _is_test_file("/home/sandbox/main.py") is False
        assert _is_test_file("/tmp/repo/tests/test_foo.py") is True
        assert _is_test_file("/tmp/repo/src/bar.py") is False

    @patch("sunaba.tools.file._docker")
    def test_write_file_dest_path_uses_forward_slash(
        self, mock_docker: MagicMock,
    ) -> None:
        """dest_path constructed by write_file must use forward slashes.

        Regression test: os.path.join would produce backslash-separated paths
        on Windows, causing "file not found" in Linux containers (Issue #219).
        """
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(b"existing content\n")
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = edit_file(
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

    @patch("sunaba.tools.file._docker")
    def test_write_file_full_overwrite_path(
        self, mock_docker: MagicMock,
    ) -> None:
        """Full overwrite mode also uses correct path (not os.path.join behavior)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file(
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


class TestTrailingNewlinePreservation:
    """The file's trailing newline survives partial edits (#570).

    Every other line-range test in this file happens to pass
    ``file_contents`` that already ends in ``\n``, which is exactly what a
    real caller does *not* do: asked to replace line 3 with ``CCC`` you
    write ``"CCC"``.  The final newline is a property of the file, not of
    the snippet, so it must be restored from the existing content.
    """

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

    @patch("sunaba.tools.file._docker")
    def test_last_line_replacement_keeps_trailing_newline(
        self, mock_docker: MagicMock,
    ) -> None:
        mock_container = self._mock_container_with_file(
            mock_docker, "aaa\nbbb\nccc\n",
        )

        result = edit_file(
            container_id="abc123",
            file_name="t.txt",
            file_contents="CCC",
            dest_dir="/root",
            start_line=3,
            end_line=3,
        )
        assert "Error" not in result
        assert _get_written_content(mock_container) == "aaa\nbbb\nCCC\n"

    @patch("sunaba.tools.file._docker")
    def test_middle_line_replacement_keeps_trailing_newline(
        self, mock_docker: MagicMock,
    ) -> None:
        mock_container = self._mock_container_with_file(
            mock_docker, "aaa\nbbb\nccc\n",
        )

        result = edit_file(
            container_id="abc123",
            file_name="t.txt",
            file_contents="BBB",
            dest_dir="/root",
            start_line=2,
            end_line=2,
        )
        assert "Error" not in result
        assert _get_written_content(mock_container) == "aaa\nBBB\nccc\n"

    @patch("sunaba.tools.file._docker")
    def test_file_without_trailing_newline_stays_without(
        self, mock_docker: MagicMock,
    ) -> None:
        mock_container = self._mock_container_with_file(
            mock_docker, "aaa\nbbb\nccc",
        )

        result = edit_file(
            container_id="abc123",
            file_name="t.txt",
            file_contents="BBB",
            dest_dir="/root",
            start_line=2,
            end_line=2,
        )
        assert "Error" not in result
        assert _get_written_content(mock_container) == "aaa\nBBB\nccc"

    @patch("sunaba.tools.file._docker")
    def test_snippet_newline_can_add_a_missing_final_newline(
        self, mock_docker: MagicMock,
    ) -> None:
        """An explicit trailing "\n" in the snippet still forces one."""
        mock_container = self._mock_container_with_file(
            mock_docker, "aaa\nbbb\nccc",
        )

        result = edit_file(
            container_id="abc123",
            file_name="t.txt",
            file_contents="CCC\n",
            dest_dir="/root",
            start_line=3,
            end_line=3,
        )
        assert "Error" not in result
        assert _get_written_content(mock_container) == "aaa\nbbb\nCCC\n"

    @patch("sunaba.tools.file._docker")
    def test_append_keeps_trailing_newline(self, mock_docker: MagicMock) -> None:
        mock_container = self._mock_container_with_file(
            mock_docker, "aaa\nbbb\n",
        )

        result = edit_file(
            container_id="abc123",
            file_name="t.txt",
            file_contents="ccc",
            dest_dir="/root",
            append=True,
        )
        assert "Error" not in result
        assert _get_written_content(mock_container) == "aaa\nbbb\nccc\n"

    @patch("sunaba.tools.file._docker")
    def test_append_to_file_without_trailing_newline_stays_without(
        self, mock_docker: MagicMock,
    ) -> None:
        mock_container = self._mock_container_with_file(mock_docker, "aaa\nbbb")

        result = edit_file(
            container_id="abc123",
            file_name="t.txt",
            file_contents="ccc",
            dest_dir="/root",
            append=True,
        )
        assert "Error" not in result
        assert _get_written_content(mock_container) == "aaa\nbbb\nccc"


class TestLlmErgonomicsGuards:
    """Anti-loop hints and parse-regression warnings (issue #599 follow-up).

    The only callers are LLMs: a mistake must come back as an actionable
    message that breaks retry loops, and a corrupting write must never
    stay silent.
    """

    # --- "edit may already be applied" hint on a failed match ---

    @patch("sunaba.tools.file._docker")
    def test_near_miss_notes_already_applied_contents(
        self, mock_docker: MagicMock,
    ) -> None:
        """Retrying an applied edit says so instead of a bare near-miss."""
        existing = "alpha\nreplacement text\ngamma\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="replacement text",
            dest_dir="/root",
            old_str="original text",
        )
        assert "Error: old_str not found" in result
        assert "file_contents already appears at line 2" in result
        assert "may have already been applied" in result

    @patch("sunaba.tools.file._docker")
    def test_no_already_applied_note_for_short_contents(
        self, mock_docker: MagicMock,
    ) -> None:
        """Tiny snippets appear coincidentally -- no misleading hint."""
        existing = "x = 1\ny = 2\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="y",
            dest_dir="/root",
            old_str="zzz not here",
        )
        assert "Error: old_str not found" in result
        assert "already been applied" not in result

    # --- parse-regression warning on .py writes ---

    @patch("sunaba.tools.file._docker")
    def test_broken_python_replace_warns_in_echo(
        self, mock_docker: MagicMock,
    ) -> None:
        """An old_str edit that breaks the file parsing gets a warning."""
        existing = "x = 1\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents='y = "unclosed',
            dest_dir="/root",
            old_str="x = 1",
        )
        assert "Error" not in result
        assert "Warning: /root/test.py does not parse as Python" in result
        assert "escaping artifacts" in result

    @patch("sunaba.tools.file._docker")
    def test_valid_python_replace_has_no_warning(
        self, mock_docker: MagicMock,
    ) -> None:
        existing = "x = 1\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="y = 2",
            dest_dir="/root",
            old_str="x = 1",
        )
        assert "Error" not in result
        assert "Warning" not in result

    @patch("sunaba.tools.file._docker")
    def test_broken_python_full_overwrite_warns(
        self, mock_docker: MagicMock,
    ) -> None:
        """The warning also covers full overwrites (no edit mode)."""
        _mock_container_with_file(mock_docker, "")

        result = write_file(
            container_id="abc123",
            file_name="test.py",
            file_contents="def broken(:\n    pass\n",
            dest_dir="/root",
        )
        assert "Written" in result
        assert "Warning: /root/test.py does not parse as Python" in result

    @patch("sunaba.tools.file._docker")
    def test_non_py_file_never_warns(self, mock_docker: MagicMock) -> None:
        _mock_container_with_file(mock_docker, "")

        result = write_file(
            container_id="abc123",
            file_name="notes.md",
            file_contents="def broken(:\n",
            dest_dir="/root",
        )
        assert "Written" in result
        assert "Warning" not in result

    # --- transform_file escape hatches at dead ends ---

    @patch("sunaba.tools.file._docker")
    def test_multi_match_error_suggests_transform_file(
        self, mock_docker: MagicMock,
    ) -> None:
        existing = "hello\nworld\nhello\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="HI",
            dest_dir="/root",
            old_str="hello",
        )
        assert "matches at 2 locations" in result
        assert "transform_file" in result

    @patch("sunaba.tools.file._docker")
    def test_near_miss_suggests_transform_file(
        self, mock_docker: MagicMock,
    ) -> None:
        existing = "aaa\nbbb\n"
        _mock_container_with_file(mock_docker, existing)

        result = edit_file(
            container_id="abc123",
            file_name="test.txt",
            file_contents="new",
            dest_dir="/root",
            old_str="zzz",
        )
        assert "Error: old_str not found" in result
        assert "transform_file" in result
