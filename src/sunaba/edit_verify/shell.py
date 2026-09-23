"""Shell-command building blocks: path quoting and environment prefixes.

These are used by runners in :mod:`sunaba.edit_verify` to construct
shell commands that execute inside sandbox containers.

Since issue #910 this module also owns the *server-side deadline*
machinery for every synchronous foreground command path:

* the **verify deadline** -- a per-call wall-clock bound armed by
  :func:`open_verify_deadline` and enforced by :func:`_exec_run` (and by
  verify's direct ``_run`` path via :func:`run_exec_under_verify_deadline`);
* the **foreground exec deadline** -- the same machinery generalized to
  ``sandbox_exec`` / ``run_python`` / ``package_install`` via
  :func:`open_foreground_deadline` / :func:`run_exec_under_foreground_deadline`,
  configured by ``SUNABA_FOREGROUND_TIMEOUT``.

When a deadline fires, a watchdog reaps the whole marked command tree --
TERM descendants-first, KILL as fallback -- and the owning thread raises
:class:`VerifyTreeTimeout` / :class:`ForegroundTreeTimeout` instead of
returning a half-finished verdict.  The verify surface (its own resolver,
marker environment and exception) is unchanged by the generalization.
"""

from __future__ import annotations

import os
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any


def _quote_path(path: str | Sequence[str]) -> str:
    """Shell-escape one or more file paths for use in a command string."""
    if isinstance(path, str):
        return shlex.quote(path)
    return " ".join(shlex.quote(p) for p in path)


def _path_display(path: str | Sequence[str]) -> str:
    """Render *path* as a single string for use as a parse-fallback label."""
    return path if isinstance(path, str) else " ".join(path)


#: Environment variables to set before running linters/type checkers
#: inside sandbox containers.  Containers run as a non-root user with
#: a read-only ``/``, so cache directories must point to ``/tmp``.
_SANDBOX_ENV: str = (
    "RUFF_CACHE_DIR=/tmp/.ruff_cache "
    "mkdir -p /tmp/.ruff_cache 2>/dev/null; "
)


def _exec_run(
    container: Any,
    cmd: list[str],
    workdir: str | None = None,
) -> tuple[int, str, str]:
    """Run ``container.exec_run`` with ``demux=True``.

    docker-py *only* returns a ``(stdout, stderr)`` tuple when
    ``demux=True`` is passed; without it the output is multiplexed
    bytes with no way to separate the streams.  This wrapper
    centralises the call so every caller gets clean decoded text
    without repeating the idiom.

    When the calling thread runs inside a #910 verify deadline scope the
    exec is routed through :func:`run_exec_under_verify_deadline` and the
    command inherits the scope's marker environment, so a deadline expiry
    reaps this command too.

    Returns:
        ``(exit_code, stdout_text, stderr_text)``.
    """
    exec_kwargs: dict[str, Any] = {
        "stdout": True,
        "stderr": True,
        "demux": True,
        "workdir": workdir,
    }
    marker_env = verify_marker_environment()
    if marker_env is not None:
        exec_kwargs["environment"] = marker_env
    exit_code, (stdout_part, stderr_part) = run_exec_under_verify_deadline(
        lambda: container.exec_run(cmd, **exec_kwargs)
    )
    # docker-py hands back ``None`` -- not ``b""`` -- for a stream that
    # produced nothing, so both parts are guarded before decoding.
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    return exit_code, stdout_text, stderr_text


#: Environment prefix for *go* invocations only (Issue #584).
#:
#: ``GOMAXPROCS=1`` serialises the Go toolchain's compile/vet/link fan-out,
#: which otherwise blows past the container's ``pids_limit`` of 100 and dies of
#: fork exhaustion (#233).  It used to be baked into ``Dockerfile.go`` as an
#: image-wide ``ENV`` -- but *every* Go binary honours ``GOMAXPROCS``, and ``gh``
#: is written in Go, so the image-wide setting throttled unrelated tools.  Once
#: the go toolchain lives in the all-in-one default image (``sandbox:full``)
#: that leak reaches every container, so the guard moves to where it belongs:
#: the go command itself.
_GO_ENV: str = "GOMAXPROCS=1 "

#: Environment prefix for *cargo* invocations only, mirroring ``_GO_ENV``.
#:
#: ``CARGO_BUILD_JOBS=1`` serialises rustc's codegen-unit / crate-graph
#: fan-out for the same reason ``GOMAXPROCS=1`` exists above: an unbounded
#: ``cargo build``/``clippy``/``test`` can spawn enough parallel rustc
#: processes to blow past the container's ``pids_limit`` (#233's failure
#: mode is toolchain-agnostic, not Go-specific).  ``CARGO_TERM_COLOR=never``
#: keeps cargo's human-readable stderr free of ANSI escapes, which would
#: otherwise land inside the panic messages that
#: :class:`~sunaba.test_report.RustTestAdapter` regex-matches out of
#: ``cargo test`` output.
_RUST_ENV: str = "CARGO_BUILD_JOBS=1 CARGO_TERM_COLOR=never "
# ---------------------------------------------------------------------------
# Server-side deadline machinery (issue #910, generalized for foreground exec)
# ---------------------------------------------------------------------------
#
# A synchronous call that outlives its deadline must terminate and reap its
# whole in-container command tree before the MCP client's ~300s tool-call
# wait, and the timed-out call must return a terminal ``status: "timeout"``
# result instead of letting a transport timeout masquerade as a successful
# (or failed) outcome.
#
# Mechanics: the call opens a *deadline scope* on its own thread.  Every
# exec owned by the call runs under that scope and inherits a unique marker
# through the environment, so descendants -- including setsid-detached
# children -- are identifiable.  A watchdog thread armed by
# :func:`open_verify_deadline` / :func:`open_foreground_deadline` fires at
# the deadline and reaps the marked tree through the same container exec:
# find the marker in ``/proc`` (the sandbox images have no ``ps``), TERM
# descendants first, wait briefly, KILL survivors, then probe again for the
# reap verdict.  The owning thread, unblocked by the reap (or by a mock
# container releasing), raises :class:`VerifyTreeTimeout` /
# :class:`ForegroundTreeTimeout` carrying the verdict; the caller converts
# it into the terminal timeout result.
#
# One implementation serves both: the scope and reaper are generic over the
# exception type and the marker string, and verify's public surface (its
# resolver, marker environment, exception and ``_reap_verify_tree`` name)
# is kept as thin aliases so the #910 contract -- including the tests that
# patch ``_reap_verify_tree`` -- is unchanged.

#: Environment variable carrying the per-call marker into every command the
#: verify owns.  Descendants inherit it, and the reaper matches it against
#: ``/proc/<pid>/environ`` -- never against the reaper's own exec (the
#: reaper passes no environment, so it cannot reap itself).
_VERIFY_MARKER_ENV: str = "SUNABA_VERIFY_MARKER"

#: Environment variable carrying the per-call private marker into every
#: foreground exec (``sandbox_exec`` / ``run_python`` / ``package_install``).
#: Distinct from the verify marker so one call's reap can never touch
#: another call's tree.
_FOREGROUND_MARKER_ENV: str = "SUNABA_FOREGROUND_MARKER"

#: Default foreground exec deadline in seconds (below the MCP client's
#: ~300s boundary).  Mirrors the #910 verify default.
_DEFAULT_FOREGROUND_TIMEOUT: float = 270.0

#: Environment variable resolving the per-call foreground deadline (seconds).
#: ``0`` disables the deadline; a positive numeric value overrides the
#: default; invalid or negative values fall back to the default (only ``0``
#: disables -- a misconfiguration must not silently turn the deadline off).
_FOREGROUND_TIMEOUT_ENV: str = "SUNABA_FOREGROUND_TIMEOUT"


def resolve_foreground_deadline() -> float:
    """Resolve the foreground exec deadline from ``SUNABA_FOREGROUND_TIMEOUT``.

    Contract (pinned by ``tests/test_foreground_deadline_config.py``):
    default 270.0s when unset; a positive numeric override is used as-is;
    exactly ``0`` disables the deadline; invalid or negative values fall
    back to the default (only ``0`` disables -- a misconfiguration must not
    silently turn the deadline off).
    """
    raw = os.environ.get(_FOREGROUND_TIMEOUT_ENV, "").strip()
    if raw == "":
        return _DEFAULT_FOREGROUND_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FOREGROUND_TIMEOUT
    if value < 0:
        return _DEFAULT_FOREGROUND_TIMEOUT
    return value


def new_foreground_marker() -> str:
    """Unique per-call marker for the foreground deadline reaper.

    Deliberately hyphen-free and free of the contract-mock classification
    substrings (``-9``, ``kill``, ``ps``, ``/proc``, ``TERM``, ``KILL`` ...):
    the reaper's commands run through contract mocks that classify by such
    substrings, and the probe command embeds the marker text.
    """
    return f"sunabafg{uuid.uuid4().hex[:12]}"


#: Python one-liner that turns the foreground exec into a child subreaper
#: before running the actual command.
#
# ``PR_SET_CHILD_SUBREAPER`` (36) makes the exec'd process adopt the orphaned
# descendants of its tree, and the flag survives ``execve``: the one-liner
# sets it, then ``execv``es ``/bin/bash`` with the real command.  With the
# reaper signalling one generation at a time (leaves first, the direct exec
# process last), every killed descendant is collected by a still-alive
# parent or by this subreaper, so a timed-out call leaves no zombie behind
# even in a container whose PID 1 never reaps (the sandbox images ship no
# init).  ``ctypes`` is used because the sandbox images guarantee Python but
# not a compiled helper.
_SUBREAPER_EXEC_PREFIX: str = (
    "import ctypes, sys, os; "
    "ctypes.CDLL(None).prctl(36, 1); "
    "os.execv('/bin/bash', ['/bin/bash', '-c', sys.argv[1]])"
)


def subreaper_exec_command(rest: str) -> str:
    """Wrap *rest* (a ``/bin/bash -c`` command string) under the subreaper
    prefix: ``exec python3 -c <prefix> '<rest>'``.

    The leading ``exec`` is load-bearing: the exec'd shell (which carries
    the per-call marker in its environment, and whose own parent is not
    marked) is *replaced* by the python3 prefix, which sets the subreaper
    flag and then ``exec``s ``/bin/bash`` with *rest*.  Without the ``exec``
    the wrapper shell would stay alive as a marked non-root process between
    the exec session and the subreaper, and the reaper -- whose "root" is
    the topmost marked process -- would TERM the subreaper as an ordinary
    non-root while its adopted descendants were still alive, re-orphaning
    them to PID 1.
    """
    return (
        "exec python3 -c " + shlex.quote(_SUBREAPER_EXEC_PREFIX)
        + " " + shlex.quote(rest)
    )


def foreground_sweep_command(marker: str) -> str:
    """Shell script that TERM/KILLs every marked process except its own
    parent (the exec'd wrapper), used by the explicit ``timeout=N`` path.

    Runs as ``env -u SUNABA_FOREGROUND_MARKER /bin/sh -c <this>`` so the
    sweep's own subprocesses never carry the marker and can never match the
    probe; the wrapper (which still carries it) is excluded via ``$PPID``.
    """
    return (
        "for d in /proc/[0-9]*; do "
        "p=${d#/proc/}; "
        '[ "$p" = "$PPID" ] && continue; '
        'e=$(tr \'\\0\' \' \' < "$d/environ" 2>/dev/null) || continue; '
        f'case "$e" in *{marker}*) '
        'kill -TERM "$p" 2>/dev/null; kill -KILL "$p" 2>/dev/null;; '
        "esac; done"
    )


def foreground_wait_fragment(marker: str, waitf: str) -> str:
    """Bash fragment appended to the wrapper's exit path (always runs).

    The exec'd wrapper is the tree's subreaper: it adopts the orphaned
    descendants of its chain, and -- critically -- the reaper needs it to
    stay alive until those adopted orphans have been killed and reaped.  A
    plain wrapper would exit the moment its direct command returned (e.g.
    ``run_python``'s runner *always* exits 0 once it emitted its result,
    even when the user code was killed mid-run), re-orphaning the adopted
    descendants to PID 1, where they become permanent zombies in a
    no-init container.  This fragment therefore runs unconditionally and
    waits (bounded, 5s) until no marked process except the wrapper itself
    remains, polling ``/proc`` with the marker removed from the probe's
    environment (so the probe can never match its own processes) and the
    wrapper's own pid excluded via the positional argument.  When the tree
    is already empty (the common fast path) the first probe breaks the
    loop immediately, so a completed command pays only one short probe.

    After the loop the fragment runs ``wait`` so every dead (adopted) child
    is reaped before the wrapper exits -- the environ probe cannot see
    zombies, so a just-killed adopted descendant would otherwise be
    re-orphaned to PID 1 when the wrapper exits.
    """
    probe = (
        "for d in /proc/[0-9]*; do "
        "p=${d#/proc/}; "
        '[ "$p" = "$1" ] && continue; '
        'e=$(tr \'\\0\' \' \' < "$d/environ" 2>/dev/null) || continue; '
        f'case "$e" in *{marker}*) echo "$p";; esac; '
        "done"
    )
    return (
        f'; i=0; empty=1; '
        f'while [ "$i" -lt 100 ]; do '
        f"env -u {_FOREGROUND_MARKER_ENV} /bin/sh -c "
        # ``sh -c CMD [ARG...]``: the first argument after the command
        # string becomes $0, not $1 -- so a dummy name precedes the
        # wrapper's pid, which the probe then sees as $1 and excludes.
        f"{shlex.quote(probe)} _ \"$$\" > {waitf}; "
        f"if [ ! -s {waitf} ]; then rm -f {waitf}; empty=1; break; fi; "
        f"rm -f {waitf}; empty=0; i=$((i+1)); sleep 0.05; "
        "done; "
        # Reap every dead (adopted) child before exiting -- but only when
        # the tree actually emptied: after a bound break a live marked
        # child may remain, and an unconditional `wait` would block on it
        # (a user's deliberate background child).  The environ probe cannot
        # see zombies, so without this a just-killed adopted descendant
        # would be re-orphaned to PID 1 when the wrapper exits.
        '[ "$empty" = 1 ] && wait 2>/dev/null'
    )

#: Wall-clock bound for one reaper exec (probe / TERM / KILL).  A reaper
#: command that does not answer in time must not hang the timed-out call
#: forever; the verdict then honestly reports ``incomplete`` (survivors
#: unknown) rather than a silent success.
_REAP_EXEC_BOUND: float = 5.0

#: How long the reaper waits after signalling before probing again.
_REAP_SETTLE_S: float = 0.15

#: Upper bound on KILL-and-reprobe passes in one reap.  A marked tree can
#: respawn children (a TERM-trapped parent's ``while :; do sleep N; done``
#: loop spawns a fresh marked child the moment its current child dies), so
#: the reaper kills and probes again until clean -- bounded so a survivor
#: that can never be removed still lands an honest ``incomplete`` verdict.
_KILL_PASS_LIMIT: int = 3


class TreeTimeout(Exception):
    """A marked command tree exceeded its server-side deadline.

    Base of the two terminal timeout exceptions (verify and foreground).
    Raised by the scope's ``run_exec`` once the watchdog has finished
    reaping (or failed to).  Carries the diagnostics the terminal
    ``status: "timeout"`` result needs: the configured deadline, the reap
    verdict (``"ok"`` / ``"incomplete"`` / ``None`` if never observed), and
    the elapsed wall-clock time.
    """

    #: Human label for the deadline owner (verify / foreground), used in the
    #: exception message only.
    _label: str = "command"

    def __init__(
        self,
        deadline_s: float,
        reap: str | None,
        elapsed_s: float,
    ) -> None:
        super().__init__(
            f"{self._label} deadline {deadline_s:g}s exceeded (reap={reap})"
        )
        self.deadline_s = deadline_s
        self.reap = reap
        self.elapsed_s = elapsed_s


class VerifyTreeTimeout(TreeTimeout):
    """A verify-owned command exceeded the #910 deadline."""

    _label = "verify"


class ForegroundTreeTimeout(TreeTimeout):
    """A foreground exec (sandbox_exec / run_python / package_install)
    exceeded its ``SUNABA_FOREGROUND_TIMEOUT`` deadline."""

    _label = "foreground"


class _DeadlineScope:
    """Per-call deadline state shared between the calling thread and the
    watchdog/reaper thread.

    ``run_exec`` is the enforcement point: it executes *exec_fn* on a
    daemon runner thread and waits for it with a poll loop.  If the runner
    finishes before the deadline the result is returned unchanged (the
    deadline machinery is transparent on the success path); once the
    deadline has passed the call waits for the watchdog's reap verdict and
    raises the scope's terminal timeout exception (see *timeout_type*).
    Running the exec on a helper thread is what makes a *blocked*
    ``container.exec_run`` (a hung test run, or a mock container waiting on
    an event) abandonable: the reap kills the in-container tree -- or,
    under mocks, the verdict arrives from the watchdog -- and the owning
    thread can return the terminal timeout result without waiting for the
    blocked call.
    """

    def __init__(
        self,
        container: Any,
        deadline_s: float,
        marker: str,
        timeout_type: type[TreeTimeout],
    ) -> None:
        self.container = container
        self.deadline_s = deadline_s
        self.marker = marker
        self.timeout_type = timeout_type
        self.started_at = time.monotonic()
        self.deadline_at = self.started_at + deadline_s
        self.cancelled = False
        self.reap: str | None = None
        self.reap_completed = threading.Event()
        self._lock = threading.Lock()

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline_at

    def run_exec(self, exec_fn: Callable[[], Any]) -> Any:
        if self.expired():
            self._await_reap()
            raise self._timeout_error()
        result_box: dict[str, Any] = {}
        done = threading.Event()

        def _runner() -> None:
            try:
                result_box["value"] = exec_fn()
            except BaseException as e:  # noqa: BLE001 - propagated to caller
                result_box["error"] = e
            finally:
                done.set()

        threading.Thread(target=_runner, daemon=True).start()
        while True:
            if done.wait(timeout=0.05):
                if not self.expired():
                    if "error" in result_box:
                        raise result_box["error"]
                    return result_box["value"]
                # Finished just past the deadline: the tree was (or is
                # being) reaped; report the terminal timeout, never a
                # half-finished verdict.
                break
            if self.expired():
                break
        self._await_reap()
        raise self._timeout_error()

    def _await_reap(self) -> None:
        """Wait for the watchdog's verdict, bounded so the timed-out call
        cannot hang on a wedged reap."""
        self.reap_completed.wait(10.0)
        with self._lock:
            if self.reap is None:
                self.reap = "incomplete"

    def _timeout_error(self) -> TreeTimeout:
        return self.timeout_type(
            deadline_s=self.deadline_s,
            reap=self.reap,
            elapsed_s=time.monotonic() - self.started_at,
        )


class _VerifyDeadlineScope(_DeadlineScope):
    """#910 verify variant: raises :class:`VerifyTreeTimeout` on expiry.

    Kept as a distinct class (rather than a bare alias) so the #910 tests
    that construct it directly keep their contract.
    """

    def __init__(
        self,
        container: Any,
        deadline_s: float,
        marker: str,
    ) -> None:
        super().__init__(container, deadline_s, marker, VerifyTreeTimeout)


#: The calling thread's active verify deadline scope, if any.  Only the
#: thread that opened the scope (the verify call) is subject to it; every
#: other tool on every other thread keeps its previous synchronous contract.
_deadline_local = threading.local()


def _active_deadline_scope() -> _VerifyDeadlineScope | None:
    return getattr(_deadline_local, "scope", None)



def verify_marker_environment() -> dict[str, str] | None:
    """Marker environment for execs owned by the active verify call.

    Returns ``None`` when no deadline scope is active (the exec runs
    without a marker, exactly as before #910); otherwise the marker env
    that descendants inherit.
    """
    scope = _active_deadline_scope()
    if scope is None:
        return None
    return {_VERIFY_MARKER_ENV: scope.marker}


def run_exec_under_verify_deadline(exec_fn: Callable[[], Any]) -> Any:
    """Run *exec_fn*, enforcing the calling thread's deadline scope (if any).

    Without an active scope this is a plain synchronous call -- the
    deadline machinery costs nothing outside verify.  Inside a scope the
    result is the runner's return value, or :class:`VerifyTreeTimeout` is
    raised once the deadline has fired and the reap verdict is in.
    """
    scope = _active_deadline_scope()
    if scope is None:
        return exec_fn()
    return scope.run_exec(exec_fn)


def open_verify_deadline(
    container: Any,
    deadline_s: float,
    marker: str,
) -> None:
    """Arm the #910 deadline for the calling thread's verify call.

    *container* is the docker container object the verify runs against
    (the watchdog reaps through the same exec path).  ``deadline_s <= 0``
    disables the deadline.  The scope is cleared and the watchdog cancelled
    by :func:`close_verify_deadline` -- always called by the verify call's
    ``finally``.
    """
    if deadline_s <= 0:
        return
    scope = _VerifyDeadlineScope(container, deadline_s, marker)
    _deadline_local.scope = scope
    threading.Thread(
        target=_watchdog,
        args=(scope,),
        daemon=True,
        name="verify-deadline-watchdog",
    ).start()


def close_verify_deadline() -> None:
    """Cancel and clear the calling thread's active deadline scope (if any)."""
    scope = _active_deadline_scope()
    if scope is not None:
        scope.cancelled = True
    _deadline_local.scope = None


#: The calling thread's active foreground deadline scope, if any.  Separate
#: slot from the verify scope: a foreground tool call never runs inside a
#: verify call (each is a distinct synchronous MCP tool invocation), and
#: keeping the two independent means verify's surface cannot be perturbed
#: by the foreground generalization.
_foreground_local = threading.local()


def _active_foreground_scope() -> _DeadlineScope | None:
    return getattr(_foreground_local, "scope", None)


def foreground_marker_environment() -> dict[str, str] | None:
    """Marker environment for execs owned by the active foreground call.

    Returns ``None`` when no foreground deadline scope is active (the exec
    runs without a marker, exactly as before the foreground deadline
    feature); otherwise the private per-call marker env that descendants
    inherit, so the reaper can find the whole tree.
    """
    scope = _active_foreground_scope()
    if scope is None:
        return None
    return {_FOREGROUND_MARKER_ENV: scope.marker}


def run_exec_under_foreground_deadline(exec_fn: Callable[[], Any]) -> Any:
    """Run *exec_fn*, enforcing the calling thread's foreground deadline
    scope (if any).

    Without an active scope this is a plain synchronous call.  Inside a
    scope the result is the runner's return value, or
    :class:`ForegroundTreeTimeout` is raised once the deadline has fired
    and the reap verdict is in.
    """
    scope = _active_foreground_scope()
    if scope is None:
        return exec_fn()
    return scope.run_exec(exec_fn)


def open_foreground_deadline(
    container: Any,
    deadline_s: float,
    marker: str,
) -> None:
    """Arm the foreground deadline for the calling thread's tool call.

    *container* is the docker container object the call runs against (the
    watchdog reaps through the same exec path).  ``deadline_s <= 0``
    disables the deadline.  The scope is cleared and the watchdog cancelled
    by :func:`close_foreground_deadline` -- always called by the tool
    call's ``finally``.
    """
    if deadline_s <= 0:
        return
    scope = _DeadlineScope(container, deadline_s, marker, ForegroundTreeTimeout)
    _foreground_local.scope = scope
    threading.Thread(
        target=_watchdog,
        args=(scope,),
        daemon=True,
        name="foreground-deadline-watchdog",
    ).start()


def close_foreground_deadline() -> None:
    """Cancel and clear the calling thread's foreground deadline scope."""
    scope = _active_foreground_scope()
    if scope is not None:
        scope.cancelled = True
    _foreground_local.scope = None


def ensure_foreground_deadline(container: Any) -> None:
    """Arm the calling thread's foreground deadline if not already armed.

    Idempotent per call: the tools that resolve their container lazily
    (``package_install`` fetches it inside every exec helper) call this on
    the first exec, and later execs of the same call reuse the same scope
    and per-call marker.  A disabled deadline (``0``) arms nothing.
    """
    if _active_foreground_scope() is not None:
        return
    open_foreground_deadline(
        container,
        resolve_foreground_deadline(),
        new_foreground_marker(),
    )


def reap_foreground_tree(container: Any, marker: str) -> str:
    """Synchronously reap the marked foreground tree (TERM/KILL/probe).

    Used by the explicit ``timeout=N`` path of ``sandbox_exec``: when the
    ``timeout(1)`` wrapper already fired (exit 124), the direct command is
    gone but ordinary and setsid-detached descendants may survive, so the
    tool reaps them deterministically before returning the terminal
    ``status: "timeout"`` result.  Returns the reap verdict
    (``"ok"`` / ``"incomplete"``).
    """
    return _reap_tree(container, marker)


def _watchdog(scope: _DeadlineScope) -> None:
    """Wait for the deadline, then reap the marked tree exactly once.

    Sleeps in short increments so an early scope close (a call that
    finished in time) cancels it promptly without ever touching the tree.
    """
    while not scope.cancelled:
        remaining = scope.deadline_at - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    if scope.cancelled:
        return
    # Bound before the try: if the reap itself raises, every exception
    # path still lands a diagnostic verdict ("incomplete" -- survivors
    # unknown) instead of leaving ``scope.reap`` unset.
    verdict: str = "incomplete"
    try:
        verdict = _reap_verify_tree(scope)
    finally:
        with scope._lock:
            scope.reap = verdict
        scope.reap_completed.set()


def _reap_verify_tree(scope: _DeadlineScope) -> str:
    """TERM descendants-first, wait, KILL survivors, then report.

    Kept as a distinct name (delegating to the shared :func:`_reap_tree`)
    because the #910 tests patch ``sunaba.edit_verify.shell._reap_verify_tree``
    to prove the watchdog lands a bound diagnostic verdict even when the
    reaper raises; the foreground machinery reaps through the same path.

    Returns ``"ok"`` only when the final probe finds no marked process;
    ``"incomplete"`` when survivors remain or a probe could not answer
    (survivors unknown).  Never claims success with survivors.
    """
    return _reap_tree(scope.container, scope.marker)


def _reap_tree(container: Any, marker: str) -> str:
    """Reap the whole marked tree: TERM descendants-first, wait, KILL
    survivors, then report.

    Shared by the verify and foreground deadlines (the only difference
    between the two is which marker string identifies the tree).  Returns
    ``"ok"`` only when the final probe finds no marked process;
    ``"incomplete"`` when survivors remain or a probe could not answer
    (survivors unknown).  Never claims success with survivors.
    """
    pids = _probe_marked_pids(container, marker)
    if pids is None:
        return "incomplete"
    _signal_tree(container, pids, "TERM")
    time.sleep(_REAP_SETTLE_S)
    # KILL-and-reprobe passes (bounded): a TERM-trapped parent can respawn
    # a fresh marked child while it is still alive, so one KILL pass is not
    # enough.  The direct exec process(es) -- the marked processes whose
    # own parent is not marked -- are the roots: they must outlive their
    # descendants (a wrapper/subreaper reaps its adopted orphans, or a
    # normal shell parent reaps its children), so they are killed only once
    # a pass confirms no non-root survivor remains.  Killing a root while a
    # respawned descendant is still alive would orphan it to PID 1, where
    # it becomes a permanent zombie in a no-init container.  A root that
    # *keeps* respawning (a TERM-trapped plain script looping forever, as
    # in verify's real descendant test) can never be left alone by the
    # probe, so the final pass falls back to the #910 one-shot form and
    # kills every marked pid in a single command -- the root dies in the
    # same instant as its children and cannot respawn between generations.
    for pass_no in range(_KILL_PASS_LIMIT):
        pids = _probe_marked_pids(container, marker)
        if pids is None:
            return "incomplete"
        if not pids:
            return "ok"
        pid_set = {p["pid"] for p in pids}
        if pass_no == _KILL_PASS_LIMIT - 1:
            _signal_tree(container, pids, "KILL", kill_all=True)
            time.sleep(_REAP_SETTLE_S)
            pids = _probe_marked_pids(container, marker)
            if pids is None:
                return "incomplete"
            return "ok" if not pids else "incomplete"
        if any(not _is_root(p, pid_set) for p in pids):
            _signal_tree(container, pids, "KILL", kill_roots=False)
            time.sleep(_REAP_SETTLE_S)
            continue
        # Only roots remain: kill them last (their descendants are already
        # collected) and let the exec session reap the direct exec.
        _signal_tree(container, pids, "KILL", kill_roots=True)
        time.sleep(_REAP_SETTLE_S)
        pids = _probe_marked_pids(container, marker)
        if pids is None:
            return "incomplete"
        return "ok" if not pids else "incomplete"
    pids = _probe_marked_pids(container, marker)
    if pids is None:
        return "incomplete"
    return "ok" if not pids else "incomplete"


def _probe_cmd(marker: str) -> str:
    """Shell loop listing ``<pid> <ppid>`` for every process whose
    environment carries *marker*.

    Scans ``/proc`` directly -- the sandbox images ship no ``ps``.  The
    ppid (``/proc/<pid>/stat`` field 4) lets the reaper TERM descendants
    before their parents.  The marker is matched only against ``environ``
    so the probe never matches itself (its own cmdline carries the marker
    text but its environment does not).
    """
    return (
        "for d in /proc/[[:digit:]]*; do "
        f"e=$(tr '\\0' ' ' < \"$d/environ\" 2>/dev/null); "
        f"case \"$e\" in *{marker}*) "
        "s=$(cat \"$d/stat\" 2>/dev/null); s=${s#*)}; set -- $s; "
        'echo "${d#/proc/} $2";; esac; done'
    )


def _bounded_exec(
    container: Any,
    cmd: list[str],
) -> tuple[int, tuple[bytes, bytes]] | None:
    """Run one reaper exec with a wall-clock bound.

    Returns ``(exit_code, (stdout, stderr))`` on success, or ``None`` when
    the exec raised or did not answer within :data:`_REAP_EXEC_BOUND` (the
    caller treats that as an unknown outcome, never as a clean probe).
    """
    box: dict[str, Any] = {}
    done = threading.Event()

    def _run() -> None:
        try:
            box["value"] = container.exec_run(
                cmd, stdout=True, stderr=True, demux=True,
            )
        except BaseException as e:  # noqa: BLE001 - reaper never raises
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    done.wait(_REAP_EXEC_BOUND)
    if "value" in box:
        return box["value"]
    return None


def _probe_marked_pids(
    container: Any,
    marker: str,
) -> list[dict[str, int]] | None:
    """Return ``[{"pid": ..., "ppid": ...}, ...]`` for the marked tree.

    ``None`` means the probe could not answer (survivors unknown) --
    distinct from an empty list (no survivors).
    """
    result = _bounded_exec(container, ["/bin/sh", "-c", _probe_cmd(marker)])
    if result is None:
        return None
    _exit_code, (out_bytes, _err_bytes) = result
    text = out_bytes.decode("utf-8", errors="replace") if out_bytes else ""
    return _parse_probe_output(text)


def _parse_probe_output(raw: str) -> list[dict[str, int]]:
    """Parse probe output into pid/ppid entries.

    Accepts both the reaper's ``<pid> <ppid>`` lines and a ps-style or
    ``/proc/<pid>``-carrying line (the contract-level mock answers with
    either), extracting every PID mentioned.  A missing ppid (ps-style
    second column that is not a number) leaves the entry without one.
    """
    entries: list[dict[str, int]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        proc_match = re.search(r"/proc/(\d+)", line)
        if proc_match:
            pid = int(proc_match.group(1))
        else:
            head_match = re.match(r"(\d+)", line)
            if not head_match:
                continue
            pid = int(head_match.group(1))
        entry: dict[str, int] = {"pid": pid}
        parts = line.split()
        if len(parts) >= 2:
            try:
                entry["ppid"] = int(parts[1])
            except ValueError:
                pass
        if entry not in entries:
            entries.append(entry)
    return entries


def _is_root(p: dict[str, int], pid_set: set[int]) -> bool:
    """Whether a marked process is a direct exec root: its own parent is
    not marked, so it is the exec session's child and docker reaps it when
    it dies.  An entry without a parent (the contract-level mock) is a
    descendant, so the mocked choreography exercises the signal path."""
    ppid = p.get("ppid")
    return ppid is not None and ppid not in pid_set


def _signal_tree(
    container: Any,
    entries: list[dict[str, int]],
    signal: str,
    kill_roots: bool = False,
    kill_all: bool = False,
) -> None:
    """Send *signal* (``"TERM"`` / ``"KILL"``) to the marked tree, one
    generation at a time (leaves first) with a settle between generations.

    Descendants are signalled before their parents (the TERM contract of
    #910: children die first so a parent cannot re-spawn or outlive them),
    and -- crucially for a no-init container whose PID 1 never reaps -- a
    generation is only signalled after the previous one has been collected
    by its still-alive parent, so no orphaned zombie is ever created.  The
    direct exec process (a marked process whose own parent is not marked)
    is excluded from the TERM stage and, in the KILL stage, is signalled
    only when *kill_roots* is true (the caller does that only after a pass
    confirms no non-root survivor remains): it is the exec session's child,
    docker reaps it when it dies, and killing it before its descendants
    would orphan them to PID 1.

    *kill_all* is the #910 one-shot form: every marked pid is signalled in
    a single command with no settling between generations.  It is the last
    resort for a tree whose root keeps respawning children, so the root
    dies in the same instant as its children and cannot respawn between
    generations.

    The command is issued even with no PIDs -- the reaper choreography is
    always the same and the verdict comes from the probe, never from the
    signal command's exit code.
    """
    if not entries:
        _bounded_exec(container, ["/bin/sh", "-c", f"kill -{signal} "])
        return
    if kill_all:
        ordered = sorted(p["pid"] for p in entries)
        _bounded_exec(
            container,
            ["/bin/sh", "-c", f"kill -{signal} " + " ".join(map(str, ordered))],
        )
        return
    pid_set = {p["pid"] for p in entries}
    roots = {p["pid"] for p in entries if _is_root(p, pid_set)}
    children: dict[int, list[int]] = {}
    for p in entries:
        ppid = p.get("ppid")
        if ppid is not None and ppid in pid_set:
            children.setdefault(ppid, []).append(p["pid"])
    depth: dict[int, int] = {}

    def _depth(pid: int) -> int:
        if pid in depth:
            return depth[pid]
        ch = children.get(pid, [])
        d = 0 if not ch else 1 + max(_depth(c) for c in ch)
        depth[pid] = d
        return d

    for p in entries:
        _depth(p["pid"])
    generations: dict[int, list[int]] = {}
    for pid, d in depth.items():
        generations.setdefault(d, []).append(pid)
    for d in range(max(generations) + 1):
        gen = sorted(p for p in generations.get(d, []) if p not in roots)
        if not gen:
            continue
        _bounded_exec(
            container,
            ["/bin/sh", "-c", f"kill -{signal} " + " ".join(map(str, gen))],
        )
        time.sleep(_REAP_SETTLE_S)
    if signal == "KILL" and roots and kill_roots:
        # Roots last: their descendants have already been collected by
        # them (or by the chain's subreaper), so killing the direct exec
        # process leaves nothing orphaned behind.
        time.sleep(_REAP_SETTLE_S)
        _bounded_exec(
            container,
            ["/bin/sh", "-c", f"kill -{signal} " + " ".join(map(str, sorted(roots)))],
        )
