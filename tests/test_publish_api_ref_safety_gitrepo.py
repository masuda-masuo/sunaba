"""Real-git safety tests for pinned GitHub API ref updates (PR952)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from sunaba.tools.publish_ops import git_push_with_fallback
from sunaba.tools.vcs.publishing import _try_api_push
from tests.test_publish_cut_parent import (
    _git,
    _PublishContext,
    _sha,
    repo_setup,
)

_FAKE_GH = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
origin = os.environ["FAKE_GH_ORIGIN"]
log = os.environ["FAKE_GH_CALLS"]
failure = os.environ.get("FAKE_GH_PATCH_FAILURE", "")
args = sys.argv[1:]
method, path, body = "GET", "", {}
i = 0
while i < len(args):
    if args[i] == "api":
        i += 1
    elif args[i] == "-X":
        method, i = args[i + 1], i + 2
    elif args[i] == "--input":
        with open(args[i + 1], encoding="utf-8") as handle:
            body = json.load(handle)
        i += 2
    else:
        path, i = args[i], i + 1
with open(log, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"method": method, "path": path, "body": body}) + "\n")
parts = path.strip("/").split("/")
def fail(status, message):
    sys.stderr.write(f"gh: {message} (HTTP {status})\n")
    sys.exit(1)
def git(*args, check=True):
    return subprocess.run(["git", "--git-dir", origin, *args], text=True,
                          capture_output=True, check=check)
if method == "GET" and "ref" in parts:
    ref = "refs/heads/" + "/".join(parts[parts.index("heads") + 1:])
    result = git("rev-parse", "--verify", ref, check=False)
    if result.returncode:
        fail(404, "Not Found")
    print(json.dumps({"object": {"sha": result.stdout.strip()}}))
    sys.exit(0)
if method == "POST" and parts[-1] == "blobs":
    import base64
    data = base64.b64decode(body["content"])
    result = subprocess.run(["git", "--git-dir", origin, "hash-object", "-w", "--stdin"],
                            input=data, capture_output=True, check=True)
    sha = result.stdout.decode().strip()
elif method == "POST" and parts[-1] == "trees":
    rows = [f'{x["mode"]} {x["type"]} {x["sha"]}\t{x["path"]}' for x in body["tree"]]
    result = subprocess.run(["git", "--git-dir", origin, "mktree"],
                            input=("\n".join(rows) + "\n").encode(), capture_output=True, check=True)
    sha = result.stdout.decode().strip()
elif method == "POST" and parts[-1] == "commits":
    command = ["git", "--git-dir", origin, "commit-tree", body["tree"]]
    for parent in body.get("parents", []):
        command += ["-p", parent]
    command += ["-m", body["message"]]
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    result = subprocess.run(command, text=True, capture_output=True, check=True, env=env)
    sha = result.stdout.strip()
elif method in ("PATCH", "POST") and "refs" in parts:
    if method == "PATCH":
        if failure:
            status, message = failure.split(":", 1)
            fail(int(status), message)
        ref = "refs/heads/" + "/".join(parts[parts.index("heads") + 1:])
        old = git("rev-parse", "--verify", ref, check=False)
        if old.returncode:
            fail(404, "Not Found")
        if body.get("force") is not True:
            ancestor = git("merge-base", "--is-ancestor", old.stdout.strip(), body["sha"], check=False)
            if ancestor.returncode:
                fail(422, "Update is not a fast forward")
    else:
        ref = body["ref"]
        if git("show-ref", "--verify", ref, check=False).returncode == 0:
            fail(422, "Reference already exists")
    git("update-ref", ref, body["sha"])
    print(json.dumps({"object": {"sha": body["sha"]}}))
    sys.exit(0)
else:
    fail(500, f"unsupported {method} {path}")
print(json.dumps({"sha": sha}))
"""

@pytest.fixture
def publish_ctx(request: pytest.FixtureRequest) -> _PublishContext:
    setup: dict[str, str] = request.getfixturevalue(repo_setup.__name__)
    return _PublishContext(setup["origin"], setup["clone"], setup["tmp"])


def _api_env(tmp_path: Path, ctx: _PublishContext, **extra: str) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    calls = tmp_path / "api-calls.jsonl"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_GH_ORIGIN": ctx.origin,
        "FAKE_GH_CALLS": str(calls),
        **extra,
    }
    return env, calls


def _source_commit(ctx: _PublishContext) -> tuple[str, str]:
    base = _sha(ctx.clone, "origin/main")
    _git(ctx.clone, "checkout", "-B", "api-source", base)
    _git(ctx.clone, "config", "user.name", "fixture")
    _git(ctx.clone, "config", "user.email", "fixture@example.com")
    (Path(ctx.clone) / "README.md").write_text("# API source\n", encoding="utf-8")
    _git(ctx.clone, "add", "README.md")
    _git(ctx.clone, "commit", "-m", "pinned source")
    return _sha(ctx.clone, "HEAD"), base


def _advance_remote(ctx: _PublishContext, ref: str, parent: str) -> str:
    tree = _sha(ctx.origin, f"{parent}^{{tree}}")
    result = _git(ctx.origin, "commit-tree", tree, "-p", parent, "-m", "remote advance")
    assert result.returncode == 0, result.stderr
    tip = result.stdout.strip()
    update = _git(ctx.origin, "update-ref", ref, tip)
    assert update.returncode == 0, update.stderr
    return tip


def _calls(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_pinned_api_fast_forward_update_succeeds_without_force(
    publish_ctx: _PublishContext, tmp_path: Path,
) -> None:
    source, base = _source_commit(publish_ctx)
    ref = "refs/heads/topic"
    _git(publish_ctx.origin, "update-ref", ref, base)
    env, log = _api_env(tmp_path, publish_ctx)

    result = _try_api_push(
        publish_ctx.container, "cid", "owner/repo", "topic", publish_ctx.clone,
        env=env, verified_sha=source,
    )

    calls = _calls(log)
    patches = [call for call in calls if call["method"] == "PATCH"]
    assert result["status"] == "ok", result
    assert patches and patches[0]["body"]["force"] is False
    api_commit = patches[0]["body"]["sha"]
    assert _sha(publish_ctx.origin, ref) == api_commit
    assert result["sha"] == api_commit[:7]
    raw_commit = _git(publish_ctx.origin, "cat-file", "-p", api_commit)
    assert raw_commit.returncode == 0, raw_commit.stderr
    assert f"parent {base}" in raw_commit.stdout.splitlines()
    assert not any(
        call["method"] == "POST" and call["path"].endswith("/git/refs")
        for call in calls
    )


def test_pinned_api_rejects_remote_advance_without_force_or_create(
    publish_ctx: _PublishContext, tmp_path: Path,
) -> None:
    source, base = _source_commit(publish_ctx)
    ref = "refs/heads/topic"
    _git(publish_ctx.origin, "update-ref", ref, base)
    advanced_tip: dict[str, str] = {}

    def run(command: str, env: dict[str, str] | None = None):
        if command == "git push origin topic":
            advanced_tip["sha"] = _advance_remote(publish_ctx, ref, base)
            return 1, "", "remote rejected: non-fast-forward"
        return 0, "", ""

    env, log = _api_env(tmp_path, publish_ctx)
    result, sha, transport = git_push_with_fallback(
        run, repo="owner/repo", branch="topic", cid="cid",
        push_cmd="git push origin topic", push_env=None,
        network_off=False, token_missing=False,
        try_api_push=lambda: _try_api_push(
            publish_ctx.container, "cid", "owner/repo", "topic", publish_ctx.clone,
            env=env, verified_sha=source,
        ),
        record_crossing=lambda *_: None, verified_sha=source,
    )
    calls = _calls(log)
    assert result is not None and result["status"] == "error"
    assert "Update is not a fast forward" in result["api_error"]
    assert result["api_error_classification"] == "non_fast_forward"
    assert "rerun publish" in result["hint"]
    assert sha == source[:7] and transport == ""
    assert _sha(publish_ctx.origin, ref) == advanced_tip["sha"]
    patch_calls = [call for call in calls if call["method"] == "PATCH"]
    assert patch_calls and patch_calls[0]["body"]["force"] is False
    assert not any(call["method"] == "POST" and call["path"].endswith("/git/refs") for call in calls)


def test_explicit_force_authorization_updates_existing_ref(
    publish_ctx: _PublishContext, tmp_path: Path,
) -> None:
    source, base = _source_commit(publish_ctx)
    ref = "refs/heads/topic"
    _git(publish_ctx.origin, "update-ref", ref, base)
    _advance_remote(publish_ctx, ref, base)
    env, log = _api_env(tmp_path, publish_ctx)

    result = _try_api_push(
        publish_ctx.container, "cid", "owner/repo", "topic", publish_ctx.clone,
        env=env, verified_sha=source, allow_force_push=True,
    )

    calls = _calls(log)
    patches = [call for call in calls if call["method"] == "PATCH"]
    assert result["status"] == "ok", result
    assert patches and patches[0]["body"]["force"] is True
    assert _sha(publish_ctx.origin, ref) == result["sha"] or _sha(publish_ctx.origin, ref).startswith(result["sha"])


def test_missing_branch_uses_post_and_validation_errors_do_not(
    publish_ctx: _PublishContext, tmp_path: Path,
) -> None:
    source, _base = _source_commit(publish_ctx)
    env, log = _api_env(tmp_path, publish_ctx)

    created = _try_api_push(
        publish_ctx.container, "cid", "owner/repo", "new-topic", publish_ctx.clone,
        env=env, verified_sha=source,
    )
    calls = _calls(log)
    posts = [call for call in calls if call["method"] == "POST" and call["path"].endswith("/git/refs")]
    assert created["status"] == "ok", created
    assert len(posts) == 1
    assert posts[0]["body"]["ref"] == "refs/heads/new-topic"


@pytest.mark.parametrize(
    ("failure", "expected_class"),
    [
        ("403:Resource not accessible", "api_rejection"),
        ("500:Internal Server Error", "api_rejection"),
        ("422:Validation Failed", "api_rejection"),
        ("422:Update is not a fast forward", "non_fast_forward"),
    ],
)
def test_existing_branch_patch_errors_never_create_or_overclassify(
    publish_ctx: _PublishContext, tmp_path: Path, failure: str, expected_class: str,
) -> None:
    source, base = _source_commit(publish_ctx)
    ref = "refs/heads/topic"
    _git(publish_ctx.origin, "update-ref", ref, base)
    env, log = _api_env(tmp_path, publish_ctx, FAKE_GH_PATCH_FAILURE=failure)

    result = _try_api_push(
        publish_ctx.container, "cid", "owner/repo", "topic", publish_ctx.clone,
        env=env, verified_sha=source,
    )

    calls = _calls(log)
    assert result["status"] == "error"
    assert result["classification"] == expected_class
    assert any(call["method"] == "PATCH" for call in calls)
    assert not any(call["method"] == "POST" and call["path"].endswith("/git/refs") for call in calls)
