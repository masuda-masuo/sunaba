"""Environment preparation tool for locked uv projects.

Allows recovering a stale Python environment without recreating the container.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from docker.errors import NotFound

from sunaba.security import NETWORK_LABEL
from sunaba.tools import common
from sunaba.tools.container.clone import prepare_uv_environment
from sunaba.verify_state import (
    get_environment_state,
    invalidate_verify_success,
    verify_guard_acquire,
    verify_guard_release,
)

logger: logging.Logger = logging.getLogger(__name__)

_docker = common._docker


def _is_container_network_enabled(container: Any) -> bool:
    """Return True if the container has network access enabled."""
    allow_net = getattr(container, "allow_network", None)
    if allow_net is not None and isinstance(allow_net, bool):
        return allow_net

    labels = getattr(container, "labels", None)
    if not isinstance(labels, dict):
        labels = (getattr(container, "attrs", None) or {}).get("Config", {}).get("Labels") or {}
    val = labels.get(NETWORK_LABEL)
    if val is not None:
        return val == "true"
    host_config = (getattr(container, "attrs", None) or {}).get("HostConfig") or {}
    if host_config.get("NetworkMode") == "none":
        return False
    return True


def sandbox_prepare_environment(
    container_id: str,
    recreate: bool = False,
) -> str:
    """Prepare or update a locked uv Python environment in the container.

    Recovers a stale environment (when pyproject.toml, uv.lock, or
    .python-version changed) back to 'ready' without recreating the container.

    Args:
        container_id: Container ID.
        recreate: Wipe .venv.

    Returns:
        JSON string with preparation status and details.
    """
    # Issue #969 Spec 4: reject when verify (or prepare) is already running
    if not verify_guard_acquire(container_id):
        return json.dumps({
            "status": "in_progress",
            "error": "verify or environment preparation is in progress for this container",
            "rejected": True,
        })

    try:
        # Issue #969 Spec 1: at start, invalidate any recorded verify success
        invalidate_verify_success(container_id)

        client = common._docker()
        try:
            container = client.containers.get(container_id)
        except NotFound:
            return json.dumps({
                "status": "error",
                "error": f"Container {container_id[:12]} not found",
            })
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": str(e),
            })

        full_cid = str(getattr(container, "id", container_id))
        invalidate_verify_success(full_cid)

        # Target only the recorded project_root (or the clone root when none is recorded)
        state = get_environment_state(full_cid) or get_environment_state(container_id)
        project_root: str | None = None
        if state:
            project_root = state.get("project_root")
            if not project_root:
                recorded_fp = state.get("fingerprint")
                if isinstance(recorded_fp, dict):
                    project_root = recorded_fp.get("root")

        if not project_root:
            from sunaba.tools.vcs.gitroot import resolve_git_root

            try:
                project_root = resolve_git_root(container)
            except Exception:
                project_root = "/workspace"

        allow_network = _is_container_network_enabled(container)
        if not allow_network:
            prepare_uv_environment(
                container,
                project_root,
                allow_network=False,
                recreate=recreate,
            )
            return json.dumps({
                "status": "error",
                "error": "network_disabled",
                "reason": "network_disabled",
            })

        res = prepare_uv_environment(
            container,
            project_root,
            allow_network=True,
            recreate=recreate,
        )

        if res.success:
            return json.dumps({
                "status": "ok",
                "environment": "ready",
                "project_root": project_root,
                "command": "uv sync --locked --all-groups",
                "message": res.message,
            })
        else:
            return json.dumps({
                "status": "error",
                "error": res.message,
                "reason": res.reason,
                "exit_code": res.exit_code,
            })
    finally:
        verify_guard_release(container_id)
