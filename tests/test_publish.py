"""Tests for publish tool (one-shot: commit → push → squash → force push → API fallback)."""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_client import CONTROL_SECRET_ENV, CONTROL_URL_ENV
from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV, EgressProxyError
from sunaba.tools.vcs import _create_pr_via_api, publish
from tests.conftest import _decode, _make_client_mock, _make_container_mock

# Standard execute exec sequence shared by the one-shot non-manifest push
# tests.  Order matches publish()'s actual exec flow: git ls-files (capture
# untracked before the add), checkout -b, add, rev-parse @{u} (no upstream
# -> skip squash), commit, push, rev-parse HEAD.
_PUSH_SEQUENCE = [
    (0, b"", b""),  # git ls-files --others --exclude-standard
    (0, b"", b""),  # git checkout -b
    (0, b"", b""),  # git add -A
    (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
    (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
    (0, b"pushed", b""),  # git push
    (0, b"abc1234def5678", b""),  # git rev-parse HEAD
]


class TestPublish:
    """Tests for publish (one-shot execute)."""

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_successful_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A successful push returns pushed status with sha."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git ls-files --others --exclude-standard
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

    @patch("sunaba.tools.vcs.publishing.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
            (0, b"[fix/x abc1234] Fix issue\n1 file changed", b""),  # git commit
            (0, b"To github.com:owner/repo.git\n * [new branch] fix/x -> fix/x", b""),  # git push
            (0, b"abc1234def5678", b""),  # git rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch("sunaba.tools.vcs.publishing.authorized_push_grant") as mock_grant:
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

    @patch("sunaba.tools.vcs.publishing.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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

    @patch("sunaba.tools.vcs.publishing.proxy_lifecycle.ensure_egress_proxy")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_reconciles_sidecar_even_when_proxy_env_is_present(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_ensure_proxy: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#533: proxy env in this process is not proof the sidecar is usable.

        ``publish`` used to skip ``ensure_egress_proxy`` whenever the control
        URL/secret were already exported, so a sidecar that had been removed --
        or baked with an allowlist the operator has since changed -- was never
        reconciled: the grant then failed against a sidecar that was gone or
        stale, for the rest of the session.
        """
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "true")
        monkeypatch.setenv(CONTROL_URL_ENV, "http://127.0.0.1:8768")
        monkeypatch.setenv(CONTROL_SECRET_ENV, "stale-secret")

        container = _make_container_mock(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch("sunaba.tools.vcs.publishing.authorized_push_grant") as mock_grant:
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix issue",
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        mock_ensure_proxy.assert_called_once_with(client)
        mock_grant.assert_called_once()

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_pr_creation_rejects_empty_body(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """create_pr=True with empty pr_body returns validation error."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            create_pr=True,
            pr_title="My PR Title",
            pr_body="",
            working_dir="/root/repo",
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        assert "pr_body" in result["error"]
        container.exec_run.assert_not_called()

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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
            "sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="ghp_test"
        ), patch(
            "sunaba.tools.vcs.publishing._create_pr_via_api",
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

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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
            "sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="ghp_test"
        ), patch(
            "sunaba.tools.vcs.publishing._create_pr_via_api",
            side_effect=RuntimeError("GitHub API POST /repos/owner/repo/pulls returned HTTP 422: A pull request already exists"),
        ):
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
        assert result["sha"] == "abc1234"
        assert "already exists" in result["pr_create_error"]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_pr_legacy_container_token_uses_gh_exec(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """No host token + no proxy → the in-container gh path still works."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git ls-files --others --exclude-standard
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
            "sunaba.tools.vcs.publishing._resolve_vcs_token", return_value=""
        ), patch(
            "sunaba.tools.vcs.publishing.proxy_configured", return_value=False
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

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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
            "sunaba.tools.vcs.publishing._resolve_vcs_token", return_value=""
        ), patch(
            "sunaba.tools.vcs.publishing.proxy_configured", return_value=True
        ), patch(
            "sunaba.tools.vcs.publishing.authorized_push_grant"
        ):
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
        assert "host-side token" in result["pr_create_error"]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_commit_nothing_to_commit_is_ok(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """git commit with 'nothing to commit' should proceed to push."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git ls-files --others --exclude-standard
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

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_push_failure(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Push failure (both transports) should return error status."""
        container = _make_container_mock([
            (0, b"", b""),  # git checkout -b
            (0, b"", b""),  # git ls-files --others --exclude-standard
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

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_push_blocked_by_egress_proxy_skips_api_fallback(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """#401: egress proxy block must NOT fall back to Objects API."""
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
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
            (0, b"", b""),  # git ls-files --others --exclude-standard
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
        assert "SUNABA_ALLOWED_REPOS" in result["hint"]
        # Exactly 7 exec calls (1 git ls-files + 5 pre-push + 1 failed push) -- no _try_api_push was triggered
        assert container.exec_run.call_count == 7

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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

    @patch("sunaba.tools.vcs.publishing.resolve_git_root")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_auto_resolves_working_dir_from_meta(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """Default working_dir auto-resolves from .sandbox-meta.json."""
        mock_resolve.return_value = "/tmp/repo/sunaba"

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
            assert "/tmp/repo/sunaba" in cmd, f"Expected resolved path in: {cmd}"

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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

        # Index 4 because [0]=checkout, [1]=ls-files, [2]=add, [3]=rev-parse, [4]=commit
        commit_call = container.exec_run.call_args_list[4]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "sunaba[bot]" in commit_cmd
        assert "sunaba[bot]@users.noreply.github.com" in commit_cmd
        assert "'sunaba[bot]'" in commit_cmd

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
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

        # Index 4 because [0]=checkout, [1]=ls-files, [2]=add, [3]=rev-parse, [4]=commit
        commit_call = container.exec_run.call_args_list[4]
        commit_cmd = commit_call[0][0][2]
        assert "user.name" in commit_cmd
        assert "'Custom User'" in commit_cmd
        assert "custom@example.com" in commit_cmd

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_swept_untracked_empty(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """swept_untracked is [] when no untracked files exist."""
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
        assert result["swept_untracked"] == []

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_swept_untracked_with_files(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """swept_untracked lists untracked files when they exist."""
        container = _make_container_mock([
            (0, b"typings/foo.pyi\ndirty.txt\n", b""),  # git ls-files
            (0, b"", b""),  # checkout -b
            (0, b"", b""),  # git add
            (1, b"", b"no upstream"),  # rev-parse @{u}
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            include_untracked=True,
        ))

        assert result["status"] == "pushed"
        assert result["swept_untracked"] == ["typings/foo.pyi", "dirty.txt"]


class TestPublishManifest:
    """Tests for manifest-based staging (files=[...] and include_untracked)."""

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_only_declared_staged(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest with one declared file: only that file is staged and pushed.

        Even though another file exists undeclared, it must not be staged.
        The new branch (no upstream) resolves origin/HEAD as the base.
        """
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b
            # New path: resolve remote base
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        assert result["staged_files"] == ["declared.txt"]
        assert result["swept_untracked"] == []

        # Verify 'git add --' was used, not 'git add -A'
        add_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git add" in str(c[0][0][2])
        ]
        assert any("git add --" in c and "declared.txt" in c for c in add_calls)
        assert not any("git add -A" in c for c in add_calls)

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_stages_untracked_declared_file(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode stages a declared file that is brand new / untracked.

        The existence check passes (test -e returns 0), and git add -- stages
        it successfully.  The new branch resolves origin/HEAD as the base.
        """
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'newfile.py' (exists)
            (0, b"", b""),  # checkout -b
            # New path: resolve remote base
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'newfile.py'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["newfile.py"],
        ))

        assert result["status"] == "pushed"
        assert result["staged_files"] == ["newfile.py"]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_after_checkpoint_excludes_undeclared(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """After a checkpoint that committed an undeclared file on a branch
        with no upstream, the push must exclude that file by building the
        commit against the remote base (origin/HEAD)."""
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b
            # Resolve base: origin/fix/x does NOT exist on remote
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Msg", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Msg",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        assert result["staged_files"] == ["declared.txt"]

        # Verify 'git reset --mixed origin/HEAD' was used (the new base path)
        reset_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git reset" in str(c[0][0][2])
        ]
        assert any("git reset --mixed" in c for c in reset_calls)

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_rejects_absolute_path(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Absolute paths produce an error and no push."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["/etc/passwd"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        assert "/etc/passwd" in result["error"]
        container.exec_run.assert_not_called()

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_rejects_dot_dot_traversal(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """.. traversal produces an error and no push."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["../outside.txt"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        container.exec_run.assert_not_called()

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_rejects_deeper_dot_dot_traversal(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """.. deeper in the path produces an error."""
        container = _make_container_mock([])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["sub/../../secret.txt"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        container.exec_run.assert_not_called()

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_rejects_nonexistent_path(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A declared path that does not exist produces an error and no push."""
        container = _make_container_mock([
            (1, b"", b""),  # test -e 'missing.txt' -> not found
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["missing.txt"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        assert "missing.txt" in result["error"]
        # Only the existence check exec happened; nothing else should
        assert container.exec_run.call_count == 1

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_no_manifest_untracked_rejection(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """No manifest + untracked files + no opt-in: error listing the files."""
        container = _make_container_mock([
            (0, b"secret.env\ndump.log\n", b""),  # git ls-files --others
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "error"
        assert result["step"] == "untracked_files"
        assert result["untracked_files"] == ["secret.env", "dump.log"]
        assert "files=[...]" in result["error"]
        assert "include_untracked=True" in result["error"]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_no_manifest_no_untracked_behaves_as_before(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """No manifest + no untracked files: identical to old behaviour."""
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
        assert result["swept_untracked"] == []
        assert "staged_files" not in result

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_fresh_branch_leak_prevention(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Criterion 1: fresh branch, no upstream, prior checkpoint committed
        an undeclared file.  The pushed commit must include only the declared
        file.  The undeclared file must be absent from the pushed history and
        still present in the worktree.

        This simulates: checkpoint committed both declared.txt and
        personal.txt, then publish with files=['declared.txt'].
        The new base-resolution path (origin/HEAD) strips the checkpoint
        commits, then only the declared file is staged.
        """
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b
            # Resolve base - branch does NOT exist on remote yet
            (1, b"", b""),  # rev-parse --verify origin/fix/x (ec=1)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD -> found
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'  <-- only declarerd
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        assert result["staged_files"] == ["declared.txt"]
        assert result["swept_untracked"] == []

        # Verify only declared.txt was staged -- no personal.txt
        add_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git add" in str(c[0][0][2])
        ]
        assert len(add_calls) == 1, f"expected 1 git add call, got {len(add_calls)}"
        assert "declared.txt" in add_calls[0]
        assert "personal.txt" not in " ".join(add_calls)

        # Verify we reset to origin/HEAD (building on remote default)
        reset_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git reset" in str(c[0][0][2])
        ]
        assert any("git reset --mixed origin/HEAD" in c for c in reset_calls)

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_follow_up_push_preserves_prior_commits(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Criterion 2: follow-up publish onto a branch that already exists
        on the remote.  The base is resolved as origin/<branch>, so
        previously pushed commits are preserved (the new commit builds on
        top of them).  Only the declared file is added.
        """
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b / checkout existing
            # Resolve base: origin/fix/x DOES exist on remote
            (0, b"abc7890def1234", b""),  # rev-parse --verify origin/fix/x
            # No fallback to origin/HEAD -- we use origin/fix/x
            (0, b"", b""),  # git reset --mixed origin/fix/x
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        assert result["staged_files"] == ["declared.txt"]
        assert result["swept_untracked"] == []

        # Verify we reset to origin/fix/x (the existing remote branch),
        # NOT to origin/HEAD -- this preserves previously pushed commits.
        reset_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git reset" in str(c[0][0][2])
        ]
        assert any("git reset --mixed origin/fix/x" in c for c in reset_calls)

        # Verify no reset to origin/HEAD occurred
        assert not any("origin/HEAD" in c for c in reset_calls)

        # Verify only declared.txt was staged
        add_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git add" in str(c[0][0][2])
        ]
        assert len(add_calls) == 1
        assert "declared.txt" in add_calls[0]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_fallback_to_origin_main(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When origin/HEAD is absent, fallback to origin/main succeeds."""
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (1, b"", b""),  # rev-parse --verify origin/HEAD (not found)
            (0, b"abc1234", b""),  # rev-parse --verify origin/main (found)
            (0, b"", b""),  # git reset --mixed origin/main
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        reset_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git reset" in str(c[0][0][2])
        ]
        assert any("git reset --mixed origin/main" in c for c in reset_calls)

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_fallback_to_origin_master(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When origin/HEAD and origin/main are both absent, fallback to
        origin/master succeeds."""
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (1, b"", b""),  # rev-parse --verify origin/HEAD (not found)
            (1, b"", b""),  # rev-parse --verify origin/main (not found)
            (0, b"abc1234", b""),  # rev-parse --verify origin/master (found)
            (0, b"", b""),  # git reset --mixed origin/master
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        reset_calls = [
            c[0][0][2]
            for c in container.exec_run.call_args_list
            if "git reset" in str(c[0][0][2])
        ]
        assert any("git reset --mixed origin/master" in c for c in reset_calls)

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_no_remote_ref_fails(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When no remote ref can be resolved (no origin/HEAD, origin/main,
        or origin/master), manifest mode fails instead of silently skipping
        the reset."""
        container = _make_container_mock([
            (0, b"", b""),  # test -e 'declared.txt'
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (1, b"", b""),  # rev-parse --verify origin/HEAD (not found)
            (1, b"", b""),  # rev-parse --verify origin/main (not found)
            (1, b"", b""),  # rev-parse --verify origin/master (not found)
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "squash_reset"
        assert "Cannot resolve a remote base for manifest mode" in result["error"]


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

    def test_create_pr_idempotent_on_422_already_exists(self) -> None:
        """When POST pulls returns 422 because PR already exists, it should GET pulls and return the existing PR's html_url."""
        error_body = json.dumps({
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for owner:fix/x."}],
        }).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo/pulls",
            422,
            "Unprocessable Entity",
            None,  # type: ignore[arg-type]
            io.BytesIO(error_body),
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            # First call (POST pulls) -> HTTP 422
            # Second call (GET pulls) -> Return list with existing PR
            mock_urlopen.side_effect = [
                http_error,
                self._response([
                    {"html_url": "https://github.com/owner/repo/pull/7", "head": {"ref": "fix/x"}}
                ])
            ]
            url = _create_pr_via_api(
                "owner/repo", "fix/x", "Title", "", "dev", "ghp_tok"
            )

        assert url == "https://github.com/owner/repo/pull/7"
        assert mock_urlopen.call_count == 2
        req1 = mock_urlopen.call_args_list[0].args[0]
        assert req1.full_url == "https://api.github.com/repos/owner/repo/pulls"
        assert req1.get_method() == "POST"

        req2 = mock_urlopen.call_args_list[1].args[0]
        assert req2.full_url == "https://api.github.com/repos/owner/repo/pulls?head=owner:fix/x&state=open"
        assert req2.get_method() == "GET"
