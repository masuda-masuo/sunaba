"""Tests for the host-side egress-proxy control client (#357).

These wire the real :class:`AuthControlServer` (#356) to the client over a real
socket and assert the effect through the guard's own decision, so both sides of
the control protocol are exercised end to end.
"""
from __future__ import annotations

import time

import pytest

from code_sandbox_mcp.proxy import AuthControlServer, EgressGuard
from code_sandbox_mcp.proxy_client import (
    CONTROL_SECRET_ENV,
    CONTROL_URL_ENV,
    ProxyAuthError,
    ProxyControlConfig,
    authorized_push_window,
    close_window,
    open_window,
)

REPO = "owner/repo"
_PUSH_PATH = "/owner/repo.git/git-receive-pack"
_PUSH_SERVICE = "git-receive-pack"
_SECRET = "s3cret"


def _push_allowed(guard: EgressGuard) -> bool:
    """Whether the guard would currently let a push to REPO through."""
    return guard.decide(_PUSH_PATH, _PUSH_SERVICE, time.monotonic()).allow


@pytest.fixture
def guard() -> EgressGuard:
    return EgressGuard(allowed_repos={REPO})


@pytest.fixture
def server(guard: EgressGuard):
    srv = AuthControlServer(guard, secret=_SECRET)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def config(server: AuthControlServer) -> ProxyControlConfig:
    return ProxyControlConfig(
        base_url=f"http://127.0.0.1:{server.port}", secret=_SECRET
    )


class TestFromEnv:
    def test_none_when_url_unset(self) -> None:
        assert ProxyControlConfig.from_env(env={}) is None

    def test_none_when_url_blank(self) -> None:
        assert ProxyControlConfig.from_env(env={CONTROL_URL_ENV: "   "}) is None

    def test_parses_url_and_secret(self) -> None:
        cfg = ProxyControlConfig.from_env(
            env={CONTROL_URL_ENV: "http://proxy:9099/", CONTROL_SECRET_ENV: "k"}
        )
        assert cfg is not None
        assert cfg.base_url == "http://proxy:9099"  # trailing slash stripped
        assert cfg.secret == "k"

    def test_secret_optional(self) -> None:
        cfg = ProxyControlConfig.from_env(env={CONTROL_URL_ENV: "http://proxy:9099"})
        assert cfg is not None
        assert cfg.secret is None


class TestUnconfiguredIsNoop:
    def test_open_close_noop(self, guard: EgressGuard) -> None:
        # No config resolvable -> both calls are no-ops and never touch a socket.
        open_window(REPO, config=None)
        close_window(REPO, config=None)
        assert not _push_allowed(guard)

    def test_context_manager_noop(self, guard: EgressGuard) -> None:
        with authorized_push_window(REPO, config=None):
            assert not _push_allowed(guard)
        assert not _push_allowed(guard)


class TestWindowLifecycle:
    def test_open_then_close_toggles_window(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        assert not _push_allowed(guard)
        open_window(REPO, config=config)
        assert _push_allowed(guard)
        close_window(REPO, config=config)
        assert not _push_allowed(guard)

    def test_context_manager_opens_then_revokes(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        assert not _push_allowed(guard)
        with authorized_push_window(REPO, config=config):
            assert _push_allowed(guard)
        assert not _push_allowed(guard)

    def test_context_manager_revokes_on_exception(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with authorized_push_window(REPO, config=config):
                assert _push_allowed(guard)
                raise RuntimeError("boom")
        # Window revoked despite the push raising.
        assert not _push_allowed(guard)


class TestAuthFailures:
    def test_wrong_secret_raises_and_leaves_window_shut(
        self, guard: EgressGuard, server: AuthControlServer
    ) -> None:
        bad = ProxyControlConfig(
            base_url=f"http://127.0.0.1:{server.port}", secret="wrong"
        )
        with pytest.raises(ProxyAuthError):
            open_window(REPO, config=bad)
        assert not _push_allowed(guard)

    def test_unreachable_proxy_raises(self, guard: EgressGuard) -> None:
        # Start then stop a server so its port is bound-and-released (a plain
        # stop() without a prior start() would hang in serve_forever's shutdown).
        dead = AuthControlServer(guard, secret=_SECRET)
        dead.start()
        port = dead.port
        dead.stop()
        cfg = ProxyControlConfig(base_url=f"http://127.0.0.1:{port}", secret=_SECRET)
        with pytest.raises(ProxyAuthError):
            open_window(REPO, config=cfg)
