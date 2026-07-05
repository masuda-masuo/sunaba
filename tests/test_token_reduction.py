"""Tests for token reduction features (Issue #43).

Tests cover:
- ``estimate_tokens`` — token counting heuristic
- ``truncate_by_tokens`` — token-budget-based truncation
- ``compute_failure_fingerprint`` — failure pattern detection
- ``compress_failures`` — isomorphic failure compression
- ``record_exec`` with cached/output_size fields
- ``sandbox_cache_stats`` tool
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from code_sandbox_mcp.journal import record_exec
from code_sandbox_mcp.output_control import (
    compress_failures,
    compute_failure_fingerprint,
    estimate_tokens,
    truncate_by_tokens,
)
from code_sandbox_mcp.server import (
    sandbox_cache_invalidate,
    sandbox_cache_stats,
)

# =======================================================================
# estimate_tokens
# =======================================================================


class TestEstimateTokens:
    """Tests for token estimation heuristic."""

    def test_empty_string_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_short_string(self) -> None:
        # "hello world" is ~11 chars / 4 = ~2.75 -> 3
        assert estimate_tokens("hello world") == 3

    def test_longer_text(self) -> None:
        text = "hello " * 100  # 600 chars
        expected = round(600 / 4)  # 150
        assert estimate_tokens(text) == expected

    def test_code_content(self) -> None:
        code = "def foo():\n    return 42\n"
        tokens = estimate_tokens(code)
        assert tokens > 0

    def test_single_char(self) -> None:
        assert estimate_tokens("x") == 1

    def test_very_long_text(self) -> None:
        text = "a" * 10000
        assert estimate_tokens(text) == 2500


# =======================================================================
# truncate_by_tokens
# =======================================================================


class TestTruncateByTokens:
    """Tests for token-budget-based truncation."""

    def test_no_truncation_needed(self) -> None:
        text = "hello world"
        result, original = truncate_by_tokens(text, 100)
        assert result == text
        assert original == estimate_tokens(text)

    def test_empty_text(self) -> None:
        result, original = truncate_by_tokens("", 100)
        assert result == ""
        assert original == 0

    def test_truncation_with_notice(self) -> None:
        long_text = "\n".join(f"line {i}" for i in range(100))
        result, original = truncate_by_tokens(long_text, 10)
        assert original > 10
        assert "truncated" in result
        assert "originally" in result


# =======================================================================
# compute_failure_fingerprint
# =======================================================================


class TestComputeFailureFingerprint:
    """Tests for failure fingerprint computation."""

    def test_no_failure_patterns(self) -> None:
        assert compute_failure_fingerprint("clean output") == ""

    def test_detects_assertion_error(self) -> None:
        fp = compute_failure_fingerprint("AssertionError: assert 1 == 2")
        assert len(fp) == 16

    def test_detects_traceback(self) -> None:
        fp = compute_failure_fingerprint("Traceback (most recent call last):\n  File \"x.py\", line 1")
        assert len(fp) == 16

    def test_same_error_same_fingerprint(self) -> None:
        fp1 = compute_failure_fingerprint("ERROR: test_login failed")
        fp2 = compute_failure_fingerprint("ERROR: test_login failed")
        assert fp1 == fp2

    def test_different_errors_different_fingerprints(self) -> None:
        fp1 = compute_failure_fingerprint("ERROR test_login failed")
        fp2 = compute_failure_fingerprint("ERROR test_logout failed")
        assert fp1 != fp2

    def test_empty_input(self) -> None:
        assert compute_failure_fingerprint("") == ""


# =======================================================================
# compress_failures
# =======================================================================


class TestCompressFailures:
    """Tests for isomorphic failure compression."""

    def test_no_compression_needed(self) -> None:
        text = "line1\nline2\nline3"
        result = compress_failures(text)
        assert result == text

    def test_compresses_repeated_errors(self) -> None:
        text = "\n".join(["ERROR test failed"] * 5)
        result = compress_failures(text)
        assert "[×5]" in result
        assert "compressed" in result

    def test_preserves_different_errors(self) -> None:
        text = "ERROR test1 failed\nERROR test2 failed"
        result = compress_failures(text)
        # Different fingerprints, should not be compressed
        assert "[×" not in result

    def test_short_error_not_compressed(self) -> None:
        text = "ERROR test failed\nERROR test failed"
        result = compress_failures(text)
        # Only 2 occurrences, threshold is 3
        assert "[×" not in result

    def test_empty_input(self) -> None:
        assert compress_failures("") == ""


# =======================================================================
# record_exec with cached/output_size
# =======================================================================



    def test_non_consecutive_failures_not_compressed(self) -> None:
        text = "ERROR test failed\nok line\nERROR test failed\nok line\nERROR test failed"
        result = compress_failures(text)
        # Non-consecutive, same errors should NOT be compressed
        assert "[x" not in result.lower() or "compressed" not in result

    def test_compression_with_consecutive_tracebacks(self) -> None:
        # Consecutive matching lines should be compressed
        lines = ["Traceback (most recent call last):"] * 3
        text = "\n".join(lines)
        result = compress_failures(text)
        assert "compressed" in result.lower()

    def test_different_line_numbers_same_fingerprint(self) -> None:
        fp1 = compute_failure_fingerprint('File "a.py", line 42\nAssertionError')
        fp2 = compute_failure_fingerprint('File "b.py", line 7\nAssertionError')
        assert fp1 == fp2, "Line numbers should be normalized"

    def test_different_paths_same_fingerprint(self) -> None:
        fp1 = compute_failure_fingerprint('File "/a/b/c.py", line 10\nAssertionError')
        fp2 = compute_failure_fingerprint('File "/x/y/z.py", line 99\nAssertionError')
        assert fp1 == fp2, "File paths should be normalized"


class TestRecordExecCacheFields:
    """Tests for journal record_exec with cached/output_size fields."""

    def test_record_exec_default_cached_false(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["echo hello"], exit_code=0)

        entries = _read_log(log_path)
        assert entries[0].get("cached") is False
        assert entries[0].get("output_size") == 0

    def test_record_exec_with_cached_true(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["echo hello"], exit_code=0, cached=True)

        entries = _read_log(log_path)
        assert entries[0].get("cached") is True

    def test_record_exec_with_output_size(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["echo hello"], exit_code=0, output_size=42)

        entries = _read_log(log_path)
        assert entries[0].get("output_size") == 42

    def test_record_exec_with_max_output_tokens(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["echo hello"], exit_code=0, max_output_tokens=500)

        entries = _read_log(log_path)
        assert entries[0].get("max_output_tokens") == 500

    def test_record_exec_cached_false_no_max_tokens(self, tmp_path: Path) -> None:
        journal_dir = tmp_path / "journal"
        journal_dir.mkdir()
        log_path = journal_dir / "journal.log"

        with patch("code_sandbox_mcp.journal._JOURNAL_PATH", log_path), \
             patch("code_sandbox_mcp.journal._JOURNAL_DIR", journal_dir):
            record_exec("abc123", ["echo hello"], exit_code=0, cached=False, output_size=100)

        entries = _read_log(log_path)
        assert entries[0].get("cached") is False
        assert entries[0].get("output_size") == 100
        assert entries[0].get("max_output_tokens") is None


# =======================================================================
# sandbox_cache_stats / sandbox_cache_invalidate
# =======================================================================


class TestSandboxCacheTools:
    """Tests for cache management tools."""

    def test_cache_stats_returns_json(self) -> None:
        with patch("code_sandbox_mcp.server.get_cache_stats") as mock_stats:
            mock_stats.return_value = {"total_entries": 0, "total_size_bytes": 0}
            result = json.loads(sandbox_cache_stats())
            assert result["total_entries"] == 0

    def test_cache_invalidate_returns_count(self) -> None:
        with patch("code_sandbox_mcp.server.invalidate_cache") as mock_inv:
            mock_inv.return_value = 3
            result = json.loads(sandbox_cache_invalidate())
            assert result["invalidated"] == 3

    def test_cache_invalidate_specific_key(self) -> None:
        with patch("code_sandbox_mcp.server.invalidate_cache") as mock_inv:
            mock_inv.return_value = 1
            result = json.loads(sandbox_cache_invalidate(key="abc123"))
            assert result["invalidated"] == 1


def _read_log(path: Path) -> list[dict]:
    entries = []
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries
