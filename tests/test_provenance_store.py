"""Unit and regression tests for sunaba.provenance_store and lifecycle provenance hooks (Issue #998)."""

from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sunaba.provenance_store import (
    ContainerProvenance,
    get_provenance_dir,
    load_container_provenance,
    prune_container_provenances,
    remove_container_provenance,
    save_container_provenance,
)
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.security import CREATED_AT_LABEL, KIND_LABEL, KIND_SANDBOX, MANAGED_LABEL
from sunaba.tools.container.clone import _provenance_capture_scope, _setup_pr_branch
from sunaba.tools.container.lifecycle import (
    _record_init_provenance,
    _sandbox_initialize,
    sandbox_stop,
)
from sunaba.tools.container.reaper import (
    _reap_idle_containers,
    _reap_orphaned_init_containers,
)

TEST_IMAGE = "python@sha256:" + "0" * 64
FULL_CID = "a1b2c3d4e5f67890" * 4
OTHER_CID = "1234567890abcdef" * 4


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env_state_dir = tmp_path / "env_state"
    env_state_dir.mkdir(parents=True)
    monkeypatch.setenv("SUNABA_ENVIRONMENT_STATE_DIR", str(env_state_dir))
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return env_state_dir


def _fake_urlopen(routes: dict[str, Any]):
    def _impl(request: Any, *args: Any, **kwargs: Any) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url not in routes:
            raise AssertionError(f"Unexpected network request: {url!r}")
        outcome = routes[url]
        if isinstance(outcome, Exception):
            raise outcome
        resp = MagicMock()
        resp.read.return_value = json.dumps(outcome).encode("utf-8")
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False
        return cm

    return _impl


def test_save_and_load_roundtrip_survives_restart() -> None:
    record = ContainerProvenance(
        target_repo_id=104201,
        target_ref="main",
        head_repo_id=900002,
        head_ref="feat/x",
        pr_number=42,
    )
    assert save_container_provenance(FULL_CID, record) is True

    loaded = load_container_provenance(FULL_CID)
    assert loaded == record
    assert load_container_provenance(FULL_CID[:12]) is None
    assert load_container_provenance("") is None
    assert load_container_provenance("../" + FULL_CID) is None


@pytest.mark.parametrize(
    "raw_content",
    [
        "",
        "not json {{{",
        "[]",
        "123",
        json.dumps({"version": 2, "container_id": FULL_CID, "target_repo_id": 1, "target_ref": "main", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": OTHER_CID, "target_repo_id": 1, "target_ref": "main", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": True, "target_ref": "main", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": 0, "target_ref": "main", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": -5, "target_ref": "main", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": "123", "target_ref": "main", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": 1, "target_ref": "", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": 1, "target_ref": "main\n", "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": 1, "target_ref": 123, "head_repo_id": None, "head_ref": None, "pr_number": None}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": 1, "target_ref": "main"}),
        json.dumps({"version": 1, "container_id": FULL_CID, "target_repo_id": None, "target_ref": None, "head_repo_id": None, "head_ref": None, "pr_number": None}),
    ],
)
def test_load_rejects_malformed_or_invalid_stored_data(raw_content: str) -> None:
    prov_dir = get_provenance_dir()
    prov_dir.mkdir(parents=True, exist_ok=True)
    (prov_dir / f"{FULL_CID}.json").write_text(raw_content, encoding="utf-8")

    assert load_container_provenance(FULL_CID) is None


def test_load_rejects_symlink(tmp_path: Path) -> None:
    real_file = tmp_path / "real.json"
    real_file.write_text(
        json.dumps(
            {
                "version": 1,
                "container_id": FULL_CID,
                "target_repo_id": 104201,
                "target_ref": "main",
                "head_repo_id": None,
                "head_ref": None,
                "pr_number": None,
            }
        ),
        encoding="utf-8",
    )
    prov_dir = get_provenance_dir()
    prov_dir.mkdir(parents=True, exist_ok=True)
    (prov_dir / f"{FULL_CID}.json").symlink_to(real_file)

    assert load_container_provenance(FULL_CID) is None


def test_persistence_failure_does_not_break_init_or_leave_partial_record() -> None:
    container = MagicMock()
    container.id = FULL_CID
    container.exec_run.return_value = (0, (b"", b""))
    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.run.return_value = container

    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        }
    }

    real_replace = os.replace

    def _failing_provenance_replace(src: Any, dst: Any) -> None:
        if "provenance" in str(dst):
            raise OSError("disk full")
        real_replace(src, dst)

    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=""),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(routes)),
        patch("sunaba.provenance_store.os.replace", side_effect=_failing_provenance_replace),
    ):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CID[:12])
    assert load_container_provenance(FULL_CID) is None
    prov_dir = get_provenance_dir()
    leftovers = list(prov_dir.iterdir()) if prov_dir.exists() else []
    assert leftovers == []


def test_authenticated_pr_path_records_trusted_metadata_and_retains_unknown_on_failure() -> None:
    pr_info = json.dumps({"headRefName": "feat/auth", "baseRefName": "trunk"}).encode("utf-8")
    container = MagicMock()
    container.id = FULL_CID
    container.exec_run.side_effect = [
        (0, (pr_info, b"")),
        (0, (b"cloned\n", b"")),
        (0, (b"checked out\n", b"")),
        (0, (b"", b"")),
    ]

    routes = {
        "https://api.github.com/repos/upstream/core/pulls/55": {
            "number": 55,
            "base": {"ref": "trunk", "repo": {"id": 500001}},
            "head": {"ref": "feat/auth", "repo": {"id": 900002}},
        }
    }

    with (
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="tok"),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(routes)),
        _provenance_capture_scope() as capture,
    ):
        _setup_pr_branch(
            container,
            FULL_CID[:12],
            "upstream/core",
            55,
            "/workspace",
            pip_extras=None,
            authenticated=True,
            install_deps=False,
        )
        _record_init_provenance(
            FULL_CID,
            clone_repo=None,
            repo="upstream/core",
            pr=55,
            branch=None,
            provenance_capture=capture,
        )

    rec = load_container_provenance(FULL_CID)
    assert rec == ContainerProvenance(
        target_repo_id=500001,
        target_ref="trunk",
        head_repo_id=900002,
        head_ref="feat/auth",
        pr_number=55,
    )

    # API failure on authenticated path must not fail setup and must not trust container gh output.
    remove_container_provenance(FULL_CID)
    container_err = MagicMock()
    container_err.id = OTHER_CID
    container_err.exec_run.side_effect = [
        (0, (pr_info, b"")),
        (0, (b"cloned\n", b"")),
        (0, (b"checked out\n", b"")),
        (0, (b"", b"")),
    ]
    err_routes = {
        "https://api.github.com/repos/upstream/core/pulls/55": urllib.error.URLError("offline")
    }
    with (
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="tok"),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(err_routes)),
        _provenance_capture_scope() as capture2,
    ):
        _setup_pr_branch(
            container_err,
            OTHER_CID[:12],
            "upstream/core",
            55,
            "/workspace",
            pip_extras=None,
            authenticated=True,
            install_deps=False,
        )
        _record_init_provenance(
            OTHER_CID,
            clone_repo=None,
            repo="upstream/core",
            pr=55,
            branch=None,
            provenance_capture=capture2,
        )

    rec2 = load_container_provenance(OTHER_CID)
    assert rec2 == ContainerProvenance(
        target_repo_id=None,
        target_ref=None,
        head_repo_id=None,
        head_ref=None,
        pr_number=55,
    )


def test_sandbox_stop_and_prune_cleanup_provenance() -> None:
    save_container_provenance(
        FULL_CID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    save_container_provenance(
        OTHER_CID,
        ContainerProvenance(target_repo_id=500001, target_ref="trunk"),
    )

    c_full = MagicMock()
    c_full.id = FULL_CID
    client = MagicMock()
    client.containers.get.return_value = c_full

    with patch("sunaba.tools.container._docker", return_value=client):
        msg = sandbox_stop(FULL_CID[:12], force=True)

    assert "stopped and removed" in msg
    assert load_container_provenance(FULL_CID) is None
    assert load_container_provenance(OTHER_CID) is not None

    # Startup pruning drops records for containers absent from Docker.
    c_alive = MagicMock()
    c_alive.id = OTHER_CID
    client.containers.list.return_value = [c_alive]
    save_container_provenance(
        FULL_CID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    assert prune_container_provenances(client) == 1
    assert load_container_provenance(FULL_CID) is None
    assert load_container_provenance(OTHER_CID) is not None

    # If Docker query fails, pruning retains existing records.
    client.containers.list.side_effect = RuntimeError("docker unavailable")
    assert prune_container_provenances(client) == 0
    assert load_container_provenance(OTHER_CID) is not None


def test_reaper_removes_provenance_for_reaped_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    save_container_provenance(
        FULL_CID,
        ContainerProvenance(target_repo_id=104201, target_ref="main"),
    )
    c_orphan = MagicMock()
    c_orphan.id = FULL_CID
    c_orphan.labels = {CREATED_AT_LABEL: "2020-01-01T00:00:00+00:00"}
    client = MagicMock()
    client.containers.list.return_value = [c_orphan]

    with patch("sunaba.tools.container.reaper._journal_container_status", return_value={}):
        reaped = _reap_orphaned_init_containers(client)
    assert reaped == [FULL_CID[:12]]
    assert load_container_provenance(FULL_CID) is None

    # Idle reaper cleanup
    save_container_provenance(
        OTHER_CID,
        ContainerProvenance(target_repo_id=500001, target_ref="trunk"),
    )
    monkeypatch.setenv("SUNABA_CONTAINER_TTL_SECONDS", "60")
    c_idle = MagicMock()
    c_idle.id = OTHER_CID
    c_idle.labels = {MANAGED_LABEL: "true", KIND_LABEL: KIND_SANDBOX}
    c_idle.name = "sunaba-idle"
    idle_client = MagicMock()
    idle_client.containers.list.return_value = [c_idle]

    with (
        patch("sunaba.tools.container._docker", return_value=idle_client),
        patch(
            "sunaba.tools.container.reaper.get_last_activity_per_container",
            return_value={OTHER_CID[:12]: "2020-01-01T00:00:00+00:00"},
        ),
    ):
        reaped_idle = _reap_idle_containers()
    assert reaped_idle == [OTHER_CID[:12]]
    assert load_container_provenance(OTHER_CID) is None


def test_proxied_default_clone_reuses_token_and_makes_single_api_call() -> None:
    container = MagicMock()
    container.id = FULL_CID
    container.exec_run.return_value = (0, (b"", b""))
    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.run.return_value = container

    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        }
    }

    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch("sunaba.tools.container.lifecycle.proxy_lifecycle.egress_proxy_enabled", return_value=True),
        patch("sunaba.tools.container.lifecycle.proxy_lifecycle.ensure_egress_proxy"),
        patch("sunaba.tools.container.lifecycle.proxy_lifecycle.sandbox_proxy_env", return_value={}),
        patch("sunaba.tools.container.lifecycle.proxy_lifecycle.apply_network", side_effect=lambda kw, rt: kw),
        patch("sunaba.tools.container.lifecycle.proxy_lifecycle.install_ca"),
        patch("sunaba.tools.container.clone.authorized_read_grant"),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="ghs_once") as mock_tok,
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(routes)) as mock_url,
    ):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CID[:12])
    assert mock_tok.call_count == 1
    assert mock_url.call_count == 1
    assert mock_url.call_args.args[0].get_header("Authorization") == "Bearer ghs_once"
    assert load_container_provenance(FULL_CID) == ContainerProvenance(
        target_repo_id=104201,
        target_ref="trunk",
    )


def test_pr_overrides_clone_repo_and_missing_repo_with_pr_records_nothing() -> None:
    container = MagicMock()
    container.id = FULL_CID
    container.exec_run.return_value = (0, (b"", b""))
    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.run.return_value = container

    routes = {
        "https://api.github.com/repos/upstream/core/pulls/77": {
            "number": 77,
            "base": {"ref": "trunk", "repo": {"id": 500001}},
            "head": {"ref": "feat/pr", "repo": {"id": 900002}},
        }
    }

    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=""),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(routes)),
    ):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="other/ignored",
            repo="upstream/core",
            pr=77,
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CID[:12])
    assert load_container_provenance(FULL_CID) == ContainerProvenance(
        target_repo_id=500001,
        target_ref="trunk",
        head_repo_id=900002,
        head_ref="feat/pr",
        pr_number=77,
    )

    container2 = MagicMock()
    container2.id = OTHER_CID
    container2.exec_run.return_value = (0, (b"", b""))
    client.containers.run.return_value = container2
    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=""),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen({})),
    ):
        res2 = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pr=77,
            pip_extras=None,
        )

    assert "pr setup failed" in res2.text
    assert load_container_provenance(OTHER_CID) is None


def test_authenticated_pr_mismatch_retains_unknown_fields() -> None:
    pr_info = json.dumps({"headRefName": "feat/local", "baseRefName": "trunk"}).encode("utf-8")
    container = MagicMock()
    container.id = FULL_CID
    container.exec_run.side_effect = [
        (0, (pr_info, b"")),
        (0, (b"cloned\n", b"")),
        (0, (b"checked out\n", b"")),
        (0, (b"", b"")),
    ]
    routes = {
        "https://api.github.com/repos/upstream/core/pulls/55": {
            "number": 55,
            "base": {"ref": "trunk", "repo": {"id": 500001}},
            "head": {"ref": "feat/different", "repo": {"id": 900002}},
        }
    }

    with (
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="tok"),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(routes)),
        _provenance_capture_scope() as capture,
    ):
        _setup_pr_branch(
            container,
            FULL_CID[:12],
            "upstream/core",
            55,
            "/workspace",
            pip_extras=None,
            authenticated=True,
            install_deps=False,
        )
        _record_init_provenance(
            FULL_CID,
            clone_repo=None,
            repo="upstream/core",
            pr=55,
            branch=None,
            provenance_capture=capture,
        )

    assert load_container_provenance(FULL_CID) == ContainerProvenance(
        target_repo_id=None,
        target_ref=None,
        head_repo_id=None,
        head_ref=None,
        pr_number=55,
    )


def test_failed_carry_does_not_record_provenance() -> None:
    new_container = MagicMock()
    new_container.id = FULL_CID
    new_container.exec_run.return_value = (0, (b"", b""))
    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.get.side_effect = RuntimeError("missing source")
    client.containers.run.return_value = new_container

    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        }
    }

    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=""),
        patch("urllib.request.urlopen", side_effect=_fake_urlopen(routes)),
    ):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            apply_from_container=OTHER_CID[:12],
            pip_extras=None,
        )

    assert "Error:" in result.text
    assert load_container_provenance(FULL_CID) is None
