"""Caller compatibility checks for the extracted authorized fetch."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_client import ProxyAuthError
from sunaba.tools.container import lifecycle
from sunaba.tools.vcs import publishing
from sunaba.tools.vcs.authorized_fetch import FetchOutcome
from sunaba.tools.vcs.publishing import publish
from tests.conftest import _make_client_mock, _make_publish_container
from tests.test_carry_fresh_origin import (
    _CarryGitContainer,
    _make_normal_merge_fixture,
    _parse_init_error,
)
from tests.test_init_apply_from_container import _patched
from tests.test_publish_ops_gitrepo import _git


class _Container:
    labels = {}

    def exec_run(self, *_args, **_kwargs):
        return (0, (b"", b""))


def _carry(monkeypatch, *, token="token-secret", repo="owner/repo", grant=None, exec_run=None):
    events = []
    monkeypatch.setattr(lifecycle.github_api, "_resolve_vcs_token", lambda: events.append("token") or token)
    monkeypatch.setattr(lifecycle, "_resolve_repo_from_container", lambda *_: events.append("repo") or repo)
    monkeypatch.setattr(lifecycle.proxy_client, "proxy_configured", lambda: events.append("proxy") or False)
    if grant is not None:
        monkeypatch.setattr(lifecycle.proxy_client, "authorized_read_grant", grant)
    container = _Container()
    if exec_run is not None:
        container.exec_run = exec_run
    result = lifecycle._fetch_source_origin(container, "/repo path")
    return result, events


def test_carry_token_is_resolved_before_repo_and_environment(monkeypatch):
    result, events = _carry(monkeypatch)
    assert result is None
    assert events == ["token", "repo", "proxy"]


def test_carry_token_resolution_public_report_matches_pre_refactor(monkeypatch):
    monkeypatch.setattr(
        lifecycle.github_api, "_resolve_vcs_token",
        lambda: (_ for _ in ()).throw(RuntimeError("provider detail")),
    )
    report = lifecycle._fetch_source_origin(_Container(), "/repo")
    assert report is not None
    assert asdict(report) == {
        "files": [], "applied": [], "conflicts": [], "untracked": [],
        "error": "Failed to resolve VCS token: token provider error",
        "checkpointed_commits": 0, "fallback_base": False,
        "step": "carry_fetch",
        "hint": "Ensure VCS token is configured in environment or token provider.",
    }


@pytest.mark.parametrize(
    "grant_factory,execute,expected_error,expected_hint",
    [
        (
            lambda repo, token: _raise_context(ProxyAuthError("denied token-secret")),
            None,
            "Proxy authorization failed for fetch: denied <redacted>",
            "Check egress proxy authentication and configuration.",
        ),
        (
            lambda repo, token: _raise_context(RuntimeError("open token-secret")),
            None,
            "Failed to open authorization grant for fetch: open <redacted>",
            "Ensure the remote repository exists and credentials are valid.",
        ),
        (
            lambda repo, token: _normal_context(),
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("exec token-secret")),
            "Failed to execute fetch in container: exec <redacted>",
            "Ensure the source container is running and healthy.",
        ),
        (
            lambda repo, token: _normal_context(),
            lambda *_a, **_k: (7, (b"out token-secret", b"err token-secret")),
            "git fetch origin failed (exit 7): err <redacted>",
            "Ensure the remote is reachable and authorization is valid.",
        ),
    ],
)
def test_carry_failure_responses_keep_caller_wording_and_hints(
    monkeypatch, grant_factory, execute, expected_error, expected_hint
):
    @contextmanager
    def grant(repo, *, token):
        ctx = grant_factory(repo, token)
        with ctx:
            yield
    monkeypatch.setattr(lifecycle.github_api, "_resolve_vcs_token", lambda: "token-secret")
    monkeypatch.setattr(lifecycle, "_resolve_repo_from_container", lambda *_: "owner/repo")
    monkeypatch.setattr(lifecycle.proxy_client, "proxy_configured", lambda: False)
    monkeypatch.setattr(lifecycle.proxy_client, "authorized_read_grant", grant)
    container = _Container()
    if execute is not None:
        container.exec_run = execute
    report = lifecycle._fetch_source_origin(container, "/repo")
    assert report is not None
    assert asdict(report) == {
        "files": [], "applied": [], "conflicts": [], "untracked": [],
        "error": expected_error,
        "checkpointed_commits": 0, "fallback_base": False,
        "step": "carry_fetch", "hint": expected_hint,
    }


@contextmanager
def _normal_context():
    yield


@contextmanager
def _raise_context(exc):
    raise exc
    yield


def test_publish_nonzero_fetch_response_is_complete_and_unchanged(monkeypatch):
    monkeypatch.setenv("SUNABA_ENABLE_EGRESS_PROXY", "false")
    container = _make_publish_container([
        (0, b"", b""),
        (7, b"stdout secret-token", b"fatal secret-token"),
    ])
    with (
        patch("sunaba.tools.vcs.publishing._docker", return_value=_make_client_mock(container)),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="secret-token"),
    ):
        response = json.loads(publish(
            container_id="abc123def456", repo="owner/repo", branch="fix/x",
            message="Fix", files=["declared.txt"],
        ))
    assert response == {
        "status": "error",
        "step": "fetch",
        "error": "git fetch origin failed (exit 7): fatal <redacted>",
        "hint": "Ensure the remote is reachable and authorization is valid.",
    }


def test_publish_token_resolution_response_is_complete_and_fail_closed(monkeypatch):
    monkeypatch.setenv("SUNABA_ENABLE_EGRESS_PROXY", "false")
    container = _make_publish_container([(0, b"", b"")])
    with (
        patch("sunaba.tools.vcs.publishing._docker", return_value=_make_client_mock(container)),
        patch(
            "sunaba.tools.vcs.publishing._resolve_vcs_token",
            side_effect=RuntimeError("private provider detail"),
        ),
    ):
        response = json.loads(publish(
            container_id="abc123def456", repo="owner/repo", branch="fix/x",
            message="Fix", files=["declared.txt"],
        ))
    assert response == {
        "status": "error",
        "step": "token_resolution",
        "error": "Failed to resolve VCS token: token provider error",
    }



def test_carry_public_initialize_response_json_is_complete(tmp_path):
    fixture = _make_normal_merge_fixture(tmp_path)
    old_c = _CarryGitContainer(
        fixture["source_dir"], cid="compatold001", repo="owner/repo",
        require_auth_for_fetch=True,
    )
    old_c.force_fetch_error = True
    old_c.fetch_error_output = b"fatal: fetch denied\n"
    new_c = _CarryGitContainer(
        fixture["dest_dir"], cid="compatnew001", repo="owner/repo",
        require_auth_for_fetch=False,
    )
    client = MagicMock()
    client.containers.run.return_value = new_c
    client.containers.get.side_effect = lambda cid: old_c if cid == old_c.id else new_c
    with _patched(client):
        result = lifecycle.sandbox_initialize(
            clone_repo="owner/repo", apply_from_container=old_c.id, pip_extras=None
        )
    data = _parse_init_error(result)
    assert data == {
        "status": "error",
        "step": "carry_fetch",
        "error": "git fetch origin failed (exit 128): fatal: fetch denied",
        "hint": "No VCS token is available on the host. Set GITHUB_TOKEN or GH_TOKEN in the server environment.",
    }


@pytest.mark.parametrize(
    "failure,expected",
    [
        (
            "token",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "Failed to resolve VCS token: token provider error",
                "hint": "Ensure VCS token is configured in environment or token provider.",
            },
        ),
        (
            "grant_open",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "Failed to open authorization grant for fetch: open <redacted>",
                "hint": "Ensure the remote repository exists and credentials are valid.",
            },
        ),
        (
            "proxy_open",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "Proxy authorization failed for fetch: denied <redacted>",
                "hint": "Check egress proxy authentication and configuration.",
            },
        ),
        (
            "execution",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "Failed to execute fetch in container: exec <redacted>",
                "hint": "Ensure the source container is running and healthy.",
            },
        ),
        (
            "grant_close",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "Failed to close authorization grant for fetch: close <redacted>",
                "hint": "Check egress proxy connection and authorization grant cleanup.",
            },
        ),
        (
            "proxy_close",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "Proxy authorization failed for fetch: denied <redacted>",
                "hint": "Check egress proxy authentication and configuration.",
            },
        ),
        (
            "exit",
            {
                "status": "error",
                "step": "carry_fetch",
                "error": "git fetch origin failed (exit 128): fatal <redacted>",
                "hint": "Ensure the remote is reachable and authorization is valid.",
            },
        ),
    ],
)
def test_public_initialize_fetch_failure_matrix_is_complete_and_fail_closed(
    tmp_path, monkeypatch, failure, expected
):
    fixture = _make_normal_merge_fixture(tmp_path)
    old_c = _CarryGitContainer(
        fixture["source_dir"], cid="matrixold001", repo="owner/repo",
        require_auth_for_fetch=True,
    )
    new_c = _CarryGitContainer(
        fixture["dest_dir"], cid="matrixnew001", repo="owner/repo",
        require_auth_for_fetch=False,
    )
    if failure == "exit":
        old_c.force_fetch_error = True
        old_c.fetch_error_output = b"fatal token-secret"
    client = MagicMock()
    client.containers.run.return_value = new_c
    client.containers.get.side_effect = lambda cid: old_c if cid == old_c.id else new_c
    monkeypatch.setattr(
        lifecycle.github_api,
        "_resolve_vcs_token",
        lambda: (_ for _ in ()).throw(RuntimeError("private provider"))
        if failure == "token"
        else "token-secret",
    )
    monkeypatch.setattr(lifecycle, "_resolve_repo_from_container", lambda *_: "owner/repo")
    monkeypatch.setattr(lifecycle.proxy_client, "proxy_configured", lambda: False)

    @contextmanager
    def grant(_repo, *, token):
        if failure in {"grant_open", "proxy_open"}:
            exc = ProxyAuthError("denied token-secret") if failure == "proxy_open" else RuntimeError("open token-secret")
            raise exc
        try:
            yield
        finally:
            if failure in {"grant_close", "proxy_close"}:
                exc = ProxyAuthError("denied token-secret") if failure == "proxy_close" else RuntimeError("close token-secret")
                raise exc

    monkeypatch.setattr(lifecycle.proxy_client, "authorized_read_grant", grant)
    if failure == "execution":
        original_exec_run = old_c.exec_run

        def fail_fetch(cmd=None, **kwargs):
            command = " ".join(cmd or [])
            if "fetch origin" in command:
                raise RuntimeError("exec token-secret")
            return original_exec_run(cmd, **kwargs)

        old_c.exec_run = fail_fetch

    before = (
        _git(fixture["source_dir"], "rev-parse", "HEAD").stdout.strip(),
        _git(fixture["source_dir"], "status", "--porcelain").stdout,
    )
    with _patched(client):
        result = lifecycle.sandbox_initialize(
            clone_repo="owner/repo", apply_from_container=old_c.id, pip_extras=None
        )
    assert _parse_init_error(result) == expected
    assert client.containers.run.call_count == 0
    after = (
        _git(fixture["source_dir"], "rev-parse", "HEAD").stdout.strip(),
        _git(fixture["source_dir"], "status", "--porcelain").stdout,
    )
    assert after == before
    assert not any("merge-base" in cmd or "cherry-pick" in cmd for cmd in old_c.fetch_commands)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (
            "grant_open",
            {
                "status": "error",
                "step": "fetch",
                "error": "Failed to open authorization grant for fetch: open <redacted>",
            },
        ),
        (
            "proxy_open",
            {
                "status": "error",
                "step": "proxy_auth",
                "error": "denied <redacted>",
            },
        ),
        (
            "execution",
            {
                "status": "error",
                "step": "fetch",
                "error": "Failed to execute fetch: exec <redacted>",
            },
        ),
        (
            "grant_close",
            {
                "status": "error",
                "step": "fetch",
                "error": "Failed to close authorization grant for fetch: close <redacted>",
            },
        ),
        (
            "proxy_close",
            {
                "status": "error",
                "step": "proxy_auth",
                "error": "denied <redacted>",
            },
        ),
        (
            "exit",
            {
                "status": "error",
                "step": "fetch",
                "error": "git fetch origin failed (exit 7): err <redacted>",
                "hint": "Ensure the remote is reachable and authorization is valid.",
            },
        ),
    ],
)
def test_publish_public_failure_matrix_is_complete_and_fail_closed(
    monkeypatch, failure, expected
):
    monkeypatch.setenv("SUNABA_ENABLE_EGRESS_PROXY", "false")
    fetch_result = {
        "exit": (7, b"out token-secret", b"err token-secret"),
    }.get(failure, (0, b"", b""))
    container = _make_publish_container([(0, b"", b""), fetch_result])
    original_exec_run = container.exec_run
    commands = []

    def track_exec_run(*args, **kwargs):
        command = " ".join(str(part) for part in (args[0] if args else kwargs.get("cmd", [])))
        commands.append(command)
        if failure == "execution" and "fetch origin" in command:
            raise RuntimeError("exec token-secret")
        return original_exec_run(*args, **kwargs)

    container.exec_run = track_exec_run

    @contextmanager
    def grant(_repo, *, token):
        if failure in {"grant_open", "proxy_open"}:
            exc = ProxyAuthError("denied token-secret") if failure == "proxy_open" else RuntimeError("open token-secret")
            raise exc
        try:
            yield
        finally:
            if failure in {"grant_close", "proxy_close"}:
                exc = ProxyAuthError("denied token-secret") if failure == "proxy_close" else RuntimeError("close token-secret")
                raise exc

    with (
        patch("sunaba.tools.vcs.publishing._docker", return_value=_make_client_mock(container)),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="token-secret"),
        patch("sunaba.tools.vcs.publishing.authorized_read_grant", grant),
    ):
        response = json.loads(publish(
            container_id="abc123def456", repo="owner/repo", branch="fix/x",
            message="Fix", files=["declared.txt"],
        ))
    assert response == expected
    assert not any("git add " in command or "git commit " in command for command in commands)

@pytest.mark.parametrize(
    "close_error,expected",
    [
        (
            RuntimeError("close token-secret"),
            {
                "status": "error",
                "step": "fetch",
                "error": "Failed to close authorization grant for fetch: close <redacted>",
            },
        ),
        (
            ProxyAuthError("denied token-secret"),
            {
                "status": "error",
                "step": "proxy_auth",
                "error": "denied <redacted>",
            },
        ),
    ],
)
def test_publish_execution_exception_and_grant_exit_failure_preserve_baseline(
    monkeypatch, close_error, expected
):
    monkeypatch.setenv("SUNABA_ENABLE_EGRESS_PROXY", "false")
    container = _make_publish_container([(0, b"", b""), (0, b"", b"")])
    original_exec_run = container.exec_run
    commands = []

    def fail_fetch(*args, **kwargs):
        command = " ".join(
            str(part)
            for part in (args[0] if args else kwargs.get("cmd", []))
        )
        commands.append(command)
        if "fetch origin" in command:
            raise RuntimeError("exec token-secret")
        return original_exec_run(*args, **kwargs)

    container.exec_run = fail_fetch

    @contextmanager
    def grant(_repo, *, token):
        try:
            yield
        finally:
            raise close_error

    with (
        patch("sunaba.tools.vcs.publishing._docker", return_value=_make_client_mock(container)),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="token-secret"),
        patch("sunaba.tools.vcs.publishing.authorized_read_grant", grant),
    ):
        response = json.loads(publish(
            container_id="abc123def456", repo="owner/repo", branch="fix/x",
            message="Fix", files=["declared.txt"],
        ))

    assert response == expected
    assert len([command for command in commands if "fetch origin" in command]) == 1
    assert not any(
        "git add " in command or "git commit " in command
        for command in commands
    )
    assert not any("git push" in command or "gh pr create" in command for command in commands)


def test_public_initialize_execution_exception_and_grant_exit_failure_are_fail_closed(
    tmp_path, monkeypatch
):
    close_error = RuntimeError("close token-secret")
    expected_error = "Failed to close authorization grant for fetch: close <redacted>"
    fixture = _make_normal_merge_fixture(tmp_path)
    old_c = _CarryGitContainer(
        fixture["source_dir"], cid="exitfailold001", repo="owner/repo",
        require_auth_for_fetch=True,
    )
    new_c = _CarryGitContainer(
        fixture["dest_dir"], cid="exitfailnew001", repo="owner/repo",
        require_auth_for_fetch=False,
    )
    client = MagicMock()
    client.containers.run.return_value = new_c
    client.containers.get.side_effect = lambda cid: old_c if cid == old_c.id else new_c
    monkeypatch.setattr(lifecycle.github_api, "_resolve_vcs_token", lambda: "token-secret")
    monkeypatch.setattr(lifecycle, "_resolve_repo_from_container", lambda *_: "owner/repo")
    monkeypatch.setattr(lifecycle.proxy_client, "proxy_configured", lambda: False)

    @contextmanager
    def grant(_repo, *, token):
        try:
            yield
        finally:
            raise close_error

    monkeypatch.setattr(lifecycle.proxy_client, "authorized_read_grant", grant)
    original_exec_run = old_c.exec_run

    def fail_fetch(cmd=None, **kwargs):
        command = " ".join(cmd or [])
        if "fetch origin" in command:
            raise RuntimeError("exec token-secret")
        return original_exec_run(cmd, **kwargs)

    old_c.exec_run = fail_fetch
    before = (
        _git(fixture["source_dir"], "rev-parse", "HEAD").stdout.strip(),
        _git(fixture["source_dir"], "status", "--porcelain").stdout,
    )

    with _patched(client):
        result = lifecycle.sandbox_initialize(
            clone_repo="owner/repo", apply_from_container=old_c.id, pip_extras=None
        )

    assert _parse_init_error(result) == {
        "status": "error",
        "step": "carry_fetch",
        "error": expected_error,
        "hint": "Check egress proxy connection and authorization grant cleanup.",
    }
    assert client.containers.run.call_count == 0
    after = (
        _git(fixture["source_dir"], "rev-parse", "HEAD").stdout.strip(),
        _git(fixture["source_dir"], "status", "--porcelain").stdout,
    )
    assert after == before
    assert not any(
        "merge-base" in command or "cherry-pick" in command
        for command in old_c.fetch_commands
    )



def test_publish_unknown_shared_fetch_failure_is_complete_and_fail_closed(monkeypatch):
    monkeypatch.setenv("SUNABA_ENABLE_EGRESS_PROXY", "false")
    container = _make_publish_container([(0, b"", b"")])
    commands = []
    original_exec_run = container.exec_run

    def track_exec_run(*args, **kwargs):
        command = " ".join(
            str(part) for part in (args[0] if args else kwargs.get("cmd", []))
        )
        commands.append(command)
        return original_exec_run(*args, **kwargs)

    container.exec_run = track_exec_run
    unknown_outcome = FetchOutcome(
        failure=cast(Any, "unrecognized private failure token"), error="token-secret raw detail",
        token="token-secret",
    )
    with (
        patch.object(publishing, "_docker", return_value=_make_client_mock(container)),
        patch.object(publishing, "_resolve_vcs_token", return_value="token-secret"),
        patch.object(publishing, "authorized_origin_fetch", return_value=unknown_outcome),
    ):
        response = json.loads(publish(
            container_id="abc123def456", repo="owner/repo", branch="fix/x",
            message="Fix", files=["declared.txt"],
        ))

    assert response == {
        "status": "error",
        "step": "fetch",
        "error": "Authorized fetch failed with an unrecognized failure.",
    }
    serialized = json.dumps(response)
    assert "unrecognized private failure token" not in serialized
    assert "token-secret" not in serialized
    assert "raw detail" not in serialized
    assert not any(
        "git add " in command
        or "git commit " in command
        or "git push" in command
        or "gh pr create" in command
        or "merge-base" in command
        or "git apply" in command
        or "cherry-pick" in command
        for command in commands
    )


def test_public_initialize_unknown_shared_fetch_failure_stops_before_allocation(tmp_path, monkeypatch):
    fixture = _make_normal_merge_fixture(tmp_path)
    old_c = _CarryGitContainer(
        fixture["source_dir"], cid="unknownold001", repo="owner/repo",
        require_auth_for_fetch=True,
    )
    new_c = _CarryGitContainer(
        fixture["dest_dir"], cid="unknownnew001", repo="owner/repo",
        require_auth_for_fetch=False,
    )
    client = MagicMock()
    client.containers.run.return_value = new_c
    client.containers.get.side_effect = lambda cid: old_c if cid == old_c.id else new_c
    monkeypatch.setattr(lifecycle.github_api, "_resolve_vcs_token", lambda: "token-secret")
    monkeypatch.setattr(lifecycle, "_resolve_repo_from_container", lambda *_: "owner/repo")
    monkeypatch.setattr(lifecycle.proxy_client, "proxy_configured", lambda: False)
    monkeypatch.setattr(
        lifecycle,
        "authorized_origin_fetch",
        lambda **_kwargs: FetchOutcome(
            failure=cast(Any, "unrecognized private failure token"), error="token-secret raw detail",
            token="token-secret",
        ),
    )

    before = (
        _git(fixture["source_dir"], "rev-parse", "HEAD").stdout.strip(),
        _git(fixture["source_dir"], "status", "--porcelain").stdout,
    )
    with _patched(client):
        result = lifecycle.sandbox_initialize(
            clone_repo="owner/repo", apply_from_container=old_c.id, pip_extras=None
        )

    response = _parse_init_error(result)
    assert response == {
        "status": "error",
        "step": "carry_fetch",
        "error": "Authorized fetch failed with an unrecognized failure.",
        "hint": "Ensure the source container is running and healthy.",
    }
    serialized = json.dumps(response)
    assert "unrecognized private failure token" not in serialized
    assert "token-secret" not in serialized
    assert "raw detail" not in serialized
    assert client.containers.run.call_count == 0
    after = (
        _git(fixture["source_dir"], "rev-parse", "HEAD").stdout.strip(),
        _git(fixture["source_dir"], "status", "--porcelain").stdout,
    )
    assert after == before
    assert not any(
        "merge-base" in command or "cherry-pick" in command or "git apply" in command
        for command in old_c.fetch_commands
    )
