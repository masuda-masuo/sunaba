"""Actual-pyright regressions for issue #951 scope execution."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sunaba.edit_verify import type_check_file
from sunaba.edit_verify.paths import _determine_scope


class _SubprocessContainer:
    def __init__(self, root: Path):
        self.id = "issue951-scope"
        self.root = root
        self.attrs = {"Config": {"WorkingDir": str(root)}}
        self.calls: list[tuple[list[str], Path]] = []

    def exec_run(self, command, **kwargs):
        argv = list(command) if isinstance(command, (list, tuple)) else [command]
        cwd = Path(kwargs.get("workdir") or self.root)
        if not cwd.is_absolute():
            cwd = self.root / cwd
        self.calls.append((argv, cwd))
        executable = shutil.which("pyright")
        assert executable, "actual pyright executable must be available"
        env = {
            **os.environ,
            "PATH": f"{Path(executable).parent}:{os.environ.get('PATH', '')}",
        }
        result = subprocess.run(  # noqa: S603
            argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode, (result.stdout, result.stderr)


def _client(root: Path):
    container = _SubprocessContainer(root)
    client = MagicMock()
    client.containers.get.return_value = container
    return client, container


@pytest.mark.parametrize("scope_dir,target_name,sibling_name", [
    ("tests", "test_clean.py", "test_bad.py"),
    ("src", "clean.py", "bad.py"),
])
def test_clean_file_still_reports_bad_sibling_from_shared_scope(
    tmp_path, scope_dir, target_name, sibling_name
):
    root = tmp_path / "scope-project"
    directory = root / scope_dir
    directory.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[tool.pyright]\npythonVersion = '3.12'\n", encoding="utf-8"
    )
    target = directory / target_name
    sibling = directory / sibling_name
    target.write_text("value: int = 1\n", encoding="utf-8")
    sibling.write_text("value: int = 'wrong'\n", encoding="utf-8")
    client, container = _client(root)
    path = f"{scope_dir}/{target_name}"

    findings = type_check_file(
        client, "issue951-scope", path, scope_workdir=_determine_scope(path)
    )

    assert any(
        item["rule"] == "reportAssignmentType"
        and item["file"].replace("\\", "/").endswith(f"{scope_dir}/{sibling_name}")
        for item in findings
    ), findings
    assert not any(
        item["rule"] == "error" or item["rule"] == "no-typechecker"
        for item in findings
    ), findings

    runner_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if "pyright --outputjson" in " ".join(argv)
    ]
    assert len(runner_calls) == 2, runner_calls
    assert all(cwd == root for _, cwd in runner_calls), runner_calls
    assert all(f"--project {root}" in " ".join(argv) for argv, _ in runner_calls)
    commands = [" ".join(argv) for argv, _ in runner_calls]
    assert any(path in command for command in commands), commands
    assert any(scope_dir in command and target_name not in command for command in commands), commands
