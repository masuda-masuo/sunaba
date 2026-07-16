"""Per-edit undo history for file-editing tools (issue #599 follow-up).

Every mutating file operation (``write_file``, ``edit_file`` in all
modes including the AST edit path, ``transform_file``) saves the
pre-edit content of
the touched file here, host-side, before writing.  ``undo_file_edit``
restores the previous version -- the escape hatch that lets an LLM
caller step back to the last state *before* it broke a file instead
of trying to repair the broken text forward.

Storage layout (host-side, sibling of the execution journal)::

    ~/.sunaba/undo/<cid>/<sha1(file_path)>/
        path        # the file path this history belongs to
        v<N>        # content snapshots, N monotonically increasing

Per file a bounded ring of :data:`_MAX_VERSIONS` snapshots is kept;
files larger than :data:`_MAX_SNAPSHOT_BYTES` are not snapshotted
(the tools' callers never edit files that large in practice, and the
cap bounds host disk usage).  History for a container is removed when
the container is stopped.

Thread-safe via a module-level lock, mirroring the journal module.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import shutil
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

#: Root directory for undo snapshots.
_UNDO_ROOT: Path = Path.home() / ".sunaba" / "undo"

#: Snapshots kept per file (oldest pruned first).
_MAX_VERSIONS: int = 10

#: Files larger than this are not snapshotted (5 MB).
_MAX_SNAPSHOT_BYTES: int = 5 * 1024 * 1024

_lock: threading.Lock = threading.Lock()


def _file_dir(container_id: str, file_path: str) -> Path:
    """Return the snapshot directory for *file_path* in *container_id*."""
    canon = posixpath.normpath(file_path)
    digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()
    return _UNDO_ROOT / container_id[:12] / digest


def _versions(d: Path) -> list[Path]:
    """Version files in *d*, oldest first."""
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.name.startswith("v") and p.name[1:].isdigit()]
    return sorted(files, key=lambda p: int(p.name[1:]))


def save_version(container_id: str, file_path: str, content: str) -> None:
    """Snapshot *content* as the newest undo version for *file_path*.

    Call with the file's **pre-edit** content, immediately before
    writing the new content.  Best-effort: an OS error is logged but
    never raised -- an edit must never fail because its undo snapshot
    could not be written.
    """
    data = content.encode("utf-8")
    if len(data) > _MAX_SNAPSHOT_BYTES:
        return
    try:
        with _lock:
            d = _file_dir(container_id, file_path)
            d.mkdir(parents=True, exist_ok=True)
            (d / "path").write_text(
                posixpath.normpath(file_path), encoding="utf-8"
            )
            existing = _versions(d)
            next_n = int(existing[-1].name[1:]) + 1 if existing else 1
            (d / f"v{next_n}").write_bytes(data)
            for stale in existing[: max(0, len(existing) + 1 - _MAX_VERSIONS)]:
                stale.unlink(missing_ok=True)
    except OSError as e:
        # The caller already reported the edit as successful, so a lost
        # snapshot must at least be visible to the server operator
        # (disk full, permissions) -- undo_file_edit will not cover
        # this edit.
        logger.warning(
            "undo snapshot failed for %s (container %s): %s",
            file_path, container_id[:12], e,
        )


def list_versions(container_id: str, file_path: str) -> list[dict[str, object]]:
    """Return available snapshots for *file_path*, newest first.

    Each entry has ``steps`` (1 = most recent pre-edit state, the value
    to pass to ``undo_file_edit``), ``saved_at`` (ISO-8601 UTC), and
    ``size_bytes``.
    """
    from datetime import datetime, timezone

    with _lock:
        files = _versions(_file_dir(container_id, file_path))
    out: list[dict[str, object]] = []
    for steps, p in enumerate(reversed(files), start=1):
        try:
            stat = p.stat()
        except OSError:
            continue
        out.append({
            "steps": steps,
            "saved_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "size_bytes": stat.st_size,
        })
    return out


def get_version(container_id: str, file_path: str, steps: int = 1) -> str | None:
    """Return the snapshot content *steps* versions back, or ``None``.

    ``steps=1`` is the most recent snapshot (the file as it was right
    before the last edit).
    """
    if steps < 1:
        return None
    with _lock:
        files = _versions(_file_dir(container_id, file_path))
        if steps > len(files):
            return None
        target = files[len(files) - steps]
        try:
            return target.read_text(encoding="utf-8")
        except OSError:
            return None


def clear_history(container_id: str) -> None:
    """Remove all undo history for *container_id* (container stopped)."""
    with _lock:
        shutil.rmtree(_UNDO_ROOT / container_id[:12], ignore_errors=True)
