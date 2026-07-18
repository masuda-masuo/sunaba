"""Tool-output parsers: convert linter/type-checker output to findings.

Each ``_parse_*`` function takes raw tool output and a file-path label,
and returns a ``list[dict[str, Any]]`` of findings in the common result
format.  Also provides the :data:`_TSC_TEXT_RE` regex, the
:data:`_RUFF_SEVERITY_MAP` dictionary, and the
:func:`_determine_lint_severity` helper.
"""

from __future__ import annotations

import json
import re
from typing import Any


def _parse_golangci_lint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse golangci-lint JSON output (when available)."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    results: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for issue in data.get("Issues", []):
            pos = issue.get("Pos", {})
            results.append({
                "file": pos.get("Filename", ""),
                "line": int(pos.get("Line", 0)),
                "rule": issue.get("FromLinter", "unknown"),
                "message": issue.get("Text", ""),
            })
    return results


def _parse_go_vet_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse go vet text output (file:line:col: message)."""
    results: list[dict[str, Any]] = []
    pat = re.compile(r"^(.+?):(\d+):\d+:\s*(.+)$")
    for line in raw.split("\n"):
        m = pat.match(line.strip())
        if m:
            results.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "rule": "go-vet",
                "message": m.group(3),
            })
    return results


def _parse_ruff_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse ruff JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []

    results: list[dict[str, Any]] = []
    for issue in issues:
        results.append(
            {
                "file": issue.get("filename", file_path),
                "line": int(issue.get("location", {}).get("row", 0)),
                "rule": issue.get("code", "unknown"),
                "message": issue.get("message", ""),
            }
        )
    return results


def _parse_pylint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse pylint JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(issues, list):
        return []

    results: list[dict[str, Any]] = []
    for issue in issues:
        results.append(
            {
                "file": issue.get("path", file_path),
                "line": int(issue.get("line", 0)),
                "rule": issue.get("symbol", issue.get("message-id", "unknown")),
                "message": issue.get("message", ""),
            }
        )
    return results


def _parse_eslint_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse eslint JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    results: list[dict[str, Any]] = []
    for result in data:
        fpath = result.get("filePath", file_path)
        for msg in result.get("messages", []):
            results.append(
                {
                    "file": fpath,
                    "line": int(msg.get("line", 0)),
                    "rule": msg.get("ruleId", "unknown"),
                    "message": msg.get("message", ""),
                }
            )
    return results


def _parse_pyright_output(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse pyright JSON output into the common result format."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    for diag in data.get("generalDiagnostics", []):
        results.append(
            {
                "file": diag.get("file", file_path),
                "line": int(diag.get("range", {}).get("start", {}).get("line", 0)) + 1,
                "rule": diag.get("rule", "unknown"),
                "message": diag.get("message", ""),
            }
        )
    return results


#: Regex for tsc text output: ``file(line,col): error TSXXXX: message``
_TSC_TEXT_RE = re.compile(
    r"^(.+?)\((\d+)(?:,\d+)?\):\s*(error|warning)\s+(TS\d+):\s*(.+)$"
)


def _parse_tsc_text(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse tsc text output into the common result format."""
    results: list[dict[str, Any]] = []
    for line in raw.split("\n"):
        m = _TSC_TEXT_RE.match(line)
        if m:
            results.append(
                {
                    "file": m.group(1),
                    "line": int(m.group(2)),
                    "rule": m.group(4),
                    "message": m.group(5),
                }
            )
    return results


def _parse_tsc_json(raw: str, file_path: str) -> list[dict[str, Any]]:
    """Parse tsc JSON output (``--listFiles`` style) if available."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    results: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for diag in data.get("diagnostics", []):
            results.append(
                {
                    "file": diag.get("file", {}).get("fileName", file_path),
                    "line": int(diag.get("file", {}).get("line", 0)),
                    "rule": diag.get("code", "unknown"),
                    "message": diag.get("messageText", ""),
                }
            )
    return results


# ---------------------------------------------------------------------------
# Severity helper for lint rules
# ---------------------------------------------------------------------------

#: Ruff rule code prefixes mapped to severity.
_RUFF_SEVERITY_MAP: dict[str, str] = {
    "E": "error",      # pycodestyle errors
    "F": "error",      # Pyflakes
    "B": "error",      # flake8-bugbear
    "RUF": "error",    # ruff-specific rules
    "W": "warning",    # pycodestyle warnings
    "C90": "warning",  # mccabe complexity
    "N": "warning",    # pep8-naming
    "D": "warning",    # pydocstyle
    "I": "info",       # isort
    "SIM": "info",     # flake8-simplify
    "PL": "info",      # Pylint
    "UP": "info",      # pyupgrade
    "CPY": "info",     # flake8-copyright
    "TID": "info",     # flake8-tidy-imports
    "TCH": "info",     # flake8-type-checking
    "Q": "info",       # flake8-quotes
    "RET": "info",     # flake8-return
    "ARG": "info",     # flake8-unused-arguments
    "PTH": "info",     # flake8-use-pathlib
    "G": "info",       # flake8-logging-format
    "PGH": "info",     # pygrep-hooks
    "S": "warning",    # flake8-bandit (security)
}


def _determine_lint_severity(rule: str) -> str:
    """Map a lint rule code to a severity level.

    Uses rule code prefix matching against
    :data:`_RUFF_SEVERITY_MAP`.  Falls back to ``"error"`` for
    unrecognised codes (conservative default).
    """
    if not rule:
        return "error"
    for prefix, severity in sorted(_RUFF_SEVERITY_MAP.items(),
                                   key=lambda x: -len(x[0])):
        if rule.startswith(prefix):
            return severity
    return "error"
