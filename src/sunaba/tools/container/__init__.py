"""Container lifecycle tools: init, stop, exec, test environment.

This package replaces the monolithic container.py (issue #649 / #646).
All symbols from the sub-modules are re-exported here so that existing
``from sunaba.tools.container import X`` imports continue to work.
"""  # noqa: D205

from __future__ import annotations  # noqa: I001

# ruff: noqa: F401
# -- image.py -----------------------------------------------------------
from .image import (
    _DEFAULT_IMAGE,
    _FULL_IMAGE,
    _GO_IMAGE,
    _JS_IMAGE,
    _NEUTRAL_IMAGE,
    _PYTHON_IMAGE,
    _ensure_image,
    _image_pins,
    _resolve_image_ref,
    _select_initial_image,
    prewarm_default_image,
)

# -- clone.py -----------------------------------------------------------
from .clone import (
    _CLONE_REPO_PATTERN,
    _clone_repo_via_network,
    _editable_install_cmd,
    _normalize_pip_extras,
    _resolve_pr_head_ref,
    _run_pip_install,
    _setup_pr_branch,
    _try_clone_into_container,
    _validate_clone_repo,
    _write_clone_meta,
    CloneResult,
)

# -- reaper.py ----------------------------------------------------------
from .reaper import (
    _CONTAINER_TTL_ENV,
    _ORPHAN_GRACE_SECONDS,
    _get_container_ttl_seconds,
    _journal_container_status,
    _reap_idle_containers,
    _reap_orphaned_init_containers,
)

# -- listing.py ---------------------------------------------------------
from .listing import (
    _age_seconds,
    _container_kind,
    _find_containers_by_name,
    _label_network,
    list_managed_containers,
    sandbox_list_containers,
)

# -- lifecycle.py -------------------------------------------------------
from .lifecycle import (
    _HARD_CAP_RATIO,
    _PROGRESS_INTERVAL_SECONDS,
    _ensure_workspace,
    run_container_and_exec,
    sandbox_attach,
    sandbox_initialize,
    sandbox_initialize_tool,
    sandbox_stop,
)

# -- Re-exports of names originally imported at module level in container.py --
# These were imported in the original container.py and tests still patch them
# at the container package level.  The sub-modules now import them individually,
# but re-exporting here keeps existing @patch("sunaba.tools.container.X")
# decorators resolving (for names that tests import-dereference from the
# container namespace rather than from the calling sub-module).

from sunaba import proxy_lifecycle  # noqa: F401

from sunaba.journal import (  # noqa: F401
    get_last_activity_per_container,
    read_container_states,
    read_journal,
    record_boundary_crossing,
    record_copy,
    record_initialize_complete,
    record_stop,
    record_tool_use,
)
from sunaba.security import (  # noqa: F401
    _detect_host_resources,
    build_secure_run_kwargs,
    validate_image_ref,
)
from sunaba.tools.common import _docker  # noqa: F401
from sunaba.tools.vcs import (  # noqa: F401
    checkpoint_list,
    resolve_git_root,
)
