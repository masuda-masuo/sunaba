"""Tests for structured initialization outcome, wrapper orchestration, and failure classification."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.tools import ToolResult

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container.clone import DepsResult
from sunaba.tools.container.lifecycle import (
    InitResult,
    sandbox_initialize,
    sandbox_initialize_tool,
)


class _MockContainer:
    def __init__(self, cid: str = "outcometest01", branch: str = "main"):
        self.id = cid
        self.branch = branch

    def exec_run(self, cmd: Any, **kwargs: Any) -> tuple[int, tuple[bytes, bytes]]:
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if "rev-parse --abbrev-ref HEAD" in cmd_str:
            return 0, (f"{self.branch}\n".encode("utf-8"), b"")
        return 0, (b"", b"")

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
        patch("sunaba.tools.container.clone._resolve_pr_head_ref", return_value=("feature", "main")),
        patch("sunaba.tools.container.clone._resolve_vcs_token", return_value=None),
        patch("sunaba.tools.container.clone._clone_repo_via_network", return_value="Clone OK"),
        patch("sunaba.tools.container.clone.record_copy"),
        patch("sunaba.tools.container.lifecycle.record_initialize_complete"),
        patch("sunaba.tools.container.lifecycle.record_stop"),
        patch("sunaba.tools.container.lifecycle._install_repo_deps", mock_deps),
        patch("sunaba.tools.container.clone._install_repo_deps", mock_deps),
    ):
        yield mock_container, mock_deps


def test_literal_deps_failed_string_remains_success_without_error(
    mock_transports: tuple[_MockContainer, MagicMock],
) -> None:
    container, mock_deps = mock_transports
    # Output note contains literal substring "[deps failed]" but failed is False
    success_note = "deps: installed package containing [deps failed] text ok"
    mock_deps.return_value = DepsResult(note=success_note, failed=False)

    # 1. Direct public API returns str without running prefix
    res_str = sandbox_initialize(
        image="python@sha256:" + "0" * 64,
        clone_repo="owner/repo",
        allow_network=True,
    )
    assert isinstance(res_str, str)
    assert "[deps failed]" in res_str
    assert f"({success_note})" in res_str
    assert not res_str.startswith("Container")
    assert "dependency preparation failed" not in res_str

    # 2. Async wrapper with ctx=None returns plain str (is_error is False)
    res_tool = asyncio.run(
        sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            clone_repo="owner/repo",
            allow_network=True,
            ctx=None,
        )
    )
    assert isinstance(res_tool, str)
    assert not isinstance(res_tool, ToolResult)
    assert not getattr(res_tool, "is_error", False)
    assert "[deps failed]" in res_tool
    assert f"({success_note})" in str(res_tool)

    # 3. Async wrapper with progress ctx also returns plain str
    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    res_tool_ctx = asyncio.run(
        sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            clone_repo="owner/repo",
            allow_network=True,
            ctx=mock_ctx,
        )
    )
    assert isinstance(res_tool_ctx, str)
    assert not isinstance(res_tool_ctx, ToolResult)
    assert not getattr(res_tool_ctx, "is_error", False)
    assert f"({success_note})" in str(res_tool_ctx)


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"branch": "feat"},  # missing clone_repo
        {"branch": "feat", "pr": 12},  # mutually exclusive
        {"branch": ""},  # empty branch
        {"apply_from_container": "oldcid123456"},  # missing clone target
    ],
)
def test_preexisting_error_prefix_remains_plain_str(bad_kwargs: dict[str, Any]) -> None:
    # Early errors returning Error: must remain plain str in both public API and wrapper
    res_sync = sandbox_initialize(**bad_kwargs)
    assert isinstance(res_sync, str)
    assert res_sync.startswith("Error:")

    # ctx=None route
    res_tool = asyncio.run(sandbox_initialize_tool(**bad_kwargs, ctx=None))
    assert isinstance(res_tool, str)
    assert not isinstance(res_tool, ToolResult)
    assert not getattr(res_tool, "is_error", False)
    assert res_tool.startswith("Error:")

    # ctx executor route
    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    res_tool_ctx = asyncio.run(sandbox_initialize_tool(**bad_kwargs, ctx=mock_ctx))
    assert isinstance(res_tool_ctx, str)
    assert not isinstance(res_tool_ctx, ToolResult)
    assert not getattr(res_tool_ctx, "is_error", False)
    assert res_tool_ctx.startswith("Error:")


def _initialize_mode_kwargs(mode: str) -> dict[str, Any]:
    if mode == "plain":
        return {"clone_repo": "owner/repo", "allow_network": True}
    if mode == "branch":
        return {"clone_repo": "owner/repo", "branch": "feature", "allow_network": True}
    if mode == "pr":
        return {"repo": "owner/repo", "pr": 42, "allow_network": True}
    raise ValueError(f"Unknown mode: {mode}")


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_failed_deps_yields_toolresult_is_error_with_running_prefix(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    container, mock_deps = mock_transports
    if mode == "branch":
        container.branch = "feature"
    fail_note = "deps: uv sync timeout (deadline_s=180s, reap=ok)"
    mock_deps.return_value = DepsResult(note=fail_note, failed=True)

    expected_prefix = (
        f"Container {container.id[:12]} is running; dependency preparation failed: {fail_note}: "
    )

    # Public API returns the failure note once, in the leading running prefix.
    res_str = sandbox_initialize(
        image="python@sha256:" + "0" * 64,
        **_initialize_mode_kwargs(mode),
    )
    assert isinstance(res_str, str)
    assert res_str.count(fail_note) == 1
    assert f"({fail_note})" not in res_str
    assert res_str.startswith(expected_prefix)
    assert "[deps failed]" in res_str

    # Wrapper with ctx=None returns ToolResult with is_error=True.
    res_tool = asyncio.run(
        sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            **_initialize_mode_kwargs(mode),
            ctx=None,
        )
    )
    assert isinstance(res_tool, ToolResult)
    assert res_tool.is_error is True
    assert isinstance(res_tool.content, list) and res_tool.content
    content_text = getattr(res_tool.content[0], "text", "")
    assert content_text.count(fail_note) == 1
    assert f"({fail_note})" not in content_text
    assert content_text.startswith(expected_prefix)
    assert "[deps failed]" in content_text

    # Wrapper with progress ctx returns ToolResult with is_error=True.
    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    res_tool_ctx = asyncio.run(
        sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            **_initialize_mode_kwargs(mode),
            ctx=mock_ctx,
        )
    )
    assert isinstance(res_tool_ctx, ToolResult)
    assert res_tool_ctx.is_error is True
    content_ctx_text = getattr(res_tool_ctx.content[0], "text", "")
    assert content_ctx_text.count(fail_note) == 1
    assert f"({fail_note})" not in content_ctx_text
    assert content_ctx_text.startswith(expected_prefix)
    assert "[deps failed]" in content_ctx_text


@pytest.mark.parametrize("mode", ["plain", "branch", "pr"])
def test_success_deps_retains_parenthesized_note(
    mock_transports: tuple[_MockContainer, MagicMock],
    mode: str,
) -> None:
    container, mock_deps = mock_transports
    if mode == "branch":
        container.branch = "feature"
    success_note = "deps: uv sync ok"
    mock_deps.return_value = DepsResult(note=success_note, failed=False)

    # 1. Public API returns str with parenthesized note and without error prefix
    res_str = sandbox_initialize(
        image="python@sha256:" + "0" * 64,
        **_initialize_mode_kwargs(mode),
    )
    assert isinstance(res_str, str)
    assert f" ({success_note})" in res_str
    assert "[deps failed]" not in res_str
    assert not res_str.startswith("Container")
    assert "dependency preparation failed" not in res_str
    assert res_str.count(success_note) == 1

    # 2. Wrapper with ctx=None returns plain str (is_error is False)
    res_tool = asyncio.run(
        sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            **_initialize_mode_kwargs(mode),
            ctx=None,
        )
    )
    assert isinstance(res_tool, str)
    assert not isinstance(res_tool, ToolResult)
    assert not getattr(res_tool, "is_error", False)
    assert f" ({success_note})" in res_tool
    assert "[deps failed]" not in res_tool
    assert not res_tool.startswith("Container")
    assert "dependency preparation failed" not in res_tool
    assert res_tool.count(success_note) == 1

    # 3. Wrapper with progress ctx also returns plain str
    mock_ctx = MagicMock()
    mock_ctx.report_progress = AsyncMock()
    res_tool_ctx = asyncio.run(
        sandbox_initialize_tool(
            image="python@sha256:" + "0" * 64,
            **_initialize_mode_kwargs(mode),
            ctx=mock_ctx,
        )
    )
    assert isinstance(res_tool_ctx, str)
    assert not isinstance(res_tool_ctx, ToolResult)
    assert not getattr(res_tool_ctx, "is_error", False)
    assert f" ({success_note})" in res_tool_ctx
    assert "[deps failed]" not in res_tool_ctx
    assert not res_tool_ctx.startswith("Container")
    assert "dependency preparation failed" not in res_tool_ctx
    assert res_tool_ctx.count(success_note) == 1


def test_structured_flag_independent_of_text_and_no_double_invocation() -> None:
    # Structured flag alone governs is_error, regardless of text contents
    # Case A: text has NO failure keywords, but deps_failed is True -> must be ToolResult(is_error=True)
    with patch(
        "sunaba.tools.container.lifecycle._sandbox_initialize",
        return_value=InitResult(text="all operations succeeded completely", deps_failed=True),
    ) as mock_impl:
        res = asyncio.run(sandbox_initialize_tool(image="full", ctx=None))
        assert isinstance(res, ToolResult)
        assert res.is_error is True
        assert getattr(res.content[0], "text", "") == "all operations succeeded completely"
        assert mock_impl.call_count == 1

    # Case B: text contains both "Error:" and "[deps failed]", but deps_failed is False -> plain str
    with patch(
        "sunaba.tools.container.lifecycle._sandbox_initialize",
        return_value=InitResult(text="Error: unexpected warning [deps failed] in note", deps_failed=False),
    ) as mock_impl:
        res = asyncio.run(sandbox_initialize_tool(image="full", ctx=None))
        assert isinstance(res, str)
        assert not isinstance(res, ToolResult)
        assert not getattr(res, "is_error", False)
        assert res == "Error: unexpected warning [deps failed] in note"
        assert mock_impl.call_count == 1
