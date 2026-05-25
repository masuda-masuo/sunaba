"""Launcher process for code-sandbox-mcp.

Spawning server.py as a child process and proxying stdio between
Claude Desktop and the server.  If the server exits with
:data:`~code_sandbox_mcp.RESTART_EXIT_CODE` (42), the launcher
restarts it (signaling a successful in-place update).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
from typing import IO

from code_sandbox_mcp import RESTART_EXIT_CODE


# ---------------------------------------------------------------------------
# Stdio proxy helpers
# ---------------------------------------------------------------------------


def _pipe_stream(src: IO[bytes], dst: IO[bytes]) -> None:
    """Forward lines from *src* to *dst* until EOF.

    MCP uses newline-delimited JSON-RPC, so readline() ensures each
    message is forwarded immediately without waiting to fill a buffer.
    """
    try:
        while True:
            data = src.readline()
            if not data:
                break
            dst.write(data)
            dst.flush()
    except (BrokenPipeError, OSError, ValueError):
        # readline() on a closed stream raises ValueError instead of
        # BrokenPipeError; catch all three to safely ignore harmless
        # stream-closure errors during shutdown.
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "code-sandbox-mcp launcher: 2-process architecture "
            "for in-place updates"
        ),
        add_help=True,
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        default=False,
        help=(
            "Automatically run sandbox_update_start() on startup "
            "(off by default)"
        ),
    )
    args, remaining = parser.parse_known_args()

    server_args = [sys.executable, "-m", "code_sandbox_mcp.server"]
    if args.auto_update:
        server_args.append("--auto-update")
    server_args.extend(remaining)

    while True:
        proc = subprocess.Popen(
            server_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        threads = [
            threading.Thread(
                target=_pipe_stream,
                args=(sys.stdin.buffer, proc.stdin),
                daemon=True,
            ),
            threading.Thread(
                target=_pipe_stream,
                args=(proc.stdout, sys.stdout.buffer),
                daemon=True,
            ),
            threading.Thread(
                target=_pipe_stream,
                args=(proc.stderr, sys.stderr.buffer),
                daemon=True,
            ),
        ]
        for t in threads:
            t.start()

        returncode = proc.wait()

        for t in threads:
            t.join(timeout=1)

        if returncode != RESTART_EXIT_CODE:
            break


if __name__ == "__main__":
    main()
