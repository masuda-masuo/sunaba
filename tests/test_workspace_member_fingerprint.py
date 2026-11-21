"""Tests for uv workspace member manifest fingerprinting (#988)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sunaba.environment import _resolve_image_tool, check_uv_environment
from sunaba.environment_state_store import (
    FingerprintProbeError,
    compute_manifest_fingerprint,
)
from sunaba.tools.container.clone import prepare_uv_environment
from sunaba.tools.container.prepare_environment import sandbox_prepare_environment
from sunaba.tools.vcs.publishing import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import (
    get_environment_state_for_root,
    has_verify_success,
    record_environment_state,
    record_verify_success,
)


class LocalWorkspaceContainer:
    """Real-exec container adapter executing commands against a temporary workspace."""

    def __init__(
        self,
        root: Path,
        *,
        cid: str = "workspace-fp-container-id-1234",
        allow_network: bool = True,
        hide_image_lint_tools: bool = True,
        path_override: str | None = None,
    ) -> None:
        self.id = cid
        self.root = str(root)
        self.allow_network = allow_network
        self.hide_image_lint_tools = hide_image_lint_tools
        self.path_override = path_override
        self.labels = {"sunaba.allow_network": "true" if allow_network else "false"}
        self.attrs = {
            "Config": {"WorkingDir": str(root)},
            "HostConfig": {"NetworkMode": "bridge" if allow_network else "none"},
        }
        self.exec_calls: list[str] = []

    def exec_run(
        self,
        cmd: list[str] | str,
        stdout: bool = True,
        stderr: bool = True,
        demux: bool = True,
        environment: dict[str, str] | None = None,
        workdir: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, tuple[bytes, bytes] | bytes]:
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else ["/bin/sh", "-c", str(cmd)]
        if self.hide_image_lint_tools and argv and isinstance(argv[-1], str):
            # Reproduce CI tool availability where /home/sandbox/.venv/bin (ruff/pyright)
            # is absent, without altering the global container environment.
            argv[-1] = argv[-1].replace("PATH=/home/sandbox/.venv/bin:", "PATH=")
        rendered = argv[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        self.exec_calls.append(rendered)
        env = dict(os.environ)
        if self.path_override is not None:
            env["PATH"] = self.path_override
        if environment:
            env.update(environment)
        res = subprocess.run(
            argv,
            cwd=workdir or self.root,
            capture_output=True,
            check=False,
            env=env,
        )
        if demux:
            return res.returncode, (res.stdout, res.stderr)
        return res.returncode, res.stdout + res.stderr


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "host-state"
    state_dir.mkdir()
    monkeypatch.setenv("SUNABA_ENVIRONMENT_STATE_DIR", str(state_dir))
    monkeypatch.setattr("sunaba.verify_state._environment_state", {})
    monkeypatch.setattr("sunaba.verify_state._verify_map", {})
    return state_dir


def _docker_client_for(container: Any) -> Any:
    class Containers:
        def get(self, container_id: str) -> Any:
            return container

    return SimpleNamespace(containers=Containers())


def _write_member(
    ws_dir: Path,
    rel_dir: str,
    pkg_name: str,
    *,
    dependencies: list[str] | None = None,
    version: str = "0.1.0",
    with_test: bool = False,
) -> Path:
    member_dir = ws_dir / rel_dir
    mod_name = pkg_name.replace("-", "_")
    src_dir = member_dir / "src" / mod_name
    src_dir.mkdir(parents=True, exist_ok=True)
    deps_lit = json.dumps(dependencies or [])
    (member_dir / "pyproject.toml").write_text(
        f"""[project]
name = "{pkg_name}"
version = "{version}"
dependencies = {deps_lit}

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
""",
        encoding="utf-8",
    )
    (src_dir / "__init__.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    if with_test:
        tests_dir = member_dir / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / f"test_{mod_name}.py").write_text(
            f"""import {mod_name}


def test_version() -> None:
    assert {mod_name}.VERSION == "{version}"
""",
            encoding="utf-8",
        )
    return member_dir


def _make_workspace(
    tmp_path: Path,
    *,
    name: str = "ws",
    with_excluded: bool = True,
    with_python_version: bool = False,
) -> tuple[Path, Path, Path, Path | None]:
    ws_dir = tmp_path / name
    ws_dir.mkdir()
    exclude_line = 'exclude = ["packages/excluded"]\n' if with_excluded else ""
    (ws_dir / "pyproject.toml").write_text(
        f"""[project]
name = "workspace-root"
version = "0.1.0"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]
{exclude_line}
[dependency-groups]
dev = ["pytest>=7.0.0"]
""",
        encoding="utf-8",
    )
    if with_python_version:
        (ws_dir / ".python-version").write_text("3.12\n", encoding="utf-8")

    pkg_a = _write_member(ws_dir, "packages/a", "pkg-a", with_test=True)
    pkg_b = _write_member(ws_dir, "packages/b", "pkg-b", with_test=False)
    pkg_excluded = (
        _write_member(ws_dir, "packages/excluded", "pkg-excluded", with_test=False)
        if with_excluded
        else None
    )
    subprocess.run(["uv", "lock"], cwd=ws_dir, check=True, capture_output=True)
    return ws_dir, pkg_a, pkg_b, pkg_excluded


def _prepare_and_verify_context(
    monkeypatch: pytest.MonkeyPatch, container: Any
) -> None:
    client = _docker_client_for(container)
    monkeypatch.setattr("sunaba.tools.container.prepare_environment.common._docker", lambda: client)
    monkeypatch.setattr("sunaba.tools.verify._docker", lambda: client)
    monkeypatch.setattr("sunaba.tools.vcs.publishing._docker", lambda: client)


def _recorded_root_state(container_id: str, root: Path) -> dict[str, Any]:
    state = get_environment_state_for_root(container_id, str(root))
    assert state is not None
    return state


def test_workspace_fingerprint_hashes_sorted_member_manifests_only(
    tmp_path: Path, isolated_env: Path
) -> None:
    ws_dir, pkg_a, _, pkg_excluded = _make_workspace(tmp_path, with_excluded=True)
    pkg_z = _write_member(ws_dir, "packages/z", "pkg-z")
    subprocess.run(["uv", "lock", "--offline"], cwd=ws_dir, check=True, capture_output=True)
    lock_before = (ws_dir / "uv.lock").read_bytes()

    container = LocalWorkspaceContainer(ws_dir)
    fp = compute_manifest_fingerprint(container, str(ws_dir))

    assert (ws_dir / "uv.lock").read_bytes() == lock_before
    assert "members" in fp
    members = fp["members"]
    assert isinstance(members, dict)
    assert list(members.keys()) == [
        "packages/a/pyproject.toml",
        "packages/b/pyproject.toml",
        "packages/z/pyproject.toml",
    ]
    assert "pyproject.toml" not in members
    assert pkg_excluded is not None
    assert "packages/excluded/pyproject.toml" not in members
    assert members["packages/a/pyproject.toml"] == hashlib.sha256(
        (pkg_a / "pyproject.toml").read_bytes()
    ).hexdigest()
    assert members["packages/z/pyproject.toml"] == hashlib.sha256(
        (pkg_z / "pyproject.toml").read_bytes()
    ).hexdigest()


def test_member_manifest_mutation_blocks_verify_from_other_member_and_publish(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, pkg_b, _ = _make_workspace(tmp_path)
    container = LocalWorkspaceContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)

    prep = json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))
    assert prep["status"] == "ok", prep

    verify_ok = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_ok["gate_passed"] is True, verify_ok
    assert has_verify_success(container.id) is True

    pub_ok = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_ok.get("step") != "verify_gate"

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    # Mutate member b without relocking
    original_b_pyproject = (pkg_b / "pyproject.toml").read_text(encoding="utf-8")
    (pkg_b / "pyproject.toml").write_text(
        original_b_pyproject.replace("dependencies = []", 'dependencies = ["six"]'),
        encoding="utf-8",
    )

    assert (ws_dir / "uv.lock").read_bytes() == lock_before

    # Verify from another member (pkg_a) must detect staleness and fail closed
    verify_stale = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_stale["status"] == "environment_setup_failed"
    assert verify_stale["gate_passed"] is False
    assert verify_stale["environment_setup"]["status"] == "stale"
    assert "packages/b/pyproject.toml" in verify_stale["environment_setup"]["reason"]
    assert has_verify_success(container.id) is False
    assert state_file.read_bytes() == state_bytes_before
    assert _recorded_root_state(container.id, ws_dir)["status"] == "ready"

    # Publish must also refuse at verify_gate
    pub_blocked = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_blocked["status"] == "error"
    assert pub_blocked["step"] == "verify_gate"
    assert "stale" in pub_blocked["error"]
    assert "packages/b/pyproject.toml" in pub_blocked["error"]
    assert state_file.read_bytes() == state_bytes_before
    assert (ws_dir / "uv.lock").read_bytes() == lock_before

    # Reverting member b restores ready without re-preparing
    (pkg_b / "pyproject.toml").write_text(original_b_pyproject, encoding="utf-8")
    verify_recovered = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_recovered["gate_passed"] is True
    assert has_verify_success(container.id) is True

    # Full edit + lock + reprepare refreshes fingerprint
    (pkg_b / "pyproject.toml").write_text(
        original_b_pyproject.replace('version = "0.1.0"', 'version = "0.2.0"'),
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=ws_dir, check=True, capture_output=True)
    reprep = json.loads(sandbox_prepare_environment(container.id, working_dir=str(pkg_a)))
    assert reprep["status"] == "ok", reprep
    assert reprep["project_root"] == str(ws_dir)

    refreshed_fp = _recorded_root_state(container.id, ws_dir)["fingerprint"]
    assert refreshed_fp["members"]["packages/b/pyproject.toml"] == hashlib.sha256(
        (pkg_b / "pyproject.toml").read_bytes()
    ).hexdigest()

    verify_after_reprep = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_after_reprep["gate_passed"] is True
    pub_after_reprep = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_after_reprep.get("step") != "verify_gate"


def test_adding_unlocked_and_locked_member_stops_verify_and_publish(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path)
    container = LocalWorkspaceContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)

    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(container.id)
    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    # Add new unlocked member matching members = ["packages/*"]
    _write_member(ws_dir, "packages/c", "pkg-c")

    verify_unlocked = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_unlocked["status"] == "environment_setup_failed"
    assert verify_unlocked["gate_passed"] is False
    assert verify_unlocked["environment_setup"]["status"] in ("failed", "stale")
    assert has_verify_success(container.id) is False
    assert (ws_dir / "uv.lock").read_bytes() == lock_before
    assert state_file.read_bytes() == state_bytes_before

    pub_unlocked = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_unlocked["status"] == "error"
    assert pub_unlocked["step"] == "verify_gate"
    assert (ws_dir / "uv.lock").read_bytes() == lock_before
    assert state_file.read_bytes() == state_bytes_before

    # Now lock the new member without running sandbox_prepare_environment
    subprocess.run(["uv", "lock", "--offline"], cwd=ws_dir, check=True, capture_output=True)
    record_verify_success(container.id)

    verify_locked = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_locked["status"] == "environment_setup_failed"
    assert verify_locked["environment_setup"]["status"] == "stale"
    assert "packages/c/pyproject.toml" in verify_locked["environment_setup"]["reason"]
    assert has_verify_success(container.id) is False

    pub_locked = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_locked["status"] == "error"
    assert pub_locked["step"] == "verify_gate"
    assert "packages/c/pyproject.toml" in pub_locked["error"]

    # Reprepare picks up the new member
    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    assert "packages/c/pyproject.toml" in (
        _recorded_root_state(container.id, ws_dir)["fingerprint"]["members"]
    )
    assert (
        json.loads(
            verify_in_container(
                container.id,
                path="tests",
                working_dir=str(pkg_a),
                skip_lint_gate=True,
                skip_type_gate=True,
            )
        )["gate_passed"]
        is True
    )


def test_removing_member_unlocked_and_locked_stops_verify_and_publish(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, pkg_b, _ = _make_workspace(tmp_path)
    container = LocalWorkspaceContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)

    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(container.id)
    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    # Delete member b without relocking
    shutil.rmtree(pkg_b)

    verify_unlocked = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_unlocked["status"] == "environment_setup_failed"
    assert verify_unlocked["environment_setup"]["status"] == "stale"
    assert "packages/b/pyproject.toml" in verify_unlocked["environment_setup"]["reason"]
    assert has_verify_success(container.id) is False
    assert (ws_dir / "uv.lock").read_bytes() == lock_before
    assert state_file.read_bytes() == state_bytes_before

    pub_unlocked = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_unlocked["status"] == "error"
    assert pub_unlocked["step"] == "verify_gate"

    # Now relock without reprepare
    subprocess.run(["uv", "lock", "--offline"], cwd=ws_dir, check=True, capture_output=True)
    verify_locked = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_locked["status"] == "environment_setup_failed"
    assert verify_locked["environment_setup"]["status"] == "stale"
    assert "packages/b/pyproject.toml" in verify_locked["environment_setup"]["reason"]

    # Reprepare updates member set and unblocks
    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    members = _recorded_root_state(container.id, ws_dir)["fingerprint"]["members"]
    assert "packages/b/pyproject.toml" not in members
    assert (
        json.loads(
            verify_in_container(
                container.id,
                path="tests",
                working_dir=str(pkg_a),
                skip_lint_gate=True,
                skip_type_gate=True,
            )
        )["gate_passed"]
        is True
    )


def test_excluded_member_and_member_python_version_are_ignored(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, pkg_excluded = _make_workspace(tmp_path, with_excluded=True)
    assert pkg_excluded is not None
    container = LocalWorkspaceContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)

    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    lock_before = (ws_dir / "uv.lock").read_bytes()

    # Mutate excluded package, add another excluded nested manifest, and add member .python-version
    (pkg_excluded / "pyproject.toml").write_text(
        '[project]\nname = "pkg-excluded"\nversion = "9.9.9"\ndependencies = ["nonexistent-pkg"]\n',
        encoding="utf-8",
    )
    excluded_sub = ws_dir / "packages" / "excluded" / "sub"
    excluded_sub.mkdir(parents=True)
    (excluded_sub / "pyproject.toml").write_text(
        '[project]\nname = "sub"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (pkg_a / ".python-version").write_text("3.11\n", encoding="utf-8")

    assert (ws_dir / "uv.lock").read_bytes() == lock_before
    assert check_uv_environment(container, str(pkg_a)) == ("ready", None)

    verify_res = json.loads(
        verify_in_container(
            container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_res["gate_passed"] is True
    pub_res = json.loads(
        publish(container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_res.get("step") != "verify_gate"


@pytest.mark.parametrize(
    "bad_stdout",
    [
        b"not json at all\n",
        json.dumps({"workspace_root": "/ws", "workspace": {"path": "/ws"}, "members": []}).encode(),
        json.dumps(
            {
                "schema": {"version": "preview"},
                "workspace_root": "/other/root",
                "workspace": {"path": "/other/root"},
                "members": [],
            }
        ).encode(),
        json.dumps(
            {
                "schema": {"version": "preview"},
                "workspace_root": "__WS__",
                "workspace": {"path": "__WS__"},
                "members": [{"name": "evil", "path": "/etc/evil", "id": "evil"}],
            }
        ).encode(),
        json.dumps(
            {
                "schema": {"version": "preview"},
                "workspace_root": "__WS__",
                "workspace": {"path": "__WS__"},
                "members": [{"name": "evil", "path": "__WS__/../../etc", "id": "evil"}],
            }
        ).encode(),
    ],
)
def test_metadata_validation_failures_do_not_read_members_or_rewrite_state(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch, bad_stdout: bytes
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path)
    base = LocalWorkspaceContainer(ws_dir)
    fp = compute_manifest_fingerprint(base, str(ws_dir))
    record_environment_state(
        base.id,
        "ready",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=0,
        fingerprint=fp,
        project_root=str(ws_dir),
    )
    record_verify_success(base.id)
    state_file = isolated_env / f"{base.id}.json"
    state_bytes_before = state_file.read_bytes()
    payload = bad_stdout.replace(b"__WS__", str(ws_dir).encode("utf-8"))

    class BadMetadataContainer(LocalWorkspaceContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
            if "uv workspace metadata" in rendered:
                self.exec_calls.append(rendered)
                return 0, (payload, b"")
            return super().exec_run(cmd, *args, **kwargs)

    bad_container = BadMetadataContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, bad_container)

    with pytest.raises(FingerprintProbeError):
        compute_manifest_fingerprint(bad_container, str(ws_dir))

    # Must validate before reading any member pyproject.toml
    assert not any("packages/a/pyproject.toml" in call for call in bad_container.exec_calls)

    verify_res = json.loads(
        verify_in_container(bad_container.id, path="tests", working_dir=str(pkg_a))
    )
    assert verify_res["status"] == "environment_setup_failed"
    assert verify_res["environment_setup"]["status"] == "failed"
    assert has_verify_success(bad_container.id) is False
    assert state_file.read_bytes() == state_bytes_before
    assert _recorded_root_state(bad_container.id, ws_dir)["status"] == "ready"

    pub_res = json.loads(
        publish(bad_container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_res["status"] == "error"
    assert pub_res["step"] == "verify_gate"
    assert state_file.read_bytes() == state_bytes_before


def test_metadata_timeout_fails_closed_and_preserves_ready_or_failed_record(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path)
    base = LocalWorkspaceContainer(ws_dir)
    fp = compute_manifest_fingerprint(base, str(ws_dir))
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "0.15")

    class HangingMetadataContainer(LocalWorkspaceContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
            if "uv workspace metadata" in rendered:
                time.sleep(0.5)
                return 0, (b"{}", b"")
            return super().exec_run(cmd, *args, **kwargs)

    container = HangingMetadataContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)

    # Ready record stays ready when metadata times out
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=0,
        fingerprint=fp,
        project_root=str(ws_dir),
    )
    record_verify_success(container.id)
    state_file = isolated_env / f"{container.id}.json"
    ready_bytes_before = state_file.read_bytes()

    with pytest.raises(FingerprintProbeError, match="timed out"):
        compute_manifest_fingerprint(container, str(ws_dir))

    verify_res = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
    )
    assert verify_res["status"] == "environment_setup_failed"
    assert verify_res["environment_setup"]["status"] == "failed"
    assert has_verify_success(container.id) is False
    assert state_file.read_bytes() == ready_bytes_before

    # Failed record also stays intact
    record_environment_state(
        container.id,
        "failed",
        reason="earlier sync failure",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=1,
        project_root=str(ws_dir),
    )
    failed_bytes_before = state_file.read_bytes()

    status, reason = check_uv_environment(container, str(pkg_a))
    assert status == "failed"
    assert reason == "earlier sync failure"
    assert state_file.read_bytes() == failed_bytes_before


def test_legacy_workspace_record_needs_one_time_reprepare(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="legacy-ws")
    ws_container = LocalWorkspaceContainer(ws_dir, cid="legacy-ws-cid")
    legacy_ws_fp = {
        "root": str(ws_dir),
        "files": {
            "pyproject.toml": hashlib.sha256((ws_dir / "pyproject.toml").read_bytes()).hexdigest(),
            "uv.lock": hashlib.sha256((ws_dir / "uv.lock").read_bytes()).hexdigest(),
            ".python-version": "absent",
        },
    }
    record_environment_state(
        ws_container.id,
        "ready",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=0,
        fingerprint=legacy_ws_fp,
        project_root=str(ws_dir),
    )
    state_file = isolated_env / f"{ws_container.id}.json"
    state_before = state_file.read_bytes()

    _prepare_and_verify_context(monkeypatch, ws_container)

    status, reason = check_uv_environment(ws_container, str(pkg_a))
    assert status == "stale"
    assert reason is not None
    assert state_file.read_bytes() == state_before

    verify_legacy = json.loads(
        verify_in_container(
            ws_container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_legacy["status"] == "environment_setup_failed"
    assert verify_legacy["environment_setup"]["status"] == "stale"

    pub_legacy = json.loads(
        publish(ws_container.id, "owner/repo", "feat/ws", "msg", skip_verify_gate=False)
    )
    assert pub_legacy["status"] == "error"
    assert pub_legacy["step"] == "verify_gate"

    # One-time reprepare refreshes fingerprint and restores ready
    assert json.loads(
        sandbox_prepare_environment(ws_container.id, working_dir=str(ws_dir))
    )["status"] == "ok"
    assert "members" in _recorded_root_state(ws_container.id, ws_dir)["fingerprint"]
    assert check_uv_environment(ws_container, str(pkg_a)) == ("ready", None)
    assert (
        json.loads(
            verify_in_container(
                ws_container.id,
                path="tests",
                working_dir=str(pkg_a),
                skip_lint_gate=True,
                skip_type_gate=True,
            )
        )["gate_passed"]
        is True
    )


def test_ordinary_legacy_record_stays_ready_without_uv_metadata(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ord_dir = tmp_path / "ordinary"
    ord_dir.mkdir()
    (ord_dir / "pyproject.toml").write_text(
        """[project]
name = "ordinary"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["pytest>=7.0.0"]
""",
        encoding="utf-8",
    )
    ord_tests = ord_dir / "tests"
    ord_tests.mkdir()
    (ord_tests / "test_ord.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    subprocess.run(["uv", "lock"], cwd=ord_dir, check=True, capture_output=True)

    ord_container = LocalWorkspaceContainer(ord_dir, cid="ordinary-cid")
    ord_legacy_fp = {
        "root": str(ord_dir),
        "files": {
            "pyproject.toml": hashlib.sha256((ord_dir / "pyproject.toml").read_bytes()).hexdigest(),
            "uv.lock": hashlib.sha256((ord_dir / "uv.lock").read_bytes()).hexdigest(),
            ".python-version": "absent",
        },
    }
    record_environment_state(
        ord_container.id,
        "ready",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=0,
        fingerprint=ord_legacy_fp,
        project_root=str(ord_dir),
    )
    subprocess.run(
        ["uv", "sync", "--locked", "--all-groups", "--all-packages"],
        cwd=ord_dir,
        check=True,
        capture_output=True,
    )
    ord_container.exec_calls.clear()

    _prepare_and_verify_context(monkeypatch, ord_container)
    assert check_uv_environment(ord_container, str(ord_dir)) == ("ready", None)
    verify_ord = json.loads(
        verify_in_container(
            ord_container.id,
            path="tests",
            working_dir=str(ord_dir),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_ord["gate_passed"] is True
    assert not any("uv workspace metadata" in call for call in ord_container.exec_calls)

    # Fresh compute_manifest_fingerprint on ordinary project also avoids uv metadata
    ord_container.exec_calls.clear()
    ord_fp = compute_manifest_fingerprint(ord_container, str(ord_dir))
    assert "members" not in ord_fp
    assert not any("uv workspace metadata" in call for call in ord_container.exec_calls)


def test_empty_virtual_workspace_computes_empty_members_map(
    tmp_path: Path, isolated_env: Path
) -> None:
    vws = tmp_path / "virtual-ws"
    vws.mkdir()
    (vws / "pyproject.toml").write_text(
        "[tool.uv.workspace]\nmembers = []\n", encoding="utf-8"
    )
    subprocess.run(["uv", "lock"], cwd=vws, check=True, capture_output=True)
    container = LocalWorkspaceContainer(vws, cid="virtual-ws-cid")

    fp = compute_manifest_fingerprint(container, str(vws))
    assert fp["members"] == {}


def test_ci_fixture_hides_image_ruff_and_pyright(
    tmp_path: Path, isolated_env: Path
) -> None:
    container = LocalWorkspaceContainer(tmp_path)
    assert _resolve_image_tool(container, str(tmp_path), "ruff") == (None, None)
    assert _resolve_image_tool(container, str(tmp_path), "pyright") == (None, None)


def _token_processes(token: str) -> list[int]:
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
        except OSError:
            continue
        if token.encode("utf-8") in cmdline:
            found.append(int(entry.name))
    return found


def _kill_token_processes(token: str) -> None:
    for pid in _token_processes(token):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _wait_token_processes_dead(token: str, timeout_s: float = 3.0) -> list[int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        alive = _token_processes(token)
        if not alive:
            return []
        time.sleep(0.05)
    return _token_processes(token)


def _build_no_timeout_blocking_uv_bin(
    bin_dir: Path,
    ws_dir: Path,
    *,
    token: str,
    started_file: Path,
    ready_file: Path,
    pids_file: Path,
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("sh", "tr", "cat", "sleep", "setsid", "env", "find", "git", "echo"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    orig_path = os.environ.get("PATH", "")
    uv_script = bin_dir / "uv"
    uv_script.write_text(
        f"""#!/bin/sh
if [ "$1" = "workspace" ] && [ "$2" = "dir" ]; then
    printf '%s\\n' '{ws_dir}'
    exit 0
fi
if [ "$1" = "workspace" ] && [ "$2" = "metadata" ]; then
    printf 'started\\n' >> '{started_file}'
    trap '' TERM
    setsid sh -c 'trap "" TERM; while :; do sleep 60; done # {token}' >/dev/null 2>&1 &
    child=$!
    while [ ! -d "/proc/$child" ]; do sleep 0.01; done
    printf '%s\\n%s\\n' "$$" "$child" > '{pids_file}'
    printf 'ready\\n' > '{ready_file}'
    while :; do sleep 60; done # {token}
fi
PATH='{orig_path}' exec '{real_uv}' "$@"
""",
        encoding="utf-8",
    )
    uv_script.chmod(0o755)
    assert shutil.which("timeout", path=str(bin_dir)) is None


def test_missing_timeout_utility_reaps_genuinely_blocked_metadata_child(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="no-timeout-reap")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="no-timeout-reap-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabanoTO-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "no-timeout-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )

    container = LocalWorkspaceContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
    )
    _prepare_and_verify_context(monkeypatch, container)
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "0.25")
    monkeypatch.setenv("SUNABA_VERIFY_TIMEOUT", "30.0")

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    try:
        box: dict[str, Any] = {}

        def _run_verify() -> None:
            try:
                box["value"] = verify_in_container(
                    container.id,
                    path="tests",
                    working_dir=str(pkg_a),
                    skip_lint_gate=True,
                    skip_type_gate=True,
                )
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_verify, daemon=True)
        t.start()
        t.join(10.0)
        assert not t.is_alive(), "verify_in_container hung when timeout utility was absent"
        if "error" in box:
            raise box["error"]

        result = json.loads(box["value"])
        assert result["status"] == "environment_setup_failed"
        assert result["gate_passed"] is False
        assert result["environment_setup"]["status"] == "failed"
        assert "timed out" in result["environment_setup"]["reason"]
        assert has_verify_success(container.id) is False
        assert state_file.read_bytes() == state_bytes_before
        assert (ws_dir / "uv.lock").read_bytes() == lock_before
        assert started_file.exists()
        assert ready_file.exists()
        assert _wait_token_processes_dead(token) == []

        # Follow-up exec still works cleanly
        ec, out = container.exec_run(["echo", "ok"])
        assert ec == 0
        assert isinstance(out, tuple) and b"ok" in out[0]
    finally:
        _kill_token_processes(token)


def test_missing_timeout_and_unbounded_exec_fails_before_starting_uv(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="no-timeout-failfast")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="no-timeout-failfast-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabanoStart-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "no-timeout-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )

    class NoEnvContainer:
        """Container whose exec_run cannot accept environment markers."""

        def __init__(self) -> None:
            self.id = prep_container.id
            self.root = str(ws_dir)

        def exec_run(
            self,
            cmd: list[str] | str,
            stdout: bool = True,
            stderr: bool = True,
            demux: bool = True,
            workdir: str | None = None,
        ) -> tuple[int, tuple[bytes, bytes] | bytes]:
            argv = list(cmd) if isinstance(cmd, (list, tuple)) else ["/bin/sh", "-c", str(cmd)]
            env = dict(os.environ)
            env["PATH"] = str(bin_dir)
            res = subprocess.run(
                argv,
                cwd=workdir or self.root,
                capture_output=True,
                check=False,
                env=env,
            )
            if demux:
                return res.returncode, (res.stdout, res.stderr)
            return res.returncode, res.stdout + res.stderr

    class DroppedEnvContainer(LocalWorkspaceContainer):
        """Container accepting kwargs but dropping env markers (simulating unpropagated env)."""

        def exec_run(
            self,
            cmd: list[str] | str,
            stdout: bool = True,
            stderr: bool = True,
            demux: bool = True,
            environment: dict[str, str] | None = None,
            workdir: str | None = None,
            **kwargs: Any,
        ) -> tuple[int, tuple[bytes, bytes] | bytes]:
            return super().exec_run(
                cmd,
                stdout=stdout,
                stderr=stderr,
                demux=demux,
                environment=None,
                workdir=workdir,
                **kwargs,
            )

    container = NoEnvContainer()
    _prepare_and_verify_context(monkeypatch, container)
    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    try:
        with pytest.raises(FingerprintProbeError):
            compute_manifest_fingerprint(container, str(ws_dir))
        assert not started_file.exists(), "uv must not start when bounded cleanup cannot be guaranteed"

        dropped_env_container = DroppedEnvContainer(
            ws_dir,
            cid=prep_container.id,
            path_override=str(bin_dir),
        )
        _prepare_and_verify_context(monkeypatch, dropped_env_container)
        verify_res = json.loads(
            verify_in_container(
                dropped_env_container.id,
                path="tests",
                working_dir=str(pkg_a),
                skip_lint_gate=True,
                skip_type_gate=True,
            )
        )
        assert verify_res["status"] == "environment_setup_failed"
        assert verify_res["gate_passed"] is False
        assert has_verify_success(dropped_env_container.id) is False
        assert not started_file.exists(), "uv must not start during verify when cleanup cannot be guaranteed"
        assert state_file.read_bytes() == state_bytes_before
        assert (ws_dir / "uv.lock").read_bytes() == lock_before

        # Non-finite / non-positive bound must also fail before starting uv
        real_container = LocalWorkspaceContainer(
            ws_dir,
            cid=prep_container.id,
            path_override=str(bin_dir),
        )
        monkeypatch.setattr("sunaba.environment_state_store._uv_metadata_timeout", lambda: 0.0)
        with pytest.raises(FingerprintProbeError):
            compute_manifest_fingerprint(real_container, str(ws_dir))
        assert not started_file.exists(), "uv must not start with non-positive timeout bound"
    finally:
        _kill_token_processes(token)


def test_outer_verify_deadline_preserved_and_reaps_blocked_metadata(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="outer-verify-deadline")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="outer-verify-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabaOuterV-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "outer-verify-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )

    container = LocalWorkspaceContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
    )
    _prepare_and_verify_context(monkeypatch, container)
    monkeypatch.setenv("SUNABA_VERIFY_TIMEOUT", "0.3")
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "15.0")

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    try:
        box: dict[str, Any] = {}

        def _run_verify() -> None:
            try:
                box["value"] = verify_in_container(
                    container.id,
                    path="tests",
                    working_dir=str(pkg_a),
                    skip_lint_gate=True,
                    skip_type_gate=True,
                )
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_verify, daemon=True)
        t.start()
        t.join(10.0)
        assert not t.is_alive(), "verify_in_container hung under outer verify deadline"
        if "error" in box:
            raise box["error"]

        result = json.loads(box["value"])
        assert result["status"] == "timeout", result
        assert result["gate_passed"] is False
        assert result["timeout"]["deadline_s"] == 0.3
        assert result["timeout"]["reap"] == "ok"
        assert has_verify_success(container.id) is False
        assert state_file.read_bytes() == state_bytes_before
        assert (ws_dir / "uv.lock").read_bytes() == lock_before
        assert started_file.exists()
        assert _wait_token_processes_dead(token) == []
    finally:
        _kill_token_processes(token)


def test_outer_deps_deadline_preserved_when_metadata_blocks(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, _, _, _ = _make_workspace(tmp_path, name="outer-deps-deadline")
    token = f"sunabaOuterD-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "outer-deps-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )
    # Short-circuit `uv sync` in the fake uv binary so the deps deadline
    # expires specifically inside `uv workspace metadata` rather than during sync.
    uv_script = bin_dir / "uv"
    uv_script.write_text(
        uv_script.read_text(encoding="utf-8").replace(
            'if [ "$1" = "workspace" ] && [ "$2" = "dir" ]; then',
            'if [ "$1" = "sync" ]; then\n    exit 0\nfi\nif [ "$1" = "workspace" ] && [ "$2" = "dir" ]; then',
        ),
        encoding="utf-8",
    )

    container = LocalWorkspaceContainer(
        ws_dir,
        cid="outer-deps-cid",
        path_override=str(bin_dir),
    )
    record_verify_success(container.id)
    monkeypatch.setenv("SUNABA_DEPS_TIMEOUT", "0.5")
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "15.0")

    lock_before = (ws_dir / "uv.lock").read_bytes()

    try:
        box: dict[str, Any] = {}

        def _run_prep() -> None:
            try:
                box["value"] = prepare_uv_environment(container, str(ws_dir))
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_prep, daemon=True)
        t.start()
        t.join(15.0)
        assert not t.is_alive(), "prepare_uv_environment hung under outer deps deadline"
        if "error" in box:
            raise box["error"]

        prep_res = box["value"]
        assert prep_res.success is False
        assert prep_res.exit_code == 124
        assert prep_res.reason is not None and "deps_timeout" in prep_res.reason
        assert "reap=ok" in prep_res.message
        assert has_verify_success(container.id) is False
        assert (ws_dir / "uv.lock").read_bytes() == lock_before
        assert started_file.exists()
        assert _wait_token_processes_dead(token) == []
    finally:
        _kill_token_processes(token)


class _DelayedLaunchMetadataContainer(LocalWorkspaceContainer):
    """Synchronized container adapter delaying `uv workspace metadata` exec launch."""

    def __init__(
        self,
        root: Path,
        *,
        cid: str,
        path_override: str,
        launch_entered: threading.Event,
        allow_launch: threading.Event,
        runner_finished: threading.Event,
        drop_env: bool = False,
    ) -> None:
        super().__init__(root, cid=cid, path_override=path_override)
        self.launch_entered = launch_entered
        self.allow_launch = allow_launch
        self.runner_finished = runner_finished
        self.drop_env = drop_env

    def exec_run(
        self,
        cmd: list[str] | str,
        stdout: bool = True,
        stderr: bool = True,
        demux: bool = True,
        environment: dict[str, str] | None = None,
        workdir: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, tuple[bytes, bytes] | bytes]:
        rendered = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        if "uv workspace metadata" in rendered:
            self.launch_entered.set()
            assert self.allow_launch.wait(timeout=15.0), "timed out waiting to release delayed exec"
            try:
                return super().exec_run(
                    cmd,
                    stdout=stdout,
                    stderr=stderr,
                    demux=demux,
                    environment=None if self.drop_env else environment,
                    workdir=workdir,
                    **kwargs,
                )
            finally:
                self.runner_finished.set()
        return super().exec_run(
            cmd,
            stdout=stdout,
            stderr=stderr,
            demux=demux,
            environment=environment,
            workdir=workdir,
            **kwargs,
        )


def _spy_reap_completion(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, list[str]]:
    import sunaba.edit_verify.shell as shell_mod

    reap_done = threading.Event()
    verdicts: list[str] = []
    orig_reap = shell_mod._reap_verify_tree

    def _wrapped(scope: Any) -> str:
        verdict = orig_reap(scope)
        verdicts.append(verdict)
        reap_done.set()
        return verdict

    monkeypatch.setattr(shell_mod, "_reap_verify_tree", _wrapped)
    return reap_done, verdicts


def test_late_metadata_exec_past_local_deadline_and_reap_never_invokes_uv(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="late-local-deadline")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="late-local-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabaLateLocal-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "late-local-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )

    launch_entered = threading.Event()
    allow_launch = threading.Event()
    runner_finished = threading.Event()
    container = _DelayedLaunchMetadataContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
        launch_entered=launch_entered,
        allow_launch=allow_launch,
        runner_finished=runner_finished,
    )
    _prepare_and_verify_context(monkeypatch, container)
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "0.2")
    monkeypatch.setenv("SUNABA_VERIFY_TIMEOUT", "30.0")
    reap_done, verdicts = _spy_reap_completion(monkeypatch)

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    try:
        box: dict[str, Any] = {}

        def _run_verify() -> None:
            try:
                box["value"] = verify_in_container(
                    container.id,
                    path="tests",
                    working_dir=str(pkg_a),
                    skip_lint_gate=True,
                    skip_type_gate=True,
                )
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_verify, daemon=True)
        t.start()

        assert launch_entered.wait(10.0), "metadata exec did not reach launch gate"
        assert reap_done.wait(10.0), "reaper did not complete before releasing exec"
        t.join(10.0)
        assert not t.is_alive(), "verify_in_container hung waiting on delayed metadata exec"
        if "error" in box:
            raise box["error"]

        result = json.loads(box["value"])
        assert result["status"] == "environment_setup_failed", result
        assert result["gate_passed"] is False
        assert result["environment_setup"]["status"] == "failed"
        assert "timed out" in result["environment_setup"]["reason"]
        assert "reap=ok" in result["environment_setup"]["reason"]
        assert verdicts == ["ok"]
        assert has_verify_success(container.id) is False
        assert state_file.read_bytes() == state_bytes_before
        assert (ws_dir / "uv.lock").read_bytes() == lock_before
        assert not started_file.exists()

        allow_launch.set()
        assert runner_finished.wait(2.0), "late metadata shell did not exit promptly"
        assert not started_file.exists(), "late-started metadata shell must not invoke uv"
        assert _wait_token_processes_dead(token) == []

        ec, out = container.exec_run(["echo", "ok"])
        assert ec == 0
        assert isinstance(out, tuple) and b"ok" in out[0]
    finally:
        allow_launch.set()
        _kill_token_processes(token)


def test_late_metadata_exec_past_outer_verify_deadline_and_reap_never_invokes_uv(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="late-outer-verify")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="late-outer-verify-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabaLateOV-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "late-ov-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )

    launch_entered = threading.Event()
    allow_launch = threading.Event()
    runner_finished = threading.Event()
    container = _DelayedLaunchMetadataContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
        launch_entered=launch_entered,
        allow_launch=allow_launch,
        runner_finished=runner_finished,
    )
    _prepare_and_verify_context(monkeypatch, container)
    monkeypatch.setenv("SUNABA_VERIFY_TIMEOUT", "0.25")
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "15.0")
    reap_done, verdicts = _spy_reap_completion(monkeypatch)

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    try:
        box: dict[str, Any] = {}

        def _run_verify() -> None:
            try:
                box["value"] = verify_in_container(
                    container.id,
                    path="tests",
                    working_dir=str(pkg_a),
                    skip_lint_gate=True,
                    skip_type_gate=True,
                )
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_verify, daemon=True)
        t.start()

        assert launch_entered.wait(10.0), "metadata exec did not reach launch gate"
        assert reap_done.wait(10.0), "outer verify reaper did not complete"
        t.join(10.0)
        assert not t.is_alive(), "verify_in_container hung under outer verify deadline"
        if "error" in box:
            raise box["error"]

        result = json.loads(box["value"])
        assert result["status"] == "timeout", result
        assert result["gate_passed"] is False
        assert result["timeout"]["deadline_s"] == 0.25
        assert result["timeout"]["reap"] == "ok"
        assert verdicts == ["ok"]
        assert has_verify_success(container.id) is False
        assert state_file.read_bytes() == state_bytes_before
        assert (ws_dir / "uv.lock").read_bytes() == lock_before
        assert not started_file.exists()

        allow_launch.set()
        assert runner_finished.wait(2.0), "late outer-verify metadata shell did not exit promptly"
        assert not started_file.exists(), "late-started shell under outer verify deadline must not invoke uv"
        assert _wait_token_processes_dead(token) == []
    finally:
        allow_launch.set()
        _kill_token_processes(token)


def test_late_metadata_exec_past_outer_deps_deadline_and_reap_never_invokes_uv(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, _, _, _ = _make_workspace(tmp_path, name="late-outer-deps")
    token = f"sunabaLateOD-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "late-od-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )
    uv_script = bin_dir / "uv"
    uv_script.write_text(
        uv_script.read_text(encoding="utf-8").replace(
            'if [ "$1" = "workspace" ] && [ "$2" = "dir" ]; then',
            'if [ "$1" = "sync" ]; then\n    exit 0\nfi\nif [ "$1" = "workspace" ] && [ "$2" = "dir" ]; then',
        ),
        encoding="utf-8",
    )

    launch_entered = threading.Event()
    allow_launch = threading.Event()
    runner_finished = threading.Event()
    container = _DelayedLaunchMetadataContainer(
        ws_dir,
        cid="late-outer-deps-cid",
        path_override=str(bin_dir),
        launch_entered=launch_entered,
        allow_launch=allow_launch,
        runner_finished=runner_finished,
    )
    record_verify_success(container.id)
    monkeypatch.setenv("SUNABA_DEPS_TIMEOUT", "0.35")
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "15.0")
    reap_done, verdicts = _spy_reap_completion(monkeypatch)

    lock_before = (ws_dir / "uv.lock").read_bytes()

    try:
        box: dict[str, Any] = {}

        def _run_prep() -> None:
            try:
                box["value"] = prepare_uv_environment(container, str(ws_dir))
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_prep, daemon=True)
        t.start()

        assert launch_entered.wait(10.0), "metadata exec did not reach launch gate"
        assert reap_done.wait(10.0), "outer deps reaper did not complete"
        t.join(10.0)
        assert not t.is_alive(), "prepare_uv_environment hung under outer deps deadline"
        if "error" in box:
            raise box["error"]

        prep_res = box["value"]
        assert prep_res.success is False
        assert prep_res.exit_code == 124
        assert prep_res.reason is not None and "deps_timeout" in prep_res.reason
        assert "reap=ok" in prep_res.message
        assert verdicts == ["ok"]
        assert has_verify_success(container.id) is False
        assert (ws_dir / "uv.lock").read_bytes() == lock_before
        assert not started_file.exists()

        allow_launch.set()
        assert runner_finished.wait(2.0), "late outer-deps metadata shell did not exit promptly"
        assert not started_file.exists(), "late-started shell under outer deps deadline must not invoke uv"
        assert _wait_token_processes_dead(token) == []
    finally:
        allow_launch.set()
        _kill_token_processes(token)


def test_late_metadata_exec_in_timeout_fallback_branch_never_renews_budget(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_timeout = shutil.which("timeout")
    if real_timeout is None:
        pytest.skip("timeout binary unavailable")

    ws_dir, _, _, _ = _make_workspace(tmp_path, name="late-timeout-fallback")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="late-timeout-fb-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabaLateTF-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "late-tf-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )
    (bin_dir / "timeout").symlink_to(real_timeout)

    launch_entered = threading.Event()
    allow_launch = threading.Event()
    runner_finished = threading.Event()
    container = _DelayedLaunchMetadataContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
        launch_entered=launch_entered,
        allow_launch=allow_launch,
        runner_finished=runner_finished,
        drop_env=True,
    )
    _prepare_and_verify_context(monkeypatch, container)
    monkeypatch.delenv("SUNABA_VERIFY_MARKER", raising=False)
    monkeypatch.delenv("SUNABA_FOREGROUND_MARKER", raising=False)
    monkeypatch.delenv("SUNABA_UV_METADATA_MARKER", raising=False)
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "0.2")
    monkeypatch.setenv("SUNABA_VERIFY_TIMEOUT", "30.0")
    reap_done, verdicts = _spy_reap_completion(monkeypatch)

    try:
        box: dict[str, Any] = {}

        def _run_fp() -> None:
            try:
                box["value"] = compute_manifest_fingerprint(container, str(ws_dir))
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        t = threading.Thread(target=_run_fp, daemon=True)
        t.start()
        assert launch_entered.wait(10.0)
        assert reap_done.wait(10.0)
        t.join(10.0)
        assert not t.is_alive()
        assert isinstance(box.get("error"), FingerprintProbeError)
        assert verdicts == ["ok"]
        assert not started_file.exists()

        allow_launch.set()
        assert runner_finished.wait(2.0), "late timeout-fallback shell did not exit promptly"
        assert not started_file.exists(), "timeout fallback must not renew relative budget on late exec"
        assert _wait_token_processes_dead(token) == []

        # Also verify non-delayed timeout fallback honors remaining outer verify deadline (< metadata bound)
        started_file.unlink(missing_ok=True)
        ready_file.unlink(missing_ok=True)
        timeout_arg_file = tmp_path / "timeout_arg.txt"
        fb_bin_dir = tmp_path / "late-tf-imm-bin"
        fb_bin_dir.mkdir()
        for tool in ("sh", "tr", "cat", "sleep", "env", "find", "git", "echo"):
            found = shutil.which(tool)
            if found:
                (fb_bin_dir / tool).symlink_to(found)
        timeout_wrapper = fb_bin_dir / "timeout"
        timeout_wrapper.write_text(
            f"""#!/bin/sh
printf '%s\\n' "$1" > '{timeout_arg_file}'
exec '{real_timeout}' -k 0.1 "$@"
""",
            encoding="utf-8",
        )
        timeout_wrapper.chmod(0o755)
        real_uv = shutil.which("uv")
        assert real_uv is not None
        orig_path = os.environ.get("PATH", "")
        fb_uv = fb_bin_dir / "uv"
        fb_uv.write_text(
            f"""#!/bin/sh
if [ "$1" = "workspace" ] && [ "$2" = "dir" ]; then
    printf '%s\\n' '{ws_dir}'
    exit 0
fi
if [ "$1" = "workspace" ] && [ "$2" = "metadata" ]; then
    exec sleep 5
fi
PATH='{orig_path}' exec '{real_uv}' "$@"
""",
            encoding="utf-8",
        )
        fb_uv.chmod(0o755)

        immediate_container = LocalWorkspaceContainer(
            ws_dir,
            cid=prep_container.id,
            path_override=str(fb_bin_dir),
        )
        orig_exec = immediate_container.exec_run

        def _drop_env_exec(
            cmd: list[str] | str,
            stdout: bool = True,
            stderr: bool = True,
            demux: bool = True,
            environment: dict[str, str] | None = None,
            workdir: str | None = None,
            **kwargs: Any,
        ) -> Any:
            return orig_exec(
                cmd,
                stdout=stdout,
                stderr=stderr,
                demux=demux,
                environment=None,
                workdir=workdir,
                **kwargs,
            )

        immediate_container.exec_run = _drop_env_exec  # type: ignore[method-assign]
        monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "15.0")

        from sunaba.edit_verify.shell import (
            VerifyTreeTimeout,
            close_verify_deadline,
            open_verify_deadline,
        )

        open_verify_deadline(immediate_container, 1.0, "sunaba910fb")
        t0 = time.monotonic()
        try:
            with pytest.raises((FingerprintProbeError, VerifyTreeTimeout)):
                compute_manifest_fingerprint(immediate_container, str(ws_dir))
        finally:
            close_verify_deadline()
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"timeout fallback renewed 15s bound instead of honoring 1.0s outer deadline: {elapsed:.2f}s"
        assert timeout_arg_file.exists()
        passed_timeout = float(timeout_arg_file.read_text(encoding="utf-8").strip())
        assert 0.0 < passed_timeout <= 1.0, f"timeout fallback must honor outer deadline, got {passed_timeout}"
    finally:
        allow_launch.set()
        _kill_token_processes(token)


def test_metadata_startup_eligibility_fails_closed_on_clock_or_protocol_mismatch(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="clock-mismatch")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="clock-mismatch-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(prep_container.id)

    token = f"sunabaClock-{uuid.uuid4().hex[:12]}"
    bin_dir = tmp_path / "clock-bin"
    started_file = tmp_path / "uv_started"
    ready_file = tmp_path / "uv_ready"
    pids_file = tmp_path / "uv_pids"
    _build_no_timeout_blocking_uv_bin(
        bin_dir,
        ws_dir,
        token=token,
        started_file=started_file,
        ready_file=ready_file,
        pids_file=pids_file,
    )

    class UptimeOverrideContainer(LocalWorkspaceContainer):
        def __init__(self, uptime_path: Path) -> None:
            super().__init__(ws_dir, cid=prep_container.id, path_override=str(bin_dir))
            self.uptime_path = str(uptime_path)

        def exec_run(
            self,
            cmd: list[str] | str,
            stdout: bool = True,
            stderr: bool = True,
            demux: bool = True,
            environment: dict[str, str] | None = None,
            workdir: str | None = None,
            **kwargs: Any,
        ) -> Any:
            if isinstance(cmd, (list, tuple)) and cmd and isinstance(cmd[-1], str):
                cmd = [*cmd[:-1], cmd[-1].replace("/proc/uptime", self.uptime_path)]
            return super().exec_run(
                cmd,
                stdout=stdout,
                stderr=stderr,
                demux=demux,
                environment=environment,
                workdir=workdir,
                **kwargs,
            )

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{prep_container.id}.json"
    state_bytes_before = state_file.read_bytes()

    try:
        # 1. Missing / unreadable /proc/uptime
        missing_container = UptimeOverrideContainer(tmp_path / "missing-uptime")
        _prepare_and_verify_context(monkeypatch, missing_container)
        with pytest.raises(FingerprintProbeError):
            compute_manifest_fingerprint(missing_container, str(ws_dir))
        assert not started_file.exists()

        # 2. Malformed /proc/uptime
        bad_uptime = tmp_path / "bad-uptime"
        bad_uptime.write_text("not-a-number 0\n", encoding="utf-8")
        bad_container = UptimeOverrideContainer(bad_uptime)
        _prepare_and_verify_context(monkeypatch, bad_container)
        with pytest.raises(FingerprintProbeError):
            compute_manifest_fingerprint(bad_container, str(ws_dir))
        assert not started_file.exists()

        # 3. Container clock behind host issued clock (VM / LXCFS mismatch)
        behind_uptime = tmp_path / "behind-uptime"
        behind_uptime.write_text("1.00 1.00\n", encoding="utf-8")
        behind_container = UptimeOverrideContainer(behind_uptime)
        _prepare_and_verify_context(monkeypatch, behind_container)
        verify_res = json.loads(
            verify_in_container(
                behind_container.id,
                path="tests",
                working_dir=str(pkg_a),
                skip_lint_gate=True,
                skip_type_gate=True,
            )
        )
        assert verify_res["status"] == "environment_setup_failed"
        assert verify_res["gate_passed"] is False
        assert has_verify_success(behind_container.id) is False
        assert not started_file.exists()
        assert state_file.read_bytes() == state_bytes_before
        assert (ws_dir / "uv.lock").read_bytes() == lock_before

        # 4. Host CLOCK_BOOTTIME unavailable fails closed before invoking exec_run/uv
        real_container = LocalWorkspaceContainer(
            ws_dir,
            cid=prep_container.id,
            path_override=str(bin_dir),
        )
        monkeypatch.delattr(time, "CLOCK_BOOTTIME", raising=False)
        with pytest.raises(FingerprintProbeError):
            compute_manifest_fingerprint(real_container, str(ws_dir))
        assert not started_file.exists()
        assert not any("uv workspace metadata" in c for c in real_container.exec_calls)
    finally:
        _kill_token_processes(token)


def _build_recording_passthrough_uv_bin(
    bin_dir: Path,
    *,
    started_file: Path,
) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in ("sh", "tr", "cat", "sleep", "env", "find", "git", "echo"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    real_uv = shutil.which("uv")
    assert real_uv is not None
    orig_path = os.environ.get("PATH", "")
    uv_script = bin_dir / "uv"
    uv_script.write_text(
        f"""#!/bin/sh
if [ "$1" = "workspace" ] && [ "$2" = "metadata" ]; then
    printf 'started\\n' >> '{started_file}'
fi
PATH='{orig_path}' exec '{real_uv}' "$@"
""",
        encoding="utf-8",
    )
    uv_script.chmod(0o755)


class _ClockDomainOverrideContainer(LocalWorkspaceContainer):
    """Container adapter substituting /proc clock-domain files in metadata commands."""

    def __init__(
        self,
        root: Path,
        *,
        cid: str,
        path_override: str,
        boot_id_path: Path | None = None,
        timens_offsets_path: Path | None = None,
        uptime_path: Path | None = None,
    ) -> None:
        super().__init__(root, cid=cid, path_override=path_override)
        self.boot_id_path = str(boot_id_path) if boot_id_path is not None else None
        self.timens_offsets_path = (
            str(timens_offsets_path) if timens_offsets_path is not None else None
        )
        self.uptime_path = str(uptime_path) if uptime_path is not None else None

    def exec_run(
        self,
        cmd: list[str] | str,
        stdout: bool = True,
        stderr: bool = True,
        demux: bool = True,
        environment: dict[str, str] | None = None,
        workdir: str | None = None,
        **kwargs: Any,
    ) -> Any:
        if isinstance(cmd, (list, tuple)) and cmd and isinstance(cmd[-1], str):
            script = cmd[-1]
            if self.boot_id_path is not None:
                script = script.replace(
                    "/proc/sys/kernel/random/boot_id", self.boot_id_path
                )
            if self.timens_offsets_path is not None:
                script = script.replace(
                    "/proc/self/timens_offsets", self.timens_offsets_path
                )
            if self.uptime_path is not None:
                script = script.replace("/proc/uptime", self.uptime_path)
            cmd = [*cmd[:-1], script]
        return super().exec_run(
            cmd,
            stdout=stdout,
            stderr=stderr,
            demux=demux,
            environment=environment,
            workdir=workdir,
            **kwargs,
        )


def test_distinct_clock_origin_inside_numeric_uptime_window_fails_before_uv(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test-first regression: matching numeric uptime across distinct clock domains must fail closed."""
    ws_dir, pkg_a, _, _ = _make_workspace(tmp_path, name="distinct-clock-origin")
    prep_container = LocalWorkspaceContainer(ws_dir, cid="distinct-clock-cid")
    _prepare_and_verify_context(monkeypatch, prep_container)
    assert (
        json.loads(sandbox_prepare_environment(prep_container.id, working_dir=str(ws_dir)))[
            "status"
        ]
        == "ok"
    )
    record_verify_success(prep_container.id)

    bin_dir = tmp_path / "clock-origin-bin"
    started_file = tmp_path / "uv_started"
    _build_recording_passthrough_uv_bin(bin_dir, started_file=started_file)

    host_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    distinct_boot_id = (
        "00000000-0000-4000-8000-000000000001"
        if host_boot_id != "00000000-0000-4000-8000-000000000001"
        else "00000000-0000-4000-8000-000000000002"
    )
    host_timens = Path("/proc/self/timens_offsets").read_text(encoding="utf-8")

    now_cs = time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 10_000_000
    in_window_uptime = tmp_path / "in_window_uptime"
    in_window_uptime.write_text(f"{(now_cs + 150) / 100:.2f} 0.00\n", encoding="utf-8")

    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{prep_container.id}.json"
    state_bytes_before = state_file.read_bytes()

    # 1. Remote/independently-booted Docker host (distinct boot_id) with coincidentally
    #    in-window numeric /proc/uptime must fail closed before invoking uv.
    remote_boot_id_file = tmp_path / "remote_boot_id"
    remote_boot_id_file.write_text(f"{distinct_boot_id}\n", encoding="utf-8")
    same_timens_file = tmp_path / "same_timens"
    same_timens_file.write_text(host_timens, encoding="utf-8")

    remote_container = _ClockDomainOverrideContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
        boot_id_path=remote_boot_id_file,
        timens_offsets_path=same_timens_file,
        uptime_path=in_window_uptime,
    )
    _prepare_and_verify_context(monkeypatch, remote_container)
    monkeypatch.setenv("SUNABA_UV_METADATA_TIMEOUT", "15.0")

    with pytest.raises(FingerprintProbeError, match="boot_id|clock domain"):
        compute_manifest_fingerprint(remote_container, str(ws_dir))
    assert not started_file.exists(), "uv must not start on distinct boot_id clock domain"

    verify_remote = json.loads(
        verify_in_container(
            remote_container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_remote["status"] == "environment_setup_failed"
    assert verify_remote["gate_passed"] is False
    assert "boot_id" in verify_remote["environment_setup"]["reason"]
    assert has_verify_success(remote_container.id) is False
    assert not started_file.exists()
    assert state_file.read_bytes() == state_bytes_before
    assert (ws_dir / "uv.lock").read_bytes() == lock_before

    # 2. Same kernel boot_id, different time-namespace boottime offset, but numeric
    #    uptime still inside [issued, cutoff) must fail closed before invoking uv.
    record_verify_success(prep_container.id)
    same_boot_id_file = tmp_path / "same_boot_id"
    same_boot_id_file.write_text(f"{host_boot_id}\n", encoding="utf-8")
    shifted_timens_file = tmp_path / "shifted_timens"
    shifted_timens_file.write_text(
        "monotonic           0         0\nboottime         3600         0\n",
        encoding="utf-8",
    )

    timens_container = _ClockDomainOverrideContainer(
        ws_dir,
        cid=prep_container.id,
        path_override=str(bin_dir),
        boot_id_path=same_boot_id_file,
        timens_offsets_path=shifted_timens_file,
        uptime_path=in_window_uptime,
    )
    _prepare_and_verify_context(monkeypatch, timens_container)

    with pytest.raises(FingerprintProbeError, match="time-namespace|boottime offset"):
        compute_manifest_fingerprint(timens_container, str(ws_dir))
    assert not started_file.exists(), "uv must not start on mismatched time-namespace boottime offset"

    verify_timens = json.loads(
        verify_in_container(
            timens_container.id,
            path="tests",
            working_dir=str(pkg_a),
            skip_lint_gate=True,
            skip_type_gate=True,
        )
    )
    assert verify_timens["status"] == "environment_setup_failed"
    assert verify_timens["gate_passed"] is False
    assert "boottime offset" in verify_timens["environment_setup"]["reason"]
    assert has_verify_success(timens_container.id) is False
    assert not started_file.exists()
    assert state_file.read_bytes() == state_bytes_before
    assert (ws_dir / "uv.lock").read_bytes() == lock_before


def test_clock_domain_validation_compatible_and_unverifiable_layouts(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, _, _, _ = _make_workspace(tmp_path, name="clock-domain-matrix")
    bin_dir = tmp_path / "matrix-bin"
    started_file = tmp_path / "uv_started"
    _build_recording_passthrough_uv_bin(bin_dir, started_file=started_file)

    host_boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    same_boot_id_file = tmp_path / "same_boot_id"
    same_boot_id_file.write_text(f"{host_boot_id}\n", encoding="utf-8")

    # 1. Compatible local clock: same boot_id and equivalent boottime offset
    #    (even when monotonic offset differs) succeeds and computes members.
    diff_monotonic_timens = tmp_path / "diff_monotonic_timens"
    diff_monotonic_timens.write_text(
        "monotonic         123   456000000\nboottime            0         0\n",
        encoding="utf-8",
    )
    compat_container = _ClockDomainOverrideContainer(
        ws_dir,
        cid="compat-cid",
        path_override=str(bin_dir),
        boot_id_path=same_boot_id_file,
        timens_offsets_path=diff_monotonic_timens,
    )
    fp = compute_manifest_fingerprint(compat_container, str(ws_dir))
    assert "members" in fp
    assert started_file.exists()
    started_file.unlink()

    # 2. Compatible non-zero matching boottime offset across host and container
    now_cs = time.clock_gettime_ns(time.CLOCK_BOOTTIME) // 10_000_000
    in_window_uptime = tmp_path / "in_window_uptime"
    in_window_uptime.write_text(f"{(now_cs + 150) / 100:.2f} 0.00\n", encoding="utf-8")
    host_nonzero_timens = tmp_path / "host_nonzero_timens"
    host_nonzero_timens.write_text(
        "monotonic           0         0\nboottime          -12 500000000\n",
        encoding="utf-8",
    )
    container_nonzero_timens = tmp_path / "container_nonzero_timens"
    container_nonzero_timens.write_text(
        "monotonic           0         0\nboottime         -012 0500000000",
        encoding="utf-8",
    )
    with monkeypatch.context() as m:
        m.setattr(
            "sunaba.environment_state_store._PROC_TIMENS_OFFSETS_PATH",
            str(host_nonzero_timens),
        )
        nonzero_container = _ClockDomainOverrideContainer(
            ws_dir,
            cid="nonzero-cid",
            path_override=str(bin_dir),
            boot_id_path=same_boot_id_file,
            timens_offsets_path=container_nonzero_timens,
            uptime_path=in_window_uptime,
        )
        fp_nz = compute_manifest_fingerprint(nonzero_container, str(ws_dir))
        assert "members" in fp_nz
        assert started_file.exists()
    started_file.unlink()

    # 3. Unverifiable container boot_id (missing / multi-line) fails closed before uv
    bad_boot_multi = tmp_path / "bad_boot_multi"
    bad_boot_multi.write_text(f"{host_boot_id}\nextra\n", encoding="utf-8")
    for bad_boot in (tmp_path / "missing_boot_id", bad_boot_multi):
        c = _ClockDomainOverrideContainer(
            ws_dir,
            cid="bad-boot-cid",
            path_override=str(bin_dir),
            boot_id_path=bad_boot,
            uptime_path=in_window_uptime,
        )
        with pytest.raises(FingerprintProbeError, match="boot_id"):
            compute_manifest_fingerprint(c, str(ws_dir))
        assert not started_file.exists()

    # 4. Unverifiable container timens_offsets (missing / no boottime / duplicate / invalid nsec)
    no_boottime = tmp_path / "no_boottime"
    no_boottime.write_text("monotonic 0 0\n", encoding="utf-8")
    dup_boottime = tmp_path / "dup_boottime"
    dup_boottime.write_text("boottime 0 0\nboottime 0 0\n", encoding="utf-8")
    bad_nsec = tmp_path / "bad_nsec"
    bad_nsec.write_text("boottime 0 1000000000\n", encoding="utf-8")
    bad_sec = tmp_path / "bad_sec"
    bad_sec.write_text("boottime abc 0\n", encoding="utf-8")
    for bad_to in (
        tmp_path / "missing_timens",
        no_boottime,
        dup_boottime,
        bad_nsec,
        bad_sec,
    ):
        c = _ClockDomainOverrideContainer(
            ws_dir,
            cid="bad-timens-cid",
            path_override=str(bin_dir),
            boot_id_path=same_boot_id_file,
            timens_offsets_path=bad_to,
            uptime_path=in_window_uptime,
        )
        with pytest.raises(FingerprintProbeError, match="timens_offsets"):
            compute_manifest_fingerprint(c, str(ws_dir))
        assert not started_file.exists()

    # 5. Unverifiable host boot_id / timens_offsets / unapplied time_for_children fails
    #    closed before exec_run or uv is invoked.
    real_container = LocalWorkspaceContainer(
        ws_dir, cid="host-unverifiable-cid", path_override=str(bin_dir)
    )
    with monkeypatch.context() as m:
        m.setattr(
            "sunaba.environment_state_store._PROC_BOOT_ID_PATH",
            str(tmp_path / "missing_host_boot_id"),
        )
        with pytest.raises(FingerprintProbeError, match="host boot_id"):
            compute_manifest_fingerprint(real_container, str(ws_dir))
    with monkeypatch.context() as m:
        m.setattr(
            "sunaba.environment_state_store._PROC_TIMENS_OFFSETS_PATH",
            str(tmp_path / "missing_host_timens"),
        )
        with pytest.raises(FingerprintProbeError, match="host timens_offsets"):
            compute_manifest_fingerprint(real_container, str(ws_dir))
    ns_a = tmp_path / "ns_a"
    ns_b = tmp_path / "ns_b"
    ns_a.symlink_to("time:[1]")
    ns_b.symlink_to("time:[2]")
    with monkeypatch.context() as m:
        m.setattr("sunaba.environment_state_store._PROC_TIME_NS_PATH", str(ns_a))
        m.setattr(
            "sunaba.environment_state_store._PROC_TIME_FOR_CHILDREN_NS_PATH", str(ns_b)
        )
        with pytest.raises(FingerprintProbeError, match="time namespace"):
            compute_manifest_fingerprint(real_container, str(ws_dir))
    assert not started_file.exists()
    assert not any("uv workspace metadata" in call for call in real_container.exec_calls)

    # 6. Ordinary non-workspace project still computes fingerprint without clock-domain probing
    ord_dir = tmp_path / "ordinary-no-clock"
    ord_dir.mkdir()
    (ord_dir / "pyproject.toml").write_text(
        '[project]\nname = "ord"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (ord_dir / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    ord_container = LocalWorkspaceContainer(
        ord_dir, cid="ord-no-clock-cid", path_override=str(bin_dir)
    )
    with monkeypatch.context() as m:
        m.setattr(
            "sunaba.environment_state_store._PROC_BOOT_ID_PATH",
            str(tmp_path / "missing_host_boot_id"),
        )
        ord_fp = compute_manifest_fingerprint(ord_container, str(ord_dir))
    assert "members" not in ord_fp
    assert not any("uv workspace metadata" in call for call in ord_container.exec_calls)
