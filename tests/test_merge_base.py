"""Tests for merge_base module — pure helpers and logic with mock containers.

The core helper functions (``_resolve_repo_from_container``,
``_resolve_base_branch``, ``merge_base``) are tested with a
``RecordingContainer`` that script responses for ``exec_run``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sunaba.tools.vcs.merge_base import (
    _resolve_base_branch,
    _resolve_repo_from_container,
    merge_abort,
    merge_base,
    merge_complete,
)


class RecordingContainer:
    """Fake container that returns scripted ``exec_run`` responses.

    Records every call in ``.calls`` so tests can inspect what was
    executed.
    """

    def __init__(
        self, returns: dict[str, list[tuple[int, str | bytes, str | bytes]]]
    ) -> None:
        # Map command-prefix patterns to lists of (ec, stdout_bytes, stderr_bytes)
        self._returns: dict[str, list[tuple[int, str | bytes, str | bytes]]] = returns
        self.calls: list[tuple[list[str], Any]] = []
        self._indices: dict[str, int] = {}

    def exec_run(self, cmd: list[str], **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        self.calls.append((cmd, kwargs))

        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        for prefix, retlist in self._returns.items():
            if cmd_str.startswith(prefix):
                idx = self._indices.get(prefix, 0)
                if idx < len(retlist):
                    ec, out, err = retlist[idx]
                    self._indices[prefix] = idx + 1
                    out_bytes = out.encode() if isinstance(out, str) else out
                    err_bytes = err.encode() if isinstance(err, str) else err
                    return ec, (out_bytes, err_bytes)
                # exhausted — return the last entry
                ec, out, err = retlist[-1]
                out_bytes = out.encode() if isinstance(out, str) else out
                err_bytes = err.encode() if isinstance(err, str) else err
                return ec, (out_bytes, err_bytes)

        return 0, (b"", b"")


# ============================================================================
# _resolve_repo_from_container
# ============================================================================


class TestResolveRepoFromContainer:
    def test_https_url(self) -> None:
        """Standard HTTPS URL is parsed correctly."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "https://github.com/owner/repo.git\n", ""),
            ],
        })
        result = _resolve_repo_from_container(container, "/workspace")
        assert result == "owner/repo"

    def test_https_url_no_dot_git(self) -> None:
        """HTTPS URL without .git suffix is parsed correctly."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "https://github.com/owner/repo\n", ""),
            ],
        })
        result = _resolve_repo_from_container(container, "/workspace")
        assert result == "owner/repo"

    def test_ssh_url(self) -> None:
        """SSH-style URL is parsed correctly."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "git@github.com:owner/repo.git\n", ""),
            ],
        })
        result = _resolve_repo_from_container(container, "/workspace")
        assert result == "owner/repo"

    def test_failure(self) -> None:
        """Error exit code raises RuntimeError."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (1, "", "fatal: not a git repository"),
            ],
        })
        with pytest.raises(RuntimeError, match="Cannot resolve origin remote"):
            _resolve_repo_from_container(container, "/workspace")

    def test_unusual_url_raises(self) -> None:
        """Non-GitHub URL raises RuntimeError."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "https://gitlab.com/owner/repo.git\n", ""),
            ],
        })
        with pytest.raises(RuntimeError, match="Unsupported remote URL"):
            _resolve_repo_from_container(container, "/workspace")

    def test_https_url_with_trailing_slash(self) -> None:
        """URL with trailing slash is parsed correctly."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "https://github.com/owner/repo/\n", ""),
            ],
        })
        result = _resolve_repo_from_container(container, "/workspace")
        assert result == "owner/repo"

    def test_https_url_with_git_and_trailing_slash(self) -> None:
        """URL with .git followed by trailing slash is parsed correctly."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "https://github.com/owner/repo.git/\n", ""),
            ],
        })
        result = _resolve_repo_from_container(container, "/workspace")
        assert result == "owner/repo"

    def test_https_url_repo_ends_with_t_char(self) -> None:
        """Repo name ending with 't' (in .git char set) is not corrupted."""
        container = RecordingContainer({
            "/bin/sh -c cd /workspace && git remote get-url origin": [
                (0, "https://github.com/owner/project\n", ""),
            ],
        })
        result = _resolve_repo_from_container(container, "/workspace")
        assert result == "owner/project"


# ============================================================================
# _resolve_base_branch
# ============================================================================


class MockMetaContainer:
    """Container with metadata for _read_container_meta.

    Returns Docker-style ``exec_run`` responses: ``(exit_code, bytes)`.
    """

    def __init__(self, meta: dict | None = None, exec_responses: dict | None = None):
        self._meta = meta
        self._exec_responses = exec_responses or {}
        self.exec_calls: list[list[str]] = []

    def exec_run(self, cmd: list[str], **kwargs: Any) -> tuple[int, bytes]:
        self.exec_calls.append(cmd)
        cmd_str = " ".join(cmd)
        # Handle metadata read by checking for the cat command
        if ".sandbox-meta.json" in cmd_str:
            if self._meta:
                return 0, json.dumps(self._meta).encode()
            return 0, b"{}"
        # Handle other exec_responses
        for key, response in self._exec_responses.items():
            if key in cmd_str:
                ec, raw = response
                if isinstance(raw, tuple):
                    return ec, raw[0]  # stdout only
                return ec, raw
        # Default to empty
        return 1, b""


class TestResolveBaseBranch:
    def test_explicit_base_branch(self) -> None:
        """Caller-supplied base_branch returns immediately."""
        container = MockMetaContainer()
        result, description = _resolve_base_branch(container, "/workspace", "develop")
        assert result == "develop"
        assert "explicit" in description

    def test_from_meta(self) -> None:
        """Base branch from container metadata."""
        container = MockMetaContainer(
            meta={"base_branch": "main"},
        )
        result, description = _resolve_base_branch(container, "/workspace")
        assert result == "main"
        assert "metadata" in description

    def test_from_origin_head(self) -> None:
        """Remote default branch (origin/HEAD) resolved via symbolic-ref."""
        container = MockMetaContainer(
            exec_responses={
                "git symbolic-ref refs/remotes/origin/HEAD": (
                    0, (b"refs/remotes/origin/main\n", b""),
                ),
            },
        )
        result, description = _resolve_base_branch(container, "/workspace")
        assert result == "main"
        assert "remote default branch" in description

    def test_guessed_branch(self) -> None:
        """Fallback to well-known branch names when origin/HEAD unavailable."""
        container = MockMetaContainer(
            exec_responses={
                "git symbolic-ref refs/remotes/origin/HEAD": (
                    1, (b"", b""),
                ),
                "git rev-parse --verify origin/main": (
                    0, (b"abc123\n", b""),
                ),
            },
        )
        result, description = _resolve_base_branch(container, "/workspace")
        assert result == "main"
        assert "guessed" in description

    def test_no_fallback_raises(self) -> None:
        """No branch can be resolved — raises RuntimeError."""
        container = MockMetaContainer(
            exec_responses={
                "git symbolic-ref refs/remotes/origin/HEAD": (
                    1, (b"", b""),
                ),
                "git rev-parse --verify origin/main": (
                    1, (b"", b""),
                ),
                "git rev-parse --verify origin/master": (
                    1, (b"", b""),
                ),
            },
        )
        with pytest.raises(RuntimeError, match="Cannot determine the base branch"):
            _resolve_base_branch(container, "/workspace")


# ============================================================================
# merge_base function
# ============================================================================


class FakeContainer:
    """Minimal container stub for merge_base injection.

    Returns Docker-style ``exec_run`` responses: ``(exit_code, bytes)``.
    Responses in ``exec_map`` are ``(exit_code, stdout_bytes, stderr_bytes)``;
    ``exec_run`` returns ``(exit_code, stdout_bytes)`` (the common case when
    only stdout is requested).
    """

    def __init__(self) -> None:
        self.exec_map: dict[str, tuple[int, bytes, bytes]] = {}
        self.exec_calls: list[list[str]] = []

    def exec_run(
        self, cmd: list[str], **kwargs: Any
    ) -> tuple[int, bytes]:
        self.exec_calls.append(cmd)
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        # Find the best matching key (may be a substring of cmd_str)
        for key, response in self.exec_map.items():
            if key in cmd_str:
                ec, out, err = response
                return ec, out
        return 0, b""


def test_merge_base_clean_merge():
    """Clean merge returns status='merged' with no conflicts."""
    container = FakeContainer()
    container.exec_map = {
        "git remote get-url origin": (0, b"https://github.com/owner/repo.git\n", b""),
        "git symbolic-ref refs/remotes/origin/HEAD": (0, b"refs/remotes/origin/main\n", b""),
        "git fetch origin main": (0, b"", b""),
        "git merge origin/main --no-edit": (0, b"Already up to date.\n", b""),
    }

    result = json.loads(merge_base(
        "test123abc", base_branch="main", repo="owner/repo",
        working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "merged"
    assert result["base_branch"] == "main"
    assert result["conflicts"] == []
    assert "merge" in result["message"].lower()


def test_merge_base_with_conflicts():
    """Conflicting merge returns status='conflicts' with conflicted file list."""
    container = FakeContainer()
    container.exec_map = {
        "git remote get-url origin": (0, b"https://github.com/owner/repo.git\n", b""),
        "git symbolic-ref refs/remotes/origin/HEAD": (0, b"refs/remotes/origin/main\n", b""),
        "git fetch origin main": (0, b"", b""),
        # 2>&1 merges stderr into stdout, so conflict output comes via stdout
        "git merge origin/main --no-edit": (
            1, b"CONFLICT (content): Merge conflict in src/foo.py\nAuto-merging src/bar.py\n", b""
        ),
        "git diff --name-only --diff-filter=U": (0, b"src/foo.py\n", b""),
    }

    result = json.loads(merge_base(
        "test123abc", base_branch="main", repo="owner/repo",
        working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "conflicts"
    assert "src/foo.py" in result["conflicts"]
    assert "resolve" in result["message"].lower()


def test_merge_base_fetch_fails():
    """Fetch failure returns status='error' with step='fetch'."""
    container = FakeContainer()
    container.exec_map = {
        "git remote get-url origin": (0, b"https://github.com/owner/repo.git\n", b""),
        "git symbolic-ref refs/remotes/origin/HEAD": (0, b"refs/remotes/origin/main\n", b""),
        # 2>&1 redirects stderr to stdout, so error text appears in stdout
        "git fetch origin main": (128, b"fatal: could not read Username", b""),
    }

    result = json.loads(merge_base(
        "test123abc", base_branch="main", repo="owner/repo",
        working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "error"
    assert result["step"] == "fetch"
    assert "could not read Username" in result["error"]


# ============================================================================
# merge_complete function
# ============================================================================


def test_merge_complete_no_merge_in_progress():
    """merge_complete without an active merge returns error."""
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"none\n", b""),
    }

    result = json.loads(merge_complete(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "error"
    assert result["step"] == "merge_check"
    assert "No merge in progress" in result["error"]


def test_merge_complete_unmerged_paths_are_not_by_themselves_an_error():
    """Paths listed by --diff-filter=U are the work to stage, not a failure.

    A resolved file stays "unmerged" in the index until it is staged, so
    treating the mere presence of U-paths as unresolved would make
    merge_complete unable to ever complete a conflicted merge.  Whether the
    resolution actually happened is decided by the marker check, covered in
    test_merge_complete_refuses_leftover_conflict_markers.
    """
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git diff --name-only --diff-filter=U": (0, b"src/conflict.py\n", b""),
        "grep -l": (1, b"", b""),  # resolved: no markers left
        "git add --": (0, b"", b""),
        "git commit --no-edit": (0, b"[branch abc1234] Merge", b""),
        "git rev-parse --short HEAD": (0, b"abc1234\n", b""),
    }

    result = json.loads(merge_complete(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "completed"


def test_merge_complete_success():
    """Successful merge completion returns status='completed' with sha."""
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git add -A": (0, b"", b""),
        "git diff --name-only --diff-filter=U": (0, b"", b""),
        "git commit --no-edit": (0, b"[branch abc1234] Merge", b""),
        "git rev-parse --short HEAD": (0, b"abc1234\n", b""),
    }

    result = json.loads(merge_complete(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "completed"
    assert result["sha"] == "abc1234"
    assert "successfully" in result["message"]


# ============================================================================
# merge_complete auto-stage tests
# ============================================================================


def test_merge_complete_stages_only_conflicted_paths():
    """Only the paths the merge was waiting on are staged -- never `git add -A`.

    `git add -A` would sweep unrelated worktree edits into the merge commit,
    the failure mode #677 removed from publish.
    """
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git diff --name-only --diff-filter=U": (0, b"a.py\nb.py\n", b""),
        # No conflict markers left in either file.
        "grep -l": (1, b"", b""),
        "git add --": (0, b"", b""),
        "git commit --no-edit": (0, b"[branch abc1234] Merge", b""),
        "git rev-parse --short HEAD": (0, b"abc1234\n", b""),
    }

    result = json.loads(merge_complete(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "completed"

    issued = [" ".join(c) for c in container.exec_calls]
    assert not any("git add -A" in c for c in issued), (
        "git add -A must never be used; it stages undeclared worktree changes"
    )
    add_calls = [c for c in issued if "git add --" in c]
    assert len(add_calls) == 2
    assert any("a.py" in c for c in add_calls)
    assert any("b.py" in c for c in add_calls)


def test_merge_complete_refuses_leftover_conflict_markers():
    """A file still holding conflict markers must not be committed as resolved.

    The staging must not happen first: `git add` marks a conflicted path
    resolved regardless of its contents, so checking afterwards can never
    catch anything.
    """
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git diff --name-only --diff-filter=U": (0, b"a.py\n", b""),
        "grep -l": (0, b"a.py\n", b""),
    }

    result = json.loads(merge_complete(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "error"
    assert result["step"] == "resolve_check"
    assert result["remaining_conflicts"] == ["a.py"]

    issued = [" ".join(c) for c in container.exec_calls]
    assert not any("git commit" in c for c in issued)
    assert not any("git add" in c for c in issued)


def test_merge_complete_git_add_fails():
    """A failing stage is reported as an error, naming the path."""
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git diff --name-only --diff-filter=U": (0, b"a.py\n", b""),
        "grep -l": (1, b"", b""),
        "git add --": (1, b"fatal: permission denied\n", b""),
    }

    result = json.loads(merge_complete(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "error"
    assert result["step"] == "git_add"
    assert result["path"] == "a.py"


# ============================================================================
# merge_abort tests
# ============================================================================


def test_merge_abort_active_merge():
    """merge_abort with an active merge returns status='aborted'."""
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git merge --abort": (0, b"", b""),
    }

    result = json.loads(merge_abort(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "aborted"
    assert "aborted" in result["message"]


def test_merge_abort_no_merge():
    """merge_abort without an active merge returns error."""
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"none\n", b""),
    }

    result = json.loads(merge_abort(
        "test123abc", working_dir="/workspace", _container=container,
    ))
    assert result["status"] == "error"
    assert result["step"] == "merge_check"
    assert "nothing to abort" in result["error"]


def test_merge_abort_failure():
    """merge_abort when git merge --abort fails returns error."""
    container = FakeContainer()
    container.exec_map = {
        "cd /workspace && [ -f .git/MERGE_HEAD ]": (0, b"in-progress\n", b""),
        "git merge --abort": (1, b"merge: no merge to abort\n", b""),
    }
