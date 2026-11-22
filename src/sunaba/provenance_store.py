"""Host-side repository provenance persistence for sandbox containers (Issue #998).

Provenance is stored outside the container below ``get_environment_state_dir()``
and keyed by the exact full Docker container ID so that in-container agents
cannot forge or tamper with target repository / PR metadata.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sunaba.environment_state_store import get_environment_state_dir

logger: logging.Logger = logging.getLogger(__name__)

_PROVENANCE_DIR_NAME: str = "provenance"
_PROVENANCE_SCHEMA_VERSION: int = 1
_FULL_CONTAINER_ID_RE: re.Pattern[str] = re.compile(r"^[0-9a-fA-F]{64}$")
_PROVENANCE_FIELDS: tuple[str, ...] = (
    "target_repo_id",
    "target_ref",
    "head_repo_id",
    "head_ref",
    "pr_number",
)


@dataclass(frozen=True)
class ContainerProvenance:
    """Trusted host-side repository provenance for a sandbox container."""

    target_repo_id: int | None = None
    target_ref: str | None = None
    head_repo_id: int | None = None
    head_ref: str | None = None
    pr_number: int | None = None


def is_full_container_id(container_id: Any) -> bool:
    """Return True when *container_id* is an exact 64-char hex Docker ID."""
    return isinstance(container_id, str) and bool(
        _FULL_CONTAINER_ID_RE.fullmatch(container_id)
    )


def get_provenance_dir() -> Path:
    """Return the host directory storing per-container provenance files."""
    return get_environment_state_dir() / _PROVENANCE_DIR_NAME


def valid_positive_github_id(value: Any) -> int | None:
    """Return *value* if it is a positive integer (excluding ``bool``), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def valid_ref_name(value: Any) -> str | None:
    """Return *value* if it is a non-empty ref name without control chars, else ``None``."""
    if not isinstance(value, str) or not value or value.isspace():
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


def has_any_provenance(provenance: ContainerProvenance) -> bool:
    """Return True when at least one provenance field is populated."""
    return any(
        getattr(provenance, field) is not None for field in _PROVENANCE_FIELDS
    )


def _sanitize_provenance(provenance: ContainerProvenance) -> ContainerProvenance:
    """Coerce every field through trusted validators."""
    return ContainerProvenance(
        target_repo_id=valid_positive_github_id(provenance.target_repo_id),
        target_ref=valid_ref_name(provenance.target_ref),
        head_repo_id=valid_positive_github_id(provenance.head_repo_id),
        head_ref=valid_ref_name(provenance.head_ref),
        pr_number=valid_positive_github_id(provenance.pr_number),
    )


def provenance_from_repo_payload(
    payload: Mapping[str, Any] | None,
    *,
    requested_branch: str | None = None,
    default_branch_override: str | None = None,
) -> ContainerProvenance:
    """Build provenance from a ``GET /repos/{owner}/{repo}`` response.

    ``target_ref`` always comes from the API response's verified
    ``default_branch`` (or *default_branch_override* when supplied by a caller
    without raw payload access) — never from *requested_branch*.  For explicit
    branch checkouts, *requested_branch* is recorded separately as ``head_ref``
    so the integration target and checked-out work branch stay distinct even
    when the repository metadata lookup fails.
    """
    target_repo_id = (
        valid_positive_github_id(payload.get("id"))
        if isinstance(payload, Mapping)
        else None
    )
    api_default = (
        valid_ref_name(payload.get("default_branch"))
        if isinstance(payload, Mapping)
        else None
    )
    target_ref = api_default or valid_ref_name(default_branch_override)
    head_ref = (
        valid_ref_name(requested_branch) if requested_branch is not None else None
    )
    return ContainerProvenance(
        target_repo_id=target_repo_id,
        target_ref=target_ref,
        head_ref=head_ref,
    )


def provenance_from_pr_payload(
    payload: Mapping[str, Any] | None,
    *,
    pr_number: int,
) -> ContainerProvenance:
    """Build provenance from a ``GET /repos/{owner}/{repo}/pulls/{pr}`` response."""
    valid_pr = valid_positive_github_id(pr_number)
    if not isinstance(payload, Mapping) or valid_pr is None:
        return ContainerProvenance(pr_number=valid_pr)

    payload_number = payload.get("number")
    if payload_number is not None and valid_positive_github_id(payload_number) != valid_pr:
        return ContainerProvenance(pr_number=valid_pr)

    base = payload.get("base")
    base_map: Mapping[str, Any] = base if isinstance(base, Mapping) else {}
    base_repo = base_map.get("repo")
    base_repo_map: Mapping[str, Any] = (
        base_repo if isinstance(base_repo, Mapping) else {}
    )

    head = payload.get("head")
    head_map: Mapping[str, Any] = head if isinstance(head, Mapping) else {}
    head_repo = head_map.get("repo")
    head_repo_map: Mapping[str, Any] = (
        head_repo if isinstance(head_repo, Mapping) else {}
    )

    return ContainerProvenance(
        target_repo_id=valid_positive_github_id(base_repo_map.get("id")),
        target_ref=valid_ref_name(base_map.get("ref")),
        head_repo_id=valid_positive_github_id(head_repo_map.get("id")),
        head_ref=valid_ref_name(head_map.get("ref")),
        pr_number=valid_pr,
    )


def _provenance_file_path(provenance_dir: Path, full_container_id: str) -> Path:
    return provenance_dir / f"{full_container_id}.json"


def save_container_provenance(
    full_container_id: str,
    provenance: ContainerProvenance,
) -> bool:
    """Atomically persist container provenance under ``get_provenance_dir()``.

    Returns ``True`` when a record was persisted, ``False`` when skipped or on
    error.  Persistence failures are logged and never propagated so a working
    container is never made inaccessible.
    """
    if not is_full_container_id(full_container_id):
        return False

    sanitized = _sanitize_provenance(provenance)
    if not has_any_provenance(sanitized):
        return False

    temp_path: str | None = None
    try:
        provenance_dir = get_provenance_dir()
        provenance_dir.mkdir(parents=True, exist_ok=True)
        target_path = _provenance_file_path(provenance_dir, full_container_id)
        data: dict[str, Any] = {
            "version": _PROVENANCE_SCHEMA_VERSION,
            "container_id": full_container_id,
            "target_repo_id": sanitized.target_repo_id,
            "target_ref": sanitized.target_ref,
            "head_repo_id": sanitized.head_repo_id,
            "head_ref": sanitized.head_ref,
            "pr_number": sanitized.pr_number,
        }
        payload = json.dumps(data, indent=2).encode("utf-8")
        fd, temp_path = tempfile.mkstemp(
            dir=provenance_dir,
            prefix=f".{full_container_id}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target_path)
        return True
    except Exception as exc:
        if temp_path is not None:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
        logger.warning(
            "Could not save container provenance for %s: %s",
            full_container_id[:12],
            exc,
        )
        return False


def _validate_loaded_field(
    data: Mapping[str, Any],
    key: str,
    validator: Any,
) -> tuple[bool, Any]:
    """Validate a required stored provenance field.

    Returns ``(is_valid, parsed_value)``.  Missing keys or invalid non-``None``
    values cause ``is_valid=False`` so malformed records are never trusted.
    """
    if key not in data:
        return False, None
    raw = data[key]
    if raw is None:
        return True, None
    parsed = validator(raw)
    if parsed is None:
        return False, None
    return True, parsed


def load_container_provenance(
    full_container_id: str,
) -> ContainerProvenance | None:
    """Load and validate host-side provenance for *full_container_id*.

    Returns ``None`` when the ID is not a full Docker ID, the file is missing,
    or the stored payload is malformed or invalid.
    """
    if not is_full_container_id(full_container_id):
        return None

    provenance_dir = get_provenance_dir()
    target_path = _provenance_file_path(provenance_dir, full_container_id)
    try:
        if target_path.is_symlink() or not target_path.is_file():
            return None
        raw_text = target_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("version") != _PROVENANCE_SCHEMA_VERSION:
        return None
    if data.get("container_id") != full_container_id:
        return None

    ok_target_id, target_repo_id = _validate_loaded_field(
        data, "target_repo_id", valid_positive_github_id
    )
    ok_target_ref, target_ref = _validate_loaded_field(
        data, "target_ref", valid_ref_name
    )
    ok_head_id, head_repo_id = _validate_loaded_field(
        data, "head_repo_id", valid_positive_github_id
    )
    ok_head_ref, head_ref = _validate_loaded_field(
        data, "head_ref", valid_ref_name
    )
    ok_pr, pr_number = _validate_loaded_field(
        data, "pr_number", valid_positive_github_id
    )
    if not (ok_target_id and ok_target_ref and ok_head_id and ok_head_ref and ok_pr):
        return None

    record = ContainerProvenance(
        target_repo_id=target_repo_id,
        target_ref=target_ref,
        head_repo_id=head_repo_id,
        head_ref=head_ref,
        pr_number=pr_number,
    )
    if not has_any_provenance(record):
        return None
    return record


def remove_container_provenance(full_container_id: str) -> bool:
    """Best-effort removal of persisted provenance for *full_container_id*."""
    if not is_full_container_id(full_container_id):
        return False
    try:
        provenance_dir = get_provenance_dir()
        if not provenance_dir.exists():
            return False
        expected_name = f"{full_container_id}.json"
        target_path = next(
            (
                entry
                for entry in provenance_dir.iterdir()
                if entry.name == expected_name
            ),
            None,
        )
        if target_path is None:
            return False
        if not target_path.exists() and not target_path.is_symlink():
            return False
        target_path.unlink(missing_ok=True)
        return True
    except Exception as exc:
        logger.warning(
            "Could not remove container provenance for %s: %s",
            full_container_id[:12],
            exc,
        )
        return False


def prune_container_provenances(client: Any = None) -> int:
    """Remove host provenance files whose containers no longer exist in Docker.

    Best-effort cleanup run on server startup.  When Docker cannot be queried,
    retains all existing records rather than pruning blindly.
    """
    provenance_dir = get_provenance_dir()
    if not provenance_dir.exists():
        return 0

    if client is None:
        try:
            from sunaba.tools.common import _docker

            client = _docker()
        except Exception as exc:
            logger.warning(
                "Could not obtain Docker client for provenance pruning: %s", exc
            )
            return 0

    try:
        existing_ids = {str(c.id) for c in client.containers.list(all=True)}
    except Exception as exc:
        logger.warning("Could not list containers for provenance pruning: %s", exc)
        return 0

    removed = 0
    for path in list(provenance_dir.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        cid = path.stem if path.suffix == ".json" else path.name
        if not is_full_container_id(cid) or cid not in existing_ids:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed
