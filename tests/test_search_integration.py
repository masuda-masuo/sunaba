"""Integration tests for search pipeline: arg building + dispatch + parsing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.sunaba.search import (
    _build_rg_args,
    _search_lexical,
    search_files,
)
from sunaba.tools.verify import search_in_container


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

    def test_rg_not_found_with_hidden_errors_instead_of_fallback(self) -> None:
        # The grep fallback cannot express --hidden/--no-ignore; falling back
        # silently would return results under a different exclusion contract
        # (the silent-miss class issue #851 exists to fix).
        container = MagicMock()
        container.exec_run.return_value = (127, (b"", b"rg: not found"))
        result = _search_lexical(container, "pattern", "/path", 50, hidden=True)
        assert result["status"] == "error"
        assert "hidden=True" in result["error"]
        assert container.exec_run.call_count == 1  # no grep attempt

    def test_rg_not_found_with_no_ignore_errors_instead_of_fallback(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (127, (b"", b"rg: not found"))
        result = _search_lexical(container, "pattern", "/path", 50, no_ignore=True)
        assert result["status"] == "error"
        assert "no_ignore=True" in result["error"]
        assert container.exec_run.call_count == 1

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


class TestBuildRgArgsHiddenNoIgnore:
    """hidden/no_ignore argument building (Issue #851)."""

    def test_hidden_adds_flag_and_keeps_git_excluded(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, hidden=True)
        assert "--hidden" in args
        # --hidden alone would also search .git/ (verified against rg 13),
        # so a basename exclusion glob keeps it out in effect.
        idx = args.index("-g")
        assert args[idx + 1] == "!.git"

    def test_no_ignore_adds_flag(self) -> None:
        args = _build_rg_args("pattern", "/path", 50, no_ignore=True)
        assert "--no-ignore" in args

    def test_both_flags(self) -> None:
        args = _build_rg_args(
            "pattern", "/path", 50, hidden=True, no_ignore=True,
        )
        assert "--hidden" in args
        assert "--no-ignore" in args
        assert ["-g", "!.git"] in [
            args[i:i + 2] for i in range(len(args) - 1)
        ]

    def test_defaults_are_byte_identical_to_today(self) -> None:
        # With both flags left False the built argv must be exactly the
        # pre-#851 argv: no --hidden, no --no-ignore, no .git glob.
        args = _build_rg_args(
            "pattern", "/path", 50, output_mode="content",
            hidden=False, no_ignore=False,
        )
        assert args == ["rg", "-n", "--json", "-m", "51", "pattern", "/path"]

    def test_defaults_across_output_modes(self) -> None:
        for mode in ("content", "count", "files_with_matches"):
            args = _build_rg_args(
                "pattern", "/path", 50, output_mode=mode,
                hidden=False, no_ignore=False,
            )
            assert "--hidden" not in args
            assert "--no-ignore" not in args
            assert "-g" not in args


class TestSearchLexicalForwardsFlags:
    """hidden/no_ignore must reach the executed rg argv (Issue #851)."""

    def test_flags_in_executed_argv(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))
        _search_lexical(
            container, "pattern", "/path", 50,
            hidden=True, no_ignore=True,
        )
        argv = container.exec_run.call_args[0][0]
        assert "--hidden" in argv
        assert "--no-ignore" in argv
        assert ["-g", "!.git"] in [
            argv[i:i + 2] for i in range(len(argv) - 1)
        ]

    def test_default_flags_absent_from_argv(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))
        _search_lexical(container, "pattern", "/path", 50)
        argv = container.exec_run.call_args[0][0]
        assert "--hidden" not in argv
        assert "--no-ignore" not in argv


class TestSearchFilesStructuralFlagErrors:
    """Structural mode has no hidden/ignore equivalent: flags must fail loudly."""

    def _client(self) -> MagicMock:
        client = MagicMock()
        client.containers.get.return_value = MagicMock()
        return client

    def test_hidden_with_structural_names_flag(self) -> None:
        result = search_files(
            self._client(), "abc123", "pattern", path="/tmp",
            mode="structural", hidden=True,
        )
        assert result["status"] == "error"
        assert "hidden=True" in result["error"]

    def test_no_ignore_with_structural_names_flag(self) -> None:
        result = search_files(
            self._client(), "abc123", "pattern", path="/tmp",
            mode="structural", no_ignore=True,
        )
        assert result["status"] == "error"
        assert "no_ignore=True" in result["error"]

    def test_both_flags_with_structural_name_both(self) -> None:
        result = search_files(
            self._client(), "abc123", "pattern", path="/tmp",
            mode="structural", hidden=True, no_ignore=True,
        )
        assert result["status"] == "error"
        assert "hidden=True" in result["error"]
        assert "no_ignore=True" in result["error"]

    def test_structural_without_flags_still_dispatches(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (127, (b"", b"sg: not found"))
        client = MagicMock()
        client.containers.get.return_value = container
        result = search_files(
            client, "abc123", "pattern", path="/tmp", mode="structural",
        )
        # Reached ast-grep (proving the flags-only guard did not hijack the
        # normal structural path).
        assert result["status"] == "error"
        assert "ast-grep" in result["error"]


class TestSearchInContainerThreading:
    """search_in_container forwards hidden/no_ignore to search_files (Issue #851)."""

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.search_files")
    def test_flags_forwarded_when_set(
        self, mock_impl: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = {
            "matches": [], "shown": 0, "total": 0, "truncated": False,
        }

        search_in_container(
            container_id="abc123", pattern="foo", path="/repo",
            hidden=True, no_ignore=True,
        )
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "foo", path="/repo", mode="lexical",
            max_results=50, glob=None, ignore_case=False, context=0,
            output_mode="content", offset=0,
            hidden=True, no_ignore=True,
        )

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.tools.verify.search_files")
    def test_defaults_omit_new_kwargs(
        self, mock_impl: MagicMock, mock_docker: MagicMock,
    ) -> None:
        mock_container = MagicMock()
        mock_client = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_impl.return_value = {
            "matches": [], "shown": 0, "total": 0, "truncated": False,
        }

        search_in_container(container_id="abc123", pattern="foo", path="/repo")
        mock_impl.assert_called_once_with(
            mock_client, "abc123", "foo", path="/repo", mode="lexical",
            max_results=50, glob=None, ignore_case=False, context=0,
            output_mode="content", offset=0,
        )
