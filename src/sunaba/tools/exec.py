"""Exec tools: sandbox_exec, sandbox_exec_background, sandbox_exec_check."""

from __future__ import annotations

import base64
import json
import os
import shlex
import time
from typing import Annotated, Any

from docker.errors import NotFound
from pydantic import BeforeValidator

from sunaba.journal import record_exec as journal_record_exec
from sunaba.journal import record_tool_use
from sunaba.output_control import (
    OutputMetadata,
    compress_failures,
    compress_repeated_lines,
    paginate_output,
    sanitize_output,
    truncate_by_tokens,
    truncate_output,
)
from sunaba.tools.common import RECOVERY_DOCKER_TIMEOUT, _coerce_list_arg, _docker


def sandbox_exec(
    container_id: str,
    commands: Annotated[list[str], BeforeValidator(_coerce_list_arg)] | None = None,
    working_dir: str = "",
    verbose: str = "summary",
    max_lines: int = 100,
    offset: int = 0,
    limit: int = 50,
    timeout: int = 0,
    max_output_tokens: int = 0,
    argv: Annotated[list[str], BeforeValidator(_coerce_list_arg)] | None = None,
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

    .. rubric:: Use when

    - Running **git commands** (add, commit, push, log, diff) inside the container
    - Running build scripts, test commands, or any shell command
    - Running VCS/``gh`` calls via the *argv* parameter to avoid shell quoting issues
    - Inspecting runtime state (package versions, environment variables, file existence)

    .. rubric:: Don't use when

    - **Reading file content** — use :func:`read_file_range` instead
    - **Editing files** — use :func:`write_file_sandbox` (declarative) or :func:`transform_file` (imperative) instead
    - **Searching file content** — use :func:`search_in_container` instead
    - **Listing files** — use :func:`list_files` instead
    - **Writing multi-line Python scripts** — use :func:`transform_file` (base64-encoded, no escaping issues)

    .. rubric:: Prefer over

    - Prefer dedicated tools (``write_file_sandbox``, ``transform_file``, ``search_in_container``, etc.) over ``sandbox_exec`` for their specific operations
    - Prefer *argv* mode over *commands* mode for ``gh`` calls to avoid shell quoting footguns

    .. rubric:: Fallback

    - If the container has no shell, *argv* mode can run binaries directly via ``execve``
    - For long-running or fire-and-forget tasks use :func:`sandbox_exec_background`
    - For a one-shot container lifecycle use :func:`run_container_and_exec`

    Args:
        container_id: 12-character container ID prefix.
        commands: List of shell commands to execute sequentially.
        working_dir: Working directory to set before executing commands
            (default ``""`` = no change).  When specified, ``cd`` to
            this directory is prepended to the command chain.
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
        argv: When given, run this argument vector **directly** (no
            ``/bin/bash -c``), so quoting, ``$'...'`` and embedded newlines
            are passed through literally.  Mutually exclusive with
            *commands*.  Intended for VCS/``gh`` calls where shell
            quoting is a footgun (issue #234 / #228).  *working_dir* is
            honoured via the exec ``workdir`` and *timeout* is prepended
            as ``timeout(1)`` argv.

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

    # --- Mode selection: commands (shell) vs argv (shell-free execve) ---
    # ``argv`` runs the program directly via ``exec_run`` without an
    # intervening ``/bin/bash -c``, so quoting / ``$'...'`` / embedded
    # newlines pass through literally (issue #234, #228 footgun).
    if argv is not None and commands:
        return json.dumps(
            {"status": "error", "error": "commands and argv are mutually exclusive"}
        )
    if argv is not None and not argv:
        return json.dumps(
            {"status": "error", "error": "argv must be a non-empty list"}
        )
    if argv is None and not commands:
        return json.dumps(
            {"status": "error", "error": "either commands or argv is required"}
        )

    use_argv = argv is not None
    if not use_argv and working_dir:
        assert commands is not None  # guaranteed by validation above
        commands = [f"cd {shlex.quote(working_dir)}"] + commands

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    if argv is not None:
        journal_subject = list(argv)
    else:
        assert commands is not None  # guaranteed by validation above
        journal_subject = commands

    # --- Execute ---
    if use_argv:
        assert argv is not None  # guaranteed by validation above
        # Direct execve: no /bin/sh, so the program receives argv
        # verbatim.  ``timeout(1)`` is prepended as argv (rather than a
        # shell wrapper) to preserve the timeout semantics.
        run_argv = ["timeout", str(timeout), *argv] if timeout > 0 else list(argv)
        exec_kwargs: dict[str, Any] = {"stdout": True, "stderr": True, "demux": True}
        if working_dir:
            exec_kwargs["workdir"] = working_dir
        exit_code, output = container.exec_run(run_argv, **exec_kwargs)
    else:
        assert commands is not None  # guaranteed by validation above
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
            ["/bin/bash", "-c", cmd],
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
    }
    if exit_code != 0:
        result["exit_code"] = exit_code
    if stderr_text and verbose != "error_only":
        result["stderr"] = stderr_text

    journal_record_exec(
        container_id[:12],
        journal_subject,
        exit_code,
        verbose=verbose,
        output_size=raw_size,
        max_output_tokens=max_output_tokens if max_output_tokens > 0 else None,
    )

    return json.dumps(result)


def sandbox_exec_background(container_id: str, commands: Annotated[list[str], BeforeValidator(_coerce_list_arg)], working_dir: str = "") -> str:
    """Execute commands in the background inside a running sandbox container.

    The command is started with ``nohup`` so it continues running even
    if the MCP connection drops.  Returns a job ID that can be used
    with :func:`sandbox_exec_check` to poll status.

    Args:
        container_id: 12-character container ID prefix.
        commands: List of shell commands to execute sequentially.
        working_dir: Working directory to set before executing commands
            (default ``""`` = no change).

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

    if working_dir:
        commands = [f"cd {shlex.quote(working_dir)}"] + commands

    job_id = f"{container_id}-{int(time.time())}"
    joined = " && ".join(commands)
    encoded = base64.b64encode(joined.encode("utf-8")).decode("ascii")
    tmpf = f"/tmp/.sx_{os.urandom(4).hex()}.sh"
    inner_cmd = (
        f"echo {shlex.quote(encoded)} | base64 -d > {tmpf} && chmod +x {tmpf} && {tmpf}; rc=$?; rm -f {tmpf}; exit $rc"
    )
    bg_cmd = (
        f"date +%s > /tmp/{job_id}.start && "
        f"nohup /bin/bash -c {shlex.quote(inner_cmd)} "
        f"> /tmp/{job_id}.out 2> /tmp/{job_id}.err; "
        f"echo $? > /tmp/{job_id}.exit"
    )
    container.exec_run(
        ["/bin/bash", "-c", bg_cmd],
        detach=True,
        stdout=False,
        stderr=False,
    )

    # Record the dispatch in the audit journal.  The command runs detached
    # so its exit code is unknown at this point; -1 is a sentinel meaning
    # "background launch, outcome not yet known" (poll via sandbox_exec_check).
    # Without this, background execs were completely invisible to the
    # journal while foreground sandbox_exec records every call (Issue #359).
    journal_record_exec(
        container_id[:12],
        commands,
        -1,
        verbose="background",
    )
    return job_id


def sandbox_exec_check(container_id: str, job_id: str) -> str:
    """Check the status of a background execution job.

    Use this to poll the status of a job started with
    :func:`sandbox_exec_background`.

    Reads the exit code and output files written by the background
    job and returns a JSON status object with timing information
    suitable for human-in-the-loop decision making.

    Args:
        container_id: 12-character container ID prefix.
        job_id: Job ID returned by :func:`sandbox_exec_background`.

    Returns:
        JSON string with fields:

        * ``status``: ``"running"`` (job still in progress),
          ``"completed"`` (job finished), or ``"error"``
          (container not found / Docker API error).
        * ``elapsed_seconds``: seconds since the job started
          (``null`` if the ``.start`` file is unavailable, e.g.
          for jobs started before the timing feature was added).
        * ``last_output_seconds_ago``: seconds since stdout/stderr
          was last written (``null`` if no output files exist yet).
          Only present when ``status`` is ``"running"``.
        * ``exit_code``: integer exit code (only when ``status``
          is ``"completed"``).
        * ``output``: stdout text on success (only when
          ``status`` is ``"completed"`` and ``exit_code`` is 0).
        * ``error`` / ``stderr``: error details on failure.
    """
    # Recovery/poll path: use a short Docker API timeout so a wedged or
    # unhealthy container fails fast instead of hanging the session
    # (Issue #181).
    client = _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(container_id[:12], "sandbox_exec_check", {"job_id": job_id})

    # --- Single exec: gather timing, staleness, and exit status ---
    status_result = container.exec_run(
        [
            "/bin/sh", "-c",
            "echo NOW=$(date +%s); "
            "echo START=$(cat /tmp/{}.start 2>/dev/null || echo ''); "
            "echo OUT_MTIME=$(stat -c %Y /tmp/{}.out 2>/dev/null || echo 0); "
            "echo ERR_MTIME=$(stat -c %Y /tmp/{}.err 2>/dev/null || echo 0); "
            "echo EXIT=$(cat /tmp/{}.exit 2>/dev/null || echo 'not_found')".format(
                job_id, job_id, job_id, job_id,
            ),
        ],
        stdout=True, stderr=False,
    )
    status_output = status_result[1].decode("utf-8").strip()
    status_kv: dict[str, str] = {}
    for line in status_output.split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            status_kv[k.strip()] = v.strip()

    # --- Parse now (safe) ---
    now = 0
    try:
        now = int(status_kv.get("NOW", "0"))
    except (ValueError, TypeError):
        pass

    # --- Elapsed: read .start file (available for jobs started after 2026-06) ---
    elapsed_seconds = None
    start_raw = status_kv.get("START", "")
    if start_raw:
        try:
            start_epoch = int(start_raw)
            elapsed_seconds = now - start_epoch
        except (ValueError, TypeError):
            pass

    # --- Staleness: time since last output write ---
    last_output_seconds_ago = None
    try:
        out_mtime = int(status_kv.get("OUT_MTIME", "0"))
        err_mtime = int(status_kv.get("ERR_MTIME", "0"))
        last_mtime = out_mtime if out_mtime > err_mtime else err_mtime
        if last_mtime > 0:
            last_output_seconds_ago = now - last_mtime
    except (ValueError, TypeError):
        pass

    # --- Check exit code file ---
    exit_code_output = status_kv.get("EXIT", "not_found")

    if exit_code_output == "not_found":
        return json.dumps({
            "status": "running",
            "elapsed_seconds": elapsed_seconds,
            "last_output_seconds_ago": last_output_seconds_ago,
        })

    try:
        exit_code = int(exit_code_output) if exit_code_output else 0
    except (ValueError, TypeError):
        exit_code = -1

    # --- Read stdout ---
    stdout_result = container.exec_run(
        ["/bin/sh", "-c", f"cat /tmp/{job_id}.out"],
        stdout=True, stderr=True,
    )
    stdout_text = stdout_result[1].decode("utf-8", errors="replace") if stdout_result[1] else ""

    result: dict[str, Any] = {
        "status": "completed",
        "elapsed_seconds": elapsed_seconds,
        "exit_code": exit_code,
    }

    if exit_code != 0:
        stderr_result = container.exec_run(
            ["/bin/sh", "-c", f"cat /tmp/{job_id}.err"],
            stdout=True, stderr=True,
        )
        stderr_text = stderr_result[1].decode("utf-8", errors="replace") if stderr_result[1] else ""
        result["error"] = f"exit code {exit_code}"
        if stderr_text:
            result["stderr"] = stderr_text
    else:
        result["output"] = stdout_text

    # --- Clean up temp files ---
    container.exec_run(
        [
            "/bin/sh",
            "-c",
            f"rm -f /tmp/{job_id}.out /tmp/{job_id}.err /tmp/{job_id}.exit /tmp/{job_id}.start",
        ],
        stdout=False,
        stderr=False,
    )

    return json.dumps(result)
