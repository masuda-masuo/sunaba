"""Tests for resolve_git_root auto-detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import resolve_git_root


def _make_container(exec_run_returns: list) -> MagicMock:
    """Build a mock container with a side-effect sequence for exec_run."""
    container = MagicMock()
    container.exec_run.side_effect = exec_run_returns
    return container


class TestResolveGitRootExplicit:
    """When working_dir is explicitly set, it is returned unchanged."""

    def test_explicit_path_returned_as_is(self) -> None:
        container = MagicMock()
        result = resolve_git_root(container, "/custom/path")
        assert result == "/custom/path"
        container.exec_run.assert_not_called()

    def test_default_value_triggers_autodetect(self) -> None:
        """'/home/sandbox' matches _DEFAULT_WD, so auto-detection runs."""
        container = _make_container([
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container, "/home/sandbox")
        assert result == "/home/sandbox"
        assert container.exec_run.call_count == 1


class TestResolveGitRootStep1:
    """Step 1: /home/sandbox is a git repository."""

    def test_home_sandbox_is_git_repo(self) -> None:
        container = _make_container([
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_home_sandbox_subdir_repo(self) -> None:
        container = _make_container([
            (0, (b"/home/sandbox/my-project\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox/my-project"


class TestResolveGitRootStep2:
    """Step 2: fallback to /tmp/repo/*/ scan."""

    def test_tmp_repo_found(self) -> None:
        container = _make_container([
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ code-sandbox-mcp found
            (0, (b"/tmp/repo/code-sandbox-mcp\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/tmp/repo/code-sandbox-mcp"

    def test_tmp_repo_no_repos_falls_back(self) -> None:
        container = _make_container([
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ has no .git dirs
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"


class TestResolveGitRootErrors:
    """Error handling for unexpected exec_run outputs."""

    def test_step1_exit_128_step2_found(self) -> None:
        """Non-zero exit from git rev-parse is treated as 'no repo'."""
        container = _make_container([
            (128, (b"fatal: not a git repository\n", b"")),
            (0, (b"/tmp/repo/code-sandbox-mcp\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/tmp/repo/code-sandbox-mcp"

    def test_both_steps_fail(self) -> None:
        container = _make_container([
            (128, (b"fatal: not a git repository\n", b"")),
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_step1_empty_output(self) -> None:
        """Empty output is not a valid path, falls to step 2 then fallback."""
        container = _make_container([
            (0, (b"", b"")),
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"
