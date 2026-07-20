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
from sunaba.tools.vcs.publishing import AutoIncludeResult
from tests.conftest import _decode, _exec_cmd, _make_client_mock, _make_publish_container

# Standard execute exec sequence shared by the one-shot non-manifest push
# tests.  Order matches publish()'s actual exec flow: git ls-files (capture
# untracked before the add), checkout -b, add, rev-parse @{u} (no upstream
# -> skip squash), commit, push, rev-parse HEAD.
_PUSH_SEQUENCE = [
    (0, b"", b""),  # git ls-files --others --exclude-standard
    (0, b"none\n", b""),  # MERGE_HEAD check
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
        container = _make_publish_container([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
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
        assert result["push_transport"] == "native"
        # No merge fields in non-merge publish
        assert "merge_discarded_sha" not in result
        assert "auto_include_applied" not in result
        assert "merge_discarded_undeclared" not in result

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

        container = _make_publish_container([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # git checkout -b
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

        container = _make_publish_container([])
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

        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
        container = _make_publish_container([])
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
        container = _make_publish_container([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
        container = _make_publish_container([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
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

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_push_failure(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Push failure (both transports) should return error status."""
        container = _make_publish_container([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
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
        container = _make_publish_container([
            (0, b"", b""),  # git ls-files --others --exclude-standard
            (0, b"none\n", b""),  # MERGE_HEAD check
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
        assert "SUNABA_ALLOWED_REPOS" in result["hint"]
        # Exactly 9 exec calls:
        #   8 positional (ls-files + MERGE_HEAD + checkout + add + rev-parse @{u}
        #                 + commit + failed push + rev-parse HEAD)
        # + 1 dispatched for the secret-scan diff-tree (run_secret_scan sees no files → no-op)
        # No _try_api_push was triggered.
        assert container.exec_run.call_count == 9

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_default_working_dir(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Default working_dir (None) auto-resolves, falling back to /home/sandbox."""
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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

        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
            cmd = _exec_cmd(call)
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))

        assert result["status"] == "pushed"

        # Index 5: [0]=ls-files, [1]=MERGE_HEAD, [2]=checkout, [3]=add,
        # [4]=rev-parse, [5]=commit
        commit_call = container.exec_run.call_args_list[5]
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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

        # Index 5: [0]=ls-files, [1]=MERGE_HEAD, [2]=checkout, [3]=add,
        # [4]=rev-parse, [5]=commit
        commit_call = container.exec_run.call_args_list[5]
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
        container = _make_publish_container([
            (0, b"typings/foo.pyi\ndirty.txt\n", b""),  # git ls-files
            (0, b"none\n", b""),  # MERGE_HEAD check
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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            # New path: resolve remote base
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []

        # Verify 'git add --' was used, not 'git add -A'
        add_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git add" in str(_exec_cmd(c))
        ]
        assert any("git add --" in c and "declared.txt" in c for c in add_calls)
        assert not any("git add -A" in c for c in add_calls)

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_reports_nonempty_worktree_leftover(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Undeclared leftovers (modified, untracked, renamed) are reported.

        ``git status --porcelain -z`` emits NUL-delimited verbatim paths
        (no C-quoting of non-ASCII); a rename entry carries its source
        path as an extra NUL-separated token.
        """
        porcelain = (
            b" M undeclared.py\x00"
            b"?? tmp/junk-\xe3\x81\x82.log\x00"
            b"R  new.py\x00old.py\x00"
        )
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- ':(literal)declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, porcelain, b""),  # git status --porcelain -z (leftovers)
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
        assert result["worktree_leftover"] == [
            "undeclared.py",
            "tmp/junk-あ.log",
            "new.py",
            "old.py",
        ]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_stages_untracked_declared_file(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode stages a declared file that is brand new / untracked.

        The existence check passes (test -f returns 0), and git add -- stages
        it successfully.  The new branch resolves origin/HEAD as the base.
        """
        container = _make_publish_container([(0, b"", b""),  # test -f 'newfile.py' (exists)

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            # New path: resolve remote base
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'newfile.py'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []

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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            # Resolve base: origin/fix/x does NOT exist on remote
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Msg", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []

        # Verify 'git reset --mixed origin/HEAD' was used (the new base path)
        reset_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git reset" in str(_exec_cmd(c))
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
        container = _make_publish_container([])
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
        container = _make_publish_container([])
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
        container = _make_publish_container([])
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
        """A declared path that does not exist (or is not a regular file) and is not tracked produces an error and no push."""
        container = _make_publish_container([
            (1, b"", b""),  # test -f 'missing.txt' -> not found
            (1, b"", b""),  # git ls-files --error-unmatch 'missing.txt' -> not tracked
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
        assert "regular file" in result["error"]
        # Only the existence checks (test -f + git ls-files) happened; nothing else
        assert container.exec_run.call_count == 2

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_rejects_dot_directory(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Declaring \".\" as a manifest path is rejected (directory, not regular file, not tracked)."""
        container = _make_publish_container([
            (1, b"", b""),  # test -f '.' -> not a regular file
            (1, b"", b""),  # git ls-files --error-unmatch '.' -> not tracked
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["."],
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        assert "." in result["error"]
        assert "regular file" in result["error"]
        # Only the existence checks happened; nothing else
        assert container.exec_run.call_count == 2

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_rejects_directory_path(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Declaring an existing directory that is not tracked produces a validation error."""
        container = _make_publish_container([
            (1, b"", b""),  # test -f 'some_dir' -> not a regular file
            (1, b"", b""),  # git ls-files --error-unmatch 'some_dir' -> not tracked
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["some_dir"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "validation"
        assert "some_dir" in result["error"]
        assert "regular file" in result["error"]
        # Only the existence checks happened; nothing else
        assert container.exec_run.call_count == 2

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_accepts_deletion_declaration(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A declared path that passes validation despite not being a regular
        file because it is tracked (deletion declaration) proceeds through
        the full publish flow.  Proves acceptance criterion #1 for #684."""
        container = _make_publish_container([
            (1, b"", b""),  # test -f 'deleted.txt' -> not a regular file
            (0, b"", b""),  # git ls-files --error-unmatch 'deleted.txt' -> tracked
            (1, b"", b""),  # rev-parse --verify HEAD^2 (NOT merge)
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b feat/del
            (1, b"", b""),  # rev-parse --verify origin/feat/del (not on remote)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD (found)
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'deleted.txt' (stages deletion)
            (0, b"[feat/del abc1234] Delete", b""),  # commit
            (0, b"", b""),  # git status --porcelain (clean)
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="feat/del",
            message="Delete todelete.txt",
            files=["deleted.txt"],
        ))

        assert result["status"] == "pushed"
        assert result["staged_files"] == ["deleted.txt"]
        assert result["worktree_leftover"] == []

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_no_manifest_untracked_rejection(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """No manifest + untracked files + no opt-in: error listing the files."""
        container = _make_publish_container([
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
        container = _make_publish_container(list(_PUSH_SEQUENCE))
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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            # Resolve base - branch does NOT exist on remote yet
            (1, b"", b""),  # rev-parse --verify origin/fix/x (ec=1)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD -> found
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'  <-- only declarerd
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []

        # Verify only declared.txt was staged -- no personal.txt
        add_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git add" in str(_exec_cmd(c))
        ]
        assert len(add_calls) == 1, f"expected 1 git add call, got {len(add_calls)}"
        assert "declared.txt" in add_calls[0]
        assert "personal.txt" not in " ".join(add_calls)

        # Verify we reset to origin/HEAD (building on remote default)
        reset_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git reset" in str(_exec_cmd(c))
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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b / checkout existing
            # Resolve base: origin/fix/x DOES exist on remote
            (0, b"abc7890def1234", b""),  # rev-parse --verify origin/fix/x
            # No fallback to origin/HEAD -- we use origin/fix/x
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/fix/x
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []

        # Verify we reset to origin/fix/x (the existing remote branch),
        # NOT to origin/HEAD -- this preserves previously pushed commits.
        reset_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git reset" in str(_exec_cmd(c))
        ]
        assert any("git reset --mixed origin/fix/x" in c for c in reset_calls)

        # Verify no reset to origin/HEAD occurred
        assert not any("origin/HEAD" in c for c in reset_calls)

        # Verify only declared.txt was staged
        add_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git add" in str(_exec_cmd(c))
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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (1, b"", b""),  # rev-parse --verify origin/HEAD (not found)
            (0, b"abc1234", b""),  # rev-parse --verify origin/main (found)
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/main
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []
        reset_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git reset" in str(_exec_cmd(c))
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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (not on remote)
            (1, b"", b""),  # rev-parse --verify origin/HEAD (not found)
            (1, b"", b""),  # rev-parse --verify origin/main (not found)
            (0, b"abc1234", b""),  # rev-parse --verify origin/master (found)
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/master
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z (no leftovers)
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
        assert result["worktree_leftover"] == []
        reset_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git reset" in str(_exec_cmd(c))
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
        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
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


class TestPublishSecretScanIntegration:
    """publish() driven end-to-end with the secret scan (issue #676).

    The scan module has its own unit tests, but nothing exercised it *through*
    publish -- which is how a bug that reset the manifest scan result to
    "clean" survived a green suite. These tests drive the real publish
    control flow with the scan stubbed at its boundary.
    """

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @staticmethod
    def _findings() -> dict:
        return {
            "secret_scan": "findings",
            "secret_scan_state": "findings",
            "files_scanned": ["declared.txt"],
            "findings": [
                {
                    "path": "declared.txt",
                    "line": 3,
                    "type": "AWS Access Key",
                }
            ],
            "scan_summary": "1 potential secret in 1 file",
        }

    @patch("sunaba.tools.secret_scan.check_override")
    @patch("sunaba.tools.secret_scan.run_secret_scan")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_findings_block_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_scan: MagicMock,
        mock_check: MagicMock,
    ) -> None:
        """A finding in a declared file blocks before anything is committed."""
        mock_scan.return_value = self._findings()
        mock_check.return_value = False

        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
        ])
        mock_docker.return_value = _make_client_mock(container)

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "error"
        assert result["step"] == "secret_scan"
        assert result["findings"][0]["path"] == "declared.txt"
        assert "secret_scan_override" in result["error"]

        # Nothing may be committed or pushed.
        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "git commit" not in issued
        assert "git push" not in issued

    @patch("sunaba.tools.secret_scan.consume_override")
    @patch("sunaba.tools.secret_scan.check_override")
    @patch("sunaba.tools.secret_scan.run_secret_scan")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_override_is_consumed_after_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_scan: MagicMock,
        mock_check: MagicMock,
        mock_consume: MagicMock,
    ) -> None:
        """An override that let a push through must be spent, not left live.

        Regression test: the manifest scan result used to be overwritten by a
        later re-initialisation, so this consume never fired and one human
        authorisation silently stayed valid for every subsequent publish.
        """
        mock_scan.return_value = self._findings()
        mock_check.return_value = True

        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x (absent)
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add -- 'declared.txt'
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        mock_docker.return_value = _make_client_mock(container)

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        mock_consume.assert_called_once()
        # The result must not claim the push was clean.
        assert result["secret_scan"] == "findings"

    @patch("sunaba.tools.secret_scan.consume_override")
    @patch("sunaba.tools.secret_scan.run_secret_scan")
    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_clean_scan_reports_scanned_files(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_scan: MagicMock,
        mock_consume: MagicMock,
    ) -> None:
        """A clean scan reports which files it actually looked at."""
        mock_scan.return_value = {
            "secret_scan": "clean",
            "secret_scan_state": "clean",
            "files_scanned": ["declared.txt"],
        }

        container = _make_publish_container([(0, b"", b""),  # test -f 'declared.txt'

            (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge) [issue #712]
            (0, b"none\n", b""),  # MERGE_HEAD check
            (0, b"", b""),  # checkout -b
            (1, b"", b""),  # rev-parse --verify origin/fix/x
            (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
            # [REMOVED] old HEAD^2 check moved before git_prepare_commit
            (0, b"", b""),  # git reset --mixed origin/HEAD
            (0, b"", b""),  # git add --
            (0, b"[fix/x abc1234] Fix", b""),  # commit
            (0, b"", b""),  # git status --porcelain -z
            (0, b"pushed", b""),  # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        mock_docker.return_value = _make_client_mock(container)

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["declared.txt"],
        ))

        assert result["status"] == "pushed"
        assert result["secret_scan"] == "clean"
        assert result["files_scanned"] == ["declared.txt"]
        # No override was involved, so none may be spent.
        mock_consume.assert_not_called()


# ============================================================================
# publish integration: _fetch_base_auto_include (issue #712 Candidate C)
# ============================================================================


class TestPublishBaseAutoInclude:
    """Integration tests for ``_fetch_base_auto_include`` in the publish flow.

    Follows the mock-at-the-call-site pattern from
    ``TestPublishHostSideBaseline``, which mocks
    ``_fetch_baseline_from_base_branch`` to verify host-side fetch behaviour.
    """

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_merge_triggers_fetch_and_threads_result(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When HEAD is a merge (HEAD^2 exists), publish() calls
        _fetch_base_auto_include and passes its return value as the
        base_auto_include argument to git_prepare_commit."""
        auto_include_data = AutoIncludeResult(
            included={"moved.txt": "moved content from host\n"},
            skipped=[],
        )

        # exec_run sequence: HEAD^2 exists (ec=0), so the merge branch
        # is entered and _fetch_base_auto_include is invoked.
        container = _make_publish_container([
            (0, b"", b""),                # test -f 'declared.txt'
            (0, b"abc123\n", b""),        # rev-parse --verify HEAD^2 (MERGE!)
            # Merge info capture (issue #711):
            (0, b"merge1234def5678\n", b""),  # rev-parse HEAD (merge SHA)
            (0, b"parent1111aaaa\n", b""),    # rev-parse HEAD^1
            (0, b"parent2222bbbb\n", b""),    # rev-parse HEAD^2
            (0, b"moved.txt\n", b""),         # git diff --name-only HEAD^1 HEAD
            (0, b"none\n", b""),          # MERGE_HEAD check
            (0, b"", b""),                # checkout -b
            (1, b"", b""),                # rev-parse --verify origin/fix/x
            (0, b"abc1234", b""),         # rev-parse --verify origin/HEAD
            (0, b"", b""),                # git reset --mixed origin/HEAD
            # auto-include: write moved.txt via base64+echo
            (0, b"", b""),                # echo | base64 -d > moved.txt
            (0, b"", b""),                # git add -- :(literal)moved.txt
            # declared file staging
            (0, b"", b""),                # git add -- :(literal)declared.txt
            (0, b"[fix/x abc1234] Fix\n", b""),  # commit
            (0, b"", b""),                # git status --porcelain -z
            (0, b"pushed", b""),          # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        mock_docker.return_value = _make_client_mock(container)

        with (
            patch(
                "sunaba.tools.vcs.publishing._resolve_vcs_token",
                return_value="ghp_test",
            ),
            patch(
                "sunaba.tools.vcs.publishing._fetch_base_auto_include",
                return_value=auto_include_data,
            ) as mock_fetch,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
            ))

        assert result["status"] == "pushed"

        # Verify _fetch_base_auto_include was called exactly once,
        # with the right arguments.
        mock_fetch.assert_called_once()
        call_args, call_kwargs = mock_fetch.call_args
        assert call_args == ("owner/repo", "ghp_test", "fix/x", "")
        assert call_kwargs == {}

        # The auto-included file is staged before the declared file.
        add_calls = [
            _exec_cmd(c)
            for c in container.exec_run.call_args_list
            if "git add" in str(_exec_cmd(c))
        ]
        assert len(add_calls) == 2, (
            f"expected 2 git add calls (auto-include + declared), "
            f"got {len(add_calls)}"
        )
        assert "moved.txt" in add_calls[0], "auto-include must be staged first"
        assert "declared.txt" in add_calls[1], "declared must be staged second"

        # Verify merge report fields (issue #711 AC 1-5)
        assert result["merge_discarded_sha"] == "merge12"
        assert result["merge_parents"] == ["parent1", "parent2"]
        assert result["auto_include_applied"] == ["moved.txt"]
        assert result["auto_include_skipped"] == []
        assert result["merge_discarded_undeclared"] == []
        assert result["push_transport"] == "native"

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_no_merge_does_not_call_fetch(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When HEAD^2 does not exist (not a merge), _fetch_base_auto_include
        is never called."""
        container = _make_publish_container([
            (0, b"", b""),                # test -f 'declared.txt'
            (1, b"", b""),                # rev-parse --verify HEAD^2 (NOT merge)
            (0, b"none\n", b""),          # MERGE_HEAD check
            (0, b"", b""),                # checkout -b
            (1, b"", b""),                # rev-parse --verify origin/fix/x
            (0, b"abc1234", b""),         # rev-parse --verify origin/HEAD
            (0, b"", b""),                # git reset --mixed origin/HEAD
            (0, b"", b""),                # git add -- :(literal)declared.txt
            (0, b"[fix/x abc1234] Fix\n", b""),  # commit
            (0, b"", b""),                # git status --porcelain -z
            (0, b"pushed", b""),          # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        mock_docker.return_value = _make_client_mock(container)

        with (
            patch(
                "sunaba.tools.vcs.publishing._resolve_vcs_token",
                return_value="ghp_test",
            ),
            patch(
                "sunaba.tools.vcs.publishing._fetch_base_auto_include",
            ) as mock_fetch,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
            ))

        assert result["status"] == "pushed"
        mock_fetch.assert_not_called()

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_merge_with_nonempty_undeclared_set(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When the merge touched paths beyond both the manifest and
        auto-include, merge_discarded_undeclared reports them (AC 4/5)."""
        auto_include_data = AutoIncludeResult(
            included={"base_advance.txt": "base content\n"},
            skipped=[],
        )

        # exec_run: merge, diff shows 3 paths.  manifest declares
        # declared.txt.  auto_include has base_advance.txt.  The third
        # path (checkpoint.txt) is neither → AC 4 set.
        container = _make_publish_container([
            (0, b"", b""),                # test -f 'declared.txt'
            (0, b"abc123\n", b""),        # rev-parse --verify HEAD^2 (MERGE!)
            # Merge info capture
            (0, b"mrg0000111aaaa\n", b""),   # rev-parse HEAD
            (0, b"p1aaaa1111bbb\n", b""),    # rev-parse HEAD^1
            (0, b"p2bbbb2222ccc\n", b""),    # rev-parse HEAD^2
            (0, b"declared.txt\nbase_advance.txt\ncheckpoint.txt\n", b""),  # diff HEAD^1 HEAD
            (0, b"none\n", b""),          # MERGE_HEAD check
            (0, b"", b""),                # checkout -b
            (1, b"", b""),                # rev-parse --verify origin/fix/x
            (0, b"abc1234", b""),         # rev-parse --verify origin/HEAD
            (0, b"", b""),                # git reset --mixed origin/HEAD
            # auto-include: write base_advance.txt
            (0, b"", b""),                # echo | base64 -d > base_advance.txt
            (0, b"", b""),                # git add -- :(literal)base_advance.txt
            # declared file staging
            (0, b"", b""),                # git add -- :(literal)declared.txt
            (0, b"[fix/x abc1234] Fix\n", b""),  # commit
            (0, b"", b""),                # git status --porcelain -z
            (0, b"pushed", b""),          # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        mock_docker.return_value = _make_client_mock(container)

        with (
            patch(
                "sunaba.tools.vcs.publishing._resolve_vcs_token",
                return_value="ghp_test",
            ),
            patch(
                "sunaba.tools.vcs.publishing._fetch_base_auto_include",
                return_value=auto_include_data,
            ),
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
            ))

        assert result["status"] == "pushed"
        # AC 4: checkpoint.txt is in the merge diff but neither declared
        # nor auto-included → appears in merge_discarded_undeclared.
        assert result["merge_discarded_undeclared"] == ["checkpoint.txt"]
        # AC 2: base_advance.txt was auto-included
        assert "base_advance.txt" in result["auto_include_applied"]
        # AC 5: non-empty set is reported as a non-empty list
        assert len(result["merge_discarded_undeclared"]) == 1
        assert result["push_transport"] == "native"

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_merge_with_auto_include_skipped(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Auto-include with skipped paths reports them in the response."""
        auto_include_data = AutoIncludeResult(
            included={"good.txt": "good content\n"},
            skipped=["renamed.txt", "noencoding.txt"],
        )

        container = _make_publish_container([
            (0, b"", b""),                # test -f 'declared.txt'
            (0, b"abc123\n", b""),        # rev-parse --verify HEAD^2 (MERGE!)
            # Merge info capture
            (0, b"mrg0000111aaaa\n", b""),   # rev-parse HEAD
            (0, b"p1aaaa1111bbb\n", b""),    # rev-parse HEAD^1
            (0, b"p2bbbb2222ccc\n", b""),    # rev-parse HEAD^2
            (0, b"good.txt\nrenamed.txt\nnoencoding.txt\ndeclared.txt\n", b""),
            (0, b"none\n", b""),          # MERGE_HEAD check
            (0, b"", b""),                # checkout -b
            (1, b"", b""),                # rev-parse --verify origin/fix/x
            (0, b"abc1234", b""),         # rev-parse --verify origin/HEAD
            (0, b"", b""),                # git reset --mixed origin/HEAD
            # auto-include: write good.txt
            (0, b"", b""),                # echo | base64 -d > good.txt
            (0, b"", b""),                # git add -- :(literal)good.txt
            # declared file staging
            (0, b"", b""),                # git add -- :(literal)declared.txt
            (0, b"[fix/x abc1234] Fix\n", b""),  # commit
            (0, b"", b""),                # git status --porcelain -z
            (0, b"pushed", b""),          # push
            (0, b"abc1234def5678", b""),  # rev-parse HEAD
        ])
        mock_docker.return_value = _make_client_mock(container)

        with (
            patch(
                "sunaba.tools.vcs.publishing._resolve_vcs_token",
                return_value="ghp_test",
            ),
            patch(
                "sunaba.tools.vcs.publishing._fetch_base_auto_include",
                return_value=auto_include_data,
            ),
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
            ))

        assert result["status"] == "pushed"
        # AC 3: skipped paths reported
        assert set(result["auto_include_skipped"]) == {"renamed.txt", "noencoding.txt"}
        # AC 2: good.txt was included
        assert "good.txt" in result["auto_include_applied"]
        # AC 4: declared.txt is in manifest so not in undeclared
        # renamed.txt and noencoding.txt are in merge diff but they are
        # reported as skipped, not as undeclared
        assert "declared.txt" not in result["merge_discarded_undeclared"]
        assert result["push_transport"] == "native"

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_merge_with_api_push_transport(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When git push fails and API fallback succeeds, push_transport
        is \"api\" (AC 7)."""
        auto_include_data = AutoIncludeResult(
            included={"base.txt": "base\n"},
            skipped=[],
        )

        container = _make_publish_container([
            (0, b"", b""),                # test -f 'declared.txt'
            (0, b"abc123\n", b""),        # rev-parse --verify HEAD^2 (MERGE!)
            # Merge info capture
            (0, b"mrg0000111aaaa\n", b""),   # rev-parse HEAD
            (0, b"p1aaaa1111bbb\n", b""),    # rev-parse HEAD^1
            (0, b"p2bbbb2222ccc\n", b""),    # rev-parse HEAD^2
            (0, b"base.txt\ndeclared.txt\n", b""),
            (0, b"none\n", b""),          # MERGE_HEAD check
            (0, b"", b""),                # checkout -b
            (1, b"", b""),                # rev-parse --verify origin/fix/x
            (0, b"abc1234", b""),         # rev-parse --verify origin/HEAD
            (0, b"", b""),                # git reset --mixed origin/HEAD
            # auto-include: write base.txt
            (0, b"", b""),                # echo | base64 -d > base.txt
            (0, b"", b""),                # git add -- :(literal)base.txt
            # declared file staging
            (0, b"", b""),                # git add -- :(literal)declared.txt
            (0, b"[fix/x abc1234] Fix\n", b""),  # commit
            (0, b"", b""),                # git status --porcelain -z
            (1, b"", b"remote rejected"), # git push FAILS → API fallback
            (0, b"apisha1234567", b""),   # rev-parse HEAD (used for sha on failure)
        ])
        mock_docker.return_value = _make_client_mock(container)

        with (
            patch(
                "sunaba.tools.vcs.publishing._resolve_vcs_token",
                return_value="ghp_test",
            ),
            patch(
                "sunaba.tools.vcs.publishing._fetch_base_auto_include",
                return_value=auto_include_data,
            ),
            patch(
                "sunaba.tools.vcs.publishing._try_api_push",
                return_value={"status": "ok", "sha": "apisha9"},
            ),
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
            ))

        assert result["status"] == "pushed"
        assert result["push_transport"] == "api"
        # Merge fields should still be present
        assert "merge_discarded_sha" in result
        assert "auto_include_applied" in result
