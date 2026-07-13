"""Container lifecycle tools: init, stop, exec, test environment."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import shlex
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated, Any, NamedTuple

from docker.errors import APIError, NotFound
from fastmcp import Context
from pydantic import BeforeValidator

from sunaba import image_pins, proxy_lifecycle
from sunaba.journal import (
    get_last_activity_per_container,
    get_session_label,
    read_container_states,
    read_journal,
    record_boundary_crossing,
    record_copy,
    record_initialize,
    record_initialize_complete,
    record_stop,
    record_tool_use,
    set_session_label,
)
from sunaba.journal import (
    record_exec as journal_record_exec,
)
from sunaba.output_control import (
    OutputMetadata,
    compress_failures,
    compress_repeated_lines,
    paginate_output,
    sanitize_output,
    truncate_by_tokens,
    truncate_output,
)
from sunaba.proxy_client import authorized_read_grant
from sunaba.security import (
    CREATED_AT_LABEL,
    KIND_LABEL,
    KIND_PROXY,
    KIND_SANDBOX,
    MANAGED_LABEL,
    NAME_LABEL,
    NETWORK_LABEL,
    _detect_host_resources,
    _parse_mem_to_mb,
    build_secure_run_kwargs,
    get_default_profile,
    validate_image_ref,
)
from sunaba.tools.common import (
    CLONE_NO_TOKEN_WARNING,
    RECOVERY_DOCKER_TIMEOUT,
    _build_clone_command,
    _coerce_list_arg,
    _docker,
)
from sunaba.tools.vcs import (
    _resolve_vcs_token,
    checkpoint_list,
    resolve_git_root,
)

logger: logging.Logger = logging.getLogger(__name__)

# -- Default sandbox image (Issue #584) -----------------------------------
# The default is the *union* of every language toolchain, not a guess at which
# one this project needs.  Host-side language detection used to run here (#313):
# it probed the GitHub contents API before starting the container and picked a
# matching variant.  That ordering was the bug -- the guess came before an
# irreversible decision (an image is immutable once the container runs), while
# the accurate detector (``edit_verify.detect_languages``, which reads the real
# files) only runs afterwards.  A failed probe therefore landed init on an image
# without the toolchain the code needed, and the first verify failed the gate for
# a reason unrelated to the code (#584).  Making the default a superset removes
# the guess instead of trying to make it more reliable.
#
# The digest pins live as data in ``sunaba/image_pins.json``; CI
# (``.github/workflows/build-sandbox-variants.yml``) rewrites that file after
# each variant build, then verifies this loader returns the new digest.  This
# replaces the old ``sed``-on-source approach that broke silently when the
# constants moved or were reformatted (#214 / #331).  All refs are digest-pinned
# per ``docs/design_multilang_support.md`` section 6.
_image_pins: dict[str, str] = image_pins.load_image_pins()

#: All-in-one image: base + every language toolchain verify can dispatch to
#: (#584).  A superset of the dispatch matrix on purpose -- see
#: ``docker/Dockerfile.full``.
_FULL_IMAGE: str = _image_pins["full"]

#: Lean images, reachable only through an explicit ``image=`` (which also
#: accepts the aliases "neutral" / "python" / "go" / "full"; alias resolution
#: reads :data:`_image_pins` directly).  ``neutral`` is also the ``FROM`` parent
#: the variants are built on.
_NEUTRAL_IMAGE: str = _image_pins["neutral"]
_PYTHON_IMAGE: str = _image_pins["python"]
_GO_IMAGE: str = _image_pins["go"]

#: Image used when ``sandbox_initialize`` is called without ``image=``.
#: Overridable via the ``--default-image`` CLI flag (server.py).
_DEFAULT_IMAGE: str = _FULL_IMAGE


_CLONE_REPO_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9._-]+$")

#: Hard cap ratio for per-call mem_limit override (Issue #201).
#: Override values exceeding this fraction of host memory are rejected.
_HARD_CAP_RATIO: float = 0.9

#: Grace period before an init-incomplete container is considered orphaned
#: and reaped (Issue #298).  Comfortably longer than any legitimate
#: ``sandbox_initialize`` so an in-progress init (possibly in a concurrent
#: session) is never mistaken for an orphan.
_ORPHAN_GRACE_SECONDS: int = 600

#: Env var name for optional container idle TTL (Issue #480).
#: When set to a positive integer, containers idle longer than this
#: many seconds are automatically stopped by :func:`_reap_idle_containers`.
#: Default 0 = disabled (no automatic idle reaping).
_CONTAINER_TTL_ENV: str = "SUNABA_CONTAINER_TTL_SECONDS"

#: How often the async ``sandbox_initialize`` emits a progress notification to
#: keep the MCP/HTTP connection alive during slow setup (Issue #298).  Must be
#: shorter than the client's request timeout (~60s).
_PROGRESS_INTERVAL_SECONDS: float = 15.0




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
    """Pull the default image so first use is warm.

    A cold-start image pull can exceed the MCP/HTTP request timeout, so the
    first ``sandbox_initialize`` fails even though the pull finishes in the
    background and the next call succeeds (Issue #303).  Pulling ahead of time
    — at server startup and periodically — removes that first-call cliff and
    does not depend on progress notifications keeping the connection alive.

    Only the *default* image is prewarmed.  It used to pull the python and go
    variants too, because host-side detection could silently pick one of them
    and trade one cold pull for another.  Detection is gone (#584): an init
    with no ``image=`` always lands on the default, and the lean variants are
    reachable only by asking for them explicitly — a caller who does that can
    afford the pull, and ``sandbox_initialize`` keeps the connection alive with
    progress notifications while it happens (#298).

    Reads the module-level :data:`_DEFAULT_IMAGE` at call time so a
    ``--default-image`` override applied before the prewarm thread starts is
    honoured.  Any failure (registry hiccup, Docker down) is swallowed so a bad
    pull never blocks startup; the next refresh cycle retries.
    """
    images = {_DEFAULT_IMAGE}
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


def _select_initial_image(image: str | None) -> str:
    """Choose the image for a new container: the explicit one, or the default.

    There is deliberately **no language detection here** (Issue #584).  There
    used to be: the host probed the GitHub contents API before starting the
    container and picked a matching variant image.  That put the *guess* before
    an irreversible decision -- a container's image is immutable once running,
    and for ``clone_repo`` the files only exist inside the container afterwards
    -- while the *accurate* detector (``edit_verify.detect_languages``, which
    reads the real files) runs afterwards, when nothing can be changed about it.
    So when the probe failed (rate limit, timeout, private repo), init silently
    landed on an image without the toolchain the code actually needed, and the
    first verify failed the gate for a reason that had nothing to do with the
    code.

    The fix is to remove the guess rather than improve it: the default image is
    the union of every toolchain verify can dispatch to (``sandbox:full``), so
    whatever the in-container detector concludes, the tools are there.  An
    explicit ``image=`` remains the escape hatch -- and the only way to ask for
    a lean variant.
    """
    return image or _DEFAULT_IMAGE


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



def _clone_repo_via_network(
    container,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
    authenticated: bool = False,
    *,
    open_read_grant: bool = False,
) -> str:
    """Clone a repo via ``gh repo clone`` (Issue #146).

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
    # Validate the repo spec before attempting clone.
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
) -> str | None:
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

    Returns:
        Error message string on failure, ``None`` on success.
    """
    if not allow_network:
        logger.info(
            "Skipping pip install for %s: container has no network access",
            clone_repo,
        )
        return None
    if not pip_extras.startswith("["):
        logger.warning(
            "pip_extras=%r does not start with '['; normalizing to [%s]",
            pip_extras,
            pip_extras,
        )
        pip_extras = f"[{pip_extras}]"
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
        detail = (stderr_text or install_output).strip()
        logger.warning(
            "pip install deps failed (extras=%s, exit=%d): %s",
            pip_extras,
            exit_code,
            detail,
        )
        return f"pip install -e .{pip_extras} failed (exit {exit_code}): {detail}"
    return None


def _try_clone_into_container(
    container: Any,
    container_id: str,
    clone_repo: str,
    clone_dest: str,
    authenticated: bool = False,
    *,
    open_read_grant: bool = False,
) -> CloneResult:
    """Clone a repo into the container via network; return (msg, error).

    Returns ``(clone_msg, None)`` on success, ``(None, error_str)`` on failure.
    """
    try:
        msg = _clone_repo_via_network(
            container,
            container_id,
            clone_repo,
            clone_dest,
            authenticated,
            open_read_grant=open_read_grant,
        )
        if not authenticated and not open_read_grant:
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
        "User-Agent": "sunaba",
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

    # Step 4: Install dev dependencies (non-fatal)
    pip_msg = ""
    local_pip_args = pip_args or ""
    if pip_extras is not None:
        if not pip_extras.startswith("["):
            logger.warning(
                "pip_extras=%r does not start with '['; normalizing to [%s]",
                pip_extras,
                pip_extras,
            )
            pip_extras = f"[{pip_extras}]"
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
            detail = (stderr_text or install_output).strip()
            logger.warning(
                "pip install deps failed (extras=%s, exit=%d): %s",
                pip_extras,
                exit_code,
                detail,
            )
            pip_msg = f" (pip install -e .{pip_extras} failed: exit {exit_code})"

    _write_clone_meta(container, safe_dest, base_branch=base_ref)

    record_copy(
        cid,
        "setup_pr_branch",
        f"repo={repo} pr=#{pr_number} branch={head_ref}",
        safe_dest,
    )

    return f"PR #{pr_number} ({head_ref}) → {clone_dest}/{repo_name} in container {cid}{pip_msg}"


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


def _label_network(labels: dict[str, str]) -> bool | None:
    """Read ``NETWORK_LABEL`` back as a bool, or ``None`` when absent.

    ``None`` means "this container predates the label" (Issue #527), which is
    deliberately distinct from ``False`` -- we report the gap rather than
    guessing a posture we cannot know.
    """
    raw = labels.get(NETWORK_LABEL)
    if raw is None:
        return None
    return raw == "true"


def _container_kind(labels: dict[str, str], docker_name: str | None) -> str:
    """Classify a managed container as ``sandbox`` or ``proxy`` (Issue #527).

    Falls back to the sidecar's fixed container name for containers created
    before ``KIND_LABEL`` existed -- notably the long-lived egress proxy, which
    has ``restart_policy=unless-stopped`` and so survives an upgrade unlabelled
    until something recreates it.
    """
    kind = labels.get(KIND_LABEL)
    if kind:
        return kind
    if docker_name == proxy_lifecycle.PROXY_CONTAINER_NAME:
        return KIND_PROXY
    return KIND_SANDBOX


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


def _get_container_ttl_seconds() -> int:
    """Read the optional container idle TTL from the environment.

    Returns:
        Positive integer seconds, or 0 when TTL is not configured
        (auto-reap disabled).
    """
    val = os.environ.get(_CONTAINER_TTL_ENV)
    if val is None or val.strip() == "":
        return 0
    try:
        ttl = int(val.strip())
        if ttl <= 0:
            return 0
        return ttl
    except (ValueError, TypeError):
        return 0


def _reap_idle_containers() -> list[str]:
    """Stop containers idle longer than the configured TTL (Issue #480).

    Reads :envvar:`SUNABA_CONTAINER_TTL_SECONDS` to determine the
    threshold.  When the env var is not set or is 0, this is a no-op
    (auto-reap disabled by default).

    Only sandboxes are considered: ``MANAGED_LABEL`` also matches the
    egress-proxy sidecar, which is shared infrastructure and must never
    be reaped.  A container is ``idle`` when no journal entry exists for
    it in the last TTL seconds.  Failures are swallowed (best-effort GC).

    Runs from both ``sandbox_initialize`` and ``sandbox_list_containers``.

    Returns:
        List of 12-character container ID prefixes that were stopped.
    """
    ttl = _get_container_ttl_seconds()
    if ttl <= 0:
        return []  # Opt-in only -- no auto-stop by default

    client = _docker(timeout=RECOVERY_DOCKER_TIMEOUT)
    try:
        containers = client.containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
    except Exception as e:
        logger.warning("idle reap: failed to list containers: %s", e)
        return []

    last_activity = get_last_activity_per_container()
    now = datetime.now(timezone.utc)
    reaped: list[str] = []
    for c in containers:
        cid = c.id[:12]
        # MANAGED_LABEL alone also matches the egress-proxy sidecar, which is
        # shared infrastructure with no journal activity of its own.  Reaping
        # it would break networked init for every other container, so scope
        # the reaper to sandboxes explicitly instead of relying on the sidecar
        # merely happening to have no activity timestamp.
        labels = getattr(c, "labels", None) or {}
        if _container_kind(labels, getattr(c, "name", None)) != KIND_SANDBOX:
            continue
        last_ts = last_activity.get(cid)
        idle_secs = _age_seconds(last_ts, now)
        if idle_secs is None or idle_secs < ttl:
            continue
        logger.info(
            "Reaping idle container %s (idle=%.0fs, ttl=%ds)", cid, idle_secs, ttl
        )
        try:
            c.kill()
        except (NotFound, APIError):
            pass
        try:
            c.remove(force=True)
        except NotFound:
            pass
        except Exception as e:
            logger.warning("idle reap: failed to remove %s: %s", cid, e)
            continue
        record_stop(cid)
        reaped.append(cid)
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


# Label-based discovery survives server restarts (#478); the egress-proxy
# sidecar carries MANAGED_LABEL too, so it is listed but tagged (#527).
def sandbox_list_containers() -> str:
    """List all managed sandbox containers with metadata.

    Discovery is Docker-label based, so it works across server
    restarts; use it to find existing containers before starting a new
    one.  The egress-proxy sidecar is listed with kind='proxy'; only
    kind='sandbox' entries are attachable.  When a container TTL is
    configured, idle containers are reaped first and reported in
    reaped_ids.

    Returns:
        JSON containers array: container_id, name, kind, image, status,
        allow_network, created_at, age_seconds, idle_seconds,
        last_activity_ts; plus reaped_ids when reaping ran.
    """
    import json

    reaped_ids = _reap_idle_containers()
    containers, error = list_managed_containers()
    if error is not None:
        return json.dumps({"containers": [], "error": error, "reaped_ids": reaped_ids})

    return json.dumps({
        "containers": containers,
        "reaped_ids": reaped_ids,
    }, ensure_ascii=False)


def list_managed_containers() -> tuple[list[dict[str, Any]], str | None]:
    """Return metadata for every managed container, plus an error message.

    The read half of :func:`sandbox_list_containers`, split out so that
    read-only consumers -- the dashboard's ``/containers`` page (Issue #527) --
    can list containers without also *reaping* them: a page that auto-refreshes
    every 10s must not quietly tear down containers as a side effect of being
    looked at.

    Returns ``(containers, None)`` on success, or ``([], message)`` when Docker
    cannot be reached.
    """
    client = _docker()
    try:
        containers = client.containers.list(
            all=True,
            filters={"label": f"{MANAGED_LABEL}=true"},
        )
    except Exception as e:
        return [], str(e)

    last_activity = get_last_activity_per_container()
    now = datetime.now(timezone.utc)
    result: list[dict[str, Any]] = []
    for c in containers:
        cid = c.id[:12]
        labels = getattr(c, "labels", None) or {}
        created_raw = labels.get(CREATED_AT_LABEL)
        name_val = labels.get(NAME_LABEL)
        age = _age_seconds(created_raw, now)

        idle = _age_seconds(last_activity.get(cid), now)
        last_ts = last_activity.get(cid)

        result.append({
            "container_id": cid,
            "name": name_val,
            "kind": _container_kind(labels, getattr(c, "name", None)),
            "image": c.image.tags[0] if c.image.tags else str(c.image.short_id),
            "status": c.status,
            "allow_network": _label_network(labels),
            "created_at": created_raw,
            "age_seconds": age,
            "idle_seconds": idle,
            "last_activity_ts": last_ts,
        })

    return result, None


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
    - ``age_seconds`` (float | None): approximate age in seconds
    - ``idle_seconds`` (float | None): seconds since last journal activity
    - ``last_activity_ts`` (str | None): most recent journal entry timestamp
    - ``session_label`` (str | None): current session label attached to this container
    - ``git``: git orientation when available:
        - ``branch`` (str | None): current git branch
        - ``status_short`` (str | None): ``git status --short`` output
        - ``repo_root`` (str | None): detected git root
    - ``last_checkpoint`` (str | None): most recent checkpoint message
    - ``last_checkpoint_ts`` (str | None): timestamp of last checkpoint
    - ``journal_activity`` (int): number of journal entries for this container
    - ``allow_network`` (bool | None): whether the container has network access (True, False, or None when the label predates Issue #527)
    - ``error`` (str | None): error message on failure

    The orientation summary lets a cold session (or a cheap model) pick up
    where a previous session left off without re-reading the entire context
    (Issue #478).

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

    last_activity = get_last_activity_per_container()
    last_ts = last_activity.get(cid)
    idle = _age_seconds(last_ts, now)

    result: dict[str, Any] = {
        "found": True,
        "container_id": cid,
        "name": name_val,
        "status": container_obj.status,
        "image": container_obj.image.tags[0] if container_obj.image.tags else str(container_obj.image.short_id),
        "created_at": created_raw,
        "age_seconds": age,
        "idle_seconds": idle,
        "last_activity_ts": last_ts,
        "match_type": match_type,
        "allow_network": _label_network(labels),
    }

    # Attach is where a different session (or a different model) picks up an
    # existing container, so the hand-off itself has to be in the journal --
    # otherwise §9.1's audit trail has entries on both sides of the switch and
    # nothing marking the switch.  A session_label swap is recorded with the
    # label it replaced: the label alone only ever rides along on *subsequent*
    # entries, so without this the boundary between two labelled runs cannot be
    # recovered from the journal (#554).
    attach_params: dict[str, Any] = {"name_or_id": name_or_id, "match_type": match_type}
    if session_label is not None:
        previous_label = get_session_label(cid)
        if previous_label != session_label:
            attach_params["previous_session_label"] = previous_label
        set_session_label(cid, session_label)
    current_label = get_session_label(cid)
    if current_label is not None:
        result["session_label"] = current_label

    # Recorded before the git orientation below, which shells into the
    # container and may fail: the hand-off must not be lost along with it.
    record_tool_use(cid, "sandbox_attach", attach_params)

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

    Args:
        image: Docker image to use (e.g. ``python@sha256:...``).
               Variant aliases resolve to the pinned GHCR digest images
               (Issue #545): the all-in-one "full", and the lean "neutral" /
               "python" / "go".  Omit this argument unless you have a reason:
               the default is the all-in-one image, which carries every
               toolchain verify can run, so nothing about the project's
               language has to be guessed (Issue #584).
        allow_network: Whether to allow network access (default ``False``).
               Set to ``True`` for VCS operations (git/gh) that need to
               reach GitHub API.  Network access is a boundary-crossing
               operation and should be used only when necessary.
        clone_repo: Optional ``owner/name`` repository to clone via
               ``gh repo clone`` over the network (``allow_network`` is
               auto-enabled).  A *private* repo clones transparently too:
               the egress proxy opens a read-authorization grant (#419)
               authenticated with a host-resolved token, so no credential
               enters the container.
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
        Network state (``[network: on]`` / ``[network: off]``) is appended.
        If *clone_repo* is specified, a message about the clone copy
        is appended (and dev install if pip_extras is set).
        If *pr* is specified, a message about the PR branch setup
        is appended.

    """
    # Opportunistic GC (Issue #298): clean up any containers orphaned by a
    # previously timed-out init before creating a new one.  Best-effort —
    # never let cleanup failure abort the init.
    try:
        _reap_orphaned_init_containers()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("orphan reap failed: %s", e)

    # Idle GC (Issue #480): init is the only call every agent makes, so it is
    # the one hook where a configured TTL reliably fires.  Reaping solely from
    # sandbox_list_containers would leave the TTL near-dead in practice --
    # agents create containers constantly and seldom list them.
    try:
        _reap_idle_containers()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("idle reap failed: %s", e)

    # When pr is specified, implicitly enable network access.
    if pr is not None:
        allow_network = True

    # Auto-enable network for clone_repo (Issue #146)
    if clone_repo and pr is None:
        allow_network = True
        logger.info(
            "clone_repo=%r: auto-enabling network access",
            clone_repo,
        )

    # Validate pip_extras format early (Issue #595).
    if pip_extras is not None and pip_extras != "" and not pip_extras.startswith("["):
        logger.warning(
            "pip_extras=%r does not start with '['; auto-normalizing to [%s]",
            pip_extras,
            pip_extras,
        )
        pip_extras = f"[{pip_extras}]"

    client = _docker()
    # Name collision check (Issue #478): reject if a container with the
    # same user-assigned name is already running.
    if name:
        existing = _find_containers_by_name(client, name)
        if existing:
            return f"Error: a container named {name!r} already exists ({existing[0][:12]}); use a different name or sandbox_attach to connect to it"

    # No image given -> the all-in-one default (#584).  Nothing is guessed.
    resolved = _select_initial_image(image)
    # -- Egress proxy sidecar (#358, Epic #509): default-on, fail closed --
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
        # Resolve variant aliases ("full", "neutral", "python", "go") to pinned digests (Issue #545)
        resolved = _image_pins.get(resolved, resolved)
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

    # -- Clone via network (Issue #146) --
    # When pr is set, _setup_pr_branch handles its own clone.
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
            pip_err = _run_pip_install(
                container, clone_repo, clone_dest, pip_extras, allow_network, pip_args
            )
            if pip_err is not None:
                clone_msg += f" (pip install failed: {pip_err})"
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

    net_state = "on" if allow_network else "off"
    net_msg = f" [network: {net_state}]"
    name_msg = f" [name: {name}]" if name else ""
    return cid + clone_msg + pr_msg + net_msg + name_msg


# Async wrapper around sandbox_initialize: the sync work runs in a thread
# pool while progress notifications keep MCP/HTTP alive (#298 orphan fix).
# ctx is FastMCP-injected; None (tests) runs inline.  Full per-parameter
# docs live on sandbox_initialize.  Private-repo pr=/clone_repo auth rides
# the egress-proxy read grant (#403/#419); push credentials stay host-side
# via publish (#347).
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
    """Start a new Docker sandbox container.

    Returns the container_id used by every other tool.  Slow setup
    phases (image pull, clone, pip install, PR checkout) can run for
    minutes; progress notifications keep the connection alive, so wait
    for the real result.

    Args:
        image: Docker image, or alias 'full'/'neutral'/'python'/'go' (pinned digests).
               Default: the all-in-one image (every toolchain verify can run).
        allow_network: Enable network access. Required for pip install,
            network clones, and publish.
        clone_repo: 'owner/name' cloned over the network (auto-enables
            allow_network; private repos authenticate host-side, no token
            in container).
        clone_dest: Parent dir; the repo lands in {clone_dest}/{repo_name}.
        repo: 'owner/name'; required with pr.
        pr: PR number to clone and check out (implies allow_network;
            anonymous checkout, no token enters the container).
        pip_extras: Extras for dev install, e.g. '[dev]'. None skips pip
            install; also auto-skipped without network.
        pip_args: Extra pip arguments; ignored when pip_extras is None.
        mem_limit: Container memory limit (e.g. '2g').
        cpus: CPU quota.
        name: Container name, resolvable later via sandbox_attach.
        session_label: Session tag recorded in the journal.

    Returns:
        Container ID prefix plus clone/checkout/network summary.
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


# Convenience wrapper over sandbox_initialize -> sandbox_exec -> sandbox_stop.
# Image aliases resolve to pinned GHCR digests (#545); private clone_repo/pr=
# checkout rides the egress-proxy read grant (#403/#419); session_label is #479.
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
    """One-shot: start a container, run commands, then remove it.

    Equivalent to sandbox_initialize -> sandbox_exec -> sandbox_stop.
    Output is sanitized (ANSI/CR/timestamps stripped, token values
    masked) and consecutive repeated lines are compressed.

    Args:
        image: Docker image, or alias 'full'/'neutral'/'python'/'go' (pinned digests).
               Default: the all-in-one image (every toolchain verify can run).
        commands: Shell commands run sequentially; must be non-empty.
        verbose: 'error_only', 'summary' (default), or 'full'.
        max_lines: Max lines shown in summary/error_only mode.
        offset: 0-indexed line offset for paging output.
        limit: Max lines per page.
        allow_network: Enable network access (needed for git/gh/PyPI).
        clone_repo: 'owner/name' cloned over the network (auto-enables
            allow_network; private repos authenticate host-side, no token
            in container).
        clone_dest: Parent dir; the repo lands in {clone_dest}/{repo_name}.
        repo: 'owner/name'; required with pr.
        pr: PR number to clone and check out (implies allow_network;
            anonymous checkout, no token enters the container).
        pip_extras: Extras for dev install, e.g. '[dev]'. None skips pip
            install; also auto-skipped without network.
        pip_args: Extra pip arguments; ignored when pip_extras is None.
        timeout: Kill after N seconds (0 = no limit); on expiry
            status='timeout', exit_code=124.
        max_output_tokens: Summarize output to this token budget (0 = off);
            full output stays retrievable via a resource://run/ handle.
        session_label: Session tag recorded in the journal.

    Returns:
        JSON: status, output (or error), shown, total_lines, truncated,
        next_offset, has_more.
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

    # Auto-enable network for clone_repo (Issue #146)
    if clone_repo and pr is None:
        allow_network = True
        logger.info(
            "clone_repo=%r: auto-enabling network access",
            clone_repo,
        )

    # Validate pip_extras format early (Issue #595).
    if pip_extras is not None and pip_extras != "" and not pip_extras.startswith("["):
        logger.warning(
            "pip_extras=%r does not start with '['; auto-normalizing to [%s]",
            pip_extras,
            pip_extras,
        )
        pip_extras = f"[{pip_extras}]"

    # No image given -> the all-in-one default (#584), same as sandbox_initialize.
    resolved = _select_initial_image(image)
    client = _docker()
    # -- Egress proxy sidecar (#358, Epic #509): default-on, fail closed --
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
        # Resolve variant aliases ("full", "neutral", "python", "go") to pinned digests (Issue #545)
        resolved = _image_pins.get(resolved, resolved)
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

    # --- Clone via network (Issue #146) ---
    # When pr is set, _setup_pr_branch handles its own clone.
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
            pip_error = _run_pip_install(
                container, clone_repo, clone_dest, pip_extras, allow_network, pip_args
            )
            if pip_error is not None:
                clone_error = pip_error
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
