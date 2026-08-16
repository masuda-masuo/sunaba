"""Capture-health guard: fail-loud detection of a broken exec-output capture path.

Issue #852 (incident 2026-08-11): after ~8h uptime every output-bearing tool
returned ``status: ok`` with empty output for all containers at once, while
the commands themselves still executed (side effects landed).  ``docker exec``
from the host worked, raw JSON-RPC reproduced it, and no exception reached the
journal -- only a service restart recovered it.  The harm is the fail-open
shape: ``ok`` + empty is indistinguishable from "the command legitimately
printed nothing", so callers (and delegation workers mid-chain) act on phantom
empty results.

The guard state is **per container**, not server-global (issue #852 review):
a consecutive-empty counter and a ``capture_broken`` flag live in a dict keyed
by the triggering call's ``container_id``.  Per-container keying is
deliberate.  The incident was server-wide, but a single-container capture
break must not be masked by healthy traffic from other containers: with a
global counter, any non-empty decode from *any* container reset it, and the
canary could land on a healthy container, so the guard never tripped for the
broken one and phantom empty results kept flowing.  Here the counter climbs
only on empties from the same container, the canary always probes the
container whose own counter tripped, and one container's non-empty decode
cannot clear another container's flag (no cross-container recovery flap).

This module owns two pieces of state per container:

* a **consecutive-empty counter**, fed at every exec-output decode whose
  result is returned to the client (the fail-open surface); any non-empty
  decode from the same container resets it;
* a **``capture_broken`` flag**, set when a canary -- an ``echo <nonce>`` run
  through the *same* ``container.exec_run(..., demux=True)`` capture path that
  broke -- fails to return its nonce.

Canary outcomes are classified into three states, each with its own client
message and journal state:

* **nonce found** -- capture is healthy; the counter resets and the call is
  served normally;
* **empty nonce** -- the incident class: the probe ran but capture silently
  returned nothing.  The loud error names the breakage and the remedy
  (``systemctl --user restart sunaba``);
* **probe exception** -- the canary itself raised (e.g. the container was
  stopped or removed).  The error names the container and the exception, and
  the remedy is container-level (verify the container is alive), *not* the
  server restart -- a dead container cannot be fixed by restarting the
  server, and conflating the classes would send the operator down the wrong
  recovery path.  The journal records the exception for forensics.

It is **detection only**: no automatic restart, no docker client re-creation,
no self-healing.  Self-healing would hide the incident from post-mortem
analysis; the whole point is to convert a silent fail-open into a loud,
actionable error that names the remedy.
"""

from __future__ import annotations

import json
import secrets
import threading
from typing import Any, Literal

from sunaba.journal import record_capture_health

#: Consecutive empty decoded outputs (per container) that trigger a canary.
EMPTY_TRIGGER: int = 3

#: The remedy every loud error must name.  A refusal without an
#: alternative pushes callers to worse paths.
RESTART_REMEDY: str = "systemctl --user restart sunaba"

#: Message returned to the client when capture is broken (empty-nonce
#: class).  It MUST carry :data:`RESTART_REMEDY` (tested).
CAPTURE_BROKEN_MESSAGE: str = (
    "exec output capture is broken server-side; results are not "
    "trustworthy. Restart the server to recover: "
    f"`{RESTART_REMEDY}`"
)

#: Client-facing message template when the canary probe itself raises
#: (container/channel-level failure).  Names the container and the
#: exception; the remedy is container-level, deliberately NOT
#: :data:`RESTART_REMEDY` -- a dead container cannot be fixed by
#: restarting the server.
CANARY_PROBE_ERROR_TEMPLATE: str = (
    "exec output capture could not be verified for container "
    "{container_id}: the canary probe itself failed ({error}). Results "
    "are not trustworthy. This is treated as a container/channel-level "
    "failure (e.g. the container was stopped or removed) rather than the "
    "#852 server-wide empty-capture break; verify the container is alive "
    "(e.g. `sandbox_list_containers`) and retry. The journal records the "
    "probe error for forensics."
)

#: Canary outcome classification: the nonce came back, the probe ran but
#: returned no nonce (incident class), or the probe itself raised.
CanaryOutcome = Literal["nonce_found", "empty", "error"]

#: Guard-state key when a call site provides no container attribution.
_UNKNOWN_CONTAINER: str = "<unknown>"


class _CaptureState:
    """Per-container guard state (guarded by ``_lock``)."""

    __slots__ = ("consecutive_empty", "capture_broken")

    def __init__(self) -> None:
        self.consecutive_empty: int = 0
        self.capture_broken: bool = False


#: Per-container guard state, keyed by container_id (12-char prefix as the
#: call sites pass it).
_state: dict[str, _CaptureState] = {}
_lock = threading.Lock()


def _state_for(container_id: str | None) -> _CaptureState:
    """Return (creating if needed) the guard state for *container_id*.

    Caller must hold ``_lock``.
    """
    key = container_id or _UNKNOWN_CONTAINER
    st = _state.get(key)
    if st is None:
        st = _CaptureState()
        _state[key] = st
    return st


def reset() -> None:
    """Reset the guard to its initial, healthy state.

    Intended for tests: guard state must not leak between cases.
    Production never calls it.
    """
    with _lock:
        _state.clear()


def prune(container_id: str | None) -> None:
    """Drop the guard state of one container (container stopped/removed).

    Keeps the per-container dict bounded: entries are only created for
    containers that produced output-bearing calls, and must not linger
    after the container is gone.  Called by ``sandbox_stop`` and the idle
    reaper once the container is positively removed.  A later call for
    the same id starts fresh.
    """
    with _lock:
        _state.pop(container_id or _UNKNOWN_CONTAINER, None)


def consecutive_empty(container_id: str) -> int:
    """Return the consecutive-empty count for one container (test hook)."""
    with _lock:
        st = _state.get(container_id)
        return st.consecutive_empty if st else 0


def is_capture_broken(container_id: str) -> bool:
    """Return whether capture is believed broken for one container."""
    with _lock:
        st = _state.get(container_id)
        return st.capture_broken if st else False


def _run_canary(container: Any, nonce: str) -> tuple[CanaryOutcome, str | None]:
    """Probe the capture path through the same docker SDK call that broke.

    ``echo <nonce>`` writes the nonce to stdout; with ``demux=True`` the
    decoded stdout must contain it.  Deliberately does not "check" via any
    alternate channel (that would be a guard approximating another resolver,
    and it drifts).

    Returns ``(outcome, error)``:

    * ``"nonce_found"`` -- the nonce came back through the capture path;
    * ``"empty"`` -- the probe ran but the nonce did not come back (the
      incident class: silent empty capture);
    * ``"error"`` -- the probe itself raised; *error* is the exception repr.
    """
    try:
        _exit_code, output = container.exec_run(
            ["echo", nonce],
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout_part, _stderr_part = output
        stdout_text = (
            stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        )
        if nonce in stdout_text:
            return "nonce_found", None
        return "empty", None
    except Exception as e:  # noqa: BLE001 - the canary must never raise into a tool
        return "error", repr(e)


def _journal(
    state: str,
    *,
    counter: int,
    canary_nonce_found: bool | None,
    container_id: str | None,
    canary_error: str | None = None,
) -> None:
    """Write a guard state change to the journal, never raising.

    Journaling is best-effort by design: a failure to observe must not
    turn detection into a crash -- the loud client error is the primary
    channel, the journal is the post-incident audit trail.
    """
    try:
        record_capture_health(
            container_id,
            state,
            consecutive_empty=counter,
            canary_nonce_found=canary_nonce_found,
            canary_error=canary_error,
        )
    except Exception:  # noqa: BLE001 - see docstring
        pass


def check_capture(
    container: Any,
    *,
    decoded_empty: bool,
    container_id: str | None = None,
) -> str | None:
    """Feed the guard with one decoded output and gate the call.

    Call this exactly once per output-bearing call, right where its exec
    output is decoded, passing:

    * ``decoded_empty`` -- ``True`` when the decoded output was empty
      (the fail-open shape), ``False`` when anything came back; and
    * ``container`` -- the docker container object the output came from,
      used to run the canary through the same ``exec_run`` capture path.

    Returns ``None`` when the call should be served normally, or a JSON
    error string (``{"status": "error", ...}``) when capture is broken
    and the caller must return it in place of its result.

    State is per ``container_id``: the counter, the flag, and the canary
    target all belong to the triggering call's container, so one
    container's traffic cannot mask or clear another container's state.

    While ``capture_broken`` is set for the container, an empty decode
    re-probes with one canary first: a healthy nonce clears the flag
    (journaling the recovery) and serves the call normally, so a one-off
    false trip can never lock a healthy server out.  A non-empty decode is
    direct evidence that capture works for that container, so it resets the
    counter and, if the flag was set, clears it the same way.

    Canary outcomes classify into three states: ``nonce_found`` serves the
    call; ``empty`` sets/keeps ``capture_broken`` and returns the loud
    breakage error naming :data:`RESTART_REMEDY`; ``error`` (the probe
    raised) sets/keeps the flag too but returns the container-level error
    from :data:`CANARY_PROBE_ERROR_TEMPLATE` instead -- the restart command
    is wrong for a dead container, and the journal carries the exception
    repr so forensics can tell the classes apart.

    Journaling is transition-only: the False->True trip and the
    True->False recovery each write one entry carrying the counter value
    and canary result.  Failed re-probes while already broken return the
    loud error without journaling, so a broken server under load does
    not spam the journal with ``consecutive_empty=0`` entries.
    """
    if not decoded_empty:
        with _lock:
            st = _state_for(container_id)
            recovered = st.capture_broken
            st.consecutive_empty = 0
            st.capture_broken = False
        if recovered:
            _journal(
                "recovered",
                counter=0,
                canary_nonce_found=None,
                container_id=container_id,
            )
        return None

    with _lock:
        st = _state_for(container_id)
        if st.capture_broken:
            should_probe = True
        else:
            st.consecutive_empty += 1
            should_probe = st.consecutive_empty >= EMPTY_TRIGGER
        counter = st.consecutive_empty

    if not should_probe:
        return None

    nonce = secrets.token_hex(8)
    outcome, canary_error = _run_canary(container, nonce)

    if outcome == "nonce_found":
        with _lock:
            st = _state_for(container_id)
            was_broken = st.capture_broken
            st.consecutive_empty = 0
            st.capture_broken = False
        if was_broken:
            _journal(
                "recovered",
                counter=0,
                canary_nonce_found=True,
                container_id=container_id,
            )
        return None

    with _lock:
        # Journal "broken" only on the False->True transition.  While
        # already broken, a failed re-probe is not a new trip: journaling it
        # would write one entry per output-bearing call, and the counter was
        # reset at the trip, so the entries would read "tripped at zero
        # empties" and hide the real value.  The trip entry below preserves
        # the snapshot counter that triggered it; failed re-probes just
        # return the loud error.
        st = _state_for(container_id)
        was_broken = st.capture_broken
        st.capture_broken = True
        st.consecutive_empty = 0
    if not was_broken:
        _journal(
            "broken",
            counter=counter,
            canary_nonce_found=False,
            container_id=container_id,
            canary_error=canary_error,
        )
    if outcome == "error":
        return json.dumps(
            {
                "status": "error",
                "error": CANARY_PROBE_ERROR_TEMPLATE.format(
                    container_id=container_id or _UNKNOWN_CONTAINER,
                    error=canary_error,
                ),
            }
        )
    return json.dumps({"status": "error", "error": CAPTURE_BROKEN_MESSAGE})
