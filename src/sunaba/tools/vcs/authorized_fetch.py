"""Shared host-authorized origin fetch execution for VCS callers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sunaba.proxy_client import ProxyAuthError

FETCH_COMMAND = (
    "git -c credential.helper= "
    "-c credential.helper='!f() { echo username=x-access-token; "
    "echo password=$GITHUB_TOKEN; }; f' fetch origin"
)


FetchFailure = Literal[
    "token_resolution",
    "repo_resolution",
    "proxy_auth",
    "grant_open",
    "grant_close",
    "execution",
    "exit",
]


@dataclass(frozen=True)
class FetchOutcome:
    """Sanitized result of one authorized fetch attempt."""

    failure: FetchFailure | None = None
    error: str = ""
    token: str | None = None
    repo: str | None = None
    proxied: bool = False
    token_missing: bool = False
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    network_off: bool = False

    def redact(self, text: str) -> str:
        """Replace this fetch token in diagnostic text."""
        return text.replace(self.token, "<redacted>") if self.token and text else text


def authorized_origin_fetch(
    *,
    resolve_token: Callable[[], str | None],
    resolve_repo: Callable[[], str],
    push_token_env: Callable[[str], dict[str, str] | None],
    proxy_configured: Callable[[], bool],
    select_push_env: Callable[[dict[str, str] | None, bool], dict[str, str] | None],
    authorized_read_grant: Callable[..., Any],
    execute: Callable[[str, Mapping[str, str] | None], tuple[int, str, str]],
    read_network_off: Callable[[], bool] | None = None,
) -> FetchOutcome:
    """Resolve, authorize and execute a credential-helper origin fetch.

    Repo resolution deliberately follows token resolution and precedes native
    or proxy environment selection. Dependencies are injected per invocation
    to preserve caller monkeypatch seams. The publish-only network label lookup
    remains a narrow callback after environment selection and before the grant.
    """
    try:
        token = resolve_token()
    except Exception:
        return FetchOutcome(failure="token_resolution", error="token provider error")

    try:
        repo = resolve_repo()
    except Exception as exc:
        return FetchOutcome(
            failure="repo_resolution", error=_redact(str(exc), token), token=token
        )

    token_env = push_token_env(token or "")
    proxied = proxy_configured()
    env = select_push_env(token_env, proxied)
    network_off = read_network_off() if read_network_off is not None else False

    entered = False
    result: tuple[int, str, str] | None = None
    execution_error: Exception | None = None
    try:
        with authorized_read_grant(repo, token=token or None):
            entered = True
            try:
                # Keep tuple unpacking inside the execution exception boundary:
                # malformed adapters historically report as fetch execution errors.
                ec, out, err = execute(FETCH_COMMAND, env)
                result = (ec, out, err)
            except Exception as exc:
                execution_error = exc
    except ProxyAuthError as exc:
        return FetchOutcome(
            failure="proxy_auth", error=_redact(str(exc), token), token=token,
            repo=repo, proxied=proxied, token_missing=(not token and not proxied),
            network_off=network_off,
        )
    except Exception as exc:
        return FetchOutcome(
            failure="grant_close" if entered else "grant_open",
            error=_redact(str(exc), token), token=token, repo=repo,
            proxied=proxied, token_missing=(not token and not proxied),
            network_off=network_off,
        )

    if execution_error is not None:
        return FetchOutcome(
            failure="execution", error=_redact(str(execution_error), token),
            token=token, repo=repo, proxied=proxied,
            token_missing=(not token and not proxied), network_off=network_off,
        )

    assert result is not None
    ec, out, err = result
    if ec != 0:
        detail = (err or out).strip() or f"exit code {ec}"
        return FetchOutcome(
            failure="exit", error=_redact(detail, token), token=token,
            repo=repo, proxied=proxied, token_missing=(not token and not proxied),
            exit_code=ec, stdout=out, stderr=err, network_off=network_off,
        )
    return FetchOutcome(
        token=token, repo=repo, proxied=proxied,
        token_missing=(not token and not proxied), exit_code=ec,
        stdout=out, stderr=err, network_off=network_off,
    )


def _redact(text: str, token: str | None) -> str:
    return text.replace(token, "<redacted>") if token and text else text
