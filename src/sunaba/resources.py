"""Host-side resource observations for the dashboard (Issue #783, phase 1).

Pure observation -- no thresholds, no policies (phase 2 decides those after
real measurements).  :func:`measure_disk_usage` probes the host once and
returns the Docker image / container-layer footprint plus the
journal/trace directory size; :func:`cached_disk_usage` wraps it with a
time interval so page renders never trigger a probe per request ("no du
per page render", Issue #783 acceptance).

The probe is injectable so tests can substitute a fake measurement; the
default probe degrades gracefully when Docker is unavailable (the sizes
come back ``None`` with an ``error`` string, never an exception).
"""

from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

#: Default minimum interval between disk probes (seconds).  A page render
#: within this window reuses the cached measurement.
_DEFAULT_DISK_PROBE_INTERVAL_S: float = 60.0

#: Module-level probe cache: ``{"ts": monotonic-epoch of last probe,
#: "value": last measurement, "probing": background-refresh-in-progress}``.
#: Guarded by ``_disk_cache_lock``, which is only ever held for cache
#: reads/writes -- never across a probe (issue #783 review).
_disk_cache_lock: threading.Lock = threading.Lock()
_disk_cache: dict[str, Any] = {"ts": 0.0, "value": None, "probing": False}


def _dir_bytes(path: Path) -> int:
    """Return the total size in bytes of *path* (0 when missing).

    Walks every file under *path* (journal, trace and sidecar files all
    live under ``~/.sunaba``); unreadable entries are skipped.
    """
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _measure_docker_disk() -> dict[str, Any]:
    """Probe the Docker image + container-layer footprint via the docker SDK.

    * Images: sum of each image's ``Size`` (rootfs size).  Shared layers
      are counted once per image, so this is an upper bound on unique
      image usage.
    * Containers: sum of ``SizeRw`` (the writable layer) from the
      low-level API's ``size=True`` listing.

    Any failure (docker absent, daemon down, permissions) is reported in
    ``error`` with ``None`` sizes so the dashboard degrades gracefully.
    """
    result: dict[str, Any] = {
        "images_bytes": None,
        "containers_bytes": None,
        "error": None,
    }
    try:
        import docker

        client = docker.from_env()
        images = client.images.list(all=True)
        result["images_bytes"] = sum(int(img.attrs.get("Size") or 0) for img in images)
        containers = client.api.containers(all=True, size=True)
        result["containers_bytes"] = sum(int(c.get("SizeRw") or 0) for c in containers)
    except Exception as e:  # docker absent / daemon down / permission
        result["error"] = str(e)
    return result


def _default_probe() -> dict[str, Any]:
    """Measure the host-side disk usage components of the #783 observation.

    Components:

    * ``docker`` -- image + container-writable-layer sizes (see
      :func:`_measure_docker_disk`; ``error`` when Docker is unreachable),
    * ``journal_dir`` -- the ``~/.sunaba`` directory (journal log, backup,
      trace files and the container-state sidecar).
    """
    from sunaba.journal import get_journal_dir

    jdir = Path(get_journal_dir())
    docker_part = _measure_docker_disk()

    known: list[int] = []
    if docker_part["images_bytes"] is not None:
        known.append(docker_part["images_bytes"])
    if docker_part["containers_bytes"] is not None:
        known.append(docker_part["containers_bytes"])

    return {
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "docker": docker_part,
        "journal_dir": {
            "path": str(jdir),
            "bytes": _dir_bytes(jdir),
        },
        # Total of the measurable components; None when Docker could not
        # be probed (the journal/trace component is still reported).
        "total_bytes": sum(known) if known else None,
    }


def measure_disk_usage(
    probe: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Measure host-side disk usage once (Issue #783, observation 2).

    *probe* is injectable for tests; the default probe measures Docker
    images + container writable layers via the docker SDK and the size of
    the journal/trace directory (``~/.sunaba``).
    """
    if probe is None:
        probe = _default_probe
    return probe()


def _refresh_disk_cache(probe: Callable[[], dict[str, Any]] | None) -> None:
    """Run one probe and publish it to the cache (background single-flight).

    Executes on a daemon thread spawned by :func:`cached_disk_usage` when a
    render finds the cache stale: the render serves the previous value
    immediately and this worker replaces it when the probe finishes.  The
    ``probing`` flag guarantees at most one refresh worker at a time; it is
    always cleared, even when the probe raises (only injectable test probes
    can -- the default probe reports failures in-band).
    """
    try:
        value = measure_disk_usage(probe=probe)
        with _disk_cache_lock:
            _disk_cache["value"] = value
            _disk_cache["ts"] = time.monotonic()
    finally:
        with _disk_cache_lock:
            _disk_cache["probing"] = False


def cached_disk_usage(
    interval_s: float = _DEFAULT_DISK_PROBE_INTERVAL_S,
    *,
    force: bool = False,
    probe: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the cached disk observation, re-probing at most once per
    *interval_s* seconds.

    Page renders (including the dashboard's 1.5s live poll) call this
    instead of :func:`measure_disk_usage`.  The cache lock is never held
    across a probe (docker SDK calls + a directory walk), and a render
    never waits for one either (issue #783 review): a stale cache is
    served as-is while a single background thread refreshes it.  Only the
    first call ever (nothing cached yet) and ``force=True`` (test knob)
    probe synchronously -- with the lock released.

    The returned dict is a deep copy: callers may mutate it freely.
    """
    now = time.monotonic()
    with _disk_cache_lock:
        value = _disk_cache["value"]
        if not force and value is not None:
            if now - _disk_cache["ts"] < interval_s:
                return copy.deepcopy(value)
            # Stale: serve the previous measurement immediately and let one
            # background worker refresh it (stale-while-revalidate).
            if not _disk_cache["probing"]:
                _disk_cache["probing"] = True
                threading.Thread(
                    target=_refresh_disk_cache,
                    args=(probe,),
                    name="sunaba-disk-probe",
                    daemon=True,
                ).start()
            return copy.deepcopy(value)
    # First-ever measurement (or force): probe on the calling thread with
    # the lock released.  Two racing first calls may both probe; the loser
    # merely overwrites an equally fresh value.
    fresh = measure_disk_usage(probe=probe)
    with _disk_cache_lock:
        _disk_cache["value"] = fresh
        _disk_cache["ts"] = time.monotonic()
    return copy.deepcopy(fresh)
