"""Container listing and discovery."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sunaba import proxy_lifecycle
from sunaba.journal import (
    get_last_activity_per_container,
)
from sunaba.security import (
    CREATED_AT_LABEL,
    KIND_LABEL,
    KIND_PROXY,
    KIND_SANDBOX,
    MANAGED_LABEL,
    NAME_LABEL,
    NETWORK_LABEL,
)

logger: logging.Logger = logging.getLogger(__name__)


def _age_seconds(iso_ts: str | None, now: datetime) -> float | None:
    """Return seconds elapsed since *iso_ts*, or ``None`` if unparseable."""
    if not iso_ts:
        return None
    try:
        created = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()


def _label_network(labels: dict[str, str]) -> bool | None:
    """Read ``NETWORK_LABEL`` back as a bool, or ``None`` when absent.

    ``None`` means "this container predates the label" (Issue #527), which is
    deliberately distinct from ``False`` -- we report the gap rather than
    guessing a posture we cannot know.
    """
    raw = labels.get(NETWORK_LABEL)
    if raw is None:
        return None
    return raw == "true"


def _container_kind(labels: dict[str, str], docker_name: str | None) -> str:
    """Classify a managed container as ``sandbox`` or ``proxy`` (Issue #527).

    Falls back to the sidecar's fixed container name for containers created
    before ``KIND_LABEL`` existed -- notably the long-lived egress proxy, which
    has ``restart_policy=unless-stopped`` and so survives an upgrade unlabelled
    until something recreates it.
    """
    kind = labels.get(KIND_LABEL)
    if kind:
        return kind
    if docker_name == proxy_lifecycle.PROXY_CONTAINER_NAME:
        return KIND_PROXY
    return KIND_SANDBOX


def _find_containers_by_name(client, name: str) -> list[str]:
    """Find running containers with the given NAME_LABEL.

    Returns a list of 12-character container ID prefixes.
    """
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": f"{NAME_LABEL}={name}"},
        )
    except Exception:
        return []
    return [c.id[:12] for c in containers if c.status == "running"]


# Label-based discovery survives server restarts (#478); the egress-proxy
# sidecar carries MANAGED_LABEL too, so it is listed but tagged (#527).


def sandbox_list_containers() -> str:
    """List all managed sandbox containers with metadata.

    Discovery is Docker-label based, so it works across server
    restarts; use it to find existing containers before starting a new
    one.  The egress-proxy sidecar is listed with kind='proxy'; only
    kind='sandbox' entries are attachable.  When a container TTL is
    configured, idle containers are reaped first and reported in
    reaped_ids.

    Returns:
        JSON containers array: container_id, name, kind, image, status,
        allow_network, created_at, age_seconds, idle_seconds,
        last_activity_ts; plus reaped_ids when reaping ran.
    """
    import json

    from .reaper import _reap_idle_containers

    reaped_ids = _reap_idle_containers()
    containers, error = list_managed_containers()
    if error is not None:
        return json.dumps({"containers": [], "error": error, "reaped_ids": reaped_ids})

    return json.dumps({
        "containers": containers,
        "reaped_ids": reaped_ids,
    }, ensure_ascii=False)


def list_managed_containers() -> tuple[list[dict[str, Any]], str | None]:
    """Return metadata for every managed container, plus an error message.

    The read half of :func:`sandbox_list_containers`, split out so that
    read-only consumers -- the dashboard's ``/containers`` page (Issue #527) --
    can list containers without also *reaping* them: a page that auto-refreshes
    every 10s must not quietly tear down containers as a side effect of being
    looked at.

    Returns ``(containers, None)`` on success, or ``([], message)`` when Docker
    cannot be reached.
    """
    from sunaba.tools.container import _docker
    client = _docker()
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )
    except Exception as e:
        return [], str(e)

    last_activity = get_last_activity_per_container()
    now = datetime.now(timezone.utc)
    result: list[dict[str, Any]] = []
    for c in containers:
        cid = c.id[:12]
        labels = getattr(c, "labels", None) or {}
        created_raw = labels.get(CREATED_AT_LABEL)
        name_val = labels.get(NAME_LABEL)
        age = _age_seconds(created_raw, now)

        idle = _age_seconds(last_activity.get(cid), now)
        last_ts = last_activity.get(cid)

        result.append({
            "container_id": cid,
            "name": name_val,
            "kind": _container_kind(labels, getattr(c, "name", None)),
            "image": c.image.tags[0] if c.image.tags else str(c.image.short_id),
            "status": c.status,
            "allow_network": _label_network(labels),
            "created_at": created_raw,
            "age_seconds": age,
            "idle_seconds": idle,
            "last_activity_ts": last_ts,
        })

    return result, None

