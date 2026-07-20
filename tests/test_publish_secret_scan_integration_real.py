"""Tests driving publish through the real run_secret_scan (issue #699).

These tests deliberately do NOT patch ``run_secret_scan`` or
``exec_in_container`` -- mocking them is what let #696 ship broken.  Instead
they use the extended ``_make_publish_container`` from conftest to inject
(detected-secrets scan / git diff-tree) output at the ``exec_run`` level,
while the real ``run_secret_scan`` parses and decides.

Pre-existing tests in ``test_publish.py`` (which patch the scan away) are
left unchanged.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from sunaba.proxy_lifecycle import ENABLE_EGRESS_PROXY_ENV
from sunaba.tools.vcs.publishing import publish
from tests.conftest import _decode, _exec_cmd, _make_client_mock, _make_publish_container

# ============================================================================
# Helpers: build fixture data at runtime (no literal secrets)
# ============================================================================


def _make_clean_scan_json() -> str:
    """Return JSON with no findings (empty results dict)."""
    return json.dumps({
        "generated_at": "2026-07-20T00:00:00Z",
        "plugins_used": [],
        "results": {},
    })


def _make_finding_json(filename: str, line: int, secret_type: str) -> str:
    """Build detect-secrets JSON output with one finding.

    Matches the REAL output shape from detect-secrets: no ``is_secret``
    key (detect-secrets does not emit that).
    The ``hashed_secret`` is a synthetic SHA-256 hex string built at
    runtime so the committed file contains no actual secret hash.
    """
    fake_secret = "".join(
        chr(ord(c) + 1) for c in "no-real-secret-here"
    )
    hashed = hashlib.sha256(fake_secret.encode()).hexdigest()
    return json.dumps({
        "generated_at": "2026-07-20T00:00:00Z",
        "plugins_used": [{"name": "AWSKeyDetector"}],
        "results": {
            filename: [
                {
                    "type": secret_type,
                    "filename": filename,
                    "line_number": line,
                    "hashed_secret": hashed,
                    "is_verified": False,
                }
            ]
        },
    })


# ============================================================================
# Tests: publish driven with the real run_secret_scan
# ============================================================================


class TestPublishSecretScanReal:
    """publish() with the real ``run_secret_scan`` (NOT patched away).

    Every test here calls the real ``run_secret_scan`` function, which in
    turn calls the real ``exec_in_container`` -- the functions that #696
    broke.  The mock container's ``exec_run`` dispatch (set up by
    ``_make_publish_container``) supplies the responses.
    """

    @pytest.fixture(autouse=True)
    def _disable_egress_proxy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENABLE_EGRESS_PROXY_ENV, "false")

    # -- Criterion 1: manifest mode, finding blocks push -------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_findings_block_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode: real run_secret_scan finds a secret --> no commit, no push."""
        finding_bytes = _make_finding_json(
            "declared.txt", 3, "AWS Access Key",
        ).encode("utf-8")

        container = _make_publish_container(
            [(0, b"", b"")],  # test -f 'declared.txt'
            detect_secrets_scan_output=finding_bytes,
        )
        mock_docker.return_value = _make_client_mock(container)

        # Pin the baseline toggle to its production default (enabled) rather
        # than to the off path: the mock container reports no
        # ``.secrets.baseline``, so an unmatched finding survives either way,
        # and the covered configuration should be the deployed one.
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
                working_dir="/root/repo",
            ))

        assert result["status"] == "error"
        assert result["step"] == "secret_scan"
        assert "publish blocked by secret scan" in result["error"]

        # No git commit or push was issued (scan blocks before commit).
        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "git commit" not in issued
        assert "git push" not in issued

        # Criterion 4: verify the scan command actually ran with the file.
        assert "detect-secrets scan" in issued
        assert "declared.txt" in issued

    # -- Criterion 2: legacy mode, finding blocks push --------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_legacy_findings_block_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Legacy mode (no manifest): git diff-tree returns files, scan finds a
        secret --> commit happened but push is blocked.
        """
        finding_bytes = _make_finding_json(
            "secret.txt", 5, "Private Key",
        ).encode("utf-8")

        container = _make_publish_container(
            [
                (0, b"", b""),  # git ls-files --others --exclude-standard
                (0, b"none\n", b""),  # MERGE_HEAD check
                (0, b"", b""),  # git checkout -b
                (0, b"", b""),  # git add -A
                (1, b"", b"no upstream"),  # git rev-parse --abbrev-ref @{u}
                (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # git commit
            ],
            detect_secrets_scan_output=finding_bytes,
            git_diff_tree_output=b"secret.txt\n",
        )
        mock_docker.return_value = _make_client_mock(container)

        # Pin the baseline toggle to its production default (enabled) rather
        # than to the off path: the mock container reports no
        # ``.secrets.baseline``, so an unmatched finding survives either way,
        # and the covered configuration should be the deployed one.
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                working_dir="/root/repo",
            ))

        assert result["status"] == "error"
        assert result["step"] == "secret_scan"
        assert "publish blocked by secret scan" in result["error"]

        # Commit happened (legacy mode commits before scanning) but push
        # must NOT be issued.  The actual command is
        # ``git -c user.name=... commit -m ...``, so we check for ``commit -m``.
        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "commit -m" in issued
        assert "git push" not in issued

        # Criterion 4: verify the scan command ran with the diff-tree file.
        assert "detect-secrets scan" in issued
        assert "secret.txt" in issued

    # -- Criterion 3: clean scan reaches push -----------------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_clean_scan_reaches_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode: clean scan result reaches status='pushed' and
        reports secret_scan='clean' with the scanned file.
        """
        clean_bytes = _make_clean_scan_json().encode("utf-8")

        container = _make_publish_container(
            [
                (0, b"", b""),  # test -f 'declared.txt'
                (0, b"none\n", b""),  # MERGE_HEAD check
                (0, b"", b""),  # checkout -b
                (1, b"", b""),  # rev-parse --verify origin/fix/x (absent)
                (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
                (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge)
                (0, b"", b""),  # git reset --mixed origin/HEAD
                (0, b"", b""),  # git add -- 'declared.txt'
                (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # commit
                (0, b"", b""),  # git status --porcelain -z (no leftovers)
                (0, b"pushed", b""),  # git push
                (0, b"abc1234def5678", b""),  # rev-parse HEAD
            ],
            detect_secrets_scan_output=clean_bytes,
        )
        mock_docker.return_value = _make_client_mock(container)

        # Pin the baseline toggle to its production default (enabled) rather
        # than to the off path: the mock container reports no
        # ``.secrets.baseline``, so an unmatched finding survives either way,
        # and the covered configuration should be the deployed one.
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
                working_dir="/root/repo",
            ))

        assert result["status"] == "pushed"
        assert result["secret_scan"] == "clean"
        assert result["files_scanned"] == ["declared.txt"]

        # Criterion 4: verify the scan command actually ran.
        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "detect-secrets scan" in issued
        assert "declared.txt" in issued
