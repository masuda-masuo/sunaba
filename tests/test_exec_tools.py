"""Tests for the exec MCP tools (sandbox_exec / sandbox_exec_background / sandbox_exec_check)."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.server import (
    _container_env,
    copy_file,
    copy_project,
    sandbox_exec,
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._ensure_image")
    @patch("code_sandbox_mcp.server.validate_image_ref")
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

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server._ensure_image")
    @patch("code_sandbox_mcp.server.validate_image_ref")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
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

    @patch("code_sandbox_mcp.server._docker")
    def test_timeout_status_on_exit_124(self, mock_docker: MagicMock) -> None:
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

    @patch("code_sandbox_mcp.server._docker")
    def test_timeout_zero_not_applied(self, mock_docker: MagicMock) -> None:
        """timeout=0 (default) does not wrap command with timeout(1)."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"ok", b""))
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        sandbox_exec(container_id="abc123def456", commands=["echo ok"])

        cmd = mock_container.exec_run.call_args[0][0][-1]
        assert "timeout" not in cmd

    @patch("code_sandbox_mcp.server._docker")
    def test_exit_124_without_timeout_is_error(self, mock_docker: MagicMock) -> None:
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







class TestCopyProject:
    """Tests for copy_project tool."""

    @patch("code_sandbox_mcp.server._docker")
    def test_copy_project_with_dot(
        self,
        mock_docker: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """local_src_dir="." should resolve to the actual directory name as arcname."""
        src_dir = tmp_path / "myproject"
        src_dir.mkdir()
        (src_dir / "hello.txt").write_text("hello")
        (src_dir / "subdir").mkdir()
        (src_dir / "subdir" / "nested.txt").write_text("nested")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        with monkeypatch.context() as m:
            m.chdir(str(src_dir))
            result = copy_project(
                container_id="abc123",
                local_src_dir=".",
                dest_dir="/root/shiori",
            )

        assert "Error" not in result
        # Should report the resolved directory name, not "."
        assert "/root/shiori/myproject" in result
        assert "/root/shiori/." not in result

        mock_container.put_archive.assert_called_once()
        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/root/shiori"

        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        import io
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert all(
            name.startswith("myproject/") or name == "myproject"
            for name in names
        ), f"Entries should be under 'myproject/', got: {names}"
        assert "myproject/hello.txt" in names
        assert "myproject/subdir/nested.txt" in names

    @patch("code_sandbox_mcp.server._docker")
    def test_copy_project_with_absolute_path(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Absolute paths should use the directory basename as arcname."""
        src_dir = tmp_path / "myapp"
        src_dir.mkdir()
        (src_dir / "app.py").write_text("print('hello')")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/opt",
        )

        assert "Error" not in result
        assert "/opt/myapp" in result

        call_args = mock_container.put_archive.call_args
        assert call_args[0][0] == "/opt"

        tar_data = call_args[0][1]
        tar_data.seek(0)
        import tarfile
        import io
        with tarfile.open(fileobj=tar_data, mode="r") as tar:
            names = tar.getnames()
        assert "myapp/app.py" in names

    @patch("code_sandbox_mcp.server._docker")
    def test_copy_project_container_not_found(
        self,
        mock_docker: MagicMock,
    ) -> None:
        """Should return error when container is not found."""
        from docker.errors import NotFound
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=".",
            dest_dir="/root",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_copy_project_src_not_a_directory(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Should return error when local_src_dir is not a directory."""
        src_file = tmp_path / "file.txt"
        src_file.write_text("not a directory")
        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_file),
            dest_dir="/root",
        )
        assert "Error" in result
        assert "not a directory" in result

    @patch("code_sandbox_mcp.server._docker")
    def test_copy_project_put_archive_fails(
        self,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Should return error when put_archive raises an APIError."""
        from docker.errors import APIError
        from unittest.mock import Mock
        src_dir = tmp_path / "testproj"
        src_dir.mkdir()
        (src_dir / "f.txt").write_text("data")

        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.reason = "Not Found"

        mock_container = MagicMock()
        mock_container.put_archive.side_effect = APIError(
            "404 Client Error: Not Found",
            mock_response,
            explanation="No such directory",
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_project(
            container_id="abc123",
            local_src_dir=str(src_dir),
            dest_dir="/nonexistent",
        )
        assert "Error" in result

    @patch("code_sandbox_mcp.server._docker")
    @patch("code_sandbox_mcp.server.record_copy")
    def test_copy_file_default_dest_path(
        self,
        mock_record_copy: MagicMock,
        mock_docker: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Default dest_path is /home/sandbox."""
        src_file = tmp_path / "hello.txt"
        src_file.write_text("hello")

        mock_container = MagicMock()
        mock_container.put_archive.return_value = True
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = copy_file(
            container_id="abc123",
            local_src_file=str(src_file),
        )

        assert "Error" not in result
        assert "/home/sandbox" in result


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
