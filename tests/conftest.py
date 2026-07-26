"""Shared fixtures for all tests.

An autouse fixture patches ``resolve_git_root`` in the VCS tools so tests
never depend on the host filesystem layout for git-root detection.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any
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


# -------------------------------------------------------------------
# Docker-py compliant exec_run fake (Issue #742)
# -------------------------------------------------------------------


def _make_exec_run_results(
    results: list[tuple[int, bytes, bytes]],
) -> list[tuple[int, bytes | tuple[bytes, bytes]]]:
    """Convert ``(ec, stdout, stderr)`` triples into docker-py-compliant
    ``exec_run`` return values.

    docker-py returns ``(ec, (stdout, stderr))`` **only** when the call was
    made with ``demux=True``.  Without ``demux`` it returns
    ``(ec, multiplexed_bytes)`` — the two streams are not separable.

    This helper only builds the demuxed form.  The ``demux``/no-``demux``
    dispatch itself lives in :class:`_DockerCompliantExec`, which
    re-multiplexes the pair back into a single ``bytes`` when the caller
    omitted ``demux`` -- so a caller that forgets it gets the same
    unusable single stream docker-py would have handed back.
    """
    return [
        (ec, (stdout, stderr)) for ec, stdout, stderr in results
    ]


class _DockerCompliantExec:
    """Replacement for ``MagicMock.exec_run`` that checks for ``demux``.

    docker-py returns ``(ec, (stdout, stderr))`` **only** when called with
    ``demux=True``; without ``demux`` it returns multiplexed bytes.  This
    class wraps a private ``MagicMock`` to enforce that contract while
    forwarding ``.return_value``, ``.side_effect``, ``.call_args``,
    ``.call_args_list`` and ``.call_count`` transparently, so existing
    patterns like ``mock.exec_run.return_value = (0, (b"...", b""))``
    continue to work.
    """

    def __init__(self) -> None:
        self._mock = MagicMock()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        result = self._mock(*args, **kwargs)
        ec, output = result
        if kwargs.get("demux"):
            if isinstance(output, tuple):
                # docker-py yields ``None``, not ``b""``, for a stream that
                # produced nothing.
                stdout_part, stderr_part = output
                return ec, (stdout_part or None, stderr_part or None)
            return ec, output
        if isinstance(output, tuple):
            return ec, (output[0] or b"") + (output[1] or b"")
        return ec, output

    @property
    def return_value(self) -> Any:
        return self._mock.return_value

    @return_value.setter
    def return_value(self, val: Any) -> None:
        self._mock.return_value = val

    @property
    def side_effect(self) -> Any:
        return self._mock.side_effect

    @side_effect.setter
    def side_effect(self, val: Any) -> None:
        self._mock.side_effect = val

    @property
    def call_args(self) -> Any:
        return self._mock.call_args

    @property
    def call_args_list(self) -> Any:
        return self._mock.call_args_list

    @property
    def call_count(self) -> int:
        return self._mock.call_count


def _docker_compliant_mock(mock: MagicMock | None = None) -> MagicMock:
    """Wrap a MagicMock so its ``exec_run`` follows docker-py's contract.

    Use this in any test that mocks ``exec_run`` so a missing ``demux=True``
    is caught by the test, not by production.

    Example::

        mock = _docker_compliant_mock(MagicMock())
        mock.exec_run.return_value = (0, (b"stdout", b"stderr"))
        # Without demux: mock.exec_run(cmd) returns (0, b"stdoutstderr")
        # With demux:    mock.exec_run(cmd, demux=True) returns (0, (b"stdout", b"stderr"))
    """
    if mock is None:
        mock = MagicMock()
    mock.exec_run = _DockerCompliantExec()
    return mock


def _make_docker_compliant_container(
    results: list[tuple[int, bytes, bytes]],
) -> MagicMock:
    """Build a mock container whose ``exec_run`` follows docker-py's real
    contract: returns ``(ec, (stdout, stderr))`` **only** when ``demux=True``
    was passed, and raw bytes otherwise.

    Use this in tests that exercise ``exec_run`` calls so that a missing
    ``demux=True`` is caught by the test, not by production.
    """
    container = MagicMock()
    compliant_results = _make_exec_run_results(results)

    def _side_effect(*args: Any, **kwargs: Any) -> Any:
        idx = _side_effect.call_count  # type: ignore[attr-defined]
        _side_effect.call_count += 1  # type: ignore[attr-defined]
        ec, out = compliant_results[idx]
        if kwargs.get("demux"):
            return ec, out
        # Without demux: return multiplexed bytes (simplified: stdout+stderr)
        stdout_part, stderr_part = out
        combined = stdout_part + stderr_part
        return ec, combined

    _side_effect.call_count = 0  # type: ignore[attr-defined]
    container.exec_run.side_effect = _side_effect
    return container


def _make_publish_container(
    exec_returns: list[tuple[int, bytes, bytes]],
    detect_secrets_scan_output: bytes | None = None,
    git_diff_tree_output: bytes | None = None,
):
    """Build a mock container for publish tests that transparently handles
    extra exec_run calls from the secret scan module.

    The publish flow calls ``container.exec_run`` in two ways:

    1. **Publish's internal ``_run()``** — positional first arg
       ``exec_run(["/bin/sh", "-c", cmd], stdout=True, stderr=True)``
       *(no ``demux``)*.  These consume from the positional *exec_returns*
       list in order.

    2. **``exec_in_container`` (from ``secret_scan``)** — keyword-only
       ``exec_run(cmd=[...], demux=True, ...)``.  These are **intercepted
       by command dispatch** so they never consume a positional entry,
       keeping the order assertions on git commands intact.

    Known secret-scan commands that are dispatched:

    * ``detect-secrets --version`` → available (exit 0, version string)
    * ``detect-secrets scan …``   → uses *detect_secrets_scan_output*
      (default clean JSON with empty results; pass ``b"..."`` with a
      finding to produce a blocking result).
    * ``cat …/.secrets.baseline …`` → baseline absent (exit 1)
    * ``git diff-tree …``          → uses *git_diff_tree_output*
      (default ``b""`` so ``run_secret_scan`` receives no files and
      returns immediately without further ``exec_run`` calls).

    Parameters
    ----------
    exec_returns:
        Same format as ``_make_container_mock`` — one ``(ec, stdout, stderr)``
        entry per publish ``_run`` call, in order.
    detect_secrets_scan_output:
        Bytes that the ``detect-secrets scan`` ``exec_in_container`` call
        returns on stdout.  Default ``None`` = clean scan (empty results),
        preserving the existing behaviour.
    git_diff_tree_output:
        Bytes that the ``git diff-tree …`` ``exec_in_container`` call returns
        on stdout.  Default ``None`` = ``b""`` (empty), which makes
        ``run_secret_scan`` receive no files and return immediately.

    Returns
    -------
    A ``MagicMock`` container whose ``exec_run`` dispatches transparently.
    """
    container = MagicMock()
    results = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    pos = [0]

    # Resolve defaults once at construction time
    _scan_out: bytes = (
        detect_secrets_scan_output
        if detect_secrets_scan_output is not None
        else (
            b'{"results": {},'
            b' "generated_at": "2026-01-01T00:00:00Z",'
            b' "plugins_used": []}'
        )
    )
    _diff_out: bytes = git_diff_tree_output if git_diff_tree_output is not None else b""

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
            return (0, (_scan_out, b""))

        # --- Secret scan: cat .secrets.baseline ---
        if ".secrets.baseline" in cmd_str:
            return (1, (b"", b""))

        # --- exec_in_container: git diff-tree ---
        # Only intercept exec_in_container calls (cmd passed as keyword,
        # no positional args).  _run calls (positional first arg) are for
        # git_prepare_commit's own committed-path derivation and must
        # consume from the positional list.
        if "git diff-tree" in cmd_str and not args:
            return (0, (_diff_out, b""))

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


def _make_publish_container_for_scan_test(
    exec_returns: list[tuple[int, bytes, bytes]],
    *,
    detect_secrets_available: bool = True,
    scan_exit_code: int = 0,
    detect_secrets_scan_output: bytes | None = None,
    git_diff_tree_output: bytes | None = None,
):
    """Like ``_make_publish_container`` but gives full control over the
    detect-secrets scan responses so tests can exercise every error branch
    of ``run_secret_scan``.

    Parameters
    ----------
    exec_returns:
        Same as ``_make_publish_container``.
    detect_secrets_available:
        When False, the ``detect-secrets --version`` probe returns exit code 1
        (scanner absent).  Default True.
    scan_exit_code:
        Exit code for the ``detect-secrets scan`` command.  Default 0.
    detect_secrets_scan_output:
        Stdout for the scan.  Default ``None`` = clean JSON.
    git_diff_tree_output:
        Same as ``_make_publish_container``.  Default ``None`` = empty.
    """
    container = MagicMock()
    results = [
        (ec, (stdout, stderr)) for ec, stdout, stderr in exec_returns
    ]
    pos = [0]

    _scan_out: bytes = (
        detect_secrets_scan_output
        if detect_secrets_scan_output is not None
        else (
            b'{"results": {},'
            b' "generated_at": "2026-01-01T00:00:00Z",'
            b' "plugins_used": []}'
        )
    )
    _diff_out: bytes = git_diff_tree_output if git_diff_tree_output is not None else b""
    _ds_version_ec: int = 0 if detect_secrets_available else 1
    _ds_version_out: bytes = b"1.5.0\n" if detect_secrets_available else b""

    def _side_effect(*args: object, **kwargs: object) -> tuple[int, tuple[bytes, bytes]]:
        nonlocal pos
        cmd = args[0] if args else kwargs.get("cmd", [])
        if not isinstance(cmd, list):
            cmd = []
        cmd_str = " ".join(str(c) for c in cmd)

        # --- Secret scan: detect-secrets --version ---
        if cmd == ["detect-secrets", "--version"]:
            return (_ds_version_ec, (_ds_version_out, b""))

        # --- Secret scan: detect-secrets scan ---
        if "detect-secrets scan" in cmd_str:
            return (scan_exit_code, (_scan_out, b""))

        # --- Secret scan: cat .secrets.baseline ---
        if ".secrets.baseline" in cmd_str:
            return (1, (b"", b""))
        # --- exec_in_container: git diff-tree ---
        # Only intercept exec_in_container calls (cmd passed as keyword,
        # no positional args).  _run calls (positional first arg) are for
        # git_prepare_commit's own committed-path derivation and must
        # consume from the positional list.
        if "git diff-tree" in cmd_str and not args:
            return (0, (_diff_out, b""))

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
        # Follow docker-py's contract (Issue #742): the split pair is only
        # returned when the caller asked for demux; otherwise the streams
        # arrive multiplexed as a single bytes object.
        out = buf.getvalue().encode("utf-8")
        if kwargs.get("demux"):
            return 0, (out or None, None)
        return 0, out


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
