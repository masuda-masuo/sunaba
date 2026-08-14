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

from sunaba.tools.common import CONTAINER_NOT_FOUND_NEXT_ACTION
from sunaba.tools.file import (
    transform_file,
)
from sunaba.tools.verify import (
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

    @patch("sunaba.tools.verify._docker")
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

    @patch("sunaba.tools.verify._docker")
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

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.apply_patch_to_file")
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

    @patch("sunaba.tools.file._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            transform_file(container_id="abc123", file_path="/tmp/f.txt", code="def transform(text): return text")
        )
        assert result["status"] == "error"
        assert "connection refused" in result["error"]

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.transform_file_in_container")
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

    @patch("sunaba.tools.file._docker")
    @patch("sunaba.tools.file.transform_file_in_container")
    @patch("sunaba.tools.file.truncate_output")
    def test_delegates_with_changes_and_paginates(
        self,
        mock_truncate: MagicMock,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """The diff is paginated and the page is described truthfully.

        Paging is no longer mocked out: it is what decides ``shown``
        and ``truncated`` now, so mocking it would test nothing.  This
        test used to assert ``truncated is False`` while ``has_more``
        was True -- a caller holding page 1 of 3 was told the diff was
        complete.
        """
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = {"status": "ok", "changed": True, "diff": "some diff"}

        class MockMeta:
            shown = 10
            total_lines = 10
            truncated = False

        mock_truncate.return_value = (
            "\n".join(f"line{i}" for i in range(10)), MockMeta(),
        )

        result = json.loads(
            transform_file(
                container_id="abc123",
                file_path="/tmp/f.txt",
                code="def transform(text): return text",
                max_lines=200,
                offset=0,
                limit=4,
            )
        )
        assert result["status"] == "ok"
        assert result["changed"] is True
        assert result["diff"] == "line0\nline1\nline2\nline3"
        assert result["shown"] == 4
        assert result["total_lines"] == 10
        assert result["truncated"] is True
        assert result["next_offset"] == 4
        assert result["has_more"] is True

        mock_truncate.assert_called_once_with("some diff", max_lines=200, verbose="full")


# ===================================================================
# search_in_container
# ===================================================================

class TestSearchInContainer:
    """Tests for the search_in_container wrapper."""

    @patch("sunaba.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo")
        )
        assert result == {
            "status": "error",
            "error": "Container abc123 not found",
            "recommended_next_action": CONTAINER_NOT_FOUND_NEXT_ACTION,
        }

    @patch("sunaba.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo")
        )
        assert result == {"status": "error", "error": "connection refused"}

    @patch("sunaba.tools.verify._docker")
    def test_denies_root_path(self, mock_docker: MagicMock) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo", path="/")
        )
        assert result["status"] == "error"
        assert "denied" in result["error"].lower()

    @patch("sunaba.tools.verify._docker")
    def test_denies_proc_path(self, mock_docker: MagicMock) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo", path="/proc")
        )
        assert result["status"] == "error"
        assert "denied" in result["error"].lower()

    @patch("sunaba.tools.verify._docker")
    def test_denies_proc_trailing_slash(self, mock_docker: MagicMock) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo", path="/proc/")
        )
        assert result["status"] == "error"
        assert "denied" in result["error"].lower()

    @patch("sunaba.tools.verify._docker")
    def test_allows_valid_path(self, mock_docker: MagicMock) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_resolve = "sunaba.tools.verify.resolve_git_root"
        with patch(mock_resolve) as mock_resolve_fn:
            mock_resolve_fn.return_value = "/repo"
            with patch("sunaba.tools.verify.search_files") as mock_impl:
                mock_impl.return_value = {"matches": [], "shown": 0, "total": 0, "truncated": False}
                result = json.loads(
                    search_in_container(container_id="abc123", pattern="foo")
                )
                assert "error" not in result
                assert "matches" in result

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.search_files")
    @patch("sunaba.tools.verify.resolve_git_root")
    def test_delegates_with_defaults(
        self,
        mock_resolve: MagicMock,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_resolve.return_value = "/repo"
        mock_impl.return_value = {"matches": [{"file": "a.txt", "line": 1, "text": "foo"}], "shown": 1, "total": 1, "truncated": False}

        result = json.loads(
            search_in_container(container_id="abc123", pattern="foo")
        )
        assert result == {"matches": [{"file": "a.txt", "line": 1, "text": "foo"}], "shown": 1, "total": 1, "truncated": False}
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "foo", path="/repo", mode="lexical",
            max_results=50, glob=None, ignore_case=False, context=0,
            output_mode="content", offset=0,
        )

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.search_files")
    def test_delegates_with_explicit_args(
        self,
        mock_impl: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = {"matches": [], "shown": 0, "total": 0, "truncated": False}

        json.loads(
            search_in_container(
                container_id="abc123", pattern="TODO",
                path="/home", mode="structural", max_results=10,
            )
        )
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "TODO", path="/home", mode="structural",
            max_results=10, glob=None, ignore_case=False, context=0,
            output_mode="content", offset=0,
        )


# ===================================================================
# lint_in_container
# ===================================================================

class TestLintInContainer:
    """Tests for the lint_in_container wrapper."""

    @patch("sunaba.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == {
            "status": "error",
            "error": "Container abc123 not found",
            "recommended_next_action": CONTAINER_NOT_FOUND_NEXT_ACTION,
        }

    @patch("sunaba.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            lint_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == {"status": "error", "error": "connection refused"}

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.lint_file")
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

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.lint_file")
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

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.lint_file")
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
        from sunaba.edit_verify import lint_file

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        result = lint_file(client, "abc123", "/tmp/f.py", fix=True)

        assert result == []
        cmd = self._exec_cmd(mock_container)
        assert "ruff check" in cmd
        assert "--fix" in cmd

    def test_ruff_no_fix_omits_fix_flag(self) -> None:
        from sunaba.edit_verify import lint_file

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        lint_file(client, "abc123", "/tmp/f.py", fix=False)

        cmd = self._exec_cmd(mock_container)
        assert "ruff check" in cmd
        assert "--fix" not in cmd

    def test_eslint_fix_adds_fix_flag(self) -> None:
        from sunaba.edit_verify import lint_file

        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"[]", b""))
        client = self._client_with(mock_container)

        lint_file(client, "abc123", "/tmp/app.ts", fix=True)

        cmd = self._exec_cmd(mock_container)
        assert "eslint" in cmd
        assert "--fix" in cmd

    def test_scope_phase_stays_read_only_when_fixing(self) -> None:
        """Single-file fix must not pass --fix to the project-wide scope run."""
        from sunaba.edit_verify import lint_file

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

    @patch("sunaba.tools.verify._docker")
    def test_container_not_found(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(
            type_check_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == {
            "status": "error",
            "error": "Container abc123 not found",
            "recommended_next_action": CONTAINER_NOT_FOUND_NEXT_ACTION,
        }

    @patch("sunaba.tools.verify._docker")
    def test_docker_error(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("connection refused")
        mock_docker.return_value = mock_client

        result = json.loads(
            type_check_in_container(container_id="abc123", file_path="/tmp/f.py")
        )
        assert result == {"status": "error", "error": "connection refused"}

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.type_check_file")
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

    @patch("sunaba.tools.verify._docker")
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

    @patch("sunaba.tools.verify._docker")
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

    @patch("sunaba.tools.verify._docker")
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

    @patch("sunaba.tools.verify._docker")
    def test_signature_accepts_working_dir(self, mock_docker: MagicMock) -> None:
        """verify_in_container accepts working_dir parameter."""
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = json.loads(verify_in_container(
            container_id="abc123",
            path="tests/",
            working_dir="/tmp/repo/sunaba",
        ))
        assert result["status"] == "error"  # container not found

    @patch("sunaba.tools.verify._docker")
    def test_working_dir_passed_to_exec_run(self, mock_docker: MagicMock) -> None:
        """working_dir is passed to exec_run internally."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Mock detect_languages to avoid find exec
        result = DetectionResult(languages={"python"}, scope={"python": "/repo"}, reason=None)

        # detect_languages is called from verify_in_container (via
        # __init__) AND from run_lint_type_gate (via gate module).
        # Use a shared mock patched at both locations (#668).
        detect_mock = MagicMock(return_value=result)

        with patch(
            "sunaba.edit_verify.detect_languages",
            detect_mock,
        ), patch(
            "sunaba.edit_verify.gate.detect_languages",
            detect_mock,
        ):
            # Mock exec_run for _run() calls (git diff, pytest)
            mock_container.exec_run.return_value = (
                0,
                (b"", b""),
            )

            verify_in_container(
                container_id="abc123",
                path="tests/",
                working_dir="/tmp/repo/sunaba",
            )

            # detect_languages runs twice now: once for the test path and
            # once for the pre-test lint/type gate scope (#293). Both calls
            # must carry working_dir.
            assert detect_mock.call_count == 2
            first_args, first_kwargs = detect_mock.call_args_list[0]
            assert first_args == (mock_container, "tests/", None)
            assert first_kwargs == {"working_dir": "/tmp/repo/sunaba"}
            for _args, _kwargs in detect_mock.call_args_list:
                assert _kwargs.get("working_dir") == "/tmp/repo/sunaba"
            # Verify exec_run was called with workdir=working_dir
            _, kwargs = mock_container.exec_run.call_args
            assert kwargs.get("workdir") == "/tmp/repo/sunaba"

    @patch("sunaba.tools.vcs.resolve_git_root")
    @patch("sunaba.tools.verify._docker")
    def test_working_dir_none_auto_detects_git_root(
        self, mock_docker: MagicMock, mock_resolve: MagicMock,
    ) -> None:
        """When working_dir is omitted, the git repo root is auto-detected
        instead of silently defaulting to /home/sandbox (matching the
        resolve_git_root usage already used by publish/etc.)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))
        mock_resolve.return_value = "/tmp/repo/sunaba"

        result = DetectionResult(languages={"python"}, scope={"python": "/repo"}, reason=None)

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=result,
        ):
            verify_in_container(
                container_id="abc123",
                path="tests/",
            )

        mock_resolve.assert_called_once_with(mock_container, None)
        _, kwargs = mock_container.exec_run.call_args
        assert kwargs.get("workdir") == "/tmp/repo/sunaba"

    @patch("sunaba.tools.verify._docker")
    def test_skip_both_gates_bypasses_lint_type_gate(self, mock_docker: MagicMock) -> None:
        """skip_lint_gate + skip_type_gate skip the gate entirely (#294 review)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate"
        ) as mock_gate:
            result = json.loads(verify_in_container(
                container_id="abc123",
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            ))

        mock_gate.assert_not_called()
        assert "lint" not in result
        assert "types" not in result
        assert "patch_targets" not in result

    @patch("sunaba.tools.verify._docker")
    def test_skip_lint_gate_maps_to_gate_on_lint_false(self, mock_docker: MagicMock) -> None:
        """skip_lint_gate=True forwards gate_on_lint=False to run_lint_type_gate."""
        from sunaba.edit_verify import DetectionResult

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
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
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

    @patch("sunaba.tools.verify._docker")
    def test_gate_scope_includes_tests_dir_when_present(
        self, mock_docker: MagicMock
    ) -> None:
        """Regression for #417: when both src/ and tests/ exist, lint_scope
        must cover both (matching CI's ``ruff check src/ tests/``) while
        type_scope stays "src" (CI has no type-check step)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # git diff HEAD --numstat
            (0, (b"", b"")),  # git diff --cached --numstat
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"src\ntests\n", b"")),  # src/tests existence probe
            (5, (b"collected 0 items\n", b"")),  # pytest
        ]
        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ) as mock_gate:
            verify_in_container(container_id="abc123", path="tests/")

        _args, kwargs = mock_gate.call_args
        assert _args[1] == "src"
        assert kwargs["lint_scope"] == ["src", "tests"]

    @patch("sunaba.tools.verify._docker")
    def test_gate_scope_falls_back_to_dot_when_neither_dir_exists(
        self, mock_docker: MagicMock
    ) -> None:
        """No src/ or tests/ (e.g. a flat-layout project) -> both scopes
        fall back to "."."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # git diff HEAD --numstat
            (0, (b"", b"")),  # git diff --cached --numstat
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),  # src/tests existence probe: neither exists
            (5, (b"collected 0 items\n", b"")),  # pytest
        ]
        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ) as mock_gate:
            verify_in_container(container_id="abc123", path="tests/")

        _args, kwargs = mock_gate.call_args
        assert _args[1] == "."
        assert kwargs["lint_scope"] == "."

    @patch("sunaba.tools.verify._docker")
    def test_collection_error_ec2_gate_fail(self, mock_docker: MagicMock) -> None:
        """ec=2 (collection error) → gate_passed=false, raw_output in reasons."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (2, (b"---PYTEST-RAW---\nImportError: No module named 'foo'\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        assert result["gate_passed"] is False
        assert result["tests"]["full"]["status"] == "collection_error"
        assert "collection error" in result["gate_fail_reasons"][0]
        assert "ImportError" in result["gate_fail_reasons"][0]

    @patch("sunaba.tools.verify._docker")
    def test_no_tests_with_filter_gate_fail(self, mock_docker: MagicMock) -> None:
        """has_filter + no_tests → gate fail (explicit filter mis-specified)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (5, (b"collected 0 items\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                test_filter="NonExistentTest",
            ))

        assert result["gate_passed"] is False
        assert result["partial_test_run"] is True
        assert "no tests matched" in result["gate_fail_reasons"][0]

    @patch("sunaba.tools.verify._docker")
    def test_no_tests_without_filter_gate_pass(self, mock_docker: MagicMock) -> None:
        """no filter + no_tests → gate pass (project without tests is ok)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (5, (b"collected 0 items\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        assert result["gate_passed"] is True
        assert result["gate_pass_reason"] == "no tests found — gate passes"

    @patch("sunaba.tools.verify._docker")
    def test_collected_metadata_in_result(self, mock_docker: MagicMock) -> None:
        """Result dict includes collected / collection_errors from pytest summary."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        junit_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites name="pytest tests"><testsuite name="pytest" '
            'errors="0" failures="0" skipped="0" tests="10" time="1.5" '
            'timestamp="2026-01-01T00:00:00" hostname="h">'
            '<testcase classname="test_a" name="test_one" time="0.1" />'
            "</testsuite></testsuites>"
        )
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (0, (f"{junit_xml}\n---PYTEST-RAW---\n".encode(), b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        full = result["tests"]["full"]
        assert full["collected"] == 10
        assert full["collection_errors"] == 0
        assert result["gate_passed"] is True

    @patch("sunaba.tools.verify._docker")
    def test_filtered_collection_error_partial_run(self, mock_docker: MagicMock) -> None:
        """Filtered tests collection error → partial_test_run, gate fail."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (2, (b"---PYTEST-RAW---\nImportError: No module named 'bar'\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
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

    @patch("sunaba.tools.verify._docker")
    def test_no_pytest_module_gate_fail(self, mock_docker: MagicMock) -> None:
        """pytest not installed → not_available, gate fail (issue #381)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Exit code 1 + "No module named pytest" in raw output
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (1, (
                b"---PYTEST-RAW---\n"
                b"python3: No module named pytest\n",
                b"",
            )),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        assert result["gate_passed"] is False
        assert result["tests"]["full"]["status"] == "not_available"
        assert "pytest not available" in result["gate_fail_reasons"][0]

    @patch("sunaba.tools.verify._docker")
    def test_no_pytest_module_with_filter_gate_fail(self, mock_docker: MagicMock) -> None:
        """pytest not installed + filter → not_available, gate fail (issue #381)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Exit code 1 + "No module named pytest" in raw output
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (1, (
                b"---PYTEST-RAW---\n"
                b"python3: No module named pytest\n",
                b"",
            )),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                test_filter="TestFoo",
            ))

        assert result["gate_passed"] is False
        assert result["partial_test_run"] is True
        assert result["tests"]["filtered"]["status"] == "not_available"
        assert "pytest not available" in result["gate_fail_reasons"][0]

    @patch("sunaba.tools.verify._docker")
    def test_python_verify_works_without_json_report_plugin(
        self, mock_docker: MagicMock
    ) -> None:
        """Acceptance #785: a pytest WITHOUT pytest-json-report still verifies.

        The python path runs pytest's built-in ``--junit-xml`` (Issue #785),
        so the plugin is not a prerequisite at all -- a custom image with a
        plain pytest (no plugin) must verify green.  The fixture below is
        exactly what such a container produces: pure JUnit XML.
        """
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        junit_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<testsuites name="pytest tests"><testsuite name="pytest" '
            'errors="0" failures="0" skipped="0" tests="2" time="0.1" '
            'timestamp="2026-01-01T00:00:00" hostname="h">'
            '<testcase classname="test_a" name="test_one" time="0.01" />'
            '<testcase classname="test_a" name="test_two" time="0.01" />'
            "</testsuite></testsuites>"
        )
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (0, (f"{junit_xml}\n---PYTEST-RAW---\n".encode(), b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        # The plugin-less environment verifies green through built-ins only.
        tests = result["tests"]["full"]
        assert tests["status"] == "ok"
        assert tests["passed"] == 2
        assert result["gate_passed"] is True
        # And the command actually run never references the plugin's flag.
        cmd = mock_container.exec_run.call_args_list[-1].args[0]
        cmd_str = cmd[-1] if isinstance(cmd, list) else str(cmd)
        assert "--junit-xml" in cmd_str
        assert "--json-report" not in cmd_str

    @patch("sunaba.tools.verify._docker")
    def test_usage_error_is_not_available(self, mock_docker: MagicMock) -> None:
        """pytest usage error → not_available, never a test verdict (#584).

        A bad command line is verify's own problem, not the code's.  Reporting
        it as ``no_tests`` ("this project has no tests") was the #584 failure:
        a tooling gap that read like a finding about the code.
        """
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # pytest exits 4 (usage error) and writes no XML report.
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (4, (
                b"---PYTEST-RAW---\n"
                b"pytest: error: unrecognized arguments: --bogus-flag\n",
                b"",
            )),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        tests = result["tests"]["full"]
        assert tests["status"] == "not_available"
        assert "usage error" in tests["error"]
        assert result["gate_passed"] is False

    @patch("sunaba.tools.verify._docker")
    def test_crash_without_report_is_error_not_no_tests(
        self, mock_docker: MagicMock
    ) -> None:
        """pytest died producing no report → error, not "no tests found" (#584).

        A non-zero exit with no XML means the run broke.  Laundering that into
        ``no_tests`` would report a benign verdict for a run that never happened.
        """
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Exit 3 (internal error), no XML, no recognizable tool-absence marker.
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),
            (3, (b"---PYTEST-RAW---\nINTERNALERROR> boom\n", b"")),
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        tests = result["tests"]["full"]
        assert tests["status"] == "error"
        assert result["gate_passed"] is False

    @patch("sunaba.tools.verify._docker")
    def test_diff_summary_includes_untracked(self, mock_docker: MagicMock) -> None:
        """diff_summary always has an 'untracked' key (Issue #687)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # git diff HEAD --numstat
            (0, (b"", b"")),  # git diff --cached --numstat
            (0, (b"new_file.py\ndirty.txt\n", b"")),  # git ls-files --others --exclude-standard
            (0, (b"", b"")),  # src/tests dir probe
            (5, (b"collected 0 items\n", b"")),  # pytest
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        ds = result["diff_summary"]
        assert "untracked" in ds
        assert ds["untracked"] == ["new_file.py", "dirty.txt"]

    @patch("sunaba.tools.verify._docker")
    def test_diff_summary_untracked_empty_when_clean(self, mock_docker: MagicMock) -> None:
        """diff_summary untracked is empty list when tree is clean."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # git diff HEAD --numstat
            (0, (b"", b"")),  # git diff --cached --numstat
            (0, (b"", b"")),  # git ls-files --others --exclude-standard (empty)
            (0, (b"", b"")),  # src/tests dir probe
            (5, (b"collected 0 items\n", b"")),  # pytest
        ]

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        ds = result["diff_summary"]
        assert "untracked" in ds
        assert ds["untracked"] == []

    @patch("sunaba.tools.verify._docker")
    def test_untracked_query_uses_exclude_standard(self, mock_docker: MagicMock) -> None:
        """The untracked query uses --exclude-standard so .gitignore is
        respected — same flag as publish's rejection path (#679)."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        # Use a function-based side_effect to capture every exec_run call.
        calls: list[str] = []
        ls_files_reply: list[tuple[int, tuple[bytes, bytes]]] = [
            (0, (b"", b"")),   # numstat1 (fallback)
            (0, (b"", b"")),   # numstat2 (fallback)
            (0, (b"", b"")),   # the ls-files reply
            (0, (b"", b"")),   # dir probe
            (5, (b"collected 0 items\n", b"")),  # pytest
        ]
        _replies = iter(ls_files_reply)

        def _side_effect(cmd, **kwargs):
            cmd_str = (
                cmd[-1].decode() if isinstance(cmd[-1], bytes)
                else str(cmd[-1])
            )
            calls.append(cmd_str)
            return next(_replies)

        mock_container.exec_run.side_effect = _side_effect

        gate_ret = {
            "gate_passed": True, "incomplete": False,
            "lint": [], "types": [], "gate_fail_reasons": [],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            json.loads(verify_in_container(
                container_id="abc123", path="tests/",
            ))

        # The third exec_run call is the ls-files untracked query
        untracked_calls = [
            c for c in calls
            if "ls-files" in c and "--exclude-standard" in c
        ]
        assert untracked_calls, (
            "expected git ls-files --others --exclude-standard; "
            f"got: {calls}"
        )


# ===================================================================
# verify_in_container: dispatch path (Issue #493)
# ===================================================================

class TestVerifyDispatch:
    """Tests for the language-aware test dispatch in verify_in_container."""

    @patch("sunaba.tools.verify._docker")
    def test_no_languages_detected_passes_gate(self, mock_docker: MagicMock) -> None:
        """detect_languages returns empty set -> gate passes with reason."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(languages=set(), scope={}, reason="no markers"),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value={
                "gate_passed": True, "incomplete": False,
                "lint": [], "types": [], "gate_fail_reasons": [],
            },
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                skip_lint_gate=True, skip_type_gate=True, skip_patch_targets_gate=True,
            ))

        assert result["gate_passed"] is True
        assert "gate_pass_reason" in result
        assert "no languages detected" in result["gate_pass_reason"]

    @patch("sunaba.tools.verify._docker")
    def test_has_filter_without_python_warns(self, mock_docker: MagicMock) -> None:
        """has_filter=True but python not in detected -> filter_warning set."""
        from sunaba.edit_verify import DetectionResult, VerifyResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        mock_runner = MagicMock(return_value=VerifyResult(
            tool="go test", status="ok", detail=json.dumps({
                "status": "ok", "passed": 1, "duration": 0.1,
            }),
        ))

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"go"}, scope={"go": "."}, reason=None,
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value={
                "gate_passed": True, "incomplete": False,
                "lint": [], "types": [], "gate_fail_reasons": [],
            },
        ), patch(
            "sunaba.edit_verify._DISPATCH",
            {"go": {"test": mock_runner, "lint": None, "type": None}},
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                test_filter="TestFoo",
                skip_lint_gate=True, skip_type_gate=True, skip_patch_targets_gate=True,
            ))

        assert "filter_warning" in result
        assert "only Python supports" in result["filter_warning"]
        assert result["gate_passed"] is True
        assert result["tests"]["full"]["status"] == "ok"

    @patch("sunaba.tools.verify._docker")
    def test_dispatch_runner_not_available(self, mock_docker: MagicMock) -> None:
        """Dispatch runner returns not_available -> gate fails."""
        from sunaba.edit_verify import DetectionResult, VerifyResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        mock_runner = MagicMock(return_value=VerifyResult(
            tool="go test", status="not_available",
            detail="go not installed in container",
        ))

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"go"}, scope={"go": "."}, reason=None,
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value={
                "gate_passed": True, "incomplete": False,
                "lint": [], "types": [], "gate_fail_reasons": [],
            },
        ), patch(
            "sunaba.edit_verify._DISPATCH",
            {"go": {"test": mock_runner, "lint": None, "type": None}},
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                skip_lint_gate=True, skip_type_gate=True, skip_patch_targets_gate=True,
            ))

        assert result["gate_passed"] is False
        assert "not installed" in result["gate_fail_reasons"][0]

    @patch("sunaba.tools.verify._docker")
    def test_dispatch_failure_without_counts(self, mock_docker: MagicMock) -> None:
        """A failed suite with no count does not report "0 failure(s)".

        A runner whose output carries no parseable counts (non-TAP
        ``npm test``) returns the failure as a raw string, so the gate
        has no ``failed`` number.  Printing one anyway said the gate went
        red on zero failures (Issue #857).
        """
        from sunaba.edit_verify import DetectionResult, VerifyResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        mock_runner = MagicMock(return_value=VerifyResult(
            tool="npm test", status="findings",
            detail="  not ok - reports ENOENT for a missing path\n3 tests, 1 failure",
            exit_code=1,
        ))

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"js"}, scope={"js": "."}, reason=None,
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value={
                "gate_passed": True, "incomplete": False,
                "lint": [], "types": [], "gate_fail_reasons": [],
            },
        ), patch(
            "sunaba.edit_verify._DISPATCH",
            {"js": {"test": mock_runner, "lint": None, "type": None}},
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                skip_lint_gate=True, skip_type_gate=True, skip_patch_targets_gate=True,
            ))

        assert result["gate_passed"] is False
        reason = result["gate_fail_reasons"][0]
        assert "0 failure(s)" not in reason
        assert "failure count unavailable" in reason
        # The runner's own output is what makes the red gate diagnosable.
        assert "3 tests, 1 failure" in reason

    @patch("sunaba.tools.verify._docker")
    def test_multi_language_results(self, mock_docker: MagicMock) -> None:
        """Multiple languages (non-python) produce per-language test results via dispatch."""
        from sunaba.edit_verify import DetectionResult, VerifyResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"", b""))

        mock_js_runner = MagicMock(return_value=VerifyResult(
            tool="jest", status="ok",
            detail=json.dumps({
                "status": "ok", "passed": 5, "duration": 0.5,
            }),
        ))
        mock_go_runner = MagicMock(return_value=VerifyResult(
            tool="go test", status="ok",
            detail=json.dumps({
                "status": "ok", "passed": 3, "duration": 0.3,
            }),
        ))

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"js", "go"}, scope={}, reason=None,
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value={
                "gate_passed": True, "incomplete": False,
                "lint": [], "types": [], "gate_fail_reasons": [],
            },
        ), patch(
            "sunaba.edit_verify._DISPATCH",
            {
                "js": {"test": mock_js_runner, "lint": None, "type": None},
                "go": {"test": mock_go_runner, "lint": None, "type": None},
            },
        ):
            result = json.loads(verify_in_container(
                container_id="abc123", path="tests/",
                skip_lint_gate=True, skip_type_gate=True, skip_patch_targets_gate=True,
            ))

        assert result["gate_passed"] is True
        full = result["tests"]["full"]
        assert "js" in full
        assert "go" in full
        assert full["js"]["status"] == "ok"
        assert full["go"]["status"] == "ok"


# ===================================================================
# _run_npm_test_verify
# ===================================================================


class TestRunNpmTestVerify:
    """Tests for _run_npm_test_verify: package.json scripts.test dispatch.

    Covers acceptance scenarios including TAP v13 output parsing
    (issue #738):
      (a) scripts.test + ec=0 + TAP output                -> ok + counts
      (b) scripts.test + ec=0 + non-TAP output             -> ok + no counts
      (c) scripts.test + ec=0 + TAP zero-tests             -> ok + total=0
      (d) scripts.test + ec=1 + TAP output                 -> findings + counts
      (e) scripts.test + ec=1 + non-TAP output             -> findings + raw
      (f) scripts.test + runner missing                    -> not_available
      (g) scripts.test absent                              -> jest fallback
      (h) no package.json                                  -> jest fallback
    """

    PKG_WITH_TEST = json.dumps({
        "name": "test-project",
        "scripts": {"test": "echo ok"},
    })
    PKG_WITHOUT_TEST = json.dumps({
        "name": "test-project",
        "scripts": {"lint": "eslint ."},
    })

    # Real captured TAP output from node --test (trimmed to essential lines)
    _TAP_OK = (
        "TAP version 13\n"
        "1..3\n"
        "# tests 3\n"
        "# suites 1\n"
        "# pass 3\n"
        "# fail 0\n"
        "# cancelled 0\n"
        "# skipped 0\n"
        "# todo 0\n"
        "# duration_ms 50.0\n"
    )
    _TAP_ZERO = (
        "TAP version 13\n"
        "1..0\n"
        "# tests 0\n"
        "# suites 0\n"
        "# pass 0\n"
        "# fail 0\n"
        "# cancelled 0\n"
        "# skipped 0\n"
        "# todo 0\n"
        "# duration_ms 2.0\n"
    )
    _TAP_FAIL = (
        "TAP version 13\n"
        "not ok 1 - failing test\n"
        "  ---\n"
        "  error: |-\n"
        "    Expected values to be strictly equal:\n"
        "    1 !== 2\n"
        "  ...\n"
        "1..1\n"
        "# tests 1\n"
        "# suites 0\n"
        "# pass 0\n"
        "# fail 1\n"
        "# cancelled 0\n"
        "# skipped 0\n"
        "# todo 0\n"
        "# duration_ms 20.0\n"
    )
    # TAP output whose single failure appears mid-stream, followed by 25
    # passing test points (well outside the 20-line raw_tail window).
    _TAP_FAIL_MIDSTREAM = (
        "TAP version 13\n"
        "not ok 1 - the failing test\n"
        "  ---\n"
        "  error: |-\n"
        "    Expected values to be strictly equal:\n"
        "    1 !== 2\n"
        "  ...\n"
        + "".join(f"ok {i} - passing test {i - 1}\n" for i in range(2, 27))
        + "1..26\n"
        "# tests 26\n"
        "# suites 0\n"
        "# pass 25\n"
        "# fail 1\n"
        "# cancelled 0\n"
        "# skipped 0\n"
        "# todo 0\n"
        "# duration_ms 100.0\n"
    )

    def _make_container(self, side_effects: list) -> MagicMock:
        container = MagicMock()
        container.exec_run.side_effect = side_effects
        return container

    # --- (a) success with TAP counts ---------------------------------------

    def test_npm_test_ok_with_tap_counts(self) -> None:
        """TAP output is parsed and counts are returned in detail."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (0, (self._TAP_OK.encode(), b"")),          # npm test (TAP)
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "ok"
        assert result.tool == "npm test"
        assert result.exit_code == 0
        detail = json.loads(result.detail)
        assert detail["passed"] == 3
        assert detail["total"] == 3
        assert detail["status"] == "ok"

    # --- (b) success with non-TAP output -----------------------------------

    def test_npm_test_ok_non_tap_output(self) -> None:
        """Non-TAP output still succeeds but detail notes missing counts."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (0, (b"PASS\n", b"")),                      # npm test
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "ok"
        assert result.tool == "npm test"
        assert result.exit_code == 0
        detail = json.loads(result.detail)
        assert "note" in detail  # caller can see counts are missing

    # --- (c) zero-test run (distinguishable from no-counts) ----------------

    def test_npm_test_zero_tests(self) -> None:
        """A run that executes 0 tests is reported with total=0."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (0, (self._TAP_ZERO.encode(), b"")),        # npm test (0 tests)
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "ok"
        assert result.tool == "npm test"
        detail = json.loads(result.detail)
        assert detail["total"] == 0
        assert detail["passed"] == 0

    # --- (d) test failure with TAP counts ----------------------------------

    def test_npm_test_failure_with_tap(self) -> None:
        """A failing run still reports TAP counts alongside diagnostic info."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (self._TAP_FAIL.encode(), b"")),        # npm test (TAP, fail)
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.tool == "npm test"
        assert result.exit_code == 1
        detail = json.loads(result.detail)
        assert detail["failed"] == 1
        assert detail["total"] == 1

    def test_npm_test_failure_name_in_detail(self) -> None:
        """The failing test name (mid-stream, outside raw_tail) appears in
        the returned detail JSON (Issue #804)."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),        # cat package.json
            (1, (self._TAP_FAIL_MIDSTREAM.encode(), b"")),  # npm test (TAP, fail)
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.tool == "npm test"
        assert result.exit_code == 1
        detail = json.loads(result.detail)
        assert detail["failed"] == 1
        assert detail["total"] == 26
        failures = detail.get("failures")
        assert failures is not None
        assert failures[0]["test"] == "the failing test"
        assert failures[0]["error"] == "Expected values to be strictly equal:\n1 !== 2"
        assert failures[0]["file"] == ""
        assert failures[0]["line"] == 0
        # The name is outside the 20-line raw_tail window — the failures
        # extraction (not the tail) is what surfaces it.
        raw_tail = detail.get("raw_tail", "")
        assert "the failing test" not in raw_tail
        assert "passing test 25" in raw_tail

    # --- (e) test failure with non-TAP output ------------------------------

    def test_npm_test_failure_non_tap(self) -> None:
        """Non-TAP failure still returns raw diagnostic output."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (b"FAIL\nsome test failed\n", b"")),    # npm test
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.tool == "npm test"
        assert result.exit_code == 1
        assert "FAIL" in result.detail

    def test_npm_test_not_available_command_not_found(self) -> None:
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (127, (b"bash: npm: command not found\\n", b"")),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    def test_npm_test_not_available_missing_script(self) -> None:
        """Missing script stays not_available.

        Fixture updated for Issue #857: the old one was a bare
        ``Missing script: "test"`` line, which is text a test body could
        print just as easily as npm.  Real npm prefixes every line of its
        own diagnostics with ``npm error`` (``npm ERR!`` before npm 10),
        and that prefix is now what the classifier keys off.  The
        assertion is unchanged -- this is still a runner-absent run.
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (
                b'npm error Missing script: "test"\n'
                b"npm error\n"
                b"npm error To see a list of scripts, run:\n"
                b"npm error   npm run\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    def test_npm_test_not_available_npm_error_no_lifecycle(self) -> None:
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (b"npm error\\nsome npm infrastructure problem\\n", b"")),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    def test_npm_test_failure_with_lifecycle(self) -> None:
        """npm error with ELIFECYCLE means the script ran but failed -> findings."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (
                b"npm error ELIFECYCLE \\u2018test\\u2019\\n"
                b"npm error lifecycle test failed\\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.tool == "npm test"
        assert result.exit_code == 1

    # --- (f2) ran-and-failed suites that print runner vocabulary ----------
    #
    # Issue #857: ``npm test --silent 2>&1`` merges the suite's own
    # stdout into the stream the classifier reads.  A suite that ran and
    # failed while printing "ENOENT" (or any other npm/shell phrase) was
    # reported as ``not_available`` -- "the runner is not installed" --
    # instead of "tests failed".

    def _ran_and_failed(self, planted: str) -> bytes:
        """Non-TAP output of a suite that ran 3 tests and failed 1.

        *planted* is written by a **test**, not by npm: it appears inside
        a test name and an assertion message, never at the start of a
        line as an npm diagnostic.
        """
        return (
            "> test-project@1.0.0 test\n"
            "> node run-tests.js\n"
            "\n"
            "  ok - reads the config\n"
            "  ok - writes the config\n"
            f"  not ok - reports {planted} for a missing path\n"
            f"    AssertionError: expected message to contain '{planted}'\n"
            "\n"
            "3 tests, 1 failure\n"
        ).encode()

    def _assert_ran_and_failed(self, planted: str) -> None:
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (self._ran_and_failed(planted), b"")),  # npm test
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings", (
            f"suite output containing {planted!r} was classified "
            f"{result.status!r} (detail: {result.detail!r})"
        )
        assert result.tool == "npm test"
        assert result.exit_code == 1

    def test_npm_test_ran_and_failed_printing_enoent(self) -> None:
        self._assert_ran_and_failed("ENOENT")

    def test_npm_test_ran_and_failed_printing_command_not_found(self) -> None:
        self._assert_ran_and_failed("command not found")

    def test_npm_test_ran_and_failed_printing_colon_not_found(self) -> None:
        self._assert_ran_and_failed("sh: 1: helper: not found")

    def test_npm_test_ran_and_failed_printing_missing_script(self) -> None:
        self._assert_ran_and_failed('Missing script: "test"')

    def test_npm_test_ran_and_failed_printing_npm_error(self) -> None:
        self._assert_ran_and_failed("npm error")

    def test_npm_test_ran_and_failed_printing_every_runner_phrase(self) -> None:
        """One failing suite printing the whole runner vocabulary at once."""
        from sunaba.edit_verify import _run_npm_test_verify

        combined = (
            "> test-project@1.0.0 test\n"
            "> node run-tests.js\n"
            "\n"
            "  ok - reads the config\n"
            "  not ok - reports ENOENT for a missing path\n"
            "    AssertionError: expected 'ENOENT: no such file or directory'\n"
            "  not ok - explains a command not found failure\n"
            "    AssertionError: got 'sh: 1: helper: not found'\n"
            "  not ok - explains Missing script: \"build\"\n"
            "    AssertionError: expected the npm error text to be quoted\n"
            "\n"
            "4 tests, 3 failures\n"
        ).encode()
        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (combined, b"")),                      # npm test
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.exit_code == 1
        assert "4 tests, 3 failures" in result.detail

    def test_npm_test_exit_127_with_lifecycle_is_a_failure(self) -> None:
        """Exit 127 alone does not prove the runner is missing.

        The test script itself exited 127 and npm said so with
        ELIFECYCLE, so this is a test failure.  ELIFECYCLE is npm <= 9
        output: npm 10.9.8 (this image) was measured printing no error
        block at all on a lifecycle failure, which is why
        ``test_npm_test_exit_127_with_not_found_in_assertion`` below
        covers the shape the real npm here produces.
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (127, (
                b"  not ok - shells out to a helper\n"
                b"    sh: 1: helper: not found\n"
                b"npm error Lifecycle script `test` failed with error:\n"
                b"npm error code ELIFECYCLE\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.exit_code == 127

    def test_npm_test_exit_127_with_not_found_in_assertion(self) -> None:
        """A suite that ran, failed with 127, and quoted a not-found message.

        This is the shape npm 10.9.8 actually produces in this image: a
        lifecycle script's exit code propagates verbatim and npm prints
        no error block, so exit 127 plus a ``: not found`` string is all
        the classifier sees.  Both also occur when a *test* shells out
        and asserts on the failure -- so 127 + the token cannot mean
        "npm is not installed" (Issue #857 round-2 finding).
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (127, (
                b"  ok - one\n"
                b"  not ok - helper missing\n"
                b"    AssertionError: got stderr: sh: 1: helper: not found\n"
                b"3 tests, 1 failure\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings", (
            f"ran-and-failed suite classified {result.status!r} "
            f"(detail: {result.detail!r})"
        )
        assert result.tool == "npm test"
        assert result.exit_code == 127

    def test_npm_test_ran_and_failed_echoing_npm_error_block(self) -> None:
        """A test that echoes captured npm stderr at line start.

        A wrapper CLI's own tests print npm's diagnostics verbatim.  The
        ``npm error`` prefix is therefore not proof by itself -- the rest
        of the stream (test names, assertion lines, a count line) says a
        suite ran (Issue #857 round-2 finding).
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (
                b"  ok - reads the config\n"
                b"  not ok - re-raises what npm printed\n"
                b"    AssertionError: expected the wrapper to re-raise:\n"
                b"npm error code ENOENT\n"
                b"npm error syscall open\n"
                b"npm error enoent spawn vitest ENOENT\n"
                b"2 tests, 1 failure\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings", (
            f"ran-and-failed suite classified {result.status!r} "
            f"(detail: {result.detail!r})"
        )
        assert result.exit_code == 1

    def test_npm_test_partial_output_then_missing_binary_is_a_failure(self) -> None:
        """A suite that printed, then hit a missing binary, is a failure.

        Measured in this image: ``npm test --silent`` with a script that
        prints and then runs a missing tool gives
        ``  ok - one\\nsh: 1: vitest: not found\\n`` and exit 127.  The
        stream is ambiguous -- a test could have printed that same line
        -- so the default of doubt applies: the suite ran, the gate goes
        red as a test failure rather than as a missing runner.
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (127, (b"  ok - one\nsh: 1: vitest: not found\n", b"")),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "findings"
        assert result.exit_code == 127

    def test_npm_test_not_available_shell_not_found_alone(self) -> None:
        """npm absent: the shell's diagnostic is the whole stream.

        Measured in this image with npm off PATH: ``npm test --silent``
        gives exactly ``/bin/sh: 1: npm: not found`` and exit 127.
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (127, (b"/bin/sh: 1: npm: not found\n", b"")),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    def test_npm_test_not_available_missing_test_binary(self) -> None:
        """The test tool is not installed: nothing else reaches the stream.

        Measured in this image with ``scripts.test = "vitest run"`` and
        no vitest installed: ``sh: 1: vitest: not found`` and exit 127.
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (127, (b"sh: 1: vitest: not found\n", b"")),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    def test_npm_test_not_available_npm_error_enoent(self) -> None:
        """ENOENT still means not_available when npm itself reports it.

        npm's own ENOENT block is line-prefixed and carries no
        ELIFECYCLE, so the script never ran.
        """
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (
                b"npm error code ENOENT\n"
                b"npm error syscall open\n"
                b"npm error path /workspace/node_modules/.bin/vitest\n"
                b"npm error enoent spawn vitest ENOENT\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    def test_npm_test_not_available_npm_err_bang_prefix(self) -> None:
        """npm <= 9 spells its diagnostics ``npm ERR!``."""
        from sunaba.edit_verify import _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITH_TEST.encode(), b"")),   # cat package.json
            (1, (
                b'npm ERR! Missing script: "test"\n'
                b"npm ERR! \n"
                b"npm ERR! To see a list of scripts, run:\n",
                b"",
            )),
        ])
        result = _run_npm_test_verify(container, "tests/", workdir="/repo")
        assert result.status == "not_available"
        assert result.tool == "npm test"

    # --- (d) fallback when scripts.test is absent --------------------------

    def test_no_scripts_test_falls_back_to_jest(self) -> None:
        from sunaba.edit_verify import VerifyResult, _run_npm_test_verify

        container = self._make_container([
            (0, (self.PKG_WITHOUT_TEST.encode(), b"")),  # cat package.json
        ])
        with patch(
            "sunaba.edit_verify.test_runners._run_jest_verify",
            return_value=VerifyResult(
                tool="jest", status="ok",
                detail=json.dumps({"status": "ok", "passed": 1}),
            ),
        ) as mock_jest:
            result = _run_npm_test_verify(container, "tests/", workdir="/repo")

        mock_jest.assert_called_once_with(container, "tests/", workdir="/repo")
        assert result.status == "ok"
        assert result.tool == "jest"

    # --- (e) fallback when package.json is missing -------------------------

    def test_no_package_json_falls_back_to_jest(self) -> None:
        from sunaba.edit_verify import VerifyResult, _run_npm_test_verify

        container = self._make_container([
            (0, (b"", b"")),  # cat package.json (empty/not found)
        ])
        with patch(
            "sunaba.edit_verify.test_runners._run_jest_verify",
            return_value=VerifyResult(
                tool="jest", status="ok",
                detail=json.dumps({"status": "ok", "passed": 1}),
            ),
        ) as mock_jest:
            result = _run_npm_test_verify(container, "tests/", workdir="/repo")

        mock_jest.assert_called_once_with(container, "tests/", workdir="/repo")
        assert result.status == "ok"
        assert result.tool == "jest"

    def test_bad_json_package_json_falls_back_to_jest(self) -> None:
        from sunaba.edit_verify import VerifyResult, _run_npm_test_verify

        container = self._make_container([
            (0, (b"not valid json", b"")),  # cat package.json (bad json)
        ])
        with patch(
            "sunaba.edit_verify.test_runners._run_jest_verify",
            return_value=VerifyResult(
                tool="jest", status="ok",
                detail=json.dumps({"status": "ok", "passed": 1}),
            ),
        ) as mock_jest:
            result = _run_npm_test_verify(container, "tests/", workdir="/repo")

        mock_jest.assert_called_once_with(container, "tests/", workdir="/repo")
        assert result.status == "ok"
        assert result.tool == "jest"


# ===================================================================
# verify_in_container: test_scope="affected" (Issue #781)
# ===================================================================

class TestVerifyAffectedScope:
    """Tests for incremental (affected-only) test selection in verify.

    ``test_scope="affected"`` runs ONLY the tests the change set touches
    and NEVER passes the gate: ``gate_passed`` stays false,
    ``partial_test_run`` is true, no verify success is recorded, and the
    result carries ``test_selection`` metadata plus a stable ``diff_hash``
    so journal analysis can pair an affected-green run with the
    subsequent full run of the same change set.
    """

    # Container id unique to this class so the process-local verify-state
    # map can be asserted without cross-test contamination.
    CID = "aff7812c0ffee"

    GREEN_REPORT = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<testsuites name="pytest tests"><testsuite name="pytest" '
        b'errors="0" failures="0" skipped="0" tests="3" time="0.1" '
        b'timestamp="2026-01-01T00:00:00" hostname="h">'
        b'<testcase classname="tests.test_app" name="test_x" time="0.01" />'
        b"</testsuite></testsuites>\n"
        b"---PYTEST-RAW---\n3 passed\n"
    )
    FAILED_REPORT = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<testsuites name="pytest tests"><testsuite name="pytest" '
        b'errors="0" failures="1" skipped="0" tests="3" time="0.1" '
        b'timestamp="2026-01-01T00:00:00" hostname="h">'
        b'<testcase classname="tests.test_app" name="test_x" time="0.01">'
        b'<failure message="boom">boom\n\ntests/test_app.py:5: AssertionError</failure>'
        b"</testcase></testsuite></testsuites>\n"
        b"---PYTEST-RAW---\n1 failed\n"
    )
    # numstat + name-status halves joined by the marker line, matching the
    # chained exec command verify builds (Issue #781).
    NS = "__SUNABA_NAMESTATUS__"
    CHANGED_UNSTAGED = (
        b"1\t1\tsrc/app.py\n" + NS.encode() + b"\nM\tsrc/app.py\n"
    )
    EMPTY_STAGED = NS.encode() + b"\n"
    SELECTOR_OK = b'{"selected": ["tests/test_app.py"], "widen_reason": null}\n'
    SELECTOR_WIDEN = b'{"selected": [], "widen_reason": "change set includes pyproject.toml"}\n'

    def _base_effects(self) -> list[tuple[int, tuple[bytes, bytes]]]:
        return [
            (0, (self.CHANGED_UNSTAGED, b"")),
            (0, (self.EMPTY_STAGED, b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
        ]

    def _run(self, effects, languages, **kwargs) -> dict:
        """Drive verify_in_container with the standard mocks."""
        from sunaba.edit_verify import DetectionResult

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_container.exec_run.side_effect = effects

        with patch("sunaba.tools.verify._docker", return_value=mock_client), patch(
            "sunaba.edit_verify.write_file",
        ), patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages=set(languages), scope={}, reason=None,
            ),
        ):
            result = json.loads(verify_in_container(
                container_id=self.CID,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
                **kwargs,
            ))
            return result, mock_container.exec_run.call_args_list

    def _exec_cmds(self, calls) -> list[str]:
        """The shell command strings verify passed to exec_run."""
        cmds = []
        for call in calls:
            cmd = call.args[0]
            if isinstance(cmd, list) and cmd and cmd[0] == "/bin/sh":
                cmds.append(cmd[-1] if isinstance(cmd[-1], bytes) else str(cmd[-1]))
        return cmds

    def test_affected_green_never_passes_gate(self) -> None:
        """Affected green: gate_passed stays false, partial run, no success
        recorded, and the selected tests are passed positionally (never -k)."""
        from sunaba.verify_state import has_verify_success

        effects = self._base_effects() + [
            (0, (self.SELECTOR_OK, b"")),
            (0, (self.GREEN_REPORT, b"")),
        ]
        with patch("sunaba.tools.verify.record_verify_success") as mock_record:
            result, calls = self._run(effects, ["python"], test_scope="affected")

        assert result["gate_passed"] is False
        assert result["partial_test_run"] is True
        assert result["tests"]["full"]["status"] == "ok"
        assert "full suite is still required" in result["gate_skipped_reason"]
        assert "test_scope='full'" in result["recommended_next_action"]
        assert "gate_fail_reasons" not in result
        mock_record.assert_not_called()
        assert has_verify_success(self.CID) is False

        ts = result["test_selection"]
        assert ts["mode"] == "affected"
        assert ts["changed_files"] == ["src/app.py"]
        assert ts["selected_count"] == 1
        assert ts["widened_to_full_reason"] is None
        assert ts["selection_ms"] >= 0
        assert result["diff_hash"]

        # Selected tests arrive as positional pytest paths, never -k.
        cmds = self._exec_cmds(calls)
        pytest_cmds = [c for c in cmds if "python3 -m pytest" in c]
        assert pytest_cmds, f"no pytest command in: {cmds}"
        assert "tests/test_app.py" in pytest_cmds[0]
        assert "-k" not in pytest_cmds[0]

    def test_affected_red_reports_failures(self) -> None:
        """Affected run with failures: gate_fail_reasons carries the count."""
        effects = self._base_effects() + [
            (0, (self.SELECTOR_OK, b"")),
            (1, (self.FAILED_REPORT, b"")),
        ]
        result, _ = self._run(effects, ["python"], test_scope="affected")

        assert result["gate_passed"] is False
        assert result["partial_test_run"] is True
        assert result["tests"]["full"]["status"] == "failed"
        assert "1 failure(s)" in result["gate_fail_reasons"][0]

    def test_affected_with_test_filter_conflict_errors(self) -> None:
        """test_scope='affected' + test_filter -> error result, no tests run."""
        effects = self._base_effects()
        result, calls = self._run(effects, ["python"], test_scope="affected", test_filter="TestFoo")

        assert result["status"] == "error"
        assert result["gate_passed"] is False
        assert "conflicting intent" in result["error"]
        # Fails fast: NO exec work at all (validation happens before the
        # diff-collection execs, Issue #781 review).
        cmds = self._exec_cmds(calls)
        assert cmds == [], f"expected no exec calls, got: {cmds}"
        # Error results still carry the test_selection/diff_hash keys so
        # journal consumers can rely on them existing.
        assert result["diff_hash"] is None
        ts = result["test_selection"]
        assert ts["changed_files"] == []
        assert ts["selected_count"] == 0
        assert ts["widened_to_full_reason"] is None

    def test_affected_with_pytest_args_conflict_errors(self) -> None:
        """test_scope='affected' + pytest_args -> error result, no tests run."""
        effects = self._base_effects()
        result, calls = self._run(effects, ["python"], test_scope="affected", pytest_args="-x")

        assert result["status"] == "error"
        assert result["gate_passed"] is False
        assert "conflicting intent" in result["error"]
        assert result["diff_hash"] is None
        assert result["test_selection"]["changed_files"] == []
        cmds = self._exec_cmds(calls)
        assert cmds == [], f"expected no exec calls, got: {cmds}"

    def test_invalid_test_scope_errors(self) -> None:
        effects = self._base_effects()
        result, calls = self._run(effects, ["python"], test_scope="bogus")
        assert result["status"] == "error"
        assert "bogus" in result["error"]
        assert result["diff_hash"] is None
        assert result["test_selection"]["changed_files"] == []
        cmds = self._exec_cmds(calls)
        assert cmds == [], f"expected no exec calls, got: {cmds}"

    def test_affected_widen_to_full_runs_full_suite(self) -> None:
        """Selector widening -> genuine full run with normal gate semantics
        and the widen reason recorded."""
        effects = self._base_effects() + [
            (0, (self.SELECTOR_WIDEN, b"")),
            (0, (self.GREEN_REPORT, b"")),
        ]
        with patch("sunaba.tools.verify.record_verify_success") as mock_record:
            result, _ = self._run(effects, ["python"], test_scope="affected")

        assert result["gate_passed"] is True  # genuine full run
        assert result["partial_test_run"] is False
        ts = result["test_selection"]
        assert ts["mode"] == "affected"
        assert ts["selected_count"] == 0
        assert ts["widened_to_full_reason"] == "change set includes pyproject.toml"
        assert result["tests"]["full"]["status"] == "ok"
        mock_record.assert_called_once_with(self.CID)

    def test_affected_selector_failure_widens(self) -> None:
        """A crashing selector must widen to full, never fail the verify."""
        effects = self._base_effects() + [
            (1, (b"", b"Traceback (most recent call last): ...")),
            (0, (self.GREEN_REPORT, b"")),
        ]
        result, _ = self._run(effects, ["python"], test_scope="affected")

        assert result["gate_passed"] is True
        assert "affected-test selector failed" in result["test_selection"]["widened_to_full_reason"]

    def test_affected_non_python_language_widens(self) -> None:
        """A language set other than exactly {python} widens before any
        selector run; the full suite then runs with normal semantics."""
        from sunaba.edit_verify import DetectionResult, VerifyResult

        mock_runner = MagicMock(return_value=VerifyResult(
            tool="go test", status="ok", detail=json.dumps({
                "status": "ok", "passed": 1, "duration": 0.1,
            }),
        ))
        effects = self._base_effects()  # no selector exec expected

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_container.exec_run.side_effect = effects

        with patch("sunaba.tools.verify._docker", return_value=mock_client), patch(
            "sunaba.edit_verify.write_file",
        ), patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(languages={"go"}, scope={}, reason=None),
        ), patch(
            "sunaba.edit_verify._DISPATCH",
            {"go": {"test": mock_runner, "lint": None, "type": None}},
        ):
            result = json.loads(verify_in_container(
                container_id=self.CID,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
                test_scope="affected",
            ))
            calls = mock_container.exec_run.call_args_list

        assert result["gate_passed"] is True
        reason = result["test_selection"]["widened_to_full_reason"]
        assert "exactly {python}" in reason
        assert "go" in reason
        # No selector exec: nothing beyond the diff collection ran before
        # the full suite (go dispatch runner).
        cmds = self._exec_cmds(calls)
        assert not [c for c in cmds if "affected_tests" in c]

    def test_affected_empty_change_set_widens(self) -> None:
        """No changed files -> widen to a genuine full run."""
        effects = [
            (0, (b"", b"")),
            (0, (b"", b"")),
            (0, (b"", b"")),  # git ls-files --others --exclude-standard
            (0, (self.GREEN_REPORT, b"")),
        ]
        result, _ = self._run(effects, ["python"], test_scope="affected")

        assert result["gate_passed"] is True
        assert "no changed files detected" in result["test_selection"]["widened_to_full_reason"]
        assert result["test_selection"]["changed_files"] == []

    def test_diff_hash_stable_across_full_and_affected_modes(self) -> None:
        """The same change set yields the same diff_hash in both modes."""
        effects = self._base_effects() + [
            (0, (self.SELECTOR_OK, b"")),
            (0, (self.GREEN_REPORT, b"")),
        ]
        affected, _ = self._run(effects, ["python"], test_scope="affected")

        full_effects = self._base_effects() + [
            (0, (self.GREEN_REPORT, b"")),
        ]
        full, _ = self._run(full_effects, ["python"])

        assert affected["diff_hash"] == full["diff_hash"]
        assert affected["diff_hash"]
        # Full mode carries the metadata too, with mode='full'.
        ts = full["test_selection"]
        assert ts["mode"] == "full"
        assert ts["changed_files"] == ["src/app.py"]
        assert ts["selected_count"] == 0
        assert ts["widened_to_full_reason"] is None
