"""Tests for sandbox_initialize branch= parameter and _setup_branch (Issue #675b)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.container import (
    _resolve_default_branch,
    _setup_branch,
    run_container_and_exec,
    sandbox_initialize,
)


def _make_container_mock(exec_returns: list[tuple[int, tuple[bytes, bytes]]]):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, (stdout, stderr) in exec_returns
    ]
    return container


class TestSetupBranch:
    """Tests for _setup_branch."""

    def test_success_authenticated(self):
        """Authenticated path: gh repo clone with -- -b <branch>."""
        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"feature-x\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ) as mock_default, patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            result = _setup_branch(
                container, "abc123def456", "owner/repo", "feature-x",
                "/tmp/repo", "[dev]", authenticated=True,
            )

        assert "Branch feature-x" in result
        assert "/tmp/repo" in result
        assert "abc123" in result
        # Verify the clone command uses gh with -b
        clone_cmd = container.exec_run.call_args_list[0][0][0][-1]
        assert "gh repo clone owner/repo" in clone_cmd
        assert "-b feature-x" in clone_cmd
        mock_default.assert_called_once_with("owner/repo", token=None)

    def test_success_anonymous(self):
        """Anonymous path: git clone -b <branch> over HTTPS."""
        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"feature-y\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            result = _setup_branch(
                container, "abc123def456", "owner/repo", "feature-y",
                "/tmp/repo", "[dev]", authenticated=False,
            )

        assert "Branch feature-y" in result
        clone_cmd = container.exec_run.call_args_list[0][0][0][-1]
        assert "git clone" in clone_cmd
        assert "-b feature-y" in clone_cmd
        assert "https://github.com/owner/repo.git" in clone_cmd
        assert "gh " not in clone_cmd

    def test_clone_failure_for_nonexistent_branch(self):
        """A non-existent branch must fail with an error naming the branch.

        git clone -b will fail with an informative message when the
        branch does not exist on the remote.
        """
        container = _make_container_mock([
            (1, (b"", b"fatal: Remote branch non-existent-branch not found in upstream origin")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ):
            with pytest.raises(RuntimeError) as exc:
                _setup_branch(
                    container, "abc123def456", "owner/repo",
                    "non-existent-branch", "/tmp/repo",
                    authenticated=False,
                )

        assert "non-existent-branch" in str(exc.value)
        assert "Failed to clone" in str(exc.value)

    def test_install_failure_non_fatal(self):
        """pip install failure is logged but does not abort the setup."""
        container = _make_container_mock([
            (0, (b"Cloned\n", b"")),
            (0, (b"feature-z\n", b"")),
            (0, (b"", b"")),
            (1, (b"", b"install failed")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ) as mock_logger, patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            result = _setup_branch(
                container, "abc123def456", "owner/repo", "feature-z",
                "/tmp/repo", "[dev]", authenticated=False,
            )

        assert "Branch feature-z" in result
        mock_logger.warning.assert_called()
        assert any(
            "pip install deps failed" in call[0][0]
            for call in mock_logger.warning.call_args_list
        )

    def test_pip_extras_none_skips_install(self):
        """pip_extras=None means no pip install exec call."""
        # 2 exec calls: clone, meta write (default branch resolved via API,
        # not via in-container exec)
        container = _make_container_mock([
            (0, (b"Cloned\n", b"")),
            (0, (b"feature-z\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            _setup_branch(
                container, "abc123def456", "owner/repo", "feature-z",
                "/tmp/repo", pip_extras=None, authenticated=False,
            )

        # 2 exec calls: clone + meta write (no pip install)
        assert container.exec_run.call_count >= 2

    def test_anonymous_git_clone_without_token(self):
        """Issue #333: no token means anonymous git clone (public works)."""
        container = _make_container_mock([
            (0, (b"", b"")),
            (0, (b"feature\n", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            _setup_branch(
                container, "abc123def456", "owner/repo", "feature",
                "/tmp/repo", authenticated=False,
            )

        cmd = container.exec_run.call_args_list[0][0][0][-1]
        assert "git clone" in cmd
        assert "https://github.com/owner/repo.git" in cmd
        assert "GIT_TERMINAL_PROMPT=0" in cmd
        assert "gh repo clone" not in cmd

    def test_authenticated_gh_clone(self):
        """authenticated=True means gh repo clone."""
        container = _make_container_mock([
            (0, (b"", b"")),
            (0, (b"feature\n", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            _setup_branch(
                container, "abc123def456", "owner/repo", "feature",
                "/tmp/repo", authenticated=True,
            )

        cmd = container.exec_run.call_args_list[0][0][0][-1]
        assert "gh repo clone owner/repo" in cmd
        assert "-b feature" in cmd

    def test_clone_succeeds_but_working_tree_on_wrong_branch(self):
        """Post-clone verification: if git clone lands on the wrong branch
        (e.g. the default branch when -b names a non-existent branch and
        git exits 0 anyway), the mismatch must be caught and reported."""
        container = _make_container_mock([
            (0, (b"", b"")),
            (0, (b"main\n", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ):
            with pytest.raises(RuntimeError) as exc:
                _setup_branch(
                    container, "abc123def456", "owner/repo",
                    "feature-x", "/tmp/repo", pip_extras=None,
                    authenticated=False,
                )

        assert "Branch mismatch" in str(exc.value)
        assert "'feature-x'" in str(exc.value)
        assert "'main'" in str(exc.value)


class TestSetupBranchReadGrant:
    """open_read_grant lets the anonymous branch checkout work for private
    repos (#419), the same mechanism _clone_repo_via_network uses."""

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
    def test_success_is_journaled(self, mock_record):
        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"feature\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            result = _setup_branch(
                container, "abc123def456", "owner/repo", "feature",
                "/tmp/repo", open_read_grant=True, authenticated=False,
            )

        assert "Branch feature" in result
        mock_record.assert_called_once_with(
            "abc123def456",
            "setup_branch",
            "repo=owner/repo branch=feature dest=/tmp/repo proxy_read_grant=True",
            approved=True,
        )

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
    def test_clone_failure_is_journaled_with_approved_false(self, mock_record):
        container = _make_container_mock([
            (1, (b"", b"fatal: could not read Username")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ):
            with pytest.raises(RuntimeError):
                _setup_branch(
                    container, "abc123def456", "owner/repo", "feature",
                    "/tmp/repo", open_read_grant=True, authenticated=False,
                )

        mock_record.assert_called_once_with(
            "abc123def456",
            "setup_branch",
            "repo=owner/repo branch=feature dest=/tmp/repo proxy_read_grant=True",
            approved=False,
        )

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
    def test_authenticated_path_ignores_open_read_grant(self, mock_record):
        """authenticated=True (in-container gh token) never needs the proxy
        read grant, even if the caller passes open_read_grant=True."""
        container = _make_container_mock([
            (0, (b"Cloned\n", b"")),
            (0, (b"feature\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            _setup_branch(
                container, "abc123def456", "owner/repo", "feature",
                "/tmp/repo", authenticated=True, open_read_grant=True,
            )

        mock_record.assert_not_called()

    @patch("sunaba.tools.container.clone.record_boundary_crossing")
    def test_no_read_grant_is_not_journaled(self, mock_record):
        """Default (open_read_grant=False) anonymous checkout is unaffected."""
        container = _make_container_mock([
            (0, (b"Cloned\n", b"")),
            (0, (b"feature\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone._resolve_default_branch",
            return_value="main",
        ), patch(
            "sunaba.tools.container.clone._resolve_vcs_token",
            return_value="",
        ), patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            _setup_branch(
                container, "abc123def456", "owner/repo", "feature",
                "/tmp/repo", authenticated=False,
            )

        mock_record.assert_not_called()

    @patch("sunaba.tools.container.clone._resolve_vcs_token")
    @patch("urllib.request.urlopen")
    def test_token_resolved_once_and_reused(self, mock_urlopen, mock_resolve_token):
        """#436: a broker-backed _resolve_vcs_token() must be called once and
        the same value reused for both the default-branch API call and the
        read grant."""
        mock_resolve_token.return_value = "ghs_minted"
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"default_branch": "main"}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        container = _make_container_mock([
            (0, (b"Cloned\n", b"")),
            (0, (b"feature\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "sunaba.tools.container.clone.logger"
        ), patch(
            "sunaba.tools.container.clone.record_copy"
        ):
            result = _setup_branch(
                container, "abc123def456", "owner/repo", "feature",
                "/tmp/repo", open_read_grant=True, authenticated=False,
            )

        assert "Branch feature" in result
        assert mock_resolve_token.call_count == 1
        # Same token reaches the GitHub API request (default-branch resolution)
        request = mock_urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer ghs_minted"


class TestResolveDefaultBranch:
    """Tests for _resolve_default_branch."""

    @staticmethod
    def _mock_urlopen_response(payload: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    @patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_returns_default_branch(self, mock_urlopen, mock_token):
        mock_urlopen.return_value = self._mock_urlopen_response(
            {"default_branch": "main"}
        )

        result = _resolve_default_branch("owner/repo")
        assert result == "main"

        request = mock_urlopen.call_args.args[0]
        assert request.full_url == "https://api.github.com/repos/owner/repo"
        assert not request.has_header("Authorization")

    @patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="ghs_tok")
    @patch("urllib.request.urlopen")
    def test_attaches_host_token_when_available(self, mock_urlopen, mock_token):
        mock_urlopen.return_value = self._mock_urlopen_response(
            {"default_branch": "main"}
        )

        _resolve_default_branch("owner/repo")

        request = mock_urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer ghs_tok"

    @patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_http_error_becomes_runtime_error(self, mock_urlopen, mock_token):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo",
            404,
            "Not Found",
            None,
            None,
        )

        with pytest.raises(RuntimeError, match="HTTP 404"):
            _resolve_default_branch("owner/repo")

    @patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_missing_default_branch_raises(self, mock_urlopen, mock_token):
        mock_urlopen.return_value = self._mock_urlopen_response({})

        with pytest.raises(RuntimeError, match="no default_branch"):
            _resolve_default_branch("owner/repo")

    def test_invalid_repo_rejected_before_any_request(self):
        with pytest.raises(ValueError):
            _resolve_default_branch("owner/repo/evil?x=1")

    @patch("sunaba.tools.container.clone._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_passed_token_used_instead_of_reresolving(self, mock_urlopen, mock_token):
        """A caller-provided token is used as-is; _resolve_vcs_token not called."""
        mock_urlopen.return_value = self._mock_urlopen_response(
            {"default_branch": "develop"}
        )

        result = _resolve_default_branch("owner/repo", token="custom_token")
        assert result == "develop"

        request = mock_urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer custom_token"


class TestSandboxInitializeBranchParam:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    @patch("sunaba.tools.container.lifecycle._setup_branch")
    def test_branch_calls_setup(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.return_value = "Branch feature-x \u2192 /workspace in container abc123"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            branch="feature-x",
            allow_network=False,
        )

        assert "abc123def456" in result
        assert "Branch feature-x" in result
        mock_setup.assert_called_once()
        args = mock_setup.call_args[0]
        assert args[2] == "owner/repo"
        assert args[3] == "feature-x"
        run_kwargs = mock_client.containers.run.call_args[1]
        # No token in the container environment
        assert "GITHUB_TOKEN" not in run_kwargs.get("environment", {})

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_branch_without_clone_repo_returns_error(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            branch="feature-x",
        )

        assert "Error" in result
        assert "branch requires clone_repo" in result

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    @patch("sunaba.tools.container.lifecycle._setup_branch")
    def test_branch_with_pr_returns_error(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            repo="owner/repo",
            branch="feature-x",
            pr=42,
        )

        assert "Error" in result
        assert "mutually exclusive" in result
        # _setup_branch should NOT be called when branch and pr are both set
        mock_setup.assert_not_called()

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_empty_branch_rejected(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        """An empty branch string must be rejected -- it would silently
        land on the default branch (acceptance criterion #3)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            branch="",
        )

        assert "Error" in result
        assert "empty" in result

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    @patch("sunaba.tools.container.lifecycle._setup_branch")
    def test_branch_setup_failure_non_fatal(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.side_effect = RuntimeError("branch not found")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            branch="nonexistent",
        )

        assert result.startswith("abc123def456")
        assert "branch setup failed" in result

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    @patch("sunaba.tools.container.lifecycle._setup_branch")
    @patch("sunaba.tools.container.clone._clone_repo_via_network")
    def test_clone_repo_not_called_when_branch_set(
        self,
        mock_clone_net: MagicMock,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.return_value = "Branch feature-x setup done"

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            branch="feature-x",
        )

        # _clone_repo_via_network should NOT be called when branch is set
        mock_clone_net.assert_not_called()
        # _setup_branch SHOULD be called
        mock_setup.assert_called_once()

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle._ensure_image")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    @patch("sunaba.tools.container.lifecycle._setup_branch")
    def test_branch_auto_enables_network(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ):
        """branch= should auto-enable allow_network."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_setup.return_value = "Branch feature-x \u2192 /workspace in container abc123"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            branch="feature-x",
            allow_network=False,  # should be auto-enabled to True
        )

        assert "[network: on]" in result


class TestRunContainerAndExecBranchParam:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    @patch("sunaba.tools.container.lifecycle._setup_branch")
    def test_branch_calls_setup(
        self,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            branch="feature-x",
        ))

        assert result["status"] == "ok"
        mock_setup.assert_called_once_with(
            mock_container, "abc123def456", "owner/repo", "feature-x",
            "/workspace", "[dev]",
            authenticated=False, open_read_grant=False, pip_args=None,
        )

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_branch_without_clone_repo_returns_warning(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            branch="feature-x",
        ))

        assert result["status"] == "error"
        assert "branch requires clone_repo" in result.get("error", "")

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_empty_branch_rejected(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            branch="",
        ))

        assert result["status"] == "error"
        assert "empty" in result.get("error", "")

    @patch("sunaba.tools.container._docker")
    @patch("sunaba.tools.container.lifecycle.validate_image_ref")
    def test_branch_with_pr_returns_error(
        self,
        mock_validate: MagicMock,
        mock_docker: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_container.exec_run.return_value = (0, (b"test output", b""))
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            clone_repo="owner/repo",
            repo="owner/repo",
            branch="feature-x",
            pr=42,
        ))

        assert result["status"] == "error"
        assert "mutually exclusive" in result.get("error", "")
