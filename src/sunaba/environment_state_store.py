# State is deliberately kept outside the container because an agent inside
# the container can rewrite it.

"""Host-side persistence and fingerprint staleness detection for uv environments (Issue #967).

State is deliberately kept outside the container because an agent inside
the container can rewrite it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

import platformdirs

logger: logging.Logger = logging.getLogger(__name__)

MANIFEST_FILES: tuple[str, ...] = ("pyproject.toml", "uv.lock", ".python-version")
MANIFEST_ABSENT_MARKER: bytes = b"__SUNABA_MANIFEST_ABSENT__\n"


class FingerprintProbeError(RuntimeError):
    """Raised when reading a manifest file from the container fails."""


def get_environment_state_dir() -> Path:
    """Return the host directory for persisted container environment state."""
    override = os.environ.get("SUNABA_ENVIRONMENT_STATE_DIR")
    if override and override.strip():
        return Path(override.strip())
    return Path(platformdirs.user_state_dir("sunaba"))


def _read_container_file(container: Any, root: str, filename: str) -> bytes | None:
    """Read bytes of *filename* from *root* in container, or None if absent.

    Raises FingerprintProbeError if probe fails.
    """
    root_str = root.rstrip("/")
    script = (
        f"cd {shlex.quote(root_str)} && "
        f"if [ ! -e {shlex.quote(filename)} ]; then "
        f"printf '__SUNABA_MANIFEST_ABSENT__\\n'; "
        f"elif [ ! -f {shlex.quote(filename)} ]; then "
        f"exit 2; "
        f"else cat {shlex.quote(filename)}; fi"
    )
    exec_kwargs: dict[str, Any] = {
        "stdout": True,
        "stderr": True,
        "demux": True,
    }
    try:
        from sunaba.edit_verify.shell import foreground_marker_environment

        marker_env = foreground_marker_environment()
        if marker_env is not None:
            exec_kwargs["environment"] = marker_env
    except Exception:
        pass

    try:
        res = container.exec_run(
            ["/bin/sh", "-c", script],
            **exec_kwargs,
        )
    except Exception as exc:
        raise FingerprintProbeError(f"container exec failed for {filename}: {exc}") from exc

    if isinstance(res, tuple):
        ec = res[0]
        output = res[1]
    else:
        ec = getattr(res, "exit_code", 0)
        output = getattr(res, "output", b"")

    if isinstance(output, tuple):
        stdout, stderr = output
    else:
        stdout, stderr = output, b""

    stdout_bytes = stdout or b""
    stderr_bytes = stderr or b""

    if ec != 0:
        err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise FingerprintProbeError(
            f"probe for {filename} failed (exit {ec}): {err_msg[:160]}"
        )

    if stdout_bytes == MANIFEST_ABSENT_MARKER:
        return None

    return stdout_bytes


def compute_manifest_fingerprint(container: Any, root: str) -> dict[str, Any]:
    """Compute content hashes for uv manifests and .python-version on the host."""
    fingerprint: dict[str, Any] = {"root": root.rstrip("/")}
    files_map: dict[str, str] = {}
    for filename in MANIFEST_FILES:
        content = _read_container_file(container, root, filename)
        if content is None:
            files_map[filename] = "absent"
        else:
            files_map[filename] = hashlib.sha256(content).hexdigest()
    fingerprint["files"] = files_map
    for filename, val in files_map.items():
        fingerprint[filename] = val
    return fingerprint


def check_manifest_staleness(
    container: Any,
    root: str,
    recorded_fingerprint: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Compare current manifest fingerprint with recorded fingerprint.

    Returns (is_stale, list_of_changed_files).
    Raises FingerprintProbeError if reading from container fails.
    """
    if not recorded_fingerprint:
        return False, []

    current_fp = compute_manifest_fingerprint(container, root)

    changed: list[str] = []
    recorded_root = recorded_fingerprint.get("root")
    if recorded_root and recorded_root != root.rstrip("/"):
        changed.append(f"root ({recorded_root} -> {root.rstrip('/')})")

    recorded_files = recorded_fingerprint.get("files")
    if not isinstance(recorded_files, dict):
        recorded_files = {f: recorded_fingerprint.get(f, "absent") for f in MANIFEST_FILES}

    current_files = current_fp.get("files", {})

    for filename in MANIFEST_FILES:
        rec_val = recorded_files.get(filename, "absent")
        cur_val = current_files.get(filename, "absent")
        if rec_val != cur_val:
            changed.append(filename)

    return bool(changed), changed


def _find_state_file(state_dir: Path, container_id: str) -> Path | None:
    p_json = state_dir / f"{container_id}.json"
    if p_json.exists():
        return p_json
    p_raw = state_dir / container_id
    if p_raw.exists():
        return p_raw
    return None


def save_environment_state(container_id: str, record: dict[str, Any]) -> None:
    """Atomically persist environment state for full container_id to disk."""
    state_dir = get_environment_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    target_path = state_dir / f"{container_id}.json"

    data = dict(record)
    data["container_id"] = container_id

    payload = json.dumps(data, indent=2).encode("utf-8")
    fd, temp_path = tempfile.mkstemp(dir=state_dir, prefix=f".{container_id}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(temp_path, target_path)
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
        raise


def remove_environment_state(container_id: str) -> None:
    """Remove persisted environment state for container_id."""
    state_dir = get_environment_state_dir()
    if not state_dir.exists():
        return
    for p in (state_dir / f"{container_id}.json", state_dir / container_id):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def load_environment_state(container_id: str) -> dict[str, Any] | None:
    """Load persisted environment state for exact full container_id."""
    state_dir = get_environment_state_dir()
    if not state_dir.exists():
        return None
    file_path = _find_state_file(state_dir, container_id)
    if file_path is None:
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"status": "not_prepared", "reason": "corrupt environment state store"}
        # Must match exact container_id, not a prefix
        stored_cid = data.get("container_id")
        if stored_cid is not None and stored_cid != container_id:
            return None
        return data
    except Exception:
        return {"status": "not_prepared", "reason": "corrupt environment state store"}


def prune_and_load_environment_states(client: Any = None) -> dict[str, dict[str, Any]]:
    """Clean up records for dead containers and load surviving records into memory."""
    state_dir = get_environment_state_dir()
    if not state_dir.exists():
        return {}

    existing_container_ids: set[str] | None = None
    if client is None:
        try:
            from sunaba.tools.common import _docker

            client = _docker()
        except Exception as exc:
            logger.warning("Could not obtain Docker client for startup pruning: %s", exc)
            client = None

    if client is not None:
        try:
            existing_container_ids = {c.id for c in client.containers.list(all=True)}
        except Exception as exc:
            logger.warning("Could not list containers for startup pruning: %s", exc)
            existing_container_ids = None

    loaded: dict[str, dict[str, Any]] = {}
    for p in list(state_dir.iterdir()):
        if p.name.startswith("."):
            continue
        if not p.is_file():
            continue
        cid = p.name[:-5] if p.name.endswith(".json") else p.name

        if existing_container_ids is not None and cid not in existing_container_ids:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                loaded[cid] = {"status": "not_prepared", "reason": "corrupt environment state store"}
            else:
                stored_cid = data.get("container_id", cid)
                if stored_cid != cid and existing_container_ids is not None and stored_cid not in existing_container_ids:
                    p.unlink(missing_ok=True)
                    continue
                loaded[stored_cid] = data
        except Exception:
            loaded[cid] = {"status": "not_prepared", "reason": "corrupt environment state store"}

    from sunaba.verify_state import _environment_state, _environment_state_lock

    with _environment_state_lock:
        for cid, record in loaded.items():
            _environment_state[cid] = record

    return loaded
