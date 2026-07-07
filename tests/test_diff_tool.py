"""Tests for diff_in_container tool (Issue #476)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.diff import (
    _parse_numstat,
    diff_in_container,
)


class TestParseNumstat:
    """Tests for _parse_numstat helper (git diff --numstat parser)."""

    def test_basic_numstat(self):
        lines = [
            "10\t5\tsrc/foo.py",
            "3\t1\tsrc/bar.py",
        ]
        result = _parse_numstat(lines)
        assert len(result) == 2
        assert result[0] == {
            "path": "src/foo.py",
            "additions": 10,
            "deletions": 5,
            "changes": 15,
        }
        assert result[1] == {
            "path": "src/bar.py",
            "additions": 3,
            "deletions": 1,
            "changes": 4,
        }

    def test_single_file(self):
        result = _parse_numstat(["1\t1\tREADME.md"])
        assert len(result) == 1
        assert result[0]["path"] == "README.md"
        assert result[0]["additions"] == 1
        assert result[0]["deletions"] == 1

    def test_new_file(self):
        result = _parse_numstat(["15\t0\tnew_file.py"])
        assert result[0]["additions"] == 15
        assert result[0]["deletions"] == 0

    def test_deleted_file(self):
        result = _parse_numstat(["0\t12\tdeleted.py"])
        assert result[0]["additions"] == 0
        assert result[0]["deletions"] == 12

    def test_binary_file(self):
        result = _parse_numstat(["-\t-\timage.png"])
        assert len(result) == 1
        assert result[0]["path"] == "image.png"
        assert result[0]["binary"] is True
        assert result[0]["additions"] == 0
        assert result[0]["deletions"] == 0

    def test_empty_input(self):
        assert _parse_numstat([]) == []

    def test_no_tab_separator_skipped(self):
        result = _parse_numstat(["not a valid line"])
        assert result == []


class TestDiffInContainer:
    """Tests for diff_in_container tool."""

    def test_container_not_found(self):
        with patch(
            "code_sandbox_mcp.tools.diff._docker"
        ) as mock_docker:
            mock_client = MagicMock()
            mock_client.containers.get.side_effect = Exception("not found")
            mock_docker.return_value = mock_client

            result = json.loads(diff_in_container("nonexistent"))
            assert result["status"] == "error"

    def test_summary_no_changes(self):
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert "files" in result
        assert result["total_files"] == 0
        assert result["total_additions"] == 0
        assert result["total_deletions"] == 0

    def test_summary_with_changes(self):
        container = MagicMock()
        numstat_output = (
            "10\t5\tsrc/foo.py\n"
            "3\t1\tsrc/bar.py\n"
        )
        container.exec_run.return_value = (0, (numstat_output.encode(), b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main"))

        assert result["total_files"] == 2
        assert result["total_additions"] == 13
        assert result["total_deletions"] == 6

    def test_summary_with_base_from_meta(self):
        container = MagicMock()
        side_effects = [
            (0, (b'{"clone_path":"/repo","base_branch":"main"}', b"")),
            (0, (b"5\t0\tnew.py\n", b"")),
        ]
        container.exec_run.side_effect = side_effects

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "new.py"

    def test_file_mode_with_hunks(self):
        container = MagicMock()
        git_output = (
            "diff --git a/foo.py b/foo.py\n"
            "index abc..def 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "-old line\n"
            "+new line\n"
            "+another line\n"
            " line3\n"
            "@@ -10,5 +11,8 @@\n"
            " context\n"
            " context\n"
            "-removed\n"
            "+added1\n"
            "+added2\n"
            "+added3\n"
            " context\n"
        )
        container.exec_run.return_value = (0, (git_output.encode(), b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main", path="foo.py"
            ))

        assert result["path"] == "foo.py"
        assert result["total"] == 2
        assert len(result["hunks"]) == 2
        assert result["hunks"][0]["old_start"] == 1
        assert result["hunks"][0]["old_count"] == 3
        assert result["hunks"][0]["new_start"] == 1
        assert result["hunks"][0]["new_count"] == 4
        assert result["hunks"][1]["old_start"] == 10
        assert not result["truncated"]

    def test_file_mode_pagination(self):
        container = MagicMock()
        git_output = (
            "@@ -1,3 +1,4 @@\n"
            " a\n"
            "-b\n"
            "+c\n"
            "@@ -5,2 +6,2 @@\n"
            " d\n"
            "-e\n"
            "+f\n"
            "@@ -8,1 +9,3 @@\n"
            " g\n"
            "+h\n"
            "+i\n"
        )
        container.exec_run.return_value = (0, (git_output.encode(), b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main", path="foo.py", offset=1, limit=1
            ))

        assert result["total"] == 3
        assert result["shown"] == 1
        assert result["truncated"] is True
        assert result["next_offset"] == 2
        assert result["hunks"][0]["old_start"] == 5

    def test_file_mode_no_diff(self):
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main", path="unchanged.py"
            ))

        assert result["status"] == "error"
        assert "No diff" in result["error"]

    def test_git_diff_failure(self):
        container = MagicMock()
        container.exec_run.return_value = (128, (b"fatal: not a git repository", b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main"))

        assert result["status"] == "error"
        assert "git diff failed" in result["error"]

    def test_no_newline_at_eof(self):
        """``\\ No newline at end of file`` is included in the preceding hunk."""
        container = MagicMock()
        git_output = (
            "@@ -1,2 +1,3 @@\n"
            " a\n"
            "-b\n"
            "+c\n"
            "+d\n"
            "\\ No newline at end of file\n"
        )
        container.exec_run.return_value = (0, (git_output.encode(), b""))

        with patch(
            "code_sandbox_mcp.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "code_sandbox_mcp.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "code_sandbox_mcp.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main", path="foo.py"
            ))

        assert result["total"] == 1
        assert "\\ No newline" in result["hunks"][0]["content"]
