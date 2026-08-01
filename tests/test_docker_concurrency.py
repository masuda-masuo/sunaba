"""Regression tests for issue #784: wedged docker calls must not exhaust the
shared anyio thread limiter.

The wedge repro (issue #784 diagnosis, 2026-08-01):

* A **wedge** is a container whose docker API never answers (unhealthy /
  hung container, dead docker daemon path, ...).  A docker call against it
  blocks for the docker-py default (~60s) or, in the pathological case,
  indefinitely.
* Every sunaba tool is a sync ``def`` and FastMCP 3.x runs sync tools on
  anyio's **shared default thread limiter (40 tokens)**.  A wedged docker
  call holds its limiter token for the whole block, so once ~40 wedged calls
  pile up (parallel kusabi workers polling/retrying), *every* sync tool --
  docker-free ones included -- queues behind them, across all sessions.
  Measured on the real server: with 60 wedged ``sandbox_exec`` calls,
  docker-free ``get_workflow_guide`` took 4.6s instead of 0.01s.

The fake docker factory below reproduces the wedge **without real docker**:
``containers.get()`` for a wedged container id sleeps (the blocked docker
API call) and then raises (the call eventually failing), while non-wedged
ids answer instantly.  The fix under test is the concurrency decorators in
``sunaba.tools.common``, applied at registration in ``sunaba.server``:

* ``docker_bound`` -- a two-level semaphore cap (global 24 / per-container
  6) for docker-bound tools; docker-free tools stay unwrapped and thus
  never wait on docker capacity.  Acquisition is **non-blocking by default**
  (``SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS`` defaults to 0): overflow calls
  return the structured busy JSON in milliseconds and hold no limiter
  tokens.
* ``recovery_bound`` -- a dedicated recovery pool (``SUNABA_RECOVERY_CONCURRENCY``,
  default 4, no per-container cap) for ``sandbox_stop`` /
  ``sandbox_list_containers``, so the documented escape hatch stays callable
  exactly when the docker caps are saturated (the #784 scenario).

Review round (issue #784 follow-up) also covers:

* ``docker_bound(..., key_on=...)`` -- tools whose container argument is
  not named ``container_id`` (``sandbox_attach``'s ``name_or_id``) declare
  it explicitly and keep per-container isolation; and
* ``sandbox_stop(force=False)`` failing **closed** when the unpushed-
  checkpoint guard cannot positively verify "no checkpoints" (busy refusal,
  docker/exec error, unparseable response) instead of destroying the
  container without the Issue #264 warning.

Review round 3 (issue #784) additionally covers:

* the checkpoint guard's inner docker calls running under
  ``RECOVERY_DOCKER_TIMEOUT`` (not docker-py's 60s default); and
* the guard failing closed on exec-level git failures that do not carry
  git's literal "not a git repository" marker (the only positive
  no-repo proof; anything else -- corrupted .git, missing git, dubious
  ownership, OOM -- refuses with the force=True override hint).

Review round 4 (issue #784) additionally covers:

* a HARD wall-clock deadline on the guard: docker-py reads the hijacked
  exec output stream with an unbounded poll (the client timeout only
  bounds the HTTP phases), so a wedged exec can block *indefinitely* --
  the guard runs its inner calls on a daemon worker and refuses
  fail-closed when the recovery timeout elapses, releasing the recovery
  permit even when ``exec_run`` never returns (the fake models exactly
  that: no self-imposed read error); and
* hardening the wall-clock wedge measurements against pytest-xdist
  worker descheduling (relaxed ``_latency_limit`` + more retries under
  xdist, wider wedge margin).

These tests fail on the base code (no caps): firing more concurrent
``sandbox_exec`` calls than the limiter has tokens makes docker-free /
other-container calls wait out the wedge, blowing the latency assertions.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
import time
from typing import Any

import pytest

from sunaba.tools.common import docker_bound, recovery_bound

#: How long the fake docker "wedges" (blocks) before failing, seconds.
#: Tests fire more concurrent calls than the caps/limiter and measure while
#: the wedge is still in flight, so this must comfortably exceed the
#: measurement delay: on base code the measured call waits until the first
#: wedge batch clears, i.e. ~WEDGE_SECONDS - MEASURE_AFTER_SECONDS, and the
#: latency assertions must fail there by a wide margin (4.0 - 0.8 = 3.2s).
WEDGE_SECONDS = 4.0

#: Delay (seconds) after firing the wedge burst before measuring.
MEASURE_AFTER_SECONDS = 0.8

#: How many concurrent docker-bound calls each wedge test fires.  Exceeds the
#: anyio default thread limiter (40), so on base code docker-free / other
#: container calls queue behind the wedge and blow the latency assertions.
BURST = 48

#: Latency (seconds) a docker-free / other-container call must stay under
#: while the wedge burst is in flight.  Serial default; see
#: :func:`_latency_limit` for the pytest-xdist relaxation (issue #784 review
#: round 4: the parallel gate flaked once on worker descheduling).
LATENCY_LIMIT = 1.0


def _latency_limit() -> float:
    """Wall-clock latency bound for the wedge measurements.

    Serial runs keep the strict :data:`LATENCY_LIMIT` (1.0s).  Under
    pytest-xdist the worker can be descheduled ~1.4s mid-measurement (the
    issue #784 review round 4 flake), so relax to 2.0s there -- still far
    below the base-code reading (~3.2s = WEDGE_SECONDS 4.0 -
    MEASURE_AFTER_SECONDS 0.8), so the assertion keeps its discriminating
    power under parallel load while a healthy measurement with a single
    deschedule stays under the bound.
    """
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return 2.0
    return LATENCY_LIMIT


# ---------------------------------------------------------------------------
# Fake docker: wedges only the configured container ids
# ---------------------------------------------------------------------------


class _FakeContainer:
    """Minimal docker container: every exec succeeds with empty output, or
    fails when *exec_fail* is set (to exercise tools' error paths).  With
    *exec_hang* the exec blocks like a wedged container in the worst way:
    docker-py reads the hijacked exec output stream with an unbounded poll
    (the client timeout only bounds the HTTP phases), so a wedged exec
    NEVER returns -- ``exec_run`` sleeps effectively forever.  That is the
    exact behavior the guard's hard wall-clock deadline must survive
    (issue #784 review round 4): the fake does NOT self-impose a
    client-timeout read error, because docker-py does not provide one.
    """

    def __init__(self, exec_fail: bool = False, exec_hang: bool = False) -> None:
        self._exec_fail = exec_fail
        self._exec_hang = exec_hang

    def exec_run(self, cmd: Any, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        if self._exec_hang:
            # Never returns: a wedged exec output read blocks indefinitely
            # no matter the client timeout.  Runs on the guard's daemon
            # worker thread, so the test process exits cleanly regardless.
            time.sleep(1e9)
            raise AssertionError("unreachable")
        if self._exec_fail:
            return 1, (b"checkpoint check failed (fake)", b"")
        return 0, (b"", b"")


class _FakeContainers:
    """``containers.get`` that wedges for the configured prefixes.

    *wedged* maps a 12-char container id prefix to a remaining wedge budget:
    ``None`` = wedge forever, ``0`` = no longer wedged, ``N`` = wedge the
    next N calls.  A wedged ``get`` sleeps :data:`WEDGE_SECONDS` (the docker
    API call that never answers) then raises (the call eventually failing);
    the sandbox tool turns the raise into its normal error JSON, so the
    wedged calls drain by themselves once the sleep expires.

    ``list`` answers instantly with no containers and counts its calls, so
    tests can prove ``sandbox_list_containers`` really reached the docker
    client (and not a busy refusal).  *exec_fail* makes every returned
    container's ``exec_run`` fail; *get_error* makes ``get`` raise a generic
    docker-level error immediately -- either exercises error paths that
    would otherwise need a real docker failure.  *exec_hang* makes every
    returned container's ``exec_run`` block forever (see
    :class:`_FakeContainer`).
    """

    def __init__(
        self,
        wedged: dict[str, int | None],
        wedge_seconds: float,
        exec_fail: bool = False,
        get_error: bool = False,
        exec_hang: bool = False,
    ) -> None:
        self._wedged = dict(wedged)
        self._wedge_seconds = wedge_seconds
        self._exec_fail = exec_fail
        self._get_error = get_error
        self._exec_hang = exec_hang
        self.list_calls = 0

    def get(self, container_id: str) -> _FakeContainer:
        if self._get_error:
            raise RuntimeError("docker api failure (fake)")
        prefix = container_id[:12]
        if prefix in self._wedged:
            remaining = self._wedged[prefix]
            if remaining is None or remaining > 0:
                if remaining is not None:
                    self._wedged[prefix] = remaining - 1
                time.sleep(self._wedge_seconds)
                raise RuntimeError("wedged container (fake docker)")
        return _FakeContainer(exec_fail=self._exec_fail, exec_hang=self._exec_hang)

    def list(self, **kwargs: Any) -> list[_FakeContainer]:
        self.list_calls += 1
        return []


class _FakeDocker:
    def __init__(self, containers: _FakeContainers) -> None:
        self.containers = containers


def _fake_docker_factory(
    wedged: dict[str, int | None],
    wedge_seconds: float = WEDGE_SECONDS,
    exec_fail: bool = False,
    get_error: bool = False,
    exec_hang: bool = False,
) -> Any:
    """Build a ``_docker``-compatible factory (``_docker(timeout=None)``).

    Patch it into the module namespace where the tool imported ``_docker``
    (e.g. ``sunaba.tools.exec._docker``), not just
    ``sunaba.tools.common._docker`` -- the name is resolved at call time in
    the importing module (see the lab repro, /tmp/lab/exp2_sunaba_layer.py).

    The ``_FakeContainers`` instance is shared across every ``_docker()``
    call so wedge budgets deplete across calls, exactly as a real wedged
    container would eventually unstick.  It is also exposed as ``.containers``
    on the factory so tests can read the call counters.  *exec_fail*,
    *get_error*, and *exec_hang* behave as in :class:`_FakeContainers`.  The
    factory records the *timeout* argument of the last ``_docker(timeout=...)``
    call as ``.last_timeout`` so tests can assert which client timeout a
    tool actually used.
    """
    containers = _FakeContainers(
        wedged,
        wedge_seconds,
        exec_fail=exec_fail,
        get_error=get_error,
        exec_hang=exec_hang,
    )

    def _make(timeout: float | None = None) -> _FakeDocker:
        _make.last_timeout = timeout  # type: ignore[attr-defined]
        return _FakeDocker(containers)

    _make.containers = containers  # type: ignore[attr-defined]
    _make.last_timeout = None  # type: ignore[attr-defined]
    return _make


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result_text(result: Any) -> str:
    """Extract the text payload from a fastmcp ``CallToolResult``."""
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is not None:
            return text
        return str(first)
    return str(content)


def _result_json(result: Any) -> dict[str, Any]:
    return json.loads(_result_text(result))


def _is_busy(result: Any) -> bool:
    """True when the tool response is the structured busy payload.

    A real result or a plain error string (e.g. sandbox_stop's
    ``"Error: ..."``) is not busy.
    """
    try:
        payload = json.loads(_result_text(result))
    except (ValueError, TypeError):
        return False
    return payload.get("busy") is True


def _run_wedge_scenario(scenario: Any) -> float:
    """Run a wedge scenario up to five times; return the first fast reading.

    The latency assertions are wall-clock, and under a parallel full-suite
    run (pytest-xdist -n 4 on 4 cores) the worker can be descheduled for
    ~1.4s mid-measurement, which would inflate a single healthy reading.  A
    retry lets the measurement happen in a calm window; the bound itself is
    :func:`_latency_limit` (relaxed under xdist -- issue #784 review round 4
    flake).

    This stays honest on base code: every retry re-fires a fresh wedge burst
    (the fake wedges forever), so a base-code reading is slow on *every*
    attempt and the last (still slow) reading is what the assertion sees.
    """
    latency: float | None = None
    for _ in range(5):
        latency = asyncio.run(scenario())
        if latency < _latency_limit():
            break
    assert latency is not None
    return latency


@pytest.fixture(autouse=True)
def _reset_docker_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop cached docker/recovery-cap state so each test's env knobs take effect.

    The global docker semaphore, the per-container registry, and the
    recovery-pool semaphore are process-global and built lazily from the
    ``SUNABA_*`` env vars on first use; tests set tiny caps via
    ``monkeypatch.setenv``, so the cached state must be discarded before
    every test (and is restored afterwards by monkeypatch).

    ``raising=False`` keeps the fixture harmless on base code (pre-fix), so
    the acceptance tests still exercise the wedge scenario there and fail on
    the latency/busy assertions rather than on setup.
    """
    import sunaba.tools.common as common_mod

    monkeypatch.setattr(common_mod, "_DOCKER_GLOBAL_SEMAPHORE", None, raising=False)
    monkeypatch.setattr(common_mod, "_DOCKER_PER_CONTAINER_SEMAPHORES", {}, raising=False)
    monkeypatch.setattr(common_mod, "_RECOVERY_SEMAPHORE", None, raising=False)
    yield


# ---------------------------------------------------------------------------
# Acceptance 2: docker-free tools stay responsive while docker is wedged
# ---------------------------------------------------------------------------


def test_docker_free_tool_stays_responsive_under_wedge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Firing more ``sandbox_exec`` calls than the global cap must not delay
    docker-free ``get_workflow_guide`` -- at PRODUCTION defaults.

    No ``SUNABA_DOCKER_*`` env overrides here: the production caps (global
    24 / per-container 6) and the **non-blocking acquire default** (timeout
    0) must make this pass by themselves.  With non-blocking acquire the 42
    overflow calls return the busy JSON in milliseconds and hold **no
    limiter tokens**; only the 6 per-container permits wedge (4s), so at
    measure time the shared thread limiter still has plenty of free tokens
    for the guide.  On base code (no caps) the 48-call burst occupies all 40
    limiter tokens and the guide waits out the wedge (~3.2s) -- the
    assertion below fails.
    """
    import sunaba.tools.exec as exec_mod

    monkeypatch.setattr(exec_mod, "_docker", _fake_docker_factory({"deadbeefcafe": None}))
    # Production defaults: explicitly no acquire-timeout override (the
    # non-blocking default is the guarantee under test).  delenv guards
    # against an ambient env var from the surrounding test process.
    monkeypatch.delenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", raising=False)

    latency = _run_wedge_scenario(_measure_guide_under_global_wedge)
    assert latency < _latency_limit(), (
        f"docker-free get_workflow_guide took {latency:.2f}s while {BURST} "
        "wedged sandbox_exec calls were in flight at production defaults; "
        "the docker caps are not protecting the shared thread limiter "
        "(issue #784)"
    )


async def _measure_guide_under_global_wedge() -> float:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        tasks = [
            asyncio.create_task(
                client.call_tool(
                    "sandbox_exec",
                    {"container_id": "deadbeefcafe", "commands": ["true"]},
                )
            )
            for _ in range(BURST)
        ]
        await asyncio.sleep(MEASURE_AFTER_SECONDS)  # let the wedge pile up
        t0 = time.monotonic()
        guide = await client.call_tool("get_workflow_guide", {})
        latency = time.monotonic() - t0
        assert _result_text(guide), "guide should return content"
        await asyncio.gather(*tasks, return_exceptions=True)  # drain wedged calls
        return latency


# ---------------------------------------------------------------------------
# Acceptance 3: a wedged container does not block other containers' docker ops
# ---------------------------------------------------------------------------


def test_other_container_is_isolated_from_wedged_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Saturating one container past its per-container cap must not block
    docker calls for a different container.

    Per-container cap 2 with a 4s wedge on ``deadbeefcafe``: only 2 calls
    wedge there, the rest return busy (non-blocking acquire; the 0.1s
    timeout here only exercises the opt-in knob), and a call for
    ``cafebabe1234`` completes promptly with a real result (not a busy
    error).  On base code the 48-call burst occupies every limiter token and
    the other-container call waits out the wedge (~3.2s) -- the assertion
    below fails.
    """
    import sunaba.tools.exec as exec_mod

    monkeypatch.setattr(exec_mod, "_docker", _fake_docker_factory({"deadbeefcafe": None}))
    monkeypatch.setenv("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", "2")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "0.1")

    # Up to 5 attempts (each re-fires a fresh wedge burst; healthy readings
    # recover, base-code readings stay slow on every attempt).  The bound is
    # _latency_limit(): strict 1.0s serial, 2.0s under pytest-xdist (worker
    # descheduling; issue #784 review round 4).
    latency: float = float("inf")
    payload: dict[str, Any] = {}
    for _ in range(5):
        latency, payload = asyncio.run(_call_other_container_under_wedge())
        if latency < _latency_limit():
            break
    assert latency < _latency_limit(), (
        f"docker call for a healthy container took {latency:.2f}s while "
        "another container was wedged; the per-container cap is not "
        "isolating containers (issue #784)"
    )
    assert payload.get("status") == "ok", f"expected a real result, got: {payload}"
    assert "busy" not in payload, f"other-container call must not be refused: {payload}"


async def _call_other_container_under_wedge() -> tuple[float, dict[str, Any]]:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        tasks = [
            asyncio.create_task(
                client.call_tool(
                    "sandbox_exec",
                    {"container_id": "deadbeefcafe", "commands": ["true"]},
                )
            )
            for _ in range(BURST)
        ]
        await asyncio.sleep(MEASURE_AFTER_SECONDS)  # saturate the wedged container's cap
        t0 = time.monotonic()
        result = await client.call_tool(
            "sandbox_exec", {"container_id": "cafebabe1234", "commands": ["true"]}
        )
        latency = time.monotonic() - t0
        payload = _result_json(result)
        await asyncio.gather(*tasks, return_exceptions=True)  # drain wedged calls
        return latency, payload


# ---------------------------------------------------------------------------
# Acceptance 1: overflow returns the structured busy error, never hangs
# ---------------------------------------------------------------------------


def test_overflow_returns_busy_error_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the caps: the overflow call gets the structured busy JSON
    (``busy: true``) instead of hanging, and the semaphores are released
    afterward so a subsequent call succeeds once the wedge clears (the
    fake's 2-call wedge budget is spent).  The tiny acquire timeout only
    exercises the opt-in knob -- the default 0 is non-blocking.
    """
    import sunaba.tools.exec as exec_mod

    monkeypatch.setattr(exec_mod, "_docker", _fake_docker_factory({"beefbeefbeef": 2}))
    monkeypatch.setenv("SUNABA_DOCKER_GLOBAL_CONCURRENCY", "2")
    monkeypatch.setenv("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", "2")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "0.05")

    busy, follow_up = asyncio.run(_overflow_then_recover())
    assert busy.get("busy") is True, f"overflow call did not return busy: {busy}"
    assert busy.get("status") == "error"
    assert busy.get("pool") == "docker", busy
    assert "recommended_next_action" in busy, busy
    assert busy.get("error"), busy
    assert follow_up.get("status") == "ok", (
        f"call after the wedge cleared must succeed (semaphores leaked?): {follow_up}"
    )


async def _overflow_then_recover() -> tuple[dict[str, Any], dict[str, Any]]:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        tasks = [
            asyncio.create_task(
                client.call_tool(
                    "sandbox_exec",
                    {"container_id": "beefbeefbeef", "commands": ["true"]},
                )
            )
            for _ in range(3)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        busy: dict[str, Any] | None = None
        for result in results:
            payload = _result_json(result)
            if payload.get("busy"):
                busy = payload
        assert busy is not None, "expected at least one busy overflow result"
        # The two wedged calls raised inside the tool after their sleep; the
        # decorator's finally must have released both semaphores, so once the
        # wedge budget is spent a follow-up call succeeds.
        follow_up = _result_json(
            await client.call_tool(
                "sandbox_exec", {"container_id": "beefbeefbeef", "commands": ["true"]}
            )
        )
        return busy, follow_up


# ---------------------------------------------------------------------------
# Recovery pool: the escape hatch is exempt from the docker caps (#784)
# ---------------------------------------------------------------------------


def test_recovery_tools_bypass_saturated_docker_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    """``sandbox_stop`` / ``sandbox_list_containers`` must stay callable
    while the wedged container's per-container permits (and the global
    docker cap) are fully held.

    The burst saturates both the global cap (2) and ``deadbeefcafe``'s
    per-container cap (2) with wedged calls; ``sandbox_stop`` for that same
    container and ``sandbox_list_containers`` then run on the recovery pool
    and **reach the fake docker** (a real result, not the busy error).  On
    the round-1 code both were ``docker_bound`` and returned busy JSON here
    -- the documented escape hatch was gated by the very caps it must break.
    """
    import sunaba.tools.container as container_mod
    import sunaba.tools.exec as exec_mod

    factory = _fake_docker_factory({"deadbeefcafe": None})
    # sandbox_stop / sandbox_list_containers resolve ``_docker`` at call
    # time from ``sunaba.tools.container``; sandbox_exec from ``exec``.
    monkeypatch.setattr(container_mod, "_docker", factory)
    monkeypatch.setattr(exec_mod, "_docker", factory)
    monkeypatch.setenv("SUNABA_DOCKER_GLOBAL_CONCURRENCY", "2")
    monkeypatch.setenv("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", "2")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "0.05")

    stop, listing = asyncio.run(_recovery_tools_under_saturation())
    assert not _is_busy(stop), (
        "sandbox_stop must reach docker even with the wedged container's "
        f"permits fully held; got a busy refusal: {_result_text(stop)}"
    )
    assert not _is_busy(listing), (
        "sandbox_list_containers must reach docker even with the docker "
        f"caps saturated; got a busy refusal: {listing}"
    )
    assert "containers" in listing, listing
    assert "error" not in listing, listing


async def _recovery_tools_under_saturation() -> tuple[Any, dict[str, Any]]:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        tasks = [
            asyncio.create_task(
                client.call_tool(
                    "sandbox_exec",
                    {"container_id": "deadbeefcafe", "commands": ["true"]},
                )
            )
            for _ in range(BURST)
        ]
        await asyncio.sleep(MEASURE_AFTER_SECONDS)  # saturate global + per-container caps
        stop = await client.call_tool(
            "sandbox_stop", {"container_id": "deadbeefcafe", "force": True}
        )
        listing = _result_json(await client.call_tool("sandbox_list_containers", {}))
        await asyncio.gather(*tasks, return_exceptions=True)  # drain wedged calls
        return stop, listing


def test_recovery_pool_exhaustion_returns_busy_then_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Holding all 4 recovery permits makes the 5th recovery call return the
    busy JSON immediately; the permits release cleanly once the holders
    finish.

    Runs at the production default ``SUNABA_RECOVERY_CONCURRENCY=4``.
    """
    monkeypatch.delenv("SUNABA_RECOVERY_CONCURRENCY", raising=False)
    release = threading.Event()

    def _slow() -> str:
        assert release.wait(timeout=5)
        return "done"

    wrapped = recovery_bound(_slow)
    holders = [threading.Thread(target=wrapped) for _ in range(4)]
    for t in holders:
        t.start()
    time.sleep(0.2)  # all four holders now hold the recovery permits

    busy = json.loads(wrapped())
    assert busy["busy"] is True, busy
    assert busy["status"] == "error"
    assert busy.get("pool") == "recovery", busy
    assert "recommended_next_action" in busy, busy

    release.set()
    for t in holders:
        t.join(timeout=5)
        assert not t.is_alive(), "a recovery holder did not finish (leaked permit?)"
    # Permits released cleanly: a follow-up call runs to completion.
    assert wrapped() == "done"


def test_sandbox_stop_fails_closed_when_checkpoint_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sandbox_stop(force=False) must refuse to destroy the container when
    the unpushed-checkpoint guard cannot positively verify "no checkpoints"
    (issue #784 review, [high] finding).

    Here the inner checkpoint_list hits a docker-level failure: its error
    payload has no "checkpoints" key, and the old guard's
    ``.get("checkpoints", [])`` misread it as "no checkpoints" and killed
    the container -- destroying unpushed local checkpoints without the
    Issue #264 warning.  The stop must fail CLOSED instead.
    """
    import sunaba.tools.container as container_mod
    import sunaba.tools.vcs.checkpoints as checkpoints_mod

    # sandbox_stop's own get resolves _docker from the container package, but
    # the inner checkpoint_list resolves it from sunaba.tools.vcs.checkpoints
    # (module-level import) -- both must be faked; only the vcs one fails.
    monkeypatch.setattr(container_mod, "_docker", _fake_docker_factory({}))
    monkeypatch.setattr(
        checkpoints_mod, "_docker", _fake_docker_factory({}, get_error=True)
    )

    result = asyncio.run(_stop_with_unverifiable_checkpoints())
    text = _result_text(result)
    assert "cannot verify unpushed checkpoints" in text, text
    assert "stopped and removed" not in text, text
    assert not _is_busy(result), text


def test_sandbox_stop_fails_closed_on_busy_checkpoint_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact busy payload -- ``{"status":"error","busy":true,...}`` from
    a docker concurrency refusal -- must fail the stop CLOSED (issue #784
    review, [high] finding).

    Covers the finding's named mechanism: if the inner checkpoint check is
    (or becomes) docker-bound, its busy refusal would previously
    ``json.loads`` fine and ``.get("checkpoints", [])`` to ``[]``, silently
    suppressing the Issue #264 warning before the kill.
    """
    import sunaba.tools.container as container_mod
    import sunaba.tools.container.lifecycle as lifecycle_mod

    monkeypatch.setattr(container_mod, "_docker", _fake_docker_factory({}))
    busy = json.dumps({
        "status": "error",
        "error": "Docker concurrency limit reached (container deadbeefcafe, cap 6)",
        "busy": True,
        "pool": "docker",
        "recommended_next_action": "Wait for in-flight docker operations",
    })
    monkeypatch.setattr(lifecycle_mod, "checkpoint_list", lambda *a, **k: busy)

    result = asyncio.run(_stop_with_unverifiable_checkpoints())
    text = _result_text(result)
    assert "cannot verify unpushed checkpoints" in text, text
    assert "stopped and removed" not in text, text
    assert not _is_busy(result), text


async def _stop_with_unverifiable_checkpoints() -> Any:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        # working_dir passed explicitly so resolve_git_root needs no attrs;
        # the fake's get() answers instantly (no wedge) for the stop itself.
        return await client.call_tool(
            "sandbox_stop",
            {"container_id": "cafebabe1234", "force": False, "working_dir": "/workspace"},
        )


def test_sandbox_stop_guard_bounded_by_recovery_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's hard wall-clock deadline must release the recovery permit
    even when the checkpoint exec NEVER returns (issue #784 review round 4,
    [high] finding).

    docker-py reads the hijacked exec output stream with an unbounded poll
    -- the client timeout only bounds the HTTP phases -- so a wedged exec
    (``containers.get`` answers, the exec stalls; Issue #181) blocks
    *indefinitely* regardless of the client timeout.  The fake models
    exactly that: ``exec_run`` never returns (no self-imposed read error).
    ``sandbox_stop(force=False)`` must still return the fail-closed refusal
    within ~``RECOVERY_DOCKER_TIMEOUT`` (scaled down to 0.5s here) and
    release its recovery permit, so 4 concurrent wedged stops can never
    exhaust the 4-slot recovery pool and block ``force=True`` stops /
    ``sandbox_list_containers``.  (The client timeout is still threaded in
    for the HTTP phases; the deadline is what bounds the exec read.)
    """
    import sunaba.tools.common as common_mod
    import sunaba.tools.container as container_mod
    import sunaba.tools.container.lifecycle as lifecycle_mod
    import sunaba.tools.vcs.checkpoints as checkpoints_mod

    recovery_timeout = 0.5
    monkeypatch.setattr(common_mod, "RECOVERY_DOCKER_TIMEOUT", recovery_timeout)
    # lifecycle imported the constant by value; the guard reads it from
    # lifecycle's namespace.
    monkeypatch.setattr(lifecycle_mod, "RECOVERY_DOCKER_TIMEOUT", recovery_timeout)

    # containers.get answers instantly; the checkpoint exec never returns
    # (exec_hang).  Pre-deadline code hangs here forever; with the deadline
    # the stop refuses at ~0.5s.
    factory = _fake_docker_factory({}, exec_hang=True)
    monkeypatch.setattr(container_mod, "_docker", factory)
    monkeypatch.setattr(checkpoints_mod, "_docker", factory)

    t0 = time.monotonic()
    result = asyncio.run(_stop_with_unverifiable_checkpoints())
    elapsed = time.monotonic() - t0
    text = _result_text(result)
    assert "cannot verify unpushed checkpoints" in text, text
    assert "stopped and removed" not in text, text
    assert elapsed < 5.0, (
        f"force=False stop held {elapsed:.1f}s on a never-returning "
        "checkpoint exec; the guard's hard deadline must fire at the "
        f"recovery timeout ({recovery_timeout}s), not hang indefinitely"
    )
    # The recovery timeout still reached the inner client for the HTTP
    # phases (the last _docker() call is the guard's checkpoint_list
    # client; without the threading it would be None -- the 60s default).
    assert factory.last_timeout == recovery_timeout, (
        f"guard's inner client used timeout {factory.last_timeout}, expected "
        f"RECOVERY_DOCKER_TIMEOUT={recovery_timeout}"
    )
    # The wrapper's finally released the recovery permit even though the
    # guard failed: a follow-up recovery call can still acquire one.
    sem = common_mod._get_recovery_semaphore()
    assert sem.acquire(timeout=0), "recovery permit leaked by the hung stop"
    sem.release()


async def _stop_with_failing_checkpoint_check() -> Any:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        # working_dir passed explicitly so resolve_git_root needs no attrs;
        # the fake's get() answers instantly (no wedge), then exec fails.
        return await client.call_tool(
            "sandbox_stop",
            {"container_id": "cafebabe1234", "force": False, "working_dir": "/workspace"},
        )


def test_sandbox_attach_is_isolated_per_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox_attach(name_or_id=...) is keyed on its container argument
    (``key_on="name_or_id"`` at registration): with the wedged container's
    per-container cap saturated -- while the global cap still has free
    permits -- an attach to that container returns busy instead of running
    on the global pool.

    Without the keying, the attach would take only a global permit, succeed,
    and report "found: false" -- the per-container isolation the cap exists
    to provide would be silently bypassed (issue #784 review, [medium]
    finding).
    """
    import sunaba.tools.exec as exec_mod

    monkeypatch.setattr(exec_mod, "_docker", _fake_docker_factory({"deadbeefcafe": None}))
    monkeypatch.setenv("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", "1")
    # Global cap stays at the production default (24): only the per-container
    # tier can refuse the attach, so this test discriminates the keying.

    payload = asyncio.run(_attach_under_per_container_saturation())
    assert payload.get("busy") is True, (
        "sandbox_attach for the saturated container must be refused by its "
        f"per-container cap, not run on the global pool: {payload}"
    )
    assert payload.get("pool") == "docker", payload


async def _attach_under_per_container_saturation() -> dict[str, Any]:
    from fastmcp import Client

    from sunaba import server

    async with Client(server.mcp) as client:
        tasks = [
            asyncio.create_task(
                client.call_tool(
                    "sandbox_exec",
                    {"container_id": "deadbeefcafe", "commands": ["true"]},
                )
            )
            for _ in range(BURST)
        ]
        await asyncio.sleep(MEASURE_AFTER_SECONDS)  # saturate deadbeefcafe's per-container cap
        payload = _result_json(
            await client.call_tool("sandbox_attach", {"name_or_id": "deadbeefcafef00d"})
        )
        await asyncio.gather(*tasks, return_exceptions=True)  # drain wedged calls
        return payload


def test_acquire_timeout_knob_waits_for_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS`` is the opt-in wait knob:
    with a positive value the wrapper waits for a permit instead of
    returning busy immediately (the default 0 is non-blocking).
    """
    monkeypatch.setenv("SUNABA_DOCKER_GLOBAL_CONCURRENCY", "1")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "1.0")
    release = threading.Event()

    def _slow(container_id: str) -> str:
        assert release.wait(timeout=5)
        return "done"

    wrapped = docker_bound(_slow)
    holder = threading.Thread(target=wrapped, args=("deadbeefcafe",))
    holder.start()
    time.sleep(0.2)  # holder holds the only global permit
    threading.Timer(0.2, release.set).start()
    # Without the knob this would be a busy error; the 1s wait acquires the
    # permit once the holder releases and returns the real result.
    assert wrapped("deadbeefcafe") == "done"
    holder.join(timeout=5)
    assert not holder.is_alive()


# ---------------------------------------------------------------------------
# Decorator unit tests
# ---------------------------------------------------------------------------


def test_decorator_preserves_signature() -> None:
    """``functools.wraps`` keeps FastMCP's signature introspection intact."""

    def _tool(container_id: str, message: str = "hi") -> str:
        """Docstring must survive too."""
        return message

    wrapped = docker_bound(_tool)
    assert inspect.signature(wrapped) == inspect.signature(_tool)
    assert wrapped.__name__ == "_tool"
    assert wrapped.__doc__ == _tool.__doc__
    # And it still calls through on the success path.
    assert wrapped(container_id="abc123def456", message="x") == "x"


def test_decorator_releases_permits_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raising tool must not leak its permits (try/finally release)."""
    monkeypatch.setenv("SUNABA_DOCKER_GLOBAL_CONCURRENCY", "1")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "0.05")

    calls: list[str] = []

    def _tool(container_id: str) -> str:
        calls.append(container_id)
        raise RuntimeError("boom")

    wrapped = docker_bound(_tool)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            wrapped("deadbeefcafe")
    # Both calls reached the body: the single global permit was released
    # after the first exception (a leaked permit would busy-error call 2).
    assert calls == ["deadbeefcafe", "deadbeefcafe"]


def test_per_container_semaphore_keyed_by_12_char_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-container permits are shared by the 12-char prefix, so two ids
    with the same prefix contend while a different prefix does not."""
    monkeypatch.setenv("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", "1")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "0.05")

    release = threading.Event()

    def _slow(container_id: str) -> str:
        assert release.wait(timeout=5)
        return "done"

    wrapped = docker_bound(_slow)

    holder = threading.Thread(target=wrapped, args=("deadbeefcafe",))
    holder.start()
    time.sleep(0.2)  # holder now holds global + per-container("deadbeefcafe")

    # Same 12-char prefix -> shared per-container semaphore -> busy, not wedged.
    busy = json.loads(wrapped("deadbeefcafef00d"))
    assert busy["busy"] is True, busy

    # Different prefix -> fresh per-container semaphore -> runs to completion.
    threading.Timer(0.2, release.set).start()
    assert wrapped("beefcafef00d") == "done"

    holder.join(timeout=5)
    assert not holder.is_alive()


def test_docker_bound_keys_per_container_cap_on_declared_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool whose container argument is not named ``container_id`` still
    gets per-container isolation when the keying parameter is declared via
    ``docker_bound(..., key_on=...)`` (e.g. sandbox_attach's ``name_or_id``;
    issue #784 review, [medium] finding)."""
    monkeypatch.setenv("SUNABA_DOCKER_PER_CONTAINER_CONCURRENCY", "1")
    monkeypatch.setenv("SUNABA_DOCKER_ACQUIRE_TIMEOUT_SECONDS", "0.05")

    release = threading.Event()

    def _tool(name_or_id: str) -> str:
        assert release.wait(timeout=5)
        return "done"

    wrapped = docker_bound(_tool, key_on="name_or_id")

    holder = threading.Thread(target=wrapped, args=("deadbeefcafe",))
    holder.start()
    time.sleep(0.2)  # holder now holds global + per-container("deadbeefcafe")

    # Same 12-char prefix -> shared per-container semaphore -> busy, not wedged.
    busy = json.loads(wrapped("deadbeefcafef00d"))
    assert busy["busy"] is True, busy

    # Different prefix -> fresh per-container semaphore -> runs to completion.
    threading.Timer(0.2, release.set).start()
    assert wrapped("beefcafef00d") == "done"

    holder.join(timeout=5)
    assert not holder.is_alive()
