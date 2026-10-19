"""Native diff fetches must not cross the host credential boundary."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_client import ProxyControlConfig
from sunaba.tools.diff import diff_in_container

TOKEN = "synthetic-native-diff-token"
MERGE_BASE = "a" * 40


def _container_with_missing_base():
    container = MagicMock()
    calls = []
    merge_attempts = 0

    def exec_run(cmd, **kwargs):
        nonlocal merge_attempts
        command = " ".join(str(part) for part in cmd)
        calls.append((command, kwargs))
        if "merge-base" in command:
            merge_attempts += 1
            if merge_attempts == 1:
                return 128, (b"", b"fatal: Not a valid object name")
            return 0, (f"{MERGE_BASE}\n".encode(), b"")
        if "--numstat" in command:
            return 0, (b"1\t0\tchanged.py\n", b"")
        if "--name-status" in command:
            return 0, (b"M\tchanged.py\n", b"")
        return 0, (b"", b"")

    container.exec_run.side_effect = exec_run
    return container, calls


@pytest.mark.parametrize(
    "resolver",
    [
        pytest.param(MagicMock(return_value=TOKEN), id="credential-available"),
        pytest.param(
            MagicMock(side_effect=RuntimeError(f"provider saw {TOKEN}")),
            id="credential-provider-fails",
        ),
    ],
)
def test_native_missing_base_fetch_is_token_free_and_retries(resolver, caplog):
    container, calls = _container_with_missing_base()
    repo_resolver = MagicMock(
        side_effect=AssertionError("native fetch must not resolve a GitHub repository")
    )
    docker = MagicMock(
        containers=MagicMock(get=MagicMock(return_value=container))
    )
    urlopen = MagicMock()

    with (
        patch("sunaba.tools.diff._docker", return_value=docker),
        patch("sunaba.tools.diff.resolve_git_root", return_value="/repo"),
        patch("sunaba.tools.diff.record_tool_use"),
        patch("sunaba.tools.github_api._resolve_vcs_token", resolver),
        patch(
            "sunaba.tools.container.lifecycle._resolve_repo_from_container",
            repo_resolver,
        ),
        patch("sunaba.proxy_client.proxy_configured", return_value=False),
        patch.object(ProxyControlConfig, "from_env", return_value=None),
        patch("sunaba.proxy_client.urllib.request.urlopen", urlopen),
    ):
        response_text = diff_in_container(
            "abc123def456",
            base="origin/topic",
        )

    response = json.loads(response_text)
    assert response.get("status") != "error"
    assert response["fetch_happened"] is True
    fetch_command, fetch_kwargs = next(
        (command, kwargs) for command, kwargs in calls if "fetch" in command
    )

    assert response["base"] == "origin/topic"
    assert fetch_kwargs["environment"] is None
    assert TOKEN not in fetch_command
    assert TOKEN not in response_text + caplog.text
    assert len([command for command, _ in calls if "merge-base" in command]) == 2
    resolver.assert_not_called()
    repo_resolver.assert_not_called()
    urlopen.assert_not_called()
