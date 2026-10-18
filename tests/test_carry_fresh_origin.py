"""Acceptance and regression tests for sunaba #935: authorized source fetch for carry.

Frozen behavioral contract verified:
1. Actual carry orchestration refreshes source origin under HOST authorized_read_grant
   BEFORE computing merge-base HEAD origin/HEAD. Native path uses established
   credential helper command with token in ephemeral env; proxied source exec
   receives no token, host grant holds it. No credential files/config persistence.
2. Real Git fixture reproduces exact NORMAL-MERGE topology:
   Source cloned A, five checkpoint commits (3a) ending B, remote main merged B
   normally into main C (tree C == B), source still origin A, unmerged tracked
   work (3b). Stale-base patch wrongly includes 3a; carry refreshes origin so
   only 3b is transported to destination at C, clean apply, upstream 3a preserved,
   and merge-base refreshed to B.
3. Fetch failure stops before merge-base/patch/apply, preserves destination
   HEAD/index/worktree/untracked files, never carries using stale base. Public
   initialize response is structured JSON (status: error, step, error, hint).
   Token resolution and read-grant failures fail closed without raw token leaks.
4. Tests exercise production carry boundaries without overfreezing private helper
   internals or command sequencing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import tarfile
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock, patch

import pytest

import sunaba.proxy_client
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.lifecycle import _carry_worktree, sandbox_initialize
from tests.test_init_apply_from_container import _patched
from tests.test_publish_ops_gitrepo import _git, _git_raw_bytes, _RealGitContainer


class GrantSpy:
    """Spies on host authorization grants during carry."""

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


class _CarryGitContainer(_RealGitContainer):
    """RealGitContainer enforcing host authorization and fetch controls for carry."""

    def __init__(
        self,
        working_dir: str,
        cid: str = "carry12345678",
        repo: str = "owner/repo",
        grant_spy: GrantSpy | None = None,
        require_auth_for_fetch: bool = True,
    ) -> None:
        super().__init__(working_dir)
        self.id = cid
        self.repo = repo
        self.labels = {"sunaba.repo": repo}
        self.attrs = {"Config": {"WorkingDir": working_dir}}
        self.status = "running"
        self.grant_spy = grant_spy
        self.require_auth_for_fetch = require_auth_for_fetch
        self.force_fetch_error: bool = False
        self.fetch_error_output: bytes = (
            b"fatal: unable to access remote: network unreachable\n"
        )
        self.fetch_commands: list[str] = []
        self.last_fetch_env: dict[str, str] | None = None

    def reload(self) -> None:
        pass

    def remove(self, force: bool = False) -> None:
        pass

    def put_archive(self, path: str, data: bytes) -> None:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(path, filter="data")
            else:
                tar.extractall(path)

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
        if "git remote get-url origin" in cmd_str:
            return self._reply(
                0, f"https://github.com/{self.repo}.git\n".encode(), b"", demux
            )
        if "git clone" in cmd_str:
            return self._reply(0, b"", b"", demux)

        is_fetch = False
        for part in cmd_str.split("&&"):
            tokens = shlex.split(part.strip())
            if tokens and tokens[0] == "git" and "fetch" in tokens:
                is_fetch = True
                break

        if is_fetch:
            self.fetch_commands.append(cmd_str)
            self.last_fetch_env = environment
            if self.force_fetch_error:
                return self._reply(128, b"", self.fetch_error_output, demux)
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
            cmd,
            stdout=stdout,
            stderr=stderr,
            environment=environment,
            demux=demux,
            workdir=workdir,
            **kwargs,
        )


def _make_normal_merge_fixture(tmp_path: Path) -> dict[str, Any]:
    """Create a bare origin and working clones reproducing the exact NORMAL-MERGE topology.

    Topology (Issue #935):
    - Base commit A on origin main.
    - Source clone cloned at A, tracking origin/main.
    - Source makes 5 checkpoint commits (3a) ending at B.
    - Remote origin main merges B normally into main commit C (tree C == B).
    - Source clone's origin/HEAD is still at A (not yet refreshed).
    - Source clone has uncommitted tracked work (3b) on app.py.
    - Destination clone initialized from origin at commit C.
    """
    origin_dir = tmp_path / "origin"
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    work_dir = tmp_path / "work"

    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", str(origin_dir))
    _git(str(tmp_path), "clone", str(origin_dir), str(source_dir))
    _git(str(source_dir), "config", "user.email", "tester@example.com")
    _git(str(source_dir), "config", "user.name", "Tester")

    (source_dir / "base.txt").write_text("init\n")
    _git(str(source_dir), "add", ".")
    _git(str(source_dir), "commit", "-m", "Commit A")
    _git(str(source_dir), "push", "origin", "main")
    _git(
        str(source_dir),
        "symbolic-ref",
        "refs/remotes/origin/HEAD",
        "refs/remotes/origin/main",
    )
    a_sha = _git(str(source_dir), "rev-parse", "HEAD").stdout.strip()

    # 5 checkpoint commits (3a) ending at commit B
    for i in range(1, 6):
        (source_dir / "app.py").write_text(f"app version {i}\n")
        _git(str(source_dir), "add", "app.py")
        _git(str(source_dir), "commit", "-m", f"checkpoint commit {i}")
    b_sha = _git(str(source_dir), "rev-parse", "HEAD").stdout.strip()

    # Remote main merges B normally into main (commit C, tree C == B)
    _git(str(tmp_path), "clone", str(origin_dir), str(work_dir))
    _git(str(work_dir), "config", "user.email", "tester@example.com")
    _git(str(work_dir), "config", "user.name", "Tester")
    _git(str(work_dir), "fetch", str(source_dir), "HEAD:branch_b")
    _git(
        str(work_dir),
        "merge",
        "--no-ff",
        "-m",
        "Merge B into main (commit C)",
        "branch_b",
    )
    _git(str(work_dir), "push", "origin", "main")
    c_sha = _git(str(work_dir), "rev-parse", "HEAD").stdout.strip()

    # Source has uncommitted tracked work (3b)
    uncommitted_content = "app version 5 + uncommitted 3b\n"
    (source_dir / "app.py").write_text(uncommitted_content)

    # Destination clone at commit C
    _git(str(tmp_path), "clone", str(origin_dir), str(dest_dir))
    _git(str(dest_dir), "config", "user.email", "tester@example.com")
    _git(str(dest_dir), "config", "user.name", "Tester")

    return {
        "origin_dir": str(origin_dir),
        "source_dir": str(source_dir),
        "dest_dir": str(dest_dir),
        "a_sha": a_sha,
        "b_sha": b_sha,
        "c_sha": c_sha,
        "uncommitted_content": uncommitted_content,
    }


def _parse_init_error(result: str) -> dict[str, Any]:
    """Parse structured JSON error from sandbox_initialize result."""
    raw = result.strip()
    if raw.startswith("Error:"):
        raw = raw[len("Error:"):].strip()
    return json.loads(raw)


def test_normal_merge_topology_carry_refreshes_origin_and_transports_only_unmerged_work(
    tmp_path: Path,
) -> None:
    """Pins Contract 1 & 2: Carry refreshes source origin under grant and transports only 3b.

    Reproduction of NORMAL-MERGE topology:
    - Verifies that without fetch, merge-base HEAD origin/HEAD in source is stale base A,
      and diff against A wrongly includes 3a checkpoint commits.
    - Production carry refreshes source origin under host authorized_read_grant.
    - Carry transports ONLY uncommitted work (3b) to destination at C.
    - Clean 3-way apply onto C with zero conflicts.
    - Upstream 3a commits on destination are preserved.
    - Source origin/HEAD is refreshed and merge-base is now commit B.
    """
    fixture = _make_normal_merge_fixture(tmp_path)
    source_dir = fixture["source_dir"]
    dest_dir = fixture["dest_dir"]
    a_sha = fixture["a_sha"]
    b_sha = fixture["b_sha"]
    c_sha = fixture["c_sha"]
    uncommitted_content = fixture["uncommitted_content"]

    # 1. Stale-base defect demonstration:
    # Before fetch, source origin/HEAD still points to initial commit A.
    stale_mb = _git(source_dir, "merge-base", "HEAD", "origin/HEAD").stdout.strip()
    assert stale_mb == a_sha, (
        f"Precondition: source origin/HEAD must be stale at A ({a_sha}), got {stale_mb}"
    )

    # Diff against stale base A includes 3a commits (creation of app.py),
    # which conflicts when applied to destination C where app.py already exists.
    stale_diff = _git_raw_bytes(source_dir, "diff", "--binary", stale_mb)
    assert b"app version" in stale_diff

    # 2. Setup mock client and real git containers
    grant_spy = GrantSpy()
    old_c = _CarryGitContainer(
        source_dir,
        cid="old012345678",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=True,
    )
    new_c = _CarryGitContainer(
        dest_dir,
        cid="new012345678",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )

    client = MagicMock()
    client.containers.get.side_effect = (
        lambda cid: old_c if cid == old_c.id else new_c
    )

    with (
        patch("sunaba.tools.file._docker", return_value=client),
        patch.object(
            sunaba.proxy_client,
            "authorized_read_grant",
            side_effect=grant_spy.capture,
        ),
    ):
        report = _carry_worktree(client, old_c.id, new_c)

    # Carry must have refreshed source origin under host authorized_read_grant
    assert grant_spy.fetch_while_active is True, (
        "git fetch origin must run under host authorized_read_grant"
    )

    # Carry must succeed with clean application
    assert report.error is None, f"Carry failed with error: {report.error}"
    assert report.conflicts == [], f"Expected 0 conflicts, got: {report.conflicts}"
    assert "app.py" in report.applied, (
        f"Expected app.py in applied files, got: {report.applied}"
    )
    # Checkpointed commits folded must be 0 because 3a commits are already part of upstream main C
    assert report.checkpointed_commits == 0, (
        f"Expected 0 checkpointed commits folded into carry, got {report.checkpointed_commits}"
    )

    # Destination worktree must carry uncommitted 3b content
    dest_app = (Path(dest_dir) / "app.py").read_text()
    assert dest_app == uncommitted_content, (
        f"Destination app.py content mismatch:\n{dest_app}\nExpected:\n{uncommitted_content}"
    )

    # Destination git log must preserve upstream merge commit C and 3a history
    dest_head = _git(dest_dir, "rev-parse", "HEAD").stdout.strip()
    assert dest_head == c_sha, (
        f"Destination HEAD should remain at upstream merge C ({c_sha}), got {dest_head}"
    )

    # In source container, origin/HEAD must now resolve fresh merge-base B
    fresh_mb = _git(source_dir, "merge-base", "HEAD", "origin/HEAD").stdout.strip()
    assert fresh_mb == b_sha, (
        f"Source merge-base should be refreshed to commit B ({b_sha}), got {fresh_mb}"
    )


def test_carry_fetch_uses_credential_helper_and_ephemeral_env_without_persistence(
    tmp_path: Path,
) -> None:
    """Pins Contract 1: Fetch uses established credential helper and ephemeral env.

    - Native path (egress proxy disabled):
      Executes `git -c credential.helper= -c credential.helper=... fetch origin`
      with GITHUB_TOKEN passed only in ephemeral exec environment.
    - Proxied path (egress proxy enabled):
      Executes fetch origin without token in exec environment; host grant holds token.
    - No persistent credential files (.git-credentials) or permanent git config.
    """
    fixture = _make_normal_merge_fixture(tmp_path)
    source_dir = fixture["source_dir"]
    dest_dir = fixture["dest_dir"]

    # 1. Native / unproxied execution
    grant_spy = GrantSpy()
    old_c = _CarryGitContainer(
        source_dir,
        cid="oldnative012",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )
    new_c = _CarryGitContainer(
        dest_dir,
        cid="newnative012",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )

    client = MagicMock()
    client.containers.get.side_effect = (
        lambda cid: old_c if cid == old_c.id else new_c
    )

    with (
        patch.dict(os.environ, {ENABLE_EGRESS_PROXY_ENV: "false"}),
        patch("sunaba.tools.file._docker", return_value=client),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value="ghp_native_token"),
        patch.object(
            sunaba.proxy_client,
            "authorized_read_grant",
            side_effect=grant_spy.capture,
        ),
    ):
        report = _carry_worktree(client, old_c.id, new_c)

    assert report.error is None
    assert len(old_c.fetch_commands) > 0, "Source container must have executed git fetch origin"
    fetch_cmd = old_c.fetch_commands[0]
    assert "fetch origin" in fetch_cmd
    assert "credential.helper=" in fetch_cmd, (
        f"Native fetch must use ephemeral credential helper, got: {fetch_cmd}"
    )
    assert old_c.last_fetch_env is not None
    assert old_c.last_fetch_env.get("GITHUB_TOKEN") == "ghp_native_token", (
        "Ephemeral env for native fetch must supply GITHUB_TOKEN"
    )

    # No persistent credentials in repository
    assert not (Path(source_dir) / ".git-credentials").exists()
    git_config = (Path(source_dir) / ".git" / "config").read_text()
    assert "credential.helper" not in git_config

    # 2. Proxied execution: token stays on host grant, not passed to container exec
    proxied_tmp = tmp_path / "proxied"
    proxied_tmp.mkdir(parents=True, exist_ok=True)
    proxied_fixture = _make_normal_merge_fixture(proxied_tmp)
    source_dir_proxied = proxied_fixture["source_dir"]
    dest_dir_proxied = proxied_fixture["dest_dir"]
    old_c_proxied = _CarryGitContainer(
        source_dir_proxied,
        cid="oldproxied01",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )
    new_c_proxied = _CarryGitContainer(
        dest_dir_proxied,
        cid="newproxied01",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )
    client_proxied = MagicMock()
    client_proxied.containers.get.side_effect = (
        lambda cid: old_c_proxied if cid == old_c_proxied.id else new_c_proxied
    )

    with (
        patch.dict(os.environ, {ENABLE_EGRESS_PROXY_ENV: "true"}),
        patch("sunaba.tools.file._docker", return_value=client_proxied),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value="ghp_proxied_token"),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch.object(
            sunaba.proxy_client,
            "authorized_read_grant",
            side_effect=grant_spy.capture,
        ),
    ):
        report_proxied = _carry_worktree(client_proxied, old_c_proxied.id, new_c_proxied)

    assert report_proxied.error is None
    assert len(old_c_proxied.fetch_commands) > 0
    if old_c_proxied.last_fetch_env:
        assert "GITHUB_TOKEN" not in old_c_proxied.last_fetch_env, (
            "Proxied container exec must not receive VCS token in environment"
        )


def test_carry_fetch_failure_stops_before_merge_base_and_preserves_destination(
    tmp_path: Path,
) -> None:
    """Pins Contract 3 & 4: Fetch failure must stop early and preserve destination.

    - Fetch failure stops before merge-base, diff, and apply.
    - Never carries using stale base A.
    - Destination HEAD, index, worktree, and untracked files are strictly preserved.
    - Helper reports fetch error.
    """
    fixture = _make_normal_merge_fixture(tmp_path)
    source_dir = fixture["source_dir"]
    dest_dir = fixture["dest_dir"]
    c_sha = fixture["c_sha"]

    # Record initial destination state before carry
    (Path(dest_dir) / "untracked_dest.txt").write_text("keep me untracked\n")
    initial_dest_status = _git(dest_dir, "status", "--porcelain").stdout

    grant_spy = GrantSpy()
    old_c = _CarryGitContainer(
        source_dir,
        cid="olderr012345",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=True,
    )
    old_c.force_fetch_error = True
    old_c.fetch_error_output = b"fatal: unable to access remote: network unreachable\n"

    new_c = _CarryGitContainer(
        dest_dir,
        cid="newerr012345",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )

    client = MagicMock()
    client.containers.get.side_effect = (
        lambda cid: old_c if cid == old_c.id else new_c
    )

    with (
        patch("sunaba.tools.file._docker", return_value=client),
        patch.object(
            sunaba.proxy_client,
            "authorized_read_grant",
            side_effect=grant_spy.capture,
        ),
    ):
        report = _carry_worktree(client, old_c.id, new_c)

    # Must report error naming fetch failure
    assert report.error is not None, "Carry must fail when origin fetch fails"
    assert (
        "fetch" in report.error.lower()
        or "network unreachable" in report.error.lower()
    ), f"Error message should mention fetch failure, got: {report.error}"
    assert report.applied == []
    assert report.files == []

    # Destination state must be completely untouched
    dest_head = _git(dest_dir, "rev-parse", "HEAD").stdout.strip()
    assert dest_head == c_sha, "Destination HEAD must remain unchanged"

    dest_status_after = _git(dest_dir, "status", "--porcelain").stdout
    assert dest_status_after == initial_dest_status, (
        f"Destination status changed after failed carry:\n{dest_status_after}\nExpected:\n{initial_dest_status}"
    )
    assert (Path(dest_dir) / "untracked_dest.txt").read_text() == "keep me untracked\n"


def test_sandbox_initialize_carry_fetch_failure_returns_structured_error(
    tmp_path: Path,
) -> None:
    """Pins Contract 3: Public initialize response is structured JSON on carry fetch failure.

    - sandbox_initialize returns structured JSON with status: "error", step: "carry_fetch" (or "fetch").
    - Diagnostics (error, hint) are present.
    - Newly created target container is removed and recorded stopped per cleanup contract.
    """
    fixture = _make_normal_merge_fixture(tmp_path)
    source_dir = fixture["source_dir"]
    dest_dir = fixture["dest_dir"]

    old_c = _CarryGitContainer(
        source_dir,
        cid="oldiniterr01",
        repo="owner/repo",
        require_auth_for_fetch=True,
    )
    old_c.force_fetch_error = True
    old_c.fetch_error_output = b"fatal: repository 'origin' does not exist\n"

    new_c = _CarryGitContainer(
        dest_dir,
        cid="newiniterr01",
        repo="owner/repo",
        require_auth_for_fetch=False,
    )

    client = MagicMock()
    client.containers.run.return_value = new_c
    client.containers.get.side_effect = (
        lambda cid: old_c if cid == old_c.id else new_c
    )

    with _patched(client):
        result = sandbox_initialize(
            clone_repo="owner/repo",
            apply_from_container=old_c.id,
            pip_extras=None,
        )

    # Response must be structured JSON (or Error: wrapping JSON)
    try:
        data = _parse_init_error(result)
    except (json.JSONDecodeError, ValueError):
        pytest.fail(
            f"sandbox_initialize must return structured JSON on carry fetch failure, got: {result!r}"
        )
    assert data.get("status") == "error", f"Expected status: error, got: {data}"
    assert data.get("step") in ("carry_fetch", "fetch"), (
        f"Expected step 'carry_fetch' or 'fetch', got: {data.get('step')}"
    )
    assert "error" in data, f"Expected error field in: {data}"
    assert "hint" in data, f"Expected hint field in: {data}"


def test_carry_fetch_fails_closed_and_redacts_token(
    tmp_path: Path,
) -> None:
    """Pins Contract 3: Fail closed on token resolution/grant failure without token leaks.

    - Any token embedded in remote error diagnostics or exception must be redacted.
    - Raw token must never appear in public response, reports, or logs.
    """
    fixture = _make_normal_merge_fixture(tmp_path)
    source_dir = fixture["source_dir"]
    dest_dir = fixture["dest_dir"]
    secret_token = "ghp_SECRET_TOKEN_CARRY_987654321"

    grant_spy = GrantSpy()
    old_c = _CarryGitContainer(
        source_dir,
        cid="oldredact001",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )
    old_c.force_fetch_error = True
    # Stderr contains the raw secret token in URL
    old_c.fetch_error_output = (
        f"fatal: unable to access 'https://x-access-token:{secret_token}@github.com/owner/repo.git/': 403\n".encode()
    )

    new_c = _CarryGitContainer(
        dest_dir,
        cid="newredact001",
        repo="owner/repo",
        grant_spy=grant_spy,
        require_auth_for_fetch=False,
    )

    client = MagicMock()
    client.containers.get.side_effect = (
        lambda cid: old_c if cid == old_c.id else new_c
    )

    with (
        patch("sunaba.tools.file._docker", return_value=client),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=secret_token),
        patch.object(
            sunaba.proxy_client,
            "authorized_read_grant",
            side_effect=grant_spy.capture,
        ),
    ):
        report = _carry_worktree(client, old_c.id, new_c)

    assert report.error is not None, "Carry must fail closed on fetch error"
    assert secret_token not in report.error, (
        f"Security violation: raw token leaked in report.error: {report.error}"
    )
    assert "<redacted>" in report.error, (
        f"Expected <redacted> in report.error, got: {report.error}"
    )
