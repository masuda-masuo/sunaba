"""Tests for lint output parsers (ruff, pylint, eslint)."""

from __future__ import annotations

import json

from src.sunaba.edit_verify import (
    _determine_lint_severity,
    _determine_scope,
    _parse_eslint_output,
    _parse_pylint_output,
    _parse_ruff_output,
)

# ===================================================================
# _parse_ruff_output tests
# ===================================================================




class TestParseRuffOutput:
    """Tests for ruff JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_ruff_output("", "file.py") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "filename": "test.py",
                    "location": {"row": 5},
                    "code": "F401",
                    "message": "`os` imported but unused",
                },
            ]
        )
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "F401"
        assert "unused" in result[0]["message"]

    def test_multiple_issues(self) -> None:
        raw = json.dumps(
            [
                {
                    "filename": "a.py",
                    "location": {"row": 1},
                    "code": "E302",
                    "message": "blank lines",
                },
                {
                    "filename": "a.py",
                    "location": {"row": 5},
                    "code": "W291",
                    "message": "trailing space",
                },
            ]
        )
        result = _parse_ruff_output(raw, "file.py")
        assert len(result) == 2

    def test_invalid_json(self) -> None:
        assert _parse_ruff_output("not json", "file.py") == []


# ===================================================================
# _parse_pylint_output tests
# ===================================================================




class TestParsePylintOutput:
    """Tests for pylint JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_pylint_output("", "file.py") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "path": "test.py",
                    "line": 10,
                    "symbol": "unused-import",
                    "message-id": "W0611",
                    "message": "Unused import os",
                },
            ]
        )
        result = _parse_pylint_output(raw, "file.py")
        assert len(result) == 1
        assert result[0]["file"] == "test.py"
        assert result[0]["line"] == 10
        assert result[0]["rule"] == "unused-import"

    def test_invalid_json(self) -> None:
        assert _parse_pylint_output("corrupt", "file.py") == []


# ===================================================================
# _parse_eslint_output tests
# ===================================================================




class TestParseEslintOutput:
    """Tests for eslint JSON output parsing."""

    def test_empty_output(self) -> None:
        assert _parse_eslint_output("", "file.js") == []

    def test_single_issue(self) -> None:
        raw = json.dumps(
            [
                {
                    "filePath": "/app/file.js",
                    "messages": [
                        {
                            "line": 5,
                            "ruleId": "no-unused-vars",
                            "message": "'x' is defined but never used",
                        },
                    ],
                },
            ]
        )
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 1
        assert result[0]["file"] == "/app/file.js"
        assert result[0]["line"] == 5
        assert result[0]["rule"] == "no-unused-vars"

    def test_multiple_files(self) -> None:
        raw = json.dumps(
            [
                {
                    "filePath": "a.js",
                    "messages": [{"line": 1, "ruleId": "R1", "message": "m1"}],
                },
                {
                    "filePath": "b.js",
                    "messages": [{"line": 2, "ruleId": "R2", "message": "m2"}],
                },
            ]
        )
        result = _parse_eslint_output(raw, "file.js")
        assert len(result) == 2

    def test_invalid_json(self) -> None:
        assert _parse_eslint_output("bad", "file.js") == []


# ===================================================================
# run_verify gate logic tests (Issue #54)
# ===================================================================




class TestDetermineLintSeverity:
    """Tests for lint severity mapping from rule codes."""

    def test_error_rules(self) -> None:
        assert _determine_lint_severity("E501") == "error"
        assert _determine_lint_severity("F401") == "error"
        assert _determine_lint_severity("B006") == "error"
        assert _determine_lint_severity("RUF001") == "error"

    def test_warning_rules(self) -> None:
        assert _determine_lint_severity("W291") == "warning"
        assert _determine_lint_severity("S101") == "warning"
        assert _determine_lint_severity("C901") == "warning"
        assert _determine_lint_severity("N801") == "warning"
        assert _determine_lint_severity("D100") == "warning"

    def test_info_rules(self) -> None:
        assert _determine_lint_severity("I001") == "info"
        assert _determine_lint_severity("SIM101") == "info"
        assert _determine_lint_severity("PLW0603") == "info"
        assert _determine_lint_severity("UP006") == "info"
        assert _determine_lint_severity("TCH001") == "info"

    def test_unknown_rule_defaults_to_error(self) -> None:
        assert _determine_lint_severity("XYZ999") == "error"

    def test_empty_rule_defaults_to_error(self) -> None:
        assert _determine_lint_severity("") == "error"

    def test_longest_prefix_match(self) -> None:
        """C90 should match C90 prefix, not C prefix."""
        assert _determine_lint_severity("C901") == "warning"


# ===================================================================
# linter parser edge cases (Issue #54)
# ===================================================================




class TestLintFileParsers:
    """Edge cases for linter output parsers."""

    def test_ruff_no_issues(self) -> None:
        """Clean ruff output returns empty list."""
        assert _parse_ruff_output("[]", "file.py") == []

    def test_pylint_no_issues(self) -> None:
        assert _parse_pylint_output("[]", "file.py") == []

    def test_eslint_no_issues(self) -> None:
        assert _parse_eslint_output("[]", "file.js") == []

    def test_ruff_non_list_json(self) -> None:
        """Ruff output that is valid JSON but not a list."""
        assert _parse_ruff_output('{"summary": "ok"}', "file.py") == []


# ===================================================================
# ruff security rules (Issue #218)
# ===================================================================


class TestRuffSecurityRules:
    """Verify curated S-rule selection and severity mapping."""

    def test_security_rules_are_warning_not_error(self) -> None:
        """S-prefixed rules must map to 'warning', not 'error'."""
        security_rules = [
            "S102", "S113", "S301", "S302", "S307",
            "S313", "S324", "S501", "S506", "S507",
            "S602", "S603", "S701",
        ]
        for rule in security_rules:
            assert _determine_lint_severity(rule) == "warning", (
                f"{rule} should be 'warning', not 'error'"
            )

    def test_excluded_noisy_rules_still_map_to_warning(self) -> None:
        """Excluded rules (S101, S105-107, S110-112, S311) are still warning
        in the severity map — they are excluded at the ruff CLI level, not here."""
        noisy = ["S101", "S105", "S106", "S107", "S110", "S112", "S311"]
        for rule in noisy:
            assert _determine_lint_severity(rule) == "warning"

    def test_security_finding_parsed_correctly(self) -> None:
        """A realistic S-rule finding is parsed with correct fields."""
        raw = json.dumps([
            {
                "filename": "app.py",
                "location": {"row": 12},
                "code": "S602",
                "message": "subprocess call with shell=True identified",
            }
        ])
        result = _parse_ruff_output(raw, "app.py")
        assert len(result) == 1
        assert result[0]["rule"] == "S602"
        assert result[0]["line"] == 12
        assert result[0]["file"] == "app.py"

    def test_mixed_error_and_security_findings(self) -> None:
        """E/F errors and S warnings can coexist in one ruff run."""
        raw = json.dumps([
            {
                "filename": "app.py",
                "location": {"row": 1},
                "code": "F401",
                "message": "unused import",
            },
            {
                "filename": "app.py",
                "location": {"row": 5},
                "code": "S507",
                "message": "paramiko call without host key verification",
            },
        ])
        result = _parse_ruff_output(raw, "app.py")
        assert len(result) == 2
        rules = {r["rule"] for r in result}
        assert rules == {"F401", "S507"}
        assert _determine_lint_severity("F401") == "error"
        assert _determine_lint_severity("S507") == "warning"


# ===================================================================
# _determine_scope tests
# ===================================================================


class TestDetermineScope:
    """Tests for unified scope + workdir determination."""

    def test_src_in_absolute_path(self) -> None:
        """/src/ in path returns scope=src and workdir=parent of src/."""
        assert _determine_scope("/app/src/foo.py") == ("src", "/app")

    def test_src_in_deep_path(self) -> None:
        """/src/ nested deeper returns scope=src and workdir=parent of src/."""
        assert _determine_scope("/home/sandbox/project/src/lib/foo.py") == ("src", "/home/sandbox/project")

    def test_src_at_root_absolute(self) -> None:
        """/src/ at root (idx=0) returns scope=src and workdir='.'."""
        assert _determine_scope("/src/foo.py") == ("src", ".")

    def test_src_prefix_relative(self) -> None:
        """src/ prefix returns scope=src and workdir='.'."""
        assert _determine_scope("src/foo.py") == ("src", ".")

    def test_no_src_absolute(self) -> None:
        """No /src/ in absolute path returns scope=dirname and workdir=dirname."""
        assert _determine_scope("/home/sandbox/lib/foo.py") == ("/home/sandbox/lib", "/home/sandbox/lib")

    def test_no_src_root(self) -> None:
        """File in root returns scope='.' and workdir='.'."""
        assert _determine_scope("foo.py") == (".", ".")

    def test_no_src_relative_dir(self) -> None:
        """Relative path with no /src/ returns scope=dirname and workdir=dirname."""
        assert _determine_scope("lib/foo.py") == ("lib", "lib")
