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


def check_uv_environment(container: Any, root: str) -> tuple[str, str | None]:
    """Check uv environment status and fingerprint staleness for root.

    Returns (status, reason_detail).
    status is one of:
      - "ready": prepared and fingerprint matches (reason is None)
      - "stale": manifest changed since preparation
      - "not_prepared": never prepared
      - "failed": preparation failed or probe failed
      - "not_uv": not a uv project root (reason is None)
    """
    from sunaba.verify_state import get_environment_state, invalidate_verify_success

    container_id = str(getattr(container, "id", ""))
    state = get_environment_state(container_id)
    if state is None:
        try:
            if not probe_uv_project_root(container, root):
                return "not_uv", None
        except Exception as exc:
            return "failed", f"uv root probe failed: {str(exc)[:180]}"
        return "not_prepared", "preparation state missing"

    status = state.get("status") or "not_prepared"
    if status != "ready":
        reason = state.get("reason") or "preparation state missing or not_prepared"
        return status, reason

    # Stored status is "ready". Check fingerprint staleness.
    recorded_fp = state.get("fingerprint")
    if not recorded_fp:
        return "ready", None

    from sunaba.environment_state_store import FingerprintProbeError, check_manifest_staleness

    try:
        is_stale, changed = check_manifest_staleness(container, root, recorded_fp)
    except FingerprintProbeError as exc:
        invalidate_verify_success(container_id)
        return "failed", f"fingerprint probe failed: {exc}"
    except Exception as exc:
        invalidate_verify_success(container_id)
        return "failed", f"fingerprint probe failed: {str(exc)[:180]}"

    if is_stale:
        invalidate_verify_success(container_id)
        changed_str = ", ".join(changed)
        reason = f"{changed_str} changed; recreate container to recover"
        return "stale", reason

    return "ready", None


def uv_environment_block_reason(container: Any, root: str) -> str | None:
    """Return why a uv root is not runnable, or None for non-uv/ready roots."""
    status, reason = check_uv_environment(container, root)
    if status in ("ready", "not_uv"):
        return None
    return f"uv environment {status}: {reason}"


def _resolve_image_tool(
    container: Any, root: str, tool: str
) -> tuple[str | None, str | None]:
    """Resolve an image tool, distinguishing explicit absence from exec failure."""
    shell_probe = (
        f"if command -v {tool} >/dev/null 2>&1; then "
        f"path=$(command -v {tool}); printf '__SUNABA_TOOL_FOUND__%s\\n' \"$path\"; "
        "else printf '__SUNABA_TOOL_ABSENT__\\n'; fi"
    )
    probe = (
        "env PATH=/home/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin "
        f"sh -c {shlex.quote(shell_probe)}"
    )
    ec, output = _run(container, root, probe)
    lines = output.splitlines()
    if ec != 0 or len(lines) != 1:
        raise EnvironmentResolutionError(
            f"image executable probe failed (exit {ec}): {output[:160]}"
        )
    line = lines[0].strip()
    if line == "__SUNABA_TOOL_ABSENT__":
        return None, None
    if not line.startswith("__SUNABA_TOOL_FOUND__"):
        raise EnvironmentResolutionError(
            f"image executable probe returned malformed output: {output[:160]}"
        )
    path = line.removeprefix("__SUNABA_TOOL_FOUND__")
    if not path.startswith("/"):
        raise EnvironmentResolutionError(
            f"image executable probe returned invalid path: {path[:160]}"
        )
    return path, _tool_version(container, root, path)


def resolve_uv_tool(
    container: Any, root: str, tool: str
) -> tuple[str | None, str | None, str | None]:
    """Return executable, source, and version for Ruff or Pyright.

    Project executables take priority. Image fallback is permitted only after
    command-v explicitly reports absence; probe and execution errors raise.
    """
    if tool not in {"ruff", "pyright"}:
        raise ValueError(f"unsupported uv tool: {tool}")
    root = root.rstrip("/")
    project_path = f"{root}/.venv/bin/{tool}"
    if _test_executable(container, root, project_path):
        return project_path, "project", _tool_version(container, root, project_path)

    image_path, version = _resolve_image_tool(container, root, tool)
    if image_path is None:
        return None, None, None
    return image_path, "image", version


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
            # pytest must run through the prepared project interpreter.
            result[tool] = {"path": None, "source": None, "version": None}
            continue

        image_path, version = _resolve_image_tool(container, root, tool)
        result[tool] = {
            "path": image_path,
            "source": "image" if image_path else None,
            "version": version,
        }
    return result
