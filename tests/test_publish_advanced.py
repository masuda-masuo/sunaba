"""Tests for publish advanced features: token flow, squash, force push, API fallback."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.vcs import publish
from tests.conftest import _decode, _make_client_mock, _make_container_mock


class TestPublishTokenFlow:
    """Integration tests for the dry_run → approve → execute flow."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_dry_run_generates_usable_token(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Token from dry_run should be usable for execute."""
        container = _make_container_mock([
            (0, b"M file.py\n---DIFF---\n 1 file changed", b""),
            (0, b"", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        dry_result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
        ))
        assert dry_result["status"] == "dry_run"
        token = dry_result["confirmation_token"]
        assert len(token) > 0

        from code_sandbox_mcp.token import verify_and_consume
        approval = verify_and_consume(token)
        assert approval is not None

        second_consume = verify_and_consume(token)
        assert second_consume is None


class TestPublishSquashCheckpoints:
    """Tests for publish with automatic checkpoint squash."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_squash_checkpoints(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Submit should squash unpushed checkpoints with reset --soft."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (0, b"main\n", b""),
            (0, b"abc1234 First checkpoint\n", b""),
            (0, b"", b""),
            (0, b"", b""),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"
        reset_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "reset --soft" in c[0][0][2]
        ]
        assert len(reset_calls) == 1

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_squash_checkpoints_no_tracking(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Submit with no tracking branch should skip squash."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"


class TestPublishAllowForcePush:
    """Tests for publish with allow_force_push=True."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_allow_force_push(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """allow_force_push=True should include --force in push command."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"pushed", b""),
            (0, b"abc1234def5678", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
            allow_force_push=True,
        ))

        assert result["status"] == "pushed"
        push_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "push origin" in c[0][0][2]
        ]
        assert len(push_calls) == 1
        assert "--force" in push_calls[0]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_dry_run_force_push_diverged_target_issues_token(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """#272: force-push dry_run must issue a token when HEAD diverges from
        origin/<branch>, even though there are no uncommitted changes and the
        --not --remotes check finds nothing (commits reachable from another
        remote branch)."""
        container = _make_container_mock([
            (0, b"---DIFF---\n", b""),   # git status/diff: clean working tree
            (0, b"", b""),               # unpushed vs --remotes: false-negative
            (0, b"1" * 40 + b"\n", b""),  # git rev-parse HEAD
            (0, b"2" * 40 + b"\n", b""),  # git rev-parse origin/fix/x (differs)
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
            allow_force_push=True,
        ))

        assert result["status"] == "dry_run"
        assert result["confirmation_token"]
        assert "force push" in result["diff_summary"].lower()

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_dry_run_force_push_target_equal_reports_no_changes(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """When origin/<branch> already equals HEAD there is nothing to force,
        so the dry_run stays a no-op (no token)."""
        container = _make_container_mock([
            (0, b"---DIFF---\n", b""),   # clean working tree
            (0, b"", b""),               # nothing unpushed
            (0, b"1" * 40 + b"\n", b""),  # git rev-parse HEAD
            (0, b"1" * 40 + b"\n", b""),  # origin/fix/x identical to HEAD
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
            allow_force_push=True,
        ))

        assert result["status"] == "dry_run"
        assert result["diff_summary"] == "(no changes detected)"
        assert "confirmation_token" not in result

    @patch("code_sandbox_mcp.tools.vcs._docker")
    def test_dry_run_without_force_flag_keeps_no_change_shortcut(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Without allow_force_push the target-divergence probe is skipped, so
        the same false-negative inputs still report no changes (the force flag
        is what unlocks the token) -- and the extra rev-parse calls are not
        made."""
        container = _make_container_mock([
            (0, b"---DIFF---\n", b""),   # clean working tree
            (0, b"", b""),               # nothing unpushed
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        assert result["diff_summary"] == "(no changes detected)"
        assert "confirmation_token" not in result
        rev_parse_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "rev-parse" in c[0][0][2]
        ]
        assert rev_parse_calls == []


class TestPublishApiPushFallback:
    """Tests for publish when git push fails and falls back to _try_api_push."""

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_api_push_fallback_succeeds(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When git push fails, _try_api_push should be used as fallback."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        push_json = json.dumps({"sha": "b" * 40}).encode()
        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (1, b"", b"remote rejected: permission denied"),
            (0, b"abc1234def5678", b""),
            (0, b"", b""),
            (0, push_json, b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "pushed"
        assert result["sha"] == "bbbbbbb"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_execute_api_push_fallback_fails(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When both git push and API push fail, return error."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }

        container = _make_container_mock([
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (1, b"", b"remote rejected"),
            (0, b"abc1234def5678", b""),
            (0, b"", b""),
            (1, b"", b"API push failed too"),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_dry_run_with_squash_and_force(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_consume: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Dry run should show checkpoint info when unpushed commits exist."""
        mock_run_id.return_value = "run123"
        mock_consume.return_value = {"token": "tok_good"}

        container = _make_container_mock([
            (0, b"M file.py\n---DIFF---\n 1 file changed", b""),
            (0, b"abc1234 First checkpoint\ndef5678 Second checkpoint\n", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
            allow_force_push=True,
        ))

        assert result["status"] == "dry_run"
        assert "Checkpoints to squash" in result["diff_summary"]
        assert "2 commit(s)" in result["diff_summary"]

    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    def test_dry_run_only_checkpoints(
        self,
        mock_consume: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Dry run should work with no working tree changes but unpushed commits."""
        mock_consume.return_value = {"token": "tok_good"}

        container = _make_container_mock([
            (0, b"---DIFF---", b""),
            (0, b"abc1234 Only checkpoint\n", b""),
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=True,
        ))

        assert result["status"] == "dry_run"
        assert "unpushed checkpoints" in result["diff_summary"]
        assert "Checkpoints to squash: 1 commit(s)" in result["diff_summary"]


class TestPublishLazyTokenInjection:
    """Tests for lazy VCS-token injection at push time (Issue #347).

    The token is resolved host-side and handed only to the push / PR
    execs, so a container started *without* ``inject_vcs_token`` can still
    publish, while read-only git execs never see a credential.
    """

    @staticmethod
    def _simple_push_returns() -> list[tuple[int, bytes, bytes]]:
        # checkout, git add, no-upstream (skip squash), commit, push, HEAD
        return [
            (0, b"", b""),
            (0, b"", b""),
            (1, b"", b"no upstream"),
            (0, b"[fix/x abc1234] Fix", b""),
            (0, b"", b""),
            (0, b"abc1234def5678", b""),
        ]

    @staticmethod
    def _env_of(call) -> dict | None:
        # exec_run is called as exec_run([...], stdout=, stderr=, environment=)
        return call.kwargs.get("environment")

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_token_injected_into_push_exec_only(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """A host-resolved token reaches the push exec but not read-only execs."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_resolve.return_value = "ghs_lazytoken"

        container = _make_container_mock(self._simple_push_returns())
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))
        assert result["status"] == "pushed"

        calls = container.exec_run.call_args_list
        push_calls = [c for c in calls if "push origin" in c.args[0][2]]
        assert len(push_calls) == 1
        push_env = self._env_of(push_calls[0])
        assert push_env == {
            "GITHUB_TOKEN": "ghs_lazytoken",
            "GH_TOKEN": "ghs_lazytoken",
        }

        # Least-privilege: read-only git execs carry no credential.
        readonly_calls = [
            c for c in calls if "push origin" not in c.args[0][2]
        ]
        assert readonly_calls  # sanity
        assert all(self._env_of(c) is None for c in readonly_calls)

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_no_host_token_leaves_push_env_unset(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """With no host token, the push exec env is None (backward compat).

        The container falls back to whatever credential it already carries
        (e.g. a startup-injected token), so behaviour is unchanged for
        containers started with ``inject_vcs_token=True``.
        """
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_resolve.return_value = ""

        container = _make_container_mock(self._simple_push_returns())
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))
        assert result["status"] == "pushed"

        calls = container.exec_run.call_args_list
        assert all(self._env_of(c) is None for c in calls)

    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_token_injected_into_api_push_fallback(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """When git push fails, the API-push script exec carries the token."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_resolve.return_value = "ghs_lazytoken"

        push_json = json.dumps({"sha": "b" * 40}).encode()
        container = _make_container_mock([
            (0, b"", b""),              # checkout
            (0, b"", b""),              # git add
            (1, b"", b"no upstream"),  # skip squash
            (0, b"[fix/x abc] Fix", b""),  # commit
            (1, b"", b"permission denied"),  # git push fails
            (0, b"abc1234def5678", b""),     # rev-parse HEAD
            (0, b"", b""),              # api-push: write script
            (0, push_json, b""),        # api-push: run script
        ])
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))
        assert result["status"] == "pushed"
        assert result["sha"] == "bbbbbbb"

        calls = container.exec_run.call_args_list
        script_calls = [
            c for c in calls if "_sandbox_create_pr.py" in c.args[0][-1]
            and "python3" in c.args[0][-1]
        ]
        assert len(script_calls) == 1
        assert script_calls[0].kwargs.get("environment") == {
            "GITHUB_TOKEN": "ghs_lazytoken",
            "GH_TOKEN": "ghs_lazytoken",
        }


class TestPublishProxiedCredentialRouting:
    """With the egress proxy configured the credential goes to the proxy (#356).

    The push exec and the API-push fallback must stay token-free (the proxy
    injects ``Authorization`` into the authorized push itself, and a token in
    the fallback would let the Objects API bypass the proxy gate); only the
    gh-pr-create exec still carries it, because api.github.com writes are not
    proxy-gated yet (#360).
    """

    @staticmethod
    def _env_of(call) -> dict | None:
        return call.kwargs.get("environment")

    @patch("code_sandbox_mcp.tools.vcs.authorized_push_window")
    @patch("code_sandbox_mcp.tools.vcs.proxy_configured", return_value=True)
    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_push_exec_token_free_and_window_carries_credential(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
        mock_proxied: MagicMock,
        mock_window: MagicMock,
    ) -> None:
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_resolve.return_value = "ghs_lazytoken"

        container = _make_container_mock(
            TestPublishLazyTokenInjection._simple_push_returns()
        )
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            dry_run=False,
            token="tok_good",
        ))
        assert result["status"] == "pushed"

        # The credential rode the authorization window to the proxy...
        mock_window.assert_called_once_with("owner/repo", token="ghs_lazytoken")
        # ...and no exec in the container ever saw it.
        calls = container.exec_run.call_args_list
        assert calls  # sanity
        assert all(self._env_of(c) is None for c in calls)

    @patch("code_sandbox_mcp.tools.vcs.authorized_push_window")
    @patch("code_sandbox_mcp.tools.vcs.proxy_configured", return_value=True)
    @patch("code_sandbox_mcp.tools.vcs._resolve_vcs_token")
    @patch("code_sandbox_mcp.tools.vcs._docker")
    @patch("code_sandbox_mcp.tools.vcs.verify_and_consume")
    @patch("code_sandbox_mcp.tools.vcs.record_boundary_crossing")
    @patch("code_sandbox_mcp.tools.vcs.get_or_create_run_id")
    def test_pr_create_runs_host_side_no_exec_token(
        self,
        mock_run_id: MagicMock,
        mock_record: MagicMock,
        mock_token: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
        mock_proxied: MagicMock,
        mock_window: MagicMock,
    ) -> None:
        """PR creation is host-side (#360): no exec ever carries a token."""
        mock_run_id.return_value = "run123"
        mock_token.return_value = {
            "token": "tok_good",
            "operation": "publish",
            "details": "...",
            "container_id": "abc123def456",
            "run_id": "run123",
        }
        mock_resolve.return_value = "ghs_lazytoken"

        returns = TestPublishLazyTokenInjection._simple_push_returns()
        container = _make_container_mock(returns)
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "code_sandbox_mcp.tools.vcs._create_pr_via_api",
            return_value="https://github.com/owner/repo/pull/9",
        ) as mock_create_pr:
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                dry_run=False,
                token="tok_good",
                create_pr=True,
                pr_title="Fix",
            ))
        assert result["status"] == "pushed"
        assert result["pr_url"] == "https://github.com/owner/repo/pull/9"

        # The PR was created host-side with the host-resolved token...
        mock_create_pr.assert_called_once_with(
            "owner/repo", "fix/x", "Fix", "", "", "ghs_lazytoken"
        )
        calls = container.exec_run.call_args_list
        # ...no gh exec ran in the container, and no exec carried a token —
        # the container stays credential-free end to end under the proxy.
        assert not [c for c in calls if "gh pr create" in c.args[0][2]]
        assert all(self._env_of(c) is None for c in calls)
        push_calls = [c for c in calls if "push origin" in c.args[0][2]]
        assert len(push_calls) == 1


class TestResolvePushToken:
    """Unit tests for the host-side token resolver (Issue #347)."""

    @patch("code_sandbox_mcp.tools.vcs.token_broker.mint_token")
    def test_prefers_minted_broker_token(self, mock_mint: MagicMock) -> None:
        from code_sandbox_mcp.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = "ghs_minted"
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}):
            assert _resolve_vcs_token() == "ghs_minted"

    @patch("code_sandbox_mcp.tools.vcs.token_broker.mint_token")
    def test_falls_back_to_static_env(self, mock_mint: MagicMock) -> None:
        from code_sandbox_mcp.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = None
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}, clear=True):
            assert _resolve_vcs_token() == "ghs_static"

    @patch("code_sandbox_mcp.tools.vcs.token_broker.mint_token")
    def test_empty_when_no_token_available(self, mock_mint: MagicMock) -> None:
        from code_sandbox_mcp.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            assert _resolve_vcs_token() == ""
