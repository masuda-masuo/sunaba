"""Tests for the exec MCP tools (sandbox_exec / sandbox_exec_background / sandbox_exec_check)."""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import (
    sandbox_exec,
    sandbox_exec_background,
)
from code_sandbox_mcp.tools.container import (
    sandbox_initialize,
)

if TYPE_CHECKING:
    from pydantic import TypeAdapter


class TestSandboxInitialize:
    """Tests for sandbox_initialize."""

    @patch("code_sandbox_mcp.tools.container.proxy_lifecycle")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_proxied_container_env_carries_no_vcs_token(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_proxy_lifecycle: MagicMock,
    ) -> None:
        """With the egress proxy on, no VCS token reaches the container env (#356/#439)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_proxy_lifecycle.egress_proxy_enabled.return_value = True
        mock_proxy_lifecycle.sandbox_proxy_env.return_value = {
            "HTTPS_PROXY": "http://egress-proxy:8080"
        }
        mock_proxy_lifecycle.apply_network.side_effect = lambda kwargs, runtime: kwargs

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            result = sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                allow_network=True,
            )

        assert result == "abc123def456"
        env = mock_client.containers.run.call_args[1]["environment"]
        assert "GITHUB_TOKEN" not in env
        assert "GH_TOKEN" not in env
        # The proxy wiring itself still lands in the env.
        assert env.get("HTTPS_PROXY") == "http://egress-proxy:8080"

    @patch("code_sandbox_mcp.tools.container._run_pip_install")
    @patch("code_sandbox_mcp.tools.container._try_clone_into_container")
    @patch("code_sandbox_mcp.tools.container.proxy_lifecycle")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_proxied_clone_goes_anonymous(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_proxy_lifecycle: MagicMock,
        mock_clone: MagicMock,
        mock_pip: MagicMock,
    ) -> None:
        """Proxied init holds no token, so the clone must not pick gh (#403)."""
        from code_sandbox_mcp.tools.container import CloneResult

        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_proxy_lifecycle.egress_proxy_enabled.return_value = True
        mock_proxy_lifecycle.sandbox_proxy_env.return_value = {}
        mock_proxy_lifecycle.apply_network.side_effect = lambda kwargs, runtime: kwargs
        mock_clone.return_value = CloneResult("cloned", None)

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                allow_network=True,
                clone_repo="owner/repo",
            )

        # 5th positional arg = authenticated: reflects the token-free
        # container env (a token is never injected, #439).
        assert mock_clone.call_args.args[4] is False

    @patch("code_sandbox_mcp.tools.container._setup_pr_branch")
    @patch("code_sandbox_mcp.tools.container.proxy_lifecycle")
    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_proxied_pr_checkout_goes_anonymous(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
        mock_proxy_lifecycle: MagicMock,
        mock_setup_pr: MagicMock,
    ) -> None:
        """pr=N under the proxy must take the anonymous checkout path (#403)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_proxy_lifecycle.egress_proxy_enabled.return_value = True
        mock_proxy_lifecycle.sandbox_proxy_env.return_value = {}
        mock_proxy_lifecycle.apply_network.side_effect = lambda kwargs, runtime: kwargs
        mock_setup_pr.return_value = "PR #7 (feature) → /tmp/repo/repo in container abc123def456"

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            result = sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
                repo="owner/repo",
                pr=7,
            )

        assert "PR #7" in result
        # authenticated must reflect the (token-free) proxied container env
        # (pr=N no longer force-enables any token flag, #439).
        assert mock_setup_pr.call_args.kwargs["authenticated"] is False

    @patch("code_sandbox_mcp.tools.container._docker")
    @patch("code_sandbox_mcp.tools.container._ensure_image")
    @patch("code_sandbox_mcp.tools.container.validate_image_ref")
    def test_container_env_never_carries_vcs_token(
        self,
        mock_validate: MagicMock,
        mock_ensure_image: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """The container env never carries VCS tokens; they stay host-side (#439)."""
        mock_container = MagicMock()
        mock_container.id = "abc123def456"
        mock_client = MagicMock()
        mock_client.containers.run.return_value = mock_container
        mock_docker.return_value = mock_client

        with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake"}, clear=True):
            result = sandbox_initialize(
                image="python@sha256:0000000000000000000000000000000000000000000000000000000000000000",
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

    def test_log_level_default(self) -> None:
        """Default log level should be INFO."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args([])
        assert args.log_level == "INFO"

    def test_log_level_debug(self) -> None:
        """--log-level DEBUG should be parsed correctly."""
        from code_sandbox_mcp.server import _build_arg_parser
        parser = _build_arg_parser()
        args = parser.parse_args(["--log-level", "DEBUG"])
        assert args.log_level == "DEBUG"


class TestSandboxExecArgv:
    """argv mode: shell-free execve path (issue #234, #228 footgun)."""

    def _decode(self, result: str) -> dict:
        return json.loads(result)

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_argv_runs_without_shell(self, mock_docker: MagicMock) -> None:
        """argv must reach exec_run verbatim, not wrapped in /bin/sh -c."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"https://x/issues/1\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        body = "multi\nline $'quoted'"
        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            argv=["gh", "issue", "create", "--title", "x", "--body", body],
        ))
        assert result["status"] == "ok"
        called = mock_container.exec_run.call_args
        # First positional arg is the argv list itself — no shell wrapper,
        # so newlines and shell metacharacters survive literally.
        assert called.args[0] == ["gh", "issue", "create", "--title", "x", "--body", body]
        assert called.args[0][0] != "/bin/sh"
        assert "shell" not in called.kwargs or not called.kwargs.get("shell")

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_argv_working_dir_uses_exec_workdir(self, mock_docker: MagicMock) -> None:
        """working_dir maps to the exec workdir kwarg, not a `cd &&` prefix."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"ok-wd\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec(
            container_id="abc123def456",
            argv=["git", "status", "wd-marker"],
            working_dir="/tmp/repo",
        )
        called = mock_container.exec_run.call_args
        assert called.args[0] == ["git", "status", "wd-marker"]
        assert called.kwargs["workdir"] == "/tmp/repo"

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_argv_timeout_prepends_timeout_binary(self, mock_docker: MagicMock) -> None:
        """timeout>0 prepends `timeout N` as argv rather than a shell wrapper."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"tmo\n", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec(
            container_id="abc123def456",
            argv=["gh", "run", "list", "timeout-marker"],
            timeout=5,
        )
        called = mock_container.exec_run.call_args
        assert called.args[0] == ["timeout", "5", "gh", "run", "list", "timeout-marker"]

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_commands_and_argv_mutually_exclusive(self, mock_docker: MagicMock) -> None:
        """Passing both commands and argv is rejected before touching docker."""
        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            commands=["echo hi"],
            argv=["echo", "hi"],
        ))
        assert result["status"] == "error"
        assert "mutually exclusive" in result["error"]
        mock_docker.assert_not_called()

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_neither_commands_nor_argv_is_error(self, mock_docker: MagicMock) -> None:
        """Passing neither commands nor argv is rejected before touching docker."""
        result = self._decode(sandbox_exec(container_id="abc123def456"))
        assert result["status"] == "error"
        assert "required" in result["error"]
        mock_docker.assert_not_called()

    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_empty_argv_is_rejected(self, mock_docker: MagicMock) -> None:
        """An empty argv list is rejected before touching docker (review #252)."""
        result = self._decode(sandbox_exec(
            container_id="abc123def456",
            argv=[],
        ))
        assert result["status"] == "error"
        assert "non-empty" in result["error"]
        mock_docker.assert_not_called()


class TestCoerceListArg:
    """Unit tests for _coerce_list_arg helper (issue #296).

    MCP clients may serialize list arguments as JSON strings before sending them
    to the server. _coerce_list_arg detects this and parses the JSON back to a
    list so pydantic validation succeeds.
    """

    def _ta(self) -> "TypeAdapter[list[str]]":
        from typing import Annotated

        from pydantic import BeforeValidator, TypeAdapter

        from code_sandbox_mcp.tools.common import _coerce_list_arg
        return TypeAdapter(Annotated[list[str], BeforeValidator(_coerce_list_arg)])

    def test_list_passthrough(self) -> None:
        """A real list is returned unchanged."""
        from code_sandbox_mcp.tools.common import _coerce_list_arg
        v = ["echo", "hi"]
        assert _coerce_list_arg(v) is v

    def test_json_string_is_coerced_to_list(self) -> None:
        """A JSON-encoded list string is decoded to a list."""
        from code_sandbox_mcp.tools.common import _coerce_list_arg
        assert _coerce_list_arg('["echo", "hi"]') == ["echo", "hi"]

    def test_non_json_string_is_returned_as_is(self) -> None:
        """A non-JSON string is returned unchanged (pydantic will reject it later)."""
        from code_sandbox_mcp.tools.common import _coerce_list_arg
        assert _coerce_list_arg("not-a-list") == "not-a-list"

    def test_json_object_string_not_coerced(self) -> None:
        """A JSON string whose payload is not a list is returned unchanged."""
        from code_sandbox_mcp.tools.common import _coerce_list_arg
        assert _coerce_list_arg('{"key": "value"}') == '{"key": "value"}'

    def test_none_passthrough(self) -> None:
        """None is returned unchanged."""
        from code_sandbox_mcp.tools.common import _coerce_list_arg
        assert _coerce_list_arg(None) is None

    def test_pydantic_accepts_json_string_for_commands(self) -> None:
        """pydantic TypeAdapter accepts a JSON-stringified list for the commands field."""
        result = self._ta().validate_python('["git log --oneline -5", "ruff --version"]')
        assert result == ["git log --oneline -5", "ruff --version"]

    def test_pydantic_accepts_json_string_for_argv(self) -> None:
        """pydantic TypeAdapter accepts a JSON-stringified list for the argv field."""
        result = self._ta().validate_python('["/bin/sh", "-c", "git log --oneline -5"]')
        assert result == ["/bin/sh", "-c", "git log --oneline -5"]

    def test_pydantic_still_accepts_real_list(self) -> None:
        """pydantic TypeAdapter still accepts a real list (no regression)."""
        result = self._ta().validate_python(["git", "log", "--oneline"])
        assert result == ["git", "log", "--oneline"]

    # --- Additional tool-specific TypeAdapter tests (issue #299) ---

    def test_run_container_and_exec_commands_coercion(self) -> None:
        """run_container_and_exec commands: JSON-stringified list is coerced."""
        from typing import Annotated

        from pydantic import BeforeValidator, TypeAdapter

        from code_sandbox_mcp.tools.common import _coerce_list_arg
        ta = TypeAdapter(Annotated[list[str], BeforeValidator(_coerce_list_arg)] | None)
        result = ta.validate_python('["echo", "hello"]')
        assert result == ["echo", "hello"]

    def test_package_install_packages_coercion(self) -> None:
        """package_install packages: JSON-stringified list is coerced, single string passes through."""
        from typing import Annotated

        from pydantic import BeforeValidator, TypeAdapter

        from code_sandbox_mcp.tools.common import _coerce_list_arg
        ta = TypeAdapter(Annotated[str | list[str], BeforeValidator(_coerce_list_arg)] | None)
        # JSON-stringified list
        result = ta.validate_python('["requests", "click"]')
        assert result == ["requests", "click"]
        # Plain string passes through
        result2 = ta.validate_python("requests")
        assert result2 == "requests"


class TestBackgroundExecJournalRecording:
    """sandbox_exec_background must leave an audit trail (Issue #359).

    Foreground sandbox_exec records every call; before this fix the
    detached background path was completely invisible to the journal.
    """

    @patch("code_sandbox_mcp.tools.exec.journal_record_exec")
    @patch("code_sandbox_mcp.tools.exec._docker")
    def test_background_records_exec(
        self,
        mock_docker: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec_background("abc123def456", ["echo done"])

        assert mock_record.called
        args = mock_record.call_args[0]
        assert args[0] == "abc123def456"
        assert args[1] == ["echo done"]
        # -1 is the sentinel for "background launch, outcome not yet known".
        assert args[2] == -1


class TestPackageInstallJournalRecording:
    """package_install must record like ``sandbox_exec pip install`` (Issue #359)."""

    @patch("code_sandbox_mcp.tools.package.journal_record_exec")
    @patch("code_sandbox_mcp.tools.package._get_installed_packages")
    @patch("code_sandbox_mcp.tools.package._run_in_container")
    def test_install_records_exec(
        self,
        mock_run: MagicMock,
        mock_pkgs: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        from code_sandbox_mcp.tools.package import package_install

        mock_pkgs.return_value = []
        mock_run.return_value = (0, "Successfully installed requests", "")

        result = json.loads(package_install("abc123def456", packages="requests"))
        assert result["status"] == "ok"

        assert mock_record.called
        args = mock_record.call_args[0]
        assert args[0] == "abc123def456"
        # #390: the journal records the runtime installer-selection command
        assert args[1][:2] == ["sh", "-c"]
        assert "uv pip install requests" in args[1][2]
        assert "else exec pip install requests" in args[1][2]
        assert args[2] == 0
