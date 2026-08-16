"""Real-git integration tests for git_prepare_commit manifest semantics.

Builds a real bare-origin + working-clone pair in tmp_path and injects a
subprocess-backed ``run`` callable so every Git command is executed against
a real repository.  No Docker, no network, no special pytest markers.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.publish_ops import git_prepare_commit
from sunaba.tools.vcs.publishing import (
    _read_upstream_guard,
    _upstream_conflict_commits,
    _upstream_guard_probe,
    _validate_manifest_path,
    publish,
)
from tests.conftest import _FakeClient


class _RealGitContainer:
    """A docker-py-shaped container whose execs run for real in a clone.

    ``publish`` reaches git only through ``container.exec_run``, so pointing
    that at a real working clone is what lets these tests drive the whole
    tool -- guard, commit, push to the bare origin -- instead of a canned
    exec script.  Two things are answered without running:

    * ``gitleaks version`` reports the scanner as absent, so the secret scan
      returns ``skipped`` deterministically (whether or not the binary
      happens to be installed on the machine running the tests);
    * nothing else -- every other command is a real ``/bin/sh -c``.
    """

    def __init__(self, working_dir: str) -> None:
        self.working_dir = working_dir
        self.labels: dict[str, str] = {}
        self.commands: list[str] = []

    def exec_run(
        self,
        cmd: list[str] | None = None,
        stdout: bool = True,
        stderr: bool = True,
        environment: dict[str, str] | None = None,
        demux: bool = False,
        workdir: str | None = None,
        **_kwargs: Any,
    ):
        argv = list(cmd or [])
        if argv[:2] == ["gitleaks", "version"]:
            return self._reply(1, b"", b"", demux)

        if len(argv) > 2 and argv[0] in ("/bin/sh", "sh"):
            shell_cmd = argv[2]
        else:
            shell_cmd = " ".join(argv)
        self.commands.append(shell_cmd)

        env = None if environment is None else {**os.environ, **environment}
        proc = subprocess.run(
            ["/bin/sh", "-c", shell_cmd],
            cwd=workdir or self.working_dir,
            capture_output=True,
            env=env,
        )
        return self._reply(proc.returncode, proc.stdout, proc.stderr, demux)

    @staticmethod
    def _reply(ec: int, out: bytes, err: bytes, demux: bool):
        # docker-py splits the streams only when the caller asked for it
        # (issue #742); without demux they arrive multiplexed, which is the
        # form publish's own _run() reads.
        if demux:
            return ec, (out or None, err or None)
        return ec, out + err


def _make_run(working_dir: str):
    """Build a ``run(cmd, env=None)`` callable that executes in *working_dir*."""

    def run(cmd: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
        full_cmd = f"cd {shlex.quote(working_dir)} && {cmd}"
        result = subprocess.run(
            ["/bin/sh", "-c", full_cmd],
            capture_output=True, text=True, env=env,
        )
        return result.returncode, result.stdout, result.stderr

    return run


def _git(working_dir: str, *args: str) -> subprocess.CompletedProcess:
    """Run a raw git command in *working_dir* and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", working_dir, *args],
        capture_output=True, text=True,
    )


def _git_raw_bytes(working_dir: str, *args: str) -> bytes:
    """Run a git command and return raw stdout bytes (no text decoding)."""
    result = subprocess.run(
        ["git", "-C", working_dir, *args],
        capture_output=True,
    )
    return result.stdout


@pytest.fixture
def repo_setup(tmp_path: Path):
    """Create a bare origin + working clone with an initial commit."""
    origin_dir = tmp_path / "origin"
    clone_dir = tmp_path / "clone"

    origin_dir.mkdir()
    _git(str(origin_dir), "init", "--bare", "--initial-branch=main")

    # Create a clone, add an initial file, and push
    _git(str(tmp_path), "clone", str(origin_dir), str(clone_dir))

    # CI runners carry no global git identity; without a repo-level one
    # every commit in this fixture and in the tests fails with
    # "empty ident name".
    _git(str(clone_dir), "config", "user.email", "gitrepo-fixture@example.com")
    _git(str(clone_dir), "config", "user.name", "gitrepo fixture")

    initial = clone_dir / "README.md"
    initial.write_text("# Initial\n")
    _git(str(clone_dir), "add", "README.md")
    commit = _git(str(clone_dir), "commit", "-m", "Initial commit")
    assert commit.returncode == 0, f"fixture commit failed: {commit.stderr}"
    push = _git(str(clone_dir), "push", "origin", "main")
    assert push.returncode == 0, f"fixture push failed: {push.stderr}"

    # Ensure origin/HEAD exists in the clone so git_prepare_commit can
    # resolve it.  Set it explicitly -- deterministic, unlike
    # `remote set-head --auto` which queries the remote's HEAD.
    _git(str(clone_dir), "fetch", "origin")
    head = _git(
        str(clone_dir), "symbolic-ref",
        "refs/remotes/origin/HEAD", "refs/remotes/origin/main",
    )
    assert head.returncode == 0, f"fixture set origin/HEAD failed: {head.stderr}"

    return {
        "origin_dir": str(origin_dir),
        "clone_dir": str(clone_dir),
    }


# ============================================================================
# Test a: checkpoint leak prevention
# ============================================================================


class TestManifestCheckpointLeakPrevention:
    """Test that manifest mode excludes undeclared files committed by a
    prior checkpoint."""

    def test_undeclared_file_excluded(self, repo_setup: dict[str, Any]) -> None:
        """After a checkpoint that committed both a declared and an undeclared
        file, manifest publish must include only the declared file.  The
        undeclared file must still exist in the worktree."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        # Simulate a checkpoint: create two files, commit both via `git add -A`
        declared = Path(clone) / "declared.txt"
        undeclared = Path(clone) / "undeclared.txt"
        declared.write_text("declared\n")
        undeclared.write_text("secret\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "checkpoint with undeclared file")

        # Now publish with manifest mode — only declared.txt is declared
        err, _committed = git_prepare_commit(
            run, branch="fix/x", message="Manifest push",
            files=["declared.txt"],
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        # The undeclared file must still be present in the worktree
        assert undeclared.exists(), "undeclared file should survive in worktree"

        # Verify the new commit's tree contains only the declared file
        tree = _git(clone, "ls-tree", "--name-only", "HEAD")
        tree_files = tree.stdout.strip().split("\n")
        assert "declared.txt" in tree_files
        assert "undeclared.txt" not in tree_files


# ============================================================================
# Test b: follow-up push preserves prior commits
# ============================================================================


class TestManifestFollowUp:
    """Manifest publish on a branch that already exists on the remote."""

    def test_follow_up_preserves_prior_and_adds_declared(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """A manifest publish onto a branch with existing remote commits
        preserves those earlier commits and adds only the declared file."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        # ---- First publish: create the branch and push ----
        _git(clone, "checkout", "-b", "feat/feature-x")
        first = Path(clone) / "first.txt"
        first.write_text("first\n")
        _git(clone, "add", "first.txt")
        _git(clone, "commit", "-m", "first commit on feat/feature-x")
        _git(clone, "push", "--set-upstream", "origin", "feat/feature-x")
        first_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()

        # Create a second file locally (untracked until the manifest stages it)
        second = Path(clone) / "second.txt"
        second.write_text("second\n")

        # ---- Second publish (manifest mode) ----
        err, _committed = git_prepare_commit(
            run, branch="feat/feature-x", message="Second commit",
            files=["second.txt"],
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        # The earlier commit must be an ancestor of HEAD (preserved in history).
        ancestors = _git(
            clone, "merge-base", "--is-ancestor", first_sha, "HEAD",
        )
        assert ancestors.returncode == 0, (
            "first commit should be ancestor of HEAD"
        )

        # The tree of HEAD must contain the declared file (second.txt) and
        # the base file (README.md).  first.txt is also present because it
        # was already in origin/feat/feature-x (the base) — the manifest
        # reset preserves the base tree and only replaces staged files with
        # the declared set.
        tree = _git(clone, "ls-tree", "--name-only", "HEAD")
        tree_files = tree.stdout.strip().split("\n")
        assert "second.txt" in tree_files, "declared file should be in tree"
        assert "README.md" in tree_files, "base file should survive"


# ============================================================================
# Test c: undeclared tracked edit excluded
# ============================================================================


class TestManifestUndeclaredEditExcluded:
    """An edit to a tracked file that is not in the manifest must not be
    included in the pushed commit."""

    def test_edit_to_undeclared_tracked_file_excluded(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """Editing README.md (tracked but undeclared) while creating a new
        file (declared).  The manifest commit must include only the new file
        and not the edit to README.md."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        # Push a branch to the remote so origin/<branch> exists
        _git(clone, "checkout", "-b", "feat/feature-y")
        _git(clone, "push", "--set-upstream", "origin", "feat/feature-y")

        # Now edit README.md (tracked, undeclared) and create new.txt (declared)
        readme = Path(clone) / "README.md"
        readme.write_text("# Edited\n")
        new_txt = Path(clone) / "new.txt"
        new_txt.write_text("new\n")

        # Manifest publish: only new.txt is declared, not README.md
        err, _committed = git_prepare_commit(
            run, branch="feat/feature-y", message="Manifest push",
            files=["new.txt"],
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        # The HEAD tree should contain new.txt but NOT the README edit
        tree = _git(clone, "ls-tree", "--name-only", "HEAD")
        tree_files = tree.stdout.strip().split("\n")
        assert "new.txt" in tree_files, "declared file should be in commit"

        readme_content = _git(clone, "show", "HEAD:README.md").stdout
        assert readme_content.strip() == "# Initial", (
            "README.md should NOT have the edit in the commit"
        )

        # The edit to README.md must still be present in the working tree
        assert readme.read_text() == "# Edited\n", (
            "README.md edit should survive in the worktree"
        )


# ============================================================================
# Test d: declared deletion of a tracked file
# ============================================================================


class TestManifestDeclaredDeletion:
    """A declared deletion of a tracked file is committed under manifest
    mode.  This proves #684."""

    def test_declared_deletion(self, repo_setup: dict[str, Any]) -> None:
        """Declaring a tracked-but-deleted file (deleted from the worktree)
        must commit the deletion — the file must be absent from HEAD's tree."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        # Create a file and push it to a branch
        _git(clone, "checkout", "-b", "feat/feature-z")
        to_delete = Path(clone) / "todelete.txt"
        to_delete.write_text("will be deleted\n")
        _git(clone, "add", "todelete.txt")
        _git(clone, "commit", "-m", "add todelete.txt")
        _git(clone, "push", "--set-upstream", "origin", "feat/feature-z")

        # Delete the file from the worktree (simulate rm)
        to_delete.unlink()

        # Manifest publish with the deleted file declared
        err, _committed = git_prepare_commit(
            run, branch="feat/feature-z", message="Delete todelete.txt",
            files=["todelete.txt"],
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        # The file must be absent from HEAD's tree (deletion committed)
        tree = _git(clone, "ls-tree", "--name-only", "HEAD")
        tree_files = tree.stdout.strip().split("\n")
        assert "todelete.txt" not in tree_files, (
            "todelete.txt should be deleted from HEAD tree"
        )

        # Verify it's actually a deletion by checking that the parent has it
        parent_tree = _git(clone, "ls-tree", "--name-only", "HEAD~1")
        assert "todelete.txt" in parent_tree.stdout, (
            "parent commit should have todelete.txt before deletion"
        )

    def test_declared_deletion_staged_with_git_rm(self, repo_setup: dict[str, Any]) -> None:
        """A deletion staged with ``git rm`` (path gone from the worktree AND
        the index) must also commit under manifest mode -- the observable
        result is identical to the unstaged case (issue #837).

        This drives ``git_prepare_commit`` (which does not validate); the
        validation that makes the staged case reachable is exercised by
        TestManifestPathValidation.  Together the two prove a declared
        deletion commits the same either way.
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        _git(clone, "checkout", "-b", "feat/feature-rm")
        to_delete = Path(clone) / "todelete.txt"
        to_delete.write_text("will be deleted\n")
        _git(clone, "add", "todelete.txt")
        _git(clone, "commit", "-m", "add todelete.txt")
        _git(clone, "push", "--set-upstream", "origin", "feat/feature-rm")

        # Stage the deletion: worktree AND index no longer know the path
        rm = _git(clone, "rm", "todelete.txt")
        assert rm.returncode == 0, f"git rm failed: {rm.stderr}"

        err, _committed = git_prepare_commit(
            run, branch="feat/feature-rm", message="Delete todelete.txt",
            files=["todelete.txt"],
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        # Same observable result as the unstaged test_declared_deletion:
        # absent from HEAD's tree, present in the parent.
        tree = _git(clone, "ls-tree", "--name-only", "HEAD")
        tree_files = tree.stdout.strip().split("\n")
        assert "todelete.txt" not in tree_files, (
            "todelete.txt should be deleted from HEAD tree"
        )
        parent_tree = _git(clone, "ls-tree", "--name-only", "HEAD~1")
        assert "todelete.txt" in parent_tree.stdout, (
            "parent commit should have todelete.txt before deletion"
        )


# ============================================================================
# Additional: non-existent untracked path still rejected
# ============================================================================


class TestManifestUntrackedPathRejection:
    """Declaring a path that is neither a regular file nor tracked in HEAD
    must fail validation (the #684 safeguard)."""

    def test_nonexistent_untracked_rejected(self, repo_setup: dict[str, Any]) -> None:
        """A path that does not exist in the worktree AND is not tracked in
        HEAD produces an error (not a deletion declaration)."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        _git(clone, "checkout", "-b", "feat/rejection")
        err, _committed = git_prepare_commit(
            run, branch="feat/rejection", message="Should fail",
            files=["nosuchfile.txt"],
        )
        assert err is not None, "expected an error for non-existent untracked path"
        # git_prepare_commit does not validate paths (the caller
        # publishing.py does).  Here the error comes from git_add:
        # `git add -- nosuchfile.txt` fails because the path is neither
        # a regular file nor tracked in HEAD.
        assert err.get("step") == "git_add", (
            f"unexpected error shape: {err}"
        )

    def test_glob_pathspec_not_interpreted(self, repo_setup: dict[str, Any]) -> None:
        """A declared path containing glob characters must not expand to
        tracked files it does not literally name (#684 review finding).

        Without :(literal) staging, ``git add -- '*.md'`` would glob-match
        the tracked README.md and silently stage it.
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        _git(clone, "checkout", "-b", "feat/glob")
        err, _committed = git_prepare_commit(
            run, branch="feat/glob", message="Should fail",
            files=["*.md"],
        )
        assert err is not None, "glob pathspec must not stage README.md"
        assert err.get("step") == "git_add", f"unexpected error shape: {err}"


# ============================================================================
# Test: manifest path validation against real git (issue #837)
# ============================================================================


class TestManifestPathValidation:
    """Drive ``_validate_manifest_path`` (publishing.py) through ``_make_run``
    so every git command runs against a real clone.

    publish()'s manifest validation used to be reachable only via mocked
    ``exec_run`` sequences; a canned exit code cannot disagree with real
    git, so the wrong question (index-only ``git ls-files``) sailed through
    the suite.  These tests make the validation answer to real git: a
    ``git rm``-staged deletion is accepted only because the path is still
    tracked in HEAD.
    """

    def test_staged_deletion_accepted(self, repo_setup: dict[str, Any]) -> None:
        """A deletion staged with ``git rm`` is accepted: the path is gone
        from the worktree and the index, but still tracked in HEAD.

        The #837 regression test -- the unfixed validation asks only the
        index and rejects this path.
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        to_delete = Path(clone) / "todelete.txt"
        to_delete.write_text("will be deleted\n")
        _git(clone, "add", "todelete.txt")
        _git(clone, "commit", "-m", "add todelete.txt")

        # Stage the deletion: worktree AND index no longer know the path
        rm = _git(clone, "rm", "todelete.txt")
        assert rm.returncode == 0, f"git rm failed: {rm.stderr}"

        assert _validate_manifest_path(run, "todelete.txt") is None

    def test_unstaged_deletion_accepted(self, repo_setup: dict[str, Any]) -> None:
        """A plain ``rm`` (deletion left unstaged) is still accepted via the
        index check."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        to_delete = Path(clone) / "todelete.txt"
        to_delete.write_text("will be deleted\n")
        _git(clone, "add", "todelete.txt")
        _git(clone, "commit", "-m", "add todelete.txt")
        to_delete.unlink()

        assert _validate_manifest_path(run, "todelete.txt") is None

    def test_staged_directory_deletion_rejected(self, repo_setup: dict[str, Any]) -> None:
        """A directory deleted and staged with ``git rm -r`` is rejected as
        a directory.

        The directory is absent from the worktree (so ``test -d`` cannot
        see it) and from the index, but the HEAD fallback must not accept
        the tree entry -- ``git add`` on the pathspec would stage the
        deletions of every file beneath it, expanding the commit beyond
        the declared path (issue #837 review finding).
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        some_dir = Path(clone) / "some_dir"
        some_dir.mkdir()
        (some_dir / "a.md").write_text("a\n")
        (some_dir / "b.md").write_text("b\n")
        _git(clone, "add", "some_dir")
        _git(clone, "commit", "-m", "add some_dir")

        # Stage the deletion: worktree AND index no longer know the dir
        rm = _git(clone, "rm", "-r", "some_dir")
        assert rm.returncode == 0, f"git rm -r failed: {rm.stderr}"

        err = _validate_manifest_path(run, "some_dir")

        assert err is not None, "staged directory deletion must be rejected"
        assert err["step"] == "validation"
        assert "directory" in err["error"]

    def test_unstaged_directory_deletion_rejected(self, repo_setup: dict[str, Any]) -> None:
        """A directory deleted from the worktree but left in the index
        (``rm -rf``, unstaged) is rejected as a directory too.

        ``git ls-files`` treats a directory pathspec as matching every
        tracked file beneath it, so only an output whose first entry is the
        declared path itself counts as a file deletion (issue #837 review
        finding).
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        some_dir = Path(clone) / "some_dir"
        some_dir.mkdir()
        (some_dir / "a.md").write_text("a\n")
        (some_dir / "b.md").write_text("b\n")
        _git(clone, "add", "some_dir")
        _git(clone, "commit", "-m", "add some_dir")

        # Delete from the worktree only; the index still tracks the files
        for f in some_dir.iterdir():
            f.unlink()
        some_dir.rmdir()

        err = _validate_manifest_path(run, "some_dir")

        assert err is not None, "unstaged directory deletion must be rejected"
        assert err["step"] == "validation"
        assert "directory" in err["error"]

    def test_nonexistent_path_rejected(self, repo_setup: dict[str, Any]) -> None:
        """A path that exists nowhere -- worktree, index, or HEAD -- is
        rejected with the unchanged error text."""
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        err = _validate_manifest_path(run, "nosuchfile.txt")

        assert err is not None, "expected a validation error"
        assert err["step"] == "validation"
        assert "nosuchfile.txt" in err["error"]
        assert "regular file" in err["error"]

    def test_directory_rejected_as_directory(self, repo_setup: dict[str, Any]) -> None:
        """A declared directory is rejected *as a directory*, before the
        tracked-path fallback.

        ``git ls-files --error-unmatch`` and ``git ls-tree`` both treat a
        directory pathspec as matching everything beneath it, so if the
        directory check ever moved after the fallback this would be
        accepted and #836 would regress.  The error text pins that the
        rejection is the directory one.
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        some_dir = Path(clone) / "some_dir"
        some_dir.mkdir()
        (some_dir / "inner.txt").write_text("x\n")
        _git(clone, "add", "some_dir/inner.txt")
        _git(clone, "commit", "-m", "add some_dir")

        err = _validate_manifest_path(run, "some_dir")

        assert err is not None, "expected a validation error"
        assert err["step"] == "validation"
        assert "directory" in err["error"]

    def test_glob_pathspec_rejected_by_head_check(self, repo_setup: dict[str, Any]) -> None:
        """A declared path containing glob characters must not pass the HEAD
        fallback by matching files it does not literally name.

        ``:(literal)`` is applied to the ``git ls-tree`` check too: without
        it, ``*.md`` would glob-match the tracked README.md in HEAD and be
        accepted by validation.
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        err = _validate_manifest_path(run, "*.md")

        assert err is not None, "glob pathspec must not pass validation"
        assert err["step"] == "validation"
        assert "regular file" in err["error"]


# ============================================================================
# Test: a completed base merge survives manifest publish (#675)
# ============================================================================


class TestManifestPreservesBaseMerge:
    """A merge of the base branch must not lose base-advance files (issue #712).

    Candidate C: the merge commit itself is intentionally reset away and
    not preserved in the pushed lineage (that was forgeable).  Instead,
    files that the base branch advanced since the feature was last pushed
    are auto-included from a host-side fetch.  The observable outcome is
    that ``moved.txt`` (from main's advance) is present in the pushed
    tree alongside the declared file.
    """

    @staticmethod
    def _compute_auto_include(
        origin_dir: str, feature_branch: str, base_branch: str = "main",
    ) -> dict[str, str | bytes | None]:
        """Simulate a host-side fetch by reading directly from the bare origin.

        This is the faithful local analogue of ``_fetch_base_auto_include``:
        it reads from *origin_dir* (the remote), not from the clone's
        working tree, so a container that forges its local refs cannot
        influence the result.
        """
        feature_sha = _git(
            origin_dir, "rev-parse", f"refs/heads/{feature_branch}",
        ).stdout.strip()
        base_sha = _git(
            origin_dir, "rev-parse", f"refs/heads/{base_branch}",
        ).stdout.strip()

        # Use the merge base (common ancestor) as the reference so that
        # deletions only capture files that existed in the shared history
        # and were removed from main — not files the feature branch added
        # independently (which would also show as "D" in a direct
        # feature→base diff, issue #715).
        merge_base = _git(
            origin_dir, "merge-base", feature_sha, base_sha,
        ).stdout.strip()

        auto: dict[str, str | bytes | None] = {}

        # --- Added / modified (what base_sha has that merge_base didn't) ---
        diff_am = _git(
            origin_dir, "diff-tree", "-r", "--name-status",
            "--diff-filter=AM", merge_base, base_sha,
        )
        for line in diff_am.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                status, path = line.split("\t", 1)
            except ValueError:
                continue
            if status in ("A", "M"):
                raw = _git_raw_bytes(
                    origin_dir, "show", f"{base_sha}:{path}",
                )
                try:
                    auto[path] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    # Binary content: preserve as bytes (issue #716)
                    auto[path] = raw

        # --- Deleted (files present in merge_base but absent from base_sha) ---
        diff_d = _git(
            origin_dir, "diff-tree", "-r", "--name-status",
            "--diff-filter=D", merge_base, base_sha,
        )
        for line in diff_d.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                status, path = line.split("\t", 1)
            except ValueError:
                continue
            if status == "D":
                auto[path] = None  # deletion marker (issue #715)

        return auto

    def test_completed_merge_survives(self, repo_setup: dict[str, Any]) -> None:
        clone = repo_setup["clone_dir"]
        origin = repo_setup["origin_dir"]
        run = _make_run(clone)

        # A feature branch, pushed, so origin/<branch> exists.
        _git(clone, "checkout", "-b", "feat/x")
        (Path(clone) / "feature.txt").write_text("feature\n")
        _git(clone, "add", "feature.txt")
        _git(clone, "commit", "-m", "feature work")
        assert _git(clone, "push", "origin", "feat/x").returncode == 0

        # main moves on independently.
        other = Path(clone).parent / "other"
        _git(str(Path(clone).parent), "clone", origin, str(other))
        _git(str(other), "config", "user.email", "other@example.com")
        _git(str(other), "config", "user.name", "other")
        (other / "moved.txt").write_text("moved\n")
        _git(str(other), "add", "moved.txt")
        _git(str(other), "commit", "-m", "main moves")
        assert _git(str(other), "push", "origin", "main").returncode == 0

        # Bring the base in, exactly as merge_base/merge_complete would.
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"

        # Compute auto-include from origin_dir (host-side, not container).
        auto_include = self._compute_auto_include(origin, "feat/x", "main")

        # Now an ordinary manifest publish of an unrelated declared file.
        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/x", message="Manifest push after merge",
            files=["declared.txt"],
            base_auto_include=auto_include,
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        # The observable outcome: both the declared file and the
        # base-advance file (moved.txt) must be present in the pushed
        # tree.  With Candidate C the merge commit is intentionally
        # not preserved in the lineage -- moved.txt survives via
        # host-fetched auto-include instead.
        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree
        assert "moved.txt" in tree, "the merged-in base file should be present"

        # Verify moved.txt content matches origin/main (host-side sourced)
        base_sha = _git(origin, "rev-parse", "refs/heads/main").stdout.strip()
        expected_moved = _git(origin, "show", f"{base_sha}:moved.txt").stdout
        actual_moved = _git(clone, "show", "HEAD:moved.txt").stdout
        assert actual_moved == expected_moved, (
            "auto-included content must match remote (host-side sourced)"
        )

    def test_checkpoint_before_merge_still_cannot_leak(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """With a local checkpoint under the merge, the reset still wins.

        Losing a merge is recoverable (merge again); pushing an undeclared
        file is not. So the ambiguous shape must fall back to the #679 reset.
        """
        clone = repo_setup["clone_dir"]
        run = _make_run(clone)

        _git(clone, "checkout", "-b", "feat/y")
        undeclared = Path(clone) / "undeclared.txt"
        undeclared.write_text("secret\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "checkpoint with undeclared file")

        _git(clone, "fetch", "origin")
        _git(clone, "merge", "origin/main", "--no-edit")

        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/y", message="Manifest push",
            files=["declared.txt"],
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree
        assert "undeclared.txt" not in tree, "#679 leak reintroduced"


# ============================================================================
# Test e: forgery of remote-tracking ref cannot leak undeclared files (AC 1)
# ============================================================================


class TestForgeRemoteRefCannotLeak:
    """Issue #712 AC 1: forging ``refs/remotes/origin/<branch>`` to make
    the old skip-the-reset check pass must not allow undeclared files to
    ride along in the pushed commit.

    With Candidate C there is no skip-the-reset check anymore, so the
    forgery is structurally impossible.
    """

    def test_forged_origin_ref_still_blocks_secret(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """After a merge that brought in a secret checkpoint, forging
        origin/<branch> to match the checkpoint SHA must NOT leak the
        secret file into the pushed commit.
        """
        clone = repo_setup["clone_dir"]
        origin = repo_setup["origin_dir"]
        run = _make_run(clone)

        # Create a feature branch and push it.
        _git(clone, "checkout", "-b", "feat/secret")
        (Path(clone) / "legit.txt").write_text("legit\n")
        _git(clone, "add", "legit.txt")
        _git(clone, "commit", "-m", "legit commit")
        assert _git(clone, "push", "origin", "feat/secret").returncode == 0

        # Create a secret file and checkpoint it locally.
        (Path(clone) / ".env").write_text("SECRET=leaked\n")
        _git(clone, "add", "-A")
        _git(clone, "commit", "-m", "checkpoint with secret")

        # Merge origin/main (the checkpoint's parent becomes P1).
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"merge failed: {merge.stderr}"

        # Forge: make origin/feat/secret point to the checkpoint SHA
        # (which is HEAD^1 = the checkpoint commit with .env).
        checkpoint_sha = _git(clone, "rev-parse", "HEAD^1").stdout.strip()
        _git(clone, "update-ref", "refs/remotes/origin/feat/secret",
             checkpoint_sha)

        # Compute auto-include from origin_dir (host-side, not container).
        auto_include = TestManifestPreservesBaseMerge._compute_auto_include(
            origin, "feat/secret", "main",
        )

        # Manifest publish of only legit.txt.
        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/secret",
            message="Manifest push after forged merge",
            files=["declared.txt"],
            base_auto_include=auto_include,
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree
        # The secret .env must NOT be in the pushed tree.
        assert ".env" not in tree, (
            "secret file leaked despite forged origin ref"
        )


# ============================================================================
# Test f: auto-include covers base-advance files (AC 2)
# ============================================================================


class TestAutoIncludeBaseAdvance:
    """Issue #712 AC 2: files that the base branch advanced independently
    must be auto-included in the pushed commit via host-side fetch.
    """

    def test_base_advance_files_auto_included(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """Main adds multiple files; feature branch merges and then
        publishes a manifest declaring only one unrelated file.  All
        base-advance files must appear in the pushed tree."""
        clone = repo_setup["clone_dir"]
        origin = repo_setup["origin_dir"]
        run = _make_run(clone)

        # Feature branch pushed.
        _git(clone, "checkout", "-b", "feat/adv")
        (Path(clone) / "feature.txt").write_text("feature\n")
        _git(clone, "add", "feature.txt")
        _git(clone, "commit", "-m", "feature work")
        assert _git(clone, "push", "origin", "feat/adv").returncode == 0

        # Main advances with two new files and one modified file.
        other = Path(clone).parent / "other2"
        _git(str(Path(clone).parent), "clone", origin, str(other))
        _git(str(other), "config", "user.email", "other@example.com")
        _git(str(other), "config", "user.name", "other")
        (other / "added_a.txt").write_text("added A\n")
        (other / "added_b.txt").write_text("added B\n")
        readme = other / "README.md"
        original_readme = readme.read_text()
        readme.write_text(original_readme + "main edit\n")
        _git(str(other), "add", "added_a.txt", "added_b.txt", "README.md")
        _git(str(other), "commit", "-m", "main adds files")
        assert _git(str(other), "push", "origin", "main").returncode == 0

        # Feature merges main.
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"merge failed: {merge.stderr}"

        # Auto-include from origin (host-side).
        auto_include = TestManifestPreservesBaseMerge._compute_auto_include(
            origin, "feat/adv", "main",
        )

        # Manifest publish with only declared.txt.
        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/adv",
            message="Manifest push after merge",
            files=["declared.txt"],
            base_auto_include=auto_include,
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree
        # Base-advance files must be present (auto-included).
        assert "added_a.txt" in tree, "base-advance added file missing"
        assert "added_b.txt" in tree, "base-advance added file missing"
        # README.md was modified by main -- the auto-included version
        # should be present (main's version).
        readme_content = _git(clone, "show", "HEAD:README.md").stdout
        assert "main edit" in readme_content, (
            "base-advance modified file should be auto-included"
        )

        # Verify content provenance: auto-included files match origin,
        # not whatever the container's working tree has.
        base_sha = _git(origin, "rev-parse", "refs/heads/main").stdout.strip()
        expected_a = _git(origin, "show", f"{base_sha}:added_a.txt").stdout
        actual_a = _git(clone, "show", "HEAD:added_a.txt").stdout
        assert actual_a == expected_a, (
            "auto-included content must match remote"
        )


# ============================================================================
# Test g: auto-include covers base-advance deletions (issue #715)
# ============================================================================


class TestAutoIncludeBaseDeletion:
    """Issue #715 AC 2: files deleted from the base branch must be removed
    from the feature branch's pushed commit via auto-include."""

    def test_base_deleted_file_removed(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """Base branch deletes a file the feature branch still has.
        Feature merges base.  Publish declares unrelated file only.
        The deleted file must be absent from the pushed tree."""
        clone = repo_setup["clone_dir"]
        origin = repo_setup["origin_dir"]
        run = _make_run(clone)

        # Feature branch pushed with inherited and new files.
        _git(clone, "checkout", "-b", "feat/del")
        (Path(clone) / "feature.txt").write_text("feature\n")
        _git(clone, "add", "feature.txt")
        _git(clone, "commit", "-m", "feature work")
        # The initial README.md is also inherited from main into the
        # feature branch — call it "inherited.txt" for clarity.
        assert _git(clone, "push", "origin", "feat/del").returncode == 0

        # Main deletes README.md in a new commit.
        other = Path(clone).parent / "other_del"
        _git(str(Path(clone).parent), "clone", origin, str(other))
        _git(str(other), "config", "user.email", "other@example.com")
        _git(str(other), "config", "user.name", "other")
        _git(str(other), "rm", "README.md")
        _git(str(other), "commit", "-m", "main deletes README.md")
        assert _git(str(other), "push", "origin", "main").returncode == 0

        # Also add a new file to main so there is something to auto-include
        # besides the deletion — verifying both directions coexist.
        (other / "added_by_main.txt").write_text("added by main\n")
        _git(str(other), "add", "added_by_main.txt")
        _git(str(other), "commit", "-m", "main adds a file")
        assert _git(str(other), "push", "origin", "main").returncode == 0

        # Feature merges main (this creates the merge commit that publish
        # detects via HEAD^2).
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"merge failed: {merge.stderr}"

        # After the real merge, README.md is gone from the worktree because
        # git merge applied the deletion.  The auto-include deletion entry
        # will try to git rm it, which must be a no-op since the file is
        # already gone from both the index and worktree.

        # Auto-include from origin (host-side).
        auto_include = TestManifestPreservesBaseMerge._compute_auto_include(
            origin, "feat/del", "main",
        )

        # Verify that ._compute_auto_include now has the deletion entry.
        assert "README.md" in auto_include, (
            "auto_include should contain README.md (removed by main)"
        )
        assert auto_include["README.md"] is None, (
            "README.md should be marked as deletion (None)"
        )
        assert "added_by_main.txt" in auto_include, (
            "auto_include should contain added_by_main.txt"
        )

        # Manifest publish with only declared.txt.
        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/del",
            message="Manifest push after base deletion merge",
            files=["declared.txt"],
            base_auto_include=auto_include,
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree, "declared file should be present"
        assert "added_by_main.txt" in tree, (
            "base-advance added file should be auto-included"
        )
        assert "README.md" not in tree, (
            "README.md should be absent (deleted by base, auto-include removal)"
        )
        # Feature's own file must still be present.
        assert "feature.txt" in tree, (
            "feature branch's own file should survive"
        )

        # Verify content provenance for the added file.
        base_sha = _git(origin, "rev-parse", "refs/heads/main").stdout.strip()
        expected_added = _git(
            origin, "show", f"{base_sha}:added_by_main.txt",
        ).stdout
        actual_added = _git(clone, "show", "HEAD:added_by_main.txt").stdout
        assert actual_added == expected_added, (
            "auto-included added content must match remote"
        )

    def test_base_deleted_file_independently_removed(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """Edge case: both branches independently delete the same file.
        The auto-include deletion entry arrives for a file that is already
        gone from the feature branch's index/worktree; the handler must
        be a no-op, not an error."""
        clone = repo_setup["clone_dir"]
        origin = repo_setup["origin_dir"]
        run = _make_run(clone)

        # Feature branch pushed after independently deleting the very file
        # that main will also delete (README.md).
        _git(clone, "checkout", "-b", "feat/also_del")
        _git(clone, "rm", "README.md")
        _git(clone, "commit", "-m", "feature also deletes README.md")
        (Path(clone) / "feature.txt").write_text("feature\n")
        _git(clone, "add", "feature.txt")
        _git(clone, "commit", "-m", "feature work")
        assert _git(clone, "push", "origin", "feat/also_del").returncode == 0

        # Main also deletes README.md.
        other = Path(clone).parent / "other_also_del"
        _git(str(Path(clone).parent), "clone", origin, str(other))
        _git(str(other), "config", "user.email", "other@example.com")
        _git(str(other), "config", "user.name", "other")
        _git(str(other), "rm", "README.md")
        _git(str(other), "commit", "-m", "main deletes README.md")
        assert _git(str(other), "push", "origin", "main").returncode == 0

        # Feature merges main (merge is clean — same file deleted on both).
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"merge failed: {merge.stderr}"

        # Auto-include from origin (host-side).
        auto_include = TestManifestPreservesBaseMerge._compute_auto_include(
            origin, "feat/also_del", "main",
        )

        # README.md should appear with None.
        assert "README.md" in auto_include, (
            "README.md should be in auto_include as a deletion"
        )
        assert auto_include["README.md"] is None

        # Also add a new file to main to verify both directions coexist.
        other2 = Path(clone).parent / "other_also_del_a"
        _git(str(Path(clone).parent), "clone", origin, str(other2))
        _git(str(other2), "config", "user.email", "other@example.com")
        _git(str(other2), "config", "user.name", "other")
        (other2 / "added_by_main.txt").write_text("added by main\n")
        _git(str(other2), "add", "added_by_main.txt")
        _git(str(other2), "commit", "-m", "main adds a file")
        assert _git(str(other2), "push", "origin", "main").returncode == 0

        # Re-fetch and re-merge to pick up the new file.
        _git(clone, "fetch", "origin")
        merge2 = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge2.returncode == 0, f"second merge failed: {merge2.stderr}"

        # Recompute auto-include (includes both the deletion and the add).
        auto_include = TestManifestPreservesBaseMerge._compute_auto_include(
            origin, "feat/also_del", "main",
        )
        assert "added_by_main.txt" in auto_include
        assert auto_include["added_by_main.txt"] is not None

        # Manifest publish — the deletion of README.md must not error
        # even though the file is already gone from the feature branch.
        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/also_del",
            message="Manifest push after independent-deletion merge",
            files=["declared.txt"],
            base_auto_include=auto_include,
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree, "declared file should be present"
        assert "added_by_main.txt" in tree, (
            "base-advance added file should be auto-included"
        )
        assert "README.md" not in tree, (
            "README.md should be absent (deleted by both branches)"
        )
        # feature.txt should survive (feature branch's own file).
        assert "feature.txt" in tree, (
            "feature branch's own file should survive"
        )


# ============================================================================
# Test h: auto-include preserves binary content (issue #716)
# ============================================================================


class TestAutoIncludeBaseBinary:
    """Issue #716: binary (non-UTF-8) content from the base branch must
    survive auto-include byte-for-byte identical."""

    def test_binary_content_survives_auto_include(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """Base branch adds a binary file with non-UTF-8 bytes; feature
        merges, publishes only declared.txt.  The binary file must be present
        in the pushed tree with identical content."""
        clone = repo_setup["clone_dir"]
        origin = repo_setup["origin_dir"]
        run = _make_run(clone)

        # Feature branch pushed.
        _git(clone, "checkout", "-b", "feat/bin")
        (Path(clone) / "feature.txt").write_text("feature\n")
        _git(clone, "add", "feature.txt")
        _git(clone, "commit", "-m", "feature work")
        assert _git(clone, "push", "origin", "feat/bin").returncode == 0

        # Main advances with a binary file.
        other = Path(clone).parent / "other_bin"
        _git(str(Path(clone).parent), "clone", origin, str(other))
        _git(str(other), "config", "user.email", "other@example.com")
        _git(str(other), "config", "user.name", "other")
        # Write binary content that is never valid UTF-8
        binary_content = b"\\xff\\xfe\\x00\\x01\\x02GIF89a"  # GIF-like
        (other / "image.bin").write_bytes(binary_content)
        _git(str(other), "add", "image.bin")
        _git(str(other), "commit", "-m", "main adds binary file")
        assert _git(str(other), "push", "origin", "main").returncode == 0

        # Feature merges main.
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"merge failed: {merge.stderr}"

        # Auto-include from origin (host-side analogue).
        auto_include = TestManifestPreservesBaseMerge._compute_auto_include(
            origin, "feat/bin", "main",
        )

        # Manifest publish with only declared.txt.
        (Path(clone) / "declared.txt").write_text("declared\n")
        err, _committed = git_prepare_commit(
            run, branch="feat/bin",
            message="Manifest push after binary merge",
            files=["declared.txt"],
            base_auto_include=auto_include,
        )
        assert err is None, f"git_prepare_commit failed: {err}"

        tree = _git(clone, "ls-tree", "--name-only", "HEAD").stdout.split()
        assert "declared.txt" in tree
        assert "image.bin" in tree, "binary base-advance file missing"

        # Compare bytes, not text
        actual = _git_raw_bytes(clone, "show", "HEAD:image.bin")
        assert actual == binary_content, (
            f"binary content mismatch: got {len(actual)} bytes, "

            f"expected {len(binary_content)} bytes"
        )


# ============================================================================
# Test f: upstream-overwrite guard (issue #863)
# ============================================================================


def _advance_upstream(
    origin_dir: str,
    changes: dict[str, str | None],
    message: str = "upstream advance",
    clone_name: str = "upstream_clone",
) -> str:
    """Land *changes* on origin/main from a second clone.

    This is upstream moving on while the container works: a PR merging into
    main after the container was cloned.  *changes* maps a repo-relative
    path to its new text, or to ``None`` to delete it.  Returns the new
    main tip SHA.
    """
    other = Path(origin_dir).parent / clone_name
    if not other.exists():
        _git(str(Path(origin_dir).parent), "clone", origin_dir, str(other))
        _git(str(other), "config", "user.email", "upstream@example.com")
        _git(str(other), "config", "user.name", "upstream")

    for path, text in changes.items():
        if text is None:
            removed = _git(str(other), "rm", "-q", path)
            assert removed.returncode == 0, f"upstream rm failed: {removed.stderr}"
            continue
        target = other / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(str(other), "add", path)

    commit = _git(str(other), "commit", "-m", message)
    assert commit.returncode == 0, f"upstream commit failed: {commit.stderr}"
    push = _git(str(other), "push", "origin", "main")
    assert push.returncode == 0, f"upstream push failed: {push.stderr}"
    return _git(str(other), "rev-parse", "HEAD").stdout.strip()


def _make_exec_run(working_dir: str):
    """``_make_run`` with docker's env semantics.

    ``exec_run(environment=...)`` adds variables to the container's own
    environment; a bare ``subprocess`` env would replace it (and lose PATH),
    so the guard probes -- which pass only their own variable -- need the
    merge to behave as they do in a container.
    """
    inner = _make_run(working_dir)

    def run(cmd: str, env: dict[str, str] | None = None):
        return inner(cmd, None if env is None else {**os.environ, **env})

    return run


def _probe(run, base_ref: str, paths: list[str]):
    """Run the guard's probe against the real clone and return its report."""
    command, env = _upstream_guard_probe(base_ref, paths)
    # The declared paths reach the probe through the environment, so the
    # command text stays independent of the manifest.
    assert not any(path in command for path in paths), (
        "declared paths must not appear in the probe command"
    )
    _, out, _ = run(command, env)
    return _read_upstream_guard(out, paths)


class TestUpstreamGuardDecision:
    """The guard's read of a real clone whose base moved under it (#863).

    The container is cloned, upstream advances, and the container still
    holds the pre-advance copy of the files it declares.  Publishing that
    manifest would stage each declared path as a whole snapshot on top of
    the fresh base -- the silent revert kusabi#294 shipped.
    """

    def test_upstream_modified_declared_path_conflicts(
        self, repo_setup: dict[str, Any],
    ) -> None:
        clone = repo_setup["clone_dir"]
        run = _make_exec_run(clone)

        (Path(clone) / "shared.py").write_text("base\n")
        _git(clone, "add", "shared.py")
        _git(clone, "commit", "-m", "add shared.py")
        assert _git(clone, "push", "origin", "main").returncode == 0
        _git(clone, "fetch", "origin")

        # Upstream extends shared.py after this container's sync point.
        _advance_upstream(
            repo_setup["origin_dir"],
            {"shared.py": "base\nupstream feature\n"},
            message="upstream PR: extend shared.py",
        )

        # The container edits its (now stale) copy on a feature branch.
        _git(clone, "checkout", "-b", "feat/x")
        (Path(clone) / "shared.py").write_text("base\ncontainer edit\n")

        report = _probe(run, "origin/main", ["shared.py"])
        assert report is not None, "guard could not resolve the sync point"
        assert report.conflicts == ["shared.py"]

        # The refusal names the upstream commits behind the conflict, read
        # from local git only -- no GitHub API call.
        commits = _upstream_conflict_commits(
            run, report.merge_base, "origin/main", report.conflicts,
        )
        assert commits["shared.py"], "no upstream commits reported"
        assert any(
            "extend shared.py" in line for line in commits["shared.py"]
        ), commits

    def test_untouched_upstream_paths_are_identical(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """A base that advanced elsewhere leaves the declared path alone."""
        clone = repo_setup["clone_dir"]
        run = _make_exec_run(clone)

        _advance_upstream(
            repo_setup["origin_dir"],
            {"unrelated.py": "upstream only\n"},
            message="upstream PR: unrelated file",
        )

        _git(clone, "checkout", "-b", "feat/y")
        (Path(clone) / "mine.py").write_text("mine\n")

        report = _probe(run, "origin/main", ["mine.py"])
        assert report is not None
        # mine.py is absent at both refs ("both absent" = identical) and the
        # upstream file is not declared, so nothing is at risk.
        assert report.conflicts == []

    def test_upstream_added_and_deleted_paths_conflict(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """Added-upstream and deleted-upstream both count as differing.

        Declaring either path means the manifest would put the container's
        answer (a missing file, or a file upstream deleted) over the base's.
        """
        clone = repo_setup["clone_dir"]
        run = _make_exec_run(clone)

        (Path(clone) / "doomed.py").write_text("doomed\n")
        _git(clone, "add", "doomed.py")
        _git(clone, "commit", "-m", "add doomed.py")
        assert _git(clone, "push", "origin", "main").returncode == 0
        _git(clone, "fetch", "origin")

        _advance_upstream(
            repo_setup["origin_dir"],
            {"added.py": "new upstream file\n", "doomed.py": None},
            message="upstream PR: add one file, delete another",
        )

        _git(clone, "checkout", "-b", "feat/z")
        (Path(clone) / "added.py").write_text("container's own version\n")
        (Path(clone) / "doomed.py").write_text("container still has it\n")

        report = _probe(run, "origin/main", ["added.py", "doomed.py"])
        assert report is not None
        assert report.conflicts == ["added.py", "doomed.py"]

    def test_unresolvable_sync_point_is_undetermined(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """No such remote ref -> no comparison, and the guard says so.

        ``None`` is the "cannot tell" answer, not "clean": publish proceeds
        (a failed fetch must never block it, #818) but reports that the
        comparison never happened.
        """
        run = _make_exec_run(repo_setup["clone_dir"])
        (Path(repo_setup["clone_dir"]) / "mine.py").write_text("mine\n")

        assert _probe(run, "origin/no-such-branch", ["mine.py"]) is None
        # Same answer for output that carries no marker at all.
        assert _read_upstream_guard("", ["mine.py"]) is None


class TestPublishUpstreamOverwriteGuard:
    """publish() itself against a real clone + bare origin (#863).

    Everything below the tool is real: the guard's git, the commit, and the
    push to the bare origin.  Only the container boundary (``exec_run``),
    the host-side token/proxy lookups, and the host-side auto-include fetch
    are stood in for.
    """

    @pytest.fixture(autouse=True)
    def _publish_env(self, monkeypatch: pytest.MonkeyPatch):
        """Keep publish host-side: no proxy, no token, no GitHub call."""
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
        with (
            patch(
                "sunaba.tools.vcs.publishing._resolve_vcs_token",
                return_value="",
            ),
            patch(
                "sunaba.tools.vcs.publishing.proxy_configured",
                return_value=False,
            ),
            patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
            # The #712 auto-include is a GitHub REST read; this guard is
            # local-git only, so the merge-shaped test must not reach out.
            patch(
                "sunaba.tools.vcs.publishing._fetch_base_auto_include",
                return_value=None,
            ),
            # The baseline is a REST read too (#708); off keeps the test
            # hermetic without changing what the guard sees.
            patch(
                "sunaba.tools.secret_scan._baseline_enabled",
                return_value=False,
            ),
        ):
            yield

    @staticmethod
    def _publish(clone: str, **kwargs: Any) -> tuple[dict[str, Any], MagicMock]:
        """Call publish() against the real *clone*; return (result, pr_mock)."""
        container = _RealGitContainer(clone)
        with (
            patch(
                "sunaba.tools.vcs.publishing._docker",
                return_value=_FakeClient(container),
            ),
            patch(
                "sunaba.tools.vcs.publishing._create_pr_via_api",
            ) as pr_mock,
        ):
            raw = publish(
                container_id="abc123def456",
                repo="owner/repo",
                working_dir=clone,
                **kwargs,
            )
        return json.loads(raw), pr_mock

    @staticmethod
    def _stale_container(repo_setup: dict[str, Any]) -> None:
        """Set up the incident shape: upstream advanced past this clone.

        ``shared.py`` is on main, the container branches off it, upstream
        then extends it, and the container edits its own pre-advance copy.
        """
        clone = repo_setup["clone_dir"]
        (Path(clone) / "shared.py").write_text("base\n")
        _git(clone, "add", "shared.py")
        _git(clone, "commit", "-m", "add shared.py")
        assert _git(clone, "push", "origin", "main").returncode == 0
        _git(clone, "fetch", "origin")

        _advance_upstream(
            repo_setup["origin_dir"],
            {"shared.py": "base\nupstream feature\n"},
            message="upstream PR: extend shared.py",
        )

        _git(clone, "checkout", "-b", "feat/x")
        (Path(clone) / "shared.py").write_text("base\ncontainer edit\n")

    def test_refuses_without_committing_pushing_or_opening_a_pr(
        self, repo_setup: dict[str, Any],
    ) -> None:
        clone = repo_setup["clone_dir"]
        self._stale_container(repo_setup)
        head_before = _git(clone, "rev-parse", "HEAD").stdout.strip()

        result, pr_mock = self._publish(
            clone,
            branch="feat/x",
            message="Manifest publish over a moved base",
            files=["shared.py"],
            base_branch="main",
            create_pr=True,
            pr_title="Feature",
            pr_body="Body",
        )

        # Machine-distinguishable: callers branch on step, not on prose.
        assert result["status"] == "error"
        assert result["step"] == "upstream_overwrite"
        assert result["conflicting_paths"] == ["shared.py"]
        assert result["base_ref"] == "origin/main"
        assert any(
            "extend shared.py" in line
            for line in result["upstream_commits"]["shared.py"]
        ), result["upstream_commits"]
        assert "allow_upstream_overwrite=True" in result["hint"]

        # Nothing happened: no commit, no branch on the remote, no PR.
        assert _git(clone, "rev-parse", "HEAD").stdout.strip() == head_before
        assert _git(
            repo_setup["origin_dir"], "rev-parse", "--verify", "refs/heads/feat/x",
        ).returncode != 0, "the refused publish still pushed a branch"
        pr_mock.assert_not_called()

    def test_declared_paths_untouched_upstream_publish_as_before(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """The base moved, but not under any declared path -> unchanged."""
        clone = repo_setup["clone_dir"]
        _advance_upstream(
            repo_setup["origin_dir"],
            {"unrelated.py": "upstream only\n"},
            message="upstream PR: unrelated file",
        )
        _git(clone, "checkout", "-b", "feat/safe")
        (Path(clone) / "mine.py").write_text("mine\n")

        result, _pr = self._publish(
            clone,
            branch="feat/safe",
            message="Manifest publish",
            files=["mine.py"],
            base_branch="main",
        )

        assert result["status"] == "pushed", result
        assert result["staged_files"] == ["mine.py"]
        # Nothing to report: the guard ran and found every declared path
        # identical at both refs.
        assert "upstream_overwrite_override" not in result
        assert "upstream_guard_undetermined" not in result

        pushed = _git(
            repo_setup["origin_dir"], "ls-tree", "--name-only", "refs/heads/feat/safe",
        ).stdout.split()
        assert "mine.py" in pushed

    def test_override_publishes_and_records_the_paths(
        self, repo_setup: dict[str, Any],
    ) -> None:
        clone = repo_setup["clone_dir"]
        self._stale_container(repo_setup)

        result, _pr = self._publish(
            clone,
            branch="feat/x",
            message="Deliberately overwrite the upstream copy",
            files=["shared.py"],
            base_branch="main",
            allow_upstream_overwrite=True,
        )

        assert result["status"] == "pushed", result
        # The override is not silent: the result names what it waved through.
        assert result["upstream_overwrite_override"] is True
        assert result["upstream_overwrite_paths"] == ["shared.py"]

        pushed = _git(
            repo_setup["origin_dir"], "show", "refs/heads/feat/x:shared.py",
        ).stdout
        assert pushed == "base\ncontainer edit\n"

    def test_completed_base_merge_publishes_without_override(
        self, repo_setup: dict[str, Any],
    ) -> None:
        """The #675 shape stays green: merging the base moves the sync point.

        After an in-container merge of origin/main, the merge-base IS the
        base tip, so every declared path's two blobs agree and the guard has
        nothing to refuse.
        """
        clone = repo_setup["clone_dir"]
        (Path(clone) / "shared.py").write_text("base\n")
        _git(clone, "add", "shared.py")
        _git(clone, "commit", "-m", "add shared.py")
        assert _git(clone, "push", "origin", "main").returncode == 0
        _git(clone, "fetch", "origin")

        _advance_upstream(
            repo_setup["origin_dir"],
            {"shared.py": "base\nupstream feature\n"},
            message="upstream PR: extend shared.py",
        )

        # The container does its own work, then brings the base in exactly
        # as merge_base / merge_complete would.
        _git(clone, "checkout", "-b", "feat/x")
        (Path(clone) / "mine.py").write_text("mine\n")
        _git(clone, "add", "mine.py")
        _git(clone, "commit", "-m", "container work")
        _git(clone, "fetch", "origin")
        merge = _git(clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"

        # Now it edits the very path upstream changed -- on top of the
        # merged-in version, so the manifest reverts nothing.
        (Path(clone) / "shared.py").write_text(
            "base\nupstream feature\ncontainer edit\n"
        )
        result, _pr = self._publish(
            clone,
            branch="feat/x",
            message="Manifest publish after base merge",
            files=["shared.py"],
            base_branch="main",
        )

        assert result["status"] == "pushed", result
        assert "upstream_overwrite_override" not in result
        pushed = _git(
            repo_setup["origin_dir"], "show", "refs/heads/feat/x:shared.py",
        ).stdout
        assert pushed == "base\nupstream feature\ncontainer edit\n", (
            "the merged-in upstream line must survive the manifest publish"
        )
