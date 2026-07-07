"""Tests for diff_in_container tool (Issue #476)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.diff import (
    _parse_diffstat,
    diff_in_container,
)


class TestParseDiffstat:
    """Tests for _parse_diffstat helper."""

    def test_basic_diffstat(self):
        lines = [
            " src/foo.py | 10 ++++++----",
            " src/bar.py | 3 ++-",
            " 2 files changed, 8 insertions(+), 5 deletions(-)",
        ]
        result = _parse_diffstat(lines)
        assert len(result) == 2
        assert result[0] == {
            "path": "src/foo.py",
            "additions": 6,
            "deletions": 4,
            "changes": 10,
        }
        assert result[1] == {
            "path": "src/bar.py",
            "additions": 2,
            "deletions": 1,
            "changes": 3,
        }

    def test_single_file(self):
        lines = [
            " README.md | 2 +-",
            " 1 file changed, 1 insertion(+), 1 deletion(-)",
        ]
        result = _parse_diffstat(lines)
        assert len(result) == 1
        assert result[0]["path"] == "README.md"
        assert result[0]["additions"] == 1
        assert result[0]["deletions"] == 1

    def test_new_file(self):
        lines = [
            " new_file.py | 15 +++++++++++++++",
            " 1 file changed, 15 insertions(+)",
        ]
        result = _parse_diffstat(lines)
        assert len(result) == 1
        assert result[0]["path"] == "new_file.py"
        assert result[0]["additions"] == 15
        assert result[0]["deletions"] == 0

    def test_deleted_file(self):
        lines = [
            " deleted.py | 12 --------------------",
            " 1 file changed, 12 deletions(-)",
        ]
        result = _parse_diffstat(lines)
        assert len(result) == 1
        assert result[0]["path"] == "deleted.py"
        assert result[0]["additions"] == 0
        # The visual bar has 20 `-` characters for 12 deletions (scaled)
        assert result[0]["deletions"] == 20

    def test_empty_lines(self):
        result = _parse_diffstat([])
        assert result == []

    def test_no_stat_lines_only_total(self):
        result = _parse_diffstat([" 0 files changed"])
        assert result == []

    def test_no_changes(self):
        result = _parse_diffstat([" 0 files changed"])
        assert result == []


def _make_container_for_summary(diff_stdout: str, meta: str = "{}") -> MagicMock:
    """Create a container mock that returns the given diff output.

    The mock responds to:
    1. Meta read (cat .sandbox-meta.json)
    2. git diff --stat
    """
    container = MagicMock()
    container.exec_run.side_effect = lambda cmd, **kw: (
        _exec_for(cmd, diff_stdout, meta)
        if isinstance(cmd, list) and len(cmd) == 3
        else (0, (b"", b""))
    )
    return container


def _exec_for(cmd: object, diff_stdout: str, meta: str):
    cmd_str = cmd[-1] if isinstance(cmd, list) and len(cmd) == 3 else str(cmd)
    if "cat /home/sandbox/.sandbox-meta.json" in cmd_str:
        return (0, (meta.encode(), b""))
    if "git diff" in cmd_str:
        return (0, (diff_stdout.encode(), b""))
    return (0, (b"", b""))


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
        git_parts = [
            (0, (b'{"clone_path":"/repo"}', b"")),
            (0, (b"", b"")),
        ]
        container.exec_run.side_effect = git_parts

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
        git_output = (
            " src/foo.py | 10 ++++++----\n"
            " src/bar.py | 3 ++-\n"
            " 2 files changed, 8 insertions(+), 5 deletions(-)\n"
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
            result = json.loads(diff_in_container("abc123def456", base="main"))

        assert result["total_files"] == 2
        assert result["total_additions"] == 8
        assert result["total_deletions"] == 5

    def test_summary_with_base_from_meta(self):
        container = MagicMock()
        git_output = " new.py | 5 +++++\n 1 file changed, 5 insertions(+)\n"
        side_effects = [
            (0, (b'{"clone_path":"/repo","base_branch":"main"}', b"")),
            (0, (git_output.encode(), b"")),
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
