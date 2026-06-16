"""Static security guardrails for Docker sandbox containers.

These are enforced at container creation time and are separate from
HITL (Human-In-The-Loop) runtime approval mechanisms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Dangerous socket paths that must not be mounted into containers.
#: Mounting the Docker socket grants equivalent root access to the host
#: Docker daemon, allowing container escape and host compromise.
_DANGEROUS_SOCKET_PATTERNS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
)

#: Whitelist of allowed host path prefixes for bind mounts.
#:
#: Only paths under these prefixes can be mounted into sandbox containers.
#: This prevents attackers from reading/writing arbitrary host files
#: (e.g. /etc/passwd, /root/.ssh).
#:
#: .. note::
#:    ``/mnt/`` is included for environments where external volumes are
#:    mounted there (e.g. cloud VM temporary disks).  Review whether this
#:    is appropriate for your deployment — it may expose sensitive mounted
#:    filesystems if not carefully controlled.
_ALLOWED_HOST_MOUNT_PREFIXES: tuple[str, ...] = (
    "/tmp/",
    "/home/",
    "/Users/",
    "/mnt/",
)

#: Default non-root user to run containers as.
#: ``nobody`` is a well-known unprivileged user available in most Linux
#: and official Docker images.
_DEFAULT_USER: str = "nobody"

#: Default memory limit.
#:
#: Chosen as a balance between:
#: - Low enough to contain resource abuse (512 MB prevents runaway processes)
#: - High enough for typical test runs (pytest, linting, compilation)
#: Users can override via explicit ``mem_limit`` in tool call kwargs.
_DEFAULT_MEM_LIMIT: str = "512m"

#: Default memory+swap limit (same as mem to disable swap).
#: Setting this equal to ``mem_limit`` effectively disables swap,
#: preventing disk-based memory pressure attacks.
_DEFAULT_MEMSWAP_LIMIT: str = "512m"

#: CPU period in microseconds (default 100ms).
#: Standard Linux CFS (Completely Fair Scheduler) period.
_DEFAULT_CPU_PERIOD: int = 100000

#: CPU quota in microseconds (50000 = 0.5 CPU cores relative to period).
#: Half a core is sufficient for most test workloads while preventing
#: CPU exhaustion attacks from a single sandbox.
_DEFAULT_CPU_QUOTA: int = 50000

#: PIDs limit (prevents fork bombs).
#: 100 processes is enough for typical test suites (pytest workers,
#: subprocess calls) but low enough to stop fork-based DoS attacks.
_DEFAULT_PIDS_LIMIT: int = 100

#: Compiled pattern for SHA-256 digest references.
#:
#: Security importance: tag references (e.g. ``python:latest``) are
#: mutable — the image a tag points to can change over time, enabling
#: supply-chain attacks and breaking reproducibility.  Digest references
#: (``image@sha256:...``) are immutable and guarantee that the exact
#: same image is used every time, even across different hosts and registries.
_DIGEST_PATTERN: re.Pattern[str] = re.compile(
    r"^.*@sha256:[a-f0-9]{64}$"
)


@dataclass(frozen=True)
class SecurityProfile:
    """Immutable security profile for sandbox containers.

    All settings have safe defaults.  Pass an instance to
    :func:`build_secure_run_kwargs` to apply them.

    To **relax** a restriction (e.g. enable networking):
    ``SecurityProfile(network_mode="bridge")`` or
    ``SecurityProfile(allow_network=True)``.

    To **tighten** a restriction (e.g. empty whitelist):
    ``SecurityProfile(allowed_host_mount_prefixes=())``.
    """

    #: Non-root user to run as (empty string means no override).
    user: str = _DEFAULT_USER

    #: Whether to reject ``privileged=True``.
    forbid_privileged: bool = True

    #: Whether to reject dangerous socket mounts (docker.sock etc.).
    reject_dangerous_sockets: bool = True

    #: Allowed host mount path prefixes.
    #: Empty tuple = no host mounts allowed.
    #: Set to ``None`` to allow all (not recommended).
    allowed_host_mount_prefixes: tuple[str, ...] = _ALLOWED_HOST_MOUNT_PREFIXES

    #: Memory limit (e.g. ``"512m"``, ``"1g"``).
    mem_limit: str = _DEFAULT_MEM_LIMIT

    #: Memory+swap limit.
    memswap_limit: str = _DEFAULT_MEMSWAP_LIMIT

    #: CPU period in microseconds.
    cpu_period: int = _DEFAULT_CPU_PERIOD

    #: CPU quota in microseconds.
    cpu_quota: int = _DEFAULT_CPU_QUOTA

    #: PIDs limit.
    pids_limit: int = _DEFAULT_PIDS_LIMIT

    #: Network mode (default ``"none"`` to disable networking).
    #:
    #: To enable networking for legitimate use cases, override with:
    #: ``SecurityProfile(network_mode="bridge")`` when calling
    #: :func:`build_secure_run_kwargs`.
    network_mode: str = "none"

    #: Whether to require image digest references (``image@sha256:...``).
    require_digest: bool = True


#: Default security profile with all restrictions enabled.
DEFAULT_SECURITY_PROFILE = SecurityProfile()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_image_ref(image: str) -> None:
    """Validate that *image* uses a digest reference.

    Args:
        image: Image reference string.

    Raises:
        ValueError: If the image does not use a ``@sha256:...`` digest.
    """
    if not _DIGEST_PATTERN.match(image):
        raise ValueError(
            f"Image must use a digest reference (image@sha256:...), "
            f"got: {image!r}"
        )


def _is_dangerous_socket(mount_target: str) -> bool:
    """Return True if *mount_target* matches a dangerous socket path."""
    for sock in _DANGEROUS_SOCKET_PATTERNS:
        if sock in mount_target:
            return True
    return False


def _is_allowed_host_path(
    host_path: str,
    allowed_prefixes: tuple[str, ...],
) -> bool:
    """Return True if *host_path* starts with an allowed prefix."""
    for prefix in allowed_prefixes:
        if host_path.startswith(prefix):
            return True
    return False


def _validate_volumes(
    volumes: dict[str, Any],
    allowed_prefixes: tuple[str, ...] | None,
    reject_dangerous_sockets: bool,
) -> None:
    """Validate a Docker volumes dict against security policy.

    Checks for:
    - Dangerous socket mounts (``/var/run/docker.sock`` etc.)
    - Host mount whitelist violations

    Args:
        volumes: Docker volumes dictionary (host_path → config).
        allowed_prefixes: Allowed host path prefixes, or ``None`` to
            allow all paths.
        reject_dangerous_sockets: Whether to reject dangerous sockets.

    Raises:
        ValueError: If any volume violates security policy.
    """
    for host_path, mount_config in volumes.items():
        # Determine the bind target from the mount config
        if isinstance(mount_config, dict):
            bind_target = mount_config.get("bind", host_path)
        elif isinstance(mount_config, str):
            bind_target = mount_config
        elif isinstance(mount_config, (list, tuple)):
            bind_target = mount_config[0] if mount_config else host_path
        else:
            bind_target = str(mount_config)

        # Check dangerous sockets
        if reject_dangerous_sockets and _is_dangerous_socket(bind_target):
            raise ValueError(
                f"Mounting {bind_target} is forbidden by security policy"
            )

        # Check host mount whitelist
        if allowed_prefixes is not None and not _is_allowed_host_path(
            host_path, allowed_prefixes
        ):
            raise ValueError(
                f"Host path {host_path!r} is not in the allowed mount "
                f"whitelist: {allowed_prefixes}"
            )


def _validate_mount_objects(
    mounts: list[Any],
    allowed_prefixes: tuple[str, ...] | None,
    reject_dangerous_sockets: bool,
) -> None:
    """Validate a list of ``docker.types.Mount`` objects.

    Args:
        mounts: List of Mount objects.
        allowed_prefixes: Allowed host path prefixes.
        reject_dangerous_sockets: Whether to reject dangerous sockets.

    Raises:
        ValueError: If any mount violates security policy.
    """
    for mount in mounts:
        source = getattr(mount, "source", None)
        if source is None:
            source = getattr(mount, "Source", "")
        source = str(source)

        if reject_dangerous_sockets and _is_dangerous_socket(source):
            raise ValueError(
                f"Mounting {source} is forbidden by security policy"
            )

        if allowed_prefixes is not None and not _is_allowed_host_path(
            source, allowed_prefixes
        ):
            raise ValueError(
                f"Host path {source!r} is not in the allowed mount "
                f"whitelist: {allowed_prefixes}"
            )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def build_secure_run_kwargs(
    profile: SecurityProfile = DEFAULT_SECURITY_PROFILE,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply security guardrails to container run keyword arguments.

    This function:
    1. Forces non-root user
    2. Rejects ``privileged=True``
    3. Validates volumes against dangerous sockets and host mount whitelist
    4. Applies resource limits (memory, CPU, pids)
    5. Disables networking by default

    Args:
        profile: Security profile to apply.
        **kwargs: Keyword arguments for ``client.containers.run()``.

    Returns:
        Sanitized copy of *kwargs* with security constraints applied.

    Raises:
        ValueError: If a constraint is violated.
    """
    result = dict(kwargs)  # Don't mutate the original

    # 1. Non-root execution
    if profile.user:
        result["user"] = profile.user

    # 2. Privileged mode forbidden
    if profile.forbid_privileged and result.get("privileged", False):
        raise ValueError("Privileged mode is forbidden by security policy")

    # 3-4. Volume/socket validation
    if profile.reject_dangerous_sockets or profile.allowed_host_mount_prefixes is not None:
        volumes = result.get("volumes", {})
        if volumes:
            _validate_volumes(
                volumes,
                allowed_prefixes=profile.allowed_host_mount_prefixes,
                reject_dangerous_sockets=profile.reject_dangerous_sockets,
            )

        mounts = result.get("mounts", [])
        if mounts:
            _validate_mount_objects(
                mounts,
                allowed_prefixes=profile.allowed_host_mount_prefixes,
                reject_dangerous_sockets=profile.reject_dangerous_sockets,
            )

    # 5. Resource limits
    if profile.mem_limit:
        result.setdefault("mem_limit", profile.mem_limit)
    if profile.memswap_limit:
        result.setdefault("memswap_limit", profile.memswap_limit)
    if profile.cpu_period:
        result.setdefault("cpu_period", profile.cpu_period)
    if profile.cpu_quota:
        result.setdefault("cpu_quota", profile.cpu_quota)
    if profile.pids_limit:
        result.setdefault("pids_limit", profile.pids_limit)

    # 6. Network off by default
    result.setdefault("network_mode", profile.network_mode)

    return result
