"""Host-side lifecycle for the egress-proxy sidecar (#358, Epic #353).

``sandbox_initialize`` (and ``run_container_and_exec``) call
:func:`ensure_egress_proxy` before starting a networked sandbox container.
It idempotently starts the shared proxy sidecar (built by
``docker/Dockerfile.proxy``) and the dedicated Docker network, then returns
the wiring the sandbox needs: proxy env vars, the network to join, and the
proxy's CA certificate to install (:func:`install_ca`).

Enabled by default; set ``SUNABA_ENABLE_EGRESS_PROXY=false`` to opt
out.  When the proxy cannot be started, callers must **fail closed** --
refuse to start a networked sandbox rather than fall
back to an unproxied bridge network.

Topology (one sidecar shared by all sandboxes)::

    [internet] <-> [proxy sidecar] <-> [internal docker network] <-> [sandbox]
                        |
              control API published on host loopback only
              (publish opens push grants through it, #357)

The sandbox-facing network is created ``internal=True`` so the *only* route
out of a sandbox is the proxy itself -- SSH and direct IP egress have no
route at all (#355's requirement falls out of the topology).  The proxy
container additionally sits on the default bridge for upstream access.

Runs **host-side** (in the MCP server process), never inside the sandbox:
the control secret is passed to the proxy container and exported to this
process's environment for :mod:`sunaba.proxy_client`, but is never
added to a sandbox container's environment.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import tarfile
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from sunaba.image_pins import load_proxy_pin
from sunaba.proxy import (
    ALLOWED_EGRESS_HOSTS_ENV,
    ALLOWED_REPOS_ENV,
    CONTROL_HOST_ENV,
    CONTROL_PORT_ENV,
    CONTROL_SECRET_ENV,
    PROXY_SOURCE_FINGERPRINT,
    PROXY_TOKEN_ENV,
)
from sunaba.proxy_client import (
    CONTROL_URL_ENV,
    ProxyControlConfig,
    fetch_proxy_fingerprint,
)
from sunaba.security import MANAGED_LABEL

logger = logging.getLogger(__name__)

#: Feature flag: the whole egress-proxy integration is opt-out (default on).
#: Set to ``false``, ``0``, ``off``, or ``no`` to disable.
#: Absent = enabled (default).
ENABLE_EGRESS_PROXY_ENV = "SUNABA_ENABLE_EGRESS_PROXY"

#: Env override for the sidecar image reference (an explicit operator escape
#: hatch that wins over the pin below).  Resolution order lives in
#: :func:`_resolve_proxy_image`.
PROXY_IMAGE_ENV = "SUNABA_PROXY_IMAGE"

#: Locally built fallback tag from ``docker/Dockerfile.proxy``.  Used only when
#: neither the env override nor a CI-published GHCR pin is available -- i.e. for
#: local development builds and the bootstrap grant before the first proxy
#: image is pushed (#432).  This ``:latest`` tag can silently drift from the
#: installed package, which is exactly why :func:`_warn_on_source_drift` warns
#: on a source mismatch (#405) and why CI now pins the image by digest (#432).
_DEFAULT_PROXY_IMAGE = "sunaba/proxy:latest"


def _resolve_proxy_image(source: MutableMapping[str, str]) -> str:
    """Resolve the sidecar image ref: env override -> GHCR pin -> local tag.

    Order of precedence:

    1. ``SUNABA_PROXY_IMAGE`` -- an explicit operator override always wins.
    2. ``proxy_pin.json`` -- the digest pin CI publishes after building and
       pushing ``docker/Dockerfile.proxy`` (#432).  This is the default on a
       deployed server, so a plain ``pip install`` of the package now picks up
       the matching sidecar the same way it picks up the sandbox variant pins
       -- closing the "sidecar left stale after a server redeploy" gap that made
       #419's read-grant auth appear broken until the image was rebuilt by hand.
    3. :data:`_DEFAULT_PROXY_IMAGE` -- the locally built ``:latest`` tag, used
       before CI has published a pin (bootstrap) and for local dev builds.
    """
    override = (source.get(PROXY_IMAGE_ENV) or "").strip()
    if override:
        return override
    return load_proxy_pin() or _DEFAULT_PROXY_IMAGE

#: Host loopback port the control API is published on (``127.0.0.1`` only;
#: the server and dashboard use 8750/8751, so the sidecar takes the next one).
CONTROL_HOST_PORT_ENV = "SUNABA_PROXY_CONTROL_HOST_PORT"
_DEFAULT_CONTROL_HOST_PORT = 8768

#: Dedicated sandbox-facing network (``internal=True``: no direct egress).
EGRESS_NETWORK_NAME = "sunaba-egress"

#: Fixed sidecar container name -- the idempotency key for reuse.
PROXY_CONTAINER_NAME = "sunaba-egress-proxy"

#: DNS alias sandboxes use to reach the proxy on the internal network.
PROXY_NETWORK_ALIAS = "egress-proxy"

#: mitmproxy listen port inside the sidecar (``docker/Dockerfile.proxy``).
_PROXY_LISTEN_PORT = 8080

#: Control-API port inside the sidecar (bound ``0.0.0.0`` so the Docker
#: port publish reaches it; the shared secret gates every request).
_CONTROL_PORT = 9099

#: mitmproxy confdir inside the sidecar (set by the image).  Backed by the
#: :data:`CERTS_VOLUME_NAME` named volume so the CA survives sidecar
#: recreation (#400).
_CERTS_DIR_IN_PROXY = "/certs"

#: Named volume mounted at :data:`_CERTS_DIR_IN_PROXY`.  mitmproxy reuses an
#: existing CA in its confdir, so persisting the directory keeps the CA
#: stable across sidecar recreations (allowlist changes, image updates,
#: crashes) -- running sandboxes that trust the old CA keep working (#400).
#: Deliberate CA rotation: ``docker volume rm sunaba-egress-certs``
#: (with the sidecar removed), then let the next start regenerate it.
CERTS_VOLUME_NAME = "sunaba-egress-certs"

#: Where mitmproxy writes its generated CA inside the sidecar.
_CA_PATH_IN_PROXY = f"{_CERTS_DIR_IN_PROXY}/mitmproxy-ca-cert.pem"

#: Where the CA lands in the sandbox; ``update-ca-certificates`` folds it
#: into the system bundle so git/curl trust the TLS-terminating proxy.
CA_CERT_PATH_IN_SANDBOX = "/usr/local/share/ca-certificates/sunaba-egress-ca.crt"

#: Debian system bundle (includes the proxy CA after ``update-ca-certificates``).
_SYSTEM_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

#: How long to wait for mitmproxy to generate its CA on first start.
_CA_WAIT_SECONDS = 30.0
_CA_POLL_INTERVAL_SECONDS = 0.5

#: How long the #405 source-fingerprint probe waits for a freshly-created
#: sidecar's control API to accept requests before giving up.  Kept short: on
#: reuse the API is already up so the first attempt succeeds; this only covers
#: the thin grant on *first* creation between ``container.start()`` and the
#: control server binding its port -- which is exactly when a just-rebuilt
#: image would drift, so losing detection there would defeat the feature.
#: Exceeding it means "cannot compare" and the check is skipped (fail open).
_FINGERPRINT_READY_WAIT_SECONDS = 3.0
_FINGERPRINT_POLL_INTERVAL_SECONDS = 0.25

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "off", "no"})

#: Config the sidecar reads *once*, at startup, from the env it was created
#: with.  Drift between these and the current host env is what
#: :func:`_recreate_reason` looks for (#533): the guard bakes the allowlists
#: into itself when it boots, so a running sidecar silently keeps enforcing
#: whatever was configured when it started.
_SIDECAR_CONFIG_ENV_KEYS = (ALLOWED_REPOS_ENV, ALLOWED_EGRESS_HOSTS_ENV, PROXY_TOKEN_ENV)


class EgressProxyError(RuntimeError):
    """The egress proxy is enabled but could not be started or wired up.

    Callers must fail closed on this: a networked sandbox must not start
    without the proxy when ``SUNABA_ENABLE_EGRESS_PROXY`` is on.
    """


@dataclass(frozen=True)
class EgressProxyRuntime:
    """Wiring a sandbox container needs to route its egress via the sidecar."""

    #: Sandbox-facing internal network to join instead of the bridge.
    network_name: str

    #: Proxy URL for ``HTTP(S)_PROXY`` inside the sandbox.
    proxy_url: str

    #: Host-side control-API base URL (for ``publish``'s push grant, #357).
    control_url: str

    #: The proxy's CA certificate (PEM) to install into the sandbox.
    ca_pem: bytes


def egress_proxy_enabled(env: MutableMapping[str, str] | None = None) -> bool:
    """Return whether the egress-proxy integration is enabled.

    Default is ``True`` (on); set ``SUNABA_ENABLE_EGRESS_PROXY=false``
    to opt out.
    """
    source = os.environ if env is None else env
    raw = (source.get(ENABLE_EGRESS_PROXY_ENV) or "").strip().lower()
    if not raw:
        return True  # absent -> default on
    return raw not in _FALSY


def ensure_egress_proxy(
    client: Any,
    env: MutableMapping[str, str] | None = None,
) -> EgressProxyRuntime:
    """Start (or reuse) the proxy sidecar + network; return the sandbox wiring.

    Idempotent per host: one sidecar (fixed name) and one internal network are
    shared by all sandbox containers.  Also exports the control URL/secret
    into *env* (default ``os.environ``) so :mod:`proxy_client`'s
    ``from_env`` -- and therefore ``publish``'s push grant (#357) -- picks
    them up with no further configuration.

    The allowlist/token env vars are baked into the sidecar at creation and
    read once by the guard at startup, so a sidecar that is still running
    would otherwise keep enforcing a stale config.  This call therefore
    reconciles: a reused sidecar whose baked config no longer matches the host
    env is recreated (#533).  Recreation is safe for running sandboxes: the CA
    lives in the :data:`CERTS_VOLUME_NAME` named volume and stays the same
    (#400), and the new sidecar rejoins the network under the same alias.

    Raises:
        EgressProxyError: When the sidecar cannot be started or its CA does
            not appear.  Callers must fail closed.
    """
    source = os.environ if env is None else env
    try:
        network = _ensure_network(client)
        container = _find_proxy_container(client)
        secret = _recover_secret(container) if container is not None else None
        if container is not None:
            reason = _recreate_reason(container, secret, source)
            if reason is not None:
                logger.info(
                    "Recreating egress-proxy container %s (%s)", container.id[:12], reason
                )
                container.remove(force=True)
                container = None
                secret = None
        if container is None or secret is None:
            secret = source.get(CONTROL_SECRET_ENV) or secrets.token_hex(32)
            container = _start_proxy_container(client, network, secret, source)
        ca_pem = _wait_for_ca(container)
    except EgressProxyError:
        raise
    except Exception as e:
        raise EgressProxyError(f"failed to start egress-proxy sidecar: {e}") from e

    control_url = f"http://127.0.0.1:{_published_control_port(container, source)}"
    # publish (#357) reads proxy_client.ProxyControlConfig.from_env() at call
    # time, so exporting here is all it takes to arm the push grant.
    source[CONTROL_URL_ENV] = control_url
    source[CONTROL_SECRET_ENV] = secret
    # Purely diagnostic (#405): a mismatch means the running sidecar was built
    # from a different proxy.py than this server -- warn, but never let the
    # probe itself keep a sandbox from starting.
    try:
        _warn_on_source_drift(control_url, secret)
    except Exception:  # pragma: no cover - defensive; diagnostics must not fail closed
        logger.debug("egress-proxy source fingerprint check errored", exc_info=True)
    return EgressProxyRuntime(
        network_name=EGRESS_NETWORK_NAME,
        proxy_url=f"http://{PROXY_NETWORK_ALIAS}:{_PROXY_LISTEN_PORT}",
        control_url=control_url,
        ca_pem=ca_pem,
    )


def sandbox_proxy_env(runtime: EgressProxyRuntime) -> dict[str, str]:
    """Env vars pointing a sandbox's tools at the proxy and its CA.

    The CA paths reference files :func:`install_ca` creates right after the
    container starts, before anything in the sandbox touches the network.
    ``SSL_CERT_FILE``/``REQUESTS_CA_BUNDLE``/``PIP_CERT`` point at the *full
    system bundle* (which contains the proxy CA after install), so trust in
    regular public CAs is preserved, not replaced.
    """
    proxy = runtime.proxy_url
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        "SSL_CERT_FILE": _SYSTEM_CA_BUNDLE,
        "REQUESTS_CA_BUNDLE": _SYSTEM_CA_BUNDLE,
        "PIP_CERT": _SYSTEM_CA_BUNDLE,
        "GIT_SSL_CAINFO": _SYSTEM_CA_BUNDLE,
        # Node keeps its builtin roots and *adds* this file, so it gets the
        # bare proxy CA rather than the system bundle.
        "NODE_EXTRA_CA_CERTS": CA_CERT_PATH_IN_SANDBOX,
    }


def apply_network(run_kwargs: dict[str, Any], runtime: EgressProxyRuntime) -> dict[str, Any]:
    """Swap the bridge ``network_mode`` for the proxy's internal network.

    docker-py treats ``network`` and ``network_mode`` as mutually exclusive,
    so the ``"bridge"`` that ``build_secure_run_kwargs`` sets for networked
    profiles is removed rather than overridden.
    """
    kwargs = dict(run_kwargs)
    kwargs.pop("network_mode", None)
    kwargs["network"] = runtime.network_name
    return kwargs


def install_ca(container: Any, ca_pem: bytes) -> None:
    """Install the proxy CA into a sandbox container's trust store.

    Writes the PEM under ``/usr/local/share/ca-certificates/`` and runs
    ``update-ca-certificates`` (as root -- the sandbox user cannot, which is
    the point: the trust store is wired by the host, not by sandboxed code).
    Falls back to appending to the system bundle directly if the Debian
    helper is unavailable.

    Raises:
        EgressProxyError: When the CA cannot be installed; callers must fail
            closed (TLS through the proxy would break in confusing ways).
    """
    cert_dir, cert_name = os.path.split(CA_CERT_PATH_IN_SANDBOX)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=cert_name)
        info.size = len(ca_pem)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(ca_pem))
    # docker-py raises APIError on failure, but the return value is also
    # specified -- check it too rather than assume (fail closed).
    if not container.put_archive(cert_dir, buf.getvalue()):
        raise EgressProxyError(f"put_archive of the proxy CA into {cert_dir} was refused")

    exit_code, output = container.exec_run(["update-ca-certificates"], user="root")
    if exit_code == 0:
        return
    logger.warning(
        "update-ca-certificates failed (exit %s), appending to bundle: %s",
        exit_code,
        _tail(output),
    )
    exit_code, output = container.exec_run(
        ["sh", "-c", f"cat {CA_CERT_PATH_IN_SANDBOX} >> {_SYSTEM_CA_BUNDLE}"],
        user="root",
    )
    if exit_code != 0:
        raise EgressProxyError(f"could not install proxy CA into sandbox: {_tail(output)}")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _warn_on_source_drift(control_url: str, secret: str) -> None:
    """Log a warning when the sidecar's addon source differs from ours (#405).

    Fetches the running sidecar's baked source fingerprint over the control
    API's ``/version`` endpoint and compares it against
    :data:`PROXY_SOURCE_FINGERPRINT` -- the digest this process computed from
    its *own* installed ``proxy.py`` at import.  A mismatch is the #404 failure
    mode (venv on new ``proxy.py``, sidecar baked from old ``proxy.py``), which
    otherwise stays invisible until the first push fails.

    Never raises and never fails closed: if the fingerprint cannot be fetched
    or either side's digest is empty ("cannot compare"), it silently does
    nothing.  The fix for a real mismatch is a deploy action (rebuild the image
    + recreate the sidecar), not aborting the sandbox.

    On *first* sidecar creation the control API may not be listening the instant
    ``container.start()`` returns, so this polls ``/version`` for up to
    :data:`_FINGERPRINT_READY_WAIT_SECONDS` before giving up -- otherwise a
    just-rebuilt image would race past detection on the very deploy that
    introduced the drift.  Residual limitation: if the sidecar stays
    unreachable past that grant (a genuinely wedged proxy) or predates the
    ``/version`` endpoint (an old image, which returns 404), the check is
    skipped for this start; a subsequent start against the now-ready, reused
    sidecar detects it.
    """
    local = PROXY_SOURCE_FINGERPRINT
    if not local:
        return
    config = ProxyControlConfig(base_url=control_url, secret=secret)
    deadline = time.monotonic() + _FINGERPRINT_READY_WAIT_SECONDS
    while True:
        remote = fetch_proxy_fingerprint(config)
        if remote is not None:
            break
        if time.monotonic() >= deadline:
            return  # never became reachable in the grant -- cannot compare
        time.sleep(_FINGERPRINT_POLL_INTERVAL_SECONDS)
    if remote == local:
        return
    logger.warning(
        "egress-proxy sidecar source fingerprint %s does not match this "
        "server's installed proxy.py %s: the running sidecar was built from a "
        "different proxy.py than the deployed package (see #405). Rebuild the "
        "proxy image (docker/Dockerfile.proxy) and recreate the sidecar "
        "(remove %s) to resync.",
        remote[:12],
        local[:12],
        PROXY_CONTAINER_NAME,
    )


def _ensure_network(client: Any) -> Any:
    """Return the internal sandbox-facing network, creating it if missing."""
    import docker.errors

    try:
        return client.networks.get(EGRESS_NETWORK_NAME)
    except docker.errors.NotFound:
        logger.info("Creating egress network %s (internal)", EGRESS_NETWORK_NAME)
        return client.networks.create(
            EGRESS_NETWORK_NAME,
            driver="bridge",
            internal=True,
            labels={MANAGED_LABEL: "true"},
        )


def _ensure_certs_volume(client: Any) -> None:
    """Ensure the CA named volume exists, labelled as managed.

    Docker would auto-create a missing named volume on container start, but
    without labels; creating it explicitly keeps it discoverable next to the
    other managed resources (the orphan reaper only ever touches containers,
    so the label carries no GC risk).
    """
    import docker.errors

    try:
        client.volumes.get(CERTS_VOLUME_NAME)
    except docker.errors.NotFound:
        logger.info("Creating CA volume %s", CERTS_VOLUME_NAME)
        client.volumes.create(CERTS_VOLUME_NAME, labels={MANAGED_LABEL: "true"})


def _find_proxy_container(client: Any) -> Any | None:
    """Return the sidecar container by its fixed name, or ``None``."""
    import docker.errors

    try:
        return client.containers.get(PROXY_CONTAINER_NAME)
    except docker.errors.NotFound:
        return None


def _start_proxy_container(
    client: Any,
    network: Any,
    secret: str,
    source: MutableMapping[str, str],
) -> Any:
    """Create and start the sidecar; attach it to the internal network.

    The container is created on the default bridge (for upstream internet and
    the loopback port publish -- ``internal`` networks cannot publish ports),
    then *additionally* connected to the internal network under the
    :data:`PROXY_NETWORK_ALIAS` DNS name sandboxes use.

    The confdir is backed by the :data:`CERTS_VOLUME_NAME` named volume so
    the CA persists across recreations (#400).  On the volume's first mount
    Docker copies the image's ``/certs`` into it -- including its
    ``egressproxy`` ownership -- so the non-root mitmproxy can write there.
    """
    image = _resolve_proxy_image(source)
    _ensure_certs_volume(client)
    proxy_env = {
        CONTROL_PORT_ENV: str(_CONTROL_PORT),
        CONTROL_HOST_ENV: "0.0.0.0",
        CONTROL_SECRET_ENV: secret,
    }
    for key in _SIDECAR_CONFIG_ENV_KEYS:
        value = source.get(key)
        if value:
            proxy_env[key] = value
    logger.info("Starting egress-proxy sidecar %s (image=%s)", PROXY_CONTAINER_NAME, image)
    container = client.containers.run(
        image,
        detach=True,
        name=PROXY_CONTAINER_NAME,
        environment=proxy_env,
        labels={MANAGED_LABEL: "true"},
        volumes={CERTS_VOLUME_NAME: {"bind": _CERTS_DIR_IN_PROXY, "mode": "rw"}},
        ports={f"{_CONTROL_PORT}/tcp": ("127.0.0.1", _control_host_port(source))},
        # unless-stopped (not always): it still auto-starts after a daemon
        # restart; only a manually `docker stop`ped sidecar stays down, which
        # keeps the operator's off switch meaningful.  A missing sidecar is
        # fail closed either way -- sandboxes sit on an internal-only network,
        # so without the proxy they have no egress at all, and the next
        # ensure_egress_proxy() recreates it.
        restart_policy={"Name": "unless-stopped"},
    )
    network.connect(container, aliases=[PROXY_NETWORK_ALIAS])
    return container


def _control_host_port(source: MutableMapping[str, str]) -> int:
    """Host loopback port to publish the control API on."""
    raw = (source.get(CONTROL_HOST_PORT_ENV) or "").strip()
    return int(raw) if raw else _DEFAULT_CONTROL_HOST_PORT


def _published_control_port(container: Any, source: MutableMapping[str, str]) -> int:
    """Recover the actually-published control port from a (reused) sidecar.

    A sidecar surviving a server restart may have been published on a port
    configured differently from the current environment, so the container's
    own ``HostConfig.PortBindings`` wins over the env/default.
    """
    bindings = (container.attrs.get("HostConfig") or {}).get("PortBindings") or {}
    for binding in bindings.get(f"{_CONTROL_PORT}/tcp") or []:
        host_port = (binding or {}).get("HostPort")
        if host_port:
            return int(host_port)
    return _control_host_port(source)


def _container_env(container: Any) -> dict[str, str]:
    """Read a container's baked env back out of ``docker inspect``.

    ``docker inspect`` already exposes the container's env to anyone with
    Docker access, so this adds no exposure beyond what starting the sidecar
    with those values already implies -- and none of it is sandbox-visible.

    Read via ``attrs`` (inspect) rather than ``exec_run(["printenv", ...])``
    deliberately: inspect works even when the container has no exec-able
    state, and mirrors how :func:`_published_control_port` recovers the port.
    """
    env: dict[str, str] = {}
    for item in (container.attrs.get("Config") or {}).get("Env") or []:
        key, sep, value = item.partition("=")
        if sep:
            env[key] = value
    return env


def _recover_secret(container: Any) -> str | None:
    """Read the control secret back from an existing sidecar's own env."""
    return _container_env(container).get(CONTROL_SECRET_ENV) or None


def _recreate_reason(
    container: Any,
    secret: str | None,
    source: MutableMapping[str, str],
) -> str | None:
    """Why the existing sidecar cannot be reused, or ``None`` to reuse it."""
    if container.status != "running":
        return f"container is {container.status}"
    if secret is None:
        # Unusable for publish's push grant, which needs the control secret.
        return "control secret not recoverable"
    drifted = _config_drift(container, source)
    if drifted:
        return "config changed: " + ", ".join(drifted)
    return None


def _config_drift(container: Any, source: MutableMapping[str, str]) -> list[str]:
    """Return the config keys on which the running sidecar is out of date (#533).

    The sidecar's guard reads the allowlists and push token from its env once,
    at startup, and ``restart_policy=unless-stopped`` keeps it alive across
    both a server and a Docker daemon restart -- so without this check,
    changing ``SUNABA_ALLOWED_REPOS`` on the host appeared to do nothing and
    ``publish`` kept failing closed against the old allowlist.

    Comparing the *inspected* env against the current one needs no bookkeeping
    on the container (no config-hash label), and so also works for sidecars
    left behind by versions that predate this check.
    """
    baked = _container_env(container)
    drifted = [
        key for key in _SIDECAR_CONFIG_ENV_KEYS if baked.get(key, "") != (source.get(key) or "")
    ]
    # The published port is baked in the same way -- :func:`_published_control_port`
    # lets the container's binding win precisely because it cannot be changed in
    # place -- so a port change belongs on the same recreation path.
    if _published_control_port(container, source) != _control_host_port(source):
        drifted.append(CONTROL_HOST_PORT_ENV)
    return drifted


def _wait_for_ca(
    container: Any,
    timeout: float = _CA_WAIT_SECONDS,
    interval: float = _CA_POLL_INTERVAL_SECONDS,
) -> bytes:
    """Poll the sidecar until mitmproxy has generated its CA; return the PEM."""
    deadline = time.monotonic() + timeout
    while True:
        exit_code, output = container.exec_run(["cat", _CA_PATH_IN_PROXY])
        if exit_code == 0 and b"BEGIN CERTIFICATE" in output:
            return output
        if time.monotonic() >= deadline:
            raise EgressProxyError(
                f"egress proxy CA did not appear at {_CA_PATH_IN_PROXY} "
                f"within {timeout:.0f}s (is the sidecar healthy?)"
            )
        time.sleep(interval)


def _tail(output: bytes | str | None, limit: int = 300) -> str:
    """Last *limit* characters of exec output, for error messages."""
    if output is None:
        return ""
    text = output.decode(errors="replace") if isinstance(output, bytes) else str(output)
    return text[-limit:]
