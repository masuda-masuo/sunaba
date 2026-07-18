"""VCS tools: issue_view, publish, sandbox_issue_write."""

from __future__ import annotations

import base64
import json
import logging
import posixpath
import re
import shlex
from typing import Any

from docker.errors import NotFound

from sunaba import proxy_lifecycle
from sunaba.journal import record_boundary_crossing, record_tool_use
from sunaba.proxy_client import (
    ProxyAuthError,
    authorized_push_grant,
    proxy_configured,
)
from sunaba.security import NETWORK_LABEL
from sunaba.tools.common import (
    LEGACY_WORKDIR,
    _docker,
    container_not_found_error,
)
from sunaba.tools.github_api import (
    _create_pr_via_api,
    _github_api_request,
    _github_api_request_list_all,
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

_DEFAULT_WD = LEGACY_WORKDIR


def resolve_git_root(
    container: Any,
    working_dir: str | None = None,
) -> str:
    """Return the container's git repository root.

    When *working_dir* is explicitly provided, return it unchanged.

    Otherwise the answer is simply the container's own working directory:
    :func:`sandbox_initialize` creates the container with the repo root as
    its ``WorkingDir``, so the path is already recorded in the container's
    config and no command has to run inside the container to find it.

    Containers created before that (``WorkingDir`` still the home directory)
    keep the old probe: metadata file, then home, then ``/tmp/repo/*``.
    """
    if working_dir is not None:
        return working_dir

    # The repo root as decided by the host at creation time.  Reading it back
    # from the container config costs no exec -- and, unlike the probe below,
    # cannot disagree with where the exec tools actually run, because it *is*
    # the directory they run in.
    config_wd = (container.attrs.get("Config") or {}).get("WorkingDir") or ""
    if config_wd and config_wd != LEGACY_WORKDIR:
        return str(config_wd)

    return _resolve_git_root_legacy(container)


def _resolve_git_root_legacy(container: Any) -> str:
    """Probe a pre-workspace container for its git root.

    Only reached for containers whose ``WorkingDir`` is still the home
    directory, i.e. ones created before the workspace became the repo root.
    """
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


# Host-side fetch (#360); the old in-container gh path failed under the
# egress proxy, which never provides a container-side token (#356).
def issue_view(
    container_id: str,
    repo: str,
    issue_number: int,
    save_to: str = "/home/sandbox/issue.md",
) -> str:
    """Fetch a GitHub issue host-side and save its body into the container.

    Works on any container -- allow_network is not required.  The
    response is a summary plus a file handle; read the full text with
    read_file_range.

    Issue comments and PR review comments are fetched automatically (with
    auto-pagination) and appended to the saved file in a ``## Comments``
    section, with ``author``, ``timestamp``, and (for PRs) review state
    and file location.  Fetched by the host-side API so no network is
    needed inside the container.

    Args:
        container_id: Container ID prefix.
        repo: 'owner/repo'.
        issue_number: Issue number to fetch.
        save_to: Path inside the container for the issue body.

    Returns:
        JSON: number, title, summary (first 100 chars of body), file,
        size_bytes, comments (count); error on failure.
    """
    client = _docker()
    try:
        container = client.containers.get(container_id)
    except NotFound:
        return container_not_found_error(container_id)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    cid = container_id[:12]

    token = _resolve_vcs_token()

    try:
        issue_data = _github_api_request(
            f"/repos/{repo}/issues/{issue_number}", token
        )
    except RuntimeError as e:
        return json.dumps({"status": "error", "error": f"Failed to fetch issue #{issue_number} from {repo}: {e}"})

    number = issue_data.get("number", issue_number)
    title = issue_data.get("title", "")
    body = issue_data.get("body") or ""

    try:
        issue_comments = _github_api_request_list_all(
            f"/repos/{repo}/issues/{issue_number}/comments?per_page=100",
            token,
        )
    except RuntimeError as e:
        return json.dumps({
            "status": "error",
            "error": f"Failed to fetch comments for issue #{issue_number} from {repo}: {e}",
        })

    all_comments: list[dict[str, Any]] = list(issue_comments)

    if issue_data.get("pull_request"):
        try:
            pr_comments = _github_api_request_list_all(
                f"/repos/{repo}/pulls/{issue_number}/comments?per_page=100",
                token,
            )
            all_comments.extend(pr_comments)
        except RuntimeError:
            pass
        try:
            pr_reviews = _github_api_request_list_all(
                f"/repos/{repo}/pulls/{issue_number}/reviews?per_page=100",
                token,
            )
            all_comments.extend(pr_reviews)
        except RuntimeError:
            pass

    displayed = 0
    if all_comments:
        all_comments.sort(key=lambda c: c.get("created_at") or c.get("submitted_at") or "")

        parts = ["\n\n## Comments\n"]
        for c in all_comments:
            author = c.get("user", {}).get("login", "unknown")
            ts = c.get("created_at") or c.get("submitted_at", "")
            state = c.get("state", "")
            path = c.get("path", "")
            line = c.get("line") or c.get("original_line") or ""
            c_body = (c.get("body") or "").strip()
            if not c_body:
                continue
            displayed += 1
            prefix = ""
            if state and state != "COMMENTED":
                prefix = f" ({state.upper()})"
            loc = f" — `{path}:{line}`" if path else ""
            parts.append(f"**@{author}**{prefix}{loc} — {ts}\n\n{c_body}\n")
        full_content = body + "\n".join(parts)
    else:
        full_content = body

    # Summary: first 100 characters of body
    summary = body[:100] if body else "(empty body)"

    # Write full content to file in container via base64
    encoded = base64.b64encode(full_content.encode("utf-8")).decode("ascii")
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

    size_bytes = len(full_content.encode("utf-8"))

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
        "comments": displayed,
    })


# ---------------------------------------------------------------------------
# sandbox_issue_write -- first-class, host-side issue create/comment (#414)
# ---------------------------------------------------------------------------


_ISSUE_WRITE_METHODS = ("create", "comment")


# Host-side non-push write: fills the #360 blind spot (under the egress
# proxy in-container gh has no credential, #356).  Direct REST via
# _github_api_request, the pattern of _create_pr_via_api / issue_view
# (#409/#413); single-call, dry-run retired for V1.0 to match publish.
def sandbox_issue_write(
    container_id: str,
    repo: str,
    method: str,
    title: str = "",
    body: str = "",
    issue_number: int | None = None,
) -> str:
    """Create a GitHub issue or comment on one -- host-side, no in-container gh.

    The GitHub REST API is called from the host, so no token reaches
    the container and container network access is not required.

    Args:
        container_id: Container ID prefix (journal trail only; the
            container's network state is irrelevant).
        repo: 'owner/repo'.
        method: 'create' (new issue) or 'comment' (on an existing
            issue or PR).
        title: Issue title; required for 'create'.
        body: Issue or comment body.
        issue_number: Issue/PR number to comment on; required for
            'comment', ignored for 'create'.

    Returns:
        JSON: status, html_url, number (for 'create'); error on
        failure.
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


# Host-side one-shot review submission, following sandbox_issue_write (#414).
def sandbox_pr_review_write(
    container_id: str,
    repo: str,
    pr: int,
    event: str,
    body: str = "",
    comments: list[dict[str, Any]] | None = None,
) -> str:
    """Create and submit a PR review in one shot -- host-side, no in-container gh.

    The GitHub REST API is called from the host process, so no token
    reaches the container.

    *APPROVE*/*REQUEST_CHANGES* auto-downgrade to *COMMENT* on own PR (#613).

    Args:
        container_id: Container ID prefix (journal trail only; the
            container's network state is irrelevant).
        repo: 'owner/repo'.
        pr: PR number to review.
        event: 'APPROVE', 'REQUEST_CHANGES', or 'COMMENT'.
        body: Review body text.
        comments: Inline comment dicts: path, line, body, optional side
            ('LEFT'/'RIGHT', default 'RIGHT').

    Returns:
        JSON: status, html_url, review_id; includes original_event and
        downgraded_to when an auto-downgrade occurred; error on failure.
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
            # Auto-downgrade to COMMENT when the bot owns the PR (issue #613)
            downgraded_event = "COMMENT"
            payload["event"] = downgraded_event
            try:
                result = _github_api_request(
                    f"/repos/{repo}/pulls/{pr}/reviews",
                    push_token,
                    method="POST",
                    payload=payload,
                )
            except RuntimeError as e2:
                record_boundary_crossing(
                    cid, "pr_review_write", f"{details} downgrade to COMMENT also failed", approved=False,
                )
                return json.dumps({"status": "error", "error": f"Downgrade to COMMENT also failed: {e2}"})
            record_boundary_crossing(
                cid, "pr_review_write", f"{details} (downgraded to COMMENT)", approved=True,
            )
            return json.dumps({
                "status": "ok",
                "html_url": result.get("html_url", ""),
                "review_id": result.get("id"),
                "original_event": event,
                "downgraded_to": downgraded_event,
            })
        return json.dumps({"status": "error", "error": err_msg})

    record_boundary_crossing(cid, "pr_review_write", details, approved=True)

    return json.dumps({
        "status": "ok",
        "html_url": result.get("html_url", ""),
        "review_id": result.get("id"),
    })


# ---------------------------------------------------------------------------
# publish \u2014 token / REST API / PR creation moved to github_api.py
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

    commit_err = git_prepare_commit(_run, branch=branch, message=message,
        author_name=author_name, author_email=author_email)
    if commit_err:
        return finish_json(commit_err, verified)

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
    }
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
