"""Egress proxy addon: gate git push at the network layer (Issue #354, part of #353).

A **mitmproxy addon** loaded by the proxy sidecar via ``mitmdump -s proxy.py``.
Its addon behaviour runs only under ``mitmdump`` and adds no runtime dependency
(``mitmproxy`` is imported lazily, so this module imports fine without it).  The
host-side ``publish`` client (#357) imports only the wire-contract constants
below, and no addon behaviour runs at import time, so nothing changes until the
sidecar is wired in (#355 / #358).

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
  short-lived authorization grant is currently open for it.  The grant is
  opened/closed via the internal control API (:class:`AuthControlServer`,
  ``POST /auth/allow`` / ``/auth/revoke``) that ``publish`` drives host-side
  (#356 / #357), authenticated by a shared secret the container never learns.

Because push is blocked at ref-discovery -- before any credentials are sent --
the guarantee holds even before tokens are moved out of the container (#356).

Validated by a PoC on 2026-07-01: a real ``git clone`` succeeds through the
TLS-terminating proxy while ``git push`` is rejected.

Non-push write APIs on ``api.github.com`` (issue/PR create, comments, reviews,
and the git Objects API used by ``publish``'s API-push fallback) are gated the
same way (#420): GET/HEAD always pass through, and POST/PUT/PATCH/DELETE are
denied by default -- allowed only inside an explicit authorization grant,
opened host-side via the control API just like a push.  The git Objects API
under ``/repos/<owner>/<repo>/git/...`` reuses the *push* grant (it already
runs inside ``authorized_push_grant`` in ``publish``, being the push
fallback transport itself); every other write path gets its own separate
"api write" grant (``/auth/allow-api-write`` / ``/auth/revoke-api-write``),
mirroring the read/push separation from #419 so opening one write grant
never widens another kind of access.

Still out of scope here (own issue): dropping the token from the container
env now that the proxy can inject it (#356 remainder).  The sidecar *image*
that runs this addon is built by
``docker/Dockerfile.proxy`` via ``proxy_entrypoint.py``, and its container
lifecycle -- starting the sidecar, joining sandboxes to the internal network
(the only egress route, #355/#358), and installing this proxy's CA into the
sandbox trust store -- is handled host-side by
:mod:`code_sandbox_mcp.proxy_lifecycle`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# mitmproxy's script loader (``mitmdump -s proxy.py``) execs this file as a
# module but does not register it in ``sys.modules`` under its ``__name__``.
# Under ``from __future__ import annotations`` every dataclass field annotation
# is a string, and ``dataclasses`` resolves it via
# ``sys.modules[cls.__module__].__dict__`` -- which is ``None`` here, crashing
# the first ``@dataclass`` below and preventing the addon from loading at all
# (found by the #358 sidecar smoke test).  Registering ourselves makes that
# lookup find a real module; on normal package import the entry already exists,
# so this is a no-op.
#
# NOTE: _self_module.__dict__ only captures globals at registration time, so a
# dataclass field annotation naming a module-level type defined *below* this
# point would not resolve.  Currently safe: every dataclass field here uses
# builtin types only.  Keep it that way, or refresh the registered dict.
if sys.modules.get(__name__) is None:  # pragma: no cover - only under mitmdump
    import types as _types

    _self_module = _types.ModuleType(__name__)
    _self_module.__dict__.update(globals())
    sys.modules[__name__] = _self_module

try:  # pragma: no cover - only importable inside the proxy sidecar image
    # mitmproxy is intentionally NOT a package dependency (it lives only in the
    # proxy sidecar image), so the resolver cannot see it here (#387).
    from mitmproxy import http  # pyright: ignore[reportMissingImports]
except ImportError:
    # The package must import without mitmproxy installed; the mitmproxy glue in
    # EgressGuard.request() only runs under mitmdump, where it is present.
    http = None  # type: ignore[assignment]

#: Service name git uses for push -- the only operation this proxy gates.
PUSH_SERVICE = "git-receive-pack"

#: Service name git uses for clone/fetch -- always allowed through.
FETCH_SERVICE = "git-upload-pack"

_KNOWN_SERVICES = frozenset({FETCH_SERVICE, PUSH_SERVICE})

#: Hostname of the GitHub REST/GraphQL API, gated separately from git
#: smart-HTTP traffic (#420) -- ``git_service_from_request`` never matches
#: it, since it carries no ``service`` marker of its own.
API_HOST = "api.github.com"

#: HTTP methods on ``api.github.com`` treated as writes and gated by an
#: authorization grant (#420).  GET/HEAD (and anything else) always pass.
API_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Environment variable holding a comma-separated owner/repo allowlist (#358).
ALLOWED_REPOS_ENV = "CODE_SANDBOX_ALLOWED_REPOS"

#: Environment variable holding a comma-separated **destination-host**
#: allowlist (#506).  This is orthogonal to :data:`ALLOWED_REPOS_ENV`, which
#: is a *write-target* allowlist: this one governs which hosts the sandbox may
#: reach at all.  Entries *extend* the always-on :data:`DEFAULT_EGRESS_HOSTS`;
#: an entry beginning with ``.`` matches that domain and its subdomains
#: (``.example.com`` -> ``example.com`` and ``a.example.com``).  The special
#: single value ``*`` disables destination-host containment entirely (any host
#: passes), restoring the pre-#506 passthrough behaviour for operators who
#: need it.
ALLOWED_EGRESS_HOSTS_ENV = "CODE_SANDBOX_ALLOWED_EGRESS_HOSTS"

#: Sentinel in the egress-host allowlist meaning "allow any destination host".
EGRESS_HOST_WILDCARD = "*"

#: Destination hosts always reachable under the egress proxy, independent of
#: user configuration: GitHub (git smart-HTTP, REST API, archive downloads,
#: and the ``*.githubusercontent.com`` hosts serving raw files / release
#: assets) plus the Python, Node, and Go package registries, so ``pip`` /
#: ``npm`` / ``go install`` keep working under default-deny (#506).
#: Operators extend this set via :data:`ALLOWED_EGRESS_HOSTS_ENV`; they
#: cannot shrink it (the built-ins are what make the proxy usable as a dev
#: sandbox out of the box).
DEFAULT_EGRESS_HOSTS: frozenset[str] = frozenset({
    "github.com",
    "api.github.com",
    "codeload.github.com",
    ".githubusercontent.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "proxy.golang.org",
    "sum.golang.org",
})

#: Static fallback push token injected into authorized pushes.  The primary
#: path is the grant-scoped token ``publish`` hands over on ``/auth/allow``
#: (#356), which needs no sidecar configuration and never outlives its grant;
#: this env var remains for operators who prefer a sidecar-held credential.
#: Unset = no static injection.
PROXY_TOKEN_ENV = "CODE_SANDBOX_PROXY_TOKEN"

#: TCP port for the internal authorization control API; unset = decision-only
#: proxy (no grant can be opened, matching today's inert behaviour).
CONTROL_PORT_ENV = "CODE_SANDBOX_PROXY_CONTROL_PORT"

#: Bind address for the control API (default ``127.0.0.1``).  The sidecar
#: (#358) sets ``0.0.0.0`` so the host can reach the port Docker publishes;
#: a non-loopback bind **requires** the shared secret, because inside the
#: sidecar the control port is reachable from the sandbox-facing Docker
#: network and must never accept unauthenticated grant requests.
CONTROL_HOST_ENV = "CODE_SANDBOX_PROXY_CONTROL_HOST"

#: Shared secret authenticating control-API callers (#356 / #357).  ``publish``
#: sends it in the ``X-Control-Token`` header; the sandbox container never sees
#: it, so it cannot open its own push grant.
CONTROL_SECRET_ENV = "CODE_SANDBOX_PROXY_CONTROL_SECRET"

#: Request header carrying the control secret on ``/auth/*`` calls.
CONTROL_TOKEN_HEADER = "X-Control-Token"

#: Authorization-grant lifetime used when a caller omits ``ttl_seconds``.
DEFAULT_GRANT_TTL_SECONDS = 30.0

#: Cap on the control-request body; payloads are tiny JSON, so anything larger
#: is rejected (413) unread, bounding handler memory (review of PR #367).
MAX_CONTROL_BODY_BYTES = 4096

#: Files baked into the sidecar image (``docker/Dockerfile.proxy``) whose bytes
#: define the addon's behaviour.  Their combined hash is the "source
#: fingerprint" compared host-side against the installed package to catch a
#: sidecar running a different ``proxy.py`` than the server expects (#405).
#: The #404 incident -- a venv on new Basic-auth ``proxy.py`` against a sidecar
#: baked from the old Bearer ``proxy.py`` -- silently broke every push; a
#: fingerprint mismatch surfaces that at sandbox start instead.
_FINGERPRINT_FILENAMES = ("proxy.py", "proxy_entrypoint.py")


def _compute_source_fingerprint() -> str:
    """Hash the addon source files next to this module, order-independent.

    Resolves each name in :data:`_FINGERPRINT_FILENAMES` relative to this
    file's directory -- ``/app`` inside the sidecar, the installed package dir
    host-side -- so the same bytes produce the same digest on both ends.  A
    missing file hashes to a fixed ``MISSING`` marker rather than raising, so
    the fingerprint stays deterministic (and a file present on only one side
    still diverges).  Returns ``""`` if the source dir cannot be read at all,
    which callers treat as "cannot compare" (skip the check).
    """
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except OSError:
        return ""
    outer = hashlib.sha256()
    for name in _FINGERPRINT_FILENAMES:
        outer.update(name.encode())
        outer.update(b"\0")
        try:
            with open(os.path.join(here, name), "rb") as fh:
                outer.update(hashlib.sha256(fh.read()).hexdigest().encode())
        except OSError:
            outer.update(b"MISSING")
        outer.update(b"\n")
    return outer.hexdigest()


#: Digest of this sidecar's baked addon source, computed once at import.  The
#: sidecar serves it over ``POST /version`` (secret-gated) and the host
#: compares it against its own installed copy after starting the sidecar (#405).
PROXY_SOURCE_FINGERPRINT = _compute_source_fingerprint()


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


def api_repo_from_path(path: str) -> str | None:
    """Return the ``owner/repo`` targeted by a REST API path, or ``None``.

    GitHub REST paths are ``/repos/<owner>/<repo>/...``; anything else (user
    endpoints, GraphQL, etc.) has no single repo target and yields ``None``,
    so callers such as :meth:`EgressGuard.decide_api_write` deny it rather
    than guess.  Lower-cased like :func:`repo_from_path`, for the same
    case-insensitive-matching reason.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 3 or parts[0].lower() != "repos":
        return None
    owner, repo = parts[1], parts[2]
    if not owner or not repo:
        return None
    return f"{owner}/{repo}".lower()


def is_git_data_api_path(path: str) -> bool:
    """Return ``True`` for a git Objects API path (``/repos/<o>/<r>/git/...``).

    This is the REST transport ``publish``'s ``_try_api_push`` fallback uses
    to create blobs/trees/commits/refs when a plain ``git push`` fails -- it
    is push in every meaningful sense, just carried over ``api.github.com``
    instead of the smart-HTTP endpoint, and already runs inside
    ``authorized_push_grant`` in ``publish``.  :meth:`EgressGuard.decide_api_write`
    therefore checks the *push* grant for these paths instead of the
    separate api-write grant that gates every other REST write (#420).
    """
    parts = [p for p in path.split("/") if p]
    return len(parts) >= 4 and parts[0].lower() == "repos" and parts[3].lower() == "git"


@dataclass(frozen=True)
class Decision:
    """Outcome of evaluating one request against the egress policy."""

    allow: bool
    reason: str


def basic_auth_header(token: str) -> str:
    """Build the ``Authorization`` value GitHub's git endpoint accepts.

    git-over-HTTPS on github.com rejects ``Bearer <token>`` with 401 and
    requires HTTP Basic with the token as the password (verified 2026-07-03:
    Bearer -> 401, ``x-access-token:<token>`` Basic -> 200 on
    ``info/refs?service=git-receive-pack``).  The ``x-access-token``
    username matches what publish's credential helper uses for the same
    transport.
    """
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"Basic {credentials}"


def bearer_auth_header(token: str) -> str:
    """Build the ``Authorization`` value ``api.github.com`` REST calls accept.

    Unlike git-over-HTTPS (:func:`basic_auth_header`), GitHub's REST API
    removed HTTP Basic auth in 2021 and expects a bearer-style token (#420).
    """
    return f"Bearer {token}"


#: Default 403 hint: which first-class tool to use instead of a raw push.
PUSH_BLOCK_HINT = "Push from the sandbox is only allowed via the publish tool."

#: 403 hint for a denied api.github.com write (#424): the push-specific
#: default above is misleading for e.g. an issue comment or PR review, which
#: never went through ``publish`` in the first place.
API_WRITE_BLOCK_HINT = (
    "Non-push writes to api.github.com from the sandbox are only allowed via "
    "first-class tools such as sandbox_issue_write or publish."
)

#: 403 hint for a host blocked by destination-host containment (#506): the
#: push/api hints above are irrelevant when the host itself is off-allowlist.
EGRESS_HOST_BLOCK_HINT = (
    "The sandbox may only reach allowlisted hosts (GitHub and the package "
    "registries by default); add the host to CODE_SANDBOX_ALLOWED_EGRESS_HOSTS "
    "if it is legitimately needed."
)


def block_body(reason: str, hint: str = PUSH_BLOCK_HINT) -> bytes:
    """Build the plain-text 403 body returned to the client for a denied request.

    *hint* names the sanctioned alternative; defaults to the push-tool hint
    for backward compatibility, but callers gating a different kind of
    request (e.g. :data:`API_WRITE_BLOCK_HINT`) should pass their own (#424).
    """
    return f"BLOCKED by egress proxy: {reason}. {hint}\n".encode()


class EgressGuard:
    """mitmproxy addon holding the allowlist + authorization-grant state.

    The decision logic (:meth:`decide`) is pure and takes an explicit *now*, so
    it can be unit-tested without mitmproxy or a real clock.  :meth:`request` is
    the thin mitmproxy hook that maps a denied :class:`Decision` to a 403.

    Repo names are matched case-insensitively (GitHub treats them so): the
    allowlist, the grant keys, and :func:`repo_from_path` are all lower-cased.

    Thread safety: the control plane (#356 / #357) will open/close grants from
    a different thread than mitmproxy's event loop, which reads them in
    :meth:`request`.  Grant state is therefore guarded by ``self._lock``.
    """

    def __init__(
        self,
        allowed_repos: set[str] | None = None,
        token: str | None = None,
        allowed_egress_hosts: set[str] | None = None,
    ) -> None:
        """Create a guard.

        *allowed_repos* is the set of ``owner/repo`` strings that may ever be
        pushed to (matched case-insensitively); anything outside it is denied
        regardless of grants.  *token*, when given, is the push token the
        proxy injects into authorized pushes so the container need not hold
        GitHub credentials (#356).

        *allowed_egress_hosts* is the set of **destination hosts** the sandbox
        may reach (#506); it is unioned with the always-on
        :data:`DEFAULT_EGRESS_HOSTS` so the built-in registries can never be
        configured away.  ``None`` (the default) means "built-ins only".  A
        set containing :data:`EGRESS_HOST_WILDCARD` disables destination-host
        containment.  This is orthogonal to *allowed_repos*: a host being
        reachable says nothing about whether a push to it is authorized.
        """
        self._allowed: set[str] = {r.lower() for r in (allowed_repos or ())}
        extra = {h.lower() for h in (allowed_egress_hosts or ())}
        #: Destination-host allowlist (#506): built-ins unioned with operator
        #: additions.  ``EGRESS_HOST_WILDCARD`` anywhere in the set means
        #: "allow any host" (checked in :meth:`decide_host`).
        self._allowed_hosts: frozenset[str] = DEFAULT_EGRESS_HOSTS | extra
        #: repo -> (monotonic expiry, grant-scoped push token or ``None``).
        #: The token travels with the grant (#356): ``publish`` hands it over
        #: on ``/auth/allow`` and it is dropped on revoke/expiry, so the proxy
        #: never holds a credential longer than one authorized push.
        self._grants: dict[str, tuple[float, str | None]] = {}
        #: Same shape as ``_grants`` but for **read** (clone/fetch,
        #: ``git-upload-pack``) authorization (#419).  Deliberately a
        #: separate table: a read grant must never make :meth:`decide`
        #: treat a repo as push-authorized, so the two are never merged.
        self._read_grants: dict[str, tuple[float, str | None]] = {}
        #: Same shape again, for non-push writes on ``api.github.com`` (issue
        #: create/comment, reviews, labels, releases -- #420).  Kept separate
        #: from both ``_grants`` and ``_read_grants`` for the same reason:
        #: opening this grant must never widen git push or read access, and
        #: a push grant must not silently double as blanket API-write
        #: authorization beyond the git Objects API paths (see
        #: :func:`is_git_data_api_path`, which intentionally checks the push
        #: grant instead of this one).
        self._api_write_grants: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()
        #: static fallback push token; ``None`` disables it.  A grant-scoped
        #: token, when present, takes precedence.
        self._token = token

    # -- authorization grant control (to be driven by publish; #356 / #357) --

    def open_grant(
        self,
        repo: str,
        ttl_seconds: float,
        now: float | None = None,
        token: str | None = None,
    ) -> None:
        """Permit push to *repo* for the next *ttl_seconds* (both git requests).

        *now* defaults to ``time.monotonic()``; pass it explicitly to keep the
        expiry deterministic in tests, mirroring :meth:`decide`.  Opening a
        grant for a repo outside the allowlist is harmless -- :meth:`decide`
        still denies the push -- so callers need not pre-check membership.

        *token*, when given, is a grant-scoped push credential injected
        into the authorized push (#356).  It lives and dies with the grant:
        revoke or expiry discards it, so the proxy holds no long-lived token.
        """
        base = time.monotonic() if now is None else now
        with self._lock:
            self._grants[repo.lower()] = (base + ttl_seconds, token)

    def close_grant(self, repo: str) -> None:
        """Revoke any open push grant for *repo* (dropping its token, if any)."""
        with self._lock:
            self._grants.pop(repo.lower(), None)

    def _grant_open(self, repo: str, now: float) -> bool:
        with self._lock:
            entry = self._grants.get(repo)
            if entry is None:
                return False
            expiry, _token = entry
            if now >= expiry:
                # Scrub eagerly: a grant-scoped token must not sit in memory
                # past its authorization (#356).
                self._grants.pop(repo, None)
                return False
        return True

    def _grant_token(self, repo: str | None, now: float) -> str | None:
        """Return the unexpired grant-scoped token for *repo*, or ``None``.

        Scrubs an expired entry just like :meth:`_grant_open` (PR #402
        review): callers such as :meth:`token_headers_for` are usable on
        their own, so neither read path may leave an expired token behind.
        """
        if repo is None:
            return None
        with self._lock:
            entry = self._grants.get(repo.lower())
            if entry is None:
                return None
            expiry, token = entry
            if now >= expiry:
                self._grants.pop(repo.lower(), None)
                return None
        return token

    def open_read_grant(
        self,
        repo: str,
        ttl_seconds: float,
        now: float | None = None,
        token: str | None = None,
    ) -> None:
        """Permit an authenticated clone/fetch of *repo* for *ttl_seconds* (#419).

        Unlike :meth:`open_grant`, this never authorizes a push -- it only
        controls whether :meth:`token_headers_for` injects credentials into a
        ``git-upload-pack`` (clone/fetch) request.  Kept in a separate table
        from the push grants so opening one can never widen push access.
        """
        base = time.monotonic() if now is None else now
        with self._lock:
            self._read_grants[repo.lower()] = (base + ttl_seconds, token)

    def close_read_grant(self, repo: str) -> None:
        """Revoke any open read grant for *repo* (dropping its token, if any)."""
        with self._lock:
            self._read_grants.pop(repo.lower(), None)

    def _read_grant_token(self, repo: str | None, now: float) -> str | None:
        """Return the unexpired read-grant token for *repo*, or ``None``.

        Scrubs an expired entry eagerly, mirroring :meth:`_grant_token`.
        """
        if repo is None:
            return None
        with self._lock:
            entry = self._read_grants.get(repo.lower())
            if entry is None:
                return None
            expiry, token = entry
            if now >= expiry:
                self._read_grants.pop(repo.lower(), None)
                return None
        return token

    def open_api_write_grant(
        self,
        repo: str,
        ttl_seconds: float,
        now: float | None = None,
        token: str | None = None,
    ) -> None:
        """Permit a non-push write to *repo* on ``api.github.com`` (#420).

        Covers issue/PR create, comments, reviews, labels, releases, etc.
        -- everything except the git Objects API paths, which reuse the push
        grant instead (see :func:`is_git_data_api_path`).  Kept in its own
        table so opening it can never authorize a push or a read.
        """
        base = time.monotonic() if now is None else now
        with self._lock:
            self._api_write_grants[repo.lower()] = (base + ttl_seconds, token)

    def close_api_write_grant(self, repo: str) -> None:
        """Revoke any open api-write grant for *repo* (dropping its token)."""
        with self._lock:
            self._api_write_grants.pop(repo.lower(), None)

    def _api_write_grant_open(self, repo: str, now: float) -> bool:
        with self._lock:
            entry = self._api_write_grants.get(repo)
            if entry is None:
                return False
            expiry, _token = entry
            if now >= expiry:
                self._api_write_grants.pop(repo, None)
                return False
        return True

    def _api_write_grant_token(self, repo: str | None, now: float) -> str | None:
        """Return the unexpired api-write grant token for *repo*, or ``None``."""
        if repo is None:
            return None
        with self._lock:
            entry = self._api_write_grants.get(repo.lower())
            if entry is None:
                return None
            expiry, token = entry
            if now >= expiry:
                self._api_write_grants.pop(repo.lower(), None)
                return None
        return token

    # -- decision core (pure) --

    def decide_host(self, host: str) -> Decision:
        """Evaluate a request's **destination host** against the allowlist (#506).

        This is the first gate every request passes through: a host outside
        the allowlist is denied outright, so the proxy is a default-deny
        egress point rather than a passthrough that only inspects git pushes.
        An allowlist entry beginning with ``.`` matches that domain and any
        subdomain; :data:`EGRESS_HOST_WILDCARD` disables the gate.  Pure and
        free of grants/clock, so it is unit-testable in isolation.
        """
        h = host.lower()
        if EGRESS_HOST_WILDCARD in self._allowed_hosts:
            return Decision(True, "destination-host containment disabled (*)")
        for entry in self._allowed_hosts:
            if entry.startswith("."):
                if h == entry[1:] or h.endswith(entry):
                    return Decision(True, f"{h} matches allowed domain {entry}")
            elif h == entry:
                return Decision(True, f"{h} is in the egress host allowlist")
        return Decision(False, f"egress to {h or '(unknown host)'} is not in the allowlist")

    def decide(self, path: str, query_service: str | None, now: float) -> Decision:
        """Evaluate a request against the policy; only git push is gated."""
        if not is_push(path, query_service):
            return Decision(True, "not a push (clone/fetch/other passes through)")
        repo = repo_from_path(path)
        if repo is None:
            return Decision(False, "push target repo could not be determined")
        if repo not in self._allowed:
            return Decision(False, f"push to {repo} is not in the allowlist")
        if not self._grant_open(repo, now):
            return Decision(False, f"no open authorization grant for {repo}")
        return Decision(True, f"push authorized for {repo}")

    def decide_api_write(self, method: str, path: str, now: float) -> Decision:
        """Evaluate an ``api.github.com`` request against the write policy (#420).

        GET/HEAD (and anything else outside :data:`API_WRITE_METHODS`) always
        pass.  A write needs its target repo in the allowlist *and* an open
        authorization grant: the git Objects API paths
        (:func:`is_git_data_api_path`) check the push grant -- they are
        publish's push fallback transport and already run inside
        ``authorized_push_grant`` -- everything else checks the separate
        api-write grant opened via ``/auth/allow-api-write``.
        """
        if method.upper() not in API_WRITE_METHODS:
            return Decision(True, "read-only GitHub API request (GET/HEAD passes through)")
        repo = api_repo_from_path(path)
        if repo is None:
            return Decision(False, "write target repo could not be determined")
        if repo not in self._allowed:
            return Decision(False, f"write to {repo} is not in the allowlist")
        if is_git_data_api_path(path):
            if not self._grant_open(repo, now):
                return Decision(False, f"no open push-authorization grant for {repo}")
            return Decision(True, f"git data API write authorized for {repo} (push grant)")
        if not self._api_write_grant_open(repo, now):
            return Decision(False, f"no open api-write authorization grant for {repo}")
        return Decision(True, f"api write authorized for {repo}")

    # -- token injection (pure) --

    def token_headers_for(
        self,
        decision: Decision,
        is_push_request: bool = False,
        repo: str | None = None,
        now: float | None = None,
        is_fetch_request: bool = False,
        is_api_write_request: bool = False,
        use_push_grant: bool = False,
    ) -> dict[str, str]:
        """Return the ``Authorization`` header to add to an authorized request.

        Empty unless the request is *allowed* **and** a token is held for its
        kind: only pushes/fetches/API-writes the policy permits get
        credentials, and with no token (the inert default) nothing is
        injected.

        For a push, a grant-scoped token for *repo* (handed over on
        ``/auth/allow``, #356) takes precedence over the static
        ``CODE_SANDBOX_PROXY_TOKEN`` fallback.  For a fetch/clone
        (``is_fetch_request``), only an explicit read-grant token
        (``/auth/allow-read``, #419) is used -- there is no static fallback,
        since an always-on read credential would authenticate every clone the
        container makes, not just ones the host explicitly authorized.  For an
        ``api.github.com`` write (``is_api_write_request``, #420), the token
        comes from the push grant when ``use_push_grant`` is set (the git
        Objects API paths -- see :func:`is_git_data_api_path`) or otherwise
        from the separate api-write grant; either way the header uses
        :func:`bearer_auth_header`, since the REST API does not accept Basic.
        Pure, so the injection policy is unit-testable without mitmproxy.
        """
        if not decision.allow:
            return {}
        base = time.monotonic() if now is None else now
        if is_push_request:
            token = self._grant_token(repo, base) or self._token
            return {"Authorization": basic_auth_header(token)} if token else {}
        if is_fetch_request:
            token = self._read_grant_token(repo, base)
            return {"Authorization": basic_auth_header(token)} if token else {}
        if is_api_write_request:
            token = (
                self._grant_token(repo, base)
                if use_push_grant
                else self._api_write_grant_token(repo, base)
            )
            return {"Authorization": bearer_auth_header(token)} if token else {}
        return {}

    # -- mitmproxy lifecycle: internal control API (#356 / #357) --

    def running(self) -> None:  # pragma: no cover - mitmproxy lifecycle hook
        """Start the authorization control server if the env configures a port.

        Reads the port/secret from the environment so the sidecar image (#358)
        can enable it; with no port the proxy stays decision-only (as today).
        """
        try:
            bind = control_bind_from_env(dict(os.environ))
        except ValueError as e:
            # Fail closed: with no control server no grant can ever open, so
            # every push stays denied -- but say why instead of dying silently.
            print(f"egress proxy control API not started: {e}", file=sys.stderr)
            return
        if bind is None:
            return
        host, port, secret = bind
        self._control = AuthControlServer(self, host=host, port=port, secret=secret)
        self._control.start()

    def done(self) -> None:  # pragma: no cover - mitmproxy lifecycle hook
        """Stop the control server on proxy shutdown, if one was started."""
        control = getattr(self, "_control", None)
        if control is not None:
            control.stop()

    # -- mitmproxy hook --

    def request(self, flow) -> None:  # pragma: no cover - exercised under mitmdump
        """mitmproxy hook: short-circuit denied requests with a 403 response."""
        if http is None:
            raise RuntimeError(
                "mitmproxy is required to run the egress proxy addon; "
                "load it with 'mitmdump -s proxy.py'"
            )
        path = flow.request.path.split("?", 1)[0]
        host = (flow.request.pretty_host or "").lower()
        now = time.monotonic()
        # Destination-host containment (#506): the first gate.  A host outside
        # the allowlist is refused here, before any git/API-specific logic, so
        # arbitrary HTTPS GET exfil to an unknown host is blocked -- not just
        # git pushes.  The GitHub hosts and package registries the later
        # branches rely on are in DEFAULT_EGRESS_HOSTS, so they pass through.
        host_decision = self.decide_host(host)
        if not host_decision.allow:
            flow.response = http.Response.make(
                403,
                block_body(host_decision.reason, hint=EGRESS_HOST_BLOCK_HINT),
                {"Content-Type": "text/plain"},
            )
            return
        if host == API_HOST:
            method = flow.request.method
            decision = self.decide_api_write(method, path, now)
            if not decision.allow:
                flow.response = http.Response.make(
                    403,
                    block_body(decision.reason, hint=API_WRITE_BLOCK_HINT),
                    {"Content-Type": "text/plain"},
                )
                return
            if method.upper() in API_WRITE_METHODS:
                repo = api_repo_from_path(path)
                for name, value in self.token_headers_for(
                    decision,
                    repo=repo,
                    is_api_write_request=True,
                    use_push_grant=is_git_data_api_path(path),
                ).items():
                    flow.request.headers[name] = value
            return
        query_service = flow.request.query.get("service")
        decision = self.decide(path, query_service, now)
        if not decision.allow:
            flow.response = http.Response.make(
                403,
                block_body(decision.reason),
                {"Content-Type": "text/plain"},
            )
            return
        service = git_service_from_request(path, query_service)
        for name, value in self.token_headers_for(
            decision,
            is_push_request=service == PUSH_SERVICE,
            repo=repo_from_path(path),
            is_fetch_request=service == FETCH_SERVICE,
        ).items():
            flow.request.headers[name] = value


@dataclass(frozen=True)
class ControlResult:
    """Outcome of a control-API request: an HTTP status and a JSON-able body."""

    status: int
    body: dict[str, object]


def handle_control_request(
    guard: EgressGuard,
    secret: str | None,
    path: str,
    provided_secret: str | None,
    payload: object,
    now: float | None = None,
) -> ControlResult:
    """Dispatch one authorization-control request against *guard* (pure).

    Authenticates the caller against *secret* with a constant-time compare --
    the sandboxed container never learns the secret, so it cannot open its own
    push grant -- then opens/closes a push grant (``/auth/allow`` /
    ``/auth/revoke``), a read grant (``/auth/allow-read`` / ``/auth/revoke-read``,
    #419), or an api-write grant (``/auth/allow-api-write`` /
    ``/auth/revoke-api-write``, #420) for ``payload["repo"]``.  Also answers
    ``/version`` with this sidecar's baked source fingerprint (#405), which
    takes no repo.  Kept free of socket/HTTP glue so the protocol is
    unit-testable on its own.
    """
    if secret is not None and not hmac.compare_digest(provided_secret or "", secret):
        return ControlResult(403, {"error": "invalid or missing control secret"})
    # Read-only source-fingerprint probe (#405): no repo/payload, so answer it
    # before the grant-oriented validation below.  Stays secret-gated -- the
    # fingerprint is non-sensitive, but the control plane authenticates every
    # request and there is no reason to carve out an exception.
    if path == "/version":
        return ControlResult(200, {"proxy_fingerprint": PROXY_SOURCE_FINGERPRINT})
    if not isinstance(payload, dict):
        return ControlResult(400, {"error": "request body must be a JSON object"})
    repo = payload.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        return ControlResult(400, {"error": "'repo' must be an 'owner/name' string"})
    if path == "/auth/allow":
        ttl = payload.get("ttl_seconds", DEFAULT_GRANT_TTL_SECONDS)
        # bool is an int subclass; reject it so True/False is not read as a TTL.
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            return ControlResult(400, {"error": "'ttl_seconds' must be a positive number"})
        # Optional grant-scoped push credential (#356).  Never echoed back:
        # the response must stay safe to log.
        grant_token = payload.get("token")
        if grant_token is not None and not isinstance(grant_token, str):
            return ControlResult(400, {"error": "'token' must be a string when present"})
        guard.open_grant(repo, float(ttl), now=now, token=grant_token or None)
        return ControlResult(200, {"ok": True, "repo": repo.lower(), "ttl_seconds": float(ttl)})
    if path == "/auth/revoke":
        guard.close_grant(repo)
        return ControlResult(200, {"ok": True, "repo": repo.lower()})
    if path == "/auth/allow-read":
        ttl = payload.get("ttl_seconds", DEFAULT_GRANT_TTL_SECONDS)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            return ControlResult(400, {"error": "'ttl_seconds' must be a positive number"})
        grant_token = payload.get("token")
        if grant_token is not None and not isinstance(grant_token, str):
            return ControlResult(400, {"error": "'token' must be a string when present"})
        guard.open_read_grant(repo, float(ttl), now=now, token=grant_token or None)
        return ControlResult(200, {"ok": True, "repo": repo.lower(), "ttl_seconds": float(ttl)})
    if path == "/auth/revoke-read":
        guard.close_read_grant(repo)
        return ControlResult(200, {"ok": True, "repo": repo.lower()})
    if path == "/auth/allow-api-write":
        ttl = payload.get("ttl_seconds", DEFAULT_GRANT_TTL_SECONDS)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            return ControlResult(400, {"error": "'ttl_seconds' must be a positive number"})
        grant_token = payload.get("token")
        if grant_token is not None and not isinstance(grant_token, str):
            return ControlResult(400, {"error": "'token' must be a string when present"})
        guard.open_api_write_grant(repo, float(ttl), now=now, token=grant_token or None)
        return ControlResult(200, {"ok": True, "repo": repo.lower(), "ttl_seconds": float(ttl)})
    if path == "/auth/revoke-api-write":
        guard.close_api_write_grant(repo)
        return ControlResult(200, {"ok": True, "repo": repo.lower()})
    return ControlResult(404, {"error": f"unknown control endpoint: {path}"})


def _make_control_handler(
    guard: EgressGuard, secret: str | None
) -> type[BaseHTTPRequestHandler]:
    """Build a POST handler bound to *guard* / *secret* (avoids module globals).

    Called once per :class:`AuthControlServer`, so binding the guard via a
    closure-scoped class (rather than module globals or ``functools.partial``)
    keeps the reference explicit with no per-request cost.
    """

    class _AuthControlHandler(BaseHTTPRequestHandler):
        #: Bound a stuck client read so AuthControlServer.stop() cannot hang on
        #: an in-flight handler (review of PR #367).
        timeout = 5

        def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            """Handle an ``/auth/allow`` or ``/auth/revoke`` control request."""
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_CONTROL_BODY_BYTES:
                self._respond(ControlResult(413, {"error": "control body too large"}))
                return
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._respond(ControlResult(400, {"error": "body is not valid JSON"}))
                return
            self._respond(
                handle_control_request(
                    guard,
                    secret,
                    self.path,
                    self.headers.get(CONTROL_TOKEN_HEADER),
                    payload,
                )
            )

        def _respond(self, result: ControlResult) -> None:
            body = json.dumps(result.body).encode()
            self.send_response(result.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (base API name)
            """Silence the handler's default request logging to stderr."""

    return _AuthControlHandler


class AuthControlServer:
    """Internal HTTP control plane opening/closing push grants on a guard.

    ``publish`` (host-side, #357) POSTs ``/auth/allow`` before a push and
    ``/auth/revoke`` afterwards, authenticated by a shared secret the sandboxed
    container never sees.  Bind to a proxy-internal interface only.
    """

    def __init__(
        self,
        guard: EgressGuard,
        host: str = "127.0.0.1",
        port: int = 0,
        secret: str | None = None,
    ) -> None:
        """Create (but do not start) the control server on *host*:*port*."""
        self._httpd = ThreadingHTTPServer(
            (host, port), _make_control_handler(guard, secret)
        )
        # Daemon handler threads so stop()/interpreter exit never block on an
        # in-flight request (review of PR #367).
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound TCP port (resolved when constructed with ``port=0``)."""
        return self._httpd.server_address[1]

    def start(self) -> None:
        """Serve control requests on a daemon background thread."""
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="egress-auth-control",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release the listening socket."""
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


def allowed_repos_from_env(environ: dict[str, str] | None = None) -> set[str]:
    """Parse the ``owner/repo`` allowlist from ``CODE_SANDBOX_ALLOWED_REPOS``."""
    env = os.environ if environ is None else environ
    raw = env.get(ALLOWED_REPOS_ENV, "")
    return {r.strip() for r in raw.split(",") if r.strip()}


def allowed_egress_hosts_from_env(environ: dict[str, str] | None = None) -> set[str]:
    """Parse the destination-host allowlist from ``CODE_SANDBOX_ALLOWED_EGRESS_HOSTS``.

    Returns only the operator-supplied hosts (lower-cased); the built-in
    :data:`DEFAULT_EGRESS_HOSTS` are added by :class:`EgressGuard`, so an unset
    or empty value yields an empty set and the guard still allows the
    registries.  The special value ``*`` passes through unchanged so the guard
    can recognise it as the containment-disable sentinel (#506).
    """
    env = os.environ if environ is None else environ
    raw = env.get(ALLOWED_EGRESS_HOSTS_ENV, "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def control_bind_from_env(env: dict[str, str]) -> tuple[str, int, str | None] | None:
    """Return ``(host, port, secret)`` for the control API, or ``None`` when unset.

    A non-loopback bind without a shared secret is refused (``ValueError``):
    inside the sidecar the control port is reachable from the sandbox-facing
    Docker network, so exposing it unauthenticated would let sandboxed code
    open its own push grant.
    """
    raw_port = (env.get(CONTROL_PORT_ENV) or "").strip()
    if not raw_port:
        return None
    try:
        port = int(raw_port)
    except ValueError:
        raise ValueError(f"{CONTROL_PORT_ENV}={raw_port!r} is not an integer") from None
    secret = env.get(CONTROL_SECRET_ENV) or None
    host = (env.get(CONTROL_HOST_ENV) or "").strip() or "127.0.0.1"
    if host not in _LOOPBACK_HOSTS and secret is None:
        raise ValueError(
            f"{CONTROL_HOST_ENV}={host!r} is a non-loopback bind and requires "
            f"{CONTROL_SECRET_ENV} to be set"
        )
    return host, port, secret


#: mitmproxy discovers addons via a module-level ``addons`` list.  Built from
#: the environment so the sidecar image (#358) can configure the allowlist.
addons = [
    EgressGuard(
        allowed_repos_from_env(),
        token=os.environ.get(PROXY_TOKEN_ENV) or None,
        allowed_egress_hosts=allowed_egress_hosts_from_env(),
    )
]
