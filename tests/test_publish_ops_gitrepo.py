"""Real-git integration tests for git_prepare_commit manifest semantics.

Builds a real bare-origin + working-clone pair in tmp_path and injects a
subprocess-backed ``run`` callable so every Git command is executed against
a real repository.  No Docker, no network, no special pytest markers.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sunaba.tools.publish_ops import git_prepare_commit


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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
            run, branch="feat/glob", message="Should fail",
            files=["*.md"],
        )
        assert err is not None, "glob pathspec must not stage README.md"
        assert err.get("step") == "git_add", f"unexpected error shape: {err}"


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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
        err = git_prepare_commit(
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
