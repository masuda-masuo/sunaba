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

    #: verify.py writes its nested pytest JUnit XML and raw log to these fixed
    #: /tmp paths.  Under xdist every worker runs on this same host filesystem,
    #: so parallel workers' nested verify calls would otherwise clobber each
    #: other's report and parse a truncated/missing XML file.
    _SHARED_REPORT_PATHS = (
        ("/tmp/_pytest_report.xml", "_pytest_report.xml"),
        ("/tmp/_pytest_raw.txt", "_pytest_raw.txt"),
    )

    #: The nested toy projects hold only a test or two.  verify's ``-n auto``
    #: would spawn ``cpu_count`` workers inside every outer xdist worker for
    #: no benefit, so cap the nested run while keeping real pytest execution.
    _NESTED_XDIST_CAP = "-n 1"

    def __init__(self, root: Path) -> None:
        self.id = "issue972-container-id"
        self.root = str(root)

    def _rewrite_command(self, text: str) -> str:
        """Isolate verify's fixed report paths and cap nested xdist fan-out."""
        for shared_path, local_name in self._SHARED_REPORT_PATHS:
            text = text.replace(shared_path, os.path.join(self.root, local_name))
        return text.replace("-n auto", self._NESTED_XDIST_CAP)

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
        if isinstance(cmd, (list, tuple)):
            argv = [self._rewrite_command(str(part)) for part in cmd]
        else:
            argv = ["/bin/sh", "-c", self._rewrite_command(str(cmd))]
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


def _stub_publish_environment(
    monkeypatch: pytest.MonkeyPatch, container: LocalExecContainer
) -> None:
    """Stub Docker lookup and post-gate egress proxy for isolated publish tests."""

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    monkeypatch.setattr(
        "sunaba.tools.vcs.publishing._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    monkeypatch.setattr(
        "sunaba.tools.vcs.publishing._ensure_proxy_ready",
        lambda _client: "stubbed after verify gate",
    )


def test_publish_retires_record_when_both_uv_manifests_are_missing(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing directory with neither uv manifest retires only its own record."""
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
    state_a_before = list_environment_states(container.id)[str(root_a)].copy()

    (root_b / "pyproject.toml").unlink()
    (root_b / "uv.lock").unlink()
    assert root_b.is_dir()
    record_verify_success(container.id)

    _stub_publish_environment(monkeypatch, container)
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )

    assert result.get("step") != "verify_gate", result.get("error")
    assert result["step"] == "egress_proxy"
    states = list_environment_states(container.id)
    assert str(root_b) not in states
    assert states[str(root_a)] == state_a_before
    persisted = json.loads(
        (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")
    )
    assert str(root_b) not in persisted.get("roots", {})
    assert persisted["roots"][str(root_a)] == state_a_before
    assert any(str(root_b) in note for note in result.get("environment_notes", []))


def test_publish_blocks_when_recorded_root_loses_pyproject_while_uv_lock_remains(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing only pyproject.toml while uv.lock remains must block and keep the record."""
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
    state_b_before = list_environment_states(container.id)[str(root_b)].copy()

    (root_b / "pyproject.toml").unlink()
    assert (root_b / "uv.lock").is_file()
    record_verify_success(container.id)

    _stub_publish_environment(monkeypatch, container)
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )

    assert result["step"] == "verify_gate"
    assert str(root_b) in result["error"]
    assert "stale" in result["error"]
    assert "pyproject.toml" in result["error"]
    assert list_environment_states(container.id)[str(root_b)] == state_b_before
    persisted = json.loads(
        (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")
    )
    assert persisted["roots"][str(root_b)] == state_b_before


def test_publish_manifest_probe_failure_preserves_root_record_and_blocks(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest probe failure after directory check must not retire the record."""
    root = tmp_path / "project"
    _make_locked_project(root, "project")
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class BrokenManifestProbe(LocalExecContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if "__SUNABA_DIRECTORY_EXISTS__" not in rendered and (
                "pyproject.toml" in rendered or "uv.lock" in rendered
            ):
                return 2, (b"", b"manifest probe failed")
            return super().exec_run(cmd, *args, **kwargs)

    container = BrokenManifestProbe(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    record_verify_success(container.id)

    _stub_publish_environment(monkeypatch, container)
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )

    assert result["step"] == "verify_gate"
    assert "failed" in result["error"]
    assert list_environment_states(container.id)[str(root)] == state_before
    persisted = json.loads(
        (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")
    )
    assert persisted["roots"][str(root)] == state_before


def test_publish_unresolved_root_manifest_probe_failure_preserves_record_and_blocks(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure in _probe_uv_manifests on an unresolved root must not retire the record."""
    root = tmp_path / "project"
    _make_locked_project(root, "project")
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class BrokenUnresolvedManifestProbe(LocalExecContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if (
                "__SUNABA_DIRECTORY_EXISTS__" not in rendered
                and "above:" not in rendered
                and ("pyproject.toml" in rendered or "uv.lock" in rendered)
            ):
                return 2, (b"", b"unresolved manifest probe failed")
            return super().exec_run(cmd, *args, **kwargs)

    container = BrokenUnresolvedManifestProbe(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    (root / "pyproject.toml").unlink()
    (root / "uv.lock").unlink()
    record_verify_success(container.id)

    _stub_publish_environment(monkeypatch, container)
    result = json.loads(
        publish(container.id, "owner/repo", "feature", "message", skip_verify_gate=False)
    )

    assert result["step"] == "verify_gate"
    assert "failed" in result["error"]
    assert list_environment_states(container.id)[str(root)] == state_before
    persisted = json.loads(
        (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")
    )
    assert persisted["roots"][str(root)] == state_before


def test_verify_then_publish_retires_record_when_both_uv_manifests_are_missing(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First verify retires a both-manifests-missing root so normal verify->publish works."""
    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    _make_locked_project(root_a, "project-a")
    _make_locked_project(root_b, "project-b")
    (root_b / "requirements.txt").write_text("", encoding="utf-8")
    tests_dir = root_b / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )

    container = LocalExecContainer(tmp_path)
    fingerprint_a = compute_manifest_fingerprint(container, str(root_a))
    fingerprint_b = compute_manifest_fingerprint(container, str(root_b))
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint_a, project_root=str(root_a)
    )
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint_b, project_root=str(root_b)
    )
    state_a_before = list_environment_states(container.id)[str(root_a)].copy()

    (root_b / "pyproject.toml").unlink()
    (root_b / "uv.lock").unlink()
    assert root_b.is_dir()
    assert has_verify_success(container.id) is False

    class Containers:
        def get(self, container_id: str) -> LocalExecContainer:
            return container

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    _stub_publish_environment(monkeypatch, container)

    verify_res = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root_b))
    )
    assert verify_res["status"] == "ok", verify_res
    assert verify_res["gate_passed"] is True, verify_res
    assert verify_res["tests"]["full"]["status"] == "ok", verify_res
    assert verify_res["tests"]["full"]["passed"] == 1, verify_res
    assert has_verify_success(container.id) is True
    states_after_verify = list_environment_states(container.id)
    assert str(root_b) not in states_after_verify
    assert states_after_verify[str(root_a)] == state_a_before
    persisted = json.loads(
        (isolated_state / f"{container.id}.json").read_text(encoding="utf-8")
    )
    assert str(root_b) not in persisted.get("roots", {})
    assert persisted["roots"][str(root_a)] == state_a_before

    pub_res = json.loads(
        publish(
            container.id,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert pub_res.get("step") != "verify_gate", pub_res.get("error")
    assert pub_res["step"] == "egress_proxy"

    # Losing only one manifest (uv.lock or pyproject.toml) must block normal verify->publish.
    for idx, missing_manifest in enumerate(("uv.lock", "pyproject.toml"), start=1):
        single_cid = f"issue972-single-{idx}-cid"
        single_root = tmp_path / f"single-missing-{idx}"
        _make_locked_project(single_root, f"single-{idx}")
        (single_root / "requirements.txt").write_text("", encoding="utf-8")
        single_tests = single_root / "tests"
        single_tests.mkdir()
        (single_tests / "test_ok.py").write_text(
            "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
        )
        single_container = LocalExecContainer(tmp_path)
        single_container.id = single_cid
        single_fp = compute_manifest_fingerprint(single_container, str(single_root))
        record_environment_state(
            single_cid, "ready", fingerprint=single_fp, project_root=str(single_root)
        )
        single_state_before = list_environment_states(single_cid)[str(single_root)].copy()
        (single_root / missing_manifest).unlink()
        assert has_verify_success(single_cid) is False

        class SingleContainers:
            def get(self, container_id: str, _c=single_container) -> LocalExecContainer:
                return _c

        monkeypatch.setattr(
            "sunaba.tools.verify._docker",
            lambda _sc=SingleContainers: SimpleNamespace(containers=_sc()),
        )
        _stub_publish_environment(monkeypatch, single_container)

        blocked_verify = json.loads(
            verify_in_container(
                single_cid, path="tests", working_dir=str(single_root)
            )
        )
        assert blocked_verify["status"] == "environment_setup_failed"
        assert blocked_verify["gate_passed"] is False
        assert blocked_verify["environment_setup"]["status"] == "stale"
        assert missing_manifest in blocked_verify["environment_setup"]["reason"]
        assert blocked_verify["tests"]["status"] == "skipped"
        assert has_verify_success(single_cid) is False
        assert list_environment_states(single_cid)[str(single_root)] == single_state_before

        blocked_pub = json.loads(
            publish(
                single_cid,
                "owner/repo",
                "feature",
                "message",
                skip_verify_gate=False,
            )
        )
        assert blocked_pub["status"] == "error"
        assert blocked_pub["step"] == "verify_gate"
        assert "no successful verify_in_container" in blocked_pub["error"]
        assert "stale" in blocked_pub["error"]
        assert missing_manifest in blocked_pub["error"]
        assert list_environment_states(single_cid)[str(single_root)] == single_state_before


def test_verify_then_publish_from_descendant_and_alias_retires_both_manifests_missing_root(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Descendant, external-symlink, and alias callers retire only the applicable root."""
    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root_a = tmp_path / "project-a"
    root_b = tmp_path / "project-b"
    _make_locked_project(root_a, "project-a")
    _make_locked_project(root_b, "project-b")
    desc = root_b / "packages" / "sub"
    desc_tests = desc / "tests"
    desc_tests.mkdir(parents=True)
    (desc / "requirements.txt").write_text("", encoding="utf-8")
    (desc_tests / "test_sub.py").write_text(
        "def test_sub() -> None:\n    assert True\n", encoding="utf-8"
    )
    outside_link = tmp_path / "outside-desc-link"
    outside_link.symlink_to(desc)

    container = LocalExecContainer(tmp_path)
    fp_a = compute_manifest_fingerprint(container, str(root_a))
    fp_b = compute_manifest_fingerprint(container, str(root_b))
    record_environment_state(
        container.id, "ready", fingerprint=fp_a, project_root=str(root_a)
    )
    record_environment_state(
        container.id, "ready", fingerprint=fp_b, project_root=str(root_b)
    )
    state_a_before = list_environment_states(container.id)[str(root_a)].copy()

    (root_b / "pyproject.toml").unlink()
    (root_b / "uv.lock").unlink()
    assert has_verify_success(container.id) is False

    class Containers:
        def get(self, container_id: str, _c=container) -> LocalExecContainer:
            return _c

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=Containers()),
    )
    _stub_publish_environment(monkeypatch, container)

    verify_res = json.loads(
        verify_in_container(
            container.id, path="tests", working_dir=str(outside_link)
        )
    )
    assert verify_res["status"] == "ok", verify_res
    assert verify_res["gate_passed"] is True, verify_res
    assert verify_res["tests"]["full"]["passed"] == 1
    assert has_verify_success(container.id) is True
    assert list_environment_states(container.id) == {str(root_a): state_a_before}

    pub_res = json.loads(
        publish(
            container.id,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert pub_res["step"] == "egress_proxy"

    # Symlinked recorded root alias: deleting both manifests in target retires the alias key.
    alias_cid = "issue972-alias-cid"
    monkeypatch.setattr(verify_state, "_verify_map", {})
    real_proj = tmp_path / "real-proj"
    _make_locked_project(real_proj, "real-proj")
    real_sub = real_proj / "sub"
    (real_sub / "tests").mkdir(parents=True)
    (real_sub / "requirements.txt").write_text("", encoding="utf-8")
    (real_sub / "tests" / "test_alias.py").write_text(
        "def test_alias() -> None:\n    assert True\n", encoding="utf-8"
    )
    alias_root = tmp_path / "alias-proj"
    alias_root.symlink_to(real_proj)
    alias_container = LocalExecContainer(tmp_path)
    alias_container.id = alias_cid
    alias_fp = compute_manifest_fingerprint(alias_container, str(alias_root))
    record_environment_state(
        alias_cid, "ready", fingerprint=alias_fp, project_root=str(alias_root)
    )
    (real_proj / "pyproject.toml").unlink()
    (real_proj / "uv.lock").unlink()
    assert has_verify_success(alias_cid) is False

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(
            containers=SimpleNamespace(get=lambda _cid: alias_container)
        ),
    )
    _stub_publish_environment(monkeypatch, alias_container)

    alias_verify = json.loads(
        verify_in_container(
            alias_cid, path="tests", working_dir=str(real_sub)
        )
    )
    assert alias_verify["status"] == "ok", alias_verify
    assert alias_verify["gate_passed"] is True
    assert list_environment_states(alias_cid) == {}
    assert has_verify_success(alias_cid) is True
    alias_pub = json.loads(
        publish(
            alias_cid,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert alias_pub["step"] == "egress_proxy"


def test_verify_retirement_does_not_mint_receipt_or_bypass_publish_gate(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retiring a non-uv root never mints a verify receipt on failing or affected runs."""
    from sunaba import verify_state

    # 1. Failing tests after retiring both-absent root must leave publish blocked.
    monkeypatch.setattr(verify_state, "_verify_map", {})
    fail_proj = tmp_path / "fail-proj"
    _make_locked_project(fail_proj, "fail-proj")
    (fail_proj / "requirements.txt").write_text("", encoding="utf-8")
    (fail_proj / "tests").mkdir()
    (fail_proj / "tests" / "test_fail.py").write_text(
        "def test_fail() -> None:\n    assert False\n", encoding="utf-8"
    )
    fail_container = LocalExecContainer(fail_proj)
    fail_fp = compute_manifest_fingerprint(fail_container, str(fail_proj))
    record_environment_state(
        fail_container.id, "ready", fingerprint=fail_fp, project_root=str(fail_proj)
    )
    (fail_proj / "pyproject.toml").unlink()
    (fail_proj / "uv.lock").unlink()
    assert has_verify_success(fail_container.id) is False

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(
            containers=SimpleNamespace(get=lambda _cid: fail_container)
        ),
    )
    _stub_publish_environment(monkeypatch, fail_container)

    fail_verify = json.loads(
        verify_in_container(
            fail_container.id, path="tests", working_dir=str(fail_proj)
        )
    )
    assert fail_verify["status"] == "failed"
    assert fail_verify["gate_passed"] is False
    assert fail_verify["tests"]["full"]["failed"] == 1
    assert list_environment_states(fail_container.id) == {}
    assert has_verify_success(fail_container.id) is False
    fail_pub = json.loads(
        publish(
            fail_container.id,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert fail_pub["step"] == "verify_gate"
    assert "no successful verify_in_container" in fail_pub["error"]

    # 2. Filtered partial run after retiring both-absent root must not mint a receipt.
    partial_cid = "issue972-partial-cid"
    monkeypatch.setattr(verify_state, "_verify_map", {})
    part_proj = tmp_path / "partial-proj"
    _make_locked_project(part_proj, "partial-proj")
    (part_proj / "requirements.txt").write_text("", encoding="utf-8")
    (part_proj / "tests").mkdir()
    (part_proj / "tests" / "test_mod.py").write_text(
        "def test_mod() -> None:\n    assert True\n", encoding="utf-8"
    )
    part_container = LocalExecContainer(part_proj)
    part_container.id = partial_cid
    part_fp = compute_manifest_fingerprint(part_container, str(part_proj))
    record_environment_state(
        partial_cid, "ready", fingerprint=part_fp, project_root=str(part_proj)
    )
    (part_proj / "pyproject.toml").unlink()
    (part_proj / "uv.lock").unlink()
    assert has_verify_success(partial_cid) is False

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(
            containers=SimpleNamespace(get=lambda _cid: part_container)
        ),
    )
    _stub_publish_environment(monkeypatch, part_container)

    part_verify = json.loads(
        verify_in_container(
            partial_cid,
            path="tests",
            working_dir=str(part_proj),
            test_filter="test_nonexistent",
        )
    )
    assert part_verify["partial_test_run"] is True
    assert part_verify["gate_passed"] is False
    assert list_environment_states(partial_cid) == {}
    assert has_verify_success(partial_cid) is False
    part_pub = json.loads(
        publish(
            partial_cid,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert part_pub["step"] == "verify_gate"
    assert "no successful verify_in_container" in part_pub["error"]


def test_verify_probe_failure_and_timeout_retain_record_when_manifests_missing(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest probe errors and timeouts during verify fail closed and keep the record."""
    from sunaba import verify_state
    from sunaba.edit_verify.shell import VerifyTreeTimeout

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root = tmp_path / "probe-proj"
    _make_locked_project(root, "probe-proj")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class BrokenUnresolvedManifestProbe(LocalExecContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if (
                "for f in pyproject.toml uv.lock" in rendered
                and "above:" not in rendered
            ):
                return 2, (b"", b"unresolved manifest probe failed")
            return super().exec_run(cmd, *args, **kwargs)

    container = BrokenUnresolvedManifestProbe(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    (root / "pyproject.toml").unlink()
    (root / "uv.lock").unlink()
    record_verify_success(container.id)

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _cid: container)),
    )
    probe_err = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert probe_err["status"] == "environment_setup_failed"
    assert probe_err["gate_passed"] is False
    assert "uv manifest probe failed" in probe_err["environment_setup"]["reason"]
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before

    class TimeoutUnresolvedManifestProbe(LocalExecContainer):
        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if (
                "for f in pyproject.toml uv.lock" in rendered
                and "above:" not in rendered
            ):
                raise VerifyTreeTimeout(5.0, "ok", 5.1)
            return super().exec_run(cmd, *args, **kwargs)

    timeout_container = TimeoutUnresolvedManifestProbe(tmp_path)
    timeout_container.id = "issue972-timeout-cid"
    record_environment_state(
        timeout_container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    timeout_state_before = list_environment_states(timeout_container.id)[str(root)].copy()
    record_verify_success(timeout_container.id)
    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(
            containers=SimpleNamespace(get=lambda _cid: timeout_container)
        ),
    )

    timeout_res = json.loads(
        verify_in_container(
            timeout_container.id, path="tests", working_dir=str(root)
        )
    )
    assert timeout_res["status"] == "timeout"
    assert timeout_res["gate_passed"] is False
    assert has_verify_success(timeout_container.id) is False
    assert list_environment_states(timeout_container.id)[str(root)] == timeout_state_before


def test_verify_initial_directory_probe_error_preserves_record_and_invalidates_success(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial directory probe failure for a direct root must fail closed immediately."""
    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root = tmp_path / "initial-probe-proj"
    _make_locked_project(root, "initial-probe-proj")
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class InitialDirectoryProbeErrorContainer(LocalExecContainer):
        def __init__(self, proj_root: Path) -> None:
            super().__init__(proj_root)
            self.directory_probe_calls = 0
            self.executed: list[str] = []

        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            self.executed.append(rendered)
            if "__SUNABA_DIRECTORY_EXISTS__" in rendered:
                self.directory_probe_calls += 1
                if self.directory_probe_calls == 1:
                    return 2, (b"", b"initial directory probe failed")
            return super().exec_run(cmd, *args, **kwargs)

    container = InitialDirectoryProbeErrorContainer(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    persisted_before = (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    )
    (root / "pyproject.toml").unlink()
    (root / "uv.lock").unlink()
    record_verify_success(container.id)
    assert has_verify_success(container.id) is True

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _cid: container)),
    )

    res = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert res["status"] == "environment_setup_failed"
    assert res["gate_passed"] is False
    assert "environment_setup" in res["gate_fail_kinds"]
    assert "directory probe failed" in res["environment_setup"]["reason"]
    assert res["tests"]["status"] == "skipped"
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before
    assert (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    ) == persisted_before
    assert container.directory_probe_calls == 1
    assert not any("for f in pyproject.toml uv.lock" in cmd for cmd in container.executed)
    assert not any("__SUNABA_PATH_OK__" in cmd for cmd in container.executed)
    assert not any(
        "--junit-xml=" in cmd or " -m pytest" in cmd for cmd in container.executed
    )


def test_verify_initial_directory_probe_timeout_preserves_record_and_invalidates_success(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Initial directory probe TreeTimeout for a direct root preserves timeout semantics."""
    from sunaba import verify_state
    from sunaba.edit_verify.shell import VerifyTreeTimeout

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root = tmp_path / "initial-timeout-proj"
    _make_locked_project(root, "initial-timeout-proj")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class InitialDirectoryProbeTimeoutContainer(LocalExecContainer):
        def __init__(self, proj_root: Path) -> None:
            super().__init__(proj_root)
            self.directory_probe_calls = 0
            self.executed: list[str] = []

        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            self.executed.append(rendered)
            if "__SUNABA_DIRECTORY_EXISTS__" in rendered:
                self.directory_probe_calls += 1
                raise VerifyTreeTimeout(5.0, "ok", 5.1)
            return super().exec_run(cmd, *args, **kwargs)

    container = InitialDirectoryProbeTimeoutContainer(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    persisted_before = (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    )
    (root / "pyproject.toml").unlink()
    (root / "uv.lock").unlink()
    record_verify_success(container.id)
    assert has_verify_success(container.id) is True

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _cid: container)),
    )

    res = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert res["status"] == "timeout"
    assert res["gate_passed"] is False
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before
    assert (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    ) == persisted_before
    assert container.directory_probe_calls == 1
    assert not any("for f in pyproject.toml uv.lock" in cmd for cmd in container.executed)
    assert not any("__SUNABA_PATH_OK__" in cmd for cmd in container.executed)
    assert not any(
        "--junit-xml=" in cmd or " -m pytest" in cmd for cmd in container.executed
    )


def test_verify_empty_manifest_probe_output_preserves_record_and_invalidates_success(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty exit-0 output from the direct manifest probe fails closed and keeps state."""
    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root = tmp_path / "empty-manifest-probe-proj"
    _make_locked_project(root, "empty-manifest-probe-proj")
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class EmptyDirectManifestProbeContainer(LocalExecContainer):
        def __init__(self, proj_root: Path) -> None:
            super().__init__(proj_root)
            self.direct_manifest_probe_calls = 0
            self.executed: list[str] = []

        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            self.executed.append(rendered)
            if (
                "for f in pyproject.toml uv.lock" in rendered
                and "above:" not in rendered
            ):
                self.direct_manifest_probe_calls += 1
                return 0, (b"", b"")
            return super().exec_run(cmd, *args, **kwargs)

    container = EmptyDirectManifestProbeContainer(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    persisted_before = (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    )
    (root / "uv.lock").unlink()
    assert (root / "pyproject.toml").is_file()
    record_verify_success(container.id)
    assert has_verify_success(container.id) is True

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _cid: container)),
    )

    res = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert res["status"] == "environment_setup_failed"
    assert res["gate_passed"] is False
    assert "environment_setup" in res["gate_fail_kinds"]
    assert "uv manifest probe" in res["environment_setup"]["reason"]
    assert res["tests"]["status"] == "skipped"
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before
    assert (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    ) == persisted_before
    assert container.direct_manifest_probe_calls == 1
    assert not any(
        "--junit-xml=" in cmd or " -m pytest" in cmd for cmd in container.executed
    )


def test_manifest_and_root_discovery_probe_protocol() -> None:
    """Both _probe_uv_manifests and resolve_uv_root enforce a single trailing marker."""
    from sunaba.edit_verify.shell import ForegroundTreeTimeout, VerifyTreeTimeout
    from sunaba.environment import (
        EnvironmentResolutionError,
        _probe_uv_manifests,
        resolve_uv_root,
    )

    class StaticProbeContainer:
        def __init__(self, responses: list[Any]) -> None:
            self._responses = list(responses)
            self.calls = 0

        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            item = self._responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    # Valid normal absence, single manifest, and both manifests.
    for payload, expected in (
        (b"__SUNABA_MANIFEST_PROBE_DONE__\n", set()),
        (b"pyproject.toml\n__SUNABA_MANIFEST_PROBE_DONE__\n", {"pyproject.toml"}),
        (b"uv.lock\n__SUNABA_MANIFEST_PROBE_DONE__\n", {"uv.lock"}),
        (
            b"pyproject.toml\nuv.lock\n__SUNABA_MANIFEST_PROBE_DONE__\n",
            {"pyproject.toml", "uv.lock"},
        ),
    ):
        c_manifest = StaticProbeContainer([(0, (payload, b""))])
        assert _probe_uv_manifests(c_manifest, "/workspace") == expected
        assert c_manifest.calls == 1

        c_root = StaticProbeContainer([(0, (payload, b""))])
        expected_root = (
            "/workspace" if expected == {"pyproject.toml", "uv.lock"} else None
        )
        assert resolve_uv_root(c_root, "/workspace") == expected_root
        assert c_root.calls == 1

    # Invalid outputs for both probes: empty, missing marker, duplicate marker,
    # marker not at end, unknown record, repeated manifest record, nonzero exit.
    bad_payloads = [
        b"",
        b"   \n",
        b"pyproject.toml\n",
        b"pyproject.toml\nuv.lock\n",
        b"__SUNABA_MANIFEST_PROBE_DONE__\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"pyproject.toml\n__SUNABA_MANIFEST_PROBE_DONE__\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"__SUNABA_MANIFEST_PROBE_DONE__\npyproject.toml\n",
        b"unknown_file\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"pyproject.toml\npyproject.toml\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"uv.lock\nuv.lock\n__SUNABA_MANIFEST_PROBE_DONE__\n",
    ]
    for bad in bad_payloads:
        c_manifest = StaticProbeContainer([(0, (bad, b""))])
        with pytest.raises(EnvironmentResolutionError):
            _probe_uv_manifests(c_manifest, "/workspace")
        assert c_manifest.calls == 1

        c_root = StaticProbeContainer([(0, (bad, b""))])
        with pytest.raises(EnvironmentResolutionError):
            resolve_uv_root(c_root, "/workspace")
        assert c_root.calls == 1

    # Direct manifest probe must also reject above: records.
    c_above_in_direct = StaticProbeContainer(
        [(0, (b"above:/workspace\n__SUNABA_MANIFEST_PROBE_DONE__\n", b""))]
    )
    with pytest.raises(EnvironmentResolutionError):
        _probe_uv_manifests(c_above_in_direct, "/workspace")
    assert c_above_in_direct.calls == 1

    # resolve_uv_root rejects missing/malformed/multiple above: records.
    for bad_ancestor in (
        b"above:\n",
        b"above:\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"above:relative/path\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"above: /workspace\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"above:/workspace/../other\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"above:/ws1\nabove:/ws2\n__SUNABA_MANIFEST_PROBE_DONE__\n",
        b"above:/ws1\nabove:/ws1\n__SUNABA_MANIFEST_PROBE_DONE__\n",
    ):
        c_root = StaticProbeContainer([(0, (bad_ancestor, b""))])
        with pytest.raises(EnvironmentResolutionError):
            resolve_uv_root(c_root, "/workspace/sub")
        assert c_root.calls == 1

    # Nonzero exit fails closed without retrying even if stdout carried a marker.
    for probe_fn in (
        lambda c: _probe_uv_manifests(c, "/workspace"),
        lambda c: resolve_uv_root(c, "/workspace"),
    ):
        c_err = StaticProbeContainer(
            [(2, (b"__SUNABA_MANIFEST_PROBE_DONE__\n", b"probe error"))]
        )
        with pytest.raises(EnvironmentResolutionError, match="exit 2"):
            probe_fn(c_err)
        assert c_err.calls == 1

    # Active deadline TreeTimeouts propagate directly without retrying or wrapping.
    for exc in (
        VerifyTreeTimeout(5.0, "ok", 5.1),
        ForegroundTreeTimeout(5.0, "ok", 5.1),
    ):
        c_manifest_to = StaticProbeContainer([exc])
        with pytest.raises(type(exc)):
            _probe_uv_manifests(c_manifest_to, "/workspace")
        assert c_manifest_to.calls == 1

        c_root_to = StaticProbeContainer([exc])
        with pytest.raises(type(exc)):
            resolve_uv_root(c_root_to, "/workspace")
        assert c_root_to.calls == 1


def test_verify_empty_root_discovery_output_preserves_record_and_invalidates_success(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty exit-0 output from resolve_uv_root's initial probe fails closed."""
    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root = tmp_path / "empty-root-discovery-proj"
    _make_locked_project(root, "empty-root-discovery-proj")
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )
    base = LocalExecContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(base, str(root))

    class EmptyRootDiscoveryContainer(LocalExecContainer):
        def __init__(self, proj_root: Path) -> None:
            super().__init__(proj_root)
            self.root_discovery_calls = 0
            self.executed: list[str] = []

        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            self.executed.append(rendered)
            if "for f in pyproject.toml uv.lock" in rendered and "above:" in rendered:
                self.root_discovery_calls += 1
                return 0, (b"", b"")
            return super().exec_run(cmd, *args, **kwargs)

    container = EmptyRootDiscoveryContainer(tmp_path)
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    persisted_before = (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    )
    record_verify_success(container.id)
    assert has_verify_success(container.id) is True

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _cid: container)),
    )

    res = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert res["status"] == "environment_setup_failed"
    assert res["gate_passed"] is False
    assert "environment_setup" in res["gate_fail_kinds"]
    assert "uv manifest probe" in res["environment_setup"]["reason"]
    assert res["tests"]["status"] == "skipped"
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before
    assert (isolated_state / f"{container.id}.json").read_text(
        encoding="utf-8"
    ) == persisted_before
    assert container.root_discovery_calls == 1
    assert not any(
        "--junit-xml=" in cmd or " -m pytest" in cmd for cmd in container.executed
    )


def test_publish_empty_probe_outputs_preserve_record_and_invalidate_verify_success(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish fails closed, keeps records, and invalidates verify success on empty probes."""
    from sunaba import verify_state

    for probe_kind in ("direct_manifest", "root_discovery"):
        monkeypatch.setattr(verify_state, "_verify_map", {})
        cid = f"issue1003-pub-{probe_kind}"
        root = tmp_path / f"pub-{probe_kind}"
        _make_locked_project(root, f"pub-{probe_kind}")
        base = LocalExecContainer(tmp_path)
        fingerprint = compute_manifest_fingerprint(base, str(root))

        class EmptyProbeContainer(LocalExecContainer):
            def __init__(self, proj_root: Path, kind: str) -> None:
                super().__init__(proj_root)
                self.kind = kind
                self.probe_calls = 0

            def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
                rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                is_root_discovery = (
                    "for f in pyproject.toml uv.lock" in rendered
                    and "above:" in rendered
                )
                is_direct_manifest = (
                    "for f in pyproject.toml uv.lock" in rendered
                    and "above:" not in rendered
                )
                if (self.kind == "root_discovery" and is_root_discovery) or (
                    self.kind == "direct_manifest" and is_direct_manifest
                ):
                    self.probe_calls += 1
                    return 0, (b"", b"")
                return super().exec_run(cmd, *args, **kwargs)

        container = EmptyProbeContainer(tmp_path, probe_kind)
        container.id = cid
        record_environment_state(
            cid, "ready", fingerprint=fingerprint, project_root=str(root)
        )
        state_before = list_environment_states(cid)[str(root)].copy()
        persisted_before = (isolated_state / f"{cid}.json").read_text(
            encoding="utf-8"
        )
        if probe_kind == "direct_manifest":
            (root / "uv.lock").unlink()
        record_verify_success(cid)
        assert has_verify_success(cid) is True

        _stub_publish_environment(monkeypatch, container)
        res = json.loads(
            publish(cid, "owner/repo", "feature", "message", skip_verify_gate=False)
        )
        assert res["status"] == "error"
        assert res["step"] == "verify_gate"
        assert "failed" in res["error"]
        assert "uv manifest probe" in res["error"]
        assert container.probe_calls == 1
        assert has_verify_success(cid) is False
        assert list_environment_states(cid)[str(root)] == state_before
        assert (isolated_state / f"{cid}.json").read_text(
            encoding="utf-8"
        ) == persisted_before


def test_verify_to_publish_stale_and_probe_error_recovery_flow(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty direct probe blocks without clearing record; real stale blocks; recovery passes."""
    import sys

    from sunaba import verify_state

    monkeypatch.setattr(verify_state, "_verify_map", {})
    root = tmp_path / "stale-recovery-proj"
    _make_locked_project(root, "stale-recovery-proj")
    (root / ".venv").symlink_to(Path(sys.executable).parent.parent)
    (root / "requirements.txt").write_text("", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
    )

    class ControllableProbeContainer(LocalExecContainer):
        def __init__(self, proj_root: Path) -> None:
            super().__init__(proj_root)
            self.inject_empty_direct_probe = False

        def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
            rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
            if (
                self.inject_empty_direct_probe
                and "for f in pyproject.toml uv.lock" in rendered
                and "above:" not in rendered
            ):
                return 0, (b"", b"")
            return super().exec_run(cmd, *args, **kwargs)

    container = ControllableProbeContainer(tmp_path)
    fingerprint = compute_manifest_fingerprint(container, str(root))
    record_environment_state(
        container.id, "ready", fingerprint=fingerprint, project_root=str(root)
    )
    state_before = list_environment_states(container.id)[str(root)].copy()
    original_lock = (root / "uv.lock").read_bytes()
    record_verify_success(container.id)

    monkeypatch.setattr(
        "sunaba.tools.verify._docker",
        lambda: SimpleNamespace(containers=SimpleNamespace(get=lambda _cid: container)),
    )
    _stub_publish_environment(monkeypatch, container)

    # 1. Remove uv.lock and inject empty direct manifest probe -> fail closed, keep record, clear verify.
    (root / "uv.lock").unlink()
    container.inject_empty_direct_probe = True
    err_verify = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert err_verify["status"] == "environment_setup_failed"
    assert err_verify["tests"]["status"] == "skipped"
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before

    # 2. Stop injecting empty probe -> real verify sees stale uv.lock, blocks, then publish blocks too.
    container.inject_empty_direct_probe = False
    stale_verify = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert stale_verify["status"] == "environment_setup_failed"
    assert stale_verify["environment_setup"]["status"] == "stale"
    assert "uv.lock" in stale_verify["environment_setup"]["reason"]
    assert has_verify_success(container.id) is False
    assert list_environment_states(container.id)[str(root)] == state_before

    stale_pub = json.loads(
        publish(
            container.id,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert stale_pub["step"] == "verify_gate"
    assert "no successful verify_in_container" in stale_pub["error"]
    assert "stale" in stale_pub["error"]
    assert "uv.lock" in stale_pub["error"]

    # 3. Restore uv.lock: publish still blocks until a fresh verify_in_container passes.
    (root / "uv.lock").write_bytes(original_lock)
    unverified_pub = json.loads(
        publish(
            container.id,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert unverified_pub["step"] == "verify_gate"
    assert "no successful verify_in_container" in unverified_pub["error"]

    ok_verify = json.loads(
        verify_in_container(container.id, path="tests", working_dir=str(root))
    )
    assert ok_verify["status"] == "ok", ok_verify
    assert ok_verify["gate_passed"] is True
    assert has_verify_success(container.id) is True

    ok_pub = json.loads(
        publish(
            container.id,
            "owner/repo",
            "feature",
            "message",
            skip_verify_gate=False,
        )
    )
    assert ok_pub["step"] == "egress_proxy"


def test_publish_tool_description_documents_public_arguments() -> None:
    description = publish.__doc__ or ""

    assert "create_pr:" in description
    assert "files:" in description
    assert len(description.splitlines()) > 1


def test_manifest_probes_fail_when_workdir_cannot_be_entered(tmp_path: Path) -> None:
    """Real shell probes fail closed when cd to workdir fails instead of returning absence."""
    from sunaba.environment import (
        EnvironmentResolutionError,
        _probe_uv_manifests,
        probe_uv_project_root,
        resolve_uv_root,
    )

    locked_root = tmp_path / "locked-root"
    _make_locked_project(locked_root, "locked-root")
    not_a_dir = locked_root / "not-a-dir"
    not_a_dir.write_text("file\n", encoding="utf-8")
    missing_dir = locked_root / "missing-workdir"
    empty_subdir = tmp_path / "outside-subdir"
    empty_subdir.mkdir()

    # Start the shell cwd inside a locked project so an unguarded tail would
    # otherwise inspect the wrong directory or emit the completion marker.
    container = LocalExecContainer(locked_root)

    assert _probe_uv_manifests(container, str(locked_root)) == {
        "pyproject.toml",
        "uv.lock",
    }
    assert resolve_uv_root(container, str(locked_root)) == str(locked_root)
    assert _probe_uv_manifests(container, str(empty_subdir)) == set()
    assert resolve_uv_root(container, str(empty_subdir)) is None

    for bad_workdir in (missing_dir, not_a_dir, locked_root / "pyproject.toml"):
        with pytest.raises(EnvironmentResolutionError, match="uv manifest probe failed"):
            _probe_uv_manifests(container, str(bad_workdir))
        with pytest.raises(EnvironmentResolutionError, match="uv manifest probe failed"):
            probe_uv_project_root(container, str(bad_workdir))
        with pytest.raises(EnvironmentResolutionError, match="uv manifest probe failed"):
            resolve_uv_root(container, str(bad_workdir))


def test_verify_failure_invalidates_requested_alias_and_canonical_receipts(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify probe, staleness, tool, and deadline failures invalidate both alias and canonical receipts."""
    import sys

    from sunaba import verify_state
    from sunaba.edit_verify.shell import VerifyTreeTimeout

    canonical_id = "abc123def456" + "0" * 52

    for idx, req_id in enumerate(("dev-box-alias", "abc123", canonical_id[:12])):
        monkeypatch.setattr(verify_state, "_verify_map", {})
        monkeypatch.setattr(verify_state, "_environment_state", {})
        proj_root = tmp_path / f"verify-alias-{idx}"
        _make_locked_project(proj_root, f"verify-alias-{idx}")
        (proj_root / ".venv").symlink_to(Path(sys.executable).parent.parent)
        (proj_root / "requirements.txt").write_text("", encoding="utf-8")
        (proj_root / "tests").mkdir()
        (proj_root / "tests" / "test_ok.py").write_text(
            "def test_ok() -> None:\n    assert True\n", encoding="utf-8"
        )
        original_lock = (proj_root / "uv.lock").read_bytes()

        class AliasContainer(LocalExecContainer):
            def __init__(self, root: Path, cid: str) -> None:
                super().__init__(root)
                self.id = cid
                self.inject_empty_root_probe = False
                self.inject_fp_timeout = False
                self.inject_lint_timeout = False

            def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
                rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                if (
                    self.inject_empty_root_probe
                    and "for f in pyproject.toml uv.lock" in rendered
                    and "above:" in rendered
                ):
                    return 0, (b"", b"")
                if self.inject_fp_timeout and "__SUNABA_MANIFEST_ABSENT__" in rendered:
                    raise VerifyTreeTimeout(5.0, "ok", 5.1)
                if (
                    self.inject_lint_timeout
                    and "ruff" in rendered
                    and "check --output-format json" in rendered
                ):
                    raise VerifyTreeTimeout(5.0, "ok", 5.1)
                return super().exec_run(cmd, *args, **kwargs)

        container = AliasContainer(tmp_path, canonical_id)
        fp = compute_manifest_fingerprint(container, str(proj_root))
        record_environment_state(
            canonical_id, "ready", fingerprint=fp, project_root=str(proj_root)
        )
        state_before = list_environment_states(canonical_id)[str(proj_root)].copy()
        persisted_before = (isolated_state / f"{canonical_id}.json").read_text(
            encoding="utf-8"
        )

        monkeypatch.setattr(
            "sunaba.tools.verify._docker",
            lambda _c=container: SimpleNamespace(
                containers=SimpleNamespace(get=lambda _cid: _c)
            ),
        )
        _stub_publish_environment(monkeypatch, container)

        def _seed_both_receipts(requested_id: str = req_id) -> None:
            record_verify_success(requested_id)
            record_verify_success(canonical_id)
            assert has_verify_success(requested_id) is True
            assert has_verify_success(canonical_id) is True

        # 1. Manifest probe failure
        _seed_both_receipts()
        container.inject_empty_root_probe = True
        probe_res = json.loads(
            verify_in_container(req_id, path="tests", working_dir=str(proj_root))
        )
        container.inject_empty_root_probe = False
        assert probe_res["status"] == "environment_setup_failed"
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before
        assert (isolated_state / f"{canonical_id}.json").read_text(
            encoding="utf-8"
        ) == persisted_before

        # 2. Staleness failure (uv.lock removed) + publish stays blocked after restore
        _seed_both_receipts()
        (proj_root / "uv.lock").unlink()
        stale_res = json.loads(
            verify_in_container(req_id, path="tests", working_dir=str(proj_root))
        )
        assert stale_res["status"] == "environment_setup_failed"
        assert stale_res["environment_setup"]["status"] == "stale"
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before
        (proj_root / "uv.lock").write_bytes(original_lock)
        for check_id in (req_id, canonical_id):
            pub_blocked = json.loads(
                publish(
                    check_id,
                    "owner/repo",
                    "feature",
                    "message",
                    skip_verify_gate=False,
                )
            )
            assert pub_blocked["step"] == "verify_gate"
            assert "no successful verify_in_container" in pub_blocked["error"]

        # 3. Deadline timeout during staleness fingerprint probe
        _seed_both_receipts()
        container.inject_fp_timeout = True
        timeout_res = json.loads(
            verify_in_container(req_id, path="tests", working_dir=str(proj_root))
        )
        container.inject_fp_timeout = False
        assert timeout_res["status"] == "timeout"
        assert timeout_res["gate_passed"] is False
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before

        # 4. Missing required uv tool
        _seed_both_receipts()
        (proj_root / ".venv").unlink()
        tool_res = json.loads(
            verify_in_container(req_id, path="tests", working_dir=str(proj_root))
        )
        assert tool_res["status"] == "environment_setup_failed"
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before
        (proj_root / ".venv").symlink_to(Path(sys.executable).parent.parent)

        # 5. Deadline timeout inside lint/type gate
        _seed_both_receipts()
        container.inject_lint_timeout = True
        lint_timeout_res = json.loads(
            verify_in_container(req_id, path="tests", working_dir=str(proj_root))
        )
        container.inject_lint_timeout = False
        assert lint_timeout_res["status"] == "timeout"
        assert lint_timeout_res["gate_passed"] is False
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before
        assert (isolated_state / f"{canonical_id}.json").read_text(
            encoding="utf-8"
        ) == persisted_before

        # 6. Already non-ready recorded roots invalidate both receipts and preserve records
        for non_ready_status in ("failed", "not_prepared"):
            record_environment_state(
                canonical_id,
                non_ready_status,
                reason=f"prior {non_ready_status}",
                project_root=str(proj_root),
            )
            non_ready_before = list_environment_states(canonical_id)[str(proj_root)].copy()
            non_ready_persisted = (isolated_state / f"{canonical_id}.json").read_text(
                encoding="utf-8"
            )
            _seed_both_receipts()
            non_ready_res = json.loads(
                verify_in_container(req_id, path="tests", working_dir=str(proj_root))
            )
            assert non_ready_res["status"] == "environment_setup_failed"
            assert non_ready_res["environment_setup"]["status"] == non_ready_status
            assert has_verify_success(req_id) is False
            assert has_verify_success(canonical_id) is False
            assert list_environment_states(canonical_id)[str(proj_root)] == non_ready_before
            assert (isolated_state / f"{canonical_id}.json").read_text(
                encoding="utf-8"
            ) == non_ready_persisted


def test_publish_recorded_root_gate_failure_invalidates_alias_and_canonical_receipts(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish recorded-root gate failures invalidate both supplied alias and canonical receipts."""
    from sunaba import verify_state
    from sunaba.edit_verify.shell import VerifyTreeTimeout

    canonical_id = "fedcba987654" + "0" * 52

    for idx, req_id in enumerate(("pub-box-alias", "fedcba", canonical_id[:12])):
        monkeypatch.setattr(verify_state, "_verify_map", {})
        monkeypatch.setattr(verify_state, "_environment_state", {})
        proj_root = tmp_path / f"pub-alias-{idx}"
        _make_locked_project(proj_root, f"pub-alias-{idx}")
        original_lock = (proj_root / "uv.lock").read_bytes()

        class PublishAliasContainer(LocalExecContainer):
            def __init__(self, root: Path, cid: str) -> None:
                super().__init__(root)
                self.id = cid
                self.inject_empty_direct_probe = False
                self.inject_fp_error = False
                self.inject_fp_timeout = False

            def exec_run(self, cmd: list[str] | str, *args: Any, **kwargs: Any) -> Any:
                rendered = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                if (
                    self.inject_empty_direct_probe
                    and "for f in pyproject.toml uv.lock" in rendered
                    and "above:" not in rendered
                ):
                    return 0, (b"", b"")
                if "__SUNABA_MANIFEST_ABSENT__" in rendered:
                    if self.inject_fp_timeout:
                        raise VerifyTreeTimeout(5.0, "ok", 5.1)
                    if self.inject_fp_error:
                        return 2, (b"", b"fingerprint probe failed")
                return super().exec_run(cmd, *args, **kwargs)

        container = PublishAliasContainer(tmp_path, canonical_id)
        fp = compute_manifest_fingerprint(container, str(proj_root))
        record_environment_state(
            canonical_id, "ready", fingerprint=fp, project_root=str(proj_root)
        )
        state_before = list_environment_states(canonical_id)[str(proj_root)].copy()
        persisted_before = (isolated_state / f"{canonical_id}.json").read_text(
            encoding="utf-8"
        )
        _stub_publish_environment(monkeypatch, container)

        def _seed_both_receipts(requested_id: str = req_id) -> None:
            record_verify_success(requested_id)
            record_verify_success(canonical_id)
            assert has_verify_success(requested_id) is True
            assert has_verify_success(canonical_id) is True

        # 1. Stale recorded root -> publish refuses, invalidates both receipts, stays blocked after restore
        _seed_both_receipts()
        (proj_root / "uv.lock").unlink()
        stale_pub = json.loads(
            publish(req_id, "owner/repo", "feature", "message", skip_verify_gate=False)
        )
        assert stale_pub["status"] == "error"
        assert stale_pub["step"] == "verify_gate"
        assert "stale" in stale_pub["error"]
        assert "no successful verify_in_container" in stale_pub["error"]
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before
        assert (isolated_state / f"{canonical_id}.json").read_text(
            encoding="utf-8"
        ) == persisted_before

        (proj_root / "uv.lock").write_bytes(original_lock)
        retry_pub = json.loads(
            publish(req_id, "owner/repo", "feature", "message", skip_verify_gate=False)
        )
        assert retry_pub["step"] == "verify_gate"
        assert "no successful verify_in_container" in retry_pub["error"]

        # 2. Fingerprint probe failure -> invalidates both receipts and stays blocked
        _seed_both_receipts()
        container.inject_fp_error = True
        fp_err_pub = json.loads(
            publish(req_id, "owner/repo", "feature", "message", skip_verify_gate=False)
        )
        container.inject_fp_error = False
        assert fp_err_pub["status"] == "error"
        assert fp_err_pub["step"] == "verify_gate"
        assert "no successful verify_in_container" in fp_err_pub["error"]
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before

        # 3. Direct manifest probe failure inside recorded_root_is_uv_project
        _seed_both_receipts()
        (proj_root / "uv.lock").unlink()
        container.inject_empty_direct_probe = True
        probe_err_pub = json.loads(
            publish(req_id, "owner/repo", "feature", "message", skip_verify_gate=False)
        )
        container.inject_empty_direct_probe = False
        (proj_root / "uv.lock").write_bytes(original_lock)
        assert probe_err_pub["status"] == "error"
        assert probe_err_pub["step"] == "verify_gate"
        assert "no successful verify_in_container" in probe_err_pub["error"]
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before

        # 4. VerifyTreeTimeout during publish staleness check propagates and invalidates both
        _seed_both_receipts()
        container.inject_fp_timeout = True
        with pytest.raises(VerifyTreeTimeout):
            publish(req_id, "owner/repo", "feature", "message", skip_verify_gate=False)
        container.inject_fp_timeout = False
        assert has_verify_success(req_id) is False
        assert has_verify_success(canonical_id) is False
        assert list_environment_states(canonical_id)[str(proj_root)] == state_before

        # 5. Already non-ready recorded roots invalidate both receipts and preserve records
        for non_ready_status in ("failed", "not_prepared"):
            record_environment_state(
                canonical_id,
                non_ready_status,
                reason=f"prior {non_ready_status}",
                project_root=str(proj_root),
            )
            non_ready_before = list_environment_states(canonical_id)[str(proj_root)].copy()
            non_ready_persisted = (isolated_state / f"{canonical_id}.json").read_text(
                encoding="utf-8"
            )
            _seed_both_receipts()
            non_ready_pub = json.loads(
                publish(req_id, "owner/repo", "feature", "message", skip_verify_gate=False)
            )
            assert non_ready_pub["status"] == "error"
            assert non_ready_pub["step"] == "verify_gate"
            assert non_ready_status in non_ready_pub["error"]
            assert "no successful verify_in_container" in non_ready_pub["error"]
            assert has_verify_success(req_id) is False
            assert has_verify_success(canonical_id) is False
            assert list_environment_states(canonical_id)[str(proj_root)] == non_ready_before
            assert (isolated_state / f"{canonical_id}.json").read_text(
                encoding="utf-8"
            ) == non_ready_persisted
