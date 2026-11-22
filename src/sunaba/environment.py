"""Root-only project environment detection and Python tool resolution."""

from __future__ import annotations

import os
import shlex
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any


class EnvironmentResolutionError(RuntimeError):
    """A container command or tool probe failed while resolving uv tools."""


def _exec_container(
    container: Any,
    script: str,
    *,
    error_prefix: str,
) -> tuple[int, str, str]:
    """Run *script* in *container*, propagating deadline timeouts."""
    from collections.abc import Callable

    exec_kwargs: dict[str, Any] = {
        "stdout": True,
        "stderr": True,
        "demux": True,
    }
    deadline_runner: Callable[[Callable[[], Any]], Any] | None = None
    tree_timeout_type: type[BaseException] | None = None
    try:
        from sunaba.edit_verify.shell import (
            TreeTimeout,
            foreground_marker_environment,
            run_exec_under_foreground_deadline,
            run_exec_under_verify_deadline,
            verify_marker_environment,
        )

        tree_timeout_type = TreeTimeout
        fg_env = foreground_marker_environment()
        if fg_env is not None:
            exec_kwargs["environment"] = fg_env
            deadline_runner = run_exec_under_foreground_deadline
        else:
            verify_env = verify_marker_environment()
            if verify_env is not None:
                exec_kwargs["environment"] = verify_env
                deadline_runner = run_exec_under_verify_deadline
    except Exception:
        pass

    def _invoke_exec() -> Any:
        return container.exec_run(["/bin/sh", "-c", script], **exec_kwargs)

    try:
        result = (
            deadline_runner(_invoke_exec)
            if deadline_runner is not None
            else _invoke_exec()
        )
    except Exception as exc:
        if tree_timeout_type is not None and isinstance(exc, tree_timeout_type):
            raise
        raise EnvironmentResolutionError(
            f"{error_prefix}: {str(exc)[:180]}"
        ) from exc

    if isinstance(result, tuple):
        ec, output = result
    else:
        ec = getattr(result, "exit_code", 0)
        output = getattr(result, "output", b"")
    stdout, stderr = output if isinstance(output, tuple) else (output, b"")
    out = stdout.decode("utf-8", errors="replace") if stdout else ""
    err = stderr.decode("utf-8", errors="replace") if stderr else ""
    return int(ec), out, err


def _run(container: Any, root: str, command: str) -> tuple[int, str]:
    ec, out, err = _exec_container(
        container,
        f"cd {shlex.quote(root)} && {command}",
        error_prefix="container command failed",
    )
    return ec, (out + err).strip()[:240]


def _run_split(container: Any, root: str, command: str) -> tuple[int, str, str]:
    """Run a container command and return its stdout and stderr separately."""
    return _exec_container(
        container,
        f"cd {shlex.quote(root)} && {command}",
        error_prefix="container command failed",
    )


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


def _probe_uv_manifests(container: Any, root: str) -> set[str]:
    """Return present uv manifest filenames directly under *root*; raise on error."""
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
    return set(lines)


def probe_uv_project_root(container: Any, root: str) -> bool:
    """Reliably detect root uv manifests; raise when the probe itself fails."""
    return {"pyproject.toml", "uv.lock"} <= _probe_uv_manifests(container, root)


def probe_directory_exists(container: Any, root: str) -> bool:
    """Probe a root directory without changing the command working directory."""
    quoted_root = shlex.quote(root)
    script = (
        f"if [ -d {quoted_root} ]; then "
        "printf '__SUNABA_DIRECTORY_EXISTS__\\n'; "
        "else printf '__SUNABA_DIRECTORY_ABSENT__\\n'; fi"
    )
    exit_code, stdout_text, stderr_text = _exec_container(
        container, script, error_prefix="directory probe failed"
    )
    if exit_code != 0:
        detail = (stdout_text + stderr_text).strip()[:180]
        raise EnvironmentResolutionError(
            f"directory probe failed (exit {exit_code}): {detail}"
        )
    marker = stdout_text.strip()
    if marker == "__SUNABA_DIRECTORY_EXISTS__":
        return True
    if marker == "__SUNABA_DIRECTORY_ABSENT__":
        return False
    raise EnvironmentResolutionError(
        f"directory probe returned malformed output: {marker[:160]}"
    )


def _resolve_physical_paths(
    container: Any, paths: Sequence[str]
) -> dict[str, str | None]:
    """Resolve physical paths for *paths* in one container exec.

    Returns ``{normalized_path: resolved_physical_path_or_None}``.
    ``None`` means the path is genuinely absent inside the container; any
    other failure, malformed output, or incomplete output fails closed.
    """
    unique_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        normalized = os.path.normpath(raw_path)
        if normalized not in seen:
            seen.add(normalized)
            unique_paths.append(normalized)
    if not unique_paths:
        return {}

    chunks: list[str] = []
    for idx, path in enumerate(unique_paths):
        quoted = shlex.quote(path)
        chunks.append(
            f"_sunaba_p={quoted}; "
            "if [ -d \"$_sunaba_p\" ]; then "
            "if _sunaba_r=$(CDPATH= cd -P -- \"$_sunaba_p\" 2>/dev/null && pwd -P); then "
            f"printf '__SUNABA_PATH_OK__{idx}:%s\\n' \"$_sunaba_r\"; "
            f"else printf '__SUNABA_PATH_ERR__{idx}\\n'; fi; "
            "elif [ -e \"$_sunaba_p\" ]; then "
            f"printf '__SUNABA_PATH_ERR__{idx}\\n'; "
            "elif command -v ls >/dev/null 2>&1; then "
            "_sunaba_e=$(LC_ALL=C ls -Ld -- \"$_sunaba_p\" 2>&1); "
            "case \"$_sunaba_e\" in "
            "*\"No such file or directory\"*) "
            f"printf '__SUNABA_PATH_ABSENT__{idx}\\n' ;; "
            f"*) printf '__SUNABA_PATH_ERR__{idx}\\n' ;; "
            "esac; "
            "elif [ ! -L \"$_sunaba_p\" ]; then "
            f"printf '__SUNABA_PATH_ABSENT__{idx}\\n'; "
            "else "
            f"printf '__SUNABA_PATH_ERR__{idx}\\n'; "
            "fi"
        )
    ec, stdout_text, stderr_text = _exec_container(
        container,
        "; ".join(chunks),
        error_prefix="physical path probe failed",
    )
    if ec != 0:
        detail = (stdout_text + stderr_text).strip()[:180]
        raise EnvironmentResolutionError(
            f"physical path probe failed (exit {ec}): {detail}"
        )

    resolved: dict[str, str | None] = {}
    seen_indices: set[int] = set()
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("__SUNABA_PATH_ERR__"):
            raise EnvironmentResolutionError(
                f"physical path probe failed: {line[:160]}"
            )
        if line.startswith("__SUNABA_PATH_ABSENT__"):
            idx_str = line.removeprefix("__SUNABA_PATH_ABSENT__")
            if not idx_str.isdigit():
                raise EnvironmentResolutionError(
                    f"physical path probe returned malformed output: {line[:160]}"
                )
            idx = int(idx_str)
            if idx < 0 or idx >= len(unique_paths) or idx in seen_indices:
                raise EnvironmentResolutionError(
                    f"physical path probe returned malformed output: {line[:160]}"
                )
            seen_indices.add(idx)
            resolved[unique_paths[idx]] = None
            continue
        if line.startswith("__SUNABA_PATH_OK__"):
            payload = line.removeprefix("__SUNABA_PATH_OK__")
            if ":" not in payload:
                raise EnvironmentResolutionError(
                    f"physical path probe returned malformed output: {line[:160]}"
                )
            idx_str, phys = payload.split(":", 1)
            if not idx_str.isdigit() or not phys.startswith("/"):
                raise EnvironmentResolutionError(
                    f"physical path probe returned malformed output: {line[:160]}"
                )
            idx = int(idx_str)
            if idx < 0 or idx >= len(unique_paths) or idx in seen_indices:
                raise EnvironmentResolutionError(
                    f"physical path probe returned malformed output: {line[:160]}"
                )
            seen_indices.add(idx)
            resolved[unique_paths[idx]] = os.path.normpath(phys)
            continue
        raise EnvironmentResolutionError(
            f"physical path probe returned malformed output: {line[:160]}"
        )

    if len(seen_indices) != len(unique_paths):
        raise EnvironmentResolutionError(
            "physical path probe returned incomplete output "
            f"({len(seen_indices)}/{len(unique_paths)})"
        )
    return resolved


def roots_refer_to_same_project(
    container: Any, root_a: str, root_b: str
) -> bool:
    """Return True when *root_a* and *root_b* normalize or physically resolve identically."""
    norm_a = os.path.normpath(root_a)
    norm_b = os.path.normpath(root_b)
    if norm_a == norm_b:
        return True
    resolved = _resolve_physical_paths(container, [norm_a, norm_b])
    phys_a = resolved.get(norm_a)
    phys_b = resolved.get(norm_b)
    return phys_a is not None and phys_a == phys_b


def recorded_root_is_uv_project(container: Any, root: str) -> tuple[bool, str]:
    """Check whether a recorded root still exists and resolves to itself."""
    if not probe_directory_exists(container, root):
        return False, "directory no longer exists"
    resolved = resolve_uv_root(container, root)
    if resolved is None:
        if not _probe_uv_manifests(container, root):
            return False, "neither pyproject.toml nor uv.lock exists"
        return True, ""
    normalized_root = os.path.normpath(root)
    if not roots_refer_to_same_project(
        container, normalized_root, resolved
    ):
        return False, f"root now resolves to {os.path.normpath(resolved)}"
    return True, ""



def resolve_uv_root(container: Any, workdir: str) -> str | None:
    """Resolve the uv workspace or project root for *workdir*.

    Probe the workdir manifests and its ancestors in one command so queued
    container replies are not consumed by a separate discovery probe.
    """
    probe = (
        "for f in pyproject.toml uv.lock; do "
        "[ -f \"$f\" ] && printf '%s\\n' \"$f\" || true; done"
    )
    ancestor_walk = (
        'd=$(pwd -P); while [ "$d" != "/" ]; do '
        'd="${d%/*}"; [ -z "$d" ] && d="/"; '
        'if [ -f "$d/uv.lock" ]; then '
        "printf 'above:%s\\n' \"$d\"; break; fi; done"
    )
    ec, stdout, stderr = _run_split(
        container, workdir, probe + "; " + ancestor_walk
    )
    if ec != 0:
        detail = (stdout + stderr).strip()[:240]
        raise EnvironmentResolutionError(
            f"uv manifest probe failed (exit {ec}): {detail}"
        )

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in lines:
        if line in {"pyproject.toml", "uv.lock"}:
            continue
        if line.startswith("above:") and os.path.isabs(line.removeprefix("above:")):
            continue
        raise EnvironmentResolutionError(
            f"uv manifest probe returned malformed output: {line[:160]}"
        )

    manifests = set(lines)
    has_workdir_project = "pyproject.toml" in manifests
    if has_workdir_project and "uv.lock" in manifests:
        return os.path.normpath(workdir)
    if not any(line.startswith("above:") for line in lines):
        return None

    ec, stdout, stderr = _run_split(
        container, workdir, "unset VIRTUAL_ENV; uv workspace dir"
    )
    if ec != 0:
        detail = (stdout + stderr).strip()[:240]
        if (
            "No `pyproject.toml` found" in stderr + stdout
            or "No `project` table found" in stderr + stdout
        ):
            return None
        raise EnvironmentResolutionError(
            f"uv workspace dir failed (exit {ec}): {detail}"
        )
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise EnvironmentResolutionError(
            f"uv workspace dir returned empty output: {stdout[:240]}"
        )
    candidate = lines[-1]
    if not candidate.startswith("/"):
        raise EnvironmentResolutionError(
            f"uv workspace dir returned non-absolute path: {candidate}"
        )
    if probe_uv_project_root(container, candidate):
        return os.path.normpath(candidate)
    return None


def is_uv_project_root(container: Any, root: str) -> bool:
    """Return true only when both uv manifests exist directly under *root*.

    Probe failures are raised instead of being mistaken for a non-uv project.
    """
    return probe_uv_project_root(container, root)


def check_recorded_environment_staleness(
    container: Any,
    state: dict[str, Any],
    caller_root: str | None = None,
) -> tuple[str, str | None]:
    """Return the recorded environment's status after its single stale check.

    Fingerprints are always read from the prepared project root. The caller's
    workdir is used only for legacy ready records that have no fingerprint.
    """
    from sunaba.edit_verify.shell import TreeTimeout
    from sunaba.verify_state import invalidate_verify_success

    container_id = str(getattr(container, "id", ""))
    status = state.get("status") or "not_prepared"
    if status != "ready":
        return status, state.get("reason") or "preparation state missing or not_prepared"

    recorded_fp = state.get("fingerprint")
    if not recorded_fp:
        explicit_root = state.get("project_root")
        project_root = explicit_root or caller_root
        if project_root:
            try:
                if resolve_uv_root(container, project_root) is None:
                    if explicit_root:
                        manifests = _probe_uv_manifests(container, explicit_root)
                        if "pyproject.toml" in manifests and "uv.lock" not in manifests:
                            invalidate_verify_success(container_id)
                            return (
                                "stale",
                                "uv.lock missing; run sandbox_prepare_environment or recreate container to recover",
                            )
                    return "not_uv", None
            except TreeTimeout:
                invalidate_verify_success(container_id)
                raise
            except Exception as exc:
                invalidate_verify_success(container_id)
                return "failed", f"uv manifest probe failed: {str(exc)[:200]}"
        return "ready", None

    from sunaba.environment_state_store import (
        FingerprintProbeError,
        compare_recorded_fingerprint,
    )

    project_root = state.get("project_root") or recorded_fp.get("root")
    try:
        is_stale, changed = compare_recorded_fingerprint(
            container, recorded_fp, project_root
        )
    except TreeTimeout:
        invalidate_verify_success(container_id)
        raise
    except FingerprintProbeError as exc:
        invalidate_verify_success(container_id)
        return "failed", f"fingerprint probe failed: {exc}"
    except Exception as exc:
        invalidate_verify_success(container_id)
        return "failed", f"fingerprint probe failed: {str(exc)[:180]}"

    if is_stale:
        invalidate_verify_success(container_id)
        return (
            "stale",
            f"{', '.join(changed)} changed; run sandbox_prepare_environment or recreate container to recover",
        )
    return "ready", None


def _deepest_recorded_ancestor(
    workdir: str, records: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]] | None:
    """Return the deepest recorded root that is a strict path-component ancestor."""
    normalized_workdir = os.path.normpath(workdir)
    workdir_parts = PurePosixPath(normalized_workdir).parts
    best_depth = -1
    best: tuple[str, dict[str, Any]] | None = None
    for recorded_root, state in records.items():
        if not recorded_root:
            continue
        normalized_root = os.path.normpath(recorded_root)
        if normalized_root == normalized_workdir:
            continue
        root_parts = PurePosixPath(normalized_root).parts
        if (
            root_parts
            and len(root_parts) < len(workdir_parts)
            and workdir_parts[: len(root_parts)] == root_parts
            and len(root_parts) > best_depth
        ):
            best_depth = len(root_parts)
            best = (normalized_root, dict(state))
    return best


def select_recorded_environment_for_workdir(
    container: Any,
    workdir: str,
    records: dict[str, dict[str, Any]],
    uv_root: str | None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Select the applicable preparation record for *workdir*.

    Normal uv root resolution always takes precedence. When resolution yields
    ``None``, preserve an exact-root record first; otherwise inspect the deepest
    recorded path-component ancestor (comparing physical paths resolved inside
    the container while keeping persisted keys and fingerprint comparison scoped
    to the recorded root) and apply it only when that ancestor is no longer a
    locked uv root (e.g. ``uv.lock`` disappeared while ``pyproject.toml``
    remained).
    """
    normalized_records = {
        (os.path.normpath(root) if root else ""): state
        for root, state in records.items()
    }
    if not normalized_records:
        if uv_root is not None:
            return os.path.normpath(uv_root), None
        return None, None

    candidate_roots = [root for root in normalized_records if root]

    if uv_root is not None:
        normalized_uv_root = os.path.normpath(uv_root)
        state = normalized_records.get(normalized_uv_root)
        if state is not None:
            return normalized_uv_root, dict(state)
        if candidate_roots:
            physical = _resolve_physical_paths(
                container, [normalized_uv_root, *candidate_roots]
            )
            phys_uv_root = physical.get(normalized_uv_root)
            if phys_uv_root is not None:
                for candidate in candidate_roots:
                    if physical.get(candidate) == phys_uv_root:
                        return candidate, dict(normalized_records[candidate])
        if len(normalized_records) == 1 and "" in normalized_records:
            # A legacy record without a root can only be scoped from the caller.
            return normalized_uv_root, dict(normalized_records[""])
        return normalized_uv_root, None

    normalized_workdir = os.path.normpath(workdir)
    if normalized_workdir in normalized_records:
        return normalized_workdir, dict(normalized_records[normalized_workdir])

    if not candidate_roots:
        return None, None

    physical = _resolve_physical_paths(
        container, [normalized_workdir, *candidate_roots]
    )
    phys_workdir = physical.get(normalized_workdir)
    if phys_workdir is None:
        return None, None

    phys_records: dict[str, dict[str, Any]] = {}
    phys_to_original: dict[str, str] = {}
    for candidate in candidate_roots:
        phys_candidate = physical.get(candidate)
        if phys_candidate is None:
            continue
        if phys_candidate == phys_workdir:
            return candidate, dict(normalized_records[candidate])
        phys_records[phys_candidate] = normalized_records[candidate]
        phys_to_original[phys_candidate] = candidate

    if not phys_records:
        return None, None

    ancestor = _deepest_recorded_ancestor(phys_workdir, phys_records)
    if ancestor is None:
        return None, None

    phys_ancestor, ancestor_state = ancestor
    ancestor_root = phys_to_original[phys_ancestor]
    if probe_uv_project_root(container, ancestor_root):
        # A still-locked ancestor means workdir is a non-uv subdir or
        # non-member rather than a descendant of a root that lost uv.lock.
        return None, None
    return ancestor_root, ancestor_state


def check_uv_environment(container: Any, root: str) -> tuple[str, str | None]:
    """Check the preparation record for the uv root containing *root*."""
    from sunaba.edit_verify.shell import TreeTimeout
    from sunaba.verify_state import (
        invalidate_verify_success,
        list_environment_states,
    )

    container_id = str(getattr(container, "id", ""))
    try:
        uv_root = resolve_uv_root(container, root)
        records = list_environment_states(container_id)
        selected_root, state = select_recorded_environment_for_workdir(
            container, root, records, uv_root
        )
    except TreeTimeout:
        invalidate_verify_success(container_id)
        raise
    except Exception as exc:
        invalidate_verify_success(container_id)
        return "failed", f"uv root probe failed: {str(exc)[:200]}"
    if uv_root is None:
        if state is None:
            return "not_uv", None
        return check_recorded_environment_staleness(
            container, state, selected_root or root
        )
    if state is None and records:
        return (
            "not_prepared",
            f"uv root {uv_root} is not prepared; run "
            f'sandbox_prepare_environment(container_id, working_dir="{uv_root}")',
        )
    if state is None:
        return "not_prepared", "preparation state missing"
    return check_recorded_environment_staleness(container, state, selected_root or uv_root)


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
