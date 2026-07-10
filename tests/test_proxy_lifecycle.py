"""Tests for the egress-proxy sidecar lifecycle (#358, Epic #353)."""
from __future__ import annotations

from unittest.mock import MagicMock

import docker.errors
import pytest

from sunaba import proxy_lifecycle as pl
from sunaba.proxy import (
    ALLOWED_EGRESS_HOSTS_ENV,
    ALLOWED_REPOS_ENV,
    CONTROL_HOST_ENV,
    CONTROL_PORT_ENV,
    CONTROL_SECRET_ENV,
    PROXY_TOKEN_ENV,
)
from sunaba.proxy_client import CONTROL_URL_ENV
from sunaba.security import MANAGED_LABEL

CA_PEM = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"


def _fresh_client() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Docker client mock with no pre-existing network or sidecar."""
    client = MagicMock()
    client.networks.get.side_effect = docker.errors.NotFound("no network")
    network = MagicMock()
    client.networks.create.return_value = network
    client.containers.get.side_effect = docker.errors.NotFound("no container")
    proxy_container = MagicMock()
    proxy_container.exec_run.return_value = (0, CA_PEM)
    proxy_container.attrs = {
        "HostConfig": {
            "PortBindings": {"9099/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8768"}]}
        }
    }
    client.containers.run.return_value = proxy_container
    return client, network, proxy_container


def _running_sidecar(secret: str | None) -> MagicMock:
    """Mock of an already-running sidecar container."""
    container = MagicMock()
    container.status = "running"
    container.id = "a" * 64
    env = [f"{CONTROL_SECRET_ENV}={secret}"] if secret else []
    container.attrs = {
        "Config": {"Env": env},
        "HostConfig": {
            "PortBindings": {"9099/tcp": [{"HostIp": "127.0.0.1", "HostPort": "9999"}]}
        },
    }
    container.exec_run.return_value = (0, CA_PEM)
    return container


@pytest.fixture(autouse=True)
def _stub_fingerprint_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the #405 source-drift probe and #432 pin lookup hermetic here.

    ``ensure_egress_proxy`` now calls ``fetch_proxy_fingerprint`` against the
    (fake) control URL; without this stub every lifecycle test would attempt a
    real socket connect -- and on a host that actually runs the sidecar on the
    published port it could hit a live proxy.  Default it to ``None`` ("cannot
    compare", so no warning); the dedicated drift tests override it.  Also zero
    the readiness wait so a ``None`` result returns at once instead of polling
    for :data:`pl._FINGERPRINT_READY_WAIT_SECONDS` seconds each ensure call.

    Likewise default ``load_proxy_pin`` (#432) to ``None`` so the sidecar image
    resolves to the local ``:latest`` tag regardless of whether a packaged
    ``proxy_pin.json`` happens to be present in the test environment (it will be
    once CI's auto-PR lands one).  ``TestResolveProxyImage`` overrides this.
    """
    monkeypatch.setattr(pl, "fetch_proxy_fingerprint", lambda config: None)
    monkeypatch.setattr(pl, "_FINGERPRINT_READY_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(pl, "load_proxy_pin", lambda: None)


class TestEgressProxyEnabled:
    """Flag parsing for SUNABA_ENABLE_EGRESS_PROXY."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True ", "banana"])
    def test_truthy(self, value: str) -> None:
        assert pl.egress_proxy_enabled({pl.ENABLE_EGRESS_PROXY_ENV: value}) is True

    @pytest.mark.parametrize("value", ["0", "false", "off"])
    def test_falsy(self, value: str) -> None:
        assert pl.egress_proxy_enabled({pl.ENABLE_EGRESS_PROXY_ENV: value}) is False

    def test_absent(self) -> None:
        assert pl.egress_proxy_enabled({}) is True


class TestEnsureEgressProxyFresh:
    """First start: network + sidecar created and wired."""

    def test_creates_internal_network(self) -> None:
        client, _, _ = _fresh_client()
        pl.ensure_egress_proxy(client, env={})
        kwargs = client.networks.create.call_args.kwargs
        assert client.networks.create.call_args.args[0] == pl.EGRESS_NETWORK_NAME
        assert kwargs["internal"] is True
        assert kwargs["labels"] == {MANAGED_LABEL: "true"}

    def test_starts_sidecar_with_control_api(self) -> None:
        client, network, container = _fresh_client()
        env: dict[str, str] = {}
        runtime = pl.ensure_egress_proxy(client, env=env)

        run_kwargs = client.containers.run.call_args.kwargs
        assert client.containers.run.call_args.args[0] == "sunaba/proxy:latest"
        assert run_kwargs["name"] == pl.PROXY_CONTAINER_NAME
        proxy_env = run_kwargs["environment"]
        assert proxy_env[CONTROL_PORT_ENV] == "9099"
        assert proxy_env[CONTROL_HOST_ENV] == "0.0.0.0"
        assert proxy_env[CONTROL_SECRET_ENV]  # generated, non-empty
        assert run_kwargs["ports"] == {"9099/tcp": ("127.0.0.1", 8768)}
        assert run_kwargs["labels"] == {MANAGED_LABEL: "true"}
        network.connect.assert_called_once_with(container, aliases=[pl.PROXY_NETWORK_ALIAS])

        assert runtime.network_name == pl.EGRESS_NETWORK_NAME
        assert runtime.proxy_url == "http://egress-proxy:8080"
        assert runtime.ca_pem == CA_PEM

    def test_exports_control_url_and_secret_for_publish(self) -> None:
        client, _, _ = _fresh_client()
        env: dict[str, str] = {}
        runtime = pl.ensure_egress_proxy(client, env=env)
        assert env[CONTROL_URL_ENV] == runtime.control_url == "http://127.0.0.1:8768"
        secret = client.containers.run.call_args.kwargs["environment"][CONTROL_SECRET_ENV]
        assert env[CONTROL_SECRET_ENV] == secret

    def test_passes_allowlist_and_token_through(self) -> None:
        client, _, _ = _fresh_client()
        env = {
            ALLOWED_REPOS_ENV: "owner/repo",
            ALLOWED_EGRESS_HOSTS_ENV: "proxy.golang.org",
            PROXY_TOKEN_ENV: "tok",
        }
        pl.ensure_egress_proxy(client, env=env)
        proxy_env = client.containers.run.call_args.kwargs["environment"]
        assert proxy_env[ALLOWED_REPOS_ENV] == "owner/repo"
        assert proxy_env[ALLOWED_EGRESS_HOSTS_ENV] == "proxy.golang.org"
        assert proxy_env[PROXY_TOKEN_ENV] == "tok"

    def test_image_and_port_overrides(self) -> None:
        client, _, container = _fresh_client()
        container.attrs = {"HostConfig": {"PortBindings": {}}}
        env = {pl.PROXY_IMAGE_ENV: "custom/proxy:v1", pl.CONTROL_HOST_PORT_ENV: "9001"}
        runtime = pl.ensure_egress_proxy(client, env=env)
        assert client.containers.run.call_args.args[0] == "custom/proxy:v1"
        assert client.containers.run.call_args.kwargs["ports"] == {
            "9099/tcp": ("127.0.0.1", 9001)
        }
        assert runtime.control_url == "http://127.0.0.1:9001"

    def test_wraps_docker_errors(self) -> None:
        client, _, _ = _fresh_client()
        client.containers.run.side_effect = RuntimeError("no such image")
        with pytest.raises(pl.EgressProxyError, match="no such image"):
            pl.ensure_egress_proxy(client, env={})

    def test_uses_ghcr_pin_when_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # With a CI-published pin and no env override, the sidecar starts from
        # the digest-pinned GHCR ref (#432), not the local :latest tag.
        pinned = f"ghcr.io/x/proxy@sha256:{'a' * 64}"
        monkeypatch.setattr(pl, "load_proxy_pin", lambda: pinned)
        client, _, _ = _fresh_client()
        pl.ensure_egress_proxy(client, env={})
        assert client.containers.run.call_args.args[0] == pinned


class TestResolveProxyImage:
    """Sidecar image precedence: env override -> GHCR pin -> local tag (#432)."""

    _PIN = f"ghcr.io/masuda-masuo/sunaba/proxy@sha256:{'b' * 64}"

    def test_env_override_wins_over_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pl, "load_proxy_pin", lambda: self._PIN)
        image = pl._resolve_proxy_image({pl.PROXY_IMAGE_ENV: "custom/proxy:v1"})
        assert image == "custom/proxy:v1"

    def test_pin_used_when_no_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pl, "load_proxy_pin", lambda: self._PIN)
        assert pl._resolve_proxy_image({}) == self._PIN

    def test_falls_back_to_local_tag_without_pin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pl, "load_proxy_pin", lambda: None)
        assert pl._resolve_proxy_image({}) == pl._DEFAULT_PROXY_IMAGE

    def test_blank_override_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A blank/whitespace env value must not shadow the pin.
        monkeypatch.setattr(pl, "load_proxy_pin", lambda: self._PIN)
        assert pl._resolve_proxy_image({pl.PROXY_IMAGE_ENV: "  "}) == self._PIN


class TestCertsVolume:
    """CA persistence via the named certs volume (#400)."""

    def test_sidecar_mounts_certs_volume(self) -> None:
        client, _, _ = _fresh_client()
        pl.ensure_egress_proxy(client, env={})
        run_kwargs = client.containers.run.call_args.kwargs
        assert run_kwargs["volumes"] == {
            pl.CERTS_VOLUME_NAME: {"bind": "/certs", "mode": "rw"}
        }

    def test_missing_volume_created_with_managed_label(self) -> None:
        client, _, _ = _fresh_client()
        client.volumes.get.side_effect = docker.errors.NotFound("no volume")
        pl.ensure_egress_proxy(client, env={})
        client.volumes.create.assert_called_once_with(
            pl.CERTS_VOLUME_NAME, labels={MANAGED_LABEL: "true"}
        )

    def test_existing_volume_reused(self) -> None:
        client, _, _ = _fresh_client()
        pl.ensure_egress_proxy(client, env={})
        client.volumes.get.assert_called_once_with(pl.CERTS_VOLUME_NAME)
        client.volumes.create.assert_not_called()

    def test_volume_create_failure_fails_closed(self) -> None:
        client, _, _ = _fresh_client()
        client.volumes.get.side_effect = docker.errors.NotFound("no volume")
        client.volumes.create.side_effect = RuntimeError("volume quota exceeded")
        with pytest.raises(pl.EgressProxyError, match="volume quota exceeded"):
            pl.ensure_egress_proxy(client, env={})
        client.containers.run.assert_not_called()

    def test_reused_sidecar_does_not_touch_volume(self) -> None:
        client, _, _ = _fresh_client()
        client.containers.get.side_effect = None
        client.containers.get.return_value = _running_sidecar("s3cret")
        pl.ensure_egress_proxy(client, env={})
        client.containers.run.assert_not_called()
        client.volumes.get.assert_not_called()
        client.volumes.create.assert_not_called()


class TestEnsureEgressProxyReuse:
    """Idempotency: a running sidecar is reused, a dead one replaced."""

    def test_reuses_running_sidecar(self) -> None:
        client, _, _ = _fresh_client()
        existing = _running_sidecar(secret="known-secret")
        client.containers.get.side_effect = None
        client.containers.get.return_value = existing

        env: dict[str, str] = {}
        runtime = pl.ensure_egress_proxy(client, env=env)
        client.containers.run.assert_not_called()
        assert env[CONTROL_SECRET_ENV] == "known-secret"
        # Published port recovered from the surviving container, not the env.
        assert runtime.control_url == "http://127.0.0.1:9999"

    def test_replaces_exited_sidecar(self) -> None:
        client, _, _ = _fresh_client()
        existing = _running_sidecar(secret="old")
        existing.status = "exited"
        client.containers.get.side_effect = None
        client.containers.get.return_value = existing

        pl.ensure_egress_proxy(client, env={})
        existing.remove.assert_called_once_with(force=True)
        client.containers.run.assert_called_once()

    def test_replaces_sidecar_with_unrecoverable_secret(self) -> None:
        client, _, _ = _fresh_client()
        existing = _running_sidecar(secret=None)
        client.containers.get.side_effect = None
        client.containers.get.return_value = existing

        pl.ensure_egress_proxy(client, env={})
        existing.remove.assert_called_once_with(force=True)
        client.containers.run.assert_called_once()


class TestWaitForCA:
    """Polling for mitmproxy's generated CA."""

    def test_returns_pem_after_retries(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [(1, b""), (1, b""), (0, CA_PEM)]
        assert pl._wait_for_ca(container, timeout=5.0, interval=0.0) == CA_PEM

    def test_times_out(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (1, b"No such file")
        with pytest.raises(pl.EgressProxyError, match="did not appear"):
            pl._wait_for_ca(container, timeout=0.0, interval=0.0)


class TestSandboxWiring:
    """Env/network/CA pieces a sandbox container gets."""

    def _runtime(self) -> pl.EgressProxyRuntime:
        return pl.EgressProxyRuntime(
            network_name=pl.EGRESS_NETWORK_NAME,
            proxy_url="http://egress-proxy:8080",
            control_url="http://127.0.0.1:8768",
            ca_pem=CA_PEM,
        )

    def test_sandbox_proxy_env(self) -> None:
        env = pl.sandbox_proxy_env(self._runtime())
        assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://egress-proxy:8080"
        # ::1 included so IPv6-loopback traffic is not misrouted via the proxy
        # (kept in sync with proxy.py's _LOOPBACK_HOSTS).
        assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"
        assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca-certificates.crt"
        assert env["NODE_EXTRA_CA_CERTS"] == pl.CA_CERT_PATH_IN_SANDBOX

    def test_sandbox_env_never_contains_control_secret(self) -> None:
        env = pl.sandbox_proxy_env(self._runtime())
        assert CONTROL_SECRET_ENV not in env
        assert CONTROL_URL_ENV not in env

    def test_apply_network_swaps_bridge_for_internal_network(self) -> None:
        run_kwargs = {"network_mode": "bridge", "detach": True}
        result = pl.apply_network(run_kwargs, self._runtime())
        assert "network_mode" not in result
        assert result["network"] == pl.EGRESS_NETWORK_NAME
        assert run_kwargs["network_mode"] == "bridge"  # input not mutated


class TestInstallCA:
    """CA installation into the sandbox trust store."""

    def test_installs_via_update_ca_certificates(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, b"")
        pl.install_ca(container, CA_PEM)
        put_dir, tar_bytes = container.put_archive.call_args.args
        assert put_dir == "/usr/local/share/ca-certificates"
        assert CA_PEM in tar_bytes
        container.exec_run.assert_called_once_with(["update-ca-certificates"], user="root")

    def test_falls_back_to_bundle_append(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [(1, b"not found"), (0, b"")]
        pl.install_ca(container, CA_PEM)
        fallback_call = container.exec_run.call_args_list[1]
        assert fallback_call.kwargs["user"] == "root"
        assert "ca-certificates.crt" in fallback_call.args[0][2]

    def test_raises_when_both_paths_fail(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [(1, b"boom"), (1, b"still boom")]
        with pytest.raises(pl.EgressProxyError, match="could not install proxy CA"):
            pl.install_ca(container, CA_PEM)

    def test_raises_when_put_archive_is_refused(self) -> None:
        container = MagicMock()
        container.put_archive.return_value = False
        with pytest.raises(pl.EgressProxyError, match="put_archive"):
            pl.install_ca(container, CA_PEM)
        container.exec_run.assert_not_called()


class TestSourceDriftWarning:
    """_warn_on_source_drift: sidecar vs installed proxy.py fingerprint (#405)."""

    def test_warns_on_mismatch(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(pl, "PROXY_SOURCE_FINGERPRINT", "a" * 64)
        monkeypatch.setattr(pl, "fetch_proxy_fingerprint", lambda config: "b" * 64)
        with caplog.at_level("WARNING"):
            pl._warn_on_source_drift("http://127.0.0.1:1", "sekret")
        assert any("does not match" in r.getMessage() for r in caplog.records)

    def test_silent_when_fingerprints_agree(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(pl, "PROXY_SOURCE_FINGERPRINT", "same")
        monkeypatch.setattr(pl, "fetch_proxy_fingerprint", lambda config: "same")
        with caplog.at_level("WARNING"):
            pl._warn_on_source_drift("http://x", "s")
        assert not caplog.records

    def test_silent_when_remote_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Old proxy without /version, or unreachable sidecar -> None -> no warn.
        monkeypatch.setattr(pl, "PROXY_SOURCE_FINGERPRINT", "local")
        monkeypatch.setattr(pl, "fetch_proxy_fingerprint", lambda config: None)
        with caplog.at_level("WARNING"):
            pl._warn_on_source_drift("http://x", "s")
        assert not caplog.records

    def test_polls_until_control_api_ready(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Regression for the #431 review: on first sidecar creation the control
        # API may not accept requests the instant container.start() returns, so
        # a single probe would miss the drift (false negative on the very deploy
        # that caused it).  The readiness poll retries until /version answers.
        calls = {"n": 0}

        def flaky(config: object) -> str | None:
            calls["n"] += 1
            return "b" * 64 if calls["n"] >= 3 else None  # not ready twice, then ready

        monkeypatch.setattr(pl, "PROXY_SOURCE_FINGERPRINT", "a" * 64)
        monkeypatch.setattr(pl, "fetch_proxy_fingerprint", flaky)
        monkeypatch.setattr(pl, "_FINGERPRINT_READY_WAIT_SECONDS", 5.0)
        monkeypatch.setattr(pl, "_FINGERPRINT_POLL_INTERVAL_SECONDS", 0.0)
        with caplog.at_level("WARNING"):
            pl._warn_on_source_drift("http://x", "s")
        assert calls["n"] == 3
        assert any("does not match" in r.getMessage() for r in caplog.records)

    def test_short_circuits_when_local_uncomputable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Empty local fingerprint = "cannot compare": no warn, and no probe.
        probed: list[object] = []
        monkeypatch.setattr(pl, "PROXY_SOURCE_FINGERPRINT", "")
        monkeypatch.setattr(
            pl, "fetch_proxy_fingerprint", lambda config: probed.append(config)
        )
        with caplog.at_level("WARNING"):
            pl._warn_on_source_drift("http://x", "s")
        assert not caplog.records
        assert not probed

    def test_ensure_egress_proxy_swallows_probe_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A blowing-up probe must never stop a sandbox from starting.
        client, _, _ = _fresh_client()

        def boom(config: object) -> str:
            raise RuntimeError("probe blew up")

        monkeypatch.setattr(pl, "PROXY_SOURCE_FINGERPRINT", "local")
        monkeypatch.setattr(pl, "fetch_proxy_fingerprint", boom)
        # Must return normally despite the probe raising.
        runtime = pl.ensure_egress_proxy(client, env={})
        assert runtime.network_name == pl.EGRESS_NETWORK_NAME
