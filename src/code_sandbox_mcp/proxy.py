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
  short-lived authorization window is currently open for it.  The window is
  opened/closed via the internal control API (:class:`AuthControlServer`,
  ``POST /auth/allow`` / ``/auth/revoke``) that ``publish`` drives host-side
  (#356 / #357), authenticated by a shared secret the container never learns.

Because push is blocked at ref-discovery -- before any credentials are sent --
the guarantee holds even before tokens are moved out of the container (#356).

Validated by a PoC on 2026-07-01: a real ``git clone`` succeeds through the
TLS-terminating proxy while ``git push`` is rejected.

Still out of scope here (own issues): dropping the token from the container
env now that the proxy can inject it (#356 remainder), and gating non-push
write APIs on ``api.github.com`` -- which this addon still passes through
(#360).  The sidecar *image* that runs this addon is built by
``docker/Dockerfile.proxy`` via ``proxy_entrypoint.py``, and its container
lifecycle -- starting the sidecar, joining sandboxes to the internal network
(the only egress route, #355/#358), and installing this proxy's CA into the
sandbox trust store -- is handled host-side by
:mod:`code_sandbox_mcp.proxy_lifecycle`.
"""
from __future__ import annotations

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

#: Environment variable holding a comma-separated owner/repo allowlist (#358).
ALLOWED_REPOS_ENV = "CODE_SANDBOX_ALLOWED_REPOS"

#: Static fallback bearer token injected into authorized pushes.  The primary
#: path is the window-scoped token ``publish`` hands over on ``/auth/allow``
#: (#356), which needs no sidecar configuration and never outlives its window;
#: this env var remains for operators who prefer a sidecar-held credential.
#: Unset = no static injection.
PROXY_TOKEN_ENV = "CODE_SANDBOX_PROXY_TOKEN"

#: TCP port for the internal authorization control API; unset = decision-only
#: proxy (no window can be opened, matching today's inert behaviour).
CONTROL_PORT_ENV = "CODE_SANDBOX_PROXY_CONTROL_PORT"

#: Bind address for the control API (default ``127.0.0.1``).  The sidecar
#: (#358) sets ``0.0.0.0`` so the host can reach the port Docker publishes;
#: a non-loopback bind **requires** the shared secret, because inside the
#: sidecar the control port is reachable from the sandbox-facing Docker
#: network and must never accept unauthenticated window requests.
CONTROL_HOST_ENV = "CODE_SANDBOX_PROXY_CONTROL_HOST"

#: Shared secret authenticating control-API callers (#356 / #357).  ``publish``
#: sends it in the ``X-Control-Token`` header; the sandbox container never sees
#: it, so it cannot open its own push window.
CONTROL_SECRET_ENV = "CODE_SANDBOX_PROXY_CONTROL_SECRET"

#: Request header carrying the control secret on ``/auth/*`` calls.
CONTROL_TOKEN_HEADER = "X-Control-Token"

#: Authorization-window lifetime used when a caller omits ``ttl_seconds``.
DEFAULT_WINDOW_TTL_SECONDS = 30.0

#: Cap on the control-request body; payloads are tiny JSON, so anything larger
#: is rejected (413) unread, bounding handler memory (review of PR #367).
MAX_CONTROL_BODY_BYTES = 4096


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

    def __init__(
        self,
        allowed_repos: set[str] | None = None,
        token: str | None = None,
    ) -> None:
        """Create a guard.

        *allowed_repos* is the set of ``owner/repo`` strings that may ever be
        pushed to (matched case-insensitively); anything outside it is denied
        regardless of windows.  *token*, when given, is the bearer token the
        proxy injects into authorized pushes so the container need not hold
        GitHub credentials (#356).
        """
        self._allowed: set[str] = {r.lower() for r in (allowed_repos or ())}
        #: repo -> (monotonic expiry, window-scoped bearer token or ``None``).
        #: The token travels with the window (#356): ``publish`` hands it over
        #: on ``/auth/allow`` and it is dropped on revoke/expiry, so the proxy
        #: never holds a credential longer than one authorized push.
        self._windows: dict[str, tuple[float, str | None]] = {}
        self._lock = threading.Lock()
        #: static fallback bearer token; ``None`` disables it.  A window-scoped
        #: token, when present, takes precedence.
        self._token = token

    # -- authorization window control (to be driven by publish; #356 / #357) --

    def open_window(
        self,
        repo: str,
        ttl_seconds: float,
        now: float | None = None,
        token: str | None = None,
    ) -> None:
        """Permit push to *repo* for the next *ttl_seconds* (both git requests).

        *now* defaults to ``time.monotonic()``; pass it explicitly to keep the
        expiry deterministic in tests, mirroring :meth:`decide`.  Opening a
        window for a repo outside the allowlist is harmless -- :meth:`decide`
        still denies the push -- so callers need not pre-check membership.

        *token*, when given, is a window-scoped bearer credential injected
        into the authorized push (#356).  It lives and dies with the window:
        revoke or expiry discards it, so the proxy holds no long-lived token.
        """
        base = time.monotonic() if now is None else now
        with self._lock:
            self._windows[repo.lower()] = (base + ttl_seconds, token)

    def close_window(self, repo: str) -> None:
        """Revoke any open push window for *repo* (dropping its token, if any)."""
        with self._lock:
            self._windows.pop(repo.lower(), None)

    def _window_open(self, repo: str, now: float) -> bool:
        with self._lock:
            entry = self._windows.get(repo)
            if entry is None:
                return False
            expiry, _token = entry
            if now >= expiry:
                # Scrub eagerly: a window-scoped token must not sit in memory
                # past its authorization (#356).
                self._windows.pop(repo, None)
                return False
        return True

    def _window_token(self, repo: str | None, now: float) -> str | None:
        """Return the unexpired window-scoped token for *repo*, or ``None``."""
        if repo is None:
            return None
        with self._lock:
            entry = self._windows.get(repo.lower())
        if entry is None:
            return None
        expiry, token = entry
        return token if now < expiry else None

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

    # -- token injection (pure) --

    def token_headers_for(
        self,
        decision: Decision,
        is_push_request: bool,
        repo: str | None = None,
        now: float | None = None,
    ) -> dict[str, str]:
        """Return the ``Authorization`` header to add to an authorized push.

        Empty unless the request is an *allowed* push **and** a token is held:
        only pushes the policy permits get credentials, and with no token (the
        inert default) nothing is injected.  A window-scoped token for *repo*
        (handed over on ``/auth/allow``, #356) takes precedence over the
        static ``CODE_SANDBOX_PROXY_TOKEN`` fallback.  Pure, so the injection
        policy is unit-testable without mitmproxy.
        """
        if not (decision.allow and is_push_request):
            return {}
        base = time.monotonic() if now is None else now
        token = self._window_token(repo, base) or self._token
        if token:
            return {"Authorization": f"Bearer {token}"}
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
            # Fail closed: with no control server no window can ever open, so
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
        query_service = flow.request.query.get("service")
        decision = self.decide(path, query_service, time.monotonic())
        if not decision.allow:
            flow.response = http.Response.make(
                403,
                block_body(decision.reason),
                {"Content-Type": "text/plain"},
            )
            return
        for name, value in self.token_headers_for(
            decision, is_push(path, query_service), repo=repo_from_path(path)
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
    push window -- then opens (``/auth/allow``) or closes (``/auth/revoke``) a
    window for ``payload["repo"]``.  Kept free of socket/HTTP glue so the
    protocol is unit-testable on its own.
    """
    if secret is not None and not hmac.compare_digest(provided_secret or "", secret):
        return ControlResult(403, {"error": "invalid or missing control secret"})
    if not isinstance(payload, dict):
        return ControlResult(400, {"error": "request body must be a JSON object"})
    repo = payload.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        return ControlResult(400, {"error": "'repo' must be an 'owner/name' string"})
    if path == "/auth/allow":
        ttl = payload.get("ttl_seconds", DEFAULT_WINDOW_TTL_SECONDS)
        # bool is an int subclass; reject it so True/False is not read as a TTL.
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl <= 0:
            return ControlResult(400, {"error": "'ttl_seconds' must be a positive number"})
        # Optional window-scoped push credential (#356).  Never echoed back:
        # the response must stay safe to log.
        window_token = payload.get("token")
        if window_token is not None and not isinstance(window_token, str):
            return ControlResult(400, {"error": "'token' must be a string when present"})
        guard.open_window(repo, float(ttl), now=now, token=window_token or None)
        return ControlResult(200, {"ok": True, "repo": repo.lower(), "ttl_seconds": float(ttl)})
    if path == "/auth/revoke":
        guard.close_window(repo)
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
    """Internal HTTP control plane opening/closing push windows on a guard.

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


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def control_bind_from_env(env: dict[str, str]) -> tuple[str, int, str | None] | None:
    """Return ``(host, port, secret)`` for the control API, or ``None`` when unset.

    A non-loopback bind without a shared secret is refused (``ValueError``):
    inside the sidecar the control port is reachable from the sandbox-facing
    Docker network, so exposing it unauthenticated would let sandboxed code
    open its own push window.
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
    )
]
