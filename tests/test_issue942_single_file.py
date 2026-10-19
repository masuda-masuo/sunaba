"""Acceptance tests for #942 single-file lint consistency.

These tests exercise the public lint_file API and the existing unified lint
runners. They pin user-visible findings and runner arguments, not helper layout.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sunaba.edit_verify import lint_file
from sunaba.verify_state import record_environment_state


@pytest.fixture(autouse=True)
def _isolate_environment_state():
    with (
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.verify_state._verify_map", {}),
    ):
        yield


def _client(container: MagicMock | None = None):
    container = container or MagicMock()
    container.id = "issue942-single"
    client = MagicMock()
    client.containers.get.return_value = container
    return client, container


def _result(status: str, detail: str = "", findings=None):
    return SimpleNamespace(status=status, detail=detail, findings=findings or [])


def test_unprepared_uv_project_is_reported_and_does_not_run_ruff():
    client, container = _client()
    container.exec_run.return_value = (0, (b"pyproject.toml\nuv.lock\n", b""))

    findings = lint_file(
        client, "issue942-single", "src/mod.py",
        scope_workdir=("src", "/workspace"),
    )

    assert len(findings) == 1
    assert set(findings[0]) == {"file", "line", "rule", "message"}
    assert findings[0]["file"] == "src/mod.py"
    assert findings[0]["line"] == 0
    assert findings[0]["rule"] == "error"
    assert "uv environment not_prepared" in findings[0]["message"]
    assert not any("ruff check" in c.args[0][-1] for c in container.exec_run.call_args_list)


def test_uv_project_uses_resolved_project_ruff_and_supplied_workdir():
    client, container = _client()
    record_environment_state("issue942-single", "ready")
    commands = []

    def exec_run(argv, **kwargs):
        command = argv[-1]
        commands.append((command, kwargs.get("workdir")))
        if "for f in pyproject.toml uv.lock" in command:
            return 0, (b"pyproject.toml\nuv.lock\n", b"")
        if "test -x /workspace/.venv/bin/ruff" in command:
            return 0, (b"", b"")
        if "/workspace/.venv/bin/ruff --version" in command:
            return 0, (b"ruff 0.17.0\n", b"")
        if "/workspace/.venv/bin/ruff check" in command:
            return 0, (b"[]", b"")
        return 1, (b"", b"unexpected probe")

    container.exec_run.side_effect = exec_run

    findings = lint_file(
        client, "issue942-single", "src/mod.py",
        scope_workdir=("src", "/workspace"),
    )

    assert findings == []
    lint_calls = [(cmd, wd) for cmd, wd in commands if "ruff check" in cmd]
    assert len(lint_calls) == 2
    for command, workdir in lint_calls:
        assert "/workspace/.venv/bin/ruff check" in command
        assert " ruff check" not in command
        assert workdir == "/workspace"


def test_uv_project_with_no_project_or_image_ruff_reports_unavailable():
    client, container = _client()
    record_environment_state("issue942-single", "ready")

    def exec_run(argv, **kwargs):
        command = argv[-1]
        if "for f in pyproject.toml uv.lock" in command:
            return 0, (b"pyproject.toml\nuv.lock\n", b"")
        if "test -x /workspace/.venv/bin/ruff" in command:
            return 1, (b"", b"")
        if "command -v ruff" in command:
            return 0, (b"__SUNABA_TOOL_ABSENT__\n", b"")
        if "ruff check" in command:
            return 0, (b"[]", b"")
        raise AssertionError(f"unexpected command: {command}")

    container.exec_run.side_effect = exec_run
    pylint_findings = [{
        "file": "src/mod.py", "line": 4, "rule": "W0611", "message": "unused import"
    }]

    with patch(
        "sunaba.edit_verify.single_file._run_pylint",
        return_value=pylint_findings,
    ) as pylint:
        findings = lint_file(
            client, "issue942-single", "src/mod.py",
            scope_workdir=("src", "/workspace"),
        )

    assert findings == pylint_findings
    pylint.assert_called_once()


def test_ruff_single_file_error_preserves_detail_and_never_calls_pylint():
    client, _ = _client()
    error = _result("error", "ruff config could not be parsed")
    with (
        patch("sunaba.edit_verify.single_file._run_ruff_verify", return_value=error) as ruff,
        patch("sunaba.edit_verify.single_file._run_pylint") as pylint,
    ):
        findings = lint_file(client, "issue942-single", "src/mod.py")

    ruff.assert_called_once()
    pylint.assert_not_called()
    assert findings == [{
        "file": "src/mod.py",
        "line": 0,
        "rule": "error",
        "message": "ruff could not run: ruff config could not be parsed",
    }]


def test_pylint_fallback_is_used_only_when_ruff_is_unavailable():
    client, _ = _client()
    with (
        patch(
            "sunaba.edit_verify.single_file._run_ruff_verify",
            return_value=_result("not_available", "ruff missing"),
        ) as ruff,
        patch(
            "sunaba.edit_verify.single_file._run_pylint",
            return_value=[{"file": "src/mod.py", "line": 2, "rule": "W0611", "message": "unused"}],
        ) as pylint,
    ):
        findings = lint_file(client, "issue942-single", "src/mod.py")

    ruff.assert_called_once()
    pylint.assert_called_once()
    assert findings[0]["rule"] == "W0611"


@pytest.mark.parametrize("language,path,runner", [
    ("ruff", "src/mod.py", "_run_ruff_verify"),
    ("eslint", "src/app.ts", "_run_eslint_verify"),
])
def test_scope_error_is_returned_with_detail_and_scope_stays_read_only(
    language, path, runner
):
    client, _ = _client()
    target = f"sunaba.edit_verify.single_file.{runner}"
    with patch(
        target,
        side_effect=[_result("ok"), _result("error", f"{language} scope config failure")],
    ) as run:
        findings = lint_file(
            client, "issue942-single", path,
            scope_workdir=("src", "/workspace"), fix=True,
        )

    assert len(findings) == 1
    assert findings[0]["file"] == path
    assert findings[0]["line"] == 0
    assert findings[0]["rule"] == "error"
    assert f"{language} scope config failure" in findings[0]["message"]
    assert run.call_args_list[0].kwargs.get("fix") is True
    assert run.call_args_list[1].kwargs.get("fix") is False
    assert all(call.kwargs.get("workdir") == "/workspace" for call in run.call_args_list)


@pytest.mark.parametrize("path,runner", [
    ("src/mod.py", "_run_ruff_verify"),
    ("src/app.ts", "_run_eslint_verify"),
])
def test_single_file_error_does_not_become_a_clean_result(path, runner):
    client, _ = _client()
    detail = "broken linter configuration"
    with (
        patch(
            f"sunaba.edit_verify.single_file.{runner}",
            return_value=_result("error", detail),
        ) as run,
        patch("sunaba.edit_verify.single_file._run_pylint", return_value=None),
    ):
        findings = lint_file(client, "issue942-single", path, fix=True)

    assert run.call_args.kwargs["fix"] is True

    assert findings and findings[0]["rule"] == "error"
    assert detail in findings[0]["message"]


def test_clean_single_file_output_shape_and_fix_scope_compatibility():
    client, _ = _client()
    calls = []

    def ruff(*args, **kwargs):
        calls.append((args, kwargs))
        return _result("ok")

    with patch("sunaba.edit_verify.single_file._run_ruff_verify", side_effect=ruff):
        findings = lint_file(
            client, "issue942-single", "src/mod.py",
            scope_workdir=("src", "/workspace"), fix=True,
        )

    assert findings == []
    assert [kwargs.get("fix") for _, kwargs in calls] == [True, False]
    assert [kwargs.get("workdir") for _, kwargs in calls] == ["/workspace", "/workspace"]


def test_verify_in_container_keeps_project_lint_scope_independent_of_test_path():
    import json

    from sunaba.tools.verify import verify_in_container
    from tests.conftest import _make_client_mock
    from tests.test_uv_environment_stage1 import _make_mock_container

    cid = "issue942-verify-scope"
    container, _ = _make_mock_container(cid, set())
    client = _make_client_mock(container)
    detection = SimpleNamespace(languages={"python"}, scope={"python": "."}, reason=None)
    failed_gate = {
        "gate_passed": False,
        "incomplete": False,
        "lint": [{"file": "tests/x.py", "line": 1, "rule": "F401", "message": "unused"}],
        "types": [],
        "patch_targets": [],
        "gate_fail_reasons": ["lint failed"],
    }

    with (
        patch("sunaba.verify_state._verify_map", {}),
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.tools.verify._docker", return_value=client),
        patch("sunaba.edit_verify.detect_languages", return_value=detection),
        patch("sunaba.edit_verify.run_lint_type_gate", return_value=failed_gate) as gate,
    ):
        result = json.loads(verify_in_container(container_id=cid, path="tests/unit/"))

    assert result["status"] == "failed"
    assert gate.call_args.args[1] == "src"
    assert gate.call_args.kwargs["lint_scope"] == ["src", "tests"]
    assert gate.call_args.kwargs["gate_on_lint"] is True
