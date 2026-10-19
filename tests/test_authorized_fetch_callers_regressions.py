"""Caller compatibility regressions for merge_base's authorized fetch adapter."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from sunaba.tools.vcs.merge_base import merge_base

TOKEN = "synthetic-merge-token"


class Container:
    def __init__(self, *, env=None):
        self.attrs = {"Config": {"Env": env or []}}
        self.calls = []

    def exec_run(self, cmd, **kwargs):
        command = " ".join(str(part) for part in cmd)
        self.calls.append((command, kwargs))
        if "fetch origin" in command:
            return 0, (b"", b"")
        if "git merge origin/" in command:
            return 0, (b"Already up to date.\n", b"")
        if "git diff --name-only --diff-filter=U" in command:
            return 0, (b"", b"")
        return 0, (b"", b"")


@contextmanager
def _patch(container, *, resolver, proxied, grant):
    with (
        patch("sunaba.tools.github_api._resolve_vcs_token", side_effect=resolver),
        patch("sunaba.proxy_client.ProxyControlConfig.from_env", return_value=object() if proxied else None),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
        patch("sunaba.journal.record_boundary_crossing"),
    ):
        yield


def _call(container):
    return json.loads(
        merge_base(
            "container-123",
            base_branch="main",
            repo="owner/repo",
            working_dir="/repo",
            _container=container,
        )
    )


def test_token_provider_exception_uses_token_free_proxy_grant_and_merges():
    container = Container()
    grant_calls = []

    @contextmanager
    def grant(repo, *, token):
        grant_calls.append((repo, token))
        yield

    def unavailable():
        raise RuntimeError("credential helper unavailable")

    with _patch(container, resolver=unavailable, proxied=True, grant=grant):
        response = _call(container)

    assert response["status"] == "merged"
    assert grant_calls == [("owner/repo", None)]
    assert any("fetch origin main" in command for command, _ in container.calls)
    assert any("git merge origin/main --no-edit" in command for command, _ in container.calls)


def test_native_public_merge_continues_when_token_provider_raises_without_grant():
    container = Container()
    grant = patch("sunaba.proxy_client.authorized_read_grant")

    def unavailable():
        raise RuntimeError("credential helper unavailable")

    with grant as grant_mock, _patch(container, resolver=unavailable, proxied=False, grant=grant_mock):
        response = _call(container)

    assert response["status"] == "merged"
    grant_mock.assert_not_called()
    fetch_kwargs = next(kwargs for command, kwargs in container.calls if "fetch origin" in command)
    assert fetch_kwargs["environment"] is None


def test_native_merge_does_not_resolve_or_inject_host_token(caplog):
    container = Container()
    resolver = MagicMock(return_value=TOKEN)
    grant = patch("sunaba.proxy_client.authorized_read_grant")
    journal = patch("sunaba.journal.record_boundary_crossing")

    with (
        grant as grant_mock,
        journal as journal_mock,
        _patch(container, resolver=resolver, proxied=False, grant=grant_mock),
    ):
        response = _call(container)

    assert response["status"] == "merged"
    resolver.assert_not_called()
    grant_mock.assert_not_called()
    fetch_command, fetch_kwargs = next(
        (command, kwargs) for command, kwargs in container.calls if "fetch origin" in command
    )
    assert "fetch origin main" in fetch_command
    assert fetch_kwargs["environment"] is None
    assert any("git merge origin/main --no-edit" in command for command, _ in container.calls)
    observable = json.dumps(response) + caplog.text + repr(journal_mock.call_args_list)
    commands = " ".join(command for command, _ in container.calls)
    assert TOKEN not in commands + observable


def test_token_bearing_proxy_container_skips_proxy_grant_and_fetches():
    container = Container(env=["GITHUB_TOKEN=container-token"])
    resolver = MagicMock(return_value=TOKEN)
    grant = patch("sunaba.proxy_client.authorized_read_grant")

    with grant as grant_mock, _patch(
        container, resolver=resolver, proxied=True, grant=grant_mock,
    ):
        response = _call(container)

    assert response["status"] == "merged"
    resolver.assert_not_called()
    grant_mock.assert_not_called()
    fetch_command, fetch_kwargs = next(
        (command, kwargs) for command, kwargs in container.calls if "fetch origin" in command
    )
    assert "fetch origin main" in fetch_command
    assert fetch_kwargs["environment"] is None
    assert TOKEN not in fetch_command + json.dumps(response)


def test_merge_base_fetches_only_shell_quoted_resolved_base():
    base = "release; touch /tmp/should-not-run"
    container = Container()
    grant = patch("sunaba.proxy_client.authorized_read_grant")

    with (
        grant as grant_mock,
        _patch(container, resolver=lambda: None, proxied=False, grant=grant_mock),
    ):
        response = json.loads(
            merge_base(
                "container-123", base_branch=base, repo="owner/repo",
                working_dir="/repo", _container=container,
            )
        )

    assert response["status"] == "merged"
    fetch_command = next(command for command, _ in container.calls if "fetch origin" in command)
    assert "fetch origin 'release; touch /tmp/should-not-run'" in fetch_command
    assert "heads/" not in fetch_command
    assert "touch /tmp/should-not-run" not in fetch_command.replace(
        "'release; touch /tmp/should-not-run'", ""
    )
