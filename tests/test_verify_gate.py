"""Tests for run_verify gate logic and DetectionResult iteration (Issue #177)."""

from __future__ import annotations

# ===================================================================
# _parse_ruff_output tests
# ===================================================================




class TestVerifyGateLogic:
    """Tests for the gate logic in run_verify.

    These tests verify the gate decision algorithm without a live
    container by exercising the severity classification and
    gate-fail-reason logic directly.
    """

    def _simulate_gate(
        self,
        lint_results: list[dict],
        type_results: list[dict],
        test_results: dict,
        gate_on_lint_error: bool = True,
        gate_on_type_error: bool = False,
        gate_on_test_fail: bool = True,
        incomplete_layers: set[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Simulate the gate logic from run_verify."""
        reasons: list[str] = []

        # Incomplete check — mirrors run_verify layer_gate_map logic
        if incomplete_layers:
            layer_gate_map = {
                "lint": gate_on_lint_error,
                "type": gate_on_type_error,
                "test": gate_on_test_fail,
            }
            for layer_name in sorted(incomplete_layers):
                if layer_gate_map.get(layer_name, True):
                    reasons.append(
                        f"verification incomplete: {layer_name} not_available"
                    )

        if gate_on_lint_error:
            lint_errors = [
                r for r in lint_results
                if r.get("severity") == "error"
                # Keep in sync with run_verify gate logic
                and r.get("rule") not in ("no-linter", "error")
            ]
            if lint_errors:
                reasons.append(f"lint: {len(lint_errors)} error(s)")

        if gate_on_type_error:
            type_errors = [
                r for r in type_results
                if r.get("severity") == "error"
                and r.get("rule") not in ("no-typechecker", "error")
            ]
            if type_errors:
                reasons.append(f"type_check: {len(type_errors)} error(s)")

        if gate_on_test_fail:
            if test_results.get("status") == "failed":
                reasons.append(
                    f"tests: {test_results.get('failed', 0)} failure(s)"
                )


        return (len(reasons) == 0, reasons)

    def test_all_clean_passes_gate(self) -> None:
        passed, reasons = self._simulate_gate([], [], {"status": "ok", "passed": 10})
        assert passed is True
        assert reasons == []

    def test_lint_error_fails_gate_by_default(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "F401",
             "severity": "error", "message": "unused import"},
        ]
        passed, reasons = self._simulate_gate(
            lint, [], {"status": "ok", "passed": 5})
        assert passed is False
        assert any("lint" in r for r in reasons)

    def test_lint_warning_does_not_fail_gate(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "W291",
             "severity": "warning", "message": "trailing whitespace"},
        ]
        passed, _ = self._simulate_gate(lint, [], {"status": "ok", "passed": 5})
        assert passed is True

    def test_lint_info_does_not_fail_gate(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "I001",
             "severity": "info", "message": "unsorted imports"},
        ]
        passed, _ = self._simulate_gate(lint, [], {"status": "ok", "passed": 5})
        assert passed is True

    def test_no_linter_tool_does_not_fail_gate(self) -> None:
        lint = [
            {"file": "a.py", "line": 0, "rule": "no-linter",
             "severity": "info", "message": "ruff not installed"},
        ]
        passed, _ = self._simulate_gate(lint, [], {"status": "ok", "passed": 5})
        assert passed is True

    def test_type_error_passes_gate_by_default(self) -> None:
        types_ = [
            {"file": "a.py", "line": 10, "rule": "reportUnknownVariableType",
             "severity": "error", "message": "unknown type"},
        ]
        passed, _ = self._simulate_gate(
            [], types_, {"status": "ok", "passed": 5})
        assert passed is True

    def test_type_error_fails_gate_when_enabled(self) -> None:
        types_ = [
            {"file": "a.py", "line": 10, "rule": "reportUnknownVariableType",
             "severity": "error", "message": "unknown type"},
        ]
        passed, reasons = self._simulate_gate(
            [], types_, {"status": "ok", "passed": 5}, [],
            gate_on_type_error=True,
        )
        assert passed is False
        assert any("type_check" in r for r in reasons)

    def test_test_failure_fails_gate(self) -> None:
        test = {"status": "failed", "passed": 8, "failed": 2, "duration": 1.5}
        passed, reasons = self._simulate_gate([], [], test)
        assert passed is False
        assert any("tests" in r for r in reasons)

    def test_test_failure_passes_when_gate_disabled(self) -> None:
        test = {"status": "failed", "passed": 8, "failed": 2, "duration": 1.5}
        passed, _ = self._simulate_gate(
            [], [], test, [], gate_on_test_fail=False
        )
        assert passed is True





    def test_multiple_fail_reasons_accumulate(self) -> None:
        lint = [
            {"file": "a.py", "line": 5, "rule": "F401",
             "severity": "error", "message": "unused import"},
        ]
        test = {"status": "failed", "passed": 5, "failed": 1, "duration": 0.5}
        passed, reasons = self._simulate_gate(lint, [], test)
        assert passed is False
        assert len(reasons) == 2

    def test_skipped_test_does_not_fail_gate(self) -> None:
        test = {"status": "skipped", "message": "no test output"}
        passed, _ = self._simulate_gate([], [], test)
        assert passed is True

    def test_incomplete_type_layer_passes_gate_when_flag_false(self) -> None:
        passed, reasons = self._simulate_gate(
            [], [], {"status": "ok", "passed": 5},
            gate_on_type_error=False,
            incomplete_layers={"type"},
        )
        assert passed is True

    def test_incomplete_type_layer_fails_gate_when_flag_true(self) -> None:
        passed, reasons = self._simulate_gate(
            [], [], {"status": "ok", "passed": 5},
            gate_on_type_error=True,
            incomplete_layers={"type"},
        )
        assert passed is False
        assert any("incomplete" in r for r in reasons)

    def test_incomplete_lint_layer_passes_gate_when_flag_false(self) -> None:
        passed, reasons = self._simulate_gate(
            [], [], {"status": "ok", "passed": 5},
            gate_on_lint_error=False,
            incomplete_layers={"lint"},
        )
        assert passed is True

    def test_incomplete_lint_layer_fails_gate_when_flag_true(self) -> None:
        passed, reasons = self._simulate_gate(
            [], [], {"status": "ok", "passed": 5},
            gate_on_lint_error=True,
            incomplete_layers={"lint"},
        )
        assert passed is False
        assert any("incomplete" in r for r in reasons)


