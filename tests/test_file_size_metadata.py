"""Tests for issue #187 (① only): file_size metadata on read/transform returns.

Scope is deliberately the non-breaking half of #187: ``read_file_range`` and
``transform_file`` returns gain a ``file_size`` field (lines / bytes /
approx_tokens).  The tier-nag half (②) and the ``write_file_sandbox`` return
contract are intentionally *not* touched here.
"""
from __future__ import annotations

import json

from code_sandbox_mcp.edit_verify import (
    _compute_file_size,
    _file_size_from_counts,
    read_file_lines,
    transform_file_in_container,
)


class TestComputeFileSize:
    def test_empty_string(self) -> None:
        assert _compute_file_size("") == {"lines": 0, "bytes": 0, "approx_tokens": 0}

    def test_single_line_no_newline(self) -> None:
        fs = _compute_file_size("hello")
        assert fs["lines"] == 1
        assert fs["bytes"] == 5
        assert fs["approx_tokens"] == 1

    def test_single_line_with_newline(self) -> None:
        fs = _compute_file_size("hello\n")
        assert fs["lines"] == 1
        assert fs["bytes"] == 6

    def test_multi_line_trailing_newline(self) -> None:
        fs = _compute_file_size("a\nb\nc\n")
        assert fs["lines"] == 3
        assert fs["bytes"] == 6

    def test_multi_line_no_trailing_newline(self) -> None:
        fs = _compute_file_size("a\nb\nc")
        assert fs["lines"] == 3
        assert fs["bytes"] == 5

    def test_unicode_byte_count(self) -> None:
        text = "日本語\n"
        fs = _compute_file_size(text)
        assert fs["lines"] == 1
        assert fs["bytes"] == len(text.encode("utf-8"))

    def test_delegates_to_from_counts(self) -> None:
        assert _compute_file_size("x\ny\n") == _file_size_from_counts(4, 2)


class TestFileSizeFromCounts:
    def test_basic(self) -> None:
        assert _file_size_from_counts(128000, 4200) == {
            "lines": 4200,
            "bytes": 128000,
            "approx_tokens": 32000,
        }


class TestReadFileLinesFileSize:
    def test_file_size_in_return(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_file",
            lambda _c, _p: "line1\nline2\nline3\n",
        )
        result = read_file_lines(
            container=None, file_path="t.txt", offset=0, limit=10
        )
        assert result["file_size"] == {"lines": 3, "bytes": 18, "approx_tokens": 4}

    def test_file_size_reflects_whole_file_not_page(self, monkeypatch) -> None:
        """Even when only a slice is shown, file_size describes the whole file."""
        whole = "\n".join(f"line{i}" for i in range(100)) + "\n"
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_file", lambda _c, _p: whole
        )
        result = read_file_lines(
            container=None, file_path="t.txt", offset=0, limit=5
        )
        assert result["shown"] == 5
        assert result["file_size"]["lines"] == 100

    def test_file_size_absent_on_error(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise ValueError("not found")

        monkeypatch.setattr("code_sandbox_mcp.edit_verify.read_file", _raise)
        result = read_file_lines(
            container=None, file_path="x.txt", offset=0, limit=10
        )
        assert "error" in result
        assert "file_size" not in result


class TestTransformRunnerNewLines:
    """The in-container runner now emits new_lines for both branches."""

    def _client(self, tmp_path, content: str):
        from tests.conftest import _FakeClient, _FakeContainer

        posix = "/sandbox/x.py"
        f = tmp_path / "x.py"
        f.write_text(content, encoding="utf-8")
        return posix, _FakeClient(_FakeContainer({posix: str(f)}))

    def test_changed_emits_new_lines(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_file_write", lambda *a, **k: None
        )
        posix, client = self._client(tmp_path, "a\nb\nc\n")
        out = transform_file_in_container(
            client, "abc123", posix,
            "def transform(text):\n    return text.replace('a', 'z')\n",
        )
        assert out["status"] == "ok"
        assert out["changed"] is True
        assert out["new_lines"] == 3

    def test_unchanged_emits_new_lines(self, tmp_path) -> None:
        posix, client = self._client(tmp_path, "hello\nworld\n")
        out = transform_file_in_container(
            client, "abc123", posix, "def transform(text):\n    return text\n",
        )
        assert out["status"] == "ok"
        assert out["changed"] is False
        assert out["new_lines"] == 2


class TestTransformFileFileSize:
    """The tools.file.transform_file wrapper surfaces file_size in its JSON."""

    def _drive(self, tmp_path, monkeypatch, content: str, code: str):
        from code_sandbox_mcp.tools import file as file_tools
        from tests.conftest import _FakeClient, _FakeContainer

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.record_file_write", lambda *a, **k: None
        )
        posix = "/sandbox/x.py"
        f = tmp_path / "x.py"
        f.write_text(content, encoding="utf-8")
        client = _FakeClient(_FakeContainer({posix: str(f)}))
        monkeypatch.setattr(file_tools, "_docker", lambda: client)
        return json.loads(file_tools.transform_file("abc123def456", posix, code))

    def test_changed_includes_file_size(self, tmp_path, monkeypatch) -> None:
        out = self._drive(
            tmp_path, monkeypatch, "a\nb\nc\n",
            "def transform(text):\n    return text.replace('a', 'z')\n",
        )
        assert out["status"] == "ok"
        assert out["changed"] is True
        assert out["file_size"] == {"lines": 3, "bytes": 6, "approx_tokens": 1}

    def test_unchanged_includes_file_size(self, tmp_path, monkeypatch) -> None:
        out = self._drive(
            tmp_path, monkeypatch, "a\nb\nc\n",
            "def transform(text):\n    return text\n",
        )
        assert out["status"] == "ok"
        assert out["changed"] is False
        assert out["file_size"] == {"lines": 3, "bytes": 6, "approx_tokens": 1}


class TestReadFileRangeFileSize:
    """read_file_range surfaces file_size (whole-file) in its JSON output."""

    def test_includes_file_size(self, monkeypatch) -> None:
        from code_sandbox_mcp.tools import file as file_tools
        from tests.conftest import _FakeClient, _FakeContainer

        monkeypatch.setattr(
            "code_sandbox_mcp.edit_verify.read_file", lambda _c, _p: "a\nb\nc\n"
        )
        monkeypatch.setattr(
            "code_sandbox_mcp.tools.file.record_tool_use", lambda *a, **k: None
        )
        client = _FakeClient(_FakeContainer())
        monkeypatch.setattr(file_tools, "_docker", lambda: client)

        out = json.loads(
            file_tools.read_file_range("abc123def456", "/x.py", offset=0, limit=10)
        )
        assert out["file_size"] == {"lines": 3, "bytes": 6, "approx_tokens": 1}
