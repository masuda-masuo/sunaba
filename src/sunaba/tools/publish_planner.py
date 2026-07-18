"""Pure decision/formatting logic for publish — extracted from tools/vcs.py (issue #650).

All functions are deterministic (no docker, no network, no I/O).  Every
function maps its inputs to a structured output or None; the publish
handler in ``vcs.py`` calls these and performs the actual ``docker exec``
calls.
"""

from __future__ import annotations

import json
import shlex
from typing import Any


def verify_gate_error(verified: bool, skip_verify_gate: bool) -> dict | None:
    """Return the error payload when verify gate blocks, or None.

    Matches the original decision in ``publish`` (lines 995–1017): when no
    successful verify_in_container is recorded and skip_verify_gate is False,
    publishing is blocked before any git operations.
    """
    if not verified and not skip_verify_gate:
        return {
            "status": "error",
            "step": "verify_gate",
            "error": (
                "no successful verify_in_container recorded for this "
                "container in this server session.  Pass "
                "skip_verify_gate=True to bypass (requires human "
                "authorization via MCP client tool-approval prompt)."
            ),
            "recommended_next_action": "verify_in_container",
        }
    return None


def pr_body_validation_error(create_pr: bool, pr_body: str) -> dict | None:
    """Return validation error payload when pr_body is empty with create_pr=True.

    Matches the original check in ``publish`` (lines 1020–1026).
    """
    if create_pr and not pr_body.strip():
        return {
            "status": "error",
            "step": "validation",
            "error": "pr_body is required when create_pr=True",
        }
    return None


def build_push_command(branch: str, allow_force_push: bool) -> str:
    """Build the git push command string with credential helper.

    Matches the original command construction in ``publish`` (lines 1170–1176).
    The returned string uses ``$GITHUB_TOKEN`` from the exec environment.
    """
    force_flag = " --force" if allow_force_push else ""
    return (
        f"git -c credential.helper= "
        f"-c credential.helper='!f() {{ echo username=x-access-token; echo password=$GITHUB_TOKEN; }}; f' "
        f"push origin {shlex.quote(branch)}{force_flag}"
    )


def select_push_env(token_env: dict | None, proxied: bool) -> dict | None:
    """Return the push exec env, or None to keep the container's own env.

    Matches the original routing in ``publish`` (line 1160): when the egress
    proxy is configured the container stays credential-free, so push_env is
    None (the credential goes through the proxy grant instead).
    """
    return None if proxied else token_env


def is_egress_block(push_error_text: str) -> bool:
    """Return True when push_error_text indicates an egress-proxy block.

    Matches the original check in ``publish`` (line 1209, Issue #401):
    when the egress proxy blocks the push, the Objects-API fallback must
    NOT be attempted (that would bypass the proxy).
    """
    return "blocked by egress proxy" in push_error_text


def push_failure_hints(network_off: bool, token_missing: bool) -> list[str]:
    """Return deterministic hint strings for push failures (Issue #577).

    Matches the original hint-building in ``publish`` (lines 1244–1258).
    Returns an empty list when neither condition applies.
    """
    hints: list[str] = []
    if network_off:
        hints.append(
            "Container was started with allow_network=False "
            "(no network access). Push needs network access "
            "to reach GitHub."
        )
    if token_missing:
        hints.append(
            "No VCS token is available on the host. "
            "Set GITHUB_TOKEN or GH_TOKEN in the "
            "server environment."
        )
    return hints


def finish_json(payload: dict[str, Any], verified: bool) -> str:
    """Add a warning when unverified, then json.dumps the payload.

    Matches the original ``_finish`` nested function in ``publish``
    (lines 1051–1063).
    """
    if not verified:
        payload["warning"] = (
            "no successful verify_in_container recorded for this "
            "container in this server session"
        )
        payload["recommended_next_action"] = "verify_in_container"
    return json.dumps(payload)
