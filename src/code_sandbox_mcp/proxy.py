"""Egress proxy addon: gate git push at the network layer (Issue #354, part of #353).

This module is a **mitmproxy addon**, loaded by the proxy sidecar via
``mitmdump -s proxy.py``.  It is intentionally *not* imported anywhere in the
MCP server and adds no runtime dependency (``mitmproxy`` is only present in
the dedicated proxy image), so merging it changes no existing behaviour.

Design (see ``designegressproxygitcontrol.md`` and ``docs/design.md`` §11.1):
git smart-HTTP uses POST for **both** clone and push, so the HTTP method
cannot distinguish them.  The reliable discriminator is the *service name*
carried in the URL -- either the ``?service=`` query on the ref-discovery
request (``GET /<repo>/info/refs``) or the trailing path segment on the data
request (``POST /<repo>/git-<service>-pack``):

======================  =================
git operation           service name
======================  =================
clone / fetch / pull    git-upload-pack
push                    git-receive-pack
======================  =================

``git-receive-pack`` (push) is blocked; ``git-upload-pack`` (clone/fetch)
passes through.  Blocking happens at ref-discovery, *before* any credentials
are sent, so no token is required to enforce it.

Validated by a PoC on 2026-07-01: a real ``git clone`` succeeds through the
TLS-terminating proxy while ``git push`` is rejected with ``BLOCK_MESSAGE``.

Deliberately out of scope here (later issues):

* per-repo allowlist + the short-lived authorization window that lets
  ``publish`` open push for one repo (#356 / #357),
* non-push write-API gating for ``api.github.com`` REST/GraphQL, which this
  addon currently lets through (#360),
* network isolation so the proxy is the only egress and SSH is blocked (#355),
* packaging the addon into a sidecar image and wiring the container CA (#358).
"""
from __future__ import annotations

try:  # pragma: no cover - only importable inside the proxy sidecar image
    from mitmproxy import http
except ImportError:
    # The package must import without mitmproxy installed; the addon glue in
    # request() only runs under mitmdump, where mitmproxy is always present.
    http = None  # type: ignore[assignment]

#: Service name git uses for push -- the only operation this proxy blocks.
PUSH_SERVICE = "git-receive-pack"

#: Service name git uses for clone/fetch -- always allowed through.
FETCH_SERVICE = "git-upload-pack"

#: Body returned to git when a push is blocked.
BLOCK_MESSAGE = (
    b"BLOCKED by egress proxy: git-receive-pack (push) is not allowed "
    b"from the sandbox. Use the publish tool.\n"
)


def git_service_from_request(path: str, query_service: str | None) -> str | None:
    """Return the git smart-HTTP service name for a request, or ``None``.

    *path* is the request path without the query string; *query_service* is
    the value of the ``service`` query parameter (``None`` if absent).  Pure
    function with no mitmproxy dependency so it is unit-testable on its own.
    """
    if query_service:
        return query_service
    tail = path.rsplit("/", 1)[-1]
    if tail in (FETCH_SERVICE, PUSH_SERVICE):
        return tail
    return None


def is_push(path: str, query_service: str | None) -> bool:
    """Return ``True`` if the request is a git push (``git-receive-pack``)."""
    return git_service_from_request(path, query_service) == PUSH_SERVICE


def request(flow) -> None:  # pragma: no cover - exercised only under mitmdump
    """mitmproxy request hook: short-circuit push with a 403 block response."""
    assert http is not None  # guaranteed inside the proxy sidecar image
    path = flow.request.path.split("?", 1)[0]
    query_service = flow.request.query.get("service")
    if is_push(path, query_service):
        flow.response = http.Response.make(
            403,
            BLOCK_MESSAGE,
            {"Content-Type": "text/plain"},
        )
