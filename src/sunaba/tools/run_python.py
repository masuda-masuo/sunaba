"""run_python tool: execute arbitrary Python code inside a sandbox container."""

from __future__ import annotations

import base64
import json
import logging
import secrets
import shlex

from docker.errors import NotFound

from sunaba.journal import record_tool_use
from sunaba.output_control import (
    sanitize_output,
    truncate_output,
)
from sunaba.tools.common import WORKSPACE, _docker, container_not_found_error

logger: logging.Logger = logging.getLogger(__name__)

# In-container runner for ``run_python``.  Decodes the user's Python code
# from base64, writes it to a temp file, executes it with ``python3``,
# captures stdout/stderr and exit code, cleans up the temp file, and emits
# the result as a JSON envelope wrapped in per-call sentinels.
# ``__CODE_B64__`` / ``__MARK_A__`` / ``__MARK_B__`` / ``__WORKING_DIR_REPR__``
# are substituted on the host.  This script runs inside the sandbox
# container, **never on the host**.
_RUN_PYTHON_RUNNER = r'''
import sys, json, base64, subprocess, os, tempfile

CODE_B64 = "__CODE_B64__"
MARK_A = "__MARK_A__"
MARK_B = "__MARK_B__"
WORKING_DIR = __WORKING_DIR_REPR__

def emit(obj):
    sys.stdout.write(MARK_A + json.dumps(obj) + MARK_B)
    sys.stdout.flush()
    sys.exit(0)

try:
    code = base64.b64decode(CODE_B64).decode("utf-8")
except Exception as e:
    emit({"status": "error", "error": "could not decode code: " + repr(e)})

# Write user code to a temp file so multi-line code with arbitrary
# quoting runs as-is through the Python interpreter.
fd, tmp_path = tempfile.mkstemp(suffix=".py", prefix="rp_")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(code)

    if WORKING_DIR:
        cwd = WORKING_DIR
    else:
        cwd = None

    proc = subprocess.run(
        [sys.executable or "python3", tmp_path],
        capture_output=True, text=True,
        cwd=cwd,
    )
    stdout = proc.stdout
    stderr = proc.stderr
    exit_code = proc.returncode
finally:
    try:
        os.unlink(tmp_path)
    except Exception:
        pass

status = "ok" if exit_code == 0 else "error"
emit({"status": status, "stdout": stdout, "stderr": stderr, "exit_code": exit_code})
'''


def run_python(
    container_id: str,
    code: str,
    working_dir: str = "",
    max_lines: int = 2000,
    verbose: str = "summary",
) -> str:
    """Execute Python code in the container.

    Base64-transported so quotes and newlines need no escaping.
    Temp file cleaned up after run regardless of outcome.

    Args:
        container_id: Container ID.
        code: Python source.
        working_dir: Working directory.
        max_lines: Max output lines.
        verbose: Output verbosity.

    Returns:
        JSON: status, stdout, stderr, exit_code, and truncation
        metadata.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    resolved_cwd = working_dir if working_dir else WORKSPACE

    record_tool_use(
        container_id[:12],
        "run_python",
        {"working_dir": resolved_cwd},
    )

    code_b64 = base64.b64encode(code.encode("utf-8")).decode("ascii")
    nonce = secrets.token_hex(8)
    mark_a = f"<<<RP_{nonce}>>>"
    mark_b = f"<<<END_RP_{nonce}>>>"

    runner = (
        _RUN_PYTHON_RUNNER
        .replace("__CODE_B64__", code_b64)
        .replace("__MARK_A__", mark_a)
        .replace("__MARK_B__", mark_b)
        .replace("__WORKING_DIR_REPR__", repr(resolved_cwd))
    )
    runner_b64 = base64.b64encode(runner.encode("utf-8")).decode("ascii")
    tmpf = f"/tmp/.rp_{nonce}.py"
    cmd = (
        f"echo {shlex.quote(runner_b64)} | base64 -d > {tmpf}"
        f" && python3 {tmpf}; rc=$?"
        f"; rm -f {tmpf}"
        f"; exit $rc"
    )

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output if isinstance(output, tuple) else (output, b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    # Infrastructure error: the Docker exec itself failed.
    if exit_code != 0 and not stdout_text:
        detail = stderr_text.strip() or stdout_text.strip() or f"exit code {exit_code}"
        return json.dumps({
            "status": "error",
            "error": f"runner shell command failed: {detail}",
        })

    start = stdout_text.find(mark_a)
    end = stdout_text.find(mark_b)
    if start == -1 or end == -1:
        detail = stderr_text.strip() or stdout_text.strip() or "no output"
        return json.dumps({
            "status": "error",
            "error": f"runner produced no valid result: {detail}",
        })

    try:
        result_str = stdout_text[start + len(mark_a):end]
        result = json.loads(result_str)
    except json.JSONDecodeError as e:
        return json.dumps({
            "status": "error",
            "error": f"could not parse runner result: {e}",
        })

    # --- Output-size management (mirrors sandbox_exec's pipeline) ---
    raw_stdout = result.get("stdout", "")
    raw_stderr = result.get("stderr", "")
    runner_exit_code = result.get("exit_code", 0)

    clean_stdout = sanitize_output(raw_stdout)
    clean_stderr = sanitize_output(raw_stderr)

    stdout_display, stdout_meta = truncate_output(
        clean_stdout,
        max_lines=max_lines,
        verbose=verbose,
        exit_code=runner_exit_code,
        stderr=clean_stderr,
    )

    # stderr gets the same line budget: an unbounded traceback would blow
    # past the caller's context even when stdout is tiny.  "full" is
    # honoured for both streams; failures keep their tail either way.
    stderr_display, stderr_meta = truncate_output(
        clean_stderr,
        max_lines=max_lines,
        verbose="full" if verbose == "full" else "summary",
    )

    return json.dumps({
        "status": result.get("status", "ok"),
        "stdout": stdout_display,
        "stderr": stderr_display,
        "exit_code": runner_exit_code,
        "stdout_shown": stdout_meta.shown,
        "stdout_total_lines": stdout_meta.total_lines,
        "stdout_truncated": stdout_meta.truncated,
        "stderr_shown": stderr_meta.shown,
        "stderr_total_lines": stderr_meta.total_lines,
        "stderr_truncated": stderr_meta.truncated,
    })
