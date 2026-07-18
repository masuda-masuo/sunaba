"""Tests for per-edit undo snapshots and the undo_file_edit tool (issue #599).

The design goal is LLM ergonomics: every editing tool snapshots the
pre-edit file automatically, so a caller that broke a file can step
BACK to the pre-edit state instead of trying to repair broken text
forward.
"""

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from sunaba import undo
from sunaba.tools.file import edit_file, transform_file, undo_file_edit, write_file
from tests.conftest import _FakeClient
from tests.test_edit_symbol import _FakeContainerWithIO
from tests.test_write_file import _exec_run_for, _get_written_content

CID = "abc123def456"
POSIX = "/sandbox/mod.py"


@pytest.fixture(autouse=True)
def _no_journal(monkeypatch) -> None:
    monkeypatch.setattr("sunaba.tools.file.record_tool_use", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.sunaba.edit_verify.fileio.record_file_write", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "sunaba.edit_verify.edits.record_file_write", lambda *a, **k: None
    )


# ===================================================================
# undo store unit tests
# ===================================================================
class TestUndoStore:
    def test_save_get_roundtrip(self) -> None:
        undo.save_version(CID, POSIX, "v1 content")
        undo.save_version(CID, POSIX, "v2 content")
        assert undo.get_version(CID, POSIX, 1) == "v2 content"
        assert undo.get_version(CID, POSIX, 2) == "v1 content"
        assert undo.get_version(CID, POSIX, 3) is None

    def test_list_versions_newest_first(self) -> None:
        undo.save_version(CID, POSIX, "old")
        undo.save_version(CID, POSIX, "new content longer")
        versions = undo.list_versions(CID, POSIX)
        assert [v["steps"] for v in versions] == [1, 2]
        assert versions[0]["size_bytes"] == len("new content longer")

    def test_ring_buffer_prunes_oldest(self) -> None:
        for i in range(undo._MAX_VERSIONS + 3):
            undo.save_version(CID, POSIX, f"content {i}")
        versions = undo.list_versions(CID, POSIX)
        assert len(versions) == undo._MAX_VERSIONS
        assert undo.get_version(CID, POSIX, 1) == (
            f"content {undo._MAX_VERSIONS + 2}"
        )
        # Oldest surviving snapshot is 3 versions in.
        assert undo.get_version(CID, POSIX, undo._MAX_VERSIONS) == "content 3"

    def test_oversized_content_is_skipped(self) -> None:
        undo.save_version(CID, POSIX, "small")
        undo.save_version(CID, POSIX, "x" * (undo._MAX_SNAPSHOT_BYTES + 1))
        assert undo.get_version(CID, POSIX, 1) == "small"

    def test_path_normalization(self) -> None:
        undo.save_version(CID, "/sandbox//mod.py", "content")
        assert undo.get_version(CID, "/sandbox/mod.py", 1) == "content"

    def test_clear_history(self) -> None:
        undo.save_version(CID, POSIX, "content")
        undo.clear_history(CID)
        assert undo.get_version(CID, POSIX, 1) is None
        assert undo.list_versions(CID, POSIX) == []

    def test_histories_are_per_container(self) -> None:
        undo.save_version("aaa111aaa111", POSIX, "from a")
        undo.save_version("bbb222bbb222", POSIX, "from b")
        assert undo.get_version("aaa111aaa111", POSIX, 1) == "from a"
        assert undo.get_version("bbb222bbb222", POSIX, 1) == "from b"

    def test_save_failure_is_logged_not_raised(self, caplog) -> None:
        # Occupy the snapshot directory path with a plain file so
        # mkdir() raises: the edit must survive, but the operator must
        # see a warning (PR #629 review finding).
        snap_dir = undo._file_dir(CID, POSIX)
        snap_dir.parent.mkdir(parents=True, exist_ok=True)
        snap_dir.write_text("in the way", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="sunaba.undo"):
            undo.save_version(CID, POSIX, "content")
        assert "undo snapshot failed" in caplog.text
        assert POSIX in caplog.text
        assert undo.get_version(CID, POSIX, 1) is None


# ===================================================================
# Editing tools snapshot the pre-edit content
# ===================================================================
class TestEditToolsSnapshot:
    def _mock(self, mock_docker: MagicMock, content: str) -> MagicMock:
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(
            content.encode("utf-8")
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("sunaba.tools.file._docker")
    def test_old_str_edit_snapshots_pre_edit_content(
        self, mock_docker: MagicMock,
    ) -> None:
        existing = "alpha\nbeta\n"
        self._mock(mock_docker, existing)
        result = edit_file(
            container_id=CID,
            file_name="test.txt",
            file_contents="BETA",
            dest_dir="/root",
            old_str="beta",
        )
        assert "Error" not in result
        assert undo.get_version(CID, "/root/test.txt", 1) == existing

    @patch("sunaba.tools.file._docker")
    def test_full_overwrite_snapshots_previous_content(
        self, mock_docker: MagicMock,
    ) -> None:
        existing = "previous content\n"
        self._mock(mock_docker, existing)
        result = write_file(
            container_id=CID,
            file_name="test.txt",
            file_contents="new content\n",
            dest_dir="/root",
        )
        assert "Written" in result
        assert undo.get_version(CID, "/root/test.txt", 1) == existing

    def test_ast_edit_snapshots_pre_edit_content(
        self, tmp_path, monkeypatch,
    ) -> None:
        src = "def foo():\n    return 1\n"
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({POSIX: str(f)})),
        )
        result = edit_file(
            container_id=CID,
            file_name=POSIX,
            file_contents="def foo():\n    return 99\n",
            old_str="def foo():",
        )
        assert "Error" not in result, result
        assert undo.get_version(CID, POSIX, 1) == src

    def test_transform_file_snapshots_pre_edit_content(
        self, tmp_path, monkeypatch,
    ) -> None:
        src = "todo: one\ntodo: two\n"
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({POSIX: str(f)})),
        )
        result = json.loads(transform_file(
            container_id=CID,
            file_path=POSIX,
            code=(
                "def transform(text):\n"
                "    return text.replace('todo', 'TODO')\n"
            ),
        ))
        assert result["status"] == "ok" and result["changed"]
        assert undo.get_version(CID, POSIX, 1) == src

    def test_transform_file_no_change_does_not_snapshot(
        self, tmp_path, monkeypatch,
    ) -> None:
        src = "nothing to do\n"
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({POSIX: str(f)})),
        )
        result = json.loads(transform_file(
            container_id=CID,
            file_path=POSIX,
            code="def transform(text):\n    return text\n",
        ))
        assert result["status"] == "ok" and not result["changed"]
        assert undo.get_version(CID, POSIX, 1) is None


# ===================================================================
# undo_file_edit tool
# ===================================================================
class TestUndoFileEdit:
    def _mock(self, mock_docker: MagicMock, content: str) -> MagicMock:
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(
            content.encode("utf-8")
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        return mock_container

    @patch("sunaba.tools.file._docker")
    def test_restores_previous_version(self, mock_docker: MagicMock) -> None:
        broken = "def foo(:\n    pass\n"
        good = "def foo():\n    return 1\n"
        undo.save_version(CID, POSIX, good)
        mock_container = self._mock(mock_docker, broken)

        result = json.loads(undo_file_edit(CID, POSIX))
        assert result["status"] == "ok"
        assert result["restored_steps_back"] == 1
        assert _get_written_content(mock_container) == good
        assert "-def foo(:" in result["diff"]
        assert "+def foo():" in result["diff"]

    @patch("sunaba.tools.file._docker")
    def test_undo_is_redoable(self, mock_docker: MagicMock) -> None:
        """The replaced (current) content is snapshotted before restore."""
        broken = "broken content\n"
        good = "good content\n"
        undo.save_version(CID, POSIX, good)
        self._mock(mock_docker, broken)

        result = json.loads(undo_file_edit(CID, POSIX))
        assert result["status"] == "ok"
        # steps=1 now points at the pre-undo (broken) content = redo.
        assert undo.get_version(CID, POSIX, 1) == broken
        assert undo.get_version(CID, POSIX, 2) == good
        assert "redo" in result["note"]

    @patch("sunaba.tools.file._docker")
    def test_no_history_is_a_clear_error(self, mock_docker: MagicMock) -> None:
        self._mock(mock_docker, "content\n")
        result = json.loads(undo_file_edit(CID, "/root/never-edited.py"))
        assert result["status"] == "error"
        assert "No undo history" in result["error"]

    @patch("sunaba.tools.file._docker")
    def test_steps_out_of_range_lists_snapshots(
        self, mock_docker: MagicMock,
    ) -> None:
        undo.save_version(CID, POSIX, "only one\n")
        self._mock(mock_docker, "current\n")
        result = json.loads(undo_file_edit(CID, POSIX, steps=5))
        assert result["status"] == "error"
        assert "out of range" in result["error"]
        assert len(result["snapshots"]) == 1

    @patch("sunaba.tools.file._docker")
    def test_steps_two_skips_most_recent(self, mock_docker: MagicMock) -> None:
        undo.save_version(CID, POSIX, "oldest\n")
        undo.save_version(CID, POSIX, "middle\n")
        mock_container = self._mock(mock_docker, "current\n")

        result = json.loads(undo_file_edit(CID, POSIX, steps=2))
        assert result["status"] == "ok"
        assert _get_written_content(mock_container) == "oldest\n"


# ===================================================================
# End-to-end: break a file, step back
# ===================================================================
class TestBreakThenUndoFlow:
    @patch("sunaba.tools.file._docker")
    def test_broken_edit_then_undo_restores_good_state(
        self, mock_docker: MagicMock,
    ) -> None:
        """The intended LLM flow: edit breaks the file -> undo -> retry.

        The parse warning tells the model to call undo_file_edit; this
        verifies the snapshot taken by the breaking edit is exactly the
        pre-edit state.
        """
        good = "x = 1\n"
        mock_container = MagicMock()
        mock_container.exec_run.side_effect = _exec_run_for(
            good.encode("utf-8")
        )
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = edit_file(
            container_id=CID,
            file_name="test.py",
            file_contents='y = "unclosed',
            dest_dir="/root",
            old_str="x = 1",
        )
        assert "Warning: /root/test.py does not parse as Python" in result
        assert "undo_file_edit" in result

        # Container now serves the broken content.
        broken = 'y = "unclosed\n'
        mock_container.exec_run.side_effect = _exec_run_for(
            broken.encode("utf-8")
        )
        undo_result = json.loads(undo_file_edit(CID, "/root/test.py"))
        assert undo_result["status"] == "ok"
        assert _get_written_content(mock_container) == good
