"""Tests for issue #187: file size metadata and tier nag."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_sandbox_mcp.tools.file import write_file_sandbox


class TestComputeFileSize:
    """Tests for the _compute_file_size helper."""

    def test_empty_string(self) -> None:
        from code_sandbox_mcp.edit_verify import _compute_file_size

        fs = _compute_file_size("")
        assert fs["lines"] == 0
        assert fs["bytes"] == 0
        assert fs["approx_tokens"] == 0

    def test_single_line_no_newline(self) -> None:
        from code_sandbox_mcp.edit_verify import _compute_file_size

        fs = _compute_file_size("hello")
        assert fs["lines"] == 1
        assert fs["bytes"] == 5
        assert fs["approx_tokens"] == 1

    def test_single_line_with_newline(self) -> None:
        from code_sandbox_mcp.edit_verify import _compute_file_size

        fs = _compute_file_size("hello\n")
        assert fs["lines"] == 1
        assert fs["bytes"] == 6
        assert fs["approx_tokens"] == 6 // 4

    def test_multi_line(self) -> None:
        from code_sandbox_mcp.edit_verify import _compute_file_size

        fs = _compute_file_size("a\nb\nc\n")
        assert fs["lines"] == 3
        assert fs["bytes"] == 6
        assert fs["approx_tokens"] == 1

    def test_multi_line_no_trailing_newline(self) -> None:
        from code_sandbox_mcp.edit_verify import _compute_file_size

        fs = _compute_file_size("a\nb\nc")
        assert fs["lines"] == 3
        assert fs["bytes"] == 5

    def test_unicode_byte_count(self) -> None:
        from code_sandbox_mcp.edit_verify import _compute_file_size

        text = "日本語\n"
        fs = _compute_file_size(text)
        assert fs["lines"] == 1
        assert fs["bytes"] == len(text.encode("utf-8"))


class TestReadFileLinesFileSize:
    """read_file_lines must now include file_size in its return dict."""

    def test_file_size_in_return(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import read_file_lines

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_file",
            lambda _c, _p: "line1\nline2\nline3\n",
        )

        result = read_file_lines(
            container=None, file_path="test.txt", offset=0, limit=10
        )
        assert "file_size" in result
        fs = result["file_size"]
        assert fs["lines"] == 3
        assert fs["bytes"] == 18
        assert fs["approx_tokens"] == 4

    def test_file_size_in_empty_file(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import read_file_lines

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_file",
            lambda _c, _p: "",
        )

        result = read_file_lines(
            container=None, file_path="empty.txt", offset=0, limit=10
        )
        assert "file_size" in result
        assert result["file_size"]["lines"] == 0

    def test_file_size_not_in_error(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import read_file_lines

        def _raise(*a, **k):
            raise ValueError("not found")

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_file",
            _raise,
        )
        result = read_file_lines(
            container=None, file_path="nope.txt", offset=0, limit=10
        )
        assert "error" in result
        assert "file_size" not in result


class TestWriteFileSandboxFileSize:
    """write_file_sandbox must return JSON with file_size."""

    @staticmethod
    def _mock_container(existing: bytes = b"") -> MagicMock:
        mock = MagicMock()
        mock.exec_run.side_effect = _exec_run_for(existing)
        return mock

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.edit_verify._check_file_size_nag")
    def test_return_includes_file_size(
        self, mock_nag: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_nag.return_value = None
        mock_container = self._mock_container()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.py",
            file_contents="print('hi')\n",
            dest_dir="/root",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert "file_size" in parsed
        fs = parsed["file_size"]
        assert fs["lines"] == 1
        assert fs["bytes"] == 12
        assert fs["approx_tokens"] == 3

    @patch("code_sandbox_mcp.tools.file._docker")
    @patch("code_sandbox_mcp.edit_verify._check_file_size_nag")
    def test_return_includes_nag_when_present(
        self, mock_nag: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_nag.return_value = "note: test.py crossed 1500 lines"
        mock_container = self._mock_container()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="test.py",
            file_contents="x\n" * 1600,
            dest_dir="/root",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert "nag" in parsed
        assert "crossed 1500 lines" in parsed["nag"]

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_old_overwrite_style_still_works(self, mock_docker: MagicMock) -> None:
        """Old tests that check for 'Written' in result still pass."""
        mock_container = self._mock_container()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="hello.txt",
            file_contents="new content",
            dest_dir="/root",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "ok"
        assert "Written" in parsed["message"]

    @patch("code_sandbox_mcp.tools.file._docker")
    def test_error_still_plain_string(self, mock_docker: MagicMock) -> None:
        """Error messages remain plain strings for backward compatibility."""
        from docker.errors import NotFound

        mock_client = MagicMock()
        mock_client.containers.get.side_effect = NotFound("not found")
        mock_docker.return_value = mock_client

        result = write_file_sandbox(
            container_id="abc123",
            file_name="f.txt",
            file_contents="data",
        )
        assert result.startswith("Error:")
        assert "not found" in result


class TestTransformFileNewLines:
    """transform_file_in_container must now return new_lines."""

    def test_runner_emits_new_lines(self, tmp_path, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import transform_file_in_container
        from tests.conftest import _FakeClient, _FakeContainer

        writes: list = []
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_file_write",
            lambda *a, **k: writes.append(a),
        )

        posix = "/sandbox/x.py"
        f = tmp_path / "x.py"
        f.write_text("a\nb\nc\n", encoding="utf-8")

        client = _FakeClient(_FakeContainer({posix: str(f)}))
        code = "def transform(text):\n    return text.replace('a', 'z')\n"
        out = transform_file_in_container(client, "abc123", posix, code)

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert "new_lines" in out
        assert out["new_lines"] == 3

    def test_runner_unchanged_emits_new_lines(self, tmp_path) -> None:
        from code_sandbox_mcp.edit_verify import transform_file_in_container
        from tests.conftest import _FakeClient, _FakeContainer

        posix = "/sandbox/x.py"
        f = tmp_path / "x.py"
        f.write_text("hello\nworld\n", encoding="utf-8")

        client = _FakeClient(_FakeContainer({posix: str(f)}))
        code = "def transform(text):\n    return text\n"
        out = transform_file_in_container(client, "abc123", posix, code)

        assert out["status"] == "ok"
        assert out["changed"] is False
        assert "new_lines" in out
        assert out["new_lines"] == 2


class TestRecordTierNag:
    """record_tier_nag writes a tier_nag operation to the journal."""

    def test_record_tier_nag_creates_entry(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.journal import get_or_create_run_id, read_journal, record_tier_nag

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            get_or_create_run_id("c123")
            record_tier_nag("c123", "/src/big.py", 1500, 1600)

            entries = read_journal(run_id=get_or_create_run_id("c123"))
            nag_entries = [e for e in entries if e["operation"] == "tier_nag"]
            assert len(nag_entries) == 1
            e = nag_entries[0]
            assert e["file_path"] == "/src/big.py"
            assert e["tier"] == 1500
            assert e["current_lines"] == 1600

    def test_multiple_tier_nags(self, tmp_path: Path) -> None:
        from code_sandbox_mcp.journal import get_or_create_run_id, read_journal, record_tier_nag

        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            run_id = get_or_create_run_id("c123")
            record_tier_nag("c123", "/src/big.py", 1500, 1600)
            record_tier_nag("c123", "/src/big.py", 3000, 3100)

            entries = read_journal(run_id=run_id)
            nags = [e for e in entries if e["operation"] == "tier_nag"]
            assert len(nags) == 2
            assert nags[0]["tier"] == 1500
            assert nags[1]["tier"] == 3000


class TestCheckFileSizeNag:
    """Tests for the _check_file_size_nag function."""

    def test_below_first_tier_no_nag(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import _check_file_size_nag

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_journal", lambda **k: []
        )
        result = _check_file_size_nag("c123", "/src/small.py", 100)
        assert result is None

    def test_above_first_tier_emits_nag(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import _check_file_size_nag

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_journal", lambda **k: []
        )
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_tier_nag",
            lambda *a, **k: None,
        )
        result = _check_file_size_nag("c123", "/src/big.py", 1600)
        assert result is not None
        assert "crossed 1500 lines" in result

    def test_above_first_tier_but_already_nagged(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import _check_file_size_nag

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_journal",
            lambda **k: [
                {
                    "operation": "tier_nag",
                    "file_path": "/src/big.py",
                    "tier": 1500,
                    "run_id": "fake-run-id",
                }
            ],
        )
        nags_recorded: list = []
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_tier_nag",
            lambda *a, **k: nags_recorded.append(a),
        )
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.get_or_create_run_id",
            lambda c: "fake-run-id",
        )
        result = _check_file_size_nag("c123", "/src/big.py", 1600)
        assert result is None
        assert len(nags_recorded) == 0

    def test_next_tier_after_nagged(self, monkeypatch) -> None:
        from code_sandbox_mcp.edit_verify import _check_file_size_nag

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_journal",
            lambda **k: [
                {
                    "operation": "tier_nag",
                    "file_path": "/src/big.py",
                    "tier": 1500,
                    "run_id": "fake-run-id",
                }
            ],
        )
        nags_recorded: list = []
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_tier_nag",
            lambda *a, **k: nags_recorded.append(a),
        )
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.get_or_create_run_id",
            lambda c: "fake-run-id",
        )
        result = _check_file_size_nag("c123", "/src/big.py", 3500)
        assert result is not None
        assert "crossed 3000 lines" in result
        assert len(nags_recorded) == 1
        assert nags_recorded[0][2] == 3000

    def test_same_tier_not_repeated(self, monkeypatch) -> None:
        """Multiple edits above the same threshold do not repeat the nag."""
        from code_sandbox_mcp.edit_verify import _check_file_size_nag

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_journal",
            lambda **k: [
                {
                    "operation": "tier_nag",
                    "file_path": "/src/big.py",
                    "tier": 1500,
                    "run_id": "fake-run-id",
                }
            ],
        )
        nags_recorded: list = []
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_tier_nag",
            lambda *a, **k: nags_recorded.append(a),
        )
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.get_or_create_run_id",
            lambda c: "fake-run-id",
        )
        result2 = _check_file_size_nag("c123", "/src/big.py", 1700)
        assert result2 is None

    def test_crosses_two_tiers_at_once(self, monkeypatch) -> None:
        """When a file crosses two tiers at once, only the first uninagged
        tier emits a nag."""
        from code_sandbox_mcp.edit_verify import _check_file_size_nag

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_journal", lambda **k: []
        )
        nags_recorded: list = []
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_tier_nag",
            lambda *a, **k: nags_recorded.append(a),
        )
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.get_or_create_run_id",
            lambda c: "fake-run-id",
        )
        result = _check_file_size_nag("c123", "/src/big.py", 5000)
        assert result is not None
        assert "crossed 1500 lines" in result
        assert len(nags_recorded) == 1


class TestParseTiers:
    """CODE_SANDBOX_FILE_TIERS env var parsing."""

    def test_default_tiers(self, monkeypatch) -> None:
        monkeypatch.delenv("CODE_SANDBOX_FILE_TIERS", raising=False)
        from code_sandbox_mcp.edit_verify import _parse_tiers

        tiers = _parse_tiers()
        assert tiers == [1500, 3000, 5000, 7500, 10000]

    def test_custom_tiers(self, monkeypatch) -> None:
        monkeypatch.setenv("CODE_SANDBOX_FILE_TIERS", "100,500,1000")
        from code_sandbox_mcp.edit_verify import _parse_tiers

        tiers = _parse_tiers()
        assert tiers == [100, 500, 1000]

    def test_invalid_env_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("CODE_SANDBOX_FILE_TIERS", "abc,def")
        from code_sandbox_mcp.edit_verify import _parse_tiers

        tiers = _parse_tiers()
        assert tiers == [1500, 3000, 5000, 7500, 10000]

    def test_empty_env_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("CODE_SANDBOX_FILE_TIERS", "")
        from code_sandbox_mcp.edit_verify import _parse_tiers

        tiers = _parse_tiers()
        assert tiers == [1500, 3000, 5000, 7500, 10000]


# ===================================================================
# Helpers
# ===================================================================


def _exec_run_for(content_bytes: bytes) -> callable:
    """Build an exec_run side effect matching write_file_sandbox tests."""

    def side_effect(cmd, **kwargs):
        shell = cmd[2] if isinstance(cmd, (list, tuple)) and len(cmd) > 2 else ""
        if shell.startswith("cat "):
            return (0, (content_bytes, b""))
        if shell.startswith("stat -c") and "%a" in shell:
            return (0, (b"1000 1000 644\n", b""))
        if shell.startswith("stat -c"):
            return (0, (b"1000 1000\n", b""))
        return (0, (b"", b""))

    return side_effect
