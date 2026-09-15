"""Issue #910: verify timeout cleanup -- reap choreography and real descendants.

When a server-side verify deadline fires, the cleanup must terminate the
whole in-container test tree -- TERM first, KILL as fallback -- and must
report an incomplete reap rather than silently succeeding.  This file
pins that contract with two layers:

* ``TestVerifyTimeoutReapChoreography`` -- mocked exec_run tests.  The
  mock answers cleanup commands by contract-level tokens (signal names
  or numbers, process probes) without pinning exact command text, and
  the tests assert the reap verdict and the TERM-before-KILL order.

* ``TestVerifyTimeoutReapsRealDescendants`` -- one deterministic
  real-process test.  There is no docker daemon inside the pytest
  sandbox, so the docker-container lookalike
  :class:`_LocalExecContainer` executes every command in this pytest
  process's own namespace: a setsid-detached descendant really is
  spawned, really ignores SIGTERM, and really must be reaped by the
  implementation under test.  Bounded to seconds, never minutes.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.verify import verify_in_container

_VERIFY_TIMEOUT_ENV = "SUNABA_VERIFY_TIMEOUT"

_GATE_OK = {
    "gate_passed": True,
    "incomplete": False,
    "lint": [],
    "types": [],
    "gate_fail_reasons": [],
}


def _cmd_text(cmd: object) -> str:
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return str(cmd)


def _client_with(container: object) -> MagicMock:
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _detection(*languages: str) -> object:
    from sunaba.edit_verify import DetectionResult

    return DetectionResult(
        languages=set(languages), scope={}, reason=None,
    )


def _verify_in_thread(
    cid: str,
    container: object,
    result_box: dict,
    **kwargs: Any,
) -> tuple[threading.Thread, MagicMock]:
    """Run verify_in_container on a worker thread with standard mocks.

    The result (a JSON string) is stored under ``result_box["value"]``
    because pytest assertions must not run inside the worker.  If the
    worker itself fails, the exception (with its traceback) is stored
    under ``result_box["error"]`` so the pytest thread can re-raise it
    faithfully via :func:`_join_verify_thread` -- never masked as a
    missing ``"value"`` key (the #912 CI failure mode).  Returns the
    thread and the patched ``record_verify_success`` mock.
    """
    recorder = MagicMock()

    def run() -> None:
        try:
            with (
                patch("sunaba.tools.verify._docker", return_value=_client_with(container)),
                patch("sunaba.edit_verify.detect_languages", return_value=_detection("python")),
                patch("sunaba.edit_verify.run_lint_type_gate", return_value=_GATE_OK),
                patch("sunaba.tools.verify.record_verify_success", recorder),
            ):
                result_box["value"] = verify_in_container(
                    cid,
                    "tests/",
                    skip_lint_gate=True,
                    skip_type_gate=True,
                    skip_patch_targets_gate=True,
                    **kwargs,
                )
        except BaseException:
            # Capture the worker's own failure (with its traceback) for
            # :func:`_join_verify_thread` to re-raise on the pytest
            # thread; not re-raised here so a worker failure does not
            # also spam stderr through threading's exception hook.
            result_box["error"] = sys.exc_info()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, recorder


def _join_bounded(t: threading.Thread, seconds: float, what: str) -> None:
    t.join(seconds)
    if t.is_alive():
        pytest.fail(
            f"{what} did not return within {seconds}s -- the {_VERIFY_TIMEOUT_ENV} "
            "deadline (issue #910) is not enforced"
        )


def _join_verify_thread(
    t: threading.Thread,
    seconds: float,
    what: str,
    box: dict,
) -> None:
    """Join a verify worker and surface any worker-side exception.

    The worker stores its JSON result under ``box["value"]``; when the
    worker itself fails (``verify_in_container`` raising against the fake
    container -- possible under CI load), that key never appears and a
    bare ``box["value"]`` read would mask the real failure as
    ``KeyError: 'value'``, exactly what #912's CI run reported.  Re-raise
    the worker's exception with its original traceback so the pytest
    thread sees the true diagnostic instead.
    """
    _join_bounded(t, seconds, what)
    error = box.get("error")
    if error is not None:
        _exc_type, exc_value, exc_tb = error
        raise exc_value.with_traceback(exc_tb)


# ---------------------------------------------------------------------------
# Mocked reap choreography
# ---------------------------------------------------------------------------


class _ScriptedReapContainer:
    """Mock container answering cleanup commands by contract-level tokens.

    The deadline implementation must reach its marked test tree through
    exec_run commands.  This mock recognises the *contract* tokens of the
    #910 cleanup choreography -- signals by name or number, and process
    probes -- without pinning exact command text:

    * a command mentioning pytest   -> block on *release* (the hung test
      run the deadline fires against);
    * a command carrying KILL/-9     -> record ``KILL``, succeed;
    * a command carrying TERM/-15,
      or spelling pkill/kill (whose
      default signal is TERM)        -> record ``TERM``, succeed;
    * a command carrying ps//proc    -> record ``probe`` and report what
      *probe_output* says (a surviving marked process by default);
    * anything else                  -> a non-empty success, so the
      capture-health guard (issue #870) never trips on phantom empties.
    """

    # A surviving marked process, formatted for both a ps-style parser
    # (first column = PID) and a /proc-style scan (path carries the PID).
    SURVIVOR = (
        b"424242 sh -c sleep 1000 __sunaba910_mock_marker__\n"
        b"/proc/424242\n"
    )

    def __init__(self, probe_output: bytes = SURVIVOR) -> None:
        self.release = threading.Event()
        self.events: list[str] = []
        self.probe_output = probe_output

    def exec_run(self, cmd: object, **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        text = _cmd_text(cmd)
        if "pytest" in text:
            self.release.wait()
            return 0, (b"", b"")
        if "KILL" in text or "-9" in text:
            self.events.append("KILL")
            return 0, (b"", b"")
        if "TERM" in text or "-15" in text or "pkill" in text or "kill" in text:
            self.events.append("TERM")
            return 0, (b"", b"")
        if "ps" in text or "/proc" in text:
            self.events.append("probe")
            return 0, (self.probe_output, b"")
        return 0, (b"x\n", b"")


class TestVerifyTimeoutReapChoreography:
    """Cleanup performs TERM then KILL fallback and reports the reap
    outcome instead of silently succeeding."""

    CID = "910reap00001"

    def test_timeout_reports_incomplete_reap_when_processes_survive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A marked process still alive after the KILL fallback must make
        the timeout result carry ``reap: "incomplete"`` -- never a
        silent success."""
        monkeypatch.setenv(_VERIFY_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer()
        box: dict = {}
        t, _rec = _verify_in_thread(self.CID, container, box)
        try:
            _join_verify_thread(t, 10.0, "verify whose cleanup cannot reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "incomplete"
            # TERM was issued before the KILL fallback.
            assert "TERM" in container.events
            assert "KILL" in container.events
            assert container.events.index("TERM") < container.events.index("KILL")
            # The verdict came from a probe, never from assuming the KILL
            # command's own exit code proved the tree gone.
            assert "probe" in container.events
        finally:
            container.release.set()
            t.join(10.0)

    def test_timeout_reap_ok_when_cleanup_fully_reaps(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clean probe after TERM reports ``reap: "ok"`` and must not
        escalate to KILL (KILL is only a fallback)."""
        monkeypatch.setenv(_VERIFY_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer(probe_output=b"")  # probe finds nothing
        box: dict = {}
        t, _rec = _verify_in_thread(self.CID, container, box)
        try:
            _join_verify_thread(t, 10.0, "verify whose cleanup fully reaps", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert "TERM" in container.events
            assert "KILL" not in container.events
        finally:
            container.release.set()
            t.join(10.0)


# ---------------------------------------------------------------------------
# Real-process descendant reaping
# ---------------------------------------------------------------------------


class _LocalExecContainer:
    """Docker container lookalike that really executes commands locally.

    The pytest sandbox has no docker socket, so this fake is faithful
    about execution instead: every exec_run runs a real ``/bin/sh``
    subprocess in this pytest process's own namespace, with the requested
    workdir and environment.  A deadline implementation that reaps by
    scanning /proc and signalling processes therefore works against real
    processes, real sessions and real signals.

    The exec that runs the test suite (the command mentioning pytest) is
    replaced by *test_run_script*: that script is what is "really
    running" when the deadline fires.
    """

    def __init__(
        self,
        test_run_script: str | None = None,
        ready_path: str | None = None,
    ) -> None:
        self.test_run_script = test_run_script
        self.ready_path = Path(ready_path) if ready_path is not None else None
        self.tree_ready = threading.Event()
        self.exec_calls: list[tuple[object, dict]] = []
        self.closed = False

    def exec_run(
        self, cmd: object, **kwargs: Any,
    ) -> tuple[int, bytes | tuple[bytes, bytes]]:
        self.exec_calls.append((cmd, dict(kwargs)))
        if self.closed:
            raise RuntimeError("container closed after verify")
        if self.test_run_script is not None and "pytest" in _cmd_text(cmd):
            argv = ["/bin/sh", "-c", self.test_run_script]
        elif isinstance(cmd, list):
            argv = cmd
        else:
            argv = ["/bin/sh", "-c", cmd]
        env = kwargs.get("environment")
        if isinstance(env, dict):
            env = dict(os.environ, **env)
        workdir = kwargs.get("workdir")
        # On CI runners, mock roots like /home/sandbox do not exist on the host filesystem.
        # Fall back to None (process cwd) so subprocess.Popen does not fail with FileNotFoundError.
        effective_cwd = workdir if isinstance(workdir, str) and os.path.isdir(workdir) else None
        proc = subprocess.Popen(  # noqa: S603 -- the fake container executes
            argv,                  # the same commands docker exec would
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=effective_cwd,
            env=env if isinstance(env, dict) else None,
        )
        if self.ready_path is not None and self.test_run_script is not None and "pytest" in _cmd_text(cmd):
            ready_deadline = time.monotonic() + 15.0
            while time.monotonic() < ready_deadline and not self.ready_path.exists():
                time.sleep(0.01)
            if self.ready_path.exists():
                self.tree_ready.set()
        try:
            out, err = proc.communicate()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if kwargs.get("demux"):
            return proc.returncode, (out, err)
        return proc.returncode, out + err


def _token_processes(token: str) -> list[int]:
    """PIDs whose cmdline carries *token* (the direct command and any
    descendant spawned from it)."""
    found: list[int] = []
    proc_root = Path("/proc")
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if token.encode() in cmdline:
            found.append(int(entry.name))
    return found


def _kill_token_processes(token: str) -> None:
    """SIGKILL every process carrying *token* -- test hygiene only, never
    the implementation under test."""
    for pid in _token_processes(token):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


class TestVerifyTimeoutReapsRealDescendants:
    """Issue #910 acceptance criterion: after a timeout, no verification
    parent or detached child remains, and the container can immediately
    execute another command."""

    CID = "910real0001"

    def test_timeout_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The test run spawns a setsid-detached descendant that ignores
        SIGTERM (logging it) and loops.  When the deadline fires, the
        direct command AND the detached descendant must both be reaped:
        no marked process may remain, the container must immediately run
        another command, and the timed-out call must terminate its own
        machinery.  TERM-before-KILL is pinned by the mocked choreography
        tests above; the real-process test does not depend on the trap
        handler's ``echo`` landing inside the reaper's fixed settle
        window (that would be load-sensitive)."""
        token = f"sunaba910-{uuid.uuid4().hex[:12]}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        # Both marked processes ignore SIGTERM, but sleep between checks so
        # readiness and timeout cleanup do not burn a CPU.  The ready file
        # is written only after the setsid child exists, giving the test a
        # positive pre-cleanup observation point.
        script = (
            f"trap 'echo TERM >> {log}' TERM; "
            f"setsid sh -c 'trap \"echo TERM >> {log}\" TERM; "
            f"while :; do sleep 60; done # {token}' & child=$!; "
            f"while [ ! -d /proc/$child ]; do sleep 0.01; done; "
            f"echo ready > {ready}; "
            f"while :; do sleep 60; done # {token}"
        )
        fake = _LocalExecContainer(test_run_script=script, ready_path=ready)
        # Readiness synchronization proves the marked tree exists before reap,
        # and a generous deadline/wait budget tolerates CPU oversubscription on CI runners.
        deadline_s = 3.0
        monkeypatch.setenv(_VERIFY_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t, _rec = _verify_in_thread(self.CID, fake, box)
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("marked pytest command did not create its setsid child")
            before_cleanup = _token_processes(token)
            assert len(before_cleanup) >= 2, (
                "marked command and setsid descendant must both exist before timeout cleanup"
            )
            _join_verify_thread(t, 25.0, "verify that must time out and reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert result["timeout"]["deadline_s"] == deadline_s

            # No marked process remains -- neither the direct command nor
            # the setsid-detached descendant.
            assert _token_processes(token) == []

            # The container can immediately execute another command.
            ec, out = fake.exec_run(["echo", "ok"])
            out_bytes = out if isinstance(out, bytes) else b"".join(out)
            assert ec == 0
            assert b"ok" in out_bytes

            # The processes ignore TERM, so only the KILL fallback could
            # have removed them -- which the clean probe above already
            # proves.  TERM-before-KILL ordering is pinned by the mocked
            # choreography tests; whether the trap handler's log write
            # landed within the reaper's settle window is not asserted
            # here, because that would be load-sensitive.

            # The timed-out call terminates its own machinery.
            t.join(10.0)
            assert not t.is_alive()
        finally:
            _kill_token_processes(token)
            Path(log).unlink(missing_ok=True)
            Path(ready).unlink(missing_ok=True)
            t.join(10.0)
            fake.closed = True


class _RaisingContainer:
    """Fake container whose every exec fails.

    Simulates the worker-side verify failure that #912's CI run hit (and
    that used to be masked as ``KeyError: 'value'`` on the pytest
    thread): ``verify_in_container`` raises against the fake container,
    and the helper thread must hand the real exception back instead of
    leaving ``box["value"]`` unset.
    """

    def exec_run(self, cmd: object, **kwargs: object) -> object:
        raise RuntimeError("exec failed under load")


class TestWorkerExceptionSurfacing:
    """A worker-side verify failure must surface with its original
    diagnostic on the pytest thread -- never as ``KeyError: 'value'``
    (#912 CI regression)."""

    CID = "910erro0001"

    def test_worker_exception_is_reraised_with_context(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_VERIFY_TIMEOUT_ENV, "0.3")
        box: dict = {}
        t, _rec = _verify_in_thread(self.CID, _RaisingContainer(), box)
        with pytest.raises(RuntimeError, match="exec failed under load"):
            _join_verify_thread(t, 10.0, "verify worker that must fail", box)
        # The masking failure mode is gone: the worker error was captured
        # and re-raised before any ``box["value"]`` read.
        assert "value" not in box
