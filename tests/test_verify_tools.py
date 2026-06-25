"""Tests for verify MCP tool wrappers (tools/verify.py).

Tests cover the 6 wrapper functions that do container-existence
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

from code_sandbox_mcp.tools.verify import (
    apply_patch,
    lint_in_container,
    search_in_container,
    transform_file,
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

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("code_sandbox_mcp.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "error"
        assert "connection refused" in result["error"]

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.transform_file_in_container")
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

    @patch("code_sandbox_mcp.tools.verify._docker")
    @patch("code_sandbox_mcp.tools.verify.transform_file_in_container")
    @patch("code_sandbox_mcp.tools.verify.truncate_output")
    @patch("code_sandbox_mcp.tools.verify.paginate_output")
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
        mock_impl.assert_called_once_with(mock_client, "abc123", "/tmp/f.py")


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
        mock_impl.assert_called_once_with(mock_client, "abc123", "/tmp/f.py")


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
