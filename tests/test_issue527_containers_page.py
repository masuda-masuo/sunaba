"""Tests for the /containers page and the labels behind it (Issue #527).

The dashboard's old "Active Environments" panel was journal-derived, so the
containers a human most wants to see -- the ones someone forgot to stop --
never appeared in it.  These tests pin the replacement: Docker's own labels are
the source of truth, and the page reads them without mutating anything.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from sunaba.dashboard import (
    get_dashboard_url,
    start_dashboard,
    stop_dashboard,
)
from sunaba.journal import get_run_id_per_container
from sunaba.proxy_lifecycle import PROXY_CONTAINER_NAME
from sunaba.security import (
    CREATED_AT_LABEL,
    KIND_LABEL,
    KIND_PROXY,
    KIND_SANDBOX,
    MANAGED_LABEL,
    NAME_LABEL,
    NETWORK_LABEL,
    build_secure_run_kwargs,
    get_default_profile,
)
from sunaba.tools.container import list_managed_containers, sandbox_list_containers


def _make_container(
    container_id: str = "abc123def456",
    labels: dict | None = None,
    docker_name: str | None = None,
    status: str = "running",
) -> MagicMock:
    c = MagicMock()
    c.id = container_id
    c.status = status
    c.image.tags = ["python:3.12"]
    c.image.short_id = "sha256:xyz"
    c.labels = labels if labels is not None else {MANAGED_LABEL: "true"}
    c.name = docker_name
    return c


def _make_client(containers: list[MagicMock]) -> MagicMock:
    client = MagicMock()
    client.containers.list.return_value = containers
    return client


class TestNetworkAndKindLabels:
    """The labels are stamped where allow_network is decided (#527)."""

    def test_networked_container_stamped_true(self) -> None:
        profile = replace(get_default_profile(), allow_network=True)
        kwargs = build_secure_run_kwargs(profile, command="sleep infinity")
        assert kwargs["labels"][NETWORK_LABEL] == "true"
        assert kwargs["labels"][KIND_LABEL] == KIND_SANDBOX

    def test_offline_container_stamped_false(self) -> None:
        profile = replace(get_default_profile(), allow_network=False)
        kwargs = build_secure_run_kwargs(profile, command="sleep infinity")
        assert kwargs["labels"][NETWORK_LABEL] == "false"

    def test_caller_labels_preserved(self) -> None:
        """The new labels sit alongside created_at/name, they don't replace them."""
        profile = replace(get_default_profile(), allow_network=False)
        kwargs = build_secure_run_kwargs(
            profile,
            command="sleep infinity",
            labels={CREATED_AT_LABEL: "2026-07-11T00:00:00+00:00", NAME_LABEL: "n"},
        )
        labels = kwargs["labels"]
        assert labels[NAME_LABEL] == "n"
        assert labels[CREATED_AT_LABEL] == "2026-07-11T00:00:00+00:00"
        assert labels[MANAGED_LABEL] == "true"
        assert labels[KIND_LABEL] == KIND_SANDBOX


class TestListManagedContainers:
    """The list exposes net posture and kind, and never guesses (#527)."""

    @patch("sunaba.tools.container._docker")
    def test_exposes_allow_network_and_kind(self, mock_docker: MagicMock) -> None:
        c = _make_container(labels={
            MANAGED_LABEL: "true",
            KIND_LABEL: KIND_SANDBOX,
            NETWORK_LABEL: "true",
        })
        mock_docker.return_value = _make_client([c])

        containers, error = list_managed_containers()

        assert error is None
        assert containers[0]["allow_network"] is True
        assert containers[0]["kind"] == KIND_SANDBOX

    @patch("sunaba.tools.container._docker")
    def test_unlabelled_container_reads_back_as_unknown(
        self, mock_docker: MagicMock
    ) -> None:
        """A container created before #527 has no posture to report -- say so."""
        c = _make_container(labels={MANAGED_LABEL: "true"})
        mock_docker.return_value = _make_client([c])

        containers, _ = list_managed_containers()

        assert containers[0]["allow_network"] is None
        assert containers[0]["kind"] == KIND_SANDBOX

    @patch("sunaba.tools.container._docker")
    def test_labelled_sidecar_is_tagged_proxy(self, mock_docker: MagicMock) -> None:
        c = _make_container(labels={MANAGED_LABEL: "true", KIND_LABEL: KIND_PROXY})
        mock_docker.return_value = _make_client([c])

        containers, _ = list_managed_containers()

        assert containers[0]["kind"] == KIND_PROXY

    @patch("sunaba.tools.container._docker")
    def test_legacy_sidecar_identified_by_container_name(
        self, mock_docker: MagicMock
    ) -> None:
        """The running sidecar predates the label and outlives an upgrade."""
        c = _make_container(
            labels={MANAGED_LABEL: "true"},
            docker_name=PROXY_CONTAINER_NAME,
        )
        mock_docker.return_value = _make_client([c])

        containers, _ = list_managed_containers()

        assert containers[0]["kind"] == KIND_PROXY

    @patch("sunaba.tools.container._docker")
    def test_docker_failure_returns_error(self, mock_docker: MagicMock) -> None:
        client = MagicMock()
        client.containers.list.side_effect = Exception("docker is down")
        mock_docker.return_value = client

        containers, error = list_managed_containers()

        assert containers == []
        assert error is not None and "docker is down" in error

    @patch("sunaba.tools.container.reaper._reap_idle_containers")
    @patch("sunaba.tools.container._docker")
    def test_read_path_does_not_reap(
        self, mock_docker: MagicMock, mock_reap: MagicMock
    ) -> None:
        """The dashboard polls this every 10s -- looking must not destroy."""
        mock_docker.return_value = _make_client([_make_container()])

        list_managed_containers()

        mock_reap.assert_not_called()

    @patch("sunaba.tools.container.reaper._reap_idle_containers")
    @patch("sunaba.tools.container._docker")
    def test_mcp_tool_still_reaps(
        self, mock_docker: MagicMock, mock_reap: MagicMock
    ) -> None:
        """...but the MCP tool keeps its TTL reaping behaviour."""
        mock_reap.return_value = []
        mock_docker.return_value = _make_client([_make_container()])

        result = json.loads(sandbox_list_containers())

        mock_reap.assert_called_once()
        assert result["containers"][0]["kind"] == KIND_SANDBOX


class TestRunIdPerContainer:
    """run_id comes from the journal file, not the in-memory map (#527)."""

    def test_latest_run_id_per_container(self, tmp_path: Path) -> None:
        log = tmp_path / "journal.jsonl"
        log.write_text(
            json.dumps({"container_id": "aaa", "run_id": "run-1", "operation": "initialize"}) + "\n"
            + json.dumps({"container_id": "aaa", "run_id": "run-2", "operation": "exec"}) + "\n"
            + json.dumps({"container_id": "bbb", "run_id": "run-3", "operation": "initialize"}) + "\n"
        )
        with patch("sunaba.journal._JOURNAL_PATH", log):
            run_ids = get_run_id_per_container()

        assert run_ids == {"aaa": "run-2", "bbb": "run-3"}

    def test_stopped_container_dropped(self, tmp_path: Path) -> None:
        log = tmp_path / "journal.jsonl"
        log.write_text(
            json.dumps({"container_id": "aaa", "run_id": "run-1", "operation": "initialize"}) + "\n"
            + json.dumps({"container_id": "aaa", "run_id": "run-1", "operation": "stop"}) + "\n"
        )
        with patch("sunaba.journal._JOURNAL_PATH", log):
            assert get_run_id_per_container() == {}


class TestContainersPage:
    """The page renders Docker's view, sorted by how forgotten a container is."""

    def _get(self, path: str) -> str:
        url = get_dashboard_url()
        assert url is not None
        with urllib.request.urlopen(url + path) as resp:
            assert resp.status == 200
            return resp.read().decode("utf-8")

    def _serve(self, containers: list[dict], run_ids: dict | None = None) -> str:
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch(
                    "sunaba.dashboard.list_managed_containers",
                    return_value=(containers, None),
                ),
                patch(
                    "sunaba.dashboard.get_run_id_per_container",
                    return_value=run_ids or {},
                ),
                patch("sunaba.dashboard.get_active_environments", return_value=[]),
            ):
                return self._get("/containers")
        finally:
            stop_dashboard()

    def test_lists_sandbox_with_net_and_run_link(self) -> None:
        html = self._serve(
            [{
                "container_id": "abc123def456",
                "name": "my-box",
                "kind": KIND_SANDBOX,
                "image": "ghcr.io/x/sandbox@sha256:deadbeef",
                "status": "running",
                "allow_network": True,
                "created_at": "2026-07-11T09:00:00+00:00",
                "age_seconds": 120.0,
                "idle_seconds": 60.0,
                "last_activity_ts": "2026-07-11T09:01:00+00:00",
            }],
            run_ids={"abc123def456": "run-9"},
        )

        assert "my-box" in html
        assert "abc123def456" in html
        assert ">on<" in html
        assert "/trace/run-9" in html
        # the digest is collapsed, not dumped in full
        assert "@sha256:..." in html
        assert "deadbeef" not in html

    def test_unknown_net_posture_is_shown_as_unknown(self) -> None:
        html = self._serve([{
            "container_id": "old123456789",
            "name": None,
            "kind": KIND_SANDBOX,
            "image": "python:3.12",
            "status": "running",
            "allow_network": None,
            "created_at": None,
            "age_seconds": None,
            "idle_seconds": None,
            "last_activity_ts": None,
        }])

        assert "net-unknown" in html
        assert "(unnamed)" in html

    def test_idle_container_flagged_and_sorted_first(self) -> None:
        """The 21-hour stray is the reason the page exists -- it goes on top."""
        def _c(cid: str, idle: float) -> dict:
            return {
                "container_id": cid,
                "name": None,
                "kind": KIND_SANDBOX,
                "image": "python:3.12",
                "status": "running",
                "allow_network": False,
                "created_at": "2026-07-10T09:00:00+00:00",
                "age_seconds": idle,
                "idle_seconds": idle,
                "last_activity_ts": "2026-07-10T09:00:00+00:00",
            }

        html = self._serve([_c("fresh0000001", 60.0), _c("stale0000001", 21 * 3600)])

        assert html.index("stale0000001") < html.index("fresh0000001")
        assert "21.0h" in html
        assert "stale" in html

    def test_sidecar_listed_apart_from_sandboxes(self) -> None:
        html = self._serve([
            {
                "container_id": "sandbox00001",
                "name": "box",
                "kind": KIND_SANDBOX,
                "image": "python:3.12",
                "status": "running",
                "allow_network": False,
                "created_at": None,
                "age_seconds": None,
                "idle_seconds": None,
                "last_activity_ts": None,
            },
            {
                "container_id": "proxy0000001",
                "name": None,
                "kind": KIND_PROXY,
                "image": "sunaba/proxy:latest",
                "status": "running",
                "allow_network": None,
                "created_at": None,
                "age_seconds": None,
                "idle_seconds": None,
                "last_activity_ts": None,
            },
        ])

        sidecar_section = html.split("Sidecars")[1]
        assert "proxy0000001" in sidecar_section
        assert "sandbox00001" not in sidecar_section

    def test_empty_state(self) -> None:
        html = self._serve([])
        assert "No managed containers" in html

    def test_dashboard_links_to_containers_page(self) -> None:
        start_dashboard(host="127.0.0.1", port=0)
        try:
            html = self._get("/")
            assert 'href="/containers"' in html
            # the journal-derived panel it replaces is gone
            assert "Active Environments" not in html
        finally:
            stop_dashboard()
