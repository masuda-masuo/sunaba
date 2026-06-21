"""Exec tools: sandbox_exec, sandbox_exec_background, sandbox_exec_check."""

from __future__ import annotations

import base64
import json
import os
import shlex
import time
from typing import Any

from docker.errors import NotFound

from code_sandbox_mcp.journal import record_exec as journal_record_exec
from code_sandbox_mcp.output_control import (
    OutputMetadata,
    compress_failures,
    compress_repeated_lines,
    paginate_output,
    sanitize_output,
    truncate_by_tokens,
    truncate_output,
)
from code_sandbox_mcp.result_cache import (
    compute_cache_key,
    get_cached_result,
    set_cached_result,
)
from code_sandbox_mcp.tools.common import RECOVERY_DOCKER_TIMEOUT, _docker


def sandbox_exec(
    container_id: str,
    commands: list[str],
    verbose: str = "summary",
    max_lines: int = 100,
    offset: int = 0,
    limit: int = 50,
    timeout: int = 0,
    max_output_tokens: int = 0,
    input_hash: str = "",
) -> str:
    """Execute commands inside a running sandbox container.

    Each command is executed sequentially in the same ``exec`` instance
    (chained via ``&&``), preserving working directory and environment
    between commands.

    .. note:: **Multibyte characters and newlines in commands**

       Pass each command as a plain string — JSON-RPC (and therefore the
       MCP transport layer) does not allow **raw newlines inside a JSON
       string value**, so including one causes an ``Unterminated string``
       parse error before the request even reaches the server.  To run a
       multi-line shell command use ``\\n`` escape sequences, or write the
       script to a file first and execute it.  Multibyte characters
       (e.g. Japanese) are safe as long as no literal newline appears
       inside the JSON string value.

    Args:
        container_id: 12-character container ID prefix.
        commands: List of shell commands to execute sequentially.
        verbose: Output verbosity:

            - ``"error_only"``: Show output only on failure.
            - ``"summary"``: Show first/last lines with omission notice.
            - ``"full"``: Show all output.
        max_lines: Maximum lines to show in summary/error_only mode.
        offset: Line offset for paging (0-indexed).  Use with *limit*
            to paginate through the output.
        limit: Maximum lines per page.
        timeout: Maximum seconds to let the command run (``0`` = no
            limit, the default).  When the timeout expires the process
            is killed and the tool returns ``status="timeout"`` with
            ``exit_code=124`` (the standard exit code for
            ``timeout(1)``).
        max_output_tokens: Token budget for output (``0`` = no limit).
            When set, the output is summarised to fit within this many
            estimated tokens and a ``resource://run/{run_id}/output``
            handle is included for full retrieval.

    Returns:
        JSON string with ``status``, ``output``, and metadata
        (``shown``, ``total_lines``, ``truncated``, ``next_offset``,
        ``has_more``).  On failure also includes ``exit_code`` and
        ``stderr``.  ``status`` is ``"timeout"`` when *timeout* was
        exceeded.
    """
    if timeout < 0:
        return json.dumps({"status": "error", "error": "timeout must be >= 0"})
    if max_output_tokens < 0:
        return json.dumps({"status": "error", "error": "max_output_tokens must be >= 0"})

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    # --- Result cache lookup ---
    try:
        raw = container.image.tags[0] if container.image.tags else container.image.id
        image_ref = str(raw) if not isinstance(raw, str) else raw
    except Exception:
        image_ref = container_id[:12]
    cache_key = compute_cache_key(image_ref, commands, input_hash=input_hash)
    cached = get_cached_result(cache_key)
    if cached is not None:
        # Journal the cache hit
        journal_record_exec(
            container_id[:12],
            commands,
            cached.get("exit_code", 0),
            verbose=verbose,
            cached=True,
            output_size=0,
        )
        cached["cached"] = True
        return json.dumps(cached)

    # --- Execute commands ---
    joined = " && ".join(commands)
    encoded = base64.b64encode(joined.encode("utf-8")).decode("ascii")
    tmpf = f"/tmp/.sx_{os.urandom(4).hex()}.sh"
    runner = f"timeout {timeout} {tmpf}" if timeout > 0 else tmpf
    cmd = (
        f"echo {shlex.quote(encoded)} | base64 -d > {tmpf}"
        f" && chmod +x {tmpf}"
        f" && {runner}; rc=$?"
        f"; rm -f {tmpf}"
        f"; exit $rc"
    )
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    # Merge for output processing: success shows stdout only,
    # failure merges both so AI sees the full failure context
    if exit_code == 0:
        raw_output = stdout_text
    else:
        if stdout_text and stderr_text:
            raw_output = stdout_text + "\n" + stderr_text
        else:
            raw_output = stdout_text or stderr_text

    raw_size = len(raw_output.encode("utf-8"))
    clean = sanitize_output(raw_output)

    # Compress repeated lines
    compressed = compress_repeated_lines(clean)

    # Compress isomorphic failures
    if exit_code != 0:
        compressed = compress_failures(compressed)

    # Token-budget truncation (takes precedence over line-based)
    if max_output_tokens > 0:
        display, original_tokens = truncate_by_tokens(compressed, max_output_tokens)
        meta = OutputMetadata(
            shown=len(display.split("\n")),
            total_lines=original_tokens,
            truncated=original_tokens > max_output_tokens,
        )
        display += "\n[resource: run output available via sandbox_read_journal]"
    else:
        display, meta = truncate_output(
            compressed,
            max_lines=max_lines,
            verbose=verbose,
            exit_code=exit_code,
            stderr=stderr_text,
        )

    page = paginate_output(display, offset=offset, limit=limit)

    if exit_code == 0:
        status = "ok"
    elif timeout > 0 and exit_code == 124:
        status = "timeout"
    else:
        status = "error"

    result: dict[str, Any] = {
        "status": status,
        "output": page.content,
        "shown": meta.shown,
        "total_lines": meta.total_lines,
        "truncated": meta.truncated,
        "next_offset": page.next_offset,
        "has_more": page.has_more,
        "cached": False,
    }
    if exit_code != 0:
        result["exit_code"] = exit_code
    if stderr_text and verbose != "error_only":
        result["stderr"] = stderr_text

    # Store in result cache
    set_cached_result(cache_key, result)

    journal_record_exec(
        container_id[:12],
        commands,
        exit_code,
        verbose=verbose,
        cached=False,
        output_size=raw_size,
        max_output_tokens=max_output_tokens if max_output_tokens > 0 else None,
        input_hash=input_hash,
    )

    return json.dumps(result)


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
    encoded = base64.b64encode(joined.encode("utf-8")).decode("ascii")
    tmpf = f"/tmp/.sx_{os.urandom(4).hex()}.sh"
    inner_cmd = (
        f"echo {shlex.quote(encoded)} | base64 -d > {tmpf} && chmod +x {tmpf} && {tmpf}; rc=$?; rm -f {tmpf}; exit $rc"
    )
    bg_cmd = (
        f"nohup /bin/sh -c {shlex.quote(inner_cmd)} "
        f"> /tmp/{job_id}.out 2> /tmp/{job_id}.err; "
        f"echo $? > /tmp/{job_id}.exit"
    )
    container.exec_run(
        ["/bin/sh", "-c", bg_cmd],
        detach=True,
        stdout=False,
        stderr=False,
    )
    return job_id


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
    # Recovery/poll path: use a short Docker API timeout so a wedged or
    # unhealthy container fails fast instead of hanging the session
    # (Issue #181).
    client = _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
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
        [
            "/bin/sh",
            "-c",
            f"rm -f /tmp/{job_id}.out /tmp/{job_id}.err /tmp/{job_id}.exit",
        ],
        stdout=False,
        stderr=False,
    )

    return stdout_text if stdout_text else ""
