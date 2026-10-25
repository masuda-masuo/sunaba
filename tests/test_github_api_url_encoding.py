"""Regressions for branch/path/query URL encoding in GitHub API interactions (Issue #959)."""

from __future__ import annotations

import base64
import io
import json
import os
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from sunaba.tools.vcs.publishing import (
    AutoIncludeResult,
    _fetch_base_auto_include,
    _fetch_base_tip_blobs,
    _host_read_branch_ref,
    _try_api_push,
)
from tests.test_publish_cut_parent import (
    _FakeResponse,
    _git,
    _HostApi,
    _PublishContext,
    _sha,
    repo_setup,
)

# ============================================================================
# Control / regression: Prove unencoded urllib Request.selector loses #fragment
# ============================================================================


def test_control_urllib_selector_loses_hash() -> None:
    """Control regression proving unencoded urllib.request.Request loses `#fragment` in selector.

    When Request is passed an unencoded URL containing `#1`, it treats `#1` as
    a URI fragment. Transmitted HTTP requests (represented by `request.selector`)
    strip the fragment completely.
    """
    import urllib.request

    req_unencoded = urllib.request.Request("https://api.github.com/repos/o/r/git/refs/heads/feat#1")
    assert req_unencoded.selector == "/repos/o/r/git/refs/heads/feat"
    assert req_unencoded.full_url == "https://api.github.com/repos/o/r/git/refs/heads/feat#1"

    req_encoded = urllib.request.Request("https://api.github.com/repos/o/r/git/ref/heads/feat%231")
    assert req_encoded.selector == "/repos/o/r/git/ref/heads/feat%231"
    assert req_encoded.full_url == "https://api.github.com/repos/o/r/git/ref/heads/feat%231"


def test_host_api_selector_unencoded_vs_encoded_hash(repo_setup: dict[str, str]) -> None:
    """Validate shared _HostApi fake resolves unencoded vs encoded branch with hash.

    Using the bare-remote fixture, create distinct legal branches `feat` and `feat#1`
    with distinct commit tips.
    Raw unencoded urllib Request('.../heads/feat#1') through _HostApi.urlopen must
    return the `feat` tip because urllib's selector loses the `#1` fragment.
    Encoded `feat%231` through _HostApi.urlopen must return the `feat#1` tip.
    """
    origin = repo_setup["origin"]
    clone = repo_setup["clone"]

    # Create distinct legal branch 'feat' with its own commit tip
    _git(clone, "checkout", "-b", "feat")
    (Path(clone) / "feat.txt").write_text("feat\n")
    _git(clone, "add", "feat.txt")
    _git(clone, "commit", "-m", "commit on feat")
    feat_sha = _sha(clone, "HEAD")
    _git(clone, "push", "origin", "feat")

    # Create distinct legal branch 'feat#1' with distinct commit tip
    _git(clone, "checkout", "main")
    _git(clone, "checkout", "-b", "feat#1")
    (Path(clone) / "feat1.txt").write_text("feat#1\n")
    _git(clone, "add", "feat1.txt")
    _git(clone, "commit", "-m", "commit on feat#1")
    feat1_sha = _sha(clone, "HEAD")
    _git(clone, "push", "origin", "feat#1")

    assert feat_sha != feat1_sha

    api = _HostApi(origin, publish_branch="feat#1", base_branch="main")

    # Raw unencoded Request treats `#1` as URI fragment; selector loses `#1`.
    req_unencoded = urllib.request.Request("https://api.github.com/repos/owner/repo/git/ref/heads/feat#1")
    assert req_unencoded.selector == "/repos/owner/repo/git/ref/heads/feat"
    resp_unencoded = api.urlopen(req_unencoded)
    data_unencoded = json.loads(resp_unencoded.read().decode("utf-8"))
    assert data_unencoded["object"]["sha"] == feat_sha

    # Properly percent-encoded Request preserves '%231' in selector.
    req_encoded = urllib.request.Request("https://api.github.com/repos/owner/repo/git/ref/heads/feat%231")
    assert req_encoded.selector == "/repos/owner/repo/git/ref/heads/feat%231"
    resp_encoded = api.urlopen(req_encoded)
    data_encoded = json.loads(resp_encoded.read().decode("utf-8"))
    assert data_encoded["object"]["sha"] == feat1_sha


# ============================================================================
# 1. Helper: ref_heads_path in sunaba.tools.github_api
# ============================================================================


@pytest.mark.parametrize(
    "branch,expected_encoded",
    [
        ("feat#1", "feat%231"),
        ("a?b", "a%3Fb"),
        ("x%y", "x%25y"),
        ("feat/x", "feat%2Fx"),
    ],
)
def test_ref_heads_path(branch: str, expected_encoded: str) -> None:
    from sunaba.tools.github_api import ref_heads_path

    # Singular default
    assert (
        ref_heads_path("owner/repo", branch)
        == f"/repos/owner/repo/git/ref/heads/{expected_encoded}"
    )
    # Plural option (for PATCH)
    assert (
        ref_heads_path("owner/repo", branch, plural=True)
        == f"/repos/owner/repo/git/refs/heads/{expected_encoded}"
    )


# ============================================================================
# 2. _host_read_branch_ref uses helper without drift
# ============================================================================


@pytest.mark.parametrize(
    "branch,expected_encoded",
    [
        ("feat#1", "feat%231"),
        ("a?b", "a%3Fb"),
        ("x%y", "x%25y"),
        ("feat/x", "feat%2Fx"),
    ],
)
def test_host_read_branch_ref_encoding(branch: str, expected_encoded: str) -> None:
    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        return _FakeResponse({"object": {"sha": "0123456789abcdef0123456789abcdef01234567"}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _host_read_branch_ref("owner/repo", "tok", branch)
        assert res.sha == "0123456789abcdef0123456789abcdef01234567"
        assert len(captured_selectors) >= 1
        assert captured_selectors[0] == f"/repos/owner/repo/git/ref/heads/{expected_encoded}"


# ============================================================================
# 3. Auto-include: feature and base branch reads
# ============================================================================


@pytest.mark.parametrize(
    "branch,expected_encoded",
    [
        ("feat#1", "feat%231"),
        ("a?b", "a%3Fb"),
        ("x%y", "x%25y"),
        ("feat/x", "feat%2Fx"),
    ],
)
def test_auto_include_feature_branch_encoding(branch: str, expected_encoded: str) -> None:
    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch_base_auto_include("owner/repo", "tok", branch=branch, base_branch="main")
        assert len(captured_selectors) >= 1
        assert captured_selectors[0] == f"/repos/owner/repo/git/ref/heads/{expected_encoded}"


@pytest.mark.parametrize(
    "branch,expected_encoded",
    [
        ("feat#1", "feat%231"),
        ("a?b", "a%3Fb"),
        ("x%y", "x%25y"),
        ("feat/x", "feat%2Fx"),
    ],
)
def test_auto_include_base_branch_encoding(branch: str, expected_encoded: str) -> None:
    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch_base_auto_include("owner/repo", "tok", branch="dev", base_branch=branch)
        assert len(captured_selectors) >= 2
        assert captured_selectors[1] == f"/repos/owner/repo/git/ref/heads/{expected_encoded}"


def test_auto_include_distinct_tips_feat_and_feat1() -> None:
    """Fake remote has distinct feat and feat#1 tips; exact requested branch tip must win."""
    compare_called_with = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        selector = getattr(req, "selector", str(req))
        parsed = urlsplit(selector)
        path = parsed.path
        if "/compare/" in path:
            compare_called_with.append(path)
            return _FakeResponse({"files": []})
        if path.endswith("/heads/feat%231"):
            return _FakeResponse({"object": {"sha": "2222222222222222222222222222222222222222"}})
        if path.endswith("/heads/feat"):
            return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})
        if path.endswith("/heads/main"):
            return _FakeResponse({"object": {"sha": "3333333333333333333333333333333333333333"}})
        return _FakeResponse({})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch_base_auto_include("owner/repo", "tok", branch="feat#1", base_branch="main")
        assert len(compare_called_with) == 1
        assert "2222222222222222222222222222222222222222" in compare_called_with[0], (
            f"Expected feat#1 tip (2222...) in compare call, but got: {compare_called_with[0]}"
        )


def test_auto_include_singular_feature_tip_404_returns_empty_result() -> None:
    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        url = getattr(req, "full_url", str(req))
        raise HTTPError(url, 404, "Not Found", Message(), io.BytesIO(b'{"message": "Not Found"}'))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _fetch_base_auto_include("owner/repo", "tok", branch="feat#1", base_branch="main")
        assert res == AutoIncludeResult(included={}, skipped=[])


def test_auto_include_missing_feat_with_prefix_feat_other_returns_empty_result() -> None:
    """Remote fake has feat-other but no exact feat.

    _fetch_base_auto_include with branch='feat', base_branch='main' must:
    - issue singular /git/ref/heads/feat
    - receive 404 and return AutoIncludeResult(included={}, skipped=[])
    - make no compare, content, or base ref lookups and never use the feat-other tip.
    The fake realistically distinguishes plural prefix endpoints (which would return
    a list containing feat-other) from the singular exact endpoint (404).
    """
    calls: list[str] = []
    feat_other_sha = "9999999999999999999999999999999999999999"
    main_sha = "3333333333333333333333333333333333333333"

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        selector = getattr(req, "selector", str(req))
        parsed = urlsplit(selector)
        path = parsed.path
        calls.append(selector)

        # Plural refs endpoint: /repos/.../git/refs/heads/<prefix>
        # GitHub returns a list of matching refs for any branch starting with <prefix>.
        if "/git/refs/heads" in path:
            raw_prefix = path.split("/git/refs/heads", 1)[1].lstrip("/")
            prefix = unquote(raw_prefix)
            matches = []
            if "feat-other".startswith(prefix):
                matches.append({
                    "ref": "refs/heads/feat-other",
                    "object": {"sha": feat_other_sha},
                })
            return _FakeResponse(matches)

        # Singular ref endpoint: /repos/.../git/ref/heads/<branch>
        # GitHub returns the exact ref object or 404 Not Found.
        if "/git/ref/heads/" in path:
            raw_branch = path.split("/git/ref/heads/", 1)[1]
            branch_name = unquote(raw_branch)
            if branch_name == "feat-other":
                return _FakeResponse({"object": {"sha": feat_other_sha}})
            if branch_name == "main":
                return _FakeResponse({"object": {"sha": main_sha}})
            url = getattr(req, "full_url", str(req))
            raise HTTPError(
                url, 404, "Not Found", Message(), io.BytesIO(b'{"message": "Not Found"}')
            )

        if "/compare/" in path:
            return _FakeResponse({"files": []})

        if "/contents/" in path:
            return _FakeResponse({"content": "e30=", "encoding": "base64"})

        url = getattr(req, "full_url", str(req))
        raise HTTPError(
            url, 404, "Not Found", Message(), io.BytesIO(b'{"message": "Not Found"}')
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _fetch_base_auto_include("owner/repo", "tok", branch="feat", base_branch="main")
        assert res == AutoIncludeResult(included={}, skipped=[])
        assert calls == ["/repos/owner/repo/git/ref/heads/feat"]


def test_auto_include_singular_feature_tip_500_returns_none() -> None:
    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        url = getattr(req, "full_url", str(req))
        raise HTTPError(
            url, 500, "Internal Server Error", Message(), io.BytesIO(b'{"message": "Internal Server Error"}')
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _fetch_base_auto_include("owner/repo", "tok", branch="feat#1", base_branch="main")
        assert res is None


def test_auto_include_base_tip_404_returns_none() -> None:
    call_count = 0

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})
        raise HTTPError(req.full_url, 404, "Not Found", Message(), io.BytesIO(b'{"message": "Not Found"}'))

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _fetch_base_auto_include("owner/repo", "tok", branch="feat#1", base_branch="main")
        assert res is None


# ============================================================================
# 4. Auto-include file path with '#' and space
# ============================================================================


def test_auto_include_filename_encoding() -> None:
    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        path = urlsplit(req.selector).path
        if "/compare/" in path:
            return _FakeResponse({
                "files": [{"filename": "dir/my # file.txt", "status": "added"}]
            })
        if "/contents/" in path:
            encoded = base64.b64encode(b"hello world").decode("ascii")
            return _FakeResponse({"content": encoded, "encoding": "base64"})
        return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = _fetch_base_auto_include("owner/repo", "tok", branch="feat", base_branch="main")
        assert res is not None
        assert "dir/my # file.txt" in res.included
        contents_calls = [s for s in captured_selectors if "/contents/" in s]
        assert len(contents_calls) == 1
        # '/' preserved, '#' and ' ' encoded
        assert "/contents/dir/my%20%23%20file.txt?ref=1111111111111111111111111111111111111111" in contents_calls[0]


# ============================================================================
# 5. _fetch_base_tip_blobs
# ============================================================================


@pytest.mark.parametrize(
    "branch,expected_encoded",
    [
        ("feat#1", "feat%231"),
        ("a?b", "a%3Fb"),
        ("x%y", "x%25y"),
        ("feat/x", "feat%2Fx"),
    ],
)
def test_base_tip_blobs_encoding(branch: str, expected_encoded: str) -> None:
    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch_base_tip_blobs("owner/repo", "tok", base_branch=branch, paths=["a.txt"])
        assert len(captured_selectors) >= 1
        assert captured_selectors[0] == f"/repos/owner/repo/git/ref/heads/{expected_encoded}"


def test_base_tip_blobs_distinct_tips() -> None:
    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        path = urlsplit(req.selector).path
        if path.endswith("/heads/feat%231"):
            return _FakeResponse({"object": {"sha": "2222222222222222222222222222222222222222"}})
        if path.endswith("/heads/feat"):
            return _FakeResponse({"object": {"sha": "1111111111111111111111111111111111111111"}})
        return _FakeResponse({})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        tip = _fetch_base_tip_blobs("owner/repo", "tok", base_branch="feat#1", paths=["a.txt"])
        assert tip.base_sha == "2222222222222222222222222222222222222222", (
            f"Expected feat#1 tip (2222...), got {tip.base_sha!r}"
        )


# ============================================================================
# 6. secret_scan.py baseline contents ref query
# ============================================================================


@pytest.mark.parametrize(
    "branch,expected_encoded",
    [
        ("feat#1", "feat%231"),
        ("a?b", "a%3Fb"),
        ("x%y", "x%25y"),
        ("feat/x", "feat%2Fx"),
    ],
)
def test_secret_scan_ref_query_encoding(branch: str, expected_encoded: str) -> None:
    from sunaba.tools.secret_scan import _fetch_baseline_from_base_branch

    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        return _FakeResponse({"content": "e30=", "encoding": "base64"})

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _fetch_baseline_from_base_branch("owner/repo", "tok", base_branch=branch)
        assert len(captured_selectors) == 1
        selector = captured_selectors[0]
        parsed = urlsplit(selector)
        assert parsed.path == "/repos/owner/repo/contents/.secrets.baseline"
        assert parsed.query == f"ref={expected_encoded}"
        qs = parse_qs(parsed.query)
        assert qs == {"ref": [branch]}


# ============================================================================
# 7. github_api.py PR lookup query encoding
# ============================================================================


def test_pr_head_query_encoding() -> None:
    from sunaba.tools.github_api import _create_pr_via_api

    captured_selectors = []

    def fake_urlopen(req: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        captured_selectors.append(req.selector)
        if req.get_method() == "POST":
            raise HTTPError(
                req.full_url,
                422,
                "Unprocessable Entity",
                Message(),
                io.BytesIO(b'{"message": "A pull request already exists for owner:fix/x."}'),
            )
        resp = _FakeResponse([{"html_url": "https://github.com/owner/repo/pull/42"}])
        resp.headers = {}  # type: ignore[attr-defined]
        return resp

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        url = _create_pr_via_api("owner/repo", "fix/x", "title", "", "main", "tok")
        assert url == "https://github.com/owner/repo/pull/42"
        assert len(captured_selectors) == 2
        get_selector = captured_selectors[1]
        parsed = urlsplit(get_selector)
        assert parsed.path == "/repos/owner/repo/pulls"
        assert parsed.query == "head=owner%3Afix%2Fx&state=open"
        qs = parse_qs(parsed.query)
        assert qs == {"head": ["owner:fix/x"], "state": ["open"]}


# ============================================================================
# 8. Embedded script test with gh stub and real script control flow
# ============================================================================


_FAKE_GH_SCRIPT = r"""#!/usr/bin/env python3
import base64, json, os, subprocess, sys
from urllib.parse import unquote

origin = os.environ["FAKE_GH_ORIGIN"]
log = os.environ["FAKE_GH_CALLS"]
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
    raw_subpath = "/".join(parts[parts.index("heads") + 1:])
    branch_name = unquote(raw_subpath)
    ref = f"refs/heads/{branch_name}"
    result = git("rev-parse", "--verify", ref, check=False)
    if result.returncode:
        fail(404, "Not Found")
    print(json.dumps({"object": {"sha": result.stdout.strip()}}))
    sys.exit(0)

if method == "POST" and parts[-1] == "blobs":
    data = base64.b64decode(body["content"])
    result = subprocess.run(["git", "--git-dir", origin, "hash-object", "-w", "--stdin"],
                            input=data, capture_output=True, check=True)
    sha = result.stdout.decode().strip()
    print(json.dumps({"sha": sha}))
    sys.exit(0)

if method == "POST" and parts[-1] == "trees":
    rows = [f'{x["mode"]} {x["type"]} {x["sha"]}\t{x["path"]}' for x in body["tree"]]
    result = subprocess.run(["git", "--git-dir", origin, "mktree"],
                            input=("\n".join(rows) + "\n").encode(), capture_output=True, check=True)
    sha = result.stdout.decode().strip()
    print(json.dumps({"sha": sha}))
    sys.exit(0)

if method == "POST" and parts[-1] == "commits":
    command = ["git", "--git-dir", origin, "commit-tree", body["tree"]]
    for parent in body.get("parents", []):
        command += ["-p", parent]
    command += ["-m", body["message"]]
    env = dict(os.environ, GIT_AUTHOR_NAME="fixture", GIT_AUTHOR_EMAIL="fixture@example.com",
               GIT_COMMITTER_NAME="fixture", GIT_COMMITTER_EMAIL="fixture@example.com")
    result = subprocess.run(command, text=True, capture_output=True, check=True, env=env)
    sha = result.stdout.strip()
    print(json.dumps({"sha": sha}))
    sys.exit(0)

if method == "PATCH" and "refs" in parts:
    raw_subpath = "/".join(parts[parts.index("heads") + 1:])
    branch_name = unquote(raw_subpath)
    ref = f"refs/heads/{branch_name}"
    old = git("rev-parse", "--verify", ref, check=False)
    if old.returncode:
        fail(404, "Not Found")
    git("update-ref", ref, body["sha"])
    print(json.dumps({"object": {"sha": body["sha"]}}))
    sys.exit(0)

if method == "POST" and parts[-1] == "refs":
    ref = body["ref"]
    if git("show-ref", "--verify", ref, check=False).returncode == 0:
        fail(422, "Reference already exists")
    git("update-ref", ref, body["sha"])
    print(json.dumps({"object": {"sha": body["sha"]}}))
    sys.exit(0)

fail(500, f"unsupported {method} {path}")
"""


@pytest.fixture
def publish_ctx(request: pytest.FixtureRequest) -> _PublishContext:
    setup: dict[str, str] = request.getfixturevalue(repo_setup.__name__)
    return _PublishContext(setup["origin"], setup["clone"], setup["tmp"])


def _setup_fake_gh(tmp_path: Path, ctx: _PublishContext) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    gh.chmod(0o755)
    calls = tmp_path / "gh-calls.jsonl"
    calls.write_text("", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FAKE_GH_ORIGIN": ctx.origin,
        "FAKE_GH_CALLS": str(calls),
    }
    return env, calls


def _read_gh_calls(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_sandbox_create_pr_script_patch_existing_ref(
    publish_ctx: _PublishContext, tmp_path: Path
) -> None:
    """Existing ref: GET ref_path and PATCH refs/heads/ must be encoded."""
    ctx = publish_ctx
    base = _sha(ctx.clone, "origin/main")
    # Branch name with '#'
    branch = "feat#1"
    ref = f"refs/heads/{branch}"
    _git(ctx.origin, "update-ref", ref, base)

    # Make local commit
    (Path(ctx.clone) / "test.txt").write_text("change\n")
    _git(ctx.clone, "add", "test.txt")
    _git(ctx.clone, "commit", "-m", "commit on feat#1")
    verified_sha = _sha(ctx.clone, "HEAD")

    env, log_file = _setup_fake_gh(tmp_path, ctx)

    res = _try_api_push(
        ctx.container,
        "cid",
        "owner/repo",
        branch,
        ctx.clone,
        env=env,
        verified_sha=verified_sha,
        allow_force_push=True,
    )
    assert res["status"] == "ok", res
    calls = _read_gh_calls(log_file)

    # Check GET existence check
    get_ref = [c for c in calls if c["method"] == "GET" and "git/ref" in c["path"]]
    assert len(get_ref) == 1
    assert get_ref[0]["path"] == "repos/owner/repo/git/ref/heads/feat%231"

    # Check PATCH
    patch_ref = [c for c in calls if c["method"] == "PATCH" and "git/refs" in c["path"]]
    assert len(patch_ref) == 1
    assert patch_ref[0]["path"] == "repos/owner/repo/git/refs/heads/feat%231"


def test_sandbox_create_pr_script_patch_slash_branch(
    publish_ctx: _PublishContext, tmp_path: Path
) -> None:
    """Branch with slash: GET and PATCH must encode slash as %2F."""
    ctx = publish_ctx
    base = _sha(ctx.clone, "origin/main")
    branch = "feat/x"
    ref = f"refs/heads/{branch}"
    _git(ctx.origin, "update-ref", ref, base)

    (Path(ctx.clone) / "test.txt").write_text("change\n")
    _git(ctx.clone, "add", "test.txt")
    _git(ctx.clone, "commit", "-m", "commit on feat/x")
    verified_sha = _sha(ctx.clone, "HEAD")

    env, log_file = _setup_fake_gh(tmp_path, ctx)

    res = _try_api_push(
        ctx.container,
        "cid",
        "owner/repo",
        branch,
        ctx.clone,
        env=env,
        verified_sha=verified_sha,
        allow_force_push=True,
    )
    assert res["status"] == "ok", res
    calls = _read_gh_calls(log_file)

    get_ref = [c for c in calls if c["method"] == "GET" and "git/ref" in c["path"]]
    assert len(get_ref) == 1
    assert get_ref[0]["path"] == "repos/owner/repo/git/ref/heads/feat%2Fx"

    patch_ref = [c for c in calls if c["method"] == "PATCH" and "git/refs" in c["path"]]
    assert len(patch_ref) == 1
    assert patch_ref[0]["path"] == "repos/owner/repo/git/refs/heads/feat%2Fx"


def test_sandbox_create_pr_script_post_new_ref(
    publish_ctx: _PublishContext, tmp_path: Path
) -> None:
    """New ref: GET 404, then POST JSON ref MUST be raw refs/heads/{branch}."""
    ctx = publish_ctx
    branch = "feat#1"

    (Path(ctx.clone) / "test.txt").write_text("change\n")
    _git(ctx.clone, "add", "test.txt")
    _git(ctx.clone, "commit", "-m", "commit on feat#1")
    verified_sha = _sha(ctx.clone, "HEAD")

    env, log_file = _setup_fake_gh(tmp_path, ctx)

    res = _try_api_push(
        ctx.container,
        "cid",
        "owner/repo",
        branch,
        ctx.clone,
        env=env,
        verified_sha=verified_sha,
        allow_force_push=True,
    )
    assert res["status"] == "ok", res
    calls = _read_gh_calls(log_file)

    # GET was attempted and encoded
    get_ref = [c for c in calls if c["method"] == "GET" and "git/ref" in c["path"]]
    assert len(get_ref) == 1
    assert get_ref[0]["path"] == "repos/owner/repo/git/ref/heads/feat%231"

    # POST to /git/refs with raw ref name in body
    post_ref = [c for c in calls if c["method"] == "POST" and c["path"].endswith("git/refs")]
    assert len(post_ref) == 1
    assert post_ref[0]["body"]["ref"] == "refs/heads/feat#1"


def test_sandbox_create_pr_script_parent_lookup_fallback(
    publish_ctx: _PublishContext, tmp_path: Path
) -> None:
    """Unpinned source: parent lookup loop encodes candidate branch, falls back to main unchanged."""
    ctx = publish_ctx
    branch = "feat#1"

    (Path(ctx.clone) / "test.txt").write_text("change\n")
    _git(ctx.clone, "add", "test.txt")
    _git(ctx.clone, "commit", "-m", "unpinned commit on feat#1")

    env, log_file = _setup_fake_gh(tmp_path, ctx)

    # verified_sha="" -> triggers parent lookup loop
    res = _try_api_push(
        ctx.container,
        "cid",
        "owner/repo",
        branch,
        ctx.clone,
        env=env,
        verified_sha="",
        allow_force_push=True,
    )
    assert res["status"] == "ok", res
    calls = _read_gh_calls(log_file)

    get_refs = [c for c in calls if c["method"] == "GET" and "git/ref" in c["path"]]
    paths = [c["path"] for c in get_refs]
    assert "repos/owner/repo/git/ref/heads/feat%231" in paths
    assert "repos/owner/repo/git/ref/heads/main" in paths
