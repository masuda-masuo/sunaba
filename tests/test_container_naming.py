"""Tests for container naming and attach (Issue #478)."""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest

from sunaba.journal import set_session_label
from sunaba.security import MANAGED_LABEL, NAME_LABEL
from sunaba.tools.container import (
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

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container._ensure_image")
    @patch("sunaba.tools.container.validate_image_ref")
    @patch("sunaba.tools.container.build_secure_run_kwargs")
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

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container._ensure_image")
    @patch("sunaba.tools.container.validate_image_ref")
    @patch("sunaba.tools.container.build_secure_run_kwargs")
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

    @patch("sunaba.tools.container.build_secure_run_kwargs")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container._ensure_image")
    @patch("sunaba.tools.container.validate_image_ref")
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

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container._ensure_image")
    @patch("sunaba.tools.container.validate_image_ref")
    @patch("sunaba.tools.container.build_secure_run_kwargs")
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

    @patch("sunaba.tools.container._docker")
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

    @patch("sunaba.tools.container._docker")
    def test_list_empty(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Returns empty list when no managed containers exist."""
        client = _make_client([])
        mock_docker.return_value = client

        result = json.loads(sandbox_list_containers())
        assert result["containers"] == []
        assert result["reaped_ids"] == []

    @patch("sunaba.tools.container._docker")
    def test_list_filters_by_managed_label(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Verifies that the label filter is applied."""
        mock_client = MagicMock()
        mock_client.containers.list.return_value = []
        mock_docker.return_value = mock_client

        result = json.loads(sandbox_list_containers())

        call_filters = mock_client.containers.list.call_args[1]
        assert call_filters["filters"]["label"] == f"{MANAGED_LABEL}=true"
        assert result["reaped_ids"] == []

    @patch("sunaba.tools.container._docker")
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
        assert result["reaped_ids"] == []


class TestSandboxAttach:
    """sandbox_attach"""

    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
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

    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
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

    @patch("sunaba.tools.container._docker")
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

    @patch("sunaba.tools.container._docker")
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

    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
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

    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
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


class TestSandboxAttachJournal:
    """#554: attach is the session hand-off, so it must be in the journal.

    Without an entry of its own, the journal shows operations on both sides of
    a session switch and nothing marking the switch itself -- and a
    ``session_label`` swap leaves no trace at all, since the label only ever
    rides along on *subsequent* entries.
    """

    @pytest.fixture(autouse=True)
    def _clear_session_label(self) -> Iterator[None]:
        # The label map is process-global, so a label set here would otherwise
        # leak into whichever test runs next and show up as a bogus
        # `previous_session_label`.
        yield
        set_session_label("abc123def456", None)

    @staticmethod
    def _attach(mock_docker: MagicMock, mock_git_root: MagicMock,
                mock_journal: MagicMock, mock_cp: MagicMock,
                name: str = "my-container") -> None:
        client = _make_client([_make_container(name=name)])
        mock_docker.return_value = client
        mock_git_root.return_value = None
        mock_journal.return_value = []
        mock_cp.return_value = json.dumps({"checkpoints": []})

    @patch("sunaba.tools.container.record_tool_use")
    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
    def test_attach_is_journaled(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        self._attach(mock_docker, mock_git_root, mock_journal, mock_cp)

        sandbox_attach("my-container")

        mock_record.assert_called_once_with(
            "abc123def456",
            "sandbox_attach",
            {"name_or_id": "my-container", "match_type": "name"},
        )

    @patch("sunaba.tools.container.record_tool_use")
    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
    def test_session_label_swap_records_the_label_it_replaced(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        # The entry carries the *new* label (record_tool_use attaches it), so
        # the outgoing one has to be in params or the boundary between two
        # labelled runs is unrecoverable.
        self._attach(mock_docker, mock_git_root, mock_journal, mock_cp)

        sandbox_attach("my-container", session_label="session-a")
        sandbox_attach("my-container", session_label="session-b")

        params = mock_record.call_args_list[-1].args[2]
        assert params["previous_session_label"] == "session-a"

    @patch("sunaba.tools.container.record_tool_use")
    @patch("sunaba.tools.container.checkpoint_list")
    @patch("sunaba.tools.container.read_journal")
    @patch("sunaba.tools.container.resolve_git_root")
    @patch("sunaba.tools.container._docker")
    def test_attach_without_label_change_omits_previous_label(
        self,
        mock_docker: MagicMock,
        mock_git_root: MagicMock,
        mock_journal: MagicMock,
        mock_cp: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        # Re-attaching within the same session is not a hand-off; a
        # `previous_session_label` here would be noise, not signal.
        self._attach(mock_docker, mock_git_root, mock_journal, mock_cp)

        sandbox_attach("my-container", session_label="session-a")
        sandbox_attach("my-container")

        params = mock_record.call_args_list[-1].args[2]
        assert "previous_session_label" not in params

    @patch("sunaba.tools.container.record_tool_use")
    @patch("sunaba.tools.container._docker")
    def test_failed_attach_is_not_journaled(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        # Nothing was attached, so there is no hand-off and no container to
        # attribute the entry to.
        mock_docker.return_value = _make_client([])

        result = json.loads(sandbox_attach("nope"))

        assert result["found"] is False
        mock_record.assert_not_called()


class TestListContainersIdleTime:
    """sandbox_list_containers with idle time (Issue #480)."""

    @patch("sunaba.tools.container.get_last_activity_per_container")
    @patch("sunaba.tools.container._docker")
    def test_idle_seconds_in_output(
        self,
        mock_docker: MagicMock,
        mock_activity: MagicMock,
    ) -> None:
        """list_containers includes idle_seconds and last_activity_ts."""
        c = _make_container(container_id="abc111", name="idle-test")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_activity.return_value = {"abc111": "2026-01-01T00:00:00+00:00"}

        result = json.loads(sandbox_list_containers())
        entry = result["containers"][0]
        assert "idle_seconds" in entry
        assert "last_activity_ts" in entry
        assert entry["last_activity_ts"] == "2026-01-01T00:00:00+00:00"

    @patch("sunaba.tools.container.get_last_activity_per_container")
    @patch("sunaba.tools.container._docker")
    def test_idle_none_when_no_activity(
        self,
        mock_docker: MagicMock,
        mock_activity: MagicMock,
    ) -> None:
        """idle_seconds is None when there is no journal entry."""
        c = _make_container(container_id="abc222", name="no-activity")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_activity.return_value = {}

        result = json.loads(sandbox_list_containers())
        entry = result["containers"][0]
        assert entry["idle_seconds"] is None
        assert entry["last_activity_ts"] is None


class TestGetContainerTTL:
    """_get_container_ttl_seconds (Issue #480)."""

    @patch.dict(os.environ, {"SUNABA_CONTAINER_TTL_SECONDS": "3600"})
    def test_reads_env_var(self) -> None:
        from sunaba.tools.container import _get_container_ttl_seconds
        assert _get_container_ttl_seconds() == 3600

    @patch.dict(os.environ, {"SUNABA_CONTAINER_TTL_SECONDS": ""})
    def test_empty_env_returns_zero(self) -> None:
        from sunaba.tools.container import _get_container_ttl_seconds
        assert _get_container_ttl_seconds() == 0

    @patch.dict(os.environ, {})
    def test_unset_env_returns_zero(self) -> None:
        from sunaba.tools.container import _get_container_ttl_seconds
        assert _get_container_ttl_seconds() == 0

    @patch.dict(os.environ, {"CSB_CONTAINER_TTL_SECONDS": "7200"})
    def test_legacy_env_var_ignored(self) -> None:
        from sunaba.tools.container import _get_container_ttl_seconds
        assert _get_container_ttl_seconds() == 0


class TestReapIdleContainers:
    """_reap_idle_containers and reap-on-list (Issue #480)."""

    @patch("sunaba.tools.container.get_last_activity_per_container")
    @patch("sunaba.tools.container._docker")
    @patch.dict(os.environ, {}, clear=True)
    def test_noop_when_ttl_not_set(
        self,
        mock_docker: MagicMock,
        mock_activity: MagicMock,
    ) -> None:
        """Reap is a no-op when TTL env var is not set."""
        from sunaba.tools.container import _reap_idle_containers
        result = _reap_idle_containers()
        assert result == []
        mock_docker.assert_not_called()

    @patch("sunaba.tools.container.get_last_activity_per_container")
    @patch("sunaba.tools.container._docker")
    @patch.dict(os.environ, {"SUNABA_CONTAINER_TTL_SECONDS": "3600"}, clear=True)
    def test_reaps_idle_container(
        self,
        mock_docker: MagicMock,
        mock_activity: MagicMock,
    ) -> None:
        """Container idle longer than TTL is stopped."""
        from sunaba.tools.container import _reap_idle_containers

        c = _make_container(container_id="idle-cid", name="idle")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_activity.return_value = {"idle-cid": "2025-01-01T00:00:00+00:00"}

        result = _reap_idle_containers()
        c.kill.assert_called_once()
        assert "idle-cid" in result

    @patch("sunaba.tools.container.get_last_activity_per_container")
    @patch("sunaba.tools.container._docker")
    @patch.dict(os.environ, {"SUNABA_CONTAINER_TTL_SECONDS": "3600"}, clear=True)
    def test_does_not_reap_active_container(
        self,
        mock_docker: MagicMock,
        mock_activity: MagicMock,
    ) -> None:
        """Container with recent activity is not stopped."""
        from sunaba.tools.container import _reap_idle_containers

        c = _make_container(container_id="active-cid", name="active")
        client = _make_client([c])
        mock_docker.return_value = client
        from datetime import datetime, timezone
        recent = datetime.now(timezone.utc).isoformat()
        mock_activity.return_value = {"active-cid": recent}

        result = _reap_idle_containers()
        assert result == []
        c.kill.assert_not_called()

    @patch("sunaba.tools.container.get_last_activity_per_container")
    @patch("sunaba.tools.container._docker")
    @patch.dict(os.environ, {"SUNABA_CONTAINER_TTL_SECONDS": "3600"}, clear=True)
    def test_list_reaps_idle_container(
        self,
        mock_docker: MagicMock,
        mock_activity: MagicMock,
    ) -> None:
        """sandbox_list_containers reaps idle containers when TTL is set."""
        c = _make_container(container_id="idle-cid", name="idle")
        client = _make_client([c])
        mock_docker.return_value = client
        mock_activity.return_value = {"idle-cid": "2025-01-01T00:00:00+00:00"}

        result = json.loads(sandbox_list_containers())
        assert "idle-cid" in result["reaped_ids"]
        c.kill.assert_called_once()
