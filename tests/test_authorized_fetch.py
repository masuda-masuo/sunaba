"""Unit tests for the shared authorized fetch engine."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from sunaba.proxy_client import ProxyAuthError
from sunaba.tools.github_api import _push_token_env as real_push_token_env
from sunaba.tools.publish_planner import select_push_env as real_select_push_env
from sunaba.tools.vcs.authorized_fetch import FETCH_COMMAND, authorized_origin_fetch

TOKEN = "secret-token"


def run_fetch(*, token=TOKEN, proxied=False, execute_fn=None, grant_factory=None, repo="owner/repo"):
    events = []
    def resolve_token():
        events.append("token")
        return token
    def resolve_repo():
        events.append("repo")
        return repo
    def push_token_env(value):
        events.append("token_env")
        assert value == (token or "")
        return real_push_token_env(value)

    def is_proxy_configured():
        events.append("proxy")
        return proxied

    def select_env(token_env, proxy):
        events.append("environment")
        assert proxy is proxied
        return real_select_push_env(token_env, proxy)
    if grant_factory is None:
        @contextmanager
        def default_grant(_repo, *, token):
            events.append(("grant", _repo, token))
            yield
        grant_factory = default_grant
    if execute_fn is None:
        def default_execute(command, env):
            events.append(("execute", command, env))
            return 0, "", ""
        execute_fn = default_execute
    outcome = authorized_origin_fetch(
        resolve_token=resolve_token,
        resolve_repo=resolve_repo,
        push_token_env=push_token_env,
        proxy_configured=is_proxy_configured,
        select_push_env=select_env,
        authorized_read_grant=grant_factory,
        execute=execute_fn,
    )
    return outcome, events


@pytest.mark.parametrize("proxied", [False, True])
def test_native_and_proxy_success_use_shared_fetch_command(proxied):
    outcome, events = run_fetch(proxied=proxied)
    assert outcome.failure is None
    assert outcome.repo == "owner/repo"
    assert outcome.proxied is proxied
    assert events[0:5] == ["token", "repo", "token_env", "proxy", "environment"]
    assert events[5][0] == "grant"
    assert events[6][1] == FETCH_COMMAND
    if proxied:
        assert events[6][2] is None
    else:
        assert events[6][2] == {"GITHUB_TOKEN": TOKEN, "GH_TOKEN": TOKEN}


def test_token_resolution_failure_stops_before_repo_or_grant():
    outcome = authorized_origin_fetch(
        resolve_token=lambda: (_ for _ in ()).throw(RuntimeError(TOKEN)),
        resolve_repo=Mock(side_effect=AssertionError("repo must not resolve")),
        push_token_env=Mock(),
        proxy_configured=Mock(),
        select_push_env=Mock(),
        authorized_read_grant=Mock(),
        execute=Mock(),
    )
    assert (outcome.failure, outcome.error, outcome.token) == (
        "token_resolution", "token provider error", None
    )


def test_repo_resolution_error_is_redacted_before_environment():
    push_token_env = Mock(return_value={"GITHUB_TOKEN": TOKEN})
    proxy_configured = Mock(return_value=False)
    select_push_env = Mock()
    authorized_read_grant = Mock()
    execute = Mock()
    outcome = authorized_origin_fetch(
        resolve_token=lambda: TOKEN,
        resolve_repo=lambda: (_ for _ in ()).throw(RuntimeError(f"bad {TOKEN}")),
        push_token_env=push_token_env,
        proxy_configured=proxy_configured,
        select_push_env=select_push_env,
        authorized_read_grant=authorized_read_grant,
        execute=execute,
    )
    assert outcome.failure == "repo_resolution"
    assert outcome.error == "bad <redacted>"
    push_token_env.assert_not_called()
    proxy_configured.assert_not_called()
    select_push_env.assert_not_called()
    authorized_read_grant.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("exc,kind", [
    (ProxyAuthError(f"proxy {TOKEN}"), "proxy_auth"),
    (RuntimeError(f"open {TOKEN}"), "grant_open"),
])
def test_grant_entry_failures_are_classified_and_redacted(exc, kind):
    @contextmanager
    def grant(*args, **kwargs):
        raise exc
        yield
    outcome, _ = run_fetch(grant_factory=grant)
    assert outcome.failure == kind
    assert TOKEN not in outcome.error
    assert "<redacted>" in outcome.error


def test_execution_exception_is_reported_after_grant_closes():
    events = []
    @contextmanager
    def grant(*args, **kwargs):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")
    outcome, _ = run_fetch(
        grant_factory=grant,
        execute_fn=lambda *_: (_ for _ in ()).throw(RuntimeError(f"exec {TOKEN}")),
    )
    assert outcome.failure == "execution"
    assert outcome.error == "exec <redacted>"
    assert events == ["enter", "exit"]


def test_nonzero_prefers_stderr_then_stdout_then_exit_and_redacts():
    for out, err, expected in [
        (f"out {TOKEN}", f"err {TOKEN}", "err <redacted>"),
        (f"out {TOKEN}", "", "out <redacted>"),
        ("", "", "exit code 7"),
    ]:
        outcome, _ = run_fetch(execute_fn=lambda *_, out=out, err=err: (7, out, err))
        assert outcome.failure == "exit"
        assert outcome.exit_code == 7
        assert outcome.error == expected


@pytest.mark.parametrize("exc,kind", [
    (ProxyAuthError(f"proxy close {TOKEN}"), "proxy_auth"),
    (RuntimeError(f"close {TOKEN}"), "grant_close"),
])
def test_exit_failures_classify_with_proxy_branch_and_cleanup_precedence(exc, kind):
    @contextmanager
    def grant(*args, **kwargs):
        yield
        raise exc
    outcome, _ = run_fetch(
        grant_factory=grant,
        execute_fn=lambda *_: (_ for _ in ()).throw(RuntimeError("execution failure")),
    )
    assert outcome.failure == kind
    assert TOKEN not in outcome.error
    assert "execution failure" not in outcome.error


def test_execution_exception_stringification_waits_until_grant_closes():
    events = []

    class DeferredError(Exception):
        def __str__(self):
            assert events == ["enter", "execute", "close"]
            return f"late {TOKEN}"

    @contextmanager
    def grant(*args, **kwargs):
        events.append("enter")
        try:
            yield
        finally:
            events.append("close")

    def execute(*_):
        events.append("execute")
        raise DeferredError()

    outcome, _ = run_fetch(grant_factory=grant, execute_fn=execute)
    assert outcome.failure == "execution"
    assert outcome.error == "late <redacted>"
    assert events == ["enter", "execute", "close"]


def test_success_does_not_format_nonzero_detail_or_strip_output():
    class Detail(str):
        def strip(self):
            raise AssertionError("successful fetch must not select failure detail")

    outcome, _ = run_fetch(execute_fn=lambda *_: (0, Detail(" ok "), ""))
    assert outcome.failure is None
    assert outcome.error == ""
    assert outcome.stdout == " ok "


def test_execute_unpacking_error_is_inside_execution_boundary():
    class BrokenResult:
        def __iter__(self):
            raise RuntimeError(f"unpack {TOKEN}")

    outcome, _ = run_fetch(execute_fn=lambda *_: BrokenResult())
    assert outcome.failure == "execution"
    assert outcome.error == "unpack <redacted>"


def test_publish_network_state_callback_runs_after_env_selection_before_grant():
    events = []

    @contextmanager
    def grant(*args, **kwargs):
        events.append("grant")
        yield

    outcome = authorized_origin_fetch(
        resolve_token=lambda: "token",
        resolve_repo=lambda: "owner/repo",
        push_token_env=lambda value: events.append("token_env") or {"GITHUB_TOKEN": value},
        proxy_configured=lambda: events.append("proxy") or False,
        select_push_env=lambda env, proxy: events.append("select") or env,
        read_network_off=lambda: events.append("network_off") or True,
        authorized_read_grant=grant,
        execute=lambda *_: (0, "", ""),
    )
    assert outcome.network_off is True
    assert events == ["token_env", "proxy", "select", "network_off", "grant"]


def test_suppressed_execution_failure_returns_explicit_execution_outcome():
    import subprocess
    import sys

    code = '''
from contextlib import contextmanager
from sunaba.tools.vcs.authorized_fetch import authorized_origin_fetch

@contextmanager
def suppress_execution_error(*args, **kwargs):
    try:
        yield
    except KeyboardInterrupt:
        pass

outcome = authorized_origin_fetch(
    resolve_token=lambda: "token-secret",
    resolve_repo=lambda: "owner/repo",
    push_token_env=lambda token: {"GITHUB_TOKEN": token},
    proxy_configured=lambda: True,
    select_push_env=lambda env, proxy: None,
    read_network_off=lambda: True,
    authorized_read_grant=suppress_execution_error,
    execute=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
)
if (outcome.failure, outcome.error, outcome.token, outcome.repo,
        outcome.proxied, outcome.token_missing, outcome.network_off) != (
        "execution", "fetch execution returned no result", "token-secret",
        "owner/repo", True, False, True):
    raise SystemExit(1)
'''
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-O", "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
