"""Acceptance tests for host-authorized publish cut parents (#948).

The #934 investigation found three container-controlled routes by which a
forged remote-tracking ref becomes the parent of the public commit:

* a non-symbolic ``refs/remotes/origin/HEAD`` survives ``git fetch`` and is
  used when ``base_branch`` is omitted (S4 in the #934 comment);
* an ``origin/<branch>`` ref for a branch that does not exist on the remote
  survives the fetch (which does not prune) and is preferred over
  ``base_branch`` (S6);
* refs can be rewritten after the authorized fetch but before
  ``git_prepare_commit`` reads them.

A fourth, lower-level route is the local-base fallback inside
``git_prepare_commit``: when ``origin/<base>`` is missing and a local
``<base>`` exists, that local ref becomes the cut parent.

Proposal B from #932 (comparing the public tree with ``base_branch``) is not
what these tests pin.  They pin the accepted design: the host reads the
actual remote SHAs through the GitHub API and compares them with the *real*
commit parents (``HEAD^1``, and ``HEAD^2`` for the #819 merge rebuild).

Everything below the tool is real: a bare git origin, a clone, the
authorized fetch, ``git_prepare_commit``, ancestry and parent resolution.
Only the container boundary (``exec_run``, backed here by a real
``/bin/sh`` in the clone), the host token/proxy lookup, the host-side
auto-include read, the host-side upstream-tip read, and the host GitHub API
(stubbed at the ``urlopen`` boundary with a server backed by the same bare
origin) are stood in for.

Note on ordering: in manifest mode the secret scan deliberately runs before
``git_prepare_commit`` (#676 -- secrets must not enter local history), so a
guard placed after the commit cannot run before that scan.  These tests
therefore pin "no push and no PR after the guard fires", and the success
tests pin that the pre-commit scan still happens before the push.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.vcs.publishing import UpstreamTipRead, publish
from tests.conftest import _FakeClient

CONTAINER_ID = "abc123def456"
REPO = "owner/repo"

#: Phrases a refusal must carry so the operator knows the remote may have
#: moved between the in-container fetch and the host's own ref read.
_RERUN_WORDS = ("rerun", "re-run", "retry", "run publish again")
_ADVANCE_WORDS = ("advanc", "moved", "changed", "behind", "newer")


# ============================================================================
# Real git helpers
# ============================================================================


def _git(workdir: str | Path, *args: str) -> subprocess.CompletedProcess:
    """Run a raw git command in *workdir*."""
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(workdir), *args],  # noqa: S607
        capture_output=True,
        text=True,
    )


def _sha(workdir: str | Path, rev: str) -> str:
    """Resolve *rev* in *workdir* to a full SHA, failing the test if absent."""
    result = _git(workdir, "rev-parse", "--verify", rev)
    assert result.returncode == 0, f"git rev-parse {rev} failed: {result.stderr}"
    return result.stdout.strip()


def _remote_ref(origin_dir: str, branch: str) -> str | None:
    """Return ``refs/heads/<branch>`` in the bare origin, or ``None``."""
    result = _git(origin_dir, "rev-parse", "--verify", f"refs/heads/{branch}")
    return result.stdout.strip() if result.returncode == 0 else None


def _checkout_branch(workdir: str | Path, name: str, start: str) -> None:
    """Create or reset local branch *name* at *start* and check it out."""
    result = _git(workdir, "checkout", "-B", name, start)
    assert result.returncode == 0, f"git checkout -B {name} {start} failed: {result.stderr}"


def _forge_bad_commit(clone: str, parent: str) -> str:
    """Commit the #934 forgery (delete ``b.txt``, add ``evil.txt``) on *parent*."""
    _checkout_branch(clone, "_forge", parent)
    (Path(clone) / "b.txt").unlink(missing_ok=True)
    (Path(clone) / "evil.txt").write_text("undeclared addition\n")
    assert _git(clone, "add", "-A").returncode == 0
    assert _git(clone, "commit", "-m", "forged checkpoint").returncode == 0
    return _sha(clone, "HEAD")


def _commit_tree_child(clone: str, parent: str) -> str:
    """Create a commit whose only parent is *parent* and whose tree is its tree.

    Used for the #819 second-parent forgery: the object must be resolvable in
    the container without moving HEAD away from the merge commit the publish
    pipeline needs to detect.
    """
    tree = _sha(clone, f"{parent}^{{tree}}")
    result = _git(
        clone,
        "-c", "user.name=forge",
        "-c", "user.email=forge@example.com",
        "commit-tree", tree, "-p", parent, "-m", "forged upstream advance",
    )
    assert result.returncode == 0, f"git commit-tree failed: {result.stderr}"
    return result.stdout.strip()


def _edit_readme(clone: str, text: str = "# Initial\ndecided change\n") -> None:
    """Give a manifest publish something real to commit."""
    (Path(clone) / "README.md").write_text(text)


def _upstream_clone(ctx: "_PublishContext") -> str:
    """Second clone used to move the bare origin; the container never sees it."""
    other = Path(ctx.tmp) / "upstream-clone"
    if not other.exists():
        clone = _git(ctx.tmp, "clone", ctx.origin, str(other))
        assert clone.returncode == 0, f"upstream clone failed: {clone.stderr}"
        _git(str(other), "config", "user.email", "upstream@example.com")
        _git(str(other), "config", "user.name", "upstream fixture")
    return str(other)


def _remote_branch(
    ctx: "_PublishContext",
    name: str,
    changes: dict[str, str],
    message: str = "upstream branch",
) -> str:
    """Create *name* on the bare origin off its current main; return its SHA."""
    other = _upstream_clone(ctx)
    assert _git(other, "fetch", "origin").returncode == 0
    _checkout_branch(other, name, "origin/main")
    for path, text in changes.items():
        target = Path(other) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        _git(other, "add", path)
    commit = _git(other, "commit", "-m", message)
    assert commit.returncode == 0, f"upstream commit failed: {commit.stderr}"
    push = _git(other, "push", "origin", f"refs/heads/{name}")
    assert push.returncode == 0, f"upstream push failed: {push.stderr}"
    return _sha(other, "HEAD")


def _advance_main(
    ctx: "_PublishContext",
    changes: dict[str, str],
    message: str = "upstream main advance",
) -> str:
    """Land *changes* on the bare origin's main; return the new main tip."""
    return _remote_branch(ctx, "main", changes, message)


def _origin_tip_blobs(
    origin_dir: str, base_branch: str, paths: list[str],
) -> UpstreamTipRead:
    """Answer the host-side upstream-tip read from the bare origin.

    This is the existing #863 test seam, not the new #948 guard: it keeps the
    unrelated upstream-overwrite guard from making network calls.
    """
    tip = _remote_ref(origin_dir, base_branch) or ""
    blobs: dict[str, str] = {}
    for path in paths:
        fields = _git(origin_dir, "ls-tree", tip, "--", path).stdout.split()
        blobs[path] = fields[2] if len(fields) >= 3 and fields[1] == "blob" else "-"
    return UpstreamTipRead(base_sha=tip, blobs=blobs, error="")


# ============================================================================
# Container boundary: real shell in the real clone
# ============================================================================


class _RealGitContainer:
    """docker-py-shaped container whose execs run for real in the clone.

    ``publish`` reaches git only through ``container.exec_run``, so this is
    what lets the tests exercise the whole tool.  ``gitleaks version`` is
    answered as "absent" so the secret scan deterministically reports
    ``skipped`` regardless of what is installed on the test host; every other
    command is a real ``/bin/sh -c``.

    ``after_fetch_once`` injects the #948 race: the hook fires once, right
    after the authorized fetch has updated the remote-tracking refs and
    before the next command reads them.
    """

    def __init__(self, working_dir: str, events: list[tuple[str, Any]]) -> None:
        self.working_dir = working_dir
        self.events = events
        self.labels: dict[str, str] = {}
        self.commands: list[str] = []
        self.after_fetch_once: Any = None

    def exec_run(
        self,
        cmd: list[str] | None = None,
        stdout: bool = True,
        stderr: bool = True,
        environment: dict[str, str] | None = None,
        demux: bool = False,
        workdir: str | None = None,
        **_kwargs: Any,
    ):
        """Run the requested command in the clone and record it."""
        argv = list(cmd or [])
        if argv[:2] == ["gitleaks", "version"]:
            return self._reply(1, b"", b"", demux)

        if len(argv) > 2 and argv[0] in ("/bin/sh", "sh"):
            shell_cmd = argv[2]
        else:
            shell_cmd = " ".join(argv)
        self.commands.append(shell_cmd)
        self.events.append(("cmd", shell_cmd))

        env = None if environment is None else {**os.environ, **environment}
        proc = subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", shell_cmd],
            cwd=workdir or self.working_dir,
            capture_output=True,
            env=env,
        )
        hook = self.after_fetch_once
        if hook is not None and re.search(r"\bfetch origin\b", shell_cmd):
            self.after_fetch_once = None
            hook()
        return self._reply(proc.returncode, proc.stdout, proc.stderr, demux)

    @staticmethod
    def _reply(ec: int, out: bytes, err: bytes, demux: bool):
        """Match docker-py's demux contract (#742)."""
        if demux:
            return ec, (out or None, err or None)
        return ec, out + err


# ============================================================================
# Host GitHub API stub: backed by the same bare origin
# ============================================================================


class _FakeResponse:
    """Minimal urllib response object."""

    def __init__(self, payload: Any) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, *_args: Any) -> bytes:
        """Return the JSON body."""
        return self._body

    def __enter__(self) -> "_FakeResponse":
        """Enter the response context."""
        return self

    def __exit__(self, *_exc: Any) -> bool:
        """Leave the response context without suppressing anything."""
        return False


def _http_error(path: str, code: int, detail: str) -> urllib.error.HTTPError:
    """Build the same 404/5xx shape ``_github_api_request`` converts."""
    url = f"https://api.github.com{path}"
    body = json.dumps({"message": detail}).encode("utf-8")
    return urllib.error.HTTPError(url, code, detail, Message(), io.BytesIO(body))


class _HostApi:
    """The host's authorized ref-read API, answered from the bare origin.

    The bare origin is the only thing that knows what the remote really
    holds; a forged container ref must therefore disagree with what this
    server returns.  ``faults`` injects failures per endpoint class:

    * ``"branch"`` -- the publish branch lookup
    * ``"base"`` -- a required base-branch lookup
    * ``"default"`` -- the repository/default-branch metadata lookup

    Values are an HTTP status code, or ``"empty"`` for a 200 carrying no
    ``default_branch``.
    """

    def __init__(
        self,
        origin_dir: str,
        *,
        publish_branch: str,
        base_branch: str,
        default_branch: str = "main",
        faults: dict[str, int | str] | None = None,
    ) -> None:
        self.origin_dir = origin_dir
        self.publish_branch = publish_branch
        self.base_branch = base_branch
        self.default_branch = default_branch
        self.faults = faults or {}
        self.calls: list[str] = []

    @staticmethod
    def _classify(path: str) -> tuple[str, str]:
        """Map an API path onto (endpoint class, ref name)."""
        clean = unquote(path)
        if re.fullmatch(r"/repos/[^/]+/[^/]+/?", clean):
            return "default", ""
        match = re.fullmatch(
            r"/repos/[^/]+/[^/]+/(?:git/refs?/heads|branches)/(.+)", clean,
        )
        if match:
            return "ref", match.group(1)
        return "other", clean

    def urlopen(self, request: Any, *_args: Any, **_kwargs: Any) -> _FakeResponse:
        """Serve one GitHub REST call, or raise its injected failure."""
        selector = getattr(request, "selector", None)
        if selector is None:
            url = getattr(request, "full_url", str(request))
            selector = url.split("api.github.com", 1)[-1]
        parsed = urlsplit(selector)
        path = parsed.path
        decoded = unquote(selector)
        self.calls.append(decoded)

        kind, ref = self._classify(path)
        if kind == "default":
            fault = self.faults.get("default")
            if fault == "empty":
                return _FakeResponse({"default_branch": ""})
            if isinstance(fault, int):
                raise _http_error(path, fault, "default branch metadata unavailable")
            return _FakeResponse({"default_branch": self.default_branch})

        if kind != "ref":
            raise _http_error(path, 500, "unexpected host API path")

        if ref == self.publish_branch:
            endpoint = "branch"
        elif ref in (self.base_branch, self.default_branch):
            endpoint = "base"
        else:
            endpoint = "other_ref"

        fault = self.faults.get(endpoint)
        if isinstance(fault, int):
            raise _http_error(path, fault, f"ref {ref} unavailable")

        sha = _remote_ref(self.origin_dir, ref)
        if sha is None:
            raise _http_error(path, 404, f"ref {ref} not found")
        return _FakeResponse({"object": {"sha": sha}})


# ============================================================================
# publish() harness
# ============================================================================


class _PublishContext:
    """Per-test real origin/clone plus the container and host-API stubs."""

    def __init__(self, origin: str, clone: str, tmp: str) -> None:
        self.origin = origin
        self.clone = clone
        self.tmp = tmp
        self.events: list[tuple[str, Any]] = []
        self.container = _RealGitContainer(clone, self.events)
        self.api: _HostApi | None = None
        self.scan_calls: list[list[str]] = []


@pytest.fixture
def repo_setup(tmp_path: Path) -> dict[str, str]:
    """Create a bare origin and a clone with README.md, a.txt and b.txt."""
    origin = tmp_path / "origin"
    clone = tmp_path / "clone"
    origin.mkdir()
    assert _git(str(origin), "init", "--bare", "--initial-branch=main").returncode == 0

    cloned = _git(str(tmp_path), "clone", str(origin), str(clone))
    assert cloned.returncode == 0, f"clone failed: {cloned.stderr}"
    _git(str(clone), "config", "user.email", "cut-parent@example.com")
    _git(str(clone), "config", "user.name", "cut parent fixture")

    (clone / "README.md").write_text("# Initial\n")
    (clone / "a.txt").write_text("a\n")
    (clone / "b.txt").write_text("b\n")
    assert _git(str(clone), "add", "README.md", "a.txt", "b.txt").returncode == 0
    commit = _git(str(clone), "commit", "-m", "Initial commit")
    assert commit.returncode == 0, f"fixture commit failed: {commit.stderr}"
    push = _git(str(clone), "push", "origin", "main")
    assert push.returncode == 0, f"fixture push failed: {push.stderr}"

    assert _git(str(clone), "fetch", "origin").returncode == 0
    symbolic = _git(
        str(clone), "symbolic-ref",
        "refs/remotes/origin/HEAD", "refs/remotes/origin/main",
    )
    assert symbolic.returncode == 0, f"fixture set-head failed: {symbolic.stderr}"

    return {"origin": str(origin), "clone": str(clone), "tmp": str(tmp_path)}


@pytest.fixture
def ctx(repo_setup: dict[str, str]) -> _PublishContext:
    """Build the publish context for one test."""
    return _PublishContext(
        repo_setup["origin"], repo_setup["clone"], repo_setup["tmp"],
    )


@pytest.fixture(autouse=True)
def _host_side_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host side hermetic: no proxy, no ambient token."""
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


def _publish(
    ctx: _PublishContext,
    *,
    branch: str,
    files: list[str],
    base_branch: str = "",
    faults: dict[str, int | str] | None = None,
    default_branch: str = "main",
    message: str = "manifest publish",
    **kwargs: Any,
) -> tuple[dict[str, Any], Any]:
    """Call the public ``publish`` tool against the real clone."""
    api = _HostApi(
        ctx.origin,
        publish_branch=branch,
        base_branch=base_branch or default_branch,
        default_branch=default_branch,
        faults=faults,
    )
    ctx.api = api

    from sunaba.tools.secret_scan import run_secret_scan as real_scan

    def scan_spy(
        container: Any,
        scan_files: list[str],
        working_dir: str,
        **scan_kwargs: Any,
    ) -> dict[str, Any]:
        """Record the manifest scan, then run the real one."""
        ctx.scan_calls.append(list(scan_files))
        ctx.events.append(("scan", tuple(scan_files)))
        return real_scan(container, scan_files, working_dir, **scan_kwargs)

    def host_base_tip(
        repo: str, token: str, tip_base_branch: str, paths: list[str],
    ) -> UpstreamTipRead:
        """Answer the unrelated #863 guard's host read from the bare origin."""
        return _origin_tip_blobs(
            ctx.origin, tip_base_branch or default_branch, paths,
        )

    with (
        patch("urllib.request.urlopen", side_effect=api.urlopen),
        patch(
            "sunaba.tools.vcs.publishing._docker",
            return_value=_FakeClient(ctx.container),
        ),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value=""),
        patch("sunaba.tools.vcs.publishing.proxy_configured", return_value=False),
        patch("sunaba.tools.vcs.publishing.record_boundary_crossing"),
        # The #712 auto-include is a separate GitHub read for merge shapes;
        # it is not the #948 guard and is covered by its own tests.
        patch("sunaba.tools.vcs.publishing._fetch_base_auto_include", return_value=None),
        patch("sunaba.tools.vcs.publishing._fetch_base_tip_blobs", side_effect=host_base_tip),
        patch("sunaba.tools.secret_scan._baseline_enabled", return_value=False),
        patch("sunaba.tools.secret_scan.run_secret_scan", side_effect=scan_spy),
        patch("sunaba.tools.vcs.publishing._create_pr_via_api") as pr_mock,
    ):
        raw = publish(
            container_id=CONTAINER_ID,
            repo=REPO,
            branch=branch,
            message=message,
            working_dir=ctx.clone,
            files=files,
            base_branch=base_branch,
            **kwargs,
        )
    return json.loads(raw), pr_mock


# ============================================================================
# Assertions
# ============================================================================


def _sha_present(text: str, sha: str) -> bool:
    """Accept a full SHA or the first seven characters the tool reports."""
    return sha in text or sha[:7] in text


def _push_index(ctx: _PublishContext) -> int | None:
    """Index of the native push command in the event log, if it ran."""
    for index, (kind, payload) in enumerate(ctx.events):
        if kind == "cmd" and re.search(r"\bpush origin\b", payload):
            return index
    return None


def _scan_index(ctx: _PublishContext) -> int | None:
    """Index of the manifest secret scan in the event log, if it ran."""
    for index, (kind, _payload) in enumerate(ctx.events):
        if kind == "scan":
            return index
    return None


def _assert_scan_before_push(ctx: _PublishContext) -> None:
    """The pre-commit scan of a successful publish still precedes the push."""
    scan = _scan_index(ctx)
    push = _push_index(ctx)
    assert scan is not None, "secret scan never ran for a manifest publish"
    assert push is not None, "push never ran"
    assert scan < push, "the secret scan must still run before the push"


def _assert_nothing_pushed(
    ctx: _PublishContext,
    pr_mock: Any,
    branch: str,
    *,
    expected_ref: str | None = None,
) -> None:
    """A refusal leaves no push and no PR behind.

    *expected_ref* is the SHA the remote branch had before the publish
    (``None`` when the branch did not exist).
    """
    assert _push_index(ctx) is None, (
        "a refused publish still reached the push command: "
        f"{ctx.container.commands}"
    )
    actual = _remote_ref(ctx.origin, branch)
    assert actual == expected_ref, (
        f"remote refs/heads/{branch} changed despite the refusal: "
        f"expected {expected_ref!r}, found {actual!r}"
    )
    pr_mock.assert_not_called()


def _assert_cut_parent_mismatch(
    result: dict[str, Any],
    *,
    expected_sha: str,
    actual_sha: str,
    ref_names: tuple[str, ...],
) -> None:
    """The refusal names both SHAs, the compared ref, and the rerun advice."""
    assert result["status"] == "error", result
    assert result["step"] == "cut_parent_mismatch", result

    text = json.dumps(result)
    assert _sha_present(text, expected_sha), (
        f"expected remote SHA {expected_sha} is not reported: {text}"
    )
    assert _sha_present(text, actual_sha), (
        f"actual commit-parent SHA {actual_sha} is not reported: {text}"
    )
    assert any(name in text for name in ref_names), (
        f"the compared ref ({ref_names}) is not named: {text}"
    )

    lowered = text.lower()
    assert any(word in lowered for word in _RERUN_WORDS), (
        f"no rerun advice in the refusal: {text}"
    )
    assert any(word in lowered for word in _ADVANCE_WORDS), (
        "the refusal must explain that the remote may have advanced between "
        f"the fetch and the host API read: {text}"
    )
    assert "fetch" in lowered, (
        f"the rerun advice must mention the fetch/host-API race: {text}"
    )


def _assert_fail_closed(
    ctx: _PublishContext,
    result: dict[str, Any],
    pr_mock: Any,
    branch: str,
    *,
    expected_ref: str | None = None,
) -> None:
    """An unreadable required host ref stops the publish before any push."""
    assert result["status"] == "error", result
    assert result.get("step"), f"failure has no machine-readable step: {result}"
    assert result.get("error"), f"failure has no error text: {result}"
    _assert_nothing_pushed(ctx, pr_mock, branch, expected_ref=expected_ref)


def _assert_api_read_attempted(ctx: _PublishContext, needle: str) -> None:
    """The guard attempted the host read that the test made fail."""
    assert ctx.api is not None
    assert any(needle in call for call in ctx.api.calls), (
        f"host API was never asked for {needle!r}: {ctx.api.calls}"
    )


# ============================================================================
# Regressions: forged refs must stop before push
# ============================================================================


class TestCutParentRegressions:
    """The #934 routes and the post-fetch rewrite race stop the publish."""

    def test_nonsymbolic_forged_origin_head_with_omitted_base_cuts_from_default(
        self, ctx: _PublishContext,
    ) -> None:
        """S4 / #946: origin/HEAD is made a direct ref to a forged checkpoint.

        Manifest publish with omitted base cuts from origin/<host default>
        (origin/main) rather than origin/HEAD. The forged origin/HEAD is
        ignored and publish succeeds against the legitimate remote default.
        """
        main_sha = _sha(ctx.clone, "refs/remotes/origin/main")
        forged = _forge_bad_commit(ctx.clone, "origin/main")
        rewrite = _git(
            ctx.clone, "update-ref", "--no-deref",
            "refs/remotes/origin/HEAD", forged,
        )
        assert rewrite.returncode == 0, rewrite.stderr
        assert _git(ctx.clone, "symbolic-ref", "refs/remotes/origin/HEAD").returncode != 0
        _edit_readme(ctx.clone)

        result, pr_mock = _publish(
            ctx, branch="feat/forged-head", files=["README.md"],
        )

        assert result["status"] == "pushed", result
        assert result["cut_from"] == "origin/main"
        created_parent = _sha(ctx.clone, "HEAD^1")
        assert created_parent == main_sha
        assert created_parent != forged

    def test_phantom_origin_branch_absent_from_remote_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """S6: a branch that exists only as origin/<branch> must not be cut from.

        The fetch does not prune, so the phantom ref survives; publish used
        to prefer it over the explicitly named base.  The host's 404 for the
        publish branch permits the *base-branch* fallback, not the phantom.
        """
        main_sha = _sha(ctx.clone, "refs/remotes/origin/main")
        forged = _forge_bad_commit(ctx.clone, "origin/main")
        rewrite = _git(
            ctx.clone, "update-ref",
            "refs/remotes/origin/feat/phantom", forged,
        )
        assert rewrite.returncode == 0, rewrite.stderr
        assert _remote_ref(ctx.origin, "feat/phantom") is None
        _edit_readme(ctx.clone)

        result, pr_mock = _publish(
            ctx,
            branch="feat/phantom",
            files=["README.md"],
            base_branch="main",
        )

        _assert_cut_parent_mismatch(
            result,
            expected_sha=main_sha,
            actual_sha=forged,
            ref_names=("refs/heads/main", "origin/main", "feat/phantom"),
        )
        _assert_nothing_pushed(ctx, pr_mock, "feat/phantom")

    def test_ref_rewrite_after_fetch_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """A ref rewritten *after* the authorized fetch is still caught.

        The remote branch really exists at ``remote_tip``; the forged
        checkpoint is a fast-forward child of it, so a push would have
        succeeded.  The host compares against the remote, not the rewritten
        tracking ref.
        """
        _remote_branch(
            ctx, "feat/live", {"live.txt": "live\n"}, "remote feature",
        )
        assert _git(ctx.clone, "fetch", "origin").returncode == 0
        remote_tip = _sha(ctx.clone, "refs/remotes/origin/feat/live")

        forged = _forge_bad_commit(ctx.clone, remote_tip)
        _edit_readme(ctx.clone)

        def rewrite_after_fetch() -> None:
            """Simulate the race window between fetch and ref read."""
            result = _git(
                ctx.clone, "update-ref",
                "refs/remotes/origin/feat/live", forged,
            )
            assert result.returncode == 0, result.stderr

        ctx.container.after_fetch_once = rewrite_after_fetch

        result, pr_mock = _publish(
            ctx,
            branch="feat/live",
            files=["README.md"],
            base_branch="main",
        )

        _assert_cut_parent_mismatch(
            result,
            expected_sha=remote_tip,
            actual_sha=forged,
            ref_names=("refs/heads/feat/live", "origin/feat/live"),
        )
        _assert_nothing_pushed(
            ctx, pr_mock, "feat/live", expected_ref=remote_tip,
        )

    def test_forged_local_base_fallback_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """A local branch selected because origin/<base> is missing is untrusted.

        ``git_prepare_commit`` may fall back to local ``<base>`` when the
        tracking ref is absent.  If the host has no such base at all, the
        guard must stop for the missing required base rather than accept the
        local commit.
        """
        _remote_branch(ctx, "feat/elsewhere", {"other.txt": "other\n"})
        assert _git(ctx.clone, "fetch", "origin").returncode == 0

        local_base = _forge_bad_commit(ctx.clone, "origin/main")
        _checkout_branch(ctx.clone, "feat/local-only", local_base)
        assert _remote_ref(ctx.origin, "feat/local-only") is None
        _edit_readme(ctx.clone)

        result, pr_mock = _publish(
            ctx,
            branch="feat/stacked-from-forgery",
            files=["README.md"],
            base_branch="feat/local-only",
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/stacked-from-forgery")
        assert result["step"] == "base_branch"
        assert "feat/local-only" in result["error"]
        assert "tried origin/feat/local-only only" in result["error"]

    def test_forged_merge_second_parent_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """#819: the rebuild's parent2 must match the remote base SHA.

        ``origin/main`` is rewritten after the fetch to a checkpoint that is
        not the remote base.  Parent1 still matches, so only the parent2
        comparison can catch this.
        """
        remote_tip = _remote_branch(
            ctx, "feat/merge-forged", {"feature.txt": "feature\n"},
        )
        main_tip = _advance_main(ctx, {"upstream.txt": "upstream\n"})
        assert _git(ctx.clone, "fetch", "origin").returncode == 0

        _checkout_branch(ctx.clone, "feat/merge-forged", remote_tip)
        merge = _git(ctx.clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"

        # A commit object reachable locally but not on the remote's main.
        forged = _commit_tree_child(ctx.clone, main_tip)

        def rewrite_main_after_fetch() -> None:
            """Replace the tracking ref for main after the authorized fetch."""
            result = _git(
                ctx.clone, "update-ref", "refs/remotes/origin/main", forged,
            )
            assert result.returncode == 0, result.stderr

        ctx.container.after_fetch_once = rewrite_main_after_fetch
        (Path(ctx.clone) / "declared.txt").write_text("declared\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/merge-forged",
            files=["declared.txt"],
            base_branch="main",
        )

        assert result["status"] == "error", result
        assert result["step"] == "cut_parent_mismatch", result
        _assert_cut_parent_mismatch(
            result,
            expected_sha=main_tip,
            actual_sha=forged,
            ref_names=("refs/heads/main", "origin/main", "feat/merge-forged"),
        )
        _assert_nothing_pushed(
            ctx, pr_mock, "feat/merge-forged", expected_ref=remote_tip,
        )


# ============================================================================
# Host API failures: fail closed, never fall back on a non-404
# ============================================================================


class TestCutParentApiFailures:
    """Only a 404 for the *publish branch* may trigger the base fallback."""

    def test_publish_branch_read_failure_does_not_fall_back(
        self, ctx: _PublishContext,
    ) -> None:
        """A 5xx on the publish-branch lookup is not evidence of absence."""
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/reads",
            files=["new.txt"],
            base_branch="main",
            faults={"branch": 500},
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/reads")
        _assert_api_read_attempted(ctx, "feat/reads")

    def test_publish_branch_read_forbidden_does_not_fall_back(
        self, ctx: _PublishContext,
    ) -> None:
        """A 403 on the publish-branch lookup is not evidence of absence."""
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/forbidden",
            files=["new.txt"],
            base_branch="main",
            faults={"branch": 403},
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/forbidden")
        _assert_api_read_attempted(ctx, "feat/forbidden")

    def test_base_branch_read_failure_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """A 5xx on the required base lookup stops the publish."""
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/base-read-fail",
            files=["new.txt"],
            base_branch="main",
            faults={"base": 500},
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/base-read-fail")
        _assert_api_read_attempted(ctx, "main")

    def test_default_branch_metadata_failure_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """The omitted-base path needs repo metadata; a 5xx stops it."""
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/default-fail",
            files=["new.txt"],
            faults={"default": 500},
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/default-fail")
        _assert_api_read_attempted(ctx, f"/repos/{REPO}")

    def test_default_branch_metadata_missing_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """Metadata without a default_branch cannot validate parent1."""
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/default-missing",
            files=["new.txt"],
            faults={"default": "empty"},
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/default-missing")
        _assert_api_read_attempted(ctx, f"/repos/{REPO}")

    def test_missing_required_base_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """A 404 for an explicitly required base is fatal, not a fallback."""
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/base-404",
            files=["new.txt"],
            base_branch="main",
            faults={"base": 404},
        )

        _assert_fail_closed(ctx, result, pr_mock, "feat/base-404")
        assert "main" in json.dumps(result), result
        _assert_api_read_attempted(ctx, "main")

    def test_merge_base_read_failure_stops(
        self, ctx: _PublishContext,
    ) -> None:
        """The merge rebuild's parent2 read is required too (#819)."""
        remote_tip = _remote_branch(
            ctx, "feat/merge-readfail", {"feature.txt": "feature\n"},
        )
        _advance_main(ctx, {"upstream.txt": "upstream\n"})
        assert _git(ctx.clone, "fetch", "origin").returncode == 0

        _checkout_branch(ctx.clone, "feat/merge-readfail", remote_tip)
        merge = _git(ctx.clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"
        (Path(ctx.clone) / "declared.txt").write_text("declared\n")

        result, pr_mock = _publish(
            ctx,
            branch="feat/merge-readfail",
            files=["declared.txt"],
            base_branch="main",
            faults={"base": 500},
        )

        _assert_fail_closed(
            ctx, result, pr_mock, "feat/merge-readfail",
            expected_ref=remote_tip,
        )
        _assert_api_read_attempted(ctx, "main")


# ============================================================================
# Ordinary paths: the guard must not change their result
# ============================================================================


class TestCutParentSuccessPaths:
    """New branch, republish, stacked and merge keep their answers."""

    def test_new_branch_publishes_from_default_branch(
        self, ctx: _PublishContext,
    ) -> None:
        """New branch, omitted base: parent1 is the remote default tip."""
        main_sha = _sha(ctx.clone, "refs/remotes/origin/main")
        (Path(ctx.clone) / "new.txt").write_text("new\n")

        result, pr_mock = _publish(
            ctx, branch="feat/brand-new", files=["new.txt"],
        )

        assert result["status"] == "pushed", result
        assert result["branch"] == "feat/brand-new"
        assert result["sha"] == _sha(ctx.origin, "refs/heads/feat/brand-new")[:7]
        assert result["staged_files"] == ["new.txt"]
        assert result["cut_from"] == "origin/main"
        assert result["secret_scan_state"] == "skipped"
        assert result["files_scanned"] == ["new.txt"]
        assert result["scan_summary"]
        assert result["files_skipped_deleted"] == []
        assert result["push_transport"] == "native"
        assert "note" in result
        assert _sha(ctx.origin, "refs/heads/feat/brand-new^") == main_sha
        _assert_scan_before_push(ctx)
        pr_mock.assert_not_called()

    def test_republish_preserves_existing_branch_parent(
        self, ctx: _PublishContext,
    ) -> None:
        """An existing remote branch stays the parent, not the base branch."""
        remote_tip = _remote_branch(
            ctx, "feat/existing", {"feature.txt": "feature\n"},
        )
        assert _git(ctx.clone, "fetch", "origin").returncode == 0
        (Path(ctx.clone) / "added.txt").write_text("added\n")

        result, _pr = _publish(
            ctx,
            branch="feat/existing",
            files=["added.txt"],
            base_branch="main",
        )

        assert result["status"] == "pushed", result
        assert result["branch"] == "feat/existing"
        assert result["staged_files"] == ["added.txt"]
        assert result["cut_from"] == "origin/feat/existing"
        assert _sha(ctx.origin, "refs/heads/feat/existing^") == remote_tip
        _assert_scan_before_push(ctx)

    def test_stacked_publish_uses_remote_base_parent(
        self, ctx: _PublishContext,
    ) -> None:
        """#891: a new branch stacked on a named base parents onto that tip."""
        base_tip = _remote_branch(
            ctx, "feat/base", {"base.txt": "base\n"},
        )
        assert _git(ctx.clone, "fetch", "origin").returncode == 0
        _checkout_branch(ctx.clone, "feat/stacked", "origin/feat/base")
        (Path(ctx.clone) / "stacked.txt").write_text("stacked\n")

        result, _pr = _publish(
            ctx,
            branch="feat/stacked",
            files=["stacked.txt"],
            base_branch="feat/base",
        )

        assert result["status"] == "pushed", result
        assert result["branch"] == "feat/stacked"
        assert result["cut_from"] == "origin/feat/base"
        assert _sha(ctx.origin, "refs/heads/feat/stacked^") == base_tip
        _assert_scan_before_push(ctx)

    def test_merge_publish_validates_both_parents_and_fields(
        self, ctx: _PublishContext,
    ) -> None:
        """#819: a valid merge rebuild keeps both parents and its report."""
        remote_tip = _remote_branch(
            ctx, "feat/merge-ok", {"feature.txt": "feature\n"},
        )
        main_tip = _advance_main(ctx, {"upstream.txt": "upstream\n"})
        assert _git(ctx.clone, "fetch", "origin").returncode == 0

        _checkout_branch(ctx.clone, "feat/merge-ok", remote_tip)
        merge = _git(ctx.clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"
        merge_sha = _sha(ctx.clone, "HEAD")
        (Path(ctx.clone) / "mine2.txt").write_text("mine\n")

        result, _pr = _publish(
            ctx,
            branch="feat/merge-ok",
            files=["mine2.txt"],
            base_branch="main",
        )

        assert result["status"] == "pushed", result
        assert result["branch"] == "feat/merge-ok"
        assert result["staged_files"] == ["mine2.txt"]
        assert result["merge_discarded_sha"] == merge_sha[:7]
        assert result["merge_parents"] == [remote_tip[:7], main_tip[:7]]
        assert result["merge_rebuilt_parents"] == [remote_tip[:7], main_tip[:7]]
        assert _sha(ctx.origin, "refs/heads/feat/merge-ok^1") == remote_tip
        assert _sha(ctx.origin, "refs/heads/feat/merge-ok^2") == main_tip
        _assert_scan_before_push(ctx)

    def test_local_only_base_without_remote_tracking_ref_refuses(
        self, ctx: _PublishContext,
    ) -> None:
        """Under #946, local-only base without remote ref fails step=base_branch.

        The tracking ref is absent because this clone only fetches main.
        Local-base fallback is eliminated; publish refuses before commit.
        """
        base_tip = _remote_branch(ctx, "feat/base", {"base.txt": "base\n"})
        assert _git(ctx.clone, "fetch", "origin").returncode == 0

        # Restrict fetches to main and drop the tracking ref, so the only
        # available base is the local branch.
        _git(
            ctx.clone, "config", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        _git(ctx.clone, "update-ref", "-d", "refs/remotes/origin/feat/base")
        assert _remote_ref(ctx.clone, "feat/base") is None
        _checkout_branch(ctx.clone, "feat/base", base_tip)
        (Path(ctx.clone) / "stacked.txt").write_text("stacked\n")

        result, _pr = _publish(
            ctx,
            branch="feat/stacked-local",
            files=["stacked.txt"],
            base_branch="feat/base",
        )

        assert result["status"] == "error", result
        assert result["step"] == "base_branch"
        assert "feat/base" in result["error"]
        assert "tried origin/feat/base only" in result["error"]
        _assert_nothing_pushed(ctx, _pr, "feat/stacked-local")
