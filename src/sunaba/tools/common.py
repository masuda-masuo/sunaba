"""Shared helpers for sunaba tools."""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import shlex
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Sequence

#: The container's workspace: the git repository root.
#:
#: Containers are created with this as their working directory, so an
#: ``exec_run`` that names no ``workdir`` still runs inside the repo.  That is
#: what makes the repo root unambiguous -- see
#: ``docs/design_filesystem_layout.md`` and Issue #600, where runners that
#: forgot to pass ``workdir`` silently ran in the home directory instead.
WORKSPACE = "/workspace"

#: Working directory of containers created before the workspace became the
#: repo root.  Their repo lives elsewhere (``/tmp/repo/*``, ``/home/sandbox``),
#: so :func:`sunaba.tools.vcs.resolve_git_root` still has to probe for it.
LEGACY_WORKDIR = "/home/sandbox"

#: Container metadata written by ``sandbox_initialize`` after a clone.  It
#: stays in the home directory on purpose: inside the workspace it would show
#: up in ``git status``.
META_PATH = f"{LEGACY_WORKDIR}/.sandbox-meta.json"


def _parse_numstat(lines: Sequence[str]) -> list[dict]:
    """Parse ``git diff --numstat`` output into structured records.

    Format (tab-separated)::

        additions<tab>deletions<tab>path
        -<tab>-<tab>path   (binary)

    Example::

        10      5       src/foo.py
        3       1       src/bar.py
    """
    records: list[dict] = []
    for line in lines:
        line = line.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        raw_add, raw_del, path = parts[0], parts[1], parts[2]
        if raw_add == "-" and raw_del == "-":
            records.append({
                "path": path,
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "binary": True,
            })
        else:
            try:
                additions = int(raw_add)
                deletions = int(raw_del)
            except ValueError:
                continue
            records.append({
                "path": path,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
            })
    return records


#: Short per-request Docker API timeout (seconds) for *recovery* and
#: *poll* operations (e.g. ``sandbox_stop``, ``sandbox_exec_check``).
#:
#: A wedged/unhealthy container can make a Docker API call block up to
#: docker-py's ~60s default -- right around the MCP client's ~60s
#: timeout.  When a recovery/poll call crosses that client timeout the
#: stdio JSON-RPC stream can desync and wedge the *whole* session,
#: including Docker-independent tools such as ``sandbox_list_runs``
#: (see docs/issue-181-followup.md for the full diagnosis).  Bounding
#: these calls well under the client timeout keeps recovery answerable.
#:
#: Override via the ``SUNABA_RECOVERY_DOCKER_TIMEOUT`` env var
#: (seconds); non-numeric or non-positive values fall back to the
#: 15s default (Issue #181).
_DEFAULT_RECOVERY_DOCKER_TIMEOUT: float = 15.0


def _recovery_timeout_from_env() -> float:
    """Resolve :data:`RECOVERY_DOCKER_TIMEOUT` from the environment.

    Reads ``SUNABA_RECOVERY_DOCKER_TIMEOUT``; falls back to
    :data:`_DEFAULT_RECOVERY_DOCKER_TIMEOUT` for unset, non-numeric, or
    non-positive values.
    """
    raw = os.environ.get("SUNABA_RECOVERY_DOCKER_TIMEOUT")
    if raw is None:
        return _DEFAULT_RECOVERY_DOCKER_TIMEOUT
    try:
        val = float(raw)
    except ValueError:
        return _DEFAULT_RECOVERY_DOCKER_TIMEOUT
    return val if val > 0 else _DEFAULT_RECOVERY_DOCKER_TIMEOUT


RECOVERY_DOCKER_TIMEOUT: float = _recovery_timeout_from_env()


def _coerce_list_arg(v: object) -> object:
    """Coerce a JSON-stringified list to list (MCP client serialization workaround, issue #296)."""
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            pass
    return v


def _docker(timeout: float | None = None) -> Any:
    """Lazy-import docker and return a Docker client.

    Args:
        timeout: Per-request Docker API timeout in seconds.  ``None``
            (the default) uses docker-py's own default (60s).  Pass a
            short value (see :data:`RECOVERY_DOCKER_TIMEOUT`) for
            recovery / poll operations so a wedged container fails fast
            rather than hanging the whole MCP session (Issue #181).
    """
    import docker

    if timeout is not None:
        # docker-py types ``timeout`` as int, but seconds-as-float is
        # intentional here (sub-second recovery budgets); accepted at runtime.
        return docker.from_env(timeout=timeout)  # type: ignore[arg-type]
    return docker.from_env()


#: Per-thread docker client timeout override, set by
#: :func:`_docker_client_timeout_scope`.  ``None`` = docker-py's default
#: (60s).
_docker_timeout_scope_state: threading.local = threading.local()


def _scoped_docker_timeout() -> float | None:
    """Return the current thread's scoped docker client timeout, or ``None``."""
    return getattr(_docker_timeout_scope_state, "value", None)


@contextmanager
def _docker_client_timeout_scope(timeout: float | None) -> Iterator[None]:
    """Run the current thread with a scoped default docker client timeout.

    Inside the scope, :func:`_scoped_docker_timeout` reports *timeout*, and
    client-building code that resolves it (e.g. ``checkpoint_list``) uses it
    instead of docker-py's 60s default.  The previous value is restored on
    exit, exception-safe, and the state is thread-local so concurrent scopes
    cannot leak into each other.

    Used by ``sandbox_stop``'s unpushed-checkpoint guard: the guard holds a
    recovery permit while its inner ``checkpoint_list`` runs, and on a
    partially wedged container (``containers.get`` answers, the exec hangs --
    Issue #181) the inner client must fail fast at
    :data:`RECOVERY_DOCKER_TIMEOUT` rather than hold the permit for the 60s
    default (issue #784 review round 3).  The scope only bounds the HTTP
    phases though: docker-py reads the hijacked exec output stream with an
    unbounded poll, so the guard additionally enforces a hard wall-clock
    deadline on the whole check (issue #784 review round 4).  Normal
    ``checkpoint_list`` tool calls run outside any scope and keep the 60s
    default -- nothing here shortens the default globally.
    """
    previous = _scoped_docker_timeout()
    _docker_timeout_scope_state.value = timeout
    try:
        yield
    finally:
        _docker_timeout_scope_state.value = previous


#: Nudge attached to "container not found" errors (Issue #550): for an
#: agent operating without instruction files, the error response is the
#: only channel that can point to the right first move, so the shared
#: payload carries a ``recommended_next_action`` field.  Advisory only.
CONTAINER_NOT_FOUND_NEXT_ACTION = (
    "sandbox_list_containers to find running containers, "
    "or sandbox_initialize to start one"
)


def container_not_found_error(container_id: str, **extra: Any) -> str:
    """Return the shared JSON error payload for a missing container.

    Carries a ``recommended_next_action`` nudge (advisory, Issue #550).
    *extra* fields are merged into the payload so callers can keep
    tool-specific keys (e.g. ``gate_passed=False`` for verify).
    """
    payload: dict[str, Any] = {
        "status": "error",
        "error": f"Container {container_id[:12]} not found",
        "recommended_next_action": CONTAINER_NOT_FOUND_NEXT_ACTION,
    }
    payload.update(extra)
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Docker concurrency caps (Issue #784)
# ---------------------------------------------------------------------------
#
# Every sunaba tool is a sync ``def``, and FastMCP 3.x runs sync tools on
# anyio's shared default thread limiter (40 tokens).  A docker call against a
# wedged container (one whose docker API never answers) holds its limiter
# token indefinitely, so once ~40 such calls pile up -- parallel workers
# polling and retrying -- EVERY sync tool queues behind them, docker-free
# ones included, across all sessions (issue #784; measured: with 60 wedged
# ``sandbox_exec`` calls, docker-free ``get_workflow_guide`` took 4.6s instead
# of 0.01s).
#
# :func:`docker_bound` caps docker-touching tools with a two-level semaphore
# so wedged calls can never exhaust the shared limiter:
#
# * a **global** cap (default 24), and
# * a **per-container** cap (default 6) keyed by the 12-char container id
#   prefix, so one wedged container cannot consume the global budget and
#   block docker operations on other, healthy containers.
#
# Acquisition order is global-then-per-container (release in reverse, always
# via ``finally``).  Acquisition is **non-blocking by default**:
# ``SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS`` defaults to 0, so a call that
# cannot get a permit returns a structured "busy" JSON error **immediately**
# -- never raises, never queues, and a refused call holds **no limiter
# token** (a wedged permit cannot free within any reasonable wait anyway,
# and every second a call waits it occupies a shared-limiter token).  The
# env var remains as an opt-in knob for bounded waiting.
#
# The escape hatch is exempt by design: ``sandbox_stop`` and
# ``sandbox_list_containers`` run on a **recovery pool**
# (:func:`recovery_bound`) -- a dedicated ``BoundedSemaphore`` with
# ``SUNABA_RECOVERY_CONCURRENCY`` permits (default 4) and **no per-container
# cap** -- because their whole purpose is to act on wedged containers, and
# the documented recovery path must stay callable exactly when the docker
# caps are saturated (the #784 scenario).
#
# Token math: docker-bound calls hold at most 24 limiter tokens and recovery
# calls at most 4, so the worst case is 28 of 40 -- **>= 12 tokens genuinely
# free** for docker-free tools at all times.  With non-blocking acquire the
# overflow calls return in milliseconds and hold no tokens at all.

#: Default process-wide cap on concurrent docker-bound tool executions.
_DEFAULT_DOCKER_GLOBAL_CONCURRENCY: int = 24
#: Default cap on concurrent docker-bound tool executions per container
#: (keyed by 12-char container id prefix).
_DEFAULT_DOCKER_PER_CONTAINER_CONCURRENCY: int = 6
#: Default per-acquire timeout in seconds.  Default 0 = **non-blocking**: a
#: docker-bound call that cannot get a permit returns the busy error
#: immediately.  Set ``SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS`` to a positive
#: value to opt into bounded waiting (issue #784 review, 2026-08-01).
_DEFAULT_DOCKER_ACQUIRE_TIMEOUT_SECONDS: float = 0.0
#: Default cap on concurrent recovery-pool executions (``sandbox_stop`` /
#: ``sandbox_list_containers``), separate from the docker caps so the escape
#: hatch stays callable when they are saturated.  No per-container cap:
#: recovery tools exist to act on wedged containers.
_DEFAULT_RECOVERY_CONCURRENCY: int = 4


def _env_int(name: str, default: int) -> int:
    """Parse an integer env knob; unset/non-numeric/non-positive -> default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        return default
    return val if val >= 1 else default


def _env_non_negative_float(name: str, default: float) -> float:
    """Parse a non-negative-float env knob; unset/non-numeric/negative -> default.

    ``0`` is honored (it is meaningful: non-blocking acquire), unlike
    :func:`_env_int` where a cap of 0 would be meaningless.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


def _docker_global_concurrency() -> int:
    """Process-wide docker concurrency cap; env ``SUNABA_DOCKER_GLOBAL_CONCURRENCY``."""
    return _env_int("SUNABA_DOCKER_GLOBAL_CONCURRENCY", _DEFAULT_DOCKER_GLOBAL_CONCURRENCY)


def _docker_per_container_concurrency() -> int:
    """Per-container docker concurrency cap; env ``SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY``."""
    return _env_int("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", _DEFAULT_DOCKER_PER_CONTAINER_CONCURRENCY)


def _docker_acquire_timeout_seconds() -> float:
    """Per-acquire timeout (seconds), read per call.

    Env ``SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS``; default
    :data:`_DEFAULT_DOCKER_ACQUIRE_TIMEOUT_SECONDS` (0 = non-blocking).
    """
    return _env_non_negative_float(
        "SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", _DEFAULT_DOCKER_ACQUIRE_TIMEOUT_SECONDS
    )


def _recovery_concurrency() -> int:
    """Recovery-pool concurrency cap; env ``SUNABA_RECOVERY_CONCURRENCY``."""
    return _env_int("SUNABA_RECOVERY_CONCURRENCY", _DEFAULT_RECOVERY_CONCURRENCY)


_DOCKER_GLOBAL_LOCK = threading.Lock()
#: Process-global docker semaphore, built lazily on first use so the
#: ``SUNABA_DOCKER_GLOBAL_CONCURRENCY`` env knob is honored at call time.
#: Tests that need a small cap reset this to ``None`` and set the env var.
_DOCKER_GLOBAL_SEMAPHORE: threading.BoundedSemaphore | None = None

_DOCKER_PER_CONTAINER_LOCK = threading.Lock()
#: Per-container docker semaphores keyed by 12-char container id prefix.
#: Entries are never removed: the set of distinct prefixes is tiny at our
#: scale and dropping an entry whose semaphore happens to be full is racy
#: (a holder may still be mid-call).  Opportunistic cleanup of idle entries
#: would save nothing measurable, so we deliberately don't bother
#: (documented choice, issue #784).
_DOCKER_PER_CONTAINER_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}

_RECOVERY_LOCK = threading.Lock()
#: Process-global recovery-pool semaphore, built lazily like the docker one
#: so the ``SUNABA_RECOVERY_CONCURRENCY`` knob is honored at call time.
#: Tests reset this to ``None`` and set the env var.
_RECOVERY_SEMAPHORE: threading.BoundedSemaphore | None = None


def _get_global_docker_semaphore() -> threading.BoundedSemaphore:
    """Return the process-wide docker semaphore, built once from the env knob."""
    global _DOCKER_GLOBAL_SEMAPHORE
    sem = _DOCKER_GLOBAL_SEMAPHORE
    if sem is None:
        with _DOCKER_GLOBAL_LOCK:
            if _DOCKER_GLOBAL_SEMAPHORE is None:
                _DOCKER_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(_docker_global_concurrency())
            sem = _DOCKER_GLOBAL_SEMAPHORE
    return sem


def _get_per_container_docker_semaphore(prefix: str) -> threading.BoundedSemaphore:
    """Return (creating on demand) the semaphore for a 12-char container prefix."""
    with _DOCKER_PER_CONTAINER_LOCK:
        sem = _DOCKER_PER_CONTAINER_SEMAPHORES.get(prefix)
        if sem is None:
            sem = threading.BoundedSemaphore(_docker_per_container_concurrency())
            _DOCKER_PER_CONTAINER_SEMAPHORES[prefix] = sem
        return sem


def _get_recovery_semaphore() -> threading.BoundedSemaphore:
    """Return the recovery-pool semaphore, built once from the env knob."""
    global _RECOVERY_SEMAPHORE
    sem = _RECOVERY_SEMAPHORE
    if sem is None:
        with _RECOVERY_LOCK:
            if _RECOVERY_SEMAPHORE is None:
                _RECOVERY_SEMAPHORE = threading.BoundedSemaphore(_recovery_concurrency())
            sem = _RECOVERY_SEMAPHORE
    return sem


def _busy_error(pool: str, limit: str, cap: int) -> str:
    """Structured JSON "busy" payload returned instead of hanging (issue #784).

    Same shape family as :func:`container_not_found_error` (``status`` /
    ``error`` / ``recommended_next_action``) plus ``busy: true`` and the
    pool name so callers can distinguish a refused call from a real failure
    and tell which cap refused it.  The docker payload's
    ``recommended_next_action`` points at ``sandbox_stop`` /
    ``sandbox_list_containers``, which run on the recovery pool and are
    therefore actionable even when the docker caps are saturated.
    """
    if pool == "recovery":
        detail = (
            f"Recovery concurrency limit reached ({limit}, cap {cap}): too many "
            "recovery operations in flight; refusing to queue so the escape "
            "hatch itself cannot be gated by the caps it is meant to break"
        )
        next_action = (
            "Wait for in-flight recovery operations (sandbox_stop / "
            "sandbox_list_containers) to finish and retry; pass force=True to "
            "sandbox_stop to skip the checkpoint guard and shorten its hold "
            "on the recovery pool"
        )
    else:
        detail = (
            f"Docker concurrency limit reached ({limit}, cap {cap}): too many "
            "docker operations in flight; refusing to queue so a wedged "
            "container cannot exhaust the server thread pool (issue #784)"
        )
        next_action = (
            "Wait for in-flight docker operations to finish and retry; if a "
            "container is wedged, sandbox_stop it (or sandbox_list_containers "
            "to see what is running)"
        )
    payload: dict[str, Any] = {
        "status": "error",
        "error": detail,
        "busy": True,
        "pool": pool,
        "limit": limit,
        "cap": cap,
        "recommended_next_action": next_action,
    }
    return json.dumps(payload)


def _journal_busy_refusal(busy_json: str, tool: str, container_id: object) -> None:
    """Best-effort journal record of a concurrency-cap refusal (Issue #783).

    Parses the structured busy payload from :func:`_busy_error` and writes
    a ``busy_refusal`` journal entry so per-pool refusal counts are
    observable on the /insights page (the initialize-wait proxy in the
    post-#784 world).  Never raises and never slows the refusal path: a
    journal failure must not turn a busy refusal into a crash -- the busy
    JSON is returned to the caller regardless.
    """
    try:
        payload = json.loads(busy_json)
        cid = container_id if isinstance(container_id, str) and container_id else None
        from sunaba.journal import record_busy_refusal

        record_busy_refusal(
            pool=payload.get("pool", "unknown"),
            limit=payload.get("limit", ""),
            cap=int(payload.get("cap") or 0),
            tool=tool,
            container_id=cid,
        )
    except Exception:
        pass  # best-effort observation (see docstring)


def _try_acquire_docker_permits(
    container_id: object, timeout: float
) -> tuple[threading.BoundedSemaphore | None, threading.BoundedSemaphore | None, str | None]:
    """Acquire the global, then the per-container docker permit.

    Returns ``(global_sem, per_sem, None)`` on success (per_sem is ``None``
    when the call provides no ``container_id``), or
    ``(None, None, busy_json)`` when a timeout occurred.  On a per-container
    timeout the global permit is released before returning, keeping the
    acquire/release order strict (global first, per-container last).  With
    the default timeout of 0 this is a plain try-acquire: the overflow call
    returns the busy JSON immediately and holds no token.
    """
    global_sem = _get_global_docker_semaphore()
    if not global_sem.acquire(timeout=timeout):
        return None, None, _busy_error("docker", "global", _docker_global_concurrency())
    per_sem: threading.BoundedSemaphore | None = None
    if isinstance(container_id, str) and container_id:
        prefix = container_id[:12]
        per_sem = _get_per_container_docker_semaphore(prefix)
        if not per_sem.acquire(timeout=timeout):
            global_sem.release()
            return None, None, _busy_error(
                "docker", f"container {prefix}", _docker_per_container_concurrency()
            )
    return global_sem, per_sem, None


def _try_acquire_recovery_permit(
    container_id: object, timeout: float
) -> tuple[threading.BoundedSemaphore | None, threading.BoundedSemaphore | None, str | None]:
    """Acquire the recovery-pool permit (non-blocking).

    *container_id* and *timeout* match the shared acquire signature but are
    unused: the recovery pool has **no per-container cap** (its purpose is
    to act on wedged containers) and acquisition is always non-blocking.
    Returns ``(sem, None, None)`` on success or ``(None, None, busy_json)``
    when the pool is exhausted.
    """
    sem = _get_recovery_semaphore()
    if not sem.acquire(timeout=0):
        return None, None, _busy_error("recovery", "recovery", _recovery_concurrency())
    return sem, None, None


def _concurrency_bound(
    fn: Callable[..., Any],
    acquire: Callable[[object, float], tuple[Any, Any, str | None]],
    *,
    key_on: str = "container_id",
) -> Callable[..., Any]:
    """Shared wrapper machinery behind :func:`docker_bound` and :func:`recovery_bound`.

    *acquire* is invoked with ``(container_id_or_None, timeout)`` and returns
    ``(primary_sem, secondary_sem, None)`` on success (secondary_sem is
    ``None`` when the pool has no per-container tier) or
    ``(None, None, busy_json)`` when the call was refused.  The wrapper
    releases secondary-then-primary in a ``finally`` so an exception inside
    the tool cannot leak a permit, and passes the busy JSON through
    unchanged.  ``functools.wraps`` preserves the real tool signature, so
    FastMCP's signature introspection (schema, ``exclude_args``, injected
    ``ctx``) still sees the underlying tool.  Async tools (e.g. the
    ``sandbox_initialize`` wrapper) are supported: the wrapper stays async
    and performs the blocking acquire in a worker thread so the event loop
    is never blocked.

    *key_on* names the tool parameter the per-container tier keys on
    (default ``"container_id"``).  A tool whose container argument has a
    different name must declare it (e.g. ``sandbox_attach``'s ``name_or_id``)
    or it silently loses per-container isolation and only the global cap
    protects it (issue #784 review).  Pools without a per-container tier
    (``recovery_bound``) ignore *key_on*.
    """
    sig = inspect.signature(fn)
    has_container_id = key_on in sig.parameters

    def _bound_container_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> object:
        if not has_container_id:
            return None
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError:
            # FastMCP validates arguments before calling, so this should not
            # happen; treat as "no container" rather than crashing the cap.
            return None
        bound.apply_defaults()
        return bound.arguments.get(key_on)

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> Any:
            timeout = _docker_acquire_timeout_seconds()
            container_id = _bound_container_id(args, kwargs)
            sem, per_sem, busy = await asyncio.to_thread(acquire, container_id, timeout)
            if busy is not None:
                # Issue #783: make the refusal observable (per-pool counts on
                # /insights) without changing the refusal itself.  The journal
                # write is file I/O, so it hops to a worker thread -- the
                # event loop is never blocked (issue #783 review).
                await asyncio.to_thread(
                    _journal_busy_refusal, busy, fn.__name__, container_id
                )
                return busy
            assert sem is not None  # success path always carries the primary permit
            try:
                return await fn(*args, **kwargs)
            finally:
                if per_sem is not None:
                    per_sem.release()
                sem.release()

        return _async_wrapper

    @functools.wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        timeout = _docker_acquire_timeout_seconds()
        container_id = _bound_container_id(args, kwargs)
        sem, per_sem, busy = acquire(container_id, timeout)
        if busy is not None:
            # Issue #783: make the refusal observable (per-pool counts on
            # /insights) without changing the refusal itself.
            _journal_busy_refusal(busy, fn.__name__, container_id)
            return busy
        assert sem is not None  # success path always carries the primary permit
        try:
            return fn(*args, **kwargs)
        finally:
            if per_sem is not None:
                per_sem.release()
            sem.release()

    return _wrapper


def docker_bound(
    fn: Callable[..., Any],
    *,
    key_on: str = "container_id",
) -> Callable[..., Any]:
    """Decorate a docker-bound tool with two-level concurrency caps (issue #784).

    Wrap every tool that calls ``_docker()`` at registration time
    (``mcp.tool()(docker_bound(tool_fn))``); leave docker-free tools
    unwrapped so they are never gated behind docker capacity.

    On entry the wrapper acquires:

    * a process-global ``threading.BoundedSemaphore`` with
      ``SUNABA_DOCKER_GLOBAL_CONCURRENCY`` permits (default 24), and
    * when the tool's signature has the parameter named by *key_on* and the
      call provides one, a per-container semaphore with
      ``SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY`` permits (default 6) keyed
      by the 12-char container id prefix (``""`` counts as omitted).

    *key_on* defaults to ``"container_id"``; tools whose container argument
    has a different name (e.g. ``sandbox_attach``'s ``name_or_id``) must
    pass that name explicitly, or they silently lose per-container isolation
    and only the global cap protects them (issue #784 review).

    Acquisition is **non-blocking by default**: ``SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS``
    defaults to 0, so a call that cannot get a permit returns the structured
    busy JSON from :func:`_busy_error` immediately -- it never raises, never
    hangs, and a refused call holds **no limiter token** (a wedged permit
    cannot free within any reasonable wait anyway).  Set the env var to a
    positive value to opt into bounded waiting.  Acquire order is global
    then per-container; release is the reverse and always happens
    (``finally``), so an exception inside the tool cannot leak a permit.
    ``functools.wraps`` preserves the real tool signature, so FastMCP's
    signature introspection (schema, ``exclude_args``, injected ``ctx``)
    still sees the underlying tool.  Async tools (e.g. the
    ``sandbox_initialize`` wrapper) are supported: the wrapper stays async
    and performs the blocking acquire in a worker thread so the event loop
    is never blocked.

    On refusal the tool returns the busy JSON **string** as its result --
    check ``"busy": true`` / ``"status": "error"`` before treating the
    response as the tool's real output; content-style tools
    (``read_file_range``, ``list_files``, ...) return it verbatim, so the
    payload is deliberately self-describing (issue #784 review).
    """
    return _concurrency_bound(fn, _try_acquire_docker_permits, key_on=key_on)


def recovery_bound(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorate a recovery tool with the dedicated recovery-pool cap (issue #784).

    For ``sandbox_stop`` / ``sandbox_list_containers``: the escape hatch
    must stay callable exactly when the docker caps are saturated (the
    wedged-container scenario), so instead of ``docker_bound`` these run on
    their own ``threading.BoundedSemaphore`` with ``SUNABA_RECOVERY_CONCURRENCY``
    permits (default 4) and **no per-container cap** -- their purpose is to
    act on wedged containers.  Acquisition is non-blocking: on pool
    exhaustion the call returns the structured busy JSON from
    :func:`_busy_error` immediately.  Worst-case shared-limiter consumption
    becomes 24 (docker) + 4 (recovery) = 28 of 40, leaving >= 12 tokens
    genuinely free for docker-free tools.
    """
    return _concurrency_bound(fn, _try_acquire_recovery_permit)


#: Warning appended to a clone result when the repo was cloned *without* a
#: VCS token.  The clone itself is anonymous (public repos only, read-only
#: working tree), but ``publish`` no longer requires the token to have been
#: present at container start: it lazily injects a host-resolved token into
#: the push exec (Issue #347), so a no-token clone can still be published
#: afterward without a re-init.  Surfacing this at clone time only flags that
#: the *clone* was unauthenticated (a private repo would have failed), not
#: that pushing is impossible.
CLONE_NO_TOKEN_WARNING = (
    "cloned without a VCS token (anonymous clone; private repos would fail). "
    "publish can still push later: it injects a host-resolved token into the "
    "push step on demand (no re-init needed), provided the host has a token "
    "available (GITHUB_TOKEN / broker)."
)


def _build_clone_command(
    repo: str,
    target: str,
    branch: str = "",
    authenticated: bool = False,
) -> str:
    """Build the in-container clone command, choosing transport by auth.

    *repo* must already be validated as ``owner/name`` by the caller
    (``_REPO_FORMAT_RE`` / ``_validate_clone_repo``), so interpolating it
    into the HTTPS URL is injection-safe.

    - **authenticated** (a VCS token is present, e.g. ``gh auth setup-git``
      succeeded): use ``gh repo clone``, which
      authenticates via ``GH_TOKEN`` and so handles private *and* public
      repositories.
    - **anonymous** (no token): use a plain ``git clone`` over HTTPS.
      Public repos clone without credentials; ``GIT_TERMINAL_PROMPT=0``
      makes a *private* repo fail fast instead of hanging on an
      interactive credential prompt.  ``gh repo clone`` cannot be used
      here because ``gh`` requires authentication even for public repos
      (Issue #333).
    """
    safe_repo = shlex.quote(repo)
    safe_target = shlex.quote(target)
    if authenticated:
        cmd = f"gh repo clone {safe_repo} {safe_target}"
        if branch:
            cmd += f" -- -b {shlex.quote(branch)}"
        return cmd
    url = shlex.quote(f"https://github.com/{repo}.git")
    branch_opt = f"-b {shlex.quote(branch)} " if branch else ""
    return f"GIT_TERMINAL_PROMPT=0 git clone {branch_opt}{url} {safe_target}"
