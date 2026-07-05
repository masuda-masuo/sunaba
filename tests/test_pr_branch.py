"""Tests for _setup_pr_branch and sandbox_initialize/run_container_and_exec with pr parameter (Issue #136)."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.tools.container import (
    _resolve_pr_head_ref,
    _setup_pr_branch,
    run_container_and_exec,
    sandbox_initialize,
)


def _make_container_mock(exec_returns: list[tuple[int, tuple[bytes, bytes]]]):
    container = MagicMock()
    container.exec_run.side_effect = [
        (ec, (stdout, stderr)) for ec, (stdout, stderr) in exec_returns
    ]
    return container


_PR_INFO_JSON = json.dumps({
    "headRefName": "feature-branch",
})


class TestSetupPrBranch:
    """Tests for _setup_pr_branch."""

    def test_success(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned repo\n", b"")),
            (0, (b"Switched to branch\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

        assert "PR #136" in result
        assert "feature-branch" in result
        assert "/tmp/repo" in result
        assert "abc123" in result

    def test_gh_view_failure(self):
        container = _make_container_mock([
            (1, (b"", b"not found")),
        ])

        with pytest.raises(RuntimeError, match="Failed to fetch PR"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 999, "/tmp/repo",
            )

    def test_clone_failure(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (1, (b"", b"Repository not found")),
        ])

        with pytest.raises(RuntimeError, match="Failed to clone"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

    def test_checkout_failure(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (1, (b"", b"checkout failed")),
        ])

        with pytest.raises(RuntimeError, match="Failed to checkout"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

    def test_install_failure_non_fatal(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (0, (b"Switched\n", b"")),
            (1, (b"", b"install failed")),
            (0, (b"", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger") as mock_logger:
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

        assert "PR #136" in result
        mock_logger.warning.assert_called_once()
        assert "pip install deps failed" in mock_logger.warning.call_args[0][0]

    def test_invalid_json_from_gh(self):
        container = _make_container_mock([
            (0, (b"not valid json", b"")),
        ])

        with pytest.raises(RuntimeError, match="Failed to parse PR info JSON"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )

    def test_incomplete_pr_info(self):
        incomplete = json.dumps({"other_field": "value"})
        container = _make_container_mock([
            (0, (incomplete.encode(), b"")),
        ])

        with pytest.raises(RuntimeError, match="Incomplete PR info"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
            )


    def test_repo_name_in_path(self):
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = [
            (0, (b'{"headRefName":"feature-branch"}', b"")),
            (0, (b"Cloning into '/tmp/repo/myrepo'...", b"")),
            (0, (b"Switched to branch 'feature-branch'", b"")),
            (0, (b"Installed", b"")),
            (0, (b"", b"")),
        ]
        with patch("code_sandbox_mcp.tools.container.record_copy"):
            result = _setup_pr_branch(
                mock_container,
                "abc123def456",
                "owner/myrepo",
                42,
                "/tmp/repo",
            )
        assert "PR #42" in result
        assert "/tmp/repo/myrepo" in result


class TestResolvePrHeadRef:
    """Tests for _resolve_pr_head_ref: host-side head-ref resolution (#403)."""

    @staticmethod
    def _mock_urlopen_response(payload: dict):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        cm = MagicMock()
        cm.__enter__.return_value = resp
        return cm

    @patch("code_sandbox_mcp.tools.container._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_returns_head_ref(self, mock_urlopen, mock_token):
        mock_urlopen.return_value = self._mock_urlopen_response(
            {"head": {"ref": "feature-branch"}}
        )

        assert _resolve_pr_head_ref("owner/repo", 136) == "feature-branch"

        request = mock_urlopen.call_args.args[0]
        assert request.full_url == "https://api.github.com/repos/owner/repo/pulls/136"
        assert not request.has_header("Authorization")

    @patch("code_sandbox_mcp.tools.container._resolve_vcs_token", return_value="ghs_tok")
    @patch("urllib.request.urlopen")
    def test_attaches_host_token_when_available(self, mock_urlopen, mock_token):
        mock_urlopen.return_value = self._mock_urlopen_response(
            {"head": {"ref": "feature-branch"}}
        )

        _resolve_pr_head_ref("owner/repo", 136)

        request = mock_urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer ghs_tok"

    @patch("code_sandbox_mcp.tools.container._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_http_error_becomes_runtime_error(self, mock_urlopen, mock_token):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/pulls/999",
            404,
            "Not Found",
            None,
            None,
        )

        with pytest.raises(RuntimeError, match="HTTP 404"):
            _resolve_pr_head_ref("owner/repo", 999)

    @patch("code_sandbox_mcp.tools.container._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_403_hints_rate_limit(self, mock_urlopen, mock_token):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/pulls/136",
            403,
            "rate limit exceeded",
            None,
            None,
        )

        with pytest.raises(RuntimeError, match="rate-limited"):
            _resolve_pr_head_ref("owner/repo", 136)

    @patch("code_sandbox_mcp.tools.container._resolve_vcs_token", return_value="")
    @patch("urllib.request.urlopen")
    def test_missing_head_ref_raises(self, mock_urlopen, mock_token):
        mock_urlopen.return_value = self._mock_urlopen_response({"head": {}})

        with pytest.raises(RuntimeError, match="no head ref"):
            _resolve_pr_head_ref("owner/repo", 136)

    def test_invalid_repo_rejected_before_any_request(self):
        with pytest.raises(ValueError):
            _resolve_pr_head_ref("owner/repo/evil?x=1", 136)


class TestSetupPrBranchAnonymous:
    """_setup_pr_branch with authenticated=False: anonymous checkout (#403)."""

    def test_success_uses_git_not_gh(self):
        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"Switched to branch 'feature-branch'\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            return_value="feature-branch",
        ) as mock_resolve, patch(
            "code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""
        ), patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                authenticated=False,
            )

        assert "PR #136" in result
        assert "feature-branch" in result
        mock_resolve.assert_called_once_with("owner/repo", 136, token=None)

        executed = " ;; ".join(
            call.args[0][2] for call in container.exec_run.call_args_list
        )
        assert "gh " not in executed
        assert "git clone" in executed
        assert "git fetch origin pull/136/head" in executed
        assert "git checkout -B feature-branch FETCH_HEAD" in executed

    def test_clone_failure_hints_private_repo(self):
        container = _make_container_mock([
            (1, (b"", b"fatal: could not read Username")),
        ])

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            return_value="feature-branch",
        ), patch(
            "code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""
        ), patch("code_sandbox_mcp.tools.container.logger"):
            with pytest.raises(RuntimeError, match="private repository"):
                _setup_pr_branch(
                    container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                    authenticated=False,
                )

    def test_fetch_or_checkout_failure(self):
        container = _make_container_mock([
            (0, (b"Cloned\n", b"")),
            (1, (b"", b"fatal: couldn't find remote ref pull/136/head")),
        ])

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            return_value="feature-branch",
        ), patch(
            "code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""
        ), patch("code_sandbox_mcp.tools.container.logger"):
            with pytest.raises(RuntimeError, match="Failed to checkout PR #136"):
                _setup_pr_branch(
                    container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                    authenticated=False,
                )

    def test_head_ref_resolution_failure_stops_before_any_exec(self):
        container = MagicMock()

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            side_effect=RuntimeError("GitHub API returned HTTP 404"),
        ), patch("code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""):
            with pytest.raises(RuntimeError, match="HTTP 404"):
                _setup_pr_branch(
                    container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                    authenticated=False,
                )

        container.exec_run.assert_not_called()


class TestSetupPrBranchReadWindow:
    """open_read_window lets the anonymous pr= checkout work for private
    repos too (#419), the same mechanism _clone_repo_via_network already
    uses for clone_repo."""

    @patch("code_sandbox_mcp.tools.container.record_boundary_crossing")
    def test_success_is_journaled(self, mock_record):
        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"Switched to branch 'feature-branch'\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            return_value="feature-branch",
        ), patch(
            "code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""
        ), patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                authenticated=False, open_read_window=True,
            )

        assert "PR #136" in result
        mock_record.assert_called_once_with(
            "abc123def456",
            "setup_pr_branch",
            "repo=owner/repo pr=#136 dest=/tmp/repo/repo proxy_read_window=True",
            approved=True,
        )

    @patch("code_sandbox_mcp.tools.container.record_boundary_crossing")
    def test_clone_failure_is_journaled_with_approved_false(self, mock_record):
        container = _make_container_mock([
            (1, (b"", b"fatal: could not read Username")),
        ])

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            return_value="feature-branch",
        ), patch(
            "code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""
        ), patch("code_sandbox_mcp.tools.container.logger"):
            with pytest.raises(RuntimeError, match="private repository"):
                _setup_pr_branch(
                    container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                    authenticated=False, open_read_window=True,
                )

        mock_record.assert_called_once_with(
            "abc123def456",
            "setup_pr_branch",
            "repo=owner/repo pr=#136 dest=/tmp/repo/repo proxy_read_window=True",
            approved=False,
        )

    @patch("code_sandbox_mcp.tools.container.record_boundary_crossing")
    def test_authenticated_path_ignores_open_read_window(self, mock_record):
        """authenticated=True (in-container gh token) never needs the proxy
        read window, even if the caller passes open_read_window=True."""
        container = _make_container_mock([
            (0, (b'{"headRefName": "feature-branch"}', b"")),
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"Switched to branch 'feature-branch'\n", b"")),
            (0, (b"Installed\n", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                authenticated=True, open_read_window=True,
            )

        mock_record.assert_not_called()

    @patch("code_sandbox_mcp.tools.container.record_boundary_crossing")
    def test_no_read_window_is_not_journaled(self, mock_record):
        """Default (open_read_window=False) anonymous checkout is unaffected
        -- no new journal entry, matching pre-existing behaviour."""
        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"Switched to branch 'feature-branch'\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch(
            "code_sandbox_mcp.tools.container._resolve_pr_head_ref",
            return_value="feature-branch",
        ), patch(
            "code_sandbox_mcp.tools.container._resolve_vcs_token", return_value=""
        ), patch("code_sandbox_mcp.tools.container.logger"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                authenticated=False,
            )

        mock_record.assert_not_called()

    @patch("code_sandbox_mcp.tools.container._resolve_vcs_token")
    @patch("urllib.request.urlopen")
    def test_token_resolved_once_and_reused(self, mock_urlopen, mock_resolve_token):
        """#436 review: a broker-backed _resolve_vcs_token() spawns a
        subprocess per call (no caching, up to a 30s timeout). The anonymous
        path must resolve it once and pass the same value to both
        _resolve_pr_head_ref (host GitHub API call) and the read window, not
        call it again for each."""
        mock_resolve_token.return_value = "ghs_minted"
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"head": {"ref": "feature-branch"}}
        ).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        container = _make_container_mock([
            (0, (b"Cloning into '/tmp/repo/repo'...\n", b"")),
            (0, (b"Switched to branch 'feature-branch'\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                authenticated=False, open_read_window=True,
            )

        assert mock_resolve_token.call_count == 1
        # Same token reaches the GitHub API request (head-ref resolution).
        request = mock_urlopen.call_args.args[0]
        assert request.get_header("Authorization") == "Bearer ghs_minted"


class TestSandboxInitializePrParam:
    """Tests for sandbox_initialize with pr parameter."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_calls_setup(
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
        mock_setup.return_value = "PR #136 (feature) \u2192 /tmp/repo/repo in container abc123"

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            repo="owner/repo",
            pr=136,
            allow_network=False,
        )

        assert "abc123def456" in result
        assert "PR #136" in result
        mock_setup.assert_called_once()
        args = mock_setup.call_args[0]
        assert args[2] == "owner/repo"
        assert args[3] == 136
        run_kwargs = mock_client.containers.run.call_args[1]
        assert "GITHUB_TOKEN" not in run_kwargs.get("environment", {})

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pr_without_repo_returns_warning(
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
            pr=136,
        )

        assert "abc123def456" in result
        assert "pr setup failed" in result
        assert "repo is required" in result

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_setup_failure_non_fatal(
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
        mock_setup.side_effect = RuntimeError("network error")

        result = sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            repo="owner/repo",
            pr=136,
        )

        assert result.startswith("abc123def456")
        assert "pr setup failed" in result

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_without_pr_works_normally(
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
        )

        assert result == "abc123def456"


class TestRunContainerAndExecPrParam:
    """Tests for run_container_and_exec with pr parameter."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_calls_setup(
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

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=False):
            result = json.loads(run_container_and_exec(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                commands=["echo hello"],
                repo="owner/repo",
                pr=136,
            ))

        assert result["status"] == "ok"
        # pr=N no longer injects a token; the container is token-free, so the
        # PR checkout takes the anonymous (authenticated=False) path (#439).
        mock_setup.assert_called_once_with(
            mock_container, "abc123def456", "owner/repo", 136, "/tmp/repo", "[dev]",
            authenticated=False,
        )

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_pr_without_repo_returns_warning(
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
            pr=136,
        ))

        assert result["status"] == "ok"
        assert result["pr_warning"] == "repo is required when pr is specified"

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    def test_pr_error_reported(
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
        mock_setup.side_effect = RuntimeError("network error")

        result = json.loads(run_container_and_exec(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            commands=["echo hello"],
            repo="owner/repo",
            pr=136,
        ))

        assert result["status"] == "ok"
        assert result["pr_warning"] == "network error"



class TestPipExtrasParam:
    """Tests for pip_extras parameter customization."""

    def test_custom_pip_extras(self):
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (0, (b"Switched\n", b"")),
            (0, (b"Installed\n", b"")),
            (0, (b"", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                pip_extras="[testing]",
            )

        assert "PR #136" in result
        # The editable install must target the repo dir ("."), i.e.
        # `pip install -e '.[testing]'` -- NOT `pip install -e '[testing]'`,
        # which is an invalid spec that silently no-ops so the dev install
        # never takes effect.
        install_cmd = container.exec_run.call_args_list[3][0][0][2]
        assert "pip install -e '.[testing]'" in install_cmd

    def test_pip_extras_none_skips_install(self):
        # 4 exec calls: gh view, clone, checkout, meta write. No pip install.
        container = _make_container_mock([
            (0, (_PR_INFO_JSON.encode(), b"")),
            (0, (b"Cloned\n", b"")),
            (0, (b"Switched\n", b"")),
            (0, (b"", b"")),
        ])

        with patch("code_sandbox_mcp.tools.container.logger"):
            result = _setup_pr_branch(
                container, "abc123def456", "owner/repo", 136, "/tmp/repo",
                pip_extras=None,
            )

        assert "PR #136" in result
        # Should be exactly 4 exec calls (no pip install, +1 meta write)
        assert container.exec_run.call_count == 4


class TestCloneRepoPrInteraction:
    """Tests for clone_repo + pr interaction."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    def test_clone_repo_skipped_when_pr_set(
        self,
        mock_clone_shiori: MagicMock,
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
        mock_setup.return_value = "PR #136 setup done"

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            repo="owner/repo",
            pr=136,
        )

        # _clone_shiori_repo_to_container should NOT be called
        mock_clone_shiori.assert_not_called()
        # _setup_pr_branch SHOULD be called
        mock_setup.assert_called_once()

    @patch("code_sandbox_mcp.tools.container._shiori_preclone_exists", return_value=True)
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    @patch("code_sandbox_mcp.tools.container._clone_shiori_repo_to_container")
    def test_clone_repo_called_when_pr_not_set(
        self,
        mock_clone_shiori: MagicMock,
        mock_setup: MagicMock,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_preclone_exists: MagicMock,
    ):
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_initialize(
            image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
            clone_repo="owner/repo",
            pip_extras=None,
        )

        # _clone_shiori_repo_to_container SHOULD be called
        mock_clone_shiori.assert_called_once()
        # _setup_pr_branch should NOT be called
        mock_setup.assert_not_called()
