"""VCS tools: issue_view, submit, sandbox_create_pr, clone_repo."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from docker.errors import NotFound

from code_sandbox_mcp.edit_verify import run_verify
from code_sandbox_mcp.journal import get_or_create_run_id, record_boundary_crossing
from code_sandbox_mcp.token import generate_token, verify_and_consume
from code_sandbox_mcp.tools.common import _docker

# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

_REPO_FORMAT_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_BRANCH_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)(?!-)(?!.*@\{)"
    r"[\w./-]+$"
)


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
            raise RuntimeError(r.stderr or r.stdout)
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
    dir_part = str(Path(save_to).parent)
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
# (Issue #169).  Both ``submit`` and ``sandbox_create_pr`` route their
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
            (e.g. ``"submit"``, ``"sandbox_create_pr"``).
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
# submit
# ---------------------------------------------------------------------------


def submit(
    container_id: str,
    repo: str,
    branch: str,
    message: str,
    working_dir: str = "/home/sandbox",
    create_pr: bool = False,
    pr_title: str = "",
    pr_body: str = "",
    base_branch: str = "",
    dry_run: bool = False,
    token: str = "",
    verify_path: str = ".",
    gate_on_lint_error: bool = True,
    gate_on_type_error: bool = False,
    gate_on_test_fail: bool = True,
    gate_on_scan_error: bool = True,
    gate_on_scan_warning: bool = False,
    author_name: str | None = None,
    author_email: str | None = None,
    language: str | None = None,
) -> str:
    """Stage, commit, push, and optionally create a PR.

    Two-step flow for boundary-crossing writes:

    1. ``dry_run=True`` — returns a diff summary and a confirmation
       token that must be approved before execution.
    2. ``dry_run=False`` + *token* — verifies the token, runs
       ``verify_in_container`` as a gate, then executes
       ``git add -A && git commit -m MESSAGE && git push origin BRANCH``
       (and ``gh pr create`` if *create_pr* is ``True``).

    Requires a container started with ``allow_network=True`` and
    ``inject_vcs_token=True``.

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
        verify_path: Path inside *working_dir* to run verification on
            (default ``"."``).
        gate_on_lint_error: Whether lint errors fail the verify gate.
        gate_on_type_error: Whether type-check errors fail the verify
            gate.
        gate_on_test_fail: Whether test failures fail the verify gate.
        gate_on_scan_error: Whether semgrep ERROR findings fail the
            verify gate.
        gate_on_scan_warning: Whether semgrep WARNING findings fail the
            verify gate.
        author_name: Git commit author name.  When set, takes precedence
            over the image-level default configured in
            ``docker/Dockerfile.sandbox`` (``code-sandbox-mcp[bot]``).
            When ``None``, the image-level default is used.
        author_email: Git commit author email.  When set, takes precedence
            over the image-level default configured in
            ``docker/Dockerfile.sandbox``
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
        # Gather diff summary
        status_ec, status_out, status_err = _run(
            "git status --porcelain && echo '---DIFF---' && git diff HEAD --stat"
        )
        diff_summary = (status_out + "\n" + status_err).strip()

        if not diff_summary or diff_summary == "---DIFF---":
            return json.dumps({
                "status": "dry_run",
                "diff_summary": "(no changes detected)",
                "branch": branch,
                "message": message,
                "warning": "No changes to commit.  Submit will succeed as a no-op.",
            })

        details = (
            f"repo={repo} branch={branch} message={message[:80]}"
        )
        if create_pr:
            details += f" pr_title={pr_title[:60]}"

        return _dry_run_response(
            "submit",
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
    # EXECUTE — require token + verify gate
    # ------------------------------------------------------------------
    if not token:
        return json.dumps({
            "status": "error",
            "error": "Token required for execution.  Run with dry_run=True first.",
        })

    # --- Verify gate ---
    if os.path.isabs(verify_path):
        verify_path_full = verify_path
    else:
        verify_path_full = f"{working_dir}/{verify_path}".rstrip("/")
    verify_result = run_verify(
        client,
        cid,
        verify_path_full,
        strict=True,
        gate_on_lint_error=gate_on_lint_error,
        gate_on_type_error=gate_on_type_error,
        gate_on_test_fail=gate_on_test_fail,
        gate_on_scan_error=gate_on_scan_error,
        gate_on_scan_warning=gate_on_scan_warning,
        language=language,
    )

    if not verify_result.get("gate_passed", False):
        record_boundary_crossing(
            cid,
            "submit",
            f"repo={repo} branch={branch} verify_failed",
            approved=False,
            token=token,
        )
        return json.dumps({
            "status": "rejected",
            "reason": "verify_gate_failed",
            "verify_result": verify_result,
        })

    # --- Consume token (after gate passes) ---
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

    # --- Git push ---
    push_cmd = (
        f"git -c credential.helper= "
        f"-c credential.helper='!f() {{ echo username=x-access-token; echo password=$GITHUB_TOKEN; }}; f' "
        f"push origin {shlex.quote(branch)}"
    )
    push_ec, push_out, push_err = _run(push_cmd)

    # Get the SHA of the pushed commit
    sha = ""
    sha_ec, sha_out, _ = _run("git rev-parse HEAD")
    if sha_ec == 0:
        sha = sha_out.strip()[:7]

    if push_ec != 0:
        record_boundary_crossing(
            cid,
            "submit",
            f"repo={repo} branch={branch} push_failed",
            approved=False,
            token=token,
        )
        return json.dumps({
            "status": "error",
            "step": "git_push",
            "error": push_err or push_out,
            "sha": sha,
            "verify_result": verify_result,
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
                f"; rm -f \"$BODY_FILE\""
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
                "submit",
                f"repo={repo} branch={branch} sha={sha} pr_create_failed",
                approved=True,
                token=token,
            )
            return json.dumps({
                "status": "pushed",
                "branch": branch,
                "sha": sha,
                "pr_create_error": pr_err or pr_out,
                "verify_result": verify_result,
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
        "submit",
        details,
        approved=True,
        token=token,
    )

    result: dict[str, Any] = {
        "status": "pushed",
        "branch": branch,
        "sha": sha,
        "verify_result": verify_result,
    }
    if pr_url:
        result["pr_url"] = pr_url

    return json.dumps(result)


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
    working_dir: str = "/home/sandbox",
    dry_run: bool = False,
    token: str = "",
) -> str:
    """Push the current branch via GitHub API and create a PR.

    Unlike :func:`submit`, this tool uses the GitHub Objects API
    (blob → tree → commit → ref) to push the branch, which works when
    the injected token cannot push via HTTPS git (e.g. GitHub App
    installation tokens).

    Typical flow:

    1. ``sandbox_initialize(allow_network=True, inject_vcs_token=True)``
    2. ``clone_repo`` / ``gh repo clone`` inside the container
    3. Make changes, run ``git add -A && git commit``
    4. ``sandbox_create_pr(dry_run=True)`` — preview + confirmation token
    5. ``sandbox_create_pr(token=...)`` — pushes via API + opens PR

    Like :func:`submit`, execution is a two-step flow: call once with
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

       Unlike :func:`submit`, this tool has **no verify gate** (no
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
    ec, out, _ = _run(
        f"trap 'rm -f /tmp/_sandbox_create_pr.py' EXIT"
        f" && python3 /tmp/_sandbox_create_pr.py {shlex.quote(repo)} {shlex.quote(branch)} {shlex.quote(working_dir)}"
    )
    if ec != 0:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push error={out[:200]}",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", "error": out})

    try:
        push_result = json.loads(out)
    except json.JSONDecodeError:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} step=api_push json_parse_error",
            approved=False, token=token,
        )
        return json.dumps({"status": "error", "step": "api_push", "error": out})

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

    pr_ec, pr_out, _ = _run(pr_cmd)
    if pr_ec != 0:
        record_boundary_crossing(
            cid, "sandbox_create_pr",
            f"repo={repo} branch={branch} sha={new_sha[:7]} step=pr_create error={pr_out[:200]}",
            approved=True, token=token,
        )
        return json.dumps({
            "status": "pushed_no_pr",
            "branch": branch,
            "sha": new_sha[:7],
            "pr_create_error": pr_out,
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
