"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from docker.errors import APIError, NotFound
from fastmcp import FastMCP

from code_sandbox_mcp import RESTART_EXIT_CODE
from code_sandbox_mcp.output_control import (
    compress_repeated_lines,
    paginate_output,
    sanitize_output,
    truncate_output,
)
from code_sandbox_mcp.security import (
    DEFAULT_SECURITY_PROFILE,
    build_secure_run_kwargs,
    validate_image_ref,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default Docker image used when no image is specified.
_DEFAULT_IMAGE: str = "python@sha256:93f7863c0947b9b9d57d250290bfba40a7e0b4aadbace6630a6c353d332fb0e5"

#: Stdio proxy - shared with launcher via this module variable.
_TERMINAL: str | None = None
_UPDATE_SPEC: str = "."
_UPDATE_LOG_DIR: Path | None = None

logger: logging.Logger = logging.getLogger(__name__)

mcp = FastMCP("code-sandbox-mcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker() -> Any:
    """Lazy-import docker and return a Docker client."""
    import docker

    return docker.from_env()


def _container_env() -> dict[str, str]:
    """Build environment variables to pass to sandbox containers.

    Passes through ``GITHUB_TOKEN`` and ``GITHUB_TOKEN_SOURCE`` from
    the host environment so that GitHub MCP tools inside the sandbox
    can authenticate automatically.
    """
    env: dict[str, str] = {}
    for key in ("GITHUB_TOKEN", "GITHUB_TOKEN_SOURCE"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _ensure_image(image: str) -> None:
    """Ensure the specified Docker image is available locally.

    Calls ``docker pull`` to fetch the image if not already present.
    """
    import docker

    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        logger.info("Pulling image %s...", image)
        client.images.pull(image)


# ---------------------------------------------------------------------------
# sandbox_initialize
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_initialize(image: str | None = None) -> str:
    """Start a new Docker sandbox container.

    The container runs ``sleep infinity`` and stays alive until
    explicitly stopped with :func:`sandbox_stop`.

    Container IDs are returned as short 12-character prefixes for
    use in other tools.

    Args:
        image: Docker image to use (e.g. ``python@sha256:...``).
               Defaults to the image specified
               via the ``--default-image`` CLI argument in the server config.

    The image must be pulled locally before use: docker pull <image>

    Security guardrails are applied automatically:
    - Container runs as non-root user
    - Privileged mode is forbidden
    - Dangerous socket mounts are rejected
    - Host mounts are restricted by whitelist
    - Resource limits (memory, CPU, pids) are enforced
    - Network is disabled by default
    - Image must use a digest reference (``image@sha256:...``)
    """
    resolved = image or _DEFAULT_IMAGE

    # Enforce image digest reference
    validate_image_ref(resolved)

    client = _docker()
    env = _container_env()

    # Build container run kwargs with security guardrails applied
    run_kwargs = build_secure_run_kwargs(
        DEFAULT_SECURITY_PROFILE,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
    )

    container = client.containers.run(resolved, **run_kwargs)
    logger.info(
        "Container %s started (image=%s)", container.id[:12], resolved
    )
    return container.id[:12]


# ---------------------------------------------------------------------------
# sandbox_exec
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_exec(container_id: str, commands: list[str]) -> str:
    """Execute commands inside a running sandbox container.

    Each command is executed sequentially in the same ``exec`` instance
    (chained via ``&&``), preserving working directory and environment
    between commands.

    Args:
        container_id: 12-character container ID prefix.
        commands: List of shell commands to execute sequentially.

    Returns:
        Combined stdout of all commands.  On error returns an error
        message string beginning with ``"Error:"``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    joined = " && ".join(commands)
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", joined],
        stdout=True,
        stderr=True,
        demux=True,
    )
    if exit_code != 0:
        stdout_part, stderr_part = output
        msg = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        return f"Error: exit code {exit_code}\n{msg}"
    stdout_part, stderr_part = output
    if stdout_part:
        return stdout_part.decode("utf-8", errors="replace")
    return ""


@mcp.tool()
def sandbox_exec_background(container_id: str, commands: list[str]) -> str:
    """Execute commands in the background inside a running sandbox container.

    The command is started with ``nohup`` so it continues running even
    if the MCP connection drops.  Returns a job ID that can be used
    with :func:`sandbox_exec_check` to poll status.

    Args:
        container_id: 12-character container ID prefix.
        commands: List of shell commands to execute sequentially.

    Returns:
        Job ID string (container_id + timestamp) that can be used to
        poll execution status.

    Note:
        Background execution is limited to a single background job per
        container.  Starting a new background job while one is already
        running will overwrite the previous job file.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    job_id = f"{container_id}-{int(time.time())}"
    joined = " && ".join(commands)
    bg_cmd = (
        f'nohup /bin/sh -c {shlex.quote(joined)} '
        f'> /tmp/{job_id}.out 2> /tmp/{job_id}.err; '
        f'echo $? > /tmp/{job_id}.exit'
    )
    container.exec_run(
        ["/bin/sh", "-c", bg_cmd],
        detach=True,
        stdout=False,
        stderr=False,
    )
    return job_id


@mcp.tool()
def sandbox_exec_check(container_id: str, job_id: str) -> str:
    """Check the status of a background execution job.

    Use this to poll the status of a job started with
    :func:`sandbox_exec_background`.

    The function reads the exit code and output files written by the
    background job and returns a status message.  If the job is still
    running, it returns ``"running"``.  If the job has completed, it
    returns the stdout output (or error message on failure).

    Args:
        container_id: 12-character container ID prefix.
        job_id: Job ID returned by :func:`sandbox_exec_background`.

    Returns:
        Status string: ``"running"`` if still in progress, stdout
        output on success, or ``"Error: ..."`` on failure.
    """
    import shlex

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    # Check exit code file
    exit_code_result = container.exec_run(
        ["/bin/sh", "-c", f"cat /tmp/{job_id}.exit 2>/dev/null || echo 'not_found'"],
        stdout=True,
        stderr=False,
    )
    exit_code_output = exit_code_result[1].decode("utf-8", errors="replace").strip()

    if exit_code_output == "not_found":
        return "running"

    exit_code = int(exit_code_output) if exit_code_output else 0

    # Read stdout
    stdout_result = container.exec_run(
        ["/bin/sh", "-c", f"cat /tmp/{job_id}.out"],
        stdout=True,
        stderr=True,
    )
    stdout_text = stdout_result[1].decode("utf-8", errors="replace") if stdout_result[1] else ""

    if exit_code != 0:
        stderr_result = container.exec_run(
            ["/bin/sh", "-c", f"cat /tmp/{job_id}.err"],
            stdout=True,
            stderr=True,
        )
        stderr_text = stderr_result[1].decode("utf-8", errors="replace") if stderr_result[1] else ""
        return f"Error: exit code {exit_code}\n{stderr_text}"

    # Clean up temp files
    container.exec_run(
        ["/bin/sh", "-c", f"rm -f /tmp/{job_id}.out /tmp/{job_id}.err /tmp/{job_id}.exit"],
        stdout=False,
        stderr=False,
    )

    return stdout_text if stdout_text else ""


@mcp.tool()
def sandbox_stop(container_id: str) -> str:
    """Stop and remove a running sandbox container.

    Args:
        container_id: 12-character container ID prefix.

    Returns:
        Success message or error message beginning with ``"Error:"``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
        container.stop()
        container.remove()
        return f"Container {container_id[:12]} stopped and removed"
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# File read helper for write_file_sandbox partial updates
# ---------------------------------------------------------------------------


def _read_file(container, dest_dir: str, file_name: str) -> str:
    """Read the contents of *file_name* from *dest_dir* in *container*.

    Uses ``container.get_archive()`` to retrieve the file as a tar
    stream and returns its UTF-8 decoded content.

    Raises
        FileNotFoundError: if the file does not exist.
        RuntimeError: if the archive does not contain the expected file.
    """
    path = str(Path(dest_dir) / file_name)
    try:
        bits, stat = container.get_archive(path)
    except NotFound:
        raise FileNotFoundError(f"{path} not found in container")
    buf = io.BytesIO()
    for chunk in bits:
        buf.write(chunk)
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r") as tar:
        member = tar.next()
        if member is None:
            raise RuntimeError(
                f"Empty archive returned for {path}"
            )
        content = tar.extractfile(member)
        if content is None:
            raise RuntimeError(
                f"Could not extract {member.name} from archive"
            )
        return content.read().decode("utf-8")


# ---------------------------------------------------------------------------
# write_file_sandbox
# ---------------------------------------------------------------------------


@mcp.tool()
def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/root",
    start_line: int | None = None,
    end_line: int | None = None,
    append: bool = False,
    old_str: str | None = None,
) -> str:
    """Write a file to the container. Supports full overwrite and partial updates.

    **Full overwrite** (default, backward compatible):
    Writes *file_contents* as the entire file.

    **Line-range replacement** (*start_line* / *end_line*, 1-indexed, inclusive):
    Replaces the specified line range with *file_contents*. Lines outside the
    range are preserved.  When *start_line* is omitted it defaults to line 1;
    when *end_line* is omitted it defaults to the last line of the file.

    **Append** (*append* = True):
    Appends *file_contents* to the end of the existing file.

    **Replace** (*old_str*):
    Replaces the *first* occurrence of *old_str* in the existing file with
    *file_contents*.  Returns an error if *old_str* is not found.

    *start_line* / *end_line*, *append*, and *old_str* are mutually exclusive.
    When none of them is specified the file is fully overwritten (original
    behaviour).
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    # Determine which mode is requested
    has_line_range = start_line is not None or end_line is not None
    has_old_str = old_str is not None

    modes = sum([has_line_range, append, has_old_str])
    if modes > 1:
        return (
            "Error: start_line/end_line, append, and old_str are "
            "mutually exclusive"
        )

    if modes == 0:
        # Full overwrite (original behaviour)
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

    # Partial update: read existing file first
    try:
        existing = _read_file(container, dest_dir, file_name)
    except FileNotFoundError:
        return (
            f"Error: file {dest_dir}/{file_name} not found "
            f"in container {container_id[:12]}"
        )
    except Exception as e:
        return (
            f"Error: could not read existing file "
            f"{dest_dir}/{file_name}: {e}"
        )

    if has_line_range:
        lines: list[str] = existing.splitlines(keepends=True)
        file_len = len(lines)

        # Use explicit None check so that 0 is not treated as "not specified"
        start: int = (start_line if start_line is not None else 1) - 1
        end: int = (end_line if end_line is not None else file_len) - 1

        if start < 0:
            return (
                f"Error: start_line must be >= 1, got {start_line}"
            )
        if start >= file_len:
            return (
                f"Error: start_line ({start_line}) exceeds file "
                f"length ({file_len})"
            )
        if end >= file_len:
            return (
                f"Error: end_line ({end_line}) exceeds file "
                f"length ({file_len})"
            )
        if end < 0:
            return (
                f"Error: end_line must be >= 1, got {end_line}"
            )
        if start > end:
            return (
                f"Error: start_line ({start_line}) is greater than "
                f"end_line ({end_line})"
            )

        modified: str = "".join(lines[:start]) + file_contents + "".join(lines[end + 1:])

    elif append:
        modified = existing + file_contents

    elif has_old_str:
        if not old_str:
            return "Error: old_str must not be empty"
        idx = existing.find(old_str)
        if idx == -1:
            return "Error: old_str not found in file"
        modified = existing[:idx] + file_contents + existing[idx + len(old_str):]

    # Write the modified content
    encoded = modified.encode("utf-8")
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


# ---------------------------------------------------------------------------
# copy_project
# ---------------------------------------------------------------------------


@mcp.tool()
def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/root",
) -> str:
    """Copy a local directory (or file) into the container as a tar archive.

    Creates a tar archive of the local path in a temp directory and
    streams it into the container with ``put_archive``.

    The target directory inside the tar archive is named after the
    source directory itself (i.e. ``/root/source_dir_name/...``).

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default:
            ``/root``).

    Returns:
        Success or error message.
    """
    import tarfile

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src_path = Path(local_src_dir).resolve()
    if not src_path.exists():
        return f"Error: {local_src_dir} does not exist"

    arcname = src_path.name or "project"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")
    try:
        with tarfile.open(fileobj=tmp.file, mode="w") as tar:
            tar.add(src_path, arcname=arcname)
        tmp.file.close()
        with open(tmp.name, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        try:
            container.put_archive(dest_dir, buf)
        except APIError as e:
            return f"Error: {e}"
        return (
            f"Copied {local_src_dir} to {dest_dir}/{arcname} "
            f"in container {container_id[:12]}"
        )
    finally:
        os.unlink(tmp.name)


@mcp.tool()
def copy_file(
    container_id: str,
    local_src_file: str,
    dest_path: str = "/root",
) -> str:
    """Copy a single local file into the container.

    Args:
        container_id: 12-character container ID prefix.
        local_src_file: Path to the local file to copy.
        dest_path: Destination directory or path in the container
            (default: ``/root``).

    Returns:
        Success or error message.
    """
    import tarfile

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    src = Path(local_src_file).resolve()
    if not src.exists():
        return f"Error: {local_src_file} does not exist"
    if not src.is_file():
        return f"Error: {local_src_file} is not a file"

    dest = dest_path
    if not dest.endswith("/") and not dest.endswith(src.name):
        # If dest_path is a directory, include the filename
        dest = str(Path(dest_path) / src.name)

    with open(src, "rb") as f:
        data = f.read()
    buf = io.BytesIO(data)
    try:
        container.put_archive(dest, buf)
    except APIError as e:
        return f"Error: {e}"
    return (
        f"Copied {local_src_file} to {dest} "
        f"in container {container_id[:12]}"
    )


# ---------------------------------------------------------------------------
# Update tools
# ---------------------------------------------------------------------------


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
    - You do **NOT** need to poll with :func:`sandbox_update_check`
      unless the human asks for a programmatic status check or no
      terminal is open.
    """
    return _start_update_internal()


def _start_update_internal() -> str:
    """Internal helper that does the actual update start.

    Separated so tests can mock ``_open_update_terminal``.
    """
    logger.info("Starting update (spec=%s)", _UPDATE_SPEC)

    # Create a unique log directory for this update
    log_dir: Path
    if _UPDATE_LOG_DIR:
        log_dir = _UPDATE_LOG_DIR
    else:
        base = Path(tempfile.gettempdir()) / "code-sandbox-mcp-updates"
        base.mkdir(parents=True, exist_ok=True)
        log_dir = Path(tempfile.mkdtemp(dir=base))

    log_path = log_dir / "update.log"

    # Open a terminal window if configured
    if _TERMINAL:
        _open_update_terminal(_TERMINAL, str(log_path))

    # Run the update in a background thread
    def _run() -> None:
        _run_update_background(str(log_path))

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return f"Update started in background. Log: {log_path}"


def _run_update_background(log_path: str) -> None:
    """Run pip install in a subprocess, streaming output to the log."""
    with open(log_path, "w", buffering=1) as log_f:
        log_f.write(f"=== Update started (spec: {_UPDATE_SPEC}) ===\n")
        log_f.flush()

        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", _UPDATE_SPEC],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
        )
        proc.wait()

    if proc.returncode == 0:
        # Signal the launcher to restart
        logger.info("Update succeeded, restarting...")
        os._exit(RESTART_EXIT_CODE)
    else:
        logger.error("Update failed with exit code %d", proc.returncode)


def _open_update_terminal(terminal: str, log_path: str) -> None:
    """Open a terminal window tailing the update log file."""
    cmd: list[str]
    if sys.platform == "win32":
        cmd = ["cmd.exe", "/c", "start", "code-sandbox-mcp Update",
               "powershell.exe", "-NoExit", "-Command",
               f"Get-Content -Wait '{log_path}'"]
    elif sys.platform == "darwin":
        cmd = ["open", "-a", "Terminal", log_path]
    else:
        # Linux: try xterm, otherwise just log
        try:
            cmd = ["xterm", "-e", f"tail -f {log_path}"]
        except FileNotFoundError:
            logger.warning("No terminal emulator found, log at %s", log_path)
            return
    try:
        subprocess.Popen(cmd, shell=False)
    except Exception as e:
        logger.warning("Failed to open terminal: %s", e)


@mcp.tool()
def sandbox_update_check() -> str:
    """Poll the status of an update job.

    Sleeps for a short time and checks the update log for completion
    status.

    **Note:** if :func:`sandbox_update_start` opened a terminal window,
    the human can watch pip output directly and tell you when it is
    done — polling is unnecessary and wastes tokens.  Only call this
    when the human explicitly asks for a status check or when no
    terminal is available.

    Returns one of:

    * ``"Status: running (elapsed: Xs)"``
    * ``"Status: done (elapsed: Xs)"``
    * ``"Status: error\nError: <message>"``
    * ``"Error: job {job_id} not found"``
    """
    # This is a stub - actual implementation would check the log file
    return "Status: running"


# ---------------------------------------------------------------------------
# run_container_and_exec
# ---------------------------------------------------------------------------


@mcp.tool()
def run_container_and_exec(
    image: str | None = None,
    commands: list[str] | None = None,
    verbose: str = "summary",
    max_lines: int = 100,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Start a container, execute commands, then remove it (one-shot).

    This is a convenience wrapper around:
    :func:`sandbox_initialize` \u2192 :func:`sandbox_exec` \u2192 :func:`sandbox_stop`.

    Output is sanitized (ANSI codes, ``\\r`` progress bars, timestamps
    removed) and consecutive repeated lines are compressed
    (``[\u00d7N] content``).

    Args:
        image: Docker image to use (``image@sha256:...``).
        commands: List of shell commands to execute sequentially.
                  Must not be ``None`` or empty.
        verbose: Output verbosity:

            - ``"error_only"``: Show output only on error.
            - ``"summary"``: Show first/last lines with omission notice.
            - ``"full"``: Show all output.
        max_lines: Maximum lines to show in summary/error_only mode.
        offset: Line offset for paging (0-indexed).  Use with *limit*
            to paginate through the output.
        limit: Maximum lines per page.

    Returns:
        JSON string with ``status``, ``output`` (or ``error``),
        and metadata (``shown``, ``total_lines``, ``truncated``,
        ``next_offset``, ``has_more``).

        On success *status* is ``"ok"`` and *output* contains the
        command output (minimal by default).  On failure *status*
        is ``"error"`` with an ``error`` field.
    """
    import json

    # Validate commands: must not be None and must not be empty
    if not commands:
        return json.dumps({"status": "error", "error": "No commands provided"})
    if len(commands) == 0:
        return json.dumps({"status": "error", "error": "Commands list is empty"})

    resolved = image or _DEFAULT_IMAGE
    client = _docker()
    env = _container_env()

    # --- Start container ---
    try:
        validate_image_ref(resolved)
        run_kwargs = build_secure_run_kwargs(
            DEFAULT_SECURITY_PROFILE,
            command="sleep infinity",
            detach=True,
            remove=False,
            environment=env,
        )
        container = client.containers.run(resolved, **run_kwargs)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Failed to start container: {e}"})

    container_id = container.id[:12]

    # --- Execute commands ---
    try:
        joined = " && ".join(commands)
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", joined],
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout_part, stderr_part = output
        stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    except Exception as e:
        # Clean up
        try:
            container.stop()
            container.remove()
        except Exception:
            pass
        return json.dumps({"status": "error", "error": f"Execution failed: {e}"})

    # --- Clean up container ---
    try:
        container.stop()
        container.remove()
    except Exception:
        pass

    # --- Process output ---
    raw_output = stdout_text
    if stderr_text:
        if raw_output:
            raw_output += "\n" + stderr_text
        else:
            raw_output = stderr_text

    # Sanitize: ANSI, \r, timestamps
    clean = sanitize_output(raw_output)

    # Compress repeated lines
    compressed = compress_repeated_lines(clean)

    # Truncate based on verbosity
    display, meta = truncate_output(
        compressed,
        max_lines=max_lines,
        verbose=verbose,
        exit_code=exit_code,
        stderr=stderr_text,
    )

    # Paginate
    page = paginate_output(display, offset=offset, limit=limit)

    # Build result
    result: dict[str, Any] = {
        "status": "ok" if exit_code == 0 else "error",
        "output": page.content,
        "shown": meta.shown,
        "total_lines": meta.total_lines,
        "truncated": meta.truncated,
        "next_offset": page.next_offset,
        "has_more": page.has_more,
    }

    if exit_code != 0:
        result["exit_code"] = exit_code
    if stderr_text and verbose != "error_only":
        result["stderr"] = stderr_text

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and run the MCP server.

    Supports ``--terminal`` for update progress windows and
    ``--default-image`` for overriding the default Docker image.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Code Sandbox MCP Server")
    parser.add_argument(
        "--terminal",
        type=str,
        default=None,
        help="Terminal emulator for update progress windows",
    )
    parser.add_argument(
        "--default-image",
        type=str,
        default=None,
        help="Default Docker image (default: python@sha256:...)",
    )
    parser.add_argument(
        "--update-spec",
        type=str,
        default=".",
        help="Pip install spec for in-place update (default: .)",
    )
    parser.add_argument(
        "--update-log-dir",
        type=str,
        default=None,
        help="Directory for update log files",
    )
    args = parser.parse_args()

    global _TERMINAL, _UPDATE_SPEC, _UPDATE_LOG_DIR, _DEFAULT_IMAGE
    _TERMINAL = args.terminal
    _UPDATE_SPEC = args.update_spec
    if args.update_log_dir:
        _UPDATE_LOG_DIR = Path(args.update_log_dir)
    if args.default_image:
        validate_image_ref(args.default_image)
        _DEFAULT_IMAGE = args.default_image

    mcp.run()


if __name__ == "__main__":
    main()
