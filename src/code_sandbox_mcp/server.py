"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time

from fastmcp import FastMCP

from code_sandbox_mcp.github_auth import setup_github_app_token
from code_sandbox_mcp.result_cache import (
    get_cache_stats,
    invalidate_cache,
)
from code_sandbox_mcp.security import (
    compute_default_limits,
    set_default_profile,
    validate_image_ref,
)

from .tools.approval import (
    sandbox_approval_status,
    sandbox_approve,
    sandbox_reject,
)
from .tools.container import (
    rerun_failed,
    run_container_and_exec,
    run_test_environment,
    sandbox_exec_diff,
    sandbox_initialize_tool,
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
    transform_file,
    write_file_sandbox,
)
from .tools.journal import (
    sandbox_journal_path,
    sandbox_list_runs,
    sandbox_read_journal,
    sandbox_trace,
    sandbox_trace_dir,
)
from .tools.package import (
    package_install,
)
from .tools.vcs import (
    checkpoint,
    checkpoint_list,
    checkpoint_restore,
    clone_repo,
    issue_view,
    publish,
)
from .tools.verify import (
    lint_in_container,
    search_in_container,
    type_check_in_container,
    verify_in_container,
)

logger: logging.Logger = logging.getLogger(__name__)

mcp = FastMCP("code-sandbox-mcp")


sandbox_exec = mcp.tool()(sandbox_exec)
sandbox_exec_background = mcp.tool()(sandbox_exec_background)
sandbox_exec_check = mcp.tool()(sandbox_exec_check)

issue_view = mcp.tool()(issue_view)
publish = mcp.tool()(publish)
checkpoint = mcp.tool()(checkpoint)
checkpoint_list = mcp.tool()(checkpoint_list)
checkpoint_restore = mcp.tool()(checkpoint_restore)
clone_repo = mcp.tool()(clone_repo)



# Container lifecycle tool registrations
# sandbox_initialize is exposed via its async wrapper (Issue #298): slow setup
# phases emit progress notifications so the request never times out and leaks a
# container.  The synchronous sandbox_initialize remains importable for reuse.
sandbox_initialize_tool = mcp.tool(name="sandbox_initialize")(sandbox_initialize_tool)
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


# Package install tool registration
package_install = mcp.tool()(package_install)

# Verify tool registrations
transform_file = mcp.tool()(transform_file)
search_in_container = mcp.tool()(search_in_container)
lint_in_container = mcp.tool()(lint_in_container)
type_check_in_container = mcp.tool()(type_check_in_container)
verify_in_container = mcp.tool()(verify_in_container)

# Journal / trace tool registrations
sandbox_read_journal = mcp.tool()(sandbox_read_journal)
sandbox_trace = mcp.tool()(sandbox_trace)
sandbox_list_runs = mcp.tool()(sandbox_list_runs)
sandbox_journal_path = mcp.tool()(sandbox_journal_path)
sandbox_trace_dir = mcp.tool()(sandbox_trace_dir)

# Approval tool registrations
sandbox_approval_status = mcp.tool()(sandbox_approval_status)
sandbox_approve = mcp.tool()(sandbox_approve)
sandbox_reject = mcp.tool()(sandbox_reject)


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
    parser.add_argument(
        "--mem-ratio",
        type=float,
        default=0.25,
        help="Fraction of host memory for default mem_limit (default: 0.25)",
    )
    parser.add_argument(
        "--cpu-ratio",
        type=float,
        default=0.25,
        help="Fraction of host CPU for default cpu quota (default: 0.25)",
    )
    parser.add_argument(
        "--prewarm-interval-seconds",
        type=int,
        default=3600,
        help=(
            "Pull the default sandbox image at startup and re-check every N "
            "seconds so the first sandbox_initialize is warm (Issue #303). "
            "Set to 0 to disable prewarming (default: 3600)."
        ),
    )
    return parser


def _start_github_app_token_refresh(interval_seconds: int = 120) -> None:
    """Mint a GitHub App token now and refresh it periodically in a daemon thread.

    No-op when no GitHub App is configured (the existing ``GITHUB_TOKEN`` is
    preserved), so existing deployments are unaffected (issue #203, PR-A).
    """
    try:
        provider = setup_github_app_token()
    except Exception:  # noqa: BLE001 - never block startup on token setup
        logger.exception("GitHub App token setup failed; continuing without it")
        return
    if provider is None:
        return

    def _refresh_loop() -> None:
        while True:
            time.sleep(interval_seconds)
            try:
                token = provider.get_token()
                if token:
                    os.environ["GITHUB_TOKEN"] = token
            except Exception:  # noqa: BLE001 - keep the daemon alive
                logger.exception("GitHub App token refresh failed")

    thread = threading.Thread(
        target=_refresh_loop, name="github-app-token-refresh", daemon=True
    )
    thread.start()
    logger.info("started GitHub App token refresh thread (every %ss)", interval_seconds)


def _start_image_prewarm(interval_seconds: int = 3600) -> None:
    """Pull the default sandbox image now and re-check it periodically.

    Removes the cold-start cliff where the first ``sandbox_initialize`` trips
    the client request timeout while the initial ``docker pull`` runs longer
    than the timeout (Issue #303).  Runs in a daemon thread so startup is
    never blocked; the initial pull happens on the first iteration and each
    subsequent cycle is a cheap local presence check (a no-op once the
    digest-pinned image is cached).  A non-positive *interval_seconds*
    disables prewarming entirely.
    """
    if interval_seconds <= 0:
        return

    from code_sandbox_mcp.tools.container import prewarm_default_image

    def _prewarm_loop() -> None:
        while True:
            prewarm_default_image()
            time.sleep(interval_seconds)

    thread = threading.Thread(
        target=_prewarm_loop, name="image-prewarm", daemon=True
    )
    thread.start()
    logger.info("started default image prewarm thread (every %ss)", interval_seconds)


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

    from dataclasses import replace

    from code_sandbox_mcp.security import _DEFAULT_CPU_PERIOD, DEFAULT_SECURITY_PROFILE
    from code_sandbox_mcp.tools import container as _ct_mod
    if args.default_image:
        validate_image_ref(args.default_image)
        _ct_mod._DEFAULT_IMAGE = args.default_image
    if args.shiori_repos_path:
        _ct_mod._SHIORI_REPOS_PATH = args.shiori_repos_path

    # Compute host-adjusted default limits (Issue #201)
    mem_limit_str, cpu_count = compute_default_limits(
        mem_ratio=args.mem_ratio,
        cpu_ratio=args.cpu_ratio,
    )
    adjusted_profile = replace(
        DEFAULT_SECURITY_PROFILE,
        mem_limit=mem_limit_str,
        memswap_limit=mem_limit_str,
        cpu_quota=int(cpu_count * _DEFAULT_CPU_PERIOD),
    )
    set_default_profile(adjusted_profile)

    # Configure notifications if webhook is set
    if args.webhook_url or args.failure_threshold != 5 or args.long_run_seconds != 300:
        from code_sandbox_mcp.notify import configure

        configure(
            webhook_url=args.webhook_url,
            failure_threshold=args.failure_threshold,
            long_run_seconds=args.long_run_seconds,
        )

    # Start dashboard if requested
    dashboard_started = False
    if args.dashboard_port > 0:
        from code_sandbox_mcp.dashboard import start_dashboard

        msg = start_dashboard(port=args.dashboard_port)
        dashboard_started = True
        logger.info(msg)

    # Self-manage a GitHub App installation token so that VCS auth keeps
    # working when running as a long-lived streamable-http daemon outside
    # mcp-launcher's GITHUB_TOKEN injection (issue #203, PR-A). When no
    # GitHub App is configured this is a no-op and the existing GITHUB_TOKEN
    # (launcher-injected or manual) is left untouched -> zero impact on
    # existing deployments.
    _start_github_app_token_refresh()

    # Keep the default sandbox image warm so the first sandbox_initialize does
    # not time out on a cold-start docker pull (Issue #303).
    _start_image_prewarm(args.prewarm_interval_seconds)

    try:
        transport = args.transport
        if transport == "stdio":
            mcp.run(transport=transport)
        else:
            mcp.run(transport=transport, host=args.host, port=args.port)
    finally:
        # Stop the observability dashboard on shutdown so the background
        # HTTP server thread does not outlive the process (issue #345).
        if dashboard_started:
            from code_sandbox_mcp.dashboard import stop_dashboard

            logger.info(stop_dashboard())


if __name__ == "__main__":
    main()
