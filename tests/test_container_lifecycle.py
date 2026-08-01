"""Tests for container lifecycle: sandbox_initialize + run_container_and_exec with clone_repo, pip_extras, timeout."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import (
    EGRESS_NETWORK_NAME,
    ENABLE_EGRESS_PROXY_ENV,
    EgressProxyError,
    EgressProxyRuntime,
)
from sunaba.tools.container import (
    sandbox_initialize,
)


class TestSandboxInitializeCloneRepo:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for sandbox_initialize with clone_repo."""

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_clone_repo_calls_helper(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Cloned owner/repo via network into /tmp/repo/repo in container abc123def456"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert "abc123def456" in result
        assert "Cloned owner/repo via network" in result
        assert "pip install" not in result
        mock_clone.assert_called_once()

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_clone_repo_failure_non_fatal(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.side_effect = ValueError("clone not found")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert result.startswith("abc123def456")
        assert "clone_repo failed" in result

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_without_clone_repo_works_normally(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
        )

        assert result == "abc123def456 [network: off] [advisory: if you intend to edit or publish, call get_workflow_guide first]"
        mock_clone.assert_not_called()

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_clone_dest_custom(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Cloned owner/repo via network..."

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            clone_dest="/tmp/proj",
        )

        args, kwargs = mock_clone.call_args
        assert args[0] is mock_container
        assert args[1] == "abc123def456"
        assert args[2] == "owner/repo"
        assert args[3] == "/tmp/proj"
        assert "open_read_grant" in kwargs

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_network_clone_default_path(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_net_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_net_clone.return_value = (
            "Cloned owner/repo via network into /tmp/repo/repo"
            " in container abc123def456"
        )

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
        )

        assert result.startswith("abc123def456")
        assert "clone_repo failed" not in result
        assert "via network" in result
        mock_net_clone.assert_called_once()


class TestSandboxInitializeCloneRepoPipExtras:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for pip_extras with clone_repo (Issue #245)."""

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_pip_extras_none_skips_install(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        """pip_extras=None skips only the python step (#798): with a python
        manifest present, no pip exec runs and the init result reports the
        pip skip; the manifest probe still runs (npm must be detected even
        when pip is disabled)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"pyproject.toml\n", b"")),  # manifest probe
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            pip_extras=None,
        )

        assert "abc123def456" in result
        # Only the manifest probe exec ran: no pip install command, and the
        # result says the python step was skipped.
        assert mock_container.exec_run.call_count == 1
        assert "pip skipped (pip_extras=None)" in result
        assert "no manifest detected" not in result

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_pip_extras_default_installs_dev(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"pyproject.toml\n", b"")),  # manifest probe
            (0, (b"Installed", b"")),  # pip install
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            allow_network=True,
        )

        assert "abc123def456" in result
        assert mock_container.exec_run.call_count == 2
        call_cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "pip install -e '.[dev]' -q" in call_cmd

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_pip_extras_installed_when_clone_repo_auto_enables_network(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        """clone_repo always auto-enables allow_network=True, so pip
        install is always possible."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"pyproject.toml\n", b"")),  # manifest probe
            (0, (b"Installed", b"")),  # pip install
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            allow_network=False,
        )

        assert "abc123def456" in result
        # Network is auto-enabled by clone_repo, so pip install runs.
        assert mock_container.exec_run.call_count == 2
        call_cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "pip install -e '.[dev]' -q" in call_cmd

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_pip_extras_custom_value(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"pyproject.toml\n", b"")),  # manifest probe
            (0, (b"", b"")),  # pip install
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            pip_extras="[test]",
            allow_network=True,
        )

        call_cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "pip install -e '.[test]' -q" in call_cmd

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_pip_install_failure_non_fatal(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = [
            (0, (b"pyproject.toml\n", b"")),  # manifest probe
            (1, (b"", b"ERROR")),  # pip install fails
        ]
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.return_value = "Clone OK"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            allow_network=True,
        )

        assert "abc123def456" in result
        assert "clone_repo failed" not in result
        # The pip failure is non-fatal and reported in the deps note
        # (format: "deps: pip install -e .[dev] failed (exit N): detail").
        assert "pip install -e .[dev] failed" in result

    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_clone_failure_skips_pip_install(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_clone.side_effect = ValueError("clone not found")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            allow_network=True,
        )

        assert "clone_repo failed" in result
        assert mock_container.exec_run.call_count == 0


class TestSandboxInitializeDepsManifests:
    """Manifest-aware dependency install at init (#798): the dispatcher probes
    the clone dest and runs every applicable installer (pip / npm), notes
    fetch-on-build toolchains, and never runs pip on a repo without a python
    manifest."""

    _IMAGE = "python@sha256:" + "0" * 64

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    def _run_init(self, exec_returns, **kwargs) -> tuple[str, MagicMock]:
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.side_effect = exec_returns
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        with patch("sunaba.tools.container.clone._clone_repo_via_network",
                   return_value="Clone OK"), patch(
            "sunaba.tools.container._docker", return_value=mock_client
        ), patch("sunaba.tools.container.lifecycle._ensure_image"), patch(
            "sunaba.tools.container.lifecycle.validate_image_ref"
        ):
            return sandbox_initialize(
                image=self._IMAGE,
                clone_repo="owner/repo",
                allow_network=True,
                **kwargs,
            ), mock_container

    def test_js_only_with_lockfile_runs_npm_ci(self) -> None:
        """package.json + package-lock.json -> npm ci, no pip command."""
        result, container = self._run_init([
            (0, (b"package.json\npackage-lock.json\n", b"")),  # probe
            (0, (b"added 42 packages", b"")),  # npm ci
        ])
        assert "npm ci ok" in result
        assert "pip" not in result
        calls = [c.args[0][-1] for c in container.exec_run.call_args_list]
        assert calls[0].startswith("cd /workspace && for f in")
        assert calls[1] == "cd /workspace && npm ci"
        assert not any("pip install" in cmd for cmd in calls)

    def test_js_only_without_lockfile_runs_npm_install(self) -> None:
        """package.json without a lockfile -> npm install, no pip command."""
        result, container = self._run_init([
            (0, (b"package.json\n", b"")),  # probe
            (0, (b"added 7 packages in 2s", b"")),  # npm install
        ])
        assert "npm install ok" in result
        assert "pip" not in result
        calls = [c.args[0][-1] for c in container.exec_run.call_args_list]
        assert calls[1] == "cd /workspace && npm install"
        assert not any("pip install" in cmd for cmd in calls)

    def test_js_only_npm_still_runs_with_pip_extras_none(self) -> None:
        """pip_extras=None skips ONLY the python step; npm still runs (#798)."""
        result, container = self._run_init(
            [
                (0, (b"package.json\n", b"")),  # probe
                (0, (b"up to date", b"")),  # npm install
            ],
            pip_extras=None,
        )
        assert "npm install ok" in result
        calls = [c.args[0][-1] for c in container.exec_run.call_args_list]
        assert any(cmd == "cd /workspace && npm install" for cmd in calls)
        assert not any("pip install" in cmd for cmd in calls)

    def test_both_manifests_run_both_installers(self) -> None:
        """pyproject.toml + package.json -> pip and npm both run."""
        result, container = self._run_init([
            (0, (b"pyproject.toml\npackage.json\npackage-lock.json\n", b"")),
            (0, (b"Installed", b"")),  # pip
            (0, (b"added 3 packages", b"")),  # npm ci
        ])
        assert "pip install ok" in result
        assert "npm ci ok" in result
        calls = [c.args[0][-1] for c in container.exec_run.call_args_list]
        assert any("pip install" in cmd for cmd in calls)
        assert calls[2] == "cd /workspace && npm ci"

    def test_go_mod_only_notes_fetch_on_build(self) -> None:
        """go.mod alone: no installer runs; the result says delegated."""
        result, container = self._run_init([
            (0, (b"go.mod\n", b"")),  # probe
        ])
        assert "go.mod detected — delegated to fetch-on-build" in result
        # Only the probe exec ran.
        assert container.exec_run.call_count == 1

    def test_cargo_toml_only_notes_fetch_on_build(self) -> None:
        result, container = self._run_init([
            (0, (b"Cargo.toml\n", b"")),
        ])
        assert "Cargo.toml detected — delegated to fetch-on-build" in result
        assert container.exec_run.call_count == 1

    def test_no_manifest_notes_skipped(self) -> None:
        """No manifest at all: no installer, short skip note in the result."""
        result, container = self._run_init([
            (0, (b"", b"")),  # probe finds nothing
        ])
        assert "no manifest detected — skipped" in result
        assert container.exec_run.call_count == 1
        assert "pip install" not in result

    def test_npm_failure_non_fatal_and_reported(self) -> None:
        """npm ci failure is non-fatal and reported in the deps note."""
        result, container = self._run_init([
            (0, (b"package.json\npackage-lock.json\n", b"")),
            (1, (b"", b"npm ERR! code ENOENT")),
        ])
        assert "npm ci failed (exit 1): npm ERR! code ENOENT" in result
        assert container.exec_run.call_count == 2

class TestSandboxInitializeEgressProxy:
    """Egress-proxy wiring in sandbox_initialize (#358, #509): default-on, fail closed."""

    _IMAGE = "python@sha256:" + "0" * 64
    _CA = b"-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"

    def _runtime(self) -> EgressProxyRuntime:
        return EgressProxyRuntime(
            network_name=EGRESS_NETWORK_NAME,
            proxy_url="http://egress-proxy:8080",
            control_url="http://127.0.0.1:8768",
            ca_pem=self._CA,
        )

    def _client(self) -> tuple[MagicMock, MagicMock]:
        container = MagicMock()
        container.id = "abc123def456abc123def456"
        container.exec_run.return_value = (0, (b"", b""))
        client = MagicMock()
        client.containers.run.return_value = container
        return client, container

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_flag_off_keeps_plain_bridge(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
        client, _ = self._client()
        mock_docker.return_value = client

        result = sandbox_initialize(image=self._IMAGE, allow_network=True)
        assert not result.startswith("Error")
        run_kwargs = client.containers.run.call_args.kwargs
        assert run_kwargs["network_mode"] == "bridge"
        assert "HTTPS_PROXY" not in run_kwargs["environment"]

    @patch("sunaba.tools.container.lifecycle.proxy_lifecycle.install_ca")
    @patch("sunaba.tools.container.lifecycle.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_flag_on_wires_proxy_network_env_and_ca(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        mock_install_ca: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        client, container = self._client()
        mock_docker.return_value = client
        mock_ensure_proxy.return_value = self._runtime()

        result = sandbox_initialize(image=self._IMAGE, allow_network=True)
        assert not result.startswith("Error")

        run_kwargs = client.containers.run.call_args.kwargs
        assert run_kwargs["network"] == EGRESS_NETWORK_NAME
        assert "network_mode" not in run_kwargs
        env = run_kwargs["environment"]
        assert env["HTTPS_PROXY"] == "http://egress-proxy:8080"
        mock_install_ca.assert_called_once_with(container, self._CA)

    @patch("sunaba.tools.container.lifecycle.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_flag_on_fails_closed_when_sidecar_unavailable(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        client, _ = self._client()
        mock_docker.return_value = client
        mock_ensure_proxy.side_effect = EgressProxyError("sidecar image missing")

        result = sandbox_initialize(image=self._IMAGE, allow_network=True)
        assert result.startswith("Error: egress proxy is enabled but unavailable")
        client.containers.run.assert_not_called()

    @patch("sunaba.tools.container.lifecycle.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_flag_on_without_network_skips_proxy(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        client, _ = self._client()
        mock_docker.return_value = client

        result = sandbox_initialize(image=self._IMAGE, allow_network=False)
        assert not result.startswith("Error")
        mock_ensure_proxy.assert_not_called()
        assert client.containers.run.call_args.kwargs["network_mode"] == "none"

    @patch("sunaba.tools.container.lifecycle.proxy_lifecycle.install_ca")
    @patch("sunaba.tools.container.lifecycle.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_ca_install_failure_tears_the_sandbox_down(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        mock_install_ca: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        client, container = self._client()
        mock_docker.return_value = client
        mock_ensure_proxy.return_value = self._runtime()
        mock_install_ca.side_effect = EgressProxyError("update-ca-certificates broke")

        result = sandbox_initialize(image=self._IMAGE, allow_network=True)
        assert result.startswith("Error: egress proxy CA install failed")
        container.remove.assert_called_once_with(force=True)
