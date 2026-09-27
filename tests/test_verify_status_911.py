"""Unit tests for issue #911: align verify status taxonomy and analytics.

Covers:
1. Four early completed-return paths of verify_in_container, asserting status
   and gate_passed together:
   - lint/type precondition failure (status="failed", gate_passed=False)
   - affected-scope run (status="failed", gate_passed=False)
   - filtered-run failure (status="failed", gate_passed=False)
   - no-languages-detected green path (status="ok", gate_passed=True)
2. Analytics handling in insights.verify_failure_reasons for timeout,
   in_progress, and pre-#910 entries lacking status.
3. Phase failure classification in phase._entry_failed for verify tool_use
   outcomes with status "timeout" or "in_progress" vs other outcomes.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from sunaba.edit_verify import DetectionResult
from sunaba.insights import compute_all_insights, verify_failure_reasons
from sunaba.phase import _entry_failed
from sunaba.tools.verify import verify_in_container


class TestVerifyEarlyReturnStatus911:
    """AC: each completed path of verify_in_container returns status 'ok'
    exactly when gate_passed is True and 'failed' when False."""

    CID = "testcont911"

    @patch("sunaba.tools.verify._docker")
    def test_precondition_gate_failure_status_failed(
        self, mock_docker: MagicMock
    ) -> None:
        """Early return path 1: lint/type gate failure returns status='failed', gate_passed=False."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client
        mock_container.exec_run.return_value = (0, (b"src\ntests\n", b""))

        gate_ret = {
            "gate_passed": False,
            "incomplete": False,
            "lint": ["src/app.py:1:1: E999 Lint error"],
            "types": [],
            "patch_targets": [],
            "gate_fail_reasons": ["lint check failed"],
        }

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch(
            "sunaba.edit_verify.run_lint_type_gate",
            return_value=gate_ret,
        ):
            res_raw = verify_in_container(
                container_id=self.CID,
                path="tests/",
            )
            result = json.loads(res_raw)

        assert result["gate_passed"] is False
        assert result["status"] == "failed"
        assert result["tests"]["status"] == "skipped"
        assert "lint check failed" in result["gate_fail_reasons"]

    @patch("sunaba.tools.verify._docker")
    @patch("sunaba.edit_verify.write_file")
    def test_affected_scope_run_status_failed(
        self, mock_write_file: MagicMock, mock_docker: MagicMock
    ) -> None:
        """Early return path 2: affected-scope run returns status='failed', gate_passed=False."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        ns = "__SUNABA_NAMESTATUS__"
        changed_unstaged = b"1\t1\tsrc/app.py\n" + ns.encode() + b"\nM\tsrc/app.py\n"
        empty_staged = ns.encode() + b"\n"
        selector_ok = b'{"selected": ["tests/test_app.py"], "widen_reason": null}\n'
        green_report = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<testsuites name="pytest tests"><testsuite name="pytest" '
            b'errors="0" failures="0" skipped="0" tests="1" time="0.05" '
            b'timestamp="2026-01-01T00:00:00" hostname="h">'
            b'<testcase classname="tests.test_app" name="test_one" time="0.01"/>'
            b'</testsuite></testsuites>\n'
            b"---PYTEST-RAW---\n1 passed\n"
        )

        mock_container.exec_run.side_effect = [
            (0, (changed_unstaged, b"")),
            (0, (empty_staged, b"")),
            (0, (b"", b"")),  # git ls-files
            (0, (selector_ok, b"")),
            (0, (green_report, b"")),
        ]

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ):
            res_raw = verify_in_container(
                container_id=self.CID,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
                test_scope="affected",
            )
            result = json.loads(res_raw)

        assert result["gate_passed"] is False
        assert result["status"] == "failed"
        assert result["partial_test_run"] is True
        assert result["tests"]["full"]["status"] == "ok"

    @patch("sunaba.tools.verify._docker")
    def test_filtered_run_failure_status_failed(
        self, mock_docker: MagicMock
    ) -> None:
        """Early return path 3: filtered-run failure returns status='failed', gate_passed=False."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        failed_report = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<testsuites name="pytest tests"><testsuite name="pytest" '
            b'errors="0" failures="1" skipped="0" tests="1" time="0.05" '
            b'timestamp="2026-01-01T00:00:00" hostname="h">'
            b'<testcase classname="tests.test_app" name="test_one" time="0.01">'
            b'<failure message="boom">AssertionError: boom</failure>'
            b'</testcase></testsuite></testsuites>\n'
            b"---PYTEST-RAW---\n1 failed\n"
        )

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # unstaged
            (0, (b"", b"")),  # staged
            (0, (b"", b"")),  # ls-files
            (1, (failed_report, b"")),  # filtered pytest run
        ]

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ):
            res_raw = verify_in_container(
                container_id=self.CID,
                path="tests/",
                test_filter="test_filter_fail",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            )
            result = json.loads(res_raw)

        assert result["gate_passed"] is False
        assert result["status"] == "failed"
        assert result["partial_test_run"] is True
        assert "tests" in result and "filtered" in result["tests"]

    @patch("sunaba.tools.verify._docker")
    def test_no_languages_detected_status_ok(
        self, mock_docker: MagicMock
    ) -> None:
        """Early return path 4: no-languages-detected green path returns status='ok', gate_passed=True."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # unstaged
            (0, (b"", b"")),  # staged
            (0, (b"", b"")),  # ls-files
        ]

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages=set(), scope={}, reason="no recognized project markers"
            ),
        ), patch("sunaba.tools.verify.record_verify_success"):
            res_raw = verify_in_container(
                container_id=self.CID,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            )
            result = json.loads(res_raw)

        assert result["gate_passed"] is True
        assert result["status"] == "ok"
        assert "gate_pass_reason" in result
        assert "no languages detected" in result["gate_pass_reason"]

    @patch("sunaba.tools.verify._docker")
    def test_main_completed_path_pass_status_ok(
        self, mock_docker: MagicMock
    ) -> None:
        """Main completed path: full test pass returns status='ok', gate_passed=True."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        green_report = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<testsuites name="pytest tests"><testsuite name="pytest" '
            b'errors="0" failures="0" skipped="0" tests="1" time="0.05" '
            b'timestamp="2026-01-01T00:00:00" hostname="h">'
            b'<testcase classname="tests.test_app" name="test_one" time="0.01"/>'
            b'</testsuite></testsuites>\n'
            b"---PYTEST-RAW---\n1 passed\n"
        )
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # unstaged
            (0, (b"", b"")),  # staged
            (0, (b"", b"")),  # ls-files
            (0, (green_report, b"")),  # full pytest run
        ]

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ), patch("sunaba.tools.verify.record_verify_success"):
            res_raw = verify_in_container(
                container_id=self.CID,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            )
            result = json.loads(res_raw)

        assert result["gate_passed"] is True
        assert result["status"] == "ok"
        assert result["tests"]["full"]["status"] == "ok"

    @patch("sunaba.tools.verify._docker")
    def test_main_completed_path_fail_status_failed(
        self, mock_docker: MagicMock
    ) -> None:
        """Main completed path: full test failure returns status='failed', gate_passed=False."""
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_docker.return_value = mock_client

        failed_report = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<testsuites name="pytest tests"><testsuite name="pytest" '
            b'errors="0" failures="1" skipped="0" tests="1" time="0.05" '
            b'timestamp="2026-01-01T00:00:00" hostname="h">'
            b'<testcase classname="tests.test_app" name="test_one" time="0.01">'
            b'<failure message="boom">AssertionError: boom</failure>'
            b'</testcase></testsuite></testsuites>\n'
            b"---PYTEST-RAW---\n1 failed\n"
        )
        mock_container.exec_run.side_effect = [
            (0, (b"", b"")),  # unstaged
            (0, (b"", b"")),  # staged
            (0, (b"", b"")),  # ls-files
            (1, (failed_report, b"")),  # full pytest run
        ]

        with patch(
            "sunaba.edit_verify.detect_languages",
            return_value=DetectionResult(
                languages={"python"}, scope={"python": "."}, reason=None
            ),
        ):
            res_raw = verify_in_container(
                container_id=self.CID,
                path="tests/",
                skip_lint_gate=True,
                skip_type_gate=True,
                skip_patch_targets_gate=True,
            )
            result = json.loads(res_raw)

        assert result["gate_passed"] is False
        assert result["status"] == "failed"
        assert result["tests"]["full"]["status"] == "failed"


class TestInsightsAnalytics911:
    """AC: verify_failure_reasons excludes timeout/in_progress from total_failed and by_kind,
    reports separate timeout/in_progress counts, and preserves classification for pre-#910 entries."""

    def test_verify_failure_reasons_excludes_timeout_and_in_progress(self) -> None:
        state = {
            "run_timeout": {
                "verify_timeline": [
                    {
                        "type": "verify_outcome",
                        "status": "timeout",
                        "passed": False,
                        "fail_kinds": [],
                    }
                ],
            },
            "run_in_progress": {
                "verify_timeline": [
                    {
                        "type": "verify_outcome",
                        "status": "in_progress",
                        "passed": False,
                        "fail_kinds": [],
                    }
                ],
            },
            "run_failed_lint": {
                "verify_timeline": [
                    {
                        "type": "verify_outcome",
                        "status": "failed",
                        "passed": False,
                        "fail_kinds": ["lint"],
                    }
                ],
            },
            "run_pre_910_with_kinds": {
                "verify_timeline": [
                    {
                        "type": "verify_outcome",
                        # pre-#910: no 'status' key
                        "passed": False,
                        "fail_kinds": ["test", "lint"],
                    }
                ],
            },
            "run_pre_910_without_kinds": {
                "verify_timeline": [
                    {
                        "type": "verify_outcome",
                        # pre-#910: no 'status' key, no 'fail_kinds' key
                        "passed": False,
                    }
                ],
            },
            "run_passed": {
                "verify_timeline": [
                    {
                        "type": "verify_outcome",
                        "status": "ok",
                        "passed": True,
                    }
                ],
            },
        }

        res = verify_failure_reasons(state)

        # total_failed should count: run_failed_lint (1), run_pre_910_with_kinds (1), run_pre_910_without_kinds (1)
        assert res["total_failed"] == 3
        assert res["by_kind"] == {"lint": 2, "test": 1, "unknown": 1}
        assert res["timeout"] == 1
        assert res["in_progress"] == 1

        # Check compute_all_insights forwards additive keys correctly
        insights = compute_all_insights(state)
        vfr = insights["verify_failure_reasons"]
        assert vfr["total_failed"] == 3
        assert vfr["by_kind"] == {"lint": 2, "test": 1, "unknown": 1}
        assert vfr["timeout"] == 1
        assert vfr["in_progress"] == 1


class TestPhaseEntryFailed911:
    """AC: phase._entry_failed does not classify verify tool_use with status 'timeout'
    or 'in_progress' as failed, while preserving all other classifications."""

    def test_verify_tool_use_timeout_and_in_progress_not_failed(self) -> None:
        timeout_entry = {
            "operation": "tool_use",
            "tool_name": "verify_in_container",
            "params": {
                "result": {
                    "status": "timeout",
                    "gate_passed": False,
                }
            },
        }
        assert _entry_failed(timeout_entry) is False

        in_progress_entry = {
            "operation": "tool_use",
            "tool_name": "verify_in_container",
            "params": {
                "result": {
                    "status": "in_progress",
                    "gate_passed": False,
                }
            },
        }
        assert _entry_failed(in_progress_entry) is False

    def test_verify_tool_use_completed_classifications_preserved(self) -> None:
        failed_entry = {
            "operation": "tool_use",
            "tool_name": "verify_in_container",
            "params": {
                "result": {
                    "status": "failed",
                    "gate_passed": False,
                }
            },
        }
        assert _entry_failed(failed_entry) is True

        ok_entry = {
            "operation": "tool_use",
            "tool_name": "verify_in_container",
            "params": {
                "result": {
                    "status": "ok",
                    "gate_passed": True,
                }
            },
        }
        assert _entry_failed(ok_entry) is False

    def test_pre_910_verify_entry_without_status_preserved(self) -> None:
        pre_910_failed = {
            "operation": "tool_use",
            "tool_name": "verify_in_container",
            "params": {
                "result": {
                    "gate_passed": False,
                }
            },
        }
        assert _entry_failed(pre_910_failed) is True

        pre_910_passed = {
            "operation": "tool_use",
            "tool_name": "verify_in_container",
            "params": {
                "result": {
                    "gate_passed": True,
                }
            },
        }
        assert _entry_failed(pre_910_passed) is False

    def test_other_operations_classification_preserved(self) -> None:
        # exec failures and passes
        assert _entry_failed({"operation": "exec", "exit_code": 1}) is True
        assert _entry_failed({"operation": "exec", "exit_code": 0}) is False
        assert _entry_failed({"operation": "exec"}) is False  # start entry

        # other tool_use (e.g. file operations using 'ok')
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "edit_file",
                    "params": {"result": {"ok": False}},
                }
            )
            is True
        )
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "edit_file",
                    "params": {"result": {"ok": True}},
                }
            )
            is False
        )

        # non-outcome operations
        assert _entry_failed({"operation": "initialize"}) is False
        assert _entry_failed({"operation": "stop"}) is False

    def test_other_tool_use_with_timeout_or_in_progress_preserves_classification(self) -> None:
        """AC: timeout/in_progress is ignored ONLY for verify_in_container.
        For every other tool_use operation, prior classification (ok=False or
        gate_passed=False -> failure) is strictly preserved."""
        # Non-verify tool_use with status="timeout" and ok=False is still failed
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "edit_file",
                    "params": {
                        "result": {
                            "status": "timeout",
                            "ok": False,
                        }
                    },
                }
            )
            is True
        )

        # Non-verify tool_use with status="in_progress" and ok=False is still failed
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "write_file",
                    "params": {
                        "result": {
                            "status": "in_progress",
                            "ok": False,
                        }
                    },
                }
            )
            is True
        )

        # Non-verify tool_use with status="timeout" and gate_passed=False is still failed
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "custom_gate",
                    "params": {
                        "result": {
                            "status": "timeout",
                            "gate_passed": False,
                        }
                    },
                }
            )
            is True
        )

        # Non-verify tool_use with status="in_progress" and gate_passed=False is still failed
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "custom_gate",
                    "params": {
                        "result": {
                            "status": "in_progress",
                            "gate_passed": False,
                        }
                    },
                }
            )
            is True
        )

        # Non-verify tool_use with status="timeout" and ok=True is not failed
        assert (
            _entry_failed(
                {
                    "operation": "tool_use",
                    "tool_name": "edit_file",
                    "params": {
                        "result": {
                            "status": "timeout",
                            "ok": True,
                        }
                    },
                }
            )
            is False
        )
