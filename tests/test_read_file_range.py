"""Tests for read_file_range and list_files tools."""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from sunaba import server, undo, workflow_guide
from sunaba.tools.file import list_files, read_file_range, transform_file, undo_file_edit
from tests.conftest import _FakeClient
from tests.test_edit_symbol import _FakeContainerWithIO
from tests.test_write_file import _exec_run_for, _get_written_content


def _make_container(exec_returns):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client(container):
    client = MagicMock()
    client.containers.get.return_value = container
    return client


class TestReadFileRange:
    """Issue #131: read_file_range must not raise NameError."""

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_no_nameerror(self, mock_docker):
        """Regression: 'name container is not defined' must not recur.

        Previously read_file_range referenced an undefined ``container``
        variable, failing every call with ``NameError``.  It must read the
        requested lines via the resolved container object instead.
        """
        file_body = "line0\nline1\nline2\nline3\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/home/sandbox/f.txt", offset=1, limit=2)
        )
        assert "error" not in result or result["error"] is None
        assert result["content"] == "line1\nline2"
        assert result["total_lines"] == 5  # trailing newline -> empty final line

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_container_not_found(self, mock_docker):
        """Missing container returns a JSON error, not a raised exception."""
        client = MagicMock()
        from docker.errors import NotFound
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        result = json.loads(read_file_range("abc123def456", "/f.txt"))
        assert "error" in result
        assert "not found" in result["error"]




class TestReadFileRangeStartEndLine:
    """Issue #386: start_line/end_line params must work correctly."""

    @patch("sunaba.tools.file._docker")
    def test_start_line_end_line_both_specified(self, mock_docker):
        """start_line=2, end_line=3 returns lines 2-3 (1-indexed, inclusive)."""
        file_body = "line0\nline1\nline2\nline3\nline4\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=2, end_line=3)
        )
        assert result["error"] is None
        assert result["content"] == "line1\nline2"
        assert result["shown"] == 2
        assert result["total_lines"] == 6

    @patch("sunaba.tools.file._docker")
    def test_start_line_only_reads_to_end(self, mock_docker):
        """start_line=3 (no end_line) reads from line 3 to end."""
        file_body = "line0\nline1\nline2\nline3\nline4\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=3)
        )
        assert result["error"] is None
        assert result["content"] == "line2\nline3\nline4\n"
        assert result["shown"] == 4  # trailing newline -> empty final line
        assert result["total_lines"] == 6

    @patch("sunaba.tools.file._docker")
    def test_start_line_end_line_single_line(self, mock_docker):
        """start_line=1, end_line=1 returns exactly one line."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=1, end_line=1)
        )
        assert result["error"] is None
        assert result["content"] == "line0"
        assert result["shown"] == 1


    @patch("sunaba.tools.file._docker")
    def test_start_line_and_offset_mutually_exclusive(self, mock_docker):
        """start_line and non-zero offset together raise error."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=2, offset=3)
        )
        assert "error" in result
        assert "mutually exclusive" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_start_line_zero_rejected(self, mock_docker):
        """start_line=0 is rejected (must be >= 1)."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=0)
        )
        assert "error" in result
        assert "must be >= 1" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_end_line_less_than_start_line_rejected(self, mock_docker):
        """end_line < start_line is rejected."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", start_line=3, end_line=1)
        )
        assert "error" in result
        assert "end_line must be >= start_line" in result["error"]


    @patch("sunaba.tools.file._docker")
    def test_offset_limit_still_works(self, mock_docker):
        """Backward compatibility: offset/limit still function."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", offset=0, limit=2)
        )
        assert result["error"] is None
        assert result["content"] == "line0\nline1"
        assert result["shown"] == 2


class TestListFiles:
    """Tests for list_files tool."""

    @patch("sunaba.tools.file._docker")
    def test_successful_list(self, mock_docker):
        """Successful listing returns file paths."""
        files = (
            "/root/file1.py\n"
            "/root/subdir/file2.py\n"
            "/root/subdir/file3.md\n"
        )
        container = _make_container([
            (0, files.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/root"))
        assert result["total"] == 3
        assert "/root/file1.py" in result["files"]
        assert "/root/subdir/file2.py" in result["files"]

    @patch("sunaba.tools.file._docker")
    def test_list_with_pattern(self, mock_docker):
        """List with glob pattern filter."""
        py_files = "/root/file1.py\n/root/file2.py\n"
        container = _make_container([
            (0, py_files.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            list_files("abc123def456", "/root", pattern="*.py")
        )
        assert result["total"] == 2

    @patch("sunaba.tools.file._docker")
    def test_list_empty_directory(self, mock_docker):
        """Empty directory returns empty list."""
        container = _make_container([
            (0, b"\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/root"))
        assert result["total"] == 0
        assert result["files"] == []

    @patch("sunaba.tools.file._docker")
    def test_list_error(self, mock_docker):
        """Find error returns error field."""
        container = _make_container([
            (1, b"", b"find: /nonexistent: No such file or directory\n"),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456", "/nonexistent"))
        assert "error" in result

    @patch("sunaba.tools.file._docker")
    def test_list_default_path(self, mock_docker):
        """Default path is /home/sandbox."""
        container = _make_container([
            (0, b"", b""),
            (0, b"", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(list_files("abc123def456"))
        assert result["path"] == "/workspace"
        assert result["total"] == 0
        assert result["files"] == []


class TestReadFileRangeTailLines:
    """Issue #847: tail_lines=N reads a file's end without a shell tail.

    Reading the last N lines used to need the line count first (one call to
    learn total_lines, one to read from it), so `tail -3 log` stayed cheaper
    than the tool and the reads left the sandbox.
    """

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_returns_last_n_lines(self, mock_docker):
        """tail_lines=3 on a 10-line file returns lines 8-10, like `tail -3`.

        The file ends in a newline, which splits into a phantom empty final
        element; counting it as a line would return lines 9-10 plus a blank.
        """
        file_body = "".join(f"line{i}\n" for i in range(1, 11))
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=3)
        )
        assert result["error"] is None
        assert result["content"] == "line8\nline9\nline10"
        assert result["shown"] == 3
        assert result["total_lines"] == 11  # trailing newline -> empty final line

    @patch("sunaba.tools.file._docker")
    def test_tail_read_ends_at_the_last_line(self, mock_docker):
        """A tail window reaches EOF: has_more false, next_offset null."""
        file_body = "".join(f"line{i}\n" for i in range(1, 11))
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=3)
        )
        assert result["has_more"] is False
        assert result["next_offset"] is None

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_past_start_of_file_returns_every_line(self, mock_docker):
        """A file shorter than N returns all of it, not an error."""
        file_body = "line1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=3)
        )
        assert result["error"] is None
        assert result["content"] == "line1\nline2"
        assert result["shown"] == 2

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_without_trailing_newline(self, mock_docker):
        """A file whose last line is unterminated still tails correctly."""
        file_body = "line1\nline2\nline3"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=2)
        )
        assert result["error"] is None
        assert result["content"] == "line2\nline3"
        assert result["shown"] == 2

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_zero_rejected(self, mock_docker):
        """tail_lines=0 is rejected rather than silently returning nothing."""
        container = _make_container([
            (0, b"line1\nline2\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=0)
        )
        assert "error" in result
        assert "tail_lines must be >= 1" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_negative_rejected(self, mock_docker):
        """A negative tail_lines is an error, not a slice from the front."""
        container = _make_container([
            (0, b"line1\nline2\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=-3)
        )
        assert "error" in result
        assert "tail_lines must be >= 1" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_and_start_line_rejected(self, mock_docker):
        """tail_lines is its own addressing mode, not a filter on a range."""
        container = _make_container([
            (0, b"line1\nline2\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=2, start_line=1)
        )
        assert "error" in result
        assert "mutually exclusive" in result["error"]
        assert "start_line" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_and_end_line_rejected(self, mock_docker):
        """end_line addresses from the front; combining it is a mistake."""
        container = _make_container([
            (0, b"line1\nline2\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=2, end_line=5)
        )
        assert "error" in result
        assert "mutually exclusive" in result["error"]
        assert "end_line" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_and_offset_rejected(self, mock_docker):
        """A non-zero offset with tail_lines is ambiguous, so it is refused."""
        container = _make_container([
            (0, b"line1\nline2\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=2, offset=3)
        )
        assert "error" in result
        assert "mutually exclusive" in result["error"]
        assert "offset" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_and_non_default_limit_rejected(self, mock_docker):
        """limit sizes a forward page; tail_lines already sizes its own."""
        container = _make_container([
            (0, b"line1\nline2\n", b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=2, limit=10)
        )
        assert "error" in result
        assert "mutually exclusive" in result["error"]
        assert "limit" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_tail_lines_with_limit_left_at_its_default(self, mock_docker):
        """Passing the default limit explicitly is not a conflict."""
        file_body = "line1\nline2\nline3\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/f.txt", tail_lines=2, limit=50)
        )
        assert result["error"] is None
        assert result["content"] == "line2\nline3"


class TestFilePathAlias:
    """Issue #876: ``path`` is accepted as an alias for ``file_path``.

    A model that guesses ``path`` on the file_path tools must not lose
    the round: the three tools accept both names, and refuse ambiguity
    instead of silently picking one.  All four resolution cases are
    covered here for read_file_range; the plain ``path=`` success case
    for undo_file_edit and transform_file proves the three agree.

    ``path`` is keyword-only on all three tools (it sits after a bare
    ``*``), so the positional contract from before the alias held by
    construction: the parameter after ``file_path`` still binds where it
    always did, and a positional ``path`` is impossible.
    """

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_path_alias_reads_the_same_file(self, mock_docker):
        """path= and file_path= name the same file, identically."""
        file_body = "line0\nline1\nline2\n"
        container = _make_container([
            (0, file_body.encode(), b""),
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        via_file_path = json.loads(
            read_file_range("abc123def456", "/f.txt", limit=-1)
        )
        via_path = json.loads(
            read_file_range("abc123def456", path="/f.txt", limit=-1)
        )
        assert via_path == via_file_path
        assert via_path["content"] == "line0\nline1\nline2\n"

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_neither_name_is_an_error_naming_file_path(
        self, mock_docker,
    ):
        """Neither name: refused, with the error pointing at file_path."""
        result = json.loads(read_file_range("abc123def456"))
        assert result["status"] == "error"
        assert "file_path" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_equal_values_are_accepted(self, mock_docker):
        """Both names, same value: not ambiguous, reads normally."""
        file_body = "line0\nline1\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", file_path="/f.txt", path="/f.txt")
        )
        assert result["error"] is None
        assert result["content"] == "line0\nline1\n"

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_conflicting_values_are_an_error(self, mock_docker):
        """Both names, different values: refused, never silently picked."""
        result = json.loads(
            read_file_range("abc123def456", file_path="/a.txt", path="/b.txt")
        )
        assert result["status"] == "error"
        assert "disagree" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_undo_file_edit_path_alias_restores_the_same_file(
        self, mock_docker, monkeypatch,
    ):
        """undo_file_edit accepts path= where it took file_path=."""
        good = "def foo():\n    return 1\n"
        broken = "def foo(:\n    pass\n"
        undo.save_version("abc123def456", "/sandbox/alias.py", good)
        container = MagicMock()
        container.exec_run.side_effect = _exec_run_for(broken.encode("utf-8"))
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client
        monkeypatch.setattr("sunaba.tools.file.record_tool_use", lambda *a, **k: None)

        result = json.loads(
            undo_file_edit(container_id="abc123def456", path="/sandbox/alias.py")
        )
        assert result["status"] == "ok"
        assert result["restored_steps_back"] == 1
        assert _get_written_content(container) == good

    def test_transform_file_path_alias_applies_the_transform(
        self, tmp_path, monkeypatch,
    ):
        """transform_file accepts path= where it took file_path=."""
        posix = "/sandbox/alias_target.py"
        f = tmp_path / "alias_target.py"
        f.write_text("aaa\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({posix: str(f)})),
        )
        monkeypatch.setattr("sunaba.tools.file.record_tool_use", lambda *a, **k: None)
        monkeypatch.setattr(
            "sunaba.edit_verify.edits.record_file_write", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "src.sunaba.edit_verify.fileio.record_file_write", lambda *a, **k: None
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            path=posix,
            code="def transform(text):\n    return text.upper()\n",
        ))
        assert result["status"] == "ok"
        assert result["changed"] is True
        assert f.read_text(encoding="utf-8") == "AAA\n"

    @patch("sunaba.tools.file._docker")
    def test_read_file_range_second_positional_still_binds_offset(self, mock_docker):
        """read_file_range(cid, path, N, M) binds N to offset and M to limit.

        The alias is keyword-only, so the parameters that followed
        file_path before the alias existed bind exactly where they did:
        a mid-signature path would swallow the third positional.
        """
        file_body = "line0\nline1\nline2\nline3\n"
        container = _make_container([
            (0, file_body.encode(), b""),
        ])
        mock_docker.return_value = _make_client(container)

        result = json.loads(
            read_file_range("abc123def456", "/home/sandbox/f.txt", 1, 2)
        )
        assert result["error"] is None
        assert result["content"] == "line1\nline2"

    @patch("sunaba.tools.file._docker")
    def test_undo_file_edit_second_positional_still_binds_steps(
        self, mock_docker, monkeypatch,
    ):
        """undo_file_edit(cid, path, N) binds N to steps, as before the alias."""
        good = "def foo():\n    return 1\n"
        broken = "def foo(:\n    pass\n"
        undo.save_version("abc123def456", "/sandbox/alias.py", good)
        undo.save_version("abc123def456", "/sandbox/alias.py", broken)
        container = MagicMock()
        container.exec_run.side_effect = _exec_run_for(broken.encode("utf-8"))
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client
        monkeypatch.setattr("sunaba.tools.file.record_tool_use", lambda *a, **k: None)

        result = json.loads(
            undo_file_edit("abc123def456", "/sandbox/alias.py", 2)
        )
        assert result["status"] == "ok"
        assert result["restored_steps_back"] == 2
        assert _get_written_content(container) == good

    def test_transform_file_second_positional_still_binds_code(
        self, tmp_path, monkeypatch,
    ):
        """transform_file(cid, path, code) binds code positionally, as before."""
        posix = "/sandbox/alias_target.py"
        f = tmp_path / "alias_target.py"
        f.write_text("aaa\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({posix: str(f)})),
        )
        monkeypatch.setattr("sunaba.tools.file.record_tool_use", lambda *a, **k: None)
        monkeypatch.setattr(
            "sunaba.edit_verify.edits.record_file_write", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "src.sunaba.edit_verify.fileio.record_file_write", lambda *a, **k: None
        )

        result = json.loads(transform_file(
            "abc123def456",
            posix,
            "def transform(text):\n    return text.upper()\n",
        ))
        assert result["status"] == "ok"
        assert result["changed"] is True
        assert f.read_text(encoding="utf-8") == "AAA\n"

    def test_read_file_range_path_cannot_be_passed_positionally(self):
        """path is keyword-only: passing it positionally raises TypeError."""
        with pytest.raises(TypeError):
            read_file_range(
                "abc123def456", "/f.txt", 0, 50, None, None, None, "/f.txt"
            )

    def test_undo_file_edit_path_cannot_be_passed_positionally(self):
        """path is keyword-only: passing it positionally raises TypeError."""
        with pytest.raises(TypeError):
            undo_file_edit(
                "abc123def456", "/sandbox/alias.py", 1, "/sandbox/alias.py"
            )

    def test_transform_file_path_cannot_be_passed_positionally(self):
        """path is keyword-only: passing it positionally raises TypeError."""
        with pytest.raises(TypeError):
            transform_file(
                "abc123def456", "/sandbox/alias_target.py", "", None, 200, 0, 100,
                "/sandbox/alias_target.py",
            )


class TestTailGuidanceSurfaces:
    """Issue #847: the guidance a model reads must point tail/sed reads here.

    The tool existing is not enough -- both mapping surfaces name the raw
    command a model would otherwise reach for.
    """

    def _read_file_range_tool(self):
        tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
        return tools["read_file_range"]

    def test_description_body_states_the_tail_capability(self):
        """FastMCP drops everything from ``Args:`` on, so the body carries it."""
        description = self._read_file_range_tool().description or ""
        assert "tail_lines" in description, (
            "the tail capability is below Args: and invisible to the client"
        )
        assert "tail" in description

    def test_tail_lines_is_exposed_as_a_parameter(self):
        props = (self._read_file_range_tool().parameters or {}).get("properties") or {}
        assert "tail_lines" in props
        assert props["tail_lines"].get("description")

    def test_server_instructions_map_tail_and_reading_sed(self):
        mapping = [
            line
            for line in server.SERVER_INSTRUCTIONS.split("\n")
            if line.startswith("Prefer dedicated tools")
        ]
        assert mapping, "the shell->tool mapping line is gone"
        line = mapping[0]
        assert "tail" in line, "tail still maps to nothing"
        assert "tail_lines=N" in line, "the mapping does not say how to tail"
        assert "sed -n" in line, "reading sed is not distinguished from editing sed"
        assert "read_file_range" in line
        assert "edit_file/transform_file" in line, "editing sed lost its mapping"

    def test_tail_lines_scoped_to_the_tail_mapping_only(self):
        """tail_lines=N binds to `tail -n N`, not to the cat/head/sed group.

        The one-line surface once read 'cat/head/tail/sed -n->read_file_range
        (tail_lines=N)', scoping the parameter to the whole group: a model
        mapping a plain cat/head read or a sed -n 'A,Bp' range read could
        pass tail_lines=N and silently receive the file's end.  The only
        occurrence of tail_lines=N must live inside the tail mapping.
        """
        line = [
            candidate
            for candidate in server.SERVER_INSTRUCTIONS.split("\n")
            if candidate.startswith("Prefer dedicated tools")
        ][0]
        tail_map = "tail -n N->read_file_range(tail_lines=N)"
        assert tail_map in line, "the tail mapping must bind tail_lines=N itself"
        assert "cat/head/tail/sed -n->read_file_range (tail_lines=N)" not in line, (
            "the group-scoped form lets cat/head/sed -n reads pass tail_lines=N"
        )
        assert "tail_lines=N" not in line.replace(tail_map, ""), (
            "tail_lines=N must not appear on any non-tail mapping"
        )

    def test_workflow_guide_maps_tail_and_reading_sed(self):
        guide = (
            pathlib.Path(workflow_guide.__file__).resolve().parent / "workflow_guide.md"
        ).read_text("utf-8")
        explore = guide.split("## phase: explore", 1)[1].split("\n## phase:", 1)[0]
        assert "tail" in explore, "the explore map does not mention tail"
        assert "tail_lines=N" in explore
        assert "sed -n" in explore, "the explore map does not cover reading sed"
