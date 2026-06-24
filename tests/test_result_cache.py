"""Tests for the content-addressable result cache (Issue #43)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from code_sandbox_mcp.result_cache import (
    compute_cache_key,
    get_cache_stats,
    get_cached_result,
    invalidate_cache,
    set_cached_result,
)


class TestComputeCacheKey:
    """Tests for cache key computation."""

    def test_same_inputs_same_key(self) -> None:
        key1 = compute_cache_key("image@sha256:abc", ["echo hello"])
        key2 = compute_cache_key("image@sha256:abc", ["echo hello"])
        assert key1 == key2

    def test_different_image_different_key(self) -> None:
        key1 = compute_cache_key("image@sha256:abc", ["echo hello"])
        key2 = compute_cache_key("image@sha256:def", ["echo hello"])
        assert key1 != key2

    def test_different_commands_different_key(self) -> None:
        key1 = compute_cache_key("image@sha256:abc", ["echo hello"])
        key2 = compute_cache_key("image@sha256:abc", ["echo world"])
        assert key1 != key2

    def test_input_hash_affects_key(self) -> None:
        key1 = compute_cache_key("image", ["cmd"], input_hash="abc")
        key2 = compute_cache_key("image", ["cmd"], input_hash="def")
        assert key1 != key2

    def test_empty_input_hash_is_valid(self) -> None:
        key = compute_cache_key("image", ["cmd"])
        assert len(key) == 64  # SHA256 hexdigest
        assert all(c in "0123456789abcdef" for c in key)

    def test_commands_order_matters(self) -> None:
        key1 = compute_cache_key("image", ["cmd1", "cmd2"])
        key2 = compute_cache_key("image", ["cmd2", "cmd1"])
        assert key1 != key2

    def test_same_commands_same_order_same_key(self) -> None:
        key1 = compute_cache_key("image", ["cmd1", "cmd2"], input_hash="x")
        key2 = compute_cache_key("image", ["cmd1", "cmd2"], input_hash="x")
        assert key1 == key2


class TestSetGetCachedResult:
    """Tests for cache store and retrieve."""

    def test_set_and_get(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        key = "test_key_123"
        result = {"status": "ok", "output": "hello"}

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result(key, result, run_id="run1")
            cached = get_cached_result(key)
            assert cached == result

    def test_get_nonexistent_key(self, tmp_path: Path) -> None:
        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", tmp_path / "empty"):
            assert get_cached_result("nonexistent_key") is None

    def test_get_after_invalidate(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        key = "test_key_456"
        result = {"status": "ok", "output": "data"}

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result(key, result)
            assert get_cached_result(key) is not None
            invalidate_cache(key=key)
            assert get_cached_result(key) is None

    def test_get_expired_entry(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        key = "expired_key"

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir), \
             patch("code_sandbox_mcp.result_cache._CACHE_TTL_SECONDS", -1):
            set_cached_result(key, {"status": "ok"})
            # Entry should be expired immediately
            assert get_cached_result(key) is None

    def test_multiple_entries(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result("key1", {"a": 1}, run_id="r1")
            set_cached_result("key2", {"b": 2}, run_id="r2")

            assert get_cached_result("key1") == {"a": 1}
            assert get_cached_result("key2") == {"b": 2}

    def test_corrupted_entry(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True)
        key = "corrupted"
        (cache_dir / f"{key}.json").write_text("not valid json{{{")

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            assert get_cached_result(key) is None
            assert not (cache_dir / f"{key}.json").exists()

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        key = "overwrite"

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result(key, {"version": 1})
            set_cached_result(key, {"version": 2})
            cached = get_cached_result(key)
            assert cached == {"version": 2}


class TestInvalidateCache:
    """Tests for cache invalidation."""

    def test_invalidate_all(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result("key1", {"a": 1})
            set_cached_result("key2", {"b": 2})

            count = invalidate_cache()
            assert count == 2
            assert get_cached_result("key1") is None
            assert get_cached_result("key2") is None

    def test_invalidate_specific(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result("keep", {"a": 1})
            set_cached_result("remove", {"b": 2})

            count = invalidate_cache(key="remove")
            assert count == 1
            assert get_cached_result("keep") is not None
            assert get_cached_result("remove") is None

    def test_invalidate_nonexistent(self, tmp_path: Path) -> None:
        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", tmp_path / "cache"):
            count = invalidate_cache(key="nonexistent")
            assert count == 0

    def test_invalidate_empty_cache(self, tmp_path: Path) -> None:
        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", tmp_path / "empty"):
            count = invalidate_cache()
            assert count == 0


class TestGetCacheStats:
    """Tests for cache statistics."""

    def test_empty_cache_stats(self, tmp_path: Path) -> None:
        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", tmp_path / "empty"):
            stats = get_cache_stats()
            assert stats["total_entries"] == 0
            assert stats["total_size_bytes"] == 0

    def test_populated_cache_stats(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"

        with patch("code_sandbox_mcp.result_cache._CACHE_DIR", cache_dir):
            set_cached_result("key1", {"a": 1}, run_id="r1")
            set_cached_result("key2", {"b": 2}, run_id="r2")

            stats = get_cache_stats()
            assert stats["total_entries"] == 2
            assert stats["total_size_bytes"] > 0
            assert stats["oldest_entry_ts"] is not None
            assert stats["newest_entry_ts"] is not None
