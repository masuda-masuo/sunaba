"""Tests for the egress proxy addon (Issue #354).

These exercise the pure classifier and the decision core only, so they run
without mitmproxy installed (the addon guards its ``from mitmproxy import
http`` import).  End-to-end behaviour (real clone passes / push blocked
through a TLS-terminating mitmproxy) was validated by a manual PoC; see the
module docstring in ``code_sandbox_mcp.proxy``.
"""
from __future__ import annotations

import time

from code_sandbox_mcp.proxy import (
    FETCH_SERVICE,
    PUSH_SERVICE,
    EgressGuard,
    allowed_repos_from_env,
    git_service_from_request,
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
