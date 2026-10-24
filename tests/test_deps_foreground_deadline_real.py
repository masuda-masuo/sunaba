"""Real-process tests for clone-time dependency install foreground deadline (Issue #949).

Pins:
1. Environment variable ``SUNABA_DEPS_TIMEOUT`` resolution contract:
   default 240s, numeric override, 0 disables, invalid/negative fall back.
2. Real-process descendant reaping for uv sync, pip install, and npm:
   when the server deadline fires, marked normal and setsid-detached
   descendants that ignore SIGTERM are completely reaped, returning
   ``DepsResult(..., failed=True)`` with ``error=True``, uv environment state
   recorded as "failed" (starting with "deps_timeout"), prior verify success
   invalidated, and the container remaining usable for follow-up execs.
3. ``SUNABA_DEPS_TIMEOUT=0`` never fires and preserves the normal install path.
4. An active outer foreground scope is reused and not closed by _install_repo_deps.
5. Partially created .venv is blocked by ``uv_environment_block_reason``.
6. ``sandbox_initialize`` response reflects deps failure and error flag is set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sunaba.edit_verify.shell import (
    _active_foreground_scope,
    close_foreground_deadline,
    new_foreground_marker,
    open_foreground_deadline,
)
from sunaba.environment import uv_environment_block_reason
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.clone import (
    _DEFAULT_DEPS_TIMEOUT,
    _DEPS_TIMEOUT_ENV,
    DepsResult,
    _install_repo_deps,
    resolve_deps_deadline,
)
from sunaba.tools.container.lifecycle import sandbox_initialize
from sunaba.verify_state import (
    clear_environment_state,
    get_environment_state,
    has_verify_success,
    record_environment_state,
    record_verify_success,
)
from tests.test_foreground_deadline_reap_real import (
    _join_tool_thread,
    _kill_pids,
    _kill_token_processes,
    _LocalExecContainer,
    _pid_alive,
    _read_pid,
    _run_in_thread,
    _token_processes,
    _wait_pids_dead,
)


class _DepsContainer(_LocalExecContainer):
    """Docker container lookalike executing locally with an explicit id."""

    def __init__(self, cid: str = "deps00000001", *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.id = cid


class TestDepsDeadlineConfig:
    """SUNABA_DEPS_TIMEOUT configuration resolution contract."""

    def test_default_is_240_below_client_boundary(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unset env resolves to 240.0s, fitting within the ~300s MCP client boundary."""
        monkeypatch.delenv(_DEPS_TIMEOUT_ENV, raising=False)
        deadline = resolve_deps_deadline()
        assert deadline == 240.0
        assert deadline == _DEFAULT_DEPS_TIMEOUT
        assert deadline < 300.0

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A positive numeric override is used as-is."""
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "3.5")
        assert resolve_deps_deadline() == 3.5

    def test_zero_disables_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exactly 0 disables the deadline."""
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "0")
        assert resolve_deps_deadline() == 0.0

    def test_invalid_values_fall_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Garbage or negative values fall back to default (240s)."""
        for bad in ("abc", "", "   ", "12x", "-1", "-10.5"):
            monkeypatch.setenv(_DEPS_TIMEOUT_ENV, bad)
            assert resolve_deps_deadline() == 240.0, f"failed for bad value {bad!r}"


class TestDepsForegroundDeadlineReapReal:
    """Real-process descendant reaping for uv sync, pip install, and npm."""

    def _make_hung_script(
        self,
        token: str,
        parent_pid: str,
        child_pid: str,
        ord_pid: str,
        ready: str,
        log: str,
    ) -> str:
        return (
            f"echo $$ > {parent_pid}; "
            f"setsid sh -c 'trap \"echo TERM >> {log}\" TERM; "
            f"echo $$ > {child_pid}; while :; do sleep 60; done # {token}' >/dev/null 2>&1 & "
            f"sh -c 'echo $$ > {ord_pid}; while :; do sleep 60; done # {token}-o' >/dev/null 2>&1 & "
            f"while [ ! -s {child_pid} ]; do sleep 0.01; done; "
            f"echo ready > {ready}; "
            f"while :; do sleep 60; done"
        )

    def test_uv_sync_deadline_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """uv sync: deadline fires, reap clears all marked descendants, and uv state is failed."""
        cid = "depsuv000001"
        clear_environment_state(cid)
        record_verify_success(cid)

        # Create uv manifests so _detect_manifests discovers uv
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
        (tmp_path / "uv.lock").write_text("", encoding="utf-8")

        token = f"sunabadeps-uv-{cid}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        parent_pid = f"/tmp/{token}.parent"
        child_pid = f"/tmp/{token}.child"
        ord_pid = f"/tmp/{token}.ord"

        script = self._make_hung_script(token, parent_pid, child_pid, ord_pid, ready, log)
        fake = _DepsContainer(
            cid=cid,
            substitute={"uv sync": script},
            ready_path=ready,
            ready_exec_substring="uv sync",
        )
        deadline_s = 3.0
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t = _run_in_thread(
            lambda: _install_repo_deps(fake, "owner/repo", str(tmp_path), "[dev]"),
            box,
        )
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("uv sync did not create its marked tree")

            pids = [
                p
                for p in (_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid))
                if p is not None
            ]
            assert len(pids) >= 2, "marked parent and descendants must exist before cleanup"
            for p in pids:
                assert _pid_alive(p), f"pid {p} must be alive before cleanup"

            _join_tool_thread(t, 25.0, "uv sync deps install that must time out", box)
            deps: DepsResult = box["value"]
            assert deps.failed is True
            assert deps.error is True
            assert "uv sync" in deps.note
            assert f"{deadline_s}s" in deps.note
            assert "reap: ok" in deps.note

            # uv environment state is recorded as "failed"
            env_state = get_environment_state(cid)
            assert env_state is not None
            assert env_state["status"] == "failed"
            reason = str(env_state.get("reason") or "")
            assert reason.startswith("deps_timeout")
            assert "uv sync" in reason
            assert "reap: ok" in reason

            # verify success record was invalidated
            assert has_verify_success(cid) is False

            # No marked process remains in the real process table
            assert _wait_pids_dead(pids), f"marked pids still alive after reap: {pids}"
            assert _token_processes(token) == []

            # Container remains usable for follow-up commands
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

    def test_pip_install_deadline_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """pip install: deadline fires, reap clears all marked descendants, error is True."""
        cid = "depspip00001"
        clear_environment_state(cid)
        record_verify_success(cid)

        # Create setup.py manifest (no uv.lock) -> selects pip
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")

        token = f"sunabadeps-pip-{cid}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        parent_pid = f"/tmp/{token}.parent"
        child_pid = f"/tmp/{token}.child"
        ord_pid = f"/tmp/{token}.ord"

        script = self._make_hung_script(token, parent_pid, child_pid, ord_pid, ready, log)
        fake = _DepsContainer(
            cid=cid,
            substitute={"pip install": script},
            ready_path=ready,
            ready_exec_substring="pip install",
        )
        deadline_s = 3.0
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t = _run_in_thread(
            lambda: _install_repo_deps(fake, "owner/repo", str(tmp_path), "[dev]"),
            box,
        )
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("pip install did not create its marked tree")

            pids = [
                p
                for p in (_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid))
                if p is not None
            ]
            assert len(pids) >= 2, "marked parent and descendants must exist before cleanup"
            for p in pids:
                assert _pid_alive(p), f"pid {p} must be alive before cleanup"

            _join_tool_thread(t, 25.0, "pip install deps that must time out", box)
            deps: DepsResult = box["value"]
            assert deps.failed is True
            assert deps.error is True
            assert "pip" in deps.note
            assert f"{deadline_s}s" in deps.note
            assert "reap: ok" in deps.note

            # verify success record was invalidated
            assert has_verify_success(cid) is False

            # No marked process remains in the real process table
            assert _wait_pids_dead(pids), f"marked pids still alive after reap: {pids}"
            assert _token_processes(token) == []

            # Container remains usable for follow-up commands
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

    def test_npm_deadline_reaps_setsid_detached_descendant(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """npm install/ci: deadline fires, reap clears all marked descendants, error is True."""
        cid = "depsnpm00001"
        clear_environment_state(cid)
        record_verify_success(cid)

        # Create package.json and package-lock.json -> selects npm ci
        (tmp_path / "package.json").write_text("{\"name\": \"test\"}\n", encoding="utf-8")
        (tmp_path / "package-lock.json").write_text("{\"name\": \"test\"}\n", encoding="utf-8")

        token = f"sunabadeps-npm-{cid}"
        log = f"/tmp/{token}.log"
        ready = f"/tmp/{token}.ready"
        parent_pid = f"/tmp/{token}.parent"
        child_pid = f"/tmp/{token}.child"
        ord_pid = f"/tmp/{token}.ord"

        script = self._make_hung_script(token, parent_pid, child_pid, ord_pid, ready, log)
        fake = _DepsContainer(
            cid=cid,
            substitute={"npm ci": script, "npm install": script},
            ready_path=ready,
            ready_exec_substring="npm ci",
        )
        deadline_s = 3.0
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, str(deadline_s))
        box: dict = {}
        t = _run_in_thread(
            lambda: _install_repo_deps(fake, "owner/repo", str(tmp_path), None),
            box,
        )
        try:
            if not fake.tree_ready.wait(15.0):
                error = box.get("error")
                if error is not None:
                    _exc_type, exc_value, exc_tb = error
                    raise exc_value.with_traceback(exc_tb)
                raise AssertionError("npm install did not create its marked tree")

            pids = [
                p
                for p in (_read_pid(parent_pid), _read_pid(child_pid), _read_pid(ord_pid))
                if p is not None
            ]
            assert len(pids) >= 2, "marked parent and descendants must exist before cleanup"
            for p in pids:
                assert _pid_alive(p), f"pid {p} must be alive before cleanup"

            _join_tool_thread(t, 25.0, "npm deps install that must time out", box)
            deps: DepsResult = box["value"]
            assert deps.failed is True
            assert deps.error is True
            assert "npm" in deps.note
            assert f"{deadline_s}s" in deps.note
            assert "reap: ok" in deps.note

            # verify success record was invalidated
            assert has_verify_success(cid) is False

            # No marked process remains in the real process table
            assert _wait_pids_dead(pids), f"marked pids still alive after reap: {pids}"
            assert _token_processes(token) == []

            # Container remains usable for follow-up commands
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

    def test_zero_deps_timeout_never_fires(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """SUNABA_DEPS_TIMEOUT=0 disables the deadline; normal path succeeds."""
        cid = "depsdis00001"
        monkeypatch.setenv(_DEPS_TIMEOUT_ENV, "0")

        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
        fake = _DepsContainer(
            cid=cid,
            substitute={"pip install": "echo 'Installed mock packages'"},
        )
        deps = _install_repo_deps(fake, "owner/repo", str(tmp_path), "[dev]")
        assert deps.failed is False
        assert deps.error is False
        assert "pip install ok" in deps.note

    def test_outer_scope_is_reused_and_not_closed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        """When an outer foreground scope is active, _install_repo_deps reuses it and does not close it."""
        cid = "depsout00001"
        (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
        fake = _DepsContainer(
            cid=cid,
            substitute={"pip install": "echo 'Installed mock packages'"},
        )

        marker = new_foreground_marker()
        open_foreground_deadline(fake, 60.0, marker)
        try:
            scope_before = _active_foreground_scope()
            assert scope_before is not None

            deps = _install_repo_deps(fake, "owner/repo", str(tmp_path), "[dev]")
            assert deps.failed is False

            # Outer scope must still be active (not closed by _install_repo_deps finally)
            scope_after = _active_foreground_scope()
            assert scope_after is not None
            assert scope_after is scope_before
        finally:
            close_foreground_deadline()
        assert _active_foreground_scope() is None


class TestDepsFailureGuardsAndInitResponse:
    """Guards against broken environments and init response error indication."""

    def test_partially_created_venv_blocked_by_uv_environment_block_reason(
        self, tmp_path: Path,
    ) -> None:
        """Spec item 7: A partially created .venv is blocked by uv_environment_block_reason."""
        cid = "depsblk00001"
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
        (tmp_path / "uv.lock").write_text("", encoding="utf-8")
        (tmp_path / ".venv").mkdir()

        # Record environment state as failed from deps_timeout
        record_environment_state(
            cid,
            "failed",
            reason="deps_timeout: uv sync timed out after 3.0s (reap: ok)",
            command="uv sync --locked --all-groups",
        )

        fake = _DepsContainer(cid=cid)
        blocked = uv_environment_block_reason(fake, str(tmp_path))
        assert blocked is not None
        assert "uv environment failed" in blocked
        assert "deps_timeout" in blocked

    def test_init_response_not_successful_on_deps_timeout(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Spec item 8: The init response deps line shows failure and error flag is set."""

        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
        cid = "depsinit0001"
        mock_container = MagicMock()
        mock_container.id = cid
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container

        failed_deps = DepsResult("deps: uv sync timed out after 3.0s (reap: ok)", True)

        with (
            patch("sunaba.tools.container._docker", return_value=mock_client),
            patch("sunaba.tools.container.lifecycle._ensure_image"),
            patch("sunaba.tools.container.lifecycle.validate_image_ref"),
            patch("sunaba.tools.container.clone._clone_repo_via_network", return_value="Cloned owner/repo"),
            patch("sunaba.tools.container.lifecycle._install_repo_deps", return_value=failed_deps) as mock_install,
        ):
            result = sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                clone_repo="owner/repo",
                allow_network=True,
            )
            mock_install.assert_called_once()
            # Assert error flag on deps result
            assert failed_deps.failed is True
            assert failed_deps.error is True
            # Assert init response shows failure and not success
            assert "deps: uv sync timed out after 3.0s (reap: ok)" in result
            assert "uv sync --locked --all-groups ok" not in result
