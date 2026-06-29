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


class TestIsCacheable:
    """Tests for the volatile command detection (issue #329)."""

    # --- P1: Volatile git commands ---

    def test_git_add_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git add foo.py"]) is False

    def test_git_commit_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git commit -m test"]) is False

    def test_git_diff_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git diff"]) is False
        assert is_cacheable(["git diff --cached --stat"]) is False

    def test_git_status_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git status"]) is False
        assert is_cacheable(["git status --porcelain"]) is False

    def test_git_push_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git push origin main"]) is False

    def test_git_pull_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git pull"]) is False

    def test_git_checkout_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git checkout main"]) is False
        assert is_cacheable(["git switch feature"]) is False

    def test_git_log_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git log --oneline -5"]) is False

    def test_git_show_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git show HEAD"]) is False

    def test_git_reset_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git reset --soft HEAD~1"]) is False

    def test_git_stash_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git stash push -m wip"]) is False

    def test_git_merge_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git merge feature"]) is False

    def test_git_rebase_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git rebase main"]) is False

    def test_git_clone_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git clone https://example.com/repo.git"]) is False

    def test_git_fetch_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git fetch origin"]) is False

    def test_git_clean_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git clean -fd"]) is False

    def test_git_cherry_pick_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git cherry-pick abc123"]) is False

    def test_git_revert_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git revert HEAD"]) is False

    def test_git_blame_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git blame foo.py"]) is False

    def test_git_describe_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git describe --tags"]) is False

    def test_git_rev_parse_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git rev-parse HEAD"]) is False

    def test_git_ls_files_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git ls-files"]) is False

    def test_git_submodule_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git submodule update --init"]) is False

    def test_git_worktree_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git worktree add ../tmp"]) is False

    def test_git_branch_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git branch -d old"]) is False

    def test_git_tag_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git tag v1.0"]) is False

    def test_git_am_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git am patch.mbox"]) is False

    def test_git_bisect_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git bisect start"]) is False

    # --- P2: Non-git volatile programs ---

    def test_ls_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["ls -la"]) is False

    def test_cat_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["cat file.txt"]) is False

    def test_rm_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["rm -rf tmp"]) is False

    def test_touch_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["touch newfile"]) is False

    def test_mv_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["mv a b"]) is False

    def test_cp_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["cp a b"]) is False

    def test_mkdir_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["mkdir -p dir"]) is False

    def test_find_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["find . -name '*.py'"]) is False

    def test_stat_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["stat file"]) is False

    # --- Cacheable commands (allow-list) ---

    def test_echo_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["echo hello"]) is True

    def test_pip_install_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["pip install -e .[dev]"]) is True

    def test_python_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["python -m pytest tests/"]) is True
        assert is_cacheable(["python3 script.py"]) is True

    def test_npm_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["npm install"]) is True
        assert is_cacheable(["npx jest"]) is True

    def test_apt_get_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["apt-get update && apt-get install -y curl"]) is True

    def test_go_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["go build ./..."]) is True
        assert is_cacheable(["cargo build --release"]) is True

    def test_gcc_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["gcc -o prog main.c"]) is True

    def test_make_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["make install"]) is True

    def test_curl_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["curl -s https://example.com"]) is True

    def test_pytest_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["pytest tests/"]) is True

    def test_ruff_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["ruff check src/"]) is True

    def test_docker_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["docker build -t img ."]) is True

    def test_gh_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["gh pr list"]) is True

    # --- P4: Compound commands ---

    def test_compound_with_volatile_subcommand_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        # git add is volatile, the whole chain should be non-cacheable
        assert is_cacheable(["cd /repo && git add -A && git diff --cached --stat"]) is False

    def test_compound_all_cacheable_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["cd /repo && pip install -e .[dev] && python -m pytest tests/"]) is True

    def test_pipe_with_volatile_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git diff | cat"]) is False

    def test_semicolon_with_volatile_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["echo start; git status; echo done"]) is False

    def test_compound_git_add_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["git add -A && git diff --cached --stat"]) is False

    # --- Edge cases ---

    def test_empty_commands_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable([]) is True

    def test_empty_string_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["", "  "]) is True

    def test_env_prefix_git_add_is_not_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["GIT_DIR=/tmp git add foo.py"]) is False

    def test_unknown_program_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        # Unknown programs are cacheable by default (deny-list only)
        # — scripts, wrappers, and custom binaries produce
        # deterministic output for a given input.
        assert is_cacheable(["my-custom-tool arg1"]) is True

    def test_script_with_dot_prefix_is_cacheable(self):
        from code_sandbox_mcp.result_cache import is_cacheable
        assert is_cacheable(["./run_tests.sh"]) is True


class TestSplitCompoundCommands:
    """Tests for _split_compound_commands helper."""

    def test_split_on_double_ampersand(self):
        from code_sandbox_mcp.result_cache import _split_compound_commands
        result = _split_compound_commands("cmd1 && cmd2")
        assert result == ["cmd1", "cmd2"]

    def test_split_on_semicolon(self):
        from code_sandbox_mcp.result_cache import _split_compound_commands
        result = _split_compound_commands("cmd1; cmd2; cmd3")
        assert result == ["cmd1", "cmd2", "cmd3"]

    def test_split_on_pipe(self):
        from code_sandbox_mcp.result_cache import _split_compound_commands
        result = _split_compound_commands("cmd1 | cmd2")
        assert result == ["cmd1", "cmd2"]

    def test_split_on_or(self):
        from code_sandbox_mcp.result_cache import _split_compound_commands
        result = _split_compound_commands("cmd1 || cmd2")
        assert result == ["cmd1", "cmd2"]

    def test_single_command_unchanged(self):
        from code_sandbox_mcp.result_cache import _split_compound_commands
        result = _split_compound_commands("git diff")
        assert result == ["git diff"]

    def test_empty_string_returns_empty(self):
        from code_sandbox_mcp.result_cache import _split_compound_commands
        result = _split_compound_commands("   ")
        assert result == []


class TestFirstProgram:
    """Tests for _first_program helper."""

    def test_simple_program(self):
        from code_sandbox_mcp.result_cache import _first_program
        assert _first_program("git add foo.py") == "git"

    def test_strips_env_vars(self):
        from code_sandbox_mcp.result_cache import _first_program
        assert _first_program("VAR=val cmd arg") == "cmd"

    def test_strips_path_prefix(self):
        from code_sandbox_mcp.result_cache import _first_program
        assert _first_program("/usr/bin/git diff") == "git"

    def test_strips_multiple_env_vars(self):
        from code_sandbox_mcp.result_cache import _first_program
        assert _first_program("A=1 B=2 C=3 prog arg") == "prog"

    def test_no_tokens_returns_empty(self):
        from code_sandbox_mcp.result_cache import _first_program
        assert _first_program("") == ""

    def test_only_env_vars_returns_empty(self):
        from code_sandbox_mcp.result_cache import _first_program
        assert _first_program("A=1 B=2") == ""


class TestComputeCacheKeyWorkspaceFingerprint:
    """Tests for workspace fingerprint in cache key (issue #329 P3)."""

    def test_fingerprint_affects_key(self):
        from code_sandbox_mcp.result_cache import compute_cache_key
        k1 = compute_cache_key("img", ["cmd"], workspace_fingerprint="abc")
        k2 = compute_cache_key("img", ["cmd"], workspace_fingerprint="def")
        assert k1 != k2

    def test_no_fingerprint_still_works(self):
        from code_sandbox_mcp.result_cache import compute_cache_key
        key = compute_cache_key("img", ["cmd"])
        assert len(key) == 64

    def test_empty_fingerprint_equivalent_to_omitted(self):
        from code_sandbox_mcp.result_cache import compute_cache_key
        k1 = compute_cache_key("img", ["cmd"], workspace_fingerprint="")
        k2 = compute_cache_key("img", ["cmd"])
        assert k1 == k2

