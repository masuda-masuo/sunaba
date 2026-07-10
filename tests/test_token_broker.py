"""Tests for the keystore-broker token provider (Issue #232)."""
from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from sunaba import token_broker


class TestMintToken:
    """mint_token() resolves a command and returns its stdout, else None."""

    def test_no_broker_configured_returns_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            assert token_broker.mint_token() is None

    def test_command_success_returns_trimmed_token(self) -> None:
        completed = subprocess.CompletedProcess(["x"], 0, stdout="ghs_minted\n", stderr="")
        with patch.dict(os.environ, {"GITHUB_TOKEN_COMMAND": "x csb"}, clear=True):
            with patch("sunaba.token_broker.subprocess.run", return_value=completed):
                assert token_broker.mint_token() == "ghs_minted"

    def test_command_nonzero_returns_none(self) -> None:
        completed = subprocess.CompletedProcess(["x"], 1, stdout="", stderr="boom")
        with patch.dict(os.environ, {"GITHUB_TOKEN_COMMAND": "x csb"}, clear=True):
            with patch("sunaba.token_broker.subprocess.run", return_value=completed):
                assert token_broker.mint_token() is None

    def test_command_empty_output_returns_none(self) -> None:
        completed = subprocess.CompletedProcess(["x"], 0, stdout="   \n", stderr="")
        with patch.dict(os.environ, {"GITHUB_TOKEN_COMMAND": "x csb"}, clear=True):
            with patch("sunaba.token_broker.subprocess.run", return_value=completed):
                assert token_broker.mint_token() is None

    def test_command_timeout_returns_none(self) -> None:
        with patch.dict(os.environ, {"GITHUB_TOKEN_COMMAND": "x csb"}, clear=True):
            with patch(
                "sunaba.token_broker.subprocess.run",
                side_effect=subprocess.TimeoutExpired("x", 30),
            ):
                assert token_broker.mint_token() is None

    def test_broker_service_uses_resolved_binary(self) -> None:
        completed = subprocess.CompletedProcess(["mcp-token", "csb"], 0, stdout="ghs_svc\n", stderr="")
        with patch.dict(os.environ, {"GITHUB_TOKEN_BROKER_SERVICE": "csb"}, clear=True):
            with patch(
                "sunaba.token_broker.resolve_broker_binary",
                return_value=token_broker.Path("/opt/mcp-token"),
            ):
                with patch(
                    "sunaba.token_broker.subprocess.run", return_value=completed
                ) as run:
                    assert token_broker.mint_token() == "ghs_svc"
                    assert run.call_args.args[0] == ["/opt/mcp-token", "csb"]


class TestVerifyAndResolve:
    """SHA-256 verification gates both download and cache reuse."""

    def test_check_sha256_mismatch_raises(self) -> None:
        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            token_broker._check_sha256(b"corrupt", "0" * 64)

    def test_check_sha256_match_ok(self) -> None:
        import hashlib

        data = b"hello"
        token_broker._check_sha256(data, hashlib.sha256(data).hexdigest())

    def test_unsupported_platform_returns_none(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("sunaba.token_broker._platform_key", return_value=None):
                assert token_broker.resolve_broker_binary() is None

    def test_override_bin_used_when_present(self, tmp_path) -> None:
        binpath = tmp_path / "mcp-token"
        binpath.write_text("#!/bin/sh\n")
        with patch.dict(os.environ, {"GITHUB_TOKEN_BROKER_BIN": str(binpath)}, clear=True):
            assert token_broker.resolve_broker_binary() == binpath

    def test_corrupt_cache_without_download_refused(self, tmp_path) -> None:
        key = ("linux", "amd64")
        env = {
            "SUNABA_TOKEN_BROKER_CACHE_DIR": str(tmp_path),
            "SUNABA_TOKEN_BROKER_NO_DOWNLOAD": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch("sunaba.token_broker._platform_key", return_value=key):
                dest = token_broker._dest_path(key)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"corrupted-binary")  # wrong sha256
                assert token_broker.resolve_broker_binary() is None

    def test_download_failure_returns_none(self, tmp_path) -> None:
        key = ("linux", "amd64")
        with patch.dict(os.environ, {"SUNABA_TOKEN_BROKER_CACHE_DIR": str(tmp_path)}, clear=True):
            with patch("sunaba.token_broker._platform_key", return_value=key):
                with patch(
                    "sunaba.token_broker._download_and_verify",
                    side_effect=RuntimeError("sha256 mismatch"),
                ):
                    assert token_broker.resolve_broker_binary() is None
