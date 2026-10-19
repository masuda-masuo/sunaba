"""Authorization and failure-contract tests for diff_in_container's origin fetch."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_client import ProxyAuthError
from sunaba.tools.diff import diff_in_container
from sunaba.tools.vcs.authorized_fetch import FetchOutcome

TOKEN = "synthetic-diff-token"
MERGE_BASE = "a" * 40


def _container(*, base_is_local: bool, fetch_result=(0, (b"", b"")), retry_succeeds=True, on_fetch=None):
    container = MagicMock()
    calls = []
    merge_attempts = 0

    def exec_run(cmd, **kwargs):
        nonlocal merge_attempts
        command = " ".join(str(part) for part in cmd)
        calls.append((command, kwargs))
        if "merge-base" in command:
            merge_attempts += 1
            if base_is_local or (merge_attempts > 1 and retry_succeeds):
                return 0, (f"{MERGE_BASE}\n".encode(), b"")
            return 128, (b"", b"fatal: Not a valid object name")
        if "fetch" in command:
            if on_fetch is not None:
                on_fetch(command)
            return fetch_result
        if "--numstat" in command:
            return 0, (b"1\t0\tchanged.py\n", b"")
        if "--name-status" in command:
            return 0, (b"M\tchanged.py\n", b"")
        return 0, (b"", b"")

    container.exec_run.side_effect = exec_run
    return container, calls


def _docker_patch(container):
    return patch(
        "sunaba.tools.diff._docker",
        return_value=MagicMock(containers=MagicMock(get=MagicMock(return_value=container))),
    )


def test_local_merge_base_skips_token_resolution_grant_and_fetch():
    container, calls = _container(base_is_local=True)
    token = MagicMock(return_value=TOKEN)
    grant = MagicMock()

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", token),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", return_value="owner/repo"),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/main"))

    assert "fetch_happened" not in response
    token.assert_not_called()
    grant.assert_not_called()
    assert not any("fetch" in command for command, _ in calls)


def test_missing_base_uses_one_authorized_all_origin_fetch_and_retries_after_success():
    events = []
    grant_active = False

    def on_fetch(command):
        events.append("fetch" if grant_active else "fetch-outside-grant")
        assert "fetch origin" in command
        assert "heads/" not in command
        assert "pull" not in command

    container, calls = _container(base_is_local=False, on_fetch=on_fetch)

    @contextmanager
    def grant(repo, *, token):
        nonlocal grant_active
        assert repo == "owner/repo"
        assert token == TOKEN
        grant_active = True
        events.append("grant-open")
        try:
            yield
        finally:
            grant_active = False
            events.append("grant-close")

    resolve_token = MagicMock(side_effect=lambda: events.append("token") or TOKEN)

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", resolve_token),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", return_value="owner/repo"),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/topic"))

    resolve_token.assert_called_once()
    assert response["fetch_happened"] is True
    assert response["base"] == "origin/topic"
    assert events == ["token", "grant-open", "fetch", "grant-close"]
    assert len([command for command, _ in calls if "fetch" in command]) == 1
    assert len([command for command, _ in calls if "merge-base" in command]) == 2


@pytest.mark.parametrize(
    "failure,fetch_result,retry_succeeds,category,expected_prefix",
    [
        ("proxy_auth", (0, (b"", b"")), False, ("proxy", "auth"), "proxy read authorization failed"),
        ("grant_open", (0, (b"", b"")), False, ("grant", "authorization"), "could not open proxy read authorization grant"),
        ("transport", (7, (b"", f"transport failed for {TOKEN}".encode())), False, ("fetch", "transport", "exit"), "origin fetch failed"),
        ("fetched_ref_missing", (0, (b"", b"")), False, ("missing", "ref"), "no merge base"),
        ("grant_close", (0, (b"", b"")), False, ("grant", "closing"), "proxy read authorization grant failed while closing"),
    ],
)
def test_fetch_failure_categories_preserve_base_and_redact_token(
    failure, fetch_result, retry_succeeds, category, expected_prefix, caplog
):
    container, calls = _container(
        base_is_local=False,
        fetch_result=fetch_result,
        retry_succeeds=retry_succeeds,
    )

    @contextmanager
    def grant(_repo, *, token):
        assert token == TOKEN
        if failure == "proxy_auth":
            raise ProxyAuthError(f"proxy rejected {TOKEN}")
        if failure == "grant_open":
            raise RuntimeError(f"grant could not start for {TOKEN}")
        yield
        if failure == "grant_close":
            raise RuntimeError(f"grant cleanup failed for {TOKEN}")

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=TOKEN),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", return_value="owner/repo"),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/private"))

    assert response.get("status") == "error"
    assert response["step"] == "merge_base"
    assert "origin/private" in json.dumps(response)
    error = response["error"].lower()
    assert error.startswith(expected_prefix)
    assert any(term in error for term in category)
    assert TOKEN not in json.dumps(response)
    assert TOKEN not in caplog.text
    if failure == "fetched_ref_missing":
        assert "ref" in error and any(term in error for term in ("not exist", "missing", "not found"))
        assert len([command for command, _ in calls if "merge-base" in command]) == 2
    else:
        assert "missing" not in error
        assert len([command for command, _ in calls if "merge-base" in command]) == 1


@pytest.mark.parametrize(
    "helper_result",
    [
        FetchOutcome(failure="unrecognized", error=TOKEN, token=TOKEN),
        RuntimeError(TOKEN),
    ],
)
def test_unknown_or_raised_helper_outcome_fails_closed_and_redacts(helper_result, caplog):
    container, _calls = _container(base_is_local=False)
    helper = MagicMock()
    if isinstance(helper_result, Exception):
        helper.side_effect = helper_result
    else:
        helper.return_value = helper_result
    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.diff.authorized_origin_fetch", helper),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/private"))

    assert response["status"] == "error"
    assert response["step"] == "merge_base"
    assert "origin/private" in response["error"]
    assert "missing" not in response["error"].lower()
    assert TOKEN not in json.dumps(response) + caplog.text
    helper.assert_called_once()


@pytest.mark.parametrize("proxied", [True, False])
def test_real_helper_uses_proxy_or_native_environment_without_token_leaks(proxied, caplog):
    container, calls = _container(base_is_local=False)
    grant_calls = []

    @contextmanager
    def grant(repo, *, token):
        grant_calls.append((repo, token))
        yield

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=TOKEN),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", return_value="owner/repo"),
        patch("sunaba.proxy_client.proxy_configured", return_value=proxied),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/topic"))

    fetch_command, fetch_kwargs = next(
        (command, kwargs) for command, kwargs in calls if "fetch" in command
    )
    assert grant_calls == [("owner/repo", TOKEN)]
    if proxied:
        assert fetch_kwargs["environment"] is None
    else:
        assert fetch_kwargs["environment"]["GITHUB_TOKEN"] == TOKEN
    assert TOKEN not in fetch_command
    assert TOKEN not in json.dumps(response) + caplog.text


def test_git_authentication_rejection_keeps_detail_without_missing_ref_retry(caplog):
    container, calls = _container(
        base_is_local=False,
        fetch_result=(128, (b"", f"fatal: Authentication failed for {TOKEN}".encode())),
    )

    @contextmanager
    def grant(_repo, *, token):
        assert token == TOKEN
        yield

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=TOKEN),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", return_value="owner/repo"),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/private"))

    assert response["status"] == "error"
    assert response["step"] == "merge_base"
    assert "authentication failed" in response["error"].lower()
    assert "missing" not in response["error"].lower()
    assert len([command for command, _ in calls if "merge-base" in command]) == 1
    assert TOKEN not in json.dumps(response) + caplog.text


def test_execution_exception_is_reported_as_fetch_failure_and_redacted(caplog):
    container, calls = _container(base_is_local=False)
    original_exec = container.exec_run.side_effect

    def fail_fetch(cmd, **kwargs):
        command = " ".join(str(part) for part in cmd)
        if "fetch" in command:
            calls.append((command, kwargs))
            raise RuntimeError(f"process launch stalled for {TOKEN}")
        return original_exec(cmd, **kwargs)

    container.exec_run.side_effect = fail_fetch

    @contextmanager
    def grant(_repo, *, token):
        assert token == TOKEN
        yield

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=TOKEN),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", return_value="owner/repo"),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/private"))

    assert response["status"] == "error"
    assert response["step"] == "merge_base"
    assert response["error"].startswith("Origin fetch could not execute or capture its result.")
    assert "missing" not in response["error"].lower()
    assert "ref" not in response["error"].lower()
    assert len([command for command, _ in calls if "merge-base" in command]) == 1
    assert TOKEN not in json.dumps(response) + caplog.text


def test_token_resolution_exception_stops_authorized_diff_before_repo_or_fetch(caplog):
    container, calls = _container(base_is_local=False)
    resolver = MagicMock(side_effect=RuntimeError(f"credential provider leaked {TOKEN}"))
    repo_resolver = MagicMock(return_value="owner/repo")
    grant = MagicMock()

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", resolver),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", repo_resolver),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/private"))

    assert response["status"] == "error"
    assert response["step"] == "merge_base"
    assert response["error"].startswith("Could not resolve host VCS credentials for fetch.")
    assert "missing" not in response["error"].lower()
    assert "ref" not in response["error"].lower()
    resolver.assert_called_once()
    repo_resolver.assert_not_called()
    grant.assert_not_called()
    assert len([command for command, _ in calls if "fetch" in command]) == 0
    assert len([command for command, _ in calls if "merge-base" in command]) == 1
    assert TOKEN not in json.dumps(response) + caplog.text


def test_repository_resolution_exception_stops_authorized_diff_before_grant_or_fetch(caplog):
    container, calls = _container(base_is_local=False)
    resolver = MagicMock(return_value=TOKEN)
    repo_resolver = MagicMock(side_effect=RuntimeError(f"remote descriptor leaked {TOKEN}"))
    grant = MagicMock()

    with (
        _docker_patch(container),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", resolver),
        patch("sunaba.tools.container.lifecycle._resolve_repo_from_container", repo_resolver),
        patch("sunaba.proxy_client.proxy_configured", return_value=True),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
    ):
        response = json.loads(diff_in_container("abc123def456", base="origin/private"))

    assert response["status"] == "error"
    assert response["step"] == "merge_base"
    assert response["error"].startswith("Could not resolve repository for authorized fetch.")
    assert "missing" not in response["error"].lower()
    assert "ref" not in response["error"].lower()
    resolver.assert_called_once()
    repo_resolver.assert_called_once()
    grant.assert_not_called()
    assert len([command for command, _ in calls if "fetch" in command]) == 0
    assert len([command for command, _ in calls if "merge-base" in command]) == 1
    assert TOKEN not in json.dumps(response) + caplog.text
