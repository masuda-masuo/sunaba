"""Shared fixtures for all tests.

An autouse fixture patches ``resolve_git_root`` in the VCS tools so tests
never depend on the host filesystem layout for git-root detection.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _mock_resolve_git_root() -> None:
    """Give VCS tools a deterministic git root."""
    with (
        patch("sunaba.tools.vcs.resolve_git_root", side_effect=lambda c, wd=None: wd if wd is not None else "/home/sandbox"),
        patch("sunaba.tools.vcs.checkpoints.resolve_git_root", side_effect=lambda c, wd=None: wd if wd is not None else "/home/sandbox"),
        patch("sunaba.tools.vcs.publishing.resolve_git_root", side_effect=lambda c, wd=None: wd if wd is not None else "/home/sandbox"),
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
    with patch("sunaba.tools.container.lifecycle._ensure_workspace"):
        yield


@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path: Path) -> None:
    """Give each test its own journal directory to avoid parallel-write conflicts.

    xdist workers (``-n N``) each get a separate process, but they share the
    real ``~/.sunaba/journal.log`` by default.  Parallel journal writes corrupt
    the sidecar ``container_state.json``.  This fixture redirects every test's
    journal to an isolated ``tmp_path`` so that parallel tests never collide.
    """
    journal_dir = tmp_path / ".sunaba"
    with (
        patch("sunaba.journal._JOURNAL_DIR", journal_dir),
        patch("sunaba.journal._JOURNAL_PATH", journal_dir / "journal.log"),
        patch("sunaba.journal._JOURNAL_BACKUP_PATH", journal_dir / "journal.log.1"),
        patch("sunaba.journal._state_synced", False),
    ):
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


@pytest.fixture(autouse=True)
def _undo_root_tmp(tmp_path, monkeypatch) -> None:
    """Keep per-edit undo snapshots out of the real ~/.sunaba during tests."""
    from sunaba import undo
    monkeypatch.setattr(undo, "_UNDO_ROOT", tmp_path / "undo-snapshots")


@pytest.fixture(autouse=True)
def _record_publish_verify() -> None:
    """Record verify success for the standard test container so publish
    tests can reach the push logic without triggering the verify gate.

    test_state_nudges.py overrides this by patching ``_verify_map`` to
    ``{}`` in its own ``_fresh_verify_state`` fixture, which runs after
    this fixture and clears the recorded state.
    """
    from sunaba.verify_state import record_verify_success
    record_verify_success("abc123def456")
