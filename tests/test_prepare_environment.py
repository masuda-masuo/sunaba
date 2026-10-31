"""Tests for sandbox_prepare_environment (Issue #969, parent #941)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

from sunaba.edit_verify.shell import ForegroundTreeTimeout
from sunaba.environment import check_uv_environment
from sunaba.environment_state_store import (
    compute_manifest_fingerprint,
)
from sunaba.security import NETWORK_LABEL
from sunaba.tools.container.prepare_environment import (
    sandbox_prepare_environment,
)
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import (
    get_environment_state,
    has_verify_success,
    record_environment_state,
    record_verify_success,
    verify_guard_acquire,
    verify_guard_release,
)
from tests.conftest import _make_client_mock


class MockPrepareContainer:
    """Mock Docker container simulating filesystem and uv sync execution."""

    def __init__(
        self,
        cid: str,
        root: str,
        files: dict[str, bytes],
        *,
        allow_network: bool = True,
        locked_fail: bool = False,
        timeout_on_sync: bool = False,
    ) -> None:
        self.id = cid
        self.root = root.rstrip("/")
        self.files = dict(files)
        self.allow_network = allow_network
        self.labels = {NETWORK_LABEL: "true" if allow_network else "false"}
        self.attrs: dict[str, Any] = {
            "Config": {"WorkingDir": root, "Labels": dict(self.labels)},
            "HostConfig": {"NetworkMode": "bridge" if allow_network else "none"},
        }
        self.locked_fail = locked_fail
        self.timeout_on_sync = timeout_on_sync
        self.executed: list[str] = []

    def exec_run(self, cmd: list[str] | str, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        cmd_str = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        self.executed.append(cmd_str)

        # Recreate .venv removal
        if "rm -rf" in cmd_str and ".venv" in cmd_str:
            return 0, (b"", b"")

        # Manifest probe loop (probe_uv_project_root)
        if "for f in pyproject.toml uv.lock" in cmd_str:
            lines = []
            for fname in ("pyproject.toml", "uv.lock"):
                full_path = f"{self.root}/{fname}"
                if full_path in self.files:
                    lines.append(fname.encode("utf-8"))
            out = b"\n".join(lines) + (b"\n" if lines else b"")
            return 0, (out, b"")

        # Single file probe from _read_container_file
        for fname in ("pyproject.toml", "uv.lock", ".python-version"):
            if f"'{fname}'" in cmd_str or f'"{fname}"' in cmd_str or f" {fname}" in cmd_str:
                full_path = f"{self.root}/{fname}"
                if full_path in self.files:
                    return 0, (self.files[full_path], b"")
                return 0, (b"__SUNABA_MANIFEST_ABSENT__\n", b"")

        # uv sync execution
        if "uv sync" in cmd_str:
            if self.timeout_on_sync:
                raise ForegroundTreeTimeout(180.0, "ok", 180.1)
            if self.locked_fail:
                return 1, (b"", b"error: `uv.lock` is out of date and `--locked` was specified\n")
            return 0, (b"Resolved 5 packages in 10ms\nPrepared 5 packages\nInstalled 5 packages\n", b"")

        # Tool detection & verification runners
        if "test -x" in cmd_str:
            return 0, (b"", b"")
        if "--version" in cmd_str:
            return 0, (b"tool 1.0.0\n", b"")
        if "git diff" in cmd_str or "git ls-files" in cmd_str:
            return 0, (b"", b"")
        if "git rev-parse" in cmd_str:
            return 0, (b"main\n", b"")

        return 0, (b"", b"")


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate state directory and in-memory caches."""
    monkeypatch.setenv("SUNABA_ENVIRONMENT_STATE_DIR", str(tmp_path))
    with (
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._inflight", {}),
        patch(
            "sunaba.tools.vcs.resolve_git_root",
            side_effect=lambda c, wd=None: wd if wd is not None else (getattr(c, "root", None) or "/workspace"),
        ),
    ):
        yield tmp_path


@contextlib.contextmanager
def _mock_docker_context(container: Any):
    mock_client = _make_client_mock(container)
    with (
        patch("sunaba.tools.common._docker", return_value=mock_client),
        patch("sunaba.tools.verify._docker", return_value=mock_client),
        patch("sunaba.tools.container.prepare_environment._docker", return_value=mock_client),
    ):
        yield mock_client


def _create_sample_files(root: str = "/workspace") -> dict[str, bytes]:
    return {
        f"{root}/pyproject.toml": b"[project]\nname = 'sample'\nversion = '0.1.0'\n",
        f"{root}/uv.lock": b"version = 1\nrevision = 1\n",
    }


class TestPrepareEnvironment:
    """Acceptance criteria test suite for sandbox_prepare_environment."""

    def test_stale_to_ready_and_verify_passes(
        self, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 1: On a stale container, running the tool returns the environment to ready, and verify passes."""
        cid = "c_prepare_stale_01"
        files = _create_sample_files()
        container = MockPrepareContainer(cid, "/workspace", files)

        with _mock_docker_context(container):
            # Initially prepare container and record ready
            fp = compute_manifest_fingerprint(container, "/workspace")
            record_environment_state(
                cid,
                "ready",
                command="uv sync --locked --all-groups",
                fingerprint=fp,
                project_root="/workspace",
            )
            status, reason = check_uv_environment(container, "/workspace")
            assert status == "ready"
            assert reason is None

            # Now modify uv.lock to make the container stale
            container.files["/workspace/uv.lock"] = b"version = 1\nrevision = 2 # modified\n"
            status, reason = check_uv_environment(container, "/workspace")
            assert status == "stale"
            assert "changed" in str(reason)
            assert "sandbox_prepare_environment" in str(reason)

            # verify_in_container fails with environment_setup_failed
            v_res = json.loads(verify_in_container(cid, path="/workspace"))
            assert v_res["status"] == "environment_setup_failed"
            assert v_res["gate_passed"] is False

            # Run sandbox_prepare_environment to recover
            prep_out = sandbox_prepare_environment(cid)
            res = json.loads(prep_out)
            assert res["status"] == "ok"
            assert res["environment"] == "ready"
            assert res["project_root"] == "/workspace"

            # Environment state is now ready with fresh fingerprint
            state = get_environment_state(cid)
            assert state is not None
            assert state["status"] == "ready"

            # check_uv_environment is ready and verify passes
            status, reason = check_uv_environment(container, "/workspace")
            assert status == "ready"
            assert reason is None

            v_res2 = json.loads(verify_in_container(cid, path="/workspace"))
            assert v_res2["status"] != "environment_setup_failed"

    def test_network_disabled_fails_with_network_disabled(
        self, isolated_env: Path
    ) -> None:
        """AC 2: With network disabled, the tool fails with network_disabled and the recorded state is failed."""
        cid = "c_prepare_nonet_02"
        files = _create_sample_files()
        container = MockPrepareContainer(cid, "/workspace", files, allow_network=False)

        with _mock_docker_context(container):
            prep_out = sandbox_prepare_environment(cid)
            res = json.loads(prep_out)
            assert res["status"] == "error"
            assert "network_disabled" in res["error"]
            assert res["reason"] == "network_disabled"

            # State recorded as failed
            state = get_environment_state(cid)
            assert state is not None
            assert state["status"] == "failed"
            assert state["reason"] == "network_disabled"

            # uv sync was never executed
            assert not any("uv sync" in cmd for cmd in container.executed)

    def test_locked_failure_records_failed_and_preserves_lockfile(
        self, isolated_env: Path
    ) -> None:
        """AC 3: When --locked fails, the state is failed and uv.lock is byte-identical before and after."""
        cid = "c_prepare_locked_03"
        files = _create_sample_files()
        original_lock_bytes = bytes(files["/workspace/uv.lock"])
        container = MockPrepareContainer(cid, "/workspace", files, locked_fail=True)

        with _mock_docker_context(container):
            prep_out = sandbox_prepare_environment(cid)
            res = json.loads(prep_out)
            assert res["status"] == "error"
            assert res["exit_code"] == 1

            # Recorded state is failed
            state = get_environment_state(cid)
            assert state is not None
            assert state["status"] == "failed"
            assert "sync_failed" in str(state["reason"])

            # uv.lock is byte-identical
            assert container.files["/workspace/uv.lock"] == original_lock_bytes

    def test_recreate_venv_deletes_venv_before_sync(
        self, isolated_env: Path
    ) -> None:
        """AC 4: With recreate=True, .venv is deleted before preparing; with recreate=False it is not."""
        cid = "c_prepare_recreate_04"
        files = _create_sample_files()

        # Without recreate: no rm -rf .venv
        container_no_recreate = MockPrepareContainer(cid, "/workspace", files)
        with _mock_docker_context(container_no_recreate):
            prep_out = sandbox_prepare_environment(cid, recreate=False)
            res = json.loads(prep_out)
            assert res["status"] == "ok"
            assert not any("rm -rf" in cmd and ".venv" in cmd for cmd in container_no_recreate.executed)

        # With recreate=True: rm -rf .../.venv is executed before uv sync
        cid2 = "c_prepare_recreate_05"
        container_recreate = MockPrepareContainer(cid2, "/workspace", files)
        with _mock_docker_context(container_recreate):
            prep_out = sandbox_prepare_environment(cid2, recreate=True)
            res = json.loads(prep_out)
            assert res["status"] == "ok"
            rm_cmds = [cmd for cmd in container_recreate.executed if "rm -rf" in cmd and ".venv" in cmd]
            assert len(rm_cmds) == 1
            sync_cmds = [cmd for cmd in container_recreate.executed if "uv sync" in cmd]
            assert len(sync_cmds) == 1
            # rm -rf occurred before uv sync
            rm_idx = container_recreate.executed.index(rm_cmds[0])
            sync_idx = container_recreate.executed.index(sync_cmds[0])
            assert rm_idx < sync_idx

    def test_concurrency_rejects_prepare_while_verify_running(
        self, isolated_env: Path
    ) -> None:
        """AC 5a: While verify is running, the tool is rejected."""
        cid = "c_prepare_conc_06"
        files = _create_sample_files()
        container = MockPrepareContainer(cid, "/workspace", files)

        # Acquire guard as verify does
        assert verify_guard_acquire(cid) is True

        with _mock_docker_context(container):
            prep_out = sandbox_prepare_environment(cid)
            res = json.loads(prep_out)
            assert res["status"] == "in_progress"
            assert res["rejected"] is True

            # Release guard and confirm tool can now proceed
            verify_guard_release(cid)
            prep_out2 = sandbox_prepare_environment(cid)
            res2 = json.loads(prep_out2)
            assert res2["status"] == "ok"

    def test_concurrency_rejects_verify_while_prepare_running(
        self, isolated_env: Path
    ) -> None:
        """AC 5b: While preparation is running, verify is rejected."""
        cid = "c_prepare_conc_07"
        files = _create_sample_files()
        container = MockPrepareContainer(cid, "/workspace", files)

        # Simulate preparation holding the guard
        assert verify_guard_acquire(cid) is True

        with _mock_docker_context(container):
            v_res = json.loads(verify_in_container(cid, path="/workspace"))
            assert v_res["status"] == "in_progress"
            assert v_res["gate_passed"] is False

            # Release guard
            verify_guard_release(cid)

    def test_deadline_overrun_reaps_process_and_records_failed(
        self, isolated_env: Path
    ) -> None:
        """AC 6: On deadline overrun, the process tree is reaped and the state becomes failed."""
        cid = "c_prepare_timeout_08"
        files = _create_sample_files()
        container = MockPrepareContainer(cid, "/workspace", files, timeout_on_sync=True)

        record_verify_success(cid)
        assert has_verify_success(cid) is True

        with _mock_docker_context(container):
            prep_out = sandbox_prepare_environment(cid)
            res = json.loads(prep_out)
            assert res["status"] == "error"
            assert "timeout" in str(res["error"])
            assert "deps_timeout" in str(res["reason"])

            # State recorded as failed
            state = get_environment_state(cid)
            assert state is not None
            assert state["status"] == "failed"
            assert "deps_timeout" in str(state["reason"])

            # Verify success was invalidated
            assert has_verify_success(cid) is False

    def test_invalidates_verify_success_at_start(
        self, isolated_env: Path
    ) -> None:
        """Spec 1: At start, invalidate any recorded verify success."""
        cid = "c_prepare_inval_09"
        files = _create_sample_files()
        container = MockPrepareContainer(cid, "/workspace", files)

        record_verify_success(cid)
        assert has_verify_success(cid) is True

        with _mock_docker_context(container):
            sandbox_prepare_environment(cid)
            # Was invalidated at start
            assert has_verify_success(cid) is False

    def test_container_not_found(self, isolated_env: Path) -> None:
        """NotFound returns clean error JSON."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("container missing")

        with (
            patch("sunaba.tools.common._docker", return_value=mock_client),
            patch("sunaba.tools.container.prepare_environment._docker", return_value=mock_client),
        ):
            res = json.loads(sandbox_prepare_environment("missing_cid"))
            assert res["status"] == "error"
            assert "not found" in res["error"].lower()
