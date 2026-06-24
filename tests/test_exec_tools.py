"""Tests for the exec MCP tools (sandbox_exec / sandbox_exec_background / sandbox_exec_check)."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import (
    sandbox_exec,
    sandbox_exec_background,
)
from code_sandbox_mcp.tools.container import (
    _container_env,
    sandbox_initialize,
)


class TestContainerEnv:
    """Tests for the _container_env helper (Issue #57 token isolation)."""

    def test_default_no_vcs_token(self) -> None:
        """inject_vcs_token=False should return empty dict even if env vars are set."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            env = _container_env(inject_vcs_token=False)
            assert env == {}

    def test_inject_vcs_token_injects_github_token(self) -> None:
        """inject_vcs_token=True should include GITHUB_TOKEN when set."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            env = _container_env(inject_vcs_token=True)
            assert env.get("GITHUB_TOKEN") == "ghp_fake"

    def test_inject_vcs_token_injects_gh_token(self) -> None:
        """inject_vcs_token=True should include GH_TOKEN when set."""
        with patch.dict(os.environ, {"GH_TOKEN": "gho_fake"}, clear=True):
            env = _container_env(inject_vcs_token=True)
            assert env.get("GH_TOKEN") == "gho_fake"

    def test_inject_vcs_token_injects_github_token_source(self) -> None:
        """inject_vcs_token=True should include GITHUB_TOKEN_SOURCE when set."""
        with patch.dict(os.environ, {"GITHUB_TOKEN_SOURCE": "ghs_fake"}, clear=True):
            env = _container_env(inject_vcs_token=True)
            assert env.get("GITHUB_TOKEN_SOURCE") == "ghs_fake"

    def test_inject_vcs_token_only_injects_set_vars(self) -> None:
        """Only vars that are set in the environment should be injected."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            env = _container_env(inject_vcs_token=True)
            assert "GITHUB_TOKEN" in env
            assert "GH_TOKEN" not in env
            assert "GITHUB_TOKEN_SOURCE" not in env

    def test_inject_vcs_token_empty_when_no_vars_set(self) -> None:
        """No env vars set should return empty dict even with inject_vcs_token=True."""
        with patch.dict(os.environ, {}, clear=True):
            env = _container_env(inject_vcs_token=True)
            assert env == {}


class TestSandboxInitialize:
    """Tests for sandbox_initialize."""

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_inject_vcs_token_passed_to_container_env(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """inject_vcs_token=True should pass through to _container_env."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            result = sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                inject_vcs_token=True,
            )

        assert result == "abc123def456"
        # Verify environment was passed with the token
        call_kwargs = mock_client.containers.run.call_args[1]
        assert call_kwargs["environment"].get("GITHUB_TOKEN") == "ghp_fake"

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_inject_vcs_token_false_omits_tokens(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """inject_vcs_token=False should omit VCS tokens from environment."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            result = sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                inject_vcs_token=False,
            )

        assert result == "abc123def456"
        call_kwargs = mock_client.containers.run.call_args[1]
        assert "GITHUB_TOKEN" not in call_kwargs["environment"]


class TestSandboxExec:
    """Tests for sandbox_exec."""


    def _decode(self, result: str) -> dict:
        return json.loads(result)

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_success_returns_ok_with_output(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Success should return JSON with status 'ok' and stdout."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"hello world", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo hello"],
        ))
        assert result["status"] == "ok"
        assert "hello world" in result["output"]

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_success_empty_stdout(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Success with empty output should return status 'ok' with empty output."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo -n"],
        ))
        assert result["status"] == "ok"
        assert result["output"] == ""

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_failure_returns_both_stdout_and_stderr(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Failure should return JSON with status 'error', exit_code, stdout, and stderr."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"stdout output", b"stderr output"))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["false"],
        ))
        assert result["status"] == "error"
        assert result["exit_code"] == 1
        assert "stdout output" in result["output"]
        assert "stderr output" in result["stderr"]

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_failure_preserves_stdout(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Failure must NOT discard stdout - this is the bug fix (issue #52)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            1,
            (b"pytest failure summary: assert 1 == 2", b"some stderr"),
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["pytest"],
        ))
        assert result["status"] == "error"
        assert "pytest failure summary" in result["output"]
        assert result["exit_code"] == 1

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_container_not_found(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Container not found should return JSON with status 'error'."""
        from docker.errors import NotFound

        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo hello"],
        ))
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_docker_exception(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Docker API exception should return JSON with status 'error'."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo hello"],
        ))
        assert result["status"] == "error"
        assert "connection refused" in result["error"]

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_verbose_full_shows_all_output(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """verbose='full' should include all output without truncation."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"line1\nline2\nline3", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo lines"],
            verbose="full",
        ))
        assert result["status"] == "ok"
        assert result["shown"] == 3
        assert result["truncated"] is False

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_verbose_error_only_hides_success_output(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """verbose='error_only' should hide output on success."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"some output", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo hello"],
            verbose="error_only",
        ))
        assert result["status"] == "ok"
        assert result["output"] == ""

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_verbose_error_only_shows_on_failure(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """verbose='error_only' should show output on failure."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (1, (b"error details", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["false"],
            verbose="error_only",
        ))
        assert result["status"] == "error"
        assert "error details" in result["output"]

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_timeout_status_on_exit_124(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Issue #131: timeout=N returns status 'timeout' when exit_code is 124."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (124, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["sleep 60"],
            timeout=5,
        ))
        assert result["status"] == "timeout"
        assert result["exit_code"] == 124

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_timeout_zero_not_applied(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """timeout=0 (default) does not wrap command with timeout(1)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"ok", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec(container_id="abc123def456", commands=["echo ok"])

        cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "timeout" not in cmd

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_exit_124_without_timeout_is_error(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """exit_code=124 without timeout set is status 'error', not 'timeout'."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (124, (b"", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["exit 124"],
        ))
        assert result["status"] == "error"

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_negative_timeout_returns_error(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """timeout < 0 is rejected immediately with a clear error."""
        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo ok"],
            timeout=-1,
        ))
        assert result["status"] == "error"
        assert "timeout" in result["error"]
        mock_docker.assert_not_called()


    @patch("code_sandbox_mcp.tools.exec._docker")
    @patch("code_sandbox_mcp.tools.exec.get_cached_result")
    def test_cache_hit_returns_cached_result(
        self,
        mock_get_cache: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_get_cache.return_value = {"status": "ok", "output": "cached output", "exit_code": 0}
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        result = json.loads(sandbox_exec(
            container_id="abc123def456",
            commands=["echo hello"],
        ))
        assert result["status"] == "ok"
        assert "cached output" in result["output"]
        assert result.get("cached") is True
        mock_container.exec_run.assert_not_called()

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_max_output_tokens_triggers_truncation(
        self,
        mock_docker: MagicMock,
    ) -> None:
        long_output = "\n".join(f"line {i}" for i in range(200))
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (long_output.encode(), b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        result = json.loads(sandbox_exec(
            container_id="abc123def456",
            commands=["echo long"],
            max_output_tokens=10,
        ))
        assert result["status"] == "ok"
        assert "truncated" in result.get("output", "")


    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_working_dir_prepends_cd(
        self,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.image.tags = ["test-image:latest"]
        mock_container.exec_run.return_value = (0, (b"hello", b""))

        result = sandbox_exec(
            "abc123def456",
            ["echo done"],
            working_dir="/tmp/repo/code-sandbox-mcp",
        )

        parsed = json.loads(result)
        assert parsed["status"] == "ok"

        # Verify cd command was prepended
        call_args = mock_container.exec_run.call_args
        cmd = call_args[0][0][2]  # The shell -c argument
        # Commands are base64-encoded; decode to check
        import base64
        # Extract base64 part: between 'echo ' and ' | base64 -d >'
        b64_start = cmd.index("echo ") + 5
        b64_end = cmd.index(" | base64 -d")
        decoded = base64.b64decode(cmd[b64_start:b64_end]).decode()
        assert "cd /tmp/repo/code-sandbox-mcp" in decoded
        assert "echo done" in decoded

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_working_dir_empty_does_nothing(
        self,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.image.tags = ["test-image:latest"]
        mock_container.exec_run.return_value = (0, (b"hello", b""))

        result = sandbox_exec(
            "abc123def456",
            ["echo done"],
            working_dir="",
        )

        parsed = json.loads(result)
        assert parsed["status"] == "ok"

        call_args = mock_container.exec_run.call_args
        cmd = call_args[0][0][2]
        assert "cd " not in cmd

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_background_working_dir_prepends_cd(
        self,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec_background(
            "abc123def456",
            ["echo done"],
            working_dir="/tmp/repo/code-sandbox-mcp",
        )

        call_args = mock_container.exec_run.call_args
        cmd = call_args[0][0][2]
        # Commands are base64-encoded inside the inner_cmd; extract and decode
        import base64
        inner_start = cmd.index("echo ") + 5
        inner_end = cmd.index(" | base64 -d >")
        decoded = base64.b64decode(cmd[inner_start:inner_end]).decode()
        assert "cd /tmp/repo/code-sandbox-mcp" in decoded
        assert "echo done" in decoded



class TestServerArgs:
    """Tests for server argument parsing using the actual parser."""

    def test_default_transport_is_stdio(self) -> None:
        """Default transport should be stdio."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args([])
        assert args.transport == "stdio"

    def test_sse_transport_parsed(self) -> None:
        """--transport sse should be parsed correctly."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args(["--transport", "sse", "--host", "0.0.0.0", "--port", "9876"])
        assert args.transport == "sse"
        assert args.host == "0.0.0.0"
        assert args.port == 9876

    def test_http_transport_parsed(self) -> None:
        """--transport http should be parsed correctly."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args(["--transport", "http"])
        assert args.transport == "http"

    def test_streamable_http_transport_parsed(self) -> None:
        """--transport streamable-http should be parsed correctly."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args(["--transport", "streamable-http"])
        assert args.transport == "streamable-http"

    def test_default_host_port(self) -> None:
        """Default host and port should be 127.0.0.1:8765."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args([])
        assert args.host == "127.0.0.1"
        assert args.port == 8765


class TestContainerEnvBroker:
    """_container_env broker integration (Issue #232): COMMAND-first, static fallback."""

    def test_minted_token_takes_precedence_over_static(self) -> None:
        """A freshly minted broker token overrides the static GITHUB_TOKEN."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_static"}, clear=True):
            with patch(
                "code_sandbox_mcp.tools.container.token_broker.mint_token",
                return_value="ghs_fresh",
            ):
                env = _container_env(inject_vcs_token=True)
                assert env["GITHUB_TOKEN"] == "ghs_fresh"

    def test_broker_failure_falls_back_to_static_token(self) -> None:
        """When the broker yields nothing, the static GITHUB_TOKEN is used."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_static"}, clear=True):
            with patch(
                "code_sandbox_mcp.tools.container.token_broker.mint_token",
                return_value=None,
            ):
                env = _container_env(inject_vcs_token=True)
                assert env["GITHUB_TOKEN"] == "ghp_static"

    def test_no_sources_is_noop(self) -> None:
        """No broker and no static token yields an empty env."""
        with patch.dict(os.environ, {}, clear=True):
            with patch(
                "code_sandbox_mcp.tools.container.token_broker.mint_token",
                return_value=None,
            ):
                assert _container_env(inject_vcs_token=True) == {}
