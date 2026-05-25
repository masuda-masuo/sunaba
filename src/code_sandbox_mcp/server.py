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

from code_sandbox_mcp import RESTART_EXIT_CODE

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
# Pass-through env keys
# ---------------------------------------------------------------------------

_PASS_THROUGH_KEYS: list[str] = []
_EXEC_TIMEOUT: int = 300
_TERMINAL: str | None = None
_TERMINAL_ARGS: str | None = None

_CONTAINER_LOG_PATH = "/tmp/mcp.log"


def _container_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in _PASS_THROUGH_KEYS
        if key in os.environ
    }


def _docker() -> docker.DockerClient:
    return docker.from_env()


# ---------------------------------------------------------------------------
# Cross-version exec_run helper
# ---------------------------------------------------------------------------

_EXEC_RUN_SUPPORTS_TIMEOUT: bool | None = None


class _ContainerExecError(Exception):
    """Raised when a container exec operation fails."""


def _exec_run(container, cmd: list[str], **kwargs):
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

    On Windows ``cmd /c start`` is used to detach the window from the
    MCP server process, so the window survives server shutdown.
    """
    if _TERMINAL is None:
        return

    short_id = container_id[:12]

    ps_script = (
        f"docker exec {container_id} tail -f {_CONTAINER_LOG_PATH} 2>$null; "
        f"Write-Host ''; "
        f"Write-Host '=== Container {short_id} stopped ===' -ForegroundColor Yellow; "
        f"Read-Host 'Press Enter to close this window'"
    )

    unix_script = (
        f"docker exec {container_id} tail -f {_CONTAINER_LOG_PATH} 2>/dev/null; "
        f"echo; echo '=== Container {short_id} stopped ==='; "
        f"echo 'Press Enter to close this window.'; read"
    )

    try:
        if sys.platform == "win32":
            if _TERMINAL_ARGS:
                extra = _TERMINAL_ARGS.format(container_id=container_id).split()
                subprocess.Popen(
                    [_TERMINAL] + extra,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                # Use cmd /c start to detach the window from the MCP server.
                # Without this, killing the MCP server closes the terminal too.
                subprocess.Popen(
                    ["cmd", "/c", "start", _TERMINAL, "-NoExit", "-Command", ps_script],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        elif _TERMINAL.endswith("osascript"):
            script = (
                'tell application "Terminal"\n'
                '  activate\n'
                f'  do script "{unix_script}"\n'
                'end tell'
            )
            subprocess.Popen(
                [_TERMINAL, "-e", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            if _TERMINAL_ARGS:
                extra = shlex.split(_TERMINAL_ARGS.format(container_id=container_id))
                cmd = [_TERMINAL] + extra
            else:
                cmd = [_TERMINAL, "-e", unix_script]
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

    _exec_run(container, ["sh", "-c", f"truncate -s 0 {_CONTAINER_LOG_PATH}"], stdout=False, stderr=False)

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
        _exec_run(container, ["sh", "-c", f"echo {header!r} >> {_CONTAINER_LOG_PATH}"], stdout=False, stderr=False)

        try:
            tee_cmd = f"({cmd}) 2>&1 | tee -a {_CONTAINER_LOG_PATH}"
            exit_code, output = _exec_run(
                container, ["sh", "-c", tee_cmd], stdout=True, stderr=True, demux=False, timeout=timeout,
            )
            decoded = output.decode("utf-8", errors="replace") if output else ""
            if decoded:
                output_parts.append(decoded.rstrip("\n"))
            if exit_code != 0:
                msg = f"Command exited with code {exit_code}"
                output_parts.append(msg)
                _exec_run(container, ["sh", "-c", f"echo {msg!r} >> {_CONTAINER_LOG_PATH}"], stdout=False, stderr=False)
                with _jobs_lock:
                    _jobs[job_id]["output"] = "\n".join(output_parts)
                break
        except TimeoutError as e:
            output_parts.append(f"Error: {e}")
            break
        except Exception as e:
            output_parts.append(f"Error executing command: {e}")
            break

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
# Update state
# ---------------------------------------------------------------------------

#: Pip install specifier for ``sandbox_update_start()``.
#: Defaults to reinstalling from the local working tree.
_UPDATE_SPEC: str = "."

#: When True, ``sandbox_update_start()`` is called automatically on server start.
_UPDATE_AUTO: bool = False


# ---------------------------------------------------------------------------
# Background update helper
# ---------------------------------------------------------------------------


def _run_update_background(job_id: str) -> None:
    """Run pip install --force-reinstall in a background thread.

    On success, sets job status to ``done`` and exits the process with
    :data:`~code_sandbox_mcp.RESTART_EXIT_CODE` (42, restart signal).
    On failure, sets job status to ``error``.
    """
    started_at = time.time()

    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "started_at": started_at,
        }

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", _UPDATE_SPEC],
            capture_output=True,
            text=True,
        )
        finished_at = time.time()

        if result.returncode == 0:
            with _jobs_lock:
                _jobs[job_id].update(
                    status="done",
                    finished_at=finished_at,
                    elapsed=finished_at - started_at,
                    output=result.stdout,
                )
            # Give Claude a brief window to poll before exiting
            time.sleep(2)
            sys.exit(RESTART_EXIT_CODE)
        else:
            error_msg = result.stderr or result.stdout or f"pip exited with code {result.returncode}"
            with _jobs_lock:
                _jobs[job_id].update(
                    status="error",
                    finished_at=finished_at,
                    elapsed=finished_at - started_at,
                    error=error_msg,
                    output=result.stdout + "\n" + result.stderr,
                )
    except Exception as e:
        finished_at = time.time()
        with _jobs_lock:
            _jobs[job_id].update(
                status="error",
                finished_at=finished_at,
                elapsed=finished_at - started_at,
                error=str(e),
            )


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("code-sandbox-mcp")


@mcp.tool()
def sandbox_initialize(image: str = "python:3.12-slim-bookworm") -> str:
    client = _docker()
    env = _container_env()
    container = client.containers.run(image, command="sleep infinity", detach=True, remove=False, environment=env)
    logger.info("Container %s started (image=%s)", container.id[:12], image)
    return container.id


@mcp.tool()
def sandbox_exec(container_id: str, commands: list[str]) -> str:
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
                container, ["sh", "-c", cmd], stdout=True, stderr=True, demux=False, timeout=_EXEC_TIMEOUT,
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
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: failed to get container {container_id[:12]}: {e}"

    job_id = str(uuid.uuid4())[:8]
    threading.Thread(target=_run_commands_in_background, args=(job_id, container_id, commands, _EXEC_TIMEOUT), daemon=True).start()

    _open_terminal_with_logs(container_id)

    terminal_note = f"\nA terminal window has been opened (tail -f {_CONTAINER_LOG_PATH})." if _TERMINAL else ""
    return (
        f"Job started: {job_id}\n"
        f"Check status with: sandbox_exec_check(container_id=\"{container_id[:12]}\", job_id=\"{job_id}\"){terminal_note}"
    )


@mcp.tool()
def sandbox_exec_check(container_id: str, job_id: str) -> str:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return f"Error: job {job_id} not found"
    status = job["status"]
    if status == "running":
        elapsed = time.time() - job["started_at"]
        return f"Status: running (elapsed: {elapsed:.0f}s)\n--- partial output ---\n{job.get('output', '')}"
    if status == "error":
        return f"Status: error\nError: {job['error']}"
    return f"Status: done (elapsed: {job.get('elapsed', 0):.0f}s)\n{job['output']}"


@mcp.tool()
def sandbox_update_start() -> str:
    """Start an in-place update (pip install --force-reinstall) in the background.

    Returns a ``job_id`` immediately.  Use :func:`sandbox_update_check` to
    poll the status.  On success the server process will restart automatically
    (the launcher maintains the stdio connection).

    The update source is controlled by the ``--update-spec`` CLI flag
    (default: ``.`` = current working directory).
    """
    job_id = str(uuid.uuid4())[:8]
    threading.Thread(
        target=_run_update_background,
        args=(job_id,),
        daemon=True,
    ).start()
    return f"Update job started: {job_id}\nPoll with: sandbox_update_check(job_id=\"{job_id}\")"


@mcp.tool()
def sandbox_update_check(job_id: str) -> str:
    """Poll the status of an update job started by :func:`sandbox_update_start`.

    Returns one of:
    - ``Status: running (elapsed: Xs)``
    - ``Status: done (elapsed: Xs)\n<pip output>``
    - ``Status: error\nError: <message>``
    - ``Error: job {job_id} not found``
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return f"Error: job {job_id} not found"
    status = job["status"]
    if status == "running":
        elapsed = time.time() - job["started_at"]
        return f"Status: running (elapsed: {elapsed:.0f}s)"
    if status == "error":
        return f"Status: error\nError: {job['error']}"
    return f"Status: done (elapsed: {job.get('elapsed', 0):.0f}s)\n{job.get('output', '')}"


@mcp.tool()
def sandbox_stop(container_id: str) -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=10)
        container.remove(v=True)
        return f"Container {container_id[:12]} stopped and removed"
    except NotFound:
        return f"Container {container_id[:12]} not found (already removed?)"
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def write_file_sandbox(container_id: str, file_name: str, file_contents: str, dest_dir: str = "/root") -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"
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
        return f"Error: {e}"


@mcp.tool()
def copy_project(container_id: str, local_src_dir: str, dest_dir: str = "/root") -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"
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
        return f"Error: {e}"


@mcp.tool()
def copy_file(container_id: str, local_src_file: str, dest_path: str = "/root") -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"
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
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="code-sandbox-mcp: Docker sandbox MCP server", add_help=True)
    parser.add_argument("--pass-through-env", metavar="VAR1,VAR2,...", default="")
    parser.add_argument("--exec-timeout", type=int, default=300)
    parser.add_argument("--terminal", metavar="TERMINAL", default=None)
    parser.add_argument("--terminal-args", metavar="ARGS", default=None)
    parser.add_argument(
        "--update-spec",
        metavar="SPEC",
        default=".",
        help="Pip install specifier for sandbox_update_start() (default: .)",
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        default=False,
        help="Automatically run sandbox_update_start() on startup",
    )
    args, remaining = parser.parse_known_args()

    global _PASS_THROUGH_KEYS, _EXEC_TIMEOUT, _TERMINAL, _TERMINAL_ARGS, _UPDATE_SPEC, _UPDATE_AUTO
    _PASS_THROUGH_KEYS = [k.strip() for k in args.pass_through_env.split(",") if k.strip()]
    _EXEC_TIMEOUT = args.exec_timeout
    _TERMINAL = args.terminal
    _TERMINAL_ARGS = args.terminal_args
    _UPDATE_SPEC = args.update_spec
    _UPDATE_AUTO = args.auto_update

    if _UPDATE_AUTO:
        # Fire-and-forget: update runs in background thread
        job_id = str(uuid.uuid4())[:8]
        threading.Thread(
            target=_run_update_background,
            args=(job_id,),
            daemon=True,
        ).start()
        logger.info("Auto-update started (job_id=%s)", job_id)

    sys.argv = [sys.argv[0]] + remaining
    mcp.run()


if __name__ == "__main__":
    main()
