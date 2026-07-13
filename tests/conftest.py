"""Shared fixtures for all tests.

An autouse fixture patches ``resolve_git_root`` in the VCS tools so tests
never depend on the host filesystem layout for git-root detection.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_resolve_git_root() -> None:
    """Give VCS tools a deterministic git root."""
    with (
        patch("sunaba.tools.vcs.resolve_git_root", side_effect=lambda c, wd=None: wd if wd is not None else "/home/sandbox"),
    ):
        yield


@pytest.fixture(autouse=True)
def _skip_workspace_bootstrap() -> None:
    """Stub out the workspace mkdir/chown exec that every init performs.

    Container init prepares the workspace inside the container before doing
    anything else.  Tests that drive init with a mock container are not about
    that exec, and letting it through would consume the first entry of every
    ``exec_run`` side-effect list.  ``tests/test_workspace_root.py`` covers the
    real thing.
    """
    with patch("sunaba.tools.container._ensure_workspace"):
        yield

# -------------------------------------------------------------------
# Shared helpers for VCS tool tests
# -------------------------------------------------------------------


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
