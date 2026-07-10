"""Tests for the dashboard module (Issue #44)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from sunaba.dashboard import (
    get_dashboard_url,
    start_dashboard,
    stop_dashboard,
)


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
