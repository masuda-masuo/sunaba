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


def _check_merge_in_progress(run: RunFunc) -> dict | None:
    """Check whether a merge is in progress.

    An in-progress merge (stopped on conflicts) has ``.git/MERGE_HEAD``
    present.  Publishing during such a state would destroy the merge state
    and any conflict resolutions via ``git reset``.

    Returns:
        An error dict if a merge is in progress, or ``None`` if not.
    """
    ec, out, _err = run("[ -f .git/MERGE_HEAD ] && echo 'in-progress' || echo 'none'")
    if ec == 0 and out.strip() == "in-progress":
        return {
            "status": "error",
            "step": "merge_in_progress",
            "error": (
                "Cannot publish during an unresolved merge. "
                "Call merge_complete(container_id=...) to finish the merge, "
                "or merge_abort(container_id=...) to abandon it, "
                "then publish again."
            ),
        }
    return None


def git_prepare_commit(
    run: RunFunc,
    *,
    branch: str,
    message: str,
    files: list[str] | None = None,
    author_name: str | None = None,
    author_email: str | None = None,
    base_auto_include: dict[str, str | bytes | None] | None = None,
    is_merge: bool = False,
    merge_parent_sha: str | None = None,
    merge_result: dict | None = None,
) -> tuple[dict | None, list[str] | None]:
    """Checkout branch, stage, squash unpushed checkpoints, then commit.

    Step names (for error reporting): ``git_checkout``, ``git_add``,
    ``squash_reset``, ``squash_readd``, ``git_commit``, ``empty_result``,
    ``committed_paths``, ``declared_unchanged``, ``auto_include_write``,
    ``auto_include_add``, ``auto_include_delete``.

    Returns ``(error_dict, None)`` on failure or
    ``(None, committed_paths)`` on success.  ``committed_paths`` is the
    list of paths that actually entered the commit, derived from
    ``git diff-tree HEAD^ HEAD`` (not from the *files* manifest).

    When ``is_merge`` is True and ``merge_parent_sha`` is provided, the
    commit is built as a two-parent merge commit (``git write-tree`` /
    ``git commit-tree`` sequence) instead of ``git commit``.  The
    ``merge_result`` dict, when passed, is populated with
    ``merge_rebuilt_parents: [p1, p2]`` on success.

    Args:
        run: Injected exec callback.
        branch: Branch name to create or checkout.
        message: Git commit message.
        files: When non-None, stage only these paths (manifest mode).
            When None, stage everything with ``git add -A`` (legacy mode).
        author_name: Override commit author name.
        author_email: Override commit author email.
        base_auto_include: Optional dict of path -> content_or_None for files
            that the base branch advanced since the feature branch was last
            pushed (Candidate C, issue #712).  A ``str`` value is UTF-8 text to
            write and stage; ``bytes`` is binary/non-UTF-8 content; ``None``
            signals the path should be deleted (deletion auto-include, issue
            #715).  Content is sourced host-side from GitHub API, never from
            the container.  Applied before declared files (declared files
            override).
        is_merge: When True, build a two-parent merge commit instead of a
            single-parent commit (#819).
        merge_parent_sha: SHA of the base branch tip to use as parent 2
            of the merge commit.  Only used when ``is_merge`` is True.
        merge_result: When provided and non-None, populated with merge
            rebuild info (e.g. ``merge_rebuilt_parents``).

    Returns ``(error_dict, None)`` on failure or
    ``(None, committed_paths)`` on success.
    """
    base_ref = ""  # resolved in manifest mode; used by declared_unchanged
    # --- Reject publish during an unresolved merge ---
    # When a merge stopped on conflicts, the merge is not yet committed:
    # HEAD still has a single parent and .git/MERGE_HEAD exists.  A reset
    # would destroy the merge state along with any resolutions.
    merge_err = _check_merge_in_progress(run)
    if merge_err is not None:
        return merge_err, None

    # --- Git branch check/create ---
    checkout_ec, checkout_out, checkout_err = run(
        f"git checkout -b {shlex.quote(branch)} 2>/dev/null"
        f" || git checkout {shlex.quote(branch)}"
    )
    if checkout_ec != 0:
        return {
            "status": "error",
            "step": "git_checkout",
            "error": checkout_err or checkout_out,
        }, None

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
                # Try to resolve via ls-remote --symref (network).
                # This resolves main, master, dev, or anything else the
                # remote has configured as its HEAD without enumerating names.
                _, ls_out, _ = run(
                    "timeout 15 git ls-remote --symref origin HEAD 2>/dev/null || true"
                )
                for ls_line in ls_out.splitlines():
                    if ls_line.startswith("ref: refs/heads/") and "\tHEAD" in ls_line:
                        # Format: "ref: refs/heads/<name>\tHEAD"
                        ls_default = ls_line.removeprefix(
                            "ref: refs/heads/"
                        ).split("\t")[0]
                        if ls_default:
                            ls_quoted = shlex.quote(ls_default)
                            # Check if the branch already exists locally
                            _, verify_out, _ = run(
                                f"git rev-parse --verify origin/{ls_quoted} 2>/dev/null"
                            )
                            if verify_out.strip():
                                base_ref = f"origin/{ls_quoted}"
                                break
                            # Fetch the branch from the remote
                            fetch_ec, _, _ = run(
                                f"git fetch origin {ls_quoted} 2>/dev/null"
                            )
                            if fetch_ec == 0:
                                _, verify2_out, _ = run(
                                    "git rev-parse --verify"
                                    f" origin/{ls_quoted} 2>/dev/null"
                                )
                                if verify2_out.strip():
                                    base_ref = f"origin/{ls_quoted}"
                                    break

                if not base_ref:
                    # Last-resort fallback: try well-known default names.
                    for default in ("main", "master"):
                        _, default_out, _ = run(
                            f"git rev-parse --verify origin/{default} 2>/dev/null"
                        )
                        if default_out.strip():
                            base_ref = f"origin/{default}"
                            break

        if base_ref:
            # Always reset to the remote base (issue #712 Candidate C).
            # The skip-the-reset merge-preservation branch is removed:
            # its inputs were all container-supplied and forgeable via
            # `git update-ref refs/remotes/origin/<branch>`.  Merged-in
            # base-advance files are recovered host-side via
            # base_auto_include instead (see below).
            reset_ec, reset_out, reset_err = run(
                f"git reset --mixed {base_ref}"
            )
            if reset_ec != 0:
                return {
                    "status": "error",
                    "step": "squash_reset",
                    "error": reset_err or reset_out,
                }, None
        else:
            # No remote ref could be resolved — fail instead of silently
            # skipping the reset, which would re-create the manifest leak.
            return {
                "status": "error",
                "step": "squash_reset",
                "error": (
                    "Cannot resolve a remote base for manifest mode. "
                    "The repository's default branch may be named something "
                    "other than 'main' or 'master'. "
                    "Ensure the remote (origin) is reachable and has a "
                    "default branch."
                ),
            }, None

        # --- Host-side auto-include (issue #712 Candidate C) ---
        # These are files that the base branch advanced since the feature
        # branch was last pushed.  Content is sourced host-side from the
        # GitHub API, never from the container.  Declared files (staged
        # after this block) override any auto-included path with the same
        # name.
        if base_auto_include:
            declared_set = set(files)
            for path, content in base_auto_include.items():
                if path in declared_set:
                    # Declared paths override auto-include: the working-tree
                    # edit is the authority, and auto-include must not
                    # overwrite it with host-fetched content.
                    continue
                if content is None:
                    # Auto-include deletion: git rm the path if tracked
                    # (issue #715).  If the path was never tracked (edge
                    # case) this is a no-op.
                    exists_ec, _, _ = run(
                        "git ls-files --error-unmatch -- "
                        + shlex.quote(f":(literal){path}")
                    )
                    if exists_ec == 0:
                        rm_ec, rm_out, rm_err = run(
                            "git rm -- "
                            + shlex.quote(f":(literal){path}")
                        )
                        if rm_ec != 0:
                            return {
                                "status": "error",
                                "step": "auto_include_delete",
                                "error": rm_err or rm_out,
                            }, None
                    continue
                # Write content via base64 to safely pass through the shell
                if isinstance(content, bytes):
                    encoded = base64.b64encode(content).decode("ascii")
                else:
                    encoded = base64.b64encode(
                        content.encode("utf-8")
                    ).decode("ascii")
                write_ec, write_out, write_err = run(
                    f"echo {shlex.quote(encoded)} | base64 -d"
                    f" > {shlex.quote(path)}"
                )
                if write_ec != 0:
                    return {
                        "status": "error",
                        "step": "auto_include_write",
                        "error": write_err or write_out,
                    }, None
                # Stage the auto-included file
                add_ec, add_out, add_err = run(
                    "git add -- "
                    + shlex.quote(f":(literal){path}")
                )
                if add_ec != 0:
                    return {
                        "status": "error",
                        "step": "auto_include_add",
                        "error": add_err or add_out,
                    }, None

        # Stage only the declared manifest paths.  :(literal) disables
        # pathspec glob interpretation so each declared path stages
        # exactly the file it literally names (add also stages deletions).
        for f in files:
            add_ec, add_out, add_err = run(
                "git add -- " + shlex.quote(f":(literal){f}")
            )
            if add_ec != 0:
                return {
                    "status": "error",
                    "step": "git_add",
                    "error": add_err or add_out,
                }, None

        # --- Reject an empty declared result ---
        # When every declared path is byte-identical to what the push
        # target already contains, the commit would be empty for those
        # paths: nothing to publish.  Use ``git diff --cached --exit-code``
        # (0 = no differences, 1 = differences, >1 = error) against the
        # base ref the index was reset to.  Auto-included non-declared
        # paths are excluded from this check.
        # Exception: when this is a history-only merge (the resolution
        # kept the branch side), byte-identical content is expected and
        # the two-parent commit advances history (#819).
        if base_ref:
            diff_cmd = (
                "git diff --cached --exit-code "
                + base_ref
                + " -- "
                + " ".join(
                    shlex.quote(f":(literal){f}") for f in files
                )
            )
            diff_ec, diff_out, diff_err = run(diff_cmd)
            if diff_ec == 0:
                if is_merge:
                    # History-only merge: the two-parent commit must
                    # be pushed even though declared content is
                    # unchanged (#819).
                    if merge_result is not None:
                        merge_result["history_only_merge"] = True
                else:
                    return {
                        "status": "error",
                        "step": "empty_result",
                        "error": (
                            "Every declared path is byte-identical to what "
                            + base_ref
                            + " already contains. No change to publish."
                        ),
                        "declared_paths": files,
                    }, None
    else:
        # --- Legacy mode: git add -A with upstream-aware squash ---
        add_ec, add_out, add_err = run("git add -A")
        if add_ec != 0:
            return {
                "status": "error",
                "step": "git_add",
                "error": add_err or add_out,
            }, None

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
                    }, None
                readd_ec, readd_out, readd_err = run("git add -A")
                if readd_ec != 0:
                    return {
                        "status": "error",
                        "step": "squash_readd",
                        "error": readd_err or readd_out,
                    }, None

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

    two_parent_built = False
    if is_merge and merge_parent_sha and base_ref:
        # --- Two-parent merge commit (#819) ---
        # When publish detects HEAD is a merge, the rebuilt commit must
        # preserve the merge lineage so GitHub can recompute the merge
        # base and mark the PR mergeable.
        _, parent1_out, _ = run(f"git rev-parse {base_ref}")
        parent1 = parent1_out.strip()
        parent2 = merge_parent_sha.strip()

        # Degenerate guard: skip two-parent if parent2 == parent1 or
        # parent2 is already an ancestor of parent1.
        two_parent = True
        if parent2 == parent1:
            two_parent = False
        else:
            anc_ec, _, _ = run(
                f"git merge-base --is-ancestor {shlex.quote(parent2)}"
                f" {shlex.quote(parent1)}"
            )
            if anc_ec == 0:
                two_parent = False

        if two_parent:
            # Write the staged tree as a tree object.
            tree_ec, tree_sha, tree_err = run("git write-tree")
            if tree_ec != 0:
                return {
                    "status": "error",
                    "step": "git_commit",
                    "error": (
                        "git write-tree failed: "
                        f"{tree_err or tree_sha}"
                    ),
                }, None
            tree_sha = tree_sha.strip()

            # Create the commit manually with two parents.
            ct_cmd = (
                f"git -c user.name={safe_name}"
                f" -c user.email={safe_email}"
                f" commit-tree {shlex.quote(tree_sha)}"
                f" -p {shlex.quote(parent1)}"
                f" -p {shlex.quote(parent2)}"
                f" -m {shlex.quote(message)}"
            )
            ct_ec, ct_sha, ct_err = run(ct_cmd)
            if ct_ec != 0:
                return {
                    "status": "error",
                    "step": "git_commit",
                    "error": (
                        "git commit-tree failed: "
                        f"{ct_err or ct_sha}"
                    ),
                }, None
            new_sha = ct_sha.strip()

            # Point HEAD to the new commit.
            ur_ec, ur_out, ur_err = run(
                f"git update-ref HEAD {shlex.quote(new_sha)}"
            )
            if ur_ec != 0:
                return {
                    "status": "error",
                    "step": "git_commit",
                    "error": (
                        "git update-ref failed: "
                        f"{ur_err or ur_out}"
                    ),
                }, None

            two_parent_built = True
            if merge_result is not None:
                merge_result["merge_rebuilt_parents"] = [
                    parent1[:7], parent2[:7],
                ]
        else:
            # Degenerate: fall through to standard git commit.
            if merge_result is not None:
                merge_result["merge_degenerate"] = True

    if not two_parent_built:
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
                }, None

    # --- Derive the actual committed paths from the commit ---
    # These are the paths that really entered the commit, not the caller's
    # manifest.  HEAD^ is the parent commit (the base after reset); fall
    # back to the empty-tree hash for root commits (though unlikely after a
    # clone + reset).
    diff_ec, diff_out, diff_err = run(
        "git diff-tree --no-commit-id -r --name-only HEAD^ HEAD 2>/dev/null"
        " || git diff-tree --no-commit-id -r --name-only"
        " 4b825dc642cb6eb9a060e54bf898b433f71bada6 HEAD"
    )
    if diff_ec != 0:
        # Fail rather than reporting an empty staged_files.  The whole point
        # of deriving these paths is that the caller can trust the field; a
        # silent empty list would re-create the defect this replaced (#736),
        # and would additionally suppress the declared_unchanged check below.
        # This runs before the push, so nothing has reached the remote.
        return {
            "status": "error",
            "step": "committed_paths",
            "error": (
                "The commit was created but its file list could not be read "
                "(git diff-tree exited "
                f"{diff_ec}): {diff_err or diff_out}"
            ),
        }, None
    committed_paths: list[str] = [
        p for p in diff_out.split("\n") if p.strip()
    ]

    # --- Check for partially-unchanged declared paths (manifest mode) ---
    # The "all unchanged" case is caught earlier by empty_result.  Here we
    # catch the case where SOME declared paths are byte-identical to the
    # base and therefore absent from the commit.
    if files is not None:
        declared_set = set(files)
        committed_set = set(committed_paths)
        unchanged = sorted(declared_set - committed_set)
        if unchanged and len(unchanged) < len(files):
            ref_name = base_ref if base_ref else "the remote base"
            if is_merge:
                # In the merge-rebuild case, declared paths identical
                # to base_ref are expected (the resolution kept the
                # branch side).  Report informationally instead of
                # erroring (#819).
                if merge_result is not None:
                    merge_result["declared_unchanged_merge"] = {
                        "paths": unchanged,
                        "note": (
                            f"These declared paths are identical to "
                            f"{ref_name} (resolution kept branch side)."
                        ),
                    }
            else:
                return {
                    "status": "error",
                    "step": "declared_unchanged",
                    "declared_unchanged": unchanged,
                    "error": (
                        f"These declared paths are identical to {ref_name} "
                        f"and contributed nothing to the commit: "
                        f"{', '.join(unchanged)}"
                    ),
                }, None

    return None, committed_paths


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
) -> tuple[dict | None, str, str]:
    """Push with transport fallback to GitHub Objects API.

    ``try_api_push`` is a zero-argument callable that runs the API push
    (injected by the caller so this function stays Docker-free).
    ``record_crossing(reason, approved)`` records a boundary-crossing
    journal entry.

    Returns ``(error_payload_or_None, sha, transport)``.  On success
    ``error_payload`` is ``None``, ``sha`` is the pushed commit SHA
    (first 7 chars), and ``transport`` is ``"native"`` (git push) or
    ``"api"`` (GitHub Objects API fallback).  On failure it returns an
    error dict with ``status``, ``step``, ``error``, ``sha`` and
    optionally ``hint``; ``transport`` is ``""`` on failure.
    """
    push_ec, push_out, push_err = run(push_cmd, env=push_env)

    # Get the SHA of the pushed commit
    sha = ""
    sha_ec, sha_out, _ = run("git rev-parse HEAD")
    if sha_ec == 0:
        sha = sha_out.strip()[:7]

    transport = ""

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
                transport,
            )

        push_result = try_api_push()
        if push_result.get("status") == "ok":
            sha = push_result.get("sha", sha)
            transport = "api"
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
            return (payload, sha, transport)

    if not transport:
        transport = "native"

    return (None, sha, transport)


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
