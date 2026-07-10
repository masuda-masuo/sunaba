"""Tests for the egress proxy addon (Issue #354).

These exercise the pure classifier and the decision core only, so they run
without mitmproxy installed (the addon guards its ``from mitmproxy import
http`` import).  End-to-end behaviour (real clone passes / push blocked
through a TLS-terminating mitmproxy) was validated by a manual PoC; see the
module docstring in ``sunaba.proxy``.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from sunaba.proxy import (
    API_WRITE_BLOCK_HINT,
    CONTROL_HOST_ENV,
    CONTROL_PORT_ENV,
    CONTROL_SECRET_ENV,
    DEFAULT_EGRESS_HOSTS,
    DEFAULT_GRANT_TTL_SECONDS,
    EGRESS_HOST_BLOCK_HINT,
    EGRESS_HOST_WILDCARD,
    FETCH_SERVICE,
    PROXY_SOURCE_FINGERPRINT,
    PUSH_BLOCK_HINT,
    PUSH_SERVICE,
    AuthControlServer,
    Decision,
    EgressGuard,
    allowed_egress_hosts_from_env,
    allowed_repos_from_env,
    api_repo_from_path,
    basic_auth_header,
    bearer_auth_header,
    block_body,
    control_bind_from_env,
    git_service_from_request,
    handle_control_request,
    is_git_data_api_path,
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
    """The decision core: only push is gated, deny-by-default + allowlist + grant."""

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

    def test_push_to_allowed_repo_without_grant_denied(self) -> None:
        guard = EgressGuard({"o/r"})
        d = guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=100.0)
        assert d.allow is False
        assert "authorization grant" in d.reason

    def test_push_allowed_repo_with_open_grant(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30)
        # Both requests of a single push must pass while the grant is open.
        now = time.monotonic()
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now).allow is True
        assert guard.decide("/o/r.git/git-receive-pack", None, now).allow is True

    def test_push_to_non_allowlisted_repo_with_grant_denied(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("other/repo", ttl_seconds=30)
        d = guard.decide("/other/repo.git/info/refs", PUSH_SERVICE, now=time.monotonic())
        # Grant open but repo not in allowlist -> still denied.
        assert d.allow is False
        assert "allowlist" in d.reason

    def test_grant_expires(self) -> None:
        guard = EgressGuard({"o/r"})
        # Drive the clock explicitly (open_grant takes now, like decide).
        base = time.monotonic()
        guard.open_grant("o/r", ttl_seconds=5.0, now=base)
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, base + 1.0).allow is True
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, base + 6.0).allow is False

    def test_push_match_is_case_insensitive(self) -> None:
        # Allowlist entry, grant key, and URL path differ only in case.
        guard = EgressGuard({"Octocat/Hello-World"})
        guard.open_grant("octocat/HELLO-world", ttl_seconds=30, now=100.0)
        d = guard.decide("/octocat/hello-world.git/info/refs", PUSH_SERVICE, now=101.0)
        assert d.allow is True

    def test_close_grant_revokes(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30)
        guard.close_grant("o/r")
        d = guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=time.monotonic())
        assert d.allow is False


class TestAllowlistFromEnv:
    """Parsing the SUNABA_ALLOWED_REPOS env var."""

    def test_comma_separated(self) -> None:
        env = {"SUNABA_ALLOWED_REPOS": "a/b, c/d ,e/f"}
        assert allowed_repos_from_env(env) == {"a/b", "c/d", "e/f"}

    def test_empty(self) -> None:
        assert allowed_repos_from_env({}) == set()
        assert allowed_repos_from_env({"SUNABA_ALLOWED_REPOS": ""}) == set()


class TestDecideHost:
    """Destination-host containment (#506): default-deny with a built-in allowlist."""

    def test_builtin_github_hosts_pass(self) -> None:
        guard = EgressGuard()
        assert guard.decide_host("github.com").allow is True
        assert guard.decide_host("api.github.com").allow is True
        assert guard.decide_host("codeload.github.com").allow is True

    def test_builtin_registries_pass(self) -> None:
        guard = EgressGuard()
        assert guard.decide_host("pypi.org").allow is True
        assert guard.decide_host("files.pythonhosted.org").allow is True
        assert guard.decide_host("registry.npmjs.org").allow is True
        assert guard.decide_host("proxy.golang.org").allow is True
        assert guard.decide_host("sum.golang.org").allow is True

    def test_unknown_host_denied_by_default(self) -> None:
        guard = EgressGuard()
        d = guard.decide_host("attacker.com")
        assert d.allow is False
        assert "allowlist" in d.reason

    def test_empty_host_denied(self) -> None:
        # A request with no resolvable host must not slip through.
        assert EgressGuard().decide_host("").allow is False

    def test_host_match_is_case_insensitive(self) -> None:
        assert EgressGuard().decide_host("GitHub.com").allow is True

    def test_subdomain_wildcard_entry_matches_subdomains(self) -> None:
        # ``.githubusercontent.com`` is a built-in dotted entry.
        guard = EgressGuard()
        assert guard.decide_host("raw.githubusercontent.com").allow is True
        assert guard.decide_host("objects.githubusercontent.com").allow is True
        # The bare apex is matched too, but a lookalike suffix is not.
        assert guard.decide_host("githubusercontent.com").allow is True
        assert guard.decide_host("evilgithubusercontent.com.attacker.com").allow is False

    def test_operator_added_host_passes(self) -> None:
        guard = EgressGuard(allowed_egress_hosts={"internal.example.com"})
        assert guard.decide_host("internal.example.com").allow is True
        assert guard.decide_host("other.example.com").allow is False

    def test_operator_added_dotted_host_matches_subdomains(self) -> None:
        guard = EgressGuard(allowed_egress_hosts={".example.com"})
        assert guard.decide_host("a.example.com").allow is True
        assert guard.decide_host("example.com").allow is True

    def test_builtins_cannot_be_configured_away(self) -> None:
        # Passing an unrelated host does not drop the built-in registries.
        guard = EgressGuard(allowed_egress_hosts={"internal.example.com"})
        assert guard.decide_host("pypi.org").allow is True

    def test_wildcard_disables_containment(self) -> None:
        guard = EgressGuard(allowed_egress_hosts={EGRESS_HOST_WILDCARD})
        assert guard.decide_host("attacker.com").allow is True
        assert guard.decide_host("anything.example").allow is True


class TestAllowedEgressHostsFromEnv:
    """Parsing SUNABA_ALLOWED_EGRESS_HOSTS (built-ins added by the guard)."""

    def test_comma_separated_lowercased(self) -> None:
        env = {"SUNABA_ALLOWED_EGRESS_HOSTS": "A.com, B.org ,.internal"}
        assert allowed_egress_hosts_from_env(env) == {"a.com", "b.org", ".internal"}

    def test_empty_is_empty_set(self) -> None:
        assert allowed_egress_hosts_from_env({}) == set()
        assert allowed_egress_hosts_from_env({"SUNABA_ALLOWED_EGRESS_HOSTS": ""}) == set()

    def test_wildcard_passes_through(self) -> None:
        env = {"SUNABA_ALLOWED_EGRESS_HOSTS": "*"}
        assert allowed_egress_hosts_from_env(env) == {EGRESS_HOST_WILDCARD}

    def test_env_wired_into_guard(self) -> None:
        hosts = allowed_egress_hosts_from_env(
            {"SUNABA_ALLOWED_EGRESS_HOSTS": "internal.example.com"}
        )
        guard = EgressGuard(allowed_egress_hosts=hosts)
        assert guard.decide_host("internal.example.com").allow is True
        # Built-ins still present.
        assert "github.com" in DEFAULT_EGRESS_HOSTS
        assert guard.decide_host("github.com").allow is True
        assert "proxy.golang.org" in DEFAULT_EGRESS_HOSTS
        assert guard.decide_host("proxy.golang.org").allow is True
        assert "sum.golang.org" in DEFAULT_EGRESS_HOSTS
        assert guard.decide_host("sum.golang.org").allow is True
        # And the block hint names the right env var.
        assert "SUNABA_ALLOWED_EGRESS_HOSTS" in EGRESS_HOST_BLOCK_HINT


class TestTokenInjection:
    """Only authorized pushes, and only when a token is held, get credentials."""

    def test_basic_auth_header_format(self) -> None:
        # GitHub's git endpoint rejects Bearer (401) and requires Basic with
        # the x-access-token username (verified live 2026-07-03); pin the exact
        # wire format so a refactor cannot silently regress it.
        assert basic_auth_header("ghs_secret") == (
            "Basic eC1hY2Nlc3MtdG9rZW46Z2hzX3NlY3JldA=="
        )

    def test_no_token_injects_nothing(self) -> None:
        guard = EgressGuard({"o/r"})  # no token configured
        guard.open_grant("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=True) == {}

    def test_authorized_push_gets_bearer(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_secret")
        guard.open_grant("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=True) == {
            "Authorization": basic_auth_header("ghs_secret")
        }

    def test_denied_push_gets_no_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_secret")  # allowlisted but no grant
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is False
        assert guard.token_headers_for(d, is_push_request=True) == {}

    def test_clone_gets_no_token(self) -> None:
        # Even an allowed fetch must not receive push credentials.
        guard = EgressGuard({"o/r"}, token="ghs_secret")
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=False) == {}

    def test_grant_scoped_token_injected(self) -> None:
        # No static token: the credential travels with the grant (#356).
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_grant")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {
            "Authorization": basic_auth_header("ghs_grant")
        }

    def test_grant_token_beats_static_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_static")
        guard.open_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_grant")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {
            "Authorization": basic_auth_header("ghs_grant")
        }

    def test_no_repo_falls_back_to_static_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_static")
        guard.open_grant("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert guard.token_headers_for(d, is_push_request=True, now=101.0) == {
            "Authorization": basic_auth_header("ghs_static")
        }

    def test_expired_grant_token_not_injected(self) -> None:
        # Even against a (fabricated) allow decision, an expired grant's
        # token must never leave the guard.
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=5.0, now=100.0, token="ghs_grant")
        d = Decision(True, "fabricated allow")
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=106.0) == {}
        # This read path bypasses decide()/_grant_open(), so it must scrub
        # the expired entry itself (PR #402 review).
        assert guard._grants == {}

    def test_closed_grant_drops_token(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_grant")
        guard.close_grant("o/r")
        d = Decision(True, "fabricated allow")
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {}

    def test_expired_grant_entry_is_scrubbed(self) -> None:
        # decide() on an expired grant must also evict the entry so the
        # grant-scoped token does not linger in memory past expiry (#356).
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=5.0, now=100.0, token="ghs_grant")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=106.0)
        assert d.allow is False
        assert guard._grants == {}


class TestReadGrantTokenInjection:
    """Read-authorization grants for clone/fetch (#419)."""

    def test_fetch_with_no_read_grant_gets_no_token(self) -> None:
        guard = EgressGuard({"o/r"}, token="ghs_push_static")
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert d.allow is True
        # The push-side static token must never leak into a fetch.
        assert (
            guard.token_headers_for(d, is_push_request=False, repo="o/r", now=101.0, is_fetch_request=True)
            == {}
        )

    def test_fetch_with_open_read_grant_gets_token(self) -> None:
        guard = EgressGuard()  # empty allowlist -- read grants bypass it
        guard.open_read_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_read")
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(
            d, is_push_request=False, repo="o/r", now=101.0, is_fetch_request=True
        ) == {"Authorization": basic_auth_header("ghs_read")}

    def test_read_grant_never_authorizes_push(self) -> None:
        # Opening a read grant must not widen push access (#419 design note).
        guard = EgressGuard({"o/r"})
        guard.open_read_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_read")
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert d.allow is False
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {}

    def test_push_grant_never_authenticates_fetch(self) -> None:
        # And the converse: a push grant's token must not leak into a fetch.
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_push")
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert guard.token_headers_for(
            d, is_push_request=False, repo="o/r", now=101.0, is_fetch_request=True
        ) == {}

    def test_expired_read_grant_token_not_injected(self) -> None:
        guard = EgressGuard()
        guard.open_read_grant("o/r", ttl_seconds=5.0, now=100.0, token="ghs_read")
        d = Decision(True, "fabricated allow")
        assert (
            guard.token_headers_for(d, is_push_request=False, repo="o/r", now=106.0, is_fetch_request=True)
            == {}
        )
        assert guard._read_grants == {}

    def test_closed_read_grant_drops_token(self) -> None:
        guard = EgressGuard()
        guard.open_read_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_read")
        guard.close_read_grant("o/r")
        d = Decision(True, "fabricated allow")
        assert (
            guard.token_headers_for(d, is_push_request=False, repo="o/r", now=101.0, is_fetch_request=True)
            == {}
        )


class TestControlRequestDispatch:
    """The pure control dispatcher: auth, validation, and grant open/close."""

    def test_allow_opens_grant(self) -> None:
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
        assert res.body["ttl_seconds"] == DEFAULT_GRANT_TTL_SECONDS

    def test_allow_with_token_arms_grant_scoped_injection(self) -> None:
        # publish hands the push credential over with the grant (#356); the
        # authorized push must then carry it as the Authorization header.
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard,
            None,
            "/auth/allow",
            None,
            {"repo": "o/r", "ttl_seconds": 30, "token": "ghs_grant"},
            now=100.0,
        )
        assert res.status == 200
        # The credential must never be echoed back (the response is loggable).
        assert "ghs_grant" not in json.dumps(res.body)
        d = guard.decide("/o/r.git/git-receive-pack", None, now=101.0)
        assert guard.token_headers_for(d, is_push_request=True, repo="o/r", now=101.0) == {
            "Authorization": basic_auth_header("ghs_grant")
        }

    def test_allow_with_non_string_token_rejected(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard, None, "/auth/allow", None, {"repo": "o/r", "token": 12345}
        )
        assert res.status == 400

    def test_revoke_closes_grant(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30, now=100.0)
        res = handle_control_request(guard, None, "/auth/revoke", None, {"repo": "o/r"})
        assert res.status == 200
        assert guard.decide("/o/r.git/info/refs", PUSH_SERVICE, now=101.0).allow is False

    def test_wrong_secret_rejected_and_grant_untouched(self) -> None:
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
        # Auth failure must not have opened a grant.
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

    def test_version_returns_source_fingerprint(self) -> None:
        # /version takes no repo and echoes this sidecar's baked source hash
        # so the host can detect a drifted proxy.py (#405).
        guard = EgressGuard()
        res = handle_control_request(guard, None, "/version", None, {})
        assert res.status == 200
        assert res.body["proxy_fingerprint"] == PROXY_SOURCE_FINGERPRINT
        assert isinstance(PROXY_SOURCE_FINGERPRINT, str) and PROXY_SOURCE_FINGERPRINT

    def test_version_ignores_missing_repo(self) -> None:
        # The repo/payload validation that guards /auth/* must not apply here.
        guard = EgressGuard()
        res = handle_control_request(guard, None, "/version", None, {"junk": 1})
        assert res.status == 200

    def test_version_still_secret_gated(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(
            guard,
            secret="s3cr3t",
            path="/version",
            provided_secret="wrong",
            payload={},
        )
        assert res.status == 403


class TestReadGrantControlDispatch:
    """``/auth/allow-read`` / ``/auth/revoke-read`` (#419)."""

    def test_allow_read_opens_read_grant(self) -> None:
        guard = EgressGuard()  # no push allowlist needed for reads
        res = handle_control_request(
            guard,
            None,
            "/auth/allow-read",
            None,
            {"repo": "o/r", "ttl_seconds": 30, "token": "ghs_read"},
            now=100.0,
        )
        assert res.status == 200
        assert "ghs_read" not in json.dumps(res.body)
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert guard.token_headers_for(
            d, is_push_request=False, repo="o/r", now=101.0, is_fetch_request=True
        ) == {"Authorization": basic_auth_header("ghs_read")}

    def test_allow_read_does_not_authorize_push(self) -> None:
        guard = EgressGuard({"o/r"})
        handle_control_request(
            guard, None, "/auth/allow-read", None, {"repo": "o/r"}, now=100.0
        )
        assert guard.decide("/o/r.git/git-receive-pack", None, now=101.0).allow is False

    def test_revoke_read_closes_read_grant(self) -> None:
        guard = EgressGuard()
        guard.open_read_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_read")
        res = handle_control_request(
            guard, None, "/auth/revoke-read", None, {"repo": "o/r"}
        )
        assert res.status == 200
        d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now=101.0)
        assert (
            guard.token_headers_for(
                d, is_push_request=False, repo="o/r", now=101.0, is_fetch_request=True
            )
            == {}
        )

    def test_allow_read_bad_repo_rejected(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(
            guard, None, "/auth/allow-read", None, {"repo": "no-slash"}
        )
        assert res.status == 400

    def test_allow_read_non_positive_ttl_rejected(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(
            guard, None, "/auth/allow-read", None, {"repo": "o/r", "ttl_seconds": -1}
        )
        assert res.status == 400


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

    def test_allow_read_then_revoke_read_over_http(self) -> None:
        # e2e counterpart to test_allow_then_revoke_over_http, for the #419
        # read-grant endpoints (unit coverage already exists via
        # TestReadGrantControlDispatch; this exercises the real socket).
        guard = EgressGuard()  # read grants need no push allowlist
        server = AuthControlServer(guard, secret="s3cr3t")
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, body = self._post(
                base + "/auth/allow-read",
                {"repo": "o/r", "ttl_seconds": 30, "token": "ghs_read"},
                {"X-Control-Token": "s3cr3t"},
            )
            assert status == 200
            assert body["ok"] is True
            now = time.monotonic()
            d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now)
            assert d.allow is True
            assert guard.token_headers_for(
                d, is_push_request=False, repo="o/r", now=now, is_fetch_request=True
            ) == {"Authorization": basic_auth_header("ghs_read")}
            # Read authorization must never leak into push.
            assert guard.decide("/o/r.git/git-receive-pack", None, now).allow is False

            status, _ = self._post(
                base + "/auth/revoke-read",
                {"repo": "o/r"},
                {"X-Control-Token": "s3cr3t"},
            )
            assert status == 200
            now = time.monotonic()
            d = guard.decide("/o/r.git/info/refs", FETCH_SERVICE, now)
            assert guard.token_headers_for(
                d, is_push_request=False, repo="o/r", now=now, is_fetch_request=True
            ) == {}
        finally:
            server.stop()

    def test_allow_api_write_then_revoke_over_http(self) -> None:
        # e2e counterpart to test_allow_then_revoke_over_http, for the #420
        # api-write control endpoints (unit coverage already exists via
        # TestApiWriteControlDispatch; this exercises the real socket).
        guard = EgressGuard({"o/r"})
        server = AuthControlServer(guard, secret="s3cr3t")
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, body = self._post(
                base + "/auth/allow-api-write",
                {"repo": "o/r", "ttl_seconds": 30, "token": "ghs_write"},
                {"X-Control-Token": "s3cr3t"},
            )
            assert status == 200
            assert body["ok"] is True
            now = time.monotonic()
            d = guard.decide_api_write("POST", "/repos/o/r/issues", now)
            assert d.allow is True
            assert guard.token_headers_for(
                d, repo="o/r", now=now, is_api_write_request=True
            ) == {"Authorization": bearer_auth_header("ghs_write")}
            # An api-write grant must never leak into push.
            assert guard.decide("/o/r.git/git-receive-pack", None, now).allow is False

            status, _ = self._post(
                base + "/auth/revoke-api-write",
                {"repo": "o/r"},
                {"X-Control-Token": "s3cr3t"},
            )
            assert status == 200
            now = time.monotonic()
            assert guard.decide_api_write("POST", "/repos/o/r/issues", now).allow is False
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

        from sunaba import proxy as installed_proxy

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


class TestApiRepoFromPath:
    """Extract owner/repo from a REST API path (#420)."""

    def test_repos_path(self) -> None:
        assert api_repo_from_path("/repos/octocat/hello-world/issues") == "octocat/hello-world"

    def test_lowercases_owner_and_repo(self) -> None:
        assert api_repo_from_path("/repos/Octocat/Hello-World/issues") == "octocat/hello-world"

    def test_non_repos_path_is_none(self) -> None:
        assert api_repo_from_path("/user") is None
        assert api_repo_from_path("/orgs/octocat/repos") is None

    def test_too_short_path_is_none(self) -> None:
        assert api_repo_from_path("/repos/octocat") is None
        assert api_repo_from_path("/") is None


class TestIsGitDataApiPath:
    """The git Objects API path used by publish's API-push fallback (#420)."""

    def test_git_blobs_path(self) -> None:
        assert is_git_data_api_path("/repos/o/r/git/blobs") is True

    def test_git_refs_path(self) -> None:
        assert is_git_data_api_path("/repos/o/r/git/refs/heads/main") is True

    def test_non_git_write_path(self) -> None:
        assert is_git_data_api_path("/repos/o/r/issues") is False
        assert is_git_data_api_path("/repos/o/r/pulls/1/reviews") is False

    def test_too_short_path(self) -> None:
        assert is_git_data_api_path("/repos/o/r") is False


class TestDecideApiWrite:
    """The api.github.com write gate: GET/HEAD pass, writes need a grant (#420)."""

    def test_read_always_passes(self) -> None:
        guard = EgressGuard()  # empty allowlist
        assert guard.decide_api_write("GET", "/repos/o/r/issues", now=100.0).allow is True
        assert guard.decide_api_write("HEAD", "/repos/o/r/issues", now=100.0).allow is True

    def test_write_denied_by_default(self) -> None:
        guard = EgressGuard()
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=100.0)
        assert d.allow is False
        assert "allowlist" in d.reason

    def test_write_to_allowed_repo_without_grant_denied(self) -> None:
        guard = EgressGuard({"o/r"})
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=100.0)
        assert d.allow is False
        assert "authorization grant" in d.reason

    def test_write_allowed_with_open_api_write_grant(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=101.0)
        assert d.allow is True

    def test_write_target_repo_undeterminable_denied(self) -> None:
        guard = EgressGuard()
        d = guard.decide_api_write("POST", "/user", now=100.0)
        assert d.allow is False
        assert "could not be determined" in d.reason

    def test_api_write_grant_expires(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=5.0, now=100.0)
        assert guard.decide_api_write("POST", "/repos/o/r/issues", now=101.0).allow is True
        assert guard.decide_api_write("POST", "/repos/o/r/issues", now=106.0).allow is False

    def test_close_api_write_grant_revokes(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0)
        guard.close_api_write_grant("o/r")
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=101.0)
        assert d.allow is False

    def test_git_data_api_uses_push_grant_not_api_write_grant(self) -> None:
        # publish's API-push fallback already runs inside authorized_push_grant
        # -- an api-write grant alone must not authorize it, and vice versa.
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide_api_write("POST", "/repos/o/r/git/blobs", now=101.0)
        assert d.allow is False
        assert "push-authorization" in d.reason

    def test_git_data_api_allowed_with_push_grant(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30, now=100.0)
        d = guard.decide_api_write("POST", "/repos/o/r/git/blobs", now=101.0)
        assert d.allow is True

    def test_api_write_grant_does_not_authorize_git_push(self) -> None:
        # And the converse: an api-write grant must not widen git push access.
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0)
        assert guard.decide("/o/r.git/git-receive-pack", None, now=101.0).allow is False


class TestApiWriteTokenInjection:
    """Authorization header injection for gated api.github.com writes (#420)."""

    def test_bearer_auth_header_format(self) -> None:
        assert bearer_auth_header("ghs_secret") == "Bearer ghs_secret"

    def test_no_grant_no_token(self) -> None:
        guard = EgressGuard({"o/r"})
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=100.0)
        assert d.allow is False
        assert guard.token_headers_for(d, repo="o/r", is_api_write_request=True) == {}

    def test_api_write_grant_token_injected(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_write")
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=101.0)
        assert guard.token_headers_for(
            d, repo="o/r", now=101.0, is_api_write_request=True
        ) == {"Authorization": bearer_auth_header("ghs_write")}

    def test_git_data_api_uses_push_grant_token(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_push")
        d = guard.decide_api_write("POST", "/repos/o/r/git/blobs", now=101.0)
        assert guard.token_headers_for(
            d, repo="o/r", now=101.0, is_api_write_request=True, use_push_grant=True
        ) == {"Authorization": bearer_auth_header("ghs_push")}

    def test_api_write_grant_token_not_used_for_git_data_api(self) -> None:
        # An api-write-grant token must not leak into the push-scoped git
        # Objects API path, and vice versa -- they read from different tables.
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0, token="ghs_write")
        d = Decision(True, "fabricated allow")
        assert guard.token_headers_for(
            d, repo="o/r", now=101.0, is_api_write_request=True, use_push_grant=True
        ) == {}

    def test_expired_api_write_token_not_injected(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=5.0, now=100.0, token="ghs_write")
        d = Decision(True, "fabricated allow")
        assert (
            guard.token_headers_for(d, repo="o/r", now=106.0, is_api_write_request=True) == {}
        )
        assert guard._api_write_grants == {}


class TestApiWriteControlDispatch:
    """``/auth/allow-api-write`` / ``/auth/revoke-api-write`` (#420)."""

    def test_allow_api_write_opens_grant(self) -> None:
        guard = EgressGuard({"o/r"})
        res = handle_control_request(
            guard,
            None,
            "/auth/allow-api-write",
            None,
            {"repo": "o/r", "ttl_seconds": 30, "token": "ghs_write"},
            now=100.0,
        )
        assert res.status == 200
        assert "ghs_write" not in json.dumps(res.body)
        d = guard.decide_api_write("POST", "/repos/o/r/issues", now=101.0)
        assert d.allow is True
        assert guard.token_headers_for(
            d, repo="o/r", now=101.0, is_api_write_request=True
        ) == {"Authorization": bearer_auth_header("ghs_write")}

    def test_allow_api_write_does_not_authorize_push(self) -> None:
        guard = EgressGuard({"o/r"})
        handle_control_request(
            guard, None, "/auth/allow-api-write", None, {"repo": "o/r"}, now=100.0
        )
        assert guard.decide("/o/r.git/git-receive-pack", None, now=101.0).allow is False

    def test_revoke_api_write_closes_grant(self) -> None:
        guard = EgressGuard({"o/r"})
        guard.open_api_write_grant("o/r", ttl_seconds=30, now=100.0)
        res = handle_control_request(
            guard, None, "/auth/revoke-api-write", None, {"repo": "o/r"}
        )
        assert res.status == 200
        assert guard.decide_api_write("POST", "/repos/o/r/issues", now=101.0).allow is False

    def test_allow_api_write_bad_repo_rejected(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(
            guard, None, "/auth/allow-api-write", None, {"repo": "no-slash"}
        )
        assert res.status == 400

    def test_allow_api_write_non_positive_ttl_rejected(self) -> None:
        guard = EgressGuard()
        res = handle_control_request(
            guard, None, "/auth/allow-api-write", None, {"repo": "o/r", "ttl_seconds": 0}
        )
        assert res.status == 400


class TestBlockBodyHint:
    """403 body hint must match the kind of request denied (#424).

    #420's api-write gate originally reused the push-only hint text for
    every denial, so an issue-comment/PR-review rejection told the caller
    to use ``publish`` -- a tool that has nothing to do with the request.
    """

    def test_default_hint_is_push(self) -> None:
        assert block_body("no open authorization grant for o/r") == (
            b"BLOCKED by egress proxy: no open authorization grant for o/r. "
            + PUSH_BLOCK_HINT.encode()
            + b"\n"
        )

    def test_api_write_hint_mentions_first_class_tools(self) -> None:
        body = block_body("no open api-write authorization grant for o/r", hint=API_WRITE_BLOCK_HINT)
        assert b"publish tool" not in body or b"sandbox_issue_write" in body
        assert API_WRITE_BLOCK_HINT.encode() in body

    def test_api_write_denial_uses_api_write_hint_not_push_hint(self) -> None:
        # The exact bug from #424: a denied api.github.com write must not
        # claim push is the only sanctioned path.
        guard = EgressGuard({"o/r"})
        d = guard.decide_api_write("POST", "/repos/o/r/issues/1/comments", now=100.0)
        assert d.allow is False
        body = block_body(d.reason, hint=API_WRITE_BLOCK_HINT)
        assert b"sandbox_issue_write" in body
        assert b"only allowed via the publish tool" not in body
