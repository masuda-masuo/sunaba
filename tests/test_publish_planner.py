"""Pure-function tests for publish_planner — no docker, no network, no mocks.

Every function in publish_planner is deterministic: its output depends solely
on its inputs.  No MagicMock for containers, no docker-client stubs.
"""

from __future__ import annotations

import json
from typing import Any

from sunaba.tools.publish_planner import (
    build_push_command,
    finish_json,
    is_egress_block,
    pr_body_validation_error,
    push_failure_hints,
    select_push_env,
    verify_gate_error,
)


class TestVerifyGateError:
    """verify_gate_error returns dict when gate blocks, None otherwise."""

    def test_blocks_when_not_verified_and_no_skip(self) -> None:
        result = verify_gate_error(verified=False, skip_verify_gate=False)
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "verify_gate"
        assert "no successful verify_in_container" in result["error"]
        assert "skip_verify_gate=True" in result["error"]
        assert result["recommended_next_action"] == "verify_in_container"

    def test_passes_when_verified(self) -> None:
        assert verify_gate_error(verified=True, skip_verify_gate=False) is None

    def test_passes_when_skip_verify_gate(self) -> None:
        assert verify_gate_error(verified=False, skip_verify_gate=True) is None

    def test_passes_when_both_true(self) -> None:
        assert verify_gate_error(verified=True, skip_verify_gate=True) is None


class TestPrBodyValidationError:
    """pr_body_validation_error returns dict when invalid, None otherwise."""

    def test_rejects_empty_body_with_create_pr(self) -> None:
        result = pr_body_validation_error(create_pr=True, pr_body="")
        assert result is not None
        assert result["status"] == "error"
        assert result["step"] == "validation"
        assert "pr_body is required" in result["error"]

    def test_rejects_whitespace_only_body(self) -> None:
        result = pr_body_validation_error(create_pr=True, pr_body="   ")
        assert result is not None
        assert result["step"] == "validation"

    def test_accepts_body_with_create_pr(self) -> None:
        assert pr_body_validation_error(create_pr=True, pr_body="PR body") is None

    def test_accepts_empty_body_without_create_pr(self) -> None:
        assert pr_body_validation_error(create_pr=False, pr_body="") is None


class TestBuildPushCommand:
    """build_push_command constructs the git push command string."""

    def test_without_force(self) -> None:
        cmd = build_push_command(branch="fix/x", allow_force_push=False)
        assert cmd.startswith("git -c credential.helper=")
        assert "push origin" in cmd
        assert "fix/x" in cmd
        assert "--force" not in cmd
        assert "$GITHUB_TOKEN" in cmd

    def test_with_force(self) -> None:
        cmd = build_push_command(branch="fix/x", allow_force_push=True)
        assert "--force" in cmd
        assert "fix/x" in cmd

    def test_branch_with_special_chars_is_quoted(self) -> None:
        cmd = build_push_command(branch="feat/issue-123", allow_force_push=False)
        assert "feat/issue-123" in cmd


class TestSelectPushEnv:
    """select_push_env returns None when proxied, env dict otherwise."""

    def test_returns_none_when_proxied_with_token(self) -> None:
        env = {"GITHUB_TOKEN": "tok", "GH_TOKEN": "tok"}
        assert select_push_env(token_env=env, proxied=True) is None

    def test_returns_env_when_not_proxied(self) -> None:
        env = {"GITHUB_TOKEN": "tok", "GH_TOKEN": "tok"}
        assert select_push_env(token_env=env, proxied=False) == env

    def test_returns_none_when_no_token_and_not_proxied(self) -> None:
        assert select_push_env(token_env=None, proxied=False) is None

    def test_returns_none_when_no_token_and_proxied(self) -> None:
        assert select_push_env(token_env=None, proxied=True) is None


class TestIsEgressBlock:
    """is_egress_block detects the egress-proxy block string."""

    def test_detects_block_phrase(self) -> None:
        assert is_egress_block("blocked by egress proxy") is True

    def test_detects_block_in_full_error(self) -> None:
        text = (
            "remote: BLOCKED by egress proxy: "
            "push to owner/repo is not in the allowlist."
        ).lower()
        assert is_egress_block(text) is True

    def test_returns_false_for_other_errors(self) -> None:
        assert is_egress_block("remote rejected: permission denied") is False

    def test_returns_false_for_empty_string(self) -> None:
        assert is_egress_block("") is False


class TestPushFailureHints:
    """push_failure_hints returns deterministic hint lists."""

    def test_both_network_off_and_token_missing(self) -> None:
        hints = push_failure_hints(network_off=True, token_missing=True)
        assert len(hints) == 2
        assert "allow_network=False" in hints[0]
        assert "No VCS token" in hints[1]

    def test_only_network_off(self) -> None:
        hints = push_failure_hints(network_off=True, token_missing=False)
        assert len(hints) == 1
        assert "allow_network=False" in hints[0]

    def test_only_token_missing(self) -> None:
        hints = push_failure_hints(network_off=False, token_missing=True)
        assert len(hints) == 1
        assert "No VCS token" in hints[0]

    def test_no_hints(self) -> None:
        assert push_failure_hints(network_off=False, token_missing=False) == []


class TestFinishJson:
    """finish_json adds warning when unverified and serializes."""

    def test_adds_warning_when_not_verified(self) -> None:
        payload: dict[str, Any] = {"status": "pushed", "sha": "abc1234"}
        raw = finish_json(payload, verified=False)
        parsed = json.loads(raw)
        assert parsed["status"] == "pushed"
        assert parsed["sha"] == "abc1234"
        assert "warning" in parsed
        assert "no successful verify_in_container" in parsed["warning"]
        assert parsed["recommended_next_action"] == "verify_in_container"

    def test_no_warning_when_verified(self) -> None:
        payload: dict[str, Any] = {"status": "pushed", "sha": "abc1234"}
        raw = finish_json(payload, verified=True)
        parsed = json.loads(raw)
        assert parsed["status"] == "pushed"
        assert parsed["sha"] == "abc1234"
        assert "warning" not in parsed
        assert "recommended_next_action" not in parsed

    def test_preserves_existing_payload_keys(self) -> None:
        payload: dict[str, Any] = {"status": "error", "step": "git_push"}
        raw = finish_json(payload, verified=True)
        parsed = json.loads(raw)
        assert parsed["status"] == "error"
        assert parsed["step"] == "git_push"
