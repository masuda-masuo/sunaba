"""Tests for the egress proxy addon service discrimination (Issue #354).

These exercise the pure classifier only, so they run without mitmproxy
installed (the addon guards its ``from mitmproxy import http`` import).
End-to-end behaviour (real clone passes / push blocked through a
TLS-terminating mitmproxy) was validated by a manual PoC; see the module
docstring in ``code_sandbox_mcp.proxy``.
"""
from __future__ import annotations

from code_sandbox_mcp.proxy import (
    FETCH_SERVICE,
    PUSH_SERVICE,
    git_service_from_request,
    is_push,
)


class TestGitServiceDiscrimination:
    """A git push must be identified by service name, not HTTP method."""

    def test_ref_discovery_query_upload_pack(self) -> None:
        # GET /<repo>/info/refs?service=git-upload-pack  (clone/fetch)
        assert git_service_from_request("/o/r.git/info/refs", FETCH_SERVICE) == FETCH_SERVICE
        assert is_push("/o/r.git/info/refs", FETCH_SERVICE) is False

    def test_ref_discovery_query_receive_pack(self) -> None:
        # GET /<repo>/info/refs?service=git-receive-pack  (push discovery)
        assert git_service_from_request("/o/r.git/info/refs", PUSH_SERVICE) == PUSH_SERVICE
        assert is_push("/o/r.git/info/refs", PUSH_SERVICE) is True

    def test_data_path_upload_pack(self) -> None:
        # POST /<repo>/git-upload-pack  (no query string)
        assert git_service_from_request("/o/r.git/git-upload-pack", None) == FETCH_SERVICE
        assert is_push("/o/r.git/git-upload-pack", None) is False

    def test_data_path_receive_pack(self) -> None:
        # POST /<repo>/git-receive-pack  (no query string)
        assert git_service_from_request("/o/r.git/git-receive-pack", None) == PUSH_SERVICE
        assert is_push("/o/r.git/git-receive-pack", None) is True

    def test_query_takes_precedence_over_path(self) -> None:
        # The ref-discovery query is authoritative even on an info/refs path.
        assert git_service_from_request("/o/r.git/info/refs", PUSH_SERVICE) == PUSH_SERVICE

    def test_non_git_request_is_not_a_service(self) -> None:
        # api.github.com REST is currently not classified as push and passes
        # through -- the #360 gap this PoC deliberately leaves open.
        assert git_service_from_request("/repos/o/r/issues", None) is None
        assert is_push("/repos/o/r/issues", None) is False
