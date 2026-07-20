"""Unit tests for _fetch_base_auto_include — the host-side GitHub API fetch
for Candidate C (issue #712).

These test the fetch function directly by mocking ``_github_api_request``,
not the container.  This proves AC 3: all content comes from host-side API
calls, never from the container.
"""

from __future__ import annotations

from unittest.mock import patch

from sunaba.tools.vcs.publishing import _fetch_base_auto_include

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ref_response(sha: str) -> dict:
    """Simulate a ``GET /repos/{repo}/git/refs/heads/{branch}`` response."""
    return {"ref": "refs/heads/test-branch", "object": {"sha": sha, "type": "commit"}}


def _make_repo_response(default_branch: str = "main") -> dict:
    """Simulate ``GET /repos/{repo}``."""
    return {"default_branch": default_branch, "full_name": "owner/repo"}


def _make_compare_response(
    files: list[dict],
    merge_base_commit: str = "base000",
) -> dict:
    """Simulate ``GET /repos/{repo}/compare/{base}...{head}``."""
    return {
        "merge_base_commit": {"sha": merge_base_commit},
        "status": "ahead",
        "ahead_by": len(files),
        "behind_by": 0,
        "total_commits": len(files),
        "files": files,
    }


def _make_file_entry(
    filename: str,
    status: str = "added",
    additions: int = 1,
    deletions: int = 0,
    changes: int = 1,
) -> dict:
    """Simulate one entry in a Compare-API ``files`` array."""
    return {
        "sha": "abc1234",
        "filename": filename,
        "status": status,
        "additions": additions,
        "deletions": deletions,
        "changes": changes,
        "raw_url": f"https://github.com/owner/repo/raw/main/{filename}",
    }


def _make_content_response(content: str) -> dict:
    """Simulate ``GET /repos/{repo}/contents/{path}?ref={sha}``.

    GitHub's Contents API returns base64-encoded content in the ``content``
    field with ``encoding`` set to ``"base64"``.
    """
    import base64

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return {
        "name": "test.txt",
        "path": "test.txt",
        "sha": "def5678",
        "content": encoded,
        "encoding": "base64",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchBaseAutoInclude:
    """Unit tests for ``_fetch_base_auto_include``."""

    def test_successful_fetch_with_default_branch_resolution(self) -> None:
        """Full happy path: resolves default branch, fetches refs, compares,
        and reads content for added/modified files."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),              # 0: repo info (default branch)
                _make_ref_response("feature111"),          # 1: feature branch ref
                _make_ref_response("base222"),             # 2: base branch ref
                _make_compare_response([                   # 3: compare
                    _make_file_entry("added.txt", "added"),
                    _make_file_entry("modified.txt", "modified"),
                    _make_file_entry("deleted.txt", "removed"),    # should be filtered
                    _make_file_entry("renamed.txt", "renamed"),    # should be filtered
                ]),
                _make_content_response("added content\n"),       # 4: content for added.txt
                _make_content_response("modified content\n"),    # 5: content for modified.txt
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",  # empty → resolve default
            )

        # Default branch resolution was called
        assert mock_api.call_args_list[0][0][0] == "/repos/owner/repo"

        # Feature branch ref
        assert "refs/heads/feat/x" in mock_api.call_args_list[1][0][0]

        # Base branch ref
        assert "refs/heads/main" in mock_api.call_args_list[2][0][0]

        # Compare API
        assert "/compare/feature111...base222" in mock_api.call_args_list[3][0][0]

        # Contents API calls (only for added + modified, 2 calls)
        assert mock_api.call_count == 6  # repo + 2 refs + compare + 2 contents

        # Result contains only added/modified files
        assert result is not None
        assert result == {
            "added.txt": "added content\n",
            "modified.txt": "modified content\n",
        }
        assert "deleted.txt" not in result
        assert "renamed.txt" not in result

    def test_explicit_base_branch_skips_resolution(self) -> None:
        """When base_branch is provided, no default-branch resolution call."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                _make_compare_response([]),
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="develop",
            )

        # No /repos/{repo} call
        repo_calls = [
            c for c in mock_api.call_args_list
            if c[0][0] == "/repos/owner/repo"
        ]
        assert len(repo_calls) == 0, (
            "expected no repo-resolution call with explicit base_branch"
        )
        # Feature and base branch refs were called
        assert "refs/heads/feat/x" in mock_api.call_args_list[0][0][0]
        assert "refs/heads/develop" in mock_api.call_args_list[1][0][0]
        assert result == {}

    def test_repo_resolution_failure_returns_none(self) -> None:
        """When the repo-info call raises, return None (safe)."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
            side_effect=RuntimeError("API rate limit"),
        ):
            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is None

    def test_feature_branch_ref_failure_returns_none(self) -> None:
        """When the feature-branch ref call raises, return None (safe)."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                RuntimeError("not found"),
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is None

    def test_feature_branch_does_not_exist_returns_empty(self) -> None:
        """When the feature branch has no remote ref yet, return {}."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                {"object": {}},  # no sha → empty string
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result == {}

    def test_base_branch_ref_failure_returns_none(self) -> None:
        """When the base-branch ref call raises, return None (safe).

        With an explicit base_branch, no repo-resolution call is made;
        the first API call is the feature-branch ref.
        """
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_ref_response("feature111"),  # feature ref (call 0)
                RuntimeError("ref not found"),      # base ref fails (call 1)
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="main",
            )

        assert result is None

    def test_compare_api_failure_returns_none(self) -> None:
        """When the Compare API call raises, return None (safe)."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                RuntimeError("compare failed"),
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is None

    def test_content_fetch_failure_skips_file(self) -> None:
        """When a single file's content fetch fails, skip that file but
        continue with others."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                _make_compare_response([
                    _make_file_entry("good.txt", "added"),
                    _make_file_entry("bad.txt", "added"),
                ]),
                _make_content_response("good content\n"),
                RuntimeError("contents API failed for bad.txt"),
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is not None
        assert result == {"good.txt": "good content\n"}

    def test_compare_with_no_files_returns_empty(self) -> None:
        """When Compare API returns no files, return empty dict."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                _make_compare_response([]),  # no files
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result == {}
        assert mock_api.call_count == 4  # repo + 2 refs + compare

    def test_non_base64_encoding_skipped(self) -> None:
        """Files with encoding != 'base64' are silently skipped."""
        import base64

        encoded = base64.b64encode(b"content").decode("ascii")

        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                _make_compare_response([
                    _make_file_entry("utf8.txt", "added"),
                    _make_file_entry("utf16.txt", "added"),
                ]),
                {  # normal: encoding=base64
                    "content": encoded,
                    "encoding": "base64",
                },
                {  # wrong encoding
                    "content": encoded,
                    "encoding": "utf-8",
                },
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is not None
        # utf8.txt was decoded, utf16.txt was skipped (wrong encoding)
        assert "utf8.txt" in result
        assert "utf16.txt" not in result

    def test_non_utf8_content_skipped(self) -> None:
        """Base64 content that isn't valid UTF-8 after decoding is silently
        skipped (see 'Not done' section in the report)."""
        import base64

        encoded = base64.b64encode(b"valid utf-8").decode("ascii")
        # \xff is never valid UTF-8
        bad_encoded = base64.b64encode(b"\xff\xfe").decode("ascii")

        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                _make_compare_response([
                    _make_file_entry("good.txt", "added"),
                    _make_file_entry("binary.bin", "added"),
                ]),
                {
                    "content": encoded,
                    "encoding": "base64",
                },
                {
                    "content": bad_encoded,
                    "encoding": "base64",
                },
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is not None
        assert "good.txt" in result
        assert "binary.bin" not in result  # silently dropped

    def test_empty_base_branch_after_resolution_returns_none(self) -> None:
        """When repo info returns no default_branch, return None."""
        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                {"default_branch": ""},  # empty default
            ]

            result = _fetch_base_auto_include(
                repo="owner/repo",
                token="ghp_test",
                branch="feat/x",
                base_branch="",
            )

        assert result is None

    def test_token_is_passed_to_all_calls(self) -> None:
        """The VCS token is passed to every _github_api_request call."""
        token = "ghp_test123"

        with patch(
            "sunaba.tools.github_api._github_api_request",
        ) as mock_api:
            mock_api.side_effect = [
                _make_repo_response("main"),
                _make_ref_response("feature111"),
                _make_ref_response("base222"),
                _make_compare_response([]),
            ]

            _fetch_base_auto_include(
                repo="owner/repo",
                token=token,
                branch="feat/x",
                base_branch="",
            )

        for call_obj in mock_api.call_args_list:
            assert call_obj[0][1] == token, (
                f"token not passed to API call: {call_obj[0][0]}"
            )
