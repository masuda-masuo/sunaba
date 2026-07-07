"""GitHub App token self-management for streamable-http daemon mode (issue #203, PR-A).

Background
----------
When the MCP server runs as a long-lived ``streamable-http`` daemon it is no
longer launched per-session by mcp-launcher, so the launcher's ``GITHUB_TOKEN``
injection stops happening and every git/gh operation inside sandboxes loses
authentication (same failure mode as shiori #95).

To stay self-sufficient, the server can mint and refresh its *own* short-lived
GitHub App installation token and publish it to ``os.environ["GITHUB_TOKEN"]``.
Host-side token resolution (``_resolve_vcs_token()`` / ``token_broker``) can
now read directly from the provider (:func:`get_global_provider`) instead of
``os.environ["GITHUB_TOKEN"]`` (issue #474).  The env write is retained for
backward compatibility.

Backward compatibility (most important)
---------------------------------------
If the GitHub App env vars are not configured, this module does nothing: the
existing ``GITHUB_TOKEN`` (injected by mcp-launcher or set manually) is left
untouched. Existing deployments are therefore completely unaffected.

Design notes (ported from shiori's ``src/shiori/github_auth.py``)
----------------------------------------------------------------
- installation token lifetime is ~1 hour; refresh once we are within
  ``REFRESH_BEFORE`` seconds of expiry.
- App JWT uses RS256, ``iat`` backdated 60s for clock skew, ``exp`` at 9 min
  (< the 10 min GitHub maximum).
- App config is read straight from the environment (this project has no
  ``Settings`` class, unlike shiori). Private key may be supplied inline via
  ``GITHUB_APP_PRIVATE_KEY`` or via a file path ``GITHUB_APP_PRIVATE_KEY_PATH``.
"""

from __future__ import annotations

import calendar
import logging
import os
import time

import httpx
import jwt

log = logging.getLogger(__name__)

API = "https://api.github.com"

# env var names
ENV_APP_ID = "GITHUB_APP_ID"
ENV_INSTALLATION_ID = "GITHUB_APP_INSTALLATION_ID"
ENV_PRIVATE_KEY = "GITHUB_APP_PRIVATE_KEY"
ENV_PRIVATE_KEY_PATH = "GITHUB_APP_PRIVATE_KEY_PATH"


class AppTokenProvider:
    """Issue and cache a GitHub App installation access token.

    ``get_token()`` re-issues the token when it is missing or within
    ``REFRESH_BEFORE`` seconds of expiry, so a long-lived daemon never serves an
    expired token to sandboxes.
    """

    REFRESH_BEFORE = 300  # re-issue once within 5 minutes of expiry

    def __init__(self, app_id: str, private_key_pem: str, installation_id: str) -> None:
        """Store App credentials. No network call happens until ``get_token()``."""
        self._app_id = app_id
        self._key = private_key_pem
        self._installation_id = installation_id
        self._token: str | None = None
        self._expires_at: float = 0.0  # epoch seconds (UTC)

    def get_token(self) -> str | None:
        """Return a valid installation token, refreshing it if needed."""
        if self._token is None or time.time() > self._expires_at - self.REFRESH_BEFORE:
            self._refresh()
        return self._token

    def _app_jwt(self) -> str:
        """Build the RS256 App-authentication JWT.

        ``iat`` is backdated 60s to absorb clock skew; ``exp`` is 9 min
        (under the 10 min GitHub maximum).
        """
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self._app_id}
        return jwt.encode(payload, self._key, algorithm="RS256")

    def _refresh(self) -> None:
        url = f"{API}/app/installations/{self._installation_id}/access_tokens"
        try:
            resp = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._app_jwt()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )
            if resp.status_code == 401:
                raise RuntimeError(
                    "GitHub App JWT rejected (401). Check that GITHUB_APP_ID matches"
                    " the private key and that the server clock is correct."
                )
            if resp.status_code == 404:
                raise RuntimeError(
                    "Installation not found (404). Check GITHUB_APP_INSTALLATION_ID and"
                    " that the App is installed on the target repositories."
                )
            if resp.status_code == 403:
                raise RuntimeError(
                    "Insufficient permissions (403). Check the App's permissions"
                    " (Contents / Issues / Pull requests) and installed repositories."
                )
            resp.raise_for_status()  # 201 is success
        except httpx.HTTPError as exc:
            # Network blip: reuse a still-valid cached token, otherwise fail.
            if self._token and time.time() < self._expires_at:
                log.warning("token refresh failed, reusing cached token: %s", exc)
                return
            raise RuntimeError(f"failed to obtain installation token: {exc}") from exc

        data = resp.json()
        self._token = data["token"]
        # expires_at like "2026-06-11T12:34:56Z" (UTC) -> epoch seconds.
        parsed = time.strptime(data["expires_at"], "%Y-%m-%dT%H:%M:%SZ")
        self._expires_at = float(calendar.timegm(parsed))
        log.info("issued installation token (expires_at=%s)", data["expires_at"])


def _read_private_key() -> str | None:
    """Return the App private key PEM. PATH wins over inline, else ``None``."""
    path = os.environ.get(ENV_PRIVATE_KEY_PATH)
    if path:
        try:
            with open(path, encoding="utf-8") as fp:
                return fp.read()
        except FileNotFoundError:
            log.warning("private key path %s not found, falling back to inline key", path)
            return os.environ.get(ENV_PRIVATE_KEY)
    return os.environ.get(ENV_PRIVATE_KEY)


def build_app_token_provider() -> AppTokenProvider | None:
    """Build an :class:`AppTokenProvider` from the environment, or ``None``.

    Returns ``None`` when *no* GitHub App env var is set (the backward-compatible
    no-op path: the existing ``GITHUB_TOKEN`` is left as-is). When the config is
    *partially* set, raise ``ValueError`` rather than silently swallowing a
    misconfiguration.
    """
    app_id = os.environ.get(ENV_APP_ID)
    installation_id = os.environ.get(ENV_INSTALLATION_ID)
    pem = _read_private_key()

    app_vars = [app_id, installation_id, pem]
    if not any(app_vars):
        return None
    if not all(app_vars):
        raise ValueError(
            "Incomplete GitHub App configuration. Set GITHUB_APP_ID, "
            "GITHUB_APP_PRIVATE_KEY(_PATH) and GITHUB_APP_INSTALLATION_ID together."
        )
    return AppTokenProvider(app_id, pem, installation_id)  # type: ignore[arg-type]


def setup_github_app_token() -> AppTokenProvider | None:
    """Mint a GitHub App token at startup and publish it to ``GITHUB_TOKEN``.

    Returns the provider on success so the caller can schedule periodic refreshes,
    or ``None`` when no GitHub App is configured (no-op; existing ``GITHUB_TOKEN``
    is preserved untouched).
    """
    provider = build_app_token_provider()
    if provider is None:
        return None
    token = provider.get_token()
    if token:
        os.environ["GITHUB_TOKEN"] = token
        log.info("GITHUB_TOKEN set from GitHub App installation token")
    return provider


# ---------------------------------------------------------------------------
# Global provider singleton (issue #474)
# ---------------------------------------------------------------------------

# Shared between the main thread and the refresh daemon thread
# (server._start_github_app_token_refresh).  The daemon only *reads*
# the provider (via get_global_provider/get_token), so no lock is needed:
# CPython's GIL serialises the single-pointer write in set_global_provider
# and all reads.  No lock needed (issue #474 review).
_GLOBAL_PROVIDER: AppTokenProvider | None = None


def set_global_provider(provider: AppTokenProvider | None) -> None:
    """Register the AppTokenProvider as the global token source.

    Consumers that call :func:`get_global_provider` then use
    ``provider.get_token()`` directly instead of reading
    ``os.environ["GITHUB_TOKEN"]`` — making the env-write refresh
    thread's side effect replaceable (issue #474).
    """
    global _GLOBAL_PROVIDER  # noqa: PLW0603
    _GLOBAL_PROVIDER = provider


def get_global_provider() -> AppTokenProvider | None:
    """Return the registered :class:`AppTokenProvider`, or ``None``."""
    return _GLOBAL_PROVIDER
