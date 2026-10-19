"""Merge-base response, fetch ordering, and authorization-boundary characterization."""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from sunaba.proxy_client import ProxyAuthError
from sunaba.tools.vcs.merge_base import merge_base

TOKEN = "synthetic-merge-token"


class RecordingContainer:
    def __init__(self, *, merge_result=0, fetch_result=0, fetch_output=b"transport failed"):
        self.merge_result = merge_result
        self.fetch_result = fetch_result
        self.fetch_output = fetch_output
        self.calls = []
        self.attrs = {"Config": {"Env": []}}

    def exec_run(self, cmd, **kwargs):
        command = " ".join(str(part) for part in cmd)
        self.calls.append((command, kwargs))
        if "fetch origin" in command:
            return self.fetch_result, (self.fetch_output if self.fetch_result else b"", b"")
        if "git merge origin/main --no-edit" in command:
            output = (
                b"CONFLICT (content): Merge conflict in src/a.py\n"
                if self.merge_result == 1
                else b"Already up to date.\n"
            )
            return self.merge_result, (output, b"")
        if "git diff --name-only --diff-filter=U" in command:
            return 0, (b"src/a.py\n", b"")
        return 0, (b"https://github.com/owner/repo.git\n", b"")


@contextmanager
def _patched(container, grant, journal):
    with (
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=TOKEN),
        patch("sunaba.proxy_client.authorized_read_grant", grant),
        patch("sunaba.proxy_client.ProxyControlConfig.from_env", return_value=object()),
        patch("sunaba.journal.record_boundary_crossing", journal),
    ):
        yield


def _call(container):
    return json.loads(
        merge_base(
            "test123abc",
            base_branch="main",
            repo="owner/repo",
            working_dir="/workspace",
            _container=container,
        )
    )


def test_clean_and_conflict_responses_keep_shape_fetch_before_merge_and_journal():
    for merge_result, expected_status, outcome, approved in [
        (0, "merged", "clean", True),
        (1, "conflicts", "conflicts", False),
        (2, "error", "failed exit=2", False),
    ]:
        container = RecordingContainer(merge_result=merge_result)
        events = []

        @contextmanager
        def grant(repo, *, token, events=events):
            events.append(("grant-open", repo, token))
            try:
                yield
            finally:
                events.append(("grant-close", repo))

        def journal(*args, events=events, **kwargs):
            events.append(("journal", args, kwargs))
        with _patched(container, grant, journal):
            response = _call(container)

        assert response["status"] == expected_status
        if expected_status != "error":
            assert response["base_branch"] == "main"
        if expected_status != "error":
            assert "conflicts" in response
        commands = [command for command, _ in container.calls]
        fetch_index = next(i for i, command in enumerate(commands) if "fetch origin" in command)
        merge_index = next(i for i, command in enumerate(commands) if "git merge origin/main" in command)
        assert fetch_index < merge_index
        assert events[0] == ("grant-open", "owner/repo", TOKEN)
        assert events[-2][0] == "journal"
        assert events[-2][1][1] == "merge_base"
        assert events[-2][2]["approved"] is approved
        assert f"outcome={outcome}" in events[-2][1][2]
        assert events[-1][0] == "grant-close"
        if expected_status == "conflicts":
            assert response["conflicts"] == ["src/a.py"]
        if expected_status == "error":
            assert response["step"] == "merge"


def test_nonzero_fetch_stops_before_merge_and_records_fetch_denial_inside_grant():
    container = RecordingContainer(fetch_result=7)
    events = []

    @contextmanager
    def grant(repo, *, token):
        events.append(("grant-open", repo, token))
        try:
            yield
        finally:
            events.append(("grant-close", repo))

    def journal(*args, events=events, **kwargs):
        events.append(("journal", args, kwargs))
    with _patched(container, grant, journal):
        response = _call(container)

    assert response["status"] == "error"
    assert response["step"] == "fetch"
    assert not any("git merge origin/main" in command for command, _ in container.calls)
    assert events[0][0] == "grant-open"
    assert events[1][0] == "journal"
    assert events[1][1][1] == "merge_base_fetch"
    assert events[1][2]["approved"] is False
    assert "step=fetch outcome=failed" in events[1][1][2]
    assert events[2][0] == "grant-close"


@pytest.mark.parametrize("failure", ["proxy_auth", "grant_open"])
def test_grant_open_failures_return_merge_error_journal_denial_without_git_commands(failure):
    container = RecordingContainer()
    events = []

    @contextmanager
    def grant(_repo, *, token):
        if failure == "proxy_auth":
            raise ProxyAuthError("proxy denied read")
        raise RuntimeError("grant could not open")
        yield

    def journal(*args, events=events, **kwargs):
        events.append(("journal", args, kwargs))
    with _patched(container, grant, journal):
        response = _call(container)

    assert response["status"] == "error"
    assert response["step"] == "merge"
    assert response["error"].startswith("Unexpected error during merge:")
    assert len(events) == 1
    assert events[0][1][1] == "merge_base"
    assert events[0][2]["approved"] is False
    assert "step=exception" in events[0][1][2]
    assert not any("fetch origin" in command or "git merge origin/main" in command for command, _ in container.calls)


def test_grant_close_failure_follows_successful_merge_and_success_journal():
    container = RecordingContainer()
    events = []

    @contextmanager
    def grant(_repo, *, token):
        try:
            yield
        finally:
            events.append(("grant-close",))
            raise RuntimeError("grant cleanup failed")

    def journal(*args, events=events, **kwargs):
        events.append(("journal", args, kwargs))
    with _patched(container, grant, journal):
        response = _call(container)

    assert response["status"] == "error"
    assert response["step"] == "merge"
    assert "Unexpected error during merge" in response["error"]
    assert any("git merge origin/main" in command for command, _ in container.calls)
    assert [event[0] for event in events] == ["journal", "grant-close", "journal"]
    assert events[0][1][1] == "merge_base"
    assert events[0][2]["approved"] is True
    assert "outcome=clean" in events[0][1][2]
    assert events[2][1][1] == "merge_base"
    assert events[2][2]["approved"] is False
    assert "step=exception" in events[2][1][2]


@pytest.mark.parametrize("failure", ["grant_exception", "fetch_output"])
def test_merge_base_redacts_token_from_response_logs_and_journal(failure, caplog):
    container = RecordingContainer(
        fetch_result=7 if failure == "fetch_output" else 0,
        fetch_output=f"transport rejected {TOKEN}".encode(),
    )
    events = []

    @contextmanager
    def grant(_repo, *, token):
        if failure == "grant_exception":
            raise RuntimeError(f"grant unavailable for {TOKEN}")
        yield

    def journal(*args, events=events, **kwargs):
        events.append(("journal", args, kwargs))
    with _patched(container, grant, journal):
        response = _call(container)

    recorded = json.dumps(response) + caplog.text + json.dumps(events)
    assert TOKEN not in recorded
