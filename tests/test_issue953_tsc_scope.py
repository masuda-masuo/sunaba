"""Actual-tsc regressions for issue #953 TypeScript scope handling."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sunaba.edit_verify import type_check_file
from sunaba.edit_verify.gate import run_lint_type_gate
from sunaba.edit_verify.lint_runners import _run_tsc_verify
from sunaba.edit_verify.paths import _determine_scope
from sunaba.edit_verify.single_file import SingleFileContext


class _SubprocessContainer:
    """Run tsc commands as subprocesses while honoring container WORKDIR."""

    def __init__(self, root: Path, container_id: str = "issue953-tsc"):
        self.id = container_id
        self.root = root
        self.attrs = {"Config": {"WorkingDir": str(root)}}
        self.calls: list[tuple[list[str], Path]] = []

    def exec_run(self, command, **kwargs):
        argv = list(command) if isinstance(command, (list, tuple)) else [command]
        cwd = Path(kwargs.get("workdir") or self.root)
        if not cwd.is_absolute():
            cwd = self.root / cwd
        self.calls.append((argv, cwd))
        executable = shutil.which("tsc")
        assert executable, "actual TypeScript compiler must be available"
        env = {
            **os.environ,
            "PATH": f"{Path(executable).parent}:{os.environ.get('PATH', '')}",
        }
        result = subprocess.run(  # noqa: S603
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode, (result.stdout, result.stderr)


def _client(root: Path, container_id: str = "issue953-tsc"):
    container = _SubprocessContainer(root, container_id)
    client = MagicMock()
    client.containers.get.return_value = container
    return client, container


def _runner_calls(container: _SubprocessContainer):
    return [
        (argv, cwd)
        for argv, cwd in container.calls
        if "--noEmit" in " ".join(argv)
    ]


def test_tsc_ts6231_directory_exit_two_without_findings_is_execution_error(tmp_path):
    root = tmp_path / "tsc-directory"
    (root / "src").mkdir(parents=True)
    assert shutil.which("tsc"), "actual TypeScript compiler must be available"
    client, container = _client(root)

    result = _run_tsc_verify(container, "src", workdir=str(root))

    assert result.exit_code == 2
    assert result.status == "error"
    assert result.findings == []
    assert result.detail.startswith("error TS6231:"), result.detail
    assert _runner_calls(container)


@pytest.mark.parametrize("exit_code", [1, 2])
def test_tsc_nonzero_exit_with_parsed_diagnostics_remains_findings(exit_code, tmp_path):
    root = tmp_path / f"tsc-findings-{exit_code}"
    root.mkdir()
    container = _SubprocessContainer(root)
    output = "src/bad.ts(1,1): error TS2322: Type 'string' is not assignable to type 'number'.\n"

    with (
        patch("sunaba.edit_verify.lint_runners._resolve_js_tool", return_value=("tsc", "global")),
        patch("sunaba.edit_verify.lint_runners._exec_run", return_value=(exit_code, output, "")),
    ):
        result = _run_tsc_verify(container, "src/bad.ts", workdir=str(root))

    assert result.status == "findings"
    assert result.exit_code == exit_code
    assert any(item["rule"] == "TS2322" for item in result.findings), result.findings


def test_tsc_exit_127_remains_not_available(tmp_path):
    root = tmp_path / "tsc-unavailable"
    root.mkdir()
    container = _SubprocessContainer(root)

    with (
        patch("sunaba.edit_verify.lint_runners._resolve_js_tool", return_value=("tsc", "global")),
        patch("sunaba.edit_verify.lint_runners._exec_run", return_value=(127, "", "tsc absent")),
    ):
        result = _run_tsc_verify(container, "src/file.ts", workdir=str(root))

    assert result.status == "not_available"
    assert result.exit_code == 127


@pytest.mark.parametrize("scope_dir,target_name,sibling_name", [
    ("tests", "clean.test.ts", "bad.test.ts"),
    ("src", "clean.ts", "bad.ts"),
])
def test_project_scope_uses_tsc_project_and_finds_bad_sibling(
    tmp_path, scope_dir, target_name, sibling_name
):
    root = tmp_path / "tsc-project"
    directory = root / scope_dir
    directory.mkdir(parents=True)
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "tsconfig.json").write_text(
        '{"compilerOptions":{"strict":true,"noEmit":true},"include":["**/*.ts"]}\n',
        encoding="utf-8",
    )
    target = directory / target_name
    sibling = directory / sibling_name
    target.write_text("const clean: number = 1;\n", encoding="utf-8")
    sibling.write_text("const broken: number = 'wrong';\n", encoding="utf-8")
    client, container = _client(root)
    relative_path = f"{scope_dir}/{target_name}"

    findings = type_check_file(
        client,
        "issue953-tsc",
        relative_path,
        scope_workdir=_determine_scope(relative_path),
    )

    assert any(
        item["rule"] == "TS2322"
        and item["file"].replace("\\", "/").endswith(f"{scope_dir}/{sibling_name}")
        for item in findings
    ), findings
    calls = _runner_calls(container)
    assert all(cwd == root for _, cwd in calls), calls
    scope_calls = [
        (argv, cwd)
        for argv, cwd in calls
        if "tsc --noEmit -p " in " ".join(argv) or "--project " in " ".join(argv)
    ]
    assert len(scope_calls) == 1, calls
    scope_command = " ".join(scope_calls[0][0])
    assert f"-p {root}" in scope_command or f"--project {root}" in scope_command
    target_calls = [
        argv for argv, _ in calls
        if target_name in " ".join(argv)
    ]
    assert target_calls, calls
    # TS 7.0.2 first emits TS5112 for explicit files beside tsconfig, then
    # the runner retries that file with --ignoreConfig. Other versions run once.
    assert len(target_calls) in (1, 2), calls
    assert all(
        not any(token.endswith(scope_dir) for token in argv)
        for argv, _ in calls
    ), calls


def test_missing_tsconfig_skips_scope_without_error_or_directory_target(tmp_path):
    root = tmp_path / "tsc-no-config"
    source = root / "src"
    source.mkdir(parents=True)
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (source / "clean.ts").write_text("const clean: number = 1;\n", encoding="utf-8")
    client, container = _client(root)
    path = "src/clean.ts"

    findings = type_check_file(
        client, "issue953-tsc", path, scope_workdir=_determine_scope(path)
    )

    assert findings == []
    calls = _runner_calls(container)
    assert len(calls) == 1, calls
    command = " ".join(calls[0][0])
    assert "src/clean.ts" in command
    assert "tsc --noEmit -p " not in command and "--project " not in command
    assert not any(token in command for token in (" src ", " tests "))


@pytest.mark.parametrize(
    "probe_result,expected_detail",
    [
        ((126, "", "probe permission denied"), "probe permission denied"),
        (RuntimeError("probe transport failed"), "probe transport failed"),
    ],
)
def test_tsconfig_probe_failures_are_reported(
    tmp_path, probe_result, expected_detail
):
    root = tmp_path / "tsc-probe-failure"
    root.mkdir()
    container = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    context = SingleFileContext(str(root), "src/clean.ts", "src")

    with (
        patch(
            "sunaba.edit_verify.single_file._resolve_single_file_context",
            return_value=context,
        ),
        patch(
            "sunaba.edit_verify.single_file._run_ts_typecheck",
            return_value=[],
        ),
        patch(
            "sunaba.edit_verify.single_file._exec_run",
            side_effect=[probe_result] if isinstance(probe_result, tuple) else probe_result,
        ) as exec_run,
    ):
        findings = type_check_file(client, "probe-failure", "src/clean.ts")

    assert findings == [{
        "file": "src/clean.ts",
        "line": 0,
        "rule": "error",
        "message": f"tsc project config probe failed: {expected_detail}",
    }]
    exec_run.assert_called_once_with(
        container,
        ["/bin/sh", "-c", "test -f tsconfig.json"],
        workdir=str(root),
    )


def test_gate_reports_tsc_directory_error_as_incomplete_without_failing_gate(tmp_path):
    root = tmp_path / "tsc-gate"
    root.mkdir()
    (root / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "tsconfig.json").write_text(
        '{"compilerOptions":{"strict":true},"include":["src/**/*.ts"]}\n',
        encoding="utf-8",
    )
    (root / "src").mkdir()
    _, container = _client(root)

    result = run_lint_type_gate(
        container,
        ".",
        working_dir=str(root),
        language="ts",
        gate_on_lint=False,
        gate_on_type=True,
    )

    assert result["incomplete"] is True, result
    assert result["gate_passed"] is True, result
    assert result["gate_fail_reasons"] == [], result
