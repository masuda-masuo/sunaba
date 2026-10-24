from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container import clone, lifecycle

_IMAGE = "python@sha256:" + "0" * 64
_CID = "issue942-network"


@pytest.fixture(autouse=True)
def _disable_egress_proxy(monkeypatch):
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")


def _lifecycle_mocks(monkeypatch):
    container = MagicMock()
    container.id = _CID
    container.exec_run.return_value = (0, (b"done\\n", b""))
    client = MagicMock()
    client.containers.run.return_value = container
    client.containers.get.side_effect = Exception("not found")

    monkeypatch.setattr("sunaba.tools.container._docker", lambda: client)
    monkeypatch.setattr(lifecycle, "_ensure_image", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "_resolve_image_ref", lambda image: image)
    monkeypatch.setattr(lifecycle, "validate_image_ref", lambda image: None)
    monkeypatch.setattr(lifecycle, "_ensure_workspace", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        lifecycle, "build_secure_run_kwargs",
        lambda profile, **kwargs: {
            "network_mode": "bridge" if profile.allow_network else "none"
        },
    )
    monkeypatch.setattr(lifecycle, "record_initialize", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "record_initialize_complete", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "record_stop", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "record_boundary_crossing", lambda *args, **kwargs: None)
    monkeypatch.setattr(lifecycle, "journal_record_exec", lambda *args, **kwargs: None)
    return client, container


@pytest.mark.parametrize("allow_network", [True, False])
def test_branch_and_pr_helpers_forward_network_to_dependency_preparation(allow_network):
    container = MagicMock()
    container.id = _CID

    with (
        patch("sunaba.tools.container.clone._build_clone_command", return_value="git clone"),
        patch("sunaba.tools.container.clone._resolve_default_branch", return_value="main"),
        patch("sunaba.tools.container.clone._install_repo_deps", return_value=SimpleNamespace(note="", failed=False)) as install,
        patch("sunaba.tools.container.clone._write_clone_meta"),
        patch("sunaba.tools.container.clone.record_copy"),
    ):
        container.exec_run.side_effect = [
            (0, (b"", b"")),  # branch clone
            (0, (b"feature\n", b"")),  # branch HEAD verification
        ]
        clone._setup_branch(
            container, _CID, "owner/repo", "feature", "/workspace",
            install_deps=True, allow_network=allow_network,
        )
        branch_call = install.call_args

        container.exec_run.side_effect = [
            (0, (b'{"headRefName":"feature","baseRefName":"main"}', b"")),  # gh pr view
            (0, (b"", b"")),  # clone
            (0, (b"", b"")),  # checkout
        ]
        clone._setup_pr_branch(
            container, _CID, "owner/repo", 7, "/workspace",
            authenticated=True, install_deps=True, allow_network=allow_network,
        )
        pr_call = install.call_args

    assert branch_call.kwargs["allow_network"] is allow_network
    assert pr_call.kwargs["allow_network"] is allow_network


@pytest.mark.parametrize("mode", ["initialize", "one_shot"])
@pytest.mark.parametrize("pathway", ["clone", "branch", "pr", "offline"])
def test_lifecycle_callers_forward_effective_network_to_each_downstream_helper(
    monkeypatch, mode, pathway
):
    client, _container = _lifecycle_mocks(monkeypatch)
    setup_branch = MagicMock(return_value="branch ready")
    setup_pr = MagicMock(return_value="pr ready")
    try_clone = MagicMock(return_value=("clone ready", None))
    install = MagicMock(return_value=SimpleNamespace(note="", failed=False))
    monkeypatch.setattr(lifecycle, "_setup_branch", setup_branch)
    monkeypatch.setattr(lifecycle, "_setup_pr_branch", setup_pr)
    monkeypatch.setattr(lifecycle, "_try_clone_into_container", try_clone)
    monkeypatch.setattr(lifecycle, "_install_repo_deps", install)

    args = {"image": _IMAGE}
    if mode == "one_shot":
        args["commands"] = ["true"]
        entrypoint = lifecycle.run_container_and_exec
    else:
        entrypoint = lifecycle.sandbox_initialize

    if pathway == "clone":
        args["clone_repo"] = "owner/repo"
        args["allow_network"] = False  # clone_repo auto-enables network
    elif pathway == "branch":
        args.update(clone_repo="owner/repo", branch="feature", allow_network=False)
    elif pathway == "pr":
        args.update(repo="owner/repo", pr=7, allow_network=False)
    else:
        args["allow_network"] = False

    result = entrypoint(**args)
    assert not (isinstance(result, str) and result.startswith("Error"))

    if pathway == "clone":
        try_clone.assert_called_once()
        install.assert_called_once()
        assert install.call_args.kwargs["allow_network"] is True
    elif pathway == "branch":
        setup_branch.assert_called_once()
        assert setup_branch.call_args.kwargs["allow_network"] is True
        install.assert_called_once()
        assert install.call_args.kwargs["allow_network"] is True
    elif pathway == "pr":
        setup_pr.assert_called_once()
        assert setup_pr.call_args.kwargs["allow_network"] is True
        install.assert_called_once()
        assert install.call_args.kwargs["allow_network"] is True
    else:
        setup_branch.assert_not_called()
        setup_pr.assert_not_called()
        try_clone.assert_not_called()
        install.assert_not_called()
        assert client.containers.run.call_args.kwargs["network_mode"] == "none"


@pytest.mark.parametrize("pathway", ["clone", "branch", "pr"])
def test_sandbox_initialize_apply_from_container_reprepares_with_network(
    monkeypatch, pathway
):
    _client, _container = _lifecycle_mocks(monkeypatch)
    setup_branch = MagicMock(return_value="branch ready")
    setup_pr = MagicMock(return_value="pr ready")
    try_clone = MagicMock(return_value=("clone ready", None))
    install = MagicMock(return_value=SimpleNamespace(note="", failed=False))
    monkeypatch.setattr(lifecycle, "_setup_branch", setup_branch)
    monkeypatch.setattr(lifecycle, "_setup_pr_branch", setup_pr)
    monkeypatch.setattr(lifecycle, "_try_clone_into_container", try_clone)
    monkeypatch.setattr(lifecycle, "_install_repo_deps", install)
    monkeypatch.setattr(
        lifecycle, "_carry_worktree",
        lambda *args, **kwargs: SimpleNamespace(error=None, step=None, hint=None),
    )
    monkeypatch.setattr(lifecycle, "_format_carry_report", lambda *args, **kwargs: "carry ok")
    monkeypatch.setattr(lifecycle, "_reap_orphaned_init_containers", lambda *args, **kwargs: None)

    args = {"image": _IMAGE, "apply_from_container": "old-container"}
    if pathway == "clone":
        args["clone_repo"] = "owner/repo"
    elif pathway == "branch":
        args.update(clone_repo="owner/repo", branch="feature")
    else:
        args.update(repo="owner/repo", pr=7)

    result = lifecycle.sandbox_initialize(**args)
    assert not result.startswith("Error")
    install.assert_called_once()
    assert install.call_args.kwargs["allow_network"] is True
    if pathway == "branch":
        assert setup_branch.call_args.kwargs["install_deps"] is False
        assert setup_branch.call_args.kwargs["allow_network"] is True
    elif pathway == "pr":
        assert setup_pr.call_args.kwargs["install_deps"] is False
        assert setup_pr.call_args.kwargs["allow_network"] is True
    else:
        try_clone.assert_called_once()
