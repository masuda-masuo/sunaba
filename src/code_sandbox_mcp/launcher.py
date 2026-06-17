"""Launcher process for code-sandbox-mcp.

Spawning server.py as a child process and proxying stdio between
Claude Desktop and the server.  If the server exits with
:data:`~code_sandbox_mcp.RESTART_EXIT_CODE` (42), the launcher
restarts it (signaling a successful in-place update).

Architecture
------------
* **stdio mode** (default):
  * **stdin proxy** – a single long-lived thread reads from
    ``sys.stdin.buffer`` and writes to the *current* server's stdin via
    a ``threading.Event``-guarded reference.  On restart the reference is
    atomically swapped so the single reader thread always feeds the live
    process without duplication or races.
  * **stdout/stderr proxies** – one thread per stream per server process,
    started fresh on each restart.  These are daemon threads so they are
    reaped automatically when the launcher exits.
* **sse/http mode** (``--transport sse`` or ``--transport http``):
  The server listens on a TCP port (default 127.0.0.1:8765) instead of
  using stdio.  The launcher only manages the server process lifecycle
  and does not proxy stdio.  This avoids the ~60-second client timeout
  that affects stdio transport.
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


def _stdin_proxy(get_dst) -> None:
    """Read sys.stdin.buffer forever, writing each line to get_dst().

    *get_dst* is a callable that returns the current destination stream.
    Returning ``None`` causes the proxy to silently drop the line, which
    happens during the brief gap between server processes on restart.
    """
    src = sys.stdin.buffer
    try:
        while True:
            data = src.readline()
            if not data:
                break
            dst = get_dst()
            if dst is None:
                continue
            try:
                dst.write(data)
                dst.flush()
            except (BrokenPipeError, OSError, ValueError):
                # Server stdin closed mid-restart; next line will get
                # the new dst from get_dst().
                pass
    except (BrokenPipeError, OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _detect_transport(argv: list[str]) -> str:
    """Detect the transport mode from CLI arguments.

    Looks for ``--transport`` in *argv* and returns its value,
    defaulting to ``"stdio"``.
    """
    for i, arg in enumerate(argv):
        if arg == "--transport" and i + 1 < len(argv):
            return argv[i + 1]
    return "stdio"


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

    transport = _detect_transport(server_args)
    is_stdio = transport == "stdio"

    # Shared mutable reference to the current server's stdin.
    # Only used in stdio mode.  A lock protects swaps during restart.
    _lock = threading.Lock()
    _current_stdin: list[IO[bytes] | None] = [None]

    def _get_current_stdin() -> IO[bytes] | None:
        with _lock:
            return _current_stdin[0]

    # stdin proxy thread (stdio mode only) starts on first server
    # iteration after the server process is ready to avoid dropping
    # the initialize request.
    stdin_thread: threading.Thread | None = None
    stdin_thread_started = False

    while True:
        if is_stdio:
            proc = subprocess.Popen(
                server_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # Point the stdin proxy at the new process.
            with _lock:
                _current_stdin[0] = proc.stdin

            # Start the single stdin proxy thread on the first iteration
            # after the server process is already running and its stdin
            # reference has been set.
            if not stdin_thread_started:
                stdin_thread_started = True
                stdin_thread = threading.Thread(
                    target=_stdin_proxy,
                    args=(_get_current_stdin,),
                    daemon=True,
                )
                stdin_thread.start()

            # Per-process stdout/stderr proxy threads.
            out_thread = threading.Thread(
                target=_pipe_stream,
                args=(proc.stdout, sys.stdout.buffer),
                daemon=True,
            )
            err_thread = threading.Thread(
                target=_pipe_stream,
                args=(proc.stderr, sys.stderr.buffer),
                daemon=True,
            )
            out_thread.start()
            err_thread.start()
        else:
            # SSE/HTTP mode: server binds to a TCP port, no stdio proxy.
            proc = subprocess.Popen(server_args)

        returncode = proc.wait()

        if is_stdio:
            # Detach stdin proxy from the dead process before restart.
            with _lock:
                _current_stdin[0] = None

            out_thread.join(timeout=1)
            err_thread.join(timeout=1)

        if returncode != RESTART_EXIT_CODE:
            break


if __name__ == "__main__":
    main()
