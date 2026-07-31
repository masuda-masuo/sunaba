"""Tests for the dashboard module (Issue #44)."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from sunaba.dashboard import (
    _render_health_badge,
    get_dashboard_url,
    start_dashboard,
    stop_dashboard,
)
from sunaba.security import KIND_SANDBOX


def _dashboard_url() -> str:
    url = get_dashboard_url()
    assert url is not None, "dashboard not running"
    return url


class TestDashboard:
    """Tests for dashboard start/stop."""

    def test_start_stop_dashboard(self):
        """Start and stop the dashboard."""
        result = start_dashboard(host="127.0.0.1", port=0)
        assert "started on" in result

        result2 = start_dashboard(host="127.0.0.1", port=0)
        assert "already running" in result2

        result3 = stop_dashboard()
        assert "stopped" in result3

    def test_stop_when_not_running(self):
        """Stopping when not running should indicate so."""
        stop_dashboard()  # ensure stopped
        result = stop_dashboard()
        assert "not running" in result

    def test_dashboard_serves_html(self):
        """Dashboard should serve HTML content on /."""
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with urllib.request.urlopen(_dashboard_url() + "/") as resp:
                assert resp.status == 200
                content = resp.read().decode("utf-8")
                assert "Code Sandbox MCP" in content
                assert "Dashboard" in content
        finally:
            stop_dashboard()

    def test_dashboard_api_runs(self):
        """Dashboard /api/runs should return JSON."""
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with urllib.request.urlopen(_dashboard_url() + "/api/runs") as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert isinstance(data, list)
        finally:
            stop_dashboard()

    def test_dashboard_api_journal(self):
        """Dashboard /api/journal should return JSON array."""
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with urllib.request.urlopen(_dashboard_url() + "/api/journal") as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert isinstance(data, list)
        finally:
            stop_dashboard()

    def test_dashboard_404(self):
        """Dashboard should return 404 for unknown paths."""
        start_dashboard(host="127.0.0.1", port=0)
        try:
            urllib.request.urlopen(_dashboard_url() + "/nonexistent")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        finally:
            stop_dashboard()

    def test_dashboard_trace_page(self):
        """Dashboard /trace/<run_id> should return HTML or 404."""
        start_dashboard(host="127.0.0.1", port=0)
        try:
            urllib.request.urlopen(_dashboard_url() + "/trace/nonexistent")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        finally:
            stop_dashboard()


class TestHealthBadges:
    """Run-health badges in the listings and the trace header (Issue #775)."""

    def _serve(self, path: str) -> str:
        url = get_dashboard_url()
        assert url is not None
        with urllib.request.urlopen(url + path) as resp:
            assert resp.status == 200
            return resp.read().decode("utf-8")

    def test_render_health_badge(self):
        html = _render_health_badge("looping")
        assert 'class="badge health-looping"' in html
        assert ">looping<" in html

    def test_render_health_badge_escapes(self):
        html = _render_health_badge('"><script>')
        assert "health-progressing" in html
        assert "&lt;script&gt;" in html

    def test_run_listing_shows_health_badge(self):
        """Recent Runs rows lead with the run's health badge."""
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run-1", "container_id": "abc", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run-1", "container_id": "abc", "operation": "stop"},
        ]
        run_summary = [{
            "run_id": "run-1",
            "started": "2026-01-01T00:00:00Z",
            "image": "python:3.12",
            "operations": 2,
            "boundary_crossings": 0,
            "vcs_operations": 0,
            "status": "stopped",
        }]
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch("sunaba.dashboard.get_runs", return_value=run_summary),
                patch("sunaba.dashboard.read_journal", return_value=entries),
            ):
                html = self._serve("/")
                assert 'class="badge health-done"' in html
                assert ">done<" in html
        finally:
            stop_dashboard()

    def test_containers_page_shows_health_badge_next_to_run(self):
        """The containers page badges the run each container is attached to."""
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run-9", "container_id": "abc123def456", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run-9", "container_id": "abc123def456", "operation": "stop"},
        ]
        container = {
            "container_id": "abc123def456",
            "name": "my-box",
            "kind": KIND_SANDBOX,
            "image": "python:3.12",
            "status": "running",
            "allow_network": True,
            "created_at": "2026-07-11T09:00:00+00:00",
            "age_seconds": 120.0,
            "idle_seconds": 60.0,
            "last_activity_ts": "2026-07-11T09:01:00+00:00",
        }
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch("sunaba.dashboard.list_managed_containers", return_value=([container], None)),
                patch("sunaba.dashboard.get_run_id_per_container", return_value={"abc123def456": "run-9"}),
                patch("sunaba.dashboard.get_active_environments", return_value=[]),
                patch("sunaba.dashboard.read_journal", return_value=entries),
            ):
                html = self._serve("/containers")
                assert 'class="badge health-done"' in html
                assert "/trace/run-9" in html
        finally:
            stop_dashboard()

    def test_trace_page_header_shows_health_badge(self):
        """The trace page header carries the run's health badge."""
        entries = [
            {"ts": "2026-01-01T00:00:00Z", "run_id": "run-1", "container_id": "abc", "operation": "initialize"},
            {"ts": "2026-01-01T00:00:01Z", "run_id": "run-1", "container_id": "abc", "operation": "stop"},
        ]
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with patch("sunaba.dashboard.read_journal", return_value=entries):
                html = self._serve("/trace/run-1")
                assert "<strong>Health:</strong>" in html
                assert "health-done" in html
        finally:
            stop_dashboard()


# ---------------------------------------------------------------------------
# Issue #776: /api/journal?offset=N diff polling
# ---------------------------------------------------------------------------


class TestJournalDiffEndpoint:
    """Offset-based journal diff endpoint (Issue #776).

    ``/api/journal?offset=N`` returns the complete JSON-lines written after
    byte position N in the live journal, the next byte position to poll
    from, and a rotation flag.  The autouse ``_isolate_journal`` fixture
    redirects ``sunaba.journal._JOURNAL_PATH`` to a per-test tmp dir, so
    these tests read and rotate a throwaway file.
    """

    _ENTRY_1 = {"ts": "2026-01-01T00:00:00Z", "run_id": "run-1", "container_id": "abc", "operation": "initialize", "image": "python:3.12"}
    _ENTRY_2 = {"ts": "2026-01-01T00:00:01Z", "run_id": "run-1", "container_id": "abc", "operation": "exec", "commands": ["echo hi"], "exit_code": 0}
    _ENTRY_3 = {"ts": "2026-01-01T00:00:02Z", "run_id": "run-1", "container_id": "abc", "operation": "stop"}

    def _journal_path(self):
        from sunaba import journal as jmod
        return jmod._JOURNAL_PATH

    def _write(self, *entries: dict) -> None:
        """Append JSON-lines entries to the test-isolated live journal."""
        path = self._journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def _fetch(self, url: str, host: str | None = None) -> tuple[int, dict]:
        """GET *url* and return ``(status, parsed_json)`` (raises on 4xx/5xx)."""
        req = urllib.request.Request(url)
        if host is not None:
            req.add_header("Host", host)
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def _journal_url(self) -> str:
        url = get_dashboard_url()
        assert url is not None, "dashboard not running"
        return url + "/api/journal"

    def _serve_page(self, path: str) -> str:
        url = get_dashboard_url()
        assert url is not None, "dashboard not running"
        with urllib.request.urlopen(url + path) as resp:
            assert resp.status == 200
            return resp.read().decode("utf-8")

    def _embedded_offset(self, html: str) -> int:
        """Extract the poll start offset embedded in a page's live script."""
        m = re.search(r"var offset = (\d+);", html)
        assert m is not None, "live script offset not found in page"
        return int(m.group(1))

    def test_initial_fetch_from_offset_zero(self):
        """offset=0 (or absent) returns every complete line plus the size."""
        self._write(self._ENTRY_1, self._ENTRY_2)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            status, data = self._fetch(self._journal_url() + "?offset=0")
            assert status == 200
            assert data["rotated"] is False
            assert [e["operation"] for e in data["entries"]] == ["initialize", "exec"]
            assert data["next_offset"] == self._journal_path().stat().st_size
        finally:
            stop_dashboard()

    def test_incremental_fetch_returns_only_new_lines(self):
        """Polling from the previous next_offset yields only the new lines."""
        self._write(self._ENTRY_1, self._ENTRY_2)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            _, first = self._fetch(self._journal_url() + "?offset=0")
            offset = first["next_offset"]

            # Nothing new: empty delta, same offset, no rotation.
            _, empty = self._fetch(self._journal_url() + "?offset=%d" % offset)
            assert empty["entries"] == []
            assert empty["rotated"] is False
            assert empty["next_offset"] == offset

            self._write(self._ENTRY_3)
            _, second = self._fetch(self._journal_url() + "?offset=%d" % offset)
            assert [e["operation"] for e in second["entries"]] == ["stop"]
            assert second["rotated"] is False
            assert second["next_offset"] == self._journal_path().stat().st_size
        finally:
            stop_dashboard()

    def test_partial_trailing_line_is_excluded(self):
        """A line without a trailing newline is never returned."""
        self._write(self._ENTRY_1)
        path = self._journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = json.dumps(self._ENTRY_2, ensure_ascii=False)  # no newline
        with open(path, "a", encoding="utf-8") as f:
            f.write(partial)

        start_dashboard(host="127.0.0.1", port=0)
        try:
            status, data = self._fetch(self._journal_url() + "?offset=0")
            assert status == 200
            # Only the complete line; the offset stays at the partial line's
            # start so the next poll re-reads it once it is finished.
            assert [e["operation"] for e in data["entries"]] == ["initialize"]
            assert data["next_offset"] == len(
                json.dumps(self._ENTRY_1, ensure_ascii=False) + "\n"
            )

            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")
            _, data = self._fetch(self._journal_url() + "?offset=%d" % data["next_offset"])
            assert [e["operation"] for e in data["entries"]] == ["exec"]
        finally:
            stop_dashboard()

    def test_rotation_sets_reset_flag(self):
        """When the live file is smaller than the client offset, rotated=True.

        Simulates rotation exactly as :func:`sunaba.journal._rotate_if_needed_unlocked`
        does it: the live file is renamed to ``journal.log.1`` and a fresh
        ``journal.log`` receives new writes.  The response must not try to
        splice the backup into the delta.
        """
        self._write(self._ENTRY_1, self._ENTRY_2)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            _, first = self._fetch(self._journal_url() + "?offset=0")
            offset = first["next_offset"]  # size of the pre-rotation file

            # Rotate: journal.log -> journal.log.1, new journal.log, one write.
            path = self._journal_path()
            from sunaba import journal as jmod
            path.replace(jmod._JOURNAL_BACKUP_PATH)
            self._write(self._ENTRY_3)

            assert path.stat().st_size < offset
            status, data = self._fetch(self._journal_url() + "?offset=%d" % offset)
            assert status == 200
            assert data["rotated"] is True
            assert data["entries"] == []  # no splicing of journal.log.1
            assert data["next_offset"] == path.stat().st_size

            # Reset fetch: offset 0 re-reads the new live file from the top.
            _, data = self._fetch(self._journal_url() + "?offset=0")
            assert data["rotated"] is False
            assert [e["operation"] for e in data["entries"]] == ["stop"]
        finally:
            stop_dashboard()

    def test_rotation_detected_by_generation_when_new_file_larger(self):
        """Rotation is caught even when the replacement file already grew
        past the client's offset.

        The size check alone (`offset > size`) misses this case: the new
        live file is bigger than the old offset, so without the generation
        token the entries in ``[0, offset)`` of the new file would be
        silently skipped.
        """
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            _, first = self._fetch(self._journal_url() + "?offset=0")
            offset = first["next_offset"]
            gen = first["generation"]
            assert gen  # inode of the live file; 0 only on exotic platforms

            # Rotate, then write MORE bytes than the old file held so the
            # size check cannot fire.
            path = self._journal_path()
            from sunaba import journal as jmod
            path.replace(jmod._JOURNAL_BACKUP_PATH)
            self._write(self._ENTRY_1, self._ENTRY_2, self._ENTRY_3)
            assert path.stat().st_size > offset

            _, data = self._fetch(
                self._journal_url() + "?offset=%d&gen=%d" % (offset, gen)
            )
            assert data["rotated"] is True
            assert data["generation"] != gen

            # Reset fetch with the new generation reads the new file whole.
            _, data = self._fetch(
                self._journal_url() + "?offset=0&gen=%d" % data["generation"]
            )
            assert data["rotated"] is False
            assert [e["operation"] for e in data["entries"]] == [
                "initialize", "exec", "stop",
            ]
        finally:
            stop_dashboard()

    def test_generation_stable_without_rotation(self):
        """The same live file keeps the same generation across polls."""
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            _, first = self._fetch(self._journal_url() + "?offset=0")
            self._write(self._ENTRY_2)
            _, second = self._fetch(
                self._journal_url()
                + "?offset=%d&gen=%d" % (first["next_offset"], first["generation"])
            )
            assert second["rotated"] is False
            assert second["generation"] == first["generation"]
            assert [e["operation"] for e in second["entries"]] == ["exec"]
        finally:
            stop_dashboard()

    def test_invalid_gen_rejected(self):
        """A non-integer gen parameter gets a 400."""
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                self._fetch(self._journal_url() + "?offset=0&gen=abc")
            assert exc.value.code == 400
        finally:
            stop_dashboard()

    def test_host_header_rejection(self):
        """A DNS-rebinding Host header is refused with 403 on both shapes."""
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            for path in ("?offset=0", ""):
                with pytest.raises(urllib.error.HTTPError) as exc:
                    self._fetch(self._journal_url() + path, host="evil.example")
                assert exc.value.code == 403
        finally:
            stop_dashboard()

    def test_invalid_offset_rejected(self):
        """Non-integer or negative offsets get a 400."""
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            for bad in ("abc", "-1"):
                with pytest.raises(urllib.error.HTTPError) as exc:
                    self._fetch(self._journal_url() + "?offset=" + bad)
                assert exc.value.code == 400
        finally:
            stop_dashboard()

    def test_trace_view_fragments(self):
        """view=trace&run_id=R adds the phase view and health badge.

        The fragments come from the same aggregation functions the trace
        page itself renders, so the polled header matches a reload.
        """
        self._write(self._ENTRY_1, self._ENTRY_2, self._ENTRY_3)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            _, data = self._fetch(
                self._journal_url() + "?offset=0&view=trace&run_id=run-1"
            )
            assert data["health"] == "done"  # stop entry -> done
            assert data["health_cls"] == "health-done"
            assert "init" in data["phase_view"]
            assert "phase-view" in data["phase_view"]
        finally:
            stop_dashboard()

    def test_containers_view_fragments(self):
        """view=containers adds server-rendered row fragments every poll."""
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch("sunaba.dashboard.list_managed_containers", return_value=([], None)),
                patch("sunaba.dashboard.get_run_id_per_container", return_value={}),
                patch("sunaba.dashboard.get_active_environments", return_value=[]),
            ):
                _, data = self._fetch(self._journal_url() + "?offset=0&view=containers")
            assert "rows_html" in data
            assert "No managed containers" in data["rows_html"]
            assert data["sidecars_html"] == ""
            assert data["error_html"] == ""
        finally:
            stop_dashboard()

    def test_dashboard_view_fragments(self):
        """view=dashboard adds the stats/journal cards and run rows."""
        self._write(self._ENTRY_1, self._ENTRY_2)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            _, data = self._fetch(self._journal_url() + "?offset=0&view=dashboard")
            assert "stats_card" in data and "journal_card" in data
            assert "run_rows" in data
            assert "Total Runs" in data["stats_card"]
            assert "run-1" in data["run_rows"]
        finally:
            stop_dashboard()

    def _write_stale_run(self) -> None:
        """Write a run whose last entry is 20 minutes old (no stop).

        Past the 10-minute stall threshold, and its last operation is not a
        long-running tool call, so the in-flight exemption does not apply:
        it must classify as ``stalled``.
        """
        stale = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        self._write(
            {"ts": stale, "run_id": "run-stall", "container_id": "abc", "operation": "initialize", "image": "python:3.12"},
            {"ts": stale, "run_id": "run-stall", "container_id": "abc", "operation": "exec", "commands": ["true"], "exit_code": 0},
        )

    def test_trace_view_fragments_refresh_without_new_entries(self):
        """Empty-delta polls still deliver time-based health flips (stalled).

        Health classification is time-dependent: a run that stopped writing
        crosses the stall threshold with no journal growth at all, so the
        fragments must be recomputed on every poll -- otherwise the badge
        stays ``progressing`` until a manual reload.
        """
        self._write_stale_run()
        start_dashboard(host="127.0.0.1", port=0)
        try:
            size = self._journal_path().stat().st_size
            _, data = self._fetch(
                self._journal_url() + "?offset=%d&view=trace&run_id=run-stall" % size
            )
            assert data["entries"] == []  # the delta is empty
            assert data["rotated"] is False
            assert data["health"] == "stalled"
            assert data["health_cls"] == "health-stalled"
        finally:
            stop_dashboard()

    def test_dashboard_view_fragments_refresh_without_new_entries(self):
        """The dashboard run rows also flip to stalled on empty deltas."""
        self._write_stale_run()
        start_dashboard(host="127.0.0.1", port=0)
        try:
            size = self._journal_path().stat().st_size
            _, data = self._fetch(self._journal_url() + "?offset=%d&view=dashboard" % size)
            assert data["entries"] == []
            assert 'class="badge health-stalled"' in data["run_rows"]
        finally:
            stop_dashboard()

    def test_giant_offset_rejected(self):
        """Unseekable offsets get a 400, and rotation detection still works.

        A hand-crafted offset like 10**100 passes int() but cannot be seeked
        to (OverflowError).  The handler bounds it to the current file size
        plus the rotation ceiling (plus a one-line margin) and returns 400
        instead of dropping the connection.  A legitimate stale offset just
        above the current size (rotation pending) must still return
        ``rotated``, not 400.
        """
        self._write(self._ENTRY_1)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc:
                self._fetch(self._journal_url() + "?offset=%d" % (10 ** 100))
            assert exc.value.code == 400

            status, data = self._fetch(
                self._journal_url() + "?offset=%d" % (self._journal_path().stat().st_size + 10)
            )
            assert status == 200
            assert data["rotated"] is True
        finally:
            stop_dashboard()

    def test_trace_page_snapshot_offset_excludes_partial_tail(self):
        """The trace page's embedded poll offset excludes a partial tail.

        Rows and the poll start offset come from one snapshot of the live
        file: a half-written trailing line is neither rendered nor covered
        by the embedded offset, and the next poll delivers it once the
        newline arrives.
        """
        self._write(self._ENTRY_1)
        path = self._journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(self._ENTRY_2, ensure_ascii=False))  # no newline

        start_dashboard(host="127.0.0.1", port=0)
        try:
            html = self._serve_page("/trace/run-1")
            offset = self._embedded_offset(html)
            assert offset == len(json.dumps(self._ENTRY_1, ensure_ascii=False) + "\n")
            # Only the complete entry is rendered.
            assert "python:3.12" in html  # initialize row detail
            assert "echo hi" not in html  # partial line not rendered

            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")
            _, data = self._fetch(
                self._journal_url() + "?offset=%d&view=trace&run_id=run-1" % offset
            )
            assert [e["operation"] for e in data["entries"]] == ["exec"]
        finally:
            stop_dashboard()

    def test_trace_page_snapshot_offset_continues_rendered_rows(self):
        """The embedded offset exactly continues the rendered rows.

        No gap: an entry appended after the render is delivered by the next
        poll.  No overlap: entries already rendered are not re-delivered.
        """
        self._write(self._ENTRY_1, self._ENTRY_2)
        start_dashboard(host="127.0.0.1", port=0)
        try:
            html = self._serve_page("/trace/run-1")
            offset = self._embedded_offset(html)
            assert offset == self._journal_path().stat().st_size

            self._write(self._ENTRY_3)
            _, data = self._fetch(
                self._journal_url() + "?offset=%d&view=trace&run_id=run-1" % offset
            )
            assert [e["operation"] for e in data["entries"]] == ["stop"]
        finally:
            stop_dashboard()


class TestLiveUpdatePages:
    """Issue #776: the pages embed the polling script and patch anchors.

    These pin the client side of the contract: every live-updated page
    carries the inline poller (no external assets), the DOM anchors it
    patches, and no meta-refresh full reload.
    """

    def _serve(self, path: str) -> str:
        url = get_dashboard_url()
        assert url is not None
        with urllib.request.urlopen(url + path) as resp:
            assert resp.status == 200
            return resp.read().decode("utf-8")

    def test_dashboard_page_embeds_poller(self):
        start_dashboard(host="127.0.0.1", port=0)
        try:
            html = self._serve("/")
            assert 'id="stats-card"' in html
            assert 'id="journal-card"' in html
            assert 'id="run-rows"' in html
            assert "/api/journal?offset=" in html
            assert 'http-equiv="refresh"' not in html
        finally:
            stop_dashboard()

    def test_containers_page_embeds_poller(self):
        start_dashboard(host="127.0.0.1", port=0)
        try:
            with (
                patch("sunaba.dashboard.list_managed_containers", return_value=([], None)),
                patch("sunaba.dashboard.get_run_id_per_container", return_value={}),
                patch("sunaba.dashboard.get_active_environments", return_value=[]),
            ):
                html = self._serve("/containers")
            assert 'id="container-rows"' in html
            assert 'id="sidecars"' in html
            assert "/api/journal?offset=" in html
            assert 'http-equiv="refresh"' not in html
        finally:
            stop_dashboard()

    def test_trace_page_embeds_poller(self):
        start_dashboard(host="127.0.0.1", port=0)
        try:
            entries = [
                {"ts": "2026-01-01T00:00:00Z", "run_id": "run-1", "container_id": "abc", "operation": "initialize"},
                {"ts": "2026-01-01T00:00:01Z", "run_id": "run-1", "container_id": "abc", "operation": "stop"},
            ]
            with patch("sunaba.dashboard.read_journal", return_value=entries):
                html = self._serve("/trace/run-1")
            assert 'id="trace-rows"' in html
            assert 'id="phase-view"' in html
            assert 'id="health-badge"' in html
            assert "/api/journal?offset=" in html
            assert '"run-1"' in html  # run_id embedded for the poller
        finally:
            stop_dashboard()

    def test_stop_failure_banner_survives_polling(self):
        """The failed-stop banner lives in its own element (#stop-error).

        The poll re-renders #containers-error every cycle from
        _containers_fragments(); if the stop failure shared that element it
        would be wiped one poll after the page loads.
        """
        from sunaba.dashboard import _containers_fragments, _render_containers_page

        with (
            patch("sunaba.dashboard.list_managed_containers", return_value=([], None)),
            patch("sunaba.dashboard.get_run_id_per_container", return_value={}),
            patch("sunaba.dashboard.get_active_environments", return_value=[]),
            patch("sunaba.dashboard.read_journal", return_value=[]),
        ):
            html = _render_containers_page(failed_stop="Error: stop went wrong")
            frag = _containers_fragments()

        # Banner rendered inside its own div, outside the polled element.
        stop_div = html.split('id="stop-error"', 1)[1].split(
            'id="containers-error"', 1
        )[0]
        assert "stop went wrong" in stop_div
        # The polled fragment never carries the stop failure.
        assert "stop went wrong" not in frag["error_html"]
