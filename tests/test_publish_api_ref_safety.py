"""Acceptance tests for safe pinned GitHub API ref updates (PR952)."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.vcs import publish
from tests.conftest import _decode, _make_client_mock, _make_publish_container


@pytest.fixture(autouse=True)
def _disable_egress_proxy(
    monkeypatch: pytest.MonkeyPatch, cut_parent_host_stub,
) -> None:
    monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")
    del cut_parent_host_stub
    monkeypatch.setattr(
        "sunaba.tools.secret_scan.run_secret_scan",
        lambda *args, **kwargs: {},
    )


def _fallback_container():
    return _make_publish_container([
        (0, b"", b""),  # declared file exists
        (0, b"", b""),  # fetch origin
        (1, b"", b""),  # HEAD^2: ordinary single-parent commit
        (0, b"", b""),  # upstream overwrite probe
        (0, b"none\n", b""),  # MERGE_HEAD check
        (0, b"", b""),  # checkout -b
        (1, b"", b""),  # remote publish branch does not exist locally
        (0, b"a" * 40, b""),  # origin/HEAD
        (0, b"", b""),  # reset --mixed origin/HEAD
        (0, b"", b""),  # git add declared file
        (1, b"diff --cached\n", b""),  # staged changes exist
        (0, b"[fix/x abc1234] Fix", b""),  # git commit
        (0, b"file1.py\n", b""),  # git diff-tree HEAD^ HEAD
        (0, b"", b""),  # git status --porcelain -z
        (1, b"", b"remote rejected: non-fast-forward"),  # native push
        (0, b"b" * 40, b""),  # validated HEAD
    ])


@patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
def test_public_publish_passes_default_force_policy_into_api_fallback(
    _record,
) -> None:
    """Pinned API fallback must receive the public default allow_force_push=False."""
    container = _fallback_container()
    client = _make_client_mock(container)

    with (
        patch(
            "sunaba.tools.secret_scan.run_secret_scan",
            return_value={
                "secret_scan": "clean",
                "secret_scan_state": "clean",
                "files_scanned": ["file1.py"],
                "scan_summary": "1 file scanned, clean",
            },
        ),
        patch("urllib.request.urlopen", side_effect=RuntimeError("network disabled")),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="test-token"),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch(
            "sunaba.tools.vcs.publishing._try_api_push",
            return_value={"status": "ok", "sha": "b" * 40},
        ) as api_push,
    ):
        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["file1.py"],
        ))

    assert result["status"] == "pushed", result
    assert api_push.call_args is not None
    assert api_push.call_args.kwargs["verified_sha"]
    assert "allow_force_push" in api_push.call_args.kwargs
    assert api_push.call_args.kwargs["allow_force_push"] is False


@patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
def test_public_publish_passes_explicit_force_authorization_to_api_fallback(
    _record,
) -> None:
    """Explicit allow_force_push=True reaches the API ref update."""
    container = _fallback_container()
    client = _make_client_mock(container)

    with (
        patch(
            "sunaba.tools.secret_scan.run_secret_scan",
            return_value={
                "secret_scan": "clean",
                "secret_scan_state": "clean",
                "files_scanned": ["file1.py"],
                "scan_summary": "1 file scanned, clean",
            },
        ),
        patch("urllib.request.urlopen", side_effect=RuntimeError("network disabled")),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="test-token"),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch(
            "sunaba.tools.vcs.publishing._try_api_push",
            return_value={"status": "ok", "sha": "b" * 40},
        ) as api_push,
    ):
        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["file1.py"],
            allow_force_push=True,
        ))

    assert result["status"] == "pushed", result
    assert api_push.call_args is not None
    assert api_push.call_args.kwargs["verified_sha"]
    assert "allow_force_push" in api_push.call_args.kwargs
    assert api_push.call_args.kwargs["allow_force_push"] is True


@patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
def test_public_publish_preserves_non_fast_forward_api_error_and_rerun_hint(
    _record,
) -> None:
    """A rejected pinned PATCH stays an intelligible public publish failure."""
    container = _fallback_container()
    client = _make_client_mock(container)
    rejection = (
        "GitHub API PATCH /repos/owner/repo/git/refs/heads/fix/x returned "
        "HTTP 422: Update is not a fast forward"
    )

    with (
        patch(
            "sunaba.tools.secret_scan.run_secret_scan",
            return_value={
                "secret_scan": "clean",
                "secret_scan_state": "clean",
                "files_scanned": ["file1.py"],
                "scan_summary": "1 file scanned, clean",
            },
        ),
        patch("urllib.request.urlopen", side_effect=RuntimeError("network disabled")),
        patch("sunaba.tools.vcs.publishing._resolve_vcs_token", return_value="test-token"),
        patch("sunaba.tools.vcs.publishing._docker", return_value=client),
        patch(
            "sunaba.tools.vcs.publishing._try_api_push",
            return_value={"status": "error", "error": rejection},
        ),
    ):
        result = _decode(publish(
            container_id="abc123def456",
            repo="owner/repo",
            branch="fix/x",
            message="Fix",
            files=["file1.py"],
        ))

    assert result["status"] == "error", result
    text = str(result).lower()
    assert "422" in text and "not a fast forward" in text, result
    assert "rerun" in text or "re-run" in text or "run publish again" in text, result
    assert "advance" in text or "moved" in text or "changed" in text, result
