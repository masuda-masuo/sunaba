"""Content-addressable result cache for command execution (§3.2).

Caches command execution results keyed by SHA256 of
(image + commands + input_hash).  When the same image,
same commands, and same input hash are seen again, returns
the cached result with ``cached: true``.

Cache entries are stored in ``~/.code-sandbox-mcp/cache/`` as
individual JSON files named by their content-addressable key.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_DIR: Path = Path.home() / ".code-sandbox-mcp" / "cache"
_CACHE_TTL_SECONDS: int = 86400 * 7  # 7 days
_MAX_CACHE_ENTRIES: int = 1000
_MAX_CACHE_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB

#: Module-level lock for thread-safe cache operations.
_lock: threading.Lock = threading.Lock()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


# ---------------------------------------------------------------------------
# Cache key computation
# ---------------------------------------------------------------------------


def compute_cache_key(
    image: str,
    commands: list[str],
    input_hash: str = "",
) -> str:
    """Compute a content-addressable cache key.

    Args:
        image: Docker image reference (e.g. ``python@sha256:abcd``).
        commands: List of shell commands.
        input_hash: Optional hash of any input data that affects output.

    Returns:
        Hex digest suitable for use as a cache filename.
    """
    parts = [image, json.dumps(commands, sort_keys=True), input_hash]
    canonical = "\0".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Cache read/write
# ---------------------------------------------------------------------------


def get_cached_result(key: str) -> dict[str, Any] | None:
    """Return the cached result dict for *key*, or ``None``.

    Returns ``None`` if the cache entry does not exist, is expired,
    or is corrupted.
    """
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None

    try:
        with _lock:
            with open(path, "r", encoding="utf-8") as f:
                entry: dict[str, Any] = json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupted entry, remove it
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    # Check TTL
    ts = entry.get("ts", 0)
    if time.time() - ts > _CACHE_TTL_SECONDS:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    return entry.get("result")


def _evict_oldest() -> None:
    """Evict the oldest entries when cache limits are exceeded."""
    paths = sorted(
        _CACHE_DIR.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    # Enforce entry count limit
    while len(paths) > _MAX_CACHE_ENTRIES:
        try:
            paths[0].unlink(missing_ok=True)
            paths.pop(0)
        except OSError:
            break
    # Enforce total size limit
    total_size = sum(p.stat().st_size for p in paths)
    while total_size > _MAX_CACHE_SIZE_BYTES and len(paths) > 1:
        try:
            total_size -= paths[0].stat().st_size
            paths[0].unlink(missing_ok=True)
            paths.pop(0)
        except OSError:
            break


def set_cached_result(
    key: str,
    result: dict[str, Any],
    run_id: str = "",
) -> None:
    """Store *result* in the cache under *key*.

    Args:
        key: Content-addressable cache key.
        result: The result dict to cache (must be JSON-serializable).
        run_id: Optional run_id for traceability.
    """
    _ensure_cache_dir()
    entry: dict[str, Any] = {
        "key": key,
        "result": result,
        "ts": time.time(),
        "run_id": run_id,
        "size_bytes": len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
    }
    path = _CACHE_DIR / f"{key}.json"
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False)

    # Enforce cache limits (evict oldest if exceeded)
    _evict_oldest()


def invalidate_cache(key: str | None = None) -> int:
    """Invalidate cache entries.

    Args:
        key: If provided, only invalidate this specific key.
             If ``None``, invalidate **all** cache entries.

    Returns:
        Number of entries invalidated.
    """
    _ensure_cache_dir()
    invalidated = 0

    if key is not None:
        path = _CACHE_DIR / f"{key}.json"
        if path.exists():
            path.unlink()
            invalidated = 1
        return invalidated

    for path in _CACHE_DIR.glob("*.json"):
        try:
            path.unlink()
            invalidated += 1
        except OSError:
            pass
    return invalidated


# ---------------------------------------------------------------------------
# Cache statistics
# ---------------------------------------------------------------------------


def get_cache_stats() -> dict[str, Any]:
    """Return cache statistics for dashboard display.

    Returns:
        Dict with ``total_entries``, ``total_size_bytes``,
        ``oldest_entry_ts``, ``newest_entry_ts``.
    """
    _ensure_cache_dir()
    paths = list(_CACHE_DIR.glob("*.json"))
    if not paths:
        return {
            "total_entries": 0,
            "total_size_bytes": 0,
            "oldest_entry_ts": None,
            "newest_entry_ts": None,
        }

    sizes = []
    mtimes = []
    for path in paths:
        try:
            st = path.stat()
            sizes.append(st.st_size)
            mtimes.append(st.st_mtime)
        except OSError:
            pass

    if not sizes:
        return {
            "total_entries": 0,
            "total_size_bytes": 0,
            "oldest_entry_ts": None,
            "newest_entry_ts": None,
        }

    return {
        "total_entries": len(sizes),
        "total_size_bytes": sum(sizes),
        "oldest_entry_ts": min(mtimes),
        "newest_entry_ts": max(mtimes),
    }
