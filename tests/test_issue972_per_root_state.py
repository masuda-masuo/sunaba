"""Regression coverage for preparation state scoped to independent uv roots (#972)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sunaba.environment import check_uv_environment
from sunaba.environment_state_store import (
    compute_manifest_fingerprint,
    load_environment_state,
    prune_and_load_environment_states,
)
from sunaba.tools.container.prepare_environment import sandbox_prepare_environment
from sunaba.tools.vcs.publishing import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import (
    clear_environment_state,
    forget_environment_state_for_root,
    get_environment_state,
    get_environment_state_for_root,
    has_verify_success,
    list_environment_states,
    record_environment_state,
    record_verify_success,
)


class LocalExecContainer:
    """Run container shell commands against the real temp project tree."""

    def __init__(self, root: Path) -> None:
        self.id = "issue972-container-id"
        self.root = str(root)

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
        env = dict(os.environ)
        if environment:
            env.update(environment)
        result = subprocess.run(
            argv,
            cwd=workdir or self.root,
            capture_output=True,
            check=False,
            env=env,
        )
        if demux:
            return result.returncode, (result.stdout, result.stderr)
        return result.returncode, result.stdout + result.stderr


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_dir = tmp_path / "host-state"
    state_dir.mkdir()
    monkeypatch.setenv("SUNABA_ENVIRONMENT_STATE_DIR", str(state_dir))
    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_environment_state", {})
    return state_dir


def _make_locked_project(root: Path, name: str) -> None:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'''[project]
name = "{name}"
version = "0.1.0"
dependencies = []

[tool.uv]
package = false
''',
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock"], cwd=root, check=True, capture_output=True)


def test_independent_uv_roots_keep_separate_preparation_records(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparing B keeps A ready, and an unprepared B is named by root."""
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    _make_locked_project(root_a, "project-a")
    _make_locked_project(root_b, "project-b")
    container = LocalExecContainer(tmp_path)

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    client = SimpleNamespace(containers=Containers())
    monkeypatch.setattr(
        "sunaba.tools.container.prepare_environment.common._docker", lambda: client
    )
    result_a = json.loads(
        sandbox_prepare_environment(container.id, working_dir=str(root_a))
    )
    assert result_a["status"] == "ok"
    fingerprint_a = get_environment_state_for_root(container.id, str(root_a))["fingerprint"]

    status, reason = check_uv_environment(container, str(root_b))
    assert status == "not_prepared"
    assert str(root_b) in (reason or "")
    assert 'sandbox_prepare_environment(container_id, working_dir="' + str(root_b) + '")' in (reason or "")
    monkeypatch.setattr("sunaba.tools.verify._docker", lambda: client)
    verify_b = json.loads(
        verify_in_container(
            container.id, path=".", working_dir=str(root_b)
        )
    )
    assert verify_b["status"] == "environment_setup_failed"
    assert str(root_b) in verify_b["environment_setup"]["reason"]
    assert "sandbox_prepare_environment(container_id, working_dir=" in verify_b["environment_setup"]["reason"]

    result_b = json.loads(
        sandbox_prepare_environment(container.id, working_dir=str(root_b))
    )
    assert result_b["status"] == "ok"
    states = list_environment_states(container.id)
    assert states[str(root_a)]["status"] == "ready"
    assert states[str(root_b)]["status"] == "ready"
    assert states[str(root_a)]["fingerprint"] == fingerprint_a
    assert check_uv_environment(container, str(root_a))[0] == "ready"
    assert check_uv_environment(container, str(root_b))[0] == "ready"


def test_old_single_record_file_loads_without_rewrite(
    tmp_path: Path, isolated_state: Path
) -> None:
    cid = "legacy-container-id"
    root = "/workspace/legacy"
    original = {
        "container_id": cid,
        "status": "ready",
        "reason": "",
        "command": "uv sync --locked",
        "exit_code": 0,
        "project_root": root,
        "fingerprint": {"root": root, "files": {"uv.lock": "old"}},
    }
    state_file = isolated_state / f"{cid}.json"
    state_file.write_text(json.dumps(original, indent=2), encoding="utf-8")

    loaded = load_environment_state(cid)
    assert loaded == original
    assert json.loads(state_file.read_text(encoding="utf-8")) == original

    class Containers:
        def list(self, all: bool = True) -> list[Any]:
            class Container:
                id = cid
            return [Container()]

    class Client:
        containers = Containers()

    prune_and_load_environment_states(Client())
    assert get_environment_state(cid)["status"] == "ready"
    assert get_environment_state_for_root(cid, root)["fingerprint"] == original["fingerprint"]
    assert json.loads(state_file.read_text(encoding="utf-8")) == original


def test_restart_normalizes_root_keys_without_rewriting_state_files(
    isolated_state: Path,
) -> None:
    root = "/workspace/"
    normalized_root = "/workspace"
    legacy_cid = "trailing-slash-legacy"
    roots_cid = "trailing-slash-roots"
    record = {
        "status": "ready",
        "reason": "",
        "command": "uv sync --locked",
        "exit_code": 0,
        "project_root": root,
        "fingerprint": {"root": root, "files": {"uv.lock": "old"}},
    }
    legacy_state = {"container_id": legacy_cid, **record}
    roots_state = {"container_id": roots_cid, "roots": {root: record}}
    legacy_file = isolated_state / f"{legacy_cid}.json"
    roots_file = isolated_state / f"{roots_cid}.json"
    legacy_file.write_text(json.dumps(legacy_state, indent=2), encoding="utf-8")
    roots_file.write_text(json.dumps(roots_state, indent=2), encoding="utf-8")
    legacy_bytes = legacy_file.read_bytes()
    roots_bytes = roots_file.read_bytes()

    class Containers:
        def list(self, all: bool = True) -> list[Any]:
            class LegacyContainer:
                id = legacy_cid

            class RootsContainer:
                id = roots_cid

            return [LegacyContainer(), RootsContainer()]

    class Client:
        containers = Containers()

    prune_and_load_environment_states(Client())

    for cid in (legacy_cid, roots_cid):
        states = list_environment_states(cid)
        assert set(states) == {normalized_root}
        assert get_environment_state_for_root(cid, normalized_root) is not None
        assert states[normalized_root]["project_root"] == root

    assert legacy_file.read_bytes() == legacy_bytes
    assert roots_file.read_bytes() == roots_bytes

    from sunaba.verify_state import _root_records

    assert set(_root_records({root: record})) == {normalized_root}
    assert set(_root_records(legacy_state)) == {normalized_root}


def test_concurrent_root_records_persist_without_losing_either_root(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sunaba import environment_state_store

    cid = "concurrent-root-record-container"
    roots = ("/workspace/project-a", "/workspace/project-b")
    start = threading.Barrier(3)
    save_lock = threading.Lock()
    save_count = 0
    original_save = environment_state_store.save_environment_states

    def slowed_first_save(container_id: str, records: dict[str, dict[str, Any]]) -> None:
        nonlocal save_count
        with save_lock:
            save_count += 1
            current_save = save_count
        if current_save == 1:
            time.sleep(0.1)
        original_save(container_id, records)

    monkeypatch.setattr(
        environment_state_store, "save_environment_states", slowed_first_save
    )
    errors: list[BaseException] = []

    def record_root(root: str) -> None:
        try:
            start.wait(timeout=5)
            record_environment_state(cid, "ready", project_root=root)
        except BaseException as exc:
            errors.append(exc)

    workers = [threading.Thread(target=record_root, args=(root,)) for root in roots]
    for worker in workers:
        worker.start()
    start.wait(timeout=5)
    for worker in workers:
        worker.join(timeout=5)

    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    expected_roots = set(roots)
    assert set(list_environment_states(cid)) == expected_roots

    persisted = load_environment_state(cid)
    assert persisted is not None
    assert set(persisted["roots"]) == expected_roots


def test_latest_record_and_clear_cover_all_roots(isolated_state: Path) -> None:
    cid = "state-order-container"
    record_environment_state(cid, "ready", project_root="/workspace/a")
    record_environment_state(cid, "failed", reason="broken", project_root="/workspace/b")

    assert get_environment_state(cid)["status"] == "failed"
    assert set(list_environment_states(cid)) == {"/workspace/a", "/workspace/b"}
    clear_environment_state(cid)
    assert list_environment_states(cid) == {}
    assert get_environment_state(cid) is None
    assert load_environment_state(cid) is None


def test_fingerprint_probe_failure_invalidates_verify_without_rewriting_state(
    tmp_path: Path, isolated_state: Path
) -> None:
    root = tmp_path / "broken-project"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname="broken"\nversion="0.1.0"\ndependencies=[]\n', encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    container = LocalExecContainer(root)
    record_environment_state(
        container.id,
        "ready",
        project_root=str(root),
        fingerprint={"root": str(root), "files": {"pyproject.toml": "prior"}},
    )
    state_file_before = (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")

    record_verify_success(container.id)

    class BrokenExec(LocalExecContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if "cat pyproject.toml" in rendered or "cat uv.lock" in rendered:
                return 2, (b"", b"read failed")
            return super().exec_run(cmd, *args, **kwargs)

    status, reason = check_uv_environment(BrokenExec(root), str(root))
    assert status == "failed"
    assert "fingerprint probe failed" in (reason or "")
    assert has_verify_success(container.id) is False
    assert (isolated_state / f"{container.id}.json").read_text(encoding="utf-8") == state_file_before


def test_publish_checks_stale_and_failed_secondary_roots(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secondary root blocks publish even if the last verify used A."""
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    _make_locked_project(root_a, "project-a")
    _make_locked_project(root_b, "project-b")
    container = LocalExecContainer(tmp_path)
    fingerprint_a = compute_manifest_fingerprint(container, str(root_a))
    fingerprint_b = compute_manifest_fingerprint(container, str(root_b))
    record_environment_state(
        container.id,
        "ready",
        fingerprint=fingerprint_a,
        project_root=str(root_a),
    )
    record_environment_state(
        container.id,
        "ready",
        fingerprint=fingerprint_b,
        project_root=str(root_b),
    )
    record_verify_success(container.id)

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    client = SimpleNamespace(containers=Containers())
    monkeypatch.setattr("sunaba.tools.vcs.publishing._docker", lambda: client)

    original_lock = (root_b / "uv.lock").read_bytes()
    (root_b / "uv.lock").write_bytes(original_lock + b"\n# changed\n")
    stale = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert stale["step"] == "verify_gate"
    assert str(root_b) in stale["error"]
    assert "stale" in stale["error"]

    (root_b / "uv.lock").write_bytes(original_lock)
    record_verify_success(container.id)
    recovered = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert recovered.get("step") != "verify_gate"

    record_environment_state(
        container.id, "failed", reason="sync failed", project_root=str(root_b)
    )
    record_verify_success(container.id)
    failed = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert failed["step"] == "verify_gate"
    assert str(root_b) in failed["error"]
    assert "failed" in failed["error"]

    # Keep B as the newest ready root while A alone is stale.
    record_environment_state(
        container.id,
        "ready",
        fingerprint=fingerprint_b,
        project_root=str(root_b),
    )
    record_verify_success(container.id)
    original_a_lock = (root_a / "uv.lock").read_bytes()
    (root_a / "uv.lock").write_bytes(original_a_lock + b"\n# changed\n")
    stale_a = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert stale_a["step"] == "verify_gate"
    assert str(root_a) in stale_a["error"]
    assert "stale" in stale_a["error"]

    (root_a / "uv.lock").write_bytes(original_a_lock)
    record_verify_success(container.id)
    recovered_a = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert recovered_a.get("step") != "verify_gate"

    # A failed root must also block while ready B remains the newest record.
    record_environment_state(
        container.id, "failed", reason="A sync failed", project_root=str(root_a)
    )
    record_environment_state(
        container.id,
        "ready",
        fingerprint=fingerprint_b,
        project_root=str(root_b),
    )
    record_verify_success(container.id)
    failed_a = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert failed_a["step"] == "verify_gate"
    assert str(root_a) in failed_a["error"]
    assert "failed" in failed_a["error"]


def test_vanished_uv_root_record_does_not_block_publish(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished secondary uv root is pruned so it cannot block publish."""
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    _make_locked_project(root_a, "project-a")
    _make_locked_project(root_b, "project-b")
    container = LocalExecContainer(tmp_path)
    fingerprint_a = compute_manifest_fingerprint(container, str(root_a))
    fingerprint_b = compute_manifest_fingerprint(container, str(root_b))
    record_environment_state(
        container.id,
        "ready",
        fingerprint=fingerprint_a,
        project_root=str(root_a),
    )
    record_environment_state(
        container.id,
        "ready",
        fingerprint=fingerprint_b,
        project_root=str(root_b),
    )
    record_verify_success(container.id)
    state_a_before = list_environment_states(container.id)[str(root_a)].copy()

    shutil.rmtree(root_b)

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    client = SimpleNamespace(containers=Containers())
    monkeypatch.setattr("sunaba.tools.vcs.publishing._docker", lambda: client)

    publish_result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert publish_result.get("step") != "verify_gate", publish_result.get("error")

    states = list_environment_states(container.id)
    assert str(root_b) not in states
    assert states[str(root_a)] == state_a_before

    persisted = json.loads(
        (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")
    )
    assert str(root_b) not in json.dumps(persisted)


def test_forget_environment_state_for_root_preserves_other_record_and_removes_last_file(
    isolated_state: Path,
) -> None:
    cid = "forget-state-container"
    root_a = "/workspace/a"
    root_b = "/workspace/b"
    record_environment_state(
        cid,
        "ready",
        reason="unchanged",
        project_root=root_a,
        fingerprint={"root": root_a, "files": {"uv.lock": "a"}},
    )
    record_environment_state(
        cid,
        "failed",
        reason="kept",
        project_root=root_b,
        fingerprint={"root": root_b, "files": {"uv.lock": "b"}},
    )
    state_file = isolated_state / f"{cid}.json"
    before = json.loads(state_file.read_text(encoding="utf-8"))
    record_b_before = before["roots"][root_b].copy()

    assert forget_environment_state_for_root(cid, root_a + "/.") is True
    assert list_environment_states(cid) == {root_b: record_b_before}
    after = json.loads(state_file.read_text(encoding="utf-8"))
    assert after["roots"][root_b] == record_b_before
    assert root_a not in after["roots"]

    assert forget_environment_state_for_root(cid, root_b) is True
    assert not state_file.exists()
    assert list_environment_states(cid) == {}
    assert forget_environment_state_for_root(cid, root_b) is False


def test_publish_blocks_when_recorded_root_loses_uv_lock(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    _make_locked_project(root_a, "project-a")
    _make_locked_project(root_b, "project-b")
    container = LocalExecContainer(tmp_path)
    fingerprint_a = compute_manifest_fingerprint(container, str(root_a))
    fingerprint_b = compute_manifest_fingerprint(container, str(root_b))
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint_a, project_root=str(root_a)
    )
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint_b, project_root=str(root_b)
    )
    record_verify_success(container.id)
    (root_b / "uv.lock").unlink()

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    monkeypatch.setattr(
        "sunaba.tools.vcs.publishing._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert result["step"] == "verify_gate"
    assert str(root_b) in result["error"]
    assert "stale" in result["error"]
    assert "uv.lock" in result["error"]
    assert str(root_b) in list_environment_states(container.id)


def test_publish_forgets_root_that_resolves_to_a_different_workspace_root(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    root_b = workspace / "project-b"
    _make_locked_project(root_b, "project-b")
    container = LocalExecContainer(tmp_path)
    fingerprint_b = compute_manifest_fingerprint(container, str(root_b))
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint_b, project_root=str(root_b)
    )
    record_verify_success(container.id)

    (root_b / "uv.lock").unlink()
    (workspace / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["project-b"]\n', encoding="utf-8"
    )
    subprocess.run(["uv", "lock"], cwd=workspace, check=True, capture_output=True)

    from sunaba.environment import resolve_uv_root

    assert resolve_uv_root(container, str(root_b)) == str(workspace)

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    monkeypatch.setattr(
        "sunaba.tools.vcs.publishing._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert result.get("step") != "verify_gate"
    assert str(root_b) not in list_environment_states(container.id)
    assert (
        f"forgot uv preparation record for {root_b}: root now resolves to {workspace}"
        in result["environment_notes"]
    )


def test_publish_probe_failure_preserves_root_record_and_blocks(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _make_locked_project(root, "project")
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class BrokenDirectoryProbe(LocalExecContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if "__SUNABA_DIRECTORY_EXISTS__" in rendered:
                raise RuntimeError("probe unavailable")
            return super().exec_run(cmd, *args, **kwargs)

    container = BrokenDirectoryProbe(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    record_verify_success(container.id)

    class Containers:
        def get(self, container_id: str) -> BrokenDirectoryProbe:
            return container

    monkeypatch.setattr(
        "sunaba.tools.vcs.publishing._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )
    assert result["step"] == "verify_gate"
    assert "failed" in result["error"]
    assert list_environment_states(container.id)[str(root)]["status"] == "ready"


def test_forgotten_root_notes_are_attached_to_later_publish_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    _make_locked_project(root, "project")
    container = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(container, str(root))
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    record_verify_success(container.id)
    shutil.rmtree(root)

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    monkeypatch.setattr(
        "sunaba.tools.vcs.publishing._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    result = json.loads(
        publish(
            container.id,
            "owner/repo",
            "feature",
            "message",
            create_pr=True,
            pr_title="Feature",
            pr_body="",
            skip_verify_gate=False,
        )
    )

    assert result["status"] == "error"
    assert result["step"] == "validation"
    assert "pr_body" in result["error"]
    assert (
        f"forgot uv preparation record for {root}: directory no longer exists"
        in result["environment_notes"]
    )


def test_publish_tool_description_documents_public_arguments() -> None:
    description = publish.__doc__ or ""

    assert "create_pr:" in description
    assert "files:" in description
    assert len(description.splitlines()) > 1
