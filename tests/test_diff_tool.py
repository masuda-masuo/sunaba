"""Tests for diff_in_container tool (Issue #476, #500)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sunaba.tools.common import _parse_numstat
from sunaba.tools.diff import (
    _parse_name_status,
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


class TestParseNameStatus:
    """Tests for _parse_name_status helper (git diff --name-status parser)."""

    def test_modified(self):
        result = _parse_name_status(["M\tsrc/foo.py"])
        assert result == {"src/foo.py": "M"}

    def test_added(self):
        result = _parse_name_status(["A\tsrc/new.py"])
        assert result == {"src/new.py": "A"}

    def test_deleted(self):
        result = _parse_name_status(["D\tsrc/deleted.py"])
        assert result == {"src/deleted.py": "D"}

    def test_renamed(self):
        result = _parse_name_status(["R100\tsrc/old.py\tsrc/new.py"])
        assert result == {"src/new.py": "R"}

    def test_copied(self):
        result = _parse_name_status(["C080\tsrc/orig.py\tsrc/copy.py"])
        assert result == {"src/copy.py": "C"}

    def test_multiple_files(self):
        result = _parse_name_status([
            "M\tsrc/foo.py",
            "A\tsrc/new.py",
            "R100\tsrc/old.py\tsrc/renamed.py",
        ])
        assert result == {
            "src/foo.py": "M",
            "src/new.py": "A",
            "src/renamed.py": "R",
        }

    def test_empty_input(self):
        assert _parse_name_status([]) == {}

    def test_no_tab_separator_skipped(self):
        result = _parse_name_status(["not valid"])
        assert result == {}


class TestDiffInContainer:
    """Tests for diff_in_container tool."""

    def test_container_not_found(self):
        with patch(
            "sunaba.tools.diff._docker"
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
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
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
        ).encode()
        name_status_output = b"M\tsrc/foo.py\nM\tsrc/bar.py\n"

        def exec_side_effect(cmd, **kwargs):
            if "--numstat" in cmd[-1]:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd[-1]:
                return (0, (name_status_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main"))

        assert result["total_files"] == 2
        assert result["total_additions"] == 13
        assert result["total_deletions"] == 6
        assert result["files"][0]["status"] == "M"
        assert result["files"][1]["status"] == "M"

    def test_summary_with_mixed_statuses(self):
        """Files should have correct status from --name-status."""
        container = MagicMock()
        numstat_output = b"10\t5\tsrc/foo.py\n0\t8\tdeleted.py\n15\t0\tnew.py\n"
        name_status_output = b"M\tsrc/foo.py\nD\tdeleted.py\nA\tnew.py\n"

        def exec_side_effect(cmd, **kwargs):
            if "--numstat" in cmd[-1]:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd[-1]:
                return (0, (name_status_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main"))

        statuses = {f["path"]: f["status"] for f in result["files"]}
        assert statuses == {
            "src/foo.py": "M",
            "deleted.py": "D",
            "new.py": "A",
        }

    def test_summary_with_renamed_file(self):
        """Renamed files get status 'R' from --name-status."""
        container = MagicMock()
        numstat_output = b"0\t0\tsrc/renamed.py\n"
        name_status_output = b"R100\tsrc/old.py\tsrc/renamed.py\n"

        def exec_side_effect(cmd, **kwargs):
            if "--numstat" in cmd[-1]:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd[-1]:
                return (0, (name_status_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main"))

        assert result["files"][0]["status"] == "R"
        assert result["files"][0]["path"] == "src/renamed.py"

    def test_summary_raw_escape_hatch(self):
        """When raw=True, raw_diff is included in the summary response."""
        container = MagicMock()
        numstat_output = b"5\t0\tnew.py\n"
        name_status_output = b"A\tnew.py\n"
        raw_diff_output = b"diff --git a/new.py b/new.py\n..."

        def exec_side_effect(cmd, **kwargs):
            cmd_str = cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            else:
                return (0, (raw_diff_output, b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main", raw=True))

        assert "raw_diff" in result
        assert "diff --git" in result["raw_diff"]

    def test_summary_raw_false_default(self):
        """When raw=False (default), raw_diff is NOT included."""
        container = MagicMock()
        numstat_output = b"5\t0\tnew.py\n"
        name_status_output = b"A\tnew.py\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="main"))

        assert "raw_diff" not in result

    def test_summary_with_base_from_meta(self):
        container = MagicMock()
        side_effects = [
            (0, (b'{"clone_path":"/repo","base_branch":"main"}', b"")),
            (0, (b"5\t0\tnew.py\n", b"")),
            (0, (b"A\tnew.py\n", b"")),
        ]
        container.exec_run.side_effect = side_effects

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
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
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
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
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
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
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
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
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
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
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main", path="foo.py"
            ))

        assert result["total"] == 1
        assert "\\ No newline" in result["hunks"][0]["content"]

    def test_file_mode_raw_escape_hatch(self):
        """When raw=True in file mode, raw_diff is included."""
        container = MagicMock()
        git_output = "@@ -1,1 +1,1 @@\n-old\n+new\n"
        container.exec_run.return_value = (0, (git_output.encode(), b""))

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main", path="foo.py", raw=True
            ))

        assert "raw_diff" in result
        assert result["raw_diff"] == git_output

    # --- worktree=True tests (Issue #633) ---

    def test_worktree_tracked_changes(self):
        """worktree=True shows tracked file changes (git diff HEAD, no triple-dot)."""
        container = MagicMock()
        numstat_output = b"10\t5\tsrc/foo.py\n"
        name_status_output = b"M\tsrc/foo.py\n"
        ls_files_output = b""  # no untracked

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (ls_files_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True,
            ))

        assert result["total_files"] == 1
        assert result["total_additions"] == 10
        assert result["total_deletions"] == 5
        assert result["files"][0]["path"] == "src/foo.py"
        assert result["files"][0]["status"] == "M"

    def test_worktree_untracked_files_summary(self):
        """worktree=True includes untracked files in summary mode."""
        container = MagicMock()
        numstat_output = b""  # no tracked changes
        name_status_output = b""
        ls_files_output = b"3 new_file.py\n5 other.py\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (ls_files_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True,
            ))

        assert result["total_files"] == 2
        assert result["total_additions"] == 8  # 3 + 5
        assert result["total_deletions"] == 0

        # Verify no "total" entry from wc -l aggregate line (#633 follow-up)
        assert not any(f.get("path") == "total" for f in result["files"]), (
            "wc -l 'total' aggregate line was not filtered"
        )

        paths_to_status = {f["path"]: f["status"] for f in result["files"]}
        assert paths_to_status == {
            "new_file.py": "untracked",
            "other.py": "untracked",
        }
        # Verify line counts
        for f in result["files"]:
            if f["path"] == "new_file.py":
                assert f["additions"] == 3
                assert f["changes"] == 3
            elif f["path"] == "other.py":
                assert f["additions"] == 5
                assert f["changes"] == 5

    def test_worktree_untracked_file_mode(self):
        """worktree=True file mode with untracked file uses --no-index diff."""
        container = MagicMock()
        diff_output = (
            "diff --git a/new_file.py b/new_file.py\n"
            "new file mode 100644\n"
            "index 0000000..abc1234\n"
            "--- /dev/null\n"
            "+++ b/new_file.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line1\n"
            "+line2\n"
            "+line3\n"
        )

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--no-index" in cmd_str:
                return (0, (diff_output.encode(), b""))
            elif "ls-files" in cmd_str:
                return (0, (b"new_file.py\n", b""))
            # git diff HEAD -- path → empty for untracked
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True, path="new_file.py",
            ))

        assert result["path"] == "new_file.py"
        assert result["total"] == 1
        assert len(result["hunks"]) == 1
        assert result["hunks"][0]["new_start"] == 1
        assert result["hunks"][0]["new_count"] == 3
        assert "+line1" in result["hunks"][0]["content"]

    def test_worktree_no_changes(self):
        """worktree=True with no tracked or untracked changes returns empty."""
        container = MagicMock()
        numstat_output = b""
        name_status_output = b""
        ls_files_output = b""

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (ls_files_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True,
            ))

        assert result["total_files"] == 0
        assert result["total_additions"] == 0
        assert result["total_deletions"] == 0

    def test_worktree_mixed_tracked_and_untracked(self):
        """worktree=True combines tracked changes with untracked files."""
        container = MagicMock()
        numstat_output = b"10\t5\tsrc/foo.py\n"
        name_status_output = b"M\tsrc/foo.py\n"
        ls_files_output = b"3 new_file.py\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (ls_files_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True,
            ))

        assert result["total_files"] == 2
        assert result["total_additions"] == 13  # 10 + 3
        assert result["total_deletions"] == 5

        paths_to_status = {f["path"]: f["status"] for f in result["files"]}
        assert paths_to_status == {
            "src/foo.py": "M",
            "new_file.py": "untracked",
        }

    def test_worktree_untracked_file_mode_no_index_exit_1(self):
        """git diff --no-index exit code 1 is treated as success."""
        container = MagicMock()
        diff_output = (
            "@@ -0,0 +1,2 @@\n"
            "+a\n"
            "+b\n"
        )

        # exit code 1 is normal for --no-index with differences
        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--no-index" in cmd_str:
                return (1, (diff_output.encode(), b""))
            elif "ls-files" in cmd_str and "--others" in cmd_str:
                return (0, (b"untracked.py\n", b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True, path="untracked.py",
            ))

        assert result["path"] == "untracked.py"
        assert result["total"] == 1
        assert len(result["hunks"]) == 1
        assert result["hunks"][0]["new_start"] == 1
        assert result["hunks"][0]["new_count"] == 2

    def test_worktree_file_mode_tracked_with_changes(self):
        """worktree=True file mode with tracked file uses git diff HEAD -- path."""
        container = MagicMock()
        git_output = (
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "-old line\n"
            "+new line\n"
            "+extra line\n"
            " line3\n"
        )

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--" in cmd_str and "--no-index" not in cmd_str:
                # This is the git diff HEAD -- path call
                return (0, (git_output.encode(), b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", worktree=True, path="src/foo.py",
            ))

        assert result["path"] == "src/foo.py"
        assert result["total"] == 1
        assert "+extra line" in result["hunks"][0]["content"]

    def test_worktree_mode_ignores_base_parameter(self):
        """worktree=True ignores the base parameter entirely."""
        container = MagicMock()
        numstat_output = b"5\t0\tfile.py\n"
        name_status_output = b""

        executed_cmds = []

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            executed_cmds.append(cmd_str)
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            diff_in_container(
                "abc123def456", base="main", worktree=True,
            )

        # Verify that the git commands use HEAD, not main...HEAD
        for cmd in executed_cmds:
            assert "main...HEAD" not in cmd
            if "git diff" in cmd:
                assert "HEAD" in cmd
                assert "..." not in cmd

    def test_existing_behavior_unchanged(self):
        """worktree=False (default) preserves existing base↔HEAD behavior."""
        container = MagicMock()
        numstat_output = b"10\t5\tsrc/foo.py\n"
        name_status_output = b"M\tsrc/foo.py\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container(
                "abc123def456", base="main",
            ))

        assert result["total_files"] == 1
        assert result["files"][0]["status"] == "M"
