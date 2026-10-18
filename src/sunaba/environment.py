"""Root-only project environment detection and Python tool resolution."""

from __future__ import annotations

import shlex
from typing import Any


class EnvironmentResolutionError(RuntimeError):
    """A container command or tool probe failed while resolving uv tools."""


def _run(container: Any, root: str, command: str) -> tuple[int, str]:
    try:
        ec, output = container.exec_run(
            ["/bin/sh", "-c", f"cd {shlex.quote(root)} && {command}"],
            stdout=True,
            stderr=True,
            demux=True,
        )
    except Exception as exc:
        raise EnvironmentResolutionError(
            f"container command failed: {str(exc)[:180]}"
        ) from exc
    stdout, stderr = output or (b"", b"")
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""
    return ec, (out + err).strip()[:240]


def _test_executable(container: Any, root: str, path: str) -> bool:
    ec, output = _run(container, root, f"test -x {shlex.quote(path)}")
    if ec == 0:
        return True
    if ec == 1:
        return False
    raise EnvironmentResolutionError(
        f"executable probe failed (exit {ec}): {output}"
    )


def _tool_version(container: Any, root: str, path: str) -> str:
    ec, version = _run(container, root, f"{shlex.quote(path)} --version")
    if ec != 0:
        raise EnvironmentResolutionError(
            f"tool version probe failed (exit {ec}): {version}"
        )
    return version

def probe_uv_project_root(container: Any, root: str) -> bool:
    """Reliably detect root uv manifests; raise when the probe itself fails."""
    probe = (
        "for f in pyproject.toml uv.lock; do "
        "[ -f \"$f\" ] && printf '%s\\n' \"$f\" || true; done"
    )
    ec, output = _run(container, root, probe)
    if ec != 0:
        raise RuntimeError(f"uv manifest probe failed (exit {ec}): {output}")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if any(line not in {"pyproject.toml", "uv.lock"} for line in lines):
        raise RuntimeError(f"uv manifest probe returned malformed output: {output[:160]}")
    manifests = set(lines)
    return {"pyproject.toml", "uv.lock"} <= manifests


def is_uv_project_root(container: Any, root: str) -> bool:
    """Return true only when both uv manifests exist directly under *root*.

    Probe failures are raised instead of being mistaken for a non-uv project.
    """
    return probe_uv_project_root(container, root)


def uv_environment_block_reason(container: Any, root: str) -> str | None:
    """Return why a uv root is not runnable, or None for non-uv/ready roots."""
    try:
        if not probe_uv_project_root(container, root):
            return None
    except Exception as exc:
        return f"uv root probe failed: {str(exc)[:180]}"
    from sunaba.verify_state import get_environment_state

    container_id = str(getattr(container, "id", ""))
    state = get_environment_state(container_id)
    status = (state or {}).get("status") or "not_prepared"
    if status != "ready":
        detail = (state or {}).get("reason") or "preparation state missing"
        return f"uv environment {status}: {detail}"
    return None


def resolve_uv_tool(
    container: Any, root: str, tool: str
) -> tuple[str | None, str | None, str | None]:
    """Return executable, source, and version for Ruff or Pyright.

    Image fallback is permitted only after a successful test reports the
    project executable absent. Probe and execution errors raise.
    """
    if tool not in {"ruff", "pyright"}:
        raise ValueError(f"unsupported uv tool: {tool}")
    root = root.rstrip("/")
    project_path = f"{root}/.venv/bin/{tool}"
    if _test_executable(container, root, project_path):
        return project_path, "project", _tool_version(container, root, project_path)

    probe = (
        "env PATH=/home/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin "
        f"sh -c 'command -v {tool}'"
    )
    ec, image_path = _run(container, root, probe)
    image_path = image_path.splitlines()[-1].strip() if image_path else ""
    if ec == 1:
        return None, None, None
    if ec != 0 or not image_path.startswith("/"):
        raise EnvironmentResolutionError(
            f"image executable probe failed (exit {ec}): {image_path[:160]}"
        )
    return image_path, "image", _tool_version(container, root, image_path)


def resolve_uv_python_tools(container: Any, root: str) -> dict[str, dict[str, str | None]]:
    """Resolve the project Python, pytest, Ruff, and Pyright with provenance."""
    root = root.rstrip("/")
    project_python = f"{root}/.venv/bin/python"
    result: dict[str, dict[str, str | None]] = {}

    python_exists = _test_executable(container, root, project_python)
    python_version = (
        _tool_version(container, root, project_python) if python_exists else None
    )
    result["python"] = {
        "path": project_python if python_exists else None,
        "source": "project" if python_exists else None,
        "version": python_version,
    }

    for tool in ("pytest", "ruff", "pyright"):
        project_path = (
            project_python if tool == "pytest" else f"{root}/.venv/bin/{tool}"
        )
        if _test_executable(container, root, project_path):
            if tool == "pytest":
                ec, version = _run(
                    container, root,
                    f"{shlex.quote(project_path)} -m pytest --version",
                )
                if ec != 0:
                    raise EnvironmentResolutionError(
                        f"project pytest probe failed (exit {ec}): {version}"
                    )
            else:
                version = _tool_version(container, root, project_path)
            result[tool] = {
                "path": project_path,
                "source": "project",
                "version": version,
            }
            continue

        if tool == "pytest":
            result[tool] = {"path": None, "source": None, "version": None}
            continue

        image_probe = (
            "env PATH=/home/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin "
            f"sh -c 'command -v {tool}'"
        )
        ec, image_path = _run(container, root, image_probe)
        image_path = image_path.splitlines()[-1].strip() if image_path else ""
        if ec == 1:
            result[tool] = {"path": None, "source": None, "version": None}
            continue
        if ec != 0 or not image_path.startswith("/"):
            raise EnvironmentResolutionError(
                f"image executable probe failed (exit {ec}): {image_path[:160]}"
            )
        result[tool] = {
            "path": image_path,
            "source": "image",
            "version": _tool_version(container, root, image_path),
        }
    return result
