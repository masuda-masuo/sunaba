"""Tests for the dashboard module (Issue #44)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest.mock import patch

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
