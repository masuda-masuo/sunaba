"""VCS tools: issues, checkpoints, publishing, git root resolution.

This package replaces the monolithic vcs.py (issue #651).  All public
symbols from the sub-modules are re-exported here so that existing
``from sunaba.tools.vcs import X`` imports continue to work.
"""

# ruff: noqa: F401

from __future__ import annotations

# -- Re-exports from github_api.py (used by clone.py and tests) ------------
from sunaba.tools.github_api import (
    _create_pr_via_api,
    _github_api_request,
    _github_api_request_list_all,
    _resolve_vcs_token,
)

# -- checkpoints.py ---------------------------------------------------------
from sunaba.tools.vcs.checkpoints import (
    checkpoint,
    checkpoint_list,
    checkpoint_restore,
)

# -- gitroot.py -------------------------------------------------------------
from sunaba.tools.vcs.gitroot import (
    _DEFAULT_WD,
    _resolve_git_root_legacy,
    resolve_git_root,
)

# -- issues.py --------------------------------------------------------------
from sunaba.tools.vcs.issues import (
    _ISSUE_WRITE_METHODS,
    _PR_REVIEW_EVENTS,
    _REPO_FORMAT_RE,
    issue_view,
    sandbox_issue_write,
    sandbox_pr_review_write,
)

# -- publishing.py ----------------------------------------------------------
from sunaba.tools.vcs.publishing import (
    _BRANCH_RE,
    _SANDBOX_CREATE_PR_SCRIPT,
    _ensure_proxy_ready,
    _try_api_push,
    publish,
)
