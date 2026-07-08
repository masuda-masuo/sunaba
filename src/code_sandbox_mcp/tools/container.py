"""Container lifecycle tools: init, stop, exec, test environment."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import re
import shlex
import tarfile
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, NamedTuple

from docker.errors import APIError, NotFound
from fastmcp import Context
from pydantic import BeforeValidator

from code_sandbox_mcp import image_pins, image_selection, proxy_lifecycle
from code_sandbox_mcp.journal import (
    get_session_label,
    read_container_states,
    read_journal,
    record_boundary_crossing,
    record_copy,
    record_initialize,
    record_initialize_complete,
    record_stop,
    set_session_label,
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
from code_sandbox_mcp.proxy_client import authorized_read_grant
from code_sandbox_mcp.security import (
    CREATED_AT_LABEL,
    MANAGED_LABEL,
    NAME_LABEL,
    _detect_host_resources,
    _parse_mem_to_mb,
    build_secure_run_kwargs,
    get_default_profile,
    validate_image_ref,
)
from code_sandbox_mcp.tools.common import (
    CLONE_NO_TOKEN_WARNING,
    RECOVERY_DOCKER_TIMEOUT,
    _build_clone_command,
    _coerce_list_arg,
    _docker,
)
from code_sandbox_mcp.tools.vcs import (
    _resolve_vcs_token,
    checkpoint_list,
    resolve_git_root,
)

logger: logging.Logger = logging.getLogger(__name__)

# -- Default sandbox images (Issue #313) ---------------------------------
# Image selection is detection-based: when ``image`` is not given, the
# project's language is detected host-side (before container start) and the
# matching variant image is chosen.  No language is hardcoded as "the
# default" -- "Python is the default" only made sense because this repo
# happens to be Python (see code_sandbox_mcp.image_selection).
#
# The digest pins live as data in ``code_sandbox_mcp/image_pins.json``; CI
# (``.github/workflows/build-sandbox-variants.yml``) rewrites that file after
# each variant build, then verifies this loader returns the new digest.  This
# replaces the old ``sed``-on-source approach that broke silently when the
# constants moved or were reformatted (#214 / #331).  All refs are digest-pinned
# per ``docs/design-multilang-support.md`` section 6.
_image_pins: dict[str, str] = image_pins.load_image_pins()
_NEUTRAL_IMAGE: str = _image_pins["neutral"]
_PYTHON_IMAGE: str = _image_pins["python"]
_GO_IMAGE: str = _image_pins["go"]

#: Neutral fallback used when detection is inconclusive (unknown / unsupported
#: / py+go polyglot) and for bare ``sandbox_initialize()`` with nothing to
#: inspect.  Overridable via the ``--default-image`` CLI flag (server.py).
_DEFAULT_IMAGE: str = _NEUTRAL_IMAGE

#: Detected-language -> variant-image map consumed by image_selection.
_LANGUAGE_IMAGE_MAP: dict[str, str] = {
    "python": _PYTHON_IMAGE,
    "go": _GO_IMAGE,
}


_SHIORI_REPOS_PATH: str | None = None

_CLONE_REPO_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9._-]+$")

#: Hard cap ratio for per-call mem_limit override (Issue #201).
#: Override values exceeding this fraction of host memory are rejected.
_HARD_CAP_RATIO: float = 0.9

#: Grace period before an init-incomplete container is considered orphaned
#: and reaped (Issue #298).  Comfortably longer than any legitimate
#: ``sandbox_initialize`` so an in-progress init (possibly in a concurrent
#: session) is never mistaken for an orphan.
_ORPHAN_GRACE_SECONDS: int = 600

#: How often the async ``sandbox_initialize`` emits a progress notification to
#: keep the MCP/HTTP connection alive during slow setup (Issue #298).  Must be
#: shorter than the client's request timeout (~60s).
_PROGRESS_INTERVAL_SECONDS: float = 15.0

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


def _resolve_image_ref(image: str) -> str:
    """Resolve a tag-based image reference to a digest-based one.

    If *image* already contains a ``@sha256:...`` digest, return it
    as-is.  Otherwise pull the image by tag and extract its digest
    from the local Docker metadata, returning ``image@sha256:...``.

    This allows callers to pass variant tags (e.g. ``sandbox:go``)
    instead of requiring a fully-qualified digest every time.
    """
    # Already a digest reference — nothing to resolve
    if re.search(r"@sha256:[a-f0-9]{64}$", image):
        return image

    _ensure_image(image)
    try:
        img = _docker().images.get(image)
    except Exception as e:
        raise ValueError(
            f"Could not resolve digest for image: {image!r}: {e}"
        )

    for repo_digest in img.attrs.get("RepoDigests") or []:
        if "@sha256:" in repo_digest:
            logger.info("Resolved image %s → %s", image, repo_digest)
            return repo_digest

    raise ValueError(
        f"Could not resolve digest for image: {image!r}. "
        "The image may not have been pushed to a registry."
    )


def _ensure_image(image: str) -> None:
    """Ensure the specified Docker image is available locally.

    Calls ``docker pull`` to fetch the image if not already present.
    """
    import docker.errors

    import docker

    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        logger.info("Pulling image %s...", image)
        client.images.pull(image)


def prewarm_default_image() -> None:
    """Pull the default and language-variant images so first use is warm.

    A cold-start image pull can exceed the MCP/HTTP request timeout, so the
    first ``sandbox_initialize`` fails even though the pull finishes in the
    background and the next call succeeds (Issue #303).  Pulling the images
    ahead of time — at server startup and periodically — removes that
    first-call cliff and does not depend on progress notifications keeping the
    connection alive.

    Originally this only pulled the neutral default image, but
    ``_select_initial_image`` can also pick the ``python`` or ``go`` variant
    based on language detection — those were never prewarmed, so detection
    routinely traded one cold pull (neutral) for another (variant).  Pull
    all three so detection never hits a cold image.

    Reads the module-level :data:`_DEFAULT_IMAGE` at call time so a
    ``--default-image`` override applied before the prewarm thread starts is
    honoured.  Any failure (registry hiccup, Docker down) is swallowed per
    image so one bad pull never blocks the others or startup; the next
    refresh cycle retries.
    """
    images = {_DEFAULT_IMAGE, _PYTHON_IMAGE, _GO_IMAGE}
    for image in images:
        try:
            _ensure_image(image)
            logger.info("prewarmed sandbox image %s", image)
        except Exception:  # noqa: BLE001 - prewarm must never break startup
            logger.exception("prewarm of image %s failed", image)


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


def _shiori_preclone_root(repo: str | None) -> Path | None:
    """Return the host path of a Shiori pre-clone for *repo*, if present.

    Used by detection-based image selection to inspect a project's root
    markers *before* the container starts, without touching the network.
    """
    if not repo or not _SHIORI_REPOS_PATH:
        return None
    repos_root = Path(_SHIORI_REPOS_PATH).resolve()
    clone_path = (repos_root / repo).resolve()
    # Stay under the configured repos root (path-traversal guard) and require a
    # real git clone, mirroring _shiori_preclone_exists.
    try:
        clone_path.relative_to(repos_root)
    except ValueError:
        return None
    if clone_path.is_dir() and (clone_path / ".git").is_dir():
        return clone_path
    return None


def _select_initial_image(
    image: str | None,
    clone_repo: str | None,
    repo: str | None,
    pr: int | None,
) -> tuple[str, str | None]:
    """Choose the image (and optional notice) for a new container (Issue #313).

    An explicit *image* wins.  Otherwise the project's language is detected
    host-side -- from a Shiori pre-clone when available, else the GitHub API
    when a repo is being cloned -- and the matching variant image is selected,
    falling back to the neutral ``_DEFAULT_IMAGE`` when detection is
    inconclusive.  Detection never blocks init: any failure degrades to the
    neutral image.
    """
    target_repo = clone_repo or repo
    # A GitHub probe is only worthwhile (and only authorized) when we are
    # actually fetching that repo over the network.
    network_clone = bool(target_repo) and (
        pr is not None or (clone_repo is not None and not _shiori_preclone_exists(clone_repo))
    )
    # Everything below is best-effort: token minting, the preclone scan, and
    # the GitHub probe can all raise, and none of them may block init.  Keep
    # them inside the guard so any failure degrades to the neutral image.
    try:
        token: str | None = None
        if network_clone:
            token = _resolve_vcs_token() or None
        return image_selection.resolve_initial_image(
            explicit_image=image,
            target_repo=target_repo,
            preclone_root=_shiori_preclone_root(target_repo),
            token=token,
            language_image_map=_LANGUAGE_IMAGE_MAP,
            neutral_image=_DEFAULT_IMAGE,
            allow_network_detection=network_clone,
        )
    except Exception as e:  # pragma: no cover - defensive: never block init
        logger.warning("image detection failed, using neutral default: %s", e)
        return (image or _DEFAULT_IMAGE), None


def _write_clone_meta(container: Any, clone_path: str, base_branch: str | None = None) -> None:
    """Write clone destination path into container metadata.

    The metadata file (``/home/sandbox/.sandbox-meta.json``) is read by
    :func:`resolve_git_root` to auto-detect the git repository root
    regardless of the ``clone_dest`` value.

    When *base_branch* is given (e.g. from a PR checkout), it is stored
    as ``base_branch`` so that :func:`diff_in_container` can resolve the
    default diff base automatically.

    Failures are logged but not propagated so that a metadata write
    failure never breaks an otherwise successful clone.
    """
    try:
        meta: dict[str, str] = {"clone_path": clone_path}
        if base_branch:
            meta["base_branch"] = base_branch
        meta_json = json.dumps(meta)
        safe_meta = shlex.quote(meta_json)
        container.exec_run(
            ["/bin/sh", "-c",
             f"mkdir -p /home/sandbox && printf '%s' {safe_meta} > /home/sandbox/.sandbox-meta.json"],
        )
    except Exception as e:
        logger.warning("Failed to write clone meta: %s", e)


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
        raise ValueError("Shiori repos path is not configured. Set --shiori-repos-path or CODE_SANDBOX_SHIORI_REPOS_PATH env var.")

    _validate_clone_repo(clone_repo)
    repo_name = clone_repo.split("/")[-1]

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
            tar.add(str(resolved_from), arcname=repo_name, filter=_filter_sensitive)
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

    clone_path = f"{clone_dest}/{repo_name}"
    try:
        container.exec_run(
            ["sh", "-c", f"chown -R $(id -u):$(id -g) {shlex.quote(clone_path)}"]
        )
    except Exception as e:
        logger.debug("chown failed for %s: %s", clone_path, e)
    _write_clone_meta(container, clone_path)

    record_copy(
        container_id[:12],
        "clone_shiori_repo",
        str(resolved_from),
        clone_path,
    )

    # -- Run git fetch --unshallow --
    safe_dest = shlex.quote(f"{clone_dest}/{repo_name}")
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

    return f"Copied Shiori clone of {clone_repo} → {clone_dest}/{repo_name} in container {container_id[:12]}"


def _clone_repo_via_network(
    container,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
    authenticated: bool = False,
    *,
    open_read_grant: bool = False,
) -> str:
    """Fallback: clone via ``gh repo clone`` when Shiori is unavailable (Issue #146).

    Requires network access and ``gh`` installed in the container.

    Note on private repositories (Issue #146, PR #170 review):
        Network access is auto-enabled for ``clone_repo``, but the VCS
        token is *not* auto-injected.  Public repos (the common case)
        clone fine without credentials via an anonymous ``git clone`` (Issue #333), and injecting the token
        unconditionally would expose it to in-container code for no
        benefit, violating the host-permission-minimization principle.
        Private repos are authenticated at the network layer by the
        egress proxy's read-authorization grant (#419,
        *open_read_grant*), so no token ever enters the container.

    *open_read_grant*, when ``True``, wraps the clone exec in a
    short-lived egress-proxy read-authorization grant (#419) with a
    host-resolved token, so the proxy authenticates this anonymous
    ``git clone`` transparently.  Used under the egress proxy, where the
    container itself never receives a token (#356).
    """
    # Not redundant with the callers: on the network-fallback path the
    # caller may skip _clone_shiori_repo_to_container entirely (when
    # _SHIORI_REPOS_PATH is unset) or that function raises before reaching
    # _validate_clone_repo (when the specific pre-clone is absent, Issue #178).
    # Either way this is the only validation on the network-fallback path.
    _validate_clone_repo(clone_repo)
    repo_name = clone_repo.split("/")[-1]
    clone_path = clone_dest.rstrip("/") + "/" + repo_name
    cmd = _build_clone_command(clone_repo, clone_path, authenticated=authenticated)
    if open_read_grant:
        with authorized_read_grant(clone_repo, token=_resolve_vcs_token() or None):
            exit_code, output = container.exec_run(["/bin/sh", "-c", cmd])
    else:
        exit_code, output = container.exec_run(["/bin/sh", "-c", cmd])
    if exit_code != 0:
        detail = output.decode("utf-8", errors="replace").strip()
        hint = ""
        if not authenticated:
            hint = " (private repo? the egress proxy needs a host-resolvable token to authenticate the read grant)"
        transport = "anonymous git clone" if not authenticated else "gh repo clone"
        # Record the egress-proxy read-grant's outcome (#421): this is the
        # boundary crossing publish's push grant already gets recorded for
        # (#356/#357), applied to the read side (#419) so a denied/failed
        # proxy-authorized clone shows up in the journal too, not just a
        # successful one.
        if open_read_grant:
            record_boundary_crossing(
                container_id[:12],
                "clone_repo",
                f"repo={clone_repo} dest={clone_path} proxy_read_grant=True",
                approved=False,
            )
        raise RuntimeError(f"{transport} failed (exit {exit_code}): {detail}{hint}")
    if open_read_grant:
        record_boundary_crossing(
            container_id[:12],
            "clone_repo",
            f"repo={clone_repo} dest={clone_path} proxy_read_grant=True",
            approved=True,
        )
    _write_clone_meta(container, clone_path)
    return "Cloned {} via network into {} in container {}".format(clone_repo, clone_path, container_id[:12])


class CloneResult(NamedTuple):
    """Return value of :func:`_try_clone_into_container`."""

    msg: str | None
    error: str | None


def _editable_install_cmd(target: str, pip_args: str = "") -> str:
    """Build a shell command that pip-installs *target* (e.g. ``".[dev]"``).

    The installer is chosen at runtime inside the container (#390): images
    with the persistent sandbox-owned venv (PR #388) set ``$VIRTUAL_ENV``,
    where ``uv pip install`` works and is much faster.  Venv-less images
    (older pins, custom images) keep plain ``pip``, whose user-site
    (``~/.local``) fallback is the only working path for a non-root user —
    uv has no ``--user``, ``--system`` hits root-owned site-packages, and
    the former mktemp-venv workaround discarded the install (#380 / #383).

    Args:
        target: Package target string (e.g. ``".[dev]"``).
        pip_args: Additional pip arguments (e.g. ``"--index-url https://..."``).
    """
    quoted = shlex.quote(target)
    args_part = ""
    if pip_args:
        tokens = shlex.split(pip_args)
        args_part = " " + " ".join(shlex.quote(t) for t in tokens)
    return (
        'if [ -n "$VIRTUAL_ENV" ] && command -v uv >/dev/null 2>&1; '
        f"then uv pip install -q -e {quoted}{args_part}; "
        f"else pip install -e {quoted} -q{args_part}; fi"
    )


def _run_pip_install(
    container: Any,
    clone_repo: str,
    clone_dest: str,
    pip_extras: str,
    allow_network: bool = True,
    pip_args: str | None = None,
) -> None:
    """Run pip install inside the container after a successful clone.

    Args:
        container: Docker container object.
        clone_repo: Repository in ``"owner/name"`` format.
        clone_dest: Destination directory for the clone.
        pip_extras: Pip extras string (e.g. ``"[dev]"``).
        allow_network: Whether the container has network access.  PyPI is
            unreachable without it, so the install would just hang until pip's
            own connect timeout fires; skip it instead.
        pip_args: Additional pip arguments (e.g. ``"--index-url https://..."``).
            Ignored when the caller passes ``pip_extras=None`` (pip install skipped).
    """
    if not allow_network:
        logger.info(
            "Skipping pip install for %s: container has no network access",
            clone_repo,
        )
        return
    repo_name = clone_repo.split("/")[-1]
    safe_dest = shlex.quote(f"{clone_dest}/{repo_name}")
    local_pip_args = pip_args or ""
    install_cmd = f"cd {safe_dest} && {_editable_install_cmd(f'.{pip_extras}', local_pip_args)}"
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


def _try_clone_into_container(
    container: Any,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
    authenticated: bool = False,
    *,
    open_read_grant: bool = False,
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
                authenticated,
                open_read_grant=open_read_grant,
            )
        else:
            msg = _clone_shiori_repo_to_container(
                container,
                container_id,
                clone_repo,
                clone_dest,
            )
        if not authenticated and not open_read_grant:
            # Anonymous clone with no proxy read grant (only reachable
            # proxy-off): warn that a private repo would have failed and
            # that this checkout is read-only (Issue #333).
            msg = f"{msg} — WARNING: {CLONE_NO_TOKEN_WARNING}"
        return CloneResult(msg, None)
    except Exception as e:
        logger.warning("Clone failed: %s", e)
        return CloneResult(None, str(e))


def _resolve_pr_head_ref(repo: str, pr_number: int, *, token: str | None = None) -> tuple[str, str]:
    """Resolve a PR's head and base branch names host-side via the GitHub REST API.

    Returns ``(head_ref, base_ref)`` tuple.

    A proxied container deliberately holds no VCS token (#356), so it cannot
    run the authenticated ``gh pr view`` the gh checkout path relies on.  The
    host process *can* authenticate, and this metadata read goes out directly
    from the host (not through the sandbox egress proxy), so resolving the
    head ref here and handing only the branch name to the container keeps the
    container credential-free (#403).

    The host token (broker mint -> static env, the same resolution publish
    uses for lazy push injection) is attached when available; anonymous
    requests still work for public repos.  The REST API accepts ``Bearer`` --
    the Basic-only quirk is specific to GitHub's git smart-HTTP endpoints
    (see ``proxy.basic_auth_header``).

    *token*, when given, is used as-is instead of calling
    :func:`_resolve_vcs_token` again.  A broker-backed resolution spawns a
    subprocess per call (no caching, up to a 30s timeout) -- callers that
    already resolved a token for this same operation (e.g.
    :func:`_setup_pr_branch`, which also needs one for the egress-proxy read
    grant) should pass it through here instead of paying for a second mint.
    """
    import urllib.error
    import urllib.request

    # Also makes interpolating *repo* into the URL injection-safe.
    _validate_clone_repo(repo)
    url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "code-sandbox-mcp",
    }
    if token is None:
        token = _resolve_vcs_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (403, 429):
            # Anonymous requests share the 60 req/h per-IP budget, so this
            # is the failure mode a token-less host hits first.
            hint = (
                " (possibly rate-limited: anonymous GitHub API requests are"
                " capped at 60/h per IP; configuring a host token raises it)"
            )
        raise RuntimeError(
            f"Failed to resolve head ref for PR #{pr_number} in {repo}:"
            f" GitHub API returned HTTP {e.code}{hint}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to resolve head ref for PR #{pr_number} in {repo}: {e.reason}"
        ) from e
    head_ref = (data.get("head") or {}).get("ref") or ""
    base_ref = (data.get("base") or {}).get("ref") or ""
    if not head_ref:
        raise RuntimeError(
            f"GitHub API returned no head ref for PR #{pr_number} in {repo}"
        )
    if not base_ref:
        raise RuntimeError(
            f"GitHub API returned no base ref for PR #{pr_number} in {repo}"
        )
    return head_ref, base_ref


def _setup_pr_branch(
    container: Any,
    container_id: str,
    repo: str,
    pr_number: int,
    clone_dest: str,
    pip_extras: str | None = "[dev]",
    *,
    authenticated: bool = True,
    open_read_grant: bool = False,
    pip_args: str | None = None,
) -> str:
    """Clone repo and check out PR branch inside the container.

    Two transports, chosen by *authenticated* (mirroring
    :func:`_build_clone_command`):

    - **authenticated** (the container env carries a VCS token): ``gh``
      fetches the PR info, clones, and checks out the head branch.
    - **anonymous** (#403, e.g. a proxied container that deliberately holds
      no token per #356): the head ref is resolved host-side via
      :func:`_resolve_pr_head_ref` and everything in the container is plain
      git -- ``refs/pull/N/head`` lives on the base repo, so this covers
      fork PRs too.  Pass *open_read_grant* to make this work for private
      repositories too (see below); without it, private-repo PRs cannot be
      checked out this way.

    Args:
        container: Docker container object.
        container_id: 12-char container ID prefix.
        repo: Repository in ``"owner/name"`` format.
        pr_number: Pull request number.
        clone_dest: Destination directory inside the container.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
            Pass ``None`` to skip pip install entirely.
        authenticated: Whether the container env actually carries a VCS
            token (ground truth: the env actually built, #356).
        pip_args: Additional pip arguments (e.g. ``"--index-url https://..."``).
            Ignored when *pip_extras* is ``None`` since pip install is skipped entirely.
        open_read_grant: When ``True`` and *authenticated* is ``False``,
            wrap the anonymous clone + fetch/checkout in a short-lived
            egress-proxy read-authorization grant (#419) with a
            host-resolved token -- the same mechanism
            :func:`_clone_repo_via_network` already uses for
            ``clone_repo``, extended here so ``pr=N`` also works for
            private repos under the proxy instead of only public ones.

    Returns:
        Success message string.
    """
    cid = container_id[:12]
    repo_name = repo.split("/")[-1]
    safe_dest = shlex.quote(f"{clone_dest}/{repo_name}")
    safe_repo = shlex.quote(repo)

    anon_token: str | None = None
    if authenticated:
        # Step 1: Get PR head branch info via in-container gh
        gh_info_cmd = f"gh pr view {pr_number} --repo {safe_repo} --json headRefName,baseRefName"
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
        base_ref = pr_info.get("baseRefName", "")
        if not head_ref:
            raise RuntimeError(f"Incomplete PR info: head_ref={head_ref!r}")
        if not base_ref:
            raise RuntimeError(f"Incomplete PR info: base_ref={base_ref!r}")

        # Clone the base repo (not head/fork) so that gh pr checkout
        # works correctly with the PR number from the base repository.
        clone_cmd = f"gh repo clone {safe_repo} {safe_dest}"
        checkout_cmd = f"cd {safe_dest} && gh pr checkout {pr_number}"
    else:
        # Step 1 (anonymous): the head ref comes from the host, and the
        # container-side commands below are token-free git.  The clone
        # command's quoting is delegated to _build_clone_command; safe_repo /
        # safe_dest above still serve the shared logging and checkout below.
        # Resolved once and reused for the read grant below: a broker-backed
        # _resolve_vcs_token() spawns a subprocess per call (no caching, up
        # to a 30s timeout), so calling it twice per checkout would double
        # that cost -- and, if the broker mints a fresh token each time,
        # hand the head-ref lookup and the read grant two different tokens
        # for no benefit (#436 review).
        anon_token = _resolve_vcs_token()
        head_ref, base_ref = _resolve_pr_head_ref(repo, pr_number, token=anon_token or None)
        clone_cmd = _build_clone_command(repo, f"{clone_dest}/{repo_name}")
        checkout_cmd = (
            f"cd {safe_dest} && git fetch origin pull/{pr_number}/head"
            f" && git checkout -B {shlex.quote(head_ref)} FETCH_HEAD"
        )

    # Step 2 + 3: Clone the base repo, then checkout the PR branch.  Both
    # are anonymous git operations against the same repo when *authenticated*
    # is False, so a single read-authorization grant (#419) covers both --
    # opened only when the caller asked for it (open_read_grant) and the
    # container actually has no token (authenticated is False); a no-op
    # nullcontext otherwise so the authenticated/public-repo paths are
    # unaffected.
    grant_open = open_read_grant and not authenticated
    grant = (
        authorized_read_grant(repo, token=anon_token or None)
        if grant_open
        else contextlib.nullcontext()
    )
    journal_detail = f"repo={repo} pr=#{pr_number} dest={safe_dest} proxy_read_grant=True"
    try:
        with grant:
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
                hint = (
                    ""
                    if authenticated
                    else (
                        " (anonymous clone: the container holds no VCS token (#356);"
                        " if this is a private repository, ensure the egress proxy has a"
                        " host-resolvable token for the read-authorization grant, see #419)"
                    )
                )
                raise RuntimeError(f"Failed to clone repo {repo}: {stderr_text or clone_output}{hint}")

            logger.info("Cloned %s → %s in container %s", safe_repo, safe_dest, cid)

            # Step 3: Checkout PR branch
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
    except Exception:
        # Mirror _clone_repo_via_network's boundary-crossing journal entry
        # (#421) for the read side (#419): a denied/failed proxy-authorized
        # PR checkout must show up in the journal too, not just a successful
        # one.
        if grant_open:
            record_boundary_crossing(cid, "setup_pr_branch", journal_detail, approved=False)
        raise
    if grant_open:
        record_boundary_crossing(cid, "setup_pr_branch", journal_detail, approved=True)

    logger.info(
        "Checked out PR #%s (%s) → %s in container %s",
        pr_number,
        head_ref,
        safe_dest,
        cid,
    )

    # Step 4: Install dev dependencies (non-fatal); the installer is
    # chosen at runtime by _editable_install_cmd (#390); network is always
    # available here since sandbox_initialize forces allow_network=True
    # whenever pr is set.
    local_pip_args = pip_args or ""
    if pip_extras is not None:
        install_cmd = f"cd {safe_dest} && {_editable_install_cmd(f'.{pip_extras}', local_pip_args)}"
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

    _write_clone_meta(container, safe_dest, base_branch=base_ref)

    record_copy(
        cid,
        "setup_pr_branch",
        f"repo={repo} pr=#{pr_number} branch={head_ref}",
        safe_dest,
    )

    return f"PR #{pr_number} ({head_ref}) → {clone_dest}/{repo_name} in container {cid}"


def _age_seconds(iso_ts: str | None, now: datetime) -> float | None:
    """Return seconds elapsed since *iso_ts*, or ``None`` if unparseable."""
    if not iso_ts:
        return None
    try:
        created = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (now - created).total_seconds()


def _journal_container_status() -> dict[str, dict[str, Any]]:
    """Summarise per-container lifecycle status from the journal.

    Returns a mapping ``container_id -> {complete, used, stopped, init_ts}``
    where *complete* means an ``initialize_complete`` event was seen and
    *used* means at least one ``exec``.  Containers with an explicit
    ``stop`` are pruned from the mapping entirely (absence = no lifecycle
    info), so ``stopped`` is never ``True`` — the key survives only for
    interface stability.
    """
    return read_container_states()


def _reap_orphaned_init_containers(client: Any = None) -> list[str]:
    """Stop & remove containers orphaned by a timed-out ``sandbox_initialize``.

    Best-effort, opportunistic GC (Issue #298).  A container is reaped only
    when *all* of these hold, so healthy / in-progress containers are never
    touched:

    - it carries our management label *and* the ``created_at`` label, i.e. it
      was created by ``sandbox_initialize`` (test-environment and other
      managed containers lack ``created_at`` and are skipped);
    - the journal shows no ``initialize_complete`` (setup never finished), no
      ``exec`` (never used), and no ``stop``;
    - it is older than :data:`_ORPHAN_GRACE_SECONDS`, so a still-running init
      (possibly in another session) is never mistaken for an orphan.

    Failures are swallowed: GC must never break the caller's init.  Returns
    the list of reaped container-id prefixes.
    """
    reaped: list[str] = []
    client = client or _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
    except Exception as e:
        logger.warning("orphan reap: failed to list containers: %s", e)
        return reaped
    if not containers:
        return reaped

    status = _journal_container_status()
    now = datetime.now(timezone.utc)
    for container in containers:
        cid = container.id[:12]
        labels = getattr(container, "labels", None) or {}
        created_label = labels.get(CREATED_AT_LABEL)
        if created_label is None:
            # Not a sandbox_initialize container (e.g. test environment) — skip.
            continue
        s = status.get(cid, {})
        if s.get("complete") or s.get("used") or s.get("stopped"):
            # Setup finished, container used, or already stopped — not an orphan.
            continue
        age = _age_seconds(s.get("init_ts") or created_label, now)
        if age is None or age < _ORPHAN_GRACE_SECONDS:
            # Unknown age or still within the grace grant (in-progress init).
            continue
        try:
            container.kill()
        except (NotFound, APIError):
            pass
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except Exception as e:
            logger.warning("orphan reap: failed to remove %s: %s", cid, e)
            continue
        record_stop(cid)
        reaped.append(cid)
        logger.info("Reaped orphaned init container %s (age=%.0fs)", cid, age)
    return reaped


def _find_containers_by_name(client, name: str) -> list[str]:
    """Find running containers with the given NAME_LABEL.

    Returns a list of 12-character container ID prefixes.
    """
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": f"{NAME_LABEL}={name}"},
        )
    except Exception:
        return []
    return [c.id[:12] for c in containers if c.status == "running"]


def sandbox_list_containers() -> str:
    """List all managed sandbox containers with metadata.

    Discovers containers via the ``MANAGED_LABEL`` Docker label so the
    list is accurate even after a server restart.  Returns a JSON array
    of container summaries, each with:

    - ``container_id`` (str): 12-character prefix
    - ``name`` (str | None): user-assigned name from ``sandbox_initialize(name=...)``
    - ``image`` (str): image ref
    - ``status`` (str): Docker container status (``running`` / ``exited`` / etc.)
    - ``created_at`` (str | None): ISO-8601 timestamp from the creation label
    - ``age_seconds`` (float | None): approximate age in seconds

    Use this to discover existing containers across sessions, especially
    when the server restarts and the in-memory registry is lost (Issue #478).

    .. rubric:: Use when

    - Finding a container by name across sessions
    - Checking what containers are still alive before starting a new one
    - Preparing to call :func:`sandbox_attach` with a name or ID

    .. rubric:: Don't use when

    - You already know the container ID — use :func:`sandbox_attach` directly
    - Inspecting journal history — use :func:`sandbox_read_journal` instead

    .. rubric:: Prefer over

    - Prefer over ``sandbox_exec docker ps`` (structured JSON, filters to managed containers)

    Returns:
        JSON string with a ``containers`` array.
    """
    import json

    client = _docker()
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )
    except Exception as e:
        return json.dumps({"containers": [], "error": str(e)})

    now = datetime.now(timezone.utc)
    result: list[dict[str, Any]] = []
    for c in containers:
        cid = c.id[:12]
        labels = getattr(c, "labels", None) or {}
        created_raw = labels.get(CREATED_AT_LABEL)
        name_val = labels.get(NAME_LABEL)
        age = _age_seconds(created_raw, now)
        result.append({
            "container_id": cid,
            "name": name_val,
            "image": c.image.tags[0] if c.image.tags else str(c.image.short_id),
            "status": c.status,
            "created_at": created_raw,
            "age_seconds": age,
        })

    return json.dumps({"containers": result}, ensure_ascii=False)


def sandbox_attach(name_or_id: str, session_label: str | None = None) -> str:
    """Connect to an existing container by name or ID prefix.

    Looks up the container by:

    1. **Name match** — the ``NAME_LABEL`` Docker label (set via
       ``sandbox_initialize(name=...)``)
    2. **ID prefix match** — a 12-character (or longer) container ID prefix

    Args:
        name_or_id: A user-assigned container name (from
            ``sandbox_initialize(name=...)``) or a 12-character (or longer)
            container ID prefix.
        session_label: Optional session identifier string.  When provided,
            this label is recorded in the journal for all subsequent
            operations on this container, replacing any previous label.
            Use this to distinguish operations from different model
            sessions or task contexts (Issue #479).

    Returns a JSON orientation summary:

    - ``found`` (bool): whether the container was located
    - ``container_id`` (str): 12-character prefix
    - ``name`` (str | None): user-assigned name
    - ``status`` (str): Docker status
    - ``image`` (str): image ref
    - ``created_at`` (str | None): ISO-8601 creation time
    - ``session_label`` (str | None): current session label attached to this container
    - ``git``: git orientation when available:
        - ``branch`` (str | None): current git branch
        - ``status_short`` (str | None): ``git status --short`` output
        - ``repo_root`` (str | None): detected git root
    - ``last_checkpoint`` (str | None): most recent checkpoint message
    - ``last_checkpoint_ts`` (str | None): timestamp of last checkpoint
    - ``journal_activity`` (int): number of journal entries for this container
    - ``error`` (str | None): error message on failure

    The orientation summary lets a cold session (or a cheap model) pick up
    where a previous session left off without re-reading the entire context
    (Issue #478).

    .. rubric:: Use when

    - Reconnecting to a named container from a different session
    - Quickly assessing a container's state without running shell commands
    - Picking up work after a server restart

    .. rubric:: Don't use when

    - Starting a new container — use :func:`sandbox_initialize` instead
    - Running commands on an already-attached container — use :func:`sandbox_exec`

    .. rubric:: Prefer over

    - Prefer over ``sandbox_exec`` for orientation (structured summary, no shell)
    - Prefer over manual ``docker ps`` filtering

    Args:
        name_or_id: A user-assigned container name (from
            ``sandbox_initialize(name=...)``) or a 12-character (or longer)
            container ID prefix.

    Returns:
        JSON string with the orientation summary.
    """
    import json

    client = _docker()
    container_obj = None
    cid = ""
    match_type = ""

    # 1) Try name match via NAME_LABEL
    if name_or_id:
        try:
            matches = client.containers.list(
                all=True,
                filters={"label": f"{NAME_LABEL}={name_or_id}"},
            )
        except Exception:
            matches = []
        if matches:
            container_obj = matches[0]
            if len(matches) > 1:
                logger.warning(
                    "sandbox_attach: multiple containers match name %r, using first (%s)",
                    name_or_id, container_obj.id[:12],
                )
            cid = container_obj.id[:12]
            match_type = "name"

    # 2) Try ID prefix match (managed containers only)
    if container_obj is None:
        try:
            all_managed = client.containers.list(
                all=True,
                filters={"label": f"{MANAGED_LABEL}=true"},
            )
            exact_match = [c for c in all_managed if c.id == name_or_id or c.id.startswith(name_or_id)]
            if len(exact_match) > 1:
                ids = [c.id[:12] for c in exact_match]
                return json.dumps({
                    "found": False,
                    "error": f"Ambiguous ID prefix {name_or_id!r} matches multiple containers: {ids}",
                })
            if exact_match:
                container_obj = exact_match[0]
                cid = container_obj.id[:12]
                match_type = "id"
        except Exception:
            pass

    if container_obj is None:
        return json.dumps({
            "found": False,
            "error": f"No managed container found matching {name_or_id!r}",
        })

    labels = getattr(container_obj, "labels", None) or {}
    created_raw = labels.get(CREATED_AT_LABEL)
    name_val = labels.get(NAME_LABEL)
    now = datetime.now(timezone.utc)
    age = _age_seconds(created_raw, now)

    result: dict[str, Any] = {
        "found": True,
        "container_id": cid,
        "name": name_val,
        "status": container_obj.status,
        "image": container_obj.image.tags[0] if container_obj.image.tags else str(container_obj.image.short_id),
        "created_at": created_raw,
        "age_seconds": age,
        "match_type": match_type,
    }

    if session_label is not None:
        set_session_label(cid, session_label)
    current_label = get_session_label(cid)
    if current_label is not None:
        result["session_label"] = current_label

    # --- Git orientation ---
    try:
        working_dir = resolve_git_root(container_obj, None)
        if working_dir:
            result["git"] = {"repo_root": str(working_dir)}
            # Current branch
            ec, out = container_obj.exec_run(
                ["/bin/sh", "-c", "git rev-parse --abbrev-ref HEAD 2>/dev/null || true"],
                workdir=working_dir,
            )
            stdout, _ = (out if isinstance(out, tuple) else (out, b""))
            branch = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            if branch:
                result["git"]["branch"] = branch
            # git status --short
            ec, out = container_obj.exec_run(
                ["/bin/sh", "-c", "git status --short 2>/dev/null || true"],
                workdir=working_dir,
            )
            stdout, _ = (out if isinstance(out, tuple) else (out, b""))
            status_short = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            if status_short:
                result["git"]["status_short"] = status_short
    except Exception as e:
        logger.debug("git orientation failed for %s: %s", cid, e)

    # --- Last checkpoint ---
    try:
        wd = result.get("git", {}).get("repo_root")
        cp_raw = json.loads(checkpoint_list(cid, wd))
        cps = cp_raw.get("checkpoints", [])
        if cps:
            last = cps[0]
            result["last_checkpoint"] = last.get("message", "")
            result["last_checkpoint_ts"] = last.get("date", "")
    except Exception as e:
        logger.debug("checkpoint query failed for %s: %s", cid, e)

    # --- Journal activity count ---
    try:
        entries = read_journal(run_id=None, max_entries=1000)
        activity = [e for e in entries if e.get("container_id") == cid]
        result["journal_activity"] = len(activity)
    except Exception:
        result["journal_activity"] = 0

    return json.dumps(result, ensure_ascii=False)


def sandbox_initialize(
    image: str | None = None,
    allow_network: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = "/tmp/repo",
    repo: str | None = None,
    pr: int | None = None,
    pip_extras: str | None = "[dev]",
    pip_args: str | None = None,
    mem_limit: str | None = None,
    cpus: float | None = None,
    name: str | None = None,
    session_label: str | None = None,
) -> str:
    """Start a new Docker sandbox container.

    The container runs ``sleep infinity`` and stays alive until
    explicitly stopped with :func:`sandbox_stop`.

    Container IDs are returned as short 12-character prefixes for
    use in other tools.

    **One-step init + clone:** pass ``clone_repo`` to avoid a separate
    :func:`clone_repo` call.  For a full one-shot workflow with commands,
    use :func:`run_container_and_exec` which wraps init/exec/stop.

    .. rubric:: Use when

    - Starting a new sandbox container for interactive/iterative work
    - Starting a container with a cloned repo via ``clone_repo``
    - Starting a container with a PR checked out via ``pr=N``
    - When you need a persistent container that stays alive across multiple tool calls

    .. rubric:: Don't use when

    - **One-shot command execution** — use :func:`run_container_and_exec` instead
    - **Cloning into an existing container** — use :func:`clone_repo` instead
    - **Reading file content** — use :func:`read_file_range` instead (no container needed for reading)

    .. rubric:: Prefer over

    - Prefer over :func:`run_container_and_exec` when you need an interactive/persistent container
    - Prefer over separate ``clone_repo`` call — use ``clone_repo`` parameter for one-step init+clone

    .. rubric:: Fallback

    - For one-shot workflows use :func:`run_container_and_exec`
    - For cloning after init use :func:`clone_repo`

    Args:
        image: Docker image to use (e.g. ``python@sha256:...``).
               Defaults to the image specified
               via the ``--default-image`` CLI argument in the server config.
        allow_network: Whether to allow network access (default ``False``).
               Set to ``True`` for VCS operations (git/gh) that need to
               reach GitHub API.  Network access is a boundary-crossing
               operation and should be used only when necessary.
        clone_repo: Optional ``owner/name`` repository to copy from the
               Shiori pre-cloned repos on the host into the container.
               Uses the host path configured via ``--shiori-repos-path``
               (default: ``None`` = no clone copy).  When Shiori is not
               configured, falls back to ``gh repo clone`` over the
               network (``allow_network`` is auto-enabled).  A *private*
               repo clones transparently too: the egress proxy opens a
               read-authorization grant (#419) authenticated with a
               host-resolved token, so no credential enters the container.
        clone_dest: Destination directory in the container for the
               cloned repository (default: ``/tmp/repo``).
               The actual path will be ``{clone_dest}/{repo_name}`` where *repo_name* is derived from *clone_repo* or *repo*.
        repo: Repository in ``"owner/name"`` format.
               Required when *pr* is specified.
        pr: Pull request number to clone and check out.
               When set, implicitly enables ``allow_network=True``,
               clones the repository
               inside the container, checks out the PR head branch,
               and installs dev dependencies.  Under the egress proxy
               (#403) this checkout is anonymous: the PR head ref is
               resolved host-side and the container never receives a
               token.  This works for public repos with no further
               setup; for a private repo, the same read-authorization
               grant (#419) that ``clone_repo`` uses is opened for the
               anonymous clone + checkout, so no extra steps are needed
               there either -- the egress proxy must simply be
               configured with a host-resolvable token (broker /
               ``GITHUB_TOKEN``) for the grant to actually authenticate.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
               Pass ``None`` to skip pip install entirely.  Also used when
               *clone_repo* is specified, and skipped automatically (with a
               log message) when the container has no network access, since
               PyPI would be unreachable.
        pip_args: Additional pip arguments (e.g. ``"--index-url https://download.pytorch.org/whl/cpu"``).
            Ignored when *pip_extras* is ``None`` since pip install is skipped entirely.
        mem_limit: Optional memory-limit override (e.g. ``"2g"``).
               The default profile caps containers at 512MB with swap
               disabled, which OOM-thrashes heavy installs (e.g. torch)
               into an unhealthy state (Issue #181).  Raise this when a
               workload legitimately needs more.  ``memswap_limit`` is
               automatically pinned to this value so swap stays
               disabled at the new ceiling.
        cpus: Optional CPU-limit override in cores (e.g. ``2.0``).
               Defaults to the profile's 0.5-core cap when omitted.
        session_label: Optional session identifier string.  When provided,
               this label is recorded in the journal for all subsequent
               operations on this container, replacing any previous label.
               Use this to distinguish operations from different model
               sessions or task contexts (Issue #479).
        name: Optional user-assigned name for the container (e.g.
               ``\"issue-123\"``).  Stored as a Docker label so it survives
               server restarts.  When a container with the same *name*
               already exists and is still running, the call returns an
               error to prevent accidental name collisions (Issue #478).

    The image must be pulled locally before use: docker pull <image>

    Returns:
        Container ID string (12-character prefix).
        If *clone_repo* is specified, a message about the clone copy
        is appended (and dev install if pip_extras is set).
        If *pr* is specified, a message about the PR branch setup
        is appended.

    See also:
        :func:`run_container_and_exec` — one-shot init + exec + stop.
        :func:`clone_repo` — clone after container is running.
    """
    # Opportunistic GC (Issue #298): clean up any containers orphaned by a
    # previously timed-out init before creating a new one.  Best-effort —
    # never let cleanup failure abort the init.
    try:
        _reap_orphaned_init_containers()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("orphan reap failed: %s", e)

    # When pr is specified, implicitly enable network access.
    if pr is not None:
        allow_network = True

    # Auto-enable network when pre-clone is absent (Issue #146, #178)
    if clone_repo and pr is None and not _shiori_preclone_exists(clone_repo):
        allow_network = True
        logger.info(
            "clone_repo=%r: pre-clone absent, auto-enabling network access",
            clone_repo,
        )

    client = _docker()
    # Name collision check (Issue #478): reject if a container with the
    # same user-assigned name is already running.
    if name:
        existing = _find_containers_by_name(client, name)
        if existing:
            return f"Error: a container named {name!r} already exists ({existing[0][:12]}); use a different name or sandbox_attach to connect to it"

    # Detection-based image selection (Issue #313): when no image is given,
    # pick the variant that matches the project's language instead of a
    # hardcoded default.  *image_notice* explains any neutral fallback.
    resolved, image_notice = _select_initial_image(
        image, clone_repo, repo, pr
    )
    # -- Egress proxy sidecar (#358, Epic #353): opt-in, fail closed --
    # A proxied container gets no VCS token in its env (#356); publish
    # hands the credential to the proxy per push grant instead.
    proxied = allow_network and proxy_lifecycle.egress_proxy_enabled()
    env: dict[str, str] = {}
    proxy_runtime = None
    if proxied:
        try:
            proxy_runtime = proxy_lifecycle.ensure_egress_proxy(client)
        except Exception as e:
            return f"Error: egress proxy is enabled but unavailable (failing closed): {e}"
        env.update(proxy_lifecycle.sandbox_proxy_env(proxy_runtime))

    try:
        resolved = _resolve_image_ref(resolved)
        validate_image_ref(resolved)
    except ValueError as e:
        return f"Error: {e}"

    profile = replace(get_default_profile(), allow_network=allow_network)

    # Resource overrides (Issue #181): the default 512MB / 0.5-CPU /
    # no-swap profile is too small for heavy installs (e.g. torch), which
    # OOM-thrash the container into an unhealthy state.  Let callers raise
    # the ceiling when they know they need it.  memswap is pinned to
    # mem_limit so swap stays disabled at the new ceiling (docker also
    # requires memswap_limit >= mem_limit).
    # Hard cap validation (Issue #201): per-call override cannot exceed
    # host resource limits.
    resource_overrides: dict[str, Any] = {}
    host_mb, host_cpus = 0, 0
    if mem_limit is not None or cpus is not None:
        host_mb, host_cpus = _detect_host_resources()
    if mem_limit is not None:
        if host_mb > 0:
            requested_mb = _parse_mem_to_mb(mem_limit)
            cap_mb = int(host_mb * _HARD_CAP_RATIO)
            if requested_mb > cap_mb:
                return (
                    f"Error: mem_limit {mem_limit} exceeds host cap "
                    f"({_HARD_CAP_RATIO:.0%} of host memory)"
                )
        resource_overrides["mem_limit"] = mem_limit
        resource_overrides["memswap_limit"] = mem_limit
    if cpus is not None:
        if cpus <= 0:
            return "Error: cpus must be > 0"
        if host_cpus and cpus > host_cpus:
            return "Error: cpus exceeds host CPU count"
        resource_overrides["cpu_quota"] = int(cpus * profile.cpu_period)

    # Stamp the creation time so the orphan reaper can age a container even if
    # this call times out before any journal entry is written (Issue #298).
    # Also stamp the user-assigned name label when provided (Issue #478).
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    labels: dict[str, str] = {CREATED_AT_LABEL: created_at}
    if name:
        labels[NAME_LABEL] = name
    run_kwargs = build_secure_run_kwargs(
        profile,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
        labels=labels,
        **resource_overrides,
    )
    if proxy_runtime is not None:
        run_kwargs = proxy_lifecycle.apply_network(run_kwargs, proxy_runtime)

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
        mem_limit=mem_limit,
        cpus=cpus,
        session_label=session_label,
    )

    # The CA must be trusted before anything in the sandbox (starting with
    # the clone below) speaks TLS through the proxy.  Fail closed: a sandbox
    # whose trust store could not be wired is torn down, not handed out.
    if proxy_runtime is not None:
        try:
            proxy_lifecycle.install_ca(container, proxy_runtime.ca_pem)
        except Exception as e:
            try:
                container.remove(force=True)
            finally:
                record_stop(cid)
            return f"Error: egress proxy CA install failed (failing closed): {e}"

    # -- Clone: Shiori fast-path, network fallback (Issue #84, #146) --
    # When pr is set, _setup_pr_branch handles its own clone,
    # so skip the Shiori clone copy to avoid redundant clone.
    clone_msg = ""
    # The container never carries a VCS token (#356/#439): its egress runs
    # through the proxy, so the network clone always takes the anonymous
    # git-clone path (#403).
    container_has_token = "GITHUB_TOKEN" in env or "GH_TOKEN" in env
    # Authenticate private reads at the proxy via a read-authorization
    # grant (#419) whenever the container is networked; public reads are
    # unaffected.
    open_read_grant = proxied and not container_has_token
    if clone_repo and pr is None:
        msg, err = _try_clone_into_container(
            container,
            cid,
            clone_repo,
            clone_dest,
            container_has_token,
            open_read_grant=open_read_grant,
        )
        if err is not None:
            clone_msg = f" (clone_repo failed: {err})"
        else:
            clone_msg = " " + (msg or "")
        if pip_extras is not None and err is None:
            _run_pip_install(
                container, clone_repo, clone_dest, pip_extras, allow_network, pip_args
            )
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
                # A token-free container (e.g. proxied, #356) takes the
                # anonymous checkout path: head ref resolved host-side,
                # plain git in the container (#403).  open_read_grant
                # (already computed above for the clone_repo path) lets
                # this anonymous path work for private repos too (#419),
                # instead of only public ones.
                pr_msg = " " + _setup_pr_branch(
                    container,
                    cid,
                    repo,
                    pr,
                    clone_dest,
                    pip_extras,
                    authenticated=container_has_token,
                    open_read_grant=open_read_grant,
                    pip_args=pip_args,
                )
            except Exception as e:
                # PR setup failure is non-fatal: the container is still usable.
                logger.warning("PR branch setup failed: %s", e)
                pr_msg = f" (pr setup failed: {e})"

    # All setup phases finished — mark the container as a completed, usable
    # init so the orphan reaper never touches it (Issue #298).  Clone / PR
    # failures above are non-fatal: the container is still a deliberate,
    # usable container, so completion is recorded regardless.
    record_initialize_complete(cid)

    image_msg = f" [image: {image_notice}]" if image_notice else ""
    name_msg = f" [name: {name}]" if name else ""
    return cid + clone_msg + pr_msg + image_msg + name_msg


async def sandbox_initialize_tool(
    image: str | None = None,
    allow_network: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = "/tmp/repo",
    repo: str | None = None,
    pr: int | None = None,
    pip_extras: str | None = "[dev]",
    pip_args: str | None = None,
    mem_limit: str | None = None,
    cpus: float | None = None,
    name: str | None = None,
    session_label: str | None = None,
    ctx: Context | None = None,
) -> str:
    """Start a new Docker sandbox container (async MCP entry point).

    Thin async wrapper around :func:`sandbox_initialize`.  The slow setup
    phases (image pull, repo clone, pip install, PR checkout) can run for
    minutes, which previously tripped the MCP/HTTP request timeout and left
    the container orphaned (Issue #298).  To prevent that, the synchronous
    work runs in a thread pool while this coroutine emits a progress
    notification every :data:`_PROGRESS_INTERVAL_SECONDS`, keeping the
    connection alive so the real ``container_id`` is always returned.

    *ctx* is injected by FastMCP.  When it is ``None`` (e.g. direct calls in
    tests) the work runs inline with no progress notifications — identical
    behaviour to calling :func:`sandbox_initialize` directly.  All other
    parameters are forwarded verbatim; see :func:`sandbox_initialize` for the
    full per-parameter docs (this wrapper's own docstring is what MCP
    clients actually see, since the inner function is never registered as a
    tool -- callers should not need to open the source to learn this).

    ``session_label`` is forwarded to :func:`sandbox_initialize` \u2014 see its
    docstring for details.

    **Private-repo note (``pr=`` and** ``clone_repo`` **on a private
    repository):** under the egress proxy (#403), ``pr=N`` resolves the PR
    head ref host-side and checks it out *anonymously* inside the
    container.  This works for public repos with no further setup; for a
    private repo, the same read-authorization grant (#419) that
    ``clone_repo`` uses is opened for the anonymous clone + checkout too,
    so ``pr=N`` alone is enough -- the egress proxy just needs to be
    configured with a host-resolvable token (broker / ``GITHUB_TOKEN``)
    for the grant to authenticate; pushes and PR creation go through
    :func:`publish`, which resolves the token host-side, so the container
    never needs a credential of its own.
    """
    def _work() -> str:
        return sandbox_initialize(
            image=image,
            allow_network=allow_network,
            clone_repo=clone_repo,
            clone_dest=clone_dest,
            repo=repo,
            pr=pr,
            pip_extras=pip_extras,
            pip_args=pip_args,
            mem_limit=mem_limit,
            cpus=cpus,
            name=name,
            session_label=session_label,
        )

    if ctx is None:
        return _work()

    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, _work)
    start = time.monotonic()
    while not future.done():
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=_PROGRESS_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            try:
                # Progress value must increase every notification (MCP spec
                # "SHOULD increase, even if total is unknown"); a constant 0
                # can be ignored by clients that only reset their request
                # timeout on advancing progress (Issue #303).  Elapsed seconds
                # is monotonically increasing.
                await ctx.report_progress(
                    elapsed, None, f"sandbox_initialize running... ({elapsed:.0f}s)"
                )
            except Exception as e:  # pragma: no cover - defensive
                # A dropped connection must not strand the in-flight future;
                # keep waiting for the work to finish and return its result.
                logger.warning("report_progress failed: %s", e)
    return await future


def sandbox_stop(
    container_id: str,
    force: bool = False,
    working_dir: str | None = None,
) -> str:
    """Stop and remove a running sandbox container.

    Args:
        container_id: 12-character container ID prefix.
        force: If False (default), warns about unpushed checkpoints
            in the container's git repo.  Use True to override.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).

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

    # Check for unpushed checkpoints (Issue #264) — reuse checkpoint_list
    if not force:
        working_dir = resolve_git_root(container, working_dir)
        result = json.loads(checkpoint_list(container_id, working_dir))
        checkpoints = result.get("checkpoints", [])
        if checkpoints:
            return f"Error: Container has {len(checkpoints)} unpushed checkpoint(s). Use force=True to override."

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
    commands: Annotated[list[str], BeforeValidator(_coerce_list_arg)] | None = None,
    verbose: str = "summary",
    max_lines: int = 100,
    offset: int = 0,
    limit: int = 50,
    allow_network: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = "/tmp/repo",
    repo: str | None = None,
    pr: int | None = None,
    pip_extras: str | None = "[dev]",
    pip_args: str | None = None,
    timeout: int = 0,
    max_output_tokens: int = 0,
    session_label: str | None = None,
) -> str:
    """Start a container, execute commands, then remove it (one-shot).

    This is a convenience wrapper around:
    :func:`sandbox_initialize` → :func:`sandbox_exec` → :func:`sandbox_stop`.

    Output is sanitized (ANSI codes, ``\r`` progress bars, timestamps
    removed, VCS token values masked) and consecutive repeated lines
    are compressed (``[×N] content``).

    .. rubric:: Use when

    - Running a single command or script in a throwaway container
    - Lightweight one-shot workflows (init → run → cleanup)
    - Testing or validation that needs a fresh environment each time

    .. rubric:: Don't use when

    - **Interactive/persistent work** — use :func:`sandbox_initialize` instead
    - **File reading** — use :func:`read_file_range` instead (no container needed)
    - **Multiple sequential commands with inspection** — use :func:`sandbox_initialize` + :func:`sandbox_exec` instead

    .. rubric:: Prefer over

    - Prefer over :func:`sandbox_initialize` + :func:`sandbox_exec` + :func:`sandbox_stop` for simple one-shots
    - Prefer over writing temporary shell scripts for single commands

    .. rubric:: Fallback

    - For persistent containers use :func:`sandbox_initialize`
    - For complex multi-step workflows use :func:`sandbox_initialize` + multiple :func:`sandbox_exec` calls

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
        clone_repo: Optional ``owner/name`` repository to copy from the
               Shiori pre-cloned repos on the host into the container.
               Uses the host path configured via ``--shiori-repos-path``
               (default: ``None`` = no clone copy).  When Shiori is not
               configured, falls back to ``gh repo clone`` over the
               network (``allow_network`` is auto-enabled).  A *private*
               repo clones transparently too: the egress proxy opens a
               read-authorization grant (#419) authenticated with a
               host-resolved token, so no credential enters the container.
        clone_dest: Destination directory in the container for the
               cloned repository (default: ``/tmp/repo``).
               The actual path will be ``{clone_dest}/{repo_name}`` where *repo_name* is derived from *clone_repo* or *repo*.
        repo: Repository in ``"owner/name"`` format.
               Required when *pr* is specified.
        pr: Pull request number to clone and check out.
               When set, implicitly enables ``allow_network=True``,
               clones the repository
               inside the container, checks out the PR head branch,
               and installs dev dependencies.  Under the egress proxy
               (#403) this checkout is anonymous: the PR head ref is
               resolved host-side and the container never receives a
               token.  This works for public repos with no further
               setup; for a private repo, the same read-authorization
               grant (#419) that ``clone_repo`` uses is opened for the
               anonymous clone + checkout, so no extra steps are needed
               there either -- the egress proxy must simply be
               configured with a host-resolvable token (broker /
               ``GITHUB_TOKEN``) for the grant to actually authenticate.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
               Pass ``None`` to skip pip install entirely.  Also used when
               *clone_repo* is specified, and skipped automatically (with a
               log message) when the container has no network access, since
               PyPI would be unreachable.
        pip_args: Additional pip arguments (e.g. ``"--index-url https://download.pytorch.org/whl/cpu"``)
               passed through to the pip install command.
               Ignored when *pip_extras* is ``None`` since pip install is skipped entirely.
        session_label: Optional session identifier string.  When provided,
               this label is recorded in the journal for all subsequent
               operations on this container, replacing any previous label.
               Use this to distinguish operations from different model
               sessions or task contexts (Issue #479).
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

    # When pr is specified, implicitly enable network access.
    if pr is not None:
        allow_network = True

    # Auto-enable network when pre-clone is absent (Issue #146, #178)
    if clone_repo and pr is None and not _shiori_preclone_exists(clone_repo):
        allow_network = True
        logger.info(
            "clone_repo=%r: pre-clone absent, auto-enabling network access",
            clone_repo,
        )

    # Detection-based image selection (Issue #313), same as sandbox_initialize.
    resolved, image_notice = _select_initial_image(
        image, clone_repo, repo, pr
    )
    if image_notice:
        logger.info("image selection: %s", image_notice)
    client = _docker()
    # -- Egress proxy sidecar (#358, Epic #353): opt-in, fail closed --
    # A proxied container gets no VCS token in its env (#356); publish
    # hands the credential to the proxy per push grant instead.
    proxied = allow_network and proxy_lifecycle.egress_proxy_enabled()
    env: dict[str, str] = {}
    proxy_runtime = None
    if proxied:
        try:
            proxy_runtime = proxy_lifecycle.ensure_egress_proxy(client)
        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"egress proxy is enabled but unavailable (failing closed): {e}",
                }
            )
        env.update(proxy_lifecycle.sandbox_proxy_env(proxy_runtime))

    # --- Start container ---
    try:
        resolved = _resolve_image_ref(resolved)
        validate_image_ref(resolved)
        profile = replace(get_default_profile(), allow_network=allow_network)
        run_kwargs = build_secure_run_kwargs(
            profile,
            command="sleep infinity",
            detach=True,
            remove=False,
            environment=env,
        )
        if proxy_runtime is not None:
            run_kwargs = proxy_lifecycle.apply_network(run_kwargs, proxy_runtime)
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
        mem_limit=None,
        cpus=None,
        session_label=session_label,
    )

    # Same fail-closed CA wiring as sandbox_initialize (#358).
    if proxy_runtime is not None:
        try:
            proxy_lifecycle.install_ca(container, proxy_runtime.ca_pem)
        except Exception as e:
            try:
                container.remove(force=True)
            finally:
                record_stop(container_id)
            return json.dumps(
                {
                    "status": "error",
                    "error": f"egress proxy CA install failed (failing closed): {e}",
                }
            )

    # --- Clone: Shiori fast-path, network fallback (Issue #84, #146) ---
    # When pr is set, _setup_pr_branch handles its own clone,
    # so skip the Shiori clone copy to avoid redundant clone.
    clone_error: str | None = None
    # Same ground truth as sandbox_initialize: a proxied container holds no
    # token (#356), so the network clone must go anonymous (#403 fallout).
    container_has_token = "GITHUB_TOKEN" in env or "GH_TOKEN" in env
    if clone_repo and pr is None:
        _, clone_error = _try_clone_into_container(
            container,
            container_id,
            clone_repo,
            clone_dest,
            container_has_token,
        )
        if pip_extras is not None and clone_error is None:
            _run_pip_install(
                container, clone_repo, clone_dest, pip_extras, allow_network, pip_args
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
                # Same as sandbox_initialize: a token-free container takes
                # the anonymous checkout path (#403).
                _setup_pr_branch(
                    container,
                    container_id,
                    repo,
                    pr,
                    clone_dest,
                    pip_extras,
                    authenticated=container_has_token,
                    pip_args=pip_args,
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
    }

    if exit_code != 0:
        result["exit_code"] = exit_code
    if stderr_text and verbose != "error_only":
        result["stderr"] = stderr_text
    if clone_error:
        result["clone_warning"] = clone_error
    if pr_error:
        result["pr_warning"] = pr_error

    journal_record_exec(
        container_id,
        commands,
        exit_code,
        verbose=verbose,
        output_size=raw_size,
        max_output_tokens=max_output_tokens if max_output_tokens > 0 else None,
    )
    if allow_network:
        record_boundary_crossing(
            container_id,
            "run_container_and_exec",
            f"network={allow_network}",
        )

    record_stop(container_id)
    return json.dumps(result)
