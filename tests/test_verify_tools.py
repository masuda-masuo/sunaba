"""Tests for verify MCP tool wrappers.

Tests cover wrapper functions in tools/verify.py and tools/file.py that do container-existence
checking then delegate to edit_verify module functions:
  - apply_patch
  - transform_file
  - search_in_container
  - lint_in_container
  - type_check_in_container
  - verify_in_container
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from code_sandbox_mcp.tools.file import (
    transform_file,
)
from code_sandbox_mcp.tools.verify import (
    apply_patch,
    lint_in_container,
    search_in_container,
    type_check_in_container,
    verify_in_container,
)

# ===================================================================
# apply_patch
# ===================================================================

class TestApplyPatch:
    """Tests for the apply_patch wrapper."""

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = apply_patch(
            container_id="abc123",
            file_path="/tmp/f.txt",
            diff_content="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        )
        assert "Error" in result
        assert "not found" in result

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = apply_patch(
            container_id="abc123",
            file_path="/tmp/f.txt",
            diff_content="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        )
        assert "Error" in result
        assert "connection refused" in result

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.apply_patch_to_file")
    def test_delegates_to_apply_patch_to_file(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = "patch applied ok"

        result = apply_patch(
            container_id="abc123",
            file_path="/tmp/f.txt",
            diff_content="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        )
        assert result == "patch applied ok"
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "/tmp/f.txt", "--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
        )


# ===================================================================
# transform_file
# ===================================================================

class TestTransformFile:
    """Tests for the transform_file wrapper."""

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "error"
        assert "connection refused" in result["error"]

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.tools.file.transform_file_in_container")
    def test_delegates_without_changes(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = {"status": "ok", "changed": False}

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "ok"
        assert result["changed"] is False
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "/tmp/f.txt", "def transform(text): return text",
        )

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.tools.file.transform_file_in_container")
    @patch("code_sandbox_mcp.tools.file.truncate_output")
    @patch("code_sandbox_mcp.tools.file.paginate_output")
    def test_delegates_with_changes_and_paginates(
        self,
        mock_paginate: MagicMock,
        mock_truncate: MagicMock,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = {"status": "ok", "changed": True, "diff": "some diff"}

        class MockMeta:
            shown = 5
            total_lines = 10
            truncated = False

        mock_truncate.return_value = ("paginated diff", MockMeta())

        class MockPage:
            content = "page 1"
            next_offset = 50
            has_more = True

        mock_paginate.return_value = MockPage()

        result = json.loads(
            transform_file(
                container_id="abc123",
                file_path="/tmp/f.txt",
                code="def transform(text): return text",
                max_lines=200,
                offset=0,
                limit=100,
            )
        )
        assert result["status"] == "ok"
        assert result["changed"] is True
        assert result["diff"] == "page 1"
        assert result["shown"] == 5
        assert result["total_lines"] == 10
        assert result["truncated"] is False
        assert result["next_offset"] == 50
        assert result["has_more"] is True

        mock_truncate.assert_called_once_with("some diff", max_lines=200, verbose="full")
        mock_paginate.assert_called_once_with("paginated diff", offset=0, limit=100)


# ===================================================================
# search_in_container
# ===================================================================

class TestSearchInContainer:
    """Tests for the search_in_container wrapper."""

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo")
        )
        assert result == [{"error": "Container abc123 not found"}]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo")
        )
        assert result == [{"error": "connection refused"}]

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.search_files")
    def test_delegates_with_defaults(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = [{"file": "a.txt", "line": 1, "text": "foo"}]

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo")
        )
        assert result == [{"file": "a.txt", "line": 1, "text": "foo"}]
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "foo", path="/", mode="lexical", max_results=50,
        )

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.search_files")
    def test_delegates_with_explicit_args(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = []

        json.loads(
            search_in_container(
                container_id="abc123", pattern="TODO",
                path="/home", mode="structural", max_results=10,
            )
        )
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "TODO", path="/home", mode="structural", max_results=10,
        )


# ===================================================================
# lint_in_container
# ===================================================================

class TestLintInContainer:
    """Tests for the lint_in_container wrapper."""

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == [
            {"file": "/tmp/f.py", "line": 0, "rule": "error", "message": "Container abc123 not found"},
        ]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == [
            {"file": "/tmp/f.py", "line": 0, "rule": "error", "message": "connection refused"},
        ]

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.lint_file")
    def test_delegates(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = [{"file": "f.py", "line": 5, "rule": "F401", "message": "unused import"}]

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == [{"file": "f.py", "line": 5, "rule": "F401", "message": "unused import"}]
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "/tmp/f.py", scope_workdir=("/tmp", "/tmp"), fix=False
        )

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.lint_file")
    def test_two_phase_scope_pass(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Single-file clean → scope check runs (filter-then-full pattern)."""
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = [{"file": "src/a.py", "line": 3, "rule": "I001", "message": "import order"}]

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="src/foo.py")
        )
        # scope check returns findings since lint_file is mocked
        assert result == [{"file": "src/a.py", "line": 3, "rule": "I001", "message": "import order"}]
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "src/foo.py", scope_workdir=("src", "."), fix=False
        )

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.lint_file")
    def test_fix_true_propagates_to_lint_file(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """fix=True is forwarded to lint_file (Issue #284)."""
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = []

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="/tmp/f.py", fix=True)
        )
        assert result == []
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "/tmp/f.py", scope_workdir=("/tmp", "/tmp"), fix=True
        )


# ===================================================================
# lint_file autofix (Issue #284) — edit_verify layer
# ===================================================================


class TestLintFileAutofix:
    """The fix flag must reach the ruff/eslint command (Issue #284)."""

    @staticmethod
    def _exec_cmd(mock_container: MagicMock) -> str:
        """Return the shell command string from the last exec_run call."""
        args, _kwargs = mock_container.exec_run.call_args
        argv = args[0]
        # argv is ["/bin/sh", "-c", "<command>"]
        return argv[2]

    def _client_with(self, mock_container: MagicMock) -> MagicMock:
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        return mock_client

    def test_ruff_fix_adds_fix_flag(self) -> None:
        from code_sandbox_mcp.edit_verify import lint_file

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        result = lint_file(client, "abc123", "/tmp/f.py", fix=True)

        assert result == []
        cmd = self._exec_cmd(mock_container)
        assert "ruff check" in cmd
        assert "--fix" in cmd

    def test_ruff_no_fix_omits_fix_flag(self) -> None:
        from code_sandbox_mcp.edit_verify import lint_file

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        lint_file(client, "abc123", "/tmp/f.py", fix=False)

        cmd = self._exec_cmd(mock_container)
        assert "ruff check" in cmd
        assert "--fix" not in cmd

    def test_eslint_fix_adds_fix_flag(self) -> None:
        from code_sandbox_mcp.edit_verify import lint_file

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        lint_file(client, "abc123", "/tmp/app.ts", fix=True)

        cmd = self._exec_cmd(mock_container)
        assert "eslint" in cmd
        assert "--fix" in cmd

    def test_scope_phase_stays_read_only_when_fixing(self) -> None:
        """Single-file fix must not pass --fix to the project-wide scope run."""
        from code_sandbox_mcp.edit_verify import lint_file

        mock_container = MagicMock()
        # First call (single file) → clean, triggers scope phase.
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        lint_file(
            client, "abc123", "src/foo.py", scope_workdir=("src", "."), fix=True
        )

        # Two exec_run calls: single-file (with --fix) then scope (read-only).
        assert mock_container.exec_run.call_count == 2
        single_cmd = mock_container.exec_run.call_args_list[0][0][0][2]
        scope_cmd = mock_container.exec_run.call_args_list[1][0][0][2]
        assert "--fix" in single_cmd
        assert "--fix" not in scope_cmd


# ===================================================================
# type_check_in_container
# ===================================================================

class TestTypeCheckInContainer:
    """Tests for the type_check_in_container wrapper."""

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            type_check_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == [
            {"file": "/tmp/f.py", "line": 0, "rule": "error", "message": "Container abc123 not found"},
        ]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            type_check_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == [
            {"file": "/tmp/f.py", "line": 0, "rule": "error", "message": "connection refused"},
        ]

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.type_check_file")
    def test_delegates(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = [{"file": "f.py", "line": 10, "rule": "arg-type", "message": "incompatible type"}]

        result = json.loads(
            type_check_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == [{"file": "f.py", "line": 10, "rule": "arg-type", "message": "incompatible type"}]
        mock_impl.assert_called_once_with(mock_client, "abc123", "/tmp/f.py", scope_workdir=("/tmp", "/tmp"))


# ===================================================================
# verify_in_container
# ===================================================================


class TestVerifyInContainer:
    """Tests for the rewritten verify_in_container (test-only with filter fallback)."""

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            verify_in_container(container_id="abc123", path="/tmp")
        )
        assert result["status"] == "error"
        assert result["gate_passed"] is False
        assert "not found" in result["error"]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            verify_in_container(container_id="abc123", path="/tmp")
        )
        assert result["status"] == "error"
        assert result["gate_passed"] is False
        assert "connection refused" in result["error"]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_signature_accepts_test_filter(self, mock_docker: MagicMock) -> None:
        """verify_in_container accepts test_filter, verbose, pytest_args."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(verify_in_container(
            container_id="abc123",
            path="/tmp",
            test_filter="TestFoo",
            verbose=True,
            pytest_args="-x --tb=short",
            language="python",
        ))
        assert result["status"] == "error"  # container not found

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_signature_accepts_working_dir(self, mock_docker: MagicMock) -> None:
        """verify_in_container accepts working_dir parameter."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(verify_in_container(
            container_id="abc123",
            path="tests/",
            working_dir="/tmp/repo/code-sandbox-mcp",
        ))
        assert result["status"] == "error"  # container not found

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_working_dir_passed_to_exec_run(self, mock_docker: MagicMock) -> None:
        """working_dir is passed to exec_run internally."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Mock detect_languages to avoid find exec
        result = DetectionResult(languages={"python"}, scope={"python": "/repo"}, reason=None)

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=result,
        ) as mock_detect:
            # Mock exec_run for _run() calls (git diff, pytest)
            mock_container.exec_run.return_value = (
                0,
                (b"", b""),
            )

            verify_in_container(
                container_id="abc123",
                path="tests/",
                working_dir="/tmp/repo/code-sandbox-mcp",
            )

            # detect_languages runs twice now: once for the test path and
            # once for the pre-test lint/type gate scope (#293). Both calls
            # must carry working_dir.
            assert mock_detect.call_count == 2
            first_args, first_kwargs = mock_detect.call_args_list[0]
            assert first_args == (mock_container, "tests/", None)
            assert first_kwargs == {"working_dir": "/tmp/repo/code-sandbox-mcp"}
            for _args, _kwargs in mock_detect.call_args_list:
                assert _kwargs.get("working_dir") == "/tmp/repo/code-sandbox-mcp"
            # Verify exec_run was called with workdir=working_dir
            _, kwargs = mock_container.exec_run.call_args
            assert kwargs.get("workdir") == "/tmp/repo/code-sandbox-mcp"

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_skip_both_gates_bypasses_lint_type_gate(self, mock_docker: MagicMock) -> None:
        """skip_lint_gate + skip_type_gate skip the gate entirely (#294 review)."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate"
        ) as mock_gate:
            result = json.loads(verify_in_container(
                container_id="abc123",
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
            ))

        mock_gate.assert_not_called()
        assert "lint" not in result
        assert "types" not in result

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_skip_lint_gate_maps_to_gate_on_lint_false(self, mock_docker: MagicMock) -> None:
        """skip_lint_gate=True forwards gate_on_lint=False to run_lint_type_gate."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))
        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ) as mock_gate:
            verify_in_container(
                container_id="abc123",
                path="tests/",
                skip_lint_gate=True,
            )

        assert mock_gate.call_count == 1
        _args, kwargs = mock_gate.call_args
        assert kwargs["gate_on_lint"] is False
        assert kwargs["gate_on_type"] is True

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_collection_error_ec2_gate_fail(self, mock_docker: MagicMock) -> None:
        """ec=2 (collection error) → gate_passed=false, raw_output in reasons."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (2, (b"---PYTEST-RAW---\nImportError: No module named 'foo'\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        assert result["gate_passed"] is False
        assert result["tests"]["full"]["status"] == "collection_error"
        assert "collection error" in result["gate_fail_reasons"][0]
        assert "ImportError" in result["gate_fail_reasons"][0]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_no_tests_with_filter_gate_fail(self, mock_docker: MagicMock) -> None:
        """has_filter + no_tests → gate fail (explicit filter mis-specified)."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (5, (b"collected 0 items\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                test_filter="NonExistentTest",
            ))

        assert result["gate_passed"] is False
        assert result["partial_test_run"] is True
        assert "no_tests" in result["gate_fail_reasons"][0]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_no_tests_without_filter_gate_pass(self, mock_docker: MagicMock) -> None:
        """no filter + no_tests → gate pass (project without tests is ok)."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (5, (b"collected 0 items\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        assert result["gate_passed"] is True
        assert result["gate_pass_reason"] == "no tests found — gate passes"

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_collected_metadata_in_result(self, mock_docker: MagicMock) -> None:
        """Result dict includes collected / collection_errors from pytest summary."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        json_report = json.dumps({
            "summary": {
                "collected": 10, "total": 10,
                "passed": 10, "failed": 0, "errors": 0,
            },
            "duration": 1.5,
            "tests": [],
        })
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (f"{json_report}\n---PYTEST-RAW---\n".encode(), b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        full = result["tests"]["full"]
        assert full["collected"] == 10
        assert full["collection_errors"] == 0
        assert result["gate_passed"] is True

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_filtered_collection_error_partial_run(self, mock_docker: MagicMock) -> None:
        """Filtered tests collection error → partial_test_run, gate fail."""
        from code_sandbox_mcp.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (2, (b"---PYTEST-RAW---\nImportError: No module named 'bar'\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "code_sandbox_mcp.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "code_sandbox_mcp.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                test_filter="TestFoo",
            ))

        assert result["gate_passed"] is False
        assert result["partial_test_run"] is True
        assert "collection error" in result["gate_fail_reasons"][0]
        assert result["tests"]["filtered"]["status"] == "collection_error"

