"""Tests for uv workspace root resolution and --all-packages sync (#972)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from sunaba.environment import (
    probe_uv_project_root,
    resolve_uv_python_tools,
    resolve_uv_root,
    resolve_uv_tool,
)
from sunaba.tools.container.clone import prepare_uv_environment


class MinimalRealExecContainer:
    """Minimal real-exec container adapter running commands via subprocess."""

    def __init__(self, root: str = "/workspace", id: str = "test-uv-container-1234") -> None:
        self.id = id
        self.root = root
        self.exec_calls: list[list[str] | str] = []
        self.exec_outputs: list[str] = []

    def exec_run(
        self,
        cmd: list[str] | str,
        stdout: bool = True,
        stderr: bool = True,
        demux: bool = True,
        environment: dict[str, str] | None = None,
        workdir: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, tuple[bytes, bytes] | bytes]:
        if isinstance(cmd, (list, tuple)):
            argv = list(cmd)
        else:
            argv = ["/bin/sh", "-c", str(cmd)]

        env = dict(os.environ)
        if environment:
            env.update(environment)

        cwd = workdir or self.root
        self.exec_calls.append(cmd)
        res = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            check=False,
            env=env,
        )
        self.exec_outputs.append((res.stdout + res.stderr).decode("utf-8", errors="replace"))
        if demux:
            return res.returncode, (res.stdout, res.stderr)
        return res.returncode, res.stdout + res.stderr


def test_resolve_uv_root_criteria(tmp_path: Path) -> None:
    """Acceptance criterion 1: uv workspace root resolution test matrix."""
    container = MinimalRealExecContainer(root=str(tmp_path))

    # Real uv workspace: root + 2 members
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir()
    (ws_dir / "pyproject.toml").write_text(
        """[project]
name = "workspace-root"
version = "0.1.0"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]

[dependency-groups]
dev = ["pytest>=7.0.0", "ruff>=0.1.0"]
"""
    )
    pkg_a = ws_dir / "packages" / "a"
    pkg_a_src = pkg_a / "src" / "pkg_a" / "sub"
    pkg_a_src.mkdir(parents=True)
    (pkg_a / "pyproject.toml").write_text(
        """[project]
name = "pkg-a"
version = "0.1.0"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    )
    (pkg_a / "src" / "pkg_a" / "__init__.py").write_text('__version__ = "0.1.0"\n')

    pkg_b = ws_dir / "packages" / "b"
    pkg_b_src = pkg_b / "src" / "pkg_b"
    pkg_b_src.mkdir(parents=True)
    (pkg_b / "pyproject.toml").write_text(
        """[project]
name = "pkg-b"
version = "0.1.0"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    )
    (pkg_b_src / "__init__.py").write_text('__version__ = "0.1.0"\n')

    # Run real uv lock in workspace
    subprocess.run(["uv", "lock"], cwd=str(ws_dir), check=True, capture_output=True)

    # 1a: Returns workspace root for a member
    assert resolve_uv_root(container, str(pkg_a)) == str(ws_dir)

    # 1b: Returns workspace root for a subdirectory of a member
    member_probe_start = len(container.exec_outputs)
    assert resolve_uv_root(container, str(pkg_a_src)) == str(ws_dir)
    assert any(
        line.startswith("above:")
        for line in container.exec_outputs[member_probe_start].splitlines()
    )

    # 1c: Returns project itself for a standalone locked project
    standalone_dir = tmp_path / "standalone"
    standalone_dir.mkdir()
    (standalone_dir / "pyproject.toml").write_text(
        """[project]
name = "standalone"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["pytest>=7.0.0"]
"""
    )
    subprocess.run(["uv", "lock"], cwd=str(standalone_dir), check=True, capture_output=True)
    assert resolve_uv_root(container, str(standalone_dir)) == str(standalone_dir)

    # Tool-only manifests are not uv projects without a lock in the ancestor chain.
    tool_only_dir = tmp_path / "tool_only"
    tool_only_dir.mkdir()
    (tool_only_dir / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    tool_only_container = MinimalRealExecContainer(root=str(tmp_path))
    assert resolve_uv_root(tool_only_container, str(tool_only_dir)) is None
    assert len(tool_only_container.exec_calls) == 1

    # A tool-only manifest inside a locked workspace reaches uv, which rejects its missing project table.
    nested_tool_only_dir = ws_dir / "nested_tool_only"
    nested_tool_only_dir.mkdir()
    (nested_tool_only_dir / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    nested_tool_container = MinimalRealExecContainer(root=str(tmp_path))
    assert resolve_uv_root(nested_tool_container, str(nested_tool_only_dir)) is None
    assert len(nested_tool_container.exec_calls) == 2
    assert "No `project` table found" in nested_tool_container.exec_outputs[-1]

    # 1d: Returns None for a non-member nested project without its own uv.lock
    non_member_dir = ws_dir / "non_member"
    non_member_dir.mkdir()
    (non_member_dir / "pyproject.toml").write_text(
        """[project]
name = "non-member"
version = "0.1.0"
dependencies = []
"""
    )
    assert resolve_uv_root(container, str(non_member_dir)) is None

    # 1e: Returns None when there is no pyproject.toml up the tree
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    assert resolve_uv_root(container, str(empty_dir)) is None

    # Simulate with PATH without uv
    class NoUvContainer(MinimalRealExecContainer):
        def exec_run(
            self,
            cmd: list[str] | str,
            stdout: bool = True,
            stderr: bool = True,
            demux: bool = True,
            environment: dict[str, str] | None = None,
            workdir: str | None = None,
            **kwargs: Any,
        ) -> tuple[int, tuple[bytes, bytes] | bytes]:
            cmd_str = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
            # Remove uv from PATH
            cmd_no_uv = f"export PATH=/usr/sbin:/sbin; {cmd_str}"
            return super().exec_run(
                ["/bin/sh", "-c", cmd_no_uv],
                stdout=stdout,
                stderr=stderr,
                demux=demux,
                environment=environment,
                workdir=workdir,
                **kwargs,
            )

    # A directory tree with no pyproject.toml returns None and uv workspace dir is never invoked
    # (e.g. PATH without uv still returns None there)
    no_uv_empty = NoUvContainer(root=str(empty_dir))
    assert resolve_uv_root(no_uv_empty, str(empty_dir)) is None
    assert len(no_uv_empty.exec_calls) == 1

    # A locked standalone project and workspace root resolve before invoking uv.
    no_uv_standalone = NoUvContainer(root=str(standalone_dir))
    assert resolve_uv_root(no_uv_standalone, str(standalone_dir)) == str(standalone_dir)
    assert len(no_uv_standalone.exec_calls) == 1
    assert resolve_uv_root(no_uv_standalone, str(ws_dir)) == str(ws_dir)

    # A workspace member has no uv.lock, so resolving it requires uv.
    no_uv_member = NoUvContainer(root=str(pkg_a))
    with pytest.raises(RuntimeError, match="uv workspace dir failed"):
        resolve_uv_root(no_uv_member, str(pkg_a))

    # Raises when uv workspace dir fails for another reason:
    # Simulate with a broken pyproject.toml
    broken_dir = ws_dir / "broken"
    broken_dir.mkdir()
    (broken_dir / "pyproject.toml").write_text("invalid toml syntax [[[\n")
    with pytest.raises(RuntimeError):
        resolve_uv_root(container, str(broken_dir))


def test_prepare_uv_environment_workspace_sync(tmp_path: Path) -> None:
    """Acceptance criterion 2: prepare_uv_environment installs members and dev dependencies."""
    ws_dir = tmp_path / "ws_sync"
    ws_dir.mkdir()
    (ws_dir / "pyproject.toml").write_text(
        """[project]
name = "ws-sync"
version = "0.1.0"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]

[dependency-groups]
dev = ["pytest>=7.0.0"]
"""
    )
    for name in ("a", "b"):
        p = ws_dir / "packages" / name / "src" / f"pkg_{name}"
        p.mkdir(parents=True)
        (ws_dir / "packages" / name / "pyproject.toml").write_text(
            f"""[project]
name = "pkg-{name}"
version = "0.1.0"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
        )
        (p / "__init__.py").write_text(f'__version__ = "0.1.0"\nNAME = "{name}"\n')

    subprocess.run(["uv", "lock"], cwd=str(ws_dir), check=True, capture_output=True)

    container = MinimalRealExecContainer(root=str(ws_dir))
    res = prepare_uv_environment(container, str(ws_dir))
    assert res.success is True, f"prepare_uv_environment failed: {res.message}"
    assert "uv sync --locked --all-groups --all-packages ok" in res.message

    # Assert root .venv contains every member and pytest
    venv_python = ws_dir / ".venv" / "bin" / "python"
    assert venv_python.exists()

    # Import the members
    cp_import = subprocess.run(
        [str(venv_python), "-c", "import pkg_a; import pkg_b; print(pkg_a.NAME, pkg_b.NAME)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "a b" in cp_import.stdout

    # Run .venv/bin/pytest --version
    venv_pytest = ws_dir / ".venv" / "bin" / "pytest"
    assert venv_pytest.exists()
    cp_pytest = subprocess.run(
        [str(venv_pytest), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "pytest" in cp_pytest.stdout or "pytest" in cp_pytest.stderr


def test_resolve_uv_tools_called_with_member_directory(tmp_path: Path) -> None:
    """Acceptance criterion 3: resolve_uv_python_tools / resolve_uv_tool with member dir."""
    ws_dir = tmp_path / "ws_tools"
    ws_dir.mkdir()
    (ws_dir / "pyproject.toml").write_text(
        """[project]
name = "ws-tools"
version = "0.1.0"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]

[dependency-groups]
dev = ["pytest>=7.0.0", "ruff>=0.1.0"]
"""
    )
    pkg_a = ws_dir / "packages" / "a" / "src" / "pkg_a"
    pkg_a.mkdir(parents=True)
    (ws_dir / "packages" / "a" / "pyproject.toml").write_text(
        """[project]
name = "pkg-a"
version = "0.1.0"
dependencies = []

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
"""
    )
    (pkg_a / "__init__.py").write_text('__version__ = "0.1.0"\n')

    subprocess.run(["uv", "lock"], cwd=str(ws_dir), check=True, capture_output=True)

    container = MinimalRealExecContainer(root=str(ws_dir))
    res = prepare_uv_environment(container, str(ws_dir))
    assert res.success is True

    member_dir = str(ws_dir / "packages" / "a")
    uv_root = resolve_uv_root(container, member_dir)
    assert uv_root is not None
    assert uv_root == str(ws_dir)
    resolved_tools = resolve_uv_python_tools(container, uv_root)

    expected_venv = str(ws_dir / ".venv")
    assert resolved_tools["python"]["path"] is not None
    assert resolved_tools["python"]["path"].startswith(expected_venv)
    assert resolved_tools["pytest"]["path"] is not None
    assert resolved_tools["pytest"]["path"].startswith(expected_venv)

    ruff_path, ruff_source, _ = resolve_uv_tool(container, uv_root, "ruff")
    assert ruff_path is not None
    assert ruff_path.startswith(expected_venv)
    assert ruff_source == "project"


def test_standalone_project_resolved_root_and_tools_equal_todays(tmp_path: Path) -> None:
    """Acceptance criterion 4: For a standalone project at repo root, root and tools equal today's."""
    root_dir = tmp_path / "standalone_repo"
    root_dir.mkdir()
    (root_dir / "pyproject.toml").write_text(
        """[project]
name = "standalone-repo"
version = "0.1.0"
dependencies = []

[dependency-groups]
dev = ["pytest>=7.0.0"]
"""
    )
    subprocess.run(["uv", "lock"], cwd=str(root_dir), check=True, capture_output=True)

    container = MinimalRealExecContainer(root=str(root_dir))
    # Before #972, probe_uv_project_root returned True for root holding pyproject.toml + uv.lock
    assert probe_uv_project_root(container, str(root_dir)) is True

    # Under #972, resolve_uv_root returns the project root itself
    resolved_root = resolve_uv_root(container, str(root_dir))
    assert resolved_root == str(root_dir)

    # After prepare, tool paths equal today's (.venv/bin/python, .venv/bin/pytest under root)
    res = prepare_uv_environment(container, str(root_dir))
    assert res.success is True

    tools = resolve_uv_python_tools(container, str(root_dir))
    assert tools["python"]["path"] == str(root_dir / ".venv" / "bin" / "python")
    assert tools["pytest"]["path"] == str(root_dir / ".venv" / "bin" / "python")


def test_callers_do_not_fallback_to_image_tools_when_root_resolution_fails(tmp_path: Path) -> None:
    """AC 2: resolve_uv_tool / resolve_uv_python_tools do not fall back to image tools

    when root resolution fails in their caller (assert the caller's error result, not a path).
    """
    from sunaba.edit_verify.lint_runners import _run_pyright_verify, _run_ruff_verify
    from sunaba.edit_verify.test_runners import _run_pytest_verify

    # Tree with locked pyproject.toml but uv missing from PATH
    ws_dir = tmp_path / "ws_fail_closed"
    ws_dir.mkdir()
    (ws_dir / "pyproject.toml").write_text(
        """[project]
name = "fail-closed"
version = "0.1.0"
dependencies = []
"""
    )
    subprocess.run(["uv", "lock"], cwd=str(ws_dir), check=True, capture_output=True)

    class NoUvContainer(MinimalRealExecContainer):
        def exec_run(
            self,
            cmd: list[str] | str,
            stdout: bool = True,
            stderr: bool = True,
            demux: bool = True,
            environment: dict[str, str] | None = None,
            workdir: str | None = None,
            **kwargs: Any,
        ) -> tuple[int, tuple[bytes, bytes] | bytes]:
            cmd_str = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
            cmd_no_uv = f"export PATH=/usr/sbin:/sbin; {cmd_str}"
            return super().exec_run(
                ["/bin/sh", "-c", cmd_no_uv],
                stdout=stdout,
                stderr=stderr,
                demux=demux,
                environment=environment,
                workdir=workdir,
                **kwargs,
            )

    no_uv = NoUvContainer(root=str(ws_dir))
    # A subdirectory of the locked root has no manifests of its own, so the resolver
    # must ask uv (absent here) and fail; the locked root itself would take the fast path.
    sub_dir = ws_dir / "sub"
    sub_dir.mkdir()

    # Ruff caller fails with error envelope, does not fall back to image ruff
    ruff_res = _run_ruff_verify(no_uv, "src", workdir=str(sub_dir))
    assert ruff_res.status == "error"
    assert "uv workspace dir failed" in ruff_res.detail

    # Pyright caller fails with error envelope, does not fall back to image pyright
    pyright_res = _run_pyright_verify(no_uv, "src", workdir=str(sub_dir))
    assert pyright_res.status == "error"
    assert "uv workspace dir failed" in pyright_res.detail

    # Pytest caller fails with error envelope, does not run pytest
    pytest_res = _run_pytest_verify(no_uv, "tests/", workdir=str(sub_dir))
    assert pytest_res.status == "error"
    assert "uv workspace dir failed" in pytest_res.detail


class StderrWarningContainer(MinimalRealExecContainer):
    """Real-exec adapter that appends a warning to selected command stderr."""

    def __init__(self, *args: Any, warning_command: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.warning_command = warning_command

    def exec_run(
        self,
        cmd: list[str] | str,
        stdout: bool = True,
        stderr: bool = True,
        demux: bool = True,
        environment: dict[str, str] | None = None,
        workdir: str | None = None,
        **kwargs: Any,
    ) -> tuple[int, tuple[bytes, bytes] | bytes]:
        result = super().exec_run(
            cmd,
            stdout=stdout,
            stderr=stderr,
            demux=demux,
            environment=environment,
            workdir=workdir,
            **kwargs,
        )
        cmd_text = cmd[-1] if isinstance(cmd, (list, tuple)) else str(cmd)
        if demux and self.warning_command in cmd_text:
            ec, (out, err) = result
            return ec, (out, err + b"uv warning: harmless diagnostic\n")
        return result


def test_workspace_member_resolves_when_uv_workspace_dir_prints_stderr_warning(
    tmp_path: Path,
) -> None:
    ws_dir = tmp_path / "warning_workspace"
    member = ws_dir / "packages" / "member"
    member.mkdir(parents=True)
    (ws_dir / "pyproject.toml").write_text(
        """[project]
name = "warning-workspace"
version = "0.1.0"
dependencies = []

[tool.uv.workspace]
members = ["packages/*"]
"""
    )
    (member / "pyproject.toml").write_text(
        """[project]
name = "warning-member"
version = "0.1.0"
dependencies = []
"""
    )
    subprocess.run(["uv", "lock"], cwd=ws_dir, check=True, capture_output=True)
    container = StderrWarningContainer(
        root=str(tmp_path), warning_command="uv workspace dir"
    )

    assert resolve_uv_root(container, str(member)) == str(ws_dir)


def test_standalone_project_resolves_when_combined_probe_prints_stderr_warning(
    tmp_path: Path,
) -> None:
    project = tmp_path / "warning_standalone"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        """[project]
name = "warning-standalone"
version = "0.1.0"
dependencies = []
"""
    )
    subprocess.run(["uv", "lock"], cwd=project, check=True, capture_output=True)
    container = StderrWarningContainer(
        root=str(tmp_path), warning_command="for f in pyproject.toml uv.lock"
    )

    assert resolve_uv_root(container, str(project)) == str(project)
