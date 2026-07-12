"""VCS tools: issue_view, publish, sandbox_issue_write, clone_repo."""

from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import re
import shlex
from typing import Any

from docker.errors import NotFound

from sunaba import github_auth, proxy_lifecycle, token_broker
from sunaba.journal import record_boundary_crossing, record_tool_use
from sunaba.proxy_client import (
    ProxyAuthError,
    authorized_push_grant,
    authorized_read_grant,
    proxy_configured,
)
from sunaba.tools.common import (
    CLONE_NO_TOKEN_WARNING,
    _build_clone_command,
    _docker,
    container_not_found_error,
)
from sunaba.verify_state import has_verify_success

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
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

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
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    record_tool_use(container_id[:12], "checkpoint_list")

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
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

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

    Fetches the issue host-side via the REST API (like :func:`publish`'s PR
    creation, #360) and writes the body into the container.  The LLM
    receives only a summary + handle (file path and size); full text can be
    retrieved with :func:`read_file_range`.

    Because the network hop happens on the host, this works on **any**
    container -- ``allow_network`` is not required.
    This also fixes the tool for egress-proxied containers: the previous
    in-container ``gh issue view`` needed a container-side token, which the
    proxy deliberately never provides (#356), so it failed outright there.

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
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    try:
        issue_data = _github_api_request(
            f"/repos/{repo}/issues/{issue_number}", _resolve_vcs_token()
        )
    except RuntimeError as e:
        return json.dumps({"status": "error", "error": f"Failed to fetch issue #{issue_number} from {repo}: {e}"})

    number = issue_data.get("number", issue_number)
    title = issue_data.get("title", "")
    body = issue_data.get("body") or ""

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
        return json.dumps({"status": "error", "error": f"Failed to write issue body to {save_to}"})

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
# sandbox_issue_write -- first-class, host-side issue create/comment (#414)
# ---------------------------------------------------------------------------


_ISSUE_WRITE_METHODS = ("create", "comment")


def sandbox_issue_write(
    container_id: str,
    repo: str,
    method: str,
    title: str = "",
    body: str = "",
    issue_number: int | None = None,
) -> str:
    """Create a GitHub issue or comment on one -- host-side, no in-container gh.

    First-class tool for the non-push write operations #360 identified as a
    blind spot: under the egress proxy, in-container ``gh issue create`` /
    ``gh issue comment`` have no credential to use (#356) and would need the
    proxy to blanket-allow api.github.com writes, defeating the point of
    gating push. Executes in a single call (the dry_run/confirmation-token
    two-step was retired for V1.0, matching :func:`publish`) and calls the
    GitHub REST API directly from the
    host (:func:`_github_api_request`, the pattern established by
    :func:`_create_pr_via_api` and :func:`issue_view`, #409/#413) -- so no
    token ever needs to reach the container, and the call does not depend on
    the container having network access at all.

    .. rubric:: Use when

    - Creating a new GitHub issue from inside a sandbox session
    - Adding a comment to an existing issue or PR (the issue-comment API
      covers both)

    .. rubric:: Don't use when

    - **Reading an issue** -- use :func:`issue_view` instead (it is read-only)
    - **Pushing code or creating a PR** -- use :func:`publish` instead

    Args:
        container_id: 12-character container ID prefix.  Used for the
            journal trail, exactly like :func:`publish`; the container's own
            network/token state is irrelevant since the GitHub call happens
            host-side.
        repo: Repository in ``"owner/repo"`` format.
        method: ``"create"`` (new issue) or ``"comment"`` (comment on an
            existing issue or PR number).
        title: Issue title.  Required when *method* is ``"create"``.
        body: Issue or comment body (optional for ``"create"``, the actual
            content for ``"comment"``).
        issue_number: Issue or PR number to comment on.  Required when
            *method* is ``"comment"``, ignored for ``"create"``.

    Returns:
        JSON string with ``status``, and on success ``html_url`` (issue or
        comment URL) plus ``number`` (for ``"create"``).  On error returns
        an ``error`` field.
    """
    if method not in _ISSUE_WRITE_METHODS:
        return json.dumps({"status": "error", "error": f"Invalid method: {method!r} (expected 'create' or 'comment')"})
    if not _REPO_FORMAT_RE.match(repo):
        return json.dumps({"status": "error", "error": f"Invalid repo format: {repo} (expected owner/repo)"})
    if method == "create" and not title:
        return json.dumps({"status": "error", "error": "title is required when method='create'"})
    if method == "comment" and not issue_number:
        return json.dumps({"status": "error", "error": "issue_number is required when method='comment'"})

    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    if method == "create":
        details = f"repo={repo} issue_create title={title[:80]}"
    else:
        details = f"repo={repo} issue_comment number=#{issue_number} body={body[:80]}"

    push_token = _resolve_vcs_token()
    if not push_token:
        return json.dumps({
            "status": "error",
            "error": "No host-side GitHub token available (GITHUB_TOKEN / broker); "
                     "issue write requires one regardless of container state.",
        })

    try:
        if method == "create":
            result = _github_api_request(
                f"/repos/{repo}/issues",
                push_token,
                method="POST",
                payload={"title": title, "body": body} if body else {"title": title},
            )
        else:
            result = _github_api_request(
                f"/repos/{repo}/issues/{issue_number}/comments",
                push_token,
                method="POST",
                payload={"body": body},
            )
    except RuntimeError as e:
        record_boundary_crossing(cid, "issue_write", f"{details} failed", approved=False)
        return json.dumps({"status": "error", "error": str(e)})

    record_boundary_crossing(cid, "issue_write", details, approved=True)

    response: dict[str, Any] = {
        "status": "ok",
        "html_url": result.get("html_url", ""),
    }
    if method == "create":
        response["number"] = result.get("number")
    return json.dumps(response)


# ---------------------------------------------------------------------------
# sandbox_pr_review_write -- host-side PR review create+submit one-shot (#477)
# ---------------------------------------------------------------------------

_PR_REVIEW_EVENTS = ("APPROVE", "REQUEST_CHANGES", "COMMENT")


def sandbox_pr_review_write(
    container_id: str,
    repo: str,
    pr: int,
    event: str,
    body: str = "",
    comments: list[dict[str, Any]] | None = None,
) -> str:
    """Create and submit a PR review in one shot -- host-side, no in-container gh.

    One-shot PR review submission following the host-side pattern established
    by :func:`sandbox_issue_write` (#414): the GitHub REST API is called
    directly from the host process, so no token ever reaches the container.
    Creates a review with optional inline comments and submits it in a single
    API call.

    .. rubric:: Use when

    - Submitting a PR review (approve, request changes, or comment) from
      inside a sandbox session
    - Adding inline code review comments alongside the review body

    .. rubric:: Don't use when

    - **Managing review threads** (resolve/unresolve) -- use GitHub MCP
    - **Commenting on an issue or PR** without a review -- use
      :func:`sandbox_issue_write` with ``method="comment"`` instead

    Args:
        container_id: 12-character container ID prefix.  Used for the
            journal trail; the container's own network/token state is
            irrelevant since the GitHub call happens host-side.
        repo: Repository in ``\"owner/repo\"`` format.
        pr: PR number to review.
        event: ``\"APPROVE\"``, ``\"REQUEST_CHANGES\"``, or ``\"COMMENT\"``.
            ``REQUEST_CHANGES`` and ``APPROVE`` fail with 422 when the
            review is created by the same GitHub App token that owns the
            PR; use ``COMMENT`` in that case.
        body: Optional review body text (default ``\"\"``).
        comments: Optional list of inline comment dicts, each with:
            - ``path`` (str): file path
            - ``line`` (int): line number
            - ``side`` (str, optional): ``\"LEFT\"`` or ``\"RIGHT\"``
              (default ``\"RIGHT\"``)
            - ``body`` (str): comment text

    Returns:
        JSON string with ``status``, ``html_url`` (review URL), and
        ``review_id``.  On error returns an ``error`` field.
    """
    if event not in _PR_REVIEW_EVENTS:
        return json.dumps({
            "status": "error",
            "error": f"Invalid event: {event!r} (expected APPROVE, REQUEST_CHANGES, or COMMENT)",
        })
    if not _REPO_FORMAT_RE.match(repo):
        return json.dumps({"status": "error", "error": f"Invalid repo format: {repo} (expected owner/repo)"})
    if pr < 1:
        return json.dumps({"status": "error", "error": f"Invalid PR number: {pr}"})

    if comments is not None:
        for i, c in enumerate(comments):
            if not isinstance(c, dict):
                return json.dumps({
                    "status": "error",
                    "error": f"Invalid comment at index {i}: expected a dict, got {type(c).__name__}",
                })
            if "path" not in c:
                return json.dumps({
                    "status": "error",
                    "error": f"Comment at index {i} is missing required key 'path'",
                })
            if "body" not in c:
                return json.dumps({
                    "status": "error",
                    "error": f"Comment at index {i} is missing required key 'body'",
                })

    client = _docker()
    try:
        client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]
    details = f"repo={repo} pr=#{pr} event={event}"

    push_token = _resolve_vcs_token()
    if not push_token:
        return json.dumps({
            "status": "error",
            "error": "No host-side GitHub token available (GITHUB_TOKEN / broker); "
                     "PR review write requires one regardless of container state.",
        })

    # Resolve PR head SHA for inline comment commit_id
    try:
        pr_data = _github_api_request(f"/repos/{repo}/pulls/{pr}", push_token)
    except RuntimeError as e:
        record_boundary_crossing(cid, "pr_review_write", f"{details} failed to fetch PR data", approved=False)
        return json.dumps({"status": "error", "error": f"Failed to fetch PR #{pr}: {e}"})

    head_sha = pr_data.get("head", {}).get("sha", "")
    if not head_sha:
        record_boundary_crossing(cid, "pr_review_write", f"{details} no head sha", approved=False)
        return json.dumps({"status": "error", "error": f"Could not resolve head SHA for PR #{pr}"})

    payload: dict[str, Any] = {
        "body": body,
        "event": event,
        "commit_id": head_sha,
    }
    if comments:
        payload["comments"] = comments

    try:
        result = _github_api_request(
            f"/repos/{repo}/pulls/{pr}/reviews",
            push_token,
            method="POST",
            payload=payload,
        )
    except RuntimeError as e:
        record_boundary_crossing(cid, "pr_review_write", f"{details} failed", approved=False)
        err_msg = str(e)
        _OWN_PR_INDICATORS = (
            "can not request changes on your own",
            "cannot request changes on your own",
            "can not approve your own",
            "cannot approve your own",
        )
        if event in ("REQUEST_CHANGES", "APPROVE") and any(i in err_msg.lower() for i in _OWN_PR_INDICATORS):
            return json.dumps({
                "status": "error",
                "error": (
                    "Cannot submit event={!r} on a pull request owned by the same GitHub App token. "
                    "Use event=\"COMMENT\" instead, or have a different user review the PR. "
                    "GitHub API: {}".format(event, err_msg)
                ),
            })
        return json.dumps({"status": "error", "error": err_msg})

    record_boundary_crossing(cid, "pr_review_write", details, approved=True)

    return json.dumps({
        "status": "ok",
        "html_url": result.get("html_url", ""),
        "review_id": result.get("id"),
    })


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def _resolve_vcs_token() -> str:
    """Resolve a VCS token host-side for lazy injection at call time (Issue #347).

    The token is *not* bound to container start: the host (this MCP server
    process) can always obtain it.  Returning it here lets callers such as
    :func:`publish` inject the credential into a single ``docker exec`` (a
    push) — instead of requiring the token to have been baked into the
    container's environment at ``sandbox_initialize`` time.  This removes the
    "no-token start → must re-init to push" penalty while keeping
    least-privilege: containers that never need a credential never receive one.

    Despite the name's push-era origin, this is a general host-side token
    resolver: it also backs authenticated host->GitHub-API GET calls
    (``_resolve_pr_head_ref``, ``issue_write``) and, since #419, egress-proxy
    read-authorization grants (``authorized_read_grant``) for
    ``clone_repo``/``sandbox_initialize``. There is nothing push-specific in
    its resolution order (broker mint -> static ``GITHUB_TOKEN``/``GH_TOKEN``).

    Resolution order: a freshly minted broker token (Issue #232) takes
    precedence, then the global ``AppTokenProvider`` (issue #474), then
    the static host ``GITHUB_TOKEN`` / ``GH_TOKEN``.  Returns an empty
    string when no token is available, in which case the push has no
    credential and fails cleanly -- the container carries none of its
    own (#356/#439).

    Note: the ``AppTokenProvider.get_token()`` step can raise
    ``RuntimeError`` if the GitHub API is unreachable *and* no
    previously-cached token is usable — this is intentional: the caller
    should surface the failure rather than silently falling back to a
    stale env var.  Broker mint and env var reads never raise.
    """
    minted = token_broker.mint_token()
    if minted:
        return minted
    provider = github_auth.get_global_provider()
    if provider is not None:
        token = provider.get_token()
        if token:
            return token
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        val = os.environ.get(key)
        if val:
            return val
    return ""


def _push_token_env(token: str) -> dict[str, str] | None:
    """Build the ephemeral exec environment carrying *token*, or ``None``.

    The returned mapping is passed only to the ``docker exec`` calls that
    actually need credentials (git push, ``gh pr create``, the API-push
    fallback).  Because it lives solely in that exec's process environment
    it leaves nothing behind in the container — no env var on the long-lived
    container, no file, no credential store (Issue #347 ephemerality).

    Docker's exec ``Env`` is **additive**: these vars are merged onto the
    container's existing environment rather than replacing it, so ``PATH`` /
    ``HOME`` stay intact and ``git`` / ``gh`` / ``python3`` still resolve
    inside the push exec (verified against docker-py 7.1.0 / Docker exec
    semantics).  We therefore only need to carry the token here, not a full
    environment.
    """
    if not token:
        return None
    return {"GITHUB_TOKEN": token, "GH_TOKEN": token}


def _github_api_request(
    path: str,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call the GitHub REST API from the host process; return the JSON body.

    Runs host-side like :func:`_resolve_vcs_token` — never inside the
    container — so no credential enters the sandbox and the request does not
    traverse the egress proxy (#360).  The REST API accepts ``Bearer``; the
    Basic-only quirk applies to git smart-HTTP endpoints only (PR #404).

    *token* may be empty for an anonymous request (e.g. a public-repo read
    such as :func:`issue_view`); no ``Authorization`` header is sent in that
    case, rather than one carrying an empty bearer value.

    Raises:
        RuntimeError: On an HTTP error (carrying GitHub's ``message`` /
            ``errors`` when present; raw body as fallback when JSON
            parsing fails) or an unreachable network.
    """
    import urllib.error
    import urllib.request

    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "sunaba")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        parts: list[str] = []
        raw_body = ""
        try:
            raw_body = e.read().decode("utf-8", errors="replace")
            body = json.loads(raw_body)
            if body.get("message"):
                parts.append(str(body["message"]))
            parts.extend(str(err.get("message", err)) for err in body.get("errors") or [])
        except Exception:  # noqa: BLE001 - the error body is diagnostics only
            if raw_body:
                parts.append(f"raw body: {raw_body[:500]}")
        detail = f": {'; '.join(parts)}" if parts else ""
        raise RuntimeError(
            f"GitHub API {method} {path} returned HTTP {e.code}{detail}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"GitHub API {method} {path} failed: {e.reason}") from e


def _create_pr_via_api(
    repo: str,
    branch: str,
    pr_title: str,
    pr_body: str,
    base_branch: str,
    token: str,
) -> str:
    """Create a pull request host-side via the REST API; return its URL.

    Replaces the in-container ``gh pr create`` exec (#360): PR creation is a
    non-push write on ``api.github.com``, so running it here keeps the
    container credential-free — that exec was the last one carrying an
    ephemeral token — and stays out of the proxy's write gate.

    Also makes *base_branch* actually work: the old shell wrapper appended
    ``--base`` after the temp-file cleanup command, so gh never saw it and a
    stacked PR silently targeted the default branch.

    Raises:
        RuntimeError: When the base branch cannot be determined or the API
            call fails (propagated from :func:`_github_api_request`).
    """
    base = base_branch or str(
        _github_api_request(f"/repos/{repo}", token).get("default_branch") or ""
    )
    if not base:
        raise RuntimeError(f"could not determine the default branch of {repo}")
    payload: dict[str, Any] = {"title": pr_title, "head": branch, "base": base}
    if pr_body:
        payload["body"] = pr_body
    created = _github_api_request(
        f"/repos/{repo}/pulls", token, method="POST", payload=payload
    )
    url = str(created.get("html_url") or "")
    if not url:
        raise RuntimeError("GitHub API created the PR but returned no html_url")
    return url


def _ensure_proxy_ready(client: Any) -> str | None:
    """Reconcile the egress-proxy sidecar before a grant is opened against it.

    ``ensure_egress_proxy`` is idempotent and cheap on the happy path (a
    ``docker inspect`` of the running sidecar), so ``publish``/``clone_repo``
    simply re-run it rather than inferring the sidecar's state:

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

Executes in a single call: stages, commits, pushes, and (when
*create_pr* is ``True``) opens the PR.  There is no dry-run /
confirmation-token step -- the human gate is the MCP client's own
tool-approval prompt and the egress proxy is the structural guard, so
an in-band token would only add ceremony (V1.0 API cleanup).

Requires a container started with ``allow_network=True``.  No VCS token
needs to reach the container: the push credential is resolved host-side
at call time (Issue #347) and — with the egress proxy configured (#356)
— injected by the proxy into the authorized push only, so the container
never holds it.  PR creation likewise runs host-side (#360).

.. rubric:: Use when

- **Pushing changes** to a remote branch as the final step of the edit → verify → publish workflow
- **Creating a PR** from the pushed branch (use ``create_pr=True``)
- Squashing multiple local checkpoints into a single commit on push

.. rubric:: Don't use when

- **Running verification** — use :func:`verify_in_container` first; ``publish`` does not verify
- **Local-only save points** — use :func:`checkpoint` instead (no network, no push)

.. rubric:: Prefer over

- Prefer over manual ``git push`` in ``sandbox_exec`` (the push credential helper is pre-configured)

.. rubric:: Fallback

- If ``git push`` fails (token permissions), the GitHub Objects API transport is tried automatically
- If the API transport also fails, check that a host-side token is available (``GITHUB_TOKEN`` / broker)

Args:
    container_id: 12-character container ID prefix.
    repo: Repository in ``"owner/repo"`` format.
    branch: Branch name to push.
    message: Git commit message.
    working_dir: Directory in the container containing the git
        repository (default ``None`` = auto-detect).
    create_pr: Whether to create a pull request after push.
    pr_title: PR title (required if ``create_pr=True``).
    pr_body: PR body (optional).
    base_branch: Base branch for the PR (default: repository
        default branch).
    allow_force_push: When ``True`` and needed, permits
        ``git push --force`` (opt-in; default ``False``).
    author_name: Git commit author name.  When set, takes precedence
        over the image-level default configured in
        ``docker/Dockerfile.base`` (``sunaba[bot]``).
        When ``None``, the image-level default is used.
    author_email: Git commit author email.  When set, takes precedence
        over the image-level default configured in
        ``docker/Dockerfile.base``
        (``sunaba[bot]@users.noreply.github.com``).
        When ``None``, the image-level default is used.

Returns:
    JSON string with operation result.
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

    # State-conditioned nudge (Issue #550): when no successful
    # verify_in_container is recorded for this container in this server
    # session, every outcome of this call carries an advisory warning.
    # Never blocks -- publish proceeds exactly as before.
    verified = has_verify_success(cid)

    def _finish(payload: dict[str, Any]) -> str:
        if not verified:
            payload["warning"] = (
                "no successful verify_in_container recorded for this "
                "container in this server session"
            )
            payload["recommended_next_action"] = "verify_in_container"
        return json.dumps(payload)

    # Helper: run a shell command in the container in working_dir.
    # *env* carries a lazily-injected VCS token (Issue #347) for the push /
    # PR execs only; it is ``None`` for the read-only git commands so no
    # credential is ever exposed to operations that do not need it.
    def _run(
        cmd: str, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        full_cmd = f"cd {shlex.quote(working_dir)} && {cmd}"
        ec, out = container.exec_run(
            ["/bin/sh", "-c", full_cmd],
            stdout=True,
            stderr=True,
            environment=env,
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
    # Push (one-shot).  The boundary-crossing confirmation two-step
    # (dry_run -> token -> execute) was retired for V1.0: the human gate
    # is the MCP client's own tool-approval prompt and the egress proxy
    # is the structural guard, so an in-band confirmation token only added
    # ceremony without a real guarantee once tools are auto-approved.
    # ------------------------------------------------------------------
    # Recover the proxy control env before touching the container's git
    # state (#428): a stale/missing control env would make the push fail
    # closed, so surface it up front rather than mid-operation.
    proxy_err = _ensure_proxy_ready(client)
    if proxy_err:
        return _finish({"status": "error", "step": "egress_proxy", "error": proxy_err})

    # --- Git branch check/create ---
    _run(f"git checkout -b {shlex.quote(branch)} 2>/dev/null || git checkout {shlex.quote(branch)}")

    # --- Git add / commit ---
    add_ec, add_out, add_err = _run("git add -A")
    if add_ec != 0:
        return _finish({
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
                return _finish({
                    "status": "error",
                    "step": "squash_reset",
                    "error": reset_err or reset_out,
                })
            readd_ec, readd_out, readd_err = _run("git add -A")
            if readd_ec != 0:
                return _finish({
                    "status": "error",
                    "step": "squash_readd",
                    "error": readd_err or readd_out,
                })

    # --- Git identity: set before commit ---
    name_to_use = author_name if author_name is not None else "sunaba[bot]"
    email_to_use = author_email if author_email is not None else "sunaba[bot]@users.noreply.github.com"
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
            return _finish({
                "status": "error",
                "step": "git_commit",
                "error": commit_err or commit_out,
            })

    # --- Lazy VCS token injection (Issue #347) ---
    # Resolve a token host-side and hand it to the push / PR / API-push
    # execs only.  The container itself never carries a VCS token (#356/
    # #439), so any networked container can publish while containers that
    # never publish never see a token.  When no host token is available,
    # ``push_env`` is ``None`` and the push has no credential to fall back
    # on (it fails cleanly rather than silently pushing unauthenticated).
    #
    # With the egress proxy configured (#356), the credential goes to the
    # proxy instead, grant-scoped via ``authorized_push_grant(token=...)``:
    # the proxy injects ``Authorization`` into the authorized push itself, so
    # neither the git-push exec nor the API-push fallback carries a token --
    # the container stays credential-free end to end, and the Objects API
    # fallback cannot become a proxy bypass.  PR creation runs host-side
    # (#360), so no exec carries a token anymore when a host token exists.
    push_token = _resolve_vcs_token()
    token_env = _push_token_env(push_token)
    proxied = proxy_configured()
    push_env = None if proxied else token_env

    # --- Git push (with transport fallback to API push) ---
    force_flag = " --force" if allow_force_push else ""
    push_cmd = (
        f"git -c credential.helper= "
        f"-c credential.helper='!f() {{ echo username=x-access-token; echo password=$GITHUB_TOKEN; }}; f' "
        f"push origin {shlex.quote(branch)}{force_flag}"
    )
    # Open a short-lived push-authorization grant on the egress proxy so
    # the sidecar lets *this* push through (#356 / #357).  When no proxy is
    # configured this is a no-op, so publish behaves exactly as before.  The
    # grant is revoked on exit -- including the early return below -- so a
    # failed push (either transport) still closes it.  (PR creation below is
    # a non-push write on api.github.com and runs host-side, #360.)
    try:
        with authorized_push_grant(repo, token=push_token or None):
            push_ec, push_out, push_err = _run(push_cmd, env=push_env)

            # Get the SHA of the pushed commit
            sha = ""
            sha_ec, sha_out, _ = _run("git rev-parse HEAD")
            if sha_ec == 0:
                sha = sha_out.strip()[:7]

            # Transport fallback: git push failed -> try GitHub API push
            if push_ec != 0:
                # Issue #401: when the egress proxy blocks the push, do NOT
                # fall back to the Objects API -- that would bypass the
                # proxy and silently hide a configuration error so that the
                # admin never notices the allowlist is misconfigured.
                push_error_text = (push_err or push_out or "").lower()
                if "blocked by egress proxy" in push_error_text:
                    record_boundary_crossing(
                        cid,
                        "publish",
                        f"repo={repo} branch={branch} push_blocked_by_egress_proxy",
                        approved=False,
                    )
                    return _finish({
                        "status": "error",
                        "step": "git_push",
                        "error": push_err or push_out,
                        "sha": sha,
                        "hint": (
                            "The egress proxy blocked this push. "
                            "When SUNABA_ENABLE_EGRESS_PROXY=true, "
                            "set SUNABA_ALLOWED_REPOS to allow "
                            "pushes to specific repositories."
                        ),
                    })

                push_result = _try_api_push(
                    container, cid, repo, branch, working_dir, env=push_env
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
                    )
                    return _finish({
                        "status": "error",
                        "step": "git_push",
                        "error": push_err or push_out,
                        "sha": sha,
                    })
    except ProxyAuthError as exc:
        record_boundary_crossing(
            cid,
            "publish",
            f"repo={repo} branch={branch} proxy_auth_failed",
            approved=False,
        )
        return _finish({
            "status": "error",
            "step": "proxy_auth",
            "error": str(exc),
        })
    # --- Optionally create PR ---
    pr_url: str | None = None
    if create_pr:
        pr_create_error: str | None = None
        if push_token:
            # Host-side REST call (#360): PR creation is a non-push write on
            # api.github.com, so keep it out of the container entirely — this
            # exec was the last one that carried an ephemeral token.
            try:
                pr_url = _create_pr_via_api(
                    repo, branch, pr_title, pr_body, base_branch, push_token
                )
            except RuntimeError as exc:
                pr_create_error = str(exc)
        elif proxied:
            # Under the proxy the container is credential-free (#356): with
            # no host token there is no transport left to try, and an
            # unauthenticated in-container gh would only fail less clearly.
            pr_create_error = (
                "PR creation needs a host-side token (GITHUB_TOKEN / broker); "
                "the container holds no credential under the egress proxy"
            )
        else:
            # Legacy tokenless-host setup: the container may carry a
            # startup-injected token, so the in-container gh still works.
            pr_cmd = (
                f"gh pr create --repo {shlex.quote(repo)}"
                f" --head {shlex.quote(branch)}"
                f" --title {shlex.quote(pr_title)}"
            )
            if base_branch:
                pr_cmd += f" --base {shlex.quote(base_branch)}"
            if pr_body:
                body_encoded = base64.b64encode(
                    pr_body.encode("utf-8")
                ).decode("ascii")
                pr_cmd = (
                    f"BODY_FILE=$(mktemp) &&"
                    f" echo {shlex.quote(body_encoded)} | base64 -d > \"$BODY_FILE\" &&"
                    f" {pr_cmd}"
                    f" --body-file \"$BODY_FILE\""
                    f'; rm -f "$BODY_FILE"'
                )
            else:
                pr_cmd += " --body ''"

            pr_ec, pr_out, pr_err = _run(pr_cmd, env=token_env)
            if pr_ec != 0:
                pr_create_error = pr_err or pr_out
            else:
                # Extract PR URL from gh output
                for line in (pr_out + pr_err).splitlines():
                    line = line.strip()
                    if line.startswith("https://github.com/"):
                        pr_url = line
                        break

        if pr_create_error is not None:
            # Push succeeded but PR creation failed — still record push
            record_boundary_crossing(
                cid,
                "publish",
                f"repo={repo} branch={branch} sha={sha} pr_create_failed",
                approved=True,
            )
            return _finish({
                "status": "pushed",
                "branch": branch,
                "sha": sha,
                "pr_create_error": pr_create_error,
            })

    # --- Success ---
    details = f"repo={repo} branch={branch} sha={sha}"
    if pr_url:
        details += f" pr_url={pr_url}"

    record_boundary_crossing(
        cid,
        "publish",
        details,
        approved=True,
    )

    result: dict[str, Any] = {
        "status": "pushed",
        "branch": branch,
        "sha": sha,
    }
    if pr_url:
        result["pr_url"] = pr_url
    if not create_pr:
        result["note"] = (
            "pushed only -- no PR was created. Pass create_pr=True to open "
            "one, or the branch may already have an open PR."
        )

    return _finish(result)


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


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------


def clone_repo(
    container_id: str,
    repo: str,
    dest_dir: str = "/home/sandbox",
    branch: str = "",
) -> str:
    """Clone a Git repository inside the container.

    Uses ``gh repo clone`` when a VCS token is present in the container,
    otherwise an anonymous ``git clone`` over HTTPS -- public repos clone
    without credentials (Issue #333).  The choice between those two
    container-side transports is made by probing the container itself
    (``gh auth setup-git``).

    Requires a container started with ``allow_network=True``.  A
    *private* repository needs no extra flag: under the egress proxy
    (#356) the container never receives a token, so a host-resolved
    token is handed to the proxy for a short read-authorization grant
    (#419) that authenticates the clone's anonymous ``git clone`` at the
    network layer -- the container's own env and any ``gh``
    credential-helper state stay untouched.

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

    - Prefer over ``sandbox_exec`` + ``gh repo clone`` (the proxy read-authorization grant handles private-repo auth, #419)
    - Prefer over ``clone_repo`` when starting a new container — use ``sandbox_initialize(clone_repo=...)`` instead

    .. rubric:: Fallback

    - If a private clone fails, ensure the egress proxy has a host-resolvable token (broker / ``GITHUB_TOKEN``) for the read-authorization grant (#419)

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
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    if not _REPO_FORMAT_RE.match(repo):
        return json.dumps(
            {"status": "error", "error": f"Invalid repo format: {repo} (expected owner/repo)"}
        )

    # ``gh repo clone`` treats its second argument as the clone target
    # directory itself, not a parent.  Clone into ``{dest_dir}/{repo_name}``
    # so the default ``dest_dir`` (``/home/sandbox``, an existing non-empty
    # home directory) works and the target matches the reported
    # ``clone_path``.
    repo_name = repo.split("/")[-1]
    clone_path = f"{dest_dir.rstrip('/')}/{repo_name}"

    # Recover the proxy control env vars before anything else touches the
    # container, so a lost-env failure (#428) is reported without a wasted
    # exec and fails closed as early as possible.
    proxy_err = _ensure_proxy_ready(client)
    if proxy_err:
        return json.dumps({"status": "error", "error": proxy_err})

    # ``gh auth setup-git`` configures gh as the git credential helper so a
    # later ``git push`` works with the injected token.  Its exit code also
    # tells us whether a VCS token is present (0 = authenticated / GH_TOKEN
    # set, non-zero = no token), which selects the clone transport
    # (Issue #333): ``gh repo clone`` when authenticated (handles private),
    # an anonymous ``git clone`` otherwise (public repos clone without
    # credentials; ``gh`` cannot, as it requires auth even for public).
    auth_ec, _ = container.exec_run(
        ["/bin/sh", "-c", "gh auth setup-git"],
        stdout=True,
        stderr=True,
    )
    authenticated = auth_ec == 0

    # Under the egress proxy the container never carries a token (#356), so
    # `authenticated` above is always False there; a read grant (#419)
    # authenticates the clone at the proxy instead, without changing the
    # container-side (anonymous) command.
    open_read_grant = not authenticated and proxy_configured()
    cmd = _build_clone_command(repo, clone_path, branch, authenticated)

    if open_read_grant:
        with authorized_read_grant(repo, token=_resolve_vcs_token() or None):
            exit_code, output = container.exec_run(
                ["/bin/sh", "-c", cmd],
                stdout=True,
                stderr=True,
            )
    else:
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
        # Record a denied/failed egress-proxy read grant (#421), mirroring
        # publish's push-grant recording (#356/#357).  Scoped to the
        # grant case only -- an ordinary (non-proxied) clone failure was
        # never journaled before and is out of scope here.
        if open_read_grant:
            record_boundary_crossing(
                cid,
                "clone_repo",
                f"repo={repo} branch={branch or 'default'} proxy_read_grant=True",
                approved=False,
            )
        return json.dumps({
            "status": "error",
            "error": error_text,
            "clone_path": clone_path,
        })

    record_boundary_crossing(
        cid,
        "clone_repo",
        f"repo={repo} branch={branch or 'default'} dest={clone_path} proxy_read_grant={open_read_grant}",
        approved=True,
    )

    result: dict[str, Any] = {
        "status": "ok",
        "repo": repo,
        "clone_path": clone_path,
        "branch": branch or "default",
    }
    if not authenticated and not open_read_grant:
        result["warning"] = CLONE_NO_TOKEN_WARNING
    return json.dumps(result)
