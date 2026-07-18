"""Container lifecycle: init, stop, attach, one-shot run."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shlex
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated, Any

from docker.errors import APIError, NotFound
from fastmcp import Context
from pydantic import BeforeValidator

from sunaba import proxy_lifecycle
from sunaba.journal import (
    get_last_activity_per_container,
    get_session_label,
    read_journal,
    record_boundary_crossing,
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
from sunaba.security import (
    CREATED_AT_LABEL,
    MANAGED_LABEL,
    NAME_LABEL,
    _detect_host_resources,
    _parse_mem_to_mb,
    build_secure_run_kwargs,
    get_default_profile,
    validate_image_ref,
)
from sunaba.tools.common import (
    RECOVERY_DOCKER_TIMEOUT,
    WORKSPACE,
    _coerce_list_arg,
)
from sunaba.tools.vcs import (
    checkpoint_list,
    resolve_git_root,
)

from .clone import (
    _normalize_pip_extras,
    _run_pip_install,
    _setup_pr_branch,
    _try_clone_into_container,
)
from .image import (
    _ensure_image,
    _image_pins,
    _resolve_image_ref,
    _select_initial_image,
)
from .listing import (
    _age_seconds,
    _find_containers_by_name,
    _label_network,
)
from .reaper import (
    _reap_idle_containers,
    _reap_orphaned_init_containers,
)

logger: logging.Logger = logging.getLogger(__name__)

#: Hard cap ratio for per-call mem_limit override (Issue #201).
#: Override values exceeding this fraction of host memory are rejected.
_HARD_CAP_RATIO: float = 0.9

#: How often the async ``sandbox_initialize`` emits a progress notification to
#: keep the MCP/HTTP connection alive during slow setup (Issue #298).  Must be
#: shorter than the client's request timeout (~60s).
_PROGRESS_INTERVAL_SECONDS: float = 15.0


def _ensure_workspace(container: Any, workspace: str) -> None:
    """Make *workspace* exist and belong to the container's user.

    Docker creates a container's working directory when the image does not
    ship it -- but creates it owned by ``root``, which the non-root sandbox
    user cannot write to.  Chowning it here means the workspace works on any
    image, including the currently pinned ones whose ``WORKDIR`` is still the
    home directory.

    Runs as root because that is the only user who can hand the directory
    over; every other exec keeps running as the sandbox user.
    """
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         f"mkdir -p {shlex.quote(workspace)} "
         f"&& chown $(id -u sandbox):$(id -g sandbox) {shlex.quote(workspace)}"],
        user="root",
    )
    if ec != 0:
        detail = out.decode("utf-8", errors="replace").strip() if out else ""
        raise RuntimeError(f"Failed to prepare workspace {workspace}: {detail}")


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

    from sunaba.tools.container import _docker

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
    clone_dest: str = WORKSPACE,
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

    **One-step init + clone:** pass ``clone_repo`` to clone the repository
    and install its dependencies as part of startup.  For a full one-shot
    workflow with commands, use :func:`run_container_and_exec` which wraps
    init/exec/stop.

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
        clone_dest: Directory the repo is cloned into; it becomes the
               git root *and* the container's working directory, so
               every command runs inside the repo by default
               (default: the workspace, ``/workspace``).
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
    from sunaba.tools.container import _docker
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

    pip_extras = _normalize_pip_extras(pip_extras)

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
        # Resolve variant aliases ("full", "neutral", "python", "go", "js") to pinned digests (Issue #545)
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
    # The workspace is the container's working directory, so an exec that
    # names no workdir still runs in the repo root (#600).  Docker records it
    # in the container config, which is where resolve_git_root reads the repo
    # root back from -- so the two cannot disagree.
    run_kwargs = build_secure_run_kwargs(
        profile,
        command="sleep infinity",
        detach=True,
        remove=False,
        environment=env,
        labels=labels,
        working_dir=clone_dest,
        **resource_overrides,
    )
    if proxy_runtime is not None:
        run_kwargs = proxy_lifecycle.apply_network(run_kwargs, proxy_runtime)

    try:
        _ensure_image(resolved)
        container = client.containers.run(resolved, **run_kwargs)
        _ensure_workspace(container, clone_dest)
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


# ctx is FastMCP-injected; None (tests) runs inline.  Full per-parameter
# docs live on sandbox_initialize.  Private-repo pr=/clone_repo auth rides
# the egress-proxy read grant (#403/#419); push credentials stay host-side
# via publish (#347).
async def sandbox_initialize_tool(
    image: str | None = None,
    allow_network: bool = False,
    clone_repo: str | None = None,
    clone_dest: str = WORKSPACE,
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
        image: Docker image, or alias 'full'/'neutral'/'python'/'go'/'js' (pinned digests).
               Default: the all-in-one image (every toolchain verify can run).
        allow_network: Enable network access. Required for pip install,
            network clones, and publish.
        clone_repo: 'owner/name' cloned over the network (auto-enables
            allow_network; private repos authenticate host-side, no token
            in container).
        clone_dest: Directory the repo is cloned into; it becomes the git
            root and the container's working directory.
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
    from sunaba.tools.container import _docker
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
    clone_dest: str = WORKSPACE,
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
        image: Docker image, or alias 'full'/'neutral'/'python'/'go'/'js' (pinned digests).
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
        clone_dest: Directory the repo is cloned into; it becomes the git
            root and the container's working directory.
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

    from sunaba.tools.container import _docker

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

    pip_extras = _normalize_pip_extras(pip_extras)

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
        # Resolve variant aliases ("full", "neutral", "python", "go", "js") to pinned digests (Issue #545)
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
            working_dir=clone_dest,
        )
        if proxy_runtime is not None:
            run_kwargs = proxy_lifecycle.apply_network(run_kwargs, proxy_runtime)
        container = client.containers.run(resolved, **run_kwargs)
        _ensure_workspace(container, clone_dest)
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
    open_read_grant = proxied and not container_has_token
    if clone_repo and pr is None:
        _, clone_error = _try_clone_into_container(
            container,
            container_id,
            clone_repo,
            clone_dest,
            container_has_token,
            open_read_grant=open_read_grant,
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
                    open_read_grant=open_read_grant,
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

