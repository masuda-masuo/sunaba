"""Regression tests for host-side container provenance on clone init (Issue #998)."""

from __future__ import annotations

import importlib
import json
import urllib.error
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from docker.errors import NotFound

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.lifecycle import _sandbox_initialize

TEST_IMAGE = "python@sha256:" + "0" * 64
FULL_CONTAINER_ID = "a1b2c3d4e5f67890" * 4
OTHER_CONTAINER_ID = "1234567890abcdef" * 4
OLD_CONTAINER_ID = "fedcba9876543210" * 4


def _load_provenance(full_container_id: str) -> Any:
    """Import and invoke ``sunaba.provenance_store.load_container_provenance``."""
    try:
        mod = importlib.import_module("sunaba.provenance_store")
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"sunaba.provenance_store.load_container_provenance is not implemented: {exc}"
        )
    loader = getattr(mod, "load_container_provenance", None)
    if loader is None:
        pytest.fail("sunaba.provenance_store.load_container_provenance is missing")
    return loader(full_container_id)


def _field(record: Any, name: str) -> Any:
    """Read a provenance field from either a mapping or an attribute-bearing record."""
    if isinstance(record, Mapping):
        if name not in record:
            pytest.fail(f"Provenance record missing key {name!r}: {record!r}")
        return record[name]
    if not hasattr(record, name):
        pytest.fail(f"Provenance record missing attribute {name!r}: {record!r}")
    return getattr(record, name)


def _assert_positive_int(value: Any, *, field_name: str) -> None:
    assert isinstance(value, int) and not isinstance(value, bool) and value > 0, (
        f"{field_name} must be a positive GitHub numeric ID, got {value!r}"
    )


def _make_mock_docker_and_container(
    full_container_id: str = FULL_CONTAINER_ID,
    *,
    checked_out_branch: str = "main",
    clone_fails: bool = False,
) -> tuple[MagicMock, MagicMock]:
    """Create a mocked Docker client + container whose ``exec_run`` routes commands."""
    container = MagicMock()
    container.id = full_container_id

    def _exec_run(cmd: Any, **kwargs: Any) -> tuple[int, bytes | tuple[bytes | None, bytes | None]]:
        if isinstance(cmd, (list, tuple)):
            cmd_str = " ".join(str(part) for part in cmd)
        else:
            cmd_str = str(cmd)

        if clone_fails and ("git clone" in cmd_str or "gh repo clone" in cmd_str):
            ec, out, err = 1, b"", b"fatal: repository not found\n"
        elif "git rev-parse --abbrev-ref HEAD" in cmd_str:
            ec, out, err = 0, f"{checked_out_branch}\n".encode("utf-8"), b""
        elif "for f in pyproject.toml" in cmd_str:
            ec, out, err = 0, b"", b""
        else:
            ec, out, err = 0, b"ok\n", b""

        if kwargs.get("demux"):
            return ec, (out or None, err or None)
        return ec, out + err

    container.exec_run.side_effect = _exec_run

    client = MagicMock()
    client.containers.list.return_value = []
    client.containers.get.side_effect = NotFound("not found")
    client.containers.run.return_value = container
    return client, container


def _make_fake_urlopen(routes: dict[str, Any]):
    """Build a ``urllib.request.urlopen`` fake dispatching by exact GitHub API URL."""

    def _fake_urlopen(request: Any, *args: Any, **kwargs: Any) -> Any:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url not in routes:
            raise AssertionError(f"Unexpected network request in test: {url!r}")
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
def _mock_init_environment(
    client: MagicMock,
    routes: dict[str, Any],
) -> Iterator[None]:
    """Patch Docker, image validation, token resolution, and ``urllib.request.urlopen``."""
    with (
        patch("sunaba.tools.container._docker", return_value=client),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.lifecycle.validate_image_ref"),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=""),
        patch("sunaba.tools.github_api._resolve_vcs_token", return_value=""),
        patch("sunaba.token_broker.mint_token", return_value=""),
        patch("sunaba.github_auth.get_global_provider", return_value=None),
        patch("urllib.request.urlopen", side_effect=_make_fake_urlopen(routes)),
    ):
        yield


@pytest.fixture(autouse=True)
def _isolated_provenance_env(
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


def test_default_clone_records_trusted_host_default_branch() -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "clone_repo failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    target_repo_id = _field(record, "target_repo_id")
    _assert_positive_int(target_repo_id, field_name="target_repo_id")
    assert target_repo_id == 104201
    assert _field(record, "target_ref") == "trunk"
    assert _field(record, "pr_number") is None
    assert _field(record, "head_repo_id") in (None, 104201)
    assert _field(record, "head_ref") in (None, "trunk")


def test_explicit_branch_records_requested_branch() -> None:
    client, _ = _make_mock_docker_and_container(
        FULL_CONTAINER_ID,
        checked_out_branch="release/v2",
    )
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            branch="release/v2",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "branch setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    target_repo_id = _field(record, "target_repo_id")
    _assert_positive_int(target_repo_id, field_name="target_repo_id")
    assert target_repo_id == 104201
    assert _field(record, "target_ref") == "trunk"
    assert _field(record, "head_ref") == "release/v2"
    assert _field(record, "pr_number") is None
    assert _field(record, "head_repo_id") is None


def test_pr_checkout_records_same_repo_base_head_and_pr_number() -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/acme/widget/pulls/77": {
            "number": 77,
            "base": {"ref": "trunk", "repo": {"id": 104201}},
            "head": {"ref": "feat/add-tests", "repo": {"id": 104201}},
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            repo="acme/widget",
            pr=77,
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "pr setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") == 104201
    assert _field(record, "target_ref") == "trunk"
    assert _field(record, "head_repo_id") == 104201
    assert _field(record, "head_ref") == "feat/add-tests"
    assert _field(record, "pr_number") == 77


def test_pr_checkout_fork_records_independent_base_and_head() -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/upstream/core/pulls/88": {
            "number": 88,
            "base": {"ref": "release-1.x", "repo": {"id": 500001}},
            "head": {"ref": "fork-fix-998", "repo": {"id": 900002}},
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            repo="upstream/core",
            pr=88,
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "pr setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    target_repo_id = _field(record, "target_repo_id")
    head_repo_id = _field(record, "head_repo_id")
    _assert_positive_int(target_repo_id, field_name="target_repo_id")
    _assert_positive_int(head_repo_id, field_name="head_repo_id")
    assert target_repo_id == 500001
    assert _field(record, "target_ref") == "release-1.x"
    assert head_repo_id == 900002
    assert _field(record, "head_ref") == "fork-fix-998"
    assert _field(record, "pr_number") == 88


def test_pr_checkout_deleted_head_repo_preserves_available_fields() -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/upstream/core/pulls/99": {
            "number": 99,
            "base": {"ref": "trunk", "repo": {"id": 500001}},
            "head": {"ref": "deleted-fork-branch", "repo": None},
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            repo="upstream/core",
            pr=99,
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "pr setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") == 500001
    assert _field(record, "target_ref") == "trunk"
    assert _field(record, "head_repo_id") is None
    assert _field(record, "head_ref") == "deleted-fork-branch"
    assert _field(record, "pr_number") == 99


def test_pr_checkout_missing_base_repo_id_preserves_other_fields() -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/upstream/core/pulls/100": {
            "number": 100,
            "base": {"ref": "trunk", "repo": {}},
            "head": {"ref": "feat-branch", "repo": {"id": 900002}},
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            repo="upstream/core",
            pr=100,
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "pr setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") is None
    assert _field(record, "target_ref") == "trunk"
    assert _field(record, "head_repo_id") == 900002
    assert _field(record, "head_ref") == "feat-branch"
    assert _field(record, "pr_number") == 100


def test_old_container_with_no_host_record_reads_as_unknown_without_container_fallback() -> None:
    legacy_container = MagicMock()
    legacy_container.id = OLD_CONTAINER_ID
    legacy_container.exec_run.return_value = (
        0,
        (b'{"clone_path": "/workspace", "base_branch": "legacy-main"}\n', b""),
    )
    legacy_client = MagicMock()
    legacy_client.containers.get.return_value = legacy_container

    with patch("sunaba.tools.container._docker", return_value=legacy_client):
        record = _load_provenance(OLD_CONTAINER_ID)

    assert record is None
    legacy_container.exec_run.assert_not_called()


@pytest.mark.parametrize(
    "api_error",
    [
        urllib.error.HTTPError(
            "https://api.github.com/repos/acme/widget",
            503,
            "Service Unavailable",
            None,  # type: ignore[arg-type]
            None,
        ),
        urllib.error.URLError("network disabled"),
    ],
)
def test_ordinary_clone_succeeds_when_github_api_fails(api_error: Exception) -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {"https://api.github.com/repos/acme/widget": api_error}

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "clone_repo failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    if record is not None:
        assert _field(record, "target_repo_id") is None
        assert _field(record, "target_ref") is None
        assert _field(record, "head_repo_id") is None
        assert _field(record, "head_ref") is None
        assert _field(record, "pr_number") is None


def test_ordinary_clone_missing_default_branch_leaves_target_ref_unknown() -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "clone_repo failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") == 104201
    assert _field(record, "target_ref") is None
    assert _field(record, "pr_number") is None


@pytest.mark.parametrize(
    "repo_payload",
    [
        {"default_branch": "develop"},
        {"id": 0, "default_branch": "develop"},
        {"id": -7, "default_branch": "develop"},
        {"id": None, "default_branch": "develop"},
        {"id": True, "default_branch": "develop"},
    ],
)
def test_ordinary_clone_missing_or_invalid_repo_id_leaves_target_repo_id_unknown(
    repo_payload: dict[str, Any],
) -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/acme/widget": repo_payload,
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "clone_repo failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") is None
    assert _field(record, "target_ref") == "develop"
    assert _field(record, "pr_number") is None


def test_explicit_branch_clone_succeeds_when_github_api_fails_and_keeps_branch() -> None:
    client, _ = _make_mock_docker_and_container(
        FULL_CONTAINER_ID,
        checked_out_branch="feat/offline-meta",
    )
    routes = {
        "https://api.github.com/repos/acme/widget": urllib.error.URLError("offline"),
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            branch="feat/offline-meta",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "branch setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") is None
    assert _field(record, "target_ref") is None
    assert _field(record, "head_repo_id") is None
    assert _field(record, "head_ref") == "feat/offline-meta"
    assert _field(record, "pr_number") is None


def test_explicit_branch_missing_default_branch_leaves_target_ref_unknown_and_keeps_head_ref() -> None:
    client, _ = _make_mock_docker_and_container(
        FULL_CONTAINER_ID,
        checked_out_branch="feat/x",
    )
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            branch="feat/x",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])
    assert "branch setup failed" not in result.text

    record = _load_provenance(FULL_CONTAINER_ID)
    assert record is not None
    assert _field(record, "target_repo_id") == 104201
    assert _field(record, "target_ref") is None
    assert _field(record, "head_repo_id") is None
    assert _field(record, "head_ref") == "feat/x"
    assert _field(record, "pr_number") is None


def test_readback_comes_from_host_disk_keyed_by_full_docker_id(
    _isolated_provenance_env: Path,
) -> None:
    client, _ = _make_mock_docker_and_container(FULL_CONTAINER_ID)
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_init_environment(client, routes):
        result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert result.text.startswith(FULL_CONTAINER_ID[:12])

    persisted_files = [p for p in _isolated_provenance_env.rglob("*") if p.is_file()]
    assert persisted_files, "expected provenance to be persisted in isolated host state dir"

    with patch(
        "sunaba.tools.container._docker",
        side_effect=AssertionError("provenance readback must not consult Docker"),
    ):
        record = _load_provenance(FULL_CONTAINER_ID)
        short_prefix_record = _load_provenance(FULL_CONTAINER_ID[:12])

    assert record is not None
    assert _field(record, "target_repo_id") == 104201
    assert _field(record, "target_ref") == "trunk"
    assert short_prefix_record is None


def test_failed_clone_and_plain_init_do_not_record_provenance() -> None:
    failed_client, _ = _make_mock_docker_and_container(
        FULL_CONTAINER_ID,
        clone_fails=True,
    )
    routes = {
        "https://api.github.com/repos/acme/widget": {
            "id": 104201,
            "default_branch": "trunk",
        },
    }

    with _mock_init_environment(failed_client, routes):
        failed_result = _sandbox_initialize(
            image=TEST_IMAGE,
            clone_repo="acme/widget",
            pip_extras=None,
        )

    assert "clone_repo failed" in failed_result.text
    assert _load_provenance(FULL_CONTAINER_ID) is None

    plain_client, _ = _make_mock_docker_and_container(OTHER_CONTAINER_ID)
    with _mock_init_environment(plain_client, {}):
        plain_result = _sandbox_initialize(image=TEST_IMAGE)

    assert plain_result.text.startswith(OTHER_CONTAINER_ID[:12])
    assert _load_provenance(OTHER_CONTAINER_ID) is None
