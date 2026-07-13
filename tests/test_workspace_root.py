"""The workspace is the repo root, and the container's working directory.

Issue #600: verify's test runners exec'd without a ``workdir``, so they ran in
the home directory while the repo sat somewhere else, and the suite came back
green without running.  The fix is not to pass ``workdir`` at more call sites
but to remove the discrepancy: the container works in the repo root, so an exec
that names no directory is already in the right place.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.common import WORKSPACE
from sunaba.tools.container import _ensure_workspace, sandbox_initialize

_IMAGE = "python@sha256:" + "0" * 64


class TestEnsureWorkspace:
    """The directory has to exist and be writable by the non-root user."""

    def test_creates_and_chowns_as_root(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        _ensure_workspace(container, "/workspace")

        cmd = container.exec_run.call_args[0][0][-1]
        assert "mkdir -p /workspace" in cmd
        assert "chown" in cmd and "/workspace" in cmd
        # Docker creates a missing working directory owned by root, so only
        # root can hand it to the sandbox user.
        assert container.exec_run.call_args[1]["user"] == "root"

    def test_quotes_the_path(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, b"")

        _ensure_workspace(container, "/tmp/my repo")

        cmd = container.exec_run.call_args[0][0][-1]
        assert "'/tmp/my repo'" in cmd

    def test_failure_is_not_swallowed(self) -> None:
        """A sandbox whose workspace could not be prepared is not handed out."""
        container = MagicMock()
        container.exec_run.return_value = (1, b"chown: invalid user")

        with pytest.raises(RuntimeError, match="workspace"):
            _ensure_workspace(container, "/workspace")


class TestInitializeWorkingDir:
    """sandbox_initialize records the repo root as the container's WorkingDir."""

    @patch("sunaba.tools.container._ensure_workspace")
    @patch("sunaba.tools.container._ensure_image")
    @patch("sunaba.tools.container.validate_image_ref")
    @patch("sunaba.tools.container._docker")
    def _run_init(self, mock_docker, mock_validate, mock_ensure_image,
                  mock_ensure_ws, **kwargs) -> tuple[dict, MagicMock]:
        container = MagicMock()
        container.id = "abc123def456789"
        client = MagicMock()
        client.containers.run.return_value = container
        mock_docker.return_value = client

        sandbox_initialize(image=_IMAGE, **kwargs)
        return client.containers.run.call_args[1], mock_ensure_ws

    def test_working_dir_defaults_to_workspace(self) -> None:
        run_kwargs, _ = self._run_init()
        assert run_kwargs["working_dir"] == WORKSPACE

    def test_working_dir_follows_clone_dest(self) -> None:
        run_kwargs, _ = self._run_init(clone_dest="/srv/checkout")
        assert run_kwargs["working_dir"] == "/srv/checkout"

    def test_workspace_prepared_at_the_same_path(self) -> None:
        _, mock_ensure_ws = self._run_init(clone_dest="/srv/checkout")
        assert mock_ensure_ws.call_args[0][1] == "/srv/checkout"
