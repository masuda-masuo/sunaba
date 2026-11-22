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
    save_environment_states,
)
from sunaba.tools.vcs.publishing import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import (
    get_environment_state,
    has_verify_success,
    list_environment_states,
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
        self.symlinks: dict[str, str] = {}
        self.absent_paths: set[str] = set()
        self.probe_failure: str | None = None
        self.fingerprint_probe_failure: str | None = None
        self.executed: list[str] = []
        self.cat_read_failure: str | None = None

    def _resolve_mock_path(self, path: str) -> str:
        current = "/" + "/".join(part for part in path.split("/") if part and part != ".")
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            replaced = False
            for link, target in sorted(self.symlinks.items(), key=lambda kv: len(kv[0]), reverse=True):
                norm_link = link.rstrip("/")
                norm_target = target.rstrip("/")
                if current == norm_link:
                    current = norm_target
                    replaced = True
                    break
                if current.startswith(norm_link + "/"):
                    current = norm_target + current[len(norm_link):]
                    replaced = True
                    break
            if not replaced:
                break
        return current

    def exec_run(self, cmd: list[str] | str, **kwargs) -> tuple[int, tuple[bytes, bytes]]:
        cmd_str = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        self.executed.append(cmd_str)

        if self.probe_failure:
            return 127, (b"", self.probe_failure.encode("utf-8"))

        if "__SUNABA_DIRECTORY_EXISTS__" in cmd_str:
            directory = cmd_str.split("[ -d ", 1)[1].split(" ];", 1)[0].strip().strip("'\\\"")
            directory = self._resolve_mock_path(directory).rstrip("/")
            exists = directory not in self.absent_paths and any(
                path.startswith(directory + "/") for path in self.files
            )
            marker = (
                b"__SUNABA_DIRECTORY_EXISTS__\n"
                if exists
                else b"__SUNABA_DIRECTORY_ABSENT__\n"
            )
            return 0, (marker, b"")

        if "__SUNABA_PATH_OK__" in cmd_str:
            matches = re.findall(r"_sunaba_p=(?:'([^']+)'|\"([^\"]+)\"|(\S+));", cmd_str)
            lines: list[bytes] = []
            for idx, groups in enumerate(matches):
                raw_path = groups[0] or groups[1] or groups[2]
                norm_path = raw_path.rstrip("/") or "/"
                if norm_path in self.absent_paths:
                    lines.append(f"__SUNABA_PATH_ABSENT__{idx}".encode("utf-8"))
                else:
                    resolved = self._resolve_mock_path(norm_path)
                    if resolved in self.absent_paths:
                        lines.append(f"__SUNABA_PATH_ABSENT__{idx}".encode("utf-8"))
                    else:
                        lines.append(f"__SUNABA_PATH_OK__{idx}:{resolved}".encode("utf-8"))
            out = b"\n".join(lines) + (b"\n" if lines else b"")
            return 0, (out, b"")

        cd_match = re.search(r"cd\s+(?:'([^']+)'|\"([^\"]+)\"|(\S+))\s+&&", cmd_str)
        raw_dir = (
            (cd_match.group(1) or cd_match.group(2) or cd_match.group(3)).rstrip("/")
            if cd_match
            else self.root
        )
        raw_dir = self._resolve_mock_path(raw_dir)
        curr_dir = self.root if raw_dir in ("/home/sandbox", self.root) else raw_dir

        if "uv workspace dir" in cmd_str:
            if f"{curr_dir}/pyproject.toml" in self.files:
                return 0, (f"{curr_dir}\n".encode("utf-8"), b"")
            return 2, (b"", b"error: No `pyproject.toml` found in current directory or any parent directory\n")

        # Manifest probe loop (probe_uv_project_root)
        if "for f in pyproject.toml uv.lock" in cmd_str:
            lines = []
            for fname in ("pyproject.toml", "uv.lock"):
                full_path = f"{curr_dir}/{fname}"
                if full_path in self.files:
                    lines.append(fname.encode("utf-8"))
            if "__SUNABA_MANIFEST_PROBE_DONE__" in cmd_str:
                lines.append(b"__SUNABA_MANIFEST_PROBE_DONE__")
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

    save_environment_states(alive_cid, {"": {"status": "ready", "command": "uv sync"}})
    save_environment_states(dead_cid, {"": {"status": "ready", "command": "uv sync"}})

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
def test_recorded_environment_is_not_stale_from_subdirectory_and_unprepared_nested_root(
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
        assert status == "not_prepared"
        assert "/workspace/pkg" in (reason or "")
        assert "sandbox_prepare_environment(container_id, working_dir=" in (reason or "")
        assert uv_environment_block_reason(container, workdir) is not None
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

    # The recorded root remains stale, while an independent nested root has
    # no preparation record of its own.
    status, reason = check_uv_environment(container, container.root)
    assert status == "stale"
    assert reason is not None and "uv.lock" in reason

    nested = f"{container.root}/pkg"
    files[f"{nested}/pyproject.toml"] = b"[project]\nname='nested'\n"
    files[f"{nested}/uv.lock"] = b"version = 1\n"
    status, reason = check_uv_environment(container, nested)
    assert status == "not_prepared"
    assert nested in (reason or "")
    assert "sandbox_prepare_environment(container_id, working_dir=" in (reason or "")

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
        save_environment_states("save_failure", {"": {"status": "ready"}})
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


def test_verify_nested_uv_workdir_requires_its_own_preparation(isolated_state: Path) -> None:
    """An independent nested uv root needs its own record before tool lookup."""
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
            unprepared = json.loads(
                verify_in_container(container_id=cid, path=nested, working_dir=nested)
            )
            assert unprepared["status"] == "environment_setup_failed"
            assert nested in unprepared["environment_setup"]["reason"]
            assert "sandbox_prepare_environment(container_id, working_dir=" in (
                unprepared["environment_setup"]["reason"]
            )
            assert resolve.call_args_list == []

            nested_fp = compute_manifest_fingerprint(container, nested)
            record_environment_state(
                cid,
                "ready",
                command="uv sync --locked",
                exit_code=0,
                fingerprint=nested_fp,
                project_root=nested,
            )
            prepared = json.loads(
                verify_in_container(container_id=cid, path=nested, working_dir=nested)
            )

    roots = [call.args[1] for call in resolve.call_args_list]
    assert roots, "resolve_uv_python_tools was not called after nested preparation"
    assert all(root == nested for root in roots), roots
    assert prepared.get("status") != "environment_setup_failed"


def test_descendant_missing_uv_lock_blocks_helper_runners_and_verify(
    isolated_state: Path,
) -> None:
    """Recorded ready root retaining pyproject.toml without uv.lock blocks descendants."""
    from sunaba.edit_verify.lint_runners import _run_pyright_verify, _run_ruff_verify
    from sunaba.edit_verify.test_runners import _run_pytest_verify

    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=fp,
        project_root=container.root,
    )
    record_verify_success(cid)
    assert has_verify_success(cid) is True

    original_uv_lock = files.pop(f"{container.root}/uv.lock")
    descendant = f"{container.root}/sub/pkg"

    status, reason = check_uv_environment(container, descendant)
    assert status == "stale"
    assert reason is not None and "uv.lock" in reason
    assert has_verify_success(cid) is False
    stored = get_environment_state(cid)
    assert stored is not None and stored["status"] == "ready"

    block_reason = uv_environment_block_reason(container, descendant)
    assert block_reason is not None
    assert "stale" in block_reason
    assert "uv.lock" in block_reason

    container.executed.clear()
    ruff_res = _run_ruff_verify(container, "src", workdir=descendant)
    assert ruff_res.status == "error"
    assert "stale" in ruff_res.detail and "uv.lock" in ruff_res.detail

    pyright_res = _run_pyright_verify(container, "src", workdir=descendant)
    assert pyright_res.status == "error"
    assert "stale" in pyright_res.detail and "uv.lock" in pyright_res.detail

    pytest_desc_res = _run_pytest_verify(container, "tests/", workdir=descendant)
    assert pytest_desc_res.status == "error"
    assert "stale" in pytest_desc_res.detail and "uv.lock" in pytest_desc_res.detail

    pytest_root_res = _run_pytest_verify(container, "tests/", workdir=container.root)
    assert pytest_root_res.status == "error"
    assert "stale" in pytest_root_res.detail and "uv.lock" in pytest_root_res.detail

    assert not any(
        "ruff check" in cmd or "pyright --outputjson" in cmd or "--junit-xml" in cmd
        for cmd in container.executed
    )

    record_verify_success(cid)
    container.executed.clear()
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        res = json.loads(
            verify_in_container(container_id=cid, path=descendant, working_dir=descendant)
        )
    assert res["status"] == "environment_setup_failed"
    assert res["gate_passed"] is False
    assert res["environment_setup"]["status"] == "stale"
    assert "uv.lock" in res["environment_setup"]["reason"]
    assert res["lint"]["status"] == "skipped"
    assert res["types"]["status"] == "skipped"
    assert res["patch_targets"]["status"] == "skipped"
    assert res["tests"]["status"] == "skipped"
    assert has_verify_success(cid) is False
    stored_after = get_environment_state(cid)
    assert stored_after is not None and stored_after["status"] == "ready"
    assert not any(
        "ruff check" in cmd or "pyright --outputjson" in cmd or "--junit-xml" in cmd
        for cmd in container.executed
    )

    files[f"{container.root}/uv.lock"] = original_uv_lock
    assert check_uv_environment(container, descendant) == ("not_uv", None)


def test_nested_independent_uv_root_wins_over_ancestor_missing_uv_lock(
    isolated_state: Path,
) -> None:
    """An independently resolved nested uv root keeps its own readiness rules."""
    container, files = _make_ready_container()
    cid = container.id
    parent_fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=parent_fp,
        project_root=container.root,
    )

    nested = f"{container.root}/pkg"
    files[f"{nested}/pyproject.toml"] = b"[project]\nname='nested'\n"
    files[f"{nested}/uv.lock"] = b"version = 1\n"
    files.pop(f"{container.root}/uv.lock")

    status, reason = check_uv_environment(container, nested)
    assert status == "not_prepared"
    assert nested in (reason or "")

    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        unprepared = json.loads(
            verify_in_container(container_id=cid, path=nested, working_dir=nested)
        )
        assert unprepared["status"] == "environment_setup_failed"
        assert unprepared["environment_setup"]["status"] == "not_prepared"
        assert nested in unprepared["environment_setup"]["reason"]

        nested_fp = compute_manifest_fingerprint(container, nested)
        record_environment_state(
            cid,
            "ready",
            command="uv sync --locked",
            exit_code=0,
            fingerprint=nested_fp,
            project_root=nested,
        )
        assert check_uv_environment(container, nested) == ("ready", None)
        prepared = json.loads(
            verify_in_container(container_id=cid, path=nested, working_dir=nested)
        )
        assert prepared.get("status") != "environment_setup_failed"


def test_overlapping_recorded_ancestors_select_deepest_and_match_path_components(
    isolated_state: Path,
) -> None:
    """Deepest path-component ancestor governs when resolution yields None."""
    container, files = _make_ready_container()
    cid = container.id
    nested = f"{container.root}/pkg"
    files[f"{nested}/pyproject.toml"] = b"[project]\nname='nested'\n"
    files[f"{nested}/uv.lock"] = b"version = 1\n"

    parent_fp = compute_manifest_fingerprint(container, container.root)
    nested_fp = compute_manifest_fingerprint(container, nested)
    record_environment_state(
        cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=parent_fp, project_root=container.root
    )
    record_environment_state(
        cid, "ready", command="uv sync --locked", exit_code=0, fingerprint=nested_fp, project_root=nested
    )

    # Outer loses uv.lock while nested stays locked:
    # - /workspace/pkg/sub sees deepest ancestor /workspace/pkg (still locked) -> not_uv
    # - /workspace/pkg2/sub does not match /workspace/pkg by path component -> checks /workspace -> stale
    # - /workspace-sibling/sub does not match /workspace by path component -> not_uv
    outer_lock = files.pop(f"{container.root}/uv.lock")
    assert check_uv_environment(container, f"{nested}/sub") == ("not_uv", None)

    status_prefix_sibling, reason_prefix_sibling = check_uv_environment(
        container, f"{container.root}/pkg2/sub"
    )
    assert status_prefix_sibling == "stale"
    assert reason_prefix_sibling is not None and "uv.lock" in reason_prefix_sibling

    assert check_uv_environment(container, f"{container.root}-sibling/sub") == ("not_uv", None)
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        sibling_verify = json.loads(
            verify_in_container(
                container_id=cid,
                path=f"{container.root}-sibling/sub",
                working_dir=f"{container.root}-sibling/sub",
            )
        )
        assert sibling_verify["status"] != "environment_setup_failed"

    # Restore outer lock and delete nested lock instead:
    # - /workspace/pkg/sub sees deepest ancestor /workspace/pkg -> stale
    files[f"{container.root}/uv.lock"] = outer_lock
    files.pop(f"{nested}/uv.lock")
    status_nested_desc, reason_nested_desc = check_uv_environment(container, f"{nested}/sub")
    assert status_nested_desc == "stale"
    assert reason_nested_desc is not None and "uv.lock" in reason_nested_desc


def test_non_member_beneath_still_locked_ancestor_and_member_descendant_missing_uv_lock(
    tmp_path: Path, isolated_state: Path
) -> None:
    """Real uv workspace: non-member stays not_uv while locked; member blocks after lock loss."""
    import subprocess

    from sunaba.edit_verify.test_runners import _run_pytest_verify
    from tests.test_issue972_per_root_state import LocalExecContainer

    ws_dir = tmp_path / "ws"
    member_src = ws_dir / "packages" / "a" / "src" / "pkg_a"
    member_src.mkdir(parents=True)
    (ws_dir / "pyproject.toml").write_text(
        '[project]\nname = "ws"\nversion = "0.1.0"\ndependencies = []\n\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n',
        encoding="utf-8",
    )
    (ws_dir / "packages" / "a" / "pyproject.toml").write_text(
        '[project]\nname = "pkg-a"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (member_src / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    non_member = ws_dir / "non_member"
    non_member.mkdir()
    (non_member / "pyproject.toml").write_text(
        '[project]\nname = "non-member"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock"], cwd=ws_dir, check=True, capture_output=True)

    container = LocalExecContainer(ws_dir)
    fp = compute_manifest_fingerprint(container, str(ws_dir))
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=0,
        fingerprint=fp,
        project_root=str(ws_dir),
    )

    # While ws_dir is still locked, non-member stays not_uv
    assert check_uv_environment(container, str(non_member)) == ("not_uv", None)
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        non_member_res = json.loads(
            verify_in_container(
                container_id=container.id, path=".", working_dir=str(non_member)
            )
        )
        assert non_member_res["status"] != "environment_setup_failed"

    # Deleting workspace uv.lock blocks member and member subdirectory descendants
    (ws_dir / "uv.lock").unlink()
    for target in (str(ws_dir / "packages" / "a"), str(member_src)):
        status, reason = check_uv_environment(container, target)
        assert status == "stale"
        assert reason is not None and "uv.lock" in reason
        pytest_res = _run_pytest_verify(container, "tests/", workdir=target)
        assert pytest_res.status == "error"
        assert "stale" in pytest_res.detail and "uv.lock" in pytest_res.detail
        with patch("sunaba.tools.verify._docker", return_value=client):
            verify_res = json.loads(
                verify_in_container(
                    container_id=container.id, path=".", working_dir=target
                )
            )
            assert verify_res["status"] == "environment_setup_failed"
            assert verify_res["environment_setup"]["status"] == "stale"
            assert "uv.lock" in verify_res["environment_setup"]["reason"]


def test_descendant_ancestor_manifest_probe_failure_fails_closed(
    isolated_state: Path,
) -> None:
    """When probing the recorded ancestor fails from a descendant, checks fail closed."""
    from sunaba.edit_verify.lint_runners import _run_pyright_verify, _run_ruff_verify
    from sunaba.edit_verify.test_runners import _run_pytest_verify

    container, files = _make_ready_container()
    cid = container.id
    fp = compute_manifest_fingerprint(container, container.root)
    record_environment_state(
        cid,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=fp,
        project_root=container.root,
    )
    record_verify_success(cid)
    descendant = f"{container.root}/sub"
    files.pop(f"{container.root}/uv.lock")

    with patch(
        "sunaba.environment.probe_uv_project_root",
        side_effect=RuntimeError("ancestor manifest probe transport failure"),
    ):
        status, reason = check_uv_environment(container, descendant)
        assert status == "failed"
        assert reason is not None and "ancestor manifest probe transport failure" in reason
        assert has_verify_success(cid) is False
        assert uv_environment_block_reason(container, descendant) is not None

        for runner_res in (
            _run_ruff_verify(container, "src", workdir=descendant),
            _run_pyright_verify(container, "src", workdir=descendant),
            _run_pytest_verify(container, "tests/", workdir=descendant),
        ):
            assert runner_res.status == "error"
            assert "failed" in runner_res.detail

        record_verify_success(cid)
        client = _make_client_mock(cast(Any, container))
        with patch("sunaba.tools.verify._docker", return_value=client):
            res = json.loads(
                verify_in_container(
                    container_id=cid, path=descendant, working_dir=descendant
                )
            )
        assert res["status"] == "environment_setup_failed"
        assert res["gate_passed"] is False
        assert res["environment_setup"]["status"] == "failed"
        assert "ancestor manifest probe transport failure" in res["environment_setup"]["reason"]
        assert res["lint"]["status"] == "skipped"
        assert res["tests"]["status"] == "skipped"
        assert has_verify_success(cid) is False
        stored = get_environment_state(cid)
        assert stored is not None and stored["status"] == "ready"


class _RecordedLocalContainer:
    """Real subprocess container tracking executed commands and Docker WorkingDir attrs."""

    def __init__(self, root: Path, cid: str = "review-local-container-id") -> None:
        self.id = cid
        self.root = str(root)
        self.attrs = {"Config": {"WorkingDir": str(root)}}
        self.executed: list[str] = []

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
        import os
        import subprocess

        argv = list(cmd) if isinstance(cmd, (list, tuple)) else ["/bin/sh", "-c", str(cmd)]
        self.executed.append(argv[-1] if argv else "")
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


def _install_local_venv_tools(root: Path) -> None:
    """Populate *root*/.venv/bin with working python, ruff, and pyright executables."""
    import shlex
    import shutil
    import sys

    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    python_bin = venv_bin / "python"
    if not python_bin.exists():
        # Wrapper script preserves sys.executable's own pyvenv.cfg lookup.
        python_bin.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        python_bin.chmod(0o755)

    ruff_real = shutil.which("ruff")
    ruff_bin = venv_bin / "ruff"
    if not ruff_bin.exists():
        if ruff_real:
            ruff_bin.symlink_to(ruff_real)
        else:
            ruff_bin.write_text(
                "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'ruff 0.17.0'; exit 0; fi\necho '[]'\n",
                encoding="utf-8",
            )
            ruff_bin.chmod(0o755)

    pyright_bin = venv_bin / "pyright"
    if not pyright_bin.exists():
        pyright_bin.write_text(
            "#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo 'pyright 1.1.400'; exit 0; fi\n"
            "echo '{\"generalDiagnostics\": []}'\n",
            encoding="utf-8",
        )
        pyright_bin.chmod(0o755)


def test_review_symlink_missing_uv_lock_blocks_and_respects_boundaries(
    tmp_path: Path, isolated_state: Path
) -> None:
    """External symlinks into a recorded root's child reject with stale after uv.lock removal."""
    from sunaba.edit_verify.lint_runners import _run_pyright_verify, _run_ruff_verify
    from sunaba.edit_verify.test_runners import _run_pytest_verify
    from sunaba.tools.verify import lint_in_container, type_check_in_container

    proj = tmp_path / "proj"
    child = proj / "child"
    child_pkg = child / "pkg"
    child_pkg.mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "proj"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (child_pkg / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (child / "tests").mkdir()
    (child / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    _install_local_venv_tools(proj)

    outside_dir = tmp_path / "outside_dir"
    (outside_dir / "tests").mkdir(parents=True)
    (outside_dir / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    (outside_dir / "tests" / "test_out.py").write_text(
        "def test_out():\n    assert True\n", encoding="utf-8"
    )

    outside_link = tmp_path / "outside_link"
    outside_link.symlink_to(child)

    # Boundary case: symlink inside recorded project pointing OUT of the project
    out_link = proj / "out_link"
    out_link.symlink_to(outside_dir)

    container = _RecordedLocalContainer(proj, cid="review-symlink-cid")
    cid = container.id
    fp = compute_manifest_fingerprint(container, str(proj))
    record_environment_state(
        cid,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=fp,
        project_root=str(proj),
    )
    record_verify_success(cid)

    # While proj/uv.lock is present, plain subdir via outside symlink resolves to proj (ready),
    # while an out-of-project symlink stays not_uv.
    assert check_uv_environment(container, str(outside_link)) == ("ready", None)
    assert check_uv_environment(container, str(out_link)) == ("not_uv", None)

    # Remove only uv.lock (pyproject.toml remains)
    (proj / "uv.lock").unlink()

    status, reason = check_uv_environment(container, str(outside_link))
    assert status == "stale"
    assert reason is not None and "uv.lock" in reason
    assert has_verify_success(cid) is False
    stored = get_environment_state(cid)
    assert stored is not None and stored["status"] == "ready"

    block_reason = uv_environment_block_reason(container, str(outside_link))
    assert block_reason is not None and "stale" in block_reason and "uv.lock" in block_reason

    container.executed.clear()
    ruff_res = _run_ruff_verify(container, "pkg/mod.py", workdir=str(outside_link))
    assert ruff_res.status == "error"
    assert "stale" in ruff_res.detail and "uv.lock" in ruff_res.detail

    pyright_res = _run_pyright_verify(container, "pkg/mod.py", workdir=str(outside_link))
    assert pyright_res.status == "error"
    assert "stale" in pyright_res.detail and "uv.lock" in pyright_res.detail

    pytest_res = _run_pytest_verify(container, "tests/", workdir=str(outside_link))
    assert pytest_res.status == "error"
    assert "stale" in pytest_res.detail and "uv.lock" in pytest_res.detail

    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        lint_findings = json.loads(
            lint_in_container(container_id=cid, file_path=str(outside_link / "pkg" / "mod.py"))
        )
        assert any(
            item["rule"] == "error"
            and "stale" in item["message"]
            and "uv.lock" in item["message"]
            for item in lint_findings
        )
        type_findings = json.loads(
            type_check_in_container(container_id=cid, file_path=str(outside_link / "pkg" / "mod.py"))
        )
        assert any(
            item["rule"] == "error"
            and "stale" in item["message"]
            and "uv.lock" in item["message"]
            for item in type_findings
        )

        record_verify_success(cid)
        verify_res = json.loads(
            verify_in_container(
                container_id=cid, path="tests", working_dir=str(outside_link)
            )
        )

    assert verify_res["status"] == "environment_setup_failed"
    assert verify_res["gate_passed"] is False
    assert verify_res["environment_setup"]["status"] == "stale"
    assert "uv.lock" in verify_res["environment_setup"]["reason"]
    assert verify_res["lint"]["status"] == "skipped"
    assert verify_res["types"]["status"] == "skipped"
    assert verify_res["patch_targets"]["status"] == "skipped"
    assert verify_res["tests"]["status"] == "skipped"
    assert has_verify_success(cid) is False
    stored_after = get_environment_state(cid)
    assert stored_after is not None and stored_after["status"] == "ready"
    assert not any(
        "ruff check" in cmd or "pyright --outputjson" in cmd or "--junit-xml" in cmd
        for cmd in container.executed
    )

    # Boundary: symlink out of the recorded project must NOT be treated as a descendant
    assert check_uv_environment(container, str(out_link)) == ("not_uv", None)
    assert uv_environment_block_reason(container, str(out_link)) is None
    with patch("sunaba.tools.verify._docker", return_value=client):
        out_verify = json.loads(
            verify_in_container(
                container_id=cid, path="tests", working_dir=str(out_link)
            )
        )
    assert out_verify["status"] != "environment_setup_failed"

    # Physical resolution of recorded root aliases (alias -> real_proj)
    real_proj = tmp_path / "real_proj"
    real_child = real_proj / "sub"
    real_child.mkdir(parents=True)
    (real_proj / "pyproject.toml").write_text(
        '[project]\nname = "alias-proj"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (real_proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (real_proj / "tests").mkdir()
    (real_proj / "tests" / "test_alias.py").write_text(
        "def test_alias():\n    assert True\n", encoding="utf-8"
    )
    _install_local_venv_tools(real_proj)
    alias_root = tmp_path / "alias_root"
    alias_root.symlink_to(real_proj)

    alias_container = _RecordedLocalContainer(alias_root, cid="review-alias-cid")
    alias_fp = compute_manifest_fingerprint(alias_container, str(alias_root))
    record_environment_state(
        alias_container.id,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=alias_fp,
        project_root=str(alias_root),
    )
    assert check_uv_environment(alias_container, str(real_proj)) == ("ready", None)
    alias_client = _make_client_mock(cast(Any, alias_container))
    with patch("sunaba.tools.verify._docker", return_value=alias_client):
        locked_alias_res = json.loads(
            verify_in_container(
                container_id=alias_container.id,
                path="tests",
                working_dir=str(real_proj),
            )
        )
    assert locked_alias_res["gate_passed"] is True, locked_alias_res
    assert str(alias_root) in list_environment_states(alias_container.id)

    (real_proj / "uv.lock").unlink()
    alias_status, alias_reason = check_uv_environment(alias_container, str(real_child))
    assert alias_status == "stale"
    assert alias_reason is not None and "uv.lock" in alias_reason
    with patch("sunaba.tools.verify._docker", return_value=alias_client):
        stale_alias_res = json.loads(
            verify_in_container(
                container_id=alias_container.id,
                path=".",
                working_dir=str(real_child),
            )
        )
    assert stale_alias_res["status"] == "environment_setup_failed"
    assert stale_alias_res["environment_setup"]["status"] == "stale"
    assert "uv.lock" in stale_alias_res["environment_setup"]["reason"]
    assert list_environment_states(alias_container.id)[str(alias_root)]["status"] == "ready"


def test_review_symlink_workspace_precedence_errors_timeouts_and_absent_roots(
    tmp_path: Path, isolated_state: Path
) -> None:
    """Locked workspace via outside symlink resolves root; probe errors/timeouts fail closed."""
    import subprocess

    from sunaba.edit_verify.lint_runners import _run_ruff_verify
    from sunaba.edit_verify.shell import ForegroundTreeTimeout, VerifyTreeTimeout
    from sunaba.edit_verify.test_runners import _run_pytest_verify
    from sunaba.environment import EnvironmentResolutionError

    ws_dir = tmp_path / "ws"
    member_src = ws_dir / "packages" / "a" / "src" / "pkg_a"
    member_src.mkdir(parents=True)
    (ws_dir / "pyproject.toml").write_text(
        '[project]\nname = "ws"\nversion = "0.1.0"\ndependencies = []\n\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n',
        encoding="utf-8",
    )
    (ws_dir / "packages" / "a" / "pyproject.toml").write_text(
        '[project]\nname = "pkg-a"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (member_src / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    non_member = ws_dir / "non_member"
    non_member.mkdir()
    (non_member / "pyproject.toml").write_text(
        '[project]\nname = "non-member"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock"], cwd=ws_dir, check=True, capture_output=True)
    _install_local_venv_tools(ws_dir)

    outside_member_link = tmp_path / "outside_member_link"
    outside_member_link.symlink_to(member_src)
    outside_non_member_link = tmp_path / "outside_non_member_link"
    outside_non_member_link.symlink_to(non_member)

    container = _RecordedLocalContainer(ws_dir, cid="review-ws-symlink-cid")
    fp = compute_manifest_fingerprint(container, str(ws_dir))
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked --all-groups --all-packages",
        exit_code=0,
        fingerprint=fp,
        project_root=str(ws_dir),
    )

    # Absent/retired recorded root alongside live root does not break unrelated checks
    absent_root = tmp_path / "retired_deleted_root"
    record_environment_state(
        container.id,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        project_root=str(absent_root),
    )

    # Actual resolved uv workspace member precedence wins via outside symlink
    assert check_uv_environment(container, str(outside_member_link)) == ("ready", None)
    # Non-member under still-locked parent remains not_uv via outside symlink
    assert check_uv_environment(container, str(outside_non_member_link)) == ("not_uv", None)

    # Inconclusive physical probe fails closed
    with patch(
        "sunaba.environment._resolve_physical_paths",
        side_effect=EnvironmentResolutionError("physical path probe returned malformed output"),
    ):
        err_status, err_reason = check_uv_environment(container, str(outside_non_member_link))
        assert err_status == "failed"
        assert err_reason is not None and "physical path probe" in err_reason

    # Timeout propagation across environment, lint, pytest, and verify
    record_verify_success(container.id)
    with patch(
        "sunaba.environment._resolve_physical_paths",
        side_effect=VerifyTreeTimeout(5.0, "ok", 5.1),
    ):
        with pytest.raises(VerifyTreeTimeout):
            check_uv_environment(container, str(outside_non_member_link))
        assert has_verify_success(container.id) is False

        with pytest.raises(VerifyTreeTimeout):
            _run_ruff_verify(container, "__init__.py", workdir=str(outside_non_member_link))
        with pytest.raises(VerifyTreeTimeout):
            _run_pytest_verify(container, "tests/", workdir=str(outside_non_member_link))

        record_verify_success(container.id)
        client = _make_client_mock(cast(Any, container))
        with patch("sunaba.tools.verify._docker", return_value=client):
            timeout_res = json.loads(
                verify_in_container(
                    container_id=container.id,
                    path=".",
                    working_dir=str(outside_non_member_link),
                )
            )
        assert timeout_res["status"] == "timeout"
        assert timeout_res["gate_passed"] is False
        assert has_verify_success(container.id) is False

    with patch(
        "sunaba.environment._resolve_physical_paths",
        side_effect=ForegroundTreeTimeout(5.0, "ok", 5.1),
    ):
        record_verify_success(container.id)
        client = _make_client_mock(cast(Any, container))
        with patch("sunaba.tools.verify._docker", return_value=client):
            with pytest.raises(ForegroundTreeTimeout):
                verify_in_container(
                    container_id=container.id,
                    path=".",
                    working_dir=str(outside_non_member_link),
                )
        assert has_verify_success(container.id) is False


def test_review_legacy_missing_uv_lock_blocks_and_preserves_locked_compat(
    tmp_path: Path, isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Old ready record with explicit project_root and fingerprint=None blocks on lock loss."""
    from sunaba.edit_verify.lint_runners import _run_pyright_verify, _run_ruff_verify
    from sunaba.edit_verify.test_runners import _run_pytest_verify

    proj = tmp_path / "legacy_proj"
    child = proj / "sub"
    child.mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        '[project]\nname = "legacy-proj"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (child / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    _install_local_venv_tools(proj)

    outside_link = tmp_path / "legacy_outside_link"
    outside_link.symlink_to(child)

    container = _RecordedLocalContainer(proj, cid="review-legacy-cid")
    cid = container.id
    record_environment_state(
        cid,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        fingerprint=None,
        project_root=str(proj),
    )

    # Locked legacy compatibility: root and child pass; verify and publish gate pass
    assert check_uv_environment(container, str(proj)) == ("ready", None)
    assert check_uv_environment(container, str(child)) == ("ready", None)
    client = _make_client_mock(cast(Any, container))
    with (
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        patch(
            "sunaba.tools.vcs.publishing._ensure_proxy_ready",
            side_effect=RuntimeError("POST_VERIFY_SENTINEL"),
        ) as proxy_ready,
    ):
        locked_verify = json.loads(
            verify_in_container(container_id=cid, path="tests", working_dir=str(proj))
        )
        assert locked_verify["gate_passed"] is True, locked_verify
        assert has_verify_success(cid) is True

        with pytest.raises(RuntimeError, match="POST_VERIFY_SENTINEL"):
            publish(
                container_id=cid,
                repo="owner/repo",
                branch="feature",
                message="msg",
                skip_verify_gate=False,
            )
        proxy_ready.assert_called_once()

    # Remove only uv.lock while pyproject.toml remains
    (proj / "uv.lock").unlink()

    for target in (str(proj), str(child), str(outside_link)):
        status, reason = check_uv_environment(container, target)
        assert status == "stale"
        assert reason is not None
        assert "uv.lock" in reason
        assert "sandbox_prepare_environment" in reason

        container.executed.clear()
        ruff_res = _run_ruff_verify(container, "mod.py", workdir=target)
        assert ruff_res.status == "error"
        assert "stale" in ruff_res.detail and "uv.lock" in ruff_res.detail

        pyright_res = _run_pyright_verify(container, "mod.py", workdir=target)
        assert pyright_res.status == "error"
        assert "stale" in pyright_res.detail and "uv.lock" in pyright_res.detail

        pytest_res = _run_pytest_verify(container, "tests/", workdir=target)
        assert pytest_res.status == "error"
        assert "stale" in pytest_res.detail and "uv.lock" in pytest_res.detail

        assert not any(
            "ruff check" in cmd or "pyright --outputjson" in cmd or "--junit-xml" in cmd
            for cmd in container.executed
        )

        record_verify_success(cid)
        with patch("sunaba.tools.verify._docker", return_value=client):
            res = json.loads(
                verify_in_container(container_id=cid, path=".", working_dir=target)
            )
        assert res["status"] == "environment_setup_failed"
        assert res["gate_passed"] is False
        assert res["environment_setup"]["status"] == "stale"
        assert "uv.lock" in res["environment_setup"]["reason"]
        assert "sandbox_prepare_environment" in res["environment_setup"]["reason"]
        assert res["lint"]["status"] == "skipped"
        assert res["types"]["status"] == "skipped"
        assert res["patch_targets"]["status"] == "skipped"
        assert res["tests"]["status"] == "skipped"
        assert has_verify_success(cid) is False

    # Stored ready record is preserved across staleness checks and gate blocks
    stored = list_environment_states(cid)[str(proj)]
    assert stored["status"] == "ready"
    assert stored.get("fingerprint") is None

    # Publish gate also blocks with stale while preserving stored ready record
    with (
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        patch(
            "sunaba.tools.vcs.publishing._ensure_proxy_ready",
            side_effect=RuntimeError("POST_VERIFY_SENTINEL"),
        ) as proxy_ready_after,
    ):
        pub_res = json.loads(
            publish(
                container_id=cid,
                repo="owner/repo",
                branch="feature",
                message="msg",
                skip_verify_gate=False,
            )
        )
        assert pub_res["status"] == "error"
        assert pub_res["step"] == "verify_gate"
        assert "stale" in pub_res["error"]
        assert "uv.lock" in pub_res["error"]
        proxy_ready_after.assert_not_called()
    assert list_environment_states(cid)[str(proj)]["status"] == "ready"

    # Unknown legacy root (no project_root, fingerprint=None) preserves prior behavior
    unknown_cid = "review-legacy-unknown-cid"
    unknown_container = _RecordedLocalContainer(proj, cid=unknown_cid)
    record_environment_state(
        unknown_cid, "ready", command="uv sync --locked", exit_code=0
    )
    assert check_uv_environment(unknown_container, str(proj)) == ("not_uv", None)
    assert check_uv_environment(unknown_container, str(child)) == ("not_uv", None)


class _ControllableDeadlineContainer:
    """Controllable container supporting real deadline watchdogs and blocked execs."""

    def __init__(self, cid: str = "review-deadline-cid", root: str = "/workspace") -> None:
        import threading

        self.id = cid
        self.root = root
        self.attrs = {"Config": {"WorkingDir": root}}
        self.block_on_target = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.target_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.reaper_calls: list[str] = []
        self.handler: Any = None

    def exec_run(self, cmd: list[str] | str, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        argv = list(cmd) if isinstance(cmd, (list, tuple)) else ["/bin/sh", "-c", str(cmd)]
        cmd_str = argv[-1] if argv else ""
        # Deadline reaper commands probe /proc or send signals without marker env.
        if "/proc/" in cmd_str or cmd_str.startswith("kill -"):
            self.reaper_calls.append(cmd_str)
            return 0, (b"", b"")

        self.target_calls.append((argv, dict(kwargs)))
        if self.block_on_target:
            self.entered.set()
            self.release.wait(5.0)
        if self.handler is not None:
            return self.handler(argv, kwargs)
        return 0, (b"", b"")


@pytest.mark.parametrize("scope_kind", ["verify", "foreground"])
def test_review_active_deadline_environment_exec_expiry_and_interrupts(
    isolated_state: Path, scope_kind: str
) -> None:
    """environment._exec_container routes through active verify/foreground deadline scopes."""
    import threading
    import time

    from sunaba.edit_verify.shell import (
        ForegroundTreeTimeout,
        VerifyTreeTimeout,
        close_foreground_deadline,
        close_verify_deadline,
        open_foreground_deadline,
        open_verify_deadline,
    )
    from sunaba.environment import (
        _resolve_physical_paths,
        probe_directory_exists,
        resolve_uv_root,
        resolve_uv_tool,
    )

    if scope_kind == "verify":
        open_scope = open_verify_deadline
        close_scope = close_verify_deadline
        expected_exc: type[Exception] = VerifyTreeTimeout
        marker_key = "SUNABA_VERIFY_MARKER"
    else:
        open_scope = open_foreground_deadline
        close_scope = close_foreground_deadline
        expected_exc = ForegroundTreeTimeout
        marker_key = "SUNABA_FOREGROUND_MARKER"

    # 1. Expired active deadline prevents any new Docker call across environment helpers.
    expired_container = _ControllableDeadlineContainer(cid=f"review-exp-{scope_kind}")
    record_environment_state(
        expired_container.id,
        "ready",
        command="uv sync --locked",
        exit_code=0,
        project_root="/workspace",
    )
    open_scope(expired_container, 0.04, f"reviewexp{scope_kind}")
    try:
        time.sleep(0.12)
        record_verify_success(expired_container.id)
        with pytest.raises(expected_exc):
            check_uv_environment(expired_container, "/workspace")
        assert has_verify_success(expired_container.id) is False
        for call in (
            lambda: resolve_uv_root(expired_container, "/workspace"),
            lambda: probe_directory_exists(expired_container, "/workspace"),
            lambda: _resolve_physical_paths(expired_container, ["/workspace", "/other"]),
            lambda: resolve_uv_tool(expired_container, "/workspace", "ruff"),
        ):
            with pytest.raises(expected_exc):
                call()
        assert expired_container.target_calls == []
    finally:
        close_scope()

    # 2. Blocked Docker call is interrupted when active deadline expires.
    for fn_name, call in (
        ("check_uv_environment", lambda c: check_uv_environment(c, "/workspace")),
        ("resolve_uv_root", lambda c: resolve_uv_root(c, "/workspace")),
        ("probe_directory_exists", lambda c: probe_directory_exists(c, "/workspace")),
        (
            "_resolve_physical_paths",
            lambda c: _resolve_physical_paths(c, ["/workspace", "/other"]),
        ),
        ("resolve_uv_tool", lambda c: resolve_uv_tool(c, "/workspace", "ruff")),
    ):
        blocked = _ControllableDeadlineContainer(cid=f"review-blk-{scope_kind}-{fn_name}")
        blocked.block_on_target = True
        record_verify_success(blocked.id)
        marker = f"reviewblk{scope_kind}"
        started = time.monotonic()
        open_scope(blocked, 0.12, marker)
        try:
            with pytest.raises(expected_exc):
                call(blocked)
            elapsed = time.monotonic() - started
            assert blocked.entered.is_set(), f"{fn_name} did not enter exec_run"
            assert elapsed < 2.0, f"{fn_name} hung instead of aborting on {scope_kind} deadline"
            _, exec_kwargs = blocked.target_calls[0]
            assert exec_kwargs.get("stdout") is True
            assert exec_kwargs.get("stderr") is True
            assert exec_kwargs.get("demux") is True
            assert exec_kwargs.get("environment") == {marker_key: marker}
            if fn_name == "check_uv_environment":
                assert has_verify_success(blocked.id) is False
        finally:
            blocked.release.set()
            close_scope()
            threading.Event().wait(0.01)


def test_review_active_deadline_foreground_precedence_no_scope_and_stderr(
    isolated_state: Path,
) -> None:
    """Foreground scope takes precedence; no-scope and stderr classification stay intact."""
    import time

    from sunaba.edit_verify.shell import (
        ForegroundTreeTimeout,
        close_foreground_deadline,
        close_verify_deadline,
        open_foreground_deadline,
        open_verify_deadline,
    )
    from sunaba.environment import resolve_uv_root

    def _workspace_missing_pyproject_handler(
        argv: list[str], kwargs: dict[str, Any]
    ) -> tuple[int, tuple[bytes, bytes]]:
        cmd_str = argv[-1]
        if "for f in pyproject.toml uv.lock" in cmd_str:
            return 0, (b"above:/\n__SUNABA_MANIFEST_PROBE_DONE__\n", b"")
        if "uv workspace dir" in cmd_str:
            return 2, (
                b"",
                b"error: No `pyproject.toml` found in current directory or any parent directory\n",
            )
        return 0, (b"", b"")

    # 1. No-scope behavior: no marker environment, exact exec args, stderr classification.
    no_scope_container = _ControllableDeadlineContainer(cid="review-noscope-cid")
    no_scope_container.handler = _workspace_missing_pyproject_handler
    assert resolve_uv_root(no_scope_container, "/workspace/sub") is None
    assert len(no_scope_container.target_calls) == 2
    for argv, exec_kwargs in no_scope_container.target_calls:
        assert argv[:2] == ["/bin/sh", "-c"]
        assert exec_kwargs == {"stdout": True, "stderr": True, "demux": True}

    # 2. Foreground precedence over verify when both scopes are active + stderr classification.
    fg_prec_container = _ControllableDeadlineContainer(cid="review-fgprec-cid")
    fg_prec_container.handler = _workspace_missing_pyproject_handler
    open_verify_deadline(fg_prec_container, 5.0, "verifyprecmarker")
    open_foreground_deadline(fg_prec_container, 5.0, "fgprecmarker")
    try:
        assert resolve_uv_root(fg_prec_container, "/workspace/sub") is None
        assert len(fg_prec_container.target_calls) == 2
        for _, exec_kwargs in fg_prec_container.target_calls:
            assert exec_kwargs.get("environment") == {
                "SUNABA_FOREGROUND_MARKER": "fgprecmarker"
            }
    finally:
        close_foreground_deadline()
        close_verify_deadline()

    # 3. Blocked call with both scopes active raises ForegroundTreeTimeout.
    both_blocked = _ControllableDeadlineContainer(cid="review-bothblk-cid")
    both_blocked.block_on_target = True
    open_verify_deadline(both_blocked, 5.0, "verifyblkmarker")
    open_foreground_deadline(both_blocked, 0.12, "fgblkmarker")
    try:
        started = time.monotonic()
        with pytest.raises(ForegroundTreeTimeout):
            resolve_uv_root(both_blocked, "/workspace")
        assert time.monotonic() - started < 2.0
        _, exec_kwargs = both_blocked.target_calls[0]
        assert exec_kwargs.get("environment") == {
            "SUNABA_FOREGROUND_MARKER": "fgblkmarker"
        }
    finally:
        both_blocked.release.set()
        close_foreground_deadline()
        close_verify_deadline()


def test_review_symlink_public_lint_and_type_check_cover_both_directions(
    tmp_path: Path, isolated_state: Path
) -> None:
    """Public lint_in_container and type_check_in_container resolve physical target dirs."""
    from sunaba.tools.verify import lint_in_container, type_check_in_container

    # Stale uv project
    stale_proj = tmp_path / "stale_proj"
    stale_sub = stale_proj / "sub"
    stale_sub.mkdir(parents=True)
    (stale_proj / "pyproject.toml").write_text(
        '[project]\nname = "stale-proj"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (stale_proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (stale_sub / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (stale_sub / "sibling.py").write_text("import os\n", encoding="utf-8")
    (stale_proj / "src").mkdir()
    _install_local_venv_tools(stale_proj)

    # Separate ready repo containing aliases pointing INTO stale_proj
    other_repo = tmp_path / "other_repo"
    (other_repo / "src").mkdir(parents=True)
    (other_repo / "pyproject.toml").write_text(
        '[project]\nname = "other-repo"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (other_repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (other_repo / "src" / "decoy.py").write_text("import json\n", encoding="utf-8")
    _install_local_venv_tools(other_repo)

    # Unmarked standalone outside directory
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir(parents=True)
    (outside_dir / "mod.py").write_text("import os\n", encoding="utf-8")

    # Separate ready uv project pointed to by an alias inside stale_proj
    other_ready_proj = tmp_path / "other_ready_proj"
    (other_ready_proj / "src").mkdir(parents=True)
    (other_ready_proj / "pyproject.toml").write_text(
        '[project]\nname = "other-ready"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (other_ready_proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (other_ready_proj / "src" / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    _install_local_venv_tools(other_ready_proj)

    # Nested uv project inside outer uv project (genuine project precedence)
    outer_proj = tmp_path / "outer_proj"
    nested_proj = outer_proj / "nested_proj"
    (nested_proj / "src").mkdir(parents=True)
    (outer_proj / "pyproject.toml").write_text(
        '[project]\nname = "outer"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (outer_proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (nested_proj / "pyproject.toml").write_text(
        '[project]\nname = "nested"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (nested_proj / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (nested_proj / "src" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    # Symlinks in both directions (directory and file symlinks)
    alias_dir_in = other_repo / "alias_into_stale"
    alias_dir_in.symlink_to(stale_sub)
    alias_file_in = other_repo / "src" / "alias_file_into_stale.py"
    alias_file_in.symlink_to(stale_sub / "mod.py")

    out_dir_link = stale_proj / "out_dir_link"
    out_dir_link.symlink_to(outside_dir)
    out_file_link = stale_proj / "src" / "out_file_link.py"
    out_file_link.symlink_to(outside_dir / "mod.py")

    out_proj_link = stale_proj / "out_proj_link"
    out_proj_link.symlink_to(other_ready_proj / "src")

    nested_alias = other_repo / "nested_alias"
    nested_alias.symlink_to(nested_proj / "src")

    broken_dir_link = stale_proj / "broken_dir_link"
    broken_dir_link.symlink_to(tmp_path / "does_not_exist")

    cid = "review-public-symlink-cid"
    stale_container = _RecordedLocalContainer(stale_proj, cid=cid)
    other_container = _RecordedLocalContainer(other_repo, cid=cid)

    # Record ready states for stale_proj, other_repo, other_ready_proj, and outer_proj
    # (nested_proj is intentionally left unprepared to test nested precedence).
    for root_dir, probe_container in (
        (stale_proj, stale_container),
        (other_repo, other_container),
        (other_ready_proj, stale_container),
        (outer_proj, stale_container),
    ):
        fp = compute_manifest_fingerprint(probe_container, str(root_dir))
        record_environment_state(
            cid,
            "ready",
            command="uv sync --locked",
            exit_code=0,
            fingerprint=fp,
            project_root=str(root_dir),
        )

    # Break stale_proj by deleting only uv.lock
    saved_uv_lock = (stale_proj / "uv.lock").read_text(encoding="utf-8")
    (stale_proj / "uv.lock").unlink()

    # Direction 1: aliases under ready other_repo pointing INTO stale_proj must block
    other_client = _make_client_mock(cast(Any, other_container))
    with patch("sunaba.tools.verify._docker", return_value=other_client):
        for target in (
            str(alias_dir_in / "mod.py"),
            "alias_into_stale/mod.py",
            str(alias_file_in),
            "src/alias_file_into_stale.py",
        ):
            other_container.executed.clear()
            lint_res = json.loads(lint_in_container(container_id=cid, file_path=target))
            assert any(
                item["rule"] == "error"
                and "stale" in item["message"]
                and "uv.lock" in item["message"]
                for item in lint_res
            ), (target, lint_res)
            assert not any("ruff check" in cmd for cmd in other_container.executed)

            other_container.executed.clear()
            type_res = json.loads(type_check_in_container(container_id=cid, file_path=target))
            assert any(
                item["rule"] == "error"
                and "stale" in item["message"]
                and "uv.lock" in item["message"]
                for item in type_res
            ), (target, type_res)
            assert not any("pyright --outputjson" in cmd for cmd in other_container.executed)

        # Genuine project precedence: alias into unprepared nested_proj must block on nested_proj,
        # not inherit ready outer_proj or ready other_repo.
        for nested_target in (str(nested_alias / "mod.py"), "nested_alias/mod.py"):
            nested_lint = json.loads(
                lint_in_container(container_id=cid, file_path=nested_target)
            )
            assert any(
                item["rule"] == "error" and str(nested_proj) in item["message"]
                for item in nested_lint
            ), (nested_target, nested_lint)
            nested_type = json.loads(
                type_check_in_container(container_id=cid, file_path=nested_target)
            )
            assert any(
                item["rule"] == "error" and str(nested_proj) in item["message"]
                for item in nested_type
            ), (nested_target, nested_type)

    # Direction 2: aliases inside stale_proj pointing OUT must NOT be blocked by stale_proj
    stale_client = _make_client_mock(cast(Any, stale_container))
    with patch("sunaba.tools.verify._docker", return_value=stale_client):
        for out_target in (
            str(out_dir_link / "mod.py"),
            "out_dir_link/mod.py",
            str(out_file_link),
            "src/out_file_link.py",
        ):
            out_lint = json.loads(
                lint_in_container(container_id=cid, file_path=out_target)
            )
            assert not any("stale" in item["message"] for item in out_lint), (
                out_target,
                out_lint,
            )
            assert any(item["rule"] == "F401" for item in out_lint), (out_target, out_lint)

            out_type = json.loads(
                type_check_in_container(container_id=cid, file_path=out_target)
            )
            assert not any("stale" in item["message"] for item in out_type), (
                out_target,
                out_type,
            )
            assert not any(item["rule"] == "error" for item in out_type), (
                out_target,
                out_type,
            )

        # Out-of-project alias pointing into another ready project uses that project's tools
        for other_proj_target in (str(out_proj_link / "clean.py"), "out_proj_link/clean.py"):
            stale_container.executed.clear()
            proj_lint = json.loads(
                lint_in_container(container_id=cid, file_path=other_proj_target)
            )
            assert proj_lint == [], (other_proj_target, proj_lint)
            assert any(
                str(other_ready_proj / ".venv" / "bin" / "ruff") in cmd
                for cmd in stale_container.executed
            ), stale_container.executed

            stale_container.executed.clear()
            proj_type = json.loads(
                type_check_in_container(container_id=cid, file_path=other_proj_target)
            )
            assert proj_type == [], (other_proj_target, proj_type)
            assert any(
                str(other_ready_proj / ".venv" / "bin" / "pyright") in cmd
                for cmd in stale_container.executed
            ), stale_container.executed

        # Missing subdir under out_dir_link does not fall back to stale_proj
        missing_out_lint = json.loads(
            lint_in_container(container_id=cid, file_path="out_dir_link/missing_sub/mod.py")
        )
        assert not any("stale" in item["message"] for item in missing_out_lint), missing_out_lint

        # Meaningful probe error handling for broken symlink parent and file-as-parent
        for bad_path in (
            str(broken_dir_link / "mod.py"),
            str(outside_dir / "mod.py" / "child.py"),
        ):
            bad_lint = json.loads(lint_in_container(container_id=cid, file_path=bad_path))
            assert len(bad_lint) == 1
            assert bad_lint[0]["rule"] == "error"
            assert "lint path resolution failed" in bad_lint[0]["message"]

            bad_type = json.loads(
                type_check_in_container(container_id=cid, file_path=bad_path)
            )
            assert len(bad_type) == 1
            assert bad_type[0]["rule"] == "error"
            assert "type_check path resolution failed" in bad_type[0]["message"]

    # When stale_proj is ready, file-symlink scope phase targets stale_proj/sub, not other_repo/src
    (stale_proj / "uv.lock").write_text(saved_uv_lock, encoding="utf-8")
    with patch("sunaba.tools.verify._docker", return_value=other_client):
        scoped_lint = json.loads(
            lint_in_container(container_id=cid, file_path=str(alias_file_in))
        )
    assert any(
        item["rule"] == "F401" and str(stale_sub / "sibling.py") in item["file"]
        for item in scoped_lint
    ), scoped_lint
    assert not any("decoy.py" in item["file"] for item in scoped_lint), scoped_lint


@pytest.mark.parametrize("scope_kind", ["verify", "foreground"])
def test_review_active_deadline_single_file_lint_and_type_check(
    tmp_path: Path, isolated_state: Path, scope_kind: str
) -> None:
    """Public lint_in_container and type_check_in_container honor active deadlines."""
    import time

    from sunaba.edit_verify.shell import (
        ForegroundTreeTimeout,
        VerifyTreeTimeout,
        close_foreground_deadline,
        close_verify_deadline,
        open_foreground_deadline,
        open_verify_deadline,
    )
    from sunaba.tools.verify import lint_in_container, type_check_in_container

    if scope_kind == "verify":
        open_scope = open_verify_deadline
        close_scope = close_verify_deadline
        expected_exc: type[Exception] = VerifyTreeTimeout
        marker_key = "SUNABA_VERIFY_MARKER"
    else:
        open_scope = open_foreground_deadline
        close_scope = close_foreground_deadline
        expected_exc = ForegroundTreeTimeout
        marker_key = "SUNABA_FOREGROUND_MARKER"

    # 1. Expired deadline prevents Docker probe calls in lint_in_container / type_check_in_container
    exp_container = _ControllableDeadlineContainer(
        cid=f"review-sf-exp-{scope_kind}", root=str(tmp_path)
    )
    exp_client = _make_client_mock(cast(Any, exp_container))
    with patch("sunaba.tools.verify._docker", return_value=exp_client):
        open_scope(exp_container, 0.04, f"reviewsfexp{scope_kind}")
        try:
            time.sleep(0.12)
            with pytest.raises(expected_exc):
                lint_in_container(container_id=exp_container.id, file_path="mod.py")
            with pytest.raises(expected_exc):
                type_check_in_container(container_id=exp_container.id, file_path="mod.py")
            assert exp_container.target_calls == []
        finally:
            close_scope()

    # 2. Blocked path-resolution exec interrupts and propagates TreeTimeout (not swallowed)
    for tool_name, invoke in (
        ("lint", lambda cid: lint_in_container(container_id=cid, file_path="mod.py")),
        ("type", lambda cid: type_check_in_container(container_id=cid, file_path="mod.py")),
    ):
        blk_container = _ControllableDeadlineContainer(
            cid=f"review-sf-blk-{scope_kind}-{tool_name}", root=str(tmp_path)
        )
        blk_container.block_on_target = True
        blk_client = _make_client_mock(cast(Any, blk_container))
        marker = f"reviewsfblk{scope_kind}"
        with patch("sunaba.tools.verify._docker", return_value=blk_client):
            open_scope(blk_container, 0.12, marker)
            try:
                started = time.monotonic()
                with pytest.raises(expected_exc):
                    invoke(blk_container.id)
                assert blk_container.entered.is_set()
                assert time.monotonic() - started < 2.0
                _, exec_kwargs = blk_container.target_calls[0]
                assert exec_kwargs.get("environment") == {marker_key: marker}
            finally:
                blk_container.release.set()
                close_scope()

    # 3. Path probes succeed; the actual lint/type command then expires and is reaped.
    root = str(tmp_path)
    for tool_name, invoke, tool_marker in (
        (
            "lint",
            lambda cid: lint_in_container(container_id=cid, file_path="mod.py"),
            "ruff check",
        ),
        (
            "type",
            lambda cid: type_check_in_container(container_id=cid, file_path="mod.py"),
            "pyright",
        ),
    ):
        live_container = _ControllableDeadlineContainer(
            cid=f"review-sf-live-{scope_kind}-{tool_name}", root=root
        )

        def _probe_then_block(
            argv: list[str],
            kwargs: dict[str, Any],
            _container: _ControllableDeadlineContainer = live_container,
            _tool_marker: str = tool_marker,
        ) -> tuple[int, tuple[bytes, bytes]]:
            cmd_str = argv[-1]
            if "_sunaba_f=" in cmd_str:
                payload = (
                    f"__SUNABA_TARGET_DIR__{root}\n"
                    f"__SUNABA_TARGET_FILE__{root}/mod.py\n"
                    f"__SUNABA_TARGET_ANCESTOR__{root}\n"
                    f"__SUNABA_BASE_WORKDIR__{root}\n"
                    f"__SUNABA_PROJECT_ROOT__{root}\n"
                )
                return 0, (payload.encode("utf-8"), b"")
            if "for f in pyproject.toml uv.lock" in cmd_str:
                return 0, (b"__SUNABA_MANIFEST_PROBE_DONE__\n", b"")
            if _tool_marker in cmd_str:
                _container.entered.set()
                _container.release.wait(5.0)
            return 0, (b"", b"")

        live_container.handler = _probe_then_block
        live_client = _make_client_mock(cast(Any, live_container))
        marker = f"reviewsf{scope_kind}{tool_name}"
        with patch("sunaba.tools.verify._docker", return_value=live_client):
            open_scope(live_container, 0.5, marker)
            try:
                started = time.monotonic()
                with pytest.raises(expected_exc):
                    invoke(live_container.id)
                assert time.monotonic() - started < 2.0
            finally:
                live_container.release.set()
                close_scope()

        # Probes ran first, then the real tool command blocked and expired.
        assert live_container.entered.is_set()
        assert any(
            tool_marker in argv[-1] for argv, _ in live_container.target_calls
        ), f"{tool_name} tool command never reached Docker"
        # Every Docker call carries the scope marker (merged into the exec env).
        for _, exec_kwargs in live_container.target_calls:
            assert exec_kwargs.get("environment") == {marker_key: marker}
        # Expiry reaped the marked tree instead of returning a partial verdict.
        assert any(marker in call for call in live_container.reaper_calls)


def test_review_active_deadline_single_file_foreground_precedence(
    tmp_path: Path, isolated_state: Path
) -> None:
    """Both scopes active: the lint command runs under the foreground scope."""
    import time

    from sunaba.edit_verify.shell import (
        ForegroundTreeTimeout,
        close_foreground_deadline,
        close_verify_deadline,
        open_foreground_deadline,
        open_verify_deadline,
    )
    from sunaba.tools.verify import lint_in_container

    root = str(tmp_path)
    container = _ControllableDeadlineContainer(cid="review-sf-precedence", root=root)

    def _probe_then_block(
        argv: list[str], kwargs: dict[str, Any]
    ) -> tuple[int, tuple[bytes, bytes]]:
        cmd_str = argv[-1]
        if "_sunaba_f=" in cmd_str:
            payload = (
                f"__SUNABA_TARGET_DIR__{root}\n"
                f"__SUNABA_TARGET_FILE__{root}/mod.py\n"
                f"__SUNABA_TARGET_ANCESTOR__{root}\n"
                f"__SUNABA_BASE_WORKDIR__{root}\n"
                f"__SUNABA_PROJECT_ROOT__{root}\n"
            )
            return 0, (payload.encode("utf-8"), b"")
        if "for f in pyproject.toml uv.lock" in cmd_str:
            return 0, (b"__SUNABA_MANIFEST_PROBE_DONE__\n", b"")
        if "ruff check" in cmd_str:
            container.entered.set()
            container.release.wait(5.0)
        return 0, (b"", b"")

    container.handler = _probe_then_block
    client = _make_client_mock(cast(Any, container))
    with patch("sunaba.tools.verify._docker", return_value=client):
        open_verify_deadline(container, 5.0, "reviewsfverifyprec")
        open_foreground_deadline(container, 0.5, "reviewsfgfprec")
        try:
            started = time.monotonic()
            with pytest.raises(ForegroundTreeTimeout):
                lint_in_container(container_id=container.id, file_path="mod.py")
            assert time.monotonic() - started < 2.0
        finally:
            container.release.set()
            close_foreground_deadline()
            close_verify_deadline()

    assert container.entered.is_set()
    for _, exec_kwargs in container.target_calls:
        assert exec_kwargs.get("environment") == {
            "SUNABA_FOREGROUND_MARKER": "reviewsfgfprec"
        }
    assert any("reviewsfgfprec" in call for call in container.reaper_calls)


def test_review_active_deadline_exec_run_merges_caller_environment() -> None:
    """_exec_run merges active scope markers onto caller-provided environment."""
    from sunaba.edit_verify.shell import (
        _exec_run,
        close_foreground_deadline,
        close_verify_deadline,
        open_foreground_deadline,
        open_verify_deadline,
    )

    container = _ControllableDeadlineContainer(cid="review-exec-env-merge")
    caller_env = {"CALLER_VAR": "1", "SUNABA_FOREGROUND_MARKER": "stale"}

    _exec_run(container, ["/bin/sh", "-c", "true"], environment=caller_env)
    assert caller_env == {"CALLER_VAR": "1", "SUNABA_FOREGROUND_MARKER": "stale"}

    open_verify_deadline(container, 5.0, "verifymerge")
    try:
        _exec_run(container, ["/bin/sh", "-c", "true"], environment=caller_env)
        open_foreground_deadline(container, 5.0, "fgmerge")
        try:
            _exec_run(container, ["/bin/sh", "-c", "true"], environment=caller_env)
        finally:
            close_foreground_deadline()
    finally:
        close_verify_deadline()

    assert [kw.get("environment") for _, kw in container.target_calls] == [
        {"CALLER_VAR": "1", "SUNABA_FOREGROUND_MARKER": "stale"},
        {
            "CALLER_VAR": "1",
            "SUNABA_FOREGROUND_MARKER": "stale",
            "SUNABA_VERIFY_MARKER": "verifymerge",
        },
        {"CALLER_VAR": "1", "SUNABA_FOREGROUND_MARKER": "fgmerge"},
    ]
