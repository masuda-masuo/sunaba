"""Foreground exec deadline: real-process descendant reaping.

The mocked choreography tests (``test_foreground_deadline_reap.py``)
pin the cleanup protocol; this file pins the *actual* outcome against
real processes (decision 8 of the brief): when the server deadline
fires, no marked ordinary or setsid/detached descendant remains, and a
follow-up exec/git operation remains usable.  There is no docker daemon
inside the pytest sandbox, so the docker-container lookalike
:class:`_LocalExecContainer` executes every command in this pytest
process's own namespace: a setsid-detached descendant really is
spawned, really ignores SIGTERM, and really must be reaped by the
implementation under test.  Bounded to seconds, never minutes.

Covered here, one per foreground tool:

* ``sandbox_exec`` -- deadline reaps the direct command plus ordinary
  and setsid-detached descendants; an explicit ``timeout=N`` also reaps
  the detached child while retaining the existing timeout result
  contract (``status: "timeout"``, ``exit_code: 124``, decision 3).
* ``run_python`` -- the deadline reaps the runner tree including the
  detached child spawned by the user code.
* ``package_install`` (pip axis) -- the deadline reaps the install exec
  tree including the detached child.

Each test forces a short deadline (seconds) via
``SUNABA_FOREGROUND_TIMEOUT``, waits for a positive pre-cleanup
observation (ready file + live PIDs), then asserts complete cleanup,
a terminal ``status: "timeout"`` result with ``reap: "ok"`` and the
configured ``deadline_s``, and a usable container afterwards.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest

from sunaba.tools.exec import sandbox_exec
from sunaba.tools.package import package_install
from sunaba.tools.run_python import run_python

_FOREGROUND_TIMEOUT_ENV = "SUNABA_FOREGROUND_TIMEOUT"


def _cmd_text(cmd: object) -> str:
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return str(cmd)


def _client_with(container: object) -> Any:
    from unittest.mock import MagicMock

    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _run_in_thread(fn: Callable[[], Any], box: dict) -> threading.Thread:
    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException:  # noqa: BLE001 - surfaced via _join_tool_thread
            box["error"] = sys.exc_info()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _join_bounded(t: threading.Thread, seconds: float, what: str) -> None:
    t.join(seconds)
    if t.is_alive():
        pytest.fail(
            f"{what} did not return within {seconds}s -- the "
            f"{_FOREGROUND_TIMEOUT_ENV} deadline (foreground exec reaping) "
            "is not enforced"
        )


def _join_tool_thread(t: threading.Thread, seconds: float, what: str, box: dict) -> None:
    _join_bounded(t, seconds, what)
    error = box.get("error")
    if error is not None:
        _exc_type, exc_value, exc_tb = error
        raise exc_value.with_traceback(exc_tb)


def _call_sandbox_exec(cid: str, container: object, commands: list[str]) -> str:
    with patch("sunaba.tools.exec._docker", return_value=_client_with(container)):
        return sandbox_exec(cid, commands=commands)


def _call_sandbox_exec_timeout(cid: str, container: object, commands: list[str], timeout: int) -> str:
    with patch("sunaba.tools.exec._docker", return_value=_client_with(container)):
        return sandbox_exec(cid, commands=commands, timeout=timeout)


def _call_run_python(cid: str, container: object, code: str) -> str:
    with patch("sunaba.tools.run_python._docker", return_value=_client_with(container)):
        # run_python defaults its cwd to /workspace, which exists in every
        # sandbox container but not on a CI runner or a dev host: the runner
        # would fail to start the user code and the tree would never appear.
        return run_python(cid, code=code, working_dir=tempfile.gettempdir())


def _call_package_install(cid: str, container: object) -> str:
    with patch("sunaba.tools.package._docker", return_value=_client_with(container)):
        return package_install(cid, packages="requests")


class _LocalExecContainer:
    """Docker container lookalike that really executes commands locally.

    The pytest sandbox has no docker socket, so this fake is faithful
    about execution instead: every exec_run runs a real subprocess in
    this pytest process's own namespace, with the requested workdir and
    environment.  A deadline implementation that reaps by scanning /proc
    and signalling processes therefore works against real processes, real
    sessions and real signals.

    ``substitute`` maps a command substring to a replacement script: an
    exec whose command text contains the substring runs
    ``/bin/sh -c <script>`` instead of the original command (used to
    stand in for ``pip install``, which cannot be let through in a test).

    The exec that runs the marked user command (identified by
    *ready_exec_substring*) is observed for a ready file so tests get a
    positive pre-cleanup observation point; when the file appears,
    ``tree_ready`` is set.
    """

    def __init__(
        self,
        substitute: dict[str, str] | None = None,
        ready_path: str | None = None,
        ready_exec_substring: str | None = None,
    ) -> None:
        self.substitute = substitute or {}
        self.ready_path = Path(ready_path) if ready_path is not None else None
        self.ready_exec_substring = ready_exec_substring
        self.tree_ready = threading.Event()
        self.exec_calls: list[tuple[object, dict]] = []
        self.closed = False

    def exec_run(self, cmd: object, **kwargs: Any) -> tuple[int, bytes | tuple[bytes, bytes]]:
        self.exec_calls.append((cmd, dict(kwargs)))
        if self.closed:
            raise RuntimeError("container closed after deadline reap")
        text = _cmd_text(cmd)
        substituted = False
        argv: list[str] = []
        for sub, script in self.substitute.items():
            if sub in text:
                argv = ["/bin/sh", "-c", script]
                substituted = True
                break
        if not substituted:
            if isinstance(cmd, list):
                argv = [str(c) for c in cmd]
            else:
                argv = ["/bin/sh", "-c", str(cmd)]
        env = kwargs.get("environment")
        if isinstance(env, dict):
            env = dict(os.environ, **env)
        workdir = kwargs.get("workdir")
        # On CI runners, mock roots like /home/sandbox do not exist on the
        # host filesystem; fall back to None (process cwd).
        effective_cwd = workdir if isinstance(workdir, str) and os.path.isdir(workdir) else None
        proc = subprocess.Popen(  # noqa: S603 -- the fake container executes
            argv,                  # the same commands docker exec would
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=effective_cwd,
            env=env if isinstance(env, dict) else None,
        )
        if (
            self.ready_path is not None
            and self.ready_exec_substring is not None
            and self.ready_exec_substring in text
        ):
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


# ---------------------------------------------------------------------------
# Process observation helpers
# ---------------------------------------------------------------------------


def _read_pid(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pids_dead(pids: list[int], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if all(not _pid_alive(p) for p in pids):
            return True
        time.sleep(0.05)
    return False


def _token_processes(token: str) -> list[int]:
    """PIDs whose cmdline carries *token* (descendants spawned from the
    marked command)."""
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


def _kill_pids(pids: list[int | None]) -> None:
    for pid in pids:
        if pid is None:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Real-process descendant reaping
# ---------------------------------------------------------------------------


class TestSandboxExecReapsRealDescendants:
    """sandbox_exec: deadline and explicit-timeout reaping against real
    ordinary and setsid-detached descendants."""

    CID = "fgre00000001"

    def test_deadline_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the server deadline fires, the direct command AND its
        ordinary and setsid-detached descendants must all be reaped: no
        marked process remains, the container immediately runs another
        command, and the timed-out call terminates its own machinery."""
        token = f"sunabafg-{uuid.uuid4().hex[:12]}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        parent_pid = f"/tmp/{token}.parent"
        child_pid = f"/tmp/{token}.child"
        ord_pid = f"/tmp/{token}.ord"
        # Both the direct command and the setsid child ignore SIGTERM
        # (trap + loop) so only the KILL fallback can remove them; the
        # ordinary background child has no trap and dies on TERM.  The
        # ready file is written only after the setsid child exists.
        script = (
            f"trap 'echo TERM >> {log}' TERM; "
            f"setsid sh -c 'trap \"echo TERM >> {log}\" TERM; "
            f"echo $$ > {child_pid}; while :; do sleep 60; done # {token}' >/dev/null 2>&1 & "
            f"sh -c 'echo $$ > {ord_pid}; while :; do sleep 60; done # {token}-o' >/dev/null 2>&1 & "
            f"echo $$ > {parent_pid}; "
            f"while [ ! -s {child_pid} ]; do sleep 0.01; done; "
            f"echo ready > {ready}; "
            f"while :; do sleep 60; done"
        )
        fake = _LocalExecContainer(ready_path=ready, ready_exec_substring="base64 -d")
        deadline_s = 3.0
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t = _run_in_thread(lambda: _call_sandbox_exec(self.CID, fake, [script]), box)
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("sandbox_exec command did not create its marked tree")
            pids = [p for p in (_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)) if p is not None]
            assert len(pids) >= 2, (
                "marked command and descendants must both exist before deadline cleanup"
            )
            for p in pids:
                assert _pid_alive(p), f"marked pid {p} must be alive before cleanup"

            _join_tool_thread(t, 25.0, "sandbox_exec that must time out and reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert result["timeout"]["deadline_s"] == deadline_s

            # No marked process remains -- neither the direct command nor
            # the ordinary / setsid-detached descendants.
            assert _wait_pids_dead(pids), f"marked pids still alive after reap: {pids}"
            assert _token_processes(token) == []

            # The container can immediately execute another command.
            ec, out = fake.exec_run(["echo", "ok"])
            out_bytes = out if isinstance(out, bytes) else b"".join(out)
            assert ec == 0
            assert b"ok" in out_bytes

            # The timed-out call terminates its own machinery.
            t.join(10.0)
            assert not t.is_alive()
        finally:
            _kill_token_processes(token)
            _kill_pids([_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)])
            for p in (log, ready, parent_pid, child_pid, ord_pid):
                Path(p).unlink(missing_ok=True)
            t.join(10.0)
            fake.closed = True

    def test_explicit_timeout_reaps_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit ``timeout=N`` keeps the existing result contract
        (status ``"timeout"``, ``exit_code`` 124) AND reaps the
        setsid-detached child that ``timeout(1)`` alone would leave
        behind (decision 3)."""
        token = f"sunabafg-{uuid.uuid4().hex[:12]}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        child_pid = f"/tmp/{token}.child"
        explicit_s = 2
        # The direct command has no trap, so timeout(1) fires cleanly at
        # N (exit 124); the setsid child ignores SIGTERM and loops, so it
        # survives timeout(1) and must be reaped by the marker machinery.
        script = (
            f"echo started; "
            f"setsid sh -c 'trap \"echo TERM >> {log}\" TERM; "
            f"echo $$ > {child_pid}; while :; do sleep 60; done # {token}' >/dev/null 2>&1 & "
            f"while [ ! -s {child_pid} ]; do sleep 0.01; done; "
            f"echo ready > {ready}; "
            f"sleep 600"
        )
        fake = _LocalExecContainer(ready_path=ready, ready_exec_substring="base64 -d")
        box: dict = {}
        t = _run_in_thread(
            lambda: _call_sandbox_exec_timeout(self.CID, fake, [script], explicit_s),
            box,
        )
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("sandbox_exec command did not create its detached child")
            child = _read_pid(child_pid)
            assert child is not None and _pid_alive(child), (
                "detached child must exist before the explicit timeout fires"
            )

            _join_tool_thread(t, 30.0, "sandbox_exec with explicit timeout that must reap", box)
            result = json.loads(box["value"])
            # Existing timeout result contract is preserved.
            assert result["status"] == "timeout"
            assert result["exit_code"] == 124

            # The detached descendant did not survive the explicit timeout.
            assert _wait_pids_dead([child]), (
                "setsid-detached descendant survived the explicit timeout"
            )
            assert _token_processes(token) == []

            ec, out = fake.exec_run(["echo", "ok"])
            out_bytes = out if isinstance(out, bytes) else b"".join(out)
            assert ec == 0
            assert b"ok" in out_bytes

            t.join(10.0)
            assert not t.is_alive()
        finally:
            _kill_token_processes(token)
            _kill_pids([_read_pid(child_pid)])
            for p in (log, ready, child_pid):
                Path(p).unlink(missing_ok=True)
            t.join(10.0)
            fake.closed = True


class TestRunPythonReapsRealDescendants:
    """run_python: the deadline reaps the runner tree including the
    setsid-detached descendant spawned by the user code."""

    CID = "fgre00000002"

    def test_deadline_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = f"sunabafg-{uuid.uuid4().hex[:12]}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        parent_pid = f"/tmp/{token}.parent"
        child_pid = f"/tmp/{token}.child"
        code = (
            "import os, subprocess, time\n"
            f"open({parent_pid!r}, 'w').write(str(os.getpid()))\n"
            "subprocess.Popen(['setsid', 'sh', '-c', "
            f"'trap \"echo TERM >> {log}\" TERM; echo $$ > {child_pid}; "
            f"while :; do sleep 60; done # {token}'], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"while not os.path.exists({child_pid!r}): time.sleep(0.01)\n"
            f"open({ready!r}, 'w').close()\n"
            "while True: time.sleep(60)\n"
        )
        fake = _LocalExecContainer(ready_path=ready, ready_exec_substring="base64 -d")
        deadline_s = 3.0
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t = _run_in_thread(lambda: _call_run_python(self.CID, fake, code), box)
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("run_python user code did not create its marked tree")
            pids = [p for p in (_read_pid(parent_pid), _read_pid(child_pid)) if p is not None]
            assert len(pids) >= 2, (
                "user-code process and detached descendant must exist before cleanup"
            )
            for p in pids:
                assert _pid_alive(p), f"marked pid {p} must be alive before cleanup"

            _join_tool_thread(t, 25.0, "run_python that must time out and reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert result["timeout"]["deadline_s"] == deadline_s

            assert _wait_pids_dead(pids), f"marked pids still alive after reap: {pids}"
            assert _token_processes(token) == []

            ec, out = fake.exec_run(["echo", "ok"])
            out_bytes = out if isinstance(out, bytes) else b"".join(out)
            assert ec == 0
            assert b"ok" in out_bytes

            t.join(10.0)
            assert not t.is_alive()
        finally:
            _kill_token_processes(token)
            _kill_pids([_read_pid(parent_pid), _read_pid(child_pid)])
            for p in (log, ready, parent_pid, child_pid):
                Path(p).unlink(missing_ok=True)
            t.join(10.0)
            fake.closed = True


class TestPackageInstallReapsRealDescendants:
    """package_install (pip axis): the deadline reaps the install exec
    tree including the setsid-detached descendant."""

    CID = "fgre00000003"

    def test_deadline_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = f"sunabafg-{uuid.uuid4().hex[:12]}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        parent_pid = f"/tmp/{token}.parent"
        child_pid = f"/tmp/{token}.child"
        ord_pid = f"/tmp/{token}.ord"
        # Stand-in for the hung ``pip install``: spawns an ordinary and a
        # setsid-detached TERM-ignoring descendant, then loops.  The real
        # ``pip list`` snapshots around it still run for real.
        script = (
            f"echo $$ > {parent_pid}; "
            f"setsid sh -c 'trap \"echo TERM >> {log}\" TERM; "
            f"echo $$ > {child_pid}; while :; do sleep 60; done # {token}' >/dev/null 2>&1 & "
            f"sh -c 'echo $$ > {ord_pid}; while :; do sleep 60; done # {token}-o' >/dev/null 2>&1 & "
            f"while [ ! -s {child_pid} ]; do sleep 0.01; done; "
            f"echo ready > {ready}; "
            f"while :; do sleep 60; done"
        )
        fake = _LocalExecContainer(
            substitute={"pip install": script},
            ready_path=ready,
            ready_exec_substring="pip install",
        )
        deadline_s = 3.0
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t = _run_in_thread(lambda: _call_package_install(self.CID, fake), box)
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("package_install install exec did not create its marked tree")
            pids = [p for p in (_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)) if p is not None]
            assert len(pids) >= 2, (
                "install command and descendants must exist before cleanup"
            )
            for p in pids:
                assert _pid_alive(p), f"marked pid {p} must be alive before cleanup"

            _join_tool_thread(t, 25.0, "package_install that must time out and reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert result["timeout"]["deadline_s"] == deadline_s

            assert _wait_pids_dead(pids), f"marked pids still alive after reap: {pids}"
            assert _token_processes(token) == []

            ec, out = fake.exec_run(["echo", "ok"])
            out_bytes = out if isinstance(out, bytes) else b"".join(out)
            assert ec == 0
            assert b"ok" in out_bytes

            t.join(10.0)
            assert not t.is_alive()
        finally:
            _kill_token_processes(token)
            _kill_pids([_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)])
            for p in (log, ready, parent_pid, child_pid, ord_pid):
                Path(p).unlink(missing_ok=True)
            t.join(10.0)
            fake.closed = True
