"""Tests for Issue #298: sandbox_initialize timeout no longer leaks containers.

Three cooperating mechanisms are covered:

- **Labels** — every container created via the secure path is stamped with
  the management label, and ``sandbox_initialize`` additionally stamps a
  ``created_at`` label.
- **Completion marker** — ``record_initialize_complete`` is written only when
  all setup phases finish.
- **Reaper** — ``_reap_orphaned_init_containers`` removes only containers that
  are provably orphaned init attempts (managed + created_at label, no
  completion / exec / stop, older than the grace window).
- **Async wrapper** — ``sandbox_initialize_tool`` runs the sync work inline
  when no Context is supplied, and emits progress notifications otherwise.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from docker.errors import NotFound

from sunaba.security import CREATED_AT_LABEL, MANAGED_LABEL, build_secure_run_kwargs
from sunaba.tools.container import (
    _ORPHAN_GRACE_SECONDS,
    _age_seconds,
    _journal_container_status,
    _reap_orphaned_init_containers,
    sandbox_initialize,
    sandbox_initialize_tool,
)

_IMAGE = "python@sha256:0000000000000000000000000000000000000000000000000000000000000000"


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def _fake_container(cid: str, labels: dict[str, str]) -> MagicMock:
    c = MagicMock()
    c.id = cid
    c.labels = labels
    return c


class TestManagementLabel:
    """Every secure container carries the management label (reaper safety net)."""

    def test_build_secure_run_kwargs_stamps_managed_label(self) -> None:
        kwargs = build_secure_run_kwargs(command="sleep infinity")
        assert kwargs["labels"][MANAGED_LABEL] == "true"

    def test_caller_labels_preserved(self) -> None:
        kwargs = build_secure_run_kwargs(labels={CREATED_AT_LABEL: "2026-01-01T00:00:00+00:00"})
        assert kwargs["labels"][MANAGED_LABEL] == "true"
        assert kwargs["labels"][CREATED_AT_LABEL] == "2026-01-01T00:00:00+00:00"

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_sandbox_initialize_stamps_both_labels(
        self, mock_validate: MagicMock, mock_ensure: MagicMock, mock_docker: MagicMock
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_client.containers.list.return_value = []  # clean reaper
        mock_docker.return_value = mock_client

        result = sandbox_initialize(image=_IMAGE)

        assert result == "abc123def456 [network: off]"
        labels = mock_client.containers.run.call_args[1]["labels"]
        assert labels[MANAGED_LABEL] == "true"
        assert CREATED_AT_LABEL in labels


class TestCompletionMarker:
    @patch("sunaba.tools.container.lifecycle.record_initialize_complete")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_completion_recorded_after_init(
        self,
        mock_validate: MagicMock,
        mock_ensure: MagicMock,
        mock_docker: MagicMock,
        mock_complete: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_client.containers.list.return_value = []
        mock_docker.return_value = mock_client

        sandbox_initialize(image=_IMAGE)

        mock_complete.assert_called_once_with("abc123def456")


class TestAgeSeconds:
    def test_parses_offset_iso(self) -> None:
        age = _age_seconds(_iso(100), datetime.now(timezone.utc))
        assert 95 <= age <= 120

    def test_naive_iso_treated_as_utc(self) -> None:
        naive = (datetime.now(timezone.utc) - timedelta(seconds=50)).replace(tzinfo=None).isoformat()
        age = _age_seconds(naive, datetime.now(timezone.utc))
        assert age is not None and age >= 45

    def test_none_and_garbage_return_none(self) -> None:
        now = datetime.now(timezone.utc)
        assert _age_seconds(None, now) is None
        assert _age_seconds("not-a-date", now) is None


class TestJournalContainerStatus:
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_aggregates_lifecycle(self, mock_journal: MagicMock) -> None:
        mock_journal.return_value = {
            "aaa": {"complete": False, "used": True, "stopped": False, "init_ts": None},
            "bbb": {"complete": True, "used": False, "stopped": False, "init_ts": None},
            "ccc": {"complete": False, "used": False, "stopped": False, "init_ts": "2026-06-29T00:00:00+00:00"},
        }
        status = _journal_container_status()
        assert status["aaa"]["used"] is True
        assert status["bbb"]["complete"] is True
        assert status["ccc"]["complete"] is False and status["ccc"]["used"] is False
        assert status["ccc"]["init_ts"] == "2026-06-29T00:00:00+00:00"


class TestReaper:
    def _client_with(self, *containers: MagicMock) -> MagicMock:
        client = MagicMock()
        client.containers.list.return_value = list(containers)
        return client

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_reaps_orphaned_init(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        # Journal has an old initialize but no completion / exec / stop.
        mock_journal.return_value = {
            "orphan123456": {"complete": False, "used": False, "stopped": False, "init_ts": _iso(_ORPHAN_GRACE_SECONDS + 60)},
        }
        c = _fake_container("orphan123456", {MANAGED_LABEL: "true", CREATED_AT_LABEL: _iso(_ORPHAN_GRACE_SECONDS + 60)})
        client = self._client_with(c)

        reaped = _reap_orphaned_init_containers(client=client)

        assert reaped == ["orphan123456"]
        c.kill.assert_called_once()
        c.remove.assert_called_once_with(force=True)
        mock_stop.assert_called_once_with("orphan123456")

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_skips_completed(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        mock_journal.return_value = {
            "done12345678": {"complete": True, "used": False, "stopped": False, "init_ts": _iso(_ORPHAN_GRACE_SECONDS + 60)},
        }
        c = _fake_container("done12345678", {MANAGED_LABEL: "true", CREATED_AT_LABEL: _iso(_ORPHAN_GRACE_SECONDS + 60)})
        assert _reap_orphaned_init_containers(client=self._client_with(c)) == []
        c.remove.assert_not_called()

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_skips_used(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        mock_journal.return_value = {
            "used12345678": {"complete": False, "used": True, "stopped": False, "init_ts": _iso(_ORPHAN_GRACE_SECONDS + 60)},
        }
        c = _fake_container("used12345678", {MANAGED_LABEL: "true", CREATED_AT_LABEL: _iso(_ORPHAN_GRACE_SECONDS + 60)})
        assert _reap_orphaned_init_containers(client=self._client_with(c)) == []
        c.remove.assert_not_called()

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_skips_within_grace(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        # Created just now — an in-progress init must never be reaped.
        mock_journal.return_value = {}
        c = _fake_container("fresh1234567", {MANAGED_LABEL: "true", CREATED_AT_LABEL: _iso(5)})
        assert _reap_orphaned_init_containers(client=self._client_with(c)) == []
        c.remove.assert_not_called()

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_skips_container_without_created_at(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        # e.g. a test-environment container: managed but not from sandbox_initialize.
        mock_journal.return_value = {}
        c = _fake_container("testenv12345", {MANAGED_LABEL: "true"})
        assert _reap_orphaned_init_containers(client=self._client_with(c)) == []
        c.remove.assert_not_called()

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_pre_record_orphan_aged_from_label(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        # The exact #298 case: timeout before any journal entry was written.
        # Age must come from the created_at label.
        mock_journal.return_value = {}
        c = _fake_container("prerec123456", {MANAGED_LABEL: "true", CREATED_AT_LABEL: _iso(_ORPHAN_GRACE_SECONDS + 120)})
        assert _reap_orphaned_init_containers(client=self._client_with(c)) == ["prerec123456"]
        mock_stop.assert_called_once_with("prerec123456")

    @patch("sunaba.tools.container.reaper.record_stop")
    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_remove_failure_is_swallowed(self, mock_journal: MagicMock, mock_stop: MagicMock) -> None:
        mock_journal.return_value = {}
        c = _fake_container("gone12345678", {MANAGED_LABEL: "true", CREATED_AT_LABEL: _iso(_ORPHAN_GRACE_SECONDS + 60)})
        c.remove.side_effect = NotFound("already gone")
        # NotFound during remove must not raise; container is effectively gone.
        assert _reap_orphaned_init_containers(client=self._client_with(c)) == ["gone12345678"]

    @patch("sunaba.tools.container.reaper.read_container_states")
    def test_list_failure_returns_empty(self, mock_journal: MagicMock) -> None:
        client = MagicMock()
        client.containers.list.side_effect = RuntimeError("docker down")
        assert _reap_orphaned_init_containers(client=client) == []


class TestAsyncWrapper:
    @patch("sunaba.tools.container.lifecycle.sandbox_initialize")
    def test_ctx_none_runs_inline(self, mock_sync: MagicMock) -> None:
        mock_sync.return_value = "abc123def456"
        result = asyncio.run(sandbox_initialize_tool(image=_IMAGE, ctx=None))
        assert result == "abc123def456"
        mock_sync.assert_called_once()

    def test_ctx_reports_progress_during_slow_init(self) -> None:
        ctx = MagicMock()
        ctx.report_progress = AsyncMock()

        def _slow(**kwargs: object) -> str:
            import time as _t
            _t.sleep(0.25)
            return "slowcid12345"

        with patch("sunaba.tools.container.lifecycle.sandbox_initialize", side_effect=_slow), patch(
            "sunaba.tools.container.lifecycle._PROGRESS_INTERVAL_SECONDS", 0.05
        ):
            result = asyncio.run(sandbox_initialize_tool(image=_IMAGE, ctx=ctx))

        assert result == "slowcid12345"
        assert ctx.report_progress.await_count >= 1

    def test_progress_failure_does_not_strand_result(self) -> None:
        ctx = MagicMock()
        ctx.report_progress = AsyncMock(side_effect=RuntimeError("connection lost"))

        def _slow(**kwargs: object) -> str:
            import time as _t
            _t.sleep(0.2)
            return "okcid1234567"

        with patch("sunaba.tools.container.lifecycle.sandbox_initialize", side_effect=_slow), patch(
            "sunaba.tools.container.lifecycle._PROGRESS_INTERVAL_SECONDS", 0.05
        ):
            # Even though report_progress keeps raising, the work's result is returned.
            result = asyncio.run(sandbox_initialize_tool(image=_IMAGE, ctx=ctx))

        assert result == "okcid1234567"
