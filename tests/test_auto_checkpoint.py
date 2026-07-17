"""Tests for auto_checkpoint (Issue #586).

Acceptance criteria:
1. Auto checkpoint created after successful edit operation.
2. Commit message embeds changed filenames with ``[auto]`` prefix.
3. No-op when ``resolve_git_root`` fails (outside git area).
4. Unpushed auto-checkpoints are squashed by publish.
5. Counter is process-local (resets on server restart).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.auto_checkpoint import (
    _auto_checkpoint_counter,
    _lock,
    auto_checkpoint,
    counter_for,
    get_changed_files,
    increment_counter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_container_mock(exec_returns: list[tuple[int, bytes, bytes]]):
    """Build a mock Docker container with a sequence of exec_run results."""
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client_mock(container: MagicMock):
    """Build a mock Docker client that returns the given container."""
    client = MagicMock()
    client.containers.get.return_value = container
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_counter():
    """Reset the auto-checkpoint counter before each test (process-local)."""
    with _lock:
        _auto_checkpoint_counter.clear()
    yield


# ---------------------------------------------------------------------------
# Unit tests for get_changed_files
# ---------------------------------------------------------------------------


class TestGetChangedFiles:
    def test_tracked_modified(self):
        """Tracked file with unstaged modifications."""
        container = _make_container_mock([
            (0, b" M src/foo.py\n", b""),
        ])
        files = get_changed_files(container, "/workspace")
        assert files == ["src/foo.py"]

    def test_untracked(self):
        """Untracked file."""
        container = _make_container_mock([
            (0, b"?? new_file.py\n", b""),
        ])
        files = get_changed_files(container, "/workspace")
        assert files == ["new_file.py"]

    def test_staged(self):
        """Staged (index) changes."""
        container = _make_container_mock([
            (0, b"M  src/staged.py\n", b""),
        ])
        files = get_changed_files(container, "/workspace")
        assert files == ["src/staged.py"]

    def test_mixed(self):
        """Multiple files in various states."""
        status = (
            b" M src/edited.py\n"
            b"?? untracked.py\n"
            b"M  src/staged.py\n"
        )
        container = _make_container_mock([(0, status, b"")])
        files = get_changed_files(container, "/workspace")
        assert files == ["src/edited.py", "untracked.py", "src/staged.py"]

    def test_git_error(self):
        """Git failure returns empty list."""
        container = _make_container_mock([
            (128, b"fatal: not a git repository", b""),
        ])
        files = get_changed_files(container, "/tmp")
        assert files == []

    def test_no_changes(self):
        """Clean working tree."""
        container = _make_container_mock([
            (0, b"", b""),
        ])
        files = get_changed_files(container, "/workspace")
        assert files == []

    def test_filename_with_space(self):
        """Filename containing a space is correctly parsed."""
        container = _make_container_mock([
            (0, b" M src/my file.py\n", b""),
        ])
        files = get_changed_files(container, "/workspace")
        assert files == ["src/my file.py"]


# ---------------------------------------------------------------------------
# Unit tests for auto_checkpoint
# ---------------------------------------------------------------------------


class TestAutoCheckpoint:
    """AC1, AC2, AC3, AC5."""

    def _make_result(self, raw: str | None) -> dict | None:
        if raw is None:
            return None
        return json.loads(raw)

    # -- AC1 + AC2: auto checkpoint created with [auto] message -------------

    def test_auto_checkpoint_created(self):
        """AC1: Auto checkpoint is created after a successful edit."""
        exec_returns = [
            (0, b" M src/foo.py\n", b""),        # git status --porcelain
            (0, b"", b""),                        # git add + commit
            (0, b"abc1234\n", b""),               # git rev-parse --short HEAD
        ]
        container = _make_container_mock(exec_returns)
        result = self._make_result(auto_checkpoint(
            container, "abc123def456", working_dir="/workspace",
        ))

        assert result is not None
        assert result["status"] == "ok"
        assert result["sha"] == "abc1234"

    def test_auto_checkpoint_message_format(self):
        """AC2: Message contains [auto] prefix and filenames."""
        exec_returns = [
            (0, b" M src/foo.py\n"
                 b"?? src/bar.py\n", b""),        # git status --porcelain
            (0, b"", b""),                        # git add + commit
            (0, b"def5678\n", b""),               # git rev-parse
        ]
        container = _make_container_mock(exec_returns)
        result = self._make_result(auto_checkpoint(
            container, "abc123def456", working_dir="/workspace",
        ))

        assert result is not None
        # Exact assertion: "[auto] checkpoint \u2014 src/foo.py, src/bar.py"
        assert result["message"] == "[auto] checkpoint \u2014 src/foo.py, src/bar.py"

    # -- AC3: no-op outside git --------------------------------------------

    def test_noop_outside_git(self):
        """AC3: No-op when resolve_git_root fails (outside git area)."""
        with patch("sunaba.tools.vcs.resolve_git_root",
                   side_effect=Exception("not a git repo")):
            container = _make_container_mock([])
            result = auto_checkpoint(container, "abc123def456")
            assert result is None

    # -- No-op when no changes ---------------------------------------------

    def test_noop_no_changes(self):
        """No checkpoint when there are no uncommitted changes."""
        exec_returns = [
            (0, b"", b""),  # git status --porcelain (clean)
        ]
        container = _make_container_mock(exec_returns)
        result = auto_checkpoint(
            container, "abc123def456", working_dir="/workspace",
        )
        assert result is None

    def test_commit_failure(self):
        """Git commit failure returns None and does NOT increment counter."""
        exec_returns = [
            (0, b" M src/foo.py\n", b""),        # git status --porcelain
            (1, b"", b"fatal: bad config"),       # git add + commit fails
        ]
        container = _make_container_mock(exec_returns)
        result = auto_checkpoint(
            container, "abc123def456", working_dir="/workspace",
        )
        assert result is None
        # Counter must NOT be incremented on failure.
        assert counter_for("abc123def456") == 0

    # -- AC5: counter is process-local (resets on restart) -----------------

    def test_counter_increments(self):
        """Counter increments after successful auto-checkpoint."""
        exec_returns = [
            (0, b" M src/foo.py\n", b""),        # git status
            (0, b"", b""),                        # git commit
            (0, b"abc1234\n", b""),               # git rev-parse
        ]
        container = _make_container_mock(exec_returns)

        assert counter_for("abc123def456") == 0
        auto_checkpoint(container, "abc123def456", working_dir="/workspace")
        assert counter_for("abc123def456") == 1

    def test_counter_noop_outside_git(self):
        """Counter does NOT increment when resolve_git_root fails."""
        with patch("sunaba.tools.vcs.resolve_git_root",
                   side_effect=Exception("not a git repo")):
            container = _make_container_mock([])
            auto_checkpoint(container, "abc123def456")
            assert counter_for("abc123def456") == 0

    def test_counter_process_local(self):
        """AC5: Counter lives only in module-level dict (process-local).

        Increment the counter, then simulate a process restart by clearing the
        dict — the count must drop back to 0.
        """
        increment_counter("abc123def456")
        assert counter_for("abc123def456") == 1

        # Simulate process restart: module-level dict is lost.
        with _lock:
            _auto_checkpoint_counter.clear()

        assert counter_for("abc123def456") == 0


# ---------------------------------------------------------------------------
# Tests for increment_counter (used by explicit checkpoint tool)
# ---------------------------------------------------------------------------


class TestIncrementCounter:
    def test_increment(self):
        """increment_counter increases the count by 1."""
        assert counter_for("abc123def456") == 0
        increment_counter("abc123def456")
        assert counter_for("abc123def456") == 1

    def test_multiple_increments(self):
        """Multiple increments accumulate."""
        for _ in range(5):
            increment_counter("abc123def456")
        assert counter_for("abc123def456") == 5

    def test_independent_per_container(self):
        """Different containers have independent counters."""
        increment_counter("aaa111aaa111")
        increment_counter("bbb222bbb222")
        increment_counter("bbb222bbb222")
        assert counter_for("aaa111aaa111") == 1
        assert counter_for("bbb222bbb222") == 2


# ---------------------------------------------------------------------------
# Integration tests: auto_checkpoint is called after tool success
# ---------------------------------------------------------------------------


class TestWriteFileIntegration:
    """Verify write_file triggers auto_checkpoint (AC1)."""

    @patch("sunaba.auto_checkpoint.auto_checkpoint")
    @patch("sunaba.tools.file._docker")
    def test_auto_checkpoint_called_after_write(
        self, mock_docker, mock_ac,
    ):
        """write_file calls auto_checkpoint on success."""
        container = MagicMock()
        # read_file: cat (new file → ValueError)
        # write_file_in_container: mkdir + stat calls + put_archive
        container.exec_run.side_effect = [
            (1, (b"", b"")),              # read_file: cat (fails - new file)
            (0, (b"", b"")),              # mkdir -p
            (1, (b"", b"")),              # stat existing file (fails)
            (0, (b"1000 1000\n", b"")),   # stat /proc/self (succeeds)
        ]
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        from sunaba.tools.file import write_file

        result = write_file(
            container_id="abc123def456",
            file_name="dest/test_write.txt",
            file_contents="hello world",
        )
        assert "Written" in result
        # Verify auto_checkpoint was called
        mock_ac.assert_called_once()

    @patch("sunaba.tools.file._docker")
    def test_auto_checkpoint_does_not_break_on_write_error(self, mock_docker):
        """auto_checkpoint failure does not affect write_file success."""
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, (b"old content\n", b"")),  # read_file: existing
            (1, (b"", b"write error")),    # mkdir fails
        ]

        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        from sunaba.tools.file import write_file

        result = write_file(
            container_id="abc123def456",
            file_name="dest/test_write.txt",
            file_contents="hello world",
        )
        assert "Error" in result


class TestTransformFileIntegration:
    """Verify transform_file triggers auto_checkpoint (AC1)."""

    @patch("sunaba.tools.file.transform_file_in_container")
    @patch("sunaba.auto_checkpoint.auto_checkpoint")
    @patch("sunaba.tools.file._docker")
    def test_auto_checkpoint_called_after_transform(
        self, m_docker, m_ac, m_tfic,
    ):
        """transform_file calls auto_checkpoint on successful change."""
        m_tfic.return_value = {
            "status": "ok",
            "changed": True,
            "diff": "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
            "new_size": 4,
            "new_lines": 1,
        }
        container = MagicMock()
        # Pre-read: file exists (for undo snapshot)
        container.exec_run.side_effect = [
            (0, (b"original content\n", b"")),
        ]
        client = MagicMock()
        client.containers.get.return_value = container
        m_docker.return_value = client

        from sunaba.tools.file import transform_file

        result = transform_file(
            container_id="abc123def456",
            file_path="/workspace/src/transformed.txt",
            code="def transform(text): return text.upper()",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert parsed["changed"] is True
        # Verify auto_checkpoint was called
        m_ac.assert_called_once()


class TestCheckpointIntegration:
    """Verify checkpoint tool increments auto-checkpoint counter (AC1)."""

    @patch("sunaba.tools.vcs._docker")
    def test_checkpoint_increments_counter(self, mock_docker):
        """Explicit checkpoint increments the auto-checkpoint counter."""
        exec_returns = [
            (0, b"", b""),        # git add -A + git commit
            (0, b"abc1234\n", b""),  # git rev-parse --short HEAD
        ]
        container = _make_container_mock(exec_returns)
        # Container needs attrs with WorkingDir for resolve_git_root
        container.attrs = {"Config": {"WorkingDir": "/workspace"}}
        client = _make_client_mock(container)
        mock_docker.return_value = client

        from sunaba.tools.vcs import checkpoint

        result = json.loads(checkpoint(
            container_id="abc123def456",
            message="my explicit checkpoint",
        ))

        assert result["status"] == "ok"
        assert result["message"] == "my explicit checkpoint"
        # Counter should be incremented
        assert counter_for("abc123def456") == 1

    @patch("sunaba.tools.vcs._docker")
    def test_checkpoint_does_not_call_auto_checkpoint_commit(self, mock_docker):
        """Explicit checkpoint does NOT create a second commit."""
        exec_returns = [
            (0, b"", b""),        # git add -A + git commit (explicit)
            (0, b"abc1234\n", b""),  # git rev-parse
        ]
        container = _make_container_mock(exec_returns)
        container.attrs = {"Config": {"WorkingDir": "/workspace"}}
        client = _make_client_mock(container)
        mock_docker.return_value = client

        from sunaba.tools.vcs import checkpoint

        result = json.loads(checkpoint(
            container_id="abc123def456",
            message="my checkpoint",
        ))

        assert result["status"] == "ok"
        # Only 2 exec calls: one for git commit, one for rev-parse
        # No extra calls for auto_checkpoint git operations
        assert container.exec_run.call_count == 2
