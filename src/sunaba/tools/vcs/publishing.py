"""Publish tool: commit, push, PR creation."""

from __future__ import annotations  # noqa: I001

import base64
import json
import logging
import os
import re
import shlex
from typing import Any

from docker.errors import NotFound

from sunaba import proxy_lifecycle
from sunaba.journal import record_boundary_crossing
from sunaba.proxy_client import (
    ProxyAuthError,
    authorized_push_grant,
    proxy_configured,
)
from sunaba.security import NETWORK_LABEL
from sunaba.tools.common import _docker, container_not_found_error
from sunaba.tools.github_api import (
    _create_pr_via_api,
    _push_token_env,
    _resolve_vcs_token,
)
from sunaba.tools.publish_ops import (
    create_pull_request,
    git_prepare_commit,
    git_push_with_fallback,
)
from sunaba.tools.publish_planner import (
    build_push_command,
    finish_json,
    pr_body_validation_error,
    select_push_env,
    verify_gate_error,
)
from sunaba.tools.vcs.gitroot import resolve_git_root
from sunaba.verify_state import has_verify_success

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)(?!-)(?!.*@\{)"
    r"[\w./-]+$"
)


# ---------------------------------------------------------------------------
# Inline Python script for GitHub API-based push (publish's Objects API
# fallback transport, run in-container by _try_api_push)
# ---------------------------------------------------------------------------

_SANDBOX_CREATE_PR_SCRIPT = '''
import base64, json, os, shlex, subprocess, sys, tempfile


def _run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _gh_api(method, path, body):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(body, f)
        tmpfile = f.name
    try:
        r = subprocess.run(
            ["gh", "api", "-X", method, path, "--input", tmpfile],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr or r.stdout or f"(exit code {r.returncode}, no output)")
        return json.loads(r.stdout)
    finally:
        os.unlink(tmpfile)


def _read_blob(path):
    \"\"\"Read blob content from git (binary-safe): returns bytes of what git
    committed for *path* at HEAD.  Git stores symlink targets as their
    target-path string, so this is safe for symlinks too (open() follows
    symlinks and reads the target file instead).\"\"\"
    r = subprocess.run(
        [\"git\", \"cat-file\", \"blob\", f\"HEAD:{path}\"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise OSError(
            r.stderr.decode(errors=\"replace\").strip()
            or f\"git cat-file blob HEAD:{path} failed (exit {r.returncode})\"
        )
    return r.stdout


repo, branch, working_dir = sys.argv[1], sys.argv[2], sys.argv[3]
os.chdir(working_dir)

# 1. Collect local commit info
ec, head_sha, _ = _run("git rev-parse HEAD")
if ec != 0:
    print(json.dumps({"status": "error", "error": "git rev-parse HEAD failed", "detail": head_sha}))
    sys.exit(1)

ec_log, commit_msg, _ = _run("git log -1 --format=%B")
if ec_log != 0 or not commit_msg:
    commit_msg = "(no commit message)"
_, author_name, _ = _run("git log -1 --format=%an")
_, author_email, _ = _run("git log -1 --format=%ae")

# 2. Get all files in HEAD
_, files_out, _ = _run("git ls-tree -r --name-only HEAD")
files = [f for f in files_out.split("\\n") if f]

# 3. Create blobs
tree_items = []
for filepath in files:
    _, mode_line, _ = _run(f"git ls-tree HEAD -- {shlex.quote(filepath)}")
    if not mode_line.strip():
        print(json.dumps({"status": "error", "error": f"git ls-tree HEAD -- {filepath}: empty output"}))
        sys.exit(1)
    parts = mode_line.split()
    if len(parts) < 3:
        print(json.dumps({"status": "error", "error": f"unexpected ls-tree output for {filepath}: {mode_line!r}"}))
        sys.exit(1)
    mode = parts[0]
    try:
        raw = _read_blob(filepath)
        file_content = base64.b64encode(raw).decode()
    except OSError as e:
        print(json.dumps({"status": "error", "error": f"read {filepath}: {e}"}))
        sys.exit(1)
    blob = _gh_api(
        "POST",
        f"repos/{repo}/git/blobs",
        {"content": file_content, "encoding": "base64"},
    )
    tree_items.append(
        {"path": filepath, "mode": mode, "type": "blob", "sha": blob["sha"]}
    )

# 4. Create tree
tree = _gh_api("POST", f"repos/{repo}/git/trees", {"tree": tree_items})

# 5. Resolve parent SHA on GitHub (existing branch > main > master)
parent_sha = None
for ref_name in [f"heads/{branch}", "heads/main", "heads/master"]:
    ec2, ref_out, _ = _run(f"gh api repos/{shlex.quote(repo)}/git/ref/{shlex.quote(ref_name)} 2>/dev/null")
    if ec2 == 0:
        try:
            parent_sha = json.loads(ref_out)["object"]["sha"]
            break
        except Exception:
            continue

# 6. Create commit
commit_body = {
    "message": commit_msg,
    "tree": tree["sha"],
    "author": {"name": author_name, "email": author_email},
}
if parent_sha:
    commit_body["parents"] = [parent_sha]
commit = _gh_api("POST", f"repos/{repo}/git/commits", commit_body)
new_sha = commit["sha"]

# 7. Create or update branch ref
try:
    _gh_api(
        "PATCH",
        f"repos/{repo}/git/refs/heads/{branch}",
        {"sha": new_sha, "force": True},
    )
except RuntimeError:
    _gh_api(
        "POST",
        f"repos/{repo}/git/refs",
        {"ref": f"refs/heads/{branch}", "sha": new_sha},
    )

print(json.dumps({"sha": new_sha, "tree_sha": tree["sha"], "parent_sha": parent_sha}))
'''


# ---------------------------------------------------------------------------
# publish — token / REST API / PR creation moved to github_api.py
# ---------------------------------------------------------------------------


def _ensure_proxy_ready(client: Any) -> str | None:
    """Reconcile the egress-proxy sidecar before a grant is opened against it.

    ``ensure_egress_proxy`` is idempotent and cheap on the happy path (a
    ``docker inspect`` of the running sidecar), so ``publish`` simply re-runs
    it rather than inferring the sidecar's state:

    - It re-exports ``SUNABA_PROXY_CONTROL_URL/SECRET`` into this process,
      which a server restart wipes even though the sidecar and a pre-existing
      container's proxied network keep running (#428).  Without this the
      caller would see ``proxy_configured()`` as ``False`` and silently skip
      the authorization grant that the sidecar still enforces.
    - It recreates a sidecar that is gone, exited, or baked with a config the
      host has since changed (#533).  Keying off the env vars alone -- as this
      used to -- meant a removed sidecar left ``publish`` reporting
      ``control API unreachable`` for the rest of the session, since the stale
      env made the proxy *look* configured.

    Returns an error string on failure (caller must fail closed), or ``None``
    when the sidecar is ready or the proxy is off.
    """
    if not proxy_lifecycle.egress_proxy_enabled():
        return None
    try:
        proxy_lifecycle.ensure_egress_proxy(client)
    except Exception as e:
        return f"egress proxy is enabled but unavailable (failing closed): {e}"
    return None


# The single exit tool (docs/design.md section 11.1).  Two transports: git
# push with credential helper, then GitHub Objects API
# (blob->tree->commit->ref) as automatic fallback.  Host-side token
# resolution is #347, proxy-injected push #356, host-side PR creation #360.
# The dry-run/confirmation-token step was retired in the V1.0 cleanup.
def publish(
    container_id: str,
    repo: str,
    branch: str,
    message: str,
    working_dir: str | None = None,
    create_pr: bool = False,
    pr_title: str = "",
    pr_body: str = "",
    base_branch: str = "",
    allow_force_push: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
    skip_verify_gate: bool = False,
    files: list[str] | None = None,
    include_untracked: bool = False,
) -> str:
    """Stage, commit, push, and optionally create a PR -- the single exit tool.

    Does NOT verify: call verify_in_container first.  Credentials resolved
    host-side; no token enters the container.  Falls back to GitHub Objects
    API on git-push refusal.

    Args:
        container_id: Container ID prefix.
        repo: 'owner/repo'.
        branch: Branch name to push.
        message: Git commit message.
        working_dir: Git repo directory (default: auto-detect).
        create_pr: Open a pull request after the push.
        pr_title: PR title; required when create_pr=True.
        pr_body: PR body.
        base_branch: PR base (default: repo default branch).
        allow_force_push: Permit git push --force.
        author_name: Override commit author name.
        author_email: Override commit author email.
        skip_verify_gate: Bypass verify gate.
        files: When non-empty, stage only the declared repo-relative paths
            (manifest mode).  Each path must be a regular file, not a
            directory.  Undeclared files stay in the worktree.
            When None or empty, fall back to the legacy ``git add -A``
            behaviour, but only if ``include_untracked`` is True or no
            untracked files exist (see below).
        include_untracked: When True and no manifest is given, stage all
            files including untracked ones (the old default).  When False
            (default) and no manifest is given, the call is rejected if
            untracked files exist.

    Returns:
        JSON with the operation result.
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

    verified = has_verify_success(cid)
    gate_err = verify_gate_error(verified, skip_verify_gate)
    if gate_err:
        return json.dumps(gate_err)

    body_err = pr_body_validation_error(create_pr, pr_body)
    if body_err:
        return json.dumps(body_err)

    def _run(cmd, env=None):
        full_cmd = f"cd {shlex.quote(working_dir)} && {cmd}"
        ec, out = container.exec_run(
            ["/bin/sh", "-c", full_cmd],
            stdout=True, stderr=True, environment=env,
        )
        o, e = out if isinstance(out, tuple) else (out, b"")
        return (ec,
            (o.decode("utf-8","replace") if o else ""),
            (e.decode("utf-8","replace") if e else ""))

    proxy_err = _ensure_proxy_ready(client)
    if proxy_err:
        return finish_json({"status": "error", "step": "egress_proxy", "error": proxy_err}, verified)

    # --- Manifest mode vs fallback mode ---
    manifest = files is not None and len(files) > 0

    if manifest:
        assert files is not None  # type-narrowing hint for pyright
        # Validate every declared path:
        # - Must be repo-relative (no absolute, no .. traversal)
        # - Must exist in the working tree OR be tracked in the index
        #   (i.e. the file is known to git; deletion declaration).
        for f in files:
            if os.path.isabs(f) or ".." in f.split("/"):
                return finish_json({
                    "status": "error",
                    "step": "validation",
                    "error": (
                        f"Invalid path '{f}': paths must be repo-relative"
                        " (no absolute paths or .. traversal)."
                    ),
                }, verified)
            ec, _, _ = _run(f"test -f {shlex.quote(f)}")
            if ec != 0:
                # Not a regular file -- allow if the path is tracked in
                # the index (i.e. the user is declaring a deletion).
                # :(literal) disables pathspec glob interpretation so a
                # declared path like '*.py' cannot match tracked files it
                # does not literally name.
                track_ec, _, _ = _run(
                    "git ls-files --error-unmatch -- "
                    + shlex.quote(f":(literal){f}")
                )
                if track_ec != 0:
                    return finish_json({
                        "status": "error",
                        "step": "validation",
                        "error": (
                            f"Path '{f}' does not exist or is not a regular file. "
                            "Manifests must list regular files one by one."
                        ),
                    }, verified)

    # --- Secret scan (issue #676) ---
    # Scan BEFORE commit in manifest mode: the declared file list is already
    # validated and the files exist on disk.  This prevents secrets from
    # entering local git history (issue #676 [medium]).
    #
    # Lazy import avoids the circular dependency:
    #   secret_scan -> vcs.gitroot -> vcs.__init__ -> vcs.publishing -> secret_scan
    from sunaba.tools.secret_scan import (  # fmt: skip  # noqa: I001  # pyright: ignore[reportUnusedImport]
        _baseline_enabled,
        _fetch_baseline_from_base_branch,
        _extract_baseline_hashes,
        check_override,
        consume_override,
        exec_in_container,
        run_secret_scan,
    )

    # Declared once, before either branch: re-initialising it below the
    # manifest branch would discard the manifest scan's outcome, leaving the
    # result reporting "clean" and — worse — never consuming a used override,
    # so a single authorisation would silently stay live for every later
    # publish.
    # Default result when no scan runs at all.  Unreachable today -- the
    # manifest / not-manifest branches below are exhaustive -- so this is
    # purely the safety net for a third branch added later.  It is therefore
    # a BLOCKING state on purpose: a code path that publishes without
    # scanning is exactly the fail-open #704 closed, so the net has to catch
    # it rather than wave it through.  Anything that legitimately skips the
    # scan must say so explicitly with "skipped".
    scan_result: dict[str, Any] = {
        "secret_scan": (
            "ERROR: no secret scan ran for this publish. "
            "Scan state could not be determined; publish blocked."
        ),
        "secret_scan_state": "unknown",
        "files_scanned": [],
    }

    # --- Host-side baseline fetch (issue #708) ---
    # The suppression list is fetched from the base branch on GitHub via the
    # REST API, NOT from the container filesystem (which the agent can write
    # to).  When the fetch fails, we pass an empty set (no suppressions),
    # which is the recoverable direction: it blocks more rather than silently
    # passing a finding.  We NEVER fall back to the container's copy.
    baseline_hashes_arg: set[str] | None = None
    if _baseline_enabled():  # noqa: F821
        git_token = _resolve_vcs_token()
        try:
            baseline_data = _fetch_baseline_from_base_branch(  # noqa: F821
                repo, git_token, base_branch,
            )
            if baseline_data is not None:
                baseline_hashes_arg = _extract_baseline_hashes(  # noqa: F821
                    baseline_data,
                )
            else:
                # No baseline on the base branch: no suppressions (safe).
                baseline_hashes_arg = set()
        except Exception as exc:
            logger.warning(
                "Failed to fetch baseline from base branch: %s", exc,
            )
            # Safe: no suppressions, all findings reported
            baseline_hashes_arg = set()

    if manifest:
        assert files is not None
        scan_files = [f for f in files if not os.path.isabs(f)]
        scan_result = run_secret_scan(
            container, scan_files, working_dir,
            baseline_hashes=baseline_hashes_arg,
        )  # noqa: F821
        scan_state = scan_result.get("secret_scan_state", "")
        # Fail-closed: proceed ONLY on known-safe states.
        if scan_state not in ("clean", "skipped"):
            record_boundary_crossing(
                cid, "publish",
                f"secret_scan state={scan_state}"
                f" findings={len(scan_result.get('findings', []))}"
                f" files={scan_result.get('files_scanned', [])}",
                approved=False,
            )
            has_override = check_override(cid)  # noqa: F821  # peek, don't consume yet
            if not has_override:
                return finish_json({
                    "status": "error",
                    "step": "secret_scan",
                    "secret_scan": scan_result.get("secret_scan"),
                    "secret_scan_state": scan_state,
                    "findings": scan_result.get("findings"),
                    "files_scanned": scan_result.get("files_scanned"),
                    "scan_summary": scan_result.get("scan_summary"),
                    "error": (
                        "publish blocked by secret scan. "
                        "Use `secret_scan_override` MCP tool to bypass "
                        "(requires human authorization)."
                    ),
                }, verified)

        swept_untracked: list[str] = []
        commit_err = git_prepare_commit(_run, branch=branch, message=message,
            files=files, author_name=author_name, author_email=author_email)
        if commit_err:
            return finish_json(commit_err, verified)

        # Compute leftover changes (undeclared tracked modifications,
        # untracked files) after the manifest commit so the caller can see
        # what was left behind.
        # -z gives NUL-delimited entries with paths verbatim (no C-quoting
        # of non-ASCII/special characters).  Entry format: "XY <path>";
        # a rename/copy entry is followed by the source path as its own
        # NUL-separated token.
        _, status_out, _ = _run("git status --porcelain -z")
        worktree_leftover: list[str] = []
        tokens = [t for t in status_out.split("\0") if t]
        i = 0
        while i < len(tokens):
            entry = tokens[i]
            i += 1
            if len(entry) < 4:
                continue
            worktree_leftover.append(entry[3:])
            if entry[0] in ("R", "C") and i < len(tokens):
                worktree_leftover.append(tokens[i])
                i += 1
    else:
        # Capture untracked files before git add -A sweeps them in
        _, ls_out, _ = _run("git ls-files --others --exclude-standard")
        swept_untracked = [f for f in ls_out.split("\n") if f.strip()]

        # Reject if untracked files exist and caller didn't opt in
        if swept_untracked and not include_untracked:
            return finish_json({
                "status": "error",
                "step": "untracked_files",
                "error": (
                    "Untracked files are present in the working tree. "
                    "Pass files=[...] with repo-relative paths to declare "
                    "exactly what to stage, or pass include_untracked=True "
                    "to opt in to the previous behaviour."
                ),
                "untracked_files": swept_untracked,
                "hint": (
                    "Use files=[...] to stage specific paths declaratively, "
                    "or include_untracked=True for the old git add -A."
                ),
            }, verified)

        commit_err = git_prepare_commit(_run, branch=branch, message=message,
            author_name=author_name, author_email=author_email)
        if commit_err:
            return finish_json(commit_err, verified)
        # Legacy mode does not report worktree_leftover
        worktree_leftover = []

    # --- Secret scan (legacy mode, issue #676) ---
    # In legacy mode the commit already happened.  Scan the HEAD commit
    # files using exec_run (Container.exec_run, not the low-level
    # exec_create/exec_start/exec_inspect which are APIClient methods).
    if not manifest:
        _, diff_out, _ = exec_in_container(
            container,
            cmd=["/bin/sh", "-c",
                 f"cd {shlex.quote(working_dir)} && git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null"],
        )
        scan_files = [f.strip() for f in diff_out.splitlines() if f.strip()]
        scan_result = run_secret_scan(
            container, scan_files, working_dir,
            baseline_hashes=baseline_hashes_arg,
        )  # noqa: F821
        scan_state = scan_result.get("secret_scan_state", "")
        # Fail-closed: proceed ONLY on known-safe states.
        if scan_state not in ("clean", "skipped"):
            record_boundary_crossing(
                cid, "publish",
                f"secret_scan state={scan_state}"
                f" findings={len(scan_result.get('findings', []))}"
                f" files={scan_result.get('files_scanned', [])}",
                approved=False,
            )
            has_override = check_override(cid)  # noqa: F821
            if not has_override:
                return finish_json({
                    "status": "error",
                    "step": "secret_scan",
                    "secret_scan": scan_result.get("secret_scan"),
                    "secret_scan_state": scan_state,
                    "findings": scan_result.get("findings"),
                    "files_scanned": scan_result.get("files_scanned"),
                    "scan_summary": scan_result.get("scan_summary"),
                    "error": (
                        "publish blocked by secret scan. "
                        "Use `secret_scan_override` MCP tool to bypass "
                        "(requires human authorization)."
                    ),
                }, verified)

    push_token = _resolve_vcs_token()
    token_env = _push_token_env(push_token)
    proxied = proxy_configured()
    push_env = select_push_env(token_env, proxied)
    network_off = container.labels.get(NETWORK_LABEL) == "false"
    token_missing = not push_token
    push_cmd = build_push_command(branch, allow_force_push)

    sha = ""

    def _record_crossing(reason, approved):
        record_boundary_crossing(cid, "publish", reason, approved=approved)

    try:
        with authorized_push_grant(repo, token=push_token or None):
            push_err_payload, sha = git_push_with_fallback(
                _run,
                repo=repo, branch=branch, cid=cid,
                push_cmd=push_cmd, push_env=push_env,
                network_off=network_off, token_missing=token_missing,
                try_api_push=lambda: _try_api_push(
                    container, cid, repo, branch, working_dir, env=push_env,
                ),
                record_crossing=_record_crossing,
            )
            if push_err_payload:
                return finish_json(push_err_payload, verified)

            # Push succeeded — consume the override flag now (not before,
            # so an override is never lost on retry after a push failure).
            scan_state = scan_result.get("secret_scan_state", "")
            if scan_state not in ("clean", "skipped"):
                consume_override(cid)  # noqa: F821

    except ProxyAuthError as exc:
        _record_crossing(
            f"repo={repo} branch={branch} proxy_auth_failed", False,
        )
        return finish_json(
            {"status": "error", "step": "proxy_auth", "error": str(exc)}, verified)

    pr_url: str | None = None
    if create_pr:
        pr_url, pr_create_error = create_pull_request(
            _run, repo=repo, branch=branch,
            pr_title=pr_title, pr_body=pr_body, base_branch=base_branch,
            push_token=push_token, proxied=proxied,
            token_env=token_env, create_pr_via_api=_create_pr_via_api)
        if pr_create_error is not None:
            _record_crossing(
                f"repo={repo} branch={branch} sha={sha} pr_create_failed",
                approved=True,
            )
            return finish_json({
                "status": "pushed", "branch": branch, "sha": sha,
                "pr_create_error": pr_create_error,
            }, verified)

    details = f"repo={repo} branch={branch} sha={sha}"
    if pr_url:
        details += f" pr_url={pr_url}"
    _record_crossing(details, approved=True)

    result: dict[str, Any] = {
        "status": "pushed", "branch": branch, "sha": sha,
        "swept_untracked": swept_untracked,
        "secret_scan": scan_result.get("secret_scan", "clean"),
        "secret_scan_state": scan_result.get("secret_scan_state", "unknown"),
        "files_scanned": scan_result.get("files_scanned", []),
    }
    if manifest:
        result["staged_files"] = files
        result["worktree_leftover"] = worktree_leftover
    if pr_url:
        result["pr_url"] = pr_url
    if not create_pr:
        result["note"] = (
            "pushed only -- no PR was created. Pass create_pr=True to open "
            "one, or the branch may already have an open PR."
        )
    return finish_json(result, verified)


# ---------------------------------------------------------------------------
# Internal transport: GitHub Objects API push (used by publish as fallback)
# ---------------------------------------------------------------------------


def _try_api_push(
    container: Any,
    cid: str,
    repo: str,
    branch: str,
    working_dir: str,
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Push HEAD via GitHub Objects API (blob->tree->commit->ref).

    Returns ``{"status": "ok", "sha": "<sha>"}`` on success,
    or ``{"status": "error", "error": "..."}`` on failure.

    *env* carries the lazily-injected VCS token (Issue #347).  It is
    forwarded only to the exec that runs the API-push script — the script
    reads ``GITHUB_TOKEN`` from its environment to authenticate — so a
    container that carries no VCS token of its own can still push via this
    fallback transport.
    """
    script_b64 = base64.b64encode(
        _SANDBOX_CREATE_PR_SCRIPT.encode("utf-8")
    ).decode("ascii")

    def _run(
        cmd: str, exec_env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        ec, out = container.exec_run(
            ["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=working_dir,
            environment=exec_env,
        )
        stdout_b, stderr_b = out or (b"", b"")
        out_text = stdout_b.decode("utf-8", errors="replace").strip() if stdout_b else ""
        err_text = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
        return ec, out_text, err_text

    _run(f"echo {shlex.quote(script_b64)} | base64 -d > /tmp/_sandbox_create_pr.py")

    ec, out, err = _run(
        f"trap 'rm -f /tmp/_sandbox_create_pr.py' EXIT"
        f" && python3 /tmp/_sandbox_create_pr.py {shlex.quote(repo)} {shlex.quote(branch)} {shlex.quote(working_dir)}",
        exec_env=env,
    )
    if ec != 0:
        return {"status": "error", "error": err or out}

    try:
        push_result = json.loads(out)
    except json.JSONDecodeError:
        return {"status": "error", "error": out or err}

    if "error" in push_result:
        return {"status": "error", "error": push_result["error"]}

    return {"status": "ok", "sha": push_result.get("sha", "unknown")[:7]}
