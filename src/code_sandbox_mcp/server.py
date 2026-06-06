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
import shutil
import subprocess
import sys
import tarfile
import tempfile
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
_DEFAULT_IMAGE: str = "python:3.12-slim-bookworm"

_CONTAINER_LOG_PATH = "/tmp/mcp.log"
# Host-side update log: use the OS temp dir so it works on Windows too.
_UPDATE_LOG_PATH = str(Path(tempfile.gettempdir()) / "mcp_update.log")


def _container_env() -> dict[str, str]:
    return {
        key: os.environ[key]
        for key in _PASS_THROUGH_KEYS
        if key in os.environ
    }


def _docker() -> docker.DockerClient:
    return docker.from_env()


# ---------------------------------------------------------------------------
# WSL detection and cmd.exe resolution
# ---------------------------------------------------------------------------


def _is_wsl() -> bool:
    """Return True when running inside WSL or a Docker container on WSL2.

    Detection priority:
    1. ``WSL_DISTRO_NAME`` env var — set in native WSL sessions.
    2. ``/proc/version`` containing ``microsoft`` — Docker containers
       running on the WSL2 backend share the WSL2 kernel and have this
       string in ``/proc/version`` even though ``WSL_DISTRO_NAME`` is
       not inherited by the container environment.
    """
    if sys.platform == "win32":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    # Docker containers on the WSL2 backend share the WSL2 kernel.
    # /proc/version contains "microsoft" in that case.
    try:
        with open("/proc/version") as _f:
            return "microsoft" in _f.read().lower()
    except OSError:
        return False


def _wsl_cmd_exe() -> str | None:
    """Return the full path to cmd.exe usable from WSL, or None if not found.

    On WSL, Windows executables live under /mnt/c/... and are not always
    on PATH. We try shutil.which first (works when the Windows System32
    directory is in the WSL PATH), then fall back to the canonical mount
    point.
    """
    # shutil.which respects PATH, so it finds cmd.exe when
    # /mnt/c/Windows/System32 (or similar) is in PATH.
    found = shutil.which("cmd.exe")
    if found:
        return found

    # Common fallback paths for standard Windows installations.
    fallbacks = [
        "/mnt/c/Windows/System32/cmd.exe",
        "/mnt/c/WINDOWS/system32/cmd.exe",
    ]
    for path in fallbacks:
        if Path(path).exists():
            return path

    return None


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
            raise TimeoutError(
                f"Command timed out after {timeout} seconds"
            )
        if isinstance(result[0], Exception):
            raise result[0]
        return result[0]

    if timeout is not None and _EXEC_RUN_SUPPORTS_TIMEOUT:
        kwargs["timeout"] = timeout
    return container.exec_run(cmd, **kwargs)


# ---------------------------------------------------------------------------
# Terminal window tracking
# ---------------------------------------------------------------------------

#: Set of container_ids for which a terminal window has already been opened.
_terminals_opened: set[str] = set()
_terminals_lock = threading.Lock()


def _forget_terminal(container_id: str) -> None:
    """Remove *container_id* from the terminal tracking set.

    Called when a container is stopped so the next invocation can
    open a fresh terminal window.
    """
    with _terminals_lock:
        _terminals_opened.discard(container_id)


def _terminal_already_open(container_id: str) -> bool:
    """Return True if a terminal window is already open for *container_id*."""
    with _terminals_lock:
        return container_id in _terminals_opened


# ---------------------------------------------------------------------------
# Terminal auto-open helper
# ---------------------------------------------------------------------------


def _open_terminal_with_logs(container_id: str) -> None:
    """Open a terminal window tailing /tmp/mcp.log inside the container.

    On Windows and WSL, ``cmd.exe /c start`` is used to detach the window
    from the MCP server process, so the window survives server shutdown.

    If a terminal window has already been opened for the same
    *container_id*, this call is a no-op so that multiple sequential
    ``sandbox_exec`` / ``sandbox_exec_background`` calls reuse the
    same window.
    """
    if _TERMINAL is None:
        return

    with _terminals_lock:
        if container_id in _terminals_opened:
            logger.debug(
                "Terminal already open for container %s, skipping",
                container_id[:12],
            )
            return
        _terminals_opened.add(container_id)

    short_id = container_id[:12]

    ps_script = (
        f"docker exec {container_id} tail -f {_CONTAINER_LOG_PATH} "
        "2>$null; "
        f"Write-Host ''; "
        f"Write-Host '=== Container {short_id} stopped ===' "
        "-ForegroundColor Yellow; "
        "Read-Host 'Press Enter to close this window'"
    )

    unix_script = (
        f"docker exec {container_id} tail -f {_CONTAINER_LOG_PATH} "
        "2>/dev/null; "
        "echo; "
        f"echo '=== Container {short_id} stopped ==='; "
        "echo 'Press Enter to close this window.'; read"
    )

    try:
        if sys.platform == "win32":
            if _TERMINAL_ARGS:
                extra = _TERMINAL_ARGS.format(
                    container_id=container_id
                ).split()
                subprocess.Popen(
                    [_TERMINAL] + extra,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                )
            else:
                subprocess.Popen(
                    [
                        "cmd.exe", "/c", "start",
                        "powershell.exe", "-NoExit", "-Command",
                        ps_script,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        elif _is_wsl():
            cmd_exe = _wsl_cmd_exe()
            if cmd_exe is None:
                logger.warning(
                    "WSL: cmd.exe not found; cannot open terminal window"
                )
                return
            if _TERMINAL_ARGS:
                extra = _TERMINAL_ARGS.format(
                    container_id=container_id
                ).split()
                subprocess.Popen(
                    [_TERMINAL] + extra,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                subprocess.Popen(
                    [
                        cmd_exe, "/c", "start",
                        "powershell.exe", "-NoExit", "-Command",
                        ps_script,
                    ],
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
                extra = shlex.split(
                    _TERMINAL_ARGS.format(container_id=container_id)
                )
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
        logger.warning(
            "Failed to open terminal (%s): %s", _TERMINAL, e
        )


def _open_update_terminal(log_path: str) -> None:
    """Open a terminal window tailing the update log file (host-side).

    Unlike ``_open_terminal_with_logs``, this tails a file on the *host*
    filesystem (not inside a Docker container), so no ``docker exec`` is
    needed.
    """
    if _TERMINAL is None:
        return

    ps_script = (
        f"Get-Content -Path '{log_path}' -Wait; "
        "Write-Host ''; "
        "Write-Host '=== Update complete ===' -ForegroundColor Green; "
        "Read-Host 'Press Enter to close this window'"
    )

    unix_script = (
        f"tail -f '{log_path}'; "
        "echo; "
        "echo '=== Update complete ==='; "
        "echo 'Press Enter to close this window.'; read"
    )

    try:
        if sys.platform == "win32":
            subprocess.Popen(
                [
                    "cmd.exe", "/c", "start",
                    "powershell.exe", "-NoExit", "-Command",
                    ps_script,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif _is_wsl():
            cmd_exe = _wsl_cmd_exe()
            if cmd_exe is None:
                logger.warning(
                    "WSL: cmd.exe not found; cannot open update terminal"
                )
                return
            subprocess.Popen(
                [
                    cmd_exe, "/c", "start",
                    "powershell.exe", "-NoExit", "-Command",
                    ps_script,
                ],
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
            subprocess.Popen(
                [_TERMINAL, "-e", unix_script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        logger.warning(
            "Failed to open update terminal (%s): %s", _TERMINAL, e
        )


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
        _exec_run(
            container,
            ["sh", "-c", f"echo {header!r} >> {_CONTAINER_LOG_PATH}"],
            stdout=False,
            stderr=False,
        )

        try:
            tee_cmd = f"({cmd}) 2>&1 | tee -a {_CONTAINER_LOG_PATH}"
            exit_code, output = _exec_run(
                container,
                ["sh", "-c", tee_cmd],
                stdout=True,
                stderr=True,
                demux=False,
                timeout=timeout,
            )
            decoded = (
                output.decode("utf-8", errors="replace")
                if output else ""
            )
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

#: When True, ``sandbox_update_start()`` is called automatically on
#: server start.
_UPDATE_AUTO: bool = False


# ---------------------------------------------------------------------------
# Background update helper
# ---------------------------------------------------------------------------


def _run_update_background(job_id: str) -> None:
    """Run pip install --force-reinstall in a background thread.

    Redirects pip stdout/stderr directly to ``_UPDATE_LOG_PATH`` so the
    terminal window opened by ``sandbox_update_start`` can tail it in real
    time.  Unlike the previous implementation that read pip output line by
    line in a Python thread (which suffered from GIL/scheduler delays), the
    subprocess writes directly to the file, eliminating the thread-
    scheduling dependency.

    On success exits the process with
    :data:`~code_sandbox_mcp.RESTART_EXIT_CODE` (42, restart signal).
    On failure sets job status to ``error``.
    """
    started_at = time.time()

    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "started_at": started_at,
        }

    try:
        log_path = Path(_UPDATE_LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with log_path.open("w", encoding="utf-8") as log_f:
            # Write an initial message immediately so the terminal window
            # shows activity before pip produces any output.  Without this
            # the log file stays at 0 bytes until pip starts writing,
            # making it impossible to tell whether the update is running
            # or stuck (#22).
            log_f.write(
                f"=== Update started (spec: {_UPDATE_SPEC}) ===\n"
            )
            log_f.flush()

            # Redirect stdout/stderr directly to the log file so pip
            # output is written immediately without going through a
            # Python-level read loop.  This avoids the GIL/scheduler
            # issue described in #24.
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "pip", "install",
                    "--force-reinstall", _UPDATE_SPEC,
                ],
                stdout=log_f,
                stderr=log_f,
            )
            proc.wait()

        # Read back the full log for the job output
        output = log_path.read_text()

        finished_at = time.time()

        if proc.returncode == 0:
            with _jobs_lock:
                _jobs[job_id].update(
                    status="done",
                    finished_at=finished_at,
                    elapsed=finished_at - started_at,
                    output=output,
                )
            time.sleep(2)
            sys.exit(RESTART_EXIT_CODE)
        else:
            with _jobs_lock:
                _jobs[job_id].update(
                    status="error",
                    finished_at=finished_at,
                    elapsed=finished_at - started_at,
                    error=f"pip exited with code {proc.returncode}",
                    output=output,
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
def sandbox_initialize(image: str | None = None) -> str:
    """Start a Docker container and return its container_id.

    Choose the image based on the project requirements:
    - Python project                        → python:3.12-slim-bookworm (default)
    - Python project requiring 3.11         → python:3.11-slim-bookworm
    - Node.js project                       → node:20-slim
    - General Linux / shell scripts         → ubuntu:24.04
    - Custom/production environment         → specify explicitly

    When *image* is omitted the default is used, which can be overridden
    via the ``--default-image`` CLI argument in the server config.

    The image must be pulled locally before use: docker pull <image>
    """
    resolved = image or _DEFAULT_IMAGE
    client = _docker()
    env = _container_env()
    container = client.containers.run(
        resolved,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
    )
    logger.info(
        "Container %s started (image=%s)", container.id[:12], resolved
    )
    return container.id


@mcp.tool()
def sandbox_exec(
    container_id: str,
    commands: list[str],
) -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return (
            f"Error: failed to get container "
            f"{container_id[:12]}: {e}"
        )

    # Only truncate the log file on the first call (when the terminal
    # window is not yet open).  Subsequent calls append to the existing
    # log so the already-open tail -f window keeps showing output.
    if not _terminal_already_open(container_id):
        _exec_run(
            container,
            ["sh", "-c", f"truncate -s 0 {_CONTAINER_LOG_PATH}"],
            stdout=False,
            stderr=False,
        )

    _open_terminal_with_logs(container_id)

    output_parts: list[str] = []
    for cmd in commands:
        header = f"$ {cmd}"
        output_parts.append(header)
        _exec_run(
            container,
            ["sh", "-c", f"echo {header!r} >> {_CONTAINER_LOG_PATH}"],
            stdout=False,
            stderr=False,
        )

        try:
            tee_cmd = f"({cmd}) 2>&1 | tee -a {_CONTAINER_LOG_PATH}"
            exit_code, output = _exec_run(
                container,
                ["sh", "-c", tee_cmd],
                stdout=True,
                stderr=True,
                demux=False,
                timeout=_EXEC_TIMEOUT,
            )
            decoded = (
                output.decode("utf-8", errors="replace")
                if output else ""
            )
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
                break
        except TimeoutError as e:
            output_parts.append(f"Error: {e}")
            break
        except Exception as e:
            output_parts.append(f"Error executing command: {e}")
            break

    result = "\n".join(output_parts)
    if _TERMINAL:
        result += (
            "\n\nA terminal window has been opened "
            f"(tail -f {_CONTAINER_LOG_PATH})."
        )
    return result


@mcp.tool()
def sandbox_exec_background(
    container_id: str,
    commands: list[str],
) -> str:
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return (
            f"Error: failed to get container "
            f"{container_id[:12]}: {e}"
        )

    job_id = str(uuid.uuid4())[:8]
    threading.Thread(
        target=_run_commands_in_background,
        args=(
            job_id,
            container_id,
            commands,
            _EXEC_TIMEOUT,
        ),
        daemon=True,
    ).start()

    _open_terminal_with_logs(container_id)

    if _TERMINAL:
        terminal_note = (
            "\nA terminal window has been opened "
            f"(tail -f {_CONTAINER_LOG_PATH})."
        )
    else:
        terminal_note = ""
    return (
        f"Job started: {job_id}\n"
        f"Check status with: sandbox_exec_check("
        f"container_id=\"{container_id[:12]}\", "
        f"job_id=\"{job_id}\"){terminal_note}"
    )


@mcp.tool()
def sandbox_exec_check(
    container_id: str,
    job_id: str,
    wait_seconds: int = 10,
    show_partial: bool = False,
) -> str:
    """Poll the status of a background exec job.

    Sleeps for *wait_seconds* before returning so the caller does not
    need to implement its own delay between polls (default: 10s).

    **Tip:** if a terminal window is open the human can watch progress
    directly and tell you when it is done — in that case there is no
    need to poll at all.  Only poll when the human asks for a status
    check or when no terminal is available.
    """
    time.sleep(wait_seconds)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return f"Error: job {job_id} not found"
    status = job["status"]
    if status == "running":
        elapsed = time.time() - job["started_at"]
        if show_partial:
            return (
                f"Status: running (elapsed: {elapsed:.0f}s)\n"
                f"--- partial output ---\n{job.get('output', '')}"
            )
        return f"Status: running (elapsed: {elapsed:.0f}s)"
    if status == "error":
        return f"Status: error\nError: {job['error']}"
    return (
        f"Status: done (elapsed: {job.get('elapsed', 0):.0f}s)\n"
        f"{job['output']}"
    )


@mcp.tool()
def sandbox_update_start() -> str:
    """Start an in-place update in the background.

    Runs ``pip install --force-reinstall`` asynchronously and streams
    the output to a terminal window (if ``--terminal`` is configured)
    so the human can watch progress in real time.

    On success the server process restarts automatically via the
    launcher (exit code 42).  The update source is controlled by the
    ``--update-spec`` CLI flag (default: ``.``).

    **Workflow:**
    - Call this tool once — a terminal window opens showing pip output.
    - The human watches the terminal and tells you when it is done.
    - You do NOT need to poll with ``sandbox_update_check`` unless the
      human asks for a programmatic status check or no terminal is open.
    """
    job_id = str(uuid.uuid4())[:8]
    threading.Thread(
        target=_run_update_background,
        args=(job_id,),
        daemon=True,
    ).start()

    # Open a terminal showing the update log so the human can watch.
    _open_update_terminal(_UPDATE_LOG_PATH)

    terminal_note = (
        f"\nPip output is streaming to a terminal window ({_UPDATE_LOG_PATH})."
        "\nWatch the terminal — when it finishes the server will restart"
        " automatically. You do NOT need to poll; just wait for the human"
        " to tell you it's done."
    ) if _TERMINAL else (
        f"\nNo terminal configured. Poll with: "
        f"sandbox_update_check(job_id=\"{job_id}\")"
    )

    return f"Update job started: {job_id}{terminal_note}"


@mcp.tool()
def sandbox_update_check(
    job_id: str,
    wait_seconds: int = 30,
) -> str:
    """Poll the status of an update job.

    Sleeps for *wait_seconds* before returning (default: 30s).

    **Note:** if ``sandbox_update_start`` opened a terminal window, the
    human can watch pip output directly and tell you when it is done —
    polling is unnecessary and wastes tokens.  Only call this when the
    human explicitly asks for a status check or when no terminal is
    available.

    Returns one of:

    * ``Status: running (elapsed: Xs)``
    * ``Status: done (elapsed: Xs)``
    * ``Status: error\\nError: <message>``
    * ``Error: job {job_id} not found``
    """
    time.sleep(wait_seconds)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return f"Error: job {job_id} not found"
    status = job["status"]
    if status == "running":
        elapsed = time.time() - job["started_at"]
        return f"Status: running (elapsed: {elapsed:.0f}s)"
    if status == "error":
        return (
            f"Status: error\nError: {job['error']}\n"
            f"{job.get('output', '')}"
        )
    return (
        f"Status: done (elapsed: {job.get('elapsed', 0):.0f}s)\n"
        f"{job.get('output', '')}"
    )


@mcp.tool()
def sandbox_stop(container_id: str) -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
        container.stop(timeout=10)
        container.remove(v=True)
        _forget_terminal(container_id)
        return (
            f"Container {container_id[:12]} stopped and removed"
        )
    except NotFound:
        _forget_terminal(container_id)
        return (
            f"Container {container_id[:12]} not found "
            "(already removed?)"
        )
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/root",
) -> str:
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
        return (
            f"Written {file_name} to {dest_dir} "
            f"in container {container_id[:12]}"
        )
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/root",
) -> str:
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
        return (
            f"Copied {local_src_dir} to "
            f"{dest_dir}/{src.name} in container "
            f"{container_id[:12]}"
        )
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = "/root",
) -> str:
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"
    src = Path(local_src_file)
    if not src.is_file():
        return f"Error: {local_src_file} is not a file\n"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname=src.name)
    buf.seek(0)
    try:
        container.put_archive(dest_path, buf)
        return (
            f"Copied {src.name} to {dest_path} "
            f"in container {container_id[:12]}"
        )
    except APIError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    global _PASS_THROUGH_KEYS, _EXEC_TIMEOUT
    global _TERMINAL, _TERMINAL_ARGS
    global _UPDATE_SPEC, _UPDATE_AUTO
    global _DEFAULT_IMAGE

    parser = argparse.ArgumentParser(
        description=(
            "code-sandbox-mcp: "
            "Docker sandbox MCP server"
        ),
        add_help=True,
    )
    parser.add_argument(
        "--pass-through-env",
        metavar="VAR1,VAR2,...",
        default="",
    )
    parser.add_argument(
        "--exec-timeout",
        type=int,
        default=300,
    )
    parser.add_argument(
        "--terminal",
        metavar="TERMINAL",
        default=None,
    )
    parser.add_argument(
        "--terminal-args",
        metavar="ARGS",
        default=None,
    )
    parser.add_argument(
        "--default-image",
        metavar="IMAGE",
        default=_DEFAULT_IMAGE,
        help=(
            "Default Docker image for sandbox_initialize() "
            f"(default: {_DEFAULT_IMAGE})"
        ),
    )
    parser.add_argument(
        "--update-spec",
        metavar="SPEC",
        default=".",
        help=(
            "Pip install specifier for "
            "sandbox_update_start() (default: .)"
        ),
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        default=False,
        help=(
            "Automatically run sandbox_update_start() "
            "on startup"
        ),
    )
    args, remaining = parser.parse_known_args()

    _PASS_THROUGH_KEYS = [
        k.strip()
        for k in args.pass_through_env.split(",")
        if k.strip()
    ]
    _EXEC_TIMEOUT = args.exec_timeout
    _TERMINAL = args.terminal
    _TERMINAL_ARGS = args.terminal_args
    _DEFAULT_IMAGE = args.default_image
    _UPDATE_SPEC = args.update_spec
    _UPDATE_AUTO = args.auto_update

    if _UPDATE_AUTO:
        job_id = str(uuid.uuid4())[:8]
        threading.Thread(
            target=_run_update_background,
            args=(job_id,),
            daemon=True,
        ).start()
        logger.info(
            "Auto-update started (job_id=%s)", job_id
        )

    sys.argv = [sys.argv[0]] + remaining
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
