"""Shell-command building blocks: path quoting and environment prefixes.

These are used by runners in :mod:`sunaba.edit_verify` to construct
shell commands that execute inside sandbox containers.

Since issue #910 this module also owns the *server-side verify deadline*:
a per-call wall-clock bound armed by :func:`open_verify_deadline` and
enforced by :func:`_exec_run` (and by verify's direct ``_run`` path via
:func:`run_exec_under_verify_deadline`).  When the deadline fires, a
watchdog reaps the whole marked command tree -- TERM descendants-first,
KILL as fallback -- and the owning thread raises
:class:`VerifyTreeTimeout` instead of returning a half-finished verdict.
"""

from __future__ import annotations

import re
import shlex
import threading
import time
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
# Server-side verify deadline (issue #910)
# ---------------------------------------------------------------------------
#
# A verify call that outlives its deadline must terminate and reap its whole
# in-container command tree before the MCP client's ~300s tool-call wait,
# and the timed-out call must return a terminal ``status: "timeout"`` result
# instead of letting a transport timeout masquerade as a failed gate.
#
# Mechanics: the verify call opens a *deadline scope* on its own thread.
# Every exec owned by the call (``_exec_run`` here and verify's direct
# ``_run`` path via :func:`run_exec_under_verify_deadline`) runs under that
# scope and inherits a unique marker through the environment, so descendants
# -- including setsid-detached test children -- are identifiable.  A
# watchdog thread armed by :func:`open_verify_deadline` fires at the
# deadline and reaps the marked tree through the same container exec:
# find the marker in ``/proc`` (the sandbox images have no ``ps``), TERM
# descendants first, wait briefly, KILL survivors, then probe again for the
# reap verdict.  The owning thread, unblocked by the reap (or by a mock
# container releasing), raises :class:`VerifyTreeTimeout` carrying the
# verdict; the caller converts it into the terminal timeout result.

#: Environment variable carrying the per-call marker into every command the
#: verify owns.  Descendants inherit it, and the reaper matches it against
#: ``/proc/<pid>/environ`` -- never against the reaper's own exec (the
#: reaper passes no environment, so it cannot reap itself).
_VERIFY_MARKER_ENV: str = "SUNABA_VERIFY_MARKER"

#: Wall-clock bound for one reaper exec (probe / TERM / KILL).  A reaper
#: command that does not answer in time must not hang the timed-out call
#: forever; the verdict then honestly reports ``incomplete`` (survivors
#: unknown) rather than a silent success.
_REAP_EXEC_BOUND: float = 5.0

#: How long the reaper waits after signalling before probing again.
_REAP_SETTLE_S: float = 0.15


class VerifyTreeTimeout(Exception):
    """A verify-owned command exceeded the #910 deadline.

    Raised by :func:`run_exec_under_verify_deadline` once the watchdog has
    finished reaping (or failed to).  Carries the diagnostics the terminal
    ``status: "timeout"`` result needs: the configured deadline, the reap
    verdict (``"ok"`` / ``"incomplete"`` / ``None`` if never observed), and
    the elapsed wall-clock time.
    """

    def __init__(
        self,
        deadline_s: float,
        reap: str | None,
        elapsed_s: float,
    ) -> None:
        super().__init__(
            f"verify deadline {deadline_s:g}s exceeded (reap={reap})"
        )
        self.deadline_s = deadline_s
        self.reap = reap
        self.elapsed_s = elapsed_s


class _VerifyDeadlineScope:
    """Per-call deadline state shared between the calling thread and the
    watchdog/reaper thread.

    ``run_exec`` is the enforcement point: it executes *exec_fn* on a
    daemon runner thread and waits for it with a poll loop.  If the runner
    finishes before the deadline the result is returned unchanged (the
    deadline machinery is transparent on the success path); once the
    deadline has passed the call waits for the watchdog's reap verdict and
    raises :class:`VerifyTreeTimeout`.  Running the exec on a helper thread
    is what makes a *blocked* ``container.exec_run`` (a hung test run, or a
    mock container waiting on an event) abandonable: the reap kills the
    in-container tree -- or, under mocks, the verdict arrives from the
    watchdog -- and the owning thread can return the terminal timeout
    result without waiting for the blocked call.
    """

    def __init__(
        self,
        container: Any,
        deadline_s: float,
        marker: str,
    ) -> None:
        self.container = container
        self.deadline_s = deadline_s
        self.marker = marker
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

    def _timeout_error(self) -> VerifyTreeTimeout:
        return VerifyTreeTimeout(
            deadline_s=self.deadline_s,
            reap=self.reap,
            elapsed_s=time.monotonic() - self.started_at,
        )


#: The calling thread's active deadline scope, if any.  Only the thread
#: that opened the scope (the verify call) is subject to it; every other
#: tool on every other thread keeps its previous synchronous contract.
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


def _watchdog(scope: _VerifyDeadlineScope) -> None:
    """Wait for the deadline, then reap the marked tree exactly once.

    Sleeps in short increments so an early scope close (a verify that
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


def _reap_verify_tree(scope: _VerifyDeadlineScope) -> str:
    """TERM descendants-first, wait, KILL survivors, then report.

    Returns ``"ok"`` only when the final probe finds no marked process;
    ``"incomplete"`` when survivors remain or a probe could not answer
    (survivors unknown).  Never claims success with survivors.
    """
    container = scope.container
    marker = scope.marker
    pids = _probe_marked_pids(container, marker)
    if pids is None:
        return "incomplete"
    _signal_pids(container, pids, "TERM")
    time.sleep(_REAP_SETTLE_S)
    pids = _probe_marked_pids(container, marker)
    if pids is None:
        return "incomplete"
    if not pids:
        return "ok"
    _signal_pids(container, pids, "KILL")
    time.sleep(_REAP_SETTLE_S)
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


def _signal_pids(
    container: Any,
    pids: list[dict[str, int]],
    signal: str,
) -> None:
    """Send *signal* (``"TERM"`` / ``"KILL"``) to the marked PIDs.

    Descendants are signalled before their parents (the TERM contract of
    #910: children die first so a parent cannot re-spawn or outlive them).
    The command is issued even with no PIDs -- the reaper choreography is
    always the same and the verdict comes from the probe, never from the
    signal command's exit code.
    """
    ordered = _descendant_first(pids)
    cmd = ["/bin/sh", "-c", "kill -" + signal + " " + " ".join(map(str, ordered))]
    _bounded_exec(container, cmd)


def _descendant_first(pids: list[dict[str, int]]) -> list[int]:
    """Order marked PIDs so children precede parents (leaves first)."""
    pid_set = {p["pid"] for p in pids}
    children: dict[int, list[int]] = {}
    for p in pids:
        ppid = p.get("ppid")
        if ppid is not None and ppid in pid_set:
            children.setdefault(ppid, []).append(p["pid"])
    order: list[int] = []
    visited: set[int] = set()

    def _visit(pid: int) -> None:
        if pid in visited:
            return
        for child in children.get(pid, []):
            _visit(child)
        visited.add(pid)
        order.append(pid)

    for p in pids:
        _visit(p["pid"])
    return order
