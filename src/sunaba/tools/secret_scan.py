"""Secret scan via detect-secrets and override tool (issue #676).

Public symbols
--------------
- ``run_secret_scan(container, files, working_dir)`` — run detect-secrets
  on files via ``container.exec_run``.
- ``secret_scan_override(container_id, ...)`` — MCP tool to bypass findings.
- ``check_override(container_id)`` — check/consume one-time override flag.

Design
------
The scanner is Yelp's ``detect-secrets`` (Apache-2.0), baked into the base
image.  It is never vendored so sunaba stays MIT.

Scan scope is the *manifest diff* / *commit* files only — never the whole
tree.  This keeps the scan cheap and avoids resurfacing approved secrets.

Missing scanner: **proceed, but flag prominently**.  The override is a
separate MCP tool, NOT a ``publish`` argument.

Baseline (``SUNABA_SECRETS_BASELINE``, default ``true``): when enabled
a repo ``.secrets.baseline`` file suppresses known findings.

Override semantics
------------------
- Baseline ON  → append to ``.secrets.baseline`` (permanent for this repo).
- Baseline OFF → one-time in-memory flag for this container + HEAD commit.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import threading
from typing import Any

from docker.errors import NotFound

from sunaba.journal import record_boundary_crossing, record_tool_use
from sunaba.tools.common import _docker, container_not_found_error
from sunaba.tools.vcs.gitroot import resolve_git_root

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

#: Controls whether a repo-local ``.secrets.baseline`` file is used to
#: suppress known/approved findings (default: enabled).
#:
#: The baseline is applied by us, not by detect-secrets: we scan plainly and
#: subtract findings whose ``hashed_secret`` the baseline already records.
#: Passing ``--baseline`` instead would make detect-secrets rewrite the
#: baseline file and print nothing, and an empty stdout parsed as "no
#: findings" would turn the whole guard into a silent pass.
SUNABA_SECRETS_BASELINE_ENV = "SUNABA_SECRETS_BASELINE"

_BASELINE_FILENAME = ".secrets.baseline"

# ---------------------------------------------------------------------------
# In-memory override state (baseline OFF path)
# ---------------------------------------------------------------------------

_OVERRIDE_MAP: dict[str, bool] = {}
_OVERRIDE_LOCK = threading.Lock()


def _baseline_enabled() -> bool:
    """Return True when the baseline feature is enabled (default)."""
    val = os.environ.get(SUNABA_SECRETS_BASELINE_ENV, "true").strip().lower()
    return val not in ("false", "0", "no")


# ---------------------------------------------------------------------------
# Low-level exec helper (avoids exec_run mock side_effect)
# ---------------------------------------------------------------------------


def exec_in_container(
    container: Any,
    cmd: list[str],
    workdir: str | None = None,
) -> tuple[int, str, str]:
    """Run *cmd* inside *container* using ``container.exec_run``.

    Returns ``(exit_code, stdout_text, stderr_text)``.

    Uses ``exec_run(\u2026, demux=True)`` so stdout and stderr are separated
    by docker-py rather than multiplexed together.  If demux is not
    supported (older docker-py), falls back to undemuxed output where
    all output is returned as stdout.

    This function does NOT use ``exec_create`` / ``exec_start`` / ``exec_inspect``
    because those are low-level ``docker.api.APIClient`` methods, not methods
    of ``docker.models.containers.Container``.
    """
    try:
        exec_result = container.exec_run(
            cmd=cmd,
            stdout=True,
            stderr=True,
            workdir=workdir,
            demux=True,
        )
    except TypeError as exc:
        # Fallback: older docker-py that does not support demux parameter.
        # Only catch TypeError caused by the demux keyword itself — an
        # unexpected TypeError from deeper in docker-py (e.g. a bug in
        # cmd handling) is re-raised so it is not silently retried.
        if "demux" not in str(exc):
            raise
        try:
            exec_result = container.exec_run(
                cmd=cmd,
                stdout=True,
                stderr=True,
                workdir=workdir,
            )
        except Exception as exc:
            logger.warning(
                "exec_run (fallback) failed (unexpected): %s", exc, exc_info=True,
            )
            return (-1, "", f"[exec_run exception] {exc}")
        exit_code = exec_result[0] if isinstance(exec_result, tuple) else -1
        raw = exec_result[1] if isinstance(exec_result, tuple) else exec_result
        if isinstance(raw, bytes):
            stdout_text = raw.decode("utf-8", errors="replace")
        else:
            stdout_text = str(raw) if raw else ""
        return (exit_code, stdout_text, "")
    except Exception as exc:
        logger.warning("exec_run failed (unexpected): %s", exc, exc_info=True)
        return (-1, "", f"[exec_run exception] {exc}")

    # exec_result is a namedtuple (exit_code, output)
    # with demux=True, output is (stdout_bytes, stderr_bytes)
    exit_code = exec_result[0] if isinstance(exec_result, tuple) else -1
    raw = exec_result[1] if isinstance(exec_result, tuple) else exec_result

    if isinstance(raw, tuple) and len(raw) == 2:
        stdout_bytes, stderr_bytes = raw
        stdout_text = stdout_bytes.decode("utf-8", errors="replace") if isinstance(stdout_bytes, bytes) else str(stdout_bytes) if stdout_bytes else ""
        stderr_text = stderr_bytes.decode("utf-8", errors="replace") if isinstance(stderr_bytes, bytes) else str(stderr_bytes) if stderr_bytes else ""
    else:
        # Undemuxed: all output is multiplexed as bytes
        if isinstance(raw, bytes):
            stdout_text = raw.decode("utf-8", errors="replace")
        else:
            stdout_text = str(raw) if raw else ""
        stderr_text = ""

    return (exit_code, stdout_text, stderr_text)


def _check_detect_secrets(container: Any) -> bool:
    """Return True when ``detect-secrets`` is available in the container.

    ``detect-secrets --version`` prints a bare version string like ``1.5.0``
    to stdout with exit code 0.  A non-zero exit or empty stdout means the
    tool is absent or broken.
    """
    ec, out, _ = exec_in_container(
        container,
        cmd=["detect-secrets", "--version"],
    )
    return ec == 0 and bool(out.strip())


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------


def run_secret_scan(
    container: Any,
    files: list[str],
    working_dir: str,
) -> dict[str, Any]:
    """Run detect-secrets over *files* and return a structured result.

    Parameters
    ----------
    container:
        Docker container object (from docker-py).
    files:
        Repo-relative paths of the files to scan.
    working_dir:
        Git repo root on disk inside the container.

    Returns
    -------
    A dict with keys:
      ``secret_scan``
          ``"clean"``, ``"findings"``, or a prominent skip message.
      ``findings``
          List of finding dicts (only when ``"findings"``).
      ``files_scanned``
          List of file paths that were actually scanned.
      ``scan_summary``
          One-line human-readable summary.

    Never raises.  When ``detect-secrets`` is absent the result says so
    prominently and the caller (publish) proceeds.
    """
    if not files:
        return {
            "secret_scan": "clean",
            "files_scanned": [],
            "scan_summary": "No files to scan.",
        }

    available = _check_detect_secrets(container)
    if not available:
        msg = (
            "SKIPPED (detect-secrets unavailable in this image). "
            "Install it or use the base/full sandbox image."
        )
        return {
            "secret_scan": msg,
            "files_scanned": files,
            "scan_summary": msg,
        }

    # Run scan via shell (detect-secrets needs shell for file globbing/quoting)
    # NOTE: we deliberately do NOT pass --baseline here.  detect-secrets scan
    # with --baseline writes to the baseline file and emits nothing on stdout,
    # which makes the scan result invisible.  Instead we scan plainly and
    # subtract known findings from the baseline ourselves (see below).
    # NOTE: --no-verify is required because verification plugins make outbound
    # calls that are always blocked by the sandbox egress proxy.  detect-secrets
    # reads the proxy's non-200 response as "verified: not a secret" and drops
    # the finding, making the guard fail open (issue #701).
    # The flag is not probed for.  Falling back to a scan without it would
    # restore exactly the fail-open this fixes, and a warning in a container
    # log is not a control.  --no-verify has existed since the verification
    # feature landed upstream (Yelp/detect-secrets#194, 2019) and the image
    # pins detect-secrets, so an image lacking it is a broken image: let the
    # scan fail loudly rather than quietly scan the wrong way.
    safe_wd = shlex.quote(working_dir)
    escaped_files = " ".join(shlex.quote(f) for f in files)
    shell_cmd = (
        f"cd {safe_wd} && detect-secrets scan --no-verify {escaped_files}"
    )
    ec, stdout, _ = exec_in_container(
        container,
        cmd=["/bin/sh", "-c", shell_cmd],
        workdir=working_dir,
    )

    if ec != 0:
        logger.warning("detect-secrets scan failed (ec=%d)", ec)
        msg = f"WARNING: detect-secrets scan failed (exit {ec}). Proceeding."
        return {
            "secret_scan": msg,
            "files_scanned": files,
            "scan_summary": msg,
        }

    # Parse JSON output
    if not stdout.strip():
        logger.warning("detect-secrets produced empty output")
        return {
            "secret_scan": (
                "ERROR: detect-secrets scan produced empty output. "
                "Scan did not complete; publish blocked."
            ),
            "files_scanned": files,
            "scan_summary": (
                "Scan produced empty output; treated as error."
            ),
        }

    try:
        scan_data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.warning("detect-secrets output is not valid JSON")
        return {
            "secret_scan": (
                "ERROR: detect-secrets scan produced unparseable output. "
                "Scan did not complete; publish blocked."
            ),
            "files_scanned": files,
            "scan_summary": "Scan produced unparseable output; treated as error.",
        }

    # Collect all findings (every entry in results is a finding;
    # detect-secrets does NOT emit an ``is_secret`` key).
    results: dict[str, list[dict]] = scan_data.get("results", {})
    raw_findings: list[dict[str, Any]] = []
    for filename, file_findings in results.items():
        for finding in file_findings:
            raw_findings.append({
                "file": finding.get("filename", filename),
                "line": finding.get("line_number", 0),
                "type": finding.get("type", "unknown"),
                "hashed_secret": finding.get("hashed_secret", ""),
            })

    # Subtract baseline-known findings (baseline ON path)
    if _baseline_enabled():
        baseline_path = os.path.join(working_dir, _BASELINE_FILENAME)
        ec_base, baseline_str, _ = exec_in_container(
            container,
            cmd=["/bin/sh", "-c",
                 f"cat {shlex.quote(baseline_path)} 2>/dev/null || true"],
            workdir=working_dir,
        )
        if ec_base == 0 and baseline_str.strip():
            try:
                baseline_data = json.loads(baseline_str)
            except json.JSONDecodeError:
                baseline_data = {}
            # Build a set of hashed_secret values already in the baseline
            baseline_hashes: set[str] = set()
            for file_findings in baseline_data.get("results", {}).values():
                for bf in file_findings:
                    hs = bf.get("hashed_secret", "")
                    if hs:
                        baseline_hashes.add(hs)
            # Keep only findings NOT in the baseline
            all_findings = [
                f for f in raw_findings
                if f["hashed_secret"] not in baseline_hashes
            ]
        else:
            all_findings = raw_findings
    else:
        all_findings = raw_findings

    if all_findings:
        n_files = len({f["file"] for f in all_findings})
        summary = f"{len(all_findings)} potential secret(s) found in {n_files} file(s)"
        return {
            "secret_scan": "findings",
            "findings": all_findings,
            "files_scanned": files,
            "scan_summary": summary,
        }

    return {
        "secret_scan": "clean",
        "files_scanned": files,
        "scan_summary": "No secrets detected.",
    }


# ---------------------------------------------------------------------------
# Override checking (publish-side)
# ---------------------------------------------------------------------------


def check_override(container_id: str) -> bool:
    """Check (peek, don't consume) the one-time override flag.

    Returns True when an override is active for *container_id*.  The flag
    is NOT consumed here — call :func:`consume_override` after a successful
    push so an override is never lost on push failure.

    Separate peek/consume semantics (issue #676 [medium]): if
    ``check_override`` consumed the flag and the push then failed, the user
    would need to call ``secret_scan_override`` again to re-issue the
    override.
    """
    with _OVERRIDE_LOCK:
        return _OVERRIDE_MAP.get(container_id[:12], False)


def consume_override(container_id: str) -> bool:
    """Consume the one-time override flag (called after a successful push).

    Returns True when an override was consumed, False if there was none.
    Idempotent: calling twice consumes at most once.
    """
    with _OVERRIDE_LOCK:
        existed = _OVERRIDE_MAP.pop(container_id[:12], False)
        if existed:
            logger.info("secret_scan_override consumed for %s", container_id[:12])
        return existed


# ---------------------------------------------------------------------------
# Baseline update helper (override tool, baseline ON path)
# ---------------------------------------------------------------------------


def _update_baseline(
    container: Any,
    files: list[str],
    working_dir: str,
) -> str | None:
    """Run the scan and merge results into ``.secrets.baseline``.

    Returns None on success, or an error message on failure.
    """
    safe_wd = shlex.quote(working_dir)
    escaped_files = " ".join(shlex.quote(f) for f in files)
    baseline_path = os.path.join(working_dir, _BASELINE_FILENAME)

    # Read existing baseline if present
    cat_ec, old_baseline_str, _ = exec_in_container(
        container,
        cmd=["/bin/sh", "-c", f"cat {shlex.quote(baseline_path)} 2>/dev/null || true"],
        workdir=working_dir,
    )
    old_baseline: dict = {}
    if cat_ec == 0 and old_baseline_str.strip():
        try:
            old_baseline = json.loads(old_baseline_str)
        except json.JSONDecodeError:
            pass

    # Run fresh scan (no baseline, so we capture all findings)
    # --no-verify: verification blocked by sandbox egress proxy (issue #701)
    shell_cmd = (
        f"cd {safe_wd} && detect-secrets scan --no-verify {escaped_files}"
    )
    ec, stdout, _ = exec_in_container(
        container,
        cmd=["/bin/sh", "-c", shell_cmd],
        workdir=working_dir,
    )
    if ec != 0:
        return f"detect-secrets scan failed (exit {ec})"

    try:
        scan_data = json.loads(stdout)
    except json.JSONDecodeError:
        return "detect-secrets output is not valid JSON"

    # Merge: for files in the new scan, replace; keep the rest.
    new_results: dict = scan_data.get("results", {})
    old_results: dict = old_baseline.get("results", {})
    merged_results = dict(old_results)
    for fname, findings in new_results.items():
        merged_results[fname] = findings

    merged: dict[str, Any] = {
        "generated_at": scan_data.get("generated_at", ""),
        "plugins_used": scan_data.get("plugins_used", []),
        "results": merged_results,
    }

    merged_json = json.dumps(merged, indent=2, ensure_ascii=False)
    write_cmd = (
        f"cd {safe_wd} &&"
        f" cat > {shlex.quote(baseline_path)} <<'SUNABA_BASELINE_EOF'\n"
        f"{merged_json}\n"
        f"SUNABA_BASELINE_EOF"
    )
    ec, _, stderr = exec_in_container(
        container,
        cmd=["/bin/sh", "-c", write_cmd],
        workdir=working_dir,
    )
    if ec != 0:
        return f"failed to write {_BASELINE_FILENAME}: {stderr}"

    logger.info("Updated .secrets.baseline (%d files)", len(merged_results))
    return None


# ---------------------------------------------------------------------------
# MCP tool: secret_scan_override
# ---------------------------------------------------------------------------


def secret_scan_override(
    container_id: str,
    files: list[str] | None = None,
    working_dir: str | None = None,
) -> str:
    """Override the secret scan for a publish that was blocked by findings.

    This is a *separate* MCP tool (not a ``publish`` argument) so the host
    can gate it by permission.  An agent that hits a finding cannot simply
    set a flag and carry on — the human decides whether the override is
    available at all.

    Semantics depend on the ``SUNABA_SECRETS_BASELINE`` environment variable:

    - **Enabled** (default): the override appends the current findings to
      the repo's ``.secrets.baseline`` file so the same secret is not
      re-flagged on later publishes.

    - **Disabled**: the override is one-time and in-memory — the current
      publish proceeds, but the next publish on a different diff will scan
      fresh.

    Args:
        container_id: 12-character container ID prefix.
        files: Repo-relative paths to suppress.  When omitted, uses the
            files in the HEAD commit (the ones that were staged for publish).
        working_dir: Git repo directory (default: auto-detect).

    Returns:
        JSON string with ``status`` and details.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]
    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)

    record_tool_use(cid, "secret_scan_override")

    # If files not specified, get them from HEAD commit
    if files is None or len(files) == 0:
        ec, out, _ = exec_in_container(
            container,
            cmd=["/bin/sh", "-c",
                 f"cd {safe_wd} && git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null"],
            workdir=working_dir,
        )
        if ec == 0 and out.strip():
            files = [f.strip() for f in out.splitlines() if f.strip()]
        else:
            files = []

    available = _check_detect_secrets(container)
    if not available:
        return json.dumps({
            "status": "error",
            "error": (
                "detect-secrets is not available in this container. "
                "The override tool requires detect-secrets. "
                "Use the base/full sandbox image."
            ),
        })

    if _baseline_enabled():
        files_to_scan = files if files else []
        err_msg = _update_baseline(container, files_to_scan, working_dir)
        if err_msg:
            return json.dumps({"status": "error", "error": err_msg})

        return json.dumps({
            "status": "ok",
            "action": "baseline_updated",
            "detail": (
                f"Updated .secrets.baseline with findings from {len(files_to_scan)} file(s). "
                "Run publish again; the same secrets will be suppressed."
            ),
        })
    else:
        with _OVERRIDE_LOCK:
            _OVERRIDE_MAP[cid] = True

        record_boundary_crossing(
            cid,
            "secret_scan_override",
            f"baseline=disabled files={len(files)}",
            approved=True,
        )
        return json.dumps({
            "status": "ok",
            "action": "override_set",
            "detail": (
                "Override set for the next publish. "
                "Run publish again; the scan will be bypassed for this commit.\n\n"
                "NOTE: This override is in-memory and server-restart volatile. "
                "Enable SUNABA_SECRETS_BASELINE (the default) for persistent suppression."
            ),
        })
