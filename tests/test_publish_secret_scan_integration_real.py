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
from tests.conftest import (
    _decode,
    _exec_cmd,
    _make_client_mock,
    _make_publish_container,
    _make_publish_container_for_scan_test,
)

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


    # -- Criterion 1: non-zero exit blocks push ----------------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_scan_nonzero_exit_blocks_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode: detect-secrets exits non-zero --> publish blocked
        with secret_scan_state='error' and no commit/push.
        """
        container = _make_publish_container_for_scan_test(
            [(0, b"", b"")],  # test -f 'declared.txt'
            scan_exit_code=1,
            detect_secrets_scan_output=b"",
        )
        mock_docker.return_value = _make_client_mock(container)

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
        assert result["secret_scan_state"] == "error"
        assert "publish blocked by secret scan" in result["error"]

        # Verify no commit or push was issued.
        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "git commit" not in issued
        assert "git push" not in issued

        # Verify the scan command actually ran.
        assert "detect-secrets scan" in issued
        assert "failed" in result["secret_scan"]

    # -- Criterion 2: empty stdout blocks push ----------------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_scan_empty_stdout_blocks_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode: detect-secrets produces empty stdout --> blocked."""
        # Empty scan output with exit 0: run_secret_scan returns state error.
        container = _make_publish_container_for_scan_test(
            [(0, b"", b"")],  # test -f 'declared.txt'
            scan_exit_code=0,
            detect_secrets_scan_output=b"",
        )
        mock_docker.return_value = _make_client_mock(container)

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
        assert result["secret_scan_state"] == "error"
        assert "publish blocked by secret scan" in result["error"]

        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "git commit" not in issued
        assert "git push" not in issued

        # Response must name the failure type: empty output.
        assert "empty output" in result["secret_scan"]

    # -- Criterion 3: unparseable output blocks push ----------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_manifest_scan_unparseable_output_blocks_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Manifest mode: detect-secrets produces non-JSON stdout --> blocked."""
        container = _make_publish_container_for_scan_test(
            [(0, b"", b"")],  # test -f 'declared.txt'
            scan_exit_code=0,
            detect_secrets_scan_output=b"this is not valid json\n",
        )
        mock_docker.return_value = _make_client_mock(container)

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
        assert result["secret_scan_state"] == "error"
        assert "publish blocked by secret scan" in result["error"]

        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert "git commit" not in issued
        assert "git push" not in issued

        # Response must name the failure type: unparseable output.
        assert "unparseable" in result["secret_scan"]

    # -- Criterion 4: three error states are distinguishable --------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_scan_error_response_names_failure_type(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Each error branch produces a distinct message that names the
        specific failure (exit code, empty output, unparseable output)."""
        from sunaba.tools.secret_scan import run_secret_scan as real_run

        # ec != 0
        r1 = real_run(
            _make_publish_container_for_scan_test(
                [], scan_exit_code=1, detect_secrets_scan_output=b"",
            ),
            ["f.py"], "/tmp",
        )
        assert "exit 1" in r1["secret_scan"] or "failed" in r1["secret_scan"]

        # empty stdout
        r2 = real_run(
            _make_publish_container_for_scan_test(
                [], scan_exit_code=0, detect_secrets_scan_output=b"",
            ),
            ["f.py"], "/tmp",
        )
        assert "empty output" in r2["secret_scan"]

        # unparseable JSON
        r3 = real_run(
            _make_publish_container_for_scan_test(
                [], scan_exit_code=0, detect_secrets_scan_output=b"garbage",
            ),
            ["f.py"], "/tmp",
        )
        assert "unparseable" in r3["secret_scan"]

        # secret_scan_state is "error" for all three
        assert r1["secret_scan_state"] == "error"
        assert r2["secret_scan_state"] == "error"
        assert r3["secret_scan_state"] == "error"

    # -- Criterion 5: findings still block (regression) -------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_findings_still_block_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Findings block with same details as before."""
        finding_bytes = _make_finding_json(
            "secret.txt", 5, "Private Key",
        ).encode("utf-8")

        container = _make_publish_container(
            [(0, b"", b"")],  # test -f
            detect_secrets_scan_output=finding_bytes,
        )
        mock_docker.return_value = _make_client_mock(container)

        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["secret.txt"],
                working_dir="/root/repo",
            ))

        assert result["status"] == "error"
        assert result["step"] == "secret_scan"
        assert result["secret_scan_state"] == "findings"
        assert "publish blocked by secret scan" in result["error"]
        assert len(result["findings"]) > 0

    # -- Criterion 8: override bypasses error block -----------------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_override_bypasses_error_block(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """``secret_scan_override`` (in-memory flag) allows publish through
        an error block (ec != 0) when baseline is disabled.
        """
        from sunaba.tools.secret_scan import _OVERRIDE_MAP

        container = _make_publish_container_for_scan_test(
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
            scan_exit_code=1,  # scan fails
            detect_secrets_scan_output=b"",
        )
        mock_docker.return_value = _make_client_mock(container)

        # Set the override flag before calling publish (baseline OFF path)
        _OVERRIDE_MAP["abc123def456"] = True

        # Baseline disabled so the in-memory override is used
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["declared.txt"],
                working_dir="/root/repo",
            ))

        # Clean up the override flag
        _OVERRIDE_MAP.pop("abc123def456", None)

        # With override set, publish proceeds past the scan block.
        assert result["status"] == "pushed"
        assert result["secret_scan_state"] == "error"
        assert "failed" in result["secret_scan"]

        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        # The push command is ``git -c credential.helper=… push …`` so
        # ``"git push"`` as a literal does not appear.  Check for the
        # word ``push`` in the issued commands instead.
        assert " push " in f" {issued} "
        assert "detect-secrets scan" in issued

    # -- Criterion 8: override bypasses findings block too ----------------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_override_bypasses_findings_block(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """``secret_scan_override`` (in-memory flag) allows publish through
        a findings block when baseline is disabled.
        """
        from sunaba.tools.secret_scan import _OVERRIDE_MAP

        finding_bytes = _make_finding_json(
            "secret.txt", 5, "Private Key",
        ).encode("utf-8")

        container = _make_publish_container_for_scan_test(
            [
                (0, b"", b""),  # test -f 'secret.txt'
                (0, b"none\n", b""),  # MERGE_HEAD check
                (0, b"", b""),  # checkout -b
                (1, b"", b""),  # rev-parse --verify origin/fix/x (absent)
                (0, b"abc1234", b""),  # rev-parse --verify origin/HEAD
                (1, b"", b""),  # rev-parse --verify HEAD^2 (not a merge)
                (0, b"", b""),  # git reset --mixed origin/HEAD
                (0, b"", b""),  # git add -- 'secret.txt'
                (0, b"[fix/x abc1234] Fix\n1 file changed", b""),  # commit
                (0, b"", b""),  # git status --porcelain -z (no leftovers)
                (0, b"pushed", b""),  # git push
                (0, b"abc1234def5678", b""),  # rev-parse HEAD
            ],
            scan_exit_code=0,
            detect_secrets_scan_output=finding_bytes,
        )
        mock_docker.return_value = _make_client_mock(container)

        # Set the override flag before calling publish (baseline OFF path)
        _OVERRIDE_MAP["abc123def456"] = True

        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = _decode(publish(
                container_id="abc123def456",
                repo="owner/repo",
                branch="fix/x",
                message="Fix",
                files=["secret.txt"],
                working_dir="/root/repo",
            ))

        _OVERRIDE_MAP.pop("abc123def456", None)

        assert result["status"] == "pushed"
        assert result["secret_scan_state"] == "findings"

        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert " push " in f" {issued} "

    # -- Criterion 7: skipped (no detect-secrets) still publishes ---------

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_skipped_still_publishes(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """Container without detect-secrets: scan is skipped, publish proceeds."""
        container = _make_publish_container_for_scan_test(
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
            detect_secrets_available=False,  # no detect-secrets in image
            scan_exit_code=0,
            detect_secrets_scan_output=b"",
        )
        mock_docker.return_value = _make_client_mock(container)

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
        assert result["secret_scan_state"] == "skipped"
        assert "SKIPPED" in result["secret_scan"]
        assert "unavailable" in result["secret_scan"]

        issued = " ".join(
            str(_exec_cmd(c)) for c in container.exec_run.call_args_list
        )
        assert " push " in f" {issued} "

    # =====================================================================
    # Fail-closed: unknown state and missing key block publish (issue #704)
    # =====================================================================

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_unknown_scan_state_blocks_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """An unrecognised secret_scan_state value blocks the publish."""
        container = _make_publish_container(
            [(0, b"", b"")],  # test -f 'declared.txt'
        )
        mock_docker.return_value = _make_client_mock(container)

        with patch(
            "sunaba.tools.secret_scan.run_secret_scan",
            return_value={
                "secret_scan": "some_future_feature",
                "secret_scan_state": "future_state",
                "files_scanned": ["declared.txt"],
            },
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
        assert result["secret_scan_state"] == "future_state"
        assert "publish blocked by secret scan" in result["error"]

    @patch("sunaba.tools.vcs.publishing._docker")
    @patch("sunaba.tools.vcs.publishing.record_boundary_crossing")
    def test_missing_scan_state_key_blocks_push(
        self,
        mock_record: MagicMock,
        mock_docker: MagicMock,
    ) -> None:
        """A scan result with no secret_scan_state key blocks the publish."""
        container = _make_publish_container(
            [(0, b"", b"")],  # test -f 'declared.txt'
        )
        mock_docker.return_value = _make_client_mock(container)

        with patch(
            "sunaba.tools.secret_scan.run_secret_scan",
            return_value={
                "secret_scan": "clean",
                # Deliberately NO secret_scan_state key
                "files_scanned": ["declared.txt"],
            },
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
        # .get("secret_scan_state", "") returns "" when key absent
        assert result["secret_scan_state"] == ""
        assert "publish blocked by secret scan" in result["error"]

    # NOTE: the no-scan default in ``publish`` is deliberately not covered by
    # a test.  The manifest / not-manifest branches are exhaustive, so the
    # default is unreachable today and can only be reached by adding a third
    # branch -- and any test that forced it would have to fake a code path
    # that does not exist.  Its intent is pinned by the comment at the
    # declaration instead: the default is a blocking state, so a future
    # branch that forgets to scan fails closed.
