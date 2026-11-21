# State is deliberately kept outside the container because an agent inside
# the container can rewrite it.

"""Host-side persistence and fingerprint staleness detection for uv environments (Issue #967).

State is deliberately kept outside the container because an agent inside
the container can rewrite it.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import os
import posixpath
import re
import shlex
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import platformdirs
import tomllib

logger: logging.Logger = logging.getLogger(__name__)
MANIFEST_FILES: tuple[str, ...] = ("pyproject.toml", "uv.lock", ".python-version")
MANIFEST_ABSENT_MARKER: bytes = b"__SUNABA_MANIFEST_ABSENT__\n"
_DEFAULT_UV_METADATA_TIMEOUT: float = 15.0
_UV_METADATA_MARKER_ENV: str = "SUNABA_UV_METADATA_MARKER"
_PROC_BOOT_ID_PATH: str = "/proc/sys/kernel/random/boot_id"
_PROC_TIMENS_OFFSETS_PATH: str = "/proc/self/timens_offsets"
_PROC_TIME_NS_PATH: str = "/proc/self/ns/time"
_PROC_TIME_FOR_CHILDREN_NS_PATH: str = "/proc/self/ns/time_for_children"
_BOOT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


class FingerprintProbeError(RuntimeError):
    """Raised when reading a manifest file from the container fails."""


def get_environment_state_dir() -> Path:
    """Return the host directory for persisted container environment state."""
    override = os.environ.get("SUNABA_ENVIRONMENT_STATE_DIR")
    if override and override.strip():
        return Path(override.strip())
    return Path(platformdirs.user_state_dir("sunaba"))


def _uv_metadata_timeout() -> float:
    """Return the wall-clock bound (seconds) for ``uv workspace metadata``."""
    raw = os.environ.get("SUNABA_UV_METADATA_TIMEOUT", "").strip()
    if raw:
        try:
            value = float(raw)
            if math.isfinite(value) and value > 0:
                return value
        except ValueError:
            pass
    try:
        from sunaba.tools.common import RECOVERY_DOCKER_TIMEOUT

        fallback = float(RECOVERY_DOCKER_TIMEOUT)
        if math.isfinite(fallback) and fallback > 0:
            return fallback
    except Exception:
        pass
    return _DEFAULT_UV_METADATA_TIMEOUT


def _exec_run_accepts_environment(container: Any) -> bool:
    """Return True when ``container.exec_run`` can accept an ``environment`` dict."""
    exec_fn = getattr(container, "exec_run", None)
    if not callable(exec_fn):
        return False
    try:
        sig = inspect.signature(exec_fn)
    except (TypeError, ValueError):
        return False
    return any(
        param.kind == inspect.Parameter.VAR_KEYWORD or param.name == "environment"
        for param in sig.parameters.values()
    )


def _unpack_exec_result(res: Any) -> tuple[int, bytes, bytes]:
    """Normalize a ``container.exec_run`` return value to ``(ec, stdout, stderr)``."""
    if isinstance(res, tuple):
        ec = res[0]
        output = res[1]
    else:
        ec = getattr(res, "exit_code", 0)
        output = getattr(res, "output", b"")

    if isinstance(output, tuple):
        stdout, stderr = output
    else:
        stdout, stderr = output, b""

    return int(ec), stdout or b"", stderr or b""


def _exec_container_script(
    container: Any,
    root: str,
    script: str,
    *,
    label: str,
) -> tuple[int, bytes, bytes]:
    """Run *script* at *root* in *container*, aligned with active deadline scopes."""
    root_str = root.rstrip("/") or "/"
    full_script = f"cd {shlex.quote(root_str)} && {script}"
    exec_kwargs: dict[str, Any] = {
        "stdout": True,
        "stderr": True,
        "demux": True,
    }
    deadline_runner: Callable[[Callable[[], Any]], Any] | None = None
    tree_timeout_types: tuple[type[BaseException], ...] = ()
    try:
        from sunaba.edit_verify.shell import (
            ForegroundTreeTimeout,
            VerifyTreeTimeout,
            foreground_marker_environment,
            run_exec_under_foreground_deadline,
            run_exec_under_verify_deadline,
            verify_marker_environment,
        )

        tree_timeout_types = (ForegroundTreeTimeout, VerifyTreeTimeout)
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
        return container.exec_run(["/bin/sh", "-c", full_script], **exec_kwargs)

    try:
        res = deadline_runner(_invoke_exec) if deadline_runner is not None else _invoke_exec()
    except FingerprintProbeError:
        raise
    except Exception as exc:
        if tree_timeout_types and isinstance(exc, tree_timeout_types):
            raise
        raise FingerprintProbeError(f"container exec failed for {label}: {exc}") from exc

    return _unpack_exec_result(res)


def _host_boot_id() -> str:
    """Return the host Linux kernel boot UUID from ``/proc/sys/kernel/random/boot_id``."""
    try:
        raw = Path(_PROC_BOOT_ID_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            f"host boot_id unavailable ({exc})"
        ) from exc
    boot_id = raw.strip()
    if not _BOOT_ID_RE.fullmatch(boot_id):
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            "host boot_id is malformed"
        )
    return boot_id


def _host_timens_boottime_offset() -> tuple[int, int]:
    """Return the host ``boottime`` time-namespace offset ``(sec, nsec)``."""
    try:
        raw = Path(_PROC_TIMENS_OFFSETS_PATH).read_text(encoding="utf-8")
    except OSError as exc:
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            f"host timens_offsets unavailable ({exc})"
        ) from exc

    try:
        time_ns = os.readlink(_PROC_TIME_NS_PATH)
        children_time_ns = os.readlink(_PROC_TIME_FOR_CHILDREN_NS_PATH)
    except OSError as exc:
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            f"host time namespace unavailable ({exc})"
        ) from exc
    if time_ns != children_time_ns:
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            "host process has unapplied child time namespace offsets"
        )

    matches: list[tuple[int, int]] = []
    for line in raw.splitlines():
        parts = line.split()
        if not parts or parts[0] != "boottime":
            continue
        if len(parts) != 3:
            raise FingerprintProbeError(
                "cannot guarantee bounded startup eligibility for uv workspace metadata: "
                "host timens_offsets boottime entry is malformed"
            )
        sec_str, nsec_str = parts[1], parts[2]
        if not re.fullmatch(r"-?[0-9]+", sec_str) or not re.fullmatch(r"[0-9]+", nsec_str):
            raise FingerprintProbeError(
                "cannot guarantee bounded startup eligibility for uv workspace metadata: "
                "host timens_offsets boottime entry is non-numeric"
            )
        sec = int(sec_str)
        nsec = int(nsec_str)
        if not (0 <= nsec < 1_000_000_000):
            raise FingerprintProbeError(
                "cannot guarantee bounded startup eligibility for uv workspace metadata: "
                "host timens_offsets boottime nsec out of range"
            )
        matches.append((sec, nsec))

    if len(matches) != 1:
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            "host timens_offsets missing unique boottime entry"
        )
    return matches[0]


def _host_boottime_ns() -> int:
    """Return host ``CLOCK_BOOTTIME`` in nanoseconds for container ``/proc/uptime`` alignment."""
    clock_boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if clock_boottime is None:
        raise FingerprintProbeError(
            "cannot guarantee bounded startup eligibility for uv workspace metadata: "
            "host CLOCK_BOOTTIME is unavailable"
        )
    try:
        return time.clock_gettime_ns(clock_boottime)
    except OSError as exc:
        raise FingerprintProbeError(
            f"cannot guarantee bounded startup eligibility for uv workspace metadata: {exc}"
        ) from exc


def _compute_boottime_cutoff_cs(effective_deadline_at: float) -> tuple[int, int]:
    """Compute ``(issued_cs, cutoff_cs)`` in centiseconds matching Linux ``/proc/uptime``."""
    boot_now_ns = _host_boottime_ns()
    mono_now = time.monotonic()
    remaining_s = max(0.0, effective_deadline_at - mono_now)
    remaining_ns = int(remaining_s * 1_000_000_000)
    issued_cs = boot_now_ns // 10_000_000
    cutoff_cs = (boot_now_ns + remaining_ns) // 10_000_000
    return issued_cs, cutoff_cs


def _compute_boottime_clock_domain_window(
    effective_deadline_at: float,
) -> tuple[str, int, int, int, int]:
    """Capture host clock-domain proof and ``[issued_cs, cutoff_cs)`` cutoff window."""
    boot_id = _host_boot_id()
    boottime_offset_sec, boottime_offset_nsec = _host_timens_boottime_offset()
    issued_cs, cutoff_cs = _compute_boottime_cutoff_cs(effective_deadline_at)
    return boot_id, boottime_offset_sec, boottime_offset_nsec, issued_cs, cutoff_cs


def _build_metadata_probe_script(
    root: str,
    marker_env_name: str,
    *,
    boot_id: str,
    boottime_offset_sec: int,
    boottime_offset_nsec: int,
    issued_cs: int,
    cutoff_cs: int,
) -> str:
    """Build the shell script enforcing clock-domain alignment and bounded execution."""
    root_str = root.rstrip("/") or "/"
    script = (
        "unset VIRTUAL_ENV; "
        "_sunaba_boot_id=''; "
        "_sunaba_boot_extra=''; "
        "_sunaba_boot_lines=0; "
        "if [ -f /proc/sys/kernel/random/boot_id ] && [ -r /proc/sys/kernel/random/boot_id ]; then "
        "{ "
        "if IFS= read -r _sunaba_boot_id || [ -n \"$_sunaba_boot_id\" ]; then "
        "_sunaba_boot_lines=1; "
        "fi; "
        "if IFS= read -r _sunaba_boot_extra || [ -n \"$_sunaba_boot_extra\" ]; then "
        "_sunaba_boot_lines=2; "
        "fi; "
        "} < /proc/sys/kernel/random/boot_id; "
        "fi; "
        'if [ "$_sunaba_boot_lines" -ne 1 ] || [ -z "$_sunaba_boot_id" ]; then '
        "echo 'uv workspace metadata boot_id unavailable' >&2; exit 125; "
        "fi; "
        f'if [ "$_sunaba_boot_id" != {shlex.quote(boot_id)} ]; then '
        "echo 'uv workspace metadata boot_id mismatch (distinct clock domain)' >&2; exit 125; "
        "fi; "
        "_sunaba_bt_sec=''; "
        "_sunaba_bt_nsec=''; "
        "_sunaba_bt_count=0; "
        "_sunaba_bt_bad=0; "
        "if [ -f /proc/self/timens_offsets ] && [ -r /proc/self/timens_offsets ]; then "
        "while IFS=' \t' read -r _sunaba_clk _sunaba_s _sunaba_ns _sunaba_rest "
        '|| [ -n "$_sunaba_clk" ]; do '
        'case "$_sunaba_clk" in '
        "boottime) "
        "_sunaba_bt_count=$((_sunaba_bt_count + 1)); "
        'if [ -n "$_sunaba_rest" ] || [ -z "$_sunaba_s" ] || [ -z "$_sunaba_ns" ]; then '
        "_sunaba_bt_bad=1; "
        "else "
        '_sunaba_bt_sec="$_sunaba_s"; '
        '_sunaba_bt_nsec="$_sunaba_ns"; '
        "fi ;; "
        "esac; "
        "done < /proc/self/timens_offsets; "
        "fi; "
        'if [ "$_sunaba_bt_count" -ne 1 ] || [ "$_sunaba_bt_bad" -ne 0 ]; then '
        "echo 'uv workspace metadata timens_offsets unavailable' >&2; exit 125; "
        "fi; "
        "_sunaba_s_sign=''; "
        'case "$_sunaba_bt_sec" in '
        "-*) _sunaba_s_sign='-'; _sunaba_bt_sec=${_sunaba_bt_sec#-} ;; "
        "esac; "
        'case "$_sunaba_bt_sec" in '
        "''|*[!0-9]*) _sunaba_bt_sec='' ;; "
        "*) "
        '_sunaba_bt_sec=${_sunaba_bt_sec#"${_sunaba_bt_sec%%[!0]*}"}; '
        ': "${_sunaba_bt_sec:=0}"; '
        'if [ "$_sunaba_s_sign" = \'-\' ] && [ "$_sunaba_bt_sec" != \'0\' ]; then '
        '_sunaba_bt_sec="-$_sunaba_bt_sec"; '
        "fi ;; "
        "esac; "
        'case "$_sunaba_bt_nsec" in '
        "''|*[!0-9]*) _sunaba_bt_nsec='' ;; "
        "*) "
        '_sunaba_bt_nsec=${_sunaba_bt_nsec#"${_sunaba_bt_nsec%%[!0]*}"}; '
        ': "${_sunaba_bt_nsec:=0}"; '
        'case "$_sunaba_bt_nsec" in '
        "??????????*) _sunaba_bt_nsec='' ;; "
        "esac ;; "
        "esac; "
        'if [ -z "$_sunaba_bt_sec" ] || [ -z "$_sunaba_bt_nsec" ]; then '
        "echo 'uv workspace metadata timens_offsets unavailable' >&2; exit 125; "
        "fi; "
        f'if [ "$_sunaba_bt_sec" != {shlex.quote(str(boottime_offset_sec))} ] '
        f'|| [ "$_sunaba_bt_nsec" != {shlex.quote(str(boottime_offset_nsec))} ]; then '
        "echo 'uv workspace metadata time-namespace boottime offset mismatch' >&2; exit 125; "
        "fi; "
        "_sunaba_now_cs=''; "
        "if [ -f /proc/uptime ] && [ -r /proc/uptime ] "
        "&& IFS=' ' read -r _sunaba_up _ < /proc/uptime; then "
        "_sunaba_sec=${_sunaba_up%%.*}; "
        "_sunaba_frac=${_sunaba_up#*.}; "
        'if [ -n "$_sunaba_sec" ] && [ -n "$_sunaba_frac" ] && [ "$_sunaba_sec" != "$_sunaba_up" ]; then '
        '_sunaba_sec=${_sunaba_sec#"${_sunaba_sec%%[!0]*}"}; '
        ': "${_sunaba_sec:=0}"; '
        'case "$_sunaba_frac" in '
        '?) _sunaba_frac="${_sunaba_frac}0" ;; '
        '??*) _sunaba_frac=${_sunaba_frac%"${_sunaba_frac#??}"} ;; '
        "esac; "
        "_sunaba_frac=${_sunaba_frac#0}; "
        ': "${_sunaba_frac:=0}"; '
        'case "$_sunaba_sec$_sunaba_frac" in '
        "''|*[!0-9]*) ;; "
        "*) _sunaba_now_cs=$((_sunaba_sec * 100 + _sunaba_frac)) ;; "
        "esac; "
        "fi; "
        "fi; "
        'if [ -z "$_sunaba_now_cs" ]; then '
        "echo 'uv workspace metadata startup eligibility unavailable' >&2; exit 125; "
        "fi; "
        f'if [ "$_sunaba_now_cs" -lt {issued_cs} ]; then '
        "echo 'uv workspace metadata clock mismatch before startup' >&2; exit 125; "
        "fi; "
        f'if [ "$_sunaba_now_cs" -ge {cutoff_cs} ]; then '
        "echo 'uv workspace metadata deadline expired before startup' >&2; exit 124; "
        "fi; "
        f'if [ -n "${{{marker_env_name}:-}}" ] && [ -r /proc/$$/environ ]; then '
        "uv workspace metadata --frozen --offline; "
        "elif command -v timeout >/dev/null 2>&1; then "
        f"_sunaba_rem_cs=$(({cutoff_cs} - _sunaba_now_cs)); "
        "_sunaba_rem_s=$((_sunaba_rem_cs / 100)); "
        "_sunaba_rem_f=$((_sunaba_rem_cs % 100)); "
        '[ "$_sunaba_rem_f" -lt 10 ] && _sunaba_rem_f="0$_sunaba_rem_f"; '
        'timeout "${_sunaba_rem_s}.${_sunaba_rem_f}" uv workspace metadata --frozen --offline; '
        "else "
        "echo 'uv workspace metadata bounded cleanup unavailable' >&2; exit 125; "
        "fi"
    )
    return f"cd {shlex.quote(root_str)} && {script}"


def _run_bounded_metadata_exec(
    container: Any,
    root: str,
    *,
    bound: float,
) -> tuple[int, bytes, bytes]:
    """Run ``uv workspace metadata`` under a guaranteed bounded exec and reaper."""
    if (
        not isinstance(bound, (int, float))
        or isinstance(bound, bool)
        or not math.isfinite(bound)
        or bound <= 0
    ):
        raise FingerprintProbeError(
            f"uv workspace metadata requires a positive finite timeout bound, got {bound!r}"
        )

    if not _exec_run_accepts_environment(container):
        raise FingerprintProbeError(
            "cannot guarantee bounded cleanup for uv workspace metadata: "
            "container exec_run does not accept environment markers"
        )

    try:
        from sunaba.edit_verify.shell import (
            ForegroundTreeTimeout,
            TreeTimeout,
            VerifyTreeTimeout,
            _active_deadline_scope,
            _active_foreground_scope,
            _DeadlineScope,
            _watchdog,
            foreground_marker_environment,
            verify_marker_environment,
        )
    except Exception as exc:
        raise FingerprintProbeError(
            f"cannot guarantee bounded cleanup for uv workspace metadata: {exc}"
        ) from exc

    class _MetadataTreeTimeout(TreeTimeout):
        """Internal timeout raised when the metadata probe deadline expires."""

        _label = "uv workspace metadata"

    fg_scope = _active_foreground_scope()
    verify_scope = _active_deadline_scope()
    outer_scope = fg_scope if fg_scope is not None else verify_scope
    outer_env = (
        foreground_marker_environment()
        if fg_scope is not None
        else verify_marker_environment()
    )

    # If an active outer scope expires at or before the metadata bound, run directly
    # under that outer scope so its watchdog reaps the tree and surfaces the actual
    # outer timeout/reap verdict.
    if outer_scope is not None and (outer_scope.deadline_at - time.monotonic()) <= bound:
        if not outer_env:
            raise FingerprintProbeError(
                "cannot guarantee bounded cleanup for uv workspace metadata: "
                "outer deadline scope has no marker environment"
            )
        marker_env_name = next(iter(outer_env))
        boot_id, off_sec, off_nsec, issued_cs, cutoff_cs = (
            _compute_boottime_clock_domain_window(outer_scope.deadline_at)
        )
        full_script = _build_metadata_probe_script(
            root,
            marker_env_name,
            boot_id=boot_id,
            boottime_offset_sec=off_sec,
            boottime_offset_nsec=off_nsec,
            issued_cs=issued_cs,
            cutoff_cs=cutoff_cs,
        )
        exec_kwargs: dict[str, Any] = {
            "stdout": True,
            "stderr": True,
            "demux": True,
            "environment": dict(outer_env),
        }

        def _invoke_outer_exec() -> Any:
            if outer_scope.cancelled or outer_scope.expired():
                raise FingerprintProbeError(
                    "uv workspace metadata deadline expired before container exec launch"
                )
            return container.exec_run(["/bin/sh", "-c", full_script], **exec_kwargs)

        try:
            res = outer_scope.run_exec(_invoke_outer_exec)
        except (ForegroundTreeTimeout, VerifyTreeTimeout):
            raise
        except Exception as exc:
            raise FingerprintProbeError(
                f"container exec failed for uv workspace metadata: {exc}"
            ) from exc
        return _unpack_exec_result(res)

    metadata_marker = f"sunabamd{uuid.uuid4().hex[:12]}"
    exec_env = dict(outer_env or {})
    exec_env[_UV_METADATA_MARKER_ENV] = metadata_marker
    exec_kwargs = {
        "stdout": True,
        "stderr": True,
        "demux": True,
        "environment": exec_env,
    }

    metadata_scope = _DeadlineScope(
        container, bound, metadata_marker, _MetadataTreeTimeout
    )
    effective_deadline_at = (
        min(metadata_scope.deadline_at, outer_scope.deadline_at)
        if outer_scope is not None
        else metadata_scope.deadline_at
    )
    try:
        boot_id, off_sec, off_nsec, issued_cs, cutoff_cs = (
            _compute_boottime_clock_domain_window(effective_deadline_at)
        )
    except Exception:
        metadata_scope.cancelled = True
        raise
    full_script = _build_metadata_probe_script(
        root,
        _UV_METADATA_MARKER_ENV,
        boot_id=boot_id,
        boottime_offset_sec=off_sec,
        boottime_offset_nsec=off_nsec,
        issued_cs=issued_cs,
        cutoff_cs=cutoff_cs,
    )
    threading.Thread(
        target=_watchdog,
        args=(metadata_scope,),
        daemon=True,
        name="uv-metadata-deadline-watchdog",
    ).start()

    def _invoke_metadata_exec() -> Any:
        if (
            metadata_scope.cancelled
            or metadata_scope.expired()
            or (outer_scope is not None and (outer_scope.cancelled or outer_scope.expired()))
        ):
            raise FingerprintProbeError(
                "uv workspace metadata deadline expired before container exec launch"
            )
        return container.exec_run(["/bin/sh", "-c", full_script], **exec_kwargs)

    def _run_under_metadata_scope() -> Any:
        return metadata_scope.run_exec(_invoke_metadata_exec)

    try:
        res = (
            outer_scope.run_exec(_run_under_metadata_scope)
            if outer_scope is not None
            else _run_under_metadata_scope()
        )
    except (ForegroundTreeTimeout, VerifyTreeTimeout):
        raise
    except _MetadataTreeTimeout as exc:
        raise FingerprintProbeError(
            f"uv workspace metadata timed out after {bound:g}s (reap={exc.reap})"
        ) from exc
    except FingerprintProbeError:
        raise
    except Exception as exc:
        raise FingerprintProbeError(
            f"container exec failed for uv workspace metadata: {exc}"
        ) from exc
    finally:
        metadata_scope.cancelled = True

    return _unpack_exec_result(res)



def _read_container_file(container: Any, root: str, filename: str) -> bytes | None:
    """Read bytes of *filename* from *root* in container, or None if absent.

    Raises FingerprintProbeError if probe fails.
    """
    script = (
        f"if [ ! -e {shlex.quote(filename)} ]; then "
        f"printf '__SUNABA_MANIFEST_ABSENT__\\n'; "
        f"elif [ ! -f {shlex.quote(filename)} ]; then "
        f"exit 2; "
        f"else cat {shlex.quote(filename)}; fi"
    )
    ec, stdout_bytes, stderr_bytes = _exec_container_script(
        container, root, script, label=filename
    )

    if ec != 0:
        err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise FingerprintProbeError(
            f"probe for {filename} failed (exit {ec}): {err_msg[:160]}"
        )

    if stdout_bytes == MANIFEST_ABSENT_MARKER:
        return None

    return stdout_bytes


def _is_explicit_uv_workspace(pyproject_bytes: bytes) -> bool:
    """Return True when root pyproject.toml explicitly declares ``[tool.uv.workspace]``."""
    try:
        data = tomllib.loads(pyproject_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise FingerprintProbeError(f"invalid root pyproject.toml: {exc}") from exc

    tool = data.get("tool")
    if not isinstance(tool, dict):
        return False
    uv_cfg = tool.get("uv")
    if not isinstance(uv_cfg, dict):
        return False
    if "workspace" not in uv_cfg:
        return False
    if not isinstance(uv_cfg["workspace"], dict):
        raise FingerprintProbeError("invalid [tool.uv.workspace] table in pyproject.toml")
    return True


def _validate_workspace_metadata(payload: Any, root: str) -> list[str]:
    """Validate ``uv workspace metadata`` JSON and return sorted member manifest paths."""
    if not isinstance(payload, dict):
        raise FingerprintProbeError("uv workspace metadata returned non-object JSON")

    schema = payload.get("schema")
    if not isinstance(schema, dict):
        raise FingerprintProbeError("uv workspace metadata missing schema object")
    schema_version = schema.get("version")
    if not isinstance(schema_version, str) or not schema_version.strip():
        raise FingerprintProbeError("uv workspace metadata missing schema.version")

    norm_root = posixpath.normpath(root.rstrip("/") or "/")

    ws_root = payload.get("workspace_root")
    if (
        not isinstance(ws_root, str)
        or not ws_root
        or "\x00" in ws_root
        or not posixpath.isabs(ws_root)
    ):
        raise FingerprintProbeError(
            f"uv workspace metadata returned invalid workspace_root: {ws_root!r}"
        )
    if posixpath.normpath(ws_root) != norm_root:
        raise FingerprintProbeError(
            f"uv workspace metadata workspace_root mismatch: expected {norm_root}, got {ws_root}"
        )

    workspace_obj = payload.get("workspace")
    if not isinstance(workspace_obj, dict):
        raise FingerprintProbeError("uv workspace metadata missing workspace object")
    ws_obj_path = workspace_obj.get("path")
    if (
        not isinstance(ws_obj_path, str)
        or not ws_obj_path
        or "\x00" in ws_obj_path
        or not posixpath.isabs(ws_obj_path)
    ):
        raise FingerprintProbeError(
            f"uv workspace metadata returned invalid workspace.path: {ws_obj_path!r}"
        )
    if posixpath.normpath(ws_obj_path) != norm_root:
        raise FingerprintProbeError(
            f"uv workspace metadata workspace.path mismatch: expected {norm_root}, got {ws_obj_path}"
        )

    # uv omits "members" on an empty virtual workspace (no [project] and no members).
    raw_members = payload.get("members", [])
    if not isinstance(raw_members, list):
        raise FingerprintProbeError("uv workspace metadata members field is not a list")

    member_manifests: set[str] = set()
    for entry in raw_members:
        if not isinstance(entry, dict):
            raise FingerprintProbeError("uv workspace metadata member entry is not an object")
        member_path = entry.get("path")
        if (
            not isinstance(member_path, str)
            or not member_path
            or "\x00" in member_path
            or not posixpath.isabs(member_path)
        ):
            raise FingerprintProbeError(
                f"uv workspace metadata invalid member path: {member_path!r}"
            )
        norm_member = posixpath.normpath(member_path)
        rel_dir = posixpath.relpath(norm_member, norm_root)
        if rel_dir == ".":
            continue
        if rel_dir == ".." or rel_dir.startswith("../") or posixpath.isabs(rel_dir):
            raise FingerprintProbeError(
                f"uv workspace member path {member_path!r} escapes root {norm_root!r}"
            )
        rel_manifest = posixpath.normpath(posixpath.join(rel_dir, "pyproject.toml"))
        if (
            rel_manifest == "pyproject.toml"
            or rel_manifest == ".."
            or rel_manifest.startswith("../")
            or posixpath.isabs(rel_manifest)
        ):
            raise FingerprintProbeError(
                f"uv workspace member manifest path {rel_manifest!r} invalid for root {norm_root!r}"
            )
        member_manifests.add(rel_manifest)

    return sorted(member_manifests)


def _enumerate_workspace_members(container: Any, root: str) -> list[str]:
    """Run ``uv workspace metadata --frozen --offline`` at *root* and validate members."""
    bound = _uv_metadata_timeout()
    ec, stdout_bytes, stderr_bytes = _run_bounded_metadata_exec(
        container,
        root,
        bound=bound,
    )
    if ec == 124:
        raise FingerprintProbeError(
            f"uv workspace metadata timed out after {bound:g}s (exit 124)"
        )
    if ec != 0:
        detail = (
            (stderr_bytes or stdout_bytes)
            .decode("utf-8", errors="replace")
            .strip()
        )
        raise FingerprintProbeError(
            f"uv workspace metadata failed (exit {ec}): {detail[:200]}"
        )

    try:
        payload = json.loads(stdout_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FingerprintProbeError(
            f"uv workspace metadata returned invalid JSON: {exc}"
        ) from exc

    return _validate_workspace_metadata(payload, root)


def compute_manifest_fingerprint(container: Any, root: str) -> dict[str, Any]:
    """Compute content hashes for uv manifests, workspace members, and .python-version."""
    fingerprint: dict[str, Any] = {"root": root.rstrip("/")}
    files_map: dict[str, str] = {}
    pyproject_bytes: bytes | None = None
    for filename in MANIFEST_FILES:
        content = _read_container_file(container, root, filename)
        if filename == "pyproject.toml":
            pyproject_bytes = content
        if content is None:
            files_map[filename] = "absent"
        else:
            files_map[filename] = hashlib.sha256(content).hexdigest()
    fingerprint["files"] = files_map
    for filename, val in files_map.items():
        fingerprint[filename] = val

    if (
        pyproject_bytes is not None
        and files_map.get("uv.lock") != "absent"
        and _is_explicit_uv_workspace(pyproject_bytes)
    ):
        members_map: dict[str, str] = {}
        for rel_manifest in _enumerate_workspace_members(container, root):
            member_content = _read_container_file(container, root, rel_manifest)
            if member_content is None:
                members_map[rel_manifest] = "absent"
            else:
                members_map[rel_manifest] = hashlib.sha256(member_content).hexdigest()
        fingerprint["members"] = members_map

    return fingerprint


def check_manifest_staleness(
    container: Any,
    root: str,
    recorded_fingerprint: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """Compare current manifest fingerprint with recorded fingerprint.

    Returns (is_stale, list_of_changed_files).
    Raises FingerprintProbeError if reading from container fails.
    """
    if not recorded_fingerprint:
        return False, []

    current_fp = compute_manifest_fingerprint(container, root)

    changed: list[str] = []
    recorded_root = recorded_fingerprint.get("root")
    if recorded_root and recorded_root != root.rstrip("/"):
        changed.append(f"root ({recorded_root} -> {root.rstrip('/')})")

    recorded_files = recorded_fingerprint.get("files")
    if not isinstance(recorded_files, dict):
        recorded_files = {f: recorded_fingerprint.get(f, "absent") for f in MANIFEST_FILES}

    current_files = current_fp.get("files", {})

    for filename in MANIFEST_FILES:
        rec_val = recorded_files.get(filename, "absent")
        cur_val = current_files.get(filename, "absent")
        if rec_val != cur_val:
            changed.append(filename)

    recorded_members = recorded_fingerprint.get("members")
    current_members = current_fp.get("members")
    if isinstance(current_members, dict):
        if not isinstance(recorded_members, dict):
            changed.append("workspace members")
        else:
            for member_path in sorted(set(recorded_members) | set(current_members)):
                if recorded_members.get(member_path) != current_members.get(member_path):
                    changed.append(member_path)
    elif isinstance(recorded_members, dict):
        if recorded_members:
            changed.extend(sorted(recorded_members))
        else:
            changed.append("workspace members")

    return bool(changed), changed


def compare_recorded_fingerprint(
    container: Any,
    recorded_fingerprint: dict[str, Any] | None,
    project_root: str | None = None,
) -> tuple[bool, list[str]]:
    """Compare manifests only at the root captured during preparation.

    Caller workdirs do not define the prepared environment. The explicit
    recorded project_root takes precedence over the fingerprint's root.
    """
    if not recorded_fingerprint:
        return False, []
    root = project_root or recorded_fingerprint.get("root")
    if not root:
        return False, []
    return check_manifest_staleness(container, root, recorded_fingerprint)

def _find_state_file(state_dir: Path, container_id: str) -> Path | None:
    p_json = state_dir / f"{container_id}.json"
    if p_json.exists():
        return p_json
    p_raw = state_dir / container_id
    if p_raw.exists():
        return p_raw
    return None


def save_environment_states(
    container_id: str, records: dict[str, dict[str, Any]]
) -> None:
    """Atomically persist all preparation records for one container."""
    _write_environment_store(container_id, {"roots": records})


def _write_environment_store(container_id: str, state: dict[str, Any]) -> None:
    temp_path: str | None = None
    try:
        state_dir = get_environment_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        target_path = state_dir / f"{container_id}.json"
        data = dict(state)
        data["container_id"] = container_id
        payload = json.dumps(data, indent=2).encode("utf-8")
        fd, temp_path = tempfile.mkstemp(
            dir=state_dir, prefix=f".{container_id}.", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target_path)
    except Exception as exc:
        if temp_path is not None:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except OSError:
                pass
        logger.warning("Could not save environment state for %s: %s", container_id, exc)


def remove_environment_state(container_id: str) -> None:
    """Remove persisted environment state for container_id."""
    try:
        state_dir = get_environment_state_dir()
        if not state_dir.exists():
            return
        for path in (state_dir / f"{container_id}.json", state_dir / container_id):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "Could not remove environment state for %s at %s: %s",
                    container_id,
                    path,
                    exc,
                )
    except Exception as exc:
        logger.warning("Could not remove environment state for %s: %s", container_id, exc)


def load_environment_state(container_id: str) -> dict[str, Any] | None:
    """Load persisted environment state for exact full container_id."""
    state_dir = get_environment_state_dir()
    if not state_dir.exists():
        return None
    file_path = _find_state_file(state_dir, container_id)
    if file_path is None:
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"status": "not_prepared", "reason": "corrupt environment state store"}
        # Must match exact container_id, not a prefix
        stored_cid = data.get("container_id")
        if stored_cid is not None and stored_cid != container_id:
            return None
        return data
    except Exception:
        return {"status": "not_prepared", "reason": "corrupt environment state store"}


def prune_and_load_environment_states(client: Any = None) -> dict[str, dict[str, Any]]:
    """Clean up records for dead containers and load surviving records into memory."""
    state_dir = get_environment_state_dir()
    if not state_dir.exists():
        return {}

    existing_container_ids: set[str] | None = None
    if client is None:
        try:
            from sunaba.tools.common import _docker

            client = _docker()
        except Exception as exc:
            logger.warning("Could not obtain Docker client for startup pruning: %s", exc)
            client = None

    if client is not None:
        try:
            existing_container_ids = {c.id for c in client.containers.list(all=True)}
        except Exception as exc:
            logger.warning("Could not list containers for startup pruning: %s", exc)
            existing_container_ids = None

    loaded: dict[str, dict[str, Any]] = {}
    for p in list(state_dir.iterdir()):
        if p.name.startswith("."):
            continue
        if not p.is_file():
            continue
        cid = p.name[:-5] if p.name.endswith(".json") else p.name

        if existing_container_ids is not None and cid not in existing_container_ids:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
            continue

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                loaded[cid] = {"status": "not_prepared", "reason": "corrupt environment state store"}
            else:
                stored_cid = data.get("container_id", cid)
                if stored_cid != cid and existing_container_ids is not None and stored_cid not in existing_container_ids:
                    p.unlink(missing_ok=True)
                    continue
                loaded[stored_cid] = data
        except Exception:
            loaded[cid] = {"status": "not_prepared", "reason": "corrupt environment state store"}

    from sunaba.verify_state import (
        _environment_state,
        _environment_state_lock,
        _normalized_root,
    )

    with _environment_state_lock:
        for cid, value in loaded.items():
            if isinstance(value, dict) and isinstance(value.get("roots"), dict):
                records = {
                    _normalized_root(str(root)): dict(record)
                    for root, record in value["roots"].items()
                    if isinstance(record, dict)
                }
            else:
                record = dict(value)
                root = record.get("project_root")
                fingerprint = record.get("fingerprint")
                if not root and isinstance(fingerprint, dict):
                    root = fingerprint.get("root")
                records = {_normalized_root(root): record}
            _environment_state[cid] = records
            loaded[cid] = dict(next(reversed(records.values()))) if records else {
                "status": "not_prepared", "reason": "corrupt environment state store"
            }

    return loaded
