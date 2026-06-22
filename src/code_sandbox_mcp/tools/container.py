"""Container lifecycle tools: init, stop, exec, test environment."""

from __future__ import annotations

import base64
import difflib
import io
import json
import logging
import os
import re
import shlex
import tarfile
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

from docker.errors import APIError, NotFound

from code_sandbox_mcp.journal import (
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
    get_cached_result,
    set_cached_result,
)
from code_sandbox_mcp.security import (
    DEFAULT_SECURITY_PROFILE,
    build_secure_run_kwargs,
    validate_image_ref,
)
from code_sandbox_mcp.tools.common import RECOVERY_DOCKER_TIMEOUT, _docker

logger: logging.Logger = logging.getLogger(__name__)

_DEFAULT_IMAGE: str = "ghcr.io/masuda-masuo/code-sandbox-mcp/sandbox@sha256:a7c48dfd938a77c33e622b2d8e888b8ff642feac205c058b87cac40be2b9275b"


_SHIORI_REPOS_PATH: str | None = None

_CLONE_REPO_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9._-]+$")

_SENSITIVE_FILE_BASENAMES: frozenset[str] = frozenset(
    {
        ".env",
        ".git-credentials",
        ".gitconfig",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
    }
)


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
        raise ValueError(f"clone_repo must be 'owner/name' format, got: {clone_repo!r}")
    owner, name = parts
    if not _CLONE_REPO_PATTERN.match(owner) or not _CLONE_REPO_PATTERN.match(name):
        raise ValueError(
            f"clone_repo must be 'owner/name' format with alphanumeric characters (._- allowed), got: {clone_repo!r}"
        )
    return owner, name


def _shiori_preclone_exists(clone_repo: str) -> bool:
    """Return True when the Shiori pre-clone for *clone_repo* is present on disk.

    Returns False when ``_SHIORI_REPOS_PATH`` is unset or when the specific
    repository directory (with a ``.git`` sub-directory) is absent.  Used to
    decide whether network access must be auto-enabled before container start,
    and whether the network fallback should run (Issue #178).
    """
    if not _SHIORI_REPOS_PATH:
        return False
    repos_root = Path(_SHIORI_REPOS_PATH).resolve()
    clone_path = (repos_root / clone_repo).resolve()
    # Shiori pre-clones are produced by a normal git clone, so .git is a
    # directory.  Bare clones and git-worktree secondaries (where .git is
    # a file) are not used by Shiori, so is_dir() is the right check.
    return clone_path.is_dir() and (clone_path / ".git").is_dir()


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
        raise ValueError(f"clone_dest must start with /tmp/, got: {clone_dest!r}")

    if not _SHIORI_REPOS_PATH:
        raise ValueError("Shiori repos path is not configured. Set --shiori-repos-path or SHIORI_REPOS_PATH env var.")

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
        raise ValueError(f"Path traversal detected: {clone_from} is outside {repos_root}")

    if not resolved_from.is_dir():
        raise ValueError(f"Repository clone not found: {resolved_from}")

    if not (resolved_from / ".git").exists():
        raise ValueError(f"Repository clone at {resolved_from} has no .git directory")

    logger.info(
        "Copying Shiori clone %s → container %s:%s",
        resolved_from,
        container_id[:12],
        clone_dest,
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
        fetch_output = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        if exit_code != 0:
            logger.warning(
                "git fetch --unshallow failed (exit=%d): %s",
                exit_code,
                fetch_output.strip(),
            )
        else:
            logger.info(
                "git fetch --unshallow succeeded: %s",
                fetch_output.strip(),
            )
    except Exception as e:
        logger.warning("git fetch --unshallow error: %s", e)

    return f"Copied Shiori clone of {clone_repo} → {clone_dest}/repo in container {container_id[:12]}"


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
    # Not redundant with the callers: on the network-fallback path the
    # caller may skip _clone_shiori_repo_to_container entirely (when
    # _SHIORI_REPOS_PATH is unset) or that function raises before reaching
    # _validate_clone_repo (when the specific pre-clone is absent, Issue #178).
    # Either way this is the only validation on the network-fallback path.
    _validate_clone_repo(clone_repo)
    repo_name = clone_repo.split("/")[-1]
    clone_path = clone_dest.rstrip("/") + "/" + repo_name
    cmd = "gh repo clone " + shlex.quote(clone_repo) + " " + shlex.quote(clone_path)
    exit_code, output = container.exec_run(["/bin/sh", "-c", cmd])
    if exit_code != 0:
        detail = output.decode("utf-8", errors="replace").strip()
        hint = ""
        if not inject_vcs_token:
            hint = " (if this is a private repository, retry with inject_vcs_token=True so gh can authenticate)"
        raise RuntimeError(f"gh repo clone failed (exit {exit_code}): {detail}{hint}")
    return "Cloned {} via network into {} in container {}".format(clone_repo, clone_path, container_id[:12])


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

    Selects the clone path up front via ``_shiori_preclone_exists``:

    - pre-clone present  -> Shiori fast-path
    - pre-clone absent   -> network fallback (Issue #178)

    Returns ``(clone_msg, None)`` on success, ``(None, error_str)`` on failure.
    """
    try:
        if not _shiori_preclone_exists(clone_repo):
            msg = _clone_repo_via_network(
                container,
                container_id,
                clone_repo,
                clone_dest,
                inject_vcs_token,
            )
        else:
            msg = _clone_shiori_repo_to_container(
                container,
                container_id,
                clone_repo,
                clone_dest,
            )
        return CloneResult(msg, None)
    except Exception as e:
        logger.warning("Clone failed: %s", e)
        return CloneResult(None, str(e))


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
    gh_info_cmd = f"gh pr view {pr_number} --repo {safe_repo} --json headRefName"
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
        raise RuntimeError(f"Failed to fetch PR #{pr_number} from {repo}: {stderr_text or stdout_text}")

    try:
        pr_info = json.loads(stdout_text)
    except json.JSONDecodeError:
        raise RuntimeError(f"Failed to parse PR info JSON: {stdout_text[:200]}")

    head_ref = pr_info.get("headRefName", "")
    if not head_ref:
        raise RuntimeError(f"Incomplete PR info: head_ref={head_ref!r}")

    # Step 2: Clone the base repo
    # Clone the base repo (not head/fork) so that gh pr checkout
    # works correctly with the PR number from the base repository.
    clone_cmd = f"gh repo clone {safe_repo} {safe_dest}"
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
        raise RuntimeError(f"Failed to clone repo {repo}: {stderr_text or clone_output}")

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
        raise RuntimeError(f"Failed to checkout PR #{pr_number}: {stderr_text or co_output}")

    logger.info(
        "Checked out PR #%s (%s) → %s in container %s",
        pr_number,
        head_ref,
        safe_dest,
        cid,
    )

    # Step 4: Install dev dependencies (non-fatal)
    if pip_extras is not None:
        install_cmd = f"cd {safe_dest} && pip install -e '.{pip_extras}' -q"
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
                pip_extras,
                exit_code,
                (stderr_text or install_output).strip(),
            )

    record_copy(
        cid,
        "setup_pr_branch",
        f"repo={repo} pr=#{pr_number} branch={head_ref}",
        safe_dest,
    )

    return f"PR #{pr_number} ({head_ref}) → {clone_dest}/repo in container {cid}"


def sandbox_initialize(
    image: str | None = None,
    allow_network: bool = False,
    inject_vcs_token: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = "/tmp/repo",
    repo: str | None = None,
    pr: int | None = None,
    pip_extras: str | None = "[dev]",
    mem_limit: str | None = None,
    cpus: float | None = None,
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
        mem_limit: Optional memory-limit override (e.g. ``"2g"``).
               The default profile caps containers at 512MB with swap
               disabled, which OOM-thrashes heavy installs (e.g. torch)
               into an unhealthy state (Issue #181).  Raise this when a
               workload legitimately needs more.  ``memswap_limit`` is
               automatically pinned to this value so swap stays
               disabled at the new ceiling.
        cpus: Optional CPU-limit override in cores (e.g. ``2.0``).
               Defaults to the profile's 0.5-core cap when omitted.

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

    # Auto-enable network when pre-clone is absent (Issue #146, #178)
    if clone_repo and pr is None and not _shiori_preclone_exists(clone_repo):
        allow_network = True
        logger.info(
            "clone_repo=%r: pre-clone absent, auto-enabling network access",
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

    # Resource overrides (Issue #181): the default 512MB / 0.5-CPU /
    # no-swap profile is too small for heavy installs (e.g. torch), which
    # OOM-thrash the container into an unhealthy state.  Let callers raise
    # the ceiling when they know they need it.  memswap is pinned to
    # mem_limit so swap stays disabled at the new ceiling (docker also
    # requires memswap_limit >= mem_limit).
    resource_overrides: dict[str, Any] = {}
    if mem_limit is not None:
        resource_overrides["mem_limit"] = mem_limit
        resource_overrides["memswap_limit"] = mem_limit
    if cpus is not None:
        if cpus <= 0:
            return "Error: cpus must be > 0"
        resource_overrides["cpu_quota"] = int(cpus * profile.cpu_period)

    run_kwargs = build_secure_run_kwargs(
        profile,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
        **resource_overrides,
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
            container,
            cid,
            clone_repo,
            clone_dest,
            inject_vcs_token,
        )
        if err is not None:
            clone_msg = f" (clone_repo failed: {err})"
        else:
            clone_msg = " " + msg
    elif clone_repo and pr is not None:
        logger.info(
            "Skipping clone_repo=%s (pr=%s handles its own clone)",
            clone_repo,
            pr,
        )

    # -- PR branch setup (Issue #136) --
    pr_msg = ""
    if pr is not None:
        if not repo:
            logger.warning("pr parameter requires repo, got repo=None")
            pr_msg = " (pr setup failed: repo is required when pr is specified)"
        else:
            try:
                pr_msg = " " + _setup_pr_branch(
                    container,
                    cid,
                    repo,
                    pr,
                    clone_dest,
                    pip_extras,
                )
            except Exception as e:
                # PR setup failure is non-fatal: the container is still usable.
                logger.warning("PR branch setup failed: %s", e)
                pr_msg = f" (pr setup failed: {e})"

    return cid + clone_msg + pr_msg


def sandbox_stop(container_id: str) -> str:
    """Stop and remove a running sandbox container.

    Args:
        container_id: 12-character container ID prefix.

    Removal is forceful: the container is killed (SIGKILL) and removed
    with ``force=True`` rather than gracefully stopped.  A graceful
    ``stop()`` waits for SIGTERM (up to 10s) then SIGKILL, which can
    itself hang on a wedged or unhealthy container — defeating the
    purpose of a recovery tool.  Combined with a short Docker API
    timeout, this guarantees ``sandbox_stop`` stays responsive even when
    other operations are stuck (Issue #181).

    Returns:
        Success message or error message beginning with ``"Error:"``.
    """
    client = _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
    cid = container_id[:12]
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return f"Error: container {cid} not found"
    except Exception as e:
        return f"Error: {e}"

    # Kill first (ignore if already stopped), then force-remove so a
    # still-running or unresponsive container is torn down regardless.
    try:
        container.kill()
    except (NotFound, APIError):
        # Already stopped / not running — proceed to removal.
        pass
    try:
        container.remove(force=True)
    except NotFound:
        pass
    except Exception as e:
        return f"Error: {e}"

    record_stop(cid)
    return f"Container {cid} stopped and removed"


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

    # Auto-enable network when pre-clone is absent (Issue #146, #178)
    if clone_repo and pr is None and not _shiori_preclone_exists(clone_repo):
        allow_network = True
        logger.info(
            "clone_repo=%r: pre-clone absent, auto-enabling network access",
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
        return json.dumps({"status": "error", "error": f"Failed to start container: {e}"})

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
            container,
            container_id,
            clone_repo,
            clone_dest,
            inject_vcs_token,
        )
    elif clone_repo and pr is not None:
        logger.info(
            "Skipping clone_repo=%s (pr=%s handles its own clone)",
            clone_repo,
            pr,
        )

    # --- PR branch setup (Issue #136) ---
    pr_error: str | None = None
    if pr is not None:
        if not repo:
            logger.warning("pr parameter requires repo, got repo=None")
            pr_error = "repo is required when pr is specified"
        else:
            try:
                _setup_pr_branch(
                    container,
                    container_id,
                    repo,
                    pr,
                    clone_dest,
                    pip_extras,
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
        stdout_text = stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
        stderr_text = stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
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
    failed: list[dict[str, Any]] = [e for e in entries if e.get("operation") == "exec" and e.get("exit_code", 0) != 0]
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
        stdout=True,
        stderr=True,
        demux=True,
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
        compressed,
        max_lines=max_lines,
        verbose=verbose,
        exit_code=new_exit_code,
        stderr=stderr_text,
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
        stdout=True,
        stderr=True,
        demux=True,
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
        diff_lines = list(
            difflib.unified_diff(
                prev_lines,
                curr_lines,
                fromfile="previous",
                tofile="current",
                lineterm="",
            )
        )
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
            compressed,
            max_lines=100,
            verbose=verbose,
            exit_code=exit_code,
            stderr=stderr_text,
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
        plan_services.append(
            {
                "name": svc["name"],
                "image": svc.get("image", "unknown"),
                "ports": svc.get("ports", {}),
                "depends_on": svc.get("depends_on", []),
            }
        )

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
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Failed to create network {network_name}: {e}",
                    "plan": plan,
                }
            )

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
                if result and "error" not in result:
                    started_services.append(result)
                    started_names.add(svc["name"])
                else:
                    started_services.append({"name": svc["name"], "error": "unresolvable dependency"})

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
        return json.dumps(
            {
                "status": "error",
                "error": str(e),
                "plan": plan,
                "services": started_services,
            },
            ensure_ascii=False,
        )


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
        return json.dumps(
            {
                "status": "ok",
                "environment_id": environment_id,
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "status": "error",
                "error": str(e),
            }
        )


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
                    return json.dumps(
                        {
                            "status": "error",
                            "error": "port is required for tcp condition",
                        }
                    )
                ready = _health_check_tcp(target, port, timeout=min(interval, 5.0))

            elif condition_type == "http":
                ready = _health_check_http(target, timeout=min(interval, 5.0))

            elif condition_type == "log":
                if not container_id or not log_pattern:
                    return json.dumps(
                        {
                            "status": "error",
                            "error": "container_id and log_pattern required for log condition",
                        }
                    )
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
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Unknown condition_type: {condition_type}. Supported: tcp, http, log",
                    }
                )

            if ready:
                elapsed = round(time.time() - start, 2)
                return json.dumps(
                    {
                        "status": "ready",
                        "condition_type": condition_type,
                        "target": target,
                        "port": port,
                        "elapsed": elapsed,
                        "attempts": attempts,
                    }
                )

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
