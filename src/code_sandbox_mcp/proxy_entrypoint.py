"""Container entrypoint for the egress-proxy sidecar (Issue #358, part of #353).

Runs **inside the proxy sidecar image** (``docker/Dockerfile.proxy``) as the
container entrypoint: it assembles the ``mitmdump`` command line from the
environment and ``exec``s it, so the running proxy loads the :mod:`proxy` addon
that gates ``git push`` at the network layer.

The addon itself (:mod:`code_sandbox_mcp.proxy`) self-configures from the
environment -- allowlist, injected token, and the authorization control
server -- so this module only has to decide *how* ``mitmdump`` is launched:
which address to listen on, which port, where to keep the CA/config dir, and
which addon file to load.  Those are distinct from the addon's own variables so
the two concerns do not collide.

Deliberately stdlib-only with no imports from the rest of the package, so the
image can copy just this file plus ``proxy.py`` (rather than installing the
whole MCP server) and run ``python proxy_entrypoint.py``.  The argv assembly is
factored into the pure :func:`build_mitmdump_argv` so it is unit-testable
without a container or mitmproxy installed.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, Sequence

#: Interface the proxy listens on.  Defaults to all interfaces because the
#: sandbox container reaches the sidecar across a Docker network, not loopback.
LISTEN_HOST_ENV = "CODE_SANDBOX_PROXY_LISTEN_HOST"
DEFAULT_LISTEN_HOST = "0.0.0.0"

#: TCP port the proxy listens on (matches ``HTTPS_PROXY=http://proxy:8080``).
LISTEN_PORT_ENV = "CODE_SANDBOX_PROXY_LISTEN_PORT"
DEFAULT_LISTEN_PORT = 8080

#: Where mitmproxy keeps its generated CA and config.  A fixed, writable path so
#: the lifecycle wiring (#358 follow-up) can read ``mitmproxy-ca-cert.pem`` from
#: it and mount it into the sandbox container's trust store.
CONFDIR_ENV = "CODE_SANDBOX_PROXY_CONFDIR"
DEFAULT_CONFDIR = "/certs"

#: Path to the addon script inside the image; the Dockerfile copies ``proxy.py``
#: here.  Overridable mainly so tests and local runs can point elsewhere.
ADDON_ENV = "CODE_SANDBOX_PROXY_ADDON"
DEFAULT_ADDON = "/app/proxy.py"


class ProxyEntrypointError(RuntimeError):
    """The sidecar was misconfigured (e.g. a non-numeric listen port).

    Raised only for configuration the entrypoint cannot proceed with; a clear
    message is printed and the container exits non-zero rather than launching a
    proxy that would silently mis-listen.
    """


def _resolve(env: Mapping[str, str], name: str, default: str) -> str:
    """Return the trimmed value of *name*, or *default* when unset/blank."""
    return (env.get(name) or "").strip() or default


def _parse_port(raw: str | None) -> int:
    """Parse a listen port, raising :class:`ProxyEntrypointError` if invalid.

    A blank/unset value yields :data:`DEFAULT_LISTEN_PORT`; anything present
    must be an integer in the valid TCP range so a typo fails loudly at startup
    rather than binding an unexpected port.
    """
    text = (raw or "").strip()
    if not text:
        return DEFAULT_LISTEN_PORT
    try:
        port = int(text)
    except ValueError:
        raise ProxyEntrypointError(
            f"{LISTEN_PORT_ENV} must be an integer, got {raw!r}"
        ) from None
    if not 1 <= port <= 65535:
        raise ProxyEntrypointError(
            f"{LISTEN_PORT_ENV} must be in 1..65535, got {port}"
        )
    return port


def build_mitmdump_argv(env: Mapping[str, str]) -> list[str]:
    """Build the ``mitmdump`` argv that launches the proxy with the addon.

    Reads the listen host/port, config dir, and addon path from *env* (falling
    back to the module defaults) and returns a ready-to-``exec`` argument
    vector.  ``mitmdump`` runs in its default *regular* HTTP-proxy mode, which
    is what git uses via ``HTTPS_PROXY``.  Pure: it neither reads the process
    environment nor touches the filesystem, so it can be asserted on directly.
    """
    host = _resolve(env, LISTEN_HOST_ENV, DEFAULT_LISTEN_HOST)
    port = _parse_port(env.get(LISTEN_PORT_ENV))
    confdir = _resolve(env, CONFDIR_ENV, DEFAULT_CONFDIR)
    addon = _resolve(env, ADDON_ENV, DEFAULT_ADDON)
    return [
        "mitmdump",
        "-s",
        addon,
        "--listen-host",
        host,
        "--listen-port",
        str(port),
        "--set",
        f"confdir={confdir}",
    ]


def main(
    env: Mapping[str, str] | None = None,
    exec_fn: Callable[[str, Sequence[str]], None] = os.execvp,
) -> int:
    """Assemble the mitmdump command and ``exec`` it (the container entrypoint).

    *exec_fn* defaults to :func:`os.execvp`, which replaces this process, so on
    success this never returns.  A configuration error prints to stderr and
    returns ``2``; if *exec_fn* returns at all (mitmdump missing on ``PATH``),
    that is treated as a launch failure and returns ``1``.  Both *env* and
    *exec_fn* are injectable so the flow is testable without a real exec.
    """
    environ = os.environ if env is None else env
    try:
        argv = build_mitmdump_argv(environ)
    except ProxyEntrypointError as exc:
        print(f"proxy entrypoint: {exc}", file=sys.stderr)
        return 2
    exec_fn(argv[0], argv)
    # os.execvp only returns on failure (it otherwise replaces the process).
    print(f"proxy entrypoint: failed to exec {argv[0]!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    sys.exit(main())
