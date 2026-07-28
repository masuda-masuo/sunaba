"""Tests for the Rust language addition to edit_verify (Cargo/Clippy).

Mirrors the existing per-language test style used for Go/Python/JS
(``tests/test_detect_languages.py``, ``tests/test_lint_parsers.py``,
``tests/test_type_check_parsers.py``, ``tests/test_test_report.py``)
but consolidated into a single file, as Rust is a single new addition
rather than an established subsystem.

The Clippy JSON fixtures under ``tests/fixtures/rust/*.ndjson`` and the
``cargo test`` text fixtures under ``tests/fixtures/rust/*.txt`` are
captured from real tool runs (rustc/cargo/clippy 1.97.1, 2026-07-29)
inside the sandbox rust image -- note the current panic-line format
includes a thread id (``thread 'name' (1234) panicked at ...``) and
error runs carry a span-less ``failure-note`` level message, both of
which the parsers must tolerate.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sunaba.edit_verify import (
    _DETECTION_MARKERS,
    _parse_clippy_output,
    detect_languages,
)
from sunaba.test_report import RustTestAdapter

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "rust"


def _load_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text("utf-8")


# ===================================================================
# Detection: .rs extension, Cargo.toml marker, polyglot, workspace-only
# ===================================================================


class TestDetectRust:
    """detect_languages picking up Rust via extension or Cargo.toml."""

    def test_file_extension_rust(self) -> None:
        mock_container = MagicMock()
        result = detect_languages(mock_container, "/app/src/main.rs")
        assert result.languages == {"rust"}
        assert result.scope.get("rust") == "/app/src/main.rs"
        assert result.reason is None
        # No exec_run needed -- extension alone resolves the language,
        # exactly like the existing .go case.
        mock_container.exec_run.assert_not_called()

    def test_directory_cargo_toml_detection(self) -> None:
        """A directory containing Cargo.toml is detected as rust."""
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/Cargo.toml\n", b""))

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"rust"}
        assert result.scope.get("rust") == "/app"

    def test_manifest_at_root_found_from_subdir_path(self) -> None:
        """Cargo.toml at the repo root is found even when called with a
        subdirectory path -- the same behaviour test_detect_languages.py
        documents for pyproject.toml via ``path="tests/"``.  detect_languages
        additionally searches "." (the working_dir root) whenever *path*
        differs from working_dir, which is exactly this case.
        """
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (0, (b"/app/Cargo.toml\n", b""))

        result = detect_languages(mock_container, "tests/", working_dir="/app")
        assert result.languages == {"rust"}
        assert result.scope.get("rust") == "/app"
        # Confirm the "." fallback search was actually built into the
        # find command sent to the container (not just coincidentally
        # matching the single mocked return value).
        call_args = mock_container.exec_run.call_args[0][0]
        find_cmd = call_args[2]
        assert "tests/" in find_cmd
        assert " . " in find_cmd or find_cmd.rstrip().endswith(".")

    def test_workspace_only_manifest_still_detects(self) -> None:
        """A Cargo workspace root with only [workspace] (no [package]) is
        detected identically to a package manifest.

        detect_languages never reads Cargo.toml's contents -- only its
        presence on disk via `find -name Cargo.toml` -- so a
        workspace-only manifest (the shape of the first real consumer,
        per the brief) takes exactly the same code path as a normal
        package manifest.  This test pins that behaviour down explicitly
        so a future content-sniffing "optimization" can't silently break
        workspace-only repos.
        """
        mock_container = MagicMock()
        # The find output looks identical whether Cargo.toml declares
        # [package] or only [workspace] -- detection doesn't care.
        mock_container.exec_run.return_value = (0, (b"/repo/Cargo.toml\n", b""))

        result = detect_languages(mock_container, "/repo")
        assert result.languages == {"rust"}
        assert result.scope.get("rust") == "/repo"

    def test_polyglot_rust_and_python(self) -> None:
        mock_container = MagicMock()
        mock_container.exec_run.return_value = (
            0,
            (b"/app/Cargo.toml\n/app/scripts/pyproject.toml\n", b""),
        )

        result = detect_languages(mock_container, "/app")
        assert result.languages == {"rust", "python"}
        assert result.scope.get("rust") == "/app"
        assert result.scope.get("python") == "/app/scripts"

    def test_cargo_toml_marker_registered(self) -> None:
        """Cargo.toml is wired into the marker table used by the find command."""
        patterns = dict(_DETECTION_MARKERS)
        assert patterns.get("Cargo.toml") == "rust"


# ===================================================================
# Clippy JSON output parsing (findings: file / line / severity / message)
# ===================================================================


class TestParseClippyOutput:
    """_parse_clippy_output against captured --message-format=json fixtures (rustc 1.97.1)."""

    def test_empty_output(self) -> None:
        assert _parse_clippy_output("", "file.rs") == []

    def test_invalid_json_lines_ignored(self) -> None:
        assert _parse_clippy_output("not json\n{also not json", "file.rs") == []

    def test_warning_only_fixture_yields_one_finding(self) -> None:
        raw = _load_fixture("clippy_warning_only.ndjson")
        result = _parse_clippy_output(raw, "fallback.rs")

        assert len(result) == 1
        finding = result[0]
        assert finding["file"] == "src/warn.rs"
        assert finding["line"] == 3
        assert finding["rule"] == "clippy::unnecessary_cast"
        assert finding["severity"] == "warning"
        assert "cast" in finding["message"]

    def test_error_fixture_distinguishes_severity(self) -> None:
        """A run with an error and a warning keeps them as distinct findings
        with distinct severities -- this is the concrete mechanism behind
        the outcome 'warnings must not be reported the same way as a run
        that fails to compile'.
        """
        raw = _load_fixture("clippy_with_error.ndjson")
        result = _parse_clippy_output(raw, "fallback.rs")

        assert len(result) == 2
        severities = {r["rule"]: r["severity"] for r in result}
        assert severities["E0308"] == "error"
        assert severities["clippy::unnecessary_cast"] == "warning"

        error_finding = next(r for r in result if r["rule"] == "E0308")
        assert error_finding["file"] == "src/broken.rs"
        assert error_finding["line"] == 2
        assert "mismatched types" in error_finding["message"]

    def test_summary_message_with_no_spans_is_not_a_finding(self) -> None:
        """cargo's trailing 'aborting due to N previous errors' message has
        no location and must not appear as a phantom zero-line finding.
        """
        raw = _load_fixture("clippy_with_error.ndjson")
        result = _parse_clippy_output(raw, "fallback.rs")
        assert not any(r["line"] == 0 for r in result)

    def test_non_compiler_message_reasons_ignored(self) -> None:
        raw = "\n".join([
            json.dumps({"reason": "compiler-artifact"}),
            json.dumps({"reason": "build-finished", "success": True}),
        ])
        assert _parse_clippy_output(raw, "file.rs") == []

    def test_note_and_help_levels_ignored(self) -> None:
        raw = json.dumps({
            "reason": "compiler-message",
            "message": {
                "level": "help",
                "message": "for further information visit https://...",
                "spans": [],
            },
        })
        assert _parse_clippy_output(raw, "file.rs") == []


# ===================================================================
# RustTestAdapter (cargo test plain-text output -> TestReport)
# ===================================================================


class TestRustTestAdapter:
    """RustTestAdapter against captured cargo test text fixtures (rustc 1.97.1)."""

    def test_all_pass_fixture(self) -> None:
        raw = _load_fixture("cargo_test_all_pass.txt")
        report = RustTestAdapter.parse(raw)

        assert report.status == "ok"
        # 2 unit tests + 1 doctest, summed across both per-binary blocks.
        assert report.passed == 3
        assert report.failed == 0
        assert report.failures is None
        assert report.duration == pytest.approx(0.05, rel=1e-6)

    def test_failure_fixture(self) -> None:
        raw = _load_fixture("cargo_test_with_failure.txt")
        report = RustTestAdapter.parse(raw)

        assert report.status == "failed"
        # 1 passing unit test + 1 passing doctest; the ignored test counts
        # in neither.
        assert report.passed == 2
        assert report.failed == 1
        assert report.failures is not None
        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure.test == "tests::it_fails"
        assert failure.file == "src/lib.rs"
        assert failure.line == 13
        assert "assertion" in failure.error

    def test_parse_json_is_an_alias_for_parse(self) -> None:
        """parse_json exists for interface parity even though cargo test's
        stable output is plain text, not JSON (mirrors TapAdapter)."""
        raw = _load_fixture("cargo_test_all_pass.txt")
        assert RustTestAdapter.parse_json(raw).to_dict() == RustTestAdapter.parse(raw).to_dict()

    def test_no_summary_line_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            RustTestAdapter.parse("not cargo test output at all")

    def test_multiple_binaries_sum_counts(self) -> None:
        """Two independent 'test result: ...' blocks (e.g. lib + integration
        test binary) are summed, not overwritten by the last one."""
        raw = (
            "running 1 test\n"
            "test a ... ok\n\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; "
            "0 filtered out; finished in 0.10s\n\n"
            "running 1 test\n"
            "test b ... ok\n\n"
            "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; "
            "0 filtered out; finished in 0.05s\n"
        )
        report = RustTestAdapter.parse(raw)
        assert report.passed == 2
        assert report.duration == pytest.approx(0.15, rel=1e-6)


# ===================================================================
# Runner envelopes: not_available when the Rust toolchain is missing
# ===================================================================


class TestRustToolchainAbsent:
    """When cargo is not on PATH, lint / type / test all report
    "not_available" rather than erroring or (worse) silently passing --
    the same established disposition Go's runners use for a missing
    ``go``/``golangci-lint`` binary (exit 127 == "command not found").
    """

    def _not_found_container(self) -> MagicMock:
        from tests.conftest import _make_docker_compliant_container

        # First scripted response answers the manifest-resolution probe
        # (a Cargo.toml exists); the cargo invocation itself then hits 127.
        return _make_docker_compliant_container([
            (0, b"Cargo.toml\n", b""),
            (127, b"", b"/bin/sh: cargo: not found\n"),
        ])

    def test_clippy_not_available(self) -> None:
        from sunaba.edit_verify import _run_clippy_verify

        result = _run_clippy_verify(self._not_found_container(), ".")
        assert result.status == "not_available"
        assert result.exit_code == 127

    def test_cargo_test_not_available(self) -> None:
        from sunaba.edit_verify import _run_cargo_test_verify

        result = _run_cargo_test_verify(self._not_found_container(), ".")
        assert result.status == "not_available"
        assert result.exit_code == 127

    def test_rust_type_layer_never_hits_the_container(self) -> None:
        """The type layer is a deliberate fold-in, not a tool invocation --
        it must not even attempt to exec anything, missing toolchain or not.
        """
        from sunaba.edit_verify import _run_rust_type_verify

        mock_container = MagicMock()
        result = _run_rust_type_verify(mock_container, ".")
        assert result.status == "skipped"
        mock_container.exec_run.assert_not_called()


# ===================================================================
# Clippy runner: exit-code envelope semantics (findings vs error)
# ===================================================================


class TestRunClippyVerifyEnvelope:
    """_run_clippy_verify's exit-code handling with a mocked container."""

    def _make_container(self, ec: int, stdout: str = "", stderr: str = ""):
        from tests.conftest import _make_docker_compliant_container

        # First scripted response answers the manifest-resolution probe.
        return _make_docker_compliant_container([
            (0, b"Cargo.toml\\n", b""),
            (ec, stdout.encode("utf-8"), stderr.encode("utf-8")),
        ])

    def test_exit_0_warnings_only_is_findings_not_error(self) -> None:
        from sunaba.edit_verify import _run_clippy_verify

        raw = _load_fixture("clippy_warning_only.ndjson")
        container = self._make_container(0, raw)

        result = _run_clippy_verify(container, ".")
        assert result.status == "findings"
        assert result.findings[0]["severity"] == "warning"

    def test_exit_101_with_error_is_findings_with_error_severity(self) -> None:
        """Exit 101 (cargo's failure convention) with real diagnostic
        output is still status="findings" -- like every other lint
        runner here, severity (not status) is what distinguishes a
        warnings-only run from one that failed to compile.
        """
        from sunaba.edit_verify import _run_clippy_verify

        raw = _load_fixture("clippy_with_error.ndjson")
        container = self._make_container(101, raw)

        result = _run_clippy_verify(container, ".")
        assert result.status == "findings"
        assert any(f["severity"] == "error" for f in result.findings)

    def test_unexpected_exit_code_is_error(self) -> None:
        from sunaba.edit_verify import _run_clippy_verify

        container = self._make_container(2, "", "internal cargo error")
        result = _run_clippy_verify(container, ".")
        assert result.status == "error"

    def test_exit_101_with_no_diagnostics_is_error_not_green(self) -> None:
        """Exit 101 with an empty JSON stream is an infrastructure
        failure (no manifest reachable, broken lockfile, ...), never a
        clean run.  Reporting it ok would be the eslint-exit-2 false
        green (#740) again.
        """
        from sunaba.edit_verify import _run_clippy_verify

        container = self._make_container(
            101, "", "error: failed to parse lock file\n"
        )
        result = _run_clippy_verify(container, ".")
        assert result.status == "error"
        assert "failed to parse lock file" in (result.detail or "")

    def test_no_manifest_anywhere_is_error_not_green(self) -> None:
        """When no Cargo.toml exists under the working directory (and
        cargo itself is present), the runner must refuse to call this a
        clean lint run.
        """
        from sunaba.edit_verify import _run_clippy_verify
        from tests.conftest import _make_docker_compliant_container

        container = _make_docker_compliant_container([
            (1, b"", b""),   # direct-path probes: nothing found
            (1, b"", b""),   # shallow find: nothing found
            (0, b"/usr/bin/cargo\n", b""),  # cargo itself exists
        ])
        result = _run_clippy_verify(container, ".")
        assert result.status == "error"
        assert "no Cargo.toml" in (result.detail or "")

    def test_manifest_in_subdirectory_is_passed_to_cargo(self) -> None:
        """The first real consumer keeps its workspace manifest in a
        subdirectory (prototypes/Cargo.toml); the runner must find it
        and name it via --manifest-path instead of running cargo at the
        git root and reporting the resulting failure as green.
        """
        from sunaba.edit_verify import _run_clippy_verify
        from tests.conftest import _make_docker_compliant_container

        raw = _load_fixture("clippy_warning_only.ndjson")
        container = _make_docker_compliant_container([
            (1, b"", b""),                          # direct-path probes fail
            (0, b"./prototypes/Cargo.toml\n", b""),  # shallow find locates it
            (0, raw.encode("utf-8"), b""),           # clippy run
        ])
        result = _run_clippy_verify(container, ".")
        assert result.status == "findings"
        cargo_call = container.exec_run.call_args_list[-1]
        cmd = cargo_call.args[0] if cargo_call.args else cargo_call.kwargs.get("cmd")
        assert "--manifest-path ./prototypes/Cargo.toml" in cmd[-1]


class TestRunCargoTestVerifyEnvelope:
    """_run_cargo_test_verify's exit-code handling with a mocked container."""

    def _make_container(self, ec: int, stdout: str = "", stderr: str = ""):
        from tests.conftest import _make_docker_compliant_container

        # First scripted response answers the manifest-resolution probe.
        return _make_docker_compliant_container([
            (0, b"Cargo.toml\\n", b""),
            (ec, stdout.encode("utf-8"), stderr.encode("utf-8")),
        ])

    def test_all_pass(self) -> None:
        from sunaba.edit_verify import _run_cargo_test_verify

        raw = _load_fixture("cargo_test_all_pass.txt")
        container = self._make_container(0, raw)

        result = _run_cargo_test_verify(container, ".")
        assert result.status == "ok"
        d = json.loads(result.detail)
        assert d["passed"] == 3

    def test_failure_reported_as_findings(self) -> None:
        from sunaba.edit_verify import _run_cargo_test_verify

        raw = _load_fixture("cargo_test_with_failure.txt")
        container = self._make_container(101, raw)

        result = _run_cargo_test_verify(container, ".")
        assert result.status == "findings"
        d = json.loads(result.detail)
        assert d["failed"] == 1
        assert d["failures"][0]["test"] == "tests::it_fails"


# ===================================================================
# Dispatch table wiring
# ===================================================================


class TestRustDispatchTable:
    """_DISPATCH["rust"] is wired for lint / type / test, mirroring the
    shape every other language's entry has."""

    def test_rust_in_dispatch_table(self) -> None:
        from sunaba.edit_verify import _DISPATCH

        assert "rust" in _DISPATCH
        assert set(_DISPATCH["rust"]) == {"lint", "type", "test"}

    def test_rust_lint_and_test_are_callables(self) -> None:
        from sunaba.edit_verify import _DISPATCH

        assert callable(_DISPATCH["rust"]["lint"])
        assert callable(_DISPATCH["rust"]["test"])

    def test_rust_type_is_a_dedicated_callable_not_none(self) -> None:
        """Unlike go/js ("type": None -> generic dispatch-layer fallback
        message), rust's type entry is a real function so its skip
        reason is specific and visible rather than a one-size-fits-all
        "language has no type layer" string.
        """
        from sunaba.edit_verify import _DISPATCH

        assert _DISPATCH["rust"]["type"] is not None
        assert callable(_DISPATCH["rust"]["type"])


# ===================================================================
# Gate wiring: lint routes to clippy; type stays "skipped", not
# "not_available", so lint_type_incomplete is never set purely because
# Rust has no standalone type checker.
# ===================================================================


class TestDeepMarkerScan:
    """A manifest 2 levels down must still be detected (measured E2E gap).

    sagasu keeps its Cargo workspace at ``prototypes/Cargo.toml``; the
    depth-1 scan finds nothing there, and before the second-chance deep
    scan existed the whole verify call reported "no languages detected"
    and passed the gate silently -- on a real repository.
    """

    def _container(self, find_output: bytes):
        from tests.conftest import _make_docker_compliant_container

        return _make_docker_compliant_container([
            (0, b"", b""),          # depth-1 marker find: nothing
            (0, find_output, b""),  # deep scan
        ])

    def test_manifest_two_levels_down_is_detected(self) -> None:
        from sunaba.edit_verify.detect import detect_languages

        container = self._container(b"./prototypes/Cargo.toml\n")
        result = detect_languages(container, ".")
        assert result.languages == {"rust"}
        assert result.scope["rust"] == "./prototypes"

    def test_workspace_root_wins_over_member_crates(self) -> None:
        from sunaba.edit_verify.detect import detect_languages

        container = self._container(
            b"./prototypes/proto-crawl/Cargo.toml\n./prototypes/Cargo.toml\n"
        )
        result = detect_languages(container, ".")
        assert result.scope["rust"] == "./prototypes"

    def test_still_unknown_when_deep_scan_finds_nothing(self) -> None:
        from sunaba.edit_verify.detect import detect_languages

        container = self._container(b"")
        result = detect_languages(container, ".")
        assert result.languages == set()
        assert result.reason is not None


class TestGateRustBranches:
    def test_gate_lint_runner_routes_rust_to_clippy(self) -> None:
        from sunaba.edit_verify import _gate_lint_runner
        from tests.conftest import _make_docker_compliant_container

        raw = _load_fixture("clippy_warning_only.ndjson")
        container = _make_docker_compliant_container([
            (0, b"Cargo.toml\n", b""),
            (0, raw.encode("utf-8"), b""),
        ])

        result = _gate_lint_runner(container, ".", "rust", None)
        assert result.tool == "clippy"
        assert result.status == "findings"

    def test_gate_type_runner_routes_rust_to_dedicated_skip(self) -> None:
        from sunaba.edit_verify import _gate_type_runner

        mock_container = MagicMock()
        result = _gate_type_runner(mock_container, ".", "rust", None)
        assert result.status == "skipped"
        assert result.tool == "rust-type"
        assert "clippy" in result.detail or "rustc" in result.detail
        mock_container.exec_run.assert_not_called()

    def test_run_lint_type_gate_rust_only_project_not_incomplete(self) -> None:
        """A pure-Rust project where clippy is present and clean must not
        report lint_type_incomplete -- the type layer's deliberate skip
        must not be conflated with "a tool was missing" (incomplete=True
        is reserved for not_available/error, and the rust type layer
        never returns either).
        """
        from unittest.mock import patch

        from sunaba.edit_verify import DetectionResult, run_lint_type_gate
        from tests.conftest import _make_docker_compliant_container

        container = _make_docker_compliant_container([
            (0, b"Cargo.toml\n", b""),
            (0, b"", b""),
        ])

        with patch(
            "sunaba.edit_verify.gate.detect_languages",
            return_value=DetectionResult(languages={"rust"}, scope={"rust": "."}, reason=None),
        ):
            result = run_lint_type_gate(container, ".", working_dir=None)

        assert result["incomplete"] is False
        assert result["gate_passed"] is True

    def test_run_lint_type_gate_missing_cargo_is_incomplete(self) -> None:
        """By contrast, a genuinely missing toolchain (clippy exit 127)
        for the *lint* layer does set incomplete=True -- the case
        lint_type_incomplete exists to flag.
        """
        from unittest.mock import patch

        from sunaba.edit_verify import DetectionResult, run_lint_type_gate
        from tests.conftest import _make_docker_compliant_container

        container = _make_docker_compliant_container([
            (0, b"Cargo.toml\n", b""),
            (127, b"", b"cargo: not found\n"),
        ])

        with patch(
            "sunaba.edit_verify.gate.detect_languages",
            return_value=DetectionResult(languages={"rust"}, scope={"rust": "."}, reason=None),
        ):
            result = run_lint_type_gate(container, ".", working_dir=None)

        assert result["incomplete"] is True
        # A missing tool does not fail the gate outright (Issue #293
        # convention -- environment gap, not a code defect).
        assert result["gate_passed"] is True
