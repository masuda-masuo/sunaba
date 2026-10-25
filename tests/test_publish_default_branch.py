"""Acceptance tests for publish default branch resolution (#946).

Pins the contract defined in issue #946 comment 6392414282:
1. Manifest git_prepare_commit never executes git fetch, ls-remote,
   symbolic-ref, or origin/HEAD (AC-1).
2. Omitted base uses origin/<host default> as cut_from, ignoring forged
   origin/HEAD pointing elsewhere (AC-2).
3. Stacked local-only base fails step=base_branch without creating a commit;
   base == default skips stacking and ancestor check (AC-3).
4. Empty default manifest call fails step=base_branch; missing default ref
   fails step=base_branch and names default; existing remote target takes
   precedence; legacy mode unaffected (AC-4).
5. publish() reads host default branch exactly once after authorized fetch,
   failing closed before commit/push on API failure; explicit base still reads;
   merge detection and upstream guard use host default ref; cut-parent helpers
   support supplied default without rereading, preserving backward compatibility (AC-5).
6. Real git test with divergent default/decoy branches and reassigned origin/HEAD
   proves actual resulting cut parent is host default (AC-6).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.publish_ops import git_prepare_commit
from sunaba.tools.vcs.publishing import (
    ExpectedCutParents,
    HostRefRead,
    UpstreamTipRead,
    _host_read_default_branch,
    _resolve_expected_cut_parents,
    _verify_cut_parents,
    publish,
)
from tests.conftest import _FakeClient
from tests.test_publish_cut_parent import (
    CONTAINER_ID,
    REPO,
    _assert_nothing_pushed,
    _git,
    _HostApi,
    _origin_tip_blobs,
    _PublishContext,
    _sha,
)

# ============================================================================
# Test Fixtures & Harness
# ============================================================================


class CommandRecorder:
    """Mock `run(cmd, env=None)` that records commands and simulates git responses."""

    def __init__(
        self,
        *,
        remote_branches: dict[str, str] | None = None,
        local_branches: dict[str, str] | None = None,
        merge_in_progress: bool = False,
        ancestor_ok: bool = True,
        diff_cached_changed: bool = True,
        committed_paths: list[str] | None = None,
        custom_responses: dict[str, tuple[int, str, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.commands: list[str] = []
        self.remote_branches = (
            remote_branches if remote_branches is not None else {"main": "main_sha_1234567890"}
        )
        self.local_branches = local_branches if local_branches is not None else {}
        self.merge_in_progress = merge_in_progress
        self.ancestor_ok = ancestor_ok
        self.diff_cached_changed = diff_cached_changed
        self.committed_paths = (
            committed_paths if committed_paths is not None else ["file.txt"]
        )
        self.custom_responses = custom_responses or {}

    def __call__(
        self, cmd: str, env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        self.calls.append((cmd, env))
        self.commands.append(cmd)

        for pattern, resp in self.custom_responses.items():
            if pattern in cmd:
                return resp

        # MERGE_HEAD
        if "MERGE_HEAD" in cmd:
            if self.merge_in_progress:
                return 0, "merge_head_sha\n", ""
            return 1, "", "fatal: Needed a single revision"

        # Checkout
        if cmd.startswith("git checkout"):
            return 0, "", ""

        # Rev-parse origin/<branch>
        match_remote = re.search(r"git rev-parse (?:--verify )?origin/([^\s]+)", cmd)
        if match_remote:
            branch = match_remote.group(1).strip("'\"")
            if branch in self.remote_branches:
                sha = self.remote_branches[branch]
                return 0, f"{sha}\n", ""
            return 1, "", f"fatal: Needed a single revision 'origin/{branch}'"

        # Rev-parse local <branch> / HEAD / refs
        match_local = re.search(r"git rev-parse (?:--verify )?([^\s]+)", cmd)
        if match_local:
            target = match_local.group(1).strip("'\"")
            if not target.startswith("-") and not target.startswith("origin/"):
                if target in self.local_branches:
                    sha = self.local_branches[target]
                    return 0, f"{sha}\n", ""
                if target == "HEAD":
                    return 0, "head_sha_1234567890\n", ""
                if target.startswith("HEAD^"):
                    return 0, "parent_sha_1234567890\n", ""
                if target == "@{u}":
                    return 1, "", "fatal: no upstream configured"
                return 1, "", f"fatal: Needed a single revision '{target}'"

        # merge-base --is-ancestor
        if "merge-base --is-ancestor" in cmd:
            return (0, "", "") if self.ancestor_ok else (1, "", "")

        # git reset --mixed
        if "git reset --mixed" in cmd:
            return 0, "", ""

        # git add
        if "git add" in cmd:
            return 0, "", ""

        # git diff --cached --exit-code (1 = has changes, 0 = no diff)
        if "diff --cached --exit-code" in cmd:
            return (
                (1, "diff --git a/file b/file\n", "")
                if self.diff_cached_changed
                else (0, "", "")
            )

        # git diff-tree HEAD^ HEAD
        if "diff-tree" in cmd:
            return 0, "\n".join(self.committed_paths) + "\n", ""

        # git commit
        if "commit" in cmd:
            return 0, "[feat abc1234] commit message\n", ""

        return 0, "", ""


@pytest.fixture
def repo_setup(tmp_path: Path) -> dict[str, str]:
    """Create a bare origin and a clone with initial commit on main."""
    origin = tmp_path / "origin"
    clone = tmp_path / "clone"
    origin.mkdir()
    assert _git(str(origin), "init", "--bare", "--initial-branch=main").returncode == 0

    cloned = _git(str(tmp_path), "clone", str(origin), str(clone))
    assert cloned.returncode == 0, f"clone failed: {cloned.stderr}"
    _git(str(clone), "config", "user.email", "default-branch@example.com")
    _git(str(clone), "config", "user.name", "default branch tester")

    (clone / "README.md").write_text("# Initial\n")
    (clone / "a.txt").write_text("a\n")
    assert _git(str(clone), "add", "README.md", "a.txt").returncode == 0
    commit = _git(str(clone), "commit", "-m", "Initial commit")
    assert commit.returncode == 0, f"fixture commit failed: {commit.stderr}"
    push = _git(str(clone), "push", "origin", "main")
    assert push.returncode == 0, f"fixture push failed: {push.stderr}"

    assert _git(str(clone), "fetch", "origin").returncode == 0
    symbolic = _git(
        str(clone), "symbolic-ref",
        "refs/remotes/origin/HEAD", "refs/remotes/origin/main",
    )
    assert symbolic.returncode == 0, f"fixture set-head failed: {symbolic.stderr}"

    return {"origin": str(origin), "clone": str(clone), "tmp": str(tmp_path)}


@pytest.fixture
def ctx(repo_setup: dict[str, str]) -> _PublishContext:
    """Build the publish context for one test."""
    return _PublishContext(
        repo_setup["origin"], repo_setup["clone"], repo_setup["tmp"],
    )


@pytest.fixture(autouse=True)
def _host_side_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host side hermetic: no proxy, no ambient token."""
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


def _publish_harness(
    ctx: _PublishContext,
    *,
    branch: str,
    files: list[str],
    base_branch: str = "",
    faults: dict[str, int | str] | None = None,
    default_branch: str = "main",
    message: str = "manifest publish",
    **kwargs: Any,
) -> tuple[dict[str, Any], Any, list[tuple[str, str, bool]]]:
    """Execute publish() with hermetic GitHub API, capturing audit boundary crossings."""
    api = _HostApi(
        ctx.origin,
        publish_branch=branch,
        base_branch=base_branch or default_branch,
        default_branch=default_branch,
        faults=faults,
    )
    ctx.api = api

    from sunaba.tools.secret_scan import run_secret_scan as real_scan

    def scan_spy(
        container: Any,
        scan_files: list[str],
        working_dir: str,
        **scan_kwargs: Any,
    ) -> dict[str, Any]:
        ctx.scan_calls.append(list(scan_files))
        ctx.events.append(("scan", tuple(scan_files)))
        return real_scan(container, scan_files, working_dir, **scan_kwargs)

    def host_base_tip(
        repo: str, token: str, tip_base_branch: str, paths: list[str],
    ) -> UpstreamTipRead:
        return _origin_tip_blobs(
            ctx.origin, tip_base_branch or default_branch, paths,
        )

    crossings: list[tuple[str, str, bool]] = []

    def crossing_spy(cid: str, action: str, reason: str, approved: bool = True) -> None:
        crossings.append((action, reason, approved))
        ctx.events.append(("crossing", (reason, approved)))

    with (
        patch("urllib.request.urlopen", side_effect=api.urlopen),
        patch(
            "sunaba.tools.vcs.publishing._docker",
            return_value=_FakeClient(ctx.container),
        ),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value=""),
        patch("sunaba.tools.vcs.publishing.proxy_configured", return_value=False),
        patch(
            "sunaba.tools.vcs.publishing.record_boundary_crossing",
            side_effect=crossing_spy,
        ),
        patch("sunaba.tools.vcs.publishing._fetch_base_auto_include", return_value=None),
        patch("sunaba.tools.vcs.publishing._fetch_base_tip_blobs", side_effect=host_base_tip),
        patch("sunaba.tools.secret_scan._baseline_enabled", return_value=False),
        patch("sunaba.tools.secret_scan.run_secret_scan", side_effect=scan_spy),
        patch("sunaba.tools.vcs.publishing._create_pr_via_api") as pr_mock,
    ):
        raw = publish(
            container_id=CONTAINER_ID,
            repo=REPO,
            branch=branch,
            message=message,
            working_dir=ctx.clone,
            files=files,
            base_branch=base_branch,
            **kwargs,
        )
    return json.loads(raw), pr_mock, crossings


# ============================================================================
# Acceptance Criteria 1: Command Audit in git_prepare_commit
# ============================================================================


class TestGitPrepareCommitCommandAudit:
    """AC-1: Manifest git_prepare_commit never executes fetch, ls-remote, symbolic-ref, or origin/HEAD."""

    def test_manifest_omitted_base_never_executes_forbidden_commands(self) -> None:
        """Manifest mode with omitted base cuts from origin/<default> without forbidden commands."""
        recorder = CommandRecorder(remote_branches={"main": "sha_main"})
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            base_branch="",
        )
        assert result is None
        assert committed == ["file.txt"]

        # Forbidden commands check
        for cmd in recorder.commands:
            assert "fetch" not in cmd, f"git fetch was executed: {cmd}"
            assert "ls-remote" not in cmd, f"git ls-remote was executed: {cmd}"
            assert "symbolic-ref" not in cmd, f"git symbolic-ref was executed: {cmd}"
            assert "origin/HEAD" not in cmd, f"origin/HEAD was referenced: {cmd}"

        # Expected mixed reset target
        assert any(
            "git reset --mixed origin/main" in cmd for cmd in recorder.commands
        ), f"reset command did not target origin/main: {recorder.commands}"

    def test_manifest_explicit_nondefault_base_never_executes_forbidden_commands(
        self,
    ) -> None:
        """Manifest mode with explicit non-default base never fetches or falls back to local."""
        recorder = CommandRecorder(
            remote_branches={"main": "sha_main", "develop": "sha_develop"},
        )
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            base_branch="develop",
        )
        assert result is None
        assert committed == ["file.txt"]

        for cmd in recorder.commands:
            assert "fetch" not in cmd, f"git fetch was executed: {cmd}"
            assert "ls-remote" not in cmd, f"git ls-remote was executed: {cmd}"
            assert "symbolic-ref" not in cmd, f"git symbolic-ref was executed: {cmd}"
            assert "origin/HEAD" not in cmd, f"origin/HEAD was referenced: {cmd}"
            # Ensure no local-only fallback check (git rev-parse --verify develop)
            assert not re.search(
                r"git rev-parse (?:--verify )?develop\b", cmd
            ), f"local branch fallback rev-parse executed: {cmd}"

        assert any(
            "git merge-base --is-ancestor origin/develop HEAD" in cmd
            for cmd in recorder.commands
        ), f"ancestor check was not executed: {recorder.commands}"
        assert any(
            "git reset --mixed origin/develop" in cmd for cmd in recorder.commands
        ), f"reset command did not target origin/develop: {recorder.commands}"

    def test_manifest_missing_default_never_runs_ls_remote_or_guesses(self) -> None:
        """When origin/<default> is absent, fail immediately without ls-remote or main/master fallback."""
        recorder = CommandRecorder(remote_branches={})  # origin/custom-default absent
        result, _ = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="custom-default",
            base_branch="",
        )
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "base_branch"

        for cmd in recorder.commands:
            assert "ls-remote" not in cmd, f"ls-remote fallback was executed: {cmd}"
            assert "symbolic-ref" not in cmd, f"symbolic-ref fallback was executed: {cmd}"
            assert "origin/HEAD" not in cmd, f"origin/HEAD was referenced: {cmd}"
            assert "origin/main" not in cmd, f"main guess was attempted: {cmd}"
            assert "origin/master" not in cmd, f"master guess was attempted: {cmd}"


# ============================================================================
# Acceptance Criteria 2, 3, 4: Cut From, Base Selection & Error Handling
# ============================================================================


class TestGitPrepareCommitCutFromAndBaseSelection:
    """AC-2, AC-3, AC-4: cut_from assignment, stacked local rejection, empty default handling."""

    def test_omitted_base_uses_origin_host_default_as_cut_from(self) -> None:
        """AC-2: Omitted base sets cut_from to origin/<default>, ignoring forged origin/HEAD."""
        # Even if origin/HEAD were to resolve locally to a decoy SHA
        recorder = CommandRecorder(
            remote_branches={"main": "sha_main", "HEAD": "sha_forged"},
        )
        cut_from: dict[str, str] = {}
        result, _ = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            base_branch="",
            cut_from=cut_from,
        )
        assert result is None
        assert cut_from.get("ref") == "origin/main"
        assert any(
            "git reset --mixed origin/main" in cmd for cmd in recorder.commands
        )

    def test_stacked_local_only_base_fails_step_base_branch_without_commit(
        self,
    ) -> None:
        """AC-3: Explicit base missing remotely fails step=base_branch without falling back to local."""
        recorder = CommandRecorder(
            remote_branches={"main": "sha_main"},
            local_branches={"feature-1": "sha_local_feature_1"},
        )
        result, committed = git_prepare_commit(
            recorder,
            branch="feature-2",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            base_branch="feature-1",
        )
        assert committed is None
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "base_branch"
        error_msg = result["error"]
        assert "origin/feature-1" in error_msg
        assert "tried origin/feature-1 only" in error_msg
        assert "Initialize a container from that branch" in error_msg
        # Assert commit was never executed
        assert not any("commit" in cmd for cmd in recorder.commands)

    def test_base_equals_default_is_not_stacked_and_skips_ancestor_check(self) -> None:
        """AC-3: base_branch == default_branch is treated as default; skips ancestor check."""
        recorder = CommandRecorder(remote_branches={"main": "sha_main"})
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            base_branch="main",
        )
        assert result is None
        assert committed == ["file.txt"]
        assert not any(
            "merge-base --is-ancestor" in cmd for cmd in recorder.commands
        ), f"ancestor check was erroneously executed: {recorder.commands}"
        assert any(
            "git reset --mixed origin/main" in cmd for cmd in recorder.commands
        )

    def test_empty_default_manifest_call_fails_step_base_branch(self) -> None:
        """AC-4: Manifest mode with empty default_branch fails step=base_branch with no ref inference."""
        recorder = CommandRecorder(remote_branches={"main": "sha_main"})
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="",
        )
        assert committed is None
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "base_branch"
        assert not any("origin/HEAD" in cmd for cmd in recorder.commands)
        assert not any("commit" in cmd for cmd in recorder.commands)

    def test_empty_default_manifest_call_with_existing_remote_target_still_fails(
        self,
    ) -> None:
        """AC-4: Empty default_branch fails step=base_branch even if origin/<branch> exists."""
        recorder = CommandRecorder(
            remote_branches={"feat": "sha_feat", "main": "sha_main"},
        )
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="",
        )
        assert committed is None
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "base_branch"
        assert not any("commit" in cmd for cmd in recorder.commands)

    def test_missing_default_ref_fails_step_base_branch_and_names_default(
        self,
    ) -> None:
        """AC-4: When origin/<default> cannot be resolved, return step=base_branch naming the default."""
        recorder = CommandRecorder(remote_branches={})
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            base_branch="",
        )
        assert committed is None
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "base_branch"
        assert "main" in result["error"]
        assert not any("commit" in cmd for cmd in recorder.commands)

    def test_existing_remote_target_precedence_with_supplied_default(self) -> None:
        """AC-4: Existing remote branch (PR follow-up push) takes precedence over default branch."""
        recorder = CommandRecorder(
            remote_branches={"feat": "sha_feat", "main": "sha_main"},
        )
        cut_from: dict[str, str] = {}
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=["file.txt"],
            default_branch="main",
            cut_from=cut_from,
        )
        assert result is None
        assert committed == ["file.txt"]
        assert cut_from.get("ref") == "origin/feat"
        assert any(
            "git reset --mixed origin/feat" in cmd for cmd in recorder.commands
        )

    def test_legacy_files_none_unaffected(self) -> None:
        """AC-4: Legacy mode (files=None) remains unaffected and does not require default_branch."""
        recorder = CommandRecorder()
        result, committed = git_prepare_commit(
            recorder,
            branch="feat",
            message="Msg",
            files=None,
            default_branch="",
        )
        assert result is None
        assert committed == ["file.txt"]
        assert any("git add -A" in cmd for cmd in recorder.commands)


# ============================================================================
# Acceptance Criteria 5: Cut Parent Helpers
# ============================================================================


class TestCutParentHelpers:
    """AC-5: Helper signature extension, no-reread optimization, and backward compatibility."""

    def test_resolve_expected_cut_parents_supplied_default_no_reread(self) -> None:
        """_resolve_expected_cut_parents uses supplied default_branch without rereading GitHub API."""
        with (
            patch("sunaba.tools.vcs.publishing._host_read_branch_ref") as mock_ref,
            patch("sunaba.tools.vcs.publishing._host_read_default_branch") as mock_default,
        ):
            mock_ref.side_effect = [
                HostRefRead("", True, ""),  # publish branch 404 (new branch)
                HostRefRead("sha_custom_default", False, ""),  # default branch ref
            ]
            expected, err = _resolve_expected_cut_parents(
                REPO, "token", branch="feat", base_branch="", merge_is_merge=False,
                default_branch="custom-default",
            )
            assert err is None
            assert expected is not None
            assert expected.parent1 == "sha_custom_default"
            assert expected.parent1_ref == "refs/heads/custom-default"
            mock_default.assert_not_called()

    def test_resolve_expected_cut_parents_omitted_default_backward_compatible(
        self,
    ) -> None:
        """_resolve_expected_cut_parents retains API read fallback when default_branch is omitted."""
        with (
            patch("sunaba.tools.vcs.publishing._host_read_branch_ref") as mock_ref,
            patch("sunaba.tools.vcs.publishing._host_read_default_branch") as mock_default,
        ):
            mock_ref.side_effect = [
                HostRefRead("", True, ""),  # publish branch 404
                HostRefRead("sha_main_tip", False, ""),  # default branch ref
            ]
            mock_default.return_value = ("main", "")
            expected, err = _resolve_expected_cut_parents(
                REPO, "token", branch="feat", base_branch="", merge_is_merge=False,
            )
            assert err is None
            assert expected is not None
            assert expected.parent1 == "sha_main_tip"
            assert expected.parent1_ref == "refs/heads/main"
            mock_default.assert_called_once_with(REPO, "token")

    def test_verify_cut_parents_supplied_default_passed_through(self) -> None:
        """_verify_cut_parents accepts default_branch and forwards it without rereading API."""
        recorder = CommandRecorder()
        with (
            patch("sunaba.tools.vcs.publishing._resolve_expected_cut_parents") as mock_resolve,
            patch("sunaba.tools.vcs.publishing._parse_captured_commit") as mock_parse,
        ):
            mock_resolve.return_value = (
                ExpectedCutParents("sha1", "refs/heads/trunk", "", ""),
                None,
            )
            mock_parse.return_value = ("sha1", ["sha1"], "")
            verified_sha, err = _verify_cut_parents(
                recorder,
                repo=REPO,
                token="token",
                branch="feat",
                base_branch="",
                merge_is_merge=False,
                merge_info={},
                default_branch="trunk",
            )
            assert err is None
            assert verified_sha == "sha1"
            assert mock_resolve.call_args.kwargs.get("default_branch") == "trunk" or (
                len(mock_resolve.call_args.args) >= 6 and mock_resolve.call_args.args[5] == "trunk"
            )

    def test_verify_cut_parents_omitted_default_backward_compatible(self) -> None:
        """_verify_cut_parents retains backward compatibility when default_branch is omitted."""
        recorder = CommandRecorder()
        with (
            patch("sunaba.tools.vcs.publishing._resolve_expected_cut_parents") as mock_resolve,
            patch("sunaba.tools.vcs.publishing._parse_captured_commit") as mock_parse,
        ):
            mock_resolve.return_value = (
                ExpectedCutParents("sha1", "refs/heads/main", "", ""),
                None,
            )
            mock_parse.return_value = ("sha1", ["sha1"], "")
            verified_sha, err = _verify_cut_parents(
                recorder,
                repo=REPO,
                token="token",
                branch="feat",
                base_branch="",
                merge_is_merge=False,
                merge_info={},
            )
            assert err is None
            assert verified_sha == "sha1"


# ============================================================================
# Acceptance Criteria 5: publish() Workflow, Upstream Guard & Audit
# ============================================================================


class TestPublishHostDefaultWorkflow:
    """AC-5: publish() single host default read, fail-closed before commit, and guard ref updates."""

    def test_publish_host_default_api_failure_fails_closed_before_commit_or_push(
        self, ctx: _PublishContext,
    ) -> None:
        """AC-5: Metadata failure fails closed immediately with cut_parent_host_read before any commit."""
        (Path(ctx.clone) / "README.md").write_text("# Updated\n")

        result, pr_mock, crossings = _publish_harness(
            ctx,
            branch="feat/api-fail",
            files=["README.md"],
            base_branch="",
            faults={"default": 500},
        )

        assert result["status"] == "error", result
        assert result["step"] == "cut_parent_host_read", result
        assert result["ref"] == f"/repos/{REPO}", result
        assert "could not verify the created commit's parent against GitHub" in result["error"]

        _assert_nothing_pushed(ctx, pr_mock, "feat/api-fail")

        # Boundary crossing audit check
        assert any(
            action == "publish"
            and f"cut_parent refused step=cut_parent_host_read branch=feat/api-fail ref=/repos/{REPO}"
            in reason
            and approved is False
            for action, reason, approved in crossings
        ), f"refusal audit crossing not recorded: {crossings}"

        # CRITICAL: Verify NO commit or checkout was created in the clone
        assert not any(
            "commit" in cmd for cmd in ctx.container.commands
        ), f"git commit was executed despite host API failure: {ctx.container.commands}"
        assert not any(
            "checkout -b" in cmd for cmd in ctx.container.commands
        ), f"git checkout -b was executed despite host API failure: {ctx.container.commands}"

    def test_publish_omitted_base_reads_host_default_exactly_once(
        self, ctx: _PublishContext,
    ) -> None:
        """AC-5: publish() with omitted base reads host default branch exactly once."""
        (Path(ctx.clone) / "README.md").write_text("# Updated once\n")

        with patch(
            "sunaba.tools.vcs.publishing._host_read_default_branch",
            wraps=_host_read_default_branch,
        ) as spy_read:
            result, _, _ = _publish_harness(
                ctx,
                branch="feat/once",
                files=["README.md"],
                base_branch="",
            )
            assert result["status"] in ("ok", "pushed"), result
            assert spy_read.call_count == 1, (
                f"_host_read_default_branch called {spy_read.call_count} times, expected exactly 1"
            )

    def test_publish_explicit_base_still_reads_host_default_and_fails_closed_on_error(
        self, ctx: _PublishContext,
    ) -> None:
        """AC-5: Even when base_branch is explicit, publish() reads host default and fails closed on error."""
        (Path(ctx.clone) / "README.md").write_text("# Updated explicit\n")

        result, pr_mock, crossings = _publish_harness(
            ctx,
            branch="feat/explicit-fail",
            files=["README.md"],
            base_branch="main",
            faults={"default": 500},
        )

        assert result["status"] == "error", result
        assert result["step"] == "cut_parent_host_read", result
        assert result["ref"] == f"/repos/{REPO}", result
        _assert_nothing_pushed(ctx, pr_mock, "feat/explicit-fail")

        assert not any(
            "commit" in cmd for cmd in ctx.container.commands
        ), f"commit was executed despite default branch failure: {ctx.container.commands}"

    def test_publish_merge_and_upstream_guard_use_host_default_ref(
        self, ctx: _PublishContext,
    ) -> None:
        """AC-5: Merge detection and upstream guard use origin/<host_default>, never origin/HEAD."""
        (Path(ctx.clone) / "README.md").write_text("# Guard test\n")

        result, _, _ = _publish_harness(
            ctx,
            branch="feat/guard-test",
            files=["README.md"],
            base_branch="",
            default_branch="main",
        )
        assert result["status"] in ("ok", "pushed"), result

        # Ensure origin/HEAD was never executed during publish
        for cmd in ctx.container.commands:
            assert "origin/HEAD" not in cmd, f"origin/HEAD was referenced: {cmd}"

        # Upstream guard probe uses origin/main
        assert any(
            "origin/main" in cmd for cmd in ctx.container.commands
        ), f"origin/main was not probed: {ctx.container.commands}"

    def test_publish_upstream_guard_quotes_explicit_base_branch(
        self, ctx: _PublishContext,
    ) -> None:
        """AC-5: Upstream guard properly quotes explicit base_branch with special characters."""
        (Path(ctx.clone) / "README.md").write_text("# Quoting test\n")

        result, _, _ = _publish_harness(
            ctx,
            branch="feat/quoting",
            files=["README.md"],
            base_branch="base with spaces",
            default_branch="main",
            faults={"base": 404},  # base does not exist remotely, stops after guard/commit attempt
        )
        # Verify the command log had origin/'base with spaces'
        assert any(
            "origin/'base with spaces'" in cmd or 'origin/"base with spaces"' in cmd
            for cmd in ctx.container.commands
        ), f"guard_base_ref was not properly quoted: {ctx.container.commands}"


# ============================================================================
# Acceptance Criteria 6: Real Git End-to-End Test
# ============================================================================


class TestRealGitPublishCutParent:
    """AC-6: Real git repo + clone with divergent branches and forged origin/HEAD cuts from host default."""

    def test_real_git_divergent_default_and_decoy_with_reassigned_origin_head_cuts_from_default(
        self, ctx: _PublishContext,
    ) -> None:
        """AC-6: When origin/HEAD points to divergent decoy branch, publish still cuts from host default."""
        main_sha = _sha(ctx.origin, "refs/heads/main")

        # 1. Create a divergent decoy branch on the remote origin
        assert _git(ctx.clone, "checkout", "-b", "decoy", "main").returncode == 0
        (Path(ctx.clone) / "decoy.txt").write_text("decoy content\n")
        assert _git(ctx.clone, "add", "decoy.txt").returncode == 0
        assert _git(ctx.clone, "commit", "-m", "decoy branch commit").returncode == 0
        assert _git(ctx.clone, "push", "origin", "decoy").returncode == 0
        decoy_sha = _sha(ctx.origin, "refs/heads/decoy")
        assert decoy_sha != main_sha, "decoy branch must diverge from main"

        # 2. Reset clone to main and fetch all remote refs
        assert _git(ctx.clone, "checkout", "main").returncode == 0
        assert _git(ctx.clone, "fetch", "origin").returncode == 0

        # 3. Forged container ref: reassign origin/HEAD to point to origin/decoy
        reassign = _git(
            ctx.clone,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/decoy",
        )
        assert reassign.returncode == 0, reassign.stderr

        # 4. Modify tracked file and execute manifest publish with omitted base_branch
        (Path(ctx.clone) / "README.md").write_text("# Real git cut from default\n")

        result, pr_mock, _ = _publish_harness(
            ctx,
            branch="feat/real-cut",
            files=["README.md"],
            base_branch="",
            default_branch="main",
        )

        assert result["status"] in ("ok", "pushed"), f"publish failed: {result}"

        # 5. Verify the actual created commit's parent1 in the clone is main_sha, NOT decoy_sha
        created_parent = _sha(ctx.clone, "HEAD^1")
        assert created_parent == main_sha, (
            f"Commit was cut from {created_parent}, expected host default {main_sha}"
        )
        assert created_parent != decoy_sha, (
            f"Commit was incorrectly cut from forged origin/HEAD decoy {decoy_sha}"
        )
