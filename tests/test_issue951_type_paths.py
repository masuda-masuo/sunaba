"""Acceptance regressions for #951 type-check path and runner handling."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sunaba.edit_verify import type_check_file
from sunaba.edit_verify.paths import ScopeWorkdir, _determine_scope
from sunaba.verify_state import record_environment_state


@pytest.fixture(autouse=True)
def _isolate_environment_state():
    with patch("sunaba.verify_state._environment_state", {}), patch("sunaba.verify_state._verify_map", {}):
        yield


class _SubprocessContainer:
    """Run Docker exec argv locally, honoring WORKDIR and exec workdir."""

    def __init__(self, root: Path, container_id: str):
        self.id, self.root = container_id, root
        self.attrs = {"Config": {"WorkingDir": str(root)}}
        self.calls: list[tuple[list[str], Path]] = []

    def exec_run(self, command, **kwargs):
        argv = list(command) if isinstance(command, (list, tuple)) else [command]
        cwd = Path(kwargs.get("workdir") or self.root)
        if not cwd.is_absolute():
            cwd = self.root / cwd
        self.calls.append((argv, cwd))
        tool_paths = [shutil.which(name) for name in ("pyright", "tsc")]
        tool_dirs = [str(Path(path).parent) for path in tool_paths if path]
        inherited_path = os.environ.get("PATH", "")
        env = {**os.environ, "PATH": ":".join([*tool_dirs, inherited_path])}
        result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)  # noqa: S603
        return result.returncode, (result.stdout, result.stderr)


def _client(root: Path, container_id: str):
    container = _SubprocessContainer(root, container_id)
    client = MagicMock()
    client.containers.get.return_value = container
    return client, container


def _pyright():
    executable = shutil.which("pyright")
    assert executable, "actual pyright executable must be available for this regression"
    return Path(executable).resolve()


def _diagnostic_file(findings):
    errors = [item for item in findings if item["rule"] == "reportAssignmentType"]
    assert errors, findings
    return errors[0]["file"]


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_tests_file_reports_real_pyright_diagnostic_without_duplicate_tests_path(tmp_path, absolute):
    root = tmp_path / "tests-project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pyright]\n", encoding="utf-8")
    target = root / "tests" / "test_x.py"
    target.write_text("value: int = 'wrong'\n", encoding="utf-8")
    client, container = _client(root, "issue951-tests-path")
    path = str(target) if absolute else "tests/test_x.py"
    findings = type_check_file(client, "issue951-tests-path", path, scope_workdir=_determine_scope(path))
    calls = [(argv, cwd) for argv, cwd in container.calls if "pyright" in " ".join(argv)]
    assert calls and calls[0][1] == root, calls
    assert "tests/test_x.py" in " ".join(calls[0][0])
    diagnostic = _diagnostic_file(findings).replace("\\", "/")
    assert diagnostic.endswith("tests/test_x.py"), findings
    assert "tests/tests/test_x.py" not in diagnostic, findings
    assert not any(item["rule"] == "error" for item in findings), findings


def test_relative_src_file_runs_pyright_from_absolute_project_root(tmp_path):
    root = tmp_path / "src-project"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.pyright]\npythonVersion = '3.12'\n", encoding="utf-8")
    (root / "src" / "foo.py").write_text("value: int = 'wrong'\n", encoding="utf-8")
    client, container = _client(root, "issue951-src-cwd")
    findings = type_check_file(client, "issue951-src-cwd", "src/foo.py", scope_workdir=_determine_scope("src/foo.py"))
    _diagnostic_file(findings)
    calls = [(argv, cwd) for argv, cwd in container.calls if "pyright" in " ".join(argv)]
    assert calls
    command, cwd = " ".join(calls[0][0]), calls[0][1]
    assert cwd == root, calls
    assert f"--project {root}" in command, calls
    assert "src/foo.py" in command, calls


def test_nested_uv_project_uses_project_pyright_interpreter_and_root(tmp_path):
    root = tmp_path / "outer" / "nested"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='nested-fixture'\nversion='0'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (root / "src" / "foo.py").write_text("value: int = 'wrong'\n", encoding="utf-8")
    project_bin = root / ".venv" / "bin"
    project_bin.mkdir(parents=True)
    project_pyright, project_python = project_bin / "pyright", project_bin / "python"
    project_pyright.symlink_to(_pyright())
    project_python.symlink_to(Path(sys.executable).resolve())
    record_environment_state("issue951-nested-uv", "ready")
    client, container = _client(tmp_path / "outer", "issue951-nested-uv")
    path = "nested/src/foo.py"
    findings = type_check_file(client, "issue951-nested-uv", path, scope_workdir=_determine_scope(path))
    _diagnostic_file(findings)
    calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if "pyright --outputjson" in " ".join(argv)
    ]
    assert calls
    command = " ".join(calls[0][0])
    assert calls[0][1] == root, calls
    assert str(project_pyright) in command, calls
    assert f"--pythonpath {project_python}" in command, calls
    assert f"--project {root}" in command, calls
    assert "nested/src/foo.py" not in command and "src/foo.py" in command, calls


@pytest.mark.parametrize("extension,runner", [(".py", "_run_pyright_verify"), (".ts", "_run_tsc_verify")])
def test_scope_stage_execution_error_is_reported(extension, runner, tmp_path):
    root = tmp_path / "scope-error"
    root.mkdir()
    if extension == ".ts":
        (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    client, container = _client(root, "issue951-scope-error")
    failed = SimpleNamespace(status="error", detail="scope runner failed", findings=[])
    stage = "_run_python_typecheck" if extension == ".py" else "_run_ts_typecheck"
    with (
        patch(f"sunaba.edit_verify.single_file.{stage}", return_value=[]),
        patch(f"sunaba.edit_verify.single_file.{runner}", return_value=failed) as scope_runner,
    ):
        findings = type_check_file(
            client, "issue951-scope-error", "clean" + extension, scope_workdir=ScopeWorkdir(".", str(root))
        )
    assert any(item["rule"] == "error" for item in findings), findings
    assert any("scope runner failed" in item["message"] for item in findings), findings
    if extension == ".ts":
        scope_runner.assert_called_once_with(
            container, ".", workdir=str(root), project=True
        )


@pytest.mark.parametrize("extension,runner", [(".py", "_run_pyright_verify"), (".ts", "_run_tsc_verify")])
def test_single_stage_execution_error_is_not_reported_as_missing_checker(extension, runner, tmp_path):
    root = tmp_path / "single-error"
    root.mkdir()
    client, _ = _client(root, "issue951-single-error")
    failed = SimpleNamespace(status="error", detail="single runner failed", findings=[])
    with patch(f"sunaba.edit_verify.single_file.{runner}", return_value=failed):
        findings = type_check_file(client, "issue951-single-error", "broken" + extension)
    assert any(item["rule"] == "error" for item in findings), findings
    assert any("single runner failed" in item["message"] for item in findings), findings
    assert not any(item["rule"] == "no-typechecker" for item in findings), findings


@pytest.mark.parametrize("extension,runner", [(".py", "_run_pyright_verify"), (".ts", "_run_tsc_verify")])
def test_only_not_available_single_stage_reports_missing_checker(extension, runner, tmp_path):
    root = tmp_path / "unavailable"
    root.mkdir()
    client, _ = _client(root, "issue951-not-available")
    unavailable = SimpleNamespace(status="not_available", detail="executable absent", findings=[])
    with patch(f"sunaba.edit_verify.single_file.{runner}", return_value=unavailable):
        findings = type_check_file(client, "issue951-not-available", "missing" + extension)
    assert len(findings) == 1 and findings[0]["rule"] == "no-typechecker", findings


def test_typescript_single_stage_receives_resolved_project_workdir(tmp_path):
    root = tmp_path / "ts-project"
    (root / "src").mkdir(parents=True)
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    client, _ = _client(root, "issue951-ts-cwd")
    clean, calls = SimpleNamespace(status="ok", detail="", findings=[]), []
    with patch(
        "sunaba.edit_verify.single_file._run_tsc_verify",
        side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or clean,
    ):
        findings = type_check_file(
            client, "issue951-ts-cwd", "src/app.ts", scope_workdir=_determine_scope("src/app.ts")
        )
    assert findings == [] and calls
    assert calls[0][1].get("workdir") == str(root), calls


def test_path_resolution_exception_is_reported_as_type_check_error(tmp_path):
    root = tmp_path / "resolution-error"
    root.mkdir()
    client, _ = _client(root, "issue951-resolution-error")
    with patch("sunaba.edit_verify.single_file._exec_run", return_value=(1, "", "expected project probe failure")):
        findings = type_check_file(
            client, "issue951-resolution-error", "tests/test_x.py", scope_workdir=_determine_scope("tests/test_x.py")
        )
    assert len(findings) == 1 and findings[0]["rule"] == "error", findings
    assert "type_check path resolution failed" in findings[0]["message"], findings
    assert "expected project probe failure" in findings[0]["message"], findings


def test_missing_docker_attrs_preserves_absolute_src_pyright_project_fallback(tmp_path):
    root = tmp_path / "legacy-project"
    (root / "src").mkdir(parents=True)
    target = root / "src" / "foo.py"
    target.write_text("value: int = 'wrong'\n", encoding="utf-8")
    client, container = _client(root, "issue951-missing-attrs")
    container.attrs = None
    findings = type_check_file(client, "issue951-missing-attrs", str(target))
    _diagnostic_file(findings)
    calls = [(argv, cwd) for argv, cwd in container.calls if "pyright" in " ".join(argv)]
    assert calls
    assert f"--project {root}" in " ".join(calls[0][0]), calls
    assert calls[0][1] == root, calls


def test_nested_uv_project_without_prepared_environment_is_an_explicit_error(tmp_path):
    root = tmp_path / "outer" / "nested"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='unprepared-fixture'\nversion='0'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    target = root / "tests" / "test_x.py"
    target.write_text("value: int = 1\n", encoding="utf-8")
    client, container = _client(tmp_path / "outer", "issue951-uv-unprepared")
    path = "nested/tests/test_x.py"

    findings = type_check_file(
        client,
        "issue951-uv-unprepared",
        path,
        scope_workdir=_determine_scope(path),
    )

    assert any(item["rule"] == "error" for item in findings), findings
    assert any("uv environment not_prepared" in item["message"] for item in findings)
    assert not any("pyright" in " ".join(argv) for argv, _ in container.calls)
