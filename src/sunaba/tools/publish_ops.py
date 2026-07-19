"""Git operations and push/PR creation for publish -- extracted from tools/vcs.py (issue #650).

All top-level functions receive a ``run`` callable as first argument for
dependency injection, allowing unit tests with fake ``run`` implementations
(no Docker dependency).  Every function is deterministic: given the same
``run`` responses and parameters, it produces the same return value.
"""

import base64
import shlex
from typing import Any, Callable, Protocol

from sunaba.tools.publish_planner import is_egress_block, push_failure_hints

# run(cmd, env) -> (exit_code, stdout, stderr)
# The ``env`` parameter is a dict forwarded as the exec environment, or None
# (keep the container's own env).  Called as run("some command") or
# run("some command", env={"KEY": "val"}).


class RunFunc(Protocol):
    """Protocol for the ``run`` callable injected into publish ops."""

    def __call__(
        self, cmd: str, env: dict[str, str] | None = None
    ) -> tuple[int, str, str]:
        """Execute a shell command in the container and return (ec, stdout, stderr)."""
        ...


def git_prepare_commit(
    run: RunFunc,
    *,
    branch: str,
    message: str,
    files: list[str] | None = None,
    author_name: str | None = None,
    author_email: str | None = None,
) -> dict | None:
    """Checkout branch, stage, squash unpushed checkpoints, then commit.

    Step names (for error reporting): ``git_add``, ``squash_reset``,
    ``squash_readd``, ``git_commit``.

    Args:
        run: Injected exec callback.
        branch: Branch name to create or checkout.
        message: Git commit message.
        files: When non-None, stage only these paths (manifest mode).
            When None, stage everything with ``git add -A`` (legacy mode).
        author_name: Override commit author name.
        author_email: Override commit author email.

    Returns an error dict on failure, or ``None`` on success (including
    "nothing to commit" which is treated as success).
    """
    # --- Git branch check/create ---
    run(
        f"git checkout -b {shlex.quote(branch)} 2>/dev/null"
        f" || git checkout {shlex.quote(branch)}"
    )

    if files is not None:
        # --- Manifest mode: build the commit against the remote base ---
        # Resolve the base: origin/<branch> if the branch already exists on
        # the remote (follow-up push to an open PR preserves earlier commits),
        # otherwise the remote default branch (origin/HEAD).  This prevents
        # files a prior checkpoint committed via `git add -A` from leaking
        # into the pushed commit -- independent of whether the local branch
        # has an upstream configured.
        _, remote_branch_out, _ = run(
            f"git rev-parse --verify origin/{shlex.quote(branch)} 2>/dev/null"
        )
        if remote_branch_out.strip():
            base_ref = f"origin/{shlex.quote(branch)}"
        else:
            # Fallback: origin/HEAD (set by git clone) points to the
            # remote default branch.
            _, head_out, _ = run(
                "git rev-parse --verify origin/HEAD 2>/dev/null"
            )
            if head_out.strip():
                base_ref = "origin/HEAD"
            else:
                # Last-resort fallback: try well-known default names.
                base_ref = ""
                for default in ("main", "master"):
                    _, default_out, _ = run(
                        f"git rev-parse --verify origin/{default} 2>/dev/null"
                    )
                    if default_out.strip():
                        base_ref = f"origin/{default}"
                        break

        if base_ref:
            reset_ec, reset_out, reset_err = run(
                f"git reset --mixed {base_ref}"
            )
            if reset_ec != 0:
                return {
                    "status": "error",
                    "step": "squash_reset",
                    "error": reset_err or reset_out,
                }
        else:
            # No remote ref could be resolved — fail instead of silently
            # skipping the reset, which would re-create the manifest leak.
            return {
                "status": "error",
                "step": "squash_reset",
                "error": (
                    "Cannot resolve a remote base for manifest mode. "
                    "Ensure the repository has been cloned from a remote "
                    "(origin) and that the remote has a default branch."
                ),
            }

        # Stage only the declared manifest paths.
        for f in files:
            add_ec, add_out, add_err = run(f"git add -- {shlex.quote(f)}")
            if add_ec != 0:
                return {
                    "status": "error",
                    "step": "git_add",
                    "error": add_err or add_out,
                }
    else:
        # --- Legacy mode: git add -A with upstream-aware squash ---
        add_ec, add_out, add_err = run("git add -A")
        if add_ec != 0:
            return {
                "status": "error",
                "step": "git_add",
                "error": add_err or add_out,
            }

        # Squash unpushed checkpoints into a single commit (upstream only).
        track_ec, track_out, _ = run(
            "git rev-parse --abbrev-ref @{u} 2>/dev/null"
        )
        if track_ec == 0 and track_out.strip():
            unpushed_ec, unpushed_out, _ = run(
                "git log --oneline @{u}..HEAD"
            )
            if unpushed_ec == 0 and unpushed_out.strip():
                reset_ec, reset_out, reset_err = run(
                    "git reset --soft @{u}"
                )
                if reset_ec != 0:
                    return {
                        "status": "error",
                        "step": "squash_reset",
                        "error": reset_err or reset_out,
                    }
                readd_ec, readd_out, readd_err = run("git add -A")
                if readd_ec != 0:
                    return {
                        "status": "error",
                        "step": "squash_readd",
                        "error": readd_err or readd_out,
                    }

    # --- Git identity: set before commit ---
    name_to_use = (
        author_name if author_name is not None else "sunaba[bot]"
    )
    email_to_use = (
        author_email
        if author_email is not None
        else "sunaba[bot]@users.noreply.github.com"
    )
    safe_name = shlex.quote(name_to_use)
    safe_email = shlex.quote(email_to_use)
    git_commit_cmd = (
        f"git -c user.name={safe_name} -c user.email={safe_email}"
        f" commit -m {shlex.quote(message)}"
    )

    commit_ec, commit_out, commit_err = run(git_commit_cmd)
    if commit_ec != 0:
        # "nothing to commit" is OK -- everything is already committed
        if "nothing to commit" in (commit_out + commit_err).lower():
            pass
        else:
            return {
                "status": "error",
                "step": "git_commit",
                "error": commit_err or commit_out,
            }

    return None


def git_push_with_fallback(
    run: RunFunc,
    *,
    repo: str,
    branch: str,
    cid: str,
    push_cmd: str,
    push_env: dict | None,
    network_off: bool,
    token_missing: bool,
    try_api_push: Callable[[], dict[str, str]],
    record_crossing: Callable[[str, bool], None],
) -> tuple[dict | None, str]:
    """Push with transport fallback to GitHub Objects API.

    ``try_api_push`` is a zero-argument callable that runs the API push
    (injected by the caller so this function stays Docker-free).
    ``record_crossing(reason, approved)`` records a boundary-crossing
    journal entry.

    Returns ``(error_payload_or_None, sha)``.  On success ``error_payload``
    is ``None``; on failure it is an error dict with ``status``, ``step``,
    ``error``, ``sha`` and optionally ``hint``.
    """
    push_ec, push_out, push_err = run(push_cmd, env=push_env)

    # Get the SHA of the pushed commit
    sha = ""
    sha_ec, sha_out, _ = run("git rev-parse HEAD")
    if sha_ec == 0:
        sha = sha_out.strip()[:7]

    # Transport fallback: git push failed -> try GitHub API push
    if push_ec != 0:
        # Issue #401: when the egress proxy blocks the push, do NOT
        # fall back to the Objects API -- that would bypass the proxy.
        push_error_text = (push_err or push_out or "").lower()
        if is_egress_block(push_error_text):
            record_crossing(
                f"repo={repo} branch={branch} push_blocked_by_egress_proxy",
                False,
            )
            return (
                {
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
                },
                sha,
            )

        push_result = try_api_push()
        if push_result.get("status") == "ok":
            sha = push_result.get("sha", sha)
            push_ec = 0  # mark success for downstream logic
        else:
            record_crossing(
                f"repo={repo} branch={branch} push_failed transport=both",
                False,
            )
            hints = push_failure_hints(network_off, token_missing)
            payload: dict[str, Any] = {
                "status": "error",
                "step": "git_push",
                "error": push_err or push_out,
                "sha": sha,
            }
            if hints:
                payload["hint"] = " ".join(hints)
            return (payload, sha)

    return (None, sha)


def create_pull_request(
    run: RunFunc,
    *,
    repo: str,
    branch: str,
    pr_title: str,
    pr_body: str,
    base_branch: str,
    push_token: str,
    proxied: bool,
    token_env: dict | None,
    create_pr_via_api: Callable[..., str] | None = None,
) -> tuple[str | None, str | None]:
    """Create a pull request via one of three transports.

    Returns ``(pr_url, pr_create_error)``.  When the PR is created
    successfully, ``pr_url`` holds the URL and ``pr_create_error`` is
    ``None``.  On failure both may be ``None`` (e.g. no host token and no
    proxy means no transport to try), or ``pr_create_error`` holds the error
    string.
    """
    pr_url: str | None = None
    pr_create_error: str | None = None

    if push_token:
        # Host-side REST call (#360): PR creation is a non-push write on
        # api.github.com, so keep it out of the container entirely.
        if create_pr_via_api is None:
            pr_create_error = "PR creation API not available"
        else:
            try:
                pr_url = create_pr_via_api(
                    repo,
                    branch,
                    pr_title,
                    pr_body,
                    base_branch,
                    push_token,
                )
            except RuntimeError as exc:
                pr_create_error = str(exc)
    elif proxied:
        # Under the proxy the container is credential-free (#356): with
        # no host token there is no transport left to try.
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
                f" echo {shlex.quote(body_encoded)} | base64 -d"
                f' > "$BODY_FILE" &&'
                f" {pr_cmd}"
                f" --body-file \"$BODY_FILE\""
                f'; rm -f "$BODY_FILE"'
            )
        else:
            pr_cmd += " --body ''"

        pr_ec, pr_out, pr_err = run(pr_cmd, env=token_env)
        if pr_ec != 0:
            pr_create_error = pr_err or pr_out
        else:
            # Extract PR URL from gh output
            for line in (pr_out + pr_err).splitlines():
                line = line.strip()
                if line.startswith("https://github.com/"):
                    pr_url = line
                    break

    return (pr_url, pr_create_error)
