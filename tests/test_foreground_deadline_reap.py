"""Foreground exec deadline: mocked reap choreography and fast-path contract.

Every synchronous foreground command path (``sandbox_exec``,
``run_python``, ``package_install``) runs under a server-owned deadline
(decisions 1-5 of the brief).  This file pins the *mocked* layers of that
contract:

* ``TestSandboxExecDeadlineChoreography`` /
  ``TestRunPythonDeadlineChoreography`` /
  ``TestPackageInstallDeadlineChoreography`` -- mocked exec_run
  cancellation choreography for all three tools: when the deadline fires
  while the user command is blocked, the cleanup must TERM the marked
  tree first, KILL as fallback, probe for the reap verdict, and the call
  must return a terminal ``status: "timeout"`` result carrying
  ``timeout: {deadline_s, reap, elapsed_s}`` (decision 5).  The mock
  answers cleanup commands by contract-level tokens without pinning exact
  command text, mirroring ``tests/test_verify_timeout_wrapper.py``.
* ``TestDeadlineJournaling`` -- journaling has one truthful terminal
  entry and no phantom success (decision 5): the timed-out call never
  records a completion with exit code 0.
* ``TestExplicitTimeoutContractRetained`` -- the existing explicit
  ``timeout=N`` result mapping (status ``"timeout"`` with
  ``exit_code=124``, no ``timeout(1)`` wrapper at ``timeout=0``) is
  preserved (decision 3).
* ``TestFastPathPreservedWithMarker`` -- fast commands preserve their
  arguments/env/output exactly apart from the private per-call marker
  environment, which every foreground exec inherits (decision 4).
* ``TestBackgroundNotArmed`` -- ``sandbox_exec_background`` stays
  deliberately durable across MCP disconnects and is not armed/reaped by
  the foreground deadline (decision 6).

The scripted containers block on the user command (any exec that is not
a cleanup command), so at base -- before the deadline machinery exists --
each choreography test fails because the tool call does not return: the
bounded join reports that the ``SUNABA_FOREGROUND_TIMEOUT`` deadline is
not enforced (a missing-behavior failure, not an import error).
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.exec import sandbox_exec, sandbox_exec_background
from sunaba.tools.package import package_install
from sunaba.tools.run_python import run_python

_FOREGROUND_TIMEOUT_ENV = "SUNABA_FOREGROUND_TIMEOUT"
_FOREGROUND_MARKER_ENV = "SUNABA_FOREGROUND_MARKER"


def _cmd_text(cmd: object) -> str:
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    return str(cmd)


def _client_with(container: object) -> MagicMock:
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _run_in_thread(fn: Callable[[], Any], box: dict) -> threading.Thread:
    """Run *fn* on a worker thread, storing the result or its exception.

    The result (a JSON string) goes under ``box["value"]``; a worker-side
    failure goes under ``box["error"]`` as ``(type, value, traceback)``
    so :func:`_join_tool_thread` can re-raise it faithfully -- pytest
    assertions must not run inside the worker.
    """

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
    """Join a tool worker and surface any worker-side exception."""
    _join_bounded(t, seconds, what)
    error = box.get("error")
    if error is not None:
        _exc_type, exc_value, exc_tb = error
        raise exc_value.with_traceback(exc_tb)


# ---------------------------------------------------------------------------
# Tool invocations on the patched docker client
# ---------------------------------------------------------------------------


def _call_sandbox_exec(cid: str, container: object) -> str:
    with patch("sunaba.tools.exec._docker", return_value=_client_with(container)):
        return sandbox_exec(cid, commands=["echo hello"])


def _call_sandbox_exec_timeout(cid: str, container: object, timeout: int) -> str:
    with patch("sunaba.tools.exec._docker", return_value=_client_with(container)):
        return sandbox_exec(cid, commands=["sleep 60"], timeout=timeout)


def _call_run_python(cid: str, container: object) -> str:
    with patch("sunaba.tools.run_python._docker", return_value=_client_with(container)):
        return run_python(cid, code="x = 1")


def _call_package_install(cid: str, container: object, **kwargs: Any) -> str:
    with patch("sunaba.tools.package._docker", return_value=_client_with(container)):
        return package_install(cid, **kwargs)


# ---------------------------------------------------------------------------
# Scripted reap container
# ---------------------------------------------------------------------------


class _ScriptedReapContainer:
    """Mock container answering cleanup commands by contract-level tokens.

    The deadline implementation must reach its marked command tree through
    exec_run commands.  This mock recognises the *contract* tokens of the
    #910 cleanup choreography (signals by name or number, and process
    probes) without pinning exact command text, and blocks on any other
    command -- the user command that must still be running when the
    deadline fires:

    * a command carrying KILL (or -9)     -> record ``KILL``, succeed;
    * a command carrying TERM (or -15),
      or spelling pkill/kill             -> record ``TERM``, succeed;
    * a command carrying ps//proc         -> record ``probe`` and report
      what *probe_output* says (a surviving marked process by default);
    * any command carrying one of
      *block_substrings*                 -> block on *release* (the hung
      user command the deadline fires against);
    * anything else                       -> a non-empty success, so the
      capture-health guard (issue #870) never trips on phantom empties.
    """

    # A surviving marked process, formatted for both a ps-style parser
    # (first column = PID) and a /proc-style scan (path carries the PID).
    SURVIVOR = (
        b"424242 sh -c sleep 1000 __sunaba_fg_mock_marker__\n"
        b"/proc/424242\n"
    )

    def __init__(
        self,
        probe_output: bytes = SURVIVOR,
        block_substrings: tuple[str, ...] = ("base64 -d",),
    ) -> None:
        self.release = threading.Event()
        self.events: list[str] = []
        self.probe_output = probe_output
        self.block_substrings = block_substrings

    def exec_run(self, cmd: object, **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        text = _cmd_text(cmd)
        # The user command is recognised FIRST: its base64 payload could
        # randomly contain cleanup-looking substrings (``ps``, ``TERM``...),
        # and the cleanup commands never carry the block substrings, so
        # ordering the block check ahead of the token classification keeps
        # the two sets disjoint.
        if any(sub in text for sub in self.block_substrings):
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


# ---------------------------------------------------------------------------
# Mocked reap choreography, one class per foreground tool
# ---------------------------------------------------------------------------


class TestSandboxExecDeadlineChoreography:
    """sandbox_exec: the deadline fires against a blocked exec and the
    cleanup reports the reap outcome instead of silently succeeding."""

    CID = "fgdc00000001"

    def test_deadline_reports_incomplete_reap_with_term_before_kill(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A marked process still alive after the KILL fallback must make
        the timeout result carry ``reap: "incomplete"`` -- never a silent
        success -- with TERM issued before KILL and a probe deciding the
        verdict."""
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer()
        box: dict = {}
        t = _run_in_thread(lambda: _call_sandbox_exec(self.CID, container), box)
        try:
            _join_tool_thread(t, 10.0, "sandbox_exec whose cleanup cannot reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "incomplete"
            assert "deadline_s" in result["timeout"]
            assert "elapsed_s" in result["timeout"]
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

    def test_deadline_reap_ok_when_cleanup_fully_reaps(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clean probe after TERM reports ``reap: "ok"`` and must not
        escalate to KILL (KILL is only a fallback)."""
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer(probe_output=b"")
        box: dict = {}
        t = _run_in_thread(lambda: _call_sandbox_exec(self.CID, container), box)
        try:
            _join_tool_thread(t, 10.0, "sandbox_exec whose cleanup fully reaps", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert "TERM" in container.events
            assert "KILL" not in container.events
        finally:
            container.release.set()
            t.join(10.0)


class TestRunPythonDeadlineChoreography:
    """run_python: same deadline/reap choreography (it shares the
    foreground command path even though it takes no explicit timeout)."""

    CID = "fgdc00000002"

    def test_deadline_reports_incomplete_reap_with_term_before_kill(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer()
        box: dict = {}
        t = _run_in_thread(lambda: _call_run_python(self.CID, container), box)
        try:
            _join_tool_thread(t, 10.0, "run_python whose cleanup cannot reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "incomplete"
            assert "TERM" in container.events
            assert "KILL" in container.events
            assert container.events.index("TERM") < container.events.index("KILL")
            assert "probe" in container.events
        finally:
            container.release.set()
            t.join(10.0)

    def test_deadline_reap_ok_when_cleanup_fully_reaps(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer(probe_output=b"")
        box: dict = {}
        t = _run_in_thread(lambda: _call_run_python(self.CID, container), box)
        try:
            _join_tool_thread(t, 10.0, "run_python whose cleanup fully reaps", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert "TERM" in container.events
            assert "KILL" not in container.events
        finally:
            container.release.set()
            t.join(10.0)


class TestPackageInstallDeadlineChoreography:
    """package_install (pip axis): the install exec is the blocked one; the
    surrounding ``pip list`` snapshots still answer."""

    CID = "fgdc00000003"

    def _scripted(self, probe_output: bytes = _ScriptedReapContainer.SURVIVOR) -> _ScriptedReapContainer:
        return _ScriptedReapContainer(probe_output=probe_output, block_substrings=("pip install",))

    def test_deadline_reports_incomplete_reap_with_term_before_kill(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = self._scripted()
        box: dict = {}
        t = _run_in_thread(
            lambda: _call_package_install(self.CID, container, packages="requests"),
            box,
        )
        try:
            _join_tool_thread(t, 10.0, "package_install whose cleanup cannot reap", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "incomplete"
            assert "TERM" in container.events
            assert "KILL" in container.events
            assert container.events.index("TERM") < container.events.index("KILL")
            assert "probe" in container.events
        finally:
            container.release.set()
            t.join(10.0)

    def test_deadline_reap_ok_when_cleanup_fully_reaps(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = self._scripted(probe_output=b"")
        box: dict = {}
        t = _run_in_thread(
            lambda: _call_package_install(self.CID, container, packages="requests"),
            box,
        )
        try:
            _join_tool_thread(t, 10.0, "package_install whose cleanup fully reaps", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "ok"
            assert "TERM" in container.events
            assert "KILL" not in container.events
        finally:
            container.release.set()
            t.join(10.0)


class TestPackageInstallNpmDeadlineChoreography:
    """package_install (npm axis): ``npm install``/``npm ci`` is the blocked
    exec; the lockfile check still answers."""

    CID = "fgdc00000004"

    def test_npm_install_exec_is_reaped_by_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer(
            probe_output=_ScriptedReapContainer.SURVIVOR,
            block_substrings=("npm install", "npm ci"),
        )
        box: dict = {}
        t = _run_in_thread(
            lambda: _call_package_install(self.CID, container, manager="npm"),
            box,
        )
        try:
            _join_tool_thread(t, 10.0, "npm package_install that must time out", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert result["timeout"]["reap"] == "incomplete"
            assert "TERM" in container.events
            assert "KILL" in container.events
            assert container.events.index("TERM") < container.events.index("KILL")
        finally:
            container.release.set()
            t.join(10.0)


# ---------------------------------------------------------------------------
# Journaling: one truthful terminal entry, no phantom success
# ---------------------------------------------------------------------------


class TestDeadlineJournaling:
    """Decision 5: journaling has one truthful terminal entry and no phantom
    success -- a timed-out call never records a completion with exit code 0."""

    CID = "fgdc00000005"

    def test_sandbox_exec_journals_one_nonzero_completion(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer()
        mock_start = MagicMock()
        mock_complete = MagicMock()
        box: dict = {}

        def _run() -> str:
            with (
                patch("sunaba.tools.exec._docker", return_value=_client_with(container)),
                patch("sunaba.tools.exec.journal_record_exec_start", mock_start),
                patch("sunaba.tools.exec.journal_record_exec", mock_complete),
            ):
                return sandbox_exec(self.CID, commands=["echo hello"])

        t = _run_in_thread(_run, box)
        try:
            _join_tool_thread(t, 10.0, "sandbox_exec journaling on deadline", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            # START entry (the "call") plus exactly one completion entry.
            assert mock_start.call_count == 1
            assert mock_complete.call_count == 1
            exit_code = mock_complete.call_args[0][2]
            assert exit_code != 0, "timed-out exec must not journal a success exit code"
        finally:
            container.release.set()
            t.join(10.0)

    def test_package_install_journals_one_nonzero_completion(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.3")
        container = _ScriptedReapContainer(block_substrings=("pip install",))
        mock_complete = MagicMock()
        box: dict = {}

        def _run() -> str:
            with (
                patch("sunaba.tools.package._docker", return_value=_client_with(container)),
                patch("sunaba.tools.package.journal_record_exec", mock_complete),
            ):
                return package_install(self.CID, packages="requests")

        t = _run_in_thread(_run, box)
        try:
            _join_tool_thread(t, 10.0, "package_install journaling on deadline", box)
            result = json.loads(box["value"])
            assert result["status"] == "timeout"
            assert mock_complete.call_count == 1
            exit_code = mock_complete.call_args[0][2]
            assert exit_code != 0, "timed-out install must not journal a success exit code"
        finally:
            container.release.set()
            t.join(10.0)


# ---------------------------------------------------------------------------
# Explicit timeout=N: existing result contract retained
# ---------------------------------------------------------------------------


class TestExplicitTimeoutContractRetained:
    """Decision 3: an explicit positive tool timeout stays authoritative for
    user-visible timing/status; the existing exit/status/error mapping --
    especially exit 124 -- is preserved."""

    @patch("sunaba.tools.exec._docker")
    def test_timeout_124_contract_unchanged(self, mock_docker: MagicMock) -> None:
        """``timeout=N`` + exit 124 still maps to ``status: "timeout"`` with
        ``exit_code: 124`` (issue #131 contract)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (124, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(sandbox_exec(
            container_id="abc123def456",
            commands=["sleep 60"],
            timeout=5,
        ))
        assert result["status"] == "timeout"
        assert result["exit_code"] == 124

    @patch("sunaba.tools.exec._docker")
    def test_timeout_zero_not_applied(self, mock_docker: MagicMock) -> None:
        """``timeout=0`` (default) does not wrap the command with
        ``timeout(1)`` (existing fast-path contract)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"ok\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec(container_id="abc123def456", commands=["echo ok"])
        cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "timeout" not in cmd


# ---------------------------------------------------------------------------
# Fast-path preservation and private marker propagation
# ---------------------------------------------------------------------------


class TestFastPathPreservedWithMarker:
    """Decision 4: fast commands preserve existing arguments/env/output
    exactly apart from the private per-call marker environment, which every
    foreground exec inherits so the reaper can find its tree."""

    def _decode_b64_payload(self, shell_cmd: str) -> str:
        """Decode the base64 command payload sandbox_exec embeds."""
        import base64

        b64_start = shell_cmd.index("echo ") + 5
        b64_end = shell_cmd.index(" | base64 -d")
        return base64.b64decode(shell_cmd[b64_start:b64_end]).decode("utf-8")

    @patch("sunaba.tools.exec._docker")
    def test_sandbox_exec_fast_path_output_unchanged_with_marker(
        self, mock_docker: MagicMock,
    ) -> None:
        """Success JSON shape is unchanged (no timeout keys) and the exec
        carries exactly the private marker environment."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"hello world\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(sandbox_exec(container_id="abc123def456", commands=["echo hello"]))
        assert result["status"] == "ok"
        assert "hello world" in result["output"]
        for key in ("shown", "total_lines", "truncated", "next_offset", "has_more"):
            assert key in result
        assert "timeout" not in result
        assert "exit_code" not in result

        assert mock_container.exec_run.call_count == 1
        called = mock_container.exec_run.call_args
        env = called.kwargs.get("environment")
        assert env is not None, "foreground exec must inherit the private marker environment"
        assert list(env) == [_FOREGROUND_MARKER_ENV]
        assert env[_FOREGROUND_MARKER_ENV]
        # Command text unchanged: the marker lives in the environment only.
        shell_cmd = called.args[0][-1]
        assert self._decode_b64_payload(shell_cmd) == "echo hello"

    @patch("sunaba.tools.exec._docker")
    def test_marker_is_per_call_unique(self, mock_docker: MagicMock) -> None:
        """Two calls receive different markers (the marker is per-call
        private, so one call's reap can never touch another call's tree)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"ok\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec(container_id="abc123def456", commands=["echo one"])
        first = mock_container.exec_run.call_args.kwargs["environment"][_FOREGROUND_MARKER_ENV]
        sandbox_exec(container_id="abc123def456", commands=["echo two"])
        second = mock_container.exec_run.call_args.kwargs["environment"][_FOREGROUND_MARKER_ENV]
        assert first != second

    @patch("sunaba.tools.exec._docker")
    def test_argv_fast_path_unchanged_with_marker(self, mock_docker: MagicMock) -> None:
        """argv mode: the argv list reaches exec_run verbatim, with the
        marker carried only in the environment (issue #234/#228 contract)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"https://x/issues/1\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        body = "multi\nline $'quoted'"
        result = json.loads(sandbox_exec(
            container_id="abc123def456",
            argv=["gh", "issue", "create", "--title", "x", "--body", body],
        ))
        assert result["status"] == "ok"
        called = mock_container.exec_run.call_args
        assert called.args[0] == ["gh", "issue", "create", "--title", "x", "--body", body]
        env = called.kwargs.get("environment")
        assert env is not None
        assert list(env) == [_FOREGROUND_MARKER_ENV]

    def test_run_python_fast_path_unchanged_with_marker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_python: result JSON is unchanged and the runner exec carries
        the marker environment."""
        import base64 as _b64
        import re as _re
        from io import StringIO

        environments: list[dict | None] = []

        class _FakeContainer:
            def exec_run(self, cmd: object, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
                environments.append(kwargs.get("environment"))
                shell_cmd = _cmd_text(cmd)
                blob = (
                    shell_cmd.split("echo ", 1)[1]
                    .split(" | base64 -d", 1)[0]
                    .strip("'\"")
                )
                runner_src = _b64.b64decode(blob).decode("utf-8")
                runner_src = _re.sub(
                    r"^WORKING_DIR = .+$", "WORKING_DIR = None", runner_src, count=1,
                    flags=_re.M,
                )
                runner_globals: dict = {}
                buf = StringIO()
                old = sys.stdout
                sys.stdout = buf
                try:
                    try:
                        exec(compile(runner_src, "<runner>", "exec"), runner_globals)
                    except SystemExit:
                        pass
                finally:
                    sys.stdout = old
                return 0, (buf.getvalue().encode("utf-8"), b"")

        fake = _FakeContainer()
        fake_client = _client_with(fake)
        monkeypatch.setattr("sunaba.tools.run_python._docker", lambda: fake_client)

        result = json.loads(run_python("abc123", "print('hello')"))
        assert result["status"] == "ok"
        assert result["stdout"] == "hello\n"
        assert result["exit_code"] == 0
        assert "timeout" not in result
        assert len(environments) == 1
        env = environments[0]
        assert env is not None
        assert list(env) == [_FOREGROUND_MARKER_ENV]
        assert env[_FOREGROUND_MARKER_ENV]

    @patch("sunaba.tools.package._docker")
    def test_package_install_fast_path_unchanged_with_marker(
        self, mock_docker: MagicMock,
    ) -> None:
        """package_install: result JSON is unchanged and every exec of the
        call (pip list snapshots and the install) inherits the marker."""
        container = MagicMock()
        environments: list[dict | None] = []

        def exec_run_side_effect(cmd: object, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
            environments.append(kwargs.get("environment"))
            text = _cmd_text(cmd)
            if "pip list" in text:
                return (0, (b'[{"name": "pip", "version": "23.0"}]', b""))
            return (0, (b"Successfully installed requests-2.31.0", b""))

        container.exec_run.side_effect = exec_run_side_effect
        mock_client = MagicMock()
        mock_client.containers.get.return_value = container
        mock_docker.return_value = mock_client

        result = json.loads(package_install(container_id="abc123", packages="requests"))
        assert result["status"] == "ok"
        assert "timeout" not in result
        # pip list before, install, pip list after.
        assert len(environments) == 3
        for env in environments:
            assert env is not None
            assert list(env) == [_FOREGROUND_MARKER_ENV]


# ---------------------------------------------------------------------------
# Background path is not armed by the foreground deadline
# ---------------------------------------------------------------------------


class TestBackgroundNotArmed:
    """Decision 6: ``sandbox_exec_background`` remains intentionally durable
    across MCP disconnects and is not given the foreground deadline -- its
    dispatch exec carries no marker and no reap machinery ever runs."""

    @patch("sunaba.tools.exec._docker")
    def test_background_dispatch_has_no_marker_and_single_exec(
        self, mock_docker: MagicMock, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(_FOREGROUND_TIMEOUT_ENV, "0.05")
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        job_id = sandbox_exec_background("abc123def456", ["echo hi"])
        assert job_id.startswith("abc123def456-")
        # A single detached dispatch -- no deadline arm, no reap execs.
        assert mock_container.exec_run.call_count == 1
        called = mock_container.exec_run.call_args
        assert called.kwargs.get("detach") is True
        env = called.kwargs.get("environment")
        assert env is None or _FOREGROUND_MARKER_ENV not in env
