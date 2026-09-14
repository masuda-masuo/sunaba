"""Contract tests for startup diagnostics CLI (Issue #906).

Seams for implementation briefing:
  probe_docker(timeout) -> check dict  (patches docker.from_env)
  probe_egress_proxy(timeout) -> check dict  (patches docker + urllib.request)
  probe_sunaba_service(timeout) -> check dict  (patches shutil.which + subprocess.run)
  run_probe(name, timeout) -> check dict  (owns real process deadline)
  diagnose(timeout=3.0) -> report dict  (assembles run_probe, computes ready)
  main()  (CLI entry, patches diagnose + sys.argv)
"""

from __future__ import annotations

import errno
import http.server
import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_client import CONTROL_SECRET_ENV, CONTROL_TOKEN_HEADER, CONTROL_URL_ENV
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV

_NAMES = ("docker", "egress_proxy", "sunaba_service")
_STATUSES = {"ok", "error", "unknown", "skipped"}
_MARKER = "X-Test-Marker-906-Unique"


@pytest.fixture()
def diagnose_mod():
    """Import sunaba.diagnose; absent module -> red, not skip."""
    import importlib

    return importlib.import_module("sunaba.diagnose")


@pytest.fixture(autouse=True)
def _clean_env():
    """Strip proxy env vars so each test starts clean."""
    keys = (
        ENABLE_EGRESS_PROXY_ENV,
        CONTROL_URL_ENV,
        CONTROL_SECRET_ENV,
    )
    removed = {k: os.environ.pop(k) for k in keys if k in os.environ}
    yield
    os.environ.update(removed)


@pytest.fixture()
def control_env(monkeypatch):
    """Explicit URL and secret for control-API tests only."""
    url = "http://127.0.0.1:9099"
    secret = "test-control-secret-906"
    monkeypatch.setenv(CONTROL_URL_ENV, url)
    monkeypatch.setenv(CONTROL_SECRET_ENV, secret)
    return {"url": url, "secret": secret}


# -- helpers ----------------------------------------------------------------


def _mk(name, status="ok", **kw):
    d = {
        "name": name,
        "status": status,
        "detail": f"{name} detail",
        "next_action": f"{name} action",
    }
    d.update(kw)
    return d


def _block_forever_probe(_timeout: float) -> dict:
    """Picklable probe that ignores its budget and blocks indefinitely."""
    while True:
        time.sleep(60)


@contextmanager
def _stalling_server():
    """Local fake HTTP server that accepts POST and never responds.

    Yields ``(url, connects)`` where *connects* holds the monotonic time of
    each POST arrival.  The handler deliberately never answers on its own --
    it unblocks only when the test's ``finally`` sets *stop_event* -- so the
    probe's own deadline machinery (urllib's socket read timeout, or
    ``run_probe``'s kill) is the only thing that can free the CLI.  That
    makes the window from *connects* to the CLI's exit a deterministic
    measure of deadline enforcement that does not include however long the
    CLI spent importing docker-py under CPU load.
    """
    stop_event = threading.Event()
    connects: list[float] = []

    class _StallingHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            connects.append(time.monotonic())
            stop_event.wait()

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StallingHandler)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    try:
        yield f"http://127.0.0.1:{port}", connects
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        srv_thread.join(timeout=1)


class TestStallingServerContract:
    """The deadline test synchronizes on the probe's connect time, so the
    fake server must record it and must never answer on its own -- a server
    that eventually responded would let a hung probe "finish" and mask a
    broken deadline."""

    def test_records_connect_and_never_responds(self):
        import socket

        with _stalling_server() as (url, connects):
            host, _, port = url.removeprefix("http://").partition(":")
            with socket.create_connection((host, int(port)), timeout=2) as sock:
                sock.sendall(
                    b"POST /version HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}"
                )
                sock.settimeout(1.0)
                with pytest.raises(TimeoutError):
                    sock.recv(1)
            assert len(connects) == 1


# -- smoke (real subprocess) ------------------------------------------------


class TestSmoke:
    def test_help_exits_zero(self):
        r = subprocess.run(
            [sys.executable, "-m", "sunaba.diagnose", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert r.returncode == 0
        assert "usage" in r.stdout.lower() or "help" in r.stdout.lower()

    @pytest.mark.parametrize("bad", ["0", "-1", "inf", "nan"])
    def test_invalid_timeout_exits_two(self, bad):
        r = subprocess.run(
            [sys.executable, "-m", "sunaba.diagnose", "--json", "--timeout", bad],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert r.returncode == 2, f"--timeout {bad} should exit 2, got {r.returncode}"

    def test_unknown_flag_exits_two(self):
        r = subprocess.run(
            [sys.executable, "-m", "sunaba.diagnose", "--bogus"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert r.returncode == 2


# -- probe_docker ------------------------------------------------------------


class TestProbeDocker:
    def test_success(self, diagnose_mod):
        import docker.errors

        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            c = MagicMock()
            mock_from_env.return_value = c
            c.ping.return_value = True
            c.version.return_value = {"Version": "24.0.0"}
            c.containers.get.side_effect = docker.errors.NotFound("no")
            check = diagnose_mod.probe_docker(timeout=3)
        assert check["status"] == "ok"
        assert check["endpoint"]  # required, non-empty
        detail = json.dumps(check)
        assert "password" not in detail.lower()
        # Read-only assertions at leaf boundary
        c.containers.run.assert_not_called()
        c.containers.create.assert_not_called()
        c.images.pull.assert_not_called()
        c.networks.create.assert_not_called()

    @pytest.mark.parametrize(
        "exc,errno_val,kind",
        [
            (FileNotFoundError, errno.ENOENT, "unreachable"),
            (ConnectionRefusedError, errno.ECONNREFUSED, "unreachable"),
            (PermissionError, errno.EACCES, "permission"),
            (TimeoutError, 0, "timeout"),
            (RuntimeError, 0, "unknown"),
        ],
    )
    def test_error_classification(self, diagnose_mod, exc, errno_val, kind):
        with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
            err = exc()
            if errno_val:
                err.errno = errno_val
            mock_from_env.side_effect = err
            check = diagnose_mod.probe_docker(timeout=3)
        assert check["status"] == "error"
        assert check["error_kind"] == kind
        assert check["endpoint"]  # required even on failure
        assert exc.__name__ not in check["detail"]

    @pytest.mark.parametrize("wrap_mode", ["cause", "context", "args"])
    def test_docker_exception_wrapping_permission_error(self, diagnose_mod, wrap_mode):
        """DockerException wrapping PermissionError preserves permission classification and redacts marker."""
        import docker.errors

        perm_err = PermissionError(errno.EACCES, f"Permission denied with secret {_MARKER}")
        if wrap_mode == "cause":
            dock_err = docker.errors.DockerException(f"Daemon access error with secret {_MARKER}")
            dock_err.__cause__ = perm_err
        elif wrap_mode == "context":
            dock_err = docker.errors.DockerException(f"Daemon access error with secret {_MARKER}")
            dock_err.__context__ = perm_err
        else:  # args
            dock_err = docker.errors.DockerException(perm_err, f"secret {_MARKER}")

        with patch("sunaba.diagnose.docker.from_env", side_effect=dock_err):
            check = diagnose_mod.probe_docker(timeout=3)
        assert check["status"] == "error"
        assert check["error_kind"] == "permission"
        assert check["endpoint"]
        assert _MARKER not in json.dumps(check)
        assert "PermissionError" not in check["detail"]

    def test_docker_exception_marker_not_leaked(self, diagnose_mod):
        """Arbitrary DockerException containing secret marker never leaks it in report."""
        import docker.errors

        exc = docker.errors.DockerException(f"API failure exposing {_MARKER}")
        with patch("sunaba.diagnose.docker.from_env", side_effect=exc):
            check = diagnose_mod.probe_docker(timeout=3)
        assert check["status"] == "error"
        assert _MARKER not in json.dumps(check)

    def test_credentials_stripped_from_endpoint(self, diagnose_mod):
        import docker.errors

        with patch.dict("os.environ", {"DOCKER_HOST": "tcp://admin:secret@myhost:2376?q=1#f"}):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                check = diagnose_mod.probe_docker(timeout=3)
        ep = check["endpoint"]
        assert "secret" not in ep
        assert "admin" not in ep
        assert "myhost" in ep
        assert "?" not in ep and "#" not in ep


# -- probe_egress_proxy ------------------------------------------------------


class TestProbeEgressProxy:
    def test_disabled_skipped(self, diagnose_mod):
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=False):
            check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "skipped"

    @pytest.mark.parametrize("val", ["false", "0", "off", "no"])
    def test_disabled_proxy_env_recognized_values(self, diagnose_mod, val):
        """Recognized falsy env values for SUNABA_ENABLE_EGRESS_PROXY skip probe without mocking egress_proxy_enabled."""
        with patch.dict("os.environ", {ENABLE_EGRESS_PROXY_ENV: val}):
            check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "skipped"

    def test_sidecar_found_via_get(self, diagnose_mod):
        """containers.get (named seam, read-only) -> running and healthy sidecar present."""
        assert os.environ.get(CONTROL_URL_ENV) is None  # Sidecar tests must keep URL absent
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                mock_ct = MagicMock()
                mock_ct.attrs = {
                    "State": {
                        "Running": True,
                        "Status": "running",
                        "Health": {"Status": "healthy"},
                    }
                }
                c.containers.get.return_value = mock_ct
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "ok"
        c.containers.get.assert_called_once()  # named seam, not list
        # Read-only assertions at leaf boundary
        c.containers.run.assert_not_called()
        c.containers.create.assert_not_called()
        mock_ct.start.assert_not_called()
        mock_ct.remove.assert_not_called()
        c.images.pull.assert_not_called()
        c.networks.create.assert_not_called()

    def test_sidecar_stopped_with_stale_health_not_ok(self, diagnose_mod):
        """Stopped container with stale Health.Status='healthy' must not be ok."""
        assert os.environ.get(CONTROL_URL_ENV) is None  # Sidecar tests must keep URL absent
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                mock_ct = MagicMock()
                mock_ct.attrs = {
                    "State": {
                        "Running": False,
                        "Status": "exited",
                        "Health": {"Status": "healthy"},
                    }
                }
                c.containers.get.return_value = mock_ct
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] != "ok"
        assert check["status"] in ("error", "unknown")

    def test_sidecar_missing(self, diagnose_mod):
        import docker.errors

        assert os.environ.get(CONTROL_URL_ENV) is None
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "error"
        assert check["next_action"]

    def test_sidecar_no_health(self, diagnose_mod):
        assert os.environ.get(CONTROL_URL_ENV) is None
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                mock_ct = MagicMock()
                mock_ct.attrs = {
                    "State": {
                        "Running": True,
                        "Status": "running",
                        "Health": None,
                    }
                }
                c.containers.get.return_value = mock_ct
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "unknown"

    def test_docker_down_yields_unknown(self, diagnose_mod):
        assert os.environ.get(CONTROL_URL_ENV) is None
        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                err = FileNotFoundError()
                err.errno = errno.ENOENT
                mock_from_env.side_effect = err
                check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "unknown"

    def test_control_url_valid_fingerprint(self, diagnose_mod, control_env):
        """Credentialed POST /version with control token yielding valid fingerprint -> ok."""
        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = json.dumps({"proxy_fingerprint": "abc"}).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                with patch("sunaba.diagnose.urllib.request.urlopen", return_value=mock_resp) as u:
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "ok"
        req = u.call_args[0][0]
        assert req.full_url.endswith("/version")
        assert req.get_method() == "POST"
        # Canonical accessor/items check, not wrong literal membership
        headers = {k.lower(): v for k, v in req.header_items()}
        assert headers.get(CONTROL_TOKEN_HEADER.lower()) == control_env["secret"]

    def test_control_url_auth_denied(self, diagnose_mod, control_env):
        import urllib.error

        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                with patch("sunaba.diagnose.urllib.request.urlopen") as u:
                    u.side_effect = urllib.error.HTTPError(
                        url=control_env["url"] + "/version",
                        code=403,
                        msg="Forbidden",
                        hdrs=None,
                        fp=MagicMock(read=lambda: b"secret-denied-error-body"),
                    )
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] == "error"
        assert check.get("error_kind") in ("auth", "permission", "forbidden")
        assert "secret-denied-error-body" not in json.dumps(check)

    def test_control_url_malformed_body(self, diagnose_mod, control_env):
        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = b"sensitive raw error details not json"
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                with patch("sunaba.diagnose.urllib.request.urlopen", return_value=mock_resp):
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] in ("error", "unknown")
        assert "sensitive raw error details" not in json.dumps(check)

    @pytest.mark.parametrize("payload", [{"other": True}, {"proxy_fingerprint": ""}])
    def test_control_url_no_fingerprint(self, diagnose_mod, control_env, payload):
        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = json.dumps(payload).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                with patch("sunaba.diagnose.urllib.request.urlopen", return_value=mock_resp):
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert check["status"] in ("error", "unknown")

    def test_control_secret_never_leaked(self, diagnose_mod, control_env):
        import urllib.error

        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                with patch("sunaba.diagnose.urllib.request.urlopen") as u:
                    u.side_effect = urllib.error.URLError("down")
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert control_env["secret"] not in json.dumps(check)

    def test_control_response_body_not_leaked(self, diagnose_mod, control_env):
        import docker.errors

        with patch("sunaba.proxy_lifecycle.egress_proxy_enabled", return_value=True):
            with patch("sunaba.diagnose.docker.from_env") as mock_from_env:
                c = MagicMock()
                mock_from_env.return_value = c
                c.ping.return_value = True
                c.version.return_value = {"Version": "24.0.0"}
                c.containers.get.side_effect = docker.errors.NotFound("no")
                mock_resp = MagicMock()
                mock_resp.status = 200
                mock_resp.read.return_value = json.dumps({"proxy_fingerprint": "x", "payload": _MARKER}).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                with patch("sunaba.diagnose.urllib.request.urlopen", return_value=mock_resp):
                    check = diagnose_mod.probe_egress_proxy(timeout=3)
        assert _MARKER not in json.dumps(check)


# -- probe_sunaba_service ----------------------------------------------------


class TestProbeSunabaService:
    def test_missing_systemctl_skipped(self, diagnose_mod):
        with patch("sunaba.diagnose.shutil.which", return_value=None):
            check = diagnose_mod.probe_sunaba_service(timeout=3)
        assert check["status"] == "skipped"

    def test_active_ok(self, diagnose_mod):
        with patch("sunaba.diagnose.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("sunaba.diagnose.subprocess.run") as m:
                m.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
                check = diagnose_mod.probe_sunaba_service(timeout=3)
        assert check["status"] == "ok"

    @pytest.mark.parametrize("code,stdout", [(3, "inactive\n"), (3, "failed\n")])
    def test_inactive_failed(self, diagnose_mod, code, stdout):
        with patch("sunaba.diagnose.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("sunaba.diagnose.subprocess.run") as m:
                m.return_value = MagicMock(returncode=code, stdout=stdout, stderr="")
                check = diagnose_mod.probe_sunaba_service(timeout=3)
        assert check["status"] != "ok"
        action = check["next_action"].lower()
        assert "journalctl" in action or "systemctl" in action

    def test_no_shiori(self, diagnose_mod):
        with patch("sunaba.diagnose.shutil.which", return_value="/usr/bin/systemctl"):
            with patch("sunaba.diagnose.subprocess.run") as m:
                m.return_value = MagicMock(returncode=0, stdout="active\n", stderr="")
                check = diagnose_mod.probe_sunaba_service(timeout=3)
        assert "shiori" not in json.dumps(check).lower()


# -- aggregation: diagnose() -------------------------------------------------


class TestAggregation:
    def test_ready_docker_ok_proxy_ok(self, diagnose_mod):
        with patch.object(
            diagnose_mod,
            "run_probe",
            side_effect=[
                _mk("docker", "ok"),
                _mk("egress_proxy", "ok"),
                _mk("sunaba_service", "ok"),
            ],
        ):
            assert diagnose_mod.diagnose(timeout=3)["ready"] is True

    def test_ready_docker_ok_proxy_skipped(self, diagnose_mod):
        with patch.object(
            diagnose_mod,
            "run_probe",
            side_effect=[
                _mk("docker", "ok"),
                _mk("egress_proxy", "skipped"),
                _mk("sunaba_service", "unknown"),
            ],
        ):
            assert diagnose_mod.diagnose(timeout=3)["ready"] is True

    def test_not_ready_docker_fails(self, diagnose_mod):
        with patch.object(
            diagnose_mod,
            "run_probe",
            side_effect=[
                _mk("docker", "error"),
                _mk("egress_proxy", "unknown"),
                _mk("sunaba_service", "ok"),
            ],
        ):
            assert diagnose_mod.diagnose(timeout=3)["ready"] is False

    def test_not_ready_proxy_fails(self, diagnose_mod):
        with patch.object(
            diagnose_mod,
            "run_probe",
            side_effect=[
                _mk("docker", "ok"),
                _mk("egress_proxy", "error"),
                _mk("sunaba_service", "ok"),
            ],
        ):
            assert diagnose_mod.diagnose(timeout=3)["ready"] is False

    def test_service_error_does_not_block_readiness(self, diagnose_mod):
        with patch.object(
            diagnose_mod,
            "run_probe",
            side_effect=[
                _mk("docker", "ok"),
                _mk("egress_proxy", "ok"),
                _mk("sunaba_service", "error"),
            ],
        ):
            assert diagnose_mod.diagnose(timeout=3)["ready"] is True

    def test_systemd_active_docker_fails_not_ready(self, diagnose_mod):
        """Active service + Docker failure -> ready=False."""
        with patch.object(
            diagnose_mod,
            "run_probe",
            side_effect=[
                _mk("docker", "error", error_kind="unreachable"),
                _mk("egress_proxy", "unknown"),
                _mk("sunaba_service", "ok"),
            ],
        ):
            assert diagnose_mod.diagnose(timeout=3)["ready"] is False

    def test_check_names_ordered(self, diagnose_mod):
        with patch.object(diagnose_mod, "run_probe", side_effect=[_mk(n) for n in _NAMES]):
            report = diagnose_mod.diagnose(timeout=3)
        assert [c["name"] for c in report["checks"]] == list(_NAMES)

    def test_check_keys(self, diagnose_mod):
        with patch.object(diagnose_mod, "run_probe", side_effect=[_mk(n) for n in _NAMES]):
            report = diagnose_mod.diagnose(timeout=3)
        for c in report["checks"]:
            assert {"name", "status", "detail", "next_action"} <= set(c.keys())
            assert c["status"] in _STATUSES

    def test_invalid_timeout(self, diagnose_mod):
        for bad in (0, -1, float("nan"), float("inf")):
            with pytest.raises((ValueError, SystemExit)):
                diagnose_mod.diagnose(timeout=bad)

    def test_default_timeout_is_three(self, diagnose_mod):
        import inspect

        assert inspect.signature(diagnose_mod.diagnose).parameters["timeout"].default == 3

    def test_no_env_dump(self, diagnose_mod):
        with patch.dict("os.environ", {"MY_SECRET": "XYZ"}):
            with patch.object(diagnose_mod, "run_probe", side_effect=[_mk(n) for n in _NAMES]):
                report = diagnose_mod.diagnose(timeout=3)
        assert "MY_SECRET" not in json.dumps(report)


# -- deadline enforcement (real process timeout) -----------------------------


class TestDeadline:
    # ``python -m sunaba.diagnose`` boots the interpreter and imports
    # sunaba.diagnose + docker-py before it probes anything.  That import is
    # not what this test measures, and on this 4-CPU container it stretches
    # from ~0.9s idle to 6.2-8.7s under 4-5x CPU oversubscription (measured).
    # This budget is therefore deliberately loose: a probe that is never
    # unblocked cannot hide behind it, because the stalling server never
    # answers, so a hung probe hangs the CLI until this budget trips.
    _CLI_START_BUDGET_S = 20.0
    # From the probe's connect to the CLI's exit: urllib's socket read
    # timeout (== --timeout, 0.3s) or run_probe's deadline (--timeout +
    # 0.5s) fires, then the two remaining probes and the JSON print.  That
    # window measured 0.34-0.38s idle and 0.39-0.99s under 4-5x CPU
    # oversubscription; 5s is a meaningful "the deadline cut the probe off
    # promptly" bound that no longer counts the import.
    _POST_CONNECT_BUDGET_S = 5.0

    def test_stalled_child_killed(self, diagnose_mod, monkeypatch):
        """A picklable probe that ignores its budget forces parent poll/SIGKILL."""
        monkeypatch.setitem(diagnose_mod._PROBES, "blocking_test", _block_forever_probe)
        real_process = diagnose_mod.multiprocessing.Process
        created = []

        def tracked_process(*args, **kwargs):
            process = real_process(*args, **kwargs)
            created.append(process)
            return process

        started = time.monotonic()
        with patch.object(diagnose_mod.multiprocessing, "Process", side_effect=tracked_process):
            result = diagnose_mod.run_probe("blocking_test", timeout=0.05)
        elapsed = time.monotonic() - started

        assert result["status"] == "error"
        assert result["error_kind"] == "timeout"
        assert 0.45 <= elapsed < 3.0, f"Unexpected parent deadline duration: {elapsed:.2f}s"
        assert len(created) == 1
        child = created[0]
        assert child.exitcode is not None, "Timed-out probe child was not reaped"
        assert not child.is_alive(), "Timed-out probe child is still alive"

    def test_stalled_http_probe_times_out(self):
        """An HTTP probe's socket timeout is covered separately from parent SIGKILL."""
        with _stalling_server() as (url, connects):
            env = os.environ.copy()
            env[CONTROL_URL_ENV] = url
            env[ENABLE_EGRESS_PROXY_ENV] = "true"
            env["DOCKER_HOST"] = "unix:///tmp/nonexistent_sunaba_test_docker.sock"

            pgid = None
            p = None
            t0 = time.monotonic()
            try:
                p = subprocess.Popen(
                    [sys.executable, "-m", "sunaba.diagnose", "--json", "--timeout", "0.3"],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    preexec_fn=os.setsid,
                )
                pgid = p.pid
                stdout, stderr = p.communicate(timeout=self._CLI_START_BUDGET_S)
                elapsed = time.monotonic() - t0

                assert p.returncode == 1, f"Expected exit code 1, got {p.returncode}; stderr: {stderr}"
                assert connects, (
                    "Probe never reached the stalling server, so the deadline path was "
                    f"never exercised; stderr: {stderr}"
                )
                # Measure from the probe's connect, not from Popen: everything
                # before the connect is interpreter/import overhead whose
                # duration depends on machine load, while everything after it
                # is the deadline-relevant window this test asserts.
                post_connect = time.monotonic() - connects[0]
                assert post_connect < self._POST_CONNECT_BUDGET_S, (
                    f"Probe deadline not enforced: CLI exited {post_connect:.2f}s after "
                    f"the probe connected (budget {self._POST_CONNECT_BUDGET_S}s); "
                    f"total {elapsed:.2f}s"
                )
                assert stdout.strip(), f"Expected JSON output from CLI, got empty stdout. stderr: {stderr}"
                data = json.loads(stdout)
                assert data["ready"] is False
                proxy_checks = [c for c in data["checks"] if c["name"] == "egress_proxy"]
                assert len(proxy_checks) == 1
                assert proxy_checks[0]["status"] == "error"
                assert proxy_checks[0].get("error_kind") == "timeout"

                # Check that no child process remains dangling in the process group
                deadline = time.monotonic() + 1.0
                dangling = True
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                        time.sleep(0.05)
                    except ProcessLookupError:
                        dangling = False
                        break
                assert not dangling, "Dangling child process detected after timeout"
            finally:
                # Clean the whole process group (the CLI and anything it
                # failed to reap), then reap the CLI itself if it is still a
                # zombie after a communicate() timeout.
                if pgid is not None:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if p is not None and p.poll() is None:
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass


# -- CLI main() --------------------------------------------------------------


class TestMain:
    @staticmethod
    def _report(ready, docker_status="ok"):
        return {
            "checks": [
                {
                    "name": "docker",
                    "status": docker_status,
                    "detail": "",
                    "next_action": "",
                    "endpoint": "/var/run/docker.sock",
                },
                {
                    "name": "egress_proxy",
                    "status": "skipped",
                    "detail": "",
                    "next_action": "",
                },
                {
                    "name": "sunaba_service",
                    "status": "skipped",
                    "detail": "",
                    "next_action": "",
                },
            ],
            "ready": ready,
        }

    def test_exit_zero_when_ready(self, diagnose_mod, capsys):
        with patch.object(diagnose_mod, "diagnose", return_value=self._report(True)):
            with patch("sys.argv", ["sunaba.diagnose", "--json"]):
                with pytest.raises(SystemExit) as e:
                    diagnose_mod.main()
        assert e.value.code == 0

    def test_exit_one_when_not_ready(self, diagnose_mod, capsys):
        with patch.object(diagnose_mod, "diagnose", return_value=self._report(False, "error")):
            with patch("sys.argv", ["sunaba.diagnose", "--json"]):
                with pytest.raises(SystemExit) as e:
                    diagnose_mod.main()
        assert e.value.code == 1

    def test_json_output_valid(self, diagnose_mod, capsys):
        with patch.object(diagnose_mod, "diagnose", return_value=self._report(True)):
            with patch("sys.argv", ["sunaba.diagnose", "--json"]):
                with pytest.raises(SystemExit):
                    diagnose_mod.main()
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["ready"] is True
        assert len(parsed["checks"]) == 3

    def test_human_readable_default(self, diagnose_mod, capsys):
        with patch.object(diagnose_mod, "diagnose", return_value=self._report(True)):
            with patch("sys.argv", ["sunaba.diagnose"]):
                with pytest.raises(SystemExit):
                    diagnose_mod.main()
        out = capsys.readouterr().out.lower()
        assert "docker" in out
