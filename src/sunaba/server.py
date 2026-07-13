"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import time

from fastmcp import FastMCP

from sunaba.github_auth import set_global_provider, setup_github_app_token
from sunaba.security import (
    compute_default_limits,
    set_default_profile,
    validate_image_ref,
)

from .tools.container import (
    run_container_and_exec,
    sandbox_attach,
    sandbox_initialize_tool,
    sandbox_list_containers,
    sandbox_stop,
)
from .tools.diff import (
    diff_in_container,
)
from .tools.exec import (
    sandbox_exec,
    sandbox_exec_background,
    sandbox_exec_check,
)
from .tools.file import (
    copy_file,
    copy_project,
    edit_symbol,
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
    sandbox_issue_write,
    sandbox_pr_review_write,
)
from .tools.verify import (
    lint_in_container,
    search_in_container,
    type_check_in_container,
    verify_in_container,
)

logger: logging.Logger = logging.getLogger(__name__)

# Server-level workflow map (Issue #550).  Docstrings stay focused on each
# tool's interface contract (arguments, side effects, failure modes); the
# cross-tool workflow lives here, sent once per session via the MCP
# ``instructions`` field.  Keep this under 2048 bytes UTF-8 (Claude Code
# truncates tool descriptions at 2 KB; instructions get one screenful too).
SERVER_INSTRUCTIONS = """\
sunaba: Docker-sandboxed dev workflow. All tools take the container_id returned by sandbox_initialize. Typical flow:
1. INIT: sandbox_initialize(clone_repo="owner/repo") clones + installs deps in one call; pr=N checks out a PR branch instead. run_container_and_exec wraps init/exec/stop for one-shot runs; sandbox_attach reconnects to a running container; sandbox_stop cleans up.
2. EXPLORE: search_in_container (grep), read_file_range (cat/head), list_files (ls/find).
3. EDIT: write_file_sandbox (full write or exact string replace), edit_symbol (replace/delete a Python def/class/method by name), transform_file (Python transform for complex edits). checkpoint() = local commit savepoint, no push, use freely; checkpoint_restore rolls back; checkpoint_list lists them.
4. VERIFY: verify_in_container is the pre-publish gate (tests + lint + type in one call; test_filter runs a fast subset first, then the full suite automatically). lint_in_container / type_check_in_container run individual single-file checks. diff_in_container reviews pending changes before pushing.
5. PUBLISH: publish(create_pr=True) stages, commits, pushes and opens the PR in one call. It does NOT verify -- run verify_in_container first. checkpoint is local-only; publish is the only network exit. Credentials are resolved host-side; never handle tokens in the container.
Issue/PR ops: issue_view (read), sandbox_issue_write (create/comment), sandbox_pr_review_write (formal reviews).
Prefer dedicated tools over raw sandbox_exec: grep->search_in_container, cat->read_file_range, sed->write_file_sandbox/transform_file, pip->package_install, pytest/ruff/pyright->verify/lint/type_check_in_container, git push/gh pr->publish.
"""

mcp = FastMCP("sunaba", instructions=SERVER_INSTRUCTIONS)


sandbox_exec = mcp.tool()(sandbox_exec)
sandbox_exec_background = mcp.tool()(sandbox_exec_background)
sandbox_exec_check = mcp.tool()(sandbox_exec_check)

issue_view = mcp.tool()(issue_view)
publish = mcp.tool()(publish)
sandbox_issue_write = mcp.tool()(sandbox_issue_write)
sandbox_pr_review_write = mcp.tool()(sandbox_pr_review_write)
checkpoint = mcp.tool()(checkpoint)
checkpoint_list = mcp.tool()(checkpoint_list)
checkpoint_restore = mcp.tool()(checkpoint_restore)
clone_repo = mcp.tool()(clone_repo)

# Container naming / discovery tools (Issue #478)
sandbox_list_containers = mcp.tool()(sandbox_list_containers)
sandbox_attach = mcp.tool()(sandbox_attach)



# Container lifecycle tool registrations
# sandbox_initialize is exposed via its async wrapper (Issue #298): slow setup
# phases emit progress notifications so the request never times out and leaks a
# container.  The synchronous sandbox_initialize remains importable for reuse.
sandbox_initialize_tool = mcp.tool(name="sandbox_initialize")(sandbox_initialize_tool)
sandbox_stop = mcp.tool()(sandbox_stop)
run_container_and_exec = mcp.tool()(run_container_and_exec)

# File tool registrations
write_file_sandbox = mcp.tool()(write_file_sandbox)
edit_symbol = mcp.tool()(edit_symbol)
copy_project = mcp.tool()(copy_project)
copy_file = mcp.tool()(copy_file)
read_file_range = mcp.tool()(read_file_range)
list_files = mcp.tool()(list_files)


# Package install tool registration
package_install = mcp.tool()(package_install)

# Diff tool registration
diff_in_container = mcp.tool()(diff_in_container)

# Verify tool registrations
transform_file = mcp.tool()(transform_file)
search_in_container = mcp.tool()(search_in_container)
lint_in_container = mcp.tool()(lint_in_container)
type_check_in_container = mcp.tool()(type_check_in_container)
verify_in_container = mcp.tool()(verify_in_container)

# Journal / trace read tools are opt-in (#460): telemetry *writes* are
# unconditional infrastructure, but the LLM-facing read surface stays off
# the default tool list.  Aggregation workflows read the journal file
# directly on the host instead.
OBSERVABILITY_TOOLS_ENV = "SUNABA_OBSERVABILITY_TOOLS"


def observability_tools_enabled() -> bool:
    """True when the journal/trace read tools should be registered."""
    val = os.environ.get(OBSERVABILITY_TOOLS_ENV)
    return val not in (None, "", "0")


if observability_tools_enabled():
    sandbox_read_journal = mcp.tool()(sandbox_read_journal)
    sandbox_trace = mcp.tool()(sandbox_trace)
    sandbox_list_runs = mcp.tool()(sandbox_list_runs)
    sandbox_journal_path = mcp.tool()(sandbox_journal_path)
    sandbox_trace_dir = mcp.tool()(sandbox_trace_dir)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """Argparse type: require an integer >= 1."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}")
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {ivalue}")
    return ivalue


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
        default=8750,
        help="Port for HTTP transport (default: 8750)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8751,
        help=(
            "Start the observability web dashboard on localhost "
            "(default: 8751).  Pass --dashboard-port 0 to disable."
        ),
    )
    parser.add_argument(
        "--dashboard-host",
        type=str,
        default="127.0.0.1",
        help=(
            "Host address for the observability dashboard "
            "(default: 127.0.0.1).  Use 0.0.0.0 to allow WSL host access."
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
            "Pull the default and language-variant sandbox images at startup "
            "and re-check every N seconds so the first sandbox_initialize is "
            "warm regardless of which image language detection picks "
            "(Issue #303). Set to 0 to disable prewarming (default: 3600)."
        ),
    )
    parser.add_argument(
        "--prewarm-timeout-seconds",
        type=_positive_int,
        default=300,
        help=(
            "Maximum seconds to wait for the first prewarm cycle to complete "
            "before starting the server anyway.  If the timeout expires, a "
            "WARNING is logged and the server starts without a warm image, "
            "meaning the first sandbox_initialize may be slower as it "
            "performs the docker pull itself (default: 300)."
        ),
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level (default: INFO).",
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
    set_global_provider(provider)

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


def _start_image_prewarm(
    interval_seconds: int = 3600,
    startup_event: threading.Event | None = None,
) -> None:
    """Pull the default and variant sandbox images now and re-check periodically.

    Removes the cold-start cliff where the first ``sandbox_initialize`` trips
    the client request timeout while the initial ``docker pull`` runs longer
    than the timeout (Issue #303).  Also covers the ``python``/``go`` variant
    images that detection-based image selection can pick instead of the
    neutral default.  Runs in a daemon thread so startup is
    never blocked; the initial pull happens on the first iteration and each
    subsequent cycle is a cheap local presence check (a no-op once the
    digest-pinned images are cached).  A non-positive *interval_seconds*
    disables prewarming entirely.

    When *startup_event* is provided, the first prewarm cycle signals it so
    the caller can wait for the initial pull before accepting requests,
    preventing a race between server startup and the first
    ``sandbox_initialize`` (Issue #371).
    """
    if interval_seconds <= 0:
        if startup_event:
            startup_event.set()
        return

    from sunaba.tools.container import prewarm_default_image

    def _prewarm_loop() -> None:
        first = True
        while True:
            try:
                prewarm_default_image()
            finally:
                if first and startup_event:
                    startup_event.set()
                    first = False
            time.sleep(interval_seconds)

    thread = threading.Thread(
        target=_prewarm_loop, name="image-prewarm", daemon=True
    )
    thread.start()
    logger.info("started image prewarm thread (every %ss)", interval_seconds)


def main() -> None:
    """Parse CLI arguments and run the MCP server.

    ``--default-image`` for overriding the default Docker image,
    ``--transport`` to select the MCP transport protocol,
    ``--dashboard-port`` for the observability dashboard (default: 8751),
    ``--dashboard-host`` to set the bind address (default: 127.0.0.1),
    ``--webhook-url`` for push notifications,
    and ``--log-level`` to control logging verbosity (default: INFO).

    HTTP-based transports (``sse``, ``http``, ``streamable-http``)
    are not subject to the ~60-second client timeout that affects
    ``stdio``, making them suitable for long-running Docker
    operations such as ``docker pull`` or ``copy_project`` on
    large directories.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    from dataclasses import replace

    from sunaba.security import _DEFAULT_CPU_PERIOD, DEFAULT_SECURITY_PROFILE
    from sunaba.tools import container as _ct_mod
    if args.default_image:
        validate_image_ref(args.default_image)
        _ct_mod._DEFAULT_IMAGE = args.default_image
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
        from sunaba.notify import configure

        configure(
            webhook_url=args.webhook_url,
            failure_threshold=args.failure_threshold,
            long_run_seconds=args.long_run_seconds,
        )

    # Start dashboard if requested
    dashboard_started = False
    if args.dashboard_port > 0:
        from sunaba.dashboard import start_dashboard

        msg = start_dashboard(host=args.dashboard_host, port=args.dashboard_port)
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
    # Wait for the first prewarm cycle to complete before accepting requests
    # so that the initial docker pull never races with sandbox_initialize
    # (Issue #371).
    prewarm_ready = threading.Event()
    _start_image_prewarm(args.prewarm_interval_seconds, prewarm_ready)
    if not prewarm_ready.wait(timeout=args.prewarm_timeout_seconds):
        logger.warning(
            "prewarm did not complete within %d seconds \u2014 starting server "
            "without a warm image.  The first sandbox_initialize may be "
            "slower as it performs docker pull itself.",
            args.prewarm_timeout_seconds,
        )

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
            from sunaba.dashboard import stop_dashboard

            logger.info(stop_dashboard())


if __name__ == "__main__":
    main()
