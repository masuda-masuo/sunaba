"""Tests for uv workspace member manifest fingerprinting (#988)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sunaba.environment import check_uv_environment
from sunaba.environment_state_store import (
    FingerprintProbeError,
    compute_manifest_fingerprint,
)
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
    ) -> None:
        self.id = cid
        self.root = str(root)
        self.allow_network = allow_network
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
        rendered = argv[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        self.exec_calls.append(rendered)
        env = dict(os.environ)
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


def _mock_clean_lint_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    detection = SimpleNamespace(languages={"python"}, scope={"python": "."}, reason=None)
    monkeypatch.setattr("sunaba.edit_verify.detect_languages", lambda *a, **kw: detection)
    monkeypatch.setattr(
        "sunaba.edit_verify.run_lint_type_gate",
        lambda *a, **kw: {
            "gate_passed": True,
            "incomplete": False,
            "lint": [],
            "types": [],
            "patch_targets": [],
            "gate_fail_reasons": [],
        },
    )


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
    _mock_clean_lint_gates(monkeypatch)

    prep = json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))
    assert prep["status"] == "ok", prep

    verify_ok = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
    _mock_clean_lint_gates(monkeypatch)

    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(container.id)
    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    # Add new unlocked member matching members = ["packages/*"]
    _write_member(ws_dir, "packages/c", "pkg-c")

    verify_unlocked = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
    assert json.loads(verify_in_container(container.id, path="tests", working_dir=str(pkg_a)))["gate_passed"] is True


def test_removing_member_unlocked_and_locked_stops_verify_and_publish(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, pkg_b, _ = _make_workspace(tmp_path)
    container = LocalWorkspaceContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)
    _mock_clean_lint_gates(monkeypatch)

    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    record_verify_success(container.id)
    lock_before = (ws_dir / "uv.lock").read_bytes()
    state_file = isolated_env / f"{container.id}.json"
    state_bytes_before = state_file.read_bytes()

    # Delete member b without relocking
    shutil.rmtree(pkg_b)

    verify_unlocked = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
    )
    assert verify_locked["status"] == "environment_setup_failed"
    assert verify_locked["environment_setup"]["status"] == "stale"
    assert "packages/b/pyproject.toml" in verify_locked["environment_setup"]["reason"]

    # Reprepare updates member set and unblocks
    assert json.loads(sandbox_prepare_environment(container.id, working_dir=str(ws_dir)))["status"] == "ok"
    members = _recorded_root_state(container.id, ws_dir)["fingerprint"]["members"]
    assert "packages/b/pyproject.toml" not in members
    assert json.loads(verify_in_container(container.id, path="tests", working_dir=str(pkg_a)))["gate_passed"] is True


def test_excluded_member_and_member_python_version_are_ignored(
    tmp_path: Path, isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws_dir, pkg_a, _, pkg_excluded = _make_workspace(tmp_path, with_excluded=True)
    assert pkg_excluded is not None
    container = LocalWorkspaceContainer(ws_dir)
    _prepare_and_verify_context(monkeypatch, container)
    _mock_clean_lint_gates(monkeypatch)

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
        verify_in_container(container.id, path="tests", working_dir=str(pkg_a))
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
    _mock_clean_lint_gates(monkeypatch)

    status, reason = check_uv_environment(ws_container, str(pkg_a))
    assert status == "stale"
    assert reason is not None
    assert state_file.read_bytes() == state_before

    verify_legacy = json.loads(
        verify_in_container(ws_container.id, path="tests", working_dir=str(pkg_a))
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
    assert json.loads(
        verify_in_container(ws_container.id, path="tests", working_dir=str(pkg_a))
    )["gate_passed"] is True


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
    _mock_clean_lint_gates(monkeypatch)
    assert check_uv_environment(ord_container, str(ord_dir)) == ("ready", None)
    verify_ord = json.loads(
        verify_in_container(ord_container.id, path="tests", working_dir=str(ord_dir))
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
