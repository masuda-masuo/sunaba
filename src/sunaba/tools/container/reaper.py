"""Container reaping (orphan + idle GC)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from docker.errors import APIError, NotFound

from sunaba.journal import (
    get_last_activity_per_container,
    read_container_states,
    record_stop,
)
from sunaba.security import (
    CREATED_AT_LABEL,
    KIND_SANDBOX,
    MANAGED_LABEL,
)
from sunaba.tools.common import (
    RECOVERY_DOCKER_TIMEOUT,
)

from .listing import _age_seconds, _container_kind

logger: logging.Logger = logging.getLogger(__name__)

#: Grace period before an init-incomplete container is considered orphaned
#: and reaped (Issue #298).  Comfortably longer than any legitimate
#: ``sandbox_initialize`` so an in-progress init (possibly in a concurrent
#: session) is never mistaken for an orphan.
_ORPHAN_GRACE_SECONDS: int = 600

#: Env var name for optional container idle TTL (Issue #480).
#: When set to a positive integer, containers idle longer than this
#: many seconds are automatically stopped by :func:`_reap_idle_containers`.
#: Default 0 = disabled (no automatic idle reaping).
_CONTAINER_TTL_ENV: str = "SUNABA_CONTAINER_TTL_SECONDS"


def _journal_container_status() -> dict[str, dict[str, Any]]:
    """Summarise per-container lifecycle status from the journal.

    Returns a mapping ``container_id -> {complete, used, stopped, init_ts}``
    where *complete* means an ``initialize_complete`` event was seen and
    *used* means at least one ``exec``.  Containers with an explicit
    ``stop`` are pruned from the mapping entirely (absence = no lifecycle
    info), so ``stopped`` is never ``True`` — the key survives only for
    interface stability.
    """
    return read_container_states()


def _reap_orphaned_init_containers(client: Any = None) -> list[str]:
    """Stop & remove containers orphaned by a timed-out ``sandbox_initialize``.

    Best-effort, opportunistic GC (Issue #298).  A container is reaped only
    when *all* of these hold, so healthy / in-progress containers are never
    touched:

    - it carries our management label *and* the ``created_at`` label, i.e. it
      was created by ``sandbox_initialize`` (test-environment and other
      managed containers lack ``created_at`` and are skipped);
    - the journal shows no ``initialize_complete`` (setup never finished), no
      ``exec`` (never used), and no ``stop``;
    - it is older than :data:`_ORPHAN_GRACE_SECONDS`, so a still-running init
      (possibly in another session) is never mistaken for an orphan.

    Failures are swallowed: GC must never break the caller's init.  Returns
    the list of reaped container-id prefixes.
    """
    from sunaba.tools.container import _docker
    reaped: list[str] = []
    client = client or _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
    except Exception as e:
        logger.warning("orphan reap: failed to list containers: %s", e)
        return reaped
    if not containers:
        return reaped

    status = _journal_container_status()
    now = datetime.now(timezone.utc)
    for container in containers:
        cid = container.id[:12]
        labels = getattr(container, "labels", None) or {}
        created_label = labels.get(CREATED_AT_LABEL)
        if created_label is None:
            # Not a sandbox_initialize container (e.g. test environment) — skip.
            continue
        s = status.get(cid, {})
        if s.get("complete") or s.get("used") or s.get("stopped"):
            # Setup finished, container used, or already stopped — not an orphan.
            continue
        age = _age_seconds(s.get("init_ts") or created_label, now)
        if age is None or age < _ORPHAN_GRACE_SECONDS:
            # Unknown age or still within the grace grant (in-progress init).
            continue
        try:
            container.kill()
        except (NotFound, APIError):
            pass
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except Exception as e:
            logger.warning("orphan reap: failed to remove %s: %s", cid, e)
            continue
        record_stop(cid)
        reaped.append(cid)
        logger.info("Reaped orphaned init container %s (age=%.0fs)", cid, age)
    return reaped


def _get_container_ttl_seconds() -> int:
    """Read the optional container idle TTL from the environment.

    Returns:
        Positive integer seconds, or 0 when TTL is not configured
        (auto-reap disabled).
    """
    val = os.environ.get(_CONTAINER_TTL_ENV)
    if val is None or val.strip() == "":
        return 0
    try:
        ttl = int(val.strip())
        if ttl <= 0:
            return 0
        return ttl
    except (ValueError, TypeError):
        return 0


def _reap_idle_containers() -> list[str]:
    """Stop containers idle longer than the configured TTL (Issue #480).

    Reads :envvar:`SUNABA_CONTAINER_TTL_SECONDS` to determine the
    threshold.  When the env var is not set or is 0, this is a no-op
    (auto-reap disabled by default).

    Only sandboxes are considered: ``MANAGED_LABEL`` also matches the
    egress-proxy sidecar, which is shared infrastructure and must never
    be reaped.  A container is ``idle`` when no journal entry exists for
    it in the last TTL seconds.  Failures are swallowed (best-effort GC).

    Runs from both ``sandbox_initialize`` and ``sandbox_list_containers``.

    Returns:
        List of 12-character container ID prefixes that were stopped.
    """
    from sunaba.tools.container import _docker
    ttl = _get_container_ttl_seconds()
    if ttl <= 0:
        return []  # Opt-in only -- no auto-stop by default

    client = _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
    except Exception as e:
        logger.warning("idle reap: failed to list containers: %s", e)
        return []

    last_activity = get_last_activity_per_container()
    now = datetime.now(timezone.utc)
    reaped: list[str] = []
    for c in containers:
        cid = c.id[:12]
        # MANAGED_LABEL alone also matches the egress-proxy sidecar, which is
        # shared infrastructure with no journal activity of its own.  Reaping
        # it would break networked init for every other container, so scope
        # the reaper to sandboxes explicitly instead of relying on the sidecar
        # merely happening to have no activity timestamp.
        labels = getattr(c, "labels", None) or {}
        if _container_kind(labels, getattr(c, "name", None)) != KIND_SANDBOX:
            continue
        last_ts = last_activity.get(cid)
        idle_secs = _age_seconds(last_ts, now)
        if idle_secs is None or idle_secs < ttl:
            continue
        logger.info(
            "Reaping idle container %s (idle=%.0fs, ttl=%ds)", cid, idle_secs, ttl
        )
        try:
            c.kill()
        except (NotFound, APIError):
            pass
        try:
            c.remove(force=True)
        except NotFound:
            pass
        except Exception as e:
            logger.warning("idle reap: failed to remove %s: %s", cid, e)
            continue
        record_stop(cid)
        reaped.append(cid)
    return reaped

