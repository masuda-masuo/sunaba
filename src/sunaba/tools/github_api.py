"""GitHub REST API client — host-side, no credential enters the container.

Extracted from tools/vcs.py (issue #648).  All host-side HTTP calls go through
a single internal wrapper; pure pagination logic (``_link_next_url``,
``_fetch_all_pages``) is separated for mock-free testing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

from sunaba import github_auth, token_broker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token resolution (Issue #347 lazy injection)
# ---------------------------------------------------------------------------


def _resolve_vcs_token() -> str:
    """Resolve a VCS token host-side for lazy injection at call time (Issue #347).

    The token is *not* bound to container start: the host (this MCP server
    process) can always obtain it.  Returning it here lets callers such as
    :func:`publish` inject the credential into a single ``docker exec`` (a
    push) — instead of requiring the token to have been baked into the
    container's environment at ``sandbox_initialize`` time.  This removes the
    "no-token start → must re-init to push" penalty while keeping
    least-privilege: containers that never need a credential never receive one.

    Despite the name's push-era origin, this is a general host-side token
    resolver: it also backs authenticated host->GitHub-API GET calls
    (``_resolve_pr_head_ref``, ``issue_write``) and, since #419, egress-proxy
    read-authorization grants (``authorized_read_grant``) for
    ``sandbox_initialize``. There is nothing push-specific in
    its resolution order (broker mint -> static ``GITHUB_TOKEN``/``GH_TOKEN``).

    Resolution order: a freshly minted broker token (Issue #232) takes
    precedence, then the global ``AppTokenProvider`` (issue #474), then
    the static host ``GITHUB_TOKEN`` / ``GH_TOKEN``.  Returns an empty
    string when no token is available, in which case the push has no
    credential and fails cleanly -- the container carries none of its
    own (#356/#439).

    Note: the ``AppTokenProvider.get_token()`` step can raise
    ``RuntimeError`` if the GitHub API is unreachable *and* no
    previously-cached token is usable — this is intentional: the caller
    should surface the failure rather than silently falling back to a
    stale env var.  Broker mint and env var reads never raise.
    """
    minted = token_broker.mint_token()
    if minted:
        return minted
    provider = github_auth.get_global_provider()
    if provider is not None:
        token = provider.get_token()
        if token:
            return token
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
    return ""


def _push_token_env(token: str) -> dict[str, str] | None:
    """Build the ephemeral exec environment carrying *token*, or ``None``.

    The returned mapping is passed only to the ``docker exec`` calls that
    actually need credentials (git push, ``gh pr create``, the API-push
    fallback).  Because it lives solely in that exec's process environment
    it leaves nothing behind in the container — no env var on the long-lived
    container, no file, no credential store (Issue #347 ephemerality).

    Docker's exec ``Env`` is **additive**: these vars are merged onto the
    container's existing environment rather than replacing it, so ``PATH`` /
    ``HOME`` stay intact and ``git`` / ``gh`` / ``python3`` still resolve
    inside the push exec (verified against docker-py 7.1.0 / Docker exec
    semantics).  We therefore only need to carry the token here, not a full
    environment.
    """
    if not token:
        return None
    return {"GITHUB_TOKEN": token, "GH_TOKEN": token}


# ---------------------------------------------------------------------------
# Single-request wrapper
# ---------------------------------------------------------------------------


def _github_api_request(
    path: str,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the GitHub REST API from the host process; return the JSON body.

    Runs host-side like :func:`_resolve_vcs_token` — never inside the
    container — so no credential enters the sandbox and the request does not
    traverse the egress proxy (#360).  The REST API accepts ``Bearer``; the
    Basic-only quirk applies to git smart-HTTP endpoints only (PR #404).

    *token* may be empty for an anonymous request (e.g. a public-repo read
    such as :func:`issue_view`); no ``Authorization`` header is sent in that
    case, rather than one carrying an empty bearer value.

    Raises:
        RuntimeError: On an HTTP error (carrying GitHub's ``message`` /
            ``errors`` when present; raw body as fallback when JSON
            parsing fails) or an unreachable network.
    """
    import urllib.error
    import urllib.request

    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "sunaba")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        parts: list[str] = []
        raw_body = ""
        try:
            raw_body = e.read().decode("utf-8", errors="replace")
            body = json.loads(raw_body)
            if body.get("message"):
                parts.append(str(body["message"]))
            parts.extend(str(err.get("message", err)) for err in body.get("errors") or [])
        except Exception:  # noqa: BLE001 - the error body is diagnostics only
            if raw_body:
                parts.append(f"raw body: {raw_body[:500]}")
        detail = f": {'; '.join(parts)}" if parts else ""
        raise RuntimeError(
            f"GitHub API {method} {path} returned HTTP {e.code}{detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GitHub API {method} {path} failed: {e.reason}") from e


# ---------------------------------------------------------------------------
# List-returning wrappers
# ---------------------------------------------------------------------------


def _github_api_request_list(
    path: str,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Call the GitHub REST API returning a JSON list instead of a dict."""
    data, _ = _github_api_request_list_with_headers(path, token, method, payload)
    return data


def _github_api_request_list_with_headers(
    path: str,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Like :func:`_github_api_request_list` but also returns response headers.

    Returns:
        Tuple of (data list, header dict).
    """
    import urllib.error
    import urllib.request

    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "sunaba")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
            headers = dict(response.headers)
            return body, headers
    except urllib.error.HTTPError as e:
        parts: list[str] = []
        raw_body = ""
        try:
            raw_body = e.read().decode("utf-8", errors="replace")
            body = json.loads(raw_body)
            if isinstance(body, dict):
                if body.get("message"):
                    parts.append(str(body["message"]))
                parts.extend(str(err.get("message", err)) for err in body.get("errors") or [])
            else:
                parts.append(f"raw body: {raw_body[:500]}")
        except Exception:
            if raw_body:
                parts.append(f"raw body: {raw_body[:500]}")
        detail = f": {'; '.join(parts)}" if parts else ""
        raise RuntimeError(
            f"GitHub API {method} {path} returned HTTP {e.code}{detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GitHub API {method} {path} failed: {e.reason}") from e


# ---------------------------------------------------------------------------
# Link-header parsing (pure function)
# ---------------------------------------------------------------------------


def _link_next_url(link_header: str | None) -> str | None:
    """Extract the ``rel="next"`` URL from a ``Link`` header, if present."""
    if not link_header:
        return None
    m = re.search(r'<([^>]+)>\s*;\s*rel="next"', link_header)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Pagination — pure loop (injectable for mock-free testing)
# ---------------------------------------------------------------------------


def _fetch_all_pages(
    fetch_page: Callable[[str], tuple[list[dict[str, Any]], str | None]],
    initial_path: str,
) -> list[dict[str, Any]]:
    """Fetch all pages of a paginated API using *fetch_page*.

    *fetch_page* is called with the next URL or path; it must return a
    ``(data_list, link_header_value)`` tuple.  The loop follows ``Link``
    headers via :func:`_link_next_url` until no ``rel="next"`` is found.

    This is a pure function: no HTTP calls, no Docker, no mocks needed
    when tested with a synthetic *fetch_page*.
    """
    all_data: list[dict[str, Any]] = []
    next_path: str | None = initial_path

    while next_path:
        page, link = fetch_page(next_path)
        all_data.extend(page)
        next_path = _link_next_url(link)

    return all_data


# ---------------------------------------------------------------------------
# Pagination — real wrapper (calls the GitHub REST API)
# ---------------------------------------------------------------------------


def _github_api_request_list_all(
    path: str,
    token: str,
) -> list[dict[str, Any]]:
    """Fetch all pages of a paginated GitHub API list endpoint.

    Follows ``Link`` headers until no ``rel="next"`` page is found.
    The response order from the API is preserved (oldest-first for comments).
    """
    import urllib.error
    import urllib.request

    def _fetch_one_page(next_path: str) -> tuple[list[dict[str, Any]], str | None]:
        url = f"https://api.github.com{next_path}" if next_path.startswith("/") else next_path
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("User-Agent", "sunaba")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                page = json.loads(response.read().decode("utf-8"))
                link = response.headers.get("Link")
                return page, link
        except urllib.error.HTTPError as e:
            parts: list[str] = []
            raw_body = ""
            try:
                raw_body = e.read().decode("utf-8", errors="replace")
                body = json.loads(raw_body)
                if isinstance(body, dict):
                    if body.get("message"):
                        parts.append(str(body["message"]))
                    parts.extend(str(err.get("message", err)) for err in body.get("errors") or [])
                else:
                    parts.append(f"raw body: {raw_body[:500]}")
            except Exception:
                if raw_body:
                    parts.append(f"raw body: {raw_body[:500]}")
            detail = f": {'; '.join(parts)}" if parts else ""
            raise RuntimeError(
                f"GitHub API GET {path} returned HTTP {e.code}{detail}"
            ) from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub API GET {path} failed: {e.reason}") from e

    return _fetch_all_pages(_fetch_one_page, path)


# ---------------------------------------------------------------------------
# PR creation
# ---------------------------------------------------------------------------


def _create_pr_via_api(
    repo: str,
    branch: str,
    pr_title: str,
    pr_body: str,
    base_branch: str,
    token: str,
) -> str:
    """Create a pull request host-side via the REST API; return its URL.

    Replaces the in-container ``gh pr create`` exec (#360): PR creation is a
    non-push write on ``api.github.com``, so running it here keeps the
    container credential-free — that exec was the last one carrying an
    ephemeral token — and stays out of the proxy's write gate.

    Also makes *base_branch* actually work: the old shell wrapper appended
    ``--base`` after the temp-file cleanup command, so gh never saw it and a
    stacked PR silently targeted the default branch.

    Raises:
        RuntimeError: When the base branch cannot be determined or the API
            call fails (propagated from :func:`_github_api_request`).
    """
    base = base_branch or str(
        _github_api_request(f"/repos/{repo}", token).get("default_branch") or ""
    )
    if not base:
        raise RuntimeError(f"could not determine the default branch of {repo}")
    payload: dict[str, Any] = {"title": pr_title, "head": branch, "base": base}
    if pr_body:
        payload["body"] = pr_body
    try:
        created = _github_api_request(
            f"/repos/{repo}/pulls", token, method="POST", payload=payload
        )
        url = str(created.get("html_url") or "")
        if not url:
            raise RuntimeError("GitHub API created the PR but returned no html_url")
        return url
    except RuntimeError as exc:
        # NOTE: "HTTP 422" / "already exists" matching depends on the
        # exact error format in _github_api_request. If that format
        # changes, this idempotent fallback will silently stop working.
        err_msg = str(exc)
        if "HTTP 422" in err_msg and "already exists" in err_msg.lower():
            try:
                owner = repo.split("/")[0]
                pulls = _github_api_request_list(
                    f"/repos/{repo}/pulls?head={owner}:{branch}&state=open", token
                )
                if pulls:
                    url = str(pulls[0].get("html_url") or "")
                    if url:
                        return url
            except Exception:
                logger.warning(
                    "PR already exists but recovery GET failed",
                    exc_info=True,
                )
        raise exc
