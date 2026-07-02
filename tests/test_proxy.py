"""Tests for the egress proxy addon (Issue #354).

These exercise the pure classifier and the decision core only, so they run
without mitmproxy installed (the addon guards its ``from mitmproxy import
http`` import).  End-to-end behaviour (real clone passes / push blocked
through a TLS-terminating mitmproxy) was validated by a manual PoC; see the
module docstring in ``code_sandbox_mcp.proxy``.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from code_sandbox_mcp.proxy import (
    CONTROL_HOST_ENV,
    CONTROL_PORT_ENV,
    CONTROL_SECRET_ENV,
    DEFAULT_WINDOW_TTL_SECONDS,
    FETCH_SERVICE,
    PUSH_SERVICE,
    AuthControlServer,
    Decision,
    EgressGuard,
    allowed_repos_from_env,
    control_bind_from_env,
    git_service_from_request,
    handle_control_request,
    is_push,
    repo_from_path,
)


class TestGitServiceDiscrimination:
    """A git push must be identified by service name, not HTTP method."""

    def test_ref_discovery_query_upload_pack(self) -> None:
        assert git_service_from_request("/o/r.git/info/refs", FETCH_SERVICE) == FETCH_SERVICE
        assert is_push("/o/r.git/info/refs", FETCH_SERVICE) is False

    def test_ref_discovery_query_receive_pack(self) -> None:
        assert git_service_from_request("/o/r.git/info/refs", PUSH_SERVICE) == PUSH_SERVICE
        assert is_push("/o/r.git/info/refs", PUSH_SERVICE) is True

    def test_data_path_upload_pack(self) -> None:
        assert git_service_from_request("/o/r.git/git-upload-pack", None) == FETCH_SERVICE
        assert is_push("/o/r.git/git-upload-pack", None) is False

    def test_data_path_receive_pack(self) -> None:
        assert git_service_from_request("/o/r.git/git-receive-pack", None) == PUSH_SERVICE
        assert is_push("/o/r.git/git-receive-pack", None) is True

    def test_query_takes_precedence_over_path(self) -> None:
        assert git_service_from_request("/o/r.git/info/refs", PUSH_SERVICE) == PUSH_SERVICE

    def test_service_match_is_case_insensitive(self) -> None:
        # PR #362 review: a non-standard cased query must not slip through.
        assert git_service_from_request("/o/r.git/info/refs", "GIT-RECEIVE-PACK") == PUSH_SERVICE
        assert is_push("/o/r.git/info/refs", "Git-Receive-Pack") is True

    def test_unknown_service_is_not_echoed(self) -> None:
        # PR #362 review: an unrecognised service yields None, not the raw value.
        assert git_service_from_request("/o/r.git/info/refs", "git-foobar") is None
        assert is_push("/o/r.git/info/refs", "git-foobar") is False

    def test_non_git_request_is_not_a_service(self) -> None:
        # api.github.com REST is not a push and passes through -- the #360 gap.
        assert git_service_from_request("/repos/o/r/issues", None) is None
        assert is_push("/repos/o/r/issues", None) is False


class TestRepoFromPath:
    """Extract owner/repo from a git smart-HTTP path."""

    def test_ref_discovery_path(self) -> None:
        assert repo_from_path("/octocat/hello-world.git/info/refs") == "octocat/hello-world"

    def test_data_path_without_dot_git(self) -> None:
        assert repo_from_path("/octocat/hello-world/git-receive-pack") == "octocat/hello-world"

    def test_lowercases_owner_and_repo(self) -> None:
        # GitHub repo names are case-insensitive (PR #365 review).
        assert repo_from_path("/Octocat/Hello-World.git/info/refs") == "octocat/hello-world"

    def test_too_short_path(self) -> None:
        assert repo_from_path("/octocat") is None
        assert repo_from_path("/") is None


class TestEgressGuardDecision:
    """The decision core: only push is gated, deny-by-default + allowlist + window."""

    def test_clone_always_passes(self) -> None:
        guard = EgressGuard()  # empty allowlist
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=100.0)
        assert d.allow is True

    def test_non_git_passes(self) -> None:
        guard = EgressGuard()
        d = guard.decide("/repos/o/r/issues", None, now=100.0)
        assert d.allow is True

    def test_push_denied_by_default(self) -> None:
        guard = EgressGuard()
        d = guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=100.0)
        assert d.allow is False
        assert "allowlist" in d.reason

    def test_push_to_allowed_repo_without_window_denied(self) -> None:
        guard = EgressGuard({"o/r"})
        d = guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=100.0)
        assert d.allow is False
        assert "authorization window" in d.reason

    def test_push_allowed_repo_with_open_window(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=30)
        # Both requests of a single push must pass while the window is open.
        now = time.monotonic()
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now).allow is True
        assert guard.decide("/o/r.git/git-receive-pack", None, now).allow is True

    def test_push_to_non_allowlisted_repo_with_window_denied(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_window("other/repo", ttl_seconds=30)
        d = guard.decide("/other/repo.git/info/refs", PUSH_SERVICE, now=time.monotonic())
        # Window open but repo not in allowlist -> still denied.
        assert d.allow is False
        assert "allowlist" in d.reason

    def test_window_expires(self) -> None:
        guard = EgressGuard({"o/r"})
        # Drive the clock explicitly (open_window takes now, like decide).
        base = time.monotonic()
        guard.open_window("o/r", ttl_seconds=5.0, now=base)
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, base + 1.0).allow is True
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, base + 6.0).allow is False

    def test_push_match_is_case_insensitive(self) -> None:
        # Allowlist entry, window key, and URL path differ only in case.
        guard = EgressGuard({"Octocat/Hello-World"})
        guard.open_window("octocat/HELLO-world", ttl_seconds=30, now=100.0)
        d = guard.decide("/octocat/hello-world.git/info/refs", PUSH_SERVICE, now=101.0)
        assert d.allow is True

    def test_close_window_revokes(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=30)
        guard.close_window("o/r")
        d = guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=time.monotonic())
        assert d.allow is False


class TestAllowlistFromEnv:
    """Parsing the CODE_SANDBOX_ALLOWED_REPOS env var."""

    def test_comma_separated(self) -> None:
        env = {"CODE_SANDBOX_ALLOWED_REPOS": "a/b, c/d ,e/f"}
        assert allowed_repos_from_env(env) == {"a/b", "c/d", "e/f"}

    def test_empty(self) -> None:
        assert allowed_repos_from_env({}) == set()
        assert allowed_repos_from_env({"CODE_SANDBOX_ALLOWED_REPOS": ""}) == set()


class TestTokenInjection:
    """Only authorized pushes, and only when a token is held, get credentials."""

    def test_no_token_injects_nothing(self) -> None:
        guard = EgressGuard({"o/r"})  # no token configured
        guard.open_window("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=True) == {}

    def test_authorized_push_gets_bearer(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_secret")
        guard.open_window("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=True) == {
            "Authorization": "Bearer ghs_secret"
        }

    def test_denied_push_gets_no_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_secret")  # allowlisted but no window
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is False
        assert guard.token_headers_for(d, is_push_request=True) == {}

    def test_clone_gets_no_token(self) -> None:
        # Even an allowed fetch must not receive push credentials.
        guard = EgressGuard({"o/r"}, token="ghs_secret")
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=False) == {}

    def test_window_scoped_token_injected(self) -> None:
        # No static token: the credential travels with the window (#356).
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=30, now=100.0, token="ghs_window")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {
            "Authorization": "Bearer ghs_window"
        }

    def test_window_token_beats_static_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_static")
        guard.open_window("o/r", ttl_seconds=30, now=100.0, token="ghs_window")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {
            "Authorization": "Bearer ghs_window"
        }

    def test_no_repo_falls_back_to_static_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_static")
        guard.open_window("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert guard.token_headers_for(d, is_push_request=True, now=101.0) == {
            "Authorization": "Bearer ghs_static"
        }

    def test_expired_window_token_not_injected(self) -> None:
        # Even against a (fabricated) allow decision, an expired window's
        # token must never leave the guard.
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=5.0, now=100.0, token="ghs_window")
        d = Decision(True, "fabricated allow")
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=106.0) == {}

    def test_closed_window_drops_token(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=30, now=100.0, token="ghs_window")
        guard.close_window("o/r")
        d = Decision(True, "fabricated allow")
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {}

    def test_expired_window_entry_is_scrubbed(self) -> None:
        # decide() on an expired window must also evict the entry so the
        # window-scoped token does not linger in memory past expiry (#356).
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=5.0, now=100.0, token="ghs_window")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=106.0)
        assert d.allow is False
        assert guard._windows == {}


class TestControlRequestDispatch:
    """The pure control dispatcher: auth, validation, and window open/close."""

    def test_allow_opens_window(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard,
            secret=None,
            path="/auth/allow",
            provided_secret=None,
            payload={"repo": "o/r", "ttl_seconds": 30},
            now=100.0,
        )
        assert res.status == 200
        assert res.body["ok"] is True
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=101.0).allow is True

    def test_allow_uses_default_ttl(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, None, "/auth/allow", None, {"repo": "o/r"}, now=100.0
        )
        assert res.status == 200
        assert res.body["ttl_seconds"] == DEFAULT_WINDOW_TTL_SECONDS

    def test_allow_with_token_arms_window_scoped_injection(self) -> None:
        # publish hands the push credential over with the window (#356); the
        # authorized push must then carry it as a Bearer header.
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard,
            None,
            "/auth/allow",
            None,
            {"repo": "o/r", "ttl_seconds": 30, "token": "ghs_window"},
            now=100.0,
        )
        assert res.status == 200
        # The credential must never be echoed back (the response is loggable).
        assert "ghs_window" not in json.dumps(res.body)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {
            "Authorization": "Bearer ghs_window"
        }

    def test_allow_with_non_string_token_rejected(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, None, "/auth/allow", None, {"repo": "o/r", "token": 12345}
        )
        assert res.status == 400

    def test_revoke_closes_window(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_window("o/r", ttl_seconds=30, now=100.0)
        res = handle_control_request(guard, None, "/auth/revoke", None, {"repo": "o/r"})
        assert res.status == 200
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=101.0).allow is False

    def test_wrong_secret_rejected_and_window_untouched(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard,
            secret="s3cr3t",
            path="/auth/allow",
            provided_secret="wrong",
            payload={"repo": "o/r"},
            now=100.0,
        )
        assert res.status == 403
        # Auth failure must not have opened a window.
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=101.0).allow is False

    def test_correct_secret_accepted(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, "s3cr3t", "/auth/allow", "s3cr3t", {"repo": "o/r"}, now=100.0
        )
        assert res.status == 200

    def test_missing_secret_when_required_rejected(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, "s3cr3t", "/auth/allow", None, {"repo": "o/r"}, now=100.0
        )
        assert res.status == 403

    def test_bad_repo_rejected(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, None, "/auth/allow", None, {"repo": "no-slash"}
        )
        assert res.status == 400

    def test_non_object_payload_rejected(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(
            guard, None, "/auth/allow", None, ["not", "a", "dict"]
        )
        assert res.status == 400

    def test_non_positive_ttl_rejected(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, None, "/auth/allow", None, {"repo": "o/r", "ttl_seconds": 0}
        )
        assert res.status == 400

    def test_bool_ttl_rejected(self) -> None:
        # bool is an int subclass; True must not be accepted as a 1-second TTL.
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, None, "/auth/allow", None, {"repo": "o/r", "ttl_seconds": True}
        )
        assert res.status == 400

    def test_unknown_endpoint(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(guard, None, "/auth/other", None, {"repo": "o/r"})
        assert res.status == 404


class TestControlServerOverHttp:
    """End-to-end over a real socket, mirroring publish's HTTP control calls."""

    @staticmethod
    def _post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers=headers or {}, method="POST"
        )
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (localhost test URL)
            return resp.status, json.loads(resp.read())

    def test_allow_then_revoke_over_http(self) -> None:
        guard = EgressGuard({"o/r"})
        server = AuthControlServer(guard, secret="s3cr3t")
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, body = self._post(
                base + "/auth/allow",
                {"repo": "o/r", "ttl_seconds": 30},
                {"X-Control-Token": "s3cr3t"},
            )
            assert status == 200
            assert body["ok"] is True
            now = time.monotonic()
            assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now).allow is True

            status, _ = self._post(
                base + "/auth/revoke",
                {"repo": "o/r"},
                {"X-Control-Token": "s3cr3t"},
            )
            assert status == 200
            now = time.monotonic()
            assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now).allow is False
        finally:
            server.stop()

    def test_oversized_body_rejected(self) -> None:
        # A body over MAX_CONTROL_BODY_BYTES is rejected 413, unread (PR #367 review).
        guard = EgressGuard({"o/r"})
        server = AuthControlServer(guard, secret="s3cr3t")
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            oversized = {"repo": "o/r", "pad": "x" * 8192}
            with pytest.raises(urllib.error.HTTPError) as ei:
                self._post(
                    base + "/auth/allow",
                    oversized,
                    {"X-Control-Token": "s3cr3t"},
                )
            assert ei.value.code == 413
            now = time.monotonic()
            assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now).allow is False
        finally:
            server.stop()

    def test_wrong_secret_over_http_is_403(self) -> None:
        guard = EgressGuard({"o/r"})
        server = AuthControlServer(guard, secret="s3cr3t")
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            with pytest.raises(urllib.error.HTTPError) as ei:
                self._post(
                    base + "/auth/allow",
                    {"repo": "o/r"},
                    {"X-Control-Token": "nope"},
                )
            assert ei.value.code == 403
            now = time.monotonic()
            assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now).allow is False
        finally:
            server.stop()


class TestLoadsUnderMitmdump:
    """Regression: the addon must import when its module is not in sys.modules.

    ``mitmdump -s proxy.py`` execs the file as a module without registering it
    in ``sys.modules`` under its ``__name__``.  Under ``from __future__ import
    annotations`` that used to crash the first ``@dataclass`` (dataclasses
    resolves string field annotations via ``sys.modules[cls.__module__]``,
    which was ``None``), so the sidecar addon failed to load at all -- caught by
    the #358 smoke test.  This reproduces that load path.
    """

    def test_exec_module_when_absent_from_sys_modules(self) -> None:
        import importlib.util
        import sys

        from code_sandbox_mcp import proxy as installed_proxy

        name = "cs_proxy_mitmdump_probe"
        spec = importlib.util.spec_from_file_location(name, installed_proxy.__file__)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # Mimic mitmproxy's loader: do NOT pre-register the module.
        assert name not in sys.modules
        try:
            spec.loader.exec_module(module)  # must not raise
            # The dataclasses that used to crash are present and usable.
            assert module.Decision(True, "ok").allow is True
            assert module.EgressGuard(allowed_repos={"o/r"}) is not None
        finally:
            sys.modules.pop(name, None)


class TestControlBindFromEnv:
    """Control-API bind config: non-loopback binds must carry a secret (#358)."""

    def test_unset_port_returns_none(self) -> None:
        assert control_bind_from_env({}) is None
        assert control_bind_from_env({CONTROL_PORT_ENV: "  "}) is None

    def test_default_binds_loopback_without_secret(self) -> None:
        assert control_bind_from_env({CONTROL_PORT_ENV: "9099"}) == ("127.0.0.1", 9099, None)

    def test_non_loopback_with_secret_is_allowed(self) -> None:
        env = {
            CONTROL_PORT_ENV: "9099",
            CONTROL_HOST_ENV: "0.0.0.0",
            CONTROL_SECRET_ENV: "s3cret",
        }
        assert control_bind_from_env(env) == ("0.0.0.0", 9099, "s3cret")

    def test_non_loopback_without_secret_is_refused(self) -> None:
        env = {CONTROL_PORT_ENV: "9099", CONTROL_HOST_ENV: "0.0.0.0"}
        with pytest.raises(ValueError, match="requires"):
            control_bind_from_env(env)

    def test_blank_host_falls_back_to_loopback(self) -> None:
        env = {CONTROL_PORT_ENV: "9099", CONTROL_HOST_ENV: "   "}
        assert control_bind_from_env(env) == ("127.0.0.1", 9099, None)

    def test_non_integer_port_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not an integer"):
            control_bind_from_env({CONTROL_PORT_ENV: "not-a-port"})
