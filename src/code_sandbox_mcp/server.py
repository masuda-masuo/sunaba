"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time

from fastmcp import FastMCP

from code_sandbox_mcp.journal import (
    get_journal_path,
    get_runs,
    read_journal,
    record_boundary_crossing,
)
from code_sandbox_mcp.result_cache import (
    get_cache_stats,
    invalidate_cache,
)
from code_sandbox_mcp.security import (
    validate_image_ref,
)
from code_sandbox_mcp.token import (
    get_pending_tokens,
    reject_token,
    verify_token,
)
from code_sandbox_mcp.trace import (
    generate_html_trace,
    generate_json_trace,
    get_trace_dir,
)

from .tools.container import (
    rerun_failed,
    run_container_and_exec,
    run_test_environment,
    sandbox_exec_diff,
    sandbox_initialize,
    sandbox_stop,
    stop_test_environment,
    wait_for_condition,
)
from .tools.exec import (
    sandbox_exec,
    sandbox_exec_background,
    sandbox_exec_check,
)
from .tools.file import (
    copy_file,
    copy_project,
    list_files,
    read_file_range,
    write_file_sandbox,
)
from .tools.vcs import (
    clone_repo,
    issue_view,
    sandbox_create_pr,
    submit,
)
from .tools.verify import (
    apply_patch,
    lint_in_container,
    search_in_container,
    transform_file,
    type_check_in_container,
    verify_in_container,
)

logger: logging.Logger = logging.getLogger(__name__)

mcp = FastMCP("code-sandbox-mcp")


sandbox_exec = mcp.tool()(sandbox_exec)
sandbox_exec_background = mcp.tool()(sandbox_exec_background)
sandbox_exec_check = mcp.tool()(sandbox_exec_check)

issue_view = mcp.tool()(issue_view)
submit = mcp.tool()(submit)
sandbox_create_pr = mcp.tool()(sandbox_create_pr)
clone_repo = mcp.tool()(clone_repo)


# Container lifecycle tool registrations
sandbox_initialize = mcp.tool()(sandbox_initialize)
sandbox_stop = mcp.tool()(sandbox_stop)
run_container_and_exec = mcp.tool()(run_container_and_exec)
rerun_failed = mcp.tool()(rerun_failed)
sandbox_exec_diff = mcp.tool()(sandbox_exec_diff)
run_test_environment = mcp.tool()(run_test_environment)
stop_test_environment = mcp.tool()(stop_test_environment)
wait_for_condition = mcp.tool()(wait_for_condition)

# File tool registrations
write_file_sandbox = mcp.tool()(write_file_sandbox)
copy_project = mcp.tool()(copy_project)
copy_file = mcp.tool()(copy_file)
read_file_range = mcp.tool()(read_file_range)
list_files = mcp.tool()(list_files)

# Verify tool registrations
apply_patch = mcp.tool()(apply_patch)
transform_file = mcp.tool()(transform_file)
search_in_container = mcp.tool()(search_in_container)
lint_in_container = mcp.tool()(lint_in_container)
type_check_in_container = mcp.tool()(type_check_in_container)
verify_in_container = mcp.tool()(verify_in_container)


@mcp.tool()
def sandbox_cache_stats() -> str:
    """Return result cache statistics.

    Returns:
        JSON string with cache stats (total_entries, total_size_bytes,
        oldest/newest entry timestamps).
    """
    stats = get_cache_stats()
    return json.dumps(stats, ensure_ascii=False)


@mcp.tool()
def sandbox_cache_invalidate(key: str | None = None) -> str:
    """Invalidate result cache entries.

    Args:
        key: Optional specific cache key to invalidate.
             If omitted, all cache entries are invalidated.

    Returns:
        JSON string with ``invalidated`` count.
    """
    count = invalidate_cache(key=key)
    return json.dumps({"invalidated": count})



@mcp.tool()
def sandbox_read_journal(
    run_id: str | None = None,
    max_entries: int = 100,
) -> str:
    """Read the append-only execution journal.

    Returns JSON array of journal entries, optionally filtered by
    *run_id*.  The journal records every container lifecycle event
    (initialize, exec, stop) and boundary-crossing operation.

    Args:
        run_id: If provided, only return entries for this run.
            Omit to see all journal entries.
        max_entries: Maximum number of entries to return
            (most recent first, default 100).

    Returns:
        JSON string with a list of journal entry objects, each
        containing ``ts``, ``run_id``, ``container_id``,
        ``operation``, and operation-specific fields.
    """
    entries = read_journal(run_id=run_id, max_entries=max_entries)
    return json.dumps(entries, ensure_ascii=False)


@mcp.tool()
def sandbox_trace(
    run_id: str,
    format: str = "json",
) -> str:
    """Generate a replay trace for a specific run.

    Creates an HTML or JSON trace file from journal entries for
    *run_id*, enabling post-hoc review of "why did it do that?".

    Args:
        run_id: The run identifier to generate a trace for.
        format: Output format - ``"json"`` or ``"html"``
            (default ``"json"``).

    Returns:
        Path to the generated trace file, or an error message
        beginning with ``"Error:"``.
    """
    if format not in ("json", "html"):
        return "Error: format must be 'json' or 'html'"

    if format == "json":
        path = generate_json_trace(run_id)
    else:
        path = generate_html_trace(run_id)

    if not path:
        return f"Error: run_id {run_id} not found in journal"
    return path


@mcp.tool()
def sandbox_list_runs() -> str:
    """List all runs recorded in the execution journal.

    Returns a JSON array of run summaries, each with ``run_id``,
    ``started``, ``image``, ``operations``, ``boundary_crossings``,
    ``status``, and ``last_ts``.

    Returns:
        JSON string with a list of run summary objects.
    """
    runs = get_runs()
    return json.dumps(runs, ensure_ascii=False)


@mcp.tool()
def sandbox_journal_path() -> str:
    """Return the filesystem path to the execution journal file.

    Returns:
        Absolute path to ``~/.code-sandbox-mcp/journal.log``.
    """
    return get_journal_path()


@mcp.tool()
def sandbox_trace_dir() -> str:
    """Return the filesystem path to the trace output directory.

    Returns:
        Absolute path to ``~/.code-sandbox-mcp/traces/``.
    """
    return get_trace_dir()


@mcp.tool()
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
    # created_at と now は同一クロック (time.monotonic()) なので
    # スリープ/サスペンドの影響を受けず正確な残り時間が計算できる。
    now = time.monotonic()
    for p in pending:
        p["remaining_seconds"] = max(
            0,
            int(p["ttl_seconds"] - (now - p["created_at"])),
        )
        del p["created_at"]
        del p["ttl_seconds"]
    return json.dumps(pending, ensure_ascii=False)


@mcp.tool()
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


@mcp.tool()
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
    ok = reject_token(token)
    if not ok:
        return json.dumps(
            {
                "status": "error",
                "error": "Token not found or already resolved",
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "message": "Token rejected",
        }
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the MCP server.

    Exported separately so tests can exercise the parser without
    starting the server.
    """
    parser = argparse.ArgumentParser(description="Code Sandbox MCP Server")
    parser.add_argument(
        "--default-image",
        type=str,
        default=None,
        help="Default Docker image (default: python@sha256:...)",
    )
    parser.add_argument(
        "--shiori-repos-path",
        type=str,
        default=os.environ.get("SHIORI_REPOS_PATH"),
        help=(
            "Host path to Shiori repos root (e.g. /data/repos). "
            "When set, sandbox_initialize and run_container_and_exec "
            "can use clone_repo to copy a pre-cloned repo into the "
            "container instead of a network git clone. "
            "Also read from SHIORI_REPOS_PATH env var."
        ),
    )
    parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=["stdio", "sse", "http", "streamable-http"],
        help=(
            "MCP transport protocol (default: stdio). "
            "Use 'sse' or 'http' to avoid the ~60s client timeout. "
            "When using SSE/HTTP, specify --host and --port."
        ),
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host address for HTTP transport (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for HTTP transport (default: 8765)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=0,
        help=(
            "Start the observability web dashboard on localhost "
            "(default: 0 = disabled).  Suggested: 8766."
        ),
    )
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=None,
        help="Webhook URL for push notifications",
    )
    parser.add_argument(
        "--failure-threshold",
        type=int,
        default=5,
        help="Notify after N consecutive failures (default: 5)",
    )
    parser.add_argument(
        "--long-run-seconds",
        type=int,
        default=300,
        help="Notify after this many seconds of execution (default: 300)",
    )
    return parser


def main() -> None:
    """Parse CLI arguments and run the MCP server.

    ``--default-image`` for overriding the default Docker image,
    ``--transport`` to select the MCP transport protocol,
    ``--dashboard-port`` for the observability dashboard,
    and ``--webhook-url`` for push notifications.

    HTTP-based transports (``sse``, ``http``, ``streamable-http``)
    are not subject to the ~60-second client timeout that affects
    ``stdio``, making them suitable for long-running Docker
    operations such as ``docker pull`` or ``copy_project`` on
    large directories.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    from code_sandbox_mcp.tools import container as _ct_mod
    if args.default_image:
        validate_image_ref(args.default_image)
        _ct_mod._DEFAULT_IMAGE = args.default_image
    if args.shiori_repos_path:
        _ct_mod._SHIORI_REPOS_PATH = args.shiori_repos_path

    # Configure notifications if webhook is set
    if args.webhook_url or args.failure_threshold != 5 or args.long_run_seconds != 300:
        from code_sandbox_mcp.notify import configure

        configure(
            webhook_url=args.webhook_url,
            failure_threshold=args.failure_threshold,
            long_run_seconds=args.long_run_seconds,
        )

    # Start dashboard if requested
    if args.dashboard_port > 0:
        from code_sandbox_mcp.dashboard import start_dashboard

        msg = start_dashboard(port=args.dashboard_port)
        logger.info(msg)

    transport = args.transport
    if transport == "stdio":
        mcp.run(transport=transport)
    else:
        mcp.run(transport=transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
