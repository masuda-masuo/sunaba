"""Approval tools: sandbox_approval_status, sandbox_approve, sandbox_reject."""

from __future__ import annotations

import json
import time

from code_sandbox_mcp.journal import (
    record_boundary_crossing,
)
from code_sandbox_mcp.token import (
    get_pending_tokens,
    reject_token,
    verify_token,
)


def sandbox_approval_status() -> str:
    """List all pending approval tokens for boundary-crossing operations.

    Returns a JSON array of pending tokens, each with ``token``,
    ``operation``, ``details``, ``container_id``, ``run_id``,
    and ``remaining_seconds``.

    Use :func:`sandbox_approve` or :func:`sandbox_reject` to resolve
    a pending token.  Tokens expire after a configurable TTL (default
    5 minutes).

    Returns:
        JSON string with a list of pending token objects.
    """
    pending = get_pending_tokens()
    now = time.monotonic()
    for p in pending:
        p = dict(p)
        p["remaining_seconds"] = max(
            0,
            int(p["ttl_seconds"] - (now - p["created_at"])),
        )
        del p["created_at"]
        del p["ttl_seconds"]
    return json.dumps(pending, ensure_ascii=False)


def sandbox_approve(token: str) -> str:
    """Approve a pending boundary-crossing operation.

    Verifies the token and records approval in the execution journal.
    Once approved, the operation that requested the token can proceed.

    Args:
        token: The confirmation token string (from dry_run output,
            ``sandbox_approval_status``, or the dashboard).

    Returns:
        JSON string with ``status`` and metadata, or error details.
    """
    result = verify_token(token)
    if result is None:
        return json.dumps(
            {
                "status": "error",
                "error": "Token invalid, expired, or already used",
            }
        )
    record_boundary_crossing(
        result["container_id"],
        result["operation"],
        result["details"],
        approved=True,
        token=token,
    )
    return json.dumps(
        {
            "status": "ok",
            "operation": result["operation"],
            "details": result["details"],
            "container_id": result["container_id"],
            "run_id": result["run_id"],
        }
    )


def sandbox_reject(token: str) -> str:
    """Reject a pending boundary-crossing operation.

    Removes the token from the pending queue.  The operation that
    requested the token will not be able to proceed without a new
    token.

    Args:
        token: The confirmation token string to reject.

    Returns:
        JSON string with ``status`` and message.
    """
    # Peek the token metadata *before* rejecting so the rejection can be
    # recorded in the journal.  Without this, a rejected boundary crossing
    # leaves its original ``approved=None`` entry unresolved and
    # ``get_pending_approvals()`` reports the token as pending forever
    # (asymmetry with sandbox_approve; Issue #359).
    meta = verify_token(token)
    ok = reject_token(token)
    if not ok:
        return json.dumps(
            {
                "status": "error",
                "error": "Token not found or already resolved",
            }
        )
    if meta is not None:
        record_boundary_crossing(
            meta["container_id"],
            meta["operation"],
            meta["details"],
            approved=False,
            token=token,
        )
    return json.dumps(
        {
            "status": "ok",
            "message": "Token rejected",
        }
    )
