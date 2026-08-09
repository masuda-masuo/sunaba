"""Tests for read_file_range and list_files tools."""

from __future__ import annotations

import asyncio
import json
import pathlib
from unittest.mock import MagicMock, patch

from sunaba import server, workflow_guide
from sunaba.tools.file import list_files, read_file_range


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
