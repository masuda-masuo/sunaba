"""FastMCP server providing Docker sandbox tools - MCP server implementation.

This module defines the FastMCP server and all tool handlers.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import io
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

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
    transform_file_in_container,
    type_check_file,
    write_file,
)
from code_sandbox_mcp.journal import (
    get_journal_path,
    get_or_create_run_id,
    get_runs,
    read_journal,
    record_boundary_crossing,
    record_copy,
    record_initialize,
    record_stop,
    record_test_environment,
)
from code_sandbox_mcp.journal import (
    record_exec as journal_record_exec,
)
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
    get_cache_stats,
    get_cached_result,
    invalidate_cache,
    set_cached_result,
)
from code_sandbox_mcp.security import (
    DEFAULT_SECURITY_PROFILE,
    build_secure_run_kwargs,
    validate_image_ref,
)
from code_sandbox_mcp.token import (
    generate_token,
    get_pending_tokens,
    reject_token,
    verify_and_consume,
    verify_token,
)
from code_sandbox_mcp.tools.common import _docker
from code_sandbox_mcp.trace import (
    generate_html_trace,
    generate_json_trace,
    get_trace_dir,
)

from .tools.exec import (
    sandbox_exec,
    sandbox_exec_background,
    sandbox_exec_check,
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
_DEFAULT_IMAGE: str = "ghcr.io/masuda-masuo/code-sandbox-mcp/sandbox@sha256:1bc3a1d3bba23e7f38cb511269efdbf0bca03497ee483a0ba25d7e308b34ec09"

#: Stdio proxy - shared with launcher via this module variable.
_TERMINAL: str | None = None
_UPDATE_SPEC: str = str(Path(__file__).resolve().parent.parent.parent)
_UPDATE_LOG_DIR: Path | None = None
_CURRENT_UPDATE_LOG_PATH: str | None = None
_UPDATE_LOCK: threading.Lock = threading.Lock()
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


def _clone_repo_via_network(
    container,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
    inject_vcs_token: bool = False,
) -> str:
    """Fallback: clone via ``gh repo clone`` when Shiori is unavailable (Issue #146).

    Requires network access and ``gh`` installed in the container.

    Note on private repositories (Issue #146, PR #170 review):
        Network access is auto-enabled for ``clone_repo``, but the VCS
        token is *not* auto-injected.  Public repos (the common case)
        clone fine without credentials, and injecting the token
        unconditionally would expose it to in-container code for no
        benefit, violating the host-permission-minimization principle.
        Private repos therefore require the caller to opt in with
        ``inject_vcs_token=True``; when the clone fails without a token
        the raised error suggests doing so.
    """
    # Not redundant with the callers: this fallback runs only when
    # _SHIORI_REPOS_PATH is unset, and _clone_shiori_repo_to_container
    # raises on that *before* it calls _validate_clone_repo.  So this is
    # the only validation on the network-fallback path (PR #170 review).
    _validate_clone_repo(clone_repo)
    repo_name = clone_repo.split("/")[-1]
    clone_path = clone_dest.rstrip("/") + "/" + repo_name
    cmd = "gh repo clone " + shlex.quote(clone_repo) + " " + shlex.quote(clone_path)
    exit_code, output = container.exec_run(["/bin/sh", "-c", cmd])
    if exit_code != 0:
        detail = output.decode("utf-8", errors="replace").strip()
        hint = ""
        if not inject_vcs_token:
            hint = (
                " (if this is a private repository, retry with"
                " inject_vcs_token=True so gh can authenticate)"
            )
        raise RuntimeError(
            f"gh repo clone failed (exit {exit_code}): {detail}{hint}"
        )
    return "Cloned {} via network into {} in container {}".format(
        clone_repo, clone_path, container_id[:12]
    )


class CloneResult(NamedTuple):
    """Return value of :func:`_try_clone_into_container`."""

    msg: str | None
    error: str | None


def _try_clone_into_container(
    container: Any,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
    inject_vcs_token: bool = False,
) -> CloneResult:
    """Attempt to clone a repo into the container; return (msg, error).

    Runs the full chain: Shiori fast-path -> network fallback when
    ``_SHIORI_REPOS_PATH`` is unset -> non-fatal error capture.

    Returns:
        ``(clone_msg, None)`` on success, ``(None, error_str)`` on failure.
    """
    try:
        msg = _clone_shiori_repo_to_container(
            container, container_id, clone_repo, clone_dest,
        )
        return CloneResult(msg, None)
    except ValueError as e:
        if not _SHIORI_REPOS_PATH:
            try:
                msg = _clone_repo_via_network(
                    container, container_id, clone_repo, clone_dest,
                    inject_vcs_token,
                )
                return CloneResult(msg, None)
            except Exception as e2:
                logger.warning("Network clone fallback failed: %s", e2)
                return CloneResult(None, str(e2))
        else:
            logger.warning("Shiori clone copy failed: %s", e)
            return CloneResult(None, str(e))
    except Exception as e:
        logger.warning("Shiori clone copy failed: %s", e)
        return CloneResult(None, str(e))


# ---------------------------------------------------------------------------
# PR branch setup helper (Issue #136)
# ---------------------------------------------------------------------------


def _setup_pr_branch(
    container: Any,
    container_id: str,
    repo: str,
    pr_number: int,
    clone_dest: str,
    pip_extras: str | None = "[dev]",
) -> str:
    """Clone repo and check out PR branch inside the container.

    Uses ``gh`` (authenticated via injected VCS token) to fetch PR info,
    clone the repository, check out the PR's head branch, and install
    dev dependencies.

    Args:
        container: Docker container object.
        container_id: 12-char container ID prefix.
        repo: Repository in ``"owner/name"`` format.
        pr_number: Pull request number.
        clone_dest: Destination directory inside the container.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
            Pass ``None`` to skip pip install entirely.

    Returns:
        Success message string.
    """
    cid = container_id[:12]
    safe_dest = shlex.quote(f"{clone_dest}/repo")
    safe_repo = shlex.quote(repo)

    # Step 1: Get PR head branch info
    gh_info_cmd = (
        f"gh pr view {pr_number} --repo {safe_repo}"
        f" --json headRefName"
    )
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", gh_info_cmd],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output or (b"", b"")
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise RuntimeError(
            f"Failed to fetch PR #{pr_number} from {repo}: {stderr_text or stdout_text}"
        )

    try:
        pr_info = json.loads(stdout_text)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Failed to parse PR info JSON: {stdout_text[:200]}"
        )

    head_ref = pr_info.get("headRefName", "")
    if not head_ref:
        raise RuntimeError(
            f"Incomplete PR info: head_ref={head_ref!r}"
        )

    # Step 2: Clone the base repo
    # Clone the base repo (not head/fork) so that gh pr checkout
    # works correctly with the PR number from the base repository.
    clone_cmd = (
        f"gh repo clone {safe_repo}"
        f" {safe_dest}"
    )
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", clone_cmd],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output or (b"", b"")
    clone_output = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise RuntimeError(
            f"Failed to clone repo {repo}: {stderr_text or clone_output}"
        )

    logger.info("Cloned %s → %s in container %s", safe_repo, safe_dest, cid)

    # Step 3: Checkout PR branch
    checkout_cmd = f"cd {safe_dest} && gh pr checkout {pr_number}"
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", checkout_cmd],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, stderr_part = output or (b"", b"")
    co_output = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code != 0:
        raise RuntimeError(
            f"Failed to checkout PR #{pr_number}: {stderr_text or co_output}"
        )

    logger.info(
        "Checked out PR #%s (%s) → %s in container %s",
        pr_number, head_ref, safe_dest, cid,
    )

    # Step 4: Install dev dependencies (non-fatal)
    if pip_extras is not None:
        install_cmd = f"cd {safe_dest} && pip install -e '{pip_extras}' -q"
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", install_cmd],
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout_part, stderr_part = output or (b"", b"")
        install_output = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

        if exit_code != 0:
            logger.warning(
                "pip install deps failed (extras=%s, exit=%d): %s",
                pip_extras, exit_code, (stderr_text or install_output).strip(),
            )

    record_copy(
        cid,
        "setup_pr_branch",
        f"repo={repo} pr=#{pr_number} branch={head_ref}",
        safe_dest,
    )

    return (
        f"PR #{pr_number} ({head_ref}) → {clone_dest}/repo "
        f"in container {cid}"
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
    repo: str | None = None,
    pr: int | None = None,
    pip_extras: str | None = "[dev]",
) -> str:
    """Start a new Docker sandbox container.

    The container runs ``sleep infinity`` and stays alive until
    explicitly stopped with :func:`sandbox_stop`.

    Container IDs are returned as short 12-character prefixes for
    use in other tools.

    **One-step init + clone:** pass ``clone_repo`` to avoid a separate
    :func:`clone_repo` call.  For a full one-shot workflow with commands,
    use :func:`run_container_and_exec` which wraps init/exec/stop.

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
               (default: ``None`` = no clone copy).  When Shiori is not
               configured, falls back to ``gh repo clone`` over the
               network (``allow_network`` is auto-enabled).  Cloning a
               *private* repo this way additionally requires
               ``inject_vcs_token=True`` so ``gh`` can authenticate;
               the token is not auto-injected because public repos (the
               common case) do not need it.
        clone_dest: Destination directory in the container for the
               cloned repository (default: ``/tmp/repo``).
               The actual path will be ``{clone_dest}/repo``.
        repo: Repository in ``"owner/name"`` format.
               Required when *pr* is specified.
        pr: Pull request number to clone and check out.
               When set, implicitly enables ``allow_network=True``
               and ``inject_vcs_token=True``, clones the repository
               inside the container, checks out the PR head branch,
               and installs dev dependencies.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
               Pass ``None`` to skip pip install entirely.
               Only used when *pr* is specified.

    The image must be pulled locally before use: docker pull <image>

    Returns:
        Container ID string (12-character prefix).
        If *clone_repo* is specified, a message about the clone copy
        is appended.
        If *pr* is specified, a message about the PR branch setup
        is appended.

    See also:
        :func:`run_container_and_exec` — one-shot init + exec + stop.
        :func:`clone_repo` — clone after container is running.
    """
    # When pr is specified, implicitly enable network and VCS token
    if pr is not None:
        allow_network = True
        inject_vcs_token = True

    # Auto-enable network when Shiori is not configured (Issue #146)
    if clone_repo and pr is None and not _SHIORI_REPOS_PATH:
        allow_network = True
        logger.info(
            "clone_repo=%r: Shiori path not configured, auto-enabling network access",
            clone_repo,
        )

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

    # -- Clone: Shiori fast-path, network fallback (Issue #84, #146) --
    # When pr is set, _setup_pr_branch handles its own clone,
    # so skip the Shiori clone copy to avoid redundant clone.
    clone_msg = ""
    if clone_repo and pr is None:
        msg, err = _try_clone_into_container(
            container, cid, clone_repo, clone_dest, inject_vcs_token,
        )
        if err is not None:
            clone_msg = f" (clone_repo failed: {err})"
        else:
            clone_msg = " " + msg
    elif clone_repo and pr is not None:
        logger.info(
            "Skipping clone_repo=%s (pr=%s handles its own clone)",
            clone_repo, pr,
        )

    # -- PR branch setup (Issue #136) --
    pr_msg = ""
    if pr is not None:
        if not repo:
            logger.warning(
                "pr parameter requires repo, got repo=None"
            )
            pr_msg = " (pr setup failed: repo is required when pr is specified)"
        else:
            try:
                pr_msg = " " + _setup_pr_branch(
                    container, cid, repo, pr, clone_dest, pip_extras,
                )
            except Exception as e:
                # PR setup failure is non-fatal: the container is still usable.
                logger.warning("PR branch setup failed: %s", e)
                pr_msg = f" (pr setup failed: {e})"

    return cid + clone_msg + pr_msg



sandbox_exec = mcp.tool()(sandbox_exec)
sandbox_exec_background = mcp.tool()(sandbox_exec_background)
sandbox_exec_check = mcp.tool()(sandbox_exec_check)

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

    **Mode selection (pick exactly one):**

    ================= ===================================================
    Mode              Parameters
    ================= ===================================================
    Full overwrite    (none of the below) — writes *file_contents* as-is
    Line-range        ``start_line`` [+ ``end_line``] — replace lines
    Append            ``append=True`` — append to existing file
    String replace    ``old_str`` — replace exact text (see matching below)
    ================= ===================================================

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

    .. hint::

       ``old_str`` mode is the default edit path for AI — it is robust
       (uniqueness check + whitespace-flexible fallback) and avoids the
       ``@@`` header errors that make hand-written diffs fail.  Use
       :func:`read_file_range` first to inspect the target area before
       editing.  For bulk / repetitive / structural / computed changes use
       :func:`transform_file` (imperative).  Reserve :func:`apply_patch` for
       *machine-generated* diffs.

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

    See also:
        :func:`read_file_range` — inspect file content before editing.
        :func:`transform_file` — imperative edits (bulk / structural / computed).
        :func:`apply_patch` — machine-generated diffs only (deprecated for
        AI-authored edits).
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    dest_path = os.path.join(dest_dir, file_name)

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
        except ValueError:
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

    .. hint::

       For Git repositories already cloned locally, prefer
       :func:`sandbox_initialize` with ``clone_repo`` — it copies
       a pre-cloned repo without network overhead.

    Args:
        container_id: 12-character container ID prefix.
        local_src_dir: Path to the local directory to copy.
        dest_dir: Destination directory in the container (default:
            ``/home/sandbox``).

    Returns:
        Success or error message.

    See also:
        :func:`clone_repo` — clone a remote Git repo inside the container.
        :func:`copy_file` — copy a single file instead of a directory.
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

    # Prevent concurrent updates (PR #130)
    global _CURRENT_UPDATE_LOG_PATH
    with _UPDATE_LOCK:
        if _CURRENT_UPDATE_LOG_PATH is not None:
            return "Error: an update is already in progress"

    # Create a unique log directory for this update
    log_dir: Path
    if _UPDATE_LOG_DIR:
        log_dir = _UPDATE_LOG_DIR
    else:
        base = Path(tempfile.gettempdir()) / "code-sandbox-mcp-updates"
        base.mkdir(parents=True, exist_ok=True)
        log_dir = Path(tempfile.mkdtemp(dir=base))

    log_path = log_dir / "update.log"
    with _UPDATE_LOCK:
        _CURRENT_UPDATE_LOG_PATH = str(log_path)

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
        log_f.write(f"=== Update started (spec: {_UPDATE_SPEC}) @ {datetime.now().isoformat()} ===\n")
        log_f.flush()

        proc = subprocess.Popen(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", _UPDATE_SPEC],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
        )
        proc.wait()

        if proc.returncode == 0:
            log_f.write("=== Update succeeded ===\n")
            log_f.flush()
            logger.info("Update succeeded, restarting...")
            os._exit(RESTART_EXIT_CODE)
        else:
            log_f.write(f"=== Update failed (exit code: {proc.returncode}) ===\n")
            log_f.flush()
            logger.error("Update failed with exit code %d", proc.returncode)
    global _CURRENT_UPDATE_LOG_PATH
    with _UPDATE_LOCK:
        if _CURRENT_UPDATE_LOG_PATH == log_path:
            _CURRENT_UPDATE_LOG_PATH = None


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
            f"Get-Content -Wait {shlex.quote(log_path)}",
        ]
    elif sys.platform == "darwin":
        quoted = shlex.quote(log_path)
        cmd = [
            "osascript",
            "-e",
            f'tell application "Terminal" to do script "tail -f {quoted}"',
        ]
    else:
        if shutil.which("xterm"):
            cmd = ["xterm", "-e", f"tail -f {shlex.quote(log_path)}"]
        else:
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
    with _UPDATE_LOCK:
        log_path_str = _CURRENT_UPDATE_LOG_PATH
    if not log_path_str:
        return "Error: no update job found"

    log_path = Path(log_path_str)
    if not log_path.exists():
        return "Error: update log not found"

    try:
        log_text = log_path.read_text()
    except OSError as e:
        return f"Error: cannot read update log: {e}"

    if not log_text:
        return "Status: running (elapsed: 0s)"

    # Parse start timestamp from the log header
    start_ts = None
    for line in log_text.splitlines():
        if "@ " in line and "=== Update started" in line:
            try:
                ts_str = line.split("@ ")[1].rstrip(" =")
                start_ts = datetime.fromisoformat(ts_str)
            except (ValueError, IndexError):
                pass
            break

    if start_ts is None:
        return "Error: no update start marker found in log"

    elapsed = datetime.now() - start_ts
    total_seconds = int(elapsed.total_seconds())
    elapsed_str = f"{total_seconds}s"

    if "=== Update succeeded ===" in log_text:
        return f"Status: done (elapsed: {elapsed_str})"
    elif "=== Update failed" in log_text:
        for line in reversed(log_text.splitlines()):
            if "=== Update failed" in line:
                return f"Status: error (elapsed: {elapsed_str})\nError: {line.strip()}"
        return f"Status: error (elapsed: {elapsed_str})"
    else:
        return f"Status: running (elapsed: {elapsed_str})"


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
    repo: str | None = None,
    pr: int | None = None,
    pip_extras: str | None = "[dev]",
    timeout: int = 0,
    max_output_tokens: int = 0,
    input_hash: str = "",
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
               (default: ``None`` = no clone copy).  When Shiori is not
               configured, falls back to ``gh repo clone`` over the
               network (``allow_network`` is auto-enabled).  Cloning a
               *private* repo this way additionally requires
               ``inject_vcs_token=True`` so ``gh`` can authenticate;
               the token is not auto-injected because public repos (the
               common case) do not need it.
        clone_dest: Destination directory in the container for the
               cloned repository (default: ``/tmp/repo``).
               The actual path will be ``{clone_dest}/repo``.
        repo: Repository in ``"owner/name"`` format.
               Required when *pr* is specified.
        pr: Pull request number to clone and check out.
               When set, implicitly enables ``allow_network=True``
               and ``inject_vcs_token=True``, clones the repository
               inside the container, checks out the PR head branch,
               and installs dev dependencies.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
               Pass ``None`` to skip pip install entirely.
               Only used when *pr* is specified.
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
    if timeout < 0:
        return json.dumps({"status": "error", "error": "timeout must be >= 0"})

    # When pr is specified, implicitly enable network and VCS token
    if pr is not None:
        allow_network = True
        inject_vcs_token = True

    # Auto-enable network when Shiori is not configured (Issue #146)
    if clone_repo and pr is None and not _SHIORI_REPOS_PATH:
        allow_network = True
        logger.info(
            "clone_repo=%r: Shiori path not configured, auto-enabling network access",
            clone_repo,
        )

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

    # --- Clone: Shiori fast-path, network fallback (Issue #84, #146) ---
    # When pr is set, _setup_pr_branch handles its own clone,
    # so skip the Shiori clone copy to avoid redundant clone.
    clone_error: str | None = None
    if clone_repo and pr is None:
        _, clone_error = _try_clone_into_container(
            container, container_id, clone_repo, clone_dest, inject_vcs_token,
        )
    elif clone_repo and pr is not None:
        logger.info(
            "Skipping clone_repo=%s (pr=%s handles its own clone)",
            clone_repo, pr,
        )

    # --- PR branch setup (Issue #136) ---
    pr_error: str | None = None
    if pr is not None:
        if not repo:
            logger.warning(
                "pr parameter requires repo, got repo=None"
            )
            pr_error = "repo is required when pr is specified"
        else:
            try:
                _setup_pr_branch(
                    container, container_id, repo, pr, clone_dest, pip_extras,
                )
            except Exception as e:
                logger.warning("PR branch setup failed: %s", e)
                pr_error = str(e)

    # --- Execute commands ---
    try:
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

    raw_size = len(raw_output.encode("utf-8"))

    # Sanitize: ANSI, \r, timestamps
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
        "status": "ok" if exit_code == 0 else ("timeout" if timeout > 0 and exit_code == 124 else "error"),
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
    if clone_error:
        result["clone_warning"] = clone_error
    if pr_error:
        result["pr_warning"] = pr_error

    # Cache the result
    cache_key = compute_cache_key(image, commands, input_hash=input_hash)
    set_cached_result(cache_key, result)

    journal_record_exec(
        container_id,
        commands,
        exit_code,
        verbose=verbose,
        cached=False,
        output_size=raw_size,
        max_output_tokens=max_output_tokens if max_output_tokens > 0 else None,
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
# Token reduction / result cache tools (§3)
# ---------------------------------------------------------------------------


@mcp.tool()
def rerun_failed(
    container_id: str,
    run_id: str,
    commands: list[str] | None = None,
    verbose: str = "summary",
    max_lines: int = 100,
    offset: int = 0,
    limit: int = 50,
    timeout: int = 0,
    input_hash: str = "",
) -> str:
    """Re-run commands from a previous run, returning only the diff.

    Retrieves the journal entries for *run_id*, finds the exec commands
    that had non-zero exit codes, and re-runs only those. Returns the
    diff between the original failed output and the new result.

    Args:
        container_id: 12-character container ID prefix.
        run_id: The run_id to re-run failed commands from.
        commands: Optional override — only re-run these specific commands.
        verbose: Output verbosity.
        max_lines: Maximum lines to show.
        offset: Line offset for paging.
        limit: Maximum lines per page.
        timeout: Maximum seconds to let each command run.

    Returns:
        JSON string with diff of changed results.
    """
    entries = read_journal(run_id=run_id)
    if not entries:
        return json.dumps({"status": "error", "error": f"run_id {run_id} not found"})

    # Filter to exec entries with non-zero exit codes
    failed: list[dict[str, Any]] = [
        e for e in entries
        if e.get("operation") == "exec" and e.get("exit_code", 0) != 0
    ]
    if not failed:
        return json.dumps({"status": "ok", "message": "No failed commands to re-run", "diff": ""})

    # Determine which commands to re-run
    target_commands = commands if commands is not None else failed[-1].get("commands", [])

    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    # Validate commands
    if not target_commands:
        return json.dumps({"status": "error", "error": "no commands to re-run"})

    # Execute the commands
    joined = " && ".join(target_commands)
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
    new_exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True, stderr=True, demux=True,
    )
    stdout_part, stderr_part = output
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    # Build new result
    if new_exit_code == 0:
        raw_output = stdout_text
    else:
        raw_output = stdout_text + "\n" + stderr_text if stdout_text and stderr_text else (stdout_text or stderr_text)

    clean = sanitize_output(raw_output)
    compressed = compress_repeated_lines(clean)
    if new_exit_code != 0:
        compressed = compress_failures(compressed)
    display, meta = truncate_output(
        compressed, max_lines=max_lines, verbose=verbose,
        exit_code=new_exit_code, stderr=stderr_text,
    )
    page = paginate_output(display, offset=offset, limit=limit)

    # Get original output from cache
    try:
        raw = container.image.tags[0] if container.image.tags else container.image.id
        image_ref = str(raw) if not isinstance(raw, str) else raw
    except Exception:
        image_ref = container_id[:12]
    original_status = "failed" if failed[-1].get("exit_code", 0) != 0 else "ok"
    changed = (new_exit_code == 0) if failed[-1].get("exit_code", 0) != 0 else (new_exit_code != 0)

    result: dict[str, Any] = {
        "status": "ok" if new_exit_code == 0 else "error",
        "output": page.content,
        "shown": meta.shown,
        "total_lines": meta.total_lines,
        "truncated": meta.truncated,
        "next_offset": page.next_offset,
        "has_more": page.has_more,
        "previous_status": original_status,
        "changed": changed,
    }
    if new_exit_code != 0:
        result["exit_code"] = new_exit_code

    # Store new result in cache
    new_cache_key = compute_cache_key(image_ref, target_commands, input_hash=input_hash)
    set_cached_result(new_cache_key, result)

    journal_record_exec(
        container_id[:12],
        target_commands,
        new_exit_code,
        verbose=verbose,
        cached=False,
        output_size=len(raw_output.encode("utf-8")),
        input_hash=input_hash,
    )
    return json.dumps(result)


@mcp.tool()
def sandbox_cache_stats() -> str:
    """Return result cache statistics.

    Returns:
        JSON string with cache stats (total_entries, total_size_bytes,
        oldest/newest entry timestamps).
    """
    stats = get_cache_stats()
    return json.dumps(stats, ensure_ascii=False)


@mcp.tool()
def sandbox_cache_invalidate(key: str | None = None) -> str:
    """Invalidate result cache entries.

    Args:
        key: Optional specific cache key to invalidate.
             If omitted, all cache entries are invalidated.

    Returns:
        JSON string with ``invalidated`` count.
    """
    count = invalidate_cache(key=key)
    return json.dumps({"invalidated": count})


@mcp.tool()
def sandbox_exec_diff(
    container_id: str,
    commands: list[str],
    verbose: str = "summary",
    timeout: int = 0,
    max_output_tokens: int = 0,
    input_hash: str = "",
) -> str:
    """Execute commands and return only the diff from the cached result.

    First execution stores the result in cache.  Subsequent calls
    with the same container_id and commands return only what changed.

    Args:
        container_id: 12-character container ID prefix.
        commands: List of shell commands to execute.
        verbose: Output verbosity.
        timeout: Maximum seconds to let the command run.
        max_output_tokens: Token budget for output (``0`` = no limit).
            When set, the output is summarised to fit within this many
            estimated tokens.

    Returns:
        JSON string with ``diff`` containing only changed lines.
    """
    # Execute
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"status": "error", "error": f"container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    try:
        raw = container.image.tags[0] if container.image.tags else container.image.id
        image_ref = str(raw) if not isinstance(raw, str) else raw
    except Exception:
        image_ref = container_id[:12]
    cache_key = "diff:" + compute_cache_key(image_ref, commands, input_hash=input_hash)
    previous = get_cached_result(cache_key)

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
        stdout=True, stderr=True, demux=True,
    )
    stdout_part, stderr_part = output
    stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""

    if exit_code == 0:
        raw_output = stdout_text
    else:
        raw_output = stdout_text + "\n" + stderr_text if stdout_text and stderr_text else (stdout_text or stderr_text)

    clean = sanitize_output(raw_output)
    compressed = compress_repeated_lines(clean)
    if exit_code != 0:
        compressed = compress_failures(compressed)

    # Compute diff from previous result
    diff = ""
    if previous and previous.get("output"):
        prev_lines = previous["output"].split("\n")
        curr_lines = compressed.split("\n")
        diff_lines = list(difflib.unified_diff(
            prev_lines, curr_lines,
            fromfile="previous", tofile="current",
            lineterm="",
        ))
        diff = "\n".join(diff_lines)

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
            compressed, max_lines=100, verbose=verbose,
            exit_code=exit_code, stderr=stderr_text,
        )

    result: dict[str, Any] = {
        "status": "ok" if exit_code == 0 else "error",
        "output": display,
        "diff": diff,
        "shown": meta.shown,
        "total_lines": meta.total_lines,
        "truncated": meta.truncated,
        "has_diff": bool(diff),
    }
    if exit_code != 0:
        result["exit_code"] = exit_code

    # Update cache
    set_cached_result(cache_key, result)
    journal_record_exec(container_id[:12], commands, exit_code, verbose=verbose)
    return json.dumps(result)

# ---------------------------------------------------------------------------
# Edit/Verify tools
# ---------------------------------------------------------------------------


@mcp.tool()
def apply_patch(container_id: str, file_path: str, diff_content: str) -> str:
    """Apply a unified diff to a file inside the sandbox container.

    .. warning::

       **Deprecated for AI-authored edits.**  Hand-written unified diffs
       almost always fail on ``@@`` header line counts or context-line
       whitespace, and each failed retry costs a full round-trip — making
       this *more* expensive than the alternatives, not less.  For AI
       editing use :func:`write_file_sandbox` with ``old_str`` (the default
       edit path) or :func:`transform_file` (imperative).  Reserve
       ``apply_patch`` for **machine-generated** diffs (``git diff`` /
       ``diff -u``), where the diff is byte-exact.

    Reads the current file from the container, applies the unified diff,
    and writes the result back.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        diff_content: Unified diff string to apply.

    Returns:
        Success message or error description.

    See also:
        :func:`write_file_sandbox` — full overwrite / line-range /
        append / string-replace modes.
        :func:`transform_file` — recommended imperative edit path; also the
        actual implementation that ``apply_patch`` now delegates to.
    """
    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return f"Error: container {container_id[:12]} not found"
    except Exception as e:
        return f"Error: {e}"

    return apply_patch_to_file(client, container_id, file_path, diff_content)


@mcp.tool()
def transform_file(
    container_id: str,
    file_path: str,
    code: str,
    max_lines: int = 200,
    offset: int = 0,
    limit: int = 100,
) -> str:
    """Edit a file imperatively by running Python that computes the new text.

    The **imperative** edit path: instead of providing the new bytes
    (:func:`write_file_sandbox`) or a diff (:func:`apply_patch`), you provide
    *code* that transforms the file's content.  Ideal for edits that the
    declarative tools handle poorly — bulk / repetitive / structural / computed
    changes (e.g. a regex applied to every occurrence, renaming a symbol,
    re-indenting, applying a value derived from the existing text).

    *code* must define a top-level callable ``transform(text: str) -> str``.
    It is base64-encoded and executed by a Python runner **inside the
    disposable sandbox container** (never on the host), the result is written
    back, and a **unified diff of the change is returned** so you can verify
    the effect without a separate read-back.

    Passing the program as a single ``code`` string (not a shell command) means
    multibyte characters, quotes, and newlines need no escaping.

    .. hint::

       For a single known string replacement prefer :func:`write_file_sandbox`
       with ``old_str``.  Reach for ``transform_file`` when the edit is better
       expressed as logic than as literal text — many occurrences, a pattern,
       or a value computed from the file.  Always check the returned ``diff``;
       an over-broad pattern can change more than intended.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Absolute path to the file inside the container.
        code: Python source defining ``transform(text: str) -> str``.
            Executed as a **full Python interpreter** (not a restricted DSL):
            ``__builtins__``, ``open()``, ``import``, ``subprocess``, etc.
            are all available inside the disposable sandbox container.
        max_lines: Maximum diff lines to show (summary truncation).
        offset: Line offset for paging through a large diff (0-indexed).
        limit: Maximum diff lines per page.

    Returns:
        JSON string.  On success: ``status="ok"``, ``changed`` (bool),
        ``diff`` (str, paginated) and diff metadata (``shown``,
        ``total_lines``, ``truncated``, ``next_offset``, ``has_more``).
        On failure: ``status="error"`` with ``error`` (and ``traceback`` when
        the caller's code raised).

    See also:
        :func:`write_file_sandbox` — declarative edits (the default path).
        :func:`read_file_range` — inspect file content before editing.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps(
            {"status": "error", "error": f"container {container_id[:12]} not found"}
        )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    result = transform_file_in_container(client, container_id, file_path, code)

    if result.get("status") == "ok" and result.get("changed"):
        display, meta = truncate_output(
            result.get("diff", ""),
            max_lines=max_lines,
            verbose="full",
        )
        page = paginate_output(display, offset=offset, limit=limit)
        return json.dumps({
            "status": "ok",
            "changed": True,
            "diff": page.content,
            "shown": meta.shown,
            "total_lines": meta.total_lines,
            "truncated": meta.truncated,
            "next_offset": page.next_offset,
            "has_more": page.has_more,
        })

    return json.dumps(result)


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

    .. hint::

       Use ``limit=-1`` to read all remaining lines from *offset*
       to end of file in one call.

    Args:
        container_id: 12-character container ID prefix.
        file_path: Path to the file inside the container.
        offset: 0-indexed line offset to start reading from.
        limit: Maximum number of lines to return.  Use ``-1`` to read
            all remaining lines from *offset*.

    Returns:
        JSON string with file content and metadata, or an error
        message beginning with ``"Error:"``.

    See also:
        :func:`search_in_container` — find content across files with
        ripgrep/ast-grep.
        :func:`write_file_sandbox` — edit files after inspection.
    """
    client = _docker()
    try:
        _ = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    result = read_file_lines(
        _, file_path, offset=offset, limit=limit
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
    language: str | None = None,
) -> str:
    """Run lint + type_check + test + scan as a bundled verification.

    **Use this instead of calling** :func:`lint_in_container` **,**
    :func:`type_check_in_container` **, and pytest separately.**
    A single call runs all four analysis layers, normalises output,
    and returns a gate decision.

    Supports multi-language verification (Python / JS / TS / Go) with
    language-aware dispatch.  Auto-detects project language from *path*
    unless overridden with *language*.

    **Layers:**

    =========== ======== ============================
    Layer       Tool    Notes
    =========== ======== ============================
    lint        ruff    Python lint (``ruff check``)
    type_check  pyright Python type checking
    test        pytest  pytest with json-report
    scan        semgrep Security scanning
    =========== ======== ============================

    **Gate logic:**

    By default the gate fails when any of the following are detected:

    * lint errors (E/F/B/RUF rule codes)
    * test failures
    * semgrep ``ERROR`` findings
    * verification incomplete (tool not available or errored)

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
        language: Explicit language override (``"python"``, ``"js"``,
            ``"ts"``, ``"go"``).  Skips auto-detection.

    Returns:
        JSON string with:

        * ``status``: ``"ok"`` or ``"failed"``
        * ``gate_passed``: ``True`` if all gate conditions are satisfied
        * ``incomplete``: ``True`` if any layer was not available / errored
        * ``detected_languages``: list of detected language keys
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
        language=language,
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


# ---------------------------------------------------------------------------
# Shared dry_run / confirmation-token flow for boundary-crossing writes
# (Issue #169).  Both ``submit`` and ``sandbox_create_pr`` route their
# dry_run -> token -> execute flow through these helpers so the logic has a
# single implementation and cannot drift between the two tools.
# ---------------------------------------------------------------------------


def _dry_run_response(
    operation: str,
    cid: str,
    run_id: str,
    details: str,
    payload: dict[str, Any],
) -> str:
    """Generate a confirmation token and return the ``dry_run`` response.

    Generates a one-time confirmation token, records a pending
    (``approved=None``) boundary crossing in the journal, and returns the
    standard ``dry_run`` JSON envelope merged with *payload*.

    Args:
        operation: Operation type recorded on the token and journal
            (e.g. ``"submit"``, ``"sandbox_create_pr"``).
        cid: 12-character container ID prefix.
        run_id: Run identifier from the journal.
        details: Human-readable summary of what execution will do.
        payload: Extra fields merged into the response (e.g.
            ``diff_summary``, ``branch``, ``pr_title``).

    Returns:
        JSON string with ``status="dry_run"``, ``confirmation_token``,
        and the *payload* fields.
    """
    conf_token = generate_token(
        operation=operation,
        details=details,
        container_id=cid,
        run_id=run_id,
    )
    record_boundary_crossing(
        cid,
        operation,
        details,
        approved=None,
        token=conf_token,
    )
    return json.dumps({
        "status": "dry_run",
        "confirmation_token": conf_token,
        **payload,
    })


def _consume_confirmation_token(
    token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and consume a confirmation token for execution.

    Returns ``(token_meta, None)`` when the token is valid (and now
    consumed), or ``(None, error_json)`` with a ready-to-return error
    response when the token is missing, invalid, expired, or already used.
    """
    if not token:
        return None, json.dumps({
            "status": "error",
            "error": "Token required for execution.  Run with dry_run=True first.",
        })
    result = verify_and_consume(token)
    if result is None:
        return None, json.dumps({
            "status": "error",
            "error": "Token invalid, expired, or already used",
        })
    return result, None


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
    author_name: str | None = None,
    author_email: str | None = None,
    language: str | None = None,
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
        author_name: Git commit author name.  When set, takes precedence
            over the image-level default configured in
            ``docker/Dockerfile.sandbox`` (``code-sandbox-mcp[bot]``).
            When ``None``, the image-level default is used.
        author_email: Git commit author email.  When set, takes precedence
            over the image-level default configured in
            ``docker/Dockerfile.sandbox``
            (``code-sandbox-mcp[bot]@users.noreply.github.com``).
            When ``None``, the image-level default is used.

    Returns:
        JSON string with operation result.
    """
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

        return _dry_run_response(
            "submit",
            cid,
            run_id,
            details,
            {
                "diff_summary": diff_summary,
                "branch": branch,
                "message": message,
                "create_pr": create_pr,
                "pr_title": pr_title if create_pr else None,
            },
        )

    # ------------------------------------------------------------------
    # EXECUTE — require token + verify gate
    # ------------------------------------------------------------------
    if not token:
        return json.dumps({
            "status": "error",
            "error": "Token required for execution.  Run with dry_run=True first.",
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
        strict=True,
        gate_on_lint_error=gate_on_lint_error,
        gate_on_type_error=gate_on_type_error,
        gate_on_test_fail=gate_on_test_fail,
        gate_on_scan_error=gate_on_scan_error,
        gate_on_scan_warning=gate_on_scan_warning,
        language=language,
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

    # --- Consume token (after gate passes) ---
    token_result, token_error = _consume_confirmation_token(token)
    if token_error is not None:
        return token_error

    # --- Git branch check/create ---
    _run(f"git checkout -b {shlex.quote(branch)} 2>/dev/null || git checkout {shlex.quote(branch)}")

    # --- Git add / commit ---
    add_ec, add_out, add_err = _run("git add -A")
    if add_ec != 0:
        return json.dumps({
            "status": "error",
            "step": "git_add",
            "error": add_err or add_out,
        })

    # --- Git identity: set before commit ---
    name_to_use = author_name if author_name is not None else "code-sandbox-mcp[bot]"
    email_to_use = author_email if author_email is not None else "code-sandbox-mcp[bot]@users.noreply.github.com"
    safe_name = shlex.quote(name_to_use)
    safe_email = shlex.quote(email_to_use)
    git_commit_cmd = (
        f"git -c user.name={safe_name} -c user.email={safe_email} commit -m {shlex.quote(message)}"
    )

    commit_ec, commit_out, commit_err = _run(git_commit_cmd)
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



_REPO_FORMAT_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_BRANCH_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)(?!-)(?!.*@\{)"
    r"[\w./-]+$"
)


_SANDBOX_CREATE_PR_SCRIPT = '''
import base64, json, os, shlex, subprocess, sys, tempfile


def _run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _gh_api(method, path, body):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        tmpfile = f.name
    try:
        r = subprocess.run(
            ["gh", "api", "-X", method, path, "--input", tmpfile],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout)
        return json.loads(r.stdout)
    finally:
        os.unlink(tmpfile)


repo, branch, working_dir = sys.argv[1], sys.argv[2], sys.argv[3]
os.chdir(working_dir)

# 1. Collect local commit info
ec, head_sha, _ = _run("git rev-parse HEAD")
if ec != 0:
    print(json.dumps({"error": "git rev-parse HEAD failed", "detail": head_sha}))
    sys.exit(1)

ec_log, commit_msg, _ = _run("git log -1 --format=%B")
if ec_log != 0 or not commit_msg:
    commit_msg = "(no commit message)"
_, author_name, _ = _run("git log -1 --format=%an")
_, author_email, _ = _run("git log -1 --format=%ae")

# 2. Get all files in HEAD
_, files_out, _ = _run("git ls-tree -r --name-only HEAD")
files = [f for f in files_out.split("\n") if f]

# 3. Create blobs
tree_items = []
for filepath in files:
    _, mode_line, _ = _run(f"git ls-tree HEAD -- {shlex.quote(filepath)}")
    parts = mode_line.split()
    mode = parts[0] if parts else "100644"
    try:
        with open(filepath, "rb") as fh:
            file_content = base64.b64encode(fh.read()).decode()
    except OSError as e:
        print(json.dumps({"error": f"read {filepath}: {e}"}))
        sys.exit(1)
    blob = _gh_api(
        "POST",
        f"repos/{repo}/git/blobs",
        {"content": file_content, "encoding": "base64"},
    )
    tree_items.append(
        {"path": filepath, "mode": mode, "type": "blob", "sha": blob["sha"]}
    )

# 4. Create tree
tree = _gh_api("POST", f"repos/{repo}/git/trees", {"tree": tree_items})

# 5. Resolve parent SHA on GitHub (existing branch > main > master)
parent_sha = None
for ref_name in [f"heads/{branch}", "heads/main", "heads/master"]:
    ec2, ref_out, _ = _run(f"gh api repos/{shlex.quote(repo)}/git/ref/{shlex.quote(ref_name)} 2>/dev/null")
    if ec2 == 0:
        try:
            parent_sha = json.loads(ref_out)["object"]["sha"]
            break
        except Exception:
            continue

# 6. Create commit
commit_body = {
    "message": commit_msg,
    "tree": tree["sha"],
    "author": {"name": author_name, "email": author_email},
}
if parent_sha:
    commit_body["parents"] = [parent_sha]
commit = _gh_api("POST", f"repos/{repo}/git/commits", commit_body)
new_sha = commit["sha"]

# 7. Create or update branch ref
try:
    _gh_api(
        "PATCH",
        f"repos/{repo}/git/refs/heads/{branch}",
        {"sha": new_sha, "force": True},
    )
except RuntimeError:
    _gh_api(
        "POST",
        f"repos/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": new_sha},
    )

print(json.dumps({"sha": new_sha, "tree_sha": tree["sha"], "parent_sha": parent_sha}))
'''


# ---------------------------------------------------------------------------
# VCS push tools (Issue #152)
# ---------------------------------------------------------------------------

@mcp.tool()
def sandbox_create_pr(
    container_id: str,
    repo: str,
    branch: str,
    pr_title: str,
    pr_body: str = "",
    base_branch: str = "",
    working_dir: str = "/home/sandbox",
    dry_run: bool = False,
    token: str = "",
) -> str:
    """Push the current branch via GitHub API and create a PR.

    Unlike :func:`submit`, this tool uses the GitHub Objects API
    (blob → tree → commit → ref) to push the branch, which works when
    the injected token cannot push via HTTPS git (e.g. GitHub App
    installation tokens).

    Typical flow:

    1. ``sandbox_initialize(allow_network=True, inject_vcs_token=True)``
    2. ``clone_repo`` / ``gh repo clone`` inside the container
    3. Make changes, run ``git add -A && git commit``
    4. ``sandbox_create_pr(dry_run=True)`` — preview + confirmation token
    5. ``sandbox_create_pr(token=...)`` — pushes via API + opens PR

    Like :func:`submit`, execution is a two-step flow: call once with
    ``dry_run=True`` to get a preview of the HEAD commit and a
    confirmation token, then call again with that ``token`` to push and
    open the PR.

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True``.

    .. note::

       Only the most recent committed state (HEAD) is pushed.  Multiple
       local commits are represented as a single commit on GitHub whose
       tree matches the HEAD tree and whose parent is the current tip of
       *branch* (or the default branch if *branch* is new).

    .. warning::

       Unlike :func:`submit`, this tool has **no verify gate** (no
       lint/type-check/test run before push).  Ensure the sandbox passes
       all checks before calling this tool.

    .. warning::

       This tool uses ``PATCH`` with ``force: true`` when updating an
       existing branch, which will **force-push** and overwrite any
       commits on *branch* that are not reflected in the container's HEAD
       tree.  Do not use this tool on shared branches where others may
       have pushed commits.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``'owner/repo'`` format.
        branch: Branch name to create or update on GitHub.
        pr_title: Title for the pull request.
        pr_body: Body text for the pull request (optional).
        base_branch: Base branch for the PR (default: repository default
            branch).
        working_dir: Directory in the container containing the git
            repository (default ``'/home/sandbox'``).
        dry_run: When ``True``, returns a preview of the HEAD commit that
            would be pushed plus a confirmation token, without pushing or
            creating a PR.
        token: Confirmation token from a previous ``dry_run`` call.
            Required for execution (``dry_run=False``).

    Returns:
        JSON with ``status``, ``pr_url``, ``branch``, and ``sha``.
        On error returns ``status='error'`` with an ``error`` field.
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
        return json.dumps({
            "status": "error",
            "error": "repo must be in owner/repo format",
        })

    if not _BRANCH_RE.match(branch):
        return json.dumps({
            "status": "error",
            "error": "branch contains invalid characters",
        })

    if base_branch and not _BRANCH_RE.match(base_branch):
        return json.dumps({
            "status": "error",
            "error": "base_branch contains invalid characters",
        })

    def _run(cmd: str) -> tuple[int, str, str]:
        exit_code, output = container.exec_run(
            ["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=working_dir,
        )
        stdout_b, stderr_b = output or (b"", b"")
        out = stdout_b.decode("utf-8", errors="replace").strip() if stdout_b else ""
        err = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
        return exit_code, out, err

    run_id = get_or_create_run_id(cid)

    # ------------------------------------------------------------------
    # DRY RUN — preview push plan and generate confirmation token
    # ------------------------------------------------------------------
    if dry_run:
        # HEAD is what gets pushed (see note above): preview that commit.
        _ec, log_out, _ = _run("git log -1 --pretty='%h %s' HEAD")
        _ec2, stat_out, _ = _run("git show --stat --pretty='' HEAD")
        diff_summary = (log_out + "\n" + stat_out).strip()
        if not diff_summary:
            diff_summary = "(no committed HEAD to push)"

        details = (
            f"repo={repo} branch={branch} pr_title={pr_title[:60]}"
            f" base={base_branch or 'default'}"
        )
        return _dry_run_response(
            "sandbox_create_pr",
            cid,
            run_id,
            details,
            {
                "diff_summary": diff_summary,
                "branch": branch,
                "pr_title": pr_title,
                "base_branch": base_branch or None,
            },
        )

    # ------------------------------------------------------------------
    # EXECUTE — require confirmation token from a prior dry_run
    # ------------------------------------------------------------------
    _token_meta, token_error = _consume_confirmation_token(token)
    if token_error is not None:
        return token_error

    # Write the API-push script into the container
    script_b64 = base64.b64encode(
        _SANDBOX_CREATE_PR_SCRIPT.encode("utf-8")
    ).decode("ascii")
    _run(f"echo {shlex.quote(script_b64)} | base64 -d > /tmp/_sandbox_create_pr.py")

    # Execute: push via GitHub API
    ec, out, _ = _run(
        f"trap 'rm -f /tmp/_sandbox_create_pr.py' EXIT"
        f" && python3 /tmp/_sandbox_create_pr.py {shlex.quote(repo)} {shlex.quote(branch)} {shlex.quote(working_dir)}"
    )
    if ec != 0:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push error={out[:200]}",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", "error": out})

    try:
        push_result = json.loads(out)
    except json.JSONDecodeError:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push json_parse_error",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", "error": out})

    if "error" in push_result:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push error={push_result.get('error', ''):.200}",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", **push_result})

    new_sha = push_result["sha"]

    # Create PR
    pr_cmd = (
        f"gh pr create --repo {shlex.quote(repo)}"
        f" --head {shlex.quote(branch)}"
        f" --title {shlex.quote(pr_title)}"
    )
    if base_branch:
        pr_cmd += f" --base {shlex.quote(base_branch)}"
    if pr_body:
        body_b64 = base64.b64encode(pr_body.encode("utf-8")).decode("ascii")
        # Wrap pr_cmd: write body to a temp file, run pr_cmd with --body-file, then clean up
        base_cmd = pr_cmd
        pr_cmd = (
            f"BODY_FILE=$(mktemp) &&"
            f" trap 'rm -f \"$BODY_FILE\"' EXIT &&"
            f" echo {shlex.quote(body_b64)} | base64 -d > \"$BODY_FILE\" &&"
            f" {base_cmd} --body-file \"$BODY_FILE\""
        )

    pr_ec, pr_out, _ = _run(pr_cmd)
    if pr_ec != 0:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} sha={new_sha[:7]} step=pr_create error={pr_out[:200]}",
            approved=True, token=token,
        )
        return json.dumps({
            "status": "pushed_no_pr",
            "branch": branch,
            "sha": new_sha[:7],
            "pr_create_error": pr_out,
        })

    pr_url = ""
    _pr_url_marker = f"github.com/{repo}/pull/"
    for line in pr_out.splitlines():
        line = line.strip()
        if _pr_url_marker in line and line.startswith("https://"):
            pr_url = line
            break

    record_boundary_crossing(
        cid,
        "sandbox_create_pr",
        f"repo={repo} branch={branch} sha={new_sha[:7]} pr_url={pr_url}",
        approved=True,
        token="",
    )

    return json.dumps({
        "status": "ok",
        "branch": branch,
        "sha": new_sha[:7],
        "pr_url": pr_url,
    })

# ---------------------------------------------------------------------------
# Repository exploration tools (Issue #86)
# ---------------------------------------------------------------------------

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

    .. hint::

       To avoid the two-step "init → clone" workflow, use
       :func:`sandbox_initialize` with ``clone_repo`` — it starts
       the container and copies a pre-cloned Shiori repo in one call.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``"owner/repo"`` format.
        dest_dir: Parent directory in the container.  The repo is cloned
            into ``{dest_dir}/{repo_name}`` (default parent
            ``"/home/sandbox"``).
        branch: Branch name to clone. Omit for the default branch.

    Returns:
        JSON string with ``status``, ``repo``, ``clone_path``, and
        ``branch``.  On error returns an ``error`` field.

    See also:
        :func:`sandbox_initialize` — one-step init + clone with
        ``clone_repo`` parameter.
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

    # ``gh repo clone`` treats its second argument as the clone target
    # directory itself, not a parent.  Clone into ``{dest_dir}/{repo_name}``
    # so the default ``dest_dir`` (``/home/sandbox``, an existing non-empty
    # home directory) works and the target matches the reported
    # ``clone_path``.
    repo_name = repo.split("/")[-1]
    clone_path = f"{dest_dir.rstrip('/')}/{repo_name}"

    safe_target = shlex.quote(clone_path)
    safe_repo = shlex.quote(repo)

    # Best-effort: configure gh as git credential helper so that
    # ``git push`` works with the injected token.  Failure is intentionally
    # ignored — when inject_vcs_token=False (no GH_TOKEN in env) the command
    # exits non-zero but cloning public repos still succeeds.  If push later
    # fails with an auth error the cause will be clear from that message.
    _auth_ec, _ = container.exec_run(
        ["/bin/sh", "-c", "gh auth setup-git"],
        stdout=True,
        stderr=True,
    )
    del _auth_ec  # intentionally ignored; see comment above

    if branch:
        cmd = (
            f"gh repo clone {safe_repo} {safe_target}"
            f" -- -b {shlex.quote(branch)}"
        )
    else:
        cmd = f"gh repo clone {safe_repo} {safe_target}"

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
        error_text = stderr_text or stdout_text
        # Surface a clearer hint when the target already exists, instead of
        # the bare git "already exists and is not an empty directory" message.
        if "already exists" in error_text:
            error_text = (
                f"{error_text.rstrip()}\n"
                f"Hint: {repr(clone_path)} already exists. Specify a different "
                f"dest_dir, or remove the existing directory first."
            )
        return json.dumps({
            "status": "error",
            "error": error_text,
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
    result = verify_token(token)
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
# Test environment tools (§10)
# ---------------------------------------------------------------------------


_TEST_ENV_NETWORKS: dict[str, list[str]] = {}
_TEST_ENV_NETWORKS_LOCK: threading.Lock = threading.Lock()
def _health_check_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is open on *host*."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def _health_check_http(url: str, timeout: float = 5.0) -> bool:
    """Check if an HTTP endpoint returns a successful response."""
    import urllib.error
    import urllib.request
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        return 200 <= resp.getcode() < 400
    except (urllib.error.URLError, OSError):
        return False


def _cleanup_test_environment(network_name: str) -> None:
    """Stop and remove all containers and network for a test environment."""
    client = _docker()
    with _TEST_ENV_NETWORKS_LOCK:
        container_ids = _TEST_ENV_NETWORKS.pop(network_name, [])
    for cid in container_ids:
        try:
            container = client.containers.get(cid)
            container.stop()
            container.remove()
            record_test_environment(cid, [{"name": cid}], "stopped")
            record_stop(cid)
        except Exception:
            pass

    try:
        network = client.networks.get(network_name)
        network.remove()
    except Exception:
        pass


@mcp.tool()
def run_test_environment(
    services: list[dict[str, Any]],
    network_name: str | None = None,
    cleanup_after: str | None = None,
) -> str:
    """Start a Compose-like test environment with multiple services.

    Creates a Docker network, starts each service container with
    health checks, waits for readiness, and returns access URLs.

    Displays a plan before execution: the response includes services
    to start, network details, and cleanup info.

    Args:
        services: List of service definitions. Each entry supports:
            - ``name`` (required): Service name.
            - ``image`` (required): Docker image (``image@sha256:...``).
            - ``command`` (optional): Command to run in the container.
            - ``ports`` (optional): Dict mapping ``host_port → container_port``.
            - ``env`` (optional): Dict of environment variables.
            - ``depends_on`` (optional): List of service names to wait for.
            - ``access_url`` (optional): Template (e.g. ``"http://localhost:{port}"``).
        network_name: Name for the Docker network. Auto-generated if omitted.
        cleanup_after: If set, auto-stop after this many seconds (string).

    Returns:
        JSON string with ``status``, ``environment_id`` (network name),
        ``services`` (list with ``name``, ``container_id``, ``access_url``),
        and ``plan`` (the execution plan).
    """
    import random
    import string

    if not services:
        return json.dumps({"status": "error", "error": "No services provided"})

    client = _docker()

    # Generate network name if not provided
    if not network_name:
        suffix = "".join(random.choices(string.ascii_lowercase, k=8))
        network_name = f"testenv_{suffix}"

    # Build and display plan
    plan_services = []
    for svc in services:
        plan_services.append({
            "name": svc["name"],
            "image": svc.get("image", "unknown"),
            "ports": svc.get("ports", {}),
            "depends_on": svc.get("depends_on", []),
        })

    plan = {
        "network": network_name,
        "services": plan_services,
        "cleanup_after": cleanup_after,
    }

    started_services: list[dict[str, Any]] = []

    try:
        # Create network
        try:
            client.networks.create(network_name, driver="bridge")
        except Exception as e:
            return json.dumps({
                "status": "error",
                "error": f"Failed to create network {network_name}: {e}",
                "plan": plan,
            })

        with _TEST_ENV_NETWORKS_LOCK:
            _TEST_ENV_NETWORKS[network_name] = []
        # Topological start respecting dependencies
        started_names: set[str] = set()

        def _start_service(svc_def: dict) -> dict[str, Any] | None:
            name = svc_def["name"]
            image = svc_def.get("image", "")
            command = svc_def.get("command")
            ports = svc_def.get("ports", {})
            env_vars = svc_def.get("env", {})

            port_bindings = {}
            for host_p, container_p in ports.items():
                port_bindings[str(container_p)] = ("0.0.0.0", int(host_p))

            try:
                validate_image_ref(image)
                profile = replace(
                    DEFAULT_SECURITY_PROFILE,
                    allow_network=True,
                )
                run_kwargs = build_secure_run_kwargs(
                    profile,
                    command=command or "sleep infinity",
                    detach=True,
                    remove=False,
                    environment=env_vars or None,
                    ports=port_bindings or None,
                )
                run_kwargs["network"] = network_name
                run_kwargs.pop("network_mode", None)

                container = client.containers.run(image, **run_kwargs)
                cid = container.id[:12]

                # Build access URL
                access_url = svc_def.get("access_url", "")
                if access_url:
                    for host_p in ports:
                        access_url = access_url.replace("{port}", str(host_p))
                elif ports:
                    host_port = list(ports.keys())[0]
                    access_url = f"http://localhost:{host_port}"

                svc_info = {
                    "name": name,
                    "container_id": cid,
                    "image": image,
                    "access_url": access_url or None,
                    "ports": ports,
                }

                with _TEST_ENV_NETWORKS_LOCK:
                    _TEST_ENV_NETWORKS[network_name].append(cid)
                record_test_environment(cid, [svc_info], "starting")

                return svc_info

            except Exception as e:
                return {"name": name, "error": str(e)}

        remaining = list(services)
        max_iter = len(services) * 2
        iteration = 0

        while remaining and iteration < max_iter:
            iteration += 1
            still_remaining = []
            for svc in remaining:
                deps = svc.get("depends_on", [])
                if all(d in started_names for d in deps):
                    result = _start_service(svc)
                    if result:
                        started_services.append(result)
                        started_names.add(svc["name"])
                    else:
                        still_remaining.append(svc)
                else:
                    still_remaining.append(svc)
            remaining = still_remaining

        # Check for circular / unresolvable dependencies
        if remaining:
            for svc in remaining:
                result = _start_service(svc)
                if result and 'error' not in result:
                    started_services.append(result)
                    started_names.add(svc['name'])
                else:
                    started_services.append({'name': svc['name'], 'error': 'unresolvable dependency'})

        # Mark all as ready
        for svc_info in started_services:
            if "error" not in svc_info:
                record_test_environment(
                    svc_info["container_id"],
                    [svc_info],
                    "ready",
                )

        result = {
            "status": "ok",
            "environment_id": network_name,
            "services": started_services,
            "plan": plan,
        }

        # Set up automatic cleanup timer if requested
        if cleanup_after:
            def _auto_cleanup():
                import time
                time.sleep(int(cleanup_after))
                try:
                    _cleanup_test_environment(network_name)
                except Exception:
                    pass
            timer = threading.Thread(target=_auto_cleanup, daemon=True)
            timer.start()

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        _cleanup_test_environment(network_name)
        return json.dumps({
            "status": "error",
            "error": str(e),
            "plan": plan,
            "services": started_services,
        }, ensure_ascii=False)


@mcp.tool()
def stop_test_environment(environment_id: str) -> str:
    """Stop and remove a test environment started by :func:`run_test_environment`.

    Stops all containers and removes the network.

    Args:
        environment_id: The network name (``environment_id``) returned
            by :func:`run_test_environment`.

    Returns:
        JSON string with ``status`` and ``environment_id``.
    """
    try:
        _cleanup_test_environment(environment_id)
        return json.dumps({
            "status": "ok",
            "environment_id": environment_id,
        })
    except Exception as e:
        return json.dumps({
            "status": "error",
            "error": str(e),
        })


@mcp.tool()
def wait_for_condition(
    condition_type: str,
    target: str,
    port: int | None = None,
    timeout: int = 60,
    interval: float = 2.0,
    container_id: str | None = None,
    log_pattern: str | None = None,

    log_tail: int = 100,
) -> str:
    """Wait for a condition to be met, with timeout.

    Eliminates the need for ``sleep 30`` patterns in AI workflows.

    Supports three condition types:

    - ``"tcp"``: Wait until a TCP port is open on *target*.
      Requires *target* (hostname/IP) and *port*.
    - ``"http"``: Wait until an HTTP endpoint returns a 2xx or 3xx status.
      *target* is the full URL (e.g. ``"http://localhost:8080/health"``).
    - ``"log"``: Wait for a regex pattern in a container's logs.
      Requires *container_id* and *log_pattern* (regex).

    Args:
        condition_type: ``"tcp"``, ``"http"``, or ``"log"``.
        target: Hostname/IP (``"tcp"``) or URL (``"http"``).
        port: TCP port (required for ``"tcp"``).
        timeout: Max seconds to wait (default 60).
        interval: Polling interval seconds (default 2.0).
        container_id: Container ID for ``"log"`` condition.
        log_pattern: Regex pattern for log matching.

        log_tail: Number of log lines to check (default 100).
    Returns:
        JSON string with ``status`` (``"ready"`` or ``"timeout"``),
        ``condition_type``, ``target``, and ``elapsed`` seconds.
    """
    import re
    import time

    start = time.time()
    deadline = start + timeout

    attempts = 0
    last_error: str | None = None

    while time.time() < deadline:
        attempts += 1
        try:
            ready = False

            if condition_type == "tcp":
                if port is None:
                    return json.dumps({
                        "status": "error",
                        "error": "port is required for tcp condition",
                    })
                ready = _health_check_tcp(target, port, timeout=min(interval, 5.0))

            elif condition_type == "http":
                ready = _health_check_http(target, timeout=min(interval, 5.0))

            elif condition_type == "log":
                if not container_id or not log_pattern:
                    return json.dumps({
                        "status": "error",
                        "error": "container_id and log_pattern required for log condition",
                    })
                client = _docker()
                try:
                    container = client.containers.get(container_id)
                except Exception as e:
                    last_error = str(e)
                    time.sleep(interval)
                    continue

                logs = container.logs(tail=log_tail, stdout=True, stderr=True)
                log_text = logs.decode("utf-8", errors="replace") if logs else ""
                if re.search(log_pattern, log_text):
                    ready = True
                else:
                    last_error = f"Pattern {log_pattern!r} not found in logs"

            else:
                return json.dumps({
                    "status": "error",
                    "error": f"Unknown condition_type: {condition_type}. "
                             f"Supported: tcp, http, log",
                })

            if ready:
                elapsed = round(time.time() - start, 2)
                return json.dumps({
                    "status": "ready",
                    "condition_type": condition_type,
                    "target": target,
                    "port": port,
                    "elapsed": elapsed,
                    "attempts": attempts,
                })

        except Exception as e:
            last_error = str(e)

        time.sleep(interval)

    elapsed = round(time.time() - start, 2)
    result: dict[str, Any] = {
        "status": "timeout",
        "condition_type": condition_type,
        "target": target,
        "port": port,
        "elapsed": elapsed,
        "attempts": attempts,
        "timeout": timeout,
    }
    if last_error:
        result["last_error"] = last_error
    return json.dumps(result)


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
