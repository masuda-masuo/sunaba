"""Tests for sandbox_initialize(apply_from_container=...) -- Issue #889.

The carry is exercised with a mocked docker client (no real daemon): the old
container's ``git diff --binary HEAD`` is faked, the patch is shuttled through
the real ``copy_file`` mechanism into the new container, and ``git apply
--3way`` / ``git reset -q`` are asserted on the new container's mocked exec.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.container.lifecycle import _carry_worktree, sandbox_initialize
from tests.test_publish_ops_gitrepo import (
    _git,
    _git_raw_bytes,
    _RealGitContainer,
)


def _parse_init_error(result: str) -> dict[str, Any]:
    """Parse structured JSON error from sandbox_initialize result."""
    raw = result.strip()
    if raw.startswith("Error:"):
        raw = raw[len("Error:"):].strip()
    return json.loads(raw)

OLD_CID = "0f0d8d07a070"
NEW_CID = "new123456789"


def _make_container(cid: str, working_dir: str = "/workspace") -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.labels = {}
    # The repo root is recorded in the container's WorkingDir at creation
    # time (sandbox_initialize passes clone_dest as working_dir), so
    # resolve_git_root reads it back from attrs -- give the mock a real one.
    c.attrs = {"Config": {"WorkingDir": working_dir}}
    return c


def _make_client(old_container: MagicMock, new_container: MagicMock) -> MagicMock:
    client = MagicMock()
    client.containers.run.return_value = new_container
    client.containers.list.return_value = []
    # copy_file looks the new container up by id; the carry looks the old up.
    def _get(cid: str) -> MagicMock:
        if cid == OLD_CID:
            return old_container
        return new_container

    client.containers.get.side_effect = _get
    return client


def _fake_exec_factory(
    patch_bytes: bytes = b"",
    untracked: bytes = b"",
    conflict_files: bytes = b"",
    changed: bytes = b"",
    conflict_apply: bool = False,
    base: bytes = b"HEAD\n",
    commits: bytes = b"0\n",
    manifest: bytes = b"",
    revparse_branch: bytes = b"main\n",
    origin_remote_url: bytes = b"https://github.com/owner/repo.git\n",
    fetch_error: bool = False,
    fetch_error_msg: bytes = b"fatal: network error\n",
):
    """Return an exec_run side_effect that answers the git commands we issue.

    *base* / *commits* drive the checkpointed-commit path (finding 2): the
    merge-base answer and the ``rev-list --count <base>..HEAD`` answer.
    *manifest* is what the dependency-install manifest probe sees (so pip
    install actually runs when a python project is present). *revparse_branch*
    is the answer to ``git rev-parse --abbrev-ref HEAD`` used by the branch
    checkout path.
    """

    def fake(cmd, *args, **kwargs):
        if isinstance(cmd, (list, tuple)):
            c = " ".join(cmd)
        else:
            c = str(cmd)

        # The real docker SDK returns (stdout, stderr) tuples when demux=True
        # (which the branch/pr checkout and pip-install paths use); callers
        # unpack those.  Mirror that shape so the fake behaves like docker.
        def _ret(payload: bytes):
            if kwargs.get("demux"):
                return (0, (payload, b""))
            return (0, payload)

        if "git remote get-url origin" in c:
            return _ret(origin_remote_url)
        if "fetch origin" in c:
            if fetch_error:
                return (128, (b"", fetch_error_msg)) if kwargs.get("demux") else (128, fetch_error_msg)
            return _ret(b"")
        if "git merge-base HEAD origin/HEAD" in c:
            return _ret(base)
        if "git rev-list --count" in c:
            return _ret(commits)
        if "git diff --binary" in c:
            return _ret(patch_bytes)
        if "for f in" in c:
            # The manifest probe (``cd <dir> && for f in pyproject.toml ...``).
            return _ret(manifest)
        if "git rev-parse --abbrev-ref HEAD" in c:
            return _ret(revparse_branch)
        if "git ls-files --others --exclude-standard" in c:
            return _ret(untracked)
        if "git apply" in c:
            if conflict_apply:
                return (1, b"error: patch does not apply cleanly") if not kwargs.get("demux") else (1, (b"error: patch does not apply cleanly", b""))
            return _ret(b"")
        if "git diff --name-only --diff-filter=U" in c:
            return _ret(conflict_files)
        if "git diff --name-only" in c:
            return _ret(changed)
        if "git reset -q" in c:
            return _ret(b"")
        if "id -u" in c or "id -g" in c:
            # copy_file resolves the container user uid/gid via `id`.
            return _ret(b"1000\n")
        # Everything else (clone, mkdir/chown, manifest probe, copy probes).
        return _ret(b"")

    return fake


def _cmd_strings(container: MagicMock) -> list[str]:
    out = []
    for call in container.exec_run.call_args_list:
        cmd = call.args[0]
        last = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd
        out.append(last if isinstance(last, str) else str(last))
    return out


@contextmanager
def _patched(client: MagicMock):
    """Patch the docker client + its side effects so init runs headless."""
    with patch("sunaba.tools.container._docker", return_value=client), patch(
        "sunaba.tools.common._docker", return_value=client
    ), patch(
        "sunaba.tools.file._docker", return_value=client
    ), patch(
        "sunaba.tools.container.lifecycle._ensure_image"
    ), patch(
        "sunaba.tools.container.lifecycle.validate_image_ref"
    ), patch(
        "sunaba.tools.container.lifecycle.build_secure_run_kwargs",
        return_value={},
    ), patch(
        "sunaba.tools.container.lifecycle._resolve_image_ref",
        lambda x: x,
    ), patch(
        "sunaba.proxy_lifecycle.egress_proxy_enabled",
        return_value=False,
    ), patch(
        # The branch/pr checkout paths resolve a host token and the default
        # branch via network; keep them offline for a headless test.
        "sunaba.tools.container.clone._resolve_vcs_token",
        return_value=None,
    ), patch(
        "sunaba.tools.container.clone._resolve_default_branch",
        return_value="main",
    ), patch(
        "sunaba.tools.container.clone.record_copy",
    ), patch(
        "sunaba.tools.container.lifecycle.record_initialize"
    ), patch(
        "sunaba.tools.container.lifecycle.record_initialize_complete"
    ), patch(
        "sunaba.tools.container.lifecycle.record_stop"
    ) as mock_stop, patch(
        "sunaba.tools.file.record_copy"
    ):
        yield mock_stop


def test_carried_two_files_clean():
    """A 2-file patch applies cleanly and the worktree carries the change."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = (
        b"diff --git a/src/a.py b/src/a.py\n+line_a\n"
        b"diff --git a/src/b.py b/src/b.py\n+line_b\n"
    )
    changed = b"src/a.py\nsrc/b.py\n"
    fake = _fake_exec_factory(patch_bytes, changed=changed)
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 2 file(s)" in result, result
    assert "2 applied cleanly" in result, result
    cmds = _cmd_strings(new)
    assert any("git apply --3way /tmp/sunaba-carry.patch" in c for c in cmds)
    assert any("git reset -q" in c for c in cmds)
    # The patch was placed in the new container via copy_file.
    assert any("sunaba-carry.patch" in c for c in cmds)


def test_conflict_reported_and_not_error():
    """A path that conflicts is reported, not turned into an Error."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = b"diff --git a/src/x.py b/src/x.py\n+line\n"
    changed = b"src/x.py\n"
    conflict_files = b"src/x.py\n"
    fake = _fake_exec_factory(
        patch_bytes,
        conflict_files=conflict_files,
        changed=changed,
        conflict_apply=True,
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "1 with conflicts: src/x.py" in result, result
    cmds = _cmd_strings(new)
    # Reset still runs so the index is clean even with conflict markers left.
    assert any("git reset -q" in c for c in cmds)


def test_apply_without_clone_errors():
    """apply_from_container without a clone is an Error naming the argument."""
    client = _make_client(_make_container(OLD_CID), _make_container(NEW_CID))

    with _patched(client):
        result = sandbox_initialize(apply_from_container=OLD_CID)

    assert result.startswith("Error:"), result
    assert "apply_from_container" in result
    # No container should have been created for a no-clone carry request.
    client.containers.run.assert_not_called()


def test_unknown_old_container_errors_and_cleans_up():
    """An unknown old container yields an Error and no half-init container."""
    from docker.errors import NotFound

    new = _make_container(NEW_CID)
    new.exec_run.side_effect = _fake_exec_factory(b"")
    old = _make_container(OLD_CID)
    old.status = "running"
    client = _make_client(old, new)

    def _get(cid: str) -> MagicMock:
        if cid == OLD_CID:
            raise NotFound("no such container")
        return new

    client.containers.get.side_effect = _get

    with _patched(client) as stop:
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert result.startswith("Error:"), result
    # The new container was created but torn down -- same path as other
    # init failures, so nothing is left running.
    assert new.remove.called
    assert stop.called


def test_git_apply_fatal_error_is_reported():
    """A non-conflict apply failure is a real Error, not a silent carry."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    # apply --3way fails AND there are no conflicted (U) paths -> real error.
    fake = _fake_exec_factory(
        patch_bytes=b"diff --git a/src/z.py b/src/z.py\n+line\n",
        conflict_files=b"",
        changed=b"",
        conflict_apply=True,
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client) as stop:
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert result.startswith("Error:"), result
    assert "git apply failed" in result, result
    # The new container was created but torn down on the carry error.
    assert new.remove.called
    assert stop.called


def test_empty_patch_and_untracked_reported():
    """An empty diff is not an error; untracked files are named, not carried."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(
        patch_bytes=b"",
        untracked=b"a.txt\nb.txt\n",
        changed=b"",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "old container had no uncommitted changes" in result, result
    assert "untracked not carried: a.txt, b.txt" in result, result
    # No patch was copied / applied when there was nothing to carry.
    cmds = _cmd_strings(new)
    assert not any("git apply --3way" in c for c in cmds)


def test_carry_runs_before_install_on_branch_path():
    """On the branch path the carry (git apply) must run BEFORE the dep
    install, so a carried change to pyproject.toml is what pip sees
    (Issue #889 finding 1)."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = (
        b"diff --git a/pyproject.toml b/pyproject.toml\n+carried-dep\n"
    )
    changed = b"pyproject.toml\n"
    # The manifest probe reports a python project so pip install actually
    # runs and we can assert ordering against it.
    fake = _fake_exec_factory(
        patch_bytes,
        changed=changed,
        manifest=b"pyproject.toml\n",
        revparse_branch=b"feature\n",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            branch="feature",
            apply_from_container=OLD_CID,
            pip_extras="[dev]",
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    cmds = _cmd_strings(new)
    apply_idx = next(i for i, c in enumerate(cmds) if "git apply --3way" in c)
    install_idx = next(i for i, c in enumerate(cmds) if "pip install" in c)
    # The carry must land before dependency install on the branch path.
    assert apply_idx < install_idx, cmds


def test_checkpointed_commit_carried():
    """A local (checkpointed) commit made by `checkpoint` plus an uncommitted
    edit both land, and the report states how many commits were folded in
    (Issue #889 finding 2)."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = (
        b"diff --git a/pyproject.toml b/pyproject.toml\n"
        b"--- a/pyproject.toml\n+++ b/pyproject.toml\n@@ -1 +1 @@\n+carried\n"
    )
    changed = b"pyproject.toml\n"
    # merge-base returns a real base (not HEAD) so rev-list reports a commit.
    fake = _fake_exec_factory(
        patch_bytes,
        changed=changed,
        base=b"baseabc123\n",
        commits=b"1\n",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert "(1 checkpointed commit(s) included)" in result, result
    cmds = _cmd_strings(new)
    assert any("git apply --3way /tmp/sunaba-carry.patch" in c for c in cmds)


def test_untracked_probe_uses_repo_root():
    """The untracked-file probe must run inside `cd <repo_root> &&`, matching
    every other git command in _carry_worktree (Issue #889 finding 3) -- it
    must not rely on the container's WORKDIR."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(
        patch_bytes=b"",
        untracked=b"a.txt\nb.txt\n",
        changed=b"",
    )
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    # The probe is issued against the OLD (source) container.
    old_cmds = _cmd_strings(old)
    assert any(
        "cd /workspace && git ls-files --others --exclude-standard" in c
        for c in old_cmds
    ), old_cmds
    # And it is not issued without the cd prefix (the regression we guard).
    assert not any(
        c == "git ls-files --others --exclude-standard" for c in old_cmds
    ), old_cmds


def test_source_diff_runs_in_source_repo_root_not_new_clone_dest():
    """Finding 3 (round-2 form): the source container's diff/merge-base/untracked
    probes must run in the SOURCE container's own repo root, not the new
    container's clone_dest.  When the old container was created with a
    non-default clone_dest that differs from the new call's clone_dest, the
    carry must still land -- the old-side commands must cd into the old
    container's repo root, and the new-side commands into the new one.
    (Issue #889 finding 3.)"""

    OLD_WD = "/srv/old/repo"
    NEW_WD = "/workspace"
    assert OLD_WD != NEW_WD

    old = _make_container(OLD_CID, working_dir=OLD_WD)
    old.status = "running"
    new = _make_container(NEW_CID, working_dir=NEW_WD)
    patch_bytes = (
        b"diff --git a/src/a.py b/src/a.py\n+line_a\n"
    )
    changed = b"src/a.py\n"
    fake = _fake_exec_factory(patch_bytes, changed=changed)
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result

    old_cmds = _cmd_strings(old)
    new_cmds = _cmd_strings(new)

    # Source-side commands (merge-base, binary diff, untracked probe) must
    # run in the OLD container's repo root.
    assert any(
        f"cd {OLD_WD} && git merge-base HEAD origin/HEAD" in c for c in old_cmds
    ), old_cmds
    assert any(
        f"cd {OLD_WD} && git diff --binary" in c for c in old_cmds
    ), old_cmds
    assert any(
        f"cd {OLD_WD} && git ls-files --others --exclude-standard" in c
        for c in old_cmds
    ), old_cmds
    # And must NOT have run in the new container's clone_dest.
    assert not any(NEW_WD in c and "git" in c for c in old_cmds), old_cmds

    # Destination-side commands (apply, reset) must run in the NEW container's
    # repo root, never the old's.
    assert any(
        f"cd {NEW_WD} && git apply --3way /tmp/sunaba-carry.patch" in c
        for c in new_cmds
    ), new_cmds
    assert any(
        f"cd {NEW_WD} && git reset -q" in c for c in new_cmds
    ), new_cmds
    assert not any(OLD_WD in c for c in new_cmds), new_cmds


def test_fallback_base_reported_when_origin_head_missing():
    """When origin/HEAD is unresolvable in the source container, the diff
    base falls back to HEAD -- the init result must say so explicitly,
    since checkpointed commits sitting on top of the (missing) tracking ref
    are silently excluded from the carry otherwise (Issue #889 finding 3)."""

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    patch_bytes = b"diff --git a/src/a.py b/src/a.py\n+line_a\n"
    changed = b"src/a.py\n"
    # Empty merge-base output simulates origin/HEAD being unresolvable.
    fake = _fake_exec_factory(patch_bytes, changed=changed, base=b"")
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert (
        "diff base: HEAD — origin/HEAD missing, checkpointed commits "
        "not included" in result
    ), result


# ---------------------------------------------------------------------------
# Additional coverage for Issue #935 (spec requirement 5)
# ---------------------------------------------------------------------------


def test_carry_fetch_token_provider_failure_fails_closed_and_cleans_up():
    """Token provider exception fails closed, returns structured error without
    leaking unknown secret material, and avoids allocating destination container."""
    secret = "secret_provider_material_99999"
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(b"")
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client) as mock_stop, patch(
        "sunaba.tools.github_api._resolve_vcs_token",
        side_effect=RuntimeError(f"internal token error with {secret}"),
    ):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:")
    data = json.loads(result)
    assert data["status"] == "error"
    assert data["step"] in ("carry_fetch", "fetch")
    assert "Failed to resolve VCS token: token provider error" in data["error"]
    assert data["error"]
    assert data["hint"]
    assert secret not in result
    client.containers.run.assert_not_called()
    assert not new.remove.called
    assert not mock_stop.called


def test_carry_fetch_grant_entry_exception_distinguished_and_redacted():
    """Failure opening authorization grant distinguishes grant entry, redacts
    resolved token, and avoids allocating destination container."""
    token = "ghp_entry_secret_token_11111"
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(b"")
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client) as mock_stop, patch(
        "sunaba.tools.github_api._resolve_vcs_token",
        return_value=token,
    ), patch(
        "sunaba.proxy_client.authorized_read_grant",
        side_effect=RuntimeError(f"grant service unreachable with token {token}"),
    ):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:")
    data = json.loads(result)
    assert data["status"] == "error"
    assert data["step"] in ("carry_fetch", "fetch")
    assert "Failed to open authorization grant for fetch" in data["error"]
    assert data["error"]
    assert data["hint"]
    assert token not in data["error"]
    assert "<redacted>" in data["error"]
    client.containers.run.assert_not_called()
    assert not new.remove.called
    assert not mock_stop.called


def test_carry_fetch_grant_close_exception_distinguished_and_redacted():
    """Failure closing authorization grant distinguishes grant close/cleanup,
    redacts resolved token, and avoids allocating destination container."""
    token = "ghp_close_secret_token_22222"
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(b"")
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    @contextmanager
    def _grant_close_fail(repo: str, *args: Any, **kwargs: Any):
        yield
        raise RuntimeError(f"grant close failed with token {token}")

    with _patched(client) as mock_stop, patch(
        "sunaba.tools.github_api._resolve_vcs_token",
        return_value=token,
    ), patch(
        "sunaba.proxy_client.authorized_read_grant",
        side_effect=_grant_close_fail,
    ):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:")
    data = json.loads(result)
    assert data["status"] == "error"
    assert data["step"] in ("carry_fetch", "fetch")
    assert "Failed to close authorization grant for fetch" in data["error"]
    assert data["error"]
    assert data["hint"]
    assert token not in data["error"]
    assert "<redacted>" in data["error"]
    client.containers.run.assert_not_called()
    assert not new.remove.called
    assert not mock_stop.called


def test_carry_fetch_exec_exception_distinguished_and_redacted():
    """Docker API or subprocess exception during fetch distinguishes execution failure,
    redacts sensitive token, and avoids allocating destination container."""
    token = "ghp_exec_secret_token_33333"
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)

    def _exec_fail(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "git remote get-url origin" in c:
            return (0, b"https://github.com/owner/repo.git\n")
        if "fetch origin" in c:
            raise RuntimeError(f"docker socket EOF with token {token}")
        return (0, b"")

    old.exec_run.side_effect = _exec_fail
    new.exec_run.side_effect = _fake_exec_factory(b"")
    client = _make_client(old, new)

    with _patched(client) as mock_stop, patch(
        "sunaba.tools.github_api._resolve_vcs_token",
        return_value=token,
    ):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:")
    data = json.loads(result)
    assert data["status"] == "error"
    assert data["step"] in ("carry_fetch", "fetch")
    assert "Failed to execute fetch in container" in data["error"]
    assert data["error"]
    assert data["hint"]
    assert token not in data["error"]
    assert "<redacted>" in data["error"]
    client.containers.run.assert_not_called()
    assert not new.remove.called
    assert not mock_stop.called


def test_carry_fetch_proxy_token_isolation_and_correct_repo():
    """When egress proxy is configured:
    - Grant receives correct repo resolved from git remote get-url origin (not labels)
    - Grant receives host token
    - Container exec does NOT receive GITHUB_TOKEN or GH_TOKEN in environment."""
    grant_calls: list[dict[str, Any]] = []

    @contextmanager
    def _spy_grant(repo: str, *args: Any, **kwargs: Any):
        grant_calls.append({"repo": repo, "args": args, "kwargs": kwargs})
        yield

    token = "ghp_proxy_isolated_token_44444"
    old = _make_container(OLD_CID)
    old.status = "running"
    old.labels = {"sunaba.repo": "attacker/spoofed-repo"}
    new = _make_container(NEW_CID)

    last_fetch_env: dict[str, str] | None = None

    def _spy_exec(cmd, *args, **kwargs):
        nonlocal last_fetch_env
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "git remote get-url origin" in c:
            return (0, b"https://github.com/validated-org/real-repo.git\n")
        if "fetch origin" in c:
            last_fetch_env = kwargs.get("environment")
            return (0, b"")
        return (0, b"")

    old.exec_run.side_effect = _spy_exec
    new.exec_run.side_effect = _fake_exec_factory(b"")
    client = _make_client(old, new)

    with _patched(client), patch(
        "sunaba.proxy_client.proxy_configured",
        return_value=True,
    ), patch(
        "sunaba.tools.github_api._resolve_vcs_token",
        return_value=token,
    ), patch(
        "sunaba.proxy_client.authorized_read_grant",
        side_effect=_spy_grant,
    ):
        result = sandbox_initialize(
            clone_repo="validated-org/real-repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    assert len(grant_calls) == 1
    # Must use resolved origin repo from container remote, NOT attacker label
    assert grant_calls[0]["repo"] == "validated-org/real-repo"
    assert grant_calls[0]["kwargs"].get("token") == token
    # Exec environment must NOT contain VCS token when proxied
    if last_fetch_env is not None:
        assert "GITHUB_TOKEN" not in last_fetch_env
        assert "GH_TOKEN" not in last_fetch_env


def test_carry_fetch_failure_preserves_destination_staged_unstaged_and_index(tmp_path: Path):
    """When carry fetch fails, destination container with staged changes, unstaged changes,
    and untracked files must remain completely identical (HEAD, staged index tree, worktree, untracked)."""
    origin_dir = tmp_path / "origin"
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"

    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", str(origin_dir))
    _git(str(tmp_path), "clone", str(origin_dir), str(source_dir))
    _git(str(source_dir), "config", "user.email", "tester@example.com")
    _git(str(source_dir), "config", "user.name", "Tester")
    (source_dir / "base.txt").write_text("base\n")
    _git(str(source_dir), "add", ".")
    _git(str(source_dir), "commit", "-m", "Base commit")
    _git(str(source_dir), "push", "origin", "main")

    _git(str(tmp_path), "clone", str(origin_dir), str(dest_dir))
    _git(str(dest_dir), "config", "user.email", "tester@example.com")
    _git(str(dest_dir), "config", "user.name", "Tester")

    # Set up destination with:
    # 1. committed file
    # 2. staged change
    # 3. unstaged change
    # 4. untracked file
    (dest_dir / "staged.txt").write_text("staged content\n")
    _git(str(dest_dir), "add", "staged.txt")
    (dest_dir / "unstaged.txt").write_text("unstaged content\n")
    (dest_dir / "untracked.txt").write_text("untracked content\n")

    # Capture complete pre-carry snapshots
    pre_head = _git(str(dest_dir), "rev-parse", "HEAD").stdout.strip()
    pre_tree = _git(str(dest_dir), "write-tree").stdout.strip()
    pre_status = _git(str(dest_dir), "status", "--porcelain").stdout
    pre_diff_cached = _git(str(dest_dir), "diff", "--cached").stdout
    pre_diff_worktree = _git(str(dest_dir), "diff").stdout

    # Old container configured to fail fetch
    old_c = _RealGitContainer(str(source_dir))
    old_c.id = "oldsource012"
    old_c.status = "running"
    old_c.attrs = {"Config": {"WorkingDir": str(source_dir)}}

    new_c = _RealGitContainer(str(dest_dir))
    new_c.id = "newdest00123"
    new_c.status = "running"
    new_c.attrs = {"Config": {"WorkingDir": str(dest_dir)}}

    client = MagicMock()
    client.containers.get.side_effect = lambda cid: old_c if cid == old_c.id else new_c

    def _old_exec(cmd, **kwargs):
        c = cmd[2] if isinstance(cmd, list) and len(cmd) > 2 else str(cmd)
        if "git remote get-url origin" in c:
            return (0, b"https://github.com/owner/repo.git\n")
        if "fetch origin" in c:
            return (128, b"fatal: mock fetch failure\n")
        return _RealGitContainer.exec_run(old_c, cmd, **kwargs)

    # Force fetch failure in old container
    with patch(
        "sunaba.tools.github_api._resolve_vcs_token",
        return_value="ghp_fake_token",
    ), patch(
        "sunaba.proxy_client.authorized_read_grant",
    ), patch.object(
        old_c,
        "exec_run",
        side_effect=_old_exec,
    ):
        report = _carry_worktree(client, old_c.id, new_c)

    assert report.error is not None
    assert "fetch" in report.error.lower()

    # Destination MUST remain completely untouched
    post_head = _git(str(dest_dir), "rev-parse", "HEAD").stdout.strip()
    post_tree = _git(str(dest_dir), "write-tree").stdout.strip()
    post_status = _git(str(dest_dir), "status", "--porcelain").stdout
    post_diff_cached = _git(str(dest_dir), "diff", "--cached").stdout
    post_diff_worktree = _git(str(dest_dir), "diff").stdout

    assert post_head == pre_head
    assert post_tree == pre_tree
    assert post_status == pre_status
    assert post_diff_cached == pre_diff_cached
    assert post_diff_worktree == pre_diff_worktree
    assert (dest_dir / "staged.txt").read_text() == "staged content\n"
    assert (dest_dir / "unstaged.txt").read_text() == "unstaged content\n"
    assert (dest_dir / "untracked.txt").read_text() == "untracked content\n"


def test_real_git_stale_patch_conflicts_and_fresh_patch_clean(tmp_path: Path):
    """Reproduces NORMAL-MERGE topology to prove:
    - Stale patch against initial commit A includes 3a commits (which created app.py),
      and executing git apply --3way on destination C (which already has app.py) CONFLICTS.
    - Fresh patch against refreshed merge-base B includes ONLY 3b,
      and executing git apply --3way on destination C applies CLEANLY with 0 conflicts."""
    from tests.test_carry_fresh_origin import _make_normal_merge_fixture

    fixture = _make_normal_merge_fixture(tmp_path)
    source_dir = fixture["source_dir"]
    dest_dir = fixture["dest_dir"]
    a_sha = fixture["a_sha"]
    b_sha = fixture["b_sha"]
    uncommitted_content = fixture["uncommitted_content"]

    # 1. Stale patch from commit A (stale merge-base before fetch)
    stale_patch = _git_raw_bytes(source_dir, "diff", "--binary", a_sha)
    stale_patch_file = tmp_path / "stale.patch"
    stale_patch_file.write_bytes(stale_patch)

    # Applying stale patch to destination at commit C fails / conflicts
    apply_res = _git(dest_dir, "apply", "--3way", str(stale_patch_file))
    # git diff --name-only --diff-filter=U shows app.py is conflicted
    conflicts_res = _git(dest_dir, "diff", "--name-only", "--diff-filter=U")
    _git(dest_dir, "reset", "--hard", "HEAD")  # clean up destination for fresh test
    assert apply_res.returncode != 0 or conflicts_res.stdout.strip() != "", (
        "Stale patch must conflict when applied to destination C"
    )

    # 2. Fresh patch from commit B (refreshed merge-base after fetch)
    fresh_patch = _git_raw_bytes(source_dir, "diff", "--binary", b_sha)
    fresh_patch_file = tmp_path / "fresh.patch"
    fresh_patch_file.write_bytes(fresh_patch)

    # Applying fresh patch to destination C applies cleanly with 0 conflicts
    apply_fresh = _git(dest_dir, "apply", "--3way", str(fresh_patch_file))
    assert apply_fresh.returncode == 0
    conflicts_fresh = _git(dest_dir, "diff", "--name-only", "--diff-filter=U")
    assert conflicts_fresh.stdout.strip() == ""
    _git(dest_dir, "reset", "-q")
    assert (Path(dest_dir) / "app.py").read_text() == uncommitted_content


# ---------------------------------------------------------------------------
# Additional coverage for Issue #937 review follow-up:
# Preflight source fetch before destination allocation & single fetch order
# ---------------------------------------------------------------------------


def test_carry_fetch_failure_never_allocates_destination_nor_clones():
    """On source fetch failure in sandbox_initialize:
    - Structured JSON is returned (step="carry_fetch")
    - Destination container is never allocated (containers.run not called)
    - Destination clone/checkout is never executed."""
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(b"", fetch_error=True)
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    data = json.loads(result)
    assert data["status"] == "error"
    assert data["step"] in ("carry_fetch", "fetch")
    assert "fatal: network error" in data["error"]
    assert data["hint"]

    # Destination container was never allocated nor cloned
    client.containers.run.assert_not_called()
    assert not new.remove.called
    assert len(new.exec_run.call_args_list) == 0


@pytest.mark.parametrize(
    "patch_target",
    [
        "sunaba.tools.container.lifecycle.resolve_git_root",
        "sunaba.tools.container.lifecycle._fetch_source_origin",
    ],
)
def test_carry_preflight_unexpected_exception_no_sentinel_exposure_and_no_allocation(
    patch_target: str,
):
    """Preflight unexpected exception (resolve_git_root or _fetch_source_origin) containing sentinel:
    - Sentinel is absent from serialized JSON result
    - Returns nonempty stable generic error and hint
    - Returns step="carry_fetch"
    - Destination container is never allocated (containers.run not called)
    - Destination clone/checkout is never executed."""
    sentinel = "FAKE_SECRET_PREFLIGHT_ONLY"
    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)
    fake = _fake_exec_factory(b"")
    old.exec_run.side_effect = fake
    new.exec_run.side_effect = fake
    client = _make_client(old, new)

    with _patched(client), patch(
        patch_target,
        side_effect=RuntimeError(f"internal failure containing {sentinel}"),
    ):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras=None,
        )

    assert not result.startswith("Error:"), result
    data = json.loads(result)
    assert data["status"] == "error"
    assert data["step"] in ("carry_fetch", "fetch")
    assert data["step"] == "carry_fetch"
    assert "Failed to preflight fetch in source container" in data["error"]
    assert data["error"]
    assert data["hint"]
    assert sentinel not in result

    # Destination container was never allocated nor cloned
    client.containers.run.assert_not_called()
    assert not new.remove.called
    assert len(new.exec_run.call_args_list) == 0


def test_carry_success_single_fetch_before_allocation_on_clone_path():
    """On clone_repo carry path:
    - Exactly one authenticated source fetch runs BEFORE destination allocation
    - Carry applies after checkout and before deps
    - Single fetch per public carry contract is upheld."""
    events: list[str] = []

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)

    base_fake = _fake_exec_factory(
        patch_bytes=b"diff --git a/src/a.py b/src/a.py\n+line_a\n",
        changed=b"src/a.py\n",
        manifest=b"pyproject.toml\n",
    )

    def _old_exec(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "fetch origin" in c:
            events.append("source_fetch")
        return base_fake(cmd, *args, **kwargs)

    def _new_exec(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "git clone" in c:
            events.append("clone")
        elif "git apply" in c:
            events.append("apply")
        elif "pip install" in c:
            events.append("install")
        return base_fake(cmd, *args, **kwargs)

    old.exec_run.side_effect = _old_exec
    new.exec_run.side_effect = _new_exec
    client = _make_client(old, new)

    def _spy_run(*args, **kwargs):
        events.append("destination_run")
        return new

    client.containers.run.side_effect = _spy_run

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=OLD_CID,
            pip_extras="[dev]",
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert events.count("source_fetch") == 1
    assert "destination_run" in events
    assert "apply" in events
    assert "install" in events

    fetch_idx = events.index("source_fetch")
    run_idx = events.index("destination_run")
    apply_idx = events.index("apply")
    install_idx = events.index("install")

    assert fetch_idx < run_idx, f"Source fetch must run before allocation: {events}"
    assert run_idx < apply_idx, f"Allocation must run before carry: {events}"
    assert apply_idx < install_idx, f"Carry must run before dep install: {events}"


def test_carry_success_single_fetch_before_allocation_on_branch_path():
    """On branch checkout carry path:
    - Exactly one authenticated source fetch runs BEFORE destination allocation
    - Branch checkout runs after destination allocation and before carry
    - Carry applies before dependency install."""
    events: list[str] = []

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)

    base_fake = _fake_exec_factory(
        patch_bytes=b"diff --git a/pyproject.toml b/pyproject.toml\n+dep\n",
        changed=b"pyproject.toml\n",
        manifest=b"pyproject.toml\n",
        revparse_branch=b"feat\n",
    )

    def _old_exec(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "fetch origin" in c:
            events.append("source_fetch")
        return base_fake(cmd, *args, **kwargs)

    def _new_exec(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "git checkout" in c or "branch" in c:
            events.append("branch_checkout")
        elif "git apply" in c:
            events.append("apply")
        elif "pip install" in c:
            events.append("install")
        return base_fake(cmd, *args, **kwargs)

    old.exec_run.side_effect = _old_exec
    new.exec_run.side_effect = _new_exec
    client = _make_client(old, new)

    def _spy_run(*args, **kwargs):
        events.append("destination_run")
        return new

    client.containers.run.side_effect = _spy_run

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            branch="feat",
            apply_from_container=OLD_CID,
            pip_extras="[dev]",
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert events.count("source_fetch") == 1
    assert "destination_run" in events
    assert "apply" in events
    assert "install" in events

    fetch_idx = events.index("source_fetch")
    run_idx = events.index("destination_run")
    apply_idx = events.index("apply")
    install_idx = events.index("install")

    assert fetch_idx < run_idx, f"Source fetch must run before allocation: {events}"
    assert run_idx < apply_idx, f"Allocation must run before carry: {events}"
    assert apply_idx < install_idx, f"Carry must run before dep install: {events}"


def test_carry_success_single_fetch_before_allocation_on_pr_path():
    """On PR checkout carry path:
    - Exactly one authenticated source fetch runs BEFORE destination allocation
    - PR setup runs after destination allocation and before carry
    - Carry applies before dependency install."""
    events: list[str] = []

    old = _make_container(OLD_CID)
    old.status = "running"
    new = _make_container(NEW_CID)

    base_fake = _fake_exec_factory(
        patch_bytes=b"diff --git a/pyproject.toml b/pyproject.toml\n+dep\n",
        changed=b"pyproject.toml\n",
        manifest=b"pyproject.toml\n",
        revparse_branch=b"pr-42\n",
    )

    def _old_exec(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "fetch origin" in c:
            events.append("source_fetch")
        return base_fake(cmd, *args, **kwargs)

    def _new_exec(cmd, *args, **kwargs):
        c = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "pr" in c or "pull" in c:
            events.append("pr_setup")
        elif "git apply" in c:
            events.append("apply")
        elif "pip install" in c:
            events.append("install")
        return base_fake(cmd, *args, **kwargs)

    old.exec_run.side_effect = _old_exec
    new.exec_run.side_effect = _new_exec
    client = _make_client(old, new)

    def _spy_run(*args, **kwargs):
        events.append("destination_run")
        return new

    client.containers.run.side_effect = _spy_run

    with _patched(client), patch(
        "sunaba.tools.container.clone._resolve_pr_head_ref",
        return_value=("owner:feature-branch", "main"),
    ):
        result = sandbox_initialize(
            repo="owner/repo",
            pr=42,
            apply_from_container=OLD_CID,
            pip_extras="[dev]",
        )

    assert not result.startswith("Error:"), result
    assert "carried 1 file(s)" in result, result
    assert events.count("source_fetch") == 1
    assert "destination_run" in events
    assert "apply" in events
    assert "install" in events

    fetch_idx = events.index("source_fetch")
    run_idx = events.index("destination_run")
    apply_idx = events.index("apply")
    install_idx = events.index("install")

    assert fetch_idx < run_idx, f"Source fetch must run before allocation: {events}"
    assert run_idx < apply_idx, f"Allocation must run before carry: {events}"
    assert apply_idx < install_idx, f"Carry must run before dep install: {events}"
