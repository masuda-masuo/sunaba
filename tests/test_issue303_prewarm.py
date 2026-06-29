"""Tests for Issue #303: cold-start image pull no longer breaks first init.

Two cooperating fixes are covered:

- **Monotonic progress** — the async ``sandbox_initialize_tool`` wrapper emits
  an *increasing* progress value (elapsed seconds) rather than a constant 0,
  so clients that only reset their request timeout on advancing progress keep
  the connection alive (MCP "SHOULD increase").
- **Image prewarm** — ``prewarm_default_image`` pulls the default sandbox
  image ahead of time and ``_start_image_prewarm`` runs it in a daemon thread
  at startup, removing the cold-start cliff without depending on progress
  notifications at all.
"""
from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from code_sandbox_mcp.server import _start_image_prewarm
from code_sandbox_mcp.tools.container import (
    prewarm_default_image,
    sandbox_initialize_tool,
)

_IMAGE = "python@sha256:0000000000000000000000000000000000000000000000000000000000000000"


class TestMonotonicProgress:
    def test_progress_value_increases_and_is_nonzero(self) -> None:
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()

        def _slow(**kwargs: object) -> str:
            import time as _t

            _t.sleep(0.3)
            return "cid1234567890"

        with patch(
            "code_sandbox_mcp.tools.container.sandbox_initialize", side_effect=_slow
        ), patch("code_sandbox_mcp.tools.container._PROGRESS_INTERVAL_SECONDS", 0.05):
            result = asyncio.run(sandbox_initialize_tool(image=_IMAGE, ctx=ctx))

        assert result == "cid1234567890"
        calls = ctx.report_progress.await_args_list
        # The slow init spans several progress intervals.
        assert len(calls) >= 2
        progresses = [c.args[0] for c in calls]
        # Regression guard for the old constant-0 bug.
        assert progresses[0] > 0
        # MCP spec: progress SHOULD increase on every notification.
        assert all(b > a for a, b in zip(progresses, progresses[1:]))


class TestPrewarmDefaultImage:
    def test_calls_ensure_image_with_default(self) -> None:
        with patch(
            "code_sandbox_mcp.tools.container._ensure_image"
        ) as ensure, patch(
            "code_sandbox_mcp.tools.container._DEFAULT_IMAGE", _IMAGE
        ):
            prewarm_default_image()
        ensure.assert_called_once_with(_IMAGE)

    def test_swallows_errors(self) -> None:
        with patch(
            "code_sandbox_mcp.tools.container._ensure_image",
            side_effect=RuntimeError("docker down"),
        ):
            # Must not raise — prewarm failures never break startup.
            prewarm_default_image()


class TestStartImagePrewarm:
    def test_disabled_when_interval_non_positive(self) -> None:
        with patch(
            "code_sandbox_mcp.server.threading.Thread"
        ) as thread_cls, patch(
            "code_sandbox_mcp.tools.container.prewarm_default_image"
        ) as prewarm:
            _start_image_prewarm(0)
            _start_image_prewarm(-5)
        thread_cls.assert_not_called()
        prewarm.assert_not_called()

    def test_starts_daemon_thread_and_prewarms(self) -> None:
        called = threading.Event()

        def _fake() -> None:
            called.set()

        with patch(
            "code_sandbox_mcp.tools.container.prewarm_default_image",
            side_effect=_fake,
        ):
            # Long interval: the loop prewarms once, then parks in sleep.  The
            # thread is a daemon so the sleeping cycle never blocks teardown.
            _start_image_prewarm(3600)
            assert called.wait(timeout=2.0)
