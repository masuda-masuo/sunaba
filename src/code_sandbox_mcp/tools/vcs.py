"""VCS tools: issue_view, publish, sandbox_create_pr, clone_repo."""

from __future__ import annotations

import base64
import json
import logging
import posixpath
import re
import shlex
from typing import Any

from docker.errors import NotFound

from code_sandbox_mcp.journal import get_or_create_run_id, record_boundary_crossing
from code_sandbox_mcp.token import generate_token, verify_and_consume
from code_sandbox_mcp.tools.common import _docker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

_REPO_FORMAT_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_BRANCH_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)(?!-)(?!.*@\{)"
    r"[\w./-]+$"
)


# ---------------------------------------------------------------------------
# Git root auto-detection
# ---------------------------------------------------------------------------

_DEFAULT_WD = "/home/sandbox"


def resolve_git_root(
    container: Any,
    working_dir: str | None = None,
) -> str:
    """Auto-detect git repository root when *working_dir* is not given.

    When *working_dir* is explicitly provided, return it unchanged.
    When *working_dir* is ``None`` (default), try to locate the
    actual git repository root by:

    0. Reading ``~/.sandbox-meta.json`` (written by
       :func:`sandbox_initialize` after a successful clone)
    1. Testing ``/home/sandbox`` with ``git rev-parse --show-toplevel``
    2. Scanning ``/tmp/repo/*/`` for a git repository

    Step 0 handles any ``clone_dest`` value and is the primary path.
    Steps 1-2 are fallbacks for containers that were cloned before the
    metadata mechanism was introduced.

    Returns the resolved path, or ``/home/sandbox`` as fallback.
    """
    if working_dir is not None:
        return working_dir

    # Step 0: container metadata — written by sandbox_initialize after clone
    ec0, out0 = container.exec_run(
        ["/bin/sh", "-c",
         "cat /home/sandbox/.sandbox-meta.json 2>/dev/null || echo __NO_META__"],
        stdout=True,
    )
    if ec0 == 0:
        _stdout0, _ = (out0 if isinstance(out0, tuple) else (out0, b""))
        meta_str = _stdout0.decode("utf-8", errors="replace").strip() if _stdout0 else ""
        if meta_str and meta_str != "__NO_META__":
            try:
                meta = json.loads(meta_str)
                clone_path = meta.get("clone_path", "")
                if clone_path:
                    ec_ck, out_ck = container.exec_run(
                        ["/bin/sh", "-c",
                         f"cd {shlex.quote(clone_path)} && git rev-parse --show-toplevel 2>/dev/null || echo __NO_REPO__"],
                        stdout=True,
                    )
                    if ec_ck == 0:
                        _stdout_ck, _ = (out_ck if isinstance(out_ck, tuple) else (out_ck, b""))
                        verified = _stdout_ck.decode("utf-8", errors="replace").strip() if _stdout_ck else ""
                        if verified and verified != "__NO_REPO__":
                            return verified
            except json.JSONDecodeError:
                pass

    # Step 1: test the default location
    ec, out = container.exec_run(
        ["/bin/sh", "-c",
         "cd /home/sandbox && git rev-parse --show-toplevel 2>/dev/null || echo __NO_REPO__"],
        stdout=True,
    )
    if ec == 0:
        _stdout, _ = (out if isinstance(out, tuple) else (out, b""))
        path = _stdout.decode("utf-8", errors="replace").strip() if _stdout else ""
        if path and path != "__NO_REPO__":
            return path

    # Step 2: scan /tmp/repo/ for cloned repositories
    ec2, out2 = container.exec_run(
        ["/bin/sh", "-c",
         "for d in /tmp/repo/*/; do"
         '  [ -d "${d}.git" ] &&'
         "  git -C \"$d\" rev-parse --show-toplevel 2>/dev/null && exit 0;"
         "done; echo __NO_REPO__"],
        stdout=True,
    )
    if ec2 == 0:
        _stdout2, _ = (out2 if isinstance(out2, tuple) else (out2, b""))
        _path2 = _stdout2.decode("utf-8", errors="replace").strip() if _stdout2 else ""
        if _path2 and _path2 != "__NO_REPO__":
            return _path2

    return _DEFAULT_WD  # fallback


# ---------------------------------------------------------------------------
# Inline Python script for GitHub API-based push (sandbox_create_pr)
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


repo, branch, working_dir = sys.argv[1], sys.argv[2], sys.argv[3]
os.chdir(working_dir)

# 1. Collect local commit info
ec, head_sha, _ = _run("git rev-parse HEAD")
if ec != 0:
    print(json.dumps({"error": "git rev-parse HEAD failed", "detail": head_sha}))
    sys.exit(1)

ec_log, commit_msg, _ = _run("git log -1 --format=%B")
if ec_log != 0 or not commit_msg:
    commit_msg = "(no commit message)"
_, author_name, _ = _run("git log -1 --format=%an")
_, author_email, _ = _run("git log -1 --format=%ae")

# 2. Get all files in HEAD
_, files_out, _ = _run("git ls-tree -r --name-only HEAD")
files = [f for f in files_out.split("\n") if f]

# 3. Create blobs
tree_items = []
for filepath in files:
    _, mode_line, _ = _run(f"git ls-tree HEAD -- {shlex.quote(filepath)}")
    parts = mode_line.split()
    mode = parts[0] if parts else "100644"
    try:
        with open(filepath, "rb") as fh:
            file_content = base64.b64encode(fh.read()).decode()
    except OSError as e:
        print(json.dumps({"error": f"read {filepath}: {e}"}))
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
# checkpoint -- local-only save point (no push, no verify, no token)
# ---------------------------------------------------------------------------


def checkpoint(
    container_id: str,
    message: str,
    working_dir: str | None = None,
) -> str:
    """Create a local Git checkpoint (commit only, no push).

    Container-local operation: no verify gate, no confirmation token,
    no network access required.  Use this frequently during edit/verify
    loops so you can roll back to any save point.

    Args:
        container_id: 12-character container ID prefix.
        message: Commit message for the checkpoint.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).

    Returns:
        JSON string with ``status``, ``sha`` (short), and ``message``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]
    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)
    safe_msg = shlex.quote(message)

    cmd = f"cd {safe_wd} && git add -A && git commit --allow-empty -m {safe_msg}"
    ec, out = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout, stderr = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "step": "checkpoint",
            "error": stderr_text or stdout_text,
        })

    sha = ""
    sha_ec, sha_out = container.exec_run(
        ["/bin/sh", "-c", f"cd {safe_wd} && git rev-parse --short HEAD"],
        stdout=True,
    )
    if sha_ec == 0:
        sha_bytes = sha_out[0] if isinstance(sha_out, tuple) else sha_out
        sha = sha_bytes.decode("utf-8", errors="replace").strip() if sha_bytes else ""

    record_boundary_crossing(
        cid,
        "checkpoint",
        f"sha={sha} message={message[:80]}",
        approved=None,
    )

    return json.dumps({
        "status": "ok",
        "sha": sha,
        "message": message,
    })


# ---------------------------------------------------------------------------
# checkpoint_list -- list local checkpoints
# ---------------------------------------------------------------------------


def checkpoint_list(
    container_id: str,
    working_dir: str | None = None,
    limit: int = 20,
) -> str:
    """List unpushed local Git checkpoints (no push, no verify, no token).

    Shows only commits that have not been pushed to any remote.  After
    :func:`publish` succeeds the list naturally becomes empty.

    Args:
        container_id: 12-character container ID prefix.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).
        limit: Maximum number of checkpoints to return (default 20).

    Returns:
        JSON string with ``checkpoints`` array, each entry with
        ``sha``, ``message``, and ``date``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)
    cmd = (
        f"cd {safe_wd} &&"
        f" git log --oneline --format='%h %aI %s' HEAD --not --remotes -{int(limit)}"
    )
    ec, out = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout, _ = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "step": "checkpoint_list",
            "error": stdout_text,
        })

    checkpoints = []
    for line in stdout_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 2)
        if len(parts) >= 3:
            checkpoints.append({
                "sha": parts[0],
                "date": parts[1],
                "message": parts[2],
            })
        elif len(parts) == 2:
            checkpoints.append({
                "sha": parts[0],
                "date": parts[1],
                "message": "",
            })

    return json.dumps({"checkpoints": checkpoints})


# ---------------------------------------------------------------------------
# checkpoint_restore -- rollback to a checkpoint
# ---------------------------------------------------------------------------


def checkpoint_restore(
    container_id: str,
    sha: str,
    working_dir: str | None = None,
) -> str:
    """Restore working tree to a previous checkpoint via ``git reset --hard``.

    **Warning:** This discards uncommitted changes.  Call
    :func:`checkpoint` first if you want to preserve current state.

    Only tracked files are restored -- untracked files are not removed.

    Container-local operation: no verify gate, no confirmation token.

    Args:
        container_id: 12-character container ID prefix.
        sha: SHA (or abbreviation) of the checkpoint to restore.
        working_dir: Directory in the container containing the git
            repository (default ``None`` = auto-detect).

    Returns:
        JSON string with ``status``, ``restored_to``, and ``warning``.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]
    working_dir = resolve_git_root(container, working_dir)
    safe_wd = shlex.quote(working_dir)
    safe_sha = shlex.quote(sha)

    cmd = f"cd {safe_wd} && git reset --hard {safe_sha}"
    ec, out = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )
    stdout, stderr = (out if isinstance(out, tuple) else (out, b""))
    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    if ec != 0:
        return json.dumps({
            "status": "error",
            "step": "checkpoint_restore",
            "error": stderr_text or stdout_text,
        })

    current_sha = ""
    sha_ec, sha_out = container.exec_run(
        ["/bin/sh", "-c", f"cd {safe_wd} && git rev-parse --short HEAD"],
        stdout=True,
    )
    if sha_ec == 0:
        sha_bytes = sha_out[0] if isinstance(sha_out, tuple) else sha_out
        current_sha = sha_bytes.decode("utf-8", errors="replace").strip() if sha_bytes else ""

    record_boundary_crossing(
        cid,
        "checkpoint_restore",
        f"restored_to={current_sha} requested={sha}",
        approved=None,
    )

    return json.dumps({
        "status": "ok",
        "restored_to": current_sha,
        "warning": (
            "Uncommitted changes were discarded. "
            "Checkpoints after the restored SHA are removed from git log "
            "(still in reflog). Untracked files are not cleaned."
        ),
    })

# ---------------------------------------------------------------------------
# issue_view
# ---------------------------------------------------------------------------


def issue_view(
    container_id: str,
    repo: str,
    issue_number: int,
    save_to: str = "/home/sandbox/issue.md",
) -> str:
    """Read a GitHub issue and save its body to a file inside the container.

    Uses ``gh issue view`` inside the container.  The issue body is
    written to *save_to* and the LLM receives only a summary + handle
    (file path and size).  Full text can be retrieved with
    :func:`read_file_range`.

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True``.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``"owner/repo"`` format.
        issue_number: Issue number to fetch.
        save_to: Path inside the container to save the issue body
            (default ``"/home/sandbox/issue.md"``).

    Returns:
        JSON string with ``number``, ``title``, ``summary`` (up to 100
        characters of body), ``file`` path, and ``size_bytes``.
        On error returns an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]

    # Fetch issue metadata as JSON (includes number, title, body)
    json_cmd = (
        f"gh issue view {issue_number} --repo {shlex.quote(repo)}"
        f" --json number,title,body"
    )
    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", json_cmd],
        stdout=True,
        stderr=True,
    )
    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        return json.dumps({
            "error": f"Failed to fetch issue #{issue_number} from {repo}: {stderr_text or stdout_text}"
        })

    try:
        issue_data = json.loads(stdout_text)
    except json.JSONDecodeError:
        return json.dumps({
            "error": f"Failed to parse issue JSON: {stdout_text[:200]}"
        })

    number = issue_data.get("number", issue_number)
    title = issue_data.get("title", "")
    body = issue_data.get("body", "")

    # Summary: first 100 characters of body
    summary = body[:100] if body else "(empty body)"

    # Write body to file in container via base64
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    dir_part = posixpath.dirname(save_to)
    write_cmd = (
        f"mkdir -p {shlex.quote(dir_part)} &&"
        f" echo {shlex.quote(encoded)} | base64 -d > {shlex.quote(save_to)}"
    )
    exit_code2, _ = container.exec_run(
        ["/bin/sh", "-c", write_cmd],
        stdout=True,
        stderr=True,
    )

    if exit_code2 != 0:
        return json.dumps({
            "error": f"Failed to write issue body to {save_to}"
        })

    size_bytes = len(body.encode("utf-8"))

    # Record boundary crossing (read-only, so approved=None)
    record_boundary_crossing(
        cid,
        "issue_view",
        f"repo={repo} issue=#{number} title={title[:60]}",
        approved=None,
    )

    return json.dumps({
        "number": number,
        "title": title,
        "summary": summary,
        "file": save_to,
        "size_bytes": size_bytes,
    })


# ---------------------------------------------------------------------------
# Shared dry_run / confirmation-token flow for boundary-crossing writes
# (Issue #169).  Both ``publish`` and ``sandbox_create_pr`` route their
# dry_run -> token -> execute flow through these helpers so the logic has a
# single implementation and cannot drift between the two tools.
# ---------------------------------------------------------------------------


def _dry_run_response(
    operation: str,
    cid: str,
    run_id: str,
    details: str,
    payload: dict[str, Any],
) -> str:
    """Generate a confirmation token and return the ``dry_run`` response.

    Generates a one-time confirmation token, records a pending
    (``approved=None``) boundary crossing in the journal, and returns the
    standard ``dry_run`` JSON envelope merged with *payload*.

    Args:
        operation: Operation type recorded on the token and journal
            (e.g. ``"publish"``, ``"sandbox_create_pr"``).
        cid: 12-character container ID prefix.
        run_id: Run identifier from the journal.
        details: Human-readable summary of what execution will do.
        payload: Extra fields merged into the response (e.g.
            ``diff_summary``, ``branch``, ``pr_title``).

    Returns:
        JSON string with ``status="dry_run"``, ``confirmation_token``,
        and the *payload* fields.
    """
    conf_token = generate_token(
        operation=operation,
        details=details,
        container_id=cid,
        run_id=run_id,
    )
    record_boundary_crossing(
        cid,
        operation,
        details,
        approved=None,
        token=conf_token,
    )
    return json.dumps({
        "status": "dry_run",
        "confirmation_token": conf_token,
        **payload,
    })


def _consume_confirmation_token(
    token: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and consume a confirmation token for execution.

    Returns ``(token_meta, None)`` when the token is valid (and now
    consumed), or ``(None, error_json)`` with a ready-to-return error
    response when the token is missing, invalid, expired, or already used.
    """
    if not token:
        return None, json.dumps({
            "status": "error",
            "error": "Token required for execution.  Run with dry_run=True first.",
        })
    result = verify_and_consume(token)
    if result is None:
        return None, json.dumps({
            "status": "error",
            "error": "Token invalid, expired, or already used",
        })
    return result, None


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


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
    dry_run: bool = False,
    token: str = "",
    allow_force_push: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
) -> str:
    """Stage, commit, push, and optionally create a PR.

The **single exit tool** (design doc `docs/design.md` section 11.1).
Internally holds two push transports: ``git push`` with credential
helper, and GitHub Objects API (blob->tree->commit->ref) as
automatic fallback.  The transport choice is transparent to the
caller.

.. important::

   ``publish`` does **not** run verification — the design assumes the
   LLM calls :func:`verify_in_container` before ``publish`` as part of
   the **edit → verify → publish** workflow (see ``AGENTS.md``).

Two-step flow for boundary-crossing writes:

1. ``dry_run=True`` -- returns a diff summary and a confirmation
   token that must be approved before execution.  (The dry-run diff
   summary shows the push plan; :func:`verify_in_container`'s diff
   summary includes test results.)
2. ``dry_run=False`` + *token* -- verifies the token, stages/commits/pushes
   (and creates a PR if *create_pr* is ``True``).

Requires a container started with ``allow_network=True`` and
``inject_vcs_token=True``.

.. rubric:: Use when

- **Pushing changes** to a remote branch as the final step of the edit → verify → publish workflow
- **Creating a PR** from the pushed branch (use ``create_pr=True``)
- Squashing multiple local checkpoints into a single commit on push

.. rubric:: Don't use when

- **Running verification** — use :func:`verify_in_container` first; ``publish`` does not verify
- **Local-only save points** — use :func:`checkpoint` instead (no network, no token)
- **Pushing via GitHub Objects API only** — use :func:`sandbox_create_pr` (deprecated fallback)

.. rubric:: Prefer over

- Prefer over :func:`sandbox_create_pr` for the standard push+PR flow (two transports, no force-push by default)
- Prefer over manual ``git push`` in ``sandbox_exec`` (the token credential helper is pre-configured)

.. rubric:: Fallback

- If ``git push`` fails (token permissions), the GitHub Objects API transport is tried automatically
- If the API transport also fails, check that the container has ``inject_vcs_token=True``

Args:
    container_id: 12-character container ID prefix.
    repo: Repository in ``"owner/repo"`` format.
    branch: Branch name to push.
    message: Git commit message.
    working_dir: Directory in the container containing the git
        repository (default ``"/home/sandbox"``).
    create_pr: Whether to create a pull request after push.
    pr_title: PR title (required if ``create_pr=True``).
    pr_body: PR body (optional).
    base_branch: Base branch for the PR (default: repository
        default branch).
    dry_run: When ``True``, returns a diff summary and
        confirmation token instead of executing.
    token: Confirmation token from a previous ``dry_run`` call.
    allow_force_push: When ``True`` and needed, permits
        ``git push --force`` (opt-in; default ``False``).
    author_name: Git commit author name.  When set, takes precedence
        over the image-level default configured in
        ``docker/Dockerfile.base`` (``code-sandbox-mcp[bot]``).
        When ``None``, the image-level default is used.
    author_email: Git commit author email.  When set, takes precedence
        over the image-level default configured in
        ``docker/Dockerfile.base``
        (``code-sandbox-mcp[bot]@users.noreply.github.com``).
        When ``None``, the image-level default is used.

Returns:
    JSON string with operation result.
"""
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]
    run_id = get_or_create_run_id(cid)
    working_dir = resolve_git_root(container, working_dir)

    # Helper: run a shell command in the container in working_dir.
    def _run(cmd: str) -> tuple[int, str, str]:
        full_cmd = f"cd {shlex.quote(working_dir)} && {cmd}"
        ec, out = container.exec_run(
            ["/bin/sh", "-c", full_cmd],
            stdout=True,
            stderr=True,
        )
        out_stdout, out_stderr = (
            out if isinstance(out, tuple) else (out, b"")
        )
        stdout_text = (
            out_stdout.decode("utf-8", errors="replace") if out_stdout else ""
        )
        stderr_text = (
            out_stderr.decode("utf-8", errors="replace") if out_stderr else ""
        )
        return ec, stdout_text, stderr_text

    # ------------------------------------------------------------------
    # DRY RUN — show plan and generate token
    # ------------------------------------------------------------------
    if dry_run:
        # Gather diff summary (uncommitted changes)
        status_ec, status_out, status_err = _run(
            "git status --porcelain && echo '---DIFF---' && git diff HEAD --stat"
        )
        diff_summary = (status_out + "\n" + status_err).strip()

        # Check for unpushed checkpoints
        unpushed_ec, unpushed_out, _ = _run(
            "git log --oneline HEAD --not --remotes 2>/dev/null"
        )
        has_unpushed = unpushed_ec == 0 and unpushed_out.strip() != ""

        if (not diff_summary or diff_summary == "---DIFF---") and not has_unpushed:
            return json.dumps({
                "status": "dry_run",
                "diff_summary": "(no changes detected)",
                "branch": branch,
                "message": message,
                "warning": "No changes to commit.  Publish will succeed as a no-op.",
            })

        # Build diff_summary with checkpoint info if present
        if has_unpushed:
            checkpoint_lines = unpushed_out.strip().splitlines()
            checkpoint_info = f"\n---\nCheckpoints to squash: {len(checkpoint_lines)} commit(s)"
            if not diff_summary or diff_summary == "---DIFF---":
                diff_summary = f"(unpushed checkpoints){checkpoint_info}"
            else:
                diff_summary += checkpoint_info

        details = (
            f"repo={repo} branch={branch} message={message[:80]}"
        )
        if create_pr:
            details += f" pr_title={pr_title[:60]}"

        return _dry_run_response(
            "publish",
            cid,
            run_id,
            details,
            {
                "diff_summary": diff_summary,
                "branch": branch,
                "message": message,
                "create_pr": create_pr,
                "pr_title": pr_title if create_pr else None,
            },
        )

    # ------------------------------------------------------------------
    # EXECUTE — require token
    # ------------------------------------------------------------------
    if not token:
        return json.dumps({
            "status": "error",
            "error": "Token required for execution.  Run with dry_run=True first.",
        })

    # --- Consume token ---
    token_result, token_error = _consume_confirmation_token(token)
    if token_error is not None:
        return token_error

    # --- Git branch check/create ---
    _run(f"git checkout -b {shlex.quote(branch)} 2>/dev/null || git checkout {shlex.quote(branch)}")

    # --- Git add / commit ---
    add_ec, add_out, add_err = _run("git add -A")
    if add_ec != 0:
        return json.dumps({
            "status": "error",
            "step": "git_add",
            "error": add_err or add_out,
        })

    # --- Always squash unpushed checkpoints into a single commit ---
    track_ec, track_out, _ = _run("git rev-parse --abbrev-ref @{u} 2>/dev/null")
    if track_ec == 0 and track_out.strip():
        unpushed_ec, unpushed_out, _ = _run("git log --oneline @{u}..HEAD")
        if unpushed_ec == 0 and unpushed_out.strip():
            reset_ec, reset_out, reset_err = _run("git reset --soft @{u}")
            if reset_ec != 0:
                return json.dumps({
                    "status": "error",
                    "step": "squash_reset",
                    "error": reset_err or reset_out,
                })
            readd_ec, readd_out, readd_err = _run("git add -A")
            if readd_ec != 0:
                return json.dumps({
                    "status": "error",
                    "step": "squash_readd",
                    "error": readd_err or readd_out,
                })

    # --- Git identity: set before commit ---
    name_to_use = author_name if author_name is not None else "code-sandbox-mcp[bot]"
    email_to_use = author_email if author_email is not None else "code-sandbox-mcp[bot]@users.noreply.github.com"
    safe_name = shlex.quote(name_to_use)
    safe_email = shlex.quote(email_to_use)
    git_commit_cmd = (
        f"git -c user.name={safe_name} -c user.email={safe_email} commit -m {shlex.quote(message)}"
    )

    commit_ec, commit_out, commit_err = _run(git_commit_cmd)
    if commit_ec != 0:
        # No changes to commit is OK if everything is already committed
        if "nothing to commit" in (commit_out + commit_err).lower():
            pass
        else:
            return json.dumps({
                "status": "error",
                "step": "git_commit",
                "error": commit_err or commit_out,
            })

    # --- Git push (with transport fallback to API push) ---
    force_flag = " --force" if allow_force_push else ""
    push_cmd = (
        f"git -c credential.helper= "
        f"-c credential.helper='!f() {{ echo username=x-access-token; echo password=$GITHUB_TOKEN; }}; f' "
        f"push origin {shlex.quote(branch)}{force_flag}"
    )
    push_ec, push_out, push_err = _run(push_cmd)

    # Get the SHA of the pushed commit
    sha = ""
    sha_ec, sha_out, _ = _run("git rev-parse HEAD")
    if sha_ec == 0:
        sha = sha_out.strip()[:7]

    # Transport fallback: git push failed -> try GitHub API push
    if push_ec != 0:
        push_result = _try_api_push(
            container, cid, repo, branch, working_dir
        )
        if push_result.get("status") == "ok":
            sha = push_result.get("sha", sha)
            push_ec = 0  # mark success for downstream logic
        else:
            record_boundary_crossing(
                cid,
                "publish",
                f"repo={repo} branch={branch} push_failed transport=both",
                approved=False,
                token=token,
            )
            return json.dumps({
                "status": "error",
                "step": "git_push",
                "error": push_err or push_out,
                "sha": sha,
            })
    # --- Optionally create PR ---
    pr_url: str | None = None
    if create_pr:
        pr_cmd = (
            f"gh pr create --repo {shlex.quote(repo)}"
            f" --head {shlex.quote(branch)}"
            f" --title {shlex.quote(pr_title)}"
        )
        if pr_body:
            body_encoded = base64.b64encode(
                pr_body.encode("utf-8")
            ).decode("ascii")
            pr_cmd = (
                f"BODY_FILE=$(mktemp) &&"
                f" echo {shlex.quote(body_encoded)} | base64 -d > \"$BODY_FILE\" &&"
                f" gh pr create --repo {shlex.quote(repo)}"
                f" --head {shlex.quote(branch)}"
                f" --title {shlex.quote(pr_title)}"
                f" --body-file \"$BODY_FILE\""
                f'; rm -f "$BODY_FILE"'
            )
        else:
            pr_cmd += " --body ''"
        if base_branch:
            pr_cmd += f" --base {shlex.quote(base_branch)}"

        pr_ec, pr_out, pr_err = _run(pr_cmd)
        if pr_ec != 0:
            # Push succeeded but PR creation failed — still record push
            record_boundary_crossing(
                cid,
                "publish",
                f"repo={repo} branch={branch} sha={sha} pr_create_failed",
                approved=True,
                token=token,
            )
            return json.dumps({
                "status": "pushed",
                "branch": branch,
                "sha": sha,
                "pr_create_error": pr_err or pr_out,
            })

        # Extract PR URL from gh output
        for line in (pr_out + pr_err).splitlines():
            line = line.strip()
            if line.startswith("https://github.com/"):
                pr_url = line
                break

    # --- Success ---
    details = f"repo={repo} branch={branch} sha={sha}"
    if pr_url:
        details += f" pr_url={pr_url}"

    record_boundary_crossing(
        cid,
        "publish",
        details,
        approved=True,
        token=token,
    )

    result: dict[str, Any] = {
        "status": "pushed",
        "branch": branch,
        "sha": sha,
    }
    if pr_url:
        result["pr_url"] = pr_url

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Internal transport: GitHub Objects API push (used by publish as fallback)
# ---------------------------------------------------------------------------


def _try_api_push(
    container: Any,
    cid: str,
    repo: str,
    branch: str,
    working_dir: str,
) -> dict[str, str]:
    """Push HEAD via GitHub Objects API (blob->tree->commit->ref).

    Returns ``{"status": "ok", "sha": "<sha>"}`` on success,
    or ``{"status": "error", "error": "..."}`` on failure.
    """
    script_b64 = base64.b64encode(
        _SANDBOX_CREATE_PR_SCRIPT.encode("utf-8")
    ).decode("ascii")

    def _run(cmd: str) -> tuple[int, str, str]:
        ec, out = container.exec_run(
            ["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=working_dir,
        )
        stdout_b, stderr_b = out or (b"", b"")
        out_text = stdout_b.decode("utf-8", errors="replace").strip() if stdout_b else ""
        err_text = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
        return ec, out_text, err_text

    _run(f"echo {shlex.quote(script_b64)} | base64 -d > /tmp/_sandbox_create_pr.py")

    ec, out, err = _run(
        f"trap 'rm -f /tmp/_sandbox_create_pr.py' EXIT"
        f" && python3 /tmp/_sandbox_create_pr.py {shlex.quote(repo)} {shlex.quote(branch)} {shlex.quote(working_dir)}"
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




# ---------------------------------------------------------------------------
# sandbox_create_pr
# ---------------------------------------------------------------------------


def sandbox_create_pr(
    container_id: str,
    repo: str,
    branch: str,
    pr_title: str,
    pr_body: str = "",
    base_branch: str = "",
    working_dir: str | None = None,
    dry_run: bool = False,
    token: str = "",
) -> str:
    """[DEPRECATED] Push the current branch via GitHub API and create a PR.

    .. deprecated::
       This function is **no longer registered as an MCP tool** (see
       issue #256).  It remains as an internal fallback for the
       GitHub Objects API push path within :func:`publish`.  Use
       :func:`publish` instead.

    Unlike :func:`publish`, this tool uses the GitHub Objects API
    (blob → tree → commit → ref) to push the branch, which works when
    the injected token cannot push via HTTPS git (e.g. GitHub App
    installation tokens).

    Typical flow:

    1. ``sandbox_initialize(allow_network=True, inject_vcs_token=True)``
    2. ``clone_repo`` / ``gh repo clone`` inside the container
    3. Make changes, run ``git add -A && git commit``
    4. ``sandbox_create_pr(dry_run=True)`` — preview + confirmation token
    5. ``sandbox_create_pr(token=...)`` — pushes via API + opens PR

    Like :func:`publish`, execution is a two-step flow: call once with
    ``dry_run=True`` to get a preview of the HEAD commit and a
    confirmation token, then call again with that ``token`` to push and
    open the PR.

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True``.

    .. note::

       Only the most recent committed state (HEAD) is pushed.  Multiple
       local commits are represented as a single commit on GitHub whose
       tree matches the HEAD tree and whose parent is the current tip of
       *branch* (or the default branch if *branch* is new).

    .. warning::

       Unlike :func:`publish`, this tool has **no verify gate** (no
       lint/type-check/test run before push).  Ensure the sandbox passes
       all checks before calling this tool.

    .. warning::

       This tool uses ``PATCH`` with ``force: true`` when updating an
       existing branch, which will **force-push** and overwrite any
       commits on *branch* that are not reflected in the container's HEAD
       tree.  Do not use this tool on shared branches where others may
       have pushed commits.

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``'owner/repo'`` format.
        branch: Branch name to create or update on GitHub.
        pr_title: Title for the pull request.
        pr_body: Body text for the pull request (optional).
        base_branch: Base branch for the PR (default: repository default
            branch).
        working_dir: Directory in the container containing the git
            repository (default ``'/home/sandbox'``).
        dry_run: When ``True``, returns a preview of the HEAD commit that
            would be pushed plus a confirmation token, without pushing or
            creating a PR.
        token: Confirmation token from a previous ``dry_run`` call.
            Required for execution (``dry_run=False``).

    Returns:
        JSON with ``status``, ``pr_url``, ``branch``, and ``sha``.
        On error returns ``status='error'`` with an ``error`` field.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]
    working_dir = resolve_git_root(container, working_dir)

    if not _REPO_FORMAT_RE.match(repo):
        return json.dumps({
            "status": "error",
            "error": "repo must be in owner/repo format",
        })

    if not _BRANCH_RE.match(branch):
        return json.dumps({
            "status": "error",
            "error": "branch contains invalid characters",
        })

    if base_branch and not _BRANCH_RE.match(base_branch):
        return json.dumps({
            "status": "error",
            "error": "base_branch contains invalid characters",
        })

    def _run(cmd: str) -> tuple[int, str, str]:
        exit_code, output = container.exec_run(
            ["sh", "-c", cmd],
            stdout=True,
            stderr=True,
            demux=True,
            workdir=working_dir,
        )
        stdout_b, stderr_b = output or (b"", b"")
        out = stdout_b.decode("utf-8", errors="replace").strip() if stdout_b else ""
        err = stderr_b.decode("utf-8", errors="replace").strip() if stderr_b else ""
        return exit_code, out, err

    run_id = get_or_create_run_id(cid)

    # ------------------------------------------------------------------
    # DRY RUN — preview push plan and generate confirmation token
    # ------------------------------------------------------------------
    if dry_run:
        # HEAD is what gets pushed (see note above): preview that commit.
        _ec, log_out, _ = _run("git log -1 --pretty='%h %s' HEAD")
        _ec2, stat_out, _ = _run("git show --stat --pretty='' HEAD")
        diff_summary = (log_out + "\n" + stat_out).strip()
        if not diff_summary:
            diff_summary = "(no committed HEAD to push)"

        details = (
            f"repo={repo} branch={branch} pr_title={pr_title[:60]}"
            f" base={base_branch or 'default'}"
        )
        return _dry_run_response(
            "sandbox_create_pr",
            cid,
            run_id,
            details,
            {
                "diff_summary": diff_summary,
                "branch": branch,
                "pr_title": pr_title,
                "base_branch": base_branch or None,
            },
        )

    # ------------------------------------------------------------------
    # EXECUTE — require confirmation token from a prior dry_run
    # ------------------------------------------------------------------
    _token_meta, token_error = _consume_confirmation_token(token)
    if token_error is not None:
        return token_error

    # Write the API-push script into the container
    script_b64 = base64.b64encode(
        _SANDBOX_CREATE_PR_SCRIPT.encode("utf-8")
    ).decode("ascii")
    _run(f"echo {shlex.quote(script_b64)} | base64 -d > /tmp/_sandbox_create_pr.py")

    # Execute: push via GitHub API
    ec, out, err = _run(
        f"trap 'rm -f /tmp/_sandbox_create_pr.py' EXIT"
        f" && python3 /tmp/_sandbox_create_pr.py {shlex.quote(repo)} {shlex.quote(branch)} {shlex.quote(working_dir)}"
    )
    if ec != 0:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push error={(err or out)[:200]}",
            approved=False, token=token,
        )
        # err or out — script crashed (ec ≠ 0), stderr has traceback
        return json.dumps({"status": "error", "step": "api_push", "error": err or out})

    try:
        push_result = json.loads(out)
    except json.JSONDecodeError:
        # json_parse_error: out or err — script exited 0 but stdout isn't JSON.
        # stdout is the diagnostic (what the script printed instead of JSON)
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push json_parse_error",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", "error": out or err})

    if "error" in push_result:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push error={push_result.get('error', ''):.200}",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", **push_result})

    new_sha = push_result["sha"]

    # Create PR
    pr_cmd = (
        f"gh pr create --repo {shlex.quote(repo)}"
        f" --head {shlex.quote(branch)}"
        f" --title {shlex.quote(pr_title)}"
    )
    if base_branch:
        pr_cmd += f" --base {shlex.quote(base_branch)}"
    if pr_body:
        body_b64 = base64.b64encode(pr_body.encode("utf-8")).decode("ascii")
        # Wrap pr_cmd: write body to a temp file, run pr_cmd with --body-file, then clean up
        base_cmd = pr_cmd
        pr_cmd = (
            f"BODY_FILE=$(mktemp) &&"
            f" trap 'rm -f \"$BODY_FILE\"' EXIT &&"
            f" echo {shlex.quote(body_b64)} | base64 -d > \"$BODY_FILE\" &&"
            f" {base_cmd} --body-file \"$BODY_FILE\""
        )

    pr_ec, pr_out, pr_err = _run(pr_cmd)
    if pr_ec != 0:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} sha={new_sha[:7]} step=pr_create error={(pr_err or pr_out)[:200]}",
            approved=True, token=token,
        )
        return json.dumps({
            "status": "pushed_no_pr",
            "branch": branch,
            "sha": new_sha[:7],
            # pr_err or pr_out — gh writes errors to stderr
            "pr_create_error": pr_err or pr_out,
        })

    pr_url = ""
    _pr_url_marker = f"github.com/{repo}/pull/"
    for line in pr_out.splitlines():
        line = line.strip()
        if _pr_url_marker in line and line.startswith("https://"):
            pr_url = line
            break

    record_boundary_crossing(
        cid,
        "sandbox_create_pr",
        f"repo={repo} branch={branch} sha={new_sha[:7]} pr_url={pr_url}",
        approved=True,
        token="",
    )

    return json.dumps({
        "status": "ok",
        "branch": branch,
        "sha": new_sha[:7],
        "pr_url": pr_url,
    })


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------


def clone_repo(
    container_id: str,
    repo: str,
    dest_dir: str = "/home/sandbox",
    branch: str = "",
) -> str:
    """Clone a Git repository inside the container using ``gh repo clone``.

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True`` for private repositories.

    .. hint::

       To avoid the two-step "init → clone" workflow, use
       :func:`sandbox_initialize` with ``clone_repo`` — it starts
       the container and copies a pre-cloned Shiori repo in one call.

    .. rubric:: Use when

    - You already have a running container and need to clone an additional repository
    - You need to clone a specific branch (``branch`` parameter)

    .. rubric:: Don't use when

    - **Starting a new container** — use :func:`sandbox_initialize` with ``clone_repo`` instead (one-step init + clone)
    - **Cloning a PR branch** — use :func:`sandbox_initialize` with ``pr=N`` instead (auto network + token)
    - **One-shot workflows** — use :func:`run_container_and_exec` with ``clone_repo`` instead

    .. rubric:: Prefer over

    - Prefer over ``sandbox_exec`` + ``gh repo clone`` for VCS-authenticated clones (token injection handled automatically)
    - Prefer over ``clone_repo`` when starting a new container — use ``sandbox_initialize(clone_repo=...)`` instead

    .. rubric:: Fallback

    - If ``clone_repo`` fails with a private repo, ensure the container was started with ``inject_vcs_token=True``

    Args:
        container_id: 12-character container ID prefix.
        repo: Repository in ``"owner/repo"`` format.
        dest_dir: Parent directory in the container.  The repo is cloned
            into ``{dest_dir}/{repo_name}`` (default parent
            ``"/home/sandbox"``).
        branch: Branch name to clone. Omit for the default branch.

    Returns:
        JSON string with ``status``, ``repo``, ``clone_path``, and
        ``branch``.  On error returns an ``error`` field.

    See also:
        :func:`sandbox_initialize` — one-step init + clone with
        ``clone_repo`` parameter.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return json.dumps({"error": f"Container {container_id[:12]} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})

    cid = container_id[:12]

    if not _REPO_FORMAT_RE.match(repo):
        return json.dumps(
            {"error": f"Invalid repo format: {repo} (expected owner/repo)"}
        )

    # ``gh repo clone`` treats its second argument as the clone target
    # directory itself, not a parent.  Clone into ``{dest_dir}/{repo_name}``
    # so the default ``dest_dir`` (``/home/sandbox``, an existing non-empty
    # home directory) works and the target matches the reported
    # ``clone_path``.
    repo_name = repo.split("/")[-1]
    clone_path = f"{dest_dir.rstrip('/')}/{repo_name}"

    safe_target = shlex.quote(clone_path)
    safe_repo = shlex.quote(repo)

    # Best-effort: configure gh as git credential helper so that
    # ``git push`` works with the injected token.  Failure is intentionally
    # ignored — when inject_vcs_token=False (no GH_TOKEN in env) the command
    # exits non-zero but cloning public repos still succeeds.  If push later
    # fails with an auth error the cause will be clear from that message.
    _auth_ec, _ = container.exec_run(
        ["/bin/sh", "-c", "gh auth setup-git"],
        stdout=True,
        stderr=True,
    )
    del _auth_ec  # intentionally ignored; see comment above

    if branch:
        cmd = (
            f"gh repo clone {safe_repo} {safe_target}"
            f" -- -b {shlex.quote(branch)}"
        )
    else:
        cmd = f"gh repo clone {safe_repo} {safe_target}"

    exit_code, output = container.exec_run(
        ["/bin/sh", "-c", cmd],
        stdout=True,
        stderr=True,
    )

    stdout_part, stderr_part = (
        output if isinstance(output, tuple) else (output, b"")
    )
    stdout_text = (
        stdout_part.decode("utf-8", errors="replace") if stdout_part else ""
    )
    stderr_text = (
        stderr_part.decode("utf-8", errors="replace") if stderr_part else ""
    )

    if exit_code != 0:
        error_text = stderr_text or stdout_text
        # Surface a clearer hint when the target already exists, instead of
        # the bare git "already exists and is not an empty directory" message.
        if "already exists" in error_text:
            error_text = (
                f"{error_text.rstrip()}\n"
                f"Hint: {repr(clone_path)} already exists. Specify a different "
                f"dest_dir, or remove the existing directory first."
            )
        return json.dumps({
            "status": "error",
            "error": error_text,
            "clone_path": clone_path,
        })

    record_boundary_crossing(
        cid,
        "clone_repo",
        f"repo={repo} branch={branch or 'default'} dest={clone_path}",
        approved=True,
    )

    return json.dumps({
        "status": "ok",
        "repo": repo,
        "clone_path": clone_path,
        "branch": branch or "default",
    })
