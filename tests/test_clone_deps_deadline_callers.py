"""Tests for caller matrix and propagation of clone/branch/PR dependency deadline failures."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.tools import ToolResult

from sunaba.edit_verify.shell import ForegroundTreeTimeout
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.clone import (
    DepsResult,
    _install_repo_deps,
    _setup_branch,
    _setup_pr_branch,
)
from sunaba.tools.container.lifecycle import (
    run_container_and_exec,
    sandbox_initialize_tool,
)


class _MockContainer:
    def __init__(self, cid: str = "caller000001", branch: str = "feat"):
        self.id = cid
        self.branch = branch
        self.exec_calls: list[tuple[Any, dict[str, Any]]] = []

    def exec_run(self, cmd: Any, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        self.exec_calls.append((cmd, kwargs))
        cmd_str = cmd if isinstance(cmd, str) else (" ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd))
        if "rev-parse --abbrev-ref HEAD" in cmd_str:
            return 0, (f"{self.branch}\n".encode("utf-8"), b"")
        return 0, (b"cmd-output\n", b"")

    def stop(self) -> None:
        pass

    def remove(self, **kwargs: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def _disable_egress_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")


@pytest.fixture
def mock_transports():
    mock_container = _MockContainer()
    mock_client = MagicMock()
    mock_client.containers.run.return_value = mock_container

    mock_deps = MagicMock()
    mock_deps.return_value = DepsResult(note="deps: uv sync ok", failed=False)

    with (
        patch("sunaba.tools.container._docker", return_value=mock_client),
        patch("sunaba.tools.container.lifecycle.validate_image_ref", return_value="python@sha256:" + "0" * 64),
        patch("sunaba.tools.container.lifecycle._ensure_image"),
        patch("sunaba.tools.container.clone._resolve_default_branch", return_value="main"),
        patch("sunaba.tools.container.clone._resolve_pr_head_ref", return_value=("feat", "main")),
        patch("sunaba.tools.container.clone._clone_repo_via_network", return_value="Clone OK"),
        patch("sunaba.tools.container.clone.record_copy"),
        patch("sunaba.tools.container.lifecycle.record_initialize_complete"),
        patch("sunaba.tools.container.lifecycle.record_stop"),
        patch("sunaba.tools.container.lifecycle._install_repo_deps", mock_deps),
        patch("sunaba.tools.container.clone._install_repo_deps", mock_deps),
    ):
        yield mock_container, mock_deps


def _mode_kwargs(mode: str) -> dict[str, Any]:
    if mode == "plain":
        return {"clone_repo": "owner/repo", "allow_network": True}
    if mode == "branch":
        return {"clone_repo": "owner/repo", "branch": "feat", "allow_network": True}
    if mode == "pr":
        return {"repo": "owner/repo", "pr": 42, "allow_network": True}
    raise ValueError(f"Unknown mode: {mode}")


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_run_container_and_exec_deps_success(mock_transports: tuple[_MockContainer, MagicMock], mode: str) -> None:
    _container, mock_deps = mock_transports
    raw = run_container_and_exec(
        image="python@sha256:" + "0" * 64,
        commands=["echo hi"],
        **_mode_kwargs(mode),
    )
    result = json.loads(raw)
    assert result.get("status") == "ok"
    assert result.get("deps") == "deps: uv sync ok"
    assert result.get("deps_failed") is not True
    assert result.get("deps_error") is None
    assert "clone_warning" not in result
    assert "pr_warning" not in result
    assert mock_deps.call_count == 1


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_run_container_and_exec_deps_failure(mock_transports: tuple[_MockContainer, MagicMock], mode: str) -> None:
    _container, mock_deps = mock_transports
    fail_note = "deps: uv sync timeout (deadline_s=2s, reap=ok)"
    mock_deps.return_value = DepsResult(note=fail_note, failed=True)

    raw = run_container_and_exec(
        image="python@sha256:" + "0" * 64,
        commands=["echo hi"],
        **_mode_kwargs(mode),
    )
    result = json.loads(raw)

    assert result.get("deps_failed") is True
    assert result.get("deps_error") == fail_note
    if mode == "pr":
        assert result.get("pr_warning") == fail_note
    else:
        assert result.get("clone_warning") == fail_note

    has_error_flag = (
        result.get("status") == "error"
        or bool(result.get("deps_error"))
        or bool(result.get("deps_failed"))
        or bool(result.get("error"))
    )
    assert has_error_flag is True
    assert mock_deps.call_count == 1


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_run_container_and_exec_setup_failure_skips_install(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    container, mock_deps = mock_transports
    raw = ""

    if mode == "plain":
        with patch("sunaba.tools.container.clone._clone_repo_via_network", side_effect=RuntimeError("plain clone failed")):
            raw = run_container_and_exec(
                image="python@sha256:" + "0" * 64,
                commands=["echo hi"],
                **_mode_kwargs(mode),
            )
    elif mode == "branch":
        container.branch = "wrong_branch"
        raw = run_container_and_exec(
            image="python@sha256:" + "0" * 64,
            commands=["echo hi"],
            **_mode_kwargs(mode),
        )
    elif mode == "pr":
        with patch("sunaba.tools.container.clone._resolve_pr_head_ref", side_effect=RuntimeError("pr ref failed")):
            raw = run_container_and_exec(
                image="python@sha256:" + "0" * 64,
                commands=["echo hi"],
                **_mode_kwargs(mode),
            )

    assert mock_deps.call_count == 0
    result = json.loads(raw)
    warning = result.get("pr_warning") if mode == "pr" else result.get("clone_warning")
    assert warning is not None


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_run_container_and_exec_args_forwarding(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    _container, mock_deps = mock_transports
    run_container_and_exec(
        image="python@sha256:" + "0" * 64,
        commands=["echo hi"],
        pip_extras="[dev,test]",
        pip_args="--no-cache-dir",
        **_mode_kwargs(mode),
    )
    assert mock_deps.call_count == 1
    call = mock_deps.call_args
    extras = call.args[3] if len(call.args) > 3 else call.kwargs.get("pip_extras")
    assert extras == "[dev,test]"
    p_args = call.kwargs.get("pip_args") if "pip_args" in call.kwargs else (call.args[5] if len(call.args) > 5 else None)
    assert p_args == "--no-cache-dir"
    net = call.kwargs.get("allow_network") if "allow_network" in call.kwargs else (call.args[4] if len(call.args) > 4 else None)
    assert net is True


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_sandbox_initialize_tool_deps_success(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    _container, mock_deps = mock_transports
    res = asyncio.run(sandbox_initialize_tool(
        image="python@sha256:" + "0" * 64,
        **_mode_kwargs(mode),
    ))
    is_err = getattr(res, "is_error", False)
    assert not is_err
    text = getattr(res.content[0], "text", "") if isinstance(res, ToolResult) else str(res)
    assert "[deps failed]" not in text
    assert "deps: uv sync ok" in text
    assert mock_deps.call_count == 1


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_sandbox_initialize_tool_deps_failure(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    _container, mock_deps = mock_transports
    fail_note = "deps: uv sync timeout (deadline_s=2s, reap=ok)"
    mock_deps.return_value = DepsResult(note=fail_note, failed=True)

    res = asyncio.run(sandbox_initialize_tool(
        image="python@sha256:" + "0" * 64,
        **_mode_kwargs(mode),
    ))
    assert isinstance(res, ToolResult)
    assert res.is_error is True
    assert isinstance(res.content, list) and res.content
    text = getattr(res.content[0], "text", "")
    assert "[deps failed]" in text
    assert "deps: uv sync timeout" in text
    assert mock_deps.call_count == 1


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_sandbox_initialize_tool_setup_failure_skips_install(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    container, mock_deps = mock_transports

    if mode == "plain":
        with patch("sunaba.tools.container.clone._clone_repo_via_network", side_effect=RuntimeError("plain clone failed")):
            asyncio.run(sandbox_initialize_tool(
                image="python@sha256:" + "0" * 64,
                **_mode_kwargs(mode),
            ))
    elif mode == "branch":
        container.branch = "wrong_branch"
        asyncio.run(sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            **_mode_kwargs(mode),
        ))
    elif mode == "pr":
        with patch("sunaba.tools.container.clone._resolve_pr_head_ref", side_effect=RuntimeError("pr ref failed")):
            asyncio.run(sandbox_initialize_tool(
                image="python@sha256:" + "0" * 64,
                **_mode_kwargs(mode),
            ))

    assert mock_deps.call_count == 0


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_sandbox_initialize_tool_args_forwarding(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    _container, mock_deps = mock_transports
    asyncio.run(sandbox_initialize_tool(
        image="python@sha256:" + "0" * 64,
        pip_extras="[test]",
        pip_args="--extra-index-url https://pypi.org/simple",
        **_mode_kwargs(mode),
    ))
    assert mock_deps.call_count == 1
    call = mock_deps.call_args
    extras = call.args[3] if len(call.args) > 3 else call.kwargs.get("pip_extras")
    assert extras == "[test]"
    p_args = call.kwargs.get("pip_args") if "pip_args" in call.kwargs else (call.args[5] if len(call.args) > 5 else None)
    assert p_args == "--extra-index-url https://pypi.org/simple"
    net = call.kwargs.get("allow_network") if "allow_network" in call.kwargs else (call.args[4] if len(call.args) > 4 else None)
    assert net is True


def test_setup_helpers_default_install_deps_signature() -> None:
    import inspect

    sig_branch = inspect.signature(_setup_branch)
    assert "install_deps" in sig_branch.parameters
    assert sig_branch.parameters["install_deps"].default is True

    sig_pr = inspect.signature(_setup_pr_branch)
    assert "install_deps" in sig_pr.parameters
    assert sig_pr.parameters["install_deps"].default is True


@pytest.mark.parametrize("caller", ["run_container_and_exec", "sandbox_initialize"])
@pytest.mark.parametrize("mode", ["branch", "pr"])
def test_callers_explicit_install_deps_false_and_single_install(
    mock_transports: tuple[_MockContainer, MagicMock],
    caller: str,
    mode: str,
) -> None:
    import sunaba.tools.container.lifecycle as lc

    _container, mock_deps = mock_transports

    target_name = "_setup_branch" if mode == "branch" else "_setup_pr_branch"
    real_func = getattr(lc, target_name)

    with patch.object(lc, target_name, wraps=real_func) as spy_setup:
        if caller == "run_container_and_exec":
            raw = run_container_and_exec(
                image="python@sha256:" + "0" * 64,
                commands=["echo hi"],
                **_mode_kwargs(mode),
            )
            result = json.loads(raw)
            assert result.get("status") == "ok"
            assert result.get("deps") == "deps: uv sync ok"
        else:
            res_str = lc.sandbox_initialize(
                image="python@sha256:" + "0" * 64,
                **_mode_kwargs(mode),
            )
            assert not res_str.startswith("Error")
            assert "deps: uv sync ok" in res_str

        assert spy_setup.call_count == 1
        assert spy_setup.call_args.kwargs.get("install_deps") is False
        assert mock_deps.call_count == 1


def test_setup_branch_direct_install_deps_control(mock_transports: tuple[_MockContainer, MagicMock]) -> None:
    container, mock_deps = mock_transports

    # Default install_deps (omitted) defaults to True and installs once
    res_default = _setup_branch(
        container,
        "cont12345678",
        "owner/repo",
        "feat",
        "/workspace",
    )
    assert mock_deps.call_count == 1
    assert "deps: uv sync ok" in res_default

    mock_deps.reset_mock()

    # install_deps=False skips install completely
    res_false = _setup_branch(
        container,
        "cont12345678",
        "owner/repo",
        "feat",
        "/workspace",
        install_deps=False,
    )
    assert mock_deps.call_count == 0
    assert "deps" not in res_false

    # install_deps=True calls install and includes note on success
    res_true = _setup_branch(
        container,
        "cont12345678",
        "owner/repo",
        "feat",
        "/workspace",
        install_deps=True,
    )
    assert mock_deps.call_count == 1
    assert "deps: uv sync ok" in res_true
    assert "[deps failed]" not in res_true

    # install_deps=True includes [deps failed] on failure
    mock_deps.return_value = DepsResult(note="deps: npm ci timeout", failed=True)
    res_fail = _setup_branch(
        container,
        "cont12345678",
        "owner/repo",
        "feat",
        "/workspace",
        install_deps=True,
    )
    assert mock_deps.call_count == 2
    assert "deps: npm ci timeout" in res_fail
    assert "[deps failed]" in res_fail


def test_setup_pr_branch_direct_install_deps_control(mock_transports: tuple[_MockContainer, MagicMock]) -> None:
    container, mock_deps = mock_transports

    # Default install_deps (omitted) defaults to True and installs once
    res_default = _setup_pr_branch(
        container,
        "cont12345678",
        "owner/repo",
        42,
        "/workspace",
        authenticated=False,
    )
    assert mock_deps.call_count == 1
    assert "deps: uv sync ok" in res_default

    mock_deps.reset_mock()

    # install_deps=False skips install completely
    res_false = _setup_pr_branch(
        container,
        "cont12345678",
        "owner/repo",
        42,
        "/workspace",
        authenticated=False,
        install_deps=False,
    )
    assert mock_deps.call_count == 0
    assert "deps" not in res_false

    # install_deps=True calls install and includes note on success
    res_true = _setup_pr_branch(
        container,
        "cont12345678",
        "owner/repo",
        42,
        "/workspace",
        authenticated=False,
        install_deps=True,
    )
    assert mock_deps.call_count == 1
    assert "deps: uv sync ok" in res_true
    assert "[deps failed]" not in res_true

    # install_deps=True includes [deps failed] on failure
    mock_deps.return_value = DepsResult(note="deps: pip timeout", failed=True)
    res_fail = _setup_pr_branch(
        container,
        "cont12345678",
        "owner/repo",
        42,
        "/workspace",
        authenticated=False,
        install_deps=True,
    )
    assert mock_deps.call_count == 2
    assert "deps: pip timeout" in res_fail
    assert "[deps failed]" in res_fail



def test_timeout_note_deadline_s_diagnostic() -> None:
    container = _MockContainer()
    with (
        patch("sunaba.tools.container.clone._detect_manifests", return_value={"pyproject.toml", "uv.lock"}),
        patch(
            "sunaba.tools.container.clone.run_exec_under_foreground_deadline",
            side_effect=ForegroundTreeTimeout(1.5, "ok", 1.5),
        ),
        patch("sunaba.verify_state.invalidate_verify_success"),
        patch("sunaba.verify_state.record_environment_state"),
    ):
        res = _install_repo_deps(
            container,
            "owner/repo",
            "/workspace",
            pip_extras="[dev]",
            allow_network=True,
        )
        assert res.failed is True
        assert "deadline_s=1.5s" in res.note
        assert "reap=ok" in res.note
