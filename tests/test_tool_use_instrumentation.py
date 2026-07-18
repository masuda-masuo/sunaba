"""Tool-use journal instrumentation for previously invisible tools (#454).

``checkpoint_list`` and ``sandbox_exec_check`` left no journal entry at
all, and ``transform_file`` only left a ``write_file`` entry when it
changed the file -- so a usage audit reading the journal could not tell
an unused tool from an uninstrumented one (the false-negative that
produced #454's "31 tools missing" premise).  Each call now records a
``tool_use`` entry under the tool's own name, after the container
lookup succeeds.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from docker.errors import NotFound

from sunaba.tools.exec import sandbox_exec_check
from sunaba.tools.file import transform_file
from sunaba.tools.vcs import checkpoint_list
from tests.conftest import _make_client_mock, _make_container_mock


class TestCheckpointListRecordsToolUse:
    @patch("sunaba.tools.vcs.checkpoints.record_tool_use")
    @patch("sunaba.tools.vcs.checkpoints._docker")
    def test_records_on_call(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        container = _make_container_mock([(0, b"", b"")])
        mock_docker.return_value = _make_client_mock(container)

        checkpoint_list(container_id="abc123def456")

        mock_record.assert_called_once_with("abc123def456", "checkpoint_list")

    @patch("sunaba.tools.vcs.checkpoints.record_tool_use")
    @patch("sunaba.tools.vcs.checkpoints._docker")
    def test_no_record_when_container_missing(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        checkpoint_list(container_id="abc123def456")

        mock_record.assert_not_called()


class TestSandboxExecCheckRecordsToolUse:
    @patch("sunaba.tools.exec.record_tool_use")
    @patch("sunaba.tools.exec._docker")
    def test_records_on_call(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        container = MagicMock()
        container.exec_run.return_value = (
            0,
            b"NOW=1700000000\nSTART=\nOUT_MTIME=0\nERR_MTIME=0\nEXIT=not_found",
        )
        client = MagicMock()
        client.containers.get.return_value = container
        mock_docker.return_value = client

        sandbox_exec_check("abc123def456", "job-1")

        mock_record.assert_called_once_with(
            "abc123def456", "sandbox_exec_check", {"job_id": "job-1"}
        )

    @patch("sunaba.tools.exec.record_tool_use")
    @patch("sunaba.tools.exec._docker")
    def test_no_record_when_container_missing(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        sandbox_exec_check("abc123def456", "job-1")

        mock_record.assert_not_called()


class TestTransformFileRecordsToolUse:
    @patch("sunaba.tools.file.record_tool_use")
    @patch("sunaba.tools.file.transform_file_in_container")
    @patch("sunaba.tools.file._docker")
    def test_records_even_when_unchanged(
        self,
        mock_docker: MagicMock,
        mock_transform: MagicMock,
        mock_record: MagicMock,
    ) -> None:
        """A no-change transform is still a use -- it must be visible (#454)."""
        mock_docker.return_value = MagicMock()
        mock_transform.return_value = {
            "status": "ok",
            "changed": False,
            "new_size": 0,
            "new_lines": 0,
        }

        transform_file(
            container_id="abc123def456",
            file_path="/tmp/f.txt",
            code="def transform(text): return text",
        )

        mock_record.assert_called_once_with(
            "abc123def456", "transform_file", {"file_path": "/tmp/f.txt"}
        )

    @patch("sunaba.tools.file.record_tool_use")
    @patch("sunaba.tools.file._docker")
    def test_no_record_when_container_missing(
        self, mock_docker: MagicMock, mock_record: MagicMock
    ) -> None:
        client = MagicMock()
        client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = client

        transform_file(
            container_id="abc123def456",
            file_path="/tmp/f.txt",
            code="def transform(text): return text",
        )

        mock_record.assert_not_called()
