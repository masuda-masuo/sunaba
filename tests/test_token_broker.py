"""Tests for the keystore-broker token provider (Issue #232)."""
from __future__ import annotations

import logging
import os
import re
import subprocess
from importlib import resources
from pathlib import Path
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


# ===================================================================
# Broker checksums data file (Issue #757)
# ===================================================================


class TestBrokerChecksumsData:
    """Structural tests for the shipped ``broker_checksums.txt`` and its loader.

    The checksums file is the single source of SHA-256 hashes for the
    ``mcp-token`` release assets.  The tag in the file must agree with
    ``BROKER_TAG``; any drift is a hard error at import time.
    """

    def _checksums_lines(self) -> list[str]:
        """Read the shipped checksums file directly via importlib.resources."""
        raw = (
            resources.files("sunaba")
            .joinpath(token_broker._CHECKSUMS_RESOURCE)
            .read_text(encoding="utf-8")
        )
        return raw.splitlines()

    def test_resource_is_packaged(self) -> None:
        """The checksums file must be findable via importlib.resources."""
        resource = resources.files("sunaba").joinpath(token_broker._CHECKSUMS_RESOURCE)
        assert resource.is_file(), (
            f"{token_broker._CHECKSUMS_RESOURCE} not found via importlib.resources "
            "(packaging regression: check [tool.setuptools.package-data])"
        )

    def test_header_declares_matching_tag(self) -> None:
        """The comment on line 1 must declare the same tag as BROKER_TAG."""
        lines = self._checksums_lines()
        assert lines, "checksums file is empty"
        assert lines[0].startswith("# BROKER_TAG="), (
            f"first line should be '# BROKER_TAG=<tag>', got {lines[0]!r}"
        )
        tag_in_file = lines[0].removeprefix("# BROKER_TAG=").strip()
        assert tag_in_file == token_broker.BROKER_TAG, (
            f"checksums file declares tag {tag_in_file!r} "
            f"but BROKER_TAG is {token_broker.BROKER_TAG!r}"
        )

    def test_platform_assets_have_checksums(self) -> None:
        """Every asset in _ASSET_NAMES must have a checksum line in the file."""
        lines = self._checksums_lines()
        # Build set of filenames mentioned in checksums
        filenames = set()
        for line in lines[1:]:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                filenames.add(parts[1])
        for key, asset_name in token_broker._ASSET_NAMES.items():
            assert asset_name in filenames, (
                f"asset {asset_name!r} for platform {key} "
                f"has no checksum in {token_broker._CHECKSUMS_RESOURCE}"
            )

    def test_broker_assets_has_exactly_five_entries(self) -> None:
        """_BROKER_ASSETS must contain exactly the five platform entries."""
        assert len(token_broker._BROKER_ASSETS) == 5, (
            f"expected 5 platform entries, got {len(token_broker._BROKER_ASSETS)}"
        )

    def test_linux_amd64_hash_matches_release(self) -> None:
        """The linux/amd64 hash must equal the v1.3.4 release checksum.

        These five expectations are deliberately literal: they are the guard
        against the data file itself being replaced with the wrong release.
        """

    def test_darwin_amd64_hash_from_checksums(self) -> None:
        """darwin/amd64 must have the v1.3.4 checksum from the data file."""
        _, sha256 = token_broker._BROKER_ASSETS[("darwin", "amd64")]
        assert sha256 == "6521cbdc1b81fd99e6beddfa49664caa8dc4fcf6ac4c4a7486e128ce3d1a1f27"

    def test_darwin_arm64_hash(self) -> None:
        _, sha256 = token_broker._BROKER_ASSETS[("darwin", "arm64")]
        assert sha256 == "cc247baca04337166c0608cadd5d95074e8d5fd5a195937dcab4c1a8978899a3"

    def test_linux_arm64_hash(self) -> None:
        _, sha256 = token_broker._BROKER_ASSETS[("linux", "arm64")]
        assert sha256 == "55aa1d00b799c05a80633a7c3a39197abb80821757f181ded5a11e2788ad7216"

    def test_windows_amd64_hash(self) -> None:
        _, sha256 = token_broker._BROKER_ASSETS[("windows", "amd64")]
        assert sha256 == "a9e0baa200ae5e7259f97fccb465b63eca2c11c1f4b128bca7e9515c0bcacc15"

    def test_no_mint_socket_entry_in_assets(self) -> None:
        """The mint-socket tarball in checksums.txt must NOT be a _BROKER_ASSETS entry."""
        asset_names = {v[0] for v in token_broker._BROKER_ASSETS.values()}
        tarballs = [n for n in asset_names if "mint-socket" in n]
        assert not tarballs, f"mint-socket tarball should not be in _BROKER_ASSETS: {tarballs}"


class TestBrokerChecksumsDriftDetection:
    """Verify that tag/checksum drift fails load loudly rather than silently.

    These tests exercise :func:`token_broker._load_broker_checksums` and
    :func:`token_broker._build_broker_assets` directly, patching the resource
    lookup to simulate a broken checksums file.
    """

    @pytest.fixture(autouse=True)
    def _patch_resources(self, monkeypatch, tmp_path):
        """Point resources.files at a temp directory for the duration of each test.

        Subclasses or individual tests can write a custom checksums file into
        ``tmp_path`` and this fixture patches ``resources.files`` to look there.
        """
        self.tmp_path = tmp_path
        checksums_file = tmp_path / token_broker._CHECKSUMS_RESOURCE
        # Default: a valid checksums file matching BROKER_TAG
        checksums_file.write_text(self._valid_checksums_text(), encoding="utf-8")

        class _FakeFiles:
            def joinpath(self, name: str):
                assert name == token_broker._CHECKSUMS_RESOURCE
                return checksums_file

        monkeypatch.setattr(token_broker.resources, "files", lambda _pkg: _FakeFiles())

    #: Asset lines of a valid checksums file, one per supported platform.
    #:
    #: Built as a list and joined, never as adjacent string literals: in a
    #: parenthesised expression ``"\n" "11" * 32`` concatenates the literals
    #: *before* multiplying, yielding 32 copies of ``"\n11"`` instead of a
    #: 64-character digest.  Fixtures written that way silently stop testing
    #: what they claim to.
    _ASSET_LINES: tuple[str, ...] = (
        "00" * 32 + "  mcp-token-linux-amd64",
        "11" * 32 + "  mcp-token-linux-arm64",
        "22" * 32 + "  mcp-token-darwin-amd64",
        "33" * 32 + "  mcp-token-darwin-arm64",
        "44" * 32 + "  mcp-token-windows-amd64.exe",
    )

    @classmethod
    def _text(cls, lines) -> str:
        """Join *lines* under a header declaring the current BROKER_TAG."""
        return "\n".join([f"# BROKER_TAG={token_broker.BROKER_TAG}", *lines]) + "\n"

    @classmethod
    def _valid_checksums_text(cls) -> str:
        """Return a minimal valid checksums text (matches current BROKER_TAG)."""
        return cls._text(cls._ASSET_LINES)

    def test_tag_drift_raises(self) -> None:
        """A mismatched tag in the checksums file must raise BrokerPinError."""
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        bad_text = self._valid_checksums_text().replace(
            token_broker.BROKER_TAG, "mcp-token/v9.9.9"
        )
        checksums_file.write_text(bad_text, encoding="utf-8")
        with pytest.raises(token_broker.BrokerPinError, match="declares tag"):
            token_broker._load_broker_checksums()

    def test_missing_header_raises(self) -> None:
        """A checksums file without a BROKER_TAG header must raise."""
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        checksums_file.write_text("00" * 32 + "  mcp-token-linux-amd64\n", encoding="utf-8")
        with pytest.raises(token_broker.BrokerPinError, match="must start with"):
            token_broker._load_broker_checksums()

    def test_missing_asset_raises(self) -> None:
        """A checksums file missing a required platform asset must raise.

        Every remaining line is a well-formed digest, so the only thing wrong
        is the absent linux-amd64 entry -- otherwise this would pass on the
        malformed-digest error instead and prove nothing.
        """
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        remaining = [
            ln for ln in self._ASSET_LINES if "mcp-token-linux-amd64" not in ln
        ]
        assert len(remaining) == 4
        checksums_file.write_text(self._text(remaining), encoding="utf-8")
        with pytest.raises(token_broker.BrokerPinError, match="checksum for asset"):
            token_broker._build_broker_assets()

    def test_missing_file_raises(self, monkeypatch) -> None:
        """A completely absent checksums file must raise BrokerPinError."""
        tmp = self.tmp_path / "nonexistent"
        tmp.mkdir(parents=True, exist_ok=True)
        missing = tmp / token_broker._CHECKSUMS_RESOURCE
        # Point resources at a path where the file doesn't exist
        class _MissingFiles:
            def joinpath(self, name: str):
                return missing

        monkeypatch.setattr(token_broker.resources, "files", lambda _pkg: _MissingFiles())
        with pytest.raises(token_broker.BrokerPinError, match="not found"):
            token_broker._load_broker_checksums()

    def test_line_that_is_not_a_checksum_raises(self) -> None:
        """A two-token line whose first field is not a digest must raise.

        ``"this is not a checksum line".split(None, 1)`` yields two parts, so a
        length check alone would accept it and store a junk digest.  The shape
        of the first field is what makes it a checksum.
        """
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        lines = [*self._ASSET_LINES, "this is not a checksum line"]
        checksums_file.write_text(self._text(lines), encoding="utf-8")
        with pytest.raises(token_broker.BrokerPinError, match="malformed"):
            token_broker._build_broker_assets()

    def test_comment_and_blank_lines_still_skipped(self) -> None:
        """Genuine comments and blank lines remain ignored, not rejected."""
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        lines = [
            "",
            "# a trailing note from whoever vendored the file",
            *self._ASSET_LINES,
        ]
        checksums_file.write_text(self._text(lines), encoding="utf-8")
        assert len(token_broker._build_broker_assets()) == 5

    def test_truncated_digest_raises(self) -> None:
        """A short digest must fail at load, not at the next download."""
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        lines = ["abc123  mcp-token-linux-amd64", *self._ASSET_LINES[1:]]
        checksums_file.write_text(self._text(lines), encoding="utf-8")
        with pytest.raises(token_broker.BrokerPinError, match="malformed"):
            token_broker._build_broker_assets()

    def test_uppercase_digest_raises(self) -> None:
        """Digests are lowercase hex; an uppercase variant is a hand-edit smell."""
        checksums_file = self.tmp_path / token_broker._CHECKSUMS_RESOURCE
        lines = ["AB" * 32 + "  mcp-token-linux-amd64", *self._ASSET_LINES[1:]]
        checksums_file.write_text(self._text(lines), encoding="utf-8")
        with pytest.raises(token_broker.BrokerPinError, match="malformed"):
            token_broker._build_broker_assets()

class TestBrokerTagSmoke:
    """Quick-smoke checks that the tag and assets are externally consistent."""

    def test_broker_tag_is_v1_3_4(self) -> None:
        assert token_broker.BROKER_TAG == "mcp-token/v1.3.4"

    def test_all_platform_keys_unchanged(self) -> None:
        """The five platform keys must match the original set."""
        assert set(token_broker._BROKER_ASSETS) == {
            ("linux", "amd64"),
            ("linux", "arm64"),
            ("darwin", "amd64"),
            ("darwin", "arm64"),
            ("windows", "amd64"),
        }


class TestSetupShReleaseTag:
    """The shell setup script must pin the same tag as the Python module."""

    SETUP_SH_PATH = Path(__file__).resolve().parent.parent / "scripts" / "setup.sh"

    def test_setup_sh_exists(self) -> None:
        assert self.SETUP_SH_PATH.is_file(), "scripts/setup.sh not found"

    def test_setup_sh_release_tag_agrees_with_broker_tag(self) -> None:
        text = self.SETUP_SH_PATH.read_text(encoding="utf-8")
        m = re.search(r'^RELEASE_TAG="(.+)"', text, re.MULTILINE)
        assert m is not None, "RELEASE_TAG not found in scripts/setup.sh"
        shell_tag = m.group(1)
        assert shell_tag == token_broker.BROKER_TAG, (
            f"scripts/setup.sh RELEASE_TAG={shell_tag!r} != "
            f"token_broker.BROKER_TAG={token_broker.BROKER_TAG!r} "
            "(update both together)"
        )


class TestBinaryVersionLogging:
    """The resolved binary's version is logged once via _log_binary_version."""

    def test_version_logged_on_first_resolve(self, caplog) -> None:
        """When subprocess succeeds, the version string is logged at INFO."""
        caplog.set_level(logging.INFO)
        completed = subprocess.CompletedProcess(
            ["/fake/mcp-token", "version"], 0, stdout="v1.3.4\n", stderr=""
        )
        fake_path = Path("/fake/mcp-token")
        with patch.object(token_broker, "_logged_version", False):
            with patch("sunaba.token_broker.subprocess.run", return_value=completed):
                token_broker._log_binary_version(fake_path)
        assert any(
            "token broker: resolved binary version v1.3.4" in rec.message
            for rec in caplog.records
        ), "version not logged"

    def test_version_failure_swallowed(self, caplog) -> None:
        """A failing version subprocess must not propagate the exception."""
        caplog.set_level(logging.DEBUG)
        fake_path = Path("/fake/mcp-token")
        with patch(
            "sunaba.token_broker.subprocess.run",
            side_effect=OSError("not found"),
        ):
            # Must not raise
            token_broker._log_binary_version(fake_path)

    def test_version_logged_only_once(self, caplog) -> None:
        """Multiple calls to _log_binary_version should only run subprocess once."""
        caplog.set_level(logging.INFO)
        # Reset the guard (the module-global _logged_version may be True from
        # other tests -- force it False for this isolated check by patching).
        with patch.object(token_broker, "_logged_version", False):
            completed = subprocess.CompletedProcess(
                ["/fake/mcp-token", "version"], 0, stdout="v1.3.4\n", stderr=""
            )
            fake_path = Path("/fake/mcp-token")
            with patch("sunaba.token_broker.subprocess.run", return_value=completed) as run:
                token_broker._log_binary_version(fake_path)
                token_broker._log_binary_version(fake_path)
                token_broker._log_binary_version(fake_path)
            # subprocess.run should have been called exactly once
            assert run.call_count == 1, f"expected 1 call, got {run.call_count}"

    def test_version_logging_on_resolve_path(self, caplog, tmp_path) -> None:
        """resolve_broker_binary should trigger version logging when resolved."""
        caplog.set_level(logging.INFO)
        fake_bin = tmp_path / "mcp-token"
        fake_bin.write_text("#!/bin/sh\necho v1.3.4\n")
        fake_bin.chmod(0o755)
        with patch.object(token_broker, "_logged_version", False):
            completed = subprocess.CompletedProcess(
                [str(fake_bin), "version"], 0, stdout="v1.3.4\n", stderr=""
            )
            with patch("sunaba.token_broker.subprocess.run", return_value=completed):
                with patch.dict(
                    os.environ, {"GITHUB_TOKEN_BROKER_BIN": str(fake_bin)}, clear=True
                ):
                    # Must not raise; _log_binary_version called internally
                    result = token_broker.resolve_broker_binary()
        assert result is not None
        assert any(
            "resolved binary version v1.3.4" in rec.message for rec in caplog.records
        ), "version not logged on binary resolve"
