"""Integration tests for search pipeline: arg building + dispatch + parsing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.sunaba.edit_verify import (
    _build_rg_args,
    _search_lexical,
    search_files,
)


class TestBuildRgArgs:
    """Tests for ripgrep argument construction per output_mode."""

    def test_content_mode_uses_json(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, output_mode="content")
        assert "--json" in args
        assert "--count-matches" not in args
        assert "--files-with-matches" not in args

    def test_count_mode_uses_count_matches(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, output_mode="count")
        assert "--count-matches" in args
        assert "--json" not in args
        assert "--files-with-matches" not in args

    def test_files_mode_uses_files_with_matches(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, output_mode="files_with_matches")
        assert "--files-with-matches" in args
        assert "--json" not in args
        assert "--count-matches" not in args

    def test_content_mode_has_max_results_plus_one(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, output_mode="content")
        idx = args.index("-m")
        assert args[idx + 1] == "51"

    def test_files_mode_uses_m_1(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, output_mode="files_with_matches")
        idx = args.index("-m")
        assert args[idx + 1] == "1"

    def test_count_mode_uses_max_results_plus_one(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, output_mode="count")
        idx = args.index("-m")
        assert args[idx + 1] == "51"

    def test_zero_max_results_omits_m(self) -> None:
        args = _build_rg_args("pattern", "/path", 0, output_mode="content")
        assert "-m" not in args

    def test_respects_ignore_case(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, ignore_case=True)
        assert "-i" in args

    def test_respects_glob(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, glob="*.py")
        idx = args.index("-g")
        assert args[idx + 1] == "*.py"

    def test_respects_context(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, context=3)
        idx = args.index("-C")
        assert args[idx + 1] == "3"


class TestSearchLexicalDispatch:
    """Tests that _search_lexical dispatches to correct parser per output_mode."""

    def _make_container(self, rg_output: str, exit_code: int = 0) -> MagicMock:
        container = MagicMock()
        container.exec_run.return_value = (exit_code, (rg_output.encode("utf-8"), b""))
        return container

    def test_content_mode_returns_full_matches(self) -> None:
        match = json.dumps({
            "type": "match",
            "data": {
                "path": {"text": "file.py"},
                "lines": {"text": "hello\n"},
                "line_number": 5,
            },
        })
        container = self._make_container(match)
        result = _search_lexical(container, "hello", "/path", 50, output_mode="content")
        assert "error" not in result
        assert len(result["matches"]) == 1
        assert result["matches"][0]["file"] == "file.py"
        assert result["matches"][0]["line"] == 5
        assert result["matches"][0]["text"] == "hello"

    def test_count_mode_returns_counts(self) -> None:
        container = self._make_container("file.py:42\nother.py:7\n")
        result = _search_lexical(container, "pattern", "/path", 50, output_mode="count")
        assert "error" not in result
        matches = result["matches"]
        assert len(matches) == 2
        assert matches[0]["file"] == "file.py"
        assert matches[0]["text"] == "42"
        assert matches[1]["file"] == "other.py"
        assert matches[1]["text"] == "7"

    def test_files_mode_returns_file_paths(self) -> None:
        container = self._make_container("file.py\nother.py\n")
        result = _search_lexical(container, "pattern", "/path", 50, output_mode="files_with_matches")
        assert "error" not in result
        matches = result["matches"]
        assert len(matches) == 2
        assert matches[0]["file"] == "file.py"
        assert matches[1]["file"] == "other.py"

    def test_rg_not_found_falls_back(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (127, (b"", b"rg: not found"))
        result = _search_lexical(container, "pattern", "/path", 50, output_mode="content")
        assert result["status"] == "error"
        assert "grep" in result["error"]

    def test_rg_error_includes_stderr(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (2, (b"", b"error: --count-matches cannot be used with --json"))
        result = _search_lexical(container, "pattern", "/path", 50, output_mode="count")
        assert result["status"] == "error"
        assert "ripgrep failed" in result["error"]
        assert "cannot be used" in result["error"]


class TestSearchFilesPipeline:
    """Minimal pipeline test: search_files container lookup + delegation."""

    @patch("sunaba.tools.verify._docker")
    def test_content_mode_via_search_files(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_container = MagicMock()
        match = json.dumps({
            "type": "match",
            "data": {
                "path": {"text": "a.txt"},
                "lines": {"text": "match\n"},
                "line_number": 3,
            },
        })
        mock_container.exec_run.return_value = (0, (match.encode("utf-8"), b""))
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = search_files(
            mock_client, "abc123", "match", path="/tmp",
            output_mode="content",
        )
        assert "error" not in result
        assert len(result["matches"]) == 1

    @patch("sunaba.tools.verify._docker")
    def test_count_mode_via_search_files(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"a.txt:5\nb.txt:3\n", b""))
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = search_files(
            mock_client, "abc123", "pattern", path="/tmp",
            output_mode="count",
        )
        assert "error" not in result
        assert len(result["matches"]) == 2
        assert result["matches"][0]["text"] == "5"

    @patch("sunaba.tools.verify._docker")
    def test_files_mode_via_search_files(self, mock_docker: MagicMock) -> None:
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"a.txt\nb.txt\n", b""))
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        result = search_files(
            mock_client, "abc123", "pattern", path="/tmp",
            output_mode="files_with_matches",
        )
        assert "error" not in result
        assert len(result["matches"]) == 2
        assert result["matches"][0]["file"] == "a.txt"
