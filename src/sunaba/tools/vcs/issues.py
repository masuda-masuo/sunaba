"""Issue/PR tools: issue_view, sandbox_issue_write, sandbox_pr_review_write."""

from __future__ import annotations

import base64
import json
import logging
import posixpath
import re
import shlex
from typing import Any

from docker.errors import NotFound

from sunaba.journal import record_boundary_crossing
from sunaba.tools.common import _docker, container_not_found_error
from sunaba.tools.github_api import (
    _github_api_request,
    _github_api_request_list_all,
    _resolve_vcs_token,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

_REPO_FORMAT_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


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
