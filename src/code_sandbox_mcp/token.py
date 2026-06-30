"""Token-based approval mechanism for boundary-crossing write operations (#50).

Implements a two-step token flow for operations that cross the sandbox
boundary with side effects (VCS writes, persistent resource deletion, etc.):

1. **dry_run** — returns an execution plan and a confirmation token.
2. **execute**  — the token must be provided; execution is unconditionally
   rejected without a valid, unexpired token.

Thread-safe via a module-level lock.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TOKEN_TTL_SECONDS: int = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

@dataclass
class _TokenEntry:
    token: str
    operation: str
    details: str
    container_id: str
    run_id: str
    created_at: float  # time.monotonic() timestamp
    ttl_seconds: int
    consumed: bool = False


_lock: threading.Lock = threading.Lock()
_store: dict[str, _TokenEntry] = {}


def _purge_expired() -> None:
    """Remove expired tokens from the in-memory store.

    呼び出し元は必ず ``_lock`` を獲得していること（本関数は
    ロック獲得を前提とし、自身ではロックを取得しない）。
    """
    now = time.monotonic()
    expired = [
        k for k, v in _store.items()
        if now - v.created_at > v.ttl_seconds
    ]
    for k in expired:
        del _store[k]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_token(
    operation: str,
    details: str,
    container_id: str,
    run_id: str,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    """Generate a confirmation token for a boundary-crossing write operation.

    Args:
        operation: Operation type (e.g. ``"git_push"``, ``"pr_create"``).
        details: Human-readable summary of what will be done.
        container_id: 12-character container ID prefix.
        run_id: Run identifier from the journal.
        ttl_seconds: Token lifetime in seconds (default 300 = 5 min).

    Returns:
        A token string to be passed back for execution confirmation.
    """
    token = secrets.token_hex(16)  # 32-char hex string
    entry = _TokenEntry(
        token=token,
        operation=operation,
        details=details,
        container_id=container_id,
        run_id=run_id,
        created_at=time.monotonic(),
        ttl_seconds=ttl_seconds,
    )
    with _lock:
        _purge_expired()
        _store[token] = entry
    return token


def verify_token(token: str) -> dict[str, Any] | None:
    """Verify a confirmation token without consuming it (peek).

    Unlike :func:`verify_and_consume`, this function does NOT mark
    the token as consumed.  Use this for approval flows where the
    token should remain valid for the subsequent execution step.

    Args:
        token: The confirmation token string.

    Returns:
        A dict with token metadata if valid, or ``None`` if
        invalid/expired/already used.
    """
    with _lock:
        _purge_expired()
        if token not in _store:
            return None
        entry = _store[token]
        if entry.consumed:
            return None
        if time.monotonic() - entry.created_at > entry.ttl_seconds:
            del _store[token]
            return None
        return {
            "token": entry.token,
            "operation": entry.operation,
            "details": entry.details,
            "container_id": entry.container_id,
            "run_id": entry.run_id,
        }


def verify_and_consume(token: str) -> dict[str, Any] | None:
    """Verify a confirmation token and mark it as consumed.

    The token is consumed (one-time use) on successful verification.

    Args:
        token: The confirmation token string.

    Returns:
        A dict with token metadata if valid, or ``None`` if invalid/expired/already used.
    """
    with _lock:
        _purge_expired()
        if token not in _store:
            return None
        entry = _store[token]
        if entry.consumed:
            return None
        # Double-check expiry
        if time.monotonic() - entry.created_at > entry.ttl_seconds:
            del _store[token]
            return None
        entry.consumed = True
        return {
            "token": entry.token,
            "operation": entry.operation,
            "details": entry.details,
            "container_id": entry.container_id,
            "run_id": entry.run_id,
        }


def reject_token(token: str) -> bool:
    """Reject a pending token, removing it from the store.

    Args:
        token: The confirmation token string.

    Returns:
        ``True`` if the token was found and rejected, ``False`` if not found.
    """
    with _lock:
        _purge_expired()
        if token in _store:
            del _store[token]
            return True
        return False


def get_pending_tokens() -> list[dict[str, Any]]:
    """Return all pending (unconsumed, unexpired) tokens.

    Returns:
        List of dicts with token metadata, sorted by creation time (oldest first).
    """
    with _lock:
        _purge_expired()
        results: list[dict[str, Any]] = []
        for entry in _store.values():
            if entry.consumed:
                continue
            results.append({
                "token": entry.token,
                "operation": entry.operation,
                "details": entry.details,
                "container_id": entry.container_id,
                "run_id": entry.run_id,
                "created_at": entry.created_at,
                "ttl_seconds": entry.ttl_seconds,
            })
        results.sort(key=lambda r: r["created_at"])
        return results
