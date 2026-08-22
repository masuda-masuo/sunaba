"""Publish tool: commit, push, PR creation."""

from __future__ import annotations  # noqa: I001

import base64
import json
import logging
import os
import re
import shlex
from typing import Any, NamedTuple
from urllib.parse import quote

from docker.errors import NotFound

from sunaba import proxy_lifecycle
from sunaba.journal import record_boundary_crossing
from sunaba.proxy_client import (
    ProxyAuthError,
    authorized_push_grant,
    proxy_configured,
)
from sunaba.security import NETWORK_LABEL
from sunaba.tools.common import _docker, container_not_found_error
from sunaba.tools.github_api import (
    _create_pr_via_api,
    _push_token_env,
    _resolve_vcs_token,
)
from sunaba.tools.publish_ops import (
    RunFunc,
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
from sunaba.tools.vcs.gitroot import resolve_git_root
from sunaba.verify_state import has_verify_success

logger = logging.getLogger(__name__)


class AutoIncludeResult(NamedTuple):
    """Result of a host-side base auto-include fetch (issue #712 Candidate C).

    Attributes
    ----------
    included:
        ``path -> content_or_None`` for files that were successfully fetched
        (or signalled as a deletion) from the base branch.  ``str`` = UTF-8
        text, ``bytes`` = binary/non-UTF-8 content, ``None`` = deletion
        (issue #715).
    skipped:
        Paths that appeared in the Compare API diff but could **not** be
        auto-included (``"renamed"`` status, per-file Contents API fetch
        failure, or non-``base64`` encoding).  These are the invisible gaps
        that issue #711 makes visible.
    """
    included: dict[str, str | bytes | None]
    skipped: list[str]


# ---------------------------------------------------------------------------
# Validation regexes
# ---------------------------------------------------------------------------

_BRANCH_RE = re.compile(
    r"^(?!.*\.\.)(?!.*\.lock$)(?!-)(?!.*@\{)"
    r"[\w./-]+$"
)


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


def _read_blob(path):
    \"\"\"Read blob content from git (binary-safe): returns bytes of what git
    committed for *path* at HEAD.  Git stores symlink targets as their
    target-path string, so this is safe for symlinks too (open() follows
    symlinks and reads the target file instead).\"\"\"
    r = subprocess.run(
        [\"git\", \"cat-file\", \"blob\", f\"HEAD:{path}\"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise OSError(
            r.stderr.decode(errors=\"replace\").strip()
            or f\"git cat-file blob HEAD:{path} failed (exit {r.returncode})\"
        )
    return r.stdout


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
files = [f for f in files_out.split("\\n") if f]

# 3. Create blobs
tree_items = []
for filepath in files:
    _, mode_line, _ = _run(f"git ls-tree HEAD -- {shlex.quote(filepath)}")
    if not mode_line.strip():
        print(json.dumps({"status": "error", "error": f"git ls-tree HEAD -- {filepath}: empty output"}))
        sys.exit(1)
    parts = mode_line.split()
    if len(parts) < 3:
        print(json.dumps({"status": "error", "error": f"unexpected ls-tree output for {filepath}: {mode_line!r}"}))
        sys.exit(1)
    mode = parts[0]
    try:
        raw = _read_blob(filepath)
        file_content = base64.b64encode(raw).decode()
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
# publish — token / REST API / PR creation moved to github_api.py
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


# ---------------------------------------------------------------------------
# Host-side merge auto-include (issue #712 Candidate C)
# ---------------------------------------------------------------------------


def _fetch_base_auto_include(
    repo: str,
    token: str,
    branch: str,
    base_branch: str = "",
) -> AutoIncludeResult | None:
    """Fetch files that the base branch advanced since *branch* was last pushed.

    Host-side read via GitHub REST API, never from the container
    (Candidate C, issue #712).  This closes the forgeable skip-the-reset
    branch in ``git_prepare_commit``.

    Parameters
    ----------
    repo:
        ``\"owner/repo\"``.
    token:
        VCS token for authentication (may be empty for public repos).
    branch:
        The feature branch name (used to determine its current remote tip).
    base_branch:
        The merge-source branch.  When empty, resolves to the repo's
        default branch.

    Returns
    -------
    An ``AutoIncludeResult`` with ``included`` (dict of ``path -> content_or_None``)
    for files that were successfully fetched, and ``skipped`` (list of paths that
    appeared in the Compare API diff but could not be auto-included due to
    rename status, Contents API fetch failure, or non-base64 encoding).
    Returns ``None`` on any error (safe fallback: no auto-include).
    """
    from sunaba.tools.github_api import _github_api_request

    # Resolve the base branch (default branch if not given).
    if not base_branch:
        try:
            repo_info = _github_api_request(f"/repos/{repo}", token)
            base_branch = str(repo_info.get("default_branch") or "")
        except Exception as exc:
            logger.warning(
                "Could not resolve default branch for %s: %s",
                repo, exc,
            )
            return None

    if not base_branch:
        return None

    # Get the feature branch's remote tip SHA.
    try:
        ref_info = _github_api_request(
            f"/repos/{repo}/git/refs/heads/{branch}", token,
        )
        feature_sha = str(ref_info.get("object", {}).get("sha") or "")
    except Exception as exc:
        logger.warning(
            "Could not get remote ref for branch %s in %s: %s",
            branch, repo, exc,
        )
        return None

    if not feature_sha:
        # Branch doesn't exist on remote yet -- no auto-include needed.
        return AutoIncludeResult(included={}, skipped=[])

    # Get the base branch's tip SHA.
    try:
        ref_info = _github_api_request(
            f"/repos/{repo}/git/refs/heads/{base_branch}", token,
        )
        base_sha = str(ref_info.get("object", {}).get("sha") or "")
    except Exception as exc:
        logger.warning(
            "Could not get remote ref for base branch %s in %s: %s",
            base_branch, repo, exc,
        )
        return None

    if not base_sha:
        return None

    # Compare: what changed between feature_sha and base_sha?
    try:
        compare = _github_api_request(
            f"/repos/{repo}/compare/{feature_sha}...{base_sha}",
            token,
        )
    except Exception as exc:
        logger.warning(
            "Compare API failed for %s...%s in %s: %s",
            feature_sha, base_sha, repo, exc,
        )
        return None

    files = compare.get("files", [])
    if not files:
        return AutoIncludeResult(included={}, skipped=[])

    result: dict[str, str | bytes | None] = {}
    skipped: list[str] = []
    for f in files:
        status = f.get("status", "")
        filename = f.get("filename", "")
        if not filename:
            continue

        if status == "removed":
            # Base branch deleted this file -- signal deletion (issue #715).
            result[filename] = None
            continue

        if status not in ("added", "modified"):
            # e.g. "renamed" -- track as skipped (issue #711).
            skipped.append(filename)
            continue

        # Fetch the file content from the base branch tip.
        try:
            content_resp = _github_api_request(
                f"/repos/{repo}/contents/{filename}?ref={base_sha}",
                token,
            )
        except Exception as exc:
            logger.warning(
                "Could not fetch content for %s@%s: %s",
                filename, base_sha, exc,
            )
            skipped.append(filename)
            continue

        content_b64 = content_resp.get("content", "")
        encoding = content_resp.get("encoding", "")
        if not content_b64 or encoding != "base64":
            skipped.append(filename)
            continue

        raw = base64.b64decode(content_b64)
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            # Binary content: keep as bytes (issue #716, non-UTF-8 files).
            result[filename] = raw
        else:
            result[filename] = decoded

    return AutoIncludeResult(included=result, skipped=skipped)


# ---------------------------------------------------------------------------
# Upstream-overwrite guard (issue #863), split across the credential
# boundary (issue #866)
# ---------------------------------------------------------------------------
#
# The guard answers one question: would staging the declared paths as whole
# snapshots revert something the base branch has and this container never
# saw?  Answering it needs two refs, and they live on opposite sides of the
# credential boundary:
#
# * the *merge-base* and its blobs are in the container's own object store,
#   readable with no credential at all;
# * the base branch's *current tip* is only knowable from the host, which
#   holds the VCS token.  The container cannot fetch it -- tokens never
#   enter the sandbox, so ``git fetch origin`` against a private repo dies
#   with "could not read Username" every single time.  #864 read the tip
#   in-container behind ``|| true``, which meant it compared the clone-point
#   refs against themselves and reported clean for every private-repo
#   publish (#866).
#
# So: merge-base from the container, tip from the host, comparison pure.
#
# The comparison needs one more container-side datum: the blob the working
# tree would stage.  Replacing upstream bytes is only a violation when the
# replacement differs from what the base tip holds -- staging bytes
# identical to the tip replaces nothing, so a path whose worktree copy
# already IS the tip is not a conflict.  That is the #865 residual: a first
# PR squash-merged, then the same manifest re-published, leaves the
# container's worktree byte-identical to the base tip while its merge-base
# still holds the pre-merge bytes.

#: Opens the guard probe's machine-readable block.  The probe shares one
#: exec with the ``git fetch origin`` refresh, whose own chatter lands on the
#: same combined stream, so the parser keys off this marker instead of
#: trusting the whole output.
_UPSTREAM_GUARD_MARK = "__sunaba_upstream_guard__"

#: Opens each per-path section of the follow-up ``git log`` probe.
_UPSTREAM_COMMITS_MARK = "__sunaba_upstream_commits__"

#: Carries the declared paths into both probes, one per line, so neither
#: command text depends on the manifest.
_UPSTREAM_GUARD_PATHS_ENV = "SUNABA_UPSTREAM_GUARD_PATHS"

#: How many upstream one-liners are reported per conflicting path.
_UPSTREAM_COMMITS_LIMIT = 20

#: A declared path that does not exist at the ref being read.  Both halves
#: spell absence the same way, so "absent at both refs" falls out of plain
#: equality instead of needing its own case.  ``-`` cannot collide with a
#: blob id.
_BLOB_ABSENT = "-"

#: A declared path whose blob id at the base tip could not be read at all.
#: Such a path is reported as undetermined rather than compared: a clean
#: verdict from a comparison that never happened is exactly the #866 defect.
_BLOB_UNREADABLE = "?"


class UpstreamMergeBaseBlobs(NamedTuple):
    """The container-side half: what this clone last saw of the base
    branch, plus what its working tree would stage.

    Both come from the container's own object store and working tree, so
    they are available whether or not the container can reach the remote --
    which, by design, it cannot.

    Attributes
    ----------
    merge_base:
        Where the container's ``HEAD`` last met ``origin/<base_branch>`` --
        the newest upstream state this container has ever seen.
    blobs:
        ``path -> blob id at *merge_base*``, or :data:`_BLOB_ABSENT` when
        the path does not exist there.
    worktree_blobs:
        ``path -> blob id of the working tree copy`` -- the bytes staging
        the path would publish.  :data:`_BLOB_ABSENT` when the path is
        absent from the working tree (a declared deletion);
        :data:`_BLOB_UNREADABLE` when the file is there but could not be
        hashed -- an unreadable file must never masquerade as a deletion
        (#865).
    """

    merge_base: str
    blobs: dict[str, str]
    worktree_blobs: dict[str, str] = {}


class UpstreamTipRead(NamedTuple):
    """The host-side half: what the base branch holds *now*.

    Read through the GitHub REST API from the host process with the VCS
    token -- the same access class :func:`_fetch_base_auto_include` uses,
    and the only side of the boundary that can answer for a private repo.

    Attributes
    ----------
    base_sha:
        The base branch's tip commit, or ``""`` when it could not be read.
    blobs:
        ``path -> blob id at *base_sha*``, :data:`_BLOB_ABSENT`, or
        :data:`_BLOB_UNREADABLE`.
    error:
        Empty on success.  Non-empty means no read happened at all (no tip,
        no blobs) and says why.
    """

    base_sha: str
    blobs: dict[str, str]
    error: str


class UpstreamGuardReport(NamedTuple):
    """The guard's verdict on a manifest, from both halves together.

    Attributes
    ----------
    merge_base:
        The container-side sync point the comparison used (``""`` when
        there was none).
    conflicts:
        Declared paths whose blob at the base branch's real tip differs
        from their blob at *merge_base* **and** from the blob the
        container's working tree would stage.  Only these paths' upstream
        changes a manifest publish would overwrite with bytes it has not
        seen (issue #863); a worktree copy already identical to the tip
        reverts nothing and is not a conflict (#865).
    undetermined:
        Empty when every declared path was actually compared.  Otherwise a
        short reason why the guard could not obtain a usable comparison --
        the honest form of ``upstream_guard_undetermined`` (#865 finding 1,
        #866).  #818's contract stands: undetermined never blocks publish.
    """

    merge_base: str
    conflicts: list[str]
    undetermined: str


def _upstream_guard_probe(
    base_ref: str, paths: list[str],
) -> tuple[str, dict[str, str]]:
    """Build the combined refresh + merge-base probe and its environment.

    The ``git fetch origin`` refresh (#818) still leads, because base *ref
    resolution* for the commit's parents wants the freshest ref the clone
    can have.  Nothing the guard decides depends on it any more: the probe
    reads only the merge-base and its blobs, which are in the clone whether
    the fetch worked, failed, or was never possible (#866).

    One exec, not one per path: everything the container side owes the
    decision is printed under :data:`_UPSTREAM_GUARD_MARK` as ``blob
    <index> <merge_base_blob> <worktree_blob>`` -- the blob at the
    merge-base and the blob the working tree would stage, per declared
    path.  Both spell :data:`_BLOB_ABSENT` for "absent" (no such path at
    the merge-base; no such file in the working tree -- a declared
    deletion), and the worktree field spells :data:`_BLOB_UNREADABLE`
    when the file is there but ``git hash-object`` cannot hash it: an
    unreadable file must never masquerade as a deletion (#865).

    The declared paths travel in the environment, never in the command
    text.  A manifest path is arbitrary caller input; keeping it out means
    the command this exec runs is a fixed string that says "refresh and
    read one ref", instead of a different string per publish that quotes
    user data into a shell loop.  Paths are reported back by index, so one
    containing whitespace cannot be misread as several fields.
    """
    fetch = "git fetch origin 2>/dev/null || true"
    if not paths:
        return fetch, {}
    command = (
        f"{fetch}; "
        f"_sb={shlex.quote(base_ref)}; "
        '_sm=$(git merge-base HEAD "$_sb" 2>/dev/null) || _sm=; '
        f"echo {shlex.quote(_UPSTREAM_GUARD_MARK)}; "
        'printf "base %s\\n" "$_sm"; '
        # No sync point (no such remote ref in this clone) means nothing to
        # compare: report the empty base and let the caller proceed.
        '[ -n "$_sm" ] || exit 0; '
        "_si=0; "
        f'printf "%s\\n" "${_UPSTREAM_GUARD_PATHS_ENV}" | '
        "while IFS= read -r _sp; do "
        '_sa=$(git rev-parse --quiet --verify "$_sm:$_sp" 2>/dev/null) || _sa=-; '
        'if test -f "$_sp"; then '
        '_sw=$(git hash-object -- "$_sp" 2>/dev/null) || _sw=?; '
        'else _sw=-; fi; '
        f'printf "blob %s %s %s\\n" "$_si" "$_sa" "$_sw"; '
        "_si=$((_si+1)); "
        "done"
    )
    return command, {_UPSTREAM_GUARD_PATHS_ENV: "\n".join(paths)}


def _read_merge_base_blobs(
    output: str, paths: list[str],
) -> UpstreamMergeBaseBlobs | None:
    """Parse the container-side probe's output -- pure, and stateless.

    Expects one ``blob <index> <merge_base_blob> <worktree_blob>`` line
    per declared path, mirroring :func:`_upstream_guard_probe`'s format.

    Returns ``None`` when the probe answered nothing usable: no marker, no
    merge-base, or a line per declared path missing.  The caller turns that
    into an undetermined verdict, never into a clean one.
    """
    if _UPSTREAM_GUARD_MARK not in output:
        return None

    merge_base = ""
    indexed: dict[int, tuple[str, str]] = {}
    for line in output.split(_UPSTREAM_GUARD_MARK, 1)[1].splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "base":
            merge_base = fields[1]
        elif len(fields) == 4 and fields[0] == "blob":
            try:
                index = int(fields[1])
            except ValueError:
                continue
            indexed[index] = (fields[2], fields[3])

    if not merge_base or set(indexed) != set(range(len(paths))):
        return None

    return UpstreamMergeBaseBlobs(
        merge_base=merge_base,
        blobs={path: indexed[index][0] for index, path in enumerate(paths)},
        worktree_blobs={
            path: indexed[index][1] for index, path in enumerate(paths)
        },
    )


def _fetch_base_tip_blobs(
    repo: str,
    token: str,
    base_branch: str,
    paths: list[str],
) -> UpstreamTipRead:
    """Read the declared paths' blob ids at the base branch tip, host-side.

    Mirrors :func:`_fetch_base_auto_include`'s access pattern -- same token,
    same REST endpoints, same host process -- because that is the only side
    of the boundary holding a credential.  Bounded by the manifest: one ref
    read plus one Contents read per declared path.

    The Contents API's ``sha`` field *is* the git blob id of the file, so
    nothing here has to be base64-decoded or re-hashed, and the two halves
    already compare on a common footing with what ``git rev-parse
    <merge_base>:<path>`` prints in the container.  It is also the field
    that survives the API's size limit: for a file too large to inline,
    ``content`` comes back empty but ``sha`` is still the blob id.

    Never raises.  Every failure becomes a recorded reason instead, because
    the caller has to be able to tell "read, and identical" apart from
    "could not read" (#866).
    """
    from sunaba.tools.github_api import _github_api_request

    if not paths:
        return UpstreamTipRead(base_sha="", blobs={}, error="")

    # Resolve the base branch (default branch if not given), exactly as the
    # auto-include path does.
    if not base_branch:
        try:
            repo_info = _github_api_request(f"/repos/{repo}", token)
            base_branch = str(repo_info.get("default_branch") or "")
        except Exception as exc:
            return UpstreamTipRead(
                "", {},
                f"could not resolve the default branch of {repo}: {exc}",
            )
    if not base_branch:
        return UpstreamTipRead("", {}, f"{repo} reports no default branch")

    try:
        ref_info = _github_api_request(
            f"/repos/{repo}/git/refs/heads/{base_branch}", token,
        )
        base_sha = str(ref_info.get("object", {}).get("sha") or "")
    except Exception as exc:
        return UpstreamTipRead(
            "", {}, f"could not read the tip of {repo}@{base_branch}: {exc}",
        )
    if not base_sha:
        return UpstreamTipRead(
            "", {}, f"{repo}@{base_branch} reports no tip commit",
        )

    # Every path is read at the same pinned commit, so a base branch that
    # advances mid-publish cannot produce a half-old comparison.
    blobs: dict[str, str] = {}
    for path in paths:
        try:
            entry: Any = _github_api_request(
                f"/repos/{repo}/contents/{quote(path)}?ref={base_sha}", token,
            )
        except Exception as exc:
            if "HTTP 404" in str(exc):
                # An answer, not a failure: the tip does not carry this
                # path.  Treating 404 as "absent" is safe even when the 404
                # is spurious -- an access hiccup on a path that does exist
                # upstream: that path still has a blob at the merge-base,
                # so tip_blob=absent differs from the merge-base blob and
                # the guard refuses (fail closed).  A spurious 404 can
                # never silently clear a conflict.
                blobs[path] = _BLOB_ABSENT
            else:
                logger.warning(
                    "Upstream guard: could not read %s@%s:%s: %s",
                    repo, base_sha, path, exc,
                )
                blobs[path] = _BLOB_UNREADABLE
            continue
        # A directory answers with a JSON list, and a malformed entry with
        # no ``sha`` leaves nothing comparable.  Either way we did not get a
        # blob id, and saying so beats guessing at one.
        sha = entry.get("sha") if isinstance(entry, dict) else None
        blobs[path] = str(sha) if sha else _BLOB_UNREADABLE

    return UpstreamTipRead(base_sha=base_sha, blobs=blobs, error="")


def _read_upstream_guard(
    local: UpstreamMergeBaseBlobs | None,
    tip: UpstreamTipRead,
    paths: list[str],
) -> UpstreamGuardReport:
    """Compare the two halves -- pure, stateless, and never optimistic.

    The invariant this protects: **a manifest publish must never replace
    upstream bytes with different bytes it has not seen**.  Each declared
    path is staged as a whole snapshot of the container's copy, so the
    question per path is whether staging the worktree copy would change
    what the base tip holds.

    A path is a conflict only when the tip blob differs from the
    merge-base blob **and** from the worktree blob:

    * tip == merge-base -- upstream never moved the path; nothing to
      refuse (as before).
    * tip == worktree -- equal blob ids, or both :data:`_BLOB_ABSENT`.
      Staging replaces identical bytes, or deletes what upstream already
      deleted; nothing is reverted, so it is not a conflict.  This is the
      squash-merged re-publish shape: the container's change already
      reached the base branch, so its worktree copy IS the tip (#865).
    * worktree :data:`_BLOB_UNREADABLE` -- it can never equal the tip, so
      the guard fails closed: conflict whenever tip != merge-base (as
      before).
    * otherwise -- tip differs from both halves; conflict, exactly as
      before.

    A path the host side could not read is *not* compared: it lands in
    ``undetermined`` instead.  Reporting it identical would be #866 in
    miniature -- a clean verdict from a comparison that never happened.
    """
    if local is None:
        return UpstreamGuardReport(
            merge_base="",
            conflicts=[],
            undetermined=(
                "no merge-base between HEAD and the base branch in this "
                "container's clone -- nothing to compare against"
            ),
        )
    if tip.error:
        return UpstreamGuardReport(
            merge_base=local.merge_base,
            conflicts=[],
            undetermined=f"base branch tip unreadable: {tip.error}",
        )

    conflicts: list[str] = []
    unreadable: list[str] = []
    for path in paths:
        tip_blob = tip.blobs.get(path, _BLOB_UNREADABLE)
        if tip_blob == _BLOB_UNREADABLE:
            unreadable.append(path)
            continue
        merge_base_blob = local.blobs.get(path, _BLOB_ABSENT)
        # A missing worktree entry can never equal the tip: fail closed,
        # exactly as before the worktree comparison existed.
        worktree_blob = local.worktree_blobs.get(path, _BLOB_UNREADABLE)
        if tip_blob != merge_base_blob and tip_blob != worktree_blob:
            conflicts.append(path)

    return UpstreamGuardReport(
        merge_base=local.merge_base,
        conflicts=conflicts,
        undetermined=(
            "base branch tip content unreadable for "
            f"{len(unreadable)} declared path(s): {', '.join(unreadable)}"
            if unreadable
            else ""
        ),
    )


def _upstream_conflict_commits(
    run: RunFunc, merge_base: str, base_ref: str, paths: list[str],
) -> dict[str, list[str]]:
    """List the upstream commits behind each conflicting path (one exec).

    Diagnostics only: the decision is already made by the time this runs,
    so it stays local git and stays best-effort.  *base_ref* is the clone's
    own remote-tracking ref, which an unauthenticated container cannot
    refresh -- when it is stale the log comes back empty and the refusal
    says the commits could not be listed (never an empty history), rather
    than losing the path.  The paths travel in the environment for the
    same reason as in :func:`_upstream_guard_probe`.
    """
    if not paths:
        return {}
    command = (
        f"_sm={shlex.quote(merge_base)}; "
        f"_sb={shlex.quote(base_ref)}; "
        "_si=0; "
        f'printf "%s\\n" "${_UPSTREAM_GUARD_PATHS_ENV}" | '
        "while IFS= read -r _sp; do "
        f'printf "{_UPSTREAM_COMMITS_MARK} %s\\n" "$_si"; '
        f"git log --oneline --no-decorate -{_UPSTREAM_COMMITS_LIMIT} "
        '"$_sm".."$_sb" -- ":(literal)$_sp" 2>/dev/null || true; '
        "_si=$((_si+1)); "
        "done"
    )
    _, out, _ = run(
        command, {_UPSTREAM_GUARD_PATHS_ENV: "\n".join(paths)},
    )

    commits: dict[str, list[str]] = {path: [] for path in paths}
    current: int | None = None
    for line in out.splitlines():
        if line.startswith(_UPSTREAM_COMMITS_MARK):
            tail = line[len(_UPSTREAM_COMMITS_MARK):].strip()
            current = int(tail) if tail.isdigit() else None
            continue
        if current is not None and current < len(paths) and line.strip():
            commits[paths[current]].append(line.strip())
    return commits


def _upstream_overwrite_error(
    base_ref: str,
    merge_base: str,
    conflicts: list[str],
    commits: dict[str, list[str]],
    base_tip: str = "",
) -> dict[str, Any]:
    """Build publish's refusal payload for the upstream-overwrite guard.

    ``step`` is the machine-distinguishable part -- callers branch on
    ``"upstream_overwrite"`` rather than on the prose, exactly as they do for
    the other refusals publish reports.

    *base_tip* is the commit the host read the upstream side from (#866).
    It is reported because the container's own ``{base_ref}`` may well be
    behind it -- that gap is the whole reason the refusal fires.

    The per-path commit listing is best-effort local git, and the
    container cannot refresh ``{base_ref}`` (no credential inside the
    sandbox), so an empty listing means "could not be listed from a stale
    ref", never "no commits exist" -- and the conflict verdict itself
    comes from the host-side tip comparison, not from the listing.  The
    message says so rather than rendering an empty history.
    """
    listed = "\n".join(
        f"  {path}\n"
        + (
            "\n".join(f"    {line}" for line in commits.get(path, []))
            or (
                "    (upstream commits could not be listed from the "
                f"container's stale {base_ref} ref; the conflict verdict "
                "comes from the host-side tip comparison)"
            )
        )
        for path in conflicts
    )
    if any(commits.get(path) for path in conflicts):
        provenance = "predates the upstream commits above"
    else:
        provenance = (
            "predates upstream commits this container's stale refs could "
            "not list"
        )
    return {
        "status": "error",
        "step": "upstream_overwrite",
        "error": (
            f"publish refused: {len(conflicts)} declared path(s) changed on "
            f"{base_ref} since this container last saw it (merge-base "
            f"{merge_base[:7]}; base tip {base_tip[:7] or 'unknown'}, read "
            "host-side).  A manifest publish stages each declared "
            "path as a whole snapshot of the container's copy, so pushing "
            "now would revert those upstream changes silently.\n"
            f"{listed}"
        ),
        "conflicting_paths": list(conflicts),
        "upstream_commits": {
            path: commits.get(path, []) for path in conflicts
        },
        "merge_base": merge_base,
        "base_ref": base_ref,
        "base_tip": base_tip,
        "hint": (
            "Re-clone from the current base and re-apply the change (the "
            f"container's copy of these paths {provenance}), or pass "
            "allow_upstream_overwrite=True to publish the container's "
            "version over the upstream one deliberately."
        ),
    }


def _validate_manifest_path(run: RunFunc, f: str, classification: dict[str, str] | None = None) -> dict[str, str] | None:
    """Validate one declared manifest path (publish's manifest mode).

    A declared path is acceptable when it is a regular file in the working
    tree, or when it is absent from the working tree but still tracked in
    the index or in HEAD (the user is declaring a deletion).  Anything else
    -- absolute or ``..``-traversing paths, directories, and paths that
    exist nowhere -- is rejected.

    Checks run in this order:

    1. repo-relative (no absolute paths, no ``..`` traversal)
    2. ``test -f`` -- a regular file in the worktree, accept
    3. ``test -d`` -- a directory, reject (a directory pathspec matches
       everything beneath it, so the tracked-path fallback and the later
       ``git add`` would stage the whole subtree, defeating the manifest)
    4. tracked as a *file* in the index (an unstaged deletion) or in HEAD (a
       staged deletion: ``git rm`` drops the entry from the index, so the
       index alone cannot see it -- issue #837), accept.  A path that
       matches a directory in the index or in HEAD is rejected as a
       directory, so a directory deleted from the worktree cannot slip
       through the fallback.

    ``:(literal)`` pathspecs disable glob interpretation so a declared path
    like ``*.py`` cannot match tracked files it does not literally name.
    It does NOT disable directory-prefix matching -- a pathspec naming a
    directory still matches everything beneath it -- which is why both
    fallback branches reject directory matches explicitly.

    Returns an error dict (``status``/``step``/``error``) or ``None`` when
    the path is acceptable.  The caller wraps the dict with ``finish_json``.
    """
    if os.path.isabs(f) or ".." in f.split("/"):
        return {
            "status": "error",
            "step": "validation",
            "error": (
                f"Invalid path '{f}': paths must be repo-relative"
                " (no absolute paths or .. traversal)."
            ),
        }
    ec, _, _ = run(f"test -f {shlex.quote(f)}")
    if ec != 0:
        # Reject directories before the tracked-path fallback below.
        # ``git ls-files --error-unmatch`` and ``git ls-tree`` both treat a
        # directory as a pathspec matching everything beneath it, so
        # ``docs`` -- and ``.`` -- would pass those checks, and the ``git
        # add`` that follows would stage the whole subtree including
        # untracked files.  That defeats the manifest, which exists
        # precisely to keep undeclared files out of the commit.
        dir_ec, _, _ = run(f"test -d {shlex.quote(f)}")
        if dir_ec == 0:
            return {
                "status": "error",
                "step": "validation",
                "error": (
                    f"Path '{f}' is a directory. "
                    "Manifests must list regular files one by one."
                ),
            }
        # Not a regular file -- allow if the path is tracked as a file in
        # the index (an unstaged deletion) or in HEAD (a staged deletion:
        # ``git rm`` drops the entry from the index, so the index alone
        # cannot see it -- issue #837).  :(literal) disables pathspec glob
        # interpretation so a declared path like '*.py' cannot match
        # tracked files it does not literally name -- but it does NOT
        # disable directory-prefix matching, so each branch below also
        # rejects a path that matches a directory instead of a file.
        track_ec, track_out, _ = run(
            "git ls-files -z --error-unmatch -- "
            + shlex.quote(f":(literal){f}")
        )
        # -z gives NUL-delimited raw paths (no C-quoting); a directory
        # pathspec lists every tracked file beneath it, so only an output
        # whose first entry is f itself names a tracked file.
        tracked_in_index = track_ec == 0 and track_out.split("\0")[0] == f
        if track_ec == 0 and not tracked_in_index:
            return {
                "status": "error",
                "step": "validation",
                "error": (
                    f"Path '{f}' is a directory. "
                    "Manifests must list regular files one by one."
                ),
            }
        in_head = False
        if not tracked_in_index:
            # ``git ls-tree`` exits 0 even when nothing matches, so test
            # the output, not the exit code.  Each matching entry reads
            # "<mode> <type> <sha>\t<path>": a file matches a ``blob``
            # entry, a directory matches a ``tree`` entry and is rejected
            # as a directory.
            head_ec, head_out, _ = run(
                "git ls-tree HEAD -- "
                + shlex.quote(f":(literal){f}")
            )
            if head_ec == 0 and head_out.strip():
                if any(
                    ln.split()[1] == "tree"
                    for ln in head_out.splitlines()
                    if ln.strip()
                ):
                    return {
                        "status": "error",
                        "step": "validation",
                        "error": (
                            f"Path '{f}' is a directory. "
                            "Manifests must list regular files one by one."
                        ),
                    }
                in_head = True
        if not tracked_in_index and not in_head:
            return {
                "status": "error",
                "step": "validation",
                "error": (
                    f"Path '{f}' does not exist or is not a regular file. "
                    "Manifests must list regular files one by one."
                ),
            }
    if classification is not None:
        classification[f] = "present" if ec == 0 else "deleted"
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
    files: list[str] | None = None,
    include_untracked: bool = False,
    allow_upstream_overwrite: bool = False,
) -> str:
    """Stage, commit, push, and optionally create a PR -- the single exit tool.

    Does NOT verify: call verify_in_container first.  No token enters the
    container.  Falls back to GitHub Objects API on push transport
    failure, never on an egress block.

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
        files: When non-empty, stage only these repo-relative paths
            (manifest mode); each must be a regular file, or tracked so a
            deletion can be declared -- never a directory.  Undeclared
            changes stay in the worktree.
        include_untracked: With no manifest, True stages untracked files
            too (the old git add -A); False (default) rejects the call when
            any exist.
        allow_upstream_overwrite: Manifest mode: publish declared paths
            whose upstream copy changed since this container's merge-base
            with the base branch.  Default False refuses
            (step: upstream_overwrite).

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

    # --- Manifest mode vs fallback mode ---
    manifest = files is not None and len(files) > 0
    # Classification established during manifest validation (present vs
    # declared deletion).  Always bound: empty in legacy mode, where no
    # manifest paths exist to classify.
    manifest_classification: dict[str, str] = {}

    if manifest:
        assert files is not None  # type-narrowing hint for pyright
        # Validate every declared path:
        # - Must be repo-relative (no absolute, no .. traversal)
        # - Must exist in the working tree OR be tracked in the index or
        #   in HEAD (i.e. the file is known to git; deletion declaration,
        #   staged or not).
        for f in files:
            path_err = _validate_manifest_path(_run, f, classification=manifest_classification)
            if path_err is not None:
                return finish_json(path_err, verified)

    # --- Secret scan (issue #676) ---
    # Scan BEFORE commit in manifest mode: the declared file list is already
    # validated and the files exist on disk.  This prevents secrets from
    # entering local git history (issue #676 [medium]).
    #
    # Lazy import avoids the circular dependency:
    #   secret_scan -> vcs.gitroot -> vcs.__init__ -> vcs.publishing -> secret_scan
    from sunaba.tools.secret_scan import (  # fmt: skip  # noqa: I001  # pyright: ignore[reportUnusedImport]
        _baseline_enabled,
        _fetch_baseline_from_base_branch,
        _extract_baseline_hashes,
        check_override,
        consume_override,
        exec_in_container,
        get_override_registry_hashes,
        run_secret_scan,
        should_consume_override,
    )

    # Declared once, before either branch: re-initialising it below the
    # manifest branch would discard the manifest scan's outcome, leaving the
    # result reporting "clean" and — worse — never consuming a used override,
    # so a single authorisation would silently stay live for every later
    # publish.
    # Default result when no scan runs at all.  Unreachable today -- the
    # manifest / not-manifest branches below are exhaustive -- so this is
    # purely the safety net for a third branch added later.  It is therefore
    # a BLOCKING state on purpose: a code path that publishes without
    # scanning is exactly the fail-open #704 closed, so the net has to catch
    # it rather than wave it through.  Anything that legitimately skips the
    # scan must say so explicitly with "skipped".
    scan_result: dict[str, Any] = {
        "secret_scan": (
            "ERROR: no secret scan ran for this publish. "
            "Scan state could not be determined; publish blocked."
        ),
        "secret_scan_state": "unknown",
        "files_scanned": [],
    }

    # --- Host-side baseline fetch (issue #708) ---
    # The suppression list is fetched from the base branch on GitHub via the
    # REST API, NOT from the container filesystem (which the agent can write
    # to).  When the fetch fails, we pass an empty set (no suppressions),
    # which is the recoverable direction: it blocks more rather than silently
    # passing a finding.  We NEVER fall back to the container's copy.
    baseline_hashes_arg: set[str] | None = None
    if _baseline_enabled():  # noqa: F821
        git_token = _resolve_vcs_token()
        try:
            baseline_data = _fetch_baseline_from_base_branch(  # noqa: F821
                repo, git_token, base_branch,
            )
            if baseline_data is not None:
                baseline_hashes_arg = _extract_baseline_hashes(  # noqa: F821
                    baseline_data,
                )
            else:
                # No baseline on the base branch: no suppressions (safe).
                baseline_hashes_arg = set()
        except Exception as exc:
            logger.warning(
                "Failed to fetch baseline from base branch: %s", exc,
            )
            # Safe: no suppressions, all findings reported
            baseline_hashes_arg = set()

    # --- Union with override registry (issue #722) ---
    # The override registry is populated by secret_scan_override (MCP tool
    # requiring human authorization) and lives in host process memory.
    # Both sources — remote baseline fetch (#708) and override registry
    # (#722) — are host-side.  Nothing inside the container can grow
    # the suppression set.  See the design doc § "Suppressions: two
    # mechanisms, two authorities".
    registry_hashes = get_override_registry_hashes(cid)  # noqa: F821
    if registry_hashes:
        if baseline_hashes_arg is None:
            baseline_hashes_arg = set()
        baseline_hashes_arg = baseline_hashes_arg | registry_hashes

    merge_info: dict = {}  # populated by git_prepare_commit (merge mode)
    # Upstream-overwrite guard state (#863).  Manifest mode only -- the
    # guard compares declared paths, so a stage-all publish has nothing to
    # declare; the defaults keep the result-building code below branch-free.
    upstream_overwritten: list[str] = []
    upstream_guard_undetermined = ""
    # Declared deletions that the secret scan skips (they are no longer in
    # the worktree).  Always bound so the result envelope can report it in
    # both manifest and legacy modes.
    files_skipped_deleted: list[str] = []
    if manifest:
        assert files is not None
        scan_files = [f for f in files if not os.path.isabs(f) and manifest_classification.get(f) != "deleted"]
        files_skipped_deleted = [f for f in files if manifest_classification.get(f) == "deleted"]
        scan_result = run_secret_scan(
            container, scan_files, working_dir,
            baseline_hashes=baseline_hashes_arg,
        )  # noqa: F821
        scan_state = scan_result.get("secret_scan_state", "")
        # Fail-closed: proceed ONLY on known-safe states.
        if scan_state not in ("clean", "skipped"):
            deleted_n = len(files_skipped_deleted)
            record_boundary_crossing(
                cid, "publish",
                f"secret_scan state={scan_state}"
                f" findings={len(scan_result.get('findings', []))}"
                f" files={scan_result.get('files_scanned', [])}"
                f" deleted={deleted_n}",
                approved=False,
            )
            has_override = check_override(cid)  # noqa: F821  # peek, don't consume yet
            if not has_override:
                return finish_json({
                    "status": "error",
                    "step": "secret_scan",
                    "secret_scan": scan_result.get("secret_scan"),
                    "secret_scan_state": scan_state,
                    "findings": scan_result.get("findings"),
                    "files_scanned": scan_result.get("files_scanned"),
                    "files_skipped_deleted": files_skipped_deleted,
                    "scan_summary": scan_result.get("scan_summary"),
                    "error": (
                        "publish blocked by secret scan. "
                        "Use `secret_scan_override` MCP tool to bypass "
                        "(requires human authorization)."
                    ),
                }, verified)

        swept_untracked: list[str] = []

        # --- Merge detection and auto-include (issue #712/#711) ---
        # When HEAD is a merge (a base merge was performed), capture the
        # discarded merge commit's SHA/parents for the response, compute the
        # set of paths the merge touched, and run the host-side auto-include.
        # These values are set independently so they survive even when
        # auto-include itself fails entirely (base_auto_include stays None,
        # but merge info is still reported).
        merge_discarded_sha: str | None = None
        merge_parents: list[str] = []
        merge_touched_paths: set[str] = set()
        auto_include_skipped: list[str] = []
        auto_include_included: dict[str, str | bytes | None] | None = None
        merge_parent_sha: str | None = None  # P2 for two-parent commit

        base_auto_include: dict[str, str | bytes | None] | None = None
        merge_ec, merge_out, _ = _run(
            "git rev-parse --verify HEAD^2 2>/dev/null"
        )
        if merge_ec == 0 and merge_out.strip():
            # Capture the merge commit info *before* git_prepare_commit
            # resets HEAD away.
            _, _sha_out, _ = _run("git rev-parse HEAD")
            merge_discarded_sha = _sha_out.strip()[:7]
            _, p1_out, _ = _run("git rev-parse HEAD^1")
            _, p2_out, _ = _run("git rev-parse HEAD^2")
            merge_parents = [p1_out.strip()[:7], p2_out.strip()[:7]]

            # Compute merge-touched paths (diagnostic only -- no security
            # decision depends on this; see #712 principle).
            _, diff_out, _ = _run(
                "git diff --name-only HEAD^1 HEAD"
            )
            merge_touched_paths = set(
                p.strip() for p in diff_out.split("\n") if p.strip()
            )

            # HEAD is a merge -- the container signals this but the
            # auto-include content always comes from the host's own
            # API call, never from the container's working tree.
            git_token = _resolve_vcs_token()
            try:
                auto_result = _fetch_base_auto_include(
                    repo, git_token, branch, base_branch,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch base auto-include for %s: %s",
                    repo, exc,
                )
                auto_result = None

            if auto_result is not None:
                base_auto_include = auto_result.included
                auto_include_skipped = auto_result.skipped
                auto_include_included = auto_result.included

        # Refresh remote-tracking refs so base resolution does not
        # work from a stale clone (#818).  Fetch failure must not
        # hard-fail publish (offline test environments) -- and for a
        # private repo it always fails, since no credential is inside
        # the container.
        #
        # The same exec reads the upstream-overwrite guard's
        # container-side half (#863): the manifest declares which files
        # the worker changed, but staging them as whole snapshots also
        # replaces whatever the base branch has now -- including commits
        # that landed after this container was cloned, which is how a
        # publish reverted a just-merged PR without a conflict or a
        # warning.  Only the merge-base is read here; the base branch's
        # real tip is read host-side below, because that is the side
        # that holds the token (#866).  Both happen before anything is
        # committed.
        guard_base_ref = (
            f"origin/{base_branch}" if base_branch else "origin/HEAD"
        )
        guard_cmd, guard_env = _upstream_guard_probe(guard_base_ref, files)
        _, guard_out, _ = _run(guard_cmd, guard_env)
        guard_local = _read_merge_base_blobs(guard_out, files)
        if guard_local is None:
            # Nothing on this side to compare against, so the host read
            # would answer a question no one can use: skip it.
            guard_tip = UpstreamTipRead(base_sha="", blobs={}, error="")
        else:
            try:
                guard_tip = _fetch_base_tip_blobs(
                    repo, _resolve_vcs_token(), base_branch, files,
                )
            except Exception as exc:  # pragma: no cover - defence in depth
                guard_tip = UpstreamTipRead("", {}, f"{exc}")
        guard_report = _read_upstream_guard(guard_local, guard_tip, files)
        if guard_report.undetermined:
            # The guard could not obtain a usable comparison.  #818's
            # contract stands: publish proceeds -- but it says so, and
            # says why, rather than letting the caller read silence as
            # "checked, clean" (#865 finding 1, #866).
            upstream_guard_undetermined = guard_report.undetermined
        if guard_report.conflicts:
            if not allow_upstream_overwrite:
                record_boundary_crossing(
                    cid, "publish",
                    "upstream_overwrite refused paths="
                    f"{guard_report.conflicts}",
                    approved=False,
                )
                return finish_json(
                    _upstream_overwrite_error(
                        guard_base_ref,
                        guard_report.merge_base,
                        guard_report.conflicts,
                        _upstream_conflict_commits(
                            _run, guard_report.merge_base,
                            guard_base_ref, guard_report.conflicts,
                        ),
                        guard_tip.base_sha,
                    ),
                    verified,
                )
            upstream_overwritten = list(guard_report.conflicts)

        merge_is_merge = merge_discarded_sha is not None
        if merge_is_merge:
            # Resolve parent2 for the two-parent rebuild (#819) AFTER
            # the fetch so it never uses a stale origin/<base_branch>
            # (the exact staleness #818 fixes for parent1).  Parent1
            # (resolved inside git_prepare_commit) and parent2 must
            # both come from the same freshly-fetched refs.
            # Container-side ``git rev-parse`` carries the same trust
            # as the reset target itself.
            # base_branch defaults to "" (= the repo default branch,
            # same as PR creation).  Fall back to origin/HEAD so the
            # common publish() call without an explicit base_branch
            # still builds the two-parent commit instead of silently
            # degrading to a single parent.
            p2_ref = (
                f"origin/{shlex.quote(base_branch)}"
                if base_branch
                else "origin/HEAD"
            )
            _, p2_sha, _ = _run(
                f"git rev-parse {p2_ref} 2>/dev/null"
            )
            if p2_sha.strip():
                merge_parent_sha = p2_sha.strip()
            else:
                merge_info["merge_second_parent_unresolved"] = p2_ref
        commit_err, committed_paths = git_prepare_commit(
            _run, branch=branch, message=message,
            files=files, author_name=author_name, author_email=author_email,
            base_auto_include=base_auto_include,
            is_merge=merge_is_merge,
            merge_parent_sha=merge_parent_sha,
            merge_result=merge_info)
        if commit_err:
            return finish_json(commit_err, verified)

        # Compute AC-4 set: paths the merge touched that are explained
        # by neither the manifest nor the base's auto-include.
        # These are the "real accident" candidates (issue #711).
        merge_discarded_undeclared: list[str] = []
        if merge_touched_paths:
            declared_or_auto = set(files or [])
            if auto_include_included is not None:
                declared_or_auto |= set(auto_include_included.keys())
            merge_discarded_undeclared = sorted(
                merge_touched_paths - declared_or_auto
            )

        # Compute leftover changes (undeclared tracked modifications,
        # untracked files) after the manifest commit so the caller can see
        # what was left behind.
        # -z gives NUL-delimited entries with paths verbatim (no C-quoting
        # of non-ASCII/special characters).  Entry format: "XY <path>";
        # a rename/copy entry is followed by the source path as its own
        # NUL-separated token.
        _, status_out, _ = _run("git status --porcelain -z")
        worktree_leftover: list[str] = []
        tokens = [t for t in status_out.split("\0") if t]
        i = 0
        while i < len(tokens):
            entry = tokens[i]
            i += 1
            if len(entry) < 4:
                continue
            worktree_leftover.append(entry[3:])
            if entry[0] in ("R", "C") and i < len(tokens):
                worktree_leftover.append(tokens[i])
                i += 1
    else:
        # Legacy mode defaults for merge-report fields (always empty --
        # merge detection only runs in manifest mode).
        merge_discarded_sha = None
        merge_parents = []
        merge_touched_paths = set()
        merge_discarded_undeclared = []
        auto_include_skipped = []
        auto_include_included = None

        # Capture untracked files before git add -A sweeps them in
        _, ls_out, _ = _run("git ls-files --others --exclude-standard")
        swept_untracked = [f for f in ls_out.split("\n") if f.strip()]

        # Reject if untracked files exist and caller didn't opt in
        if swept_untracked and not include_untracked:
            return finish_json({
                "status": "error",
                "step": "untracked_files",
                "error": (
                    "Untracked files are present in the working tree. "
                    "Pass files=[...] with repo-relative paths to declare "
                    "exactly what to stage, or pass include_untracked=True "
                    "to opt in to the previous behaviour."
                ),
                "untracked_files": swept_untracked,
                "hint": (
                    "Use files=[...] to stage specific paths declaratively, "
                    "or include_untracked=True for the old git add -A."
                ),
            }, verified)

        commit_err, committed_paths = git_prepare_commit(
            _run, branch=branch, message=message,
            author_name=author_name, author_email=author_email)
        if commit_err:
            return finish_json(commit_err, verified)
        # Legacy mode does not report worktree_leftover
        worktree_leftover = []

    # --- Secret scan (legacy mode, issue #676) ---
    # In legacy mode the commit already happened.  Scan the HEAD commit
    # files using exec_run (Container.exec_run, not the low-level
    # exec_create/exec_start/exec_inspect which are APIClient methods).
    if not manifest:
        _, diff_out, _ = exec_in_container(
            container,
            cmd=["/bin/sh", "-c",
                 f"cd {shlex.quote(working_dir)} && git diff-tree --no-commit-id -r --name-status HEAD 2>/dev/null"],
        )
        # Parse name-status: each line is "<status>\t<path>" (e.g. "A\tf",
        # "M\tf", "D\tf").  A "D" status means the commit deleted the file,
        # so it is no longer in the worktree and gitleaks cannot stat it --
        # skip it from the scan and record it as a skipped deletion.  Lines
        # without a tab (e.g. bare --name-only output) are treated as
        # present, preserving the pre-existing behaviour.
        files_skipped_deleted = []
        scan_files = []
        for raw in diff_out.splitlines():
            line = raw.strip()
            if not line:
                continue
            if "\t" in line:
                status, path = line.split("\t", 1)
            else:
                status, path = "", line
            if status == "D":
                files_skipped_deleted.append(path)
            else:
                scan_files.append(path)
        scan_result = run_secret_scan(
            container, scan_files, working_dir,
            baseline_hashes=baseline_hashes_arg,
        )  # noqa: F821
        scan_state = scan_result.get("secret_scan_state", "")
        # Fail-closed: proceed ONLY on known-safe states.
        if scan_state not in ("clean", "skipped"):
            deleted_n = len(files_skipped_deleted)
            record_boundary_crossing(
                cid, "publish",
                f"secret_scan state={scan_state}"
                f" findings={len(scan_result.get('findings', []))}"
                f" files={scan_result.get('files_scanned', [])}"
                f" deleted={deleted_n}",
                approved=False,
            )
            has_override = check_override(cid)  # noqa: F821
            if not has_override:
                return finish_json({
                    "status": "error",
                    "step": "secret_scan",
                    "secret_scan": scan_result.get("secret_scan"),
                    "secret_scan_state": scan_state,
                    "findings": scan_result.get("findings"),
                    "files_scanned": scan_result.get("files_scanned"),
                    "files_skipped_deleted": files_skipped_deleted,
                    "scan_summary": scan_result.get("scan_summary"),
                    "error": (
                        "publish blocked by secret scan. "
                        "Use `secret_scan_override` MCP tool to bypass "
                        "(requires human authorization)."
                    ),
                }, verified)

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
            push_err_payload, sha, push_transport = git_push_with_fallback(
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

            # Push succeeded — consume the override flag now (not before,
            # so an override is never lost on retry after a push failure).
            # A registry/baseline-suppressed publish counts as override use:
            # a stale flag would authorize a future publish with new
            # findings without re-authorization (#722 review).
            scan_state = scan_result.get("secret_scan_state", "")
            if should_consume_override(  # noqa: F821
                scan_state, scan_result.get("suppressed_count", 0),
            ):
                consume_override(cid)  # noqa: F821

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
        "swept_untracked": swept_untracked,
        "secret_scan": scan_result.get("secret_scan", "clean"),
        "secret_scan_state": scan_result.get("secret_scan_state", "unknown"),
        "files_scanned": scan_result.get("files_scanned", []),
        "push_transport": push_transport,
        "suppressed_count": scan_result.get("suppressed_count", 0),
        # No reassuring default: every run_secret_scan branch supplies
        # scan_summary, so a missing one means an unexpected path, and the
        # envelope must not assert cleanliness on its behalf (cf. the
        # "unknown" state above, which blocks rather than waving through).
        "scan_summary": scan_result.get("scan_summary"),
    }
    result["staged_files"] = committed_paths
    if manifest:
        result["worktree_leftover"] = worktree_leftover
    result["files_skipped_deleted"] = files_skipped_deleted
    if upstream_overwritten:
        # The guard found upstream changes on these declared paths and
        # allow_upstream_overwrite waved them through: the push carries the
        # container's copy over the base branch's (#863).
        result["upstream_overwrite_override"] = True
        result["upstream_overwrite_paths"] = upstream_overwritten
    if upstream_guard_undetermined:
        # The guard did not perform the comparison it exists to perform.
        # The reason travels with the flag: "undetermined" with no cause
        # is only marginally better than silence (#866).
        result["upstream_guard_undetermined"] = True
        result["upstream_guard_undetermined_reason"] = (
            upstream_guard_undetermined
        )
    if merge_discarded_sha:
        # Merge report fields (issue #711): present only when a merge
        # commit was detected at HEAD before git_prepare_commit reset it.
        result["merge_discarded_sha"] = merge_discarded_sha
        result["merge_parents"] = merge_parents
        result["auto_include_applied"] = (
            list(auto_include_included.keys())
            if auto_include_included is not None
            else []
        )
        result["auto_include_skipped"] = auto_include_skipped
        result["merge_discarded_undeclared"] = merge_discarded_undeclared
        # Everything git_prepare_commit recorded about the rebuild
        # (merge_rebuilt_parents, history_only_merge, merge_degenerate,
        # declared_unchanged_merge, merge_second_parent_unresolved) --
        # dropping any of these hides from the caller whether the
        # two-parent rebuild actually applied.
        result.update(merge_info)
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
