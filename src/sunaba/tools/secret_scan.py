"""Secret scan via detect-secrets and override tool (issue #676).

Public symbols
--------------
- ``run_secret_scan(container, files, working_dir)`` — run detect-secrets
  on files via ``container.exec_run``.
- ``secret_scan_override(container_id, ...)`` — MCP tool to bypass findings.
- ``check_override(container_id)`` — check/consume one-time override flag.

Design
------
**docs/design_secret_scan.md is authoritative.**  This docstring summarises;
that document carries the reasoning, the threat model, the known gaps and the
alternatives already considered and rejected.  If the two disagree, the
document is right and this file has drifted.

The scanner is Yelp's ``detect-secrets`` (Apache-2.0), baked into the base
image.  It is never vendored so sunaba stays MIT.

Scan scope is the *manifest diff* / *commit* files only — never the whole
tree.  This keeps the scan cheap and avoids resurfacing approved secrets.

``run_secret_scan`` reports one state: ``clean`` / ``findings`` / ``error`` /
``skipped``.  ``publish`` proceeds only on ``clean`` or ``skipped``, so an
unrecognised or absent state blocks (#704).

Missing scanner → ``skipped``, and publish proceeds.  A deliberate, named
exception, not an accident; see the known gap in the design document.

Suppressions have two separate authorities (#708):

- **immediate** — a host-held one-time flag, gated by the tool-approval
  prompt.  ``secret_scan_override`` sets it; it authorises one publish.
- **durable** — ``.secrets.baseline`` **as committed on the base branch**,
  fetched host-side, gated by PR review.

The container's ``.secrets.baseline`` is a *proposal* for a human to commit.
It carries no authority: it is agent-writable, and trusting it was a working
bypass of the permission gate (#708).
"""

from __future__ import annotations

import base64
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


def _exclude_baseline(files: list[str]) -> list[str]:
    """Filter out the repo-root .secrets.baseline path from *files*.

    The match is exact — only the literal file name ``.secrets.baseline``
    at the repo root is excluded, not lookalikes in subdirectories or with
    extras/ extensions/substrings.
    """
    return [f for f in files if f != _BASELINE_FILENAME]


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
# In-memory override registry (hashed-secret-level, issue #722)
# ---------------------------------------------------------------------------
# Maps container_id → set[hashed_secret] for findings the operator has
# overridden via the secret_scan_override MCP tool.  Lives in server process
# memory only — server restart loses it, but re-running the override restores
# it.  The publish path unions this set with the remote-base-branch baseline
# hashes, so the override takes effect immediately for THIS container without
# waiting for .secrets.baseline to land on the remote base branch.
_OVERRIDE_REGISTRY: dict[str, set[str]] = {}
_REGISTRY_LOCK = threading.Lock()


def get_override_registry_hashes(container_id: str) -> set[str]:
    """Return the hashed_secret values overridden for *container_id*.

    Returns a copy so callers can mutate it safely.  Never raises.
    """
    with _REGISTRY_LOCK:
        return _OVERRIDE_REGISTRY.get(container_id[:12], set()).copy()


def _add_to_override_registry(container_id: str, hashes: set[str]) -> None:
    """Add *hashes* to the override registry for *container_id*."""
    cid = container_id[:12]
    with _REGISTRY_LOCK:
        existing = _OVERRIDE_REGISTRY.get(cid, set())
        _OVERRIDE_REGISTRY[cid] = existing | hashes
    logger.info(
        "Override registry updated for %s: %d hashes total",
        cid, len(_OVERRIDE_REGISTRY.get(cid, set())),
    )


# ---------------------------------------------------------------------------
# Host-side baseline fetch (issue #708)
# ---------------------------------------------------------------------------


def _fetch_baseline_from_base_branch(
    repo: str,
    token: str,
    base_branch: str = "",
) -> dict | None:
    """Fetch ``.secrets.baseline`` from the base branch on GitHub, host-side.

    Uses the GitHub Contents API (``GET /repos/{repo}/contents/{path}``)
    to read the file as committed on the remote base branch.  The API call
    is made from the host process (not inside the container) so the result
    cannot be tampered with by an agent in the sandbox.

    Parameters
    ----------
    repo:
        ``"owner/repo"``.
    token:
        VCS token for authentication (may be empty for public repos).
    base_branch:
        Branch to read the baseline from.  When empty, resolves to the
        repo's default branch.

    Returns
    -------
    The parsed JSON baseline ``dict``, or ``None`` when:
    - ``.secrets.baseline`` does not exist on the base branch (HTTP 404).
    - The file exists but is not valid JSON.
    - Any network or API error occurs.

    Never raises: all errors are caught, logged, and return ``None``.
    """
    from sunaba.tools.github_api import _github_api_request

    # Resolve base branch when not given
    branch = base_branch
    if not branch:
        try:
            repo_info = _github_api_request(f"/repos/{repo}", token)
            branch = str(repo_info.get("default_branch") or "")
        except Exception as exc:
            logger.warning(
                "Could not resolve default branch for %s: %s",
                repo, exc,
            )
            return None

    if not branch:
        logger.warning(
            "Cannot fetch baseline: no base branch for %s",
            repo,
        )
        return None

    try:
        response = _github_api_request(
            f"/repos/{repo}/contents/{_BASELINE_FILENAME}?ref={branch}",
            token,
        )
    except RuntimeError as exc:
        err_msg = str(exc)
        # HTTP 404 means file does not exist on that branch — not an error
        if "HTTP 404" in err_msg:
            logger.info(
                "No .secrets.baseline on %s/%s: %s",
                repo, branch, err_msg,
            )
            return None
        logger.warning(
            "Failed to fetch .secrets.baseline from %s/%s: %s",
            repo, branch, exc,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Unexpected error fetching .secrets.baseline from %s/%s: %s",
            repo, branch, exc,
        )
        return None

    if not isinstance(response, dict):
        logger.warning(
            "Unexpected response type fetching .secrets.baseline: %s",
            type(response).__name__,
        )
        return None

    # The Contents API returns a dict with base64-encoded ``content``
    content_b64 = response.get("content", "")
    encoding = response.get("encoding", "")
    if not content_b64 or encoding != "base64":
        logger.warning(
            "Unexpected .secrets.baseline response: encoding=%s",
            encoding,
        )
        return None

    try:
        raw = base64.b64decode(content_b64)
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Could not parse .secrets.baseline from %s/%s: %s",
            repo, branch, exc,
        )
        return None


def _extract_baseline_hashes(baseline_data: dict) -> set[str]:
    """Extract all ``hashed_secret`` values from a parsed baseline dict.

    Parameters
    ----------
    baseline_data:
        A parsed ``.secrets.baseline`` JSON dict (same shape as
        detect-secrets output).

    Returns
    -------
    A ``set`` of ``hashed_secret`` strings found in the baseline.
    Never raises; returns an empty set on any structural issue.
    """
    hashes: set[str] = set()
    for file_findings in baseline_data.get("results", {}).values():
        for bf in file_findings:
            hs = bf.get("hashed_secret", "")
            if hs:
                hashes.add(hs)
    return hashes


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
    *,
    baseline_hashes: set[str] | None = None,
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
            "secret_scan_state": "clean",
            "files_scanned": [],
            "scan_summary": "No files to scan.",
        }

    # Exclude the baseline path from scanning: the baseline stores hashed
    # secrets by design, and scanning them produces false-positive findings
    # (HexHighEntropyString / KeywordDetector on every hashed_secret value).
    # An exact-path match is used so that lookalike files in subdirectories
    # (e.g. sub/dir/.secrets.baseline) are still scanned normally.
    files = _exclude_baseline(files)
    if not files:
        return {
            "secret_scan": "clean",
            "secret_scan_state": "clean",
            "files_scanned": [],
            "scan_summary": "No files to scan (baseline excluded).",
        }

    available = _check_detect_secrets(container)
    if not available:
        msg = (
            "SKIPPED (detect-secrets unavailable in this image). "
            "Install it or use the base/full sandbox image."
        )
        return {
            "secret_scan": msg,
            "secret_scan_state": "skipped",
            "files_scanned": files,
            "scan_summary": msg,
        }

    # Run scan via shell (detect-secrets needs shell for file globbing/quoting)
    #
    # Two invocation decisions here look wrong at a glance and are not.  The
    # reasoning lives in docs/design_secret_scan.md (Part 1) -- read it before
    # changing either, and do not re-derive it from these comments.
    #
    # No --baseline: with it, the scan ADDS newly found secrets to the baseline
    # and prints nothing.  Running the scan would be the act of suppressing, so
    # a blocked publish would pass on retry (#703, and the threat model).
    #
    # --no-verify, never probed for: AWSKeyDetector reads HTTP 403 as "not a
    # secret", and the egress proxy answers blocked hosts with 403, so a real
    # key pair comes back clean (#701, upstream Yelp/detect-secrets#976).
    # Falling back to a scan without the flag would restore that fail-open.
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
        msg = (
            f"WARNING: detect-secrets scan failed (exit {ec}). "
            "Scan did not complete; publish blocked."
        )
        return {
            "secret_scan": msg,
            "secret_scan_state": "error",
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
            "secret_scan_state": "error",
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
            "secret_scan_state": "error",
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

    # Subtract baseline-known findings
    if baseline_hashes is not None:
        # Host-side suppression set: remote baseline hashes (#708) +
        # override registry hashes (#722).  Applied regardless of
        # _baseline_enabled() — the caller (publish) constructs the set
        # from host-side sources only.  Nothing inside the container can
        # grow the suppression set (threat model preserved).
        all_findings = [
            f for f in raw_findings
            if f["hashed_secret"] not in baseline_hashes
        ]
    elif _baseline_enabled():
        # No host-resolved hashes supplied: apply NO suppressions.
        #
        # The container's .secrets.baseline is deliberately NOT read here.
        # It is agent-writable, and trusting it IS the bypass #708 closed:
        # take the hashed_secret out of the block response, write it into
        # the file, publish again, pass -- with the override tool's
        # permission gate never firing.
        #
        # publish always supplies a host-resolved set (empty when the
        # fetch fails), so this branch is only reachable by direct callers.
        # They get more findings, never fewer: the recoverable direction.
        all_findings = raw_findings
    else:
        all_findings = raw_findings

    suppressed_count = len(raw_findings) - len(all_findings)

    if all_findings:
        n_files = len({f["file"] for f in all_findings})
        summary = f"{len(all_findings)} potential secret(s) found in {n_files} file(s)"
        return {
            "secret_scan": "findings",
            "secret_scan_state": "findings",
            "findings": all_findings,
            "files_scanned": files,
            "scan_summary": summary,
            "suppressed_count": suppressed_count,
        }

    clean_summary = (
        f"No secrets detected ({suppressed_count} finding(s) suppressed"
        " by baseline/override)."
        if suppressed_count else "No secrets detected."
    )
    return {
        "secret_scan": "clean",
        "secret_scan_state": "clean",
        "files_scanned": files,
        "scan_summary": clean_summary,
        "suppressed_count": suppressed_count,
    }


# ---------------------------------------------------------------------------
# Override checking (publish-side)
# ---------------------------------------------------------------------------


def should_consume_override(scan_state: str, suppressed_count: int) -> bool:
    """Decide whether a successful push must burn the one-time override flag.

    A publish that needed the override machinery — a blocked-state scan or a
    registry/baseline suppression that turned findings into "clean" — must
    consume the flag; otherwise the stale flag would silently authorize a
    FUTURE publish with new findings without re-authorization (#722 review).
    A genuinely clean publish (nothing suppressed) keeps the unused flag.
    """
    return scan_state not in ("clean", "skipped") or suppressed_count > 0


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
) -> tuple[str | None, set[str]]:
    """Run the scan and merge results into ``.secrets.baseline``.

    Returns ``(None, hashes_found)`` on success, or
    ``(error_message, set())`` on failure.
    The returned hashes are the hashed_secret values from the CURRENT scan
    (before merging with any old baseline), so the caller can register them
    for immediate suppression.
    """
    # Exclude the baseline path from the scan so its own stored hashes are
    # not re-appended to the baseline on every override (self-referential
    # ratchet described in issue #703).
    files = _exclude_baseline(files)
    if not files:
        logger.info(
            "No files to scan for baseline update (baseline was the only file)."
        )
        return None, set()

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
        return f"detect-secrets scan failed (exit {ec})", set()

    try:
        scan_data = json.loads(stdout)
    except json.JSONDecodeError:
        return "detect-secrets output is not valid JSON", set()

    # Merge: for files in the new scan, replace; keep the rest.
    new_results: dict = scan_data.get("results", {})
    old_results: dict = old_baseline.get("results", {})
    merged_results = dict(old_results)
    for fname, findings in new_results.items():
        merged_results[fname] = findings

    # Extract hashed_secrets from the fresh scan for the override registry
    new_hashes: set[str] = set()
    for fname, findings in new_results.items():
        for finding in findings:
            hs = finding.get("hashed_secret", "")
            if hs:
                new_hashes.add(hs)

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
        return f"failed to write {_BASELINE_FILENAME}: {stderr}", set()

    logger.info("Updated .secrets.baseline (%d files)", len(merged_results))
    return None, new_hashes


# ---------------------------------------------------------------------------
# MCP tool: secret_scan_override
# ---------------------------------------------------------------------------


def secret_scan_override(
    container_id: str,
    files: list[str] | None = None,
    working_dir: str | None = None,
) -> str:
    """Override the secret scan for a publish that was blocked by findings.

    A separate MCP tool (not a ``publish`` argument) so the host can gate
    it by permission — the human decides whether an override is available.

    With ``SUNABA_SECRETS_BASELINE`` enabled (default) it writes the
    findings into ``.secrets.baseline`` (durable once merged to the base
    branch) AND registers their hashes host-side, so the next publish from
    THIS container suppresses them immediately (registry is lost on server
    restart; re-run to restore).  Disabled: one-time flag — the current
    publish passes the scan block; the next one scans fresh.

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
        err_msg, new_hashes = _update_baseline(
            container, files_to_scan, working_dir,
        )
        if err_msg is None:
            # Register hashes for immediate suppression in THIS container
            # (host-side, in-process-memory).  The durable path requires
            # .secrets.baseline to be committed and merged to the base branch.
            if new_hashes:
                _add_to_override_registry(cid, new_hashes)
            return json.dumps({
                "status": "ok",
                "action": "baseline_updated",
                "detail": (
                    f"Updated .secrets.baseline with findings from "
                    f"{len(files_to_scan)} file(s). "
                    f"Override registered ({len(new_hashes)} hash(es)) "
                    f"for this container: the same secrets will be suppressed "
                    f"on the next publish from THIS container (immediate, "
                    f"in-process-memory). "
                    f"For durable suppression across future containers, "
                    f"include .secrets.baseline in your publish manifest "
                    f"and merge it to the base branch."
                ),
            })
        # Baseline update failed (e.g. scanner error).  Fall through to
        # in-memory override so the tool works for error blocks too.
        logger.warning(
            "baseline update failed, falling back to in-memory override: %s",
            err_msg,
        )

    # In-memory override: used directly when baseline is disabled, or as a
    # fallback when the baseline update itself failed (scanner error).
    with _OVERRIDE_LOCK:
        _OVERRIDE_MAP[cid] = True

    record_boundary_crossing(
        cid,
        "secret_scan_override",
        f"baseline={_baseline_enabled()} files={len(files)}",
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
