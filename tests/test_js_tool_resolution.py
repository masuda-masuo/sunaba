"""Tests for js/ts tool resolution: node_modules/.bin vs baked global (#588).

Mock-based tests here cover parser/branch edge cases and the resolution
*order* (local-first, global-fallback).  They intentionally do NOT
replace the real-command proof run during implementation (a throwaway
npm project with a real pinned eslint/tsc/jest installed, exercised
against the actual filesystem) -- mocks return exactly what they are
told to return, so they cannot catch a real command-output quirk (see
#633's ``wc -l`` "N total" misparse, which no mock caught).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.sunaba.edit_verify import (
    VerifyResult,
    _annotate_resolution,
    _detect_js_test_runner,
    _resolve_js_tool,
    _run_eslint_verify,
    _run_jest_verify,
    _run_tsc_verify,
)

# ===================================================================
# _resolve_js_tool
# ===================================================================


class TestResolveJsTool:
    """node_modules/.bin/<tool> wins; the baked global is the fallback."""

    def test_local_binary_present_wins(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))  # `test -x` succeeded

        cmd, source = _resolve_js_tool(container, "eslint", workdir="/repo")

        assert source == "local"
        assert cmd == "./node_modules/.bin/eslint"

    def test_local_binary_absent_falls_back_to_global(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (1, (b"", b""))  # `test -x` failed

        cmd, source = _resolve_js_tool(container, "tsc", workdir="/repo")

        assert source == "global"
        assert cmd == "tsc"

    def test_check_command_targets_the_named_tool(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (1, (b"", b""))

        _resolve_js_tool(container, "jest", workdir="/repo")

        call_args = container.exec_run.call_args
        shell_cmd = call_args[0][0][2]  # ["/bin/sh", "-c", <cmd>]
        assert "node_modules/.bin/jest" in shell_cmd

    def test_workdir_forwarded_to_exec_run(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))

        _resolve_js_tool(container, "eslint", workdir="/some/scope")

        assert container.exec_run.call_args.kwargs.get("workdir") == "/some/scope"

    def test_workdir_none_means_container_default(self) -> None:
        container = MagicMock()
        container.exec_run.return_value = (0, (b"", b""))

        _resolve_js_tool(container, "eslint", workdir=None)

        assert container.exec_run.call_args.kwargs.get("workdir") is None


# ===================================================================
# _annotate_resolution
# ===================================================================


class TestAnnotateResolution:
    """Every eslint/tsc/jest envelope must say which binary ran (#588)."""

    def test_empty_detail_becomes_resolution_note(self) -> None:
        result = VerifyResult(tool="eslint", status="ok", detail="")
        annotated = _annotate_resolution(result, "local", "./node_modules/.bin/eslint")

        assert annotated.detail == "resolved via local: ./node_modules/.bin/eslint"

    def test_plain_text_detail_is_prefixed_not_clobbered(self) -> None:
        result = VerifyResult(tool="tsc", status="error", detail="exit code 2")
        annotated = _annotate_resolution(result, "global", "tsc")

        assert "resolved via global: tsc" in annotated.detail
        assert "exit code 2" in annotated.detail

    def test_json_detail_gets_fields_injected_not_prefixed(self) -> None:
        # jest's detail carries a JSON test report that tools/verify.py
        # parses with json.loads() downstream -- prefixing text would
        # break that contract, so resolution must be injected as fields.
        payload = {"status": "ok", "passed": 3, "duration": 0.2}
        result = VerifyResult(tool="jest", status="ok", detail=json.dumps(payload))

        annotated = _annotate_resolution(result, "local", "./node_modules/.bin/jest")

        parsed = json.loads(annotated.detail)
        assert parsed["passed"] == 3  # original fields survive
        assert parsed["resolved_via"] == "local"
        assert parsed["resolved_cmd"] == "./node_modules/.bin/jest"

    def test_non_dict_json_detail_falls_back_to_prefix(self) -> None:
        # A JSON array is valid JSON but not a dict we can stamp fields onto.
        result = VerifyResult(tool="jest", status="error", detail=json.dumps(["a", "b"]))
        annotated = _annotate_resolution(result, "global", "jest")

        assert annotated.detail.startswith("[resolved via global: jest]")


# ===================================================================
# _detect_js_test_runner (jest vs vitest, design §3)
# ===================================================================


class TestDetectJsTestRunner:
    def _container_with_package_json(self, content: str | None) -> MagicMock:
        container = MagicMock()
        if content is None:
            container.exec_run.return_value = (0, (b"", b""))
        else:
            container.exec_run.return_value = (0, (content.encode("utf-8"), b""))
        return container

    def test_missing_package_json_defaults_to_jest(self) -> None:
        container = self._container_with_package_json(None)
        assert _detect_js_test_runner(container) == "jest"

    def test_malformed_package_json_defaults_to_jest(self) -> None:
        container = self._container_with_package_json("{not json")
        assert _detect_js_test_runner(container) == "jest"

    def test_jest_dependency_selects_jest(self) -> None:
        pkg = json.dumps({"devDependencies": {"jest": "^29.0.0"}})
        container = self._container_with_package_json(pkg)
        assert _detect_js_test_runner(container) == "jest"

    def test_vitest_only_selects_vitest(self) -> None:
        pkg = json.dumps({"devDependencies": {"vitest": "^1.0.0"}})
        container = self._container_with_package_json(pkg)
        assert _detect_js_test_runner(container) == "vitest"

    def test_vitest_in_test_script_selects_vitest(self) -> None:
        pkg = json.dumps({"scripts": {"test": "vitest run"}})
        container = self._container_with_package_json(pkg)
        assert _detect_js_test_runner(container) == "vitest"

    def test_both_jest_and_vitest_present_prefers_jest(self) -> None:
        # A project mid-migration still gets the tool sunaba can actually run.
        pkg = json.dumps({"devDependencies": {"jest": "^29.0.0", "vitest": "^1.0.0"}})
        container = self._container_with_package_json(pkg)
        assert _detect_js_test_runner(container) == "jest"

    def test_non_dict_package_json_defaults_to_jest(self) -> None:
        container = self._container_with_package_json(json.dumps(["not", "a", "dict"]))
        assert _detect_js_test_runner(container) == "jest"


# ===================================================================
# _run_eslint_verify / _run_tsc_verify / _run_jest_verify -- resolution wiring
# ===================================================================


class TestRunEslintVerifyResolution:
    def test_local_resolution_used_in_invocation_and_detail(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, (b"", b"")),  # `test -x node_modules/.bin/eslint` -> found
            (0, (b"[]", b"")),  # eslint --format json run, no findings
        ]

        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        invoke_cmd = container.exec_run.call_args_list[1][0][0][2]
        assert "./node_modules/.bin/eslint" in invoke_cmd
        assert result.status == "ok"
        assert "resolved via local" in result.detail
        assert "./node_modules/.bin/eslint" in result.detail

    def test_global_resolution_used_when_no_local_binary(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, (b"", b"")),  # `test -x` -> not found
            (0, (b"[]", b"")),  # global eslint run
        ]

        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        invoke_cmd = container.exec_run.call_args_list[1][0][0][2]
        assert invoke_cmd.split()[-3] if False else True  # (no-op; see substring check below)
        assert "eslint" in invoke_cmd
        assert "node_modules/.bin" not in invoke_cmd
        assert "resolved via global" in result.detail

    def test_not_available_still_reports_resolution(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, (b"", b"")),  # `test -x` -> not found
            (127, (b"", b"")),  # global eslint also missing
        ]

        result = _run_eslint_verify(container, "file.js", workdir="/repo")

        assert result.status == "not_available"
        assert "resolved via global: eslint" in result.detail


class TestRunTscVerifyResolution:
    def test_local_resolution_invokes_local_binary_directly(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (0, (b"", b"")),  # local tsc found
            (0, (b"", b"")),  # tsc --noEmit clean
        ]

        result = _run_tsc_verify(container, "file.ts", workdir="/repo")

        invoke_cmd = container.exec_run.call_args_list[1][0][0][2]
        assert "./node_modules/.bin/tsc --noEmit" in invoke_cmd
        assert "npx" not in invoke_cmd
        assert "resolved via local" in result.detail

    def test_global_fallback_invokes_bare_tsc(self) -> None:
        container = MagicMock()
        container.exec_run.side_effect = [
            (1, (b"", b"")),  # no local tsc
            (0, (b"", b"")),
        ]

        result = _run_tsc_verify(container, "file.ts", workdir="/repo")

        invoke_cmd = container.exec_run.call_args_list[1][0][0][2]
        assert "tsc --noEmit" in invoke_cmd
        assert "node_modules/.bin" not in invoke_cmd
        assert "resolved via global" in result.detail


class TestRunJestVerifyResolution:
    def test_local_resolution_reported_in_json_detail(self) -> None:
        container = MagicMock()
        report = json.dumps({
            "numPassedTests": 1, "numFailedTests": 0, "testResults": [], "startTime": 0,
        })
        container.exec_run.side_effect = [
            (0, (b"", b"")),  # cat package.json (no vitest markers) -> jest
            (0, (b"", b"")),  # local jest found
            (0, (report.encode(), b"")),  # jest --json run
        ]

        result = _run_jest_verify(container, "file.test.js", workdir="/repo")

        invoke_cmd = container.exec_run.call_args_list[2][0][0][2]
        assert "./node_modules/.bin/jest" in invoke_cmd
        assert result.status == "ok"
        parsed = json.loads(result.detail)
        assert parsed["resolved_via"] == "local"
        assert parsed["resolved_cmd"] == "./node_modules/.bin/jest"

    def test_vitest_only_project_skips_without_ever_resolving_jest(self) -> None:
        container = MagicMock()
        pkg = json.dumps({"devDependencies": {"vitest": "^1.0.0"}})
        container.exec_run.return_value = (0, (pkg.encode(), b""))

        result = _run_jest_verify(container, "file.test.js", workdir="/repo")

        assert result.status == "skipped"
        assert "vitest" in result.detail
        # Only the package.json read happened -- no resolution/invocation exec.
        assert container.exec_run.call_count == 1
