"""Dependency preparation deadline: real-process descendant reaping and contract.

Issue #949: Dependency preparation (uv sync, pip install, npm ci/install) in
_install_repo_deps runs under a server-side wall-clock deadline so an install
exceeding the MCP boundary (~300s) terminates boundedly, reaps all marked ordinary
and setsid-detached descendants, records uv state failure, blocks subsequent
verify/type checks, and reports the failure cleanly to callers.

Covered here:
* Criterion 1: Real-process reaping for uv sync, pip install, and npm ci/install
  using _LocalExecContainer with ordinary and setsid-detached SIGTERM-ignoring
  descendants under SUNABA_DEPS_TIMEOUT; bounded return, all positive live PIDs
  reaped, no marked processes remain, DepsResult.failed True, step/deadline_s/reap
  diagnostics, uv state failed, and subsequent commands succeed. Bounded cleanup
  ensures red tests never leak or hang.
* Criterion 2: SUNABA_DEPS_TIMEOUT=0 disables deadline (installer lasting beyond
  comparison deadline exits cleanly with normal result and unchanged notes); quick
  successful install under positive deadline preserves exact result note, passes
  private foreground marker environment, and preserves command order (uv before npm).
* Criterion 3: Partial uv .venv after real timeout causes uv_environment_block_reason
  to block verify/type check, and invalidates old verify success; caller initialization
  responses (sandbox_initialize, run_container_and_exec, _setup_branch, _setup_pr_branch)
  visibly report failed deps line and error flag.
* Criterion 4: SUNABA_DEPS_TIMEOUT configuration parsing (default/override/zero/bad/negative);
  existing-scope reuse (reuse active outer scope, do not close caller's scope; owned scope
  closed on return); shared deadline budget across manifest probes and installer execs.
* Criterion 5: CI compatibility without skip decorators, docker socket, network, or
  host-only paths.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import sunaba.edit_verify.shell as shell
from sunaba.edit_verify.shell import (
    _FOREGROUND_MARKER_ENV,
    _active_foreground_scope,
    close_foreground_deadline,
    open_foreground_deadline,
)
from sunaba.environment import uv_environment_block_reason
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.clone import (
    DepsResult,
    _install_repo_deps,
    _setup_branch,
    _setup_pr_branch,
)
from sunaba.tools.container.lifecycle import run_container_and_exec, sandbox_initialize
from sunaba.tools.verify import type_check_in_container, verify_in_container
from sunaba.verify_state import (
    get_environment_state,
    has_verify_success,
    record_verify_success,
)
from tests.test_foreground_deadline_reap_real import (
    _cmd_text,
    _LocalExecContainer,
    _pid_alive,
    _read_pid,
    _run_in_thread,
    _token_processes,
    _wait_pids_dead,
)

_DEPS_TIMEOUT_ENV = "SUNABA_DEPS_TIMEOUT"


class _LocalDepsContainer(_LocalExecContainer):
    id: str = ""


def _join_deps_thread(t: threading.Thread, seconds: float, what: str, box: dict[str, Any]) -> None:
    """Join thread *t* bounded by *seconds* or fail with descriptive deadline failure."""
    t.join(seconds)
    if t.is_alive():
        pytest.fail(
            f"{what} did not return within {seconds}s -- the "
            f"{_DEPS_TIMEOUT_ENV} deadline (dependency preparation deadline) "
            "is not enforced"
        )
    error = box.get("error")
    if error is not None:
        _exc_type, exc_value, exc_tb = error
        raise exc_value.with_traceback(exc_tb)


def _processes_with_env_marker(marker: str) -> list[int]:
    """Find processes whose /proc/<pid>/environ contains *marker* as an exact env entry."""
    if not marker:
        return []
    found: list[int] = []
    proc_root = Path("/proc")
    target = f"{_FOREGROUND_MARKER_ENV}={marker}".encode("utf-8")
    self_pid = os.getpid()
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 1 or pid == self_pid:
            continue
        try:
            environ_bytes = (entry / "environ").read_bytes()
            if target in environ_bytes.split(b"\x00"):
                found.append(pid)
        except OSError:
            continue
    return found


def _reap_process_tree(pids: list[int | None], token: str, marker: str | None = None) -> None:
    """Kill all processes carrying *token*, *marker*, or descended from *pids* (bounded test hygiene)."""
    self_pid = os.getpid()
    self_ppid = os.getppid()
    all_to_kill = set(p for p in pids if p is not None and p > 1 and p != self_pid and p != self_ppid)
    if marker:
        all_to_kill.update(_processes_with_env_marker(marker))
    try:
        proc_root = Path("/proc")
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                pid = int(entry.name)
                if pid <= 1 or pid == self_pid or pid == self_ppid:
                    continue
                stat = (entry / "stat").read_text().split()
                ppid = int(stat[3])
                if ppid in all_to_kill and ppid > 1:
                    all_to_kill.add(pid)
                if token:
                    cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
                    if token.encode("utf-8") in cmdline:
                        all_to_kill.add(pid)
            except OSError:
                continue
    except OSError:
        pass
    for p in all_to_kill:
        if p > 1 and p != self_pid and p != self_ppid:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def _make_hanging_script(token: str, log: str, ready: str, parent_pid: str, child_pid: str, ord_pid: str) -> str:
    """Fake installer script starting ordinary + setsid-detached TERM-ignoring descendants.

    The detached child explicitly ignores TERM using `trap '' TERM`.
    Ready file is emitted only once all three PID files (parent, child, ord) exist and are non-empty.
    """
    return (
        f"trap 'echo TERM >> {log}' TERM; "
        f'setsid sh -c \'trap "" TERM; '
        f"echo $$ > {child_pid}; while :; do sleep 60; done # {token}' >/dev/null 2>&1 & "
        f"sh -c 'echo $$ > {ord_pid}; while :; do sleep 60; done # {token}-o' >/dev/null 2>&1 & "
        f"echo $$ > {parent_pid}; "
        f"while [ ! -s {child_pid} ] || [ ! -s {ord_pid} ] || [ ! -s {parent_pid} ]; do sleep 0.01; done; "
        f"echo ready > {ready}; "
        f"while :; do sleep 60; done"
    )


def _run_timeout_reap_case(
    step_name: str,
    manifest_files: list[str],
    sub_key: str,
    ready_exec_substring: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    is_uv: bool = False,
) -> None:
    for mf in manifest_files:
        (tmp_path / mf).touch()

    token = f"sunabadeps-{uuid.uuid4().hex[:12]}"
    log = str(tmp_path / f"{token}.log")
    ready = str(tmp_path / f"{token}.ready")
    parent_pid = str(tmp_path / f"{token}.parent")
    child_pid = str(tmp_path / f"{token}.child")
    ord_pid = str(tmp_path / f"{token}.ord")

    script = _make_hanging_script(token, log, ready, parent_pid, child_pid, ord_pid)
    fake = _LocalDepsContainer(
        substitute={sub_key: script},
        ready_path=ready,
        ready_exec_substring=ready_exec_substring,
    )
    cid = f"dep{uuid.uuid4().hex[:7]}"
    fake.id = cid

    deadline_s = 1.5
    monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(deadline_s))
    box: dict[str, Any] = {}
    t = _run_in_thread(
        lambda: _install_repo_deps(fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True),
        box,
    )
    pids: list[int | None] = []
    marker: str | None = None
    try:
        if not fake.tree_ready.wait(15.0):
            error = box.get("error")
            if error is not None:
                _exc_type, exc_value, exc_tb = error
                raise exc_value.with_traceback(exc_tb)
            raise AssertionError(f"{step_name} fake command did not create its marked tree")

        parent = _read_pid(parent_pid)
        child = _read_pid(child_pid)
        ord_child = _read_pid(ord_pid)
        assert parent is not None and parent > 0, f"parent pid must be positive: {parent}"
        assert child is not None and child > 0, f"detached child pid must be positive: {child}"
        assert ord_child is not None and ord_child > 0, f"ordinary child pid must be positive: {ord_child}"
        live_pids: list[int] = [parent, child, ord_child]
        pids = [parent, child, ord_child]
        for p in live_pids:
            assert _pid_alive(p), f"marked pid {p} must be alive before cleanup"

        _join_deps_thread(t, 15.0, f"_install_repo_deps ({step_name})", box)
        result = box["value"]

        # Bounded return, actual DepsResult.failed True (no getattr fallbacks)
        assert result.failed is True, f"expected DepsResult.failed True, got: {result!r}"

        # Step / numeric deadline_s / reap ok diagnostics
        note = result.note
        assert step_name in note, f"note must identify {step_name} step: {note!r}"
        assert (
            f"{deadline_s:g}s" in note
            or f"{deadline_s}s" in note
            or f"{deadline_s:g}" in note
            or str(deadline_s) in note
        ), f"note must report configured numeric deadline {deadline_s}: {note!r}"
        assert re.search(r"reap[=:\s]+ok\b", note, re.IGNORECASE) is not None, (
            f"note must report reap verdict ok: {note!r}"
        )

        # Foreground marker captured from installer exec environment
        captured_markers = [
            kwargs["environment"][_FOREGROUND_MARKER_ENV]
            for _cmd, kwargs in fake.exec_calls
            if isinstance(kwargs.get("environment"), dict) and _FOREGROUND_MARKER_ENV in kwargs["environment"]
        ]
        assert captured_markers, f"installer exec kwargs must include {_FOREGROUND_MARKER_ENV}"
        marker = captured_markers[0]

        # No remaining processes carrying the actual foreground marker
        assert marker is not None
        assert _processes_with_env_marker(marker) == [], (
            f"processes carrying foreground marker {marker} still exist after reap"
        )

        # No marked / token PIDs remain
        valid_pids = [p for p in pids if p is not None]
        assert _wait_pids_dead(valid_pids), f"marked pids still alive: {valid_pids}"
        assert _token_processes(token) == [], "token processes still exist after reap"

        # uv environment state failed (when applicable)
        if is_uv:
            state = get_environment_state(cid)
            assert state is not None, "environment state must be recorded on uv timeout"
            assert state.get("status") == "failed", f"expected status failed, got: {state!r}"
            reason = str(state.get("reason") or "")
            assert "deps_timeout" in reason, f"expected reason to contain 'deps_timeout': {reason!r}"

        # Subsequent exec in container succeeds
        ec, out = fake.exec_run(["echo", "ok"])
        out_bytes = out if isinstance(out, bytes) else b"".join(out)
        assert ec == 0 and b"ok" in out_bytes, "container unusable after deadline reap"
    finally:
        all_pids = [_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)]
        _reap_process_tree(all_pids + pids, token, marker)
        for p in (log, ready, parent_pid, child_pid, ord_pid):
            Path(p).unlink(missing_ok=True)
        t.join(2.0)
        fake.closed = True


class TestDepsDeadlineRealProcessReap:
    """Criterion 1: Real-process descendant reaping under SUNABA_DEPS_TIMEOUT matrix."""

    @pytest.mark.parametrize(
        ("case_name", "manifest_files", "sub_key", "step_name", "is_uv"),
        [
            ("uv_sync", ["pyproject.toml", "uv.lock"], "uv sync", "uv sync", True),
            ("pip_install", ["setup.py"], "pip install", "pip install", False),
            ("npm_ci", ["package.json", "package-lock.json"], "npm ci", "npm", False),
            ("npm_install_fallback", ["package.json"], "npm install", "npm", False),
        ],
        ids=["uv_sync", "pip_install", "npm_ci", "npm_install_fallback"],
    )
    def test_deadline_reaps_setsid_detached_descendant(
        self,
        case_name: str,
        manifest_files: list[str],
        sub_key: str,
        step_name: str,
        is_uv: bool,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        _run_timeout_reap_case(
            step_name,
            manifest_files,
            sub_key,
            sub_key,
            monkeypatch,
            tmp_path,
            is_uv=is_uv,
        )


class TestDeadlineZeroAndQuickInstall:
    """Criterion 2: Deadline 0 disabled and quick successful install preserving order/markers."""

    def test_deadline_zero_does_not_fire_for_installer_exceeding_comparison_deadline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()

        # Finite self-ending installer taking 0.6s (exceeds comparison threshold 0.25s)
        fake = _LocalDepsContainer(substitute={"uv sync": "sleep 0.6; echo ok"})
        fake.id = f"zero{uuid.uuid4().hex[:8]}"

        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "0")
        box: dict[str, Any] = {}
        t0 = time.monotonic()
        t = _run_in_thread(
            lambda: _install_repo_deps(
                fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
            ),
            box,
        )
        _join_deps_thread(t, 15.0, "_install_repo_deps under deadline 0", box)
        elapsed = time.monotonic() - t0
        result = box["value"]

        assert elapsed >= 0.5, f"installer elapsed {elapsed}s must exceed comparison threshold 0.25s"
        assert result.failed is False, f"expected DepsResult success under deadline 0, got: {result!r}"
        assert "uv sync --locked --all-groups ok" in result.note
        assert "timeout" not in result.note.lower()

    def test_quick_install_under_positive_deadline_preserves_order_and_passes_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()
        (tmp_path / "package.json").touch()
        (tmp_path / "package-lock.json").touch()

        # Run 1: positive deadline (30.0s)
        fake_pos = _LocalDepsContainer(substitute={"uv sync": "echo ok", "npm ci": "echo ok"})
        fake_pos.id = f"pos{uuid.uuid4().hex[:9]}"
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "30.0")
        res_pos = _install_repo_deps(
            fake_pos, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
        )

        # Run 2: deadline 0
        fake_zero = _LocalDepsContainer(substitute={"uv sync": "echo ok", "npm ci": "echo ok"})
        fake_zero.id = f"zero{uuid.uuid4().hex[:9]}"
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "0")
        res_zero = _install_repo_deps(
            fake_zero, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
        )

        # Exact result note must match
        assert res_pos.failed is False and res_zero.failed is False
        assert res_pos.note == res_zero.note
        assert "uv sync --locked --all-groups ok" in res_pos.note
        assert "npm ci ok" in res_pos.note

        def _user_commands(calls: list[tuple[object, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
            reaper_patterns = ("/proc/", "stat", "kill", "xargs")
            res = []
            for cmd, kwargs in calls:
                text = _cmd_text(cmd)
                if any(p in text for p in reaper_patterns):
                    continue
                res.append((text, kwargs))
            return res

        pos_cmds = _user_commands(fake_pos.exec_calls)
        zero_cmds = _user_commands(fake_zero.exec_calls)

        # Manifest and install command order must be identical
        assert [c[0] for c in pos_cmds] == [c[0] for c in zero_cmds]

        # Mixed repo order: manifest probe, then uv before npm
        manifest_idx = next(i for i, c in enumerate(pos_cmds) if "for f in pyproject.toml" in c[0])
        uv_idx = next(i for i, c in enumerate(pos_cmds) if "uv sync" in c[0])
        npm_idx = next(i for i, c in enumerate(pos_cmds) if "npm ci" in c[0])
        assert manifest_idx < uv_idx < npm_idx, (
            f"expected manifest probe before uv sync before npm ci, got indices: {manifest_idx}, {uv_idx}, {npm_idx}"
        )

        # Ensure every user exec including manifest carries the same marker when enabled
        markers: list[str] = []
        for cmd_text, call_kwargs in pos_cmds:
            env = call_kwargs.get("environment")
            assert isinstance(env, dict), f"exec {cmd_text!r} missing environment dict: {call_kwargs!r}"
            assert _FOREGROUND_MARKER_ENV in env, f"exec {cmd_text!r} must carry {_FOREGROUND_MARKER_ENV}"
            markers.append(env[_FOREGROUND_MARKER_ENV])
        assert len(markers) >= 3, "expected at least manifest probe, uv sync, and npm ci execs"
        assert len(set(markers)) == 1, f"all user execs must carry the same foreground marker: {markers}"


class TestUvEnvironmentStateAndVerifyBlockingAfterRealTimeout:
    """Criterion 3: Partial uv .venv after real timeout causes uv_environment_block_reason
    to block verify/type check and invalidates prior verify success."""

    def test_partial_venv_blocked_by_uv_environment_block_reason_and_verify_invalidated(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()
        (tmp_path / "mod.py").write_text("x: int = 1\n")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True)

        # Simulate partial .venv created before / during failure
        venv_dir = tmp_path / ".venv" / "bin"
        venv_dir.mkdir(parents=True, exist_ok=True)
        (venv_dir / "python").touch(mode=0o755)

        cid = f"uvblk{uuid.uuid4().hex[:7]}"
        record_verify_success(cid)
        assert has_verify_success(cid) is True, "prior verify success must be recorded initially"

        token = f"sunabadeps-{uuid.uuid4().hex[:12]}"
        log = str(tmp_path / f"{token}.log")
        ready = str(tmp_path / f"{token}.ready")
        parent_pid = str(tmp_path / f"{token}.parent")
        child_pid = str(tmp_path / f"{token}.child")
        ord_pid = str(tmp_path / f"{token}.ord")

        script = _make_hanging_script(token, log, ready, parent_pid, child_pid, ord_pid)
        fake = _LocalDepsContainer(
            substitute={"uv sync": script},
            ready_path=ready,
            ready_exec_substring="uv sync",
        )
        fake.id = cid

        deadline_s = 1.5
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(deadline_s))
        box: dict[str, Any] = {}
        t = _run_in_thread(
            lambda: _install_repo_deps(
                fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
            ),
            box,
        )
        pids: list[int | None] = []
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("uv sync fake command did not create its marked tree")

            parent = _read_pid(parent_pid)
            child = _read_pid(child_pid)
            ord_child = _read_pid(ord_pid)
            assert parent is not None and parent > 0
            assert child is not None and child > 0
            assert ord_child is not None and ord_child > 0
            live_pids: list[int] = [parent, child, ord_child]
            pids = [parent, child, ord_child]
            for p in live_pids:
                assert _pid_alive(p)

            _join_deps_thread(t, 15.0, "_install_repo_deps (uv sync timeout)", box)

            # Prior verify success must be invalidated
            assert has_verify_success(cid) is False, "prior verify success must be invalidated after uv deps timeout"

            # Environment state recorded as failed with deps_timeout
            state = get_environment_state(cid)
            assert state is not None, "environment state must exist"
            assert state.get("status") == "failed", f"expected failed state, got: {state!r}"
            reason = str(state.get("reason") or "")
            assert "deps_timeout" in reason, f"expected deps_timeout in reason: {reason!r}"

            # uv_environment_block_reason must block subsequent verify/type checking
            block_reason = uv_environment_block_reason(fake, str(tmp_path))
            assert block_reason is not None, "uv_environment_block_reason must block partial venv"
            assert "failed" in block_reason and "deps_timeout" in block_reason, (
                f"uv_environment_block_reason must report failure detail: {block_reason!r}"
            )

            # Invoke actual verify and single-file type-check entrypoints with that container
            client = MagicMock()
            client.containers.get.return_value = fake
            client.containers.run.return_value = fake
            with (
                patch("sunaba.tools.verify._docker", return_value=client),
                patch("sunaba.tools.vcs.publishing._docker", return_value=client),
            ):
                verify_raw = verify_in_container(
                    container_id=cid,
                    path=".",
                    working_dir=str(tmp_path),
                    skip_lint_gate=True,
                    skip_type_gate=True,
                    skip_patch_targets_gate=True,
                )
                verify_res = json.loads(verify_raw)
                assert verify_res.get("gate_passed") is False
                assert verify_res.get("status") == "environment_setup_failed"
                assert "deps_timeout" in str(verify_res) and "failed" in str(verify_res)
                assert has_verify_success(cid) is False

                type_raw = type_check_in_container(
                    container_id=cid,
                    file_path=str(tmp_path / "mod.py"),
                )
                type_res = json.loads(type_raw)
                assert any(
                    "deps_timeout" in str(item) and "failed" in str(item)
                    for item in (type_res if isinstance(type_res, list) else [type_res])
                )
        finally:
            all_pids = [_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)]
            _reap_process_tree(all_pids + pids, token)
            for p in (log, ready, parent_pid, child_pid, ord_pid):
                Path(p).unlink(missing_ok=True)
            t.join(2.0)
            fake.closed = True


class TestCallerInitializationPlumbing:
    """Criterion 3: All four callers visibly report failed deps and error flag."""

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @patch("sunaba.tools.container.lifecycle._install_repo_deps")
    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_sandbox_initialize_reports_failed_deps_and_error(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_install_deps: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "initfail0001"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        mock_install_deps.return_value = DepsResult(
            note="deps: uv sync timeout 2.0s, reap ok",
            failed=True,
        )

        result = sandbox_initialize(
            image="python@sha256:" + "0" * 64,
            clone_repo="owner/repo",
            allow_network=True,
        )

        assert "deps: uv sync" in result
        assert "timeout" in result
        assert "deps failed" in result.lower() or "[deps failed" in result.lower() or "error" in result.lower(), (
            f"sandbox_initialize must visibly report failed deps and error indicator: {result!r}"
        )

    @patch("sunaba.tools.container.lifecycle._install_repo_deps")
    @patch("sunaba.tools.container.lifecycle._try_clone_into_container")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_run_container_and_exec_reports_failed_deps_and_error(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_install_deps: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "execfail0001"
        mock_container.exec_run.return_value = (0, (b"hello\n", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = ("Clone OK", None)

        mock_install_deps.return_value = DepsResult(
            note="deps: pip install timeout 2.0s, reap ok",
            failed=True,
        )

        raw = run_container_and_exec(
            image="python@sha256:" + "0" * 64,
            commands=["echo hello"],
            clone_repo="owner/repo",
            allow_network=True,
        )
        result = json.loads(raw)

        # Result must visibly report failed deps and indicate error flag (not status 'ok' alone)
        has_error_flag = (
            result.get("status") == "error"
            or bool(result.get("deps_error"))
            or bool(result.get("deps_failed"))
            or bool(result.get("error"))
        )
        assert has_error_flag, f"run_container_and_exec must carry error flag on failed deps: {result!r}"
        reported = str(result.get("deps_error") or result.get("error") or result.get("clone_warning") or "")
        assert "deps: pip install" in reported and "timeout" in reported

    @patch("sunaba.tools.container.clone._resolve_default_branch", return_value="main")
    @patch("sunaba.tools.container.clone._install_repo_deps")
    @patch("sunaba.tools.container.clone._write_clone_meta")
    @patch("sunaba.tools.container.clone.record_copy")
    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    def test_setup_branch_reports_failed_deps_and_error(
        self,
        mock_clone: MagicMock,
        mock_record: MagicMock,
        mock_meta: MagicMock,
        mock_install_deps: MagicMock,
        mock_branch: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "branchfail01"
        mock_container.exec_run.return_value = (0, (b"feature\n", b""))

        mock_install_deps.return_value = DepsResult(
            note="deps: npm ci timeout 2.0s, reap ok",
            failed=True,
        )

        result = _setup_branch(
            mock_container,
            "branchfail01",
            "owner/repo",
            "feature",
            "/tmp/dest",
            install_deps=True,
        )

        assert "deps: npm ci" in result
        assert "timeout" in result
        assert "deps failed" in result.lower() or "[deps failed" in result.lower() or "error" in result.lower(), (
            f"_setup_branch must visibly report deps failure: {result!r}"
        )

    @patch("sunaba.tools.container.clone._install_repo_deps")
    @patch("sunaba.tools.container.clone._write_clone_meta")
    @patch("sunaba.tools.container.clone.record_copy")
    @patch("sunaba.tools.container.clone._resolve_pr_head_ref")
    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    def test_setup_pr_branch_reports_failed_deps_and_error(
        self,
        mock_clone: MagicMock,
        mock_head: MagicMock,
        mock_record: MagicMock,
        mock_meta: MagicMock,
        mock_install_deps: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "prfail000001"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_head.return_value = ("pr-ref", "main")

        mock_install_deps.return_value = DepsResult(
            note="deps: uv sync timeout 2.0s, reap ok",
            failed=True,
        )

        result = _setup_pr_branch(
            mock_container,
            "prfail000001",
            "owner/repo",
            42,
            "/tmp/dest",
            authenticated=False,
            install_deps=True,
        )

        assert "deps: uv sync" in result
        assert "timeout" in result
        assert "deps failed" in result.lower() or "[deps failed" in result.lower() or "error" in result.lower(), (
            f"_setup_pr_branch must visibly report deps failure: {result!r}"
        )

    @patch("sunaba.tools.container.lifecycle._install_repo_deps")
    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_sandbox_initialize_tool_mcp_wrapper_sets_is_error(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
        mock_install_deps: MagicMock,
    ) -> None:
        import asyncio

        from fastmcp.tools import ToolResult

        from sunaba.tools.container.lifecycle import sandbox_initialize_tool

        mock_container = MagicMock()
        mock_container.id = "wrapfail0001"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        mock_install_deps.return_value = DepsResult(
            note="deps: uv sync timeout 2.0s, reap ok",
            failed=True,
        )

        res = asyncio.run(
            sandbox_initialize_tool(
                image="python@sha256:" + "0" * 64,
                clone_repo="owner/repo",
                allow_network=True,
            )
        )
        assert isinstance(res, ToolResult), f"expected ToolResult, got {type(res)}: {res!r}"
        assert res.is_error is True, f"expected is_error True on failed deps: {res!r}"
        assert isinstance(res.content, list) and res.content
        first = res.content[0]
        assert hasattr(first, "text")
        text = getattr(first, "text", "")
        assert "deps: uv sync" in text and "timeout" in text


class TestDepsDeadlineConfig:
    """Criterion 4: SUNABA_DEPS_TIMEOUT parsing (default/override/zero/invalid/negative)."""

    def _observe_deadline(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> float:
        recorded: list[float] = []
        orig_open = shell.open_foreground_deadline

        def spy_open(container: Any, deadline_s: float, marker: str) -> None:
            recorded.append(deadline_s)
            return orig_open(container, deadline_s, marker)

        monkeypatch.setattr(shell, "open_foreground_deadline", spy_open)
        import sunaba.tools.container.clone as clone_mod

        if hasattr(clone_mod, "open_foreground_deadline"):
            monkeypatch.setattr(clone_mod, "open_foreground_deadline", spy_open)

        fake = _LocalDepsContainer(substitute={"uv sync": "echo ok"})
        fake.id = f"cfg{uuid.uuid4().hex[:8]}"

        old_exec_run = fake.exec_run

        def tracking_exec_run(cmd: object, **kwargs: Any) -> Any:
            scope = _active_foreground_scope()
            if scope is not None and not recorded:
                recorded.append(scope.deadline_s)
            return old_exec_run(cmd, **kwargs)

        fake.exec_run = tracking_exec_run  # type: ignore

        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()

        _install_repo_deps(fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True)
        if not recorded:
            raise AssertionError(
                f"no foreground deadline was armed during _install_repo_deps ({_DEPS_TIMEOUT_ENV} not observed)"
            )
        return recorded[0]

    def test_default_deadline_unset_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(_DEPS_TIMEOUT_ENV, raising=False)
        deadline = self._observe_deadline(monkeypatch, tmp_path)
        assert deadline > 0.0
        assert deadline < 300.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "14.5")
        assert self._observe_deadline(monkeypatch, tmp_path) == 14.5

    def test_zero_disables_deadline(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "0")
        assert self._observe_deadline(monkeypatch, tmp_path) == 0.0

    def test_invalid_values_fall_back_to_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(_DEPS_TIMEOUT_ENV, raising=False)
        default = self._observe_deadline(monkeypatch, tmp_path)
        for bad in ("abc", "", "   ", "bad15s"):
            monkeypatch.setenv(_DEPS_TIMEOUT_ENV, bad)
            assert self._observe_deadline(monkeypatch, tmp_path) == default, (
                f"bad value {bad!r} must fall back to default"
            )

    def test_negative_values_fall_back_to_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv(_DEPS_TIMEOUT_ENV, raising=False)
        default = self._observe_deadline(monkeypatch, tmp_path)
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "-10")
        assert self._observe_deadline(monkeypatch, tmp_path) == default

    def test_tiny_deadline_behavioral_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A tiny configured deadline causes a slow installer to time out."""
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()
        token = f"sunabadeps-{uuid.uuid4().hex[:12]}"
        log = str(tmp_path / f"{token}.log")
        ready = str(tmp_path / f"{token}.ready")
        parent_pid = str(tmp_path / f"{token}.parent")
        child_pid = str(tmp_path / f"{token}.child")
        ord_pid = str(tmp_path / f"{token}.ord")

        script = _make_hanging_script(token, log, ready, parent_pid, child_pid, ord_pid)
        fake = _LocalDepsContainer(
            substitute={"uv sync": script},
            ready_path=ready,
            ready_exec_substring="uv sync",
        )
        fake.id = f"tiny{uuid.uuid4().hex[:8]}"

        tiny_deadline = 0.5
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(tiny_deadline))
        box: dict[str, Any] = {}
        t = _run_in_thread(
            lambda: _install_repo_deps(
                fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
            ),
            box,
        )
        pids: list[int | None] = []
        try:
            if not fake.tree_ready.wait(15.0):
                raise AssertionError("tree not ready")
            pids = [_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)]
            _join_deps_thread(t, 15.0, "_install_repo_deps with tiny deadline", box)
            res = box["value"]
            assert res.failed is True
            assert f"{tiny_deadline:g}s" in res.note or str(tiny_deadline) in res.note
            assert re.search(r"reap[=:\s]+ok\b", res.note, re.IGNORECASE) is not None
        finally:
            all_pids = [_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)]
            _reap_process_tree(all_pids + pids, token)
            for p in (log, ready, parent_pid, child_pid, ord_pid):
                Path(p).unlink(missing_ok=True)
            t.join(2.0)
            fake.closed = True


class TestScopeReuseAndSharedBudget:
    """Criterion 4: Scope reuse, owned scope lifecycle, and single shared budget."""

    def test_existing_scope_reused_and_not_closed_by_deps(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When an outer foreground deadline is active, _install_repo_deps reuses it
        and leaves it active on return (does not close caller's scope)."""
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()

        fake = _LocalDepsContainer(substitute={"uv sync": "echo ok"})
        fake.id = f"reuse{uuid.uuid4().hex[:7]}"

        outer_marker = f"outermk{uuid.uuid4().hex[:8]}"
        open_foreground_deadline(fake, 60.0, outer_marker)
        try:
            result = _install_repo_deps(
                fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
            )
            assert result.failed is False

            # Execs must have run with the outer marker, not a new one
            for _cmd, call_kwargs in fake.exec_calls:
                env = call_kwargs.get("environment")
                assert isinstance(env, dict), f"exec missing environment dict: {call_kwargs!r}"
                assert env.get(_FOREGROUND_MARKER_ENV) == outer_marker, f"exec must reuse active outer marker: {env!r}"

            # Outer scope must STILL be active after _install_repo_deps returns
            active = _active_foreground_scope()
            assert active is not None, "outer scope must not be closed by _install_repo_deps"
            assert active.marker == outer_marker
        finally:
            close_foreground_deadline()

    def test_owned_scope_closed_on_return(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When no outer scope exists, _install_repo_deps arms its own scope and
        closes it in finally before returning."""
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()

        fake = _LocalDepsContainer(substitute={"uv sync": "echo ok"})
        fake.id = f"own{uuid.uuid4().hex[:9]}"

        assert _active_foreground_scope() is None, "no scope should be active before call"
        result = _install_repo_deps(fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True)
        assert result.failed is False

        # Verify that execs ran under an owned foreground marker environment
        assert len(fake.exec_calls) >= 2, "manifest probe and uv sync must both execute"
        for _cmd, call_kwargs in fake.exec_calls:
            env = call_kwargs.get("environment")
            assert isinstance(env, dict), f"exec kwargs must include environment dict: {call_kwargs!r}"
            assert _FOREGROUND_MARKER_ENV in env, f"exec must inherit private foreground marker: {env!r}"

        # After return, owned scope must be cleared
        assert _active_foreground_scope() is None, "owned scope must be closed on return"

    def test_shared_budget_across_manifest_probe_and_installer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Single shared budget covers both manifest detection and installer exec:
        time consumed by manifest probe reduces the remaining time for the installer."""
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "uv.lock").touch()

        token = f"sunabadeps-{uuid.uuid4().hex[:12]}"
        log = str(tmp_path / f"{token}.log")
        ready = str(tmp_path / f"{token}.ready")
        parent_pid = str(tmp_path / f"{token}.parent")
        child_pid = str(tmp_path / f"{token}.child")
        ord_pid = str(tmp_path / f"{token}.ord")

        installer_script = _make_hanging_script(token, log, ready, parent_pid, child_pid, ord_pid)

        # Manifest probe takes 0.8s; total budget is 1.4s.
        # Under a shared budget, installer has ~0.6s remaining before timeout.
        probe_sub = (
            "sleep 0.8; "
            f"cd {tmp_path} && for f in pyproject.toml uv.lock setup.py package.json "
            'package-lock.json go.mod Cargo.toml; do [ -e "$f" ] && printf \'%s\\n\' "$f" || true; done'
        )
        fake = _LocalDepsContainer(
            substitute={
                "for f in pyproject.toml": probe_sub,
                "uv sync": installer_script,
            },
            ready_path=ready,
            ready_exec_substring="uv sync",
        )
        cid = f"budget{uuid.uuid4().hex[:6]}"
        fake.id = cid

        opened_scopes: list[tuple[float, str]] = []
        orig_open = shell.open_foreground_deadline

        def spy_open(container: Any, deadline_s: float, marker: str) -> None:
            opened_scopes.append((deadline_s, marker))
            return orig_open(container, deadline_s, marker)

        monkeypatch.setattr(shell, "open_foreground_deadline", spy_open)
        import sunaba.tools.container.clone as clone_mod

        if hasattr(clone_mod, "open_foreground_deadline"):
            monkeypatch.setattr(clone_mod, "open_foreground_deadline", spy_open)

        observed_execs: list[tuple[str, str | None]] = []
        old_exec_run = fake.exec_run

        def tracking_exec_run(cmd: object, **kwargs: Any) -> Any:
            text = _cmd_text(cmd)
            env = kwargs.get("environment")
            marker = env.get(_FOREGROUND_MARKER_ENV) if isinstance(env, dict) else None
            observed_execs.append((text, marker))
            return old_exec_run(cmd, **kwargs)

        fake.exec_run = tracking_exec_run  # type: ignore

        deadline_s = 1.4
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(deadline_s))
        box: dict[str, Any] = {}
        t0 = time.monotonic()
        t = _run_in_thread(
            lambda: _install_repo_deps(
                fake, "testowner/testrepo", str(tmp_path), pip_extras="[dev]", allow_network=True
            ),
            box,
        )
        pids: list[int | None] = []
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("installer did not become ready")

            pids = [_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)]
            _join_deps_thread(t, 15.0, "_install_repo_deps with shared budget", box)
            elapsed = time.monotonic() - t0

            # Deterministic scope and deadline identity observation across execs:
            assert len(opened_scopes) == 1, (
                f"expected exactly one deadline scope opened, got {len(opened_scopes)}: {opened_scopes}"
            )
            opened_deadline, opened_marker = opened_scopes[0]
            assert opened_deadline == deadline_s

            manifest_obs = [obs for obs in observed_execs if "for f in pyproject.toml" in obs[0]]
            installer_obs = [obs for obs in observed_execs if "uv sync" in obs[0]]
            assert manifest_obs, "manifest detection exec must be observed"
            assert installer_obs, "installer exec must be observed"

            _m_cmd, m_marker = manifest_obs[0]
            _i_cmd, i_marker = installer_obs[0]
            assert m_marker is not None and m_marker == opened_marker, (
                "manifest probe must run with the armed foreground marker"
            )
            assert i_marker is not None and i_marker == opened_marker, (
                "installer must run with the same armed foreground marker"
            )

            # Finite timing: total duration terminates boundedly around 1.4s deadline (probe 0.8s + remaining 0.6s)
            assert elapsed < 6.0, f"shared budget exceeded: elapsed {elapsed}s for {deadline_s}s deadline"
            result = box["value"]
            assert result.failed is True
        finally:
            all_pids = [_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid)]
            _reap_process_tree(all_pids + pids, token)
            for p in (log, ready, parent_pid, child_pid, ord_pid):
                Path(p).unlink(missing_ok=True)
            t.join(2.0)
            fake.closed = True
