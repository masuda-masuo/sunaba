"""Acceptance tests for sunaba #930 stage 1: uv environment contract.

Pins behavioral requirements for uv detection, locked sync, process-local
preparation state, verification short-circuit, and publish invalidation.
These tests act as the frozen acceptance oracle before implementation.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.clone import _install_repo_deps
from sunaba.tools.vcs.publishing import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import has_verify_success, record_verify_success
from tests.conftest import _make_client_mock


@pytest.fixture(autouse=True)
def _isolate_verify_state():
    """Isolate the module-level verify map between tests."""
    with patch("sunaba.verify_state._verify_map", {}):
        yield


@pytest.fixture(autouse=True)
def _disable_egress_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep publish off the egress-proxy path."""
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")


def _make_mock_container(
    cid: str,
    root_files: set[str],
    *,
    uv_sync_ec: int = 0,
    uv_sync_output: bytes = b"Resolved 5 packages in 10ms\n",
    pip_ec: int = 0,
) -> tuple[MagicMock, list[str]]:
    """Build a mock container that simulates filesystem checks and commands."""
    container = MagicMock()
    container.id = cid
    executed_cmds: list[str] = []

    def _exec(cmd, **kwargs):
        cmd_str = (
            cmd
            if isinstance(cmd, str)
            else (cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd))
        )
        executed_cmds.append(cmd_str)

        # Manifest probe loop
        if "for f in" in cmd_str and any(
            m in cmd_str for m in ("pyproject.toml", "uv.lock", "package.json")
        ):
            probed = [
                f
                for f in [
                    "pyproject.toml",
                    "uv.lock",
                    "package.json",
                    "package-lock.json",
                    "setup.py",
                    "go.mod",
                    "Cargo.toml",
                ]
                if f in root_files and f in cmd_str
            ]
            out = ("\n".join(probed) + "\n").encode() if probed else b""
            return 0, (out, b"")

        # find command for project markers
        if "find " in cmd_str:
            lines: list[bytes] = []
            if "pyproject.toml" in root_files and "pyproject.toml" in cmd_str:
                lines.append(b"./pyproject.toml")
            if "uv.lock" in root_files and "uv.lock" in cmd_str:
                lines.append(b"./uv.lock")
            if "package.json" in root_files and "package.json" in cmd_str:
                lines.append(b"./package.json")
            out = b"\n".join(lines) + b"\n" if lines else b""
            return 0, (out, b"")

        # Direct existence checks (e.g. [ -e uv.lock ], test -f uv.lock)
        for filename in (
            "uv.lock",
            "pyproject.toml",
            "package.json",
            "package-lock.json",
        ):
            if filename in cmd_str and any(
                marker in cmd_str for marker in ("test -", "[ -", "[ ! -")
            ):
                ec = 0 if filename in root_files else 1
                return ec, (b"", b"")

        if "uv sync" in cmd_str:
            if uv_sync_ec != 0:
                return uv_sync_ec, (b"", b"error: locked sync failed\n")
            return 0, (uv_sync_output, b"")

        if "pip install" in cmd_str:
            if pip_ec != 0:
                return pip_ec, (b"", b"error: pip install failed\n")
            return 0, (b"Successfully installed\n", b"")

        if "npm" in cmd_str:
            return 0, (b"added 3 packages\n", b"")

        if "for d in src tests" in cmd_str:
            return 0, (b"src\ntests\n", b"")

        # git commands
        if "git diff" in cmd_str:
            return 0, (b"", b"")
        if "git ls-files" in cmd_str:
            return 0, (b"", b"")
        if "git rev-parse" in cmd_str:
            return 0, (b"main\n", b"")
        if any(
            g in cmd_str
            for g in ("git checkout", "git add", "git commit", "git push")
        ):
            return 0, (b"", b"")

        return 0, (b"", b"")

    container.exec_run.side_effect = _exec
    return container, executed_cmds


class TestUvEnvironmentStage1:
    """Acceptance test suite for sunaba #930 stage 1."""

    def test_spec1_2_uv_root_runs_locked_sync_without_pip(self) -> None:
        """Spec 1 & 2: Root with pyproject.toml + uv.lock uses uv sync --locked --all-groups.

        --frozen is superseded, pip fallback is forbidden, and pip_extras default
        [dev] is a compatibility default rather than an extra request to uv.
        """
        container, executed = _make_mock_container(
            "uv_stage1_sync", {"pyproject.toml", "uv.lock"}
        )

        deps = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )

        assert deps.failed is False
        assert any(
            "uv sync --locked --all-groups" in cmd for cmd in executed
        ), f"Expected 'uv sync --locked --all-groups' in commands, got: {executed}"
        assert not any(
            "--frozen" in cmd for cmd in executed
        ), f"--frozen is superseded but was found in commands: {executed}"
        assert not any(
            "pip install" in cmd for cmd in executed
        ), f"pip install must not run for uv project, got: {executed}"
        assert (
            "uv" in deps.note and "pip install" not in deps.note
        ), f"Expected uv note without pip install, got: {deps.note}"

    def test_spec2_uv_sync_failure_fails_preparation_without_pip_fallback(
        self,
    ) -> None:
        """Spec 2: uv sync failure fails preparation and never falls back to pip."""
        container, executed = _make_mock_container(
            "uv_stage1_fail", {"pyproject.toml", "uv.lock"}, uv_sync_ec=1
        )

        deps = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )

        assert (
            deps.failed is True
        ), f"Expected deps.failed=True on uv sync failure, got: {deps}"
        assert any(
            "uv sync" in cmd for cmd in executed
        ), f"Expected uv sync command attempt, got: {executed}"
        assert not any(
            "pip install" in cmd for cmd in executed
        ), f"Must never fall back to pip install on uv sync failure, got: {executed}"

    def test_spec2_uv_rejects_nondefault_pip_extras_and_pip_args(self) -> None:
        """Spec 2: Nondefault explicit pip extras or nonempty pip_args are rejected for uv."""
        container1, executed1 = _make_mock_container(
            "uv_stage1_extra", {"pyproject.toml", "uv.lock"}
        )
        deps1 = _install_repo_deps(
            container1, "owner/repo", "/workspace", pip_extras="[test]"
        )
        assert (
            deps1.failed is True
        ), f"Explicit nondefault pip_extras must fail for uv, got: {deps1}"
        assert not any("pip install" in cmd for cmd in executed1)

        container2, executed2 = _make_mock_container(
            "uv_stage1_args", {"pyproject.toml", "uv.lock"}
        )
        deps2 = _install_repo_deps(
            container2,
            "owner/repo",
            "/workspace",
            "[dev]",
            pip_args="--no-deps",
        )
        assert (
            deps2.failed is True
        ), f"Nonempty pip_args must fail for uv, got: {deps2}"
        assert not any("pip install" in cmd for cmd in executed2)

    def test_spec2_3_uv_pip_extras_none_skips_python_and_leaves_not_prepared(
        self,
    ) -> None:
        """Spec 2 & 3: pip_extras=None skips Python prep, leaves uv not_prepared, while JS install runs."""
        cid = "uv_stage1_none"
        container, executed = _make_mock_container(
            cid, {"pyproject.toml", "uv.lock", "package.json"}
        )

        deps = _install_repo_deps(container, "owner/repo", "/workspace", None)
        assert deps.failed is False
        assert any(
            "npm" in cmd for cmd in executed
        ), f"JS install must run independently, got: {executed}"
        assert not any(
            "uv sync" in cmd for cmd in executed
        ), f"uv sync must be skipped when pip_extras=None, got: {executed}"

        # Verify must fail because uv environment was left not_prepared
        mock_client = _make_client_mock(container)
        with patch("sunaba.tools.verify._docker", return_value=mock_client):
            res_raw = verify_in_container(
                container_id=cid,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            )
            result = json.loads(res_raw)

        assert result.get("status") == "environment_setup_failed"
        assert result.get("gate_passed") is False
        assert "environment_setup" in result.get("gate_fail_kinds", [])

    def test_spec5_uv_network_disabled_fails_preparation(self) -> None:
        """Spec 5: allow_network=False for uv root fails with reason network_disabled."""
        container, _ = _make_mock_container(
            "uv_stage1_nonet", {"pyproject.toml", "uv.lock"}
        )

        deps = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]", allow_network=False
        )

        assert (
            deps.failed is True
        ), f"allow_network=False must fail preparation for uv root, got: {deps}"
        assert (
            "network_disabled" in deps.note
        ), f"Expected 'network_disabled' in note, got: {deps.note}"

    def test_spec3_uv_failed_preparation_short_circuits_verify_with_no_runners(
        self,
    ) -> None:
        """Spec 3: After failed preparation, verify returns environment_setup_failed without running runners."""
        cid = "uv_stage1_shrt"
        container, executed = _make_mock_container(
            cid, {"pyproject.toml", "uv.lock"}, uv_sync_ec=1, pip_ec=1
        )

        # Trigger failed preparation
        deps = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )
        assert deps.failed is True

        # Now invoke verify; even with all skip flags, it must short-circuit
        exec_count_before = len(executed)
        mock_client = _make_client_mock(container)
        with patch("sunaba.tools.verify._docker", return_value=mock_client):
            res_raw = verify_in_container(
                container_id=cid,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            )
            result = json.loads(res_raw)

        assert result.get("status") == "environment_setup_failed"
        assert result.get("gate_passed") is False
        assert "environment_setup" in result.get("gate_fail_kinds", [])

        # Lint, type, and tests must NOT be executed
        verify_cmds = executed[exec_count_before:]
        assert not any("ruff" in cmd for cmd in verify_cmds)
        assert not any("pyright" in cmd for cmd in verify_cmds)
        assert not any("pytest" in cmd for cmd in verify_cmds)

    def test_spec3_uv_missing_state_disallows_verification(self) -> None:
        """Spec 3: Detected uv repo with missing state must not allow verification."""
        cid = "uv_stage1_nostate"
        container, executed = _make_mock_container(
            cid, {"pyproject.toml", "uv.lock"}
        )

        # No preparation was run for this container (state is missing/not_prepared)
        mock_client = _make_client_mock(container)
        with patch("sunaba.tools.verify._docker", return_value=mock_client):
            res_raw = verify_in_container(container_id=cid, path="tests/")
            result = json.loads(res_raw)

        assert result.get("status") == "environment_setup_failed"
        assert result.get("gate_passed") is False
        assert "environment_setup" in result.get("gate_fail_kinds", [])
        assert not any("pytest" in cmd for cmd in executed)

    def test_spec4_uv_prep_failure_invalidates_verify_success_and_blocks_publish(
        self,
    ) -> None:
        """Spec 4 (CRITICAL regression): Success then uv failure in same container invalidates publish gate."""
        cid = "uv_stage1_regress"
        container, executed = _make_mock_container(
            cid, {"pyproject.toml", "uv.lock"}, uv_sync_ec=1, pip_ec=1
        )

        # 1. Container initially records a verify success
        record_verify_success(cid)
        assert has_verify_success(cid) is True

        # 2. Trigger uv dependency preparation failure in the SAME container
        deps = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )
        assert deps.failed is True

        # 3. Previous verify success MUST be invalidated
        assert (
            has_verify_success(cid) is False
        ), "CRITICAL: Previous verify success was not invalidated on prep failure"

        # 4. publish normal gate must refuse before any commit/push
        mock_client = _make_client_mock(container)
        with (
            patch(
                "sunaba.tools.vcs.publishing._docker", return_value=mock_client
            ),
            patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        ):
            pub_raw = publish(
                container_id=cid,
                repo="owner/repo",
                branch="fix/test",
                message="Test commit",
                skip_verify_gate=False,
            )
            pub_result = json.loads(pub_raw)

        assert pub_result.get("status") == "error"
        assert pub_result.get("step") == "verify_gate"
        assert "no successful verify_in_container" in pub_result.get(
            "error", ""
        )
        assert not any("git push" in cmd for cmd in executed)

    def test_spec6_uv_prep_success_does_not_create_verify_success(self) -> None:
        """Spec 6: Preparation success alone never creates verify success."""
        cid = "uv_stage1_prepok"
        container, executed = _make_mock_container(
            cid, {"pyproject.toml", "uv.lock"}
        )

        deps = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )
        assert any(
            "uv sync --locked --all-groups" in cmd for cmd in executed
        ), f"Expected uv sync in commands, got: {executed}"
        assert deps.failed is False

        # Preparation success alone must NEVER create verify success
        assert has_verify_success(cid) is False

        mock_client = _make_client_mock(container)
        with (
            patch(
                "sunaba.tools.vcs.publishing._docker", return_value=mock_client
            ),
            patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        ):
            pub_raw = publish(
                container_id=cid,
                repo="owner/repo",
                branch="fix/test",
                message="Test commit",
                skip_verify_gate=False,
            )
            pub_result = json.loads(pub_raw)

        assert pub_result.get("status") == "error"
        assert pub_result.get("step") == "verify_gate"
