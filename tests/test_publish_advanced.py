"""Tests for publish advanced features: squash, force push, API fallback, token routing."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.vcs import publish
from tests.conftest import _decode, _make_client_mock, _make_container_mock


class TestPublishSquashCheckpoints:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for publish with automatic checkpoint squash."""

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_squash_checkpoints(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Publish should squash unpushed checkpoints with reset --soft."""
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
        ))

        assert result["status"] == "pushed"
        reset_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "reset --soft" in c[0][0][2]
        ]
        assert len(reset_calls) == 1

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_squash_checkpoints_no_tracking(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Publish with no tracking branch should skip squash."""
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
        ))

        assert result["status"] == "pushed"


class TestPublishAllowForcePush:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for publish with allow_force_push=True."""

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_allow_force_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """allow_force_push=True should include --force in push command."""
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
            allow_force_push=True,
        ))

        assert result["status"] == "pushed"
        push_calls = [
            c[0][0][2] for c in container.exec_run.call_args_list
            if "push origin" in c[0][0][2]
        ]
        assert len(push_calls) == 1
        assert "--force" in push_calls[0]


class TestPublishApiPushFallback:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for publish when git push fails and falls back to _try_api_push."""

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_api_push_fallback_succeeds(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When git push fails, _try_api_push should be used as fallback."""
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
        ))

        assert result["status"] == "pushed"
        assert result["sha"] == "bbbbbbb"

    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_execute_api_push_fallback_fails(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """When both git push and API push fail, return error."""
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
        ))

        assert result["status"] == "error"
        assert result["step"] == "git_push"


class TestPublishLazyTokenInjection:
    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    """Tests for lazy VCS-token injection at push time (Issue #347).

    The token is resolved host-side and handed only to the push / PR
    execs, so a container that carries no VCS token of its own can still
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

    @patch("sunaba.tools.vcs._resolve_vcs_token")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_token_injected_into_push_exec_only(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """A host-resolved token reaches the push exec but not read-only execs."""
        mock_resolve.return_value = "ghs_lazytoken"

        container = _make_container_mock(self._simple_push_returns())
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
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

    @patch("sunaba.tools.vcs._resolve_vcs_token")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_no_host_token_leaves_push_env_unset(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """With no host token, the push exec env is None.

        The container carries no VCS token of its own (#356/#439), so there
        is no credential to fall back on -- the push proceeds without one.
        """
        mock_resolve.return_value = ""

        container = _make_container_mock(self._simple_push_returns())
        client = _make_client_mock(container)
        mock_docker.return_value = client

        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
        ))
        assert result["status"] == "pushed"

        calls = container.exec_run.call_args_list
        assert all(self._env_of(c) is None for c in calls)

    @patch("sunaba.tools.vcs._resolve_vcs_token")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_token_injected_into_api_push_fallback(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        """When git push fails, the API-push script exec carries the token."""
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

    @patch("sunaba.tools.vcs.authorized_push_grant")
    @patch("sunaba.tools.vcs.proxy_configured", return_value=True)
    @patch("sunaba.tools.vcs._resolve_vcs_token")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_push_exec_token_free_and_grant_carries_credential(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
        mock_proxied: MagicMock,
        mock_grant: MagicMock,
    ) -> None:
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
        ))
        assert result["status"] == "pushed"

        # The credential rode the authorization grant to the proxy...
        mock_grant.assert_called_once_with("owner/repo", token="ghs_lazytoken")
        # ...and no exec in the container ever saw it.
        calls = container.exec_run.call_args_list
        assert calls  # sanity
        assert all(self._env_of(c) is None for c in calls)

    @patch("sunaba.tools.vcs.authorized_push_grant")
    @patch("sunaba.tools.vcs.proxy_configured", return_value=True)
    @patch("sunaba.tools.vcs._resolve_vcs_token")
    @patch("sunaba.tools.vcs._docker")
    @patch("sunaba.tools.vcs.record_boundary_crossing")
    def test_pr_create_runs_host_side_no_exec_token(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
        mock_resolve: MagicMock,
        mock_proxied: MagicMock,
        mock_grant: MagicMock,
    ) -> None:
        """PR creation is host-side (#360): no exec ever carries a token."""
        mock_resolve.return_value = "ghs_lazytoken"

        returns = TestPublishLazyTokenInjection._simple_push_returns()
        container = _make_container_mock(returns)
        client = _make_client_mock(container)
        mock_docker.return_value = client

        with patch(
            "sunaba.tools.vcs._create_pr_via_api",
            return_value="https://github.com/owner/repo/pull/9",
        ) as mock_create_pr:
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
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

    @patch("sunaba.tools.vcs.token_broker.mint_token")
    def test_prefers_minted_broker_token(self, mock_mint: MagicMock) -> None:
        from sunaba.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = "ghs_minted"
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}):
            assert _resolve_vcs_token() == "ghs_minted"

    @patch("sunaba.tools.vcs.token_broker.mint_token")
    def test_falls_back_to_static_env(self, mock_mint: MagicMock) -> None:
        from sunaba.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = None
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}, clear=True):
            assert _resolve_vcs_token() == "ghs_static"

    @patch("sunaba.tools.vcs.token_broker.mint_token")
    @patch("sunaba.tools.vcs.github_auth.get_global_provider")
    def test_prefers_global_provider_over_static_env(
        self, mock_get_provider: MagicMock, mock_mint: MagicMock
    ) -> None:
        from sunaba.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = None
        mock_provider = MagicMock()
        mock_provider.get_token.return_value = "ghs_provider_tok"
        mock_get_provider.return_value = mock_provider
        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_static"}, clear=True):
            assert _resolve_vcs_token() == "ghs_provider_tok"

    @patch("sunaba.tools.vcs.token_broker.mint_token")
    def test_empty_when_no_token_available(self, mock_mint: MagicMock) -> None:
        from sunaba.tools.vcs import _resolve_vcs_token

        mock_mint.return_value = None
        with patch.dict("os.environ", {}, clear=True):
            assert _resolve_vcs_token() == ""
