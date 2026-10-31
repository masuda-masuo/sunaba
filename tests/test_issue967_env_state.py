"""Tests for Issue #967: uv environment state fingerprint stale detection & persistence."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from sunaba.environment import check_uv_environment, uv_environment_block_reason
from sunaba.environment_state_store import (
    compute_manifest_fingerprint,
    load_environment_state,
    prune_and_load_environment_states,
    remove_environment_state,
    save_environment_state,
)
from sunaba.tools.vcs.publishing import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import (
    get_environment_state,
    has_verify_success,
    record_environment_state,
    record_verify_success,
)
from tests.conftest import _make_client_mock


class MockFilesystemContainer:
    """Simulates container exec_run with an in-memory filesystem for manifest probing."""

    def __init__(self, cid: str, root: str, files: dict[str, bytes]) -> None:
        self.id = cid
        self.root = root.rstrip("/")
        self.files = files
        self.probe_failure: str | None = None
        self.fingerprint_probe_failure: str | None = None
        self.executed: list[str] = []
        self.cat_read_failure: str | None = None

    def exec_run(self, cmd: list[str] | str, **kwargs) -> tuple[int, tuple[bytes, bytes]]:
        cmd_str = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        self.executed.append(cmd_str)

        if self.probe_failure:
            return 127, (b"", self.probe_failure.encode("utf-8"))

        cd_match = re.search(r"cd\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))\s+&&", cmd_str)
        raw_dir = (
            (cd_match.group(1) or cd_match.group(2) or cd_match.group(3)).rstrip("/")
            if cd_match
            else self.root
        )
        curr_dir = self.root if raw_dir in ("/home/sandbox", self.root) else raw_dir

        # Manifest probe loop (probe_uv_project_root)
        if "for f in pyproject.toml uv.lock" in cmd_str:
            lines = []
            for fname in ("pyproject.toml", "uv.lock"):
                full_path = f"{curr_dir}/{fname}"
                if full_path in self.files:
                    lines.append(fname.encode("utf-8"))
            out = b"\n".join(lines) + (b"\n" if lines else b"")
            return 0, (out, b"")

        # Single file probe from _read_container_file:
        for fname in ("pyproject.toml", "uv.lock", ".python-version"):
            if f"'{fname}'" in cmd_str or f'"{fname}"' in cmd_str or f" {fname}" in cmd_str:
                if self.fingerprint_probe_failure:
                    return 127, (b"", self.fingerprint_probe_failure.encode("utf-8"))
                full_path = f"{curr_dir}/{fname}"
                if full_path in self.files:
                    if self.cat_read_failure is not None:
                        return 1, (b"", self.cat_read_failure.encode("utf-8"))
                    return 0, (self.files[full_path], b"")
                return 0, (b"__SUNABA_MANIFEST_ABSENT__\n", b"")

        # Resolving tools
        if "test -x" in cmd_str:
            # Let python / ruff / pyright / pytest exist
            return 0, (b"", b"")
        if "--version" in cmd_str:
            return 0, (b"tool 1.0.0\n", b"")
        if "git diff" in cmd_str or "git ls-files" in cmd_str:
            return 0, (b"", b"")
        if "git rev-parse" in cmd_str:
            return 0, (b"main\n", b"")

        return 0, (b"", b"")


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate environment state and persistence store directory."""
    monkeypatch.setenv("SUNABA_ENVIRONMENT_STATE_DIR", str(tmp_path))
    with (
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.verify_state._verify_map", {}),
        patch(
            "sunaba.tools.vcs.resolve_git_root",
            side_effect=lambda c, wd=None: wd if wd is not None else (getattr(c, "root", None) or "/workspace"),
        ),
    ):
        yield tmp_path


def _make_ready_container(
    cid: str = "container_967_full_id_1234567890abcdef",
    root: str = "/workspace",
    include_python_version: bool = False,
) -> tuple[MockFilesystemContainer, dict[str, bytes]]:
    files: dict[str, bytes] = {
        f"{root}/pyproject.toml": b"[project]\nname = 'test'\nversion = '0.1.0'\n",
        f"{root}/uv.lock": b"version = 1\nrevision = 1\n",
    }
    if include_python_version:
        files[f"{root}/.python-version"] = b"3.11\n"
    container = MockFilesystemContainer(cid, root, files)
    return container, files


def test_staleness_on_uv_lock_change_and_recovery(isolated_state: Path) -> None:
    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=fp, project_root=container.root
    )
    record_verify_success(cid)
    assert has_verify_success(cid) is True

    # Initial check passes
    status, reason = check_uv_environment(container, container.root)
    assert status == "ready"
    assert reason is None
    assert uv_environment_block_reason(container, container.root) is None

    # Change uv.lock
    original_uv_lock = files[f"{container.root}/uv.lock"]
    files[f"{container.root}/uv.lock"] = b"version = 1\nrevision = 2 # modified\n"

    # Now staleness is detected
    status, reason = check_uv_environment(container, container.root)
    assert status == "stale"
    assert reason is not None
    assert "uv.lock" in reason
    assert "recreate container" in reason
    assert has_verify_success(cid) is False
    block_reason = uv_environment_block_reason(container, container.root)
    assert block_reason is not None
    assert "stale" in block_reason

    # Stored state in memory and store remains "ready"
    stored = get_environment_state(cid)
    assert stored is not None
    assert stored["status"] == "ready"

    # Verify fails without running gates
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res_json = verify_in_container(container_id=cid, path=container.root)
        res = json.loads(res_json)
        assert res["status"] == "environment_setup_failed"
        assert res["gate_passed"] is False
        assert res["environment_setup"]["status"] == "stale"
        assert "uv.lock" in res["environment_setup"]["reason"]
        assert res["lint"]["status"] == "skipped"
        assert res["tests"]["status"] == "skipped"

    # Publish is refused
    with patch("sunaba.tools.vcs.publishing._docker", return_value=client):
        pub_json = publish(
            container_id=cid,
            repo="owner/repo",
            branch="feature",
            message="msg",
            skip_verify_gate=False,
        )
        pub = json.loads(pub_json)
        assert pub["status"] == "error"
        assert pub["step"] == "verify_gate"
        assert "stale" in pub["error"]
        assert "uv.lock" in pub["error"]

    # Restore uv.lock to original content
    files[f"{container.root}/uv.lock"] = original_uv_lock

    # Status returns to ready without re-preparing
    status, reason = check_uv_environment(container, container.root)
    assert status == "ready"
    assert reason is None
    assert uv_environment_block_reason(container, container.root) is None


def test_staleness_on_creating_and_deleting_python_version(isolated_state: Path) -> None:
    container, files = _make_ready_container(include_python_version=False)
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    assert fp["files"][".python-version"] == "absent"
    record_environment_state(
        cid, "ready", command="uv sync", exit_code=0, fingerprint=fp, project_root=container.root
    )
    record_verify_success(cid)

    # Creating .python-version makes verify and publish report stale, naming .python-version
    files[f"{container.root}/.python-version"] = b"3.12.0\n"

    status, reason = check_uv_environment(container, container.root)
    assert status == "stale"
    assert reason is not None
    assert ".python-version" in reason
    assert has_verify_success(cid) is False

    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res = json.loads(verify_in_container(container_id=cid, path=container.root))
        assert res["status"] == "environment_setup_failed"
        assert res["environment_setup"]["status"] == "stale"
        assert ".python-version" in res["environment_setup"]["reason"]

    with patch("sunaba.tools.vcs.publishing._docker", return_value=client):
        pub = json.loads(
            publish(
                container_id=cid,
                repo="owner/repo",
                branch="feat",
                message="m",
                skip_verify_gate=False,
            )
        )
        assert pub["status"] == "error"
        assert "stale" in pub["error"]
        assert ".python-version" in pub["error"]

    # Deleting .python-version restores ready
    del files[f"{container.root}/.python-version"]
    status, reason = check_uv_environment(container, container.root)
    assert status == "ready"
    assert reason is None


def test_staleness_on_removing_originally_present_python_version(isolated_state: Path) -> None:
    container, files = _make_ready_container(include_python_version=True)
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    assert fp["files"][".python-version"] != "absent"
    record_environment_state(
        cid, "ready", command="uv sync", exit_code=0, fingerprint=fp, project_root=container.root
    )

    # Delete .python-version -> mismatch (present-to-absent)
    del files[f"{container.root}/.python-version"]
    status, reason = check_uv_environment(container, container.root)
    assert status == "stale"
    assert reason is not None
    assert ".python-version" in reason


def test_staleness_on_pyproject_toml_change(isolated_state: Path) -> None:
    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid, "ready", command="uv sync", exit_code=0, fingerprint=fp, project_root=container.root
    )

    # Change pyproject.toml
    orig_pyproject = files[f"{container.root}/pyproject.toml"]
    files[f"{container.root}/pyproject.toml"] = b"[project]\nname='test'\nversion='0.2.0'\n"

    status, reason = check_uv_environment(container, container.root)
    assert status == "stale"
    assert reason is not None
    assert "pyproject.toml" in reason

    # Restore pyproject.toml
    files[f"{container.root}/pyproject.toml"] = orig_pyproject
    status, reason = check_uv_environment(container, container.root)
    assert status == "ready"


def test_probe_read_failure_stops_verify_clears_success_keeps_state_ready(
    isolated_state: Path,
) -> None:
    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid, "ready", command="uv sync", exit_code=0, fingerprint=fp, project_root=container.root
    )
    record_verify_success(cid)
    assert has_verify_success(cid) is True

    # Inject transient probe failure
    container.fingerprint_probe_failure = "I/O error reading manifest"

    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res = json.loads(verify_in_container(container_id=cid, path=container.root))
        assert res["status"] == "environment_setup_failed"
        assert res["environment_setup"]["status"] == "failed"
        assert "fingerprint probe failed" in res["environment_setup"]["reason"]

    # verify success record is cleared
    assert has_verify_success(cid) is False

    # stored state remains ready (not overwritten on transient probe failure)
    stored = get_environment_state(cid)
    assert stored is not None
    assert stored["status"] == "ready"


def test_persistence_across_simulated_restart(isolated_state: Path) -> None:
    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid, "ready", command="uv sync", exit_code=0, fingerprint=fp, project_root=container.root
    )
    record_verify_success(cid)

    # The persisted file exists on host
    store_file = isolated_state / f"{cid}.json"
    assert store_file.exists()

    # Simulate server restart by clearing in-memory dicts
    import sunaba.verify_state

    sunaba.verify_state._environment_state.clear()
    sunaba.verify_state._verify_map.clear()
    assert sunaba.verify_state._environment_state == {}
    assert has_verify_success(cid) is False

    # Reload from store (prune_and_load_environment_states with mock client)
    client = MagicMock()
    mock_c = MagicMock()
    mock_c.id = cid
    client.containers.list.return_value = [mock_c]

    loaded = prune_and_load_environment_states(client)
    assert cid in loaded
    assert loaded[cid]["status"] == "ready"
    assert loaded[cid]["fingerprint"] == fp

    # get_environment_state returns ready
    state = get_environment_state(cid)
    assert state is not None
    assert state["status"] == "ready"
    assert state["fingerprint"] == fp

    # _verify_map is NOT restored (must run verify again after restart)
    assert has_verify_success(cid) is False


def test_corrupt_or_missing_store_file_yields_not_prepared(isolated_state: Path) -> None:
    # Missing file returns None / not_prepared
    assert load_environment_state("non_existent_cid_99999") is None

    # Corrupt store file
    corrupt_cid = "corrupt_container_id_12345678"
    corrupt_file = isolated_state / f"{corrupt_cid}.json"
    corrupt_file.write_text("invalid json {{{", encoding="utf-8")

    state = load_environment_state(corrupt_cid)
    assert state is not None
    assert state["status"] == "not_prepared"

    # Server still starts (prune_and_load doesn't raise on corrupt file)
    client = MagicMock()
    mock_c = MagicMock()
    mock_c.id = corrupt_cid
    client.containers.list.return_value = [mock_c]

    loaded = prune_and_load_environment_states(client)
    assert loaded[corrupt_cid]["status"] == "not_prepared"


def test_startup_prunes_records_for_dead_containers(isolated_state: Path) -> None:
    alive_cid = "alive_container_id_111111111111"
    dead_cid = "dead_container_id_222222222222"

    save_environment_state(alive_cid, {"status": "ready", "command": "uv sync"})
    save_environment_state(dead_cid, {"status": "ready", "command": "uv sync"})

    assert (isolated_state / f"{alive_cid}.json").exists()
    assert (isolated_state / f"{dead_cid}.json").exists()

    # Container runtime reports only alive_cid
    client = MagicMock()
    c_alive = MagicMock()
    c_alive.id = alive_cid
    client.containers.list.return_value = [c_alive]

    prune_and_load_environment_states(client)

    # dead_cid was pruned from host store
    assert (isolated_state / f"{dead_cid}.json").exists() is False
    # alive_cid was retained
    assert (isolated_state / f"{alive_cid}.json").exists() is True


def test_lookup_by_full_container_id_no_prefix_match(isolated_state: Path) -> None:
    cid_full_a = "123456789012aaaa3333444455556666"
    cid_full_b = "123456789012bbbb3333444455556666"
    prefix_12 = "123456789012"

    record_environment_state(cid_full_a, "ready", command="uv sync", exit_code=0)

    # Exact full match succeeds
    state_a = get_environment_state(cid_full_a)
    assert state_a is not None
    assert state_a["status"] == "ready"

    # Container with shared prefix does NOT match
    assert get_environment_state(cid_full_b) is None

    # Prefix itself does NOT match
    assert get_environment_state(prefix_12) is None

    # check_uv_environment does not match container with shared prefix
    container_b = MockFilesystemContainer(cid_full_b, "/workspace", {})
    status, reason = check_uv_environment(container_b, "/workspace")
    assert status == "not_uv"

    container_prefix = MockFilesystemContainer(prefix_12, "/workspace", {})
    status, reason = check_uv_environment(container_prefix, "/workspace")
    container_prefix = MockFilesystemContainer(prefix_12, "/workspace", {})
    status, reason = check_uv_environment(container_prefix, "/workspace")
    assert status == "not_uv"


def test_container_prepared_with_short_id_found_by_full_id_and_isolated_from_prefix_sibling(
    isolated_state: Path,
) -> None:
    from docker.errors import NotFound

    cid_full_a = "123456789012aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    cid_full_b = "123456789012bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    prefix_12 = "123456789012"

    container_a, files_a = _make_ready_container(cid=cid_full_a)
    container_b, files_b = _make_ready_container(cid=cid_full_b)

    client = MagicMock()

    def get_container(cid_arg: str):
        if cid_arg in (prefix_12, cid_full_a):
            return container_a
        if cid_arg == cid_full_b:
            return container_b
        raise NotFound(f"No such container: {cid_arg}")

    client.containers.get.side_effect = get_container

    # Prepare container with 12-character ID argument
    fp_a = compute_manifest_fingerprint(container_a, container_a.root)
    with patch("sunaba.tools.common._docker", return_value=client):
        record_environment_state(
            prefix_12,
            "ready",
            command="uv sync --locked",
            exit_code=0,
            fingerprint=fp_a,
            project_root=container_a.root,
        )

    # State is recorded under the full 64-char ID
    state_a = get_environment_state(cid_full_a)
    assert state_a is not None
    assert state_a["status"] == "ready"
    assert get_environment_state(prefix_12) is None
    # Container B with shared prefix has NO record
    assert get_environment_state(cid_full_b) is None

    # Found by verify_in_container using the full ID
    with patch("sunaba.tools.verify._docker", return_value=client):
        res_a = json.loads(verify_in_container(container_id=cid_full_a, path=container_a.root))
        assert res_a["gate_passed"] is True

    # Also found by verify_in_container using the short 12-character ID
    with patch("sunaba.tools.verify._docker", return_value=client):
        res_short = json.loads(verify_in_container(container_id=prefix_12, path=container_a.root))
        assert res_short["gate_passed"] is True

    # Record for A is NOT returned for container B that shares the prefix
    with patch("sunaba.tools.verify._docker", return_value=client):
        res_b = json.loads(verify_in_container(container_id=cid_full_b, path=container_b.root))
        assert res_b["status"] == "environment_setup_failed"
        assert res_b["environment_setup"]["status"] == "not_prepared"

    # Container A verified passes verify_gate in publish using full ID
    with patch("sunaba.tools.vcs.publishing._docker", return_value=client):
        pub_a = json.loads(
            publish(
                container_id=cid_full_a,
                repo="owner/repo",
                branch="feature",
                message="msg",
                skip_verify_gate=False,
            )
        )
        assert pub_a.get("step") != "verify_gate"

    # Record B as failed to prove B's lookup is isolated from A's ready record
    record_environment_state(
        cid_full_b, "failed", reason="preparation failed in B"
    )
    # Container B publish is refused by verify_gate
    with patch("sunaba.tools.vcs.publishing._docker", return_value=client):
        pub_b = json.loads(
            publish(
                container_id=cid_full_b,
                repo="owner/repo",
                branch="feature",
                message="msg",
                skip_verify_gate=False,
            )
        )
        assert pub_b["status"] == "error"
        assert pub_b["step"] == "verify_gate"
        assert "uv environment is failed: preparation failed in B" in pub_b["error"]


def test_staleness_on_deleting_uv_lock_and_recovery(isolated_state: Path) -> None:
    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=fp, project_root=container.root
    )
    record_verify_success(cid)
    assert has_verify_success(cid) is True

    # Initial check passes
    status, reason = check_uv_environment(container, container.root)
    assert status == "ready"
    assert reason is None
    assert uv_environment_block_reason(container, container.root) is None

    # Delete uv.lock
    original_uv_lock = files.pop(f"{container.root}/uv.lock")

    # verify check and block reason report stale naming uv.lock
    status, reason = check_uv_environment(container, container.root)
    assert status == "stale"
    assert reason is not None
    assert "uv.lock" in reason
    assert "recreate container" in reason
    assert has_verify_success(cid) is False
    block_reason = uv_environment_block_reason(container, container.root)
    assert block_reason is not None
    assert "stale" in block_reason
    assert "uv.lock" in block_reason

    # Stored state in memory and store remains "ready"
    stored = get_environment_state(cid)
    assert stored is not None
    assert stored["status"] == "ready"

    client = _make_client_mock(cast(Any, container))

    # verify_in_container reports stale naming uv.lock and gate does not run
    with patch("sunaba.tools.verify._docker", return_value=client):
        res_json = verify_in_container(container_id=cid, path=container.root)
        res = json.loads(res_json)
        assert res["status"] == "environment_setup_failed"
        assert res["gate_passed"] is False
        assert res["environment_setup"]["status"] == "stale"
        assert "uv.lock" in res["environment_setup"]["reason"]
        assert res["lint"]["status"] == "skipped"
        assert res["tests"]["status"] == "skipped"

    # Publish check is refused naming uv.lock
    with patch("sunaba.tools.vcs.publishing._docker", return_value=client):
        pub_json = publish(
            container_id=cid,
            repo="owner/repo",
            branch="feature",
            message="msg",
            skip_verify_gate=False,
        )
        pub = json.loads(pub_json)
        assert pub["status"] == "error"
        assert pub["step"] == "verify_gate"
        assert "stale" in pub["error"]
        assert "uv.lock" in pub["error"]

    # Restore uv.lock to original content
    files[f"{container.root}/uv.lock"] = original_uv_lock
    # Status returns to ready without re-preparing
    status, reason = check_uv_environment(container, container.root)
    assert status == "ready"
    assert reason is None
    assert uv_environment_block_reason(container, container.root) is None

    # verify passes again after restore
    with patch("sunaba.tools.verify._docker", return_value=client):
        recovered_json = verify_in_container(container_id=cid, path=container.root)
        recovered = json.loads(recovered_json)
        assert recovered["gate_passed"] is True
        assert has_verify_success(cid) is True

    # publish passes verify gate again after restore
    with patch("sunaba.tools.vcs.publishing._docker", return_value=client):
        pub_rec_json = publish(
            container_id=cid,
            repo="owner/repo",
            branch="feature",
            message="msg",
            skip_verify_gate=False,
        )
        pub_rec = json.loads(pub_rec_json)
        assert pub_rec.get("step") != "verify_gate"


def test_failing_cat_is_read_failure_not_absent(isolated_state: Path) -> None:
    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid, "ready", command="uv sync", exit_code=0, fingerprint=fp, project_root=container.root
    )
    record_verify_success(cid)
    assert has_verify_success(cid) is True

    # Simulate cat failing with exit 1 even though file exists
    container.cat_read_failure = "cat: /workspace/uv.lock: Permission denied"

    status, reason = check_uv_environment(container, container.root)
    assert status == "failed"
    assert reason is not None
    assert "fingerprint probe failed" in reason
    assert has_verify_success(cid) is False

    # Stored status stays ready
    stored = get_environment_state(cid)
    assert stored is not None
    assert stored["status"] == "ready"

    # Verify also stops with failed
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res = json.loads(verify_in_container(container_id=cid, path=container.root))
        assert res["status"] == "environment_setup_failed"
        assert res["environment_setup"]["status"] == "failed"
        assert "fingerprint probe failed" in res["environment_setup"]["reason"]


@pytest.mark.parametrize("workdir", ["/workspace/sub", "/workspace/pkg"])
def test_recorded_environment_is_not_stale_from_subdirectory(
    isolated_state: Path, workdir: str
) -> None:
    container, files = _make_ready_container()
    if workdir.endswith("/pkg"):
        files[f"{container.root}/pkg/pyproject.toml"] = b"[project]\nname='nested'\n"
        files[f"{container.root}/pkg/uv.lock"] = b"version = 1\n"
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=fp,
        project_root=container.root,
    )

    status, reason = check_uv_environment(container, workdir)
    if workdir.endswith("/pkg"):
        assert (status, reason) == ("ready", None)
    else:
        assert (status, reason) == ("not_uv", None)
    assert uv_environment_block_reason(container, workdir) is None


def test_recorded_root_change_is_stale_from_any_workdir(isolated_state: Path) -> None:
    container, files = _make_ready_container()
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=fp,
        project_root=container.root,
    )
    files[f"{container.root}/uv.lock"] = b"version = 1\nrevision = changed\n"

    # Stale detection applies when caller root is a uv project or the root itself
    files[f"{container.root}/pkg/pyproject.toml"] = b"[project]\nname='nested'\n"
    files[f"{container.root}/pkg/uv.lock"] = b"version = 1\n"
    for workdir in (container.root, f"{container.root}/pkg"):
        status, reason = check_uv_environment(container, workdir)
        assert status == "stale"
        assert reason is not None and "uv.lock" in reason

    # Non-uv subdirectory is not blocked (not_uv)
    status, reason = check_uv_environment(container, f"{container.root}/sub")
    assert (status, reason) == ("not_uv", None)
    assert uv_environment_block_reason(container, f"{container.root}/sub") is None


@pytest.mark.parametrize("recorded_status", ["failed", "not_prepared"])
def test_non_uv_subdir_not_blocked_when_recorded_state_is_failed_or_not_prepared(
    isolated_state: Path, recorded_status: str
) -> None:
    container, files = _make_ready_container()
    reason = "sync_failed: exit 1" if recorded_status == "failed" else "preparation state missing"
    record_environment_state(
        container.id,
        recorded_status,
        reason=reason,
        command="uv sync",
        project_root=container.root,
    )
    workdir = f"{container.root}/sub"
    status, reason_out = check_uv_environment(container, workdir)
    assert status == "not_uv"
    assert reason_out is None
    assert uv_environment_block_reason(container, workdir) is None


def test_non_uv_subdir_not_blocked_when_recorded_state_is_ready(
    isolated_state: Path,
) -> None:
    container, files = _make_ready_container()
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=fp,
        project_root=container.root,
    )
    workdir = f"{container.root}/sub"
    status, reason = check_uv_environment(container, workdir)
    assert status == "not_uv"
    assert reason is None
    assert uv_environment_block_reason(container, workdir) is None


def test_prepared_root_itself_blocked_when_recorded_state_is_failed(
    isolated_state: Path,
) -> None:
    container, files = _make_ready_container()
    record_environment_state(
        container.id,
        "failed",
        reason="sync_failed: exit 1",
        command="uv sync",
        project_root=container.root,
    )
    status, reason = check_uv_environment(container, container.root)
    assert status == "failed"
    assert reason == "sync_failed: exit 1"
    block_reason = uv_environment_block_reason(container, container.root)
    assert block_reason is not None
    assert "uv environment failed: sync_failed: exit 1" in block_reason


def test_differing_caller_root_probe_failure_returns_failed(
    isolated_state: Path,
) -> None:
    container, files = _make_ready_container()
    record_environment_state(
        container.id,
        "ready",
        command="uv sync",
        exit_code=0,
        project_root=container.root,
    )
    container.probe_failure = "sh: 1: probe failed: permission denied"
    status, reason = check_uv_environment(container, f"{container.root}/sub")
    assert status == "failed"
    assert reason is not None and "uv root probe failed" in reason
    block_reason = uv_environment_block_reason(container, f"{container.root}/sub")
    assert block_reason is not None
    assert "failed" in block_reason


def test_legacy_record_without_project_root_non_uv_subdir_not_blocked(
    isolated_state: Path,
) -> None:
    container, files = _make_ready_container()
    record_environment_state(
        container.id,
        "failed",
        reason="preparation state missing",
    )
    status, reason = check_uv_environment(container, f"{container.root}/sub")
    assert status == "not_uv"
    assert reason is None
    assert uv_environment_block_reason(container, f"{container.root}/sub") is None


def test_legacy_record_without_project_root_prepared_root_blocked(
    isolated_state: Path,
) -> None:
    container, files = _make_ready_container()
    record_environment_state(
        container.id,
        "failed",
        reason="sync_failed: exit 1",
    )
    status, reason = check_uv_environment(container, container.root)
    assert status == "failed"
    assert reason == "sync_failed: exit 1"
    block_reason = uv_environment_block_reason(container, container.root)
    assert block_reason is not None
    assert "uv environment failed: sync_failed: exit 1" in block_reason


def test_environment_store_failures_are_logged_and_do_not_raise(
    isolated_state: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import sunaba.environment_state_store as store

    with patch.object(store.os, "replace", side_effect=OSError("replace denied")):
        save_environment_state("save_failure", {"status": "ready"})
    assert "Could not save environment state" in caplog.text

    store_file = isolated_state / "remove_failure.json"
    store_file.write_text("{}", encoding="utf-8")
    with patch.object(Path, "unlink", side_effect=OSError("unlink denied")):
        remove_environment_state("remove_failure")
    assert "Could not remove environment state" in caplog.text


def test_fingerprint_read_failure_after_sync_is_recorded_as_fingerprint_failure(
    isolated_state: Path,
) -> None:
    from sunaba.tools.container import clone

    container = MagicMock()
    container.id = "fingerprint_read_failure"
    with (
        patch.object(clone, "_active_foreground_scope", return_value=object()),
        patch.object(clone, "_detect_manifests", return_value={"pyproject.toml", "uv.lock"}),
        patch.object(clone, "_exec_run_with_deadline", return_value=(0, (b"", b""))),
        patch(
            "sunaba.environment_state_store.compute_manifest_fingerprint",
            side_effect=RuntimeError("manifest read denied"),
        ),
    ):
        result = clone._install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )

    state = get_environment_state(container.id)
    assert result.failed is True
    assert state is not None
    assert state["status"] == "failed"
    assert state["reason"].startswith("fingerprint_read_failed:")
    assert state["project_root"] == "/workspace"


@pytest.mark.parametrize(
    ("prep_kwargs", "exec_result", "expected_reason_prefix"),
    [
        ({"allow_network": False}, None, "network_disabled"),
        ({"pip_extras": "[unsupported]"}, None, "unsupported_uv_arguments"),
        ({}, (1, (b"", b"uv sync error")), "sync_failed: uv sync error"),
        ({}, RuntimeError("connection lost"), "sync_error: connection lost"),
    ],
)
def test_failed_uv_preparation_records_project_root_equal_to_prepared_root(
    isolated_state: Path,
    prep_kwargs: dict[str, Any],
    exec_result: Any,
    expected_reason_prefix: str,
) -> None:
    from sunaba.tools.container import clone

    container = MagicMock()
    container.id = "failed_prep_project_root"
    prepared_root = "/workspace/my_project"
    exec_side_effect: Any
    if isinstance(exec_result, Exception):
        exec_side_effect = exec_result
    elif exec_result is not None:
        def _exec_fn(*args: Any, **kwargs: Any) -> Any:
            return exec_result

        exec_side_effect = _exec_fn
    else:
        exec_side_effect = None
    with (
        patch.object(clone, "_active_foreground_scope", return_value=object()),
        patch.object(clone, "_detect_manifests", return_value={"pyproject.toml", "uv.lock"}),
        patch.object(clone, "_exec_run_with_deadline", side_effect=exec_side_effect)
        if exec_side_effect
        else patch.object(clone, "_exec_run_with_deadline", return_value=(0, (b"", b""))),
    ):
        result = clone._install_repo_deps(
            container, "owner/repo", prepared_root, **prep_kwargs
        )

    state = get_environment_state(container.id)
    assert result.failed is True
    assert state is not None
    assert state["status"] == "failed"
    assert state["project_root"] == prepared_root
    assert expected_reason_prefix in state["reason"]


@pytest.mark.parametrize("recorded_status", ["failed", "stale"])
def test_verify_non_uv_working_dir_not_blocked_by_recorded_state_failure_or_staleness(
    isolated_state: Path, recorded_status: str
) -> None:
    container, files = _make_ready_container()
    cid = container.id
    prepared_root = container.root
    non_uv_workdir = f"{container.root}/sub"

    if recorded_status == "failed":
        record_environment_state(
            cid, "failed", reason="sync_failed: exit 1", command="uv sync", project_root=prepared_root
        )
    else:
        fp = compute_manifest_fingerprint(container, prepared_root)
        record_environment_state(
            cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=fp, project_root=prepared_root
        )
        files[f"{prepared_root}/uv.lock"] = b"version = 1\nrevision = 2 # modified\n"

    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res = json.loads(
            verify_in_container(container_id=cid, path="tests/", working_dir=non_uv_workdir)
        )
        assert res["status"] != "environment_setup_failed"
        assert "environment_setup" not in res.get("gate_fail_kinds", [])


@pytest.mark.parametrize("recorded_status", ["failed", "stale"])
def test_verify_prepared_root_working_dir_blocked_by_recorded_state_failure_or_staleness(
    isolated_state: Path, recorded_status: str
) -> None:
    container, files = _make_ready_container()
    cid = container.id
    prepared_root = container.root

    if recorded_status == "failed":
        expected_reason = "sync_failed: exit 1"
        record_environment_state(
            cid, "failed", reason=expected_reason, command="uv sync", project_root=prepared_root
        )
    else:
        fp = compute_manifest_fingerprint(container, prepared_root)
        record_environment_state(
            cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=fp, project_root=prepared_root
        )
        files[f"{prepared_root}/uv.lock"] = b"version = 1\nrevision = 2 # modified\n"
        expected_reason = "uv.lock"

    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res = json.loads(
            verify_in_container(container_id=cid, path=prepared_root, working_dir=prepared_root)
        )
        assert res["status"] == "environment_setup_failed"
        assert res["gate_passed"] is False
        assert res["environment_setup"]["status"] == recorded_status
        assert expected_reason in res["environment_setup"]["reason"]


def test_verify_nested_uv_workdir_resolves_tools_from_working_dir(isolated_state: Path) -> None:
    """A nested uv project as working_dir looks up tools there, not at the prepared root (main behaviour)."""
    from sunaba import environment as env_mod

    container, files = _make_ready_container()
    cid = container.id
    prepared_root = container.root
    nested = f"{prepared_root}/pkg"
    files[f"{nested}/pyproject.toml"] = b"[project]\nname='nested'\n"
    files[f"{nested}/uv.lock"] = b"version = 1\n"
    fp = compute_manifest_fingerprint(container, prepared_root)
    record_environment_state(
        cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=fp, project_root=prepared_root
    )

    real_resolve = env_mod.resolve_uv_python_tools
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        with patch.object(env_mod, "resolve_uv_python_tools", side_effect=real_resolve) as resolve:
            res = json.loads(verify_in_container(container_id=cid, path=nested, working_dir=nested))

    roots = [call.args[1] for call in resolve.call_args_list]
    assert roots, "resolve_uv_python_tools was not called"
    assert all(root == nested for root in roots), roots
    assert res.get("status") != "environment_setup_failed"

