"""Unit tests for the #948 host reads behind the cut-parent guard.

The frozen acceptance suite (``test_publish_cut_parent.py``) drives the whole
tool against a real clone.  These tests pin the smaller decisions that are
easy to regress once the end-to-end path is green:

* only a structured 404 means "branch absent" -- a 500 whose message happens
  to contain the text ``HTTP 404`` must still fail closed;
* the publish branch name is one URL path segment, not two;
* a missing/malformed ``object.sha`` is an error, never a usable value;
* malformed parent-probe output is a refusal, not an optimistic parse;
* a container-written ``merge_degenerate`` flag cannot authorize skipping the
  host comparison of the second-parent candidate (the degenerate decision is
  made from container-controlled refs);
* malformed default-branch metadata fails closed.

The real-git tests at the bottom rebind to the frozen harness helpers
(imported, never edited) and pin the repair end to end: a post-fetch rewrite
that forces a degenerate single-parent rebuild is refused, a genuinely valid
degenerate rebuild is preserved, and a forged local base is refused even when
the remote base exists.
"""
from __future__ import annotations

import io
import json
import os
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.publish_ops import RunFunc, git_push_with_fallback
from sunaba.tools.publish_planner import build_push_command
from sunaba.tools.vcs.publishing import (
    _CUT_PARENT_MARK,
    ExpectedCutParents,
    HostRefRead,
    _host_read_branch_ref,
    _host_read_default_branch,
    _read_commit_parents,
    _resolve_expected_cut_parents,
    _try_api_push,
    _validate_cut_parents,
)
from tests.test_publish_cut_parent import (
    _advance_main,
    _assert_cut_parent_mismatch,
    _assert_nothing_pushed,
    _assert_scan_before_push,
    _checkout_branch,
    _commit_tree_child,
    _forge_bad_commit,
    _git,
    _publish,
    _PublishContext,
    _remote_branch,
    _remote_ref,
    _sha,
    repo_setup,
)

#: A host-authorized parent1 (an existing feature tip).
PARENT1 = "a" * 40
#: A host-authorized parent2 (the remote base tip).
BASE = "b" * 40
#: A container-resolved second-parent candidate that is not the host base.
CANDIDATE = "c" * 40
#: The created commit object name reported by the synthetic probe.
PROBED_COMMIT = "d" * 40
#: The tree header of the synthetic raw commit object.
PROBED_TREE = "e" * 40

#: Referenced so the frozen module's real-git fixture stays registered for
#: this module (pytest registers imported fixtures by their attribute name).
_FROZEN_FIXTURES = (repo_setup,)


@pytest.fixture(autouse=True)
def _host_side_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host side hermetic: no proxy, no ambient token."""
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


@pytest.fixture
def publish_ctx(request: pytest.FixtureRequest) -> _PublishContext:
    """Build the frozen harness context from its real-git repo fixture."""
    setup: dict[str, str] = request.getfixturevalue("repo_setup")
    return _PublishContext(setup["origin"], setup["clone"], setup["tmp"])


def _http_error(status: int, body: bytes = b"") -> HTTPError:
    return HTTPError(
        "https://api.github.com/repos/o/r/git/ref/heads/main",
        status,
        "synthetic",
        Message(),
        io.BytesIO(body),
    )


def _expected(
    *, parent1: str = PARENT1, parent2: str = BASE,
) -> ExpectedCutParents:
    """Host-authorized parents for a merge publish of ``feat/x`` onto ``main``."""
    return ExpectedCutParents(
        parent1=parent1,
        parent1_ref="refs/heads/feat/x",
        parent2=parent2,
        parent2_ref="refs/heads/main",
    )


def _probe(parent1: str, parent2: str = "") -> RunFunc:
    """Build a ``run`` stub answering the raw post-commit parent probe."""
    lines = [
        f"{_CUT_PARENT_MARK} merge",
        PROBED_COMMIT,
        f"tree {PROBED_TREE}",
        f"parent {parent1}",
    ]
    if parent2:
        lines.append(f"parent {parent2}")
    lines += [
        "author fixture <fixture@example.com> 0 +0000",
        "committer fixture <fixture@example.com> 0 +0000",
        "",
        "mocked merge",
    ]
    output = "\n".join(lines)

    def _run(
        cmd: str, env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        del cmd, env
        return 0, output, ""

    return _run


def _validate(
    *,
    parent1: str = PARENT1,
    parent2: str = "",
    merge_info: dict | None = None,
    merge_parent_sha: str = "",
    merge_is_merge: bool = True,
    expected: ExpectedCutParents | None = None,
) -> dict | None:
    """Call the guard with a fixed host-authorized expectation."""
    with patch(
        "sunaba.tools.vcs.publishing._resolve_expected_cut_parents",
        return_value=(expected or _expected(), None),
    ):
        return _validate_cut_parents(
            _probe(parent1, parent2),
            repo="o/r",
            token="tok",
            branch="feat/x",
            base_branch="main",
            merge_is_merge=merge_is_merge,
            merge_info=merge_info or {},
            merge_parent_sha=merge_parent_sha,
        )


# ============================================================================
# Host-read seam (unchanged decisions)
# ============================================================================


def test_only_structured_404_is_absence() -> None:
    """A 500 mentioning 404 in its body is a failure, not a missing ref."""
    failure = RuntimeError(
        'GitHub API GET /repos/o/r/git/ref/heads/main returned HTTP 500: '
        'upstream said "HTTP 404"'
    )
    failure.__cause__ = _http_error(500, b'HTTP 404')
    with patch(
        "sunaba.tools.github_api._github_api_request",
        side_effect=failure,
    ):
        result = _host_read_branch_ref("o/r", "tok", "main")

    assert result.absent is False
    assert result.sha == ""
    assert "HTTP 500" in result.error


def test_structured_404_is_absence() -> None:
    failure = RuntimeError("GitHub API GET ... returned HTTP 404")
    failure.__cause__ = _http_error(404)
    with patch(
        "sunaba.tools.github_api._github_api_request",
        side_effect=failure,
    ):
        assert _host_read_branch_ref("o/r", "tok", "gone") == HostRefRead(
            "", True, "",
        )


def test_branch_name_is_one_url_segment() -> None:
    seen: list[str] = []

    def _fake(path: str, token: str):
        seen.append(path)
        return {"object": {"sha": "a" * 40}}

    with patch("sunaba.tools.github_api._github_api_request", side_effect=_fake):
        _host_read_branch_ref("o/r", "tok", "feat/stacked")

    assert seen == ["/repos/o/r/git/ref/heads/feat%2Fstacked"]


def test_malformed_sha_is_an_error() -> None:
    for info in (
        {"object": {"sha": "abc1234"}},
        {"object": {}},
        {"object": "nope"},
        {"object": {"sha": "z" * 40}},
        {},
    ):
        with patch(
            "sunaba.tools.github_api._github_api_request",
            return_value=info,
        ):
            result = _host_read_branch_ref("o/r", "tok", "main")
        assert result.sha == ""
        assert result.error, info


def _probe_output(output: str, ec: int = 0) -> RunFunc:
    """Build a ``run`` stub returning *output* with exit code *ec*."""

    def _run(
        cmd: str, env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        del cmd, env
        return ec, output, ""

    return _run


def _raw_probe(
    parents: tuple[str, ...] = ("a" * 40,),
    *,
    mode: str = "merge",
    commit: str = PROBED_COMMIT,
    tree: str = PROBED_TREE,
    message: str = "message",
) -> str:
    """Build a probe answer in the raw ``cat-file -p`` shape."""
    lines = [f"{_CUT_PARENT_MARK} {mode}", commit, f"tree {tree}"]
    lines += [f"parent {parent}" for parent in parents]
    lines += [
        "author fixture <fixture@example.com> 0 +0000",
        "committer fixture <fixture@example.com> 0 +0000",
        "",
        message,
        "",
    ]
    return "\n".join(lines)


def test_parent_probe_requires_both_full_shas() -> None:
    good = _raw_probe(
        ("a" * 40, "b" * 40),
        message=f"parent {'c' * 40} in the message is not a header",
    )
    assert _read_commit_parents(_probe_output(good), "merge") == (
        PROBED_COMMIT, ["a" * 40, "b" * 40], None,
    )
    # A single-parent merge rebuild is legitimate (#819 degenerate).
    assert _read_commit_parents(
        _probe_output(_raw_probe(("a" * 40,))), "merge",
    ) == (PROBED_COMMIT, ["a" * 40], None)

    bad_outputs = (
        "",
        _raw_probe().replace(_CUT_PARENT_MARK, "no-marker"),
        f"{_CUT_PARENT_MARK} merge\n{PROBED_COMMIT}\n",  # no raw header
        _raw_probe(commit="abc1234"),
        _raw_probe(tree="malformed"),
        _raw_probe(("not-a-sha",)),
        _raw_probe(()),
        # Shape is part of the contract: single wants exactly one parent,
        # merge allows one or two.
        _raw_probe(("a" * 40, "b" * 40), mode="single"),
        _raw_probe(("a" * 40, "b" * 40, "c" * 40)),
    )
    for output in bad_outputs:
        assert _read_commit_parents(_probe_output(output), "merge")[2] is not None, (
            output
        )

    assert _read_commit_parents(
        _probe_output(_raw_probe(("a" * 40,))), "merge",
    ) == (PROBED_COMMIT, ["a" * 40], None)
    assert _read_commit_parents(_probe_output(good, 1), "merge")[2] is not None


def test_merge_expects_base_even_when_publish_branch_exists() -> None:
    reads: list[str] = []

    def _fake(repo: str, token: str, branch: str) -> HostRefRead:
        reads.append(branch)
        return HostRefRead(f"{len(reads):040x}", False, "")

    with patch(
        "sunaba.tools.vcs.publishing._host_read_branch_ref",
        side_effect=_fake,
    ):
        expected, err = _resolve_expected_cut_parents(
            "o/r", "tok", "feat/existing", "main", True,
        )

    assert err is None
    assert expected is not None
    assert reads == ["feat/existing", "main"]
    assert expected.parent1_ref == "refs/heads/feat/existing"
    assert expected.parent2_ref == "refs/heads/main"
    assert expected.parent2 == f"{2:040x}"


# ============================================================================
# The degenerate flag cannot authorize skipping the host base comparison
# ============================================================================


class TestDegenerateRebuildGuard:
    """The container-written ``merge_degenerate`` flag cannot authorize itself.

    ``publish_ops`` sets the flag when the *container-resolved* second parent
    equals parent1 or is its ancestor.  Both sides of that test live in refs
    the sandbox can rewrite after the authorized fetch, so the flag alone
    must never stand in for the host read.
    """

    def test_feature_tip_candidate_is_not_the_host_base(self) -> None:
        """The adjudicated bypass: origin/base rewritten to the feature tip."""
        result = _validate(
            merge_info={"merge_degenerate": True},
            merge_parent_sha=PARENT1,
        )

        assert result is not None
        assert result["step"] == "cut_parent_mismatch", result
        assert result["expected_sha"] == BASE, result
        assert result["actual_sha"] == PARENT1, result
        assert result["resolved_second_parent"] == PARENT1, result
        assert result["degenerate_rebuild"] is True, result
        assert result["ref"] == "refs/heads/main", result
        assert result["parent"] == 2, result

        text = json.dumps(result).lower()
        assert "rerun" in text
        assert "fetch" in text
        assert "advanc" in text

    def test_ancestor_candidate_is_compared_with_host_base(self) -> None:
        """The 'is ancestor' arm of the degenerate decision is untrusted too."""
        result = _validate(
            merge_info={"merge_degenerate": True},
            merge_parent_sha=CANDIDATE,
        )

        assert result is not None
        assert result["step"] == "cut_parent_mismatch", result
        assert result["expected_sha"] == BASE, result
        assert result["actual_sha"] == CANDIDATE, result

    def test_candidate_matching_host_base_is_allowed(self) -> None:
        """A genuinely degenerate rebuild resolves the host base and passes."""
        assert _validate(
            merge_info={"merge_degenerate": True},
            merge_parent_sha=BASE,
        ) is None

    @pytest.mark.parametrize("candidate", ["", "not-a-sha", PARENT1[:7]])
    def test_missing_or_malformed_candidate_fails_closed(
        self, candidate: str,
    ) -> None:
        """No resolved candidate means the flag is unverifiable, not valid."""
        result = _validate(
            merge_info={"merge_degenerate": True},
            merge_parent_sha=candidate,
        )

        assert result is not None
        assert result["step"] == "cut_parent_read", result
        assert result["parent"] == 2, result
        assert "second-parent candidate" in result["error"], result

    def test_unexpected_second_parent_is_checked_despite_flag(self) -> None:
        """An actual HEAD^2 is compared even when the flag says degenerate."""
        result = _validate(
            parent2=CANDIDATE,
            merge_info={"merge_degenerate": True},
            merge_parent_sha=BASE,
        )

        assert result is not None
        assert result["step"] == "cut_parent_mismatch", result
        assert result["expected_sha"] == BASE, result
        assert result["actual_sha"] == CANDIDATE, result
        # This is the plain HEAD^2 mismatch, not the degenerate note.
        assert "degenerate_rebuild" not in result

    def test_matching_second_parent_wins_over_flag(self) -> None:
        """A matching real HEAD^2 is accepted regardless of the flag."""
        assert _validate(
            parent2=BASE,
            merge_info={"merge_degenerate": True},
            merge_parent_sha=CANDIDATE,
        ) is None

    def test_single_parent_without_degenerate_flag_fails_closed(self) -> None:
        """A merge that silently lost HEAD^2 is still refused."""
        result = _validate(merge_info={})

        assert result is not None
        assert result["step"] == "cut_parent_read", result
        assert result["parent"] == 2, result
        assert "HEAD^2" in result["error"], result

    def test_unresolved_second_parent_still_fails_closed(self) -> None:
        """The unresolved-base marker is refused before the flag is read."""
        result = _validate(
            merge_info={"merge_second_parent_unresolved": "origin/main"},
            merge_parent_sha=BASE,
        )

        assert result is not None
        assert result["step"] == "cut_parent_read", result
        assert result["parent"] == 2, result

    def test_missing_host_authorized_parent_fails_closed(self) -> None:
        """A resolver that yields no expected parent cannot validate anything."""
        with patch(
            "sunaba.tools.vcs.publishing._resolve_expected_cut_parents",
            return_value=(None, None),
        ):
            result = _validate_cut_parents(
                _probe(PARENT1),
                repo="o/r",
                token="tok",
                branch="feat/x",
                base_branch="main",
                merge_is_merge=True,
                merge_info={},
                merge_parent_sha=BASE,
            )

        assert result is not None
        assert result["step"] == "cut_parent_read", result
        assert result["parent"] == 1, result


# ============================================================================
# Default-branch metadata: malformed values fail closed
# ============================================================================


class TestDefaultBranchMetadata:
    """The omitted-base path needs a usable default branch name."""

    @pytest.mark.parametrize(
        "info",
        (
            [],
            {},
            None,
            {"default_branch": ""},
            {"default_branch": "   "},
            {"default_branch": 5},
            {"default_branch": ["main"]},
        ),
    )
    def test_malformed_metadata_is_an_error(self, info: object) -> None:
        """A non-string or empty default_branch is not a branch name."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
            return_value=info,
        ):
            branch, err = _host_read_default_branch("o/r", "tok")

        assert branch == ""
        assert err

    def test_valid_metadata_returns_the_branch(self) -> None:
        with patch(
            "sunaba.tools.github_api._github_api_request",
            return_value={"default_branch": " trunk "},
        ):
            assert _host_read_default_branch("o/r", "tok") == ("trunk", "")

    def test_malformed_metadata_stops_resolution(self) -> None:
        """The malformed metadata error reaches the resolver payload."""
        with (
            patch(
                "sunaba.tools.vcs.publishing._host_read_branch_ref",
                return_value=HostRefRead("", True, ""),
            ),
            patch(
                "sunaba.tools.github_api._github_api_request",
                return_value={"default_branch": 5},
            ),
        ):
            expected, err = _resolve_expected_cut_parents(
                "o/r", "tok", "feat/new", "", False,
            )

        assert expected is None
        assert err is not None
        assert err["step"] == "cut_parent_host_read", err
        assert "default branch" in err["error"], err


# ============================================================================
# Public fail-closed: an unreadable parent stops before the push
# ============================================================================


def test_unreadable_parent_probe_stops_before_push(
    publish_ctx: _PublishContext,
) -> None:
    """A parent-read failure refuses publicly instead of trusting the commit."""
    (Path(publish_ctx.clone) / "new.txt").write_text("new\n")
    failure = {
        "status": "error",
        "step": "cut_parent_read",
        "error": "the created commit's parent could not be read reliably",
        "parent": 1,
    }

    with patch(
        "sunaba.tools.vcs.publishing._read_commit_parents",
        return_value=("", "", failure),
    ):
        result, pr_mock = _publish(
            publish_ctx, branch="feat/probe-fail", files=["new.txt"],
        )

    assert result["status"] == "error", result
    assert result["step"] == "cut_parent_read", result
    _assert_nothing_pushed(publish_ctx, pr_mock, "feat/probe-fail")


# ============================================================================
# Real-git public regressions for the degenerate repair
# ============================================================================


class TestDegenerateMergePublic:
    """End-to-end: a real degenerate rebuild is validated against the host."""

    @pytest.mark.parametrize("rewrite_target", ["feature_tip", "ancestor"])
    def test_rewritten_base_forcing_degenerate_rebuild_stops(
        self, publish_ctx: _PublishContext, rewrite_target: str,
    ) -> None:
        """Rewriting origin/main after the fetch cannot drop HEAD^2 for free.

        The remote feature and a divergent remote main both exist; the local
        HEAD is a real merge.  The post-fetch hook points ``origin/main`` at
        the feature tip (or an older ancestor of it), so the #819 rebuild
        degenerates to a single-parent commit.  The host main still diverges,
        and the resolved second-parent candidate is what must be compared.
        """
        remote_tip = _remote_branch(
            publish_ctx, "feat/merge-degenerate-forged", {"feature.txt": "feature\n"},
        )
        main_tip = _advance_main(publish_ctx, {"upstream.txt": "upstream\n"})
        assert _git(publish_ctx.clone, "fetch", "origin").returncode == 0

        _checkout_branch(
            publish_ctx.clone, "feat/merge-degenerate-forged", remote_tip,
        )
        merge = _git(publish_ctx.clone, "merge", "origin/main", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"

        if rewrite_target == "feature_tip":
            forged_base = remote_tip
        else:
            forged_base = _sha(publish_ctx.clone, f"{remote_tip}~1")
        assert forged_base != main_tip

        def rewrite_main_after_fetch() -> None:
            """Replace the tracking ref for main after the authorized fetch."""
            rewritten = _git(
                publish_ctx.clone, "update-ref", "refs/remotes/origin/main", forged_base,
            )
            assert rewritten.returncode == 0, rewritten.stderr

        publish_ctx.container.after_fetch_once = rewrite_main_after_fetch
        (Path(publish_ctx.clone) / "declared.txt").write_text("declared\n")

        result, pr_mock = _publish(
            publish_ctx,
            branch="feat/merge-degenerate-forged",
            files=["declared.txt"],
            base_branch="main",
        )

        assert result["status"] == "error", result
        assert result["step"] == "cut_parent_mismatch", result
        assert result["parent"] == 2, result
        assert result["expected_sha"] == main_tip, result
        assert result["actual_sha"] == forged_base, result
        assert result["resolved_second_parent"] == forged_base, result
        assert result["degenerate_rebuild"] is True, result
        _assert_cut_parent_mismatch(
            result,
            expected_sha=main_tip,
            actual_sha=forged_base,
            ref_names=(
                "refs/heads/main",
                "origin/main",
                "feat/merge-degenerate-forged",
            ),
        )
        _assert_nothing_pushed(
            publish_ctx, pr_mock, "feat/merge-degenerate-forged",
            expected_ref=remote_tip,
        )
        # The rebuilt public commit really degenerated: the rewrite dropped
        # the second parent before the guard refused the push.
        assert _sha(publish_ctx.clone, "HEAD^1") == remote_tip
        assert _git(publish_ctx.clone, "rev-parse", "--verify", "HEAD^2").returncode != 0

    def test_valid_degenerate_merge_rebuild_is_allowed(
        self, publish_ctx: _PublishContext,
    ) -> None:
        """A host base that is an ancestor of the feature tip stays allowed.

        The remote main tip is already contained in the remote feature tip,
        so the rebuild legitimately has one parent; the resolved candidate is
        the host base and the publish proceeds.
        """
        main_tip = _advance_main(publish_ctx, {"upstream.txt": "upstream\n"})
        remote_tip = _remote_branch(
            publish_ctx, "feat/merge-degenerate-ok", {"feature.txt": "feature\n"},
        )
        assert _git(publish_ctx.clone, "fetch", "origin").returncode == 0
        ancestor = _git(
            publish_ctx.clone, "merge-base", "--is-ancestor", main_tip, remote_tip,
        )
        assert ancestor.returncode == 0, ancestor.stderr

        # Build a local merge whose second parent is not on main, so merge
        # detection (HEAD^2 present, HEAD not an ancestor of origin/main)
        # enters the #819 rebuild path.
        _checkout_branch(publish_ctx.clone, "side", _sha(publish_ctx.clone, f"{remote_tip}~1"))
        (Path(publish_ctx.clone) / "side.txt").write_text("side\n")
        assert _git(publish_ctx.clone, "add", "side.txt").returncode == 0
        assert _git(publish_ctx.clone, "commit", "-m", "side work").returncode == 0

        _checkout_branch(publish_ctx.clone, "feat/merge-degenerate-ok", remote_tip)
        merge = _git(publish_ctx.clone, "merge", "side", "--no-edit")
        assert merge.returncode == 0, f"fixture merge failed: {merge.stderr}"
        merge_sha = _sha(publish_ctx.clone, "HEAD")
        assert _git(publish_ctx.clone, "rev-parse", "--verify", "HEAD^2").returncode == 0
        detected = _git(
            publish_ctx.clone, "merge-base", "--is-ancestor", "HEAD", "origin/main",
        )
        assert detected.returncode != 0

        (Path(publish_ctx.clone) / "declared.txt").write_text("declared\n")

        result, _pr = _publish(
            publish_ctx,
            branch="feat/merge-degenerate-ok",
            files=["declared.txt"],
            base_branch="main",
        )

        assert result["status"] == "pushed", result
        assert result["merge_discarded_sha"] == merge_sha[:7], result
        assert result["merge_degenerate"] is True, result
        assert "merge_rebuilt_parents" not in result, result
        published = _sha(publish_ctx.origin, "refs/heads/feat/merge-degenerate-ok")
        assert result["sha"] == published[:7], result
        assert _sha(publish_ctx.origin, f"{published}^") == remote_tip
        second = _git(publish_ctx.origin, "rev-parse", "--verify", f"{published}^2")
        assert second.returncode != 0, second.stdout
        _assert_scan_before_push(publish_ctx)


class TestForgedLocalBaseWithRemoteBase:
    """A forged local base is refused even when the remote base exists."""

    def test_forged_local_base_with_existing_remote_base_stops(
        self, publish_ctx: _PublishContext,
    ) -> None:
        """The restricted refspec hides origin/base; the host read exposes it.

        The frozen local-fallback test covers a base the remote does not
        have.  Here the remote base exists at ``remote_base`` while the clone
        can only resolve a forged local ``feat/base-live``; the host SHA is
        the expected parent, so the publish is refused with both SHAs.
        """
        remote_base = _remote_branch(
            publish_ctx, "feat/base-live", {"base.txt": "base\n"},
        )
        assert _git(publish_ctx.clone, "fetch", "origin").returncode == 0

        _git(
            publish_ctx.clone, "config", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        _git(publish_ctx.clone, "update-ref", "-d", "refs/remotes/origin/feat/base-live")
        assert _remote_ref(publish_ctx.clone, "feat/base-live") is None
        assert _remote_ref(publish_ctx.origin, "feat/base-live") == remote_base

        forged = _forge_bad_commit(publish_ctx.clone, "origin/main")
        assert forged != remote_base
        _checkout_branch(publish_ctx.clone, "feat/base-live", forged)
        (Path(publish_ctx.clone) / "stacked.txt").write_text("stacked\n")

        result, pr_mock = _publish(
            publish_ctx,
            branch="feat/stacked-forged",
            files=["stacked.txt"],
            base_branch="feat/base-live",
        )

        assert result["status"] == "error", result
        assert result["step"] == "cut_parent_mismatch", result
        assert result["parent"] == 1, result
        assert result["expected_sha"] == remote_base, result
        assert result["actual_sha"] == forged, result
        _assert_cut_parent_mismatch(
            result,
            expected_sha=remote_base,
            actual_sha=forged,
            ref_names=("refs/heads/feat/base-live", "feat/base-live"),
        )
        _assert_nothing_pushed(publish_ctx, pr_mock, "feat/stacked-forged")



# ============================================================================
# The raw object read: replace refs and grafts cannot hide a forged parent
# ============================================================================


class TestRawCommitObjectRead:
    """The guard reads the created object, not the revised history around it."""

    @staticmethod
    def _install_spoof_hook(
        clone: str, kind: str, legitimate_sha: str,
    ) -> None:
        """Make the publish commit's post-commit hook spoof parent traversal.

        *legitimate_sha* is the commit the host guard expects as the parent
        (here, the remote base branch tip).
        """
        hook = Path(clone) / ".git" / "hooks" / "post-commit"
        if kind == "replace":
            script = f"""#!/bin/sh
set -e
c=$(git rev-parse --verify HEAD)
tree=$(git rev-parse "$c^{{tree}}")
cprime=$(git -c user.name=forge -c user.email=forge@example.com commit-tree "$tree" -p {legitimate_sha} -m replacement)
git replace "$c" "$cprime"
"""
        else:
            script = f"""#!/bin/sh
set -e
c=$(git rev-parse --verify HEAD)
mkdir -p .git/info
printf '%s %s\\n' "$c" {legitimate_sha} >> .git/info/grafts
"""
        hook.write_text(script)
        hook.chmod(0o755)

    @pytest.mark.parametrize("kind", ["replace", "graft"])
    def test_spoofed_traversal_cannot_hide_a_forged_parent(
        self, publish_ctx: _PublishContext, kind: str,
    ) -> None:
        """A replacement ref or graft rewrites HEAD^1; the raw object does not.

        The publish commit is cut from a forged local base (the host knows
        the remote base instead), but the post-commit hook rewrites traversal
        so ``rev-parse HEAD^1`` reports the host-authorized remote base.  A
        guard that trusts ``HEAD^1`` would pass and push the forgery.
        """
        remote_base = _remote_branch(
            publish_ctx, "feat/base-live", {"base.txt": "base\n"},
        )
        assert _git(publish_ctx.clone, "fetch", "origin").returncode == 0
        # Restrict the refspec and drop the tracking ref so prepare_commit
        # has to fall back to the local branch of the same name.
        _git(
            publish_ctx.clone, "config", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        _git(
            publish_ctx.clone, "update-ref", "-d",
            "refs/remotes/origin/feat/base-live",
        )
        forged = _forge_bad_commit(publish_ctx.clone, "origin/main")
        _checkout_branch(
            publish_ctx.clone, "feat/base-live", forged,
        )
        (Path(publish_ctx.clone) / "README.md").write_text(
            "# Initial\nhidden spoof\n",
        )
        self._install_spoof_hook(
            publish_ctx.clone, kind, remote_base,
        )

        result, pr_mock = _publish(
            publish_ctx,
            branch="feat/stacked-spoof",
            files=["README.md"],
            base_branch="feat/base-live",
        )

        assert result["status"] == "error", result
        assert result["step"] == "cut_parent_mismatch", result
        assert result["parent"] == 1, result
        assert result["expected_sha"] == remote_base, result
        assert result["actual_sha"] == forged, result
        # Show that the spoof is real: a traversal-based guard would have
        # compared against the remote base and allowed the push.
        assert _sha(publish_ctx.clone, "HEAD^1") == remote_base
        _assert_cut_parent_mismatch(
            result,
            expected_sha=remote_base,
            actual_sha=forged,
            ref_names=(
                "refs/heads/feat/base-live", "feat/base-live",
                "feat/stacked-spoof",
            ),
        )
        _assert_nothing_pushed(
            publish_ctx, pr_mock, "feat/stacked-spoof",
        )


# ============================================================================
# The push is pinned to the SHA the guard validated
# ============================================================================


def test_build_push_command_pins_the_validated_sha() -> None:
    """The refspec names the validated object; legacy callers keep the branch."""
    sha = "a" * 40
    pinned = build_push_command("feat/pinned", False, source_sha=sha)
    assert f"{sha}:refs/heads/feat/pinned" in pinned
    legacy = build_push_command("feat/pinned", False)
    assert legacy.endswith("push origin feat/pinned")
    assert sha not in legacy


def test_push_reports_the_validated_sha_not_a_later_head() -> None:
    """A moved branch after validation cannot change the reported SHA."""
    calls: list[str] = []

    def run(cmd: str, env: dict | None = None) -> tuple[int, str, str]:
        del env
        calls.append(cmd)
        if cmd.startswith("git push"):
            return 0, "", ""
        return 0, "f" * 40, ""

    err, sha, transport = git_push_with_fallback(
        run,
        repo="o/r",
        branch="feat/pinned",
        cid="cid",
        push_cmd="git push origin pinned",
        push_env=None,
        network_off=False,
        token_missing=False,
        try_api_push=lambda: {},
        record_crossing=lambda _reason, _approved: None,
        verified_sha="a" * 40,
    )
    assert err is None
    assert (sha, transport) == ("a" * 7, "native")
    assert not any("rev-parse HEAD" in command for command in calls)

    calls.clear()
    err, sha, transport = git_push_with_fallback(
        run,
        repo="o/r",
        branch="feat/pinned",
        cid="cid",
        push_cmd="git push origin pinned",
        push_env=None,
        network_off=False,
        token_missing=False,
        try_api_push=lambda: {},
        record_crossing=lambda _reason, _approved: None,
    )
    assert err is None
    assert (sha, transport) == ("f" * 7, "native")
    assert any("rev-parse HEAD" in command for command in calls)


def test_native_push_pins_the_validated_commit_after_a_ref_move(
    publish_ctx: _PublishContext,
) -> None:
    """Moving refs/heads/<branch> after the guard cannot publish another commit."""
    (Path(publish_ctx.clone) / "README.md").write_text(
        "# Initial\npinned native\n",
    )
    captured: dict[str, str] = {}
    original_exec_run = publish_ctx.container.exec_run

    def exec_run(cmd: list[str] | None = None, **kwargs: object):
        argv = list(cmd or [])
        shell_cmd = (
            argv[2]
            if len(argv) > 2 and argv[0] in ("/bin/sh", "sh")
            else " ".join(argv)
        )
        reply = original_exec_run(cmd=cmd, **kwargs)
        if f"{_CUT_PARENT_MARK} single" in shell_cmd:
            if kwargs.get("demux"):
                raw = reply[1][0]
            else:
                raw = reply[1]
            created = raw.decode("utf-8").splitlines()[1].strip()
            captured["sha"] = created
            captured["moved"] = _commit_tree_child(publish_ctx.clone, created)
            moved = _git(
                publish_ctx.clone, "update-ref",
                "refs/heads/feat/native-pinned", captured["moved"],
            )
            assert moved.returncode == 0, moved.stderr
        return reply

    publish_ctx.container.exec_run = exec_run

    result, pr_mock = _publish(
        publish_ctx,
        branch="feat/native-pinned",
        files=["README.md"],
    )

    assert result["status"] == "pushed", result
    published = _sha(publish_ctx.origin, "refs/heads/feat/native-pinned")
    assert published == captured["sha"]
    assert published != captured["moved"]
    assert result["sha"] == published[:7], result
    pr_mock.assert_not_called()


_FAKE_GH_SCRIPT = """#!/usr/bin/env python3
import base64
import json
import os
import subprocess
import sys

origin = os.environ["FAKE_GH_ORIGIN"]
args = sys.argv[1:]
method = "GET"
path = ""
body = {}
index = 0
while index < len(args):
    token = args[index]
    if token == "api":
        index += 1
    elif token == "-X":
        method = args[index + 1]
        index += 2
    elif token == "--input":
        with open(args[index + 1], encoding="utf-8") as handle:
            body = json.load(handle)
        index += 2
    else:
        path = token
        index += 1

parts = [part for part in path.strip("/").split("/") if part]
kind = parts[-1] if parts else ""
author = body.get("author", {})
committer = body.get("committer", author)
env = dict(os.environ)
env.update({
    "GIT_AUTHOR_NAME": author.get("name", "fixture"),
    "GIT_AUTHOR_EMAIL": author.get("email", "fixture@example.com"),
    "GIT_AUTHOR_DATE": author.get("date", "now"),
    "GIT_COMMITTER_NAME": committer.get("name", "fixture"),
    "GIT_COMMITTER_EMAIL": committer.get("email", "fixture@example.com"),
    "GIT_COMMITTER_DATE": os.environ.get(
        "FAKE_GH_COMMITTER_DATE", committer.get("date", "now")
    ),
})

if method == "GET" and "ref" in parts:
    head = parts.index("heads")
    ref = "refs/heads/" + "/".join(parts[head + 1:])
    result = subprocess.run(
        ["git", "-C", origin, "rev-parse", "--verify", ref],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.stderr.write("gh: Not Found (HTTP 404)\\n")
        sys.exit(1)
    print(json.dumps({"object": {"sha": result.stdout.strip()}}))
    sys.exit(0)
elif kind == "blobs":
    content = base64.b64decode(body["content"])
    result = subprocess.run(
        ["git", "-C", origin, "hash-object", "-w", "--stdin"],
        input=content, capture_output=True, check=True,
    )
    sha = result.stdout.decode().strip()
elif kind == "trees":
    lines = [
        f'{item["mode"]} {item["type"]} {item["sha"]}\\t{item["path"]}'
        for item in body["tree"]
    ]
    result = subprocess.run(
        ["git", "-C", origin, "mktree"],
        input=("\\n".join(lines) + "\\n").encode(),
        capture_output=True, check=True,
    )
    sha = result.stdout.decode().strip()
elif kind == "commits":
    command = ["git", "-C", origin, "commit-tree", body["tree"]]
    for parent in body.get("parents", []):
        command += ["-p", parent]
    command += ["-m", body["message"]]
    result = subprocess.run(command, capture_output=True, check=True, env=env)
    sha = result.stdout.decode().strip()
elif method in ("PATCH", "POST") and "refs" in parts:
    if method == "PATCH":
        head = parts.index("heads")
        ref = "refs/heads/" + "/".join(parts[head + 1:])
    else:
        ref = body["ref"]
    sha = body["sha"]
    subprocess.run(
        ["git", "-C", origin, "update-ref", ref, sha], check=True,
    )
    print(json.dumps({"object": {"sha": sha}}))
    sys.exit(0)
else:
    sys.stderr.write(f"fake gh: unsupported {method} {path}\\n")
    sys.exit(1)

print(json.dumps({"sha": sha}))
"""


def _fake_gh_env(tmp_path: Path, origin: str) -> dict[str, str]:
    """Install a ``gh`` that builds real objects in the bare origin."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH_SCRIPT)
    gh.chmod(0o755)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_GH_ORIGIN": origin,
        # Simulate an Objects API commit whose metadata differs from the
        # validated source while preserving its tree and raw parents.
        "FAKE_GH_COMMITTER_DATE": "@2000000000 +0000",
    }


def test_api_fallback_publishes_the_validated_raw_commit(
    publish_ctx: _PublishContext, tmp_path: Path,
) -> None:
    """The API fallback preserves source tree/parents after local refs move.

    The API may recreate the source commit with different metadata and thus a
    new SHA.  A replacement ref spoofs the parent as observed through ordinary
    revision lookup; the API source must still come from the captured raw
    object, not the moved local branch.
    """
    base = _sha(publish_ctx.clone, "origin/main")
    tree = _sha(publish_ctx.clone, f"{base}^{{tree}}")

    def commit_tree(parent: str, message: str) -> str:
        result = _git(
            publish_ctx.clone,
            "-c", "user.name=fixture",
            "-c", "user.email=fixture@example.com",
            "commit-tree", tree, "-p", parent, "-m", message,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    forged_parent = commit_tree(base, "forged parent")
    validated = commit_tree(base, "validated api commit")
    replacement = commit_tree(forged_parent, "replacement")
    replaced = _git(publish_ctx.clone, "replace", validated, replacement)
    assert replaced.returncode == 0, replaced.stderr
    moved = _commit_tree_child(publish_ctx.clone, validated)
    _checkout_branch(publish_ctx.clone, "feat/api-pinned", moved)

    result = _try_api_push(
        publish_ctx.container,
        "cid",
        "owner/repo",
        "feat/api-pinned",
        publish_ctx.clone,
        env=_fake_gh_env(tmp_path, publish_ctx.origin),
        verified_sha=validated,
    )

    published = _sha(publish_ctx.origin, "refs/heads/feat/api-pinned")
    assert result["status"] == "ok", result
    assert result["sha"] == published[:7], result
    assert published != moved
    assert published != validated  # API metadata recreation changes the SHA.

    def raw_header(repo: str, sha: str) -> list[str]:
        raw = _git(
            repo, "--no-replace-objects", "cat-file", "-p", sha,
        )
        assert raw.returncode == 0, raw.stderr
        return raw.stdout.partition("\n\n")[0].splitlines()

    source_header = raw_header(publish_ctx.clone, validated)
    published_header = raw_header(publish_ctx.origin, published)
    source_tree = next(line for line in source_header if line.startswith("tree "))
    published_tree = next(line for line in published_header if line.startswith("tree "))
    source_parents = [
        line for line in source_header if line.startswith("parent ")
    ]
    published_parents = [
        line for line in published_header if line.startswith("parent ")
    ]

    assert source_tree == published_tree == f"tree {tree}"
    assert published_parents == source_parents == [f"parent {base}"]
    assert _sha(publish_ctx.origin, "refs/heads/feat/api-pinned") == published
