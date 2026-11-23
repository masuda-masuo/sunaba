"""Regression and acceptance tests for carry repository provenance (Issue #999 / #975).

Verifies that ``sandbox_initialize(apply_from_container=...)`` enforces host-side
repository provenance compatibility before diff extraction or patch transfer (and
before destination allocation wherever trusted host metadata is available):
- Comparison keys: ``target_repo_id``, ``target_ref``, and effective checkout
  repository ID (``head_repo_id`` for PR checkouts, ``target_repo_id`` for
  ordinary/explicit-branch clones).
- Fail-closed structured JSON errors with ``step="carry_provenance"`` and fixed
  ``reason`` values: ``source_provenance_unknown``,
  ``destination_provenance_unknown``, ``repo_mismatch``, and
  ``target_ref_mismatch``.
- No origin/config fallback for absent or incomplete source provenance.
- Source and existing destinations remain untouched on rejection, with no fetch,
  allocation, patch extraction, patch transfer, or destination provenance record.
- Valid compatible carries proceed and persist destination provenance only after
  successful carry.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

from sunaba.provenance_store import (
    ContainerProvenance,
    load_container_provenance,
    save_container_provenance,
)
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.lifecycle import sandbox_initialize
from tests.test_publish_ops_gitrepo import _git

TEST_IMAGE = "python@sha256:" + "0" * 64
OLD_FULL_ID = "a1b2c3d4e5f67890" * 4
NEW_FULL_ID = "1234567890abcdef" * 4
EXISTING_FULL_ID = "fedcba9876543210" * 4


@pytest.fixture(autouse=True)
def _isolated_carry_provenance_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Isolate host-side state directories and disable proxy/tokens per test."""
    state_root = tmp_path / "provenance_state"
    env_state_dir = state_root / "env_state"
    xdg_state_dir = state_root / "xdg_state"
    env_state_dir.mkdir(parents=True)
    xdg_state_dir.mkdir(parents=True)

    monkeypatch.setenv("SUNABA_ENVIRONMENT_STATE_DIR", str(env_state_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state_dir))
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return state_root


def _cmd_text(cmd: Any) -> str:
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


def _make_mock_container(
    full_container_id: str,
    *,
    working_dir: str = "/workspace",
    patch_bytes: bytes = b"diff --git a/src/app.py b/src/app.py\n+carried = True\n",
    changed: bytes = b"src/app.py\n",
    checked_out_branch: str = "main",
    origin_url: str = "https://github.com/acme/widget.git",
    meta_json: bytes | None = None,
    apply_fatal_error: bool = False,
    on_apply_hook: Any = None,
) -> MagicMock:
    """Create a mocked Docker container tracking executed commands."""
    container = MagicMock()
    container.id = full_container_id
    container.status = "running"
    container.labels = {}
    container.attrs = {"Config": {"WorkingDir": working_dir}}

    def _exec_run(cmd: Any, **kwargs: Any) -> tuple[int, Any]:
        cmd_str = _cmd_text(cmd)

        def _reply(ec: int, out: bytes, err: bytes = b"") -> tuple[int, Any]:
            if kwargs.get("demux"):
                return ec, (out or None, err or None)
            return ec, out + err

        if ".sandbox-meta.json" in cmd_str and meta_json is not None and "printf" not in cmd_str:
            return _reply(0, meta_json)
        if "git remote get-url origin" in cmd_str:
            return _reply(0, f"{origin_url}\n".encode("utf-8"))
        if "fetch origin" in cmd_str:
            return _reply(0, b"")
        if "git merge-base HEAD origin/HEAD" in cmd_str:
            return _reply(0, b"abc1234\n")
        if "git rev-list --count" in cmd_str:
            return _reply(0, b"0\n")
        if "git diff --binary" in cmd_str:
            return _reply(0, patch_bytes)
        if "git ls-files --others --exclude-standard" in cmd_str:
            return _reply(0, b"")
        if "git apply" in cmd_str:
            if on_apply_hook is not None:
                on_apply_hook()
            if apply_fatal_error:
                return _reply(1, b"", b"error: patch failed: src/app.py:1\n")
            return _reply(0, b"")
        if "git diff --name-only --diff-filter=U" in cmd_str:
            return _reply(0, b"")
        if "git diff --name-only" in cmd_str:
            return _reply(0, changed)
        if "git reset -q" in cmd_str:
            return _reply(0, b"")
        if "git rev-parse --abbrev-ref HEAD" in cmd_str:
            return _reply(0, f"{checked_out_branch}\n".encode("utf-8"))
        if "for f in pyproject.toml" in cmd_str:
            return _reply(0, b"")
        if "id -u" in cmd_str or "id -g" in cmd_str:
            return _reply(0, b"1000\n")
        return _reply(0, b"")

    container.exec_run.side_effect = _exec_run
    return container


def _make_docker_client(
    old_container: MagicMock,
    new_container: MagicMock,
    *,
    extra_containers: dict[str, MagicMock] | None = None,
) -> MagicMock:
    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.run.return_value = new_container

    lookup: dict[str, MagicMock] = {
        str(old_container.id): old_container,
        str(old_container.id)[:12]: old_container,
        str(new_container.id): new_container,
        str(new_container.id)[:12]: new_container,
    }
    if extra_containers:
        for cid, cont in extra_containers.items():
            lookup[cid] = cont
            lookup[cid[:12]] = cont

    def _get(cid: str) -> MagicMock:
        if cid in lookup:
            return lookup[cid]
        raise NotFound(f"container {cid!r} not found")

    client.containers.get.side_effect = _get
    return client


def _make_fake_urlopen(routes: dict[str, Any]):
    def _fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url not in routes:
            raise AssertionError(f"Unexpected GitHub API request in test: {url!r}")
        outcome = routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome(request)

        resp = MagicMock()
        resp.read.return_value = json.dumps(outcome).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    return _fake_urlopen


@contextmanager
def _mock_carry_environment(
    client: MagicMock,
    routes: dict[str, Any],
) -> Iterator[None]:
    """Patch Docker, image checks, token resolution, and GitHub API urlopen."""
    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.common._docker", return_value=client),
        patch("sunaba.tools.file._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch(
            "sunaba.tools.container.lifecycle.build_secure_run_kwargs",
            return_value={},
        ),
        patch("sunaba.tools.container.lifecycle._resolve_image_ref", lambda x: x),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=""),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=""),
        patch("sunaba.token_broker.mint_token", return_value=""),
        patch("sunaba.github_auth.get_global_provider", return_value=None),
        patch("urllib.request.urlopen", side_effect=_make_fake_urlopen(routes)),
    ):
        yield


def _executed_commands(container: MagicMock) -> list[str]:
    return [_cmd_text(call.args[0]) for call in container.exec_run.call_args_list if call.args]


def _parse_carry_provenance_error(result: str) -> dict[str, Any]:
    raw = result.strip()
    if raw.startswith("Error:"):
        raw = raw[len("Error:") :].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"Expected structured JSON error from sandbox_initialize, got: {result!r} ({exc})"
        )
    assert isinstance(data, dict), f"Expected JSON object, got: {data!r}"
    return data


def _assert_carry_provenance_refusal(
    result: str,
    *,
    expected_reason: str,
    client: MagicMock,
    old_container: MagicMock,
    new_container: MagicMock,
    new_full_id: str = NEW_FULL_ID,
) -> dict[str, Any]:
    data = _parse_carry_provenance_error(result)
    assert data.get("status") == "error", f"Unexpected status in {data!r}"
    assert data.get("step") == "carry_provenance", (
        f"Expected step='carry_provenance', got {data!r}"
    )
    assert data.get("reason") == expected_reason, (
        f"Expected reason={expected_reason!r}, got {data!r}"
    )
    assert isinstance(data.get("error"), str) and data["error"].strip(), (
        f"Expected non-empty error field in {data!r}"
    )

    # Preflight refusal: no destination allocation, no source fetch, no diff, no transfer.
    client.containers.run.assert_not_called()
    old_cmds = _executed_commands(old_container)
    new_cmds = _executed_commands(new_container)
    assert not any("fetch origin" in c for c in old_cmds), (
        f"Source fetch should not run on provenance refusal: {old_cmds}"
    )
    assert not any("git diff" in c for c in old_cmds), (
        f"Patch extraction should not run on provenance refusal: {old_cmds}"
    )
    assert not any("git apply" in c or "sunaba-carry.patch" in c for c in new_cmds), (
        f"Patch transfer/apply should not run on provenance refusal: {new_cmds}"
    )
    new_container.put_archive.assert_not_called()
    assert load_container_provenance(new_full_id) is None
    return data


def _init_git_repo(repo_dir: Path) -> tuple[str, str]:
    """Initialize a real git repository with uncommitted tracked + untracked edits."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(str(repo_dir), "init", "--initial-branch=main")
    _git(str(repo_dir), "config", "user.email", "tester@example.com")
    _git(str(repo_dir), "config", "user.name", "Tester")
    _git(
        str(repo_dir),
        "remote",
        "add",
        "origin",
        "https://github.com/acme/widget.git",
    )
    (repo_dir / "app.py").write_text("base = 1\n", encoding="utf-8")
    _git(str(repo_dir), "add", "app.py")
    _git(str(repo_dir), "commit", "-m", "initial commit")
    head_sha = _git(str(repo_dir), "rev-parse", "HEAD").stdout.strip()

    # Uncommitted tracked change + untracked file that must stay untouched.
    (repo_dir / "app.py").write_text("base = 2\n", encoding="utf-8")
    (repo_dir / "notes.txt").write_text("keep untracked\n", encoding="utf-8")
    status = _git(str(repo_dir), "status", "--porcelain").stdout
    return head_sha, status


def test_legacy_source_without_host_record_rejects_without_origin_or_meta_fallback(
    tmp_path: Path,
) -> None:
    """Absent host provenance rejects even when in-container origin and .sandbox-meta.json match."""
    src_dir = tmp_path / "legacy_source"
    head_sha, initial_status = _init_git_repo(src_dir)

    meta_payload = json.dumps(
        {"clone_path": str(src_dir), "base_branch": "trunk"}
    ).encode("utf-8")
    old = _make_mock_container(
        OLD_FULL_ID,
        working_dir=str(src_dir),
        origin_url="https://github.com/acme/widget.git",
        meta_json=meta_payload,
    )
    new = _make_mock_container(NEW_FULL_ID)

    # Pre-existing unrelated container + host provenance must stay untouched.
    existing = _make_mock_container(EXISTING_FULL_ID)
    existing_prov = ContainerProvenance(target_repo_id=777777, target_ref="main")
    assert save_container_provenance(EXISTING_FULL_ID, existing_prov) is True

    client = _make_docker_client(
        old, new, extra_containers={EXISTING_FULL_ID: existing}
    )
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="source_provenance_unknown",
        client=client,
        old_container=old,
        new_container=new,
    )
    assert _git(str(src_dir), "rev-parse", "HEAD").stdout.strip() == head_sha
    assert _git(str(src_dir), "status", "--porcelain").stdout == initial_status
    assert (src_dir / "app.py").read_text(encoding="utf-8") == "base = 2\n"
    assert (src_dir / "notes.txt").read_text(encoding="utf-8") == "keep untracked\n"
    assert load_container_provenance(EXISTING_FULL_ID) == existing_prov
    existing.exec_run.assert_not_called()


@pytest.mark.parametrize(
    "source_provenance",
    [
        pytest.param(
            ContainerProvenance(
                target_repo_id=None,
                target_ref="trunk",
                head_ref="feat/x",
            ),
            id="missing_target_repo_id",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=104201,
                target_ref=None,
                head_ref="feat/x",
            ),
            id="missing_target_ref",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
                head_repo_id=None,
                head_ref="deleted-fork",
                pr_number=99,
            ),
            id="pr_missing_head_repo_id_with_known_base",
        ),
    ],
)
def test_source_with_incomplete_provenance_rejects(
    source_provenance: ContainerProvenance,
) -> None:
    """Incomplete source host provenance (including PR missing head_repo_id) fails closed."""
    assert save_container_provenance(OLD_FULL_ID, source_provenance) is True
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID)
    client = _make_docker_client(old, new)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="source_provenance_unknown",
        client=client,
        old_container=old,
        new_container=new,
    )


def test_source_with_non_full_docker_id_rejects_as_unknown() -> None:
    """Source container lacking a 64-char hex Docker ID cannot resolve a trusted host record."""
    short_id = "deadbeef1234"
    old = _make_mock_container(short_id)
    new = _make_mock_container(NEW_FULL_ID)
    client = _make_docker_client(old, new)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=short_id,
            pip_extras=None,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="source_provenance_unknown",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    ("init_kwargs", "api_url", "api_error"),
    [
        pytest.param(
            {"clone_repo": "acme/widget"},
            "https://api.github.com/repos/acme/widget",
            urllib.error.HTTPError(
                "https://api.github.com/repos/acme/widget",
                503,
                "Service Unavailable",
                None,  # type: ignore[arg-type]
                None,
            ),
            id="clone_http_503",
        ),
        pytest.param(
            {"clone_repo": "acme/widget", "branch": "feat/x"},
            "https://api.github.com/repos/acme/widget",
            urllib.error.URLError("network unreachable"),
            id="branch_url_error",
        ),
        pytest.param(
            {"repo": "acme/widget", "pr": 42},
            "https://api.github.com/repos/acme/widget/pulls/42",
            urllib.error.URLError("network unreachable"),
            id="pr_url_error",
        ),
    ],
)
def test_destination_github_api_unavailable_rejects(
    init_kwargs: dict[str, Any],
    api_url: str,
    api_error: Exception,
) -> None:
    """Unavailable GitHub metadata for destination fails closed with no origin fallback."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch=str(init_kwargs.get("branch", "main")),
    )
    client = _make_docker_client(old, new)

    with _mock_carry_environment(client, {api_url: api_error}):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
            **init_kwargs,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="destination_provenance_unknown",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    "repo_payload",
    [
        {"id": 104201},
        {"default_branch": "trunk"},
        {"id": 0, "default_branch": "trunk"},
        {"id": -5, "default_branch": "trunk"},
        {"id": True, "default_branch": "trunk"},
        {"id": None, "default_branch": "trunk"},
    ],
)
def test_destination_incomplete_repo_metadata_rejects(
    repo_payload: dict[str, Any],
) -> None:
    """Destination repo payload missing id or default_branch rejects as destination_provenance_unknown."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID)
    client = _make_docker_client(old, new)
    routes = {"https://api.github.com/repos/acme/widget": repo_payload}

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="destination_provenance_unknown",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    "head_repo_value",
    [None, {}, {"id": 0}],
)
def test_destination_pr_missing_head_repo_id_rejects_even_with_known_base(
    head_repo_value: Any,
) -> None:
    """A destination PR missing head_repo_id is unknown even when base repo/ref match."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID)
    client = _make_docker_client(old, new)
    routes = {
        "https://api.github.com/repos/acme/widget/pulls/99": {
            "number": 99,
            "base": {"ref": "trunk", "repo": {"id": 104201}},
            "head": {"ref": "deleted-fork-branch", "repo": head_repo_value},
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            repo="acme/widget",
            pr=99,
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="destination_provenance_unknown",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    ("init_kwargs", "routes"),
    [
        pytest.param(
            {"clone_repo": "other/widget"},
            {
                "https://api.github.com/repos/other/widget": {
                    "id": 209999,
                    "default_branch": "trunk",
                },
            },
            id="distinct_clone_repo_id",
        ),
        pytest.param(
            {"clone_repo": "other/widget", "branch": "feat/x"},
            {
                "https://api.github.com/repos/other/widget": {
                    "id": 209999,
                    "default_branch": "trunk",
                },
            },
            id="distinct_branch_repo_id",
        ),
        pytest.param(
            {"repo": "other/widget", "pr": 12},
            {
                "https://api.github.com/repos/other/widget/pulls/12": {
                    "number": 12,
                    "base": {"ref": "trunk", "repo": {"id": 209999}},
                    "head": {"ref": "feat/x", "repo": {"id": 209999}},
                },
            },
            id="distinct_pr_base_repo_id",
        ),
    ],
)
def test_distinct_target_repo_ids_reject_with_repo_mismatch(
    init_kwargs: dict[str, Any],
    routes: dict[str, Any],
) -> None:
    """Distinct numeric target_repo_id rejects with repo_mismatch even when ref names match."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch=str(init_kwargs.get("branch", "main")),
    )
    client = _make_docker_client(old, new)

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
            **init_kwargs,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="repo_mismatch",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    ("source_prov", "init_kwargs", "routes"),
    [
        pytest.param(
            ContainerProvenance(
                target_repo_id=500001,
                target_ref="trunk",
                head_repo_id=900002,
                head_ref="fork-fix",
                pr_number=88,
            ),
            {"clone_repo": "upstream/core"},
            {
                "https://api.github.com/repos/upstream/core": {
                    "id": 500001,
                    "default_branch": "trunk",
                },
            },
            id="fork_pr_to_upstream_default_clone",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=500001,
                target_ref="trunk",
                head_repo_id=900002,
                head_ref="fork-fix",
                pr_number=88,
            ),
            {"clone_repo": "upstream/core", "branch": "feat/upstream"},
            {
                "https://api.github.com/repos/upstream/core": {
                    "id": 500001,
                    "default_branch": "trunk",
                },
            },
            id="fork_pr_to_upstream_explicit_branch",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=500001,
                target_ref="trunk",
            ),
            {"repo": "upstream/core", "pr": 88},
            {
                "https://api.github.com/repos/upstream/core/pulls/88": {
                    "number": 88,
                    "base": {"ref": "trunk", "repo": {"id": 500001}},
                    "head": {"ref": "fork-fix", "repo": {"id": 900002}},
                },
            },
            id="upstream_default_clone_to_fork_pr",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=500001,
                target_ref="trunk",
                head_repo_id=900002,
                head_ref="fork-a-fix",
                pr_number=88,
            ),
            {"repo": "upstream/core", "pr": 89},
            {
                "https://api.github.com/repos/upstream/core/pulls/89": {
                    "number": 89,
                    "base": {"ref": "trunk", "repo": {"id": 500001}},
                    "head": {"ref": "fork-b-fix", "repo": {"id": 900003}},
                },
            },
            id="fork_a_pr_to_fork_b_pr",
        ),
    ],
)
def test_fork_vs_upstream_with_identical_target_repo_and_ref_rejects(
    source_prov: ContainerProvenance,
    init_kwargs: dict[str, Any],
    routes: dict[str, Any],
) -> None:
    """Distinct effective checkout repo IDs reject with repo_mismatch even when target repo/ref match."""
    assert save_container_provenance(OLD_FULL_ID, source_prov) is True
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch=str(init_kwargs.get("branch", "main")),
    )
    client = _make_docker_client(old, new)

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
            **init_kwargs,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="repo_mismatch",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    ("source_prov", "init_kwargs", "routes"),
    [
        pytest.param(
            ContainerProvenance(target_repo_id=104201, target_ref="main"),
            {"repo": "acme/widget", "pr": 55},
            {
                "https://api.github.com/repos/acme/widget/pulls/55": {
                    "number": 55,
                    "base": {"ref": "release-1.x", "repo": {"id": 104201}},
                    "head": {"ref": "feat/x", "repo": {"id": 104201}},
                },
            },
            id="default_clone_main_to_pr_release_branch",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="release-1.x",
                head_repo_id=104201,
                head_ref="feat/x",
                pr_number=55,
            ),
            {"clone_repo": "acme/widget"},
            {
                "https://api.github.com/repos/acme/widget": {
                    "id": 104201,
                    "default_branch": "main",
                },
            },
            id="pr_release_branch_to_default_clone_main",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="main",
                head_ref="feat/work",
            ),
            {"clone_repo": "acme/widget", "branch": "feat/next"},
            {
                "https://api.github.com/repos/acme/widget": {
                    "id": 104201,
                    "default_branch": "trunk",
                },
            },
            id="explicit_branch_default_branch_changed_on_github",
        ),
    ],
)
def test_distinct_target_refs_same_repo_reject_with_target_ref_mismatch(
    source_prov: ContainerProvenance,
    init_kwargs: dict[str, Any],
    routes: dict[str, Any],
) -> None:
    """Matching repo IDs with distinct target_ref reject with target_ref_mismatch."""
    assert save_container_provenance(OLD_FULL_ID, source_prov) is True
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch=str(init_kwargs.get("branch", "main")),
    )
    client = _make_docker_client(old, new)

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
            **init_kwargs,
        )

    _assert_carry_provenance_refusal(
        result,
        expected_reason="target_ref_mismatch",
        client=client,
        old_container=old,
        new_container=new,
    )


@pytest.mark.parametrize(
    ("source_prov", "init_kwargs", "routes", "checked_out_branch", "expected_dest"),
    [
        pytest.param(
            ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
            {"clone_repo": "acme/widget"},
            {
                "https://api.github.com/repos/acme/widget": {
                    "id": 104201,
                    "default_branch": "trunk",
                },
            },
            "trunk",
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
            ),
            id="default_to_default",
        ),
        pytest.param(
            ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
            {"clone_repo": "acme/widget", "branch": "feat/next"},
            {
                "https://api.github.com/repos/acme/widget": {
                    "id": 104201,
                    "default_branch": "trunk",
                },
            },
            "feat/next",
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
                head_ref="feat/next",
            ),
            id="default_to_explicit_branch_same_target",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
                head_ref="feat/old",
            ),
            {"clone_repo": "acme/widget"},
            {
                "https://api.github.com/repos/acme/widget": {
                    "id": 104201,
                    "default_branch": "trunk",
                },
            },
            "trunk",
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
            ),
            id="explicit_branch_to_default_same_target",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
                head_repo_id=104201,
                head_ref="feat/pr-old",
                pr_number=77,
            ),
            {"clone_repo": "acme/widget", "branch": "feat/pr-followup"},
            {
                "https://api.github.com/repos/acme/widget": {
                    "id": 104201,
                    "default_branch": "trunk",
                },
            },
            "feat/pr-followup",
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
                head_ref="feat/pr-followup",
            ),
            id="same_repo_pr_to_explicit_branch",
        ),
        pytest.param(
            ContainerProvenance(
                target_repo_id=500001,
                target_ref="trunk",
                head_repo_id=900002,
                head_ref="fork-a",
                pr_number=88,
            ),
            {"repo": "upstream/core", "pr": 89},
            {
                "https://api.github.com/repos/upstream/core/pulls/89": {
                    "number": 89,
                    "base": {"ref": "trunk", "repo": {"id": 500001}},
                    "head": {"ref": "fork-b", "repo": {"id": 900002}},
                },
            },
            "fork-b",
            ContainerProvenance(
                target_repo_id=500001,
                target_ref="trunk",
                head_repo_id=900002,
                head_ref="fork-b",
                pr_number=89,
            ),
            id="same_fork_pr_to_same_fork_pr",
        ),
        pytest.param(
            ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
            {"clone_repo": "renamed-org/renamed-widget"},
            {
                "https://api.github.com/repos/renamed-org/renamed-widget": {
                    "id": 104201,
                    "default_branch": "trunk",
                },
            },
            "trunk",
            ContainerProvenance(
                target_repo_id=104201,
                target_ref="trunk",
            ),
            id="renamed_repo_same_numeric_id",
        ),
    ],
)
def test_valid_compatible_carry_proceeds_and_persists_after_carry(
    source_prov: ContainerProvenance,
    init_kwargs: dict[str, Any],
    routes: dict[str, Any],
    checked_out_branch: str,
    expected_dest: ContainerProvenance,
) -> None:
    """Compatible provenance allows carry and persists destination provenance only after carry."""
    assert save_container_provenance(OLD_FULL_ID, source_prov) is True

    provenance_during_apply: list[ContainerProvenance | None] = []

    def _capture_provenance_at_apply() -> None:
        provenance_during_apply.append(load_container_provenance(NEW_FULL_ID))

    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch=checked_out_branch,
        on_apply_hook=_capture_provenance_at_apply,
    )
    client = _make_docker_client(old, new)

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
            **init_kwargs,
        )

    assert result.startswith(NEW_FULL_ID[:12]), result
    assert "carried 1 file(s)" in result, result
    assert provenance_during_apply == [None], (
        "Destination provenance must not be persisted before carry completes"
    )
    assert load_container_provenance(NEW_FULL_ID) == expected_dest


def test_failed_carry_after_checkout_leaves_no_destination_provenance_record() -> None:
    """When provenance matches at preflight but carry apply fails, no destination provenance is saved."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="trunk"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch="trunk",
        apply_fatal_error=True,
    )
    client = _make_docker_client(old, new)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    assert result.startswith("Error:"), result
    assert new.remove.called
    assert load_container_provenance(NEW_FULL_ID) is None


def _assert_post_checkout_provenance_refusal(
    result: str,
    *,
    expected_reason: str,
    old_container: MagicMock,
    new_container: MagicMock,
    new_full_id: str = NEW_FULL_ID,
) -> dict[str, Any]:
    data = _parse_carry_provenance_error(result)
    assert data.get("status") == "error", f"Unexpected status in {data!r}"
    assert data.get("step") == "carry_provenance", (
        f"Expected step='carry_provenance', got {data!r}"
    )
    assert data.get("reason") == expected_reason, (
        f"Expected reason={expected_reason!r}, got {data!r}"
    )
    assert isinstance(data.get("error"), str) and data["error"].strip()

    old_cmds = _executed_commands(old_container)
    new_cmds = _executed_commands(new_container)
    assert not any("fetch origin" in c for c in old_cmds), (
        f"Source fetch must not run on post-checkout refusal: {old_cmds}"
    )
    assert not any("git diff" in c for c in old_cmds), (
        f"Patch extraction must not run on post-checkout refusal: {old_cmds}"
    )
    assert not any("git apply" in c or "sunaba-carry.patch" in c for c in new_cmds), (
        f"Patch copy/apply must not run on post-checkout refusal: {new_cmds}"
    )
    new_container.put_archive.assert_not_called()
    assert new_container.remove.called, "Fresh destination must be cleaned up"
    assert load_container_provenance(new_full_id) is None
    return data


def test_default_clone_branch_moved_from_main_to_develop_rejects_without_source_fetch() -> None:
    """If default_branch moves from main to develop after preflight, fail closed before source fetch."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID, checked_out_branch="main")
    client = _make_docker_client(old, new)

    responses = iter(
        [
            {"id": 104201, "default_branch": "main"},
            {"id": 104201, "default_branch": "develop"},
        ]
    )
    routes = {
        "https://api.github.com/repos/acme/widget": lambda _req: next(responses),
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_post_checkout_provenance_refusal(
        result,
        expected_reason="target_ref_mismatch",
        old_container=old,
        new_container=new,
    )
    assert old.exec_run.call_count == 0


def test_default_clone_actual_checkout_mismatch_independently_of_api_rejects() -> None:
    """When API matches preflight but destination checked out develop instead of main, reject."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID, checked_out_branch="develop")
    client = _make_docker_client(old, new)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "main",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_post_checkout_provenance_refusal(
        result,
        expected_reason="target_ref_mismatch",
        old_container=old,
        new_container=new,
    )
    assert old.exec_run.call_count == 0


@pytest.mark.parametrize(
    ("rev_parse_exit", "rev_parse_output"),
    [
        pytest.param(0, b"HEAD\n", id="detached_head"),
        pytest.param(128, b"fatal: not a git repository\n", id="git_failure"),
        pytest.param(0, b"\n", id="empty_branch_output"),
    ],
)
def test_default_clone_detached_head_or_git_failure_rejects_as_unknown(
    rev_parse_exit: int,
    rev_parse_output: bytes,
) -> None:
    """Detached HEAD or git branch inspection failure fails closed with destination_provenance_unknown."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID, checked_out_branch="main")
    base_exec = new.exec_run.side_effect

    def _exec_with_head_override(cmd: Any, **kwargs: Any) -> tuple[int, Any]:
        if "git rev-parse --abbrev-ref HEAD" in _cmd_text(cmd):
            if kwargs.get("demux"):
                return rev_parse_exit, (rev_parse_output, b"")
            return rev_parse_exit, rev_parse_output
        return base_exec(cmd, **kwargs)

    new.exec_run.side_effect = _exec_with_head_override
    client = _make_docker_client(old, new)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "main",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_post_checkout_provenance_refusal(
        result,
        expected_reason="destination_provenance_unknown",
        old_container=old,
        new_container=new,
    )
    assert old.exec_run.call_count == 0


def test_default_clone_real_git_detached_head_rejects_as_unknown(
    tmp_path: Path,
) -> None:
    """Real git detached HEAD on destination fails closed without source fetch or diff."""
    from tests.test_publish_ops_gitrepo import _RealGitContainer

    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old = _make_mock_container(OLD_FULL_ID)

    dest_dir = tmp_path / "dest_detached"
    _init_git_repo(dest_dir)
    _git(str(dest_dir), "checkout", "--detach", "HEAD")

    new_real = _RealGitContainer(str(dest_dir))
    new_real.id = NEW_FULL_ID
    new_real.status = "running"
    new_real.labels = {}
    new_real.attrs = {"Config": {"WorkingDir": str(dest_dir)}}
    new_real.remove = MagicMock()
    new_real.put_archive = MagicMock()

    orig_exec = new_real.exec_run

    def _dest_exec(cmd: Any, **kwargs: Any) -> tuple[int, Any]:
        if "git clone" in _cmd_text(cmd):
            return (0, (b"", b"")) if kwargs.get("demux") else (0, b"")
        return orig_exec(cmd, **kwargs)

    new_real.exec_run = MagicMock(side_effect=_dest_exec)
    client = _make_docker_client(old, new_real)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "main",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_post_checkout_provenance_refusal(
        result,
        expected_reason="destination_provenance_unknown",
        old_container=old,
        new_container=new_real,
    )
    assert old.exec_run.call_count == 0


@pytest.mark.parametrize(
    ("second_outcome", "expected_reason"),
    [
        pytest.param(
            urllib.error.URLError("network unreachable"),
            "destination_provenance_unknown",
            id="post_checkout_url_error",
        ),
        pytest.param(
            urllib.error.HTTPError(
                "https://api.github.com/repos/acme/widget",
                503,
                "Service Unavailable",
                None,  # type: ignore[arg-type]
                None,
            ),
            "destination_provenance_unknown",
            id="post_checkout_http_503",
        ),
        pytest.param(
            {"id": 104201},
            "destination_provenance_unknown",
            id="post_checkout_missing_default_branch",
        ),
        pytest.param(
            {"default_branch": "main"},
            "destination_provenance_unknown",
            id="post_checkout_missing_repo_id",
        ),
        pytest.param(
            {"id": 999999, "default_branch": "main"},
            "repo_mismatch",
            id="post_checkout_repo_id_mismatch_despite_matching_origin",
        ),
    ],
)
def test_default_clone_unknown_or_mismatched_post_checkout_metadata_rejects(
    second_outcome: Any,
    expected_reason: str,
) -> None:
    """Missing, failed, or mismatched host metadata after default clone rejects without origin fallback."""
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(
        NEW_FULL_ID,
        checked_out_branch="main",
        origin_url="https://github.com/acme/widget.git",
    )
    client = _make_docker_client(old, new)

    calls = 0

    def _route(_req: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"id": 104201, "default_branch": "main"}
        if isinstance(second_outcome, Exception):
            raise second_outcome
        return second_outcome

    routes = {"https://api.github.com/repos/acme/widget": _route}

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_post_checkout_provenance_refusal(
        result,
        expected_reason=expected_reason,
        old_container=old,
        new_container=new,
    )
    assert calls == 2
    assert old.exec_run.call_count == 0


def test_late_provenance_rejection_leaves_source_remote_tracking_refs_untouched(
    tmp_path: Path,
) -> None:
    """A late provenance rejection leaves a real-git source's remote-tracking refs completely untouched."""
    from tests.test_publish_ops_gitrepo import _RealGitContainer

    origin_dir = tmp_path / "origin.git"
    source_dir = tmp_path / "source"
    work_dir = tmp_path / "work"

    _git(str(tmp_path), "init", "--bare", "--initial-branch=main", str(origin_dir))
    _git(str(tmp_path), "clone", str(origin_dir), str(source_dir))
    _git(str(source_dir), "config", "user.email", "tester@example.com")
    _git(str(source_dir), "config", "user.name", "Tester")
    (source_dir / "app.py").write_text("v1\n", encoding="utf-8")
    _git(str(source_dir), "add", "app.py")
    _git(str(source_dir), "commit", "-m", "initial")
    _git(str(source_dir), "push", "origin", "main")
    initial_remote_sha = _git(
        str(source_dir), "rev-parse", "refs/remotes/origin/main"
    ).stdout.strip()

    # Advance upstream origin/main without fetching in source_dir.
    _git(str(tmp_path), "clone", str(origin_dir), str(work_dir))
    _git(str(work_dir), "config", "user.email", "tester@example.com")
    _git(str(work_dir), "config", "user.name", "Tester")
    (work_dir / "upstream.py").write_text("new\n", encoding="utf-8")
    _git(str(work_dir), "add", "upstream.py")
    _git(str(work_dir), "commit", "-m", "upstream advance")
    _git(str(work_dir), "push", "origin", "main")
    upstream_sha = _git(str(work_dir), "rev-parse", "HEAD").stdout.strip()
    assert upstream_sha != initial_remote_sha

    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old_real = _RealGitContainer(str(source_dir))
    old_real.id = OLD_FULL_ID
    old_real.status = "running"
    old_real.labels = {}
    old_real.attrs = {"Config": {"WorkingDir": str(source_dir)}}
    old_real.exec_run = MagicMock(side_effect=old_real.exec_run)

    new = _make_mock_container(NEW_FULL_ID, checked_out_branch="main")
    client = _make_docker_client(old_real, new)
    responses = iter(
        [
            {"id": 104201, "default_branch": "main"},
            {"id": 104201, "default_branch": "develop"},
        ]
    )
    routes = {
        "https://api.github.com/repos/acme/widget": lambda _req: next(responses),
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    _assert_post_checkout_provenance_refusal(
        result,
        expected_reason="target_ref_mismatch",
        old_container=old_real,
        new_container=new,
    )
    assert old_real.exec_run.call_count == 0
    assert (
        _git(str(source_dir), "rev-parse", "refs/remotes/origin/main").stdout.strip()
        == initial_remote_sha
    )


def test_valid_default_clone_carry_fetches_once_after_consistency_checks() -> None:
    """Valid default-clone carry checks host metadata and destination branch before fetching source once."""
    events: list[str] = []
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old = _make_mock_container(OLD_FULL_ID)
    new = _make_mock_container(NEW_FULL_ID, checked_out_branch="main")

    old_base_exec = old.exec_run.side_effect
    new_base_exec = new.exec_run.side_effect

    def _old_exec(cmd: Any, **kwargs: Any) -> tuple[int, Any]:
        c = _cmd_text(cmd)
        if "fetch origin" in c:
            events.append("source_fetch")
        elif "git merge-base" in c:
            events.append("source_merge_base")
        elif "git diff --binary" in c:
            events.append("source_diff")
        return old_base_exec(cmd, **kwargs)

    def _new_exec(cmd: Any, **kwargs: Any) -> tuple[int, Any]:
        c = _cmd_text(cmd)
        if "git clone" in c:
            events.append("clone")
        elif "git rev-parse --abbrev-ref HEAD" in c:
            events.append("branch_check")
        elif "git apply" in c:
            events.append("apply")
        return new_base_exec(cmd, **kwargs)

    old.exec_run.side_effect = _old_exec
    new.exec_run.side_effect = _new_exec
    client = _make_docker_client(old, new)

    def _spy_run(*args: Any, **kwargs: Any) -> MagicMock:
        events.append("destination_run")
        return new

    client.containers.run.side_effect = _spy_run

    api_calls = 0

    def _route(_req: Any) -> dict[str, Any]:
        nonlocal api_calls
        api_calls += 1
        events.append(f"api_{api_calls}")
        return {"id": 104201, "default_branch": "main"}

    routes = {"https://api.github.com/repos/acme/widget": _route}

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OLD_FULL_ID[:12],
            pip_extras=None,
        )

    assert result.startswith(NEW_FULL_ID[:12]), result
    assert "carried 1 file(s)" in result, result
    assert events.count("source_fetch") == 1
    assert events == [
        "api_1",
        "destination_run",
        "clone",
        "api_2",
        "branch_check",
        "source_fetch",
        "source_merge_base",
        "source_diff",
        "apply",
    ]
    assert load_container_provenance(NEW_FULL_ID) == ContainerProvenance(
        target_repo_id=104201,
        target_ref="main",
    )


def test_post_checkout_fetch_uses_immutable_validated_source_id_binding() -> None:
    """Alias rebinding after preflight still binds post-checkout fetch and diff to the validated source ID."""
    other_full_id = "beef" * 16
    assert save_container_provenance(
        OLD_FULL_ID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    old_a = _make_mock_container(OLD_FULL_ID)
    old_b = _make_mock_container(other_full_id)
    new = _make_mock_container(NEW_FULL_ID, checked_out_branch="main")

    rebound = False
    client = MagicMock()
    client.containers.list.return_value = []

    def _spy_run(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal rebound
        rebound = True
        return new

    def _get(cid: str) -> MagicMock:
        if cid == "shared-alias":
            return old_b if rebound else old_a
        if cid in (OLD_FULL_ID, OLD_FULL_ID[:12]):
            return old_a
        if cid in (other_full_id, other_full_id[:12]):
            return old_b
        return new

    client.containers.run.side_effect = _spy_run
    client.containers.get.side_effect = _get

    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "main",
        },
    }

    with _mock_carry_environment(client, routes):
        result = sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container="shared-alias",
            pip_extras=None,
        )

    assert result.startswith(NEW_FULL_ID[:12]), result
    assert old_b.exec_run.call_count == 0
    old_a_cmds = _executed_commands(old_a)
    assert sum("fetch origin" in c for c in old_a_cmds) == 1
    assert any("git diff --binary" in c for c in old_a_cmds)
