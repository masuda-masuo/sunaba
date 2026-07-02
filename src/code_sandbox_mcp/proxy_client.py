"""Host-side client for the egress-proxy authorization control API (#357).

Counterpart to the control server in :mod:`code_sandbox_mcp.proxy` (#356).
``publish`` opens a short-lived push window before pushing and closes it after,
so the egress-proxy sidecar lets *that* push through while still rejecting a raw
``git push`` from ``sandbox_exec``.

Runs **host-side** (in the MCP server process), never inside the sandbox
container, so the shared control secret it sends never reaches sandboxed code --
the container therefore cannot open its own push window.

Inert until configured: when ``CODE_SANDBOX_PROXY_CONTROL_URL`` is unset the
context manager is a no-op and ``publish`` behaves exactly as before the egress
proxy exists.  This keeps the change mergeable ahead of the sidecar (#355/#358).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from code_sandbox_mcp.proxy import (
    CONTROL_SECRET_ENV,
    CONTROL_TOKEN_HEADER,
    DEFAULT_WINDOW_TTL_SECONDS,
)

#: Host-facing base URL of the proxy's control API (e.g. ``http://127.0.0.1:9099``
#: or a Docker-network DNS name).  Unset = proxy integration disabled (no-op),
#: so ``publish`` keeps working before the sidecar exists.
CONTROL_URL_ENV = "CODE_SANDBOX_PROXY_CONTROL_URL"

#: Seconds to wait on each control-API call.  The endpoint is a localhost/sidecar
#: hop returning tiny JSON, so this only bounds the failure case (proxy down or
#: wedged), not the happy path.
_CONTROL_TIMEOUT_SECONDS = 5.0

_ALLOW_PATH = "/auth/allow"
_REVOKE_PATH = "/auth/revoke"


class ProxyAuthError(RuntimeError):
    """A configured proxy's control API could not be reached or refused the call.

    Raised only when the proxy *is* configured (``CODE_SANDBOX_PROXY_CONTROL_URL``
    set) but opening the window failed, so ``publish`` fails closed rather than
    pushing unprotected.  Never raised on the unconfigured (inert) path.
    """


@dataclass(frozen=True)
class ProxyControlConfig:
    """Where to reach the proxy control API, and the secret to authenticate with."""

    base_url: str
    secret: str | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ProxyControlConfig | None:
        """Build config from the environment, or ``None`` when the proxy is unset.

        *env* defaults to ``os.environ``; pass an explicit mapping in tests.  A
        blank/whitespace ``CODE_SANDBOX_PROXY_CONTROL_URL`` counts as unset.
        """
        source = os.environ if env is None else env
        base_url = (source.get(CONTROL_URL_ENV) or "").strip()
        if not base_url:
            return None
        secret = source.get(CONTROL_SECRET_ENV) or None
        return cls(base_url=base_url.rstrip("/"), secret=secret)


def _post(config: ProxyControlConfig, path: str, payload: dict[str, object]) -> None:
    """POST *payload* as JSON to ``config.base_url + path``; raise on any failure."""
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if config.secret is not None:
        headers[CONTROL_TOKEN_HEADER] = config.secret
    request = urllib.request.Request(
        config.base_url + path, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_CONTROL_TIMEOUT_SECONDS) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise ProxyAuthError(f"control API {path} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ProxyAuthError(
            f"egress proxy control API unreachable at {config.base_url}: {exc.reason}"
        ) from exc
    if status != 200:
        raise ProxyAuthError(f"control API {path} returned HTTP {status}")


def proxy_configured(env: dict[str, str] | None = None) -> bool:
    """Return ``True`` when the egress-proxy control API is configured.

    ``publish`` uses this to decide whether the push credential should stay
    host-side (handed to the proxy per window, #356) instead of entering the
    container's exec environment.
    """
    return ProxyControlConfig.from_env(env) is not None


def open_window(
    repo: str,
    ttl_seconds: float = DEFAULT_WINDOW_TTL_SECONDS,
    *,
    token: str | None = None,
    config: ProxyControlConfig | None = None,
) -> None:
    """Open a push-authorization window for *repo* (no-op when the proxy is unset).

    *token*, when given, is handed to the proxy as the window-scoped push
    credential (#356): the proxy injects it into the authorized push and
    discards it on revoke/expiry, so the sandbox container never holds it.

    Pass *config* explicitly in tests; in production it is resolved from the
    environment, and a missing configuration makes this a no-op.
    """
    cfg = config or ProxyControlConfig.from_env()
    if cfg is None:
        return
    payload: dict[str, object] = {"repo": repo, "ttl_seconds": ttl_seconds}
    if token:
        payload["token"] = token
    _post(cfg, _ALLOW_PATH, payload)


def close_window(repo: str, *, config: ProxyControlConfig | None = None) -> None:
    """Revoke any push-authorization window for *repo* (no-op when unset)."""
    cfg = config or ProxyControlConfig.from_env()
    if cfg is None:
        return
    _post(cfg, _REVOKE_PATH, {"repo": repo})


@contextmanager
def authorized_push_window(
    repo: str,
    ttl_seconds: float = DEFAULT_WINDOW_TTL_SECONDS,
    *,
    token: str | None = None,
    config: ProxyControlConfig | None = None,
) -> Iterator[None]:
    """Open a push window for *repo*, then always revoke it on exit.

    A no-op when the proxy is unconfigured.  When configured, an ``open`` that
    fails raises :class:`ProxyAuthError` (fail closed).  *token* is forwarded
    to :func:`open_window` as the window-scoped push credential (#356).  The
    ``close`` in the ``finally`` is best-effort: a revoke failure is swallowed
    because the window's TTL guarantees it lapses anyway, and the push has
    already run -- so a proxy hiccup on teardown must not mask the push's real
    outcome.
    """
    cfg = config or ProxyControlConfig.from_env()
    if cfg is None:
        yield
        return
    open_window(repo, ttl_seconds, token=token, config=cfg)
    try:
        yield
    finally:
        try:
            close_window(repo, config=cfg)
        except ProxyAuthError:
            pass
