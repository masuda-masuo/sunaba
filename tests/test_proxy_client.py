"""Tests for the host-side egress-proxy control client (#357).

These wire the real :class:`AuthControlServer` (#356) to the client over a real
socket and assert the effect through the guard's own decision, so both sides of
the control protocol are exercised end to end.
"""
from __future__ import annotations

import time

import pytest

from code_sandbox_mcp.proxy import (
    PROXY_SOURCE_FINGERPRINT,
    AuthControlServer,
    EgressGuard,
    basic_auth_header,
    bearer_auth_header,
)
from code_sandbox_mcp.proxy_client import (
    CONTROL_SECRET_ENV,
    CONTROL_URL_ENV,
    ProxyAuthError,
    ProxyControlConfig,
    authorized_api_write_grant,
    authorized_push_grant,
    authorized_read_grant,
    close_api_write_grant,
    close_read_grant,
    close_grant,
    fetch_proxy_fingerprint,
    open_api_write_grant,
    open_read_grant,
    open_grant,
    proxy_configured,
)

REPO = "owner/repo"
_PUSH_PATH = "/owner/repo.git/git-receive-pack"
_PUSH_SERVICE = "git-receive-pack"
_FETCH_PATH = "/owner/repo.git/info/refs"
_FETCH_SERVICE = "git-upload-pack"
_API_WRITE_PATH = "/repos/owner/repo/issues"
_SECRET = "s3cret"


def _push_allowed(guard: EgressGuard) -> bool:
    """Whether the guard would currently let a push to REPO through."""
    return guard.decide(_PUSH_PATH, _PUSH_SERVICE, time.monotonic()).allow


def _push_headers(guard: EgressGuard) -> dict[str, str]:
    """Headers the guard would inject into a push to REPO right now."""
    now = time.monotonic()
    decision = guard.decide(_PUSH_PATH, _PUSH_SERVICE, now)
    return guard.token_headers_for(decision, is_push_request=True, repo=REPO, now=now)


def _fetch_headers(guard: EgressGuard) -> dict[str, str]:
    """Headers the guard would inject into a clone/fetch of REPO right now (#419)."""
    now = time.monotonic()
    decision = guard.decide(_FETCH_PATH, _FETCH_SERVICE, now)
    return guard.token_headers_for(
        decision, is_push_request=False, repo=REPO, now=now, is_fetch_request=True
    )


def _api_write_allowed(guard: EgressGuard) -> bool:
    """Whether the guard would currently let a non-push write to REPO through (#420)."""
    return guard.decide_api_write("POST", _API_WRITE_PATH, time.monotonic()).allow


def _api_write_headers(guard: EgressGuard) -> dict[str, str]:
    """Headers the guard would inject into a non-push write to REPO right now (#420)."""
    now = time.monotonic()
    decision = guard.decide_api_write("POST", _API_WRITE_PATH, now)
    return guard.token_headers_for(
        decision, repo=REPO, now=now, is_api_write_request=True
    )


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
        open_grant(REPO, config=None)
        close_grant(REPO, config=None)
        assert not _push_allowed(guard)

    def test_context_manager_noop(self, guard: EgressGuard) -> None:
        with authorized_push_grant(REPO, config=None):
            assert not _push_allowed(guard)
        assert not _push_allowed(guard)


class TestGrantLifecycle:
    def test_open_then_close_toggles_grant(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        assert not _push_allowed(guard)
        open_grant(REPO, config=config)
        assert _push_allowed(guard)
        close_grant(REPO, config=config)
        assert not _push_allowed(guard)

    def test_context_manager_opens_then_revokes(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        assert not _push_allowed(guard)
        with authorized_push_grant(REPO, config=config):
            assert _push_allowed(guard)
        assert not _push_allowed(guard)

    def test_context_manager_revokes_on_exception(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with authorized_push_grant(REPO, config=config):
                assert _push_allowed(guard)
                raise RuntimeError("boom")
        # Grant revoked despite the push raising.
        assert not _push_allowed(guard)


class TestGrantScopedToken:
    """The push credential travels with the grant over the control API (#356)."""

    def test_token_rides_the_grant(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        open_grant(REPO, token="ghs_grant", config=config)
        assert _push_headers(guard) == {"Authorization": basic_auth_header("ghs_grant")}
        close_grant(REPO, config=config)
        # Revoked grant -> credential gone with it.
        assert _push_headers(guard) == {}

    def test_context_manager_scopes_the_token(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with authorized_push_grant(REPO, token="ghs_grant", config=config):
            assert _push_headers(guard) == {"Authorization": basic_auth_header("ghs_grant")}
        assert _push_headers(guard) == {}

    def test_grants_without_token_inject_nothing(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        open_grant(REPO, config=config)
        assert _push_allowed(guard)
        assert _push_headers(guard) == {}


class TestProxyConfigured:
    def test_false_when_unset(self) -> None:
        assert proxy_configured(env={}) is False

    def test_true_when_url_set(self) -> None:
        assert proxy_configured(env={CONTROL_URL_ENV: "http://proxy:9099"}) is True


class TestAuthFailures:
    def test_wrong_secret_raises_and_leaves_grant_shut(
        self, guard: EgressGuard, server: AuthControlServer
    ) -> None:
        bad = ProxyControlConfig(
            base_url=f"http://127.0.0.1:{server.port}", secret="wrong"
        )
        with pytest.raises(ProxyAuthError):
            open_grant(REPO, config=bad)
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
            open_grant(REPO, config=cfg)


class TestReadGrantLifecycle:
    """Host-side read-authorization grants for clone/fetch (#419)."""

    def test_unconfigured_open_close_noop(self, guard: EgressGuard) -> None:
        open_read_grant(REPO, config=None)
        close_read_grant(REPO, config=None)
        assert _fetch_headers(guard) == {}

    def test_context_manager_noop_when_unconfigured(self, guard: EgressGuard) -> None:
        with authorized_read_grant(REPO, config=None):
            assert _fetch_headers(guard) == {}

    def test_token_rides_the_read_grant(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        open_read_grant(REPO, token="ghs_read", config=config)
        assert _fetch_headers(guard) == {"Authorization": basic_auth_header("ghs_read")}
        close_read_grant(REPO, config=config)
        assert _fetch_headers(guard) == {}

    def test_context_manager_scopes_the_read_token(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with authorized_read_grant(REPO, token="ghs_read", config=config):
            assert _fetch_headers(guard) == {"Authorization": basic_auth_header("ghs_read")}
        assert _fetch_headers(guard) == {}

    def test_context_manager_revokes_read_grant_on_exception(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with authorized_read_grant(REPO, token="ghs_read", config=config):
                assert _fetch_headers(guard) == {"Authorization": basic_auth_header("ghs_read")}
                raise RuntimeError("boom")
        assert _fetch_headers(guard) == {}

    def test_read_grant_never_unlocks_push(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        # guard's allowlist already contains REPO (fixture), so this isolates
        # the read/push separation rather than allowlist membership.
        with authorized_read_grant(REPO, token="ghs_read", config=config):
            assert not _push_allowed(guard)


class TestApiWriteGrantLifecycle:
    """Host-side api-write-authorization grants for non-push REST writes (#420)."""

    def test_unconfigured_open_close_noop(self, guard: EgressGuard) -> None:
        open_api_write_grant(REPO, config=None)
        close_api_write_grant(REPO, config=None)
        assert not _api_write_allowed(guard)

    def test_context_manager_noop_when_unconfigured(self, guard: EgressGuard) -> None:
        with authorized_api_write_grant(REPO, config=None):
            assert not _api_write_allowed(guard)

    def test_open_then_close_toggles_grant(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        assert not _api_write_allowed(guard)
        open_api_write_grant(REPO, config=config)
        assert _api_write_allowed(guard)
        close_api_write_grant(REPO, config=config)
        assert not _api_write_allowed(guard)

    def test_context_manager_opens_then_revokes(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        assert not _api_write_allowed(guard)
        with authorized_api_write_grant(REPO, config=config):
            assert _api_write_allowed(guard)
        assert not _api_write_allowed(guard)

    def test_context_manager_revokes_on_exception(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with pytest.raises(RuntimeError, match="boom"):
            with authorized_api_write_grant(REPO, config=config):
                assert _api_write_allowed(guard)
                raise RuntimeError("boom")
        assert not _api_write_allowed(guard)

    def test_token_rides_the_api_write_grant(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        open_api_write_grant(REPO, token="ghs_write", config=config)
        assert _api_write_headers(guard) == {"Authorization": bearer_auth_header("ghs_write")}
        close_api_write_grant(REPO, config=config)
        assert _api_write_headers(guard) == {}

    def test_context_manager_scopes_the_token(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with authorized_api_write_grant(REPO, token="ghs_write", config=config):
            assert _api_write_headers(guard) == {
                "Authorization": bearer_auth_header("ghs_write")
            }
        assert _api_write_headers(guard) == {}

    def test_api_write_grant_never_unlocks_push(
        self, guard: EgressGuard, config: ProxyControlConfig
    ) -> None:
        with authorized_api_write_grant(REPO, token="ghs_write", config=config):
            assert not _push_allowed(guard)


class TestFingerprintProbe:
    """``fetch_proxy_fingerprint`` over the real control server (#405)."""

    def test_returns_running_source_fingerprint(
        self, config: ProxyControlConfig
    ) -> None:
        # In-process the "sidecar" runs the same proxy.py, so the fetched
        # fingerprint must equal our own computed one.
        assert fetch_proxy_fingerprint(config) == PROXY_SOURCE_FINGERPRINT

    def test_wrong_secret_returns_none_not_raises(
        self, server: AuthControlServer
    ) -> None:
        # /version is secret-gated (403); the probe is best-effort, so a 403
        # HTTPError degrades to None rather than raising (never fails closed).
        bad = ProxyControlConfig(
            base_url=f"http://127.0.0.1:{server.port}", secret="wrong"
        )
        assert fetch_proxy_fingerprint(bad) is None

    def test_unreachable_proxy_returns_none(self) -> None:
        # No server here: an old proxy without /version or a down sidecar must
        # not turn a diagnostic probe into an error.
        dead = ProxyControlConfig(base_url="http://127.0.0.1:9", secret=_SECRET)
        assert fetch_proxy_fingerprint(dead) is None
