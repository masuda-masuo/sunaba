"""Focused regression tests for root uv tool selection and command provenance."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunaba.edit_verify.lint_runners import _run_pyright_verify
from sunaba.edit_verify.test_runners import _run_pytest_verify
from sunaba.environment import EnvironmentResolutionError, resolve_uv_python_tools, resolve_uv_tool
from sunaba.test_report import build_pytest_cmd
from sunaba.tools.container.clone import _install_repo_deps
from sunaba.tools.vcs.publishing import publish
from sunaba.tools.verify import verify_in_container
from sunaba.verify_state import (
    has_verify_success,
    record_environment_state,
)
from tests.conftest import _make_client_mock
from tests.test_uv_environment_stage1 import _make_mock_container


def test_resolve_uv_tool_prefers_project_executable() -> None:
    container = MagicMock()
    container.exec_run.side_effect = [
        (0, (b"", b"")),
        (0, (b"ruff 0.17.0\n", b"")),
    ]

    path, source, version = resolve_uv_tool(container, "/workspace", "ruff")

    assert path == "/workspace/.venv/bin/ruff"
    assert source == "project"
    assert version == "ruff 0.17.0"
    assert container.exec_run.call_count == 2


def test_resolve_uv_tool_falls_back_to_image_only_when_project_tool_absent() -> None:
    container = MagicMock()
    container.exec_run.side_effect = [
        (1, (b"", b"")),
        (0, (b"/home/sandbox/.venv/bin/pyright\n", b"")),
        (0, (b"pyright 1.1.414\n", b"")),
    ]

    path, source, version = resolve_uv_tool(container, "/workspace", "pyright")

    assert path == "/home/sandbox/.venv/bin/pyright"
    assert source == "image"
    assert version == "pyright 1.1.414"


def test_pyright_receives_project_interpreter_and_absolute_executable() -> None:
    container = MagicMock()
    with (
        patch("sunaba.environment.is_uv_project_root", return_value=True),
        patch("sunaba.environment.uv_environment_block_reason", return_value=None),
        patch(
            "sunaba.environment.resolve_uv_tool",
            return_value=("/workspace/.venv/bin/pyright", "project", "pyright 1.1"),
        ),
        patch(
            "sunaba.edit_verify.lint_runners._exec_run",
            return_value=(0, '{"generalDiagnostics": []}', ""),
        ) as run,
    ):
        _run_pyright_verify(container, "src", workdir="/workspace")

    command = run.call_args.args[1][-1]
    assert "/workspace/.venv/bin/pyright --outputjson" in command
    assert "--pythonpath /workspace/.venv/bin/python" in command


def test_pytest_command_uses_project_interpreter_when_selected() -> None:
    command = build_pytest_cmd(
        "/tmp/report.xml",
        "/tmp/raw.txt",
        "",
        "tests/",
        python_executable="/workspace/.venv/bin/python",
    )

    assert "/workspace/.venv/bin/python -m pytest" in command
    assert "python3 -m pytest" not in command



def test_actual_verify_success_then_uv_failure_invalidates_publish_gate() -> None:
    cid = "uv_verify_then_fail"
    container, executed = _make_mock_container(
        cid, {"pyproject.toml", "uv.lock"}, uv_sync_ec=1
    )
    original_exec = container.exec_run.side_effect
    junit = (
        b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        b'<testcase classname="test_sample" name="test_pass" /></testsuite></testsuites>'
        b'\n---PYTEST-RAW---\n'
    )

    def exec_with_test_report(cmd, **kwargs):
        command = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        if "--junit-xml=" in command:
            executed.append(command)
            return 0, (junit, b"")
        return original_exec(cmd, **kwargs)

    container.exec_run.side_effect = exec_with_test_report
    client = _make_client_mock(container)
    detection = MagicMock(
        languages={"python"}, scope={"python": "."}, reason=None
    )
    with (
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.edit_verify.detect_languages", return_value=detection),
        patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value={
                "gate_passed": True,
                "incomplete": False,
                "lint": [],
                "types": [],
                "patch_targets": [],
                "gate_fail_reasons": [],
            },
        ),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
    ):
        record_environment_state(cid, "ready")
        verified = __import__("json").loads(
            verify_in_container(container_id=cid, path="tests/")
        )
        assert verified["gate_passed"] is True
        assert has_verify_success(cid) is True

        failed = _install_repo_deps(
            container, "owner/repo", "/workspace", "[dev]"
        )
        assert failed.failed is True
        assert has_verify_success(cid) is False

        result = __import__("json").loads(
            publish(
                container_id=cid,
                repo="owner/repo",
                branch="fix/test",
                message="test",
                skip_verify_gate=False,
            )
        )
        assert result["status"] == "error"
        assert result["step"] == "verify_gate"
        assert not any("git commit" in command or "git push" in command for command in executed)



def test_uv_to_non_uv_preparation_then_actual_verify_restores_publish_gate() -> None:
    import json

    cid = "uv_branch_transition"
    root_files = {"pyproject.toml", "uv.lock"}
    container, executed = _make_mock_container(cid, root_files)
    original_exec = container.exec_run.side_effect
    junit = (
        b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        b'<testcase classname="test_sample" name="test_pass" /></testsuite></testsuites>'
        b'\n---PYTEST-RAW---\n'
    )

    def exec_with_test_report(cmd, **kwargs):
        command = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        if "--junit-xml=" in command:
            executed.append(command)
            return 0, (junit, b"")
        return original_exec(cmd, **kwargs)

    container.exec_run.side_effect = exec_with_test_report
    client = _make_client_mock(container)
    detection = MagicMock(languages={"python"}, scope={"python": "."}, reason=None)
    clean_gate = {
        "gate_passed": True, "incomplete": False, "lint": [], "types": [],
        "patch_targets": [], "gate_fail_reasons": [],
    }

    with (
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        patch("sunaba.edit_verify.detect_languages", return_value=detection),
        patch("sunaba.edit_verify.run_lint_type_gate", return_value=clean_gate),
    ):
        record_environment_state(cid, "failed", reason="sync_failed")
        assert has_verify_success(cid) is False

        # Simulate checkout of a non-uv branch at the same root.
        root_files.clear()
        root_files.add("pyproject.toml")
        prepared = _install_repo_deps(container, "owner/repo", "/workspace", "[dev]")
        assert prepared.failed is False
        assert not any("uv sync" in command for command in executed)
        assert any("pip install" in command for command in executed)

        verified = json.loads(verify_in_container(container_id=cid, path="tests/"))
        assert verified["gate_passed"] is True
        assert has_verify_success(cid) is True

        published = json.loads(publish(
            container_id=cid, repo="owner/repo", branch="fix/transition",
            message="transition", skip_verify_gate=False,
        ))
        assert published.get("step") != "verify_gate"


def test_uv_prerequisite_failure_invalidates_success_and_is_journaled_as_environment_failure() -> None:
    import json

    cid = "uv_missing_pytest"
    container, executed = _make_mock_container(cid, {"pyproject.toml", "uv.lock"})
    original_exec = container.exec_run.side_effect
    junit = (
        b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        b'<testcase classname="test_sample" name="test_pass" /></testsuite></testsuites>'
        b'\n---PYTEST-RAW---\n'
    )

    def exec_with_test_report(cmd, **kwargs):
        command = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        if "--junit-xml=" in command:
            executed.append(command)
            return 0, (junit, b"")
        return original_exec(cmd, **kwargs)

    container.exec_run.side_effect = exec_with_test_report
    client = _make_client_mock(container)
    detection = MagicMock(languages={"python"}, scope={"python": "."}, reason=None)
    ready_toolchain = {
        "python": {"path": "/workspace/.venv/bin/python", "source": "project", "version": "Python 3.14"},
        "pytest": {"path": "/workspace/.venv/bin/python", "source": "project", "version": "pytest 8"},
        "ruff": {"path": "/workspace/.venv/bin/ruff", "source": "project", "version": "ruff"},
        "pyright": {"path": "/workspace/.venv/bin/pyright", "source": "project", "version": "pyright"},
    }
    missing_pytest = {
        **ready_toolchain,
        "pytest": {"path": None, "source": None, "version": None},
    }
    clean_gate = {
        "gate_passed": True, "incomplete": False, "lint": [], "types": [],
        "patch_targets": [], "gate_fail_reasons": [],
    }
    with (
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        patch("sunaba.tools.verify.record_tool_use"),
        patch("sunaba.edit_verify.detect_languages", return_value=detection),
        patch("sunaba.edit_verify.run_lint_type_gate", return_value=clean_gate),
        patch(
            "sunaba.environment.resolve_uv_python_tools",
            side_effect=[ready_toolchain, missing_pytest],
        ),
    ):
        record_environment_state(cid, "ready")
        verified = json.loads(verify_in_container(container_id=cid, path="tests/"))
        assert verified["gate_passed"] is True
        assert has_verify_success(cid) is True

        junit_count_before_failure = sum("--junit-xml" in command for command in executed)
        failed = json.loads(verify_in_container(
            container_id=cid, path="tests/", skip_lint_gate=True,
            skip_type_gate=True, skip_patch_targets_gate=True,
        ))
        assert failed["status"] == "environment_setup_failed"
        assert failed["gate_fail_kinds"] == ["environment_setup"]
        assert failed["tests"]["status"] == "skipped"
        assert has_verify_success(cid) is False
        assert sum("--junit-xml" in command for command in executed) == junit_count_before_failure

        refused = json.loads(publish(
            container_id=cid, repo="owner/repo", branch="fix/missing-pytest",
            message="missing pytest", skip_verify_gate=False,
        ))
        assert refused["status"] == "error"
        assert refused["step"] == "verify_gate"

        from sunaba.phase import aggregate_run_phases
        from sunaba.tools.verify import _record_verify_outcome
        captured = []
        with patch("sunaba.tools.verify.record_tool_use", side_effect=lambda *a, **k: captured.append(a)):
            _record_verify_outcome(cid, failed)
        result_entry = {
            "run_id": "phase-test", "ts": "2026-10-18T00:00:00Z",
            "operation": "tool_use", "tool_name": "verify_in_container",
            "params": {"result": captured[-1][2]["result"]},
        }
        phase = aggregate_run_phases(None, [result_entry])["phase-test"]
        assert phase["verify_timeline"][-1]["status"] == "environment_setup_failed"
        assert phase["verify_timeline"][-1]["fail_kinds"] == ["environment_setup"]
        assert phase["verify_timeline"][-1]["environment_setup"]["status"] == "failed"

def test_uv_probe_errors_fail_closed_without_using_fixture_output_as_detection() -> None:
    import json

    cid = "uv_probe_error"
    container = MagicMock()
    container.id = cid
    container.exec_run.return_value = (2, (b"pyproject.toml\nuv.lock\n", b"probe failed"))
    client = _make_client_mock(container)
    with (
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.edit_verify.detect_languages"),
    ):
        result = json.loads(verify_in_container(container_id=cid, path="tests/"))
    assert result["status"] == "environment_setup_failed"
    assert result["gate_fail_kinds"] == ["environment_setup"]
    assert result["tests"]["status"] == "skipped"
    # Language sniffing may precede the root probe; no validator or test
    # command may run after that probe fails.
    actual_container = client.containers.get.return_value
    commands = [
        call.args[0][-1] if isinstance(call.args[0], (list, tuple)) else str(call.args[0])
        for call in actual_container.exec_run.call_args_list
        if call.args
    ]
    assert not any("--junit-xml" in command for command in commands)
    assert not any("ruff check" in command or "pyright --outputjson" in command for command in commands)



def test_shared_pytest_runner_uses_same_resolved_uv_python() -> None:
    container = MagicMock()
    container.id = "uv_runner_ready"
    report = (
        b'<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0">'
        b'<testcase classname="test_x" name="test_y" /></testsuite></testsuites>'
        b'\n---PYTEST-RAW---\n'
    )
    with (
        patch("sunaba.environment.probe_uv_project_root", return_value=True),
        patch("sunaba.environment.uv_environment_block_reason", return_value=None),
        patch(
            "sunaba.environment.resolve_uv_python_tools",
            return_value={"python": {"path": "/workspace/.venv/bin/python", "source": "project", "version": "Python 3.14"}},
        ),
        patch("sunaba.edit_verify.test_runners._exec_run", return_value=(0, report.decode(), "")) as run,
    ):
        _run_pytest_verify(container, "tests/test_one.py", workdir="/workspace")

    command = run.call_args.args[1][-1]
    assert "/workspace/.venv/bin/python -m pytest" in command
    assert "python3 -m pytest" not in command


def test_shared_pytest_runner_refuses_unprepared_uv_root_without_execution() -> None:
    container = MagicMock()
    container.id = "uv_runner_unprepared"
    with (
        patch("sunaba.environment.probe_uv_project_root", return_value=True),
        patch(
            "sunaba.environment.uv_environment_block_reason",
            return_value="uv environment not_prepared: pip_extras=None",
        ),
        patch("sunaba.edit_verify.test_runners._exec_run") as run,
    ):
        result = _run_pytest_verify(container, "tests/", workdir="/workspace")

    assert result.status == "error"
    assert "not_prepared" in result.detail
    run.assert_not_called()



def test_uv_sync_execution_exception_marks_environment_failed() -> None:
    from sunaba.verify_state import get_environment_state

    cid = "uv_sync_exec_error"
    container, _ = _make_mock_container(cid, {"pyproject.toml", "uv.lock"})
    original_exec = container.exec_run.side_effect

    def timeout_sync(cmd, **kwargs):
        command = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        if "uv sync --locked --all-groups" in command:
            raise TimeoutError("uv sync execution exception")
        return original_exec(cmd, **kwargs)

    container.exec_run.side_effect = timeout_sync
    with patch("sunaba.verify_state._environment_state", {}):
        result = _install_repo_deps(container, "owner/repo", "/workspace", "[dev]")
        state = get_environment_state(cid)

    assert result.failed is True
    assert state["status"] == "failed"
    assert "execution exception" in state["reason"]



def test_resolve_uv_tool_does_not_fallback_after_docker_exec_exception() -> None:
    container = MagicMock()
    container.exec_run.side_effect = RuntimeError("docker transport failure")

    with pytest.raises(EnvironmentResolutionError, match="docker transport failure"):
        resolve_uv_tool(container, "/workspace", "ruff")

    assert container.exec_run.call_count == 1


def test_resolve_uv_python_tools_does_not_fallback_after_docker_exec_exception() -> None:
    container = MagicMock()
    container.exec_run.side_effect = RuntimeError("docker transport failure")

    with pytest.raises(EnvironmentResolutionError, match="docker transport failure"):
        resolve_uv_python_tools(container, "/workspace")

    assert container.exec_run.call_count == 1


def test_project_tool_probe_error_does_not_fallback_to_image() -> None:
    container = MagicMock()
    container.exec_run.side_effect = [
        (0, (b"", b"")),
        (2, (b"", b"project tool config failed")),
    ]

    with pytest.raises(EnvironmentResolutionError, match="version probe failed"):
        resolve_uv_tool(container, "/workspace", "ruff")

    assert container.exec_run.call_count == 2


def test_uv_install_without_network_records_network_disabled() -> None:
    from sunaba.verify_state import get_environment_state

    cid = "uv_network_disabled"
    container, executed = _make_mock_container(cid, {"pyproject.toml", "uv.lock"})

    result = _install_repo_deps(
        container, "owner/repo", "/workspace", "[dev]", allow_network=False
    )

    assert result.failed is True
    state = get_environment_state(cid)
    assert state is not None
    assert state["status"] == "failed"
    assert state["reason"] == "network_disabled"
    assert not any("uv sync" in command for command in executed)



def test_uv_tool_resolution_exception_fails_verify_and_invalidates_success() -> None:
    import json

    cid = "uv_resolution_exception"
    container, executed = _make_mock_container(cid, {"pyproject.toml", "uv.lock"})
    client = _make_client_mock(container)
    detection = MagicMock(languages={"python"}, scope={"python": "."}, reason=None)
    with (
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.edit_verify.detect_languages", return_value=detection),
        patch("sunaba.edit_verify.run_lint_type_gate") as lint_type_gate,
        patch(
            "sunaba.environment.resolve_uv_python_tools",
            side_effect=RuntimeError("docker transport failure"),
        ),
    ):
        record_environment_state(cid, "ready")
        from sunaba.verify_state import record_verify_success

        record_verify_success(cid)
        result = json.loads(verify_in_container(container_id=cid, path="tests/"))

    assert result["status"] == "environment_setup_failed"
    assert result["gate_fail_kinds"] == ["environment_setup"]
    assert "docker transport failure" in result["environment_setup"]["reason"]
    assert result["tests"]["status"] == "skipped"
    assert has_verify_success(cid) is False
    lint_type_gate.assert_not_called()
    assert not any(
        "ruff check" in command
        or "pyright --outputjson" in command
        or "--junit-xml" in command
        for command in executed
    )


def test_shared_pytest_runner_reports_resolution_exception_without_running_tests() -> None:
    container = MagicMock()
    container.id = "uv_pytest_resolution_error"
    with (
        patch("sunaba.environment.probe_uv_project_root", return_value=True),
        patch("sunaba.environment.uv_environment_block_reason", return_value=None),
        patch(
            "sunaba.environment.resolve_uv_python_tools",
            side_effect=RuntimeError("docker transport failure"),
        ),
        patch("sunaba.edit_verify.test_runners._exec_run") as run,
    ):
        result = _run_pytest_verify(container, "tests/", workdir="/workspace")

    assert result.status == "error"
    assert "docker transport failure" in result.detail
    run.assert_not_called()
