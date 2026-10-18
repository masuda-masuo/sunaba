"""Acceptance and regression oracle for sunaba #932: fresh publication bases.

Verifies manifest publish remote base resolution, host-managed authorization
grant usage, fresh base parenting, undeclared content preservation, fetch
failure refusal, and upstream overwrite conflict controls.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

import sunaba.proxy_client
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.vcs.publishing import UpstreamTipRead, publish
from tests.conftest import _FakeClient
from tests.test_publish_ops_gitrepo import (
    _advance_upstream,
    _git,
    _origin_tip_blobs,
    _RealGitContainer,
)


class GrantSpy:
    """Spies on host authorization grants during publication."""

    def __init__(self) -> None:
        self.active: bool = False
        self.calls: list[dict[str, Any]] = []
        self.fetch_while_active: bool = False

    @contextlib.contextmanager
    def capture(self, repo: str, *args: Any, **kwargs: Any) -> Iterator[None]:
        self.active = True
        self.calls.append({"repo": repo, "args": args, "kwargs": kwargs})
        try:
            yield
        finally:
            self.active = False


class _IntegrityGitContainer(_RealGitContainer):
    """RealGitContainer enforcing host authorization and fetch controls."""

    def __init__(
        self,
        working_dir: str,
        grant_spy: GrantSpy | None = None,
        require_auth_for_fetch: bool = True,
    ) -> None:
        super().__init__(working_dir)
        self.grant_spy = grant_spy
        self.require_auth_for_fetch = require_auth_for_fetch
        self.force_fetch_error: bool = False

    def exec_run(
        self,
        cmd: list[str] | None = None,
        stdout: bool = True,
        stderr: bool = True,
        environment: dict[str, str] | None = None,
        demux: bool = False,
        workdir: str | None = None,
        **kwargs: Any,
    ):
        argv = list(cmd or [])
        cmd_str = (
            argv[2]
            if len(argv) > 2 and argv[0] in ("/bin/sh", "sh")
            else " ".join(argv)
        )
        is_fetch = False
        for part in cmd_str.split("&&"):
            tokens = shlex.split(part.strip())
            if tokens and tokens[0] == "git" and "fetch" in tokens:
                is_fetch = True
                break
        if is_fetch:
            if self.force_fetch_error:
                return self._reply(
                    128,
                    b"",
                    b"fatal: unable to access remote: network unreachable\n",
                    demux,
                )
            if self.require_auth_for_fetch and self.grant_spy is not None:
                if not self.grant_spy.active:
                    return self._reply(
                        128,
                        b"",
                        b"fatal: could not read Username: authorization grant required\n",
                        demux,
                    )
                self.grant_spy.fetch_while_active = True
        return super().exec_run(
            cmd, stdout, stderr, environment, demux, workdir, **kwargs
        )


@pytest.fixture
def repo_env(tmp_path: Path):
    """Create a bare origin and a working clone at initial commit A."""
    origin_dir = tmp_path / "origin"
    clone_dir = tmp_path / "clone"

    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", str(origin_dir))
    _git(str(tmp_path), "clone", str(origin_dir), str(clone_dir))

    _git(str(clone_dir), "config", "user.email", "tester@example.com")
    _git(str(clone_dir), "config", "user.name", "Tester")

    (clone_dir / "preserve.txt").write_text("preserve A\n")
    (clone_dir / "delete_me.txt").write_text("delete me\n")
    (clone_dir / "declared_edit.txt").write_text("orig\n")
    _git(str(clone_dir), "add", ".")
    commit = _git(str(clone_dir), "commit", "-m", "Commit A")
    assert commit.returncode == 0
    push = _git(str(clone_dir), "push", "origin", "main")
    assert push.returncode == 0
    _git(str(clone_dir), "fetch", "origin")
    _git(
        str(clone_dir),
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    a_sha = _git(str(clone_dir), "rev-parse", "HEAD").stdout.strip()

    return {
        "origin_dir": str(origin_dir),
        "clone_dir": str(clone_dir),
        "a_sha": a_sha,
    }


def _run_publish(
    clone: str,
    container: _IntegrityGitContainer,
    grant_spy: GrantSpy,
    repo_setup: dict[str, Any],
    tip_read_override: UpstreamTipRead | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    tip_read_fn = (
        (lambda _repo, _token, _base, _paths: tip_read_override)
        if tip_read_override is not None
        else (
            lambda repo, token, base_branch, paths: _origin_tip_blobs(
                repo_setup["origin_dir"], base_branch or "main", paths
            )
        )
    )

    with (
        patch.dict(os.environ, {ENABLE_EGRESS_PROXY_ENV: "false"}),
        patch(
            "sunaba.tools.vcs.publishing._docker",
            return_value=_FakeClient(container),
        ),
        patch("sunaba.tools.vcs.publishing._create_pr_via_api"),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        patch(
            "sunaba.tools.vcs.publishing._resolve_vcs_token",
            return_value="ghp_test_token",
        ),
        patch("sunaba.tools.vcs.publishing.proxy_configured", return_value=False),
        patch(
            "sunaba.tools.vcs.publishing._fetch_base_auto_include",
            return_value=None,
        ),
        patch(
            "sunaba.tools.vcs.publishing._fetch_base_tip_blobs",
            side_effect=tip_read_fn,
        ),
        patch("sunaba.tools.secret_scan._baseline_enabled", return_value=False),
        patch.object(
            sunaba.proxy_client,
            "authorized_read_grant",
            side_effect=grant_spy.capture,
        ),
    ):
        import sunaba.tools.vcs.publishing as pub_mod

        patch_pub = (
            patch.object(
                pub_mod, "authorized_read_grant", side_effect=grant_spy.capture
            )
            if hasattr(pub_mod, "authorized_read_grant")
            else contextlib.nullcontext()
        )
        with patch_pub:
            raw = publish(
                container_id="abc123def456",
                repo="owner/repo",
                working_dir=clone,
                **kwargs,
            )
            return json.loads(raw)


def test_manifest_publish_refreshes_fresh_remote_base_under_auth_grant(
    repo_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 1 & 2: Remote tracking refs refreshed under auth grant before commit prep.

    Commit is parented on fresh B, preserving B's undeclared additions and modifications,
    while applying declared edit and deletion. Push verified locally without network.
    """
    origin_dir = repo_env["origin_dir"]
    clone = repo_env["clone_dir"]

    # Remote advances to commit B
    b_sha = _advance_upstream(
        origin_dir,
        {
            "upstream_new.txt": "upstream new\n",
            "preserve.txt": "preserve B\n",
        },
        message="upstream advance B",
    )

    # Stale clone has declared edit and declared deletion
    (Path(clone) / "declared_edit.txt").write_text("local edit\n")
    (Path(clone) / "delete_me.txt").unlink()

    grant_spy = GrantSpy()
    container = _IntegrityGitContainer(
        clone, grant_spy=grant_spy, require_auth_for_fetch=True
    )

    result = _run_publish(
        clone,
        container,
        grant_spy,
        repo_env,
        branch="feat/fresh-base",
        message="Manifest publish onto fresh base",
        files=["declared_edit.txt", "delete_me.txt"],
    )

    # 1. Host-managed authorization mechanism must be used around fetch
    assert len(grant_spy.calls) >= 1, "authorized_read_grant was not opened"
    assert grant_spy.calls[0]["repo"] == "owner/repo"
    assert (
        grant_spy.fetch_while_active is True
    ), "fetch did not occur under authorized_read_grant"

    # 2. No credential file written in container
    assert not (Path(clone) / ".git-credentials").exists()
    assert not (Path(clone) / ".netrc").exists()
    config_dump = _git(clone, "config", "--list").stdout.lower()
    assert "token" not in config_dump

    # 3. Successful publish status
    assert result.get("status") == "pushed", f"publish did not succeed: {result}"
    pushed_sha = result["sha"]

    # 4. Commit parent is fresh B (not stale A)
    parent_sha = _git(clone, "rev-parse", f"{pushed_sha}^").stdout.strip()
    assert parent_sha == b_sha, f"Expected parent {b_sha} (B), got {parent_sha}"

    # 5. Undeclared files from B are preserved
    assert _git(clone, "show", f"{pushed_sha}:preserve.txt").stdout == "preserve B\n"
    assert (
        _git(clone, "show", f"{pushed_sha}:upstream_new.txt").stdout
        == "upstream new\n"
    )

    # 6. Declared changes are applied
    assert (
        _git(clone, "show", f"{pushed_sha}:declared_edit.txt").stdout
        == "local edit\n"
    )
    del_check = _git(clone, "rev-parse", "--verify", f"{pushed_sha}:delete_me.txt")
    assert del_check.returncode != 0, "deleted file delete_me.txt still in tree"

    # 7. Push was captured locally at bare origin (no real network)
    assert (
        _git(origin_dir, "rev-parse", "refs/heads/feat/fresh-base").stdout.strip()
        == _git(clone, "rev-parse", pushed_sha).stdout.strip()
    )


def test_manifest_publish_refuses_when_remote_fetch_fails(
    repo_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 3: Fetch failure must refuse manifest publication before reset/commit/push.

    Worktree and HEAD are preserved; applies even if remote API tip is unavailable.
    """
    origin_dir = repo_env["origin_dir"]
    clone = repo_env["clone_dir"]

    # Local worktree modification
    (Path(clone) / "worktree.txt").write_text("uncommitted worktree modification\n")
    head_before = _git(clone, "rev-parse", "HEAD").stdout.strip()
    worktree_before = (Path(clone) / "worktree.txt").read_text()

    grant_spy = GrantSpy()
    container = _IntegrityGitContainer(
        clone, grant_spy=grant_spy, require_auth_for_fetch=False
    )
    container.force_fetch_error = True

    # Remote API tip is also unavailable
    tip_err = UpstreamTipRead(
        base_sha="", blobs={}, error="GitHub API unreachable HTTP 500"
    )

    result = _run_publish(
        clone,
        container,
        grant_spy,
        repo_env,
        branch="feat/fetch-fail",
        message="Attempt publish with broken fetch",
        files=["worktree.txt"],
        tip_read_override=tip_err,
    )

    # Must refuse with status == "error" and step == "fetch"
    assert result.get("status") == "error", f"Expected status error, got: {result}"
    assert result.get("step") == "fetch", f"Expected step 'fetch', got: {result}"
    assert "network unreachable" in str(result.get("error", "")), (
        f"Injected stderr 'network unreachable' not found in error: {result}"
    )

    # No push occurred
    pushed_ref = _git(
        origin_dir, "rev-parse", "--verify", "refs/heads/feat/fetch-fail"
    )
    assert pushed_ref.returncode != 0, "Branch was pushed despite fetch failure"

    # No commit preparation: HEAD and worktree preserved
    head_after = _git(clone, "rev-parse", "HEAD").stdout.strip()
    assert (
        head_after == head_before
    ), f"HEAD moved: before={head_before}, after={head_after}"
    assert (Path(clone) / "worktree.txt").read_text() == worktree_before


def test_upstream_overwrite_conflict_control_under_fresh_base(
    repo_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4: Conflicting declared edit after remote advance refuses by default.

    Explicit allow_upstream_overwrite=True remains authorized and parents on fresh B.
    """
    origin_dir = repo_env["origin_dir"]
    clone = repo_env["clone_dir"]

    # Remote advances to B, modifying declared_edit.txt and adding undeclared_base.txt
    b_sha = _advance_upstream(
        origin_dir,
        {
            "declared_edit.txt": "upstream edit B\n",
            "undeclared_base.txt": "undeclared from B\n",
        },
        message="upstream advance with conflict",
    )

    # Local clone makes a conflicting edit based on old A
    (Path(clone) / "declared_edit.txt").write_text("local conflicting edit\n")
    head_before = _git(clone, "rev-parse", "HEAD").stdout.strip()

    grant_spy = GrantSpy()
    container = _IntegrityGitContainer(
        clone, grant_spy=grant_spy, require_auth_for_fetch=True
    )

    # Part A: Default refusal (allow_upstream_overwrite=False)
    res_default = _run_publish(
        clone,
        container,
        grant_spy,
        repo_env,
        branch="feat/conflict-default",
        message="Publish conflicting edit without override",
        files=["declared_edit.txt"],
        allow_upstream_overwrite=False,
    )
    assert (
        res_default.get("status") == "error"
    ), f"Expected error on conflict: {res_default}"
    assert (
        res_default.get("step") == "upstream_overwrite"
        or "declared_edit.txt" in str(res_default.get("conflicting_paths", []))
        or "conflict" in str(res_default).lower()
    )
    # No push
    assert (
        _git(origin_dir, "rev-parse", "--verify", "refs/heads/feat/conflict-default").returncode
        != 0
    )
    # HEAD unchanged
    assert _git(clone, "rev-parse", "HEAD").stdout.strip() == head_before

    # Part B: Explicit allow_upstream_overwrite=True succeeds and parents on fresh B
    res_allow = _run_publish(
        clone,
        container,
        grant_spy,
        repo_env,
        branch="feat/conflict-allow",
        message="Publish conflicting edit with explicit override",
        files=["declared_edit.txt"],
        allow_upstream_overwrite=True,
    )
    assert res_allow.get("status") == "pushed", f"Expected pushed: {res_allow}"
    assert res_allow.get("upstream_overwrite_override") is True
    pushed_sha = res_allow["sha"]

    # Commit parented on fresh B (not stale A)
    parent_sha = _git(clone, "rev-parse", f"{pushed_sha}^").stdout.strip()
    assert parent_sha == b_sha, f"Expected parent {b_sha} (fresh B), got {parent_sha}"

    # Overwritten declared file has local edit, while B's undeclared file is preserved
    assert (
        _git(clone, "show", f"{pushed_sha}:declared_edit.txt").stdout
        == "local conflicting edit\n"
    )
    assert (
        _git(clone, "show", f"{pushed_sha}:undeclared_base.txt").stdout
        == "undeclared from B\n"
    )

    # Branch pushed to bare origin
    assert (
        _git(origin_dir, "rev-parse", "refs/heads/feat/conflict-allow").stdout.strip()
        == _git(clone, "rev-parse", pushed_sha).stdout.strip()
    )
