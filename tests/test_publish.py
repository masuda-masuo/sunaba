"""Tests for publish tool (one-shot: commit → push → squash → force push → API fallback)."""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from code_sandbox_mcp.proxy_client import CONTROL_SECRET_ENV, CONTROL_URL_ENV
from code_sandbox_mcp.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV, EgressProxyError
from code_sandbox_mcp.tools.vcs import _create_pr_via_api, publish
from tests.conftest import _decode, _make_client_mock, _make_container_mock

# Standard execute exec sequence shared by the one-shot push tests: git
# checkout -b, git add, rev-parse @{u} (no upstream -> skip squash), commit,
# push, rev-parse HEAD.  publish no longer has a dry_run/token step (retired
# for V1.0), so every call runs this straight through.
_PUSH_SEQUENCE = [
    (0, b"", b""),  # git checkout -b
    (0, b"", b""),  # git add
    (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
    (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
    (0, b"pushed", b""),  # git push
    (0, b"abc1234def5678", b""),  # git rev-parse HEAD
]


class TestPublish:
    """Tests for publish (one-shot execute)."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_successful_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A successful push returns pushed status with sha."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix issue\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix issue",
            working_dir="/root/repo",
        ))

        assert result["status"] == "pushed"
        assert result["branch"] == "fix/x"
        assert result["sha"] == "abc1234"

    @patch("code_sandbox_mcp.tools.vcs.proxy_lifecycle.ensure_egress_proxy")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_recovers_proxy_env_lost_after_restart(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#428: a server restart wipes the dynamic proxy env vars this

        process exports, even though the sidecar (and the container's
        proxied network) are still running.  ``publish`` must recover them
        via ``ensure_egress_proxy`` before deciding whether to open a push
        grant, rather than silently treating the proxy as unconfigured.
        """
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        monkeypatch.delenv(CONTROL_URL_ENV, raising=False)
        monkeypatch.delenv(CONTROL_SECRET_ENV, raising=False)

        def _recover(client: MagicMock, env: dict[str, str] | None = None) -> MagicMock:
            # Mirrors what the real ensure_egress_proxy does: recover the
            # running sidecar's secret/URL back into the process env.
            monkeypatch.setenv(CONTROL_URL_ENV, "http://127.0.0.1:8768")
            monkeypatch.setenv(CONTROL_SECRET_ENV, "recovered-secret")
            return MagicMock()

        mock_ensure_proxy.side_effect = _recover

        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix issue\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch("code_sandbox_mcp.tools.vcs.authorized_push_grant") as mock_grant:
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix issue",
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        mock_ensure_proxy.assert_called_once_with(client)
        # A real grant is opened this time (proxy recognized as configured),
        # not silently skipped as it would be if the env stayed lost.
        mock_grant.assert_called_once()

    @patch("code_sandbox_mcp.tools.vcs.proxy_lifecycle.ensure_egress_proxy")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_fails_closed_when_proxy_env_unrecoverable(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#428: when the sidecar truly cannot be recovered, publish must

        fail closed with a clear error instead of falling through to an
        unprotected push.  The proxy-env check runs before any git command,
        so a fail-closed abort must not touch the container at all.
        """
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        monkeypatch.delenv(CONTROL_URL_ENV, raising=False)
        monkeypatch.delenv(CONTROL_SECRET_ENV, raising=False)
        mock_ensure_proxy.side_effect = EgressProxyError("sidecar unreachable")

        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix issue",
            working_dir="/root/repo",
        ))

        assert result["status"] == "error"
        assert result["step"] == "egress_proxy"
        assert "sidecar unreachable" in result["error"]
        container.exec_run.assert_not_called()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_successful_push_with_pr(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Successful push + PR creation should include pr_url."""
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        # With a host token, PR creation runs host-side (#360) — no gh exec.
        with patch(
            "code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="ghp_test"
        ), patch(
            "code_sandbox_mcp.tools.vcs._create_pr_via_api",
            return_value="https://github.com/owner/repo/pull/99",
        ) as mock_create_pr:
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                create_pr=True,
                pr_title="My PR Title",
                pr_body="PR body here",
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/99"
        mock_create_pr.assert_called_once_with(
            "owner/repo", "fix/x", "My PR Title", "PR body here", "", "ghp_test"
        )

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_pr_host_api_failure_still_reports_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A host-side PR-creation failure returns pushed + pr_create_error."""
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value="ghp_test"
        ), patch(
            "code_sandbox_mcp.tools.vcs._create_pr_via_api",
            side_effect=RuntimeError("GitHub API POST /repos/owner/repo/pulls returned HTTP 422: A pull request already exists"),
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                create_pr=True,
                pr_title="My PR Title",
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        assert result["sha"] == "abc1234"
        assert "already exists" in result["pr_create_error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_pr_legacy_container_token_uses_gh_exec(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """No host token + no proxy → the in-container gh path still works."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            (0, b"pushed", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
            (0, b"https://github.com/owner/repo/pull/99", b""),  # gh pr create
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value=""
        ), patch(
            "code_sandbox_mcp.tools.vcs.proxy_configured", return_value=False
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                create_pr=True,
                pr_title="My PR Title",
                pr_body="PR body here",
                base_branch="dev",
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/99"
        # The --base flag must be part of the gh invocation itself — the old
        # wrapper appended it after the body-file cleanup ('; rm -f ...'),
        # so gh never saw it and the PR silently targeted the default branch.
        gh_cmd = str(container.exec_run.call_args_list[-1])
        assert "--base dev" in gh_cmd
        assert gh_cmd.index("--base dev") < gh_cmd.index("; rm -f")

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_pr_proxied_without_host_token_errors(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Proxied + no host token → clear pr_create_error, no gh exec attempt."""
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "code_sandbox_mcp.tools.vcs._resolve_vcs_token", return_value=""
        ), patch(
            "code_sandbox_mcp.tools.vcs.proxy_configured", return_value=True
        ), patch(
            "code_sandbox_mcp.tools.vcs.authorized_push_grant"
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                create_pr=True,
                pr_title="My PR Title",
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        assert "host-side token" in result["pr_create_error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_commit_nothing_to_commit_is_ok(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """git commit with 'nothing to commit' should proceed to push."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"nothing to commit, working tree clean", b""),  # git commit
            (0, b"Everything up-to-date", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            working_dir="/root/repo",
        ))

        assert result["status"] == "pushed"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_push_failure(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Push failure (both transports) should return error status."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix", b""),  # git commit
            (1, b"", b"remote rejected: permission denied"),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
            (0, b"", b""),  # write API push script
            (1, b"", b"push failed"),  # API push also fails
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            working_dir="/root/repo",
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"
        assert "permission denied" in result["error"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_push_blocked_by_egress_proxy_skips_api_fallback(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """#401: egress proxy block must NOT fall back to Objects API."""
        # The git push error contains "BLOCKED by egress proxy" -- exactly
        # what the real proxy.py::block_body() emits.  There are only 6
        # exec calls: the 5 pre-push steps + the failed git push.  No
        # _try_api_push exec happens after that.
        proxy_error = (
            "remote: BLOCKED by egress proxy: "
            "push to owner/repo is not in the allowlist. "
            "Push from the sandbox is only allowed via the publish tool."
        )
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix", b""),  # git commit
            (1, b"", proxy_error.encode()),  # git push BLOCKED by proxy
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            working_dir="/root/repo",
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"
        assert "BLOCKED by egress proxy" in result["error"]
        assert "hint" in result
        assert "CODE_SANDBOX_ALLOWED_REPOS" in result["hint"]
        # Exactly 6 exec calls -- no _try_api_push was triggered
        assert container.exec_run.call_count == 6

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_default_working_dir(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Default working_dir (None) auto-resolves, falling back to /home/sandbox."""
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"
        first_cmd = container.exec_run.call_args_list[0][0][0][2]
        assert "cd /home/sandbox" in first_cmd

    @patch("code_sandbox_mcp.tools.vcs.resolve_git_root")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_auto_resolves_working_dir_from_meta(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """Default working_dir auto-resolves from .sandbox-meta.json."""
        mock_resolve.return_value = "/tmp/repo/code-sandbox-mcp"

        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"
        mock_resolve.assert_called_once()
        for call in container.exec_run.call_args_list:
            args, _kwargs = call
            cmd = args[0][2]
            if "cd " not in cmd:
                continue
            assert "/tmp/repo/code-sandbox-mcp" in cmd, f"Expected resolved path in: {cmd}"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_uses_default_identity(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Default identity should be used when author_name/email are None."""
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"

        commit_call = container.exec_run.call_args_list[3]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "code-sandbox-mcp[bot]" in commit_cmd
        assert "code-sandbox-mcp[bot]@users.noreply.github.com" in commit_cmd
        assert "'code-sandbox-mcp[bot]'" in commit_cmd

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    def test_with_custom_identity(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Custom author_name/email should override the defaults."""
        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            author_name="Custom User",
            author_email="custom@example.com",
        ))

        assert result["status"] == "pushed"

        commit_call = container.exec_run.call_args_list[3]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "'Custom User'" in commit_cmd
        assert "custom@example.com" in commit_cmd


class TestCreatePrViaApi:
    """Host-side PR creation via the GitHub REST API (#360)."""

    @staticmethod
    def _response(payload: dict) -> MagicMock:
        """Context-manager mock mimicking urlopen's response."""
        cm = MagicMock()
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        cm.__enter__.return_value = resp
        return cm

    def test_creates_pr_with_explicit_base(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._response(
                {"html_url": "https://github.com/owner/repo/pull/7"}
            )
            url = _create_pr_via_api(
                "owner/repo", "fix/x", "Title", "Body", "dev", "ghp_tok"
            )

        assert url == "https://github.com/owner/repo/pull/7"
        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args.args[0]
        assert request.full_url == "https://api.github.com/repos/owner/repo/pulls"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer ghp_tok"
        payload = json.loads(request.data.decode("utf-8"))
        assert payload == {
            "title": "Title", "head": "fix/x", "base": "dev", "body": "Body",
        }

    def test_resolves_default_branch_when_base_empty(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = [
                self._response({"default_branch": "main"}),
                self._response({"html_url": "https://github.com/owner/repo/pull/8"}),
            ]
            url = _create_pr_via_api(
                "owner/repo", "fix/x", "Title", "", "", "ghp_tok"
            )

        assert url == "https://github.com/owner/repo/pull/8"
        lookup = mock_urlopen.call_args_list[0].args[0]
        assert lookup.full_url == "https://api.github.com/repos/owner/repo"
        assert lookup.get_method() == "GET"
        create = mock_urlopen.call_args_list[1].args[0]
        payload = json.loads(create.data.decode("utf-8"))
        assert payload["base"] == "main"
        assert "body" not in payload  # empty pr_body is omitted

    def test_http_error_carries_github_message(self) -> None:
        error_body = json.dumps({
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for fix/x."}],
        }).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/pulls",
            422,
            "Unprocessable Entity",
            None,  # type: ignore[arg-type]
            io.BytesIO(error_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_error):
            with pytest.raises(RuntimeError, match="Validation Failed.*already exists"):
                _create_pr_via_api(
                    "owner/repo", "fix/x", "Title", "", "dev", "ghp_tok"
                )

    def test_network_error_becomes_runtime_error(self) -> None:
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("no route to host"),
        ):
            with pytest.raises(RuntimeError, match="no route to host"):
                _create_pr_via_api(
                    "owner/repo", "fix/x", "Title", "", "dev", "ghp_tok"
                )

    def test_missing_html_url_raises(self) -> None:
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = self._response({})
            with pytest.raises(RuntimeError, match="no html_url"):
                _create_pr_via_api(
                    "owner/repo", "fix/x", "Title", "", "dev", "ghp_tok"
                )
