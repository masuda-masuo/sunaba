"""Tests for search output parsers (ripgrep, grep, ast-grep)."""

from __future__ import annotations

import json

from src.code_sandbox_mcp.edit_verify import (
    _parse_grep_output,
    _parse_rg_json,
    _parse_sg_json,
)

# ===================================================================
# _parse_ruff_output tests
# ===================================================================




class TestParseRgJson:
    """Tests for ripgrep --json output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_rg_json("", 50)["matches"] == []

    def test_single_match(self) -> None:
        raw = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "app.py"},
                    "lines": {"text": "def add(a, b):\n"},
                    "line_number": 42,
                },
            }
        )
        result = _parse_rg_json(raw, 50)["matches"]
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 42
        assert result[0]["text"] == "def add(a, b):"

    def test_skips_non_match_types(self) -> None:
        lines = [
            json.dumps({"type": "begin", "data": {"path": {"text": "x.py"}}}),
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": "x.py"},
                        "lines": {"text": "hello\n"},
                        "line_number": 5,
                    },
                }
            ),
            json.dumps({"type": "end", "data": {}}),
            json.dumps({"type": "summary", "data": {}}),
        ]
        result = _parse_rg_json("\n".join(lines), 50)["matches"]
        assert len(result) == 1
        assert result[0]["line"] == 5

    def test_max_results_cap(self) -> None:
        matches = []
        for i in range(10):
            matches.append(
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": f"file{i}.py"},
                            "lines": {"text": f"line {i}"},
                            "line_number": i,
                        },
                    }
                )
            )
        result = _parse_rg_json("\n".join(matches), 5)["matches"]
        assert len(result) == 5
        assert result[0]["file"] == "file0.py"
        assert result[-1]["file"] == "file4.py"

    def test_invalid_json_ignored(self) -> None:
        raw = "not valid json\n" + json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "ok.py"},
                    "lines": {"text": "ok"},
                    "line_number": 1,
                },
            }
        )
        result = _parse_rg_json(raw, 50)["matches"]
        assert len(result) == 1




class TestParseGrepOutput:
    """Tests for grep -rnI output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_grep_output("", 50)["matches"] == []

    def test_single_match(self) -> None:
        raw = "app.py:42:def add(a, b):"
        result = _parse_grep_output(raw, 50)["matches"]
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 42
        assert result[0]["text"] == "def add(a, b):"

    def test_multiple_matches(self) -> None:
        raw = "\n".join(
            [
                "src/a.py:1:import os",
                "src/b.py:5:x = 1",
                "src/c.py:10:return 42",
            ]
        )
        result = _parse_grep_output(raw, 50)["matches"]
        assert len(result) == 3
        assert result[1]["file"] == "src/b.py"
        assert result[1]["line"] == 5

    def test_path_with_colon(self) -> None:
        """Only the last colon pair is line:text; handle paths like 'a:b'."""
        raw = "src/main.py:15:x: int = 1"
        result = _parse_grep_output(raw, 50)["matches"]
        assert len(result) == 1
        assert result[0]["file"] == "src/main.py"
        assert result[0]["line"] == 15
        assert "int = 1" in result[0]["text"]

    def test_non_matching_lines_ignored(self) -> None:
        raw = "Binary file matches\nsome random output\napp.py:1:ok"
        result = _parse_grep_output(raw, 50)["matches"]
        assert len(result) == 1

    def test_max_results_cap(self) -> None:
        lines = [f"f{i}.py:1:text" for i in range(20)]
        result = _parse_grep_output("\n".join(lines), 7)["matches"]
        assert len(result) == 7




class TestParseSgJson:
    """Tests for ast-grep (sg) --json output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_sg_json("", 50)["matches"] == []

    def test_single_match(self) -> None:
        raw = json.dumps(
            [
                {
                    "file": "app.py",
                    "range": {"start": {"line": 5, "column": 4}},
                    "text": "def add(a, b):\n",
                }
            ]
        )
        result = _parse_sg_json(raw, 50)["matches"]
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 5
        assert result[0]["text"] == "def add(a, b):"

    def test_multiple_files_newline_separated(self) -> None:
        a = json.dumps([{"file": "a.py", "range": {"start": {"line": 1}}, "text": "a"}])
        b = json.dumps([{"file": "b.py", "range": {"start": {"line": 3}}, "text": "b"}])
        raw = a + "\n" + b
        result = _parse_sg_json(raw, 50)["matches"]
        assert len(result) == 2
        assert result[0]["file"] == "a.py"
        assert result[1]["file"] == "b.py"

    def test_max_results_cap(self) -> None:
        entries = [
            {"file": f"f{i}.py", "range": {"start": {"line": i}}, "text": f"text{i}"}
            for i in range(10)
        ]
        raw = json.dumps(entries)
        result = _parse_sg_json(raw, 5)["matches"]
        assert len(result) == 5

    def test_invalid_json_ignored(self) -> None:
        raw = "not json\n" + json.dumps(
            [{"file": "ok.py", "range": {"start": {"line": 1}}, "text": "ok"}]
        )
        result = _parse_sg_json(raw, 50)["matches"]
        assert len(result) == 1

    def test_dict_entry_handled(self) -> None:
        """Single dict per line (stream format) is wrapped and processed."""
        raw = json.dumps({"file": "app.py", "range": {"start": {"line": 5}}, "text": "hello"})
        result = _parse_sg_json(raw, 50)["matches"]
        assert len(result) == 1
        assert result[0]["file"] == "app.py"
        assert result[0]["line"] == 5
        assert result[0]["text"] == "hello"


# ===================================================================
# _determine_lint_severity tests (Issue #54)
# ===================================================================

