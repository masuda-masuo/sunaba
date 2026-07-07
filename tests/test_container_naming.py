"""Tests for container naming and attach (Issue #478)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.security import MANAGED_LABEL, NAME_LABEL
from code_sandbox_mcp.tools.container import (
    sandbox_attach,
    sandbox_initialize,
    sandbox_list_containers,
)


def _make_container(
    container_id: str = "abc123def456",
    status: str = "running",
    name: str | None = "my-container",
    image_tag: str = "python:3.12",
    labels: dict | None = None,
) -> MagicMock:
    """Build a mock Docker container with labels."""
    c = MagicMock()
    c.id = container_id
    c.status = status
    c.image.tags = [image_tag]
    c.image.short_id = "sha256:xyz"
    c.labels = labels or {
        MANAGED_LABEL: "true",
        NAME_LABEL: name,
    }
    c.exec_run.side_effect = [
        (0, (b"main\n", b"")),
        (0, (b"", b"")),
    ]
    return c


def _make_client(containers: list[MagicMock]) -> MagicMock:
    """Build a mock Docker client with filter-aware list()."""
    client = MagicMock()

    def _list(all=False, filters=None, **kw):
        label_filter = (filters or {}).get("label", "")
        if not label_filter:
            return list(containers)
        result = []
        for c in containers:
            clabels = getattr(c, "labels", None) or {}
            key, _, val = label_filter.partition("=")
            if clabels.get(key) == val:
                result.append(c)
        return result

    client.containers.list.side_effect = _list
    if containers:
        client.containers.get.return_value = containers[0]
    else:
        client.containers.get.side_effect = Exception("not found")
    return client


class TestSandboxInitializeName:
    """sandbox_initialize(name=...)"""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container.build_secure_run_kwargs")
    def test_name_label_stored(
        self,
        mock_build: MagicMock,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """NAME_LABEL is set on the container when name= is given."""
        mock_container = _make_container(name="my-sandbox")
        mock_container.labels = {}
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_build.return_value = {
            "command": "sleep infinity",
            "detach": True,
        }

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            name="my-sandbox",
        )

        call_kwargs = mock_build.call_args[1]
        labels = call_kwargs.get("labels", {})
        assert labels.get(NAME_LABEL) == "my-sandbox"
        assert "my-sandbox" in result

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container.build_secure_run_kwargs")
    def test_name_collision_raises_error(
        self,
        mock_build: MagicMock,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Initialize with an already-running name returns an error."""
        existing = _make_container(name="my-sandbox")
        client = _make_client([existing])
        mock_docker.return_value = client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            name="my-sandbox",
        )

        assert result.startswith("Error:")
        assert "my-sandbox" in result
        assert "already exists" in result

    @patch("code_sandbox_mcp.tools.container.build_secure_run_kwargs")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_name_collision_stopped_container(
        self,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
        mock_build: MagicMock,
    ) -> None:
        """Initialize with a stopped container's name succeeds (not collided)."""
        existing = _make_container(name="my-sandbox", status="exited")
        new_container = _make_container(name="my-sandbox", container_id="new123456789")
        client = MagicMock()
        client.containers.list.return_value = [existing]
        client.containers.run.return_value = new_container
        mock_docker.return_value = client
        mock_build.return_value = {}

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            name="my-sandbox",
        )

        assert not result.startswith("Error:")

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container.build_secure_run_kwargs")
    def test_name_collision_exact_id_match(
        self,
        mock_build: MagicMock,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A running container matched by NAME_LABEL triggers collision."""
        existing = _make_container(name="foo-bar", container_id="xyz789")
        client = _make_client([existing])
        mock_docker.return_value = client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            name="foo-bar",
        )

        assert result.startswith("Error:")
        assert "foo-bar" in result


class TestSandboxListContainers:
    """sandbox_list_containers"""

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_list_returns_managed_containers(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Returns only containers with MANAGED_LABEL=true."""
        c1 = _make_container(container_id="aaa111", name="alpha")
        c2 = _make_container(container_id="bbb222", name="beta")
        client = _make_client([c1, c2])
        mock_docker.return_value = client

        result = json.loads(sandbox_list_containers())
        assert len(result["containers"]) == 2
        assert result["containers"][0]["container_id"] == "aaa111"
        assert result["containers"][0]["name"] == "alpha"
        assert result["containers"][1]["container_id"] == "bbb222"
        assert result["containers"][1]["name"] == "beta"

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_list_empty(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Returns empty list when no managed containers exist."""
        client = _make_client([])
        mock_docker.return_value = client

        result = json.loads(sandbox_list_containers())
        assert result["containers"] == []

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_list_filters_by_managed_label(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Verifies that the label filter is applied."""
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []
        mock_docker.return_value = mock_client

        sandbox_list_containers()

        call_filters = mock_client.containers.list.call_args[1]
        assert call_filters["filters"]["label"] == f"{MANAGED_LABEL}=true"

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_list_without_name(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Containers without NAME_LABEL appear with name=None."""
        c = _make_container(container_id="ccc333", name=None)
        client = _make_client([c])
        mock_docker.return_value = client

        result = json.loads(sandbox_list_containers())
        assert result["containers"][0]["name"] is None


class TestSandboxAttach:
    """sandbox_attach"""

    @patch("code_sandbox_mcp.tools.container.checkpoint_list")
    @patch("code_sandbox_mcp.tools.container.read_journal")
    @patch("code_sandbox_mcp.tools.container.resolve_git_root")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_attach_by_name(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
    ) -> None:
        """Attach by NAME_LABEL match."""
        c = _make_container(name="my-container")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_git_root.return_value = "/home/sandbox"
        mock_journal.return_value = [
            {"container_id": "abc123def456", "operation": "exec"},
        ]
        mock_cp.return_value = json.dumps({
            "checkpoints": [{"sha": "abc", "message": "wip", "date": "2026-01-01"}],
        })

        result = json.loads(sandbox_attach("my-container"))
        assert result["found"] is True
        assert result["container_id"] == "abc123def456"
        assert result["name"] == "my-container"
        assert result["match_type"] == "name"

    @patch("code_sandbox_mcp.tools.container.checkpoint_list")
    @patch("code_sandbox_mcp.tools.container.read_journal")
    @patch("code_sandbox_mcp.tools.container.resolve_git_root")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_attach_by_id_prefix(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
    ) -> None:
        """Attach by managed-container ID prefix."""
        c = _make_container(container_id="abc123def456", name=None)
        client = _make_client([c])
        mock_docker.return_value = client
        mock_git_root.return_value = None
        mock_journal.return_value = []
        mock_cp.return_value = json.dumps({"checkpoints": []})

        result = json.loads(sandbox_attach("abc123"))
        assert result["found"] is True
        assert result["container_id"] == "abc123def456"
        assert result["match_type"] == "id"

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_attach_not_found(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Attach to a non-existent name or ID returns error."""
        client = _make_client([])
        mock_docker.return_value = client

        result = json.loads(sandbox_attach("nonexistent"))
        assert result["found"] is False
        assert "No managed container" in result["error"]

    @patch("code_sandbox_mcp.tools.container._docker")
    def test_attach_ambiguous_prefix(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Ambiguous ID prefix returns an error listing matches."""
        c1 = _make_container(container_id="abc111def222", name=None)
        c2 = _make_container(container_id="abc333def444", name=None)
        client = _make_client([c1, c2])
        mock_docker.return_value = client

        result = json.loads(sandbox_attach("abc"))
        assert result["found"] is False
        assert "Ambiguous" in result["error"]
        assert "abc111" in result["error"]

    @patch("code_sandbox_mcp.tools.container.checkpoint_list")
    @patch("code_sandbox_mcp.tools.container.read_journal")
    @patch("code_sandbox_mcp.tools.container.resolve_git_root")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_attach_orientation_info(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
    ) -> None:
        """Orientation summary includes git, checkpoint, and journal info."""
        c = _make_container(name="my-container")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_git_root.return_value = "/home/sandbox"
        mock_journal.return_value = [
            {"container_id": "abc123def456", "operation": "exec"},
            {"container_id": "abc123def456", "operation": "exec"},
        ]
        mock_cp.return_value = json.dumps({
            "checkpoints": [{"sha": "abc", "message": "checkpoint-1", "date": "2026-01-01T00:00:00"}],
        })

        result = json.loads(sandbox_attach("my-container"))
        assert result["found"] is True
        assert result["git"]["branch"] == "main"
        assert result["last_checkpoint"] == "checkpoint-1"
        assert result["journal_activity"] == 2

    @patch("code_sandbox_mcp.tools.container.checkpoint_list")
    @patch("code_sandbox_mcp.tools.container.read_journal")
    @patch("code_sandbox_mcp.tools.container.resolve_git_root")
    @patch("code_sandbox_mcp.tools.container._docker")
    def test_attach_orientation_no_git(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
    ) -> None:
        """Orientation gracefully handles missing git repo."""
        c = _make_container(name="no-git")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_git_root.side_effect = Exception("no git")
        mock_journal.return_value = []
        mock_cp.return_value = json.dumps({"checkpoints": []})

        result = json.loads(sandbox_attach("no-git"))
        assert result["found"] is True
        assert "git" not in result
