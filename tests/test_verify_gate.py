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
            [], types_, {"status": "ok", "passed": 5},
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


    def test_incomplete_test_layer_passes_gate_when_flag_false(self) -> None:
        passed, reasons = self._simulate_gate(
            [], [], {"status": "ok", "passed": 5},
            gate_on_test_fail=False,
            incomplete_layers={"test"},
        )
        assert passed is True


# ===================================================================
# _run_pyright_verify tests
# ===================================================================




class TestRunVerifyDetectionResult:
    """Regression tests for #177: run_verify must iterate detected.languages,
    not the DetectionResult object itself.

    The bug was sorted(detected) instead of sorted(detected.languages), which
    raised 'DetectionResult' object is not iterable.
    """

    def _make_client(self, container_id="abc123abc123"):
        """Return a minimal mock Docker client."""
        from unittest.mock import MagicMock
        container = MagicMock()
        client = MagicMock()
        client.containers.get.return_value = container
        return client, container

    def test_python_file_does_not_raise(self, monkeypatch):
        """run_verify with a .py path must not raise TypeError (regression #177)."""
        from src.code_sandbox_mcp.edit_verify import DetectionResult, VerifyResult, run_verify

        client, _container = self._make_client()

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.detect_languages",
            lambda *a, **k: DetectionResult(
                languages={"python"}, scope={"python": "/app"}, reason=None
            ),
        )
        ok_result = VerifyResult(tool="ruff", status="ok", findings=[], exit_code=0)
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._dispatch_layer",
            lambda *a, **k: ok_result,
        )

        result = run_verify(client, "abc123abc123", "/app/main.py")
        assert result["gate_passed"] is True
        assert "python" in result["detected_languages"]

    def test_no_languages_detected_passes_gate(self, monkeypatch):
        """When no language is detected, run_verify must not iterate DetectionResult
        itself and should pass the gate (no findings)."""
        from src.code_sandbox_mcp.edit_verify import DetectionResult, run_verify

        client, _container = self._make_client()

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.detect_languages",
            lambda *a, **k: DetectionResult(
                languages=set(), scope={}, reason="no markers found"
            ),
        )

        result = run_verify(client, "abc123abc123", "/app/unknown")
        assert result["gate_passed"] is True
        assert result["detected_languages"] == []

    def test_polyglot_project_iterates_all_languages(self, monkeypatch):
        """run_verify must dispatch layers for every language in detected.languages."""
        from src.code_sandbox_mcp.edit_verify import DetectionResult, VerifyResult, run_verify

        client, _container = self._make_client()
        dispatched: list[tuple[str, str]] = []

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.detect_languages",
            lambda *a, **k: DetectionResult(
                languages={"python", "ts"},
                scope={"python": "/app/backend", "ts": "/app/frontend"},
                reason=None,
            ),
        )

        def fake_dispatch(container, path, lang, layer):
            dispatched.append((lang, layer))
            return VerifyResult(tool=layer, status="ok", findings=[], exit_code=0)

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._dispatch_layer",
            fake_dispatch,
        )

        result = run_verify(client, "abc123abc123", "/app")
        assert result["gate_passed"] is True
        dispatched_langs = {lang for lang, _ in dispatched}
        assert dispatched_langs == {"python", "ts"}
        assert result["detected_languages"] == ["python", "ts"]

    def test_explicit_language_override_is_respected(self, monkeypatch):
        """When language= is passed explicitly, detect_languages must receive it
        and run_verify must iterate only that language."""
        from src.code_sandbox_mcp.edit_verify import DetectionResult, VerifyResult, run_verify

        client, _container = self._make_client()
        detected_calls: list = []

        def fake_detect(container, path, language=None):
            detected_calls.append(language)
            return DetectionResult(
                languages={language} if language else set(),
                scope={language: path} if language else {},
                reason=None,
            )

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.detect_languages",
            fake_detect,
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._dispatch_layer",
            lambda *a, **k: VerifyResult(tool="x", status="ok", findings=[], exit_code=0),
        )

        result = run_verify(client, "abc123abc123", "/app", language="python")
        assert detected_calls == ["python"]
        assert "python" in result["detected_languages"]
