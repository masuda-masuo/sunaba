"""Egress proxy addon: gate git push at the network layer (Issue #354, part of #353).

A **mitmproxy addon** loaded by the proxy sidecar via ``mitmdump -s proxy.py``.
It is intentionally *not* imported anywhere in the MCP server and adds no
runtime dependency (``mitmproxy`` lives only in the dedicated proxy image), so
merging it changes no existing behaviour until the sidecar is wired in
(#355 / #358).

Why service names, not HTTP methods
-----------------------------------
git smart-HTTP uses POST for **both** clone and push, so the HTTP method cannot
distinguish them.  The reliable discriminator is the *service name* in the URL
-- the ``?service=`` query on the ref-discovery ``GET /<repo>/info/refs`` or the
trailing ``/git-<service>-pack`` segment on the data ``POST``:

======================  =================
git operation           service name
======================  =================
clone / fetch / pull    git-upload-pack
push                    git-receive-pack
======================  =================

Policy
------
* ``git-upload-pack`` (clone/fetch) and any non-git request pass through.
* ``git-receive-pack`` (push) is **denied by default** and only allowed when
  *both* hold: the target ``owner/repo`` is in the allowlist, *and* a
  short-lived authorization window is currently open for it.  The window is
  opened/closed by the control plane that ``publish`` will drive (#356 / #357);
  this module provides the in-proxy decision core and state, not yet the
  control API transport.

Because push is blocked at ref-discovery -- before any credentials are sent --
the guarantee holds even before tokens are moved out of the container (#356).

Validated by a PoC on 2026-07-01: a real ``git clone`` succeeds through the
TLS-terminating proxy while ``git push`` is rejected.

Still out of scope here (own issues): the control-API server + ``publish``
integration that opens windows (#356 / #357), gating non-push write APIs on
``api.github.com`` -- which this addon still passes through (#360), network
isolation so the proxy is the only egress and SSH is blocked (#355), and
sidecar image packaging + container CA wiring (#358).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

try:  # pragma: no cover - only importable inside the proxy sidecar image
    from mitmproxy import http
except ImportError:
    # The package must import without mitmproxy installed; the mitmproxy glue in
    # EgressGuard.request() only runs under mitmdump, where it is present.
    http = None  # type: ignore[assignment]

#: Service name git uses for push -- the only operation this proxy gates.
PUSH_SERVICE = "git-receive-pack"

#: Service name git uses for clone/fetch -- always allowed through.
FETCH_SERVICE = "git-upload-pack"

_KNOWN_SERVICES = frozenset({FETCH_SERVICE, PUSH_SERVICE})

#: Environment variable holding a comma-separated owner/repo allowlist (#358).
ALLOWED_REPOS_ENV = "CODE_SANDBOX_ALLOWED_REPOS"


def git_service_from_request(path: str, query_service: str | None) -> str | None:
    """Return the git smart-HTTP service name for a request, or ``None``.

    *path* is the request path without the query string; *query_service* is the
    value of the ``service`` query parameter (``None`` if absent).  Matching is
    case-insensitive and only ever returns a **known** service name -- an
    unrecognised ``?service=`` value yields ``None`` rather than being echoed
    back, so callers can trust the return value (addresses PR #362 review).
    Pure function with no mitmproxy dependency, so it is unit-testable alone.
    """
    if query_service:
        svc = query_service.strip().lower()
        return svc if svc in _KNOWN_SERVICES else None
    tail = path.rsplit("/", 1)[-1].lower()
    return tail if tail in _KNOWN_SERVICES else None


def is_push(path: str, query_service: str | None) -> bool:
    """Return ``True`` if the request is a git push (``git-receive-pack``)."""
    return git_service_from_request(path, query_service) == PUSH_SERVICE


def repo_from_path(path: str) -> str | None:
    """Return the ``owner/repo`` targeted by a git smart-HTTP path, or ``None``.

    GitHub paths look like ``/<owner>/<repo>.git/info/refs`` or
    ``/<owner>/<repo>.git/git-receive-pack``; the trailing ``.git`` is stripped.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo:
        return None
    # GitHub repo names are case-insensitive; normalise so allowlist matching
    # is not defeated by URL casing (PR #365 review).
    return f"{owner}/{repo}".lower()


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one request against the egress policy."""

    allow: bool
    reason: str


def block_body(reason: str) -> bytes:
    """Build the plain-text 403 body returned to git for a denied request."""
    return (
        f"BLOCKED by egress proxy: {reason}. Push from the sandbox is only "
        "allowed via the publish tool.\n"
    ).encode()


class EgressGuard:
    """mitmproxy addon holding the allowlist + authorization-window state.

    The decision logic (:meth:`decide`) is pure and takes an explicit *now*, so
    it can be unit-tested without mitmproxy or a real clock.  :meth:`request` is
    the thin mitmproxy hook that maps a denied :class:`Decision` to a 403.

    Repo names are matched case-insensitively (GitHub treats them so): the
    allowlist, the window keys, and :func:`repo_from_path` are all lower-cased.

    Thread safety: the control plane (#356 / #357) will open/close windows from
    a different thread than mitmproxy's event loop, which reads them in
    :meth:`request`.  Window state is therefore guarded by ``self._lock``.
    """

    def __init__(self, allowed_repos: set[str] | None = None) -> None:
        """Create a guard.

        *allowed_repos* is the set of ``owner/repo`` strings that may ever be
        pushed to (matched case-insensitively); anything outside it is denied
        regardless of windows.
        """
        self._allowed: set[str] = {r.lower() for r in (allowed_repos or ())}
        #: repo -> monotonic expiry timestamp of an open push window.
        self._windows: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- authorization window control (to be driven by publish; #356 / #357) --

    def open_window(self, repo: str, ttl_seconds: float, now: float | None = None) -> None:
        """Permit push to *repo* for the next *ttl_seconds* (both git requests).

        *now* defaults to ``time.monotonic()``; pass it explicitly to keep the
        expiry deterministic in tests, mirroring :meth:`decide`.
        """
        base = time.monotonic() if now is None else now
        with self._lock:
            self._windows[repo.lower()] = base + ttl_seconds

    def close_window(self, repo: str) -> None:
        """Revoke any open push window for *repo*."""
        with self._lock:
            self._windows.pop(repo.lower(), None)

    def _window_open(self, repo: str, now: float) -> bool:
        with self._lock:
            expiry = self._windows.get(repo)
        return expiry is not None and now < expiry

    # -- decision core (pure) --

    def decide(self, path: str, query_service: str | None, now: float) -> Decision:
        """Evaluate a request against the policy; only git push is gated."""
        if not is_push(path, query_service):
            return Decision(True, "not a push (clone/fetch/other passes through)")
        repo = repo_from_path(path)
        if repo is None:
            return Decision(False, "push target repo could not be determined")
        if repo not in self._allowed:
            return Decision(False, f"push to {repo} is not in the allowlist")
        if not self._window_open(repo, now):
            return Decision(False, f"no open authorization window for {repo}")
        return Decision(True, f"push authorized for {repo}")

    # -- mitmproxy hook --

    def request(self, flow) -> None:  # pragma: no cover - exercised under mitmdump
        """mitmproxy hook: short-circuit denied requests with a 403 response."""
        if http is None:
            raise RuntimeError(
                "mitmproxy is required to run the egress proxy addon; "
                "load it with 'mitmdump -s proxy.py'"
            )
        path = flow.request.path.split("?", 1)[0]
        query_service = flow.request.query.get("service")
        decision = self.decide(path, query_service, time.monotonic())
        if not decision.allow:
            flow.response = http.Response.make(
                403,
                block_body(decision.reason),
                {"Content-Type": "text/plain"},
            )


def allowed_repos_from_env(environ: dict[str, str] | None = None) -> set[str]:
    """Parse the ``owner/repo`` allowlist from ``CODE_SANDBOX_ALLOWED_REPOS``."""
    env = os.environ if environ is None else environ
    raw = env.get(ALLOWED_REPOS_ENV, "")
    return {r.strip() for r in raw.split(",") if r.strip()}


#: mitmproxy discovers addons via a module-level ``addons`` list.  Built from
#: the environment so the sidecar image (#358) can configure the allowlist.
addons = [EgressGuard(allowed_repos_from_env())]
