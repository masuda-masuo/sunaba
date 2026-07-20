"""Repository clone and PR-branch setup."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex
from typing import Any, NamedTuple

from sunaba.journal import (
    record_boundary_crossing,
    record_copy,
)
from sunaba.proxy_client import authorized_read_grant
from sunaba.tools.common import (
    CLONE_NO_TOKEN_WARNING,
    META_PATH,
    _build_clone_command,
)
from sunaba.tools.vcs import (
    _resolve_vcs_token,
)

logger: logging.Logger = logging.getLogger(__name__)

_CLONE_REPO_PATTERN: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9._-]+$")


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


def _write_clone_meta(container: Any, clone_path: str, base_branch: str | None = None) -> None:
    """Write clone destination path into container metadata.

    The metadata file lives in the home directory, not the workspace, so it
    never turns up in ``git status``.  :func:`resolve_git_root` no longer
    needs it to find the repo -- the container's ``WorkingDir`` is the repo
    root -- but ``diff_in_container`` still reads *base_branch* from it to
    resolve the default diff base after a PR checkout, and containers created
    before the workspace existed still fall back to ``clone_path``.

    Failures are logged but not propagated so that a metadata write
    failure never breaks an otherwise successful clone.
    """
    try:
        meta: dict[str, str] = {"clone_path": clone_path}
        if base_branch:
            meta["base_branch"] = base_branch
        meta_json = json.dumps(meta)
        safe_meta = shlex.quote(meta_json)
        meta_dir = META_PATH.rsplit("/", 1)[0]
        container.exec_run(
            ["/bin/sh", "-c",
             f"mkdir -p {shlex.quote(meta_dir)} "
             f"&& printf '%s' {safe_meta} > {shlex.quote(META_PATH)}"],
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
    clone_path = clone_dest.rstrip("/") or clone_dest
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


def _normalize_pip_extras(pip_extras: str | None) -> str | None:
    """Normalize pip_extras format: bare ``"dev"`` → ``"[dev]"``.

    Returns the normalized value (or ``None`` unchanged).
    """
    if pip_extras is not None and pip_extras != "" and not pip_extras.startswith("["):
        logger.warning(
            "pip_extras=%r does not start with '['; auto-normalizing to [%s]",
            pip_extras,
            pip_extras,
        )
        return f"[{pip_extras}]"
    return pip_extras


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
    safe_dest = shlex.quote(clone_dest)
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
    safe_dest = shlex.quote(clone_dest)
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
        clone_cmd = _build_clone_command(repo, clone_dest)
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
            pip_msg = f" (pip install -e .{pip_extras} failed (exit {exit_code}): {detail})"

    _write_clone_meta(container, clone_dest, base_branch=base_ref)

    record_copy(
        cid,
        "setup_pr_branch",
        f"repo={repo} pr=#{pr_number} branch={head_ref}",
        safe_dest,
    )

    return f"PR #{pr_number} ({head_ref}) → {clone_dest} in container {cid}{pip_msg}"


def _resolve_default_branch(repo: str, *, token: str | None = None) -> str:
    """Resolve the default branch of a repository via the GitHub REST API.

    Returns the default branch name (e.g. ``"main"``).

    The same host-side auth model as :func:`_resolve_pr_head_ref`: the host
    resolves the API call with its own token (or anonymously for public repos).

    *token*, when given, is used as-is instead of calling
    :func:`_resolve_vcs_token` again.  A broker-backed resolution spawns a
    subprocess per call (no caching, up to a 30s timeout) -- callers that
    already resolved a token for this same operation should pass it through
    here instead of paying for a second mint.

    Raises:
        RuntimeError: If the API call fails or returns no default branch.
    """
    import urllib.error
    import urllib.request

    _validate_clone_repo(repo)
    url = f"https://api.github.com/repos/{repo}"
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
            f"Failed to resolve default branch for {repo}:"
            f" GitHub API returned HTTP {e.code}{hint}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Failed to resolve default branch for {repo}: {e.reason}"
        ) from e
    default_branch = data.get("default_branch", "")
    if not default_branch:
        raise RuntimeError(
            f"GitHub API returned no default_branch for {repo}"
        )
    return default_branch


def _setup_branch(
    container: Any,
    container_id: str,
    repo: str,
    branch_name: str,
    clone_dest: str,
    pip_extras: str | None = "[dev]",
    *,
    authenticated: bool = False,
    open_read_grant: bool = False,
    pip_args: str | None = None,
) -> str:
    """Clone repo and check out a named branch inside the container.

    Two transports, chosen by *authenticated* (mirroring
    :func:`_setup_pr_branch` and :func:`_build_clone_command`):

    - **authenticated** (the container env carries a VCS token): ``gh repo
      clone`` with ``-- -b <branch>`` passed to the underlying git clone.
    - **anonymous** (no token, the default for proxied containers per #356):
      a plain ``git clone -b <branch>`` over HTTPS.

    A branch that does not exist on the remote produces an error naming the
    branch, rather than silently falling back to the default branch.

    The default branch of the repository is resolved via the GitHub API and
    recorded as the *base_branch* in the clone metadata, so tools like
    :func:`diff_in_container` know what this branch is based on.

    Args:
        container: Docker container object.
        container_id: 12-char container ID prefix.
        repo: Repository in ``"owner/name"`` format.
        branch_name: Branch name to check out.
        clone_dest: Destination directory inside the container.
        pip_extras: Pip extras string (e.g. ``"[dev]"``) for dev install.
            Pass ``None`` to skip pip install entirely.
        authenticated: Whether the container env actually carries a VCS
            token (ground truth: the env actually built, #356).
        open_read_grant: When ``True`` and *authenticated* is ``False``,
            wrap the anonymous clone in a short-lived egress-proxy
            read-authorization grant (#419) with a host-resolved token
            -- the same mechanism :func:`_clone_repo_via_network` and
            :func:`_setup_pr_branch` use for private repos under the proxy.
        pip_args: Additional pip arguments (e.g.
            ``"--index-url https://..."``).  Ignored when *pip_extras* is
            ``None`` since pip install is skipped entirely.

    Returns:
        Success message string.
    """
    cid = container_id[:12]
    safe_dest = shlex.quote(clone_dest)
    safe_repo = shlex.quote(repo)
    safe_branch = shlex.quote(branch_name)

    # Resolve a host token once and reuse for both the default-branch API
    # call and (when applicable) the read grant, avoiding a second expensive
    # broker-backed subprocess spawn per #436.
    anon_token: str | None = None
    if not authenticated:
        anon_token = _resolve_vcs_token()

    # Build the clone command with branch parameter.
    # _build_clone_command already handles -b <branch> for both auth paths.
    clone_cmd = _build_clone_command(
        repo, clone_dest, branch=branch_name, authenticated=authenticated,
    )

    # Open read-authorization grant for private repos under the proxy (#419).
    grant_open = open_read_grant and not authenticated
    grant = (
        authorized_read_grant(repo, token=anon_token or None)
        if grant_open
        else contextlib.nullcontext()
    )
    journal_detail = (
        f"repo={repo} branch={branch_name} dest={clone_dest}"
        f" proxy_read_grant=True"
    )

    try:
        with grant:
            exit_code, output = container.exec_run(
                ["/bin/sh", "-c", clone_cmd],
                stdout=True,
                stderr=True,
                demux=True,
            )
            stdout_part, stderr_part = output or (b"", b"")
            clone_output = (
                stdout_part.decode("utf-8", errors="replace")
                if stdout_part else ""
            )
            stderr_text = (
                stderr_part.decode("utf-8", errors="replace")
                if stderr_part else ""
            )

            if exit_code != 0:
                hint = (
                    ""
                    if authenticated
                    else (
                        " (anonymous clone: the container holds no VCS token"
                        " (#356); if this is a private repository, ensure the"
                        " egress proxy has a host-resolvable token for the"
                        " read-authorization grant, see #419)"
                    )
                )
                raise RuntimeError(
                    f"Failed to clone repo {repo} on branch {branch_name}:"
                    f" {stderr_text or clone_output}{hint}"
                )

            logger.info(
                "Cloned %s (branch=%s) → %s in container %s",
                safe_repo,
                safe_branch,
                safe_dest,
                cid,
            )
    except Exception:
        if grant_open:
            record_boundary_crossing(
                cid, "setup_branch", journal_detail, approved=False,
            )
        raise
    if grant_open:
        record_boundary_crossing(
            cid, "setup_branch", journal_detail, approved=True,
        )

    # Verify the working tree is on the requested branch.
    # git clone silently lands on the default branch when the -b argument
    # names a branch that does not exist; git clone exits 0 even then.  An
    # explicit check catches the mismatch early (acceptance criterion #3).
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", f"cd {safe_dest} && git rev-parse --abbrev-ref HEAD"],
        stdout=True,
        stderr=True,
        demux=True,
    )
    stdout_part, _ = output or (b"", b"")
    actual_branch = (
        stdout_part.decode("utf-8", errors="replace").strip()
        if stdout_part else ""
    )

    if exit_code != 0 or actual_branch != branch_name:
        raise RuntimeError(
            f"Branch mismatch after clone: expected {branch_name!r},"
            f" working tree is on {actual_branch!r}"
            f" (exit {exit_code})"
        )

    # Resolve the default branch via GitHub API for clone metadata.
    # Non-fatal: if it fails, metadata records no base_branch.
    base_branch = ""
    try:
        base_branch = _resolve_default_branch(repo, token=anon_token or None)
    except Exception:
        logger.warning(
            "Failed to resolve default branch for %s", repo, exc_info=True,
        )

    _write_clone_meta(
        container, clone_dest, base_branch=base_branch or None,
    )

    # Install dev dependencies (non-fatal)
    pip_msg = ""
    local_pip_args = pip_args or ""
    if pip_extras is not None:
        install_cmd = (
            f"cd {safe_dest}"
            f" && {_editable_install_cmd(f'.{pip_extras}', local_pip_args)}"
        )
        exit_code, output = container.exec_run(
            ["/bin/sh", "-c", install_cmd],
            stdout=True,
            stderr=True,
            demux=True,
        )
        stdout_part, stderr_part = output or (b"", b"")
        install_output = (
            stdout_part.decode("utf-8", errors="replace")
            if stdout_part else ""
        )
        stderr_text = (
            stderr_part.decode("utf-8", errors="replace")
            if stderr_part else ""
        )

        if exit_code != 0:
            detail = (stderr_text or install_output).strip()
            logger.warning(
                "pip install deps failed (extras=%s, exit=%d): %s",
                pip_extras,
                exit_code,
                detail,
            )
            pip_msg = (
                f" (pip install -e .{pip_extras} failed"
                f" (exit {exit_code}): {detail})"
            )

    record_copy(
        cid,
        "setup_branch",
        f"repo={repo} branch={branch_name}",
        safe_dest,
    )

    return (
        f"Branch {branch_name} → {clone_dest} in container {cid}{pip_msg}"
    )
