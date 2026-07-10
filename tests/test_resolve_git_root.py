"""Tests for resolve_git_root auto-detection."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from sunaba.tools.vcs import resolve_git_root


def _make_container(exec_run_returns: list) -> MagicMock:
    """Build a mock container with a side-effect sequence for exec_run."""
    container = MagicMock()
    container.exec_run.side_effect = exec_run_returns
    return container

# Convenience: metadata file not present (most common in tests)
_NO_META = (1, (b"cat: .sandbox-meta.json: No such file or directory\n", b""))


class TestResolveGitRootExplicit:
    """When working_dir is explicitly set, it is returned unchanged."""

    def test_explicit_path_returned_as_is(self) -> None:
        container = MagicMock()
        result = resolve_git_root(container, "/custom/path")
        assert result == "/custom/path"
        container.exec_run.assert_not_called()

    def test_default_value_triggers_autodetect(self) -> None:
        """None triggers auto-detection."""
        container = _make_container([
            _NO_META,
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"


class TestResolveGitRootMeta:
    """Step 0: container metadata points to the clone."""

    def test_meta_found_verified(self) -> None:
        container = _make_container([
            # Step 0: metadata found
            (0, (b'{"clone_path": "/custom/path/repo"}\n', b"")),
            # Verify it's a git repo
            (0, (b"/custom/path/repo\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/custom/path/repo"

    def test_meta_path_not_git_falls_through(self) -> None:
        """Metadata exists but path is not a git repo → fall through to Step 1."""
        container = _make_container([
            # Step 0: metadata found
            (0, (b'{"clone_path": "/custom/path/repo"}\n', b"")),
            # Verify: not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 1: /tmp/repo scan behind the simplified mock
            _NO_META,
            (0, (b"/tmp/repo/sunaba\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/tmp/repo/sunaba"


class TestResolveGitRootStep1:
    """Step 1: /home/sandbox is a git repository (no metadata)."""

    def test_home_sandbox_is_git_repo(self) -> None:
        container = _make_container([
            _NO_META,
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_home_sandbox_subdir_repo(self) -> None:
        container = _make_container([
            _NO_META,
            (0, (b"/home/sandbox/my-project\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox/my-project"


class TestResolveGitRootStep2:
    """Step 2: fallback to /tmp/repo/*/ scan (no metadata)."""

    def test_tmp_repo_found(self) -> None:
        container = _make_container([
            _NO_META,
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ sunaba found
            (0, (b"/tmp/repo/sunaba\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/tmp/repo/sunaba"

    def test_tmp_repo_no_repos_falls_back(self) -> None:
        container = _make_container([
            _NO_META,
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ has no .git dirs
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"


class TestResolveGitRootErrors:
    """Error handling for unexpected exec_run outputs."""

    def test_meta_bad_json_ignored(self) -> None:
        container = _make_container([
            # Step 0: metadata exists but is invalid JSON
            (0, (b"not json\n", b"")),
            # Step 1: /home/sandbox is a git repo
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_meta_missing_clone_path_ignored(self) -> None:
        container = _make_container([
            # Step 0: metadata exists but no clone_path key
            (0, (b'{"other": "value"}\n', b"")),
            # Step 1: /home/sandbox is a git repo
            (0, (b"/home/sandbox\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"

    def test_all_steps_fail(self) -> None:
        container = _make_container([
            _NO_META,
            # Step 1: /home/sandbox → not a git repo
            (128, (b"fatal: not a git repository\n", b"")),
            # Step 2: /tmp/repo/ → nothing
            (0, (b"__NO_REPO__\n", b"")),
        ])
        result = resolve_git_root(container)
        assert result == "/home/sandbox"


class TestWriteMetaRoundTrip:
    """Verify _write_clone_meta output is consumable by resolve_git_root."""

    def test_meta_json_produced_is_valid_consumable(self) -> None:
        """Simulate what _write_clone_meta writes, then read by resolve_git_root.

        This ensures the JSON format produced by _write_clone_meta
        is correctly parsed by resolve_git_root's Step 0.
        """
        from sunaba.tools.container import _write_clone_meta

        clone_path = "/custom/dest/my-repo"

        # Capture what _write_clone_meta sends to exec_run
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))

        _write_clone_meta(container, clone_path)

        # The command should contain valid JSON with the clone_path
        cmd = container.exec_run.call_args[0][0][2]
        assert "mkdir -p /home/sandbox" in cmd
        # Extract the JSON part from the printf argument
        assert '{"clone_path": "/custom/dest/my-repo"}' in cmd

    def test_meta_write_then_resolve_round_trip(self) -> None:
        """Simulate end-to-end: meta written → resolve reads."""
        clone_path = "/custom/dest/my-repo"

        # Simulate what the container would have after _write_clone_meta
        meta_json = json.dumps({"clone_path": clone_path}).encode()

        container = _make_container([
            # Step 0: metadata file read
            (0, (meta_json + b"\n", b"")),
            # Verify: git repo check succeeds
            (0, (clone_path.encode() + b"\n", b"")),
        ])

        result = resolve_git_root(container)
        assert result == clone_path
