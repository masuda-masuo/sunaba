"""Tests for diff_in_container tool (Issue #476, #500, #748)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sunaba.tools.common import _parse_numstat
from sunaba.tools.diff import (
    _parse_name_status,
    diff_in_container,
)

# Reusable fixture helpers ---------------------------------------------------

#: A successful `git merge-base` always prints a commit, so mocks must too:
#: diff_in_container treats an empty result as "no merge base" and returns an
#: error rather than diffing against nothing (#748).
_MERGE_BASE_SHA = "0" * 40


def _responder(default):
    """exec_run stand-in yielding *default*, but a real sha for merge-base."""

    def run(cmd, **kwargs):
        if "merge-base" in str(cmd[-1]):
            return (0, (f"{_MERGE_BASE_SHA}\n".encode(), b""))
        return default

    return run


def _make_basic_container() -> MagicMock:
    """Create a container that returns empty for all git commands."""
    container = MagicMock()
    container.exec_run.side_effect = _responder((0, (b"", b"")))
    return container


def _meta_container(meta_json: str = "{}") -> MagicMock:
    """Create a container whose first exec_run returns the given meta JSON."""
    container = MagicMock()
    meta_bytes = meta_json.encode()
    side_effects = [
        (0, (meta_bytes, b"")),  # cat .sandbox-meta.json (in diff_in_container)
        (0, (meta_bytes, b"")),  # cat .sandbox-meta.json (in _resolve_base_branch)
        (0, (b"refs/remotes/origin/main\n", b"")),  # git symbolic-ref
        (0, (b"", b"")),  # ls-remote (silence)
        (0, (b"abc123\n", b"")),  # git rev-parse origin/main
    ]
    container.exec_run.side_effect = side_effects
    return container


# Helpers to build side_effect functions for common diff responses -----------

def _summary_side_effect(
    numstat: bytes,
    name_status: bytes,
    ls_files: bytes = b"",
    raw_diff: bytes | None = None,
    meta_first: bool = False,
) -> list | None:
    """Build exec_run side_effect list for summary mode responses.

    Returns a list to assign to container.exec_run.side_effect.
    If meta_first is True, a meta lookup is prepended (for tests
    that rely on metadata base branch).
    """
    effects: list = []
    if meta_first:
        effects.append((0, (b'{"base_branch":"main"}', b"")))
    effects.append((0, (numstat, b"")))  # --numstat
    effects.append((0, (name_status, b"")))  # --name-status
    if raw_diff is not None:
        effects.append((0, (raw_diff, b"")))  # raw diff
    effects.append((0, (ls_files, b"")))  # git ls-files
    return effects


# Tests ----------------------------------------------------------------------


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

    # --- Error/edge cases ---

    def test_container_not_found(self):
        with patch(
            "sunaba.tools.diff._docker"
        ) as mock_docker:
            mock_client = MagicMock()
            mock_client.containers.get.side_effect = Exception("not found")
            mock_docker.return_value = mock_client

            result = json.loads(diff_in_container("nonexistent"))
            assert result["status"] == "error"

    # --- Summary mode with explicit base (unchanged resolution path) ---

    def test_summary_no_changes(self):
        container = _make_basic_container()

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
        ), patch(
            "sunaba.tools.diff.resolve_git_root",
            return_value="/repo",
        ), patch(
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert "files" in result
        assert result["total_files"] == 0
        assert result["total_additions"] == 0
        assert result["total_deletions"] == 0
        assert result["base"] == "main"
        assert result["mode"] == "merge-base"
        assert result["untracked"] == []

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
            elif "ls-files" in cmd[-1]:
                return (0, (b"", b""))
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
        assert result["base"] == "main"
        assert result["mode"] == "merge-base"

    def test_summary_with_mixed_statuses(self):
        """Files should have correct status from --name-status."""
        container = MagicMock()
        numstat_output = b"10\t5\tsrc/foo.py\n0\t8\tdeleted.py\n15\t0\tnew.py\n"
        name_status_output = b"M\tsrc/foo.py\nD\tdeleted.py\nA\tnew.py\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            cmd_str = cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
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
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
        """Default base resolves from container metadata."""
        container = MagicMock()
        side_effects = [
            (0, (b'{"clone_path":"/repo","base_branch":"main"}', b"")),  # meta
            (0, (b"ca17829\n", b"")),  # rev-parse origin/<branch>
            (0, (f"{_MERGE_BASE_SHA}\n".encode(), b"")),  # git merge-base
            (0, (b"5\t0\tnew.py\n", b"")),  # --numstat
            (0, (b"A\tnew.py\n", b"")),  # --name-status
            (0, (b"", b"")),  # ls-files
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
        # The metadata records a bare branch name; it is resolved to the
        # remote-tracking ref for the same reason an auto-resolved one is.
        assert result["base"] == "origin/main"
        assert result["mode"] == "merge-base"

    # --- File mode tests ---

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
        container.exec_run.side_effect = _responder((0, (git_output.encode(), b"")))

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
        assert result["base"] == "main"
        assert result["mode"] == "merge-base"

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
        container.exec_run.side_effect = _responder((0, (git_output.encode(), b"")))

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
        container.exec_run.side_effect = _responder((0, (b"", b"")))

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
        container.exec_run.side_effect = _responder((128, (b"fatal: not a git repository", b"")))

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
        r"""``\ No newline at end of file`` is included in the preceding hunk."""
        container = MagicMock()
        git_output = (
            "@@ -1,2 +1,3 @@\n"
            " a\n"
            "-b\n"
            "+c\n"
            "+d\n"
            "\\ No newline at end of file\n"
        )
        container.exec_run.side_effect = _responder((0, (git_output.encode(), b"")))

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
        container.exec_run.side_effect = _responder((0, (git_output.encode(), b"")))

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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
        assert result["base"] == "HEAD"
        assert result["mode"] == "worktree"

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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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

    # --- New tests for Issue #748 (merge-base defaults) ---

    def test_merge_base_default_shows_working_tree_changes(self):
        """Without explicit base, shows tracked changes in the working tree."""
        container = MagicMock()
        numstat_output = b"10\t5\tsrc/foo.py\n"
        name_status_output = b"M\tsrc/foo.py\n"
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "src/foo.py"
        assert result["files"][0]["status"] == "M"
        assert result["base"] == "main"
        assert result["mode"] == "merge-base"

    def test_merge_base_includes_untracked_files(self):
        """Default mode includes untracked files in response."""
        container = MagicMock()
        numstat_output = b""
        name_status_output = b""
        ls_files_output = b"3 NEWFILE.txt\n"

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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "NEWFILE.txt"
        assert result["files"][0]["status"] == "untracked"
        assert "NEWFILE.txt" in result["untracked"]

    def test_merge_base_does_not_show_upstream_pr_changes(self):
        """The diff does NOT show change set of a recently merged PR.

        When base == HEAD (e.g. on a fresh clone at main), the merge-base
        is HEAD itself, so git diff HEAD shows only uncommitted work.
        """
        container = MagicMock()
        # Empty numstat + name-status means no tracked diffs from main
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        # No tracked changes, no untracked files = empty diff
        assert result["total_files"] == 0

    def test_explicit_HEAD_tilde_1_still_works(self):
        """base='HEAD~1' passed explicitly still produces a diff based on HEAD~1."""
        container = MagicMock()
        numstat_output = b"5\t0\tnew.py\n"
        name_status_output = b"A\tnew.py\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
                "abc123def456", base="HEAD~1",
            ))

        assert result["total_files"] == 1
        assert result["files"][0]["status"] == "A"
        assert result["base"] == "HEAD~1"
        assert result["mode"] == "merge-base"

    def test_response_contains_base_and_mode(self):
        """Every successful response includes 'base' and 'mode' fields."""
        container = MagicMock()
        numstat_output = b"1\t1\tREADME.md\n"
        name_status_output = b"M\tREADME.md\n"

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--numstat" in cmd_str:
                return (0, (numstat_output, b""))
            elif "--name-status" in cmd_str:
                return (0, (name_status_output, b""))
            elif "ls-files" in cmd_str:
                return (0, (b"", b""))
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            # Summary mode with explicit base
            result_s = json.loads(diff_in_container(
                "abc123def456", base="main",
            ))
            assert result_s["base"] == "main"
            assert result_s["mode"] == "merge-base"

            # Summary mode with worktree
            result_w = json.loads(diff_in_container(
                "abc123def456", worktree=True,
            ))
            assert result_w["base"] == "HEAD"
            assert result_w["mode"] == "worktree"

    def test_file_mode_contains_base_and_mode(self):
        """File mode response includes 'base' and 'mode' fields."""
        container = MagicMock()
        container.exec_run.return_value = (
            0, (b"@@ -1,1 +1,1 @@\n-old\n+new\n", b"")
        )

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
            result_f = json.loads(diff_in_container(
                "abc123def456", base="main", path="foo.py",
            ))
            assert result_f["base"] == "main"
            assert result_f["mode"] == "merge-base"

    def test_file_mode_untracked_in_default_mode(self):
        """File mode with an untracked file in non-worktree mode falls back to --no-index."""
        container = MagicMock()
        diff_output = (
            "diff --git a/NEWFILE.txt b/NEWFILE.txt\n"
            "new file mode 100644\n"
            "index 0000000..abc1234\n"
            "--- /dev/null\n"
            "+++ b/NEWFILE.txt\n"
            "@@ -0,0 +1,3 @@\n"
            "+a\n"
            "+b\n"
            "+c\n"
        )

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "--no-index" in cmd_str:
                return (0, (diff_output.encode(), b""))
            elif "ls-files" in cmd_str and "--others" in cmd_str:
                return (0, (b"NEWFILE.txt\n", b""))
            # git diff for tracked file returns empty (untracked)
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
                "abc123def456", base="main", path="NEWFILE.txt",
            ))

        assert result["path"] == "NEWFILE.txt"
        assert result["total"] == 1
        assert len(result["hunks"]) == 1
        assert result["hunks"][0]["new_count"] == 3
        assert "+a" in result["hunks"][0]["content"]

    def test_index_unchanged(self):
        """diff_in_container does not mutate the git index.

        We verify that no 'git add' commands are ever issued.
        """
        container = MagicMock()
        numstat_output = b"5\t0\tnew.py\n"
        name_status_output = b"A\tnew.py\n"

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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            # Both summary and file mode
            diff_in_container("abc123def456", base="main")
            diff_in_container("abc123def456", base="main", path="new.py")
            # Both worktree modes
            diff_in_container("abc123def456", worktree=True)
            diff_in_container("abc123def456", worktree=True, path="new.py")

        # No 'git add' commands should have been issued
        for cmd in executed_cmds:
            assert "git add" not in cmd, f"Index-mutating command detected: {cmd}"
            assert "git add -N" not in cmd, (
                f"Index-mutating command detected: {cmd}"
            )

    def test_merge_base_default_with_base_from_meta(self):
        """Default base resolves from container metadata first."""
        container = MagicMock()
        # First call returns meta with base_branch
        side_effects = [
            (0, (b'{"clone_path":"/repo","base_branch":"develop"}', b"")),  # meta
            (0, (b"ca17829\n", b"")),  # rev-parse origin/<branch>
            (0, (f"{_MERGE_BASE_SHA}\n".encode(), b"")),  # git merge-base
            (0, (b"3\t1\tfeature.py\n", b"")),  # --numstat
            (0, (b"M\tfeature.py\n", b"")),  # --name-status
            (0, (b"", b"")),  # ls-files
        ]
        container.exec_run.side_effect = side_effects

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
            result = json.loads(diff_in_container("abc123def456"))

        assert result["total_files"] == 1
        assert result["base"] == "origin/develop"
        assert result["mode"] == "merge-base"

    def test_merge_base_committed_work_appears(self):
        """Work committed via checkpoint shows together with uncommitted work.

        Simulates the case where HEAD has commits beyond the base.
        git diff $(git merge-base main HEAD) shows both committed and
        uncommitted changes.
        """
        container = MagicMock()
        numstat_output = b"10\t2\tcommitted.py\n5\t0\tuncommitted.py\n"
        name_status_output = b"M\tcommitted.py\nA\tuncommitted.py\n"
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
            if "merge-base" in str(cmd[-1]):
                return (0, (b"0000000000000000000000000000000000000000\n", b""))
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
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        # Both committed.py and uncommitted.py appear in the same diff
        assert result["total_files"] == 2
        paths = {f["path"] for f in result["files"]}
        assert paths == {"committed.py", "uncommitted.py"}

    # --- Regressions found by running against a real container (#748) ---

    def test_auto_resolved_base_prefers_remote_tracking_ref(self):
        """An auto-resolved base must become origin/<branch>, not the local one.

        A container works directly on the cloned default branch, so after a
        checkpoint the *local* `main` has moved along with HEAD and
        merge-base(main, HEAD) collapses to HEAD -- every committed change
        disappears from the diff.  Only the remote-tracking ref stays put.
        Mocked merge-base hides this, so assert on the ref that was used.
        """
        container = MagicMock()
        seen = []

        def exec_side_effect(cmd, **kwargs):
            cmd_str = str(cmd[-1])
            seen.append(cmd_str)
            if "rev-parse --verify --quiet" in cmd_str:
                return (0, (b"ca17829\n", b""))  # origin/main exists
            if "merge-base" in cmd_str:
                return (0, (f"{_MERGE_BASE_SHA}\n".encode(), b""))
            if "--numstat" in cmd_str:
                return (0, (b"1\t0\tcommitted.py\n", b""))
            if "--name-status" in cmd_str:
                return (0, (b"M\tcommitted.py\n", b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo",
        ), patch(
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert result["base"] == "origin/main"
        merge_base_cmds = [c for c in seen if "merge-base" in c]
        assert merge_base_cmds, "merge-base was never resolved"
        assert "origin/main" in merge_base_cmds[0]

    def test_auto_resolved_base_falls_back_when_no_remote_ref(self):
        """Without origin/<branch>, the bare branch name is still used."""
        container = MagicMock()

        def exec_side_effect(cmd, **kwargs):
            cmd_str = str(cmd[-1])
            if "rev-parse --verify --quiet" in cmd_str:
                return (1, (b"", b""))  # no origin/main
            if "merge-base" in cmd_str:
                return (0, (f"{_MERGE_BASE_SHA}\n".encode(), b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo",
        ), patch(
            "sunaba.tools.diff._resolve_base_branch",
            return_value=("main", "mocked"),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert result["base"] == "main"

    def test_explicit_base_is_not_rewritten(self):
        """A base the caller wrote by hand is used verbatim."""
        container = MagicMock()

        def exec_side_effect(cmd, **kwargs):
            cmd_str = str(cmd[-1])
            if "merge-base" in cmd_str:
                assert "origin/" not in cmd_str, cmd_str
                return (0, (f"{_MERGE_BASE_SHA}\n".encode(), b""))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456", base="HEAD~1"))

        assert result["base"] == "HEAD~1"

    def test_unresolvable_merge_base_is_an_error(self):
        """No merge base must fail loudly, not degrade to a bare git diff.

        `git diff $(git merge-base bad HEAD)` exits 0 with an empty
        substitution and reports only unstaged changes -- a wrong answer
        indistinguishable from a right one, which is the #748 failure mode.
        """
        container = MagicMock()

        def exec_side_effect(cmd, **kwargs):
            cmd_str = str(cmd[-1])
            if "merge-base" in cmd_str:
                return (128, (b"", b"fatal: Not a valid object name"))
            return (0, (b"", b""))

        container.exec_run.side_effect = exec_side_effect

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo",
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(
                diff_in_container("abc123def456", base="no-such-branch")
            )

        assert result["status"] == "error"
        assert result["step"] == "merge_base"
        assert "no-such-branch" in result["error"]

    def test_unresolvable_base_branch_is_an_error(self):
        """A base that cannot be resolved is an error, not a silent HEAD."""
        container = MagicMock()
        container.exec_run.side_effect = _responder((0, (b"", b"")))

        with patch(
            "sunaba.tools.diff._docker",
            return_value=MagicMock(
                containers=MagicMock(get=MagicMock(return_value=container))
            ),
        ), patch(
            "sunaba.tools.diff.resolve_git_root", return_value="/repo",
        ), patch(
            "sunaba.tools.diff._resolve_base_branch",
            side_effect=RuntimeError("Cannot determine the base branch."),
        ), patch(
            "sunaba.tools.diff.record_tool_use",
        ):
            result = json.loads(diff_in_container("abc123def456"))

        assert result["status"] == "error"
        assert result["step"] == "resolve_base"


    # --- Fetch-once retry (Issue #765) ---

    def test_fetch_on_missing_base(self):
        """When merge base fails first time, fetch happens, then succeeds.

        Response includes fetch_happened=True.
        The fetch command is ``git fetch origin`` (never pull, never single-branch).
        """
        container = MagicMock()
        call_count = [0]  # mutable counter for the closure
        seen_fetch_cmds = []

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "merge-base" in cmd_str:
                call_count[0] += 1
                if call_count[0] == 1:
                    # First attempt: unknown ref
                    return (128, (b"", b"fatal: Not a valid object name"))
                # Second attempt: success after fetch
                return (0, (f"{_MERGE_BASE_SHA}\n".encode(), b""))
            if "fetch" in cmd_str:
                seen_fetch_cmds.append(cmd_str)
                return (0, (b"", b""))
            if "--numstat" in cmd_str:
                return (0, (b"1\t0\tfixed.py\n", b""))
            if "--name-status" in cmd_str:
                return (0, (b"A\tfixed.py\n", b""))
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
            result = json.loads(
                diff_in_container("abc123def456", base="origin/v2")
            )

        assert result["total_files"] == 1
        assert result["files"][0]["path"] == "fixed.py"
        assert result["base"] == "origin/v2"
        assert result["fetch_happened"] is True

        # Verify the fetch command is `git fetch origin` (never pull)
        assert len(seen_fetch_cmds) == 1, (
            f"Expected exactly one fetch, got {len(seen_fetch_cmds)}"
        )
        fetch_cmd = seen_fetch_cmds[0]
        assert "git fetch origin" in fetch_cmd, (
            f"Fetch command should be 'git fetch origin': {fetch_cmd}"
        )
        assert "pull" not in fetch_cmd, (
            f"Must not be a pull: {fetch_cmd}"
        )
        assert "refs/heads/" not in fetch_cmd and "heads/" not in fetch_cmd, (
            f"Must fetch all refs, not a single branch: {fetch_cmd}"
        )

    def test_no_fetch_when_base_resolved(self):
        """When base resolves immediately, no fetch occurs.

        Response does NOT include fetch_happened.
        """
        container = MagicMock()

        seen_fetch = []

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "fetch" in cmd_str and "origin" in cmd_str:
                seen_fetch.append(cmd_str)
            if "merge-base" in cmd_str:
                return (0, (f"{_MERGE_BASE_SHA}\n".encode(), b""))
            if "--numstat" in cmd_str:
                return (0, (b"2\t1\texisting.py\n", b""))
            if "--name-status" in cmd_str:
                return (0, (b"M\texisting.py\n", b""))
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
            result = json.loads(
                diff_in_container("abc123def456", base="main")
            )

        assert result["total_files"] == 1
        assert result["base"] == "main"
        assert "fetch_happened" not in result
        assert len(seen_fetch) == 0, f"Unexpected fetch command: {seen_fetch}"

    def test_fetch_still_fails(self):
        """When base does not resolve even after fetch, error is returned."""
        container = MagicMock()

        seen_fetch = []

        def exec_side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes) else str(cmd[-1])
            )
            if "fetch" in cmd_str and "origin" in cmd_str:
                seen_fetch.append(cmd_str)
                return (0, (b"", b""))  # fetch succeeds but ref still missing
            if "merge-base" in cmd_str:
                return (128, (b"", b"fatal: Not a valid object name"))
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
            result = json.loads(
                diff_in_container("abc123def456", base="no-such-branch")
            )

        assert result["status"] == "error"
        assert result["step"] == "merge_base"
        assert "no-such-branch" in result["error"]
        assert len(seen_fetch) == 1, "Fetch should have been attempted once"
        # The caller must be told the fetch already ran, or they will go and
        # do it by hand expecting a different answer.
        assert "already attempted" in result["error"], result["error"]


class TestFetchHelperImport:
    """Verify the shared fetch helper is importable."""

    def test_git_fetch_origin_importable(self):
        from sunaba.tools.vcs.fetch import git_fetch_origin

        assert callable(git_fetch_origin)
