"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from docker.errors import APIError, NotFound
from fastmcp import FastMCP

from code_sandbox_mcp import RESTART_EXIT_CODE
from code_sandbox_mcp.edit_verify import (
    apply_patch_to_file,
    lint_file,
    read_file,
    read_file_lines,
    run_verify,
    search_files,
    type_check_file,
    write_file,
)
from code_sandbox_mcp.output_control import (
    compress_repeated_lines,
    paginate_output,
    sanitize_output,
    truncate_output,
)
from code_sandbox_mcp.journal import (
    get_or_create_run_id,
    record_boundary_crossing,
    record_copy,
    record_exec as journal_record_exec,
    record_initialize,
    record_stop,
    read_journal,
    get_runs,
    get_journal_path,
)
from code_sandbox_mcp.trace import (
    generate_json_trace,
    generate_html_trace,
    get_trace_dir,
)
from code_sandbox_mcp.security import (
    DEFAULT_SECURITY_PROFILE,
    build_secure_run_kwargs,
    validate_image_ref,
)
from code_sandbox_mcp.token import (
    generate_token,
    verify_and_consume,
    reject_token,
    get_pending_tokens,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default Docker image used when no image is specified.
#:
#: Uses the pre-built sandbox image (``docker/Dockerfile.sandbox``) which
#: includes git/gh/uv/ripgrep/ruff/pyright/semgrep and runs as the
#: dedicated ``sandbox`` user (non-root).
#:
#: **このフィールドは直接編集しないこと。**
#: ``docker/Dockerfile.sandbox`` を変更すると CI
#: (``.github/workflows/build-sandbox-image.yml``) が自動で
#: GHCR へ push し、新ダイジェストを書き込んだ PR を作成する。
#:
#: ローカルで試す場合::
#:
#:   docker build -f docker/Dockerfile.sandbox -t code-sandbox-mcp/sandbox:latest .
#:   docker images --digests code-sandbox-mcp/sandbox  # sha256 を取得
#:   # 取得した sha256 を下の文字列に貼り付けてテスト
#:
#: Refs: Issue #56, docs/design.md §2.1, §11, §12
_DEFAULT_IMAGE: str = "ghcr.io/masuda-masuo/code-sandbox-mcp/sandbox@sha256:749832ed0d5c642574688e3ad0cc9179f1649495bd2bbc0179e11e25b32b8271"

#: Stdio proxy - shared with launcher via this module variable.
_TERMINAL: str | None = None
_UPDATE_SPEC: str = "."
_UPDATE_LOG_DIR: Path | None = None
#: Shiori repos root path on the host for cp-by-pass git clone (Issue #84).
#: Set via ``--shiori-repos-path`` CLI arg or ``SHIORI_REPOS_PATH`` env var.
#: When set, ``sandbox_initialize`` and ``run_container_and_exec`` can use
#: ``clone_repo`` to copy a pre-cloned repository from this path into the
#: container, bypassing a network ``git clone``.
_SHIORI_REPOS_PATH: str | None = None
#: Compiled pattern for validating clone_repo ``owner/name`` format.
_CLONE_REPO_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9._-]+$")
#: Sensitive file/directory basenames to exclude from tar archive.
_SENSITIVE_FILE_BASENAMES: frozenset[str] = frozenset({
    ".env",
    ".git-credentials",
    ".gitconfig",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
})

logger: logging.Logger = logging.getLogger(__name__)

mcp = FastMCP("code-sandbox-mcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _docker() -> Any:
    """Lazy-import docker and return a Docker client."""
    import docker

    return docker.from_env()


def _container_env(inject_vcs_token: bool = False) -> dict[str, str]:
    """Build environment variables to pass to sandbox containers.

    When *inject_vcs_token* is ``True``, passes through
    ``GITHUB_TOKEN``, ``GITHUB_TOKEN_SOURCE``, and ``GH_TOKEN``
    from the host environment so that GitHub MCP tools inside the
    sandbox can authenticate automatically.

    Token injection is opt-in (``inject_vcs_token=True``) to avoid
    leaking credentials into containers that do not need VCS access
    (principle of least privilege, Issue #57).
    """
    env: dict[str, str] = {}
    if inject_vcs_token:
        for key in ("GITHUB_TOKEN", "GITHUB_TOKEN_SOURCE", "GH_TOKEN"):
            val = os.environ.get(key)
            if val:
                env[key] = val
                logger.info("Injected VCS env var %s into container environment", key)
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
# Shiori clone helper (Issue #84)
# ---------------------------------------------------------------------------


def _validate_clone_repo(clone_repo: str) -> tuple[str, str]:
    """Validate *clone_repo* as ``owner/name`` format.

    Returns:
        (owner, name) tuple.

    Raises:
        ValueError: If the format is invalid.
    """
    if not clone_repo:
        raise ValueError("clone_repo must not be empty")
    parts = clone_repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError(
            f"clone_repo must be 'owner/name' format, got: {clone_repo!r}"
        )
    owner, name = parts
    if not _CLONE_REPO_PATTERN.match(owner) or not _CLONE_REPO_PATTERN.match(name):
        raise ValueError(
            f"clone_repo must be 'owner/name' format with alphanumeric "
            f"characters (._- allowed), got: {clone_repo!r}"
        )
    return owner, name


def _clone_shiori_repo_to_container(
    container: Any,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
) -> str:
    """Copy a Shiori pre-cloned repo into the container.

    Computes the host-side path from ``_SHIORI_REPOS_PATH`` and
    ``clone_repo``, validates it, copies via ``put_archive``, then
    runs ``git fetch --unshallow`` in the container.

    Args:
        container: Docker container object.
        container_id: 12-char container ID prefix.
        clone_repo: ``owner/name`` repository identifier.
        clone_dest: Destination directory inside the container.

    Returns:
        Success message string.
    """

    # Validate clone_dest is a safe path inside the container
    if not clone_dest.startswith("/tmp/"):
        raise ValueError(
            f"clone_dest must start with /tmp/, got: {clone_dest!r}"
        )

    if not _SHIORI_REPOS_PATH:
        raise ValueError(
            "Shiori repos path is not configured. "
            "Set --shiori-repos-path or SHIORI_REPOS_PATH env var."
        )

    _validate_clone_repo(clone_repo)

    repos_root = Path(_SHIORI_REPOS_PATH).resolve()
    if not repos_root.is_dir():
        raise ValueError(f"Shiori repos root not found: {repos_root}")

    clone_from = repos_root / clone_repo
    resolved_from = clone_from.resolve()

    # Path traversal prevention: must stay under repos_root
    try:
        resolved_from.relative_to(repos_root)
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {clone_from} is outside {repos_root}"
        )

    if not resolved_from.is_dir():
        raise ValueError(f"Repository clone not found: {resolved_from}")

    if not (resolved_from / ".git").exists():
        raise ValueError(
            f"Repository clone at {resolved_from} has no .git directory"
        )

    logger.info(
        "Copying Shiori clone %s → container %s:%s",
        resolved_from, container_id[:12], clone_dest,
    )

    # -- Copy via put_archive (same mechanism as copy_project) --
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar")

    def _filter_sensitive(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        name = Path(tarinfo.name).name
        if name in _SENSITIVE_FILE_BASENAMES:
            return None
        if name.startswith(".env."):
            return None
        if "/.ssh/" in tarinfo.name:
            return None
        return tarinfo

    try:
        with tarfile.open(fileobj=tmp.file, mode="w") as tar:
            tar.add(str(resolved_from), arcname="repo", filter=_filter_sensitive)
        tmp.file.close()
        with open(tmp.name, "rb") as f:
            data = f.read()
        buf = io.BytesIO(data)
        try:
            container.put_archive(clone_dest, buf)
        except APIError as e:
            raise RuntimeError(f"Failed to copy repo into container: {e}") from e
    finally:
        os.unlink(tmp.name)

    record_copy(
        container_id[:12],
        "clone_shiori_repo",
        str(resolved_from),
        f"{clone_dest}/repo",
    )

    # -- Run git fetch --unshallow --
    safe_dest = shlex.quote(f"{clone_dest}/repo")
    try:
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", f"cd {safe_dest} && git fetch --unshallow 2>&1"],
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout_part, stderr_part = output
        fetch_output = (
            stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        )
        if exit_code != 0:
            logger.warning(
                "git fetch --unshallow failed (exit=%d): %s",
                exit_code, fetch_output.strip(),
            )
        else:
            logger.info(
                "git fetch --unshallow succeeded: %s", fetch_output.strip(),
            )
    except Exception as e:
        logger.warning("git fetch --unshallow error: %s", e)

    return (
        f"Copied Shiori clone of {clone_repo} → {clone_dest}/repo "
        f"in container {container_id[:12]}"
    )


# ---------------------------------------------------------------------------
# sandbox_initialize
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_initialize(
    image: str | None = None,
    allow_network: bool = False,
    inject_vcs_token: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = "/tmp/repo",
) -> str:
    """Start a new Docker sandbox container.

    The container runs ``sleep infinity`` and stays alive until
    explicitly stopped with :func:`sandbox_stop`.

    Container IDs are returned as short 12-character prefixes for
    use in other tools.

    Args:
        image: Docker image to use (e.g. ``python@sha256:...``).
               Defaults to the image specified
               via the ``--default-image`` CLI argument in the server config.
        allow_network: Whether to allow network access (default ``False``).
               Set to ``True`` for VCS operations (git/gh) that need to
               reach GitHub API.  Network access is a boundary-crossing
               operation and should be used only when necessary.
        inject_vcs_token: Whether to inject VCS authentication tokens
               (``GITHUB_TOKEN``, ``GITHUB_TOKEN_SOURCE``, ``GH_TOKEN``)
               as environment variables in the container (default ``False``).
               Enable only for containers that need git/gh access to
               remote repositories.  Token injection is a boundary-crossing
               operation and should be used only when necessary.
        clone_repo: Optional ``owner/name`` repository to copy from the
               Shiori pre-cloned repos on the host into the container.
               Uses the host path configured via ``--shiori-repos-path``
               (default: ``None`` = no clone copy).
        clone_dest: Destination directory in the container for the
               cloned repository (default: ``/tmp/repo``).
               The actual path will be ``{clone_dest}/repo``.

    The image must be pulled locally before use: docker pull <image>

    Returns:
        Container ID string (12-character prefix).
        If *clone_repo* is specified, a message about the clone copy
        is appended.
    """
    client = _docker()
    resolved = image or _DEFAULT_IMAGE
    env = _container_env(inject_vcs_token=inject_vcs_token)

    try:
        validate_image_ref(resolved)
    except ValueError as e:
        return f"Error: {e}"

    profile = replace(DEFAULT_SECURITY_PROFILE, allow_network=allow_network)

    run_kwargs = build_secure_run_kwargs(
        profile,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
    )

    try:
        _ensure_image(resolved)
        container = client.containers.run(resolved, **run_kwargs)
    except Exception as e:
        return f"Error: {e}"

    cid = container.id[:12]
    logger.info("Container %s started (image=%s)", cid, resolved)
    record_initialize(
        cid,
        resolved,
        allow_network=allow_network,
        inject_vcs_token=inject_vcs_token,
    )

    # -- Shiori clone copy (Issue #84) --
    clone_msg = ""
    if clone_repo:
        try:
            clone_msg = " " + _clone_shiori_repo_to_container(
                container, cid, clone_repo, clone_dest,
            )
        except Exception as e:
            # Clone failure is non-fatal: the container is still usable.
            logger.warning("Shiori clone copy failed: %s", e)
            clone_msg = f" (clone_repo failed: {e})"

    return cid + clone_msg


# ---------------------------------------------------------------------------
# sandbox_exec
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_exec(
    container_id: str,
    commands: list[str],
    verbose: str = "summary",
    max_lines: int = 100,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Execute commands inside a running sandbox container.

    Each command is executed sequentially in the same ``exec`` instance
    (chained via ``&&``), preserving working directory and environment
    between commands.

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

    Returns:
        JSON string with ``status``, ``output``, and metadata
        (``shown``, ``total_lines``, ``truncated``, ``next_offset``,
        ``has_more``).  On failure also includes ``exit_code`` and
        ``stderr``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            {"status": "error", "error": f"container {container_id[:12]} not found"}
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

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

    # Merge for output processing: success shows stdout only,
    # failure merges both so AI sees the full failure context
    if exit_code == 0:
        raw_output = stdout_text
    else:
        if stdout_text and stderr_text:
            raw_output = stdout_text + "\n" + stderr_text
        else:
            raw_output = stdout_text or stderr_text

    clean = sanitize_output(raw_output)
    compressed = compress_repeated_lines(clean)
    display, meta = truncate_output(
        compressed,
        max_lines=max_lines,
        verbose=verbose,
        exit_code=exit_code,
        stderr=stderr_text,
    )
    page = paginate_output(display, offset=offset, limit=limit)

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

    journal_record_exec(
        container_id[:12],
        commands,
        exit_code,
        verbose=verbose,
    )

    return json.dumps(result)


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
        f"nohup /bin/sh -c {shlex.quote(joined)} "
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
    stdout_text = (
        stdout_result[1].decode("utf-8", errors="replace") if stdout_result[1] else ""
    )

    if exit_code != 0:
        stderr_result = container.exec_run(
            ["/bin/sh", "-c", f"cat /tmp/{job_id}.err"],
            stdout=True,
            stderr=True,
        )
        stderr_text = (
            stderr_result[1].decode("utf-8", errors="replace")
            if stderr_result[1]
            else ""
        )
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


@mcp.tool()
def sandbox_stop(container_id: str) -> str:
    """Stop and remove a running sandbox container.

    Args:
        container_id: 12-character container ID prefix.

    Returns:
        Success message or error message beginning with ``"Error:"``.
    """
    client = _docker()
    cid = container_id[:12]
    try:
        container = client.containers.get(container_id)
        container.stop()
        container.remove()
        record_stop(cid)
        return f"Container {cid} stopped and removed"
    except NotFound:
        return f"Error: container {cid} not found"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# write_file_sandbox  --  old_str helper functions
# ---------------------------------------------------------------------------


def _find_all_matches(text: str, pattern: str) -> list[tuple[int, int]]:
    """Find all non-overlapping occurrences of *pattern* in *text*.

    Returns a list of ``(offset, line_number)`` tuples.
    """
    matches: list[tuple[int, int]] = []
    idx = 0
    while True:
        idx = text.find(pattern, idx)
        if idx == -1:
            break
        line_no = text[:idx].count("\n") + 1
        matches.append((idx, line_no))
        idx += 1
    return matches


def _get_line_indent(line: str) -> int:
    """Return the leading whitespace length of *line*."""
    return len(line) - len(line.lstrip())


def _reindent_lines(lines: list[str], delta: int) -> list[str]:
    """Apply an indentation *delta* (number of spaces) to each line.

    Empty/whitespace-only lines are passed through unchanged.
    A positive *delta* adds leading spaces; a negative *delta* removes them.
    """
    result: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            result.append("")
            continue
        if delta >= 0:
            result.append(" " * delta + line)
        else:
            remove = min(-delta, _get_line_indent(line))
            result.append(line[remove:])
    return result


def _try_whitespace_flexible(
    existing: str, old_str: str, new_str: str,
) -> str | None:
    """Attempt whitespace-flexible matching.

    Strips leading/trailing whitespace from each line of *old_str* and
    slides over the file looking for a block whose stripped lines match.
    When found the file's original indentation is preserved and *new_str*
    is re-indented to fit.

    Returns the new file content on success, or ``None`` if no match
    was found.
    """
    existing_lines = existing.splitlines()
    old_lines = old_str.splitlines()
    old_stripped = [line.strip() for line in old_lines]

    if len(old_lines) > len(existing_lines):
        return None

    matches: list[int] = []
    for i in range(len(existing_lines) - len(old_lines) + 1):
        chunk = existing_lines[i : i + len(old_lines)]
        if [line.strip() for line in chunk] == old_stripped:
            matches.append(i)

    if not matches:
        return None

    if len(matches) > 1:
        line_nos = ", ".join(str(m + 1) for m in matches[:10])
        suffix = "..." if len(matches) > 10 else ""
        return (
            f"Error: old_str matches at {len(matches)} locations "
            f"(lines {line_nos}{suffix}) after whitespace normalization. "
            "Add more surrounding context to make it unique."
        )

    i = matches[0]
    chunk = existing_lines[i : i + len(old_lines)]
    file_first_indent = _get_line_indent(chunk[0])
    old_first_indent = _get_line_indent(old_lines[0])
    delta = file_first_indent - old_first_indent
    reindented = _reindent_lines(new_str.splitlines(), delta)
    new_content = "\n".join(reindented)

    # Build character offsets to do a string-level replacement
    # (preserves trailing whitespace and file structure).
    pos = 0
    line_starts: list[int] = []
    for line in existing_lines:
        line_starts.append(pos)
        pos += len(line) + 1  # +1 for newline
    # offset right after the last matched line
    start_offset = line_starts[i]
    end_idx = i + len(old_lines)
    if end_idx < len(line_starts):
        end_offset = line_starts[end_idx]
    else:
        end_offset = len(existing)

    result = existing[:start_offset] + new_content + existing[end_offset:]
    if existing.endswith("\n") and not result.endswith("\n"):
        result += "\n"
    return result


def _build_near_miss_echo(existing: str, old_str: str, dest_path: str) -> str:
    """Build a near-miss error message with the most similar file region.

    Uses :mod:`difflib` to locate the area that best matches *old_str*
    and shows it with line numbers as context for the caller.
    """
    existing_lines = existing.splitlines()

    sm = difflib.SequenceMatcher(None, existing, old_str)
    match = sm.find_longest_match(0, len(existing), 0, len(old_str))

    lines_to_show: list[str] = []

    if match.size >= max(5, len(old_str) * 0.3):
        match_line = existing[: match.a].count("\n") + 1
        match_end = existing[match.a : match.a + match.size].count("\n") + match_line

        ctx_start = max(0, match_line - 4)
        ctx_end = min(len(existing_lines), match_end + 3)

        for i in range(ctx_start, ctx_end):
            prefix = ">>>" if match_line - 1 <= i < match_end else "   "
            lines_to_show.append(f"{prefix} {i + 1:4d} | {existing_lines[i]}")
    else:
        for i in range(min(8, len(existing_lines))):
            lines_to_show.append(f"    {i + 1:4d} | {existing_lines[i]}")

    context_block = "\n".join(lines_to_show)

    return (
        f"Error: old_str not found in {dest_path}.\n"
        f"Most relevant file area:\n"
        f"{context_block}\n"
        "Tip: Use read_file_range first to confirm the exact content "
        "(including whitespace)."
    )


# ---------------------------------------------------------------------------
# write_file_sandbox
# ---------------------------------------------------------------------------


@mcp.tool()
def write_file_sandbox(
    container_id: str,
    file_name: str,
    file_contents: str,
    dest_dir: str = "/home/sandbox",
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
    Replaces *old_str* with *file_contents*.  The matching logic is:

    1. **Exact match** -- if *old_str* appears exactly once, it is replaced.
       If it appears multiple times the call is rejected with the line numbers
       of each match so the caller can add more surrounding context.
    2. **Whitespace-flexible fallback** -- if exact matching fails, leading
       and trailing whitespace is stripped from each line and the search is
       retried.  On success *file_contents* is re-indented to match the
       file's original indentation.
    3. **Near-miss echo** -- if neither strategy finds a match, the most
       similar region of the file is returned with line numbers via
       :func:`difflib.SequenceMatcher`.

    *start_line* / *end_line*, *append*, and *old_str* are mutually exclusive.
    When none of them is specified the file is fully overwritten (original
    behaviour).

    Args:
        container_id: 12-character container ID prefix.
        file_name: Name of the file to write.
        file_contents: Content to write.
        dest_dir: Destination directory in the container (default: ``/home/sandbox``).
        start_line: Start line for line-range replacement (1-indexed, inclusive).
        end_line: End line for line-range replacement (1-indexed, inclusive).
        append: When True, appends to the end of the file.
        old_str: When specified, replaces this string in the existing file.
            Performs uniqueness check, whitespace-flexible fallback, and near-miss echo (see above).

    Returns:
        Success or error message.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    dest_path = f"{dest_dir}/{file_name}"

    # Validate mutual exclusivity
    has_line_range = start_line is not None or end_line is not None
    mode_count = sum([append, old_str is not None, has_line_range])
    if mode_count > 1:
        return "Error: start_line/end_line, append, and old_str are mutually exclusive"

    if old_str is not None and old_str == "":
        return "Error: old_str must not be empty"
    if start_line is not None and start_line < 1:
        return "Error: start_line must be >= 1"

    content = file_contents

    # For partial updates, read existing content
    if append or old_str is not None or has_line_range:
        try:
            existing = read_file(container, dest_path)
        except ValueError as e:
            return f"Error: file {dest_path} not found"
        existing_lines = existing.splitlines()

        # Validate bounds
        if start_line is not None and start_line > len(existing_lines):
            return f"Error: start_line {start_line} exceeds file length ({len(existing_lines)} lines)"
        if end_line is not None:
            if end_line > len(existing_lines):
                return f"Error: end_line {end_line} exceeds file length ({len(existing_lines)} lines)"
            if start_line is not None and start_line > end_line:
                return "Error: start_line is greater than end_line"

        if append:
            sep = "\n" if existing else ""
            content = existing.rstrip("\n") + sep + file_contents
        elif old_str is not None:
            # 1. Exact match with uniqueness check
            exact_matches = _find_all_matches(existing, old_str)
            if len(exact_matches) > 1:
                line_nos = ", ".join(str(m[1]) for m in exact_matches[:10])
                suffix = "..." if len(exact_matches) > 10 else ""
                return (
                    f"Error: old_str matches at {len(exact_matches)} locations "
                    f"(lines {line_nos}{suffix}). "
                    "Add more surrounding context to make it unique."
                )
            if len(exact_matches) == 1:
                idx = exact_matches[0][0]
                content = (
                    existing[:idx]
                    + file_contents
                    + existing[idx + len(old_str) :]
                )
            else:
                # 2. Whitespace-flexible fallback
                result = _try_whitespace_flexible(
                    existing, old_str, file_contents,
                )
                if result is not None:
                    if result.startswith("Error:"):
                        return result
                    content = result
                else:
                    # 3. Near-miss echo
                    return _build_near_miss_echo(existing, old_str, dest_path)
        else:
            start = start_line - 1 if start_line is not None else 0
            end = end_line if end_line is not None else len(existing_lines)
            new_lines = file_contents.splitlines()
            content_lines = existing_lines[:start] + new_lines + existing_lines[end:]
            content = "\n".join(content_lines)
            if file_contents.endswith("\n"):
                content += "\n"

    try:
        write_file(container, container_id[:12], dest_path, content)
    except ValueError as e:
        return f"Error: {e}"
    return f"Written {len(content)} bytes to {dest_path}"


# ---------------------------------------------------------------------------
# copy_project
# ---------------------------------------------------------------------------


@mcp.tool()
def copy_project(
    container_id: str,
    local_src_dir: str,
    dest_dir: str = "/home/sandbox",
) -> str:
    """Copy a local directory (or file) into the container as a tar archive.

    Creates a tar archive of the local path in a temp directory and
    streams it into the container with ``put_archive``.

    The target directory inside the tar archive is named after the
    source directory itself (i.e. ``/home/sandbox/source_dir_name/...``).

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default:
            ``/home/sandbox``).

    Returns:
        Success or error message.
    """
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
    if not src_path.is_dir():
        return f"Error: {local_src_dir} is not a directory"

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
        record_copy(
            container_id[:12], "copy_project", local_src_dir, f"{dest_dir}/{arcname}"
        )
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
    dest_path: str = "/home/sandbox",
) -> str:
    """Copy a single local file into the container.

    Args:
        container_id: 12-character container ID prefix.
        local_src_file: Path to the local file to copy.
        dest_path: Destination directory or path in the container
            (default: ``/home/sandbox``).

    Returns:
        Success or error message.
    """
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
    record_copy(container_id[:12], "copy_file", local_src_file, dest)
    return f"Copied {local_src_file} to {dest} in container {container_id[:12]}"


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
        cmd = [
            "cmd.exe",
            "/c",
            "start",
            "code-sandbox-mcp Update",
            "powershell.exe",
            "-NoExit",
            "-Command",
            f"Get-Content -Wait '{log_path}'",
        ]
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
    allow_network: bool = False,
    inject_vcs_token: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = "/tmp/repo",
) -> str:
    """Start a container, execute commands, then remove it (one-shot).

    This is a convenience wrapper around:
    :func:`sandbox_initialize` → :func:`sandbox_exec` → :func:`sandbox_stop`.

    Output is sanitized (ANSI codes, ``\r`` progress bars, timestamps
    removed, VCS token values masked) and consecutive repeated lines
    are compressed (``[×N] content``).

    Args:
        image: Docker image to use (``image@sha256:...``).
        commands: List of shell commands to execute sequentially.
                  Must not be ``None`` or empty.
        verbose: Output verbosity:

            - ``"error_only"``: Show output only on failure.
            - ``"summary"``: Show first/last lines with omission notice.
            - ``"full"``: Show all output.
        max_lines: Maximum lines to show in summary/error_only mode.
        offset: Line offset for paging (0-indexed).  Use with *limit*
            to paginate through the output.
        limit: Maximum lines per page.
        allow_network: Whether to allow network access (default ``False``).
               Set to ``True`` for VCS operations (git/gh) that need to
               reach GitHub API.
        inject_vcs_token: Whether to inject VCS authentication tokens
               (``GITHUB_TOKEN``, ``GITHUB_TOKEN_SOURCE``, ``GH_TOKEN``)
               as environment variables in the container (default
               ``False``).  Enable only for containers that need git/gh
               access to remote repositories.
        clone_repo: Optional ``owner/name`` repository to copy from the
               Shiori pre-cloned repos on the host into the container.
               Uses the host path configured via ``--shiori-repos-path``
               (default: ``None`` = no clone copy).
        clone_dest: Destination directory in the container for the
               cloned repository (default: ``/tmp/repo``).
               The actual path will be ``{clone_dest}/repo``.

    Returns:
        JSON string with ``status``, ``output`` (or ``error``),
        and metadata (``shown``, ``total_lines``, ``truncated``,
        ``next_offset``, ``has_more``).

        On success *status* is ``"ok"`` and *output* contains the
        command output (minimal by default).  On failure *status*
        is ``"error"`` with an ``error`` field.
    """
    import json

    # Validate commands: must not be None or empty
    if not commands:
        return json.dumps({"status": "error", "error": "No commands provided"})

    resolved = image or _DEFAULT_IMAGE
    client = _docker()
    env = _container_env(inject_vcs_token=inject_vcs_token)

    # --- Start container ---
    try:
        validate_image_ref(resolved)
        profile = replace(DEFAULT_SECURITY_PROFILE, allow_network=allow_network)
        run_kwargs = build_secure_run_kwargs(
            profile,
            command="sleep infinity",
            detach=True,
            remove=False,
            environment=env,
        )
        container = client.containers.run(resolved, **run_kwargs)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})
    except Exception as e:
        return json.dumps(
            {"status": "error", "error": f"Failed to start container: {e}"}
        )

    container_id = container.id[:12]
    record_initialize(
        container_id,
        resolved,
        allow_network=allow_network,
        inject_vcs_token=inject_vcs_token,
    )

    # --- Shiori clone copy (Issue #84) ---
    clone_error: str | None = None
    if clone_repo:
        try:
            _clone_shiori_repo_to_container(
                container, container_id, clone_repo, clone_dest,
            )
        except Exception as e:
            logger.warning("Shiori clone copy failed: %s", e)
            clone_error = str(e)

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
        stdout_text = (
            stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
        )
    except Exception as e:
        # Clean up
        try:
            container.stop()
            container.remove()
        except Exception:
            pass
        record_stop(container_id)
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
    if clone_error:
        result["clone_warning"] = clone_error

    journal_record_exec(
        container_id,
        commands,
        exit_code,
        verbose=verbose,
    )
    if allow_network or inject_vcs_token:
        record_boundary_crossing(
            container_id,
            "run_container_and_exec",
            f"network={allow_network} vcs_token={inject_vcs_token}",
        )

    record_stop(container_id)
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Edit/Verify tools
# ---------------------------------------------------------------------------


@mcp.tool()
def apply_patch(container_id: str, file_path: str, diff_content: str) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    Reads the current file from the container, applies the unified diff,
    and writes the result back.  The caller sends only a compact diff
    instead of the full file content, reducing token cost by 1-2 orders
    of magnitude.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        diff_content: Unified diff string to apply.

    Returns:
        Success message or error description.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    return apply_patch_to_file(client, container_id, file_path, diff_content)


@mcp.tool()
def read_file_range(
    container_id: str,
    file_path: str,
    offset: int = 0,
    limit: int = 50,
) -> str:
    """Read lines from *file_path* starting at *offset*.

    Returns a JSON string with:
    - ``content`` (str): the requested lines
    - ``total_lines`` (int): total lines in the file
    - ``shown`` (int): lines returned
    - ``has_more`` (bool): whether more lines exist after this range
    - ``next_offset`` (int | None): offset for pagination

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        offset: 0-indexed line offset to start reading from.
        limit: Maximum number of lines to return.  Use ``-1`` to read
            all remaining lines from *offset*.

    Returns:
        JSON string with file content and metadata, or an error
        message beginning with ``"Error:"``.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    result = read_file_lines(
        container, file_path, offset=offset, limit=limit
    )
    return json.dumps(result)


@mcp.tool()
def search_in_container(
    container_id: str,
    pattern: str,
    path: str = "/",
    mode: str = "lexical",
    max_results: int = 50,
) -> str:
    """Search for *pattern* inside the container using ripgrep/ast-grep.

    Returns a JSON array of matches, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``text`` (str): matching line text

    **Lexical** mode (default) uses ripgrep (``rg``) with regex support,
    falling back to ``grep`` if ripgrep is not installed.

    **Structural** mode uses ``ast-grep`` (``sg``) for AST-aware search
    that ignores whitespace/formatting differences.

    Args:
        container_id: 12-character container ID prefix.
        pattern: Search pattern (regex for lexical, AST pattern for structural).
        path: Directory or file path to search within (default ``"/"``).
        mode: ``"lexical"`` (ripgrep → grep) or ``"structural"`` (ast-grep).
        max_results: Maximum results to return (default 50).

    Returns:
        JSON string with a list of match objects, each with ``file``,
        ``line`` (int), ``text`` fields.  On container-not-found returns
        a JSON object with an ``error`` field.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps([{"error": f"Container {container_id[:12]} not found"}])
    except Exception as e:
        return json.dumps([{"error": str(e)}])

    results = search_files(
        client, container_id, pattern, path=path, mode=mode, max_results=max_results
    )
    return json.dumps(results)


@mcp.tool()
def lint_in_container(container_id: str, file_path: str) -> str:
    """Run a linter on *file_path* inside the container.

    Detects the file type from its extension and chooses an appropriate
    linter.  Returns a JSON array of findings, each with:
    - ``file`` (str): file path
    - ``line`` (int): line number
    - ``rule`` (str): rule identifier (e.g. ``"F401"``)
    - ``message`` (str): human-readable message

    Supported:
    - ``.py`` → ``ruff check`` (falls back to ``pylint``)
    - ``.js``, ``.ts``, ``.jsx``, ``.tsx`` → ``eslint``

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.

    Returns:
        JSON string of lint findings, or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            [
                {
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"Container {container_id[:12]} not found",
                }
            ]
        )
    except Exception as e:
        return json.dumps(
            [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]
        )

    results = lint_file(client, container_id, file_path)
    return json.dumps(results)


@mcp.tool()
def type_check_in_container(container_id: str, file_path: str) -> str:
    """Run a type checker on *file_path* inside the container.

    Returns the same format as :func:`lint_in_container`.

    Supported:
    - ``.py`` → ``mypy`` (falls back to ``pyright``)
    - ``.ts``, ``.tsx`` → ``tsc --noEmit``

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.

    Returns:
        JSON string of type check findings, or an error message.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            [
                {
                    "file": file_path,
                    "line": 0,
                    "rule": "error",
                    "message": f"Container {container_id[:12]} not found",
                }
            ]
        )
    except Exception as e:
        return json.dumps(
            [{"file": file_path, "line": 0, "rule": "error", "message": str(e)}]
        )

    results = type_check_file(client, container_id, file_path)
    return json.dumps(results)


@mcp.tool()
def verify_in_container(
    container_id: str,
    path: str,
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
) -> str:
    """Run lint + type_check + test + scan as a bundled verification.

    Executes all four analysis layers inside the container in a single
    call, normalises output to a unified schema, and returns a gate
    decision.

    **Layers:**

    =========== ======= ============================
    Layer       Tool    Notes
    =========== ======= ============================
    lint        ruff    Python lint (``ruff check``)
    type_check  pyright Python type checking
    test        pytest  pytest with json-report
    scan        semgrep Security scanning
    =========== ======= ============================

    **Gate logic:**

    By default the gate fails when any of the following are detected:

    * lint errors (E/F/B/RUF rule codes)
    * test failures
    * semgrep ``ERROR`` findings

    Type-check errors and semgrep ``WARNING`` findings are
    configurable via the ``gate_on_*`` parameters.

    Args:
        container_id: 12-character container ID prefix.
        path: File or directory path inside the container.
        gate_on_lint_error: Whether lint errors fail the gate
            (default ``True``).
        gate_on_type_error: Whether type-check errors fail the gate
            (default ``False``).
        gate_on_test_fail: Whether test failures fail the gate
            (default ``True``).
        gate_on_scan_error: Whether semgrep ERROR findings fail the gate
            (default ``True``).
        gate_on_scan_warning: Whether semgrep WARNING findings fail the gate
            (default ``False``).

    Returns:
        JSON string with:

        * ``status``: ``"ok"`` or ``"failed"``
        * ``gate_passed``: ``True`` if all gate conditions are satisfied
        * ``lint``: list of ``{file, line, rule, severity, message}``
        * ``types``: list of ``{file, line, rule, severity, message}``
        * ``tests``: ``{status, passed, failed, duration, failures?}``
        * ``scan``: list of ``{file, line, rule, severity, message}``
        * ``gate_fail_reasons`` (optional): list of human-readable reasons
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": f"Container {container_id[:12]} not found",
        })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "gate_passed": False,
            "error": str(e),
        })

    result = run_verify(
        client,
        container_id,
        path,
        gate_on_lint_error=gate_on_lint_error,
        gate_on_type_error=gate_on_type_error,
        gate_on_test_fail=gate_on_test_fail,
        gate_on_scan_error=gate_on_scan_error,
        gate_on_scan_warning=gate_on_scan_warning,
    )
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Observability tools (Issue #44)
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_read_journal(
    run_id: str | None = None,
    max_entries: int = 100,
) -> str:
    """Read the append-only execution journal.

    Returns JSON array of journal entries, optionally filtered by
    *run_id*.  The journal records every container lifecycle event
    (initialize, exec, stop) and boundary-crossing operation.

    Args:
        run_id: If provided, only return entries for this run.
            Omit to see all journal entries.
        max_entries: Maximum number of entries to return
            (most recent first, default 100).

    Returns:
        JSON string with a list of journal entry objects, each
        containing ``ts``, ``run_id``, ``container_id``,
        ``operation``, and operation-specific fields.
    """
    entries = read_journal(run_id=run_id, max_entries=max_entries)
    return json.dumps(entries, ensure_ascii=False)


@mcp.tool()
def sandbox_trace(
    run_id: str,
    format: str = "json",
) -> str:
    """Generate a replay trace for a specific run.

    Creates an HTML or JSON trace file from journal entries for
    *run_id*, enabling post-hoc review of "why did it do that?".

    Args:
        run_id: The run identifier to generate a trace for.
        format: Output format - ``"json"`` or ``"html"``
            (default ``"json"``).

    Returns:
        Path to the generated trace file, or an error message
        beginning with ``"Error:"``.
    """
    if format not in ("json", "html"):
        return "Error: format must be 'json' or 'html'"

    if format == "json":
        path = generate_json_trace(run_id)
    else:
        path = generate_html_trace(run_id)

    if not path:
        return f"Error: run_id {run_id} not found in journal"
    return path


@mcp.tool()
def sandbox_list_runs() -> str:
    """List all runs recorded in the execution journal.

    Returns a JSON array of run summaries, each with ``run_id``,
    ``started``, ``image``, ``operations``, ``boundary_crossings``,
    ``status``, and ``last_ts``.

    Returns:
        JSON string with a list of run summary objects.
    """
    runs = get_runs()
    return json.dumps(runs, ensure_ascii=False)


@mcp.tool()
def sandbox_journal_path() -> str:
    """Return the filesystem path to the execution journal file.

    Returns:
        Absolute path to ``~/.code-sandbox-mcp/journal.log``.
    """
    return get_journal_path()


@mcp.tool()
def sandbox_trace_dir() -> str:
    """Return the filesystem path to the trace output directory.

    Returns:
        Absolute path to ``~/.code-sandbox-mcp/traces/``.
    """
    return get_trace_dir()


# ---------------------------------------------------------------------------
# External VCS tools (Issue #55)
# ---------------------------------------------------------------------------


@mcp.tool()
def issue_view(
    container_id: str,
    repo: str,
    issue_number: int,
    save_to: str = "/home/sandbox/issue.md",
) -> str:
    """Read a GitHub issue and save its body to a file inside the container.

    Uses ``gh issue view`` inside the container.  The issue body is
    written to *save_to* and the LLM receives only a summary + handle
    (file path and size).  Full text can be retrieved with
    :func:`read_file_range`.

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True``.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``"owner/repo"`` format.
        issue_number: Issue number to fetch.
        save_to: Path inside the container to save the issue body
            (default ``"/home/sandbox/issue.md"``).

    Returns:
        JSON string with ``number``, ``title``, ``summary`` (up to 100
        characters of body), ``file`` path, and ``size_bytes``.
        On error returns an ``error`` field.
    """
    import base64

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]

    # Fetch issue metadata as JSON (includes number, title, body)
    json_cmd = (
        f"gh issue view {issue_number} --repo {shlex.quote(repo)}"
        f" --json number,title,body"
    )
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", json_cmd],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        return json.dumps({
            "error": f"Failed to fetch issue #{issue_number} from {repo}: {stderr_text or stdout_text}"
        })

    try:
        issue_data = json.loads(stdout_text)
    except json.JSONDecodeError:
        return json.dumps({
            "error": f"Failed to parse issue JSON: {stdout_text[:200]}"
        })

    number = issue_data.get("number", issue_number)
    title = issue_data.get("title", "")
    body = issue_data.get("body", "")

    # Summary: first 100 characters of body
    summary = body[:100] if body else "(empty body)"

    # Write body to file in container via base64
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    dir_part = str(Path(save_to).parent)
    write_cmd = (
        f"mkdir -p {shlex.quote(dir_part)} &&"
        f" echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(save_to)}"
    )
    exit_code2, _ = container.exec_run(
        ["/bin/sh", "-c", write_cmd],
        stdout=True,
        stderr=True,
    )

    if exit_code2 != 0:
        return json.dumps({
            "error": f"Failed to write issue body to {save_to}"
        })

    size_bytes = len(body.encode("utf-8"))

    # Record boundary crossing (read-only, so approved=None)
    record_boundary_crossing(
        cid,
        "issue_view",
        f"repo={repo} issue=#{number} title={title[:60]}",
        approved=None,
    )

    return json.dumps({
        "number": number,
        "title": title,
        "summary": summary,
        "file": save_to,
        "size_bytes": size_bytes,
    })


@mcp.tool()
def submit(
    container_id: str,
    repo: str,
    branch: str,
    message: str,
    working_dir: str = "/home/sandbox",
    create_pr: bool = False,
    pr_title: str = "",
    pr_body: str = "",
    base_branch: str = "",
    dry_run: bool = False,
    token: str = "",
    verify_path: str = ".",
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
) -> str:
    """Stage, commit, push, and optionally create a PR.

    Two-step flow for boundary-crossing writes:

    1. ``dry_run=True`` — returns a diff summary and a confirmation
       token that must be approved before execution.
    2. ``dry_run=False`` + *token* — verifies the token, runs
       ``verify_in_container`` as a gate, then executes
       ``git add -A && git commit -m MESSAGE && git push origin BRANCH``
       (and ``gh pr create`` if *create_pr* is ``True``).

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True``.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``"owner/repo"`` format.
        branch: Branch name to push.
        message: Git commit message.
        working_dir: Directory in the container containing the git
            repository (default ``"/home/sandbox"``).
        create_pr: Whether to create a pull request after push.
        pr_title: PR title (required if ``create_pr=True``).
        pr_body: PR body (optional).
        base_branch: Base branch for the PR (default: repository
            default branch).
        dry_run: When ``True``, returns a diff summary and
            confirmation token instead of executing.
        token: Confirmation token from a previous ``dry_run`` call.
        verify_path: Path inside *working_dir* to run verification on
            (default ``"."``).
        gate_on_lint_error: Whether lint errors fail the verify gate.
        gate_on_type_error: Whether type-check errors fail the verify
            gate.
        gate_on_test_fail: Whether test failures fail the verify gate.
        gate_on_scan_error: Whether semgrep ERROR findings fail the
            verify gate.
        gate_on_scan_warning: Whether semgrep WARNING findings fail the
            verify gate.

    Returns:
        JSON string with operation result.
    """
    import base64

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]
    run_id = get_or_create_run_id(cid)

    # Helper: run a shell command in the container in working_dir.
    def _run(cmd: str) -> tuple[int, str, str]:
        full_cmd = f"cd {shlex.quote(working_dir)} && {cmd}"
        ec, out = container.exec_run(
            ["/bin/sh", "-c", full_cmd],
            stdout=True,
            stderr=True,
        )
        out_stdout, out_stderr = (
            out if isinstance(out, tuple) else (out, b"")
        )
        stdout_text = (
            out_stdout.decode("utf-8", errors="replace") if out_stdout else ""
        )
        stderr_text = (
            out_stderr.decode("utf-8", errors="replace") if out_stderr else ""
        )
        return ec, stdout_text, stderr_text

    # ------------------------------------------------------------------
    # DRY RUN — show plan and generate token
    # ------------------------------------------------------------------
    if dry_run:
        # Gather diff summary
        status_ec, status_out, status_err = _run(
            "git status --porcelain && echo '---DIFF---' && git diff HEAD --stat"
        )
        diff_summary = (status_out + "\n" + status_err).strip()

        if not diff_summary or diff_summary == "---DIFF---":
            return json.dumps({
                "status": "dry_run",
                "diff_summary": "(no changes detected)",
                "branch": branch,
                "message": message,
                "warning": "No changes to commit.  Submit will succeed as a no-op.",
            })

        details = (
            f"repo={repo} branch={branch} message={message[:80]}"
        )
        if create_pr:
            details += f" pr_title={pr_title[:60]}"

        conf_token = generate_token(
            operation="submit",
            details=details,
            container_id=cid,
            run_id=run_id,
        )

        # Record pending boundary crossing
        record_boundary_crossing(
            cid,
            "submit",
            details,
            approved=None,
            token=conf_token,
        )

        return json.dumps({
            "status": "dry_run",
            "diff_summary": diff_summary,
            "branch": branch,
            "message": message,
            "confirmation_token": conf_token,
            "create_pr": create_pr,
            "pr_title": pr_title if create_pr else None,
        })

    # ------------------------------------------------------------------
    # EXECUTE — require token + verify gate
    # ------------------------------------------------------------------
    if not token:
        return json.dumps({
            "status": "error",
            "error": "Token required for execution.  Run with dry_run=True first.",
        })

    token_result = verify_and_consume(token)
    if token_result is None:
        return json.dumps({
            "status": "error",
            "error": "Token invalid, expired, or already used",
        })

    # --- Verify gate ---
    if os.path.isabs(verify_path):
        verify_path_full = verify_path
    else:
        verify_path_full = f"{working_dir}/{verify_path}".rstrip("/")
    verify_result = run_verify(
        client,
        cid,
        verify_path_full,
        gate_on_lint_error=gate_on_lint_error,
        gate_on_type_error=gate_on_type_error,
        gate_on_test_fail=gate_on_test_fail,
        gate_on_scan_error=gate_on_scan_error,
        gate_on_scan_warning=gate_on_scan_warning,
    )

    if not verify_result.get("gate_passed", False):
        record_boundary_crossing(
            cid,
            "submit",
            f"repo={repo} branch={branch} verify_failed",
            approved=False,
            token=token,
        )
        return json.dumps({
            "status": "rejected",
            "reason": "verify_gate_failed",
            "verify_result": verify_result,
        })

    # --- Git add / commit ---
    add_ec, add_out, add_err = _run("git add -A")
    if add_ec != 0:
        return json.dumps({
            "status": "error",
            "step": "git_add",
            "error": add_err or add_out,
        })

    commit_ec, commit_out, commit_err = _run(
        f"git commit -m {shlex.quote(message)}"
    )
    if commit_ec != 0:
        # No changes to commit is OK if everything is already committed
        if "nothing to commit" in (commit_out + commit_err).lower():
            pass
        else:
            return json.dumps({
                "status": "error",
                "step": "git_commit",
                "error": commit_err or commit_out,
            })

    # --- Git push ---
    push_cmd = (
        f"git -c credential.helper= "
        f"-c credential.helper='!f() {{ echo username=x-access-token; echo password=$GITHUB_TOKEN; }}; f' "
        f"push origin {shlex.quote(branch)}"
    )
    push_ec, push_out, push_err = _run(push_cmd)

    # Get the SHA of the pushed commit
    sha = ""
    sha_ec, sha_out, _ = _run("git rev-parse HEAD")
    if sha_ec == 0:
        sha = sha_out.strip()[:7]

    if push_ec != 0:
        record_boundary_crossing(
            cid,
            "submit",
            f"repo={repo} branch={branch} push_failed",
            approved=False,
            token=token,
        )
        return json.dumps({
            "status": "error",
            "step": "git_push",
            "error": push_err or push_out,
            "sha": sha,
            "verify_result": verify_result,
        })

    # --- Optionally create PR ---
    pr_url: str | None = None
    if create_pr:
        pr_cmd = (
            f"gh pr create --repo {shlex.quote(repo)}"
            f" --head {shlex.quote(branch)}"
            f" --title {shlex.quote(pr_title)}"
        )
        if pr_body:
            body_encoded = base64.b64encode(
                pr_body.encode("utf-8")
            ).decode("ascii")
            pr_cmd = (
                f"BODY_FILE=$(mktemp) &&"
                f" echo {shlex.quote(body_encoded)} | base64 -d > \"$BODY_FILE\" &&"
                f" gh pr create --repo {shlex.quote(repo)}"
                f" --head {shlex.quote(branch)}"
                f" --title {shlex.quote(pr_title)}"
                f" --body-file \"$BODY_FILE\""
                f"; rm -f \"$BODY_FILE\""
            )
        else:
            pr_cmd += " --body ''"
        if base_branch:
            pr_cmd += f" --base {shlex.quote(base_branch)}"

        pr_ec, pr_out, pr_err = _run(pr_cmd)
        if pr_ec != 0:
            # Push succeeded but PR creation failed — still record push
            record_boundary_crossing(
                cid,
                "submit",
                f"repo={repo} branch={branch} sha={sha} pr_create_failed",
                approved=True,
                token=token,
            )
            return json.dumps({
                "status": "pushed",
                "branch": branch,
                "sha": sha,
                "pr_create_error": pr_err or pr_out,
                "verify_result": verify_result,
            })

        # Extract PR URL from gh output
        for line in (pr_out + pr_err).splitlines():
            line = line.strip()
            if line.startswith("https://github.com/"):
                pr_url = line
                break

    # --- Success ---
    details = f"repo={repo} branch={branch} sha={sha}"
    if pr_url:
        details += f" pr_url={pr_url}"

    record_boundary_crossing(
        cid,
        "submit",
        details,
        approved=True,
        token=token,
    )

    result: dict[str, Any] = {
        "status": "pushed",
        "branch": branch,
        "sha": sha,
        "verify_result": verify_result,
    }
    if pr_url:
        result["pr_url"] = pr_url

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Repository exploration tools (Issue #86)
# ---------------------------------------------------------------------------

_REPO_FORMAT_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


@mcp.tool()
def clone_repo(
    container_id: str,
    repo: str,
    dest_dir: str = "/home/sandbox",
    branch: str = "",
) -> str:
    """Clone a Git repository inside the container using ``gh repo clone``.

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True`` for private repositories.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``"owner/repo"`` format.
        dest_dir: Destination directory in the container
            (default ``"/home/sandbox"``).
        branch: Branch name to clone. Omit for the default branch.

    Returns:
        JSON string with ``status``, ``repo``, ``clone_path``, and
        ``branch``.  On error returns an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]

    if not _REPO_FORMAT_RE.match(repo):
        return json.dumps(
            {"error": f"Invalid repo format: {repo} (expected owner/repo)"}
        )

    safe_dest = shlex.quote(dest_dir)
    safe_repo = shlex.quote(repo)

    if branch:
        cmd = (
            f"gh repo clone {safe_repo} {safe_dest}"
            f" -- -b {shlex.quote(branch)}"
        )
    else:
        cmd = f"gh repo clone {safe_repo} {safe_dest}"

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )

    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    repo_name = repo.split("/")[-1]
    clone_path = f"{dest_dir}/{repo_name}"

    if exit_code != 0:
        return json.dumps({
            "status": "error",
            "error": stderr_text or stdout_text,
            "clone_path": clone_path,
        })

    record_boundary_crossing(
        cid,
        "clone_repo",
        f"repo={repo} branch={branch or 'default'} dest={clone_path}",
        approved=True,
    )

    return json.dumps({
        "status": "ok",
        "repo": repo,
        "clone_path": clone_path,
        "branch": branch or "default",
    })


@mcp.tool()
def list_files(
    container_id: str,
    path: str = "/home/sandbox",
    max_depth: int = 3,
    pattern: str = "",
) -> str:
    """List files inside the container using ``find``.

    Returns a JSON array of file paths sorted alphabetically.
    Hidden files (dotfiles) and directories under ``.git`` are
    excluded.

    Args:
        container_id: 12-character container ID prefix.
        path: Directory path to list (default ``"/home/sandbox"``).
        max_depth: Maximum directory depth (default 3).
        pattern: Optional glob pattern to filter files
            (e.g. ``"*.py"``, ``"*.md"``).

    Returns:
        JSON string with ``path``, ``total``, and ``files`` list.
        On error returns an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    safe_path = shlex.quote(path)

    name_filter = ""
    if pattern:
        name_filter = f" -name {shlex.quote(pattern)}"

    cmd = (
        f"find {safe_path} -maxdepth {max_depth}"
        f" -not -path '*/\\.*'"
        f" -type f{name_filter}"
        f" | sort"
    )

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )

    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        return json.dumps({"error": stderr_text or stdout_text})

    files = [f for f in stdout_text.strip().split("\n") if f]

    return json.dumps({
        "path": path,
        "total": len(files),
        "files": files,
    })


# ---------------------------------------------------------------------------
# Approval / Token tools (Issue #50)
# ---------------------------------------------------------------------------


@mcp.tool()
def sandbox_approval_status() -> str:
    """List all pending approval tokens for boundary-crossing operations.

    Returns a JSON array of pending tokens, each with ``token``,
    ``operation``, ``details``, ``container_id``, ``run_id``,
    and ``remaining_seconds``.

    Use :func:`sandbox_approve` or :func:`sandbox_reject` to resolve
    a pending token.  Tokens expire after a configurable TTL (default
    5 minutes).

    Returns:
        JSON string with a list of pending token objects.
    """
    pending = get_pending_tokens()
    # created_at と now は同一クロック (time.monotonic()) なので
    # スリープ/サスペンドの影響を受けず正確な残り時間が計算できる。
    now = time.monotonic()
    for p in pending:
        p["remaining_seconds"] = max(
            0,
            int(p["ttl_seconds"] - (now - p["created_at"])),
        )
        del p["created_at"]
        del p["ttl_seconds"]
    return json.dumps(pending, ensure_ascii=False)


@mcp.tool()
def sandbox_approve(token: str) -> str:
    """Approve a pending boundary-crossing operation.

    Verifies the token and records approval in the execution journal.
    Once approved, the operation that requested the token can proceed.

    Args:
        token: The confirmation token string (from dry_run output,
            ``sandbox_approval_status``, or the dashboard).

    Returns:
        JSON string with ``status`` and metadata, or error details.
    """
    result = verify_and_consume(token)
    if result is None:
        return json.dumps(
            {
                "status": "error",
                "error": "Token invalid, expired, or already used",
            }
        )
    record_boundary_crossing(
        result["container_id"],
        result["operation"],
        result["details"],
        approved=True,
        token=token,
    )
    return json.dumps(
        {
            "status": "ok",
            "operation": result["operation"],
            "details": result["details"],
            "container_id": result["container_id"],
            "run_id": result["run_id"],
        }
    )


@mcp.tool()
def sandbox_reject(token: str) -> str:
    """Reject a pending boundary-crossing operation.

    Removes the token from the pending queue.  The operation that
    requested the token will not be able to proceed without a new
    token.

    Args:
        token: The confirmation token string to reject.

    Returns:
        JSON string with ``status`` and message.
    """
    ok = reject_token(token)
    if not ok:
        return json.dumps(
            {
                "status": "error",
                "error": "Token not found or already resolved",
            }
        )
    return json.dumps(
        {
            "status": "ok",
            "message": "Token rejected",
        }
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the MCP server.

    Exported separately so tests can exercise the parser without
    starting the server.
    """
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
    parser.add_argument(
        "--shiori-repos-path",
        type=str,
        default=os.environ.get("SHIORI_REPOS_PATH"),
        help=(
            "Host path to Shiori repos root (e.g. /data/repos). "
            "When set, sandbox_initialize and run_container_and_exec "
            "can use clone_repo to copy a pre-cloned repo into the "
            "container instead of a network git clone. "
            "Also read from SHIORI_REPOS_PATH env var."
        ),
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
        default=8765,
        help="Port for HTTP transport (default: 8765)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=0,
        help=(
            "Start the observability web dashboard on localhost "
            "(default: 0 = disabled).  Suggested: 8766."
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
    return parser


def main() -> None:
    """Parse CLI arguments and run the MCP server.

    Supports ``--terminal`` for update progress windows,
    ``--default-image`` for overriding the default Docker image,
    ``--transport`` to select the MCP transport protocol,
    ``--dashboard-port`` for the observability dashboard,
    and ``--webhook-url`` for push notifications.

    HTTP-based transports (``sse``, ``http``, ``streamable-http``)
    are not subject to the ~60-second client timeout that affects
    ``stdio``, making them suitable for long-running Docker
    operations such as ``docker pull`` or ``copy_project`` on
    large directories.
    """
    parser = _build_arg_parser()
    args = parser.parse_args()

    global _TERMINAL, _UPDATE_SPEC, _UPDATE_LOG_DIR, _DEFAULT_IMAGE, _SHIORI_REPOS_PATH
    _TERMINAL = args.terminal
    _UPDATE_SPEC = args.update_spec
    if args.update_log_dir:
        _UPDATE_LOG_DIR = Path(args.update_log_dir)
    if args.default_image:
        validate_image_ref(args.default_image)
        _DEFAULT_IMAGE = args.default_image
    if args.shiori_repos_path:
        _SHIORI_REPOS_PATH = args.shiori_repos_path

    # Configure notifications if webhook is set
    if args.webhook_url or args.failure_threshold != 5 or args.long_run_seconds != 300:
        from code_sandbox_mcp.notify import configure

        configure(
            webhook_url=args.webhook_url,
            failure_threshold=args.failure_threshold,
            long_run_seconds=args.long_run_seconds,
        )

    # Start dashboard if requested
    if args.dashboard_port > 0:
        from code_sandbox_mcp.dashboard import start_dashboard

        msg = start_dashboard(port=args.dashboard_port)
        logger.info(msg)

    transport = args.transport
    if transport == "stdio":
        mcp.run(transport=transport)
    else:
        mcp.run(transport=transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
