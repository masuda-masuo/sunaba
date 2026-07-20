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


def _make_publish_container(exec_returns: list[tuple[int, bytes, bytes]]):
    """Build a mock container for publish tests that transparently handles
    extra exec_run calls from the secret scan module.

    The publish flow calls ``container.exec_run`` in two ways:

    1. **Publish's internal ``_run()``** — positional first arg
       ``exec_run([\"/bin/sh\", \"-c\", cmd], stdout=True, stderr=True)``
       *(no ``demux``)*.  These consume from the positional *exec_returns*
       list in order.

    2. **``exec_in_container`` (from ``secret_scan``)** — keyword-only
       ``exec_run(cmd=[...], demux=True, ...)``.  These are **intercepted
       by command dispatch** so they never consume a positional entry,
       keeping the order assertions on git commands intact.

    Known secret-scan commands that are dispatched:

    * ``detect-secrets --version`` → available (exit 0, version string)
    * ``detect-secrets scan …``   → clean scan output (empty results)
    * ``cat …/.secrets.baseline …`` → baseline absent (exit 1)
    * ``git diff-tree …``          → returns *diff_tree_output* (default
      ``b""`` so ``run_secret_scan`` receives no files and returns
      immediately without further ``exec_run`` calls).

    Parameters
    ----------
    exec_returns:
        Same format as ``_make_container_mock`` — one ``(ec, stdout, stderr)``
        entry per publish ``_run`` call, in order.
    diff_tree_output:
        Bytes that the ``git diff-tree …`` ``exec_in_container`` call returns
        on stdout.  Default ``b""`` (empty) makes ``run_secret_scan`` a no-op.

    Returns
    -------
    A ``MagicMock`` container whose ``exec_run`` dispatches transparently.
    """
    container = MagicMock()
    results = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    pos = [0]

    def _side_effect(*args: object, **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        nonlocal pos
        cmd = args[0] if args else kwargs.get("cmd", [])
        if not isinstance(cmd, list):
            cmd = []
        cmd_str = " ".join(str(c) for c in cmd)

        # --- Secret scan: detect-secrets --version ---
        if cmd == ["detect-secrets", "--version"]:
            return (0, (b"1.5.0\n", b""))

        # --- Secret scan: detect-secrets scan ---
        if "detect-secrets scan" in cmd_str:
            clean = (
                '{"results": {},'
                ' "generated_at": "2026-01-01T00:00:00Z",'
                ' "plugins_used": []}'
            )
            return (0, (clean.encode("utf-8"), b""))

        # --- Secret scan: cat .secrets.baseline ---
        if ".secrets.baseline" in cmd_str:
            return (1, (b"", b""))  # baseline not found

        # --- exec_in_container: git diff-tree ---
        if "git diff-tree" in cmd_str:
            return (0, (b"", b""))

        # --- Regular publish _run calls: consume from positional list ---
        if pos[0] >= len(results):
            raise StopIteration(
                f"Mock exec_run called {pos[0] + 1} times "
                f"but only {len(results)} results provided"
            )
        result = results[pos[0]]
        pos[0] += 1
        return result

    container.exec_run.side_effect = _side_effect
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


def _exec_cmd(call) -> str:
    """Extract the shell command string from an exec_run call.

    ``publish``'s internal ``_run()`` calls ``exec_run`` with a positional
    first argument ``[\"/bin/sh\", \"-c\", cmd_str]``, while the secret scan's
    ``exec_in_container`` calls it with keyword ``cmd=[...]``.

    This helper handles both forms, so test assertions that iterate over
    ``call_args_list`` never crash on ``IndexError`` from a keyword-only call.
    """
    args, kwargs = call
    if args:
        # Positional: args[0] is the list ["/bin/sh", "-c", cmd_str]
        cmd_list = args[0]
        if isinstance(cmd_list, list) and len(cmd_list) > 2:
            return str(cmd_list[2])
        return " ".join(str(x) for x in cmd_list)
    # Keyword-only: cmd= keyword
    cmd_list = kwargs.get("cmd", [])
    return " ".join(str(x) for x in cmd_list)
