"""Tests for the /containers Stop button (Issue #528).

This is where the dashboard stops being an observation deck and becomes a
control plane, so the tests care about two things in equal measure: that the
button works, and that nothing else can press it.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

import pytest

from sunaba.dashboard import (
    _CSRF_TOKEN,
    get_dashboard_url,
    start_dashboard,
    stop_dashboard,
)

_UNPUSHED = "Error: Container has 2 unpushed checkpoint(s). Use force=True to override."


def _sandbox(container_id: str = "abc123def456") -> dict:
    return {
        "container_id": container_id,
        "name": "box",
        "kind": "sandbox",
        "image": "python:3.12",
        "status": "running",
        "allow_network": False,
        "created_at": "2026-07-11T09:00:00+00:00",
        "age_seconds": 60.0,
        "idle_seconds": 60.0,
        "last_activity_ts": "2026-07-11T09:01:00+00:00",
    }


def _proxy(container_id: str = "proxy0000001") -> dict:
    return {**_sandbox(container_id), "kind": "proxy", "name": None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface the 303 instead of quietly following it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


@pytest.fixture
def dashboard():
    start_dashboard(host="127.0.0.1", port=0)
    url = get_dashboard_url()
    assert url is not None
    try:
        yield url
    finally:
        stop_dashboard()


def _post(
    url: str,
    fields: dict[str, str],
    host: str | None = None,
) -> tuple[int, str]:
    """POST *fields*; return (status, body).  Redirects are not followed."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if host is not None:
        headers["Host"] = host
    req = urllib.request.Request(
        url + "/containers/stop", data=data, headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


class TestControlPlaneGuards:
    """Only the real page may drive the control plane."""

    def test_post_without_csrf_token_is_refused(self, dashboard: str) -> None:
        with patch("sunaba.dashboard.sandbox_stop") as stop:
            status, _ = _post(dashboard, {"container_id": "abc123def456"})

        assert status == 403
        stop.assert_not_called()

    def test_post_with_wrong_csrf_token_is_refused(self, dashboard: str) -> None:
        with patch("sunaba.dashboard.sandbox_stop") as stop:
            status, _ = _post(
                dashboard,
                {"container_id": "abc123def456", "csrf": "not-the-token"},
            )

        assert status == 403
        stop.assert_not_called()

    def test_post_from_rebound_host_is_refused(self, dashboard: str) -> None:
        """DNS rebinding: right token would not help, the Host gives it away."""
        with patch("sunaba.dashboard.sandbox_stop") as stop:
            status, _ = _post(
                dashboard,
                {"container_id": "abc123def456", "csrf": _CSRF_TOKEN},
                host="attacker.example.com",
            )

        assert status == 403
        stop.assert_not_called()

    def test_host_pin_lifts_when_operator_publishes_the_dashboard(self) -> None:
        """--dashboard-host beyond loopback is a deliberate exposure.

        Pinning Host there would only break the operator's own Stop button:
        they may reach the dashboard under any hostname, and rebinding is moot
        once the address is routable.  The CSRF token still guards the POST.
        """
        from sunaba.dashboard import _host_allowed

        with patch("sunaba.dashboard._dashboard_host", "127.0.0.1"):
            assert _host_allowed("127.0.0.1") is True
            assert _host_allowed("attacker.example.com") is False

        with patch("sunaba.dashboard._dashboard_host", "0.0.0.0"):
            assert _host_allowed("box.internal") is True

    def test_unknown_post_path_404s(self, dashboard: str) -> None:
        req = urllib.request.Request(dashboard + "/nope", data=b"", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 404

    def test_missing_container_id_is_rejected(self, dashboard: str) -> None:
        with patch("sunaba.dashboard.sandbox_stop") as stop:
            status, _ = _post(dashboard, {"csrf": _CSRF_TOKEN})

        assert status == 400
        stop.assert_not_called()


class TestStop:
    """The happy path, and the informed confirmation in front of it."""

    def test_clean_container_stops_without_ceremony(self, dashboard: str) -> None:
        """Nothing unpushed, nothing to warn about -- one click is enough."""
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_sandbox()], None),
            ),
            patch(
                "sunaba.dashboard.sandbox_stop",
                return_value="Container abc123def456 stopped and removed",
            ) as stop,
        ):
            status, _ = _post(
                dashboard,
                {"container_id": "abc123def456", "csrf": _CSRF_TOKEN},
            )

        assert status == 303
        stop.assert_called_once_with("abc123def456", force=False)

    def test_unpushed_work_surfaces_the_real_warning(self, dashboard: str) -> None:
        """The confirmation says what is at stake, not "are you sure?"."""
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_sandbox()], None),
            ),
            patch("sunaba.dashboard.sandbox_stop", return_value=_UNPUSHED) as stop,
        ):
            status, body = _post(
                dashboard,
                {"container_id": "abc123def456", "csrf": _CSRF_TOKEN},
            )

        assert status == 200
        assert "2 unpushed checkpoint(s)" in body
        assert "Stop anyway" in body
        assert "abc123def456" in body
        # the tool's advice to its LLM caller has no place on a human page
        assert "force=True" not in body
        # the container is still alive: sandbox_stop refused rather than stopped
        stop.assert_called_once_with("abc123def456", force=False)

    def test_force_stop_from_the_confirmation(self, dashboard: str) -> None:
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_sandbox()], None),
            ),
            patch(
                "sunaba.dashboard.sandbox_stop",
                return_value="Container abc123def456 stopped and removed",
            ) as stop,
        ):
            status, _ = _post(
                dashboard,
                {
                    "container_id": "abc123def456",
                    "csrf": _CSRF_TOKEN,
                    "force": "true",
                },
            )

        assert status == 303
        stop.assert_called_once_with("abc123def456", force=True)

    def test_other_errors_are_shown_on_the_page(self, dashboard: str) -> None:
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_sandbox()], None),
            ),
            patch(
                "sunaba.dashboard.sandbox_stop",
                return_value="Error: container abc123def456 not found",
            ),
            patch("sunaba.dashboard.get_run_id_per_container", return_value={}),
            patch("sunaba.dashboard.get_active_environments", return_value=[]),
        ):
            status, body = _post(
                dashboard,
                {"container_id": "abc123def456", "csrf": _CSRF_TOKEN},
            )

        assert status == 500
        assert "not found" in body

    def test_sidecar_cannot_be_stopped(self, dashboard: str) -> None:
        """It restarts itself, and it is every sandbox's egress -- not a row action."""
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_proxy()], None),
            ),
            patch("sunaba.dashboard.sandbox_stop") as stop,
        ):
            status, _ = _post(
                dashboard,
                {"container_id": "proxy0000001", "csrf": _CSRF_TOKEN},
            )

        assert status == 400
        stop.assert_not_called()

    def test_only_a_listed_sandbox_can_be_stopped(self, dashboard: str) -> None:
        """The guard is an allowlist, so a snapshot that missed a container
        refuses the stop rather than passing an unclassified id to sandbox_stop.
        """
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_sandbox("abc123def456")], None),
            ),
            patch("sunaba.dashboard.sandbox_stop") as stop,
        ):
            status, _ = _post(
                dashboard,
                {"container_id": "notlisted123", "csrf": _CSRF_TOKEN},
            )

        assert status == 400
        stop.assert_not_called()


class TestStopButtonRendering:
    def test_button_on_sandbox_rows_only(self, dashboard: str) -> None:
        with (
            patch(
                "sunaba.dashboard.list_managed_containers",
                return_value=([_sandbox(), _proxy()], None),
            ),
            patch("sunaba.dashboard.get_run_id_per_container", return_value={}),
            patch("sunaba.dashboard.get_active_environments", return_value=[]),
        ):
            with urllib.request.urlopen(dashboard + "/containers") as resp:
                body = resp.read().decode("utf-8")

        table, sidecars = body.split("Sidecars", 1)
        assert table.count('action="/containers/stop"') == 1
        assert _CSRF_TOKEN in table
        assert "Stop" not in sidecars

    def test_read_only_pages_carry_no_stop_form(self, dashboard: str) -> None:
        with urllib.request.urlopen(dashboard + "/") as resp:
            body = resp.read().decode("utf-8")

        assert "/containers/stop" not in body


class TestThreadedServer:
    def test_server_is_threaded(self) -> None:
        """A Docker round trip on POST must not stall the page behind it."""
        from http.server import ThreadingHTTPServer

        import sunaba.dashboard as d

        start_dashboard(host="127.0.0.1", port=0)
        try:
            assert isinstance(d._dashboard_server, ThreadingHTTPServer)
        finally:
            stop_dashboard()


def test_csrf_token_is_unguessable() -> None:
    assert len(_CSRF_TOKEN) >= 32
