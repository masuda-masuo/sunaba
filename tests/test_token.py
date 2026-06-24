"""Tests for the token-based approval mechanism (Issue #50)."""
from __future__ import annotations

import time

from code_sandbox_mcp.token import (
    generate_token,
    get_pending_tokens,
    reject_token,
    verify_and_consume,
    verify_token,
)


class TestTokenGenerate:
    """Tests for token generation."""

    def test_generate_returns_string(self) -> None:
        token = generate_token("git_push", "push to main", "abc123", "run1")
        assert isinstance(token, str)
        assert len(token) == 32  # hex 16 bytes

    def test_generate_unique_tokens(self) -> None:
        tokens = {
            generate_token("git_push", "push", "abc123", "run1")
            for _ in range(50)
        }
        assert len(tokens) == 50

    def test_generated_token_appears_in_pending(self) -> None:
        token = generate_token("pr_create", "create PR", "abc123", "run1")
        pending = get_pending_tokens()
        found = [p for p in pending if p["token"] == token]
        assert len(found) == 1
        assert found[0]["operation"] == "pr_create"
        assert found[0]["container_id"] == "abc123"
        assert found[0]["run_id"] == "run1"


class TestTokenVerify:
    """Tests for token verification and consumption."""

    def test_verify_valid_token(self) -> None:
        token = generate_token("git_push", "push to main", "abc123", "run1")
        result = verify_and_consume(token)
        assert result is not None
        assert result["operation"] == "git_push"
        assert result["details"] == "push to main"

    def test_verify_consumes_token(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        assert verify_and_consume(token) is not None
        assert verify_and_consume(token) is None

    def test_verify_invalid_token(self) -> None:
        assert verify_and_consume("nonexistent") is None

    def test_verify_empty_token(self) -> None:
        assert verify_and_consume("") is None

    def test_consumed_token_removed_from_pending(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        verify_and_consume(token)
        pending = get_pending_tokens()
        found = [p for p in pending if p["token"] == token]
        assert len(found) == 0


class TestTokenVerifyPeek:
    """Tests for verify_token() (non-consuming peek)."""

    def test_verify_peek_valid_token(self) -> None:
        token = generate_token("git_push", "push to main", "abc123", "run1")
        result = verify_token(token)
        assert result is not None
        assert result["operation"] == "git_push"

    def test_verify_peek_does_not_consume(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        assert verify_token(token) is not None
        # verify_and_consume should still work (token not consumed)
        assert verify_and_consume(token) is not None
        # Second consume should fail
        assert verify_and_consume(token) is None

    def test_verify_peek_invalid_token(self) -> None:
        assert verify_token("nonexistent") is None

    def test_verify_peek_empty_token(self) -> None:
        assert verify_token("") is None

    def test_verify_peek_token_remains_in_pending(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        verify_token(token)
        pending = get_pending_tokens()
        found = [p for p in pending if p["token"] == token]
        assert len(found) == 1  # not removed by peek


class TestTokenReject:
    """Tests for token rejection."""

    def test_reject_valid_token(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        assert reject_token(token) is True

    def test_reject_removes_from_pending(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        reject_token(token)
        pending = get_pending_tokens()
        found = [p for p in pending if p["token"] == token]
        assert len(found) == 0

    def test_reject_nonexistent_token(self) -> None:
        assert reject_token("nonexistent") is False

    def test_reject_then_verify_fails(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1")
        reject_token(token)
        assert verify_and_consume(token) is None


class TestTokenExpiry:
    """Tests for token expiry."""

    def test_expired_token_verify_returns_none(self) -> None:
        token = generate_token("git_push", "push", "abc123", "run1", ttl_seconds=0)
        assert verify_and_consume(token) is None

    def test_expired_token_excluded_from_pending(self) -> None:
        """TTL切れのトークンが get_pending_tokens から除外されることの確認。

        上の test_expired_token_verify_returns_none とは検証対象が
        異なる (verify_and_consume vs get_pending_tokens)。
        """
        token = generate_token("git_push", "push", "abc123", "run1", ttl_seconds=0)
        pending = get_pending_tokens()
        found = [p for p in pending if p["token"] == token]
        assert len(found) == 0

    def test_pending_sorted_by_creation_time(self) -> None:
        token1 = generate_token("op1", "d1", "abc123", "run1")
        time.sleep(0.01)
        token2 = generate_token("op2", "d2", "abc123", "run1")
        pending = get_pending_tokens()
        tokens_in_order = [p["token"] for p in pending if p["token"] in (token1, token2)]
        assert tokens_in_order == [token1, token2]
