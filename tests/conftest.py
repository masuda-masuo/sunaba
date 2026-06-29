"""Shared fixtures for all tests.

An autouse fixture patches ``get_cached_result`` and ``set_cached_result``
in the tools submodules that use them (``exec``, ``container``) so existing
and new tests are never accidentally affected by real cache data written to
``~/.code-sandbox-mcp/cache/`` by a previous test run.

Tests that need to verify cache behaviour can still override these mocks
by patching the same targets with custom return values (decorators or
context managers).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _mock_result_cache() -> None:
    """Prevent all tests from reading/writing real cache data."""
    with (
        patch("code_sandbox_mcp.tools.exec.get_cached_result", return_value=None),
        patch("code_sandbox_mcp.tools.exec.set_cached_result"),
        patch("code_sandbox_mcp.tools.container.get_cached_result", return_value=None),
        patch("code_sandbox_mcp.tools.container.set_cached_result"),
        patch("code_sandbox_mcp.tools.vcs.resolve_git_root", side_effect=lambda c, wd: wd if wd is not None else "/home/sandbox"),
    ):
        yield

# -------------------------------------------------------------------
# Shared helpers for VCS tool tests
# -------------------------------------------------------------------


import asyncio
import inspect
import json
from unittest.mock import MagicMock


def _make_container_mock(exec_returns: list[tuple[int, bytes, bytes]]):
    """Build a mock Docker container with a sequence of exec_run results."""
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    return container


def _make_client_mock(container: MagicMock):
    """Build a mock Docker client that returns the given container."""
    client = MagicMock()
    client.containers.get.return_value = container
    return client


def _decode(result):
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return json.loads(result)


# -------------------------------------------------------------------
# Shared test helpers for edit-verify tests
# -------------------------------------------------------------------


class _FakeContainer:
    """Emulates the in-container shell for the transform runner."""

    def __init__(self, path_map=None) -> None:
        self.ran = False
        self.path_map = path_map or {}

    def exec_run(self, cmd, **kwargs):
        import base64 as _b64
        import io
        import sys

        self.ran = True
        shell_cmd = cmd[-1]
        blob = shell_cmd.split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'\"")
        runner_src = _b64.b64decode(blob).decode("utf-8")

        real_open = open
        pm = self.path_map

        def mapped_open(path, *a, **k):
            return real_open(pm.get(path, path), *a, **k)

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            try:
                exec(compile(runner_src, "<runner>", "exec"), {"open": mapped_open})
            except SystemExit:
                pass
        finally:
            sys.stdout = old
        return 0, (buf.getvalue().encode("utf-8"), b"")


class _FakeClient:
    def __init__(self, container) -> None:
        self._c = container

    class _Containers:
        def __init__(self, c) -> None:
            self._c = c

        def get(self, _cid):
            return self._c

    @property
    def containers(self):
        return _FakeClient._Containers(self._c)
