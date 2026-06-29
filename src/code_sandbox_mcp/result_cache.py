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
import re
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

# --- Volatile command detection (issue #329) ---

# Git subcommands whose output or side effects depend on the
# working-tree / index / HEAD state.  Caching these returns
# stale results from a potentially different context
# (issue #329 § P1).
_VOLATILE_GIT_COMMANDS: frozenset[str] = frozenset({
    # Mutating — change repository state
    "add", "rm", "mv",
    "commit",
    "checkout", "switch",
    "merge",
    "rebase",
    "stash",
    "reset",
    "clean",
    "cherry-pick", "cherry",
    "revert",
    "am",
    "bisect",
    "clone", "init",
    "fetch", "pull", "push",
    "remote",
    "submodule",
    "worktree",
    "tag",                               # creating / deleting tags
    "branch",                            # creating / deleting branches
    # Inspecting — output reflects current state
    "diff",
    "status",
    "log",
    "show",
    "blame",
    "describe",
    "rev-parse", "rev-list",
    "ls-files", "ls-tree",
    "grep",
    "shortlog",
    "whatchanged",
})

# Non-git commands that depend on or mutate mutable filesystem state
# and should not be cached (issue #329 § P2).
_VOLATILE_NON_GIT_PROGRAMS: frozenset[str] = frozenset({
    "ls", "cat", "stat", "file", "find",
    "du", "df", "wc", "head", "tail",
    "touch", "mkdir", "rmdir", "chmod", "chown",
    "cp", "mv", "rm",
})

# Commands that are known to be safe to cache (non-volatile builds/installs).
# Used as an allow-list for programs we don't recognise otherwise.
_CACHEABLE_PROGRAMS: frozenset[str] = frozenset({
    "cd",  # shell builtin (no-output navigation)
    "echo", "printf",
    "pip", "pip3", "python", "python3",
    "npm", "npx", "yarn", "pnpm",
    "apt-get", "apt", "dpkg",
    "yum", "dnf", "rpm",
    "go", "cargo", "rustup",
    "gcc", "g++", "clang", "clang++",
    "make", "cmake", "ninja",
    "gem", "bundle",
    "curl", "wget",
    "pytest", "ruff", "pyright", "eslint", "mypy",
    "npx", "tsc", "node",
    "systemctl", "docker", "gh",
})


def _split_compound_commands(cmd: str) -> list[str]:
    """Split *cmd* on ``&&``, ``;``, ``||``, and ``|`` boundaries."""
    parts = re.split(r"&&|\|\||[;&|]", cmd)
    return [p.strip() for p in parts if p.strip()]


def _first_program(sub: str) -> str:
    """Return the program name from a subcommand string.

    Strips leading variable assignments (``VAR=val``) and returns
    the bare program name without flags or path components.
    """
    tokens = sub.strip().split()
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        idx += 1
    if idx >= len(tokens):
        return ""
    prog = tokens[idx]
    # Strip leading ./ ../ prefixes
    while prog.startswith("../") or prog.startswith("./"):
        prog = prog[2:] if prog.startswith("./") else prog[3:]
        # Re-check after stripping
    return prog.rsplit("/", 1)[-1]


def is_cacheable(commands: list[str]) -> bool:
    """Check whether *commands* can safely be cached (issue #329).

    Returns ``False`` for:

    * **Git subcommands** that mutate or inspect repository state
      (e.g. ``git add``, ``git diff``, ``git status``, ``git commit``)
      — :issue:`329` § P1.
    * **Non-git volatile programs** that read or write mutable files
      (e.g. ``ls``, ``cat``, ``rm``, ``touch``) when they appear
      outside a known-cacheable pipeline — :issue:`329` § P2.
    * **Compound commands** (``&&`` / ``;`` / ``||`` chaining or pipes)
      where **any** subcommand is volatile — caching the chain would
      skip the side-effect subcommands as well — :issue:`329` § P4.
    """
    for cmd in commands:
        stripped = cmd.strip()
        if not stripped:
            continue
        # P4: Split on &&, ;, ||, and | — if any subcommand is
        # volatile the whole chain is non-cacheable.
        subcommands = _split_compound_commands(stripped)
        for sub in subcommands:
            prog = _first_program(sub)
            if not prog:
                continue
            # P1: Volatile git subcommands
            if prog == "git":
                tokens = sub.strip().split()
                git_idx = 0
                while git_idx < len(tokens) and tokens[git_idx] != "git":
                    git_idx += 1
                if git_idx + 1 < len(tokens):
                    git_cmd_idx = git_idx + 1
                    while git_cmd_idx < len(tokens) and tokens[git_cmd_idx].startswith("-"):
                        git_cmd_idx += 1
                    if git_cmd_idx < len(tokens):
                        if tokens[git_cmd_idx] in _VOLATILE_GIT_COMMANDS:
                            return False
                continue
            # P2: Non-git volatile programs (deny-list only).
            # Unknown programs (scripts, tool wrappers, custom
            # binaries) are treated as potentially cacheable —
            # they produce deterministic output for a given input.
            if prog in _VOLATILE_NON_GIT_PROGRAMS:
                return False
    return True


def compute_cache_key(
    image: str,
    commands: list[str],
    input_hash: str = "",
    workspace_fingerprint: str = "",
) -> str:
    """Compute a content-addressable cache key.

    Args:
        image: Docker image reference (e.g. ``python@sha256:abcd``).
        commands: List of shell commands.
        input_hash: Optional hash of any input data that affects output.
        workspace_fingerprint: Optional hash of workspace state
            (e.g. ``git rev-parse HEAD`` + ``git status --porcelain``)
            to namespace the cache key per working-tree state
            (issue #329 § P3).

    Returns:
        Hex digest suitable for use as a cache filename.
    """
    parts = [image, json.dumps(commands, sort_keys=True), input_hash]
    if workspace_fingerprint:
        parts.append(workspace_fingerprint)
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
