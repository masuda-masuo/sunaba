"""MCP server for Docker sandbox execution with pass-through-env support.

Inspired by Automata-Labs-team/code-sandbox-mcp.
"""
from __future__ import annotations

import argparse
import inspect
import io
import logging
import os
import shlex
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path

import docker
from docker.errors import APIError, NotFound
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Monkey-patch: prevent server crash when client times out
# ---------------------------------------------------------------------------

import mcp.shared.session as _mcp_session  # noqa: E402

_original_respond = _mcp_session.RequestResponder.respond


async def _safe_respond(self, response) -> None:
    if getattr(self, "_completed", False):
        return
    try:
        await _original_respond(self, response)
    except AssertionError:
        pass


_mcp_session.RequestResponder.respond = _safe_respond  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("code-sandbox-mcp")


# ---------------------------------------------------------------------------
# Pass-through env keys (populated in main() before mcp.run())
# ---------------------------------------------------------------------------

_PASS_THROUGH_KEYS: list[str] = []
_EXEC_TIMEOUT: int = 300  # Default 5 minutes
_TERMINAL: str | None = None       # Full path to terminal executable
_TERMINAL_ARGS: str | None = None  # Argument template; {container_id} is substituted

# Path inside the container where background job output is streamed
_CONTAINER_LOG_PATH = "/tmp/mcp.log"


def _container_env() -> dict[str, str]:
    """Return env vars that should be injected into every new container."""
    return {
        key: os.environ[key]
        for key in _PASS_THROUGH_KEYS
        if key in os.environ
    }


# ---------------------------------------------------------------------------
# Docker client helper
# ---------------------------------------------------------------------------


def _docker() -> docker.DockerClient:
    return docker.from_env()


# ---------------------------------------------------------------------------
# Cross-version exec_run helper
# ---------------------------------------------------------------------------

_EXEC_RUN_SUPPORTS_TIMEOUT: bool | None = None


def _exec_run(container, cmd: list[str], **kwargs):
    """Call exec_run with timeout if the SDK supports it, else without."""
    global _EXEC_RUN_SUPPORTS_TIMEOUT
    if _EXEC_RUN_SUPPORTS_TIMEOUT is None:
        try:
            sig = inspect.signature(container.exec_run)
            _EXEC_RUN_SUPPORTS_TIMEOUT = "timeout" in sig.parameters
        except (ValueError, TypeError):
            _EXEC_RUN_SUPPORTS_TIMEOUT = False

    timeout = kwargs.pop("timeout", None)
    if timeout is not None and not _EXEC_RUN_SUPPORTS_TIMEOUT:
        result: list[tuple[int, bytes] | Exception] = []

        def _run():
            try:
                ec, out = container.exec_run(cmd, **kwargs)
                result.append((ec, out))
            except Exception as e:
                result.append(e)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"Command timed out after {timeout} seconds")
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    if timeout is not None and _EXEC_RUN_SUPPORTS_TIMEOUT:
        kwargs["timeout"] = timeout
    return container.exec_run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# Terminal auto-open helper
# ---------------------------------------------------------------------------

def _open_terminal_with_logs(container_id: str) -> None:
    """Open a terminal window tailing /tmp/mcp.log inside the container.

    --terminal : full path to the terminal executable
    --terminal-args : optional extra args inserted before the tail command.
                      {container_id} is substituted at runtime.
                      When omitted, sensible defaults are used per platform.

    Windows default (PowerShell):
        Opens a single PowerShell window with -NoExit that runs
        ``docker exec -it <id> tail -f /tmp/mcp.log`` directly.
        No Start-Process to avoid double-window.

    macOS default:
        osascript -e 'tell application "Terminal" ...'

    Linux default:
        <terminal> -e "docker exec -it <id> tail -f /tmp/mcp.log"
    """
    if _TERMINAL is None:
        return

    tail_cmd = f"docker exec -it {container_id} tail -f {_CONTAINER_LOG_PATH}"

    try:
        if sys.platform == "win32":
            if _TERMINAL_ARGS:
                # User supplied custom args: substitute and split.
                extra = _TERMINAL_ARGS.format(container_id=container_id).split()
                cmd = [_TERMINAL] + extra
                subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                # Open ONE PowerShell window directly with the tail command.
                # -NoExit keeps the window open after tail -f ends.
                subprocess.Popen(
                    [_TERMINAL, "-NoExit", "-Command", tail_cmd],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        elif _TERMINAL.endswith("osascript"):
            script = (
                'tell application "Terminal"\n'
                '  activate\n'
                f'  do script "{tail_cmd}"\n'
                'end tell'
            )
            subprocess.Popen(
                [_TERMINAL, "-e", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            # Generic Linux terminal emulator
            if _TERMINAL_ARGS:
                extra = shlex.split(_TERMINAL_ARGS.format(container_id=container_id))
                cmd = [_TERMINAL] + extra
            else:
                cmd = [_TERMINAL, "-e", tail_cmd]
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        logger.warning("Failed to open terminal (%s): %s", _TERMINAL, e)


# ---------------------------------------------------------------------------
# Background job tracking
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _run_commands_in_background(
    job_id: str,
    container_id: str,
    commands: list[str],
    timeout: int,
):
    """Run commands in a background thread, updating job status.

    Each command's output is tee'd to _CONTAINER_LOG_PATH inside the
    container so a terminal running 'tail -f' can display it in real time.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        with _jobs_lock:
            _jobs[job_id] = {
                "status": "error",
                "error": f"container {container_id[:12]} not found",
                "finished_at": time.time(),
            }
        return
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id] = {
                "status": "error",
                "error": f"failed to get container: {e}",
                "finished_at": time.time(),
            }
        return

    # Initialise the log file inside the container
    _exec_run(
        container,
        ["sh", "-c", f"truncate -s 0 {_CONTAINER_LOG_PATH}"],
        stdout=False,
        stderr=False,
    )

    output_parts: list[str] = []
    started_at = time.time()

    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "container_id": container_id,
            "commands": commands,
            "started_at": started_at,
            "output": "",
        }

    for cmd in commands:
        header = f"$ {cmd}"
        output_parts.append(header)

        # Echo the command itself into the log so the human sees what's running
        _exec_run(
            container,
            ["sh", "-c", f"echo {header!r} >> {_CONTAINER_LOG_PATH}"],
            stdout=False,
            stderr=False,
        )

        try:
            # Run the command, tee-ing stdout+stderr into the log file
            tee_cmd = f"({cmd}) 2>&1 | tee -a {_CONTAINER_LOG_PATH}"
            exit_code, output = _exec_run(
                container,
                ["sh", "-c", tee_cmd],
                stdout=True,
                stderr=True,
                demux=False,
                timeout=timeout,
            )
            decoded = output.decode("utf-8", errors="replace") if output else ""
            if decoded:
                output_parts.append(decoded.rstrip("\n"))
            if exit_code != 0:
                msg = f"Command exited with code {exit_code}"
                output_parts.append(msg)
                _exec_run(
                    container,
                    ["sh", "-c", f"echo {msg!r} >> {_CONTAINER_LOG_PATH}"],
                    stdout=False,
                    stderr=False,
                )
                with _jobs_lock:
                    _jobs[job_id]["output"] = "\n".join(output_parts)
                break
        except TimeoutError as e:
            output_parts.append(f"Error: {e}")
            break
        except Exception as e:
            output_parts.append(f"Error executing command: {e}")
            break

        # Update partial output for polling
        with _jobs_lock:
            _jobs[job_id]["output"] = "\n".join(output_parts)

    finished_at = time.time()
    with _jobs_lock:
        _jobs[job_id].update(
            status="done",
            output="\n".join(output_parts),
            finished_at=finished_at,
            elapsed=finished_at - started_at,
        )


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("code-sandbox-mcp")


@mcp.tool()
def sandbox_initialize(image: str = "python:3.12-slim-bookworm") -> str:
    """Initialize a new compute environment for code execution.

    Creates a Docker container based on the specified image.
    Returns a container_id that must be passed to other sandbox tools.

    Args:
        image: Docker image to use (default: python:3.12-slim-bookworm)
    """
    client = _docker()
    env = _container_env()
    container = client.containers.run(
        image,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
    )
    logger.info("Container %s started (image=%s)", container.id[:12], image)
    return container.id


@mcp.tool()
def sandbox_exec(container_id: str, commands: list[str]) -> str:
    """Execute commands sequentially inside a running container.

    Runs each command via 'sh -c'. Stops on first non-zero exit code.
    Returns combined stdout/stderr output with exit codes.

    NOTE: For commands that may exceed the MCP client timeout (60 s),
    use ``sandbox_exec_background`` + ``sandbox_exec_check`` instead.

    Args:
        container_id: ID returned by sandbox_initialize
        commands: List of shell commands to run in order
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    output_parts: list[str] = []
    for cmd in commands:
        output_parts.append(f"$ {cmd}")
        try:
            exit_code, output = _exec_run(
                container,
                ["sh", "-c", cmd],
                stdout=True,
                stderr=True,
                demux=False,
                timeout=_EXEC_TIMEOUT,
            )
            decoded = output.decode("utf-8", errors="replace") if output else ""
            if decoded:
                output_parts.append(decoded.rstrip("\n"))
            if exit_code != 0:
                output_parts.append(f"Command exited with code {exit_code}")
                break
        except TimeoutError as e:
            output_parts.append(f"Error: {e}")
            break
        except Exception as e:
            output_parts.append(f"Error executing command: {e}")
            break

    return "\n".join(output_parts)


@mcp.tool()
def sandbox_exec_background(container_id: str, commands: list[str]) -> str:
    """Start commands in the background and return immediately.

    Use ``sandbox_exec_check`` to poll for completion and retrieve output.
    This is the recommended way to run commands that take > 60 seconds.

    If the server was started with ``--terminal``, a terminal window is
    automatically opened showing live output via
    ``docker exec -it <container> tail -f /tmp/mcp.log``.

    Args:
        container_id: ID returned by sandbox_initialize
        commands: List of shell commands to run in order

    Returns:
        job_id: Unique identifier to pass to sandbox_exec_check
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    job_id = str(uuid.uuid4())[:8]

    t = threading.Thread(
        target=_run_commands_in_background,
        args=(job_id, container_id, commands, _EXEC_TIMEOUT),
        daemon=True,
    )
    t.start()

    # Open a terminal window for real-time log tailing if configured
    _open_terminal_with_logs(container_id)

    terminal_note = (
        f"\nA terminal window has been opened (tail -f {_CONTAINER_LOG_PATH})."
        if _TERMINAL
        else ""
    )

    return (
        f"Job started: {job_id}\n"
        f"Check status with: sandbox_exec_check(container_id=\"{container_id[:12]}\", job_id=\"{job_id}\"){terminal_note}"
    )


@mcp.tool()
def sandbox_exec_check(container_id: str, job_id: str) -> str:
    """Check the status of a background job started by sandbox_exec_background.

    Args:
        container_id: Same container_id used in sandbox_exec_background
        job_id: Job identifier returned by sandbox_exec_background

    Returns:
        Job status ("running" or "done") with output when complete.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        return f"Error: job {job_id} not found (may have been cleaned up or never existed)"

    status = job["status"]

    if status == "running":
        elapsed = time.time() - job["started_at"]
        partial = job.get("output", "")
        return (
            f"Status: running (elapsed: {elapsed:.0f}s)\n"
            f"--- partial output ---\n"
            f"{partial}"
        )

    if status == "error":
        return f"Status: error\nError: {job['error']}"

    # done
    return f"Status: done (elapsed: {job.get('elapsed', 0):.0f}s)\n{job['output']}"


@mcp.tool()
def sandbox_stop(container_id: str) -> str:
    """Stop and remove a running container sandbox.

    Args:
        container_id: ID returned by sandbox_initialize
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=10)
        container.remove(v=True)
        logger.info("Container %s stopped", container_id[:12])
        return f"Container {container_id[:12]} stopped and removed"
    except NotFound:
        return f"Container {container_id[:12]} not found (already removed?)"
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: unexpected error while stopping container: {e}"


@mcp.tool()
def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/root",
) -> str:
    """Write a file into the container filesystem.

    Args:
        container_id: ID returned by sandbox_initialize
        file_name: Name of the file to create
        file_contents: Text content to write
        dest_dir: Directory inside the container (default: /root)
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    encoded = file_contents.encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=file_name)
        info.size = len(encoded)
        tar.addfile(info, io.BytesIO(encoded))
    buf.seek(0)

    try:
        container.put_archive(dest_dir, buf)
        return f"Written {file_name} to {dest_dir} in container {container_id[:12]}"
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: unexpected error while writing file: {e}"


@mcp.tool()
def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/root",
) -> str:
    """Copy a local directory into the container filesystem.

    Args:
        container_id: ID returned by sandbox_initialize
        local_src_dir: Absolute path to a directory on the host
        dest_dir: Destination directory inside the container (default: /root)
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    src = Path(local_src_dir)
    if not src.is_dir():
        return f"Error: {local_src_dir} is not a directory"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=src.name)
    buf.seek(0)

    try:
        container.put_archive(dest_dir, buf)
        return f"Copied {local_src_dir} to {dest_dir}/{src.name} in container {container_id[:12]}"
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: unexpected error while copying project: {e}"


@mcp.tool()
def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = "/root",
) -> str:
    """Copy a single local file into the container filesystem.

    Args:
        container_id: ID returned by sandbox_initialize
        local_src_file: Absolute path to a file on the host
        dest_path: Destination directory inside the container (default: /root)
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    src = Path(local_src_file)
    if not src.is_file():
        return f"Error: {local_src_file} is not a file"

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=src.name)
    buf.seek(0)

    try:
        container.put_archive(dest_path, buf)
        return f"Copied {src.name} to {dest_path} in container {container_id[:12]}"
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: unexpected error while copying file: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="code-sandbox-mcp: Docker sandbox MCP server",
        add_help=True,
    )
    parser.add_argument(
        "--pass-through-env",
        metavar="VAR1,VAR2,...",
        default="",
        help="Comma-separated list of environment variable names to pass into containers",
    )
    parser.add_argument(
        "--exec-timeout",
        type=int,
        default=300,
        help="Timeout for command execution in seconds (default: 300)",
    )
    parser.add_argument(
        "--terminal",
        metavar="TERMINAL",
        default=None,
        help=(
            "Full path to a terminal executable. When set, a new terminal window "
            "is opened automatically on sandbox_exec_background, tailing "
            "/tmp/mcp.log inside the container. "
            "Windows example: C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe  "
            "macOS example: /usr/bin/osascript"
        ),
    )
    parser.add_argument(
        "--terminal-args",
        metavar="ARGS",
        default=None,
        help=(
            "Optional extra arguments passed to --terminal before the tail command. "
            "{container_id} is substituted at runtime. "
            "When omitted, sensible defaults are used per platform. "
            "Windows/PowerShell default: -NoExit -Command <tail_cmd>"
        ),
    )
    args, remaining = parser.parse_known_args()

    global _PASS_THROUGH_KEYS, _EXEC_TIMEOUT, _TERMINAL, _TERMINAL_ARGS
    _PASS_THROUGH_KEYS = [k.strip() for k in args.pass_through_env.split(",") if k.strip()]
    _EXEC_TIMEOUT = args.exec_timeout
    _TERMINAL = args.terminal
    _TERMINAL_ARGS = args.terminal_args

    sys.argv = [sys.argv[0]] + remaining

    mcp.run()


if __name__ == "__main__":
    main()