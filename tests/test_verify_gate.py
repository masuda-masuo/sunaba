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




class TestRunLintTypeGate:
    """Tests for run_lint_type_gate -- the pre-test lint+type gate (#293)."""

    def _vr(self, status, findings=None, tool="ruff"):
        from src.code_sandbox_mcp.edit_verify import VerifyResult
        return VerifyResult(
            tool=tool, status=status, findings=findings or [], exit_code=0
        )

    def _patch_detect(self, monkeypatch, languages={"python"}):
        from src.code_sandbox_mcp.edit_verify import DetectionResult
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify.detect_languages",
            lambda *a, **k: DetectionResult(
                languages=set(languages),
                scope={lang: "." for lang in languages},
                reason=None if languages else "no markers",
            ),
        )

    def test_clean_passes(self, monkeypatch):
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner",
            lambda *a, **k: self._vr("ok"),
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner",
            lambda *a, **k: self._vr("ok", tool="pyright"),
        )
        r = run_lint_type_gate(object(), "src")
        assert r["gate_passed"] is True
        assert r["incomplete"] is False
        assert r["gate_fail_reasons"] == []

    def test_warning_severity_lint_rule_still_fails_gate(self, monkeypatch):
        """Regression for #293: D101 is severity 'warning' but must fail the
        gate -- CI's ``ruff check`` exits non-zero for it regardless."""
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        d101 = self._vr("findings", [{
            "file": "src/x.py", "line": 1, "rule": "D101",
            "severity": "warning", "message": "Missing docstring",
        }])
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner",
            lambda *a, **k: d101,
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner",
            lambda *a, **k: self._vr("ok", tool="pyright"),
        )
        r = run_lint_type_gate(object(), "src")
        assert r["gate_passed"] is False
        assert any("lint" in reason for reason in r["gate_fail_reasons"])

    def test_type_error_fails_gate(self, monkeypatch):
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner",
            lambda *a, **k: self._vr("ok"),
        )
        type_err = self._vr("findings", [{
            "file": "src/x.py", "line": 2, "rule": "reportArgumentType",
            "severity": "error", "message": "bad type",
        }], tool="pyright")
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner",
            lambda *a, **k: type_err,
        )
        r = run_lint_type_gate(object(), "src")
        assert r["gate_passed"] is False
        assert any("type_check" in reason for reason in r["gate_fail_reasons"])

    def test_tool_absence_is_incomplete_but_does_not_block(self, monkeypatch):
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner",
            lambda *a, **k: self._vr("not_available"),
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner",
            lambda *a, **k: self._vr("not_available", tool="pyright"),
        )
        r = run_lint_type_gate(object(), "src")
        assert r["gate_passed"] is True
        assert r["incomplete"] is True

    def test_sentinel_only_findings_do_not_fail_gate(self, monkeypatch):
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        sentinel = self._vr("findings", [{
            "file": "src/x.py", "line": 0, "rule": "no-linter",
            "message": "no linter",
        }])
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner",
            lambda *a, **k: sentinel,
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner",
            lambda *a, **k: self._vr("ok", tool="pyright"),
        )
        r = run_lint_type_gate(object(), "src")
        assert r["gate_passed"] is True

    def test_no_languages_passes_vacuously(self, monkeypatch):
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch, languages=set())
        r = run_lint_type_gate(object(), "src")
        assert r["gate_passed"] is True
        assert r["detected_languages"] == []

    def test_gate_on_type_false_skips_type_layer(self, monkeypatch):
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner",
            lambda *a, **k: self._vr("ok"),
        )
        called = {"type": False}

        def _type_runner(*a, **k):
            called["type"] = True
            return self._vr("findings", [{"rule": "x", "severity": "error"}],
                            tool="pyright")
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner", _type_runner
        )
        r = run_lint_type_gate(object(), "src", gate_on_type=False)
        assert called["type"] is False
        assert r["gate_passed"] is True

    def test_gate_lint_runner_uses_plain_ruff(self, monkeypatch):
        """The gate must run ruff WITHOUT the security extend-select so it
        matches CI exactly."""
        from src.code_sandbox_mcp.edit_verify import _gate_lint_runner
        captured = {}

        def _fake_ruff(container, path, workdir=None, extra_select=True):
            captured["extra_select"] = extra_select
            from src.code_sandbox_mcp.edit_verify import VerifyResult
            return VerifyResult(tool="ruff", status="ok", findings=[], exit_code=0)
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._run_ruff_verify", _fake_ruff
        )
        _gate_lint_runner(object(), "src", "python", None)
        assert captured["extra_select"] is False

    def test_lint_scope_overrides_scope_for_lint_only(self, monkeypatch):
        """Regression for #417: lint_scope must reach the lint runner while
        the type runner keeps using *scope* unchanged -- CI has no
        type-check step, so only lint needs the wider src+tests scope."""
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        seen = {}

        def _fake_lint(container, path, lang, workdir):
            seen["lint_path"] = path
            return self._vr("ok")

        def _fake_type(container, path, lang, workdir):
            seen["type_path"] = path
            return self._vr("ok", tool="pyright")

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner", _fake_lint
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner", _fake_type
        )
        r = run_lint_type_gate(object(), "src", lint_scope=["src", "tests"])
        assert seen["lint_path"] == ["src", "tests"]
        assert seen["type_path"] == "src"
        assert r["gate_passed"] is True

    def test_lint_scope_defaults_to_scope_when_omitted(self, monkeypatch):
        """Back-compat: callers that don't pass lint_scope (e.g. direct
        run_lint_type_gate(container, "src") calls elsewhere in this test
        module) still lint the same scope as the type check."""
        from src.code_sandbox_mcp.edit_verify import run_lint_type_gate
        self._patch_detect(monkeypatch)
        seen = {}

        def _fake_lint(container, path, lang, workdir):
            seen["lint_path"] = path
            return self._vr("ok")

        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_lint_runner", _fake_lint
        )
        monkeypatch.setattr(
            "src.code_sandbox_mcp.edit_verify._gate_type_runner",
            lambda *a, **k: self._vr("ok", tool="pyright"),
        )
        run_lint_type_gate(object(), "src")
        assert seen["lint_path"] == "src"


class TestQuotePath:
    """Tests for _quote_path -- single path vs multi-path shell quoting (#417)."""

    def test_single_string_path_quoted_as_before(self):
        from src.code_sandbox_mcp.edit_verify import _quote_path
        assert _quote_path("src") == "src"
        assert _quote_path("a b") == "'a b'"

    def test_list_of_paths_quoted_as_separate_tokens(self):
        """A list must become multiple shell-quoted tokens, not one path
        string containing a literal space (which would name a
        non-existent directory)."""
        from src.code_sandbox_mcp.edit_verify import _quote_path
        assert _quote_path(["src", "tests"]) == "src tests"
        assert _quote_path(["a b", "c"]) == "'a b' c"


class TestPathDisplay:
    """Tests for _path_display -- parse-fallback label rendering (#417)."""

    def test_string_passthrough(self):
        from src.code_sandbox_mcp.edit_verify import _path_display
        assert _path_display("src") == "src"

    def test_list_joined_with_space(self):
        from src.code_sandbox_mcp.edit_verify import _path_display
        assert _path_display(["src", "tests"]) == "src tests"

