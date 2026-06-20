"""Structured test report adapters for pytest, jest, and go test.

Provides a common schema and framework-specific adapters that convert
raw test runner output into a structured JSON format suitable for AI
consumption (minimal, frame-pruned, consistent).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Common schema
# ---------------------------------------------------------------------------


@dataclass
class TestFailure:
    """A single test failure with location information."""

    test: str
    error: str
    file: str
    line: int


@dataclass
class TestReport:
    """Structured test result."""

    status: str  # "ok" | "failed"
    duration: float  # seconds
    passed: int
    failed: int = 0
    failures: list[TestFailure] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the common JSON schema.

        Success case returns minimal dict (status, passed, duration).
        """
        base: dict[str, Any] = {
            "status": self.status,
            "duration": self.duration,
            "passed": self.passed,
        }
        if self.status == "failed" and self.failures:
            base["failed"] = self.failed
            base["failures"] = [asdict(f) for f in self.failures]
        return base


def export_test_report(report: TestReport) -> str:
    """Serialize a TestReport to JSON string."""
    return json.dumps(report.to_dict(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Library frame pruning
# ---------------------------------------------------------------------------

# Patterns that identify non-user (library/framework) stack frames.
# These are matched against individual lines of a traceback.
_LIBRARY_FRAME_PATTERNS: list[re.Pattern] = [
    # Python site-packages (pip-installed libraries)
    re.compile(r"site-packages/"),
    # Debian/Ubuntu system packages
    re.compile(r"dist-packages/"),
    # Standard library paths (e.g. /usr/lib/python3.12/)
    re.compile(r"lib/python"),
    # pytest internals (both plugin and runner modules)
    re.compile(r"pytest/"),
    re.compile(r"_pytest/"),
    # Frozen/stdlib internals (Python 3.x)
    re.compile(r"<frozen "),
    # NumPy internals
    re.compile(r"<__array_function__"),
]


def _is_library_frame(frame: str) -> bool:
    """Return True if *frame* looks like a library/framework frame."""
    return any(p.search(frame) for p in _LIBRARY_FRAME_PATTERNS)


def prune_library_frames(
    traceback: str,
    *,
    max_frames: int = 5,
) -> str:
    """Remove library/framework frames from a traceback string.

    Keeps only user-code frames.  Limits the output to *max_frames*
    lines (default: 5).  If no user frames remain, returns the last
    *max_frames* lines of the original traceback as a fallback (since
    the last frames typically contain the actual error message).

    Parameters
    ----------
    traceback:
        The raw traceback string (multi-line).
    max_frames:
        Maximum number of lines to return (default 5).  Can be
        overridden by callers that need more or less context.
    """
    lines = traceback.split("\n")
    user_lines = [line for line in lines if not _is_library_frame(line)]

    if not user_lines:
        # Fallback: keep the last N lines (often the actual error).
        user_lines = lines[-max_frames:] if len(lines) > max_frames else lines

    return "\n".join(user_lines[:max_frames])


# ---------------------------------------------------------------------------
# Pytest adapter
# ---------------------------------------------------------------------------


@dataclass
class PytestAdapter:
    """Adapt **pytest-json-report** output (``pytest --json-report``).

    Expects the JSON report dict as produced by the plugin, with keys:
    ``summary``, ``tests``, ``duration``, etc.
    """

    @staticmethod
    def parse(report: dict[str, Any]) -> TestReport:
        """Parse a pytest-json-report dict into a TestReport."""
        summary = report.get("summary", {})
        duration = float(report.get("duration", 0.0))
        total = int(summary.get("total", 0))
        passed = int(summary.get("passed", 0))
        failed = int(summary.get("failed", 0))
        # Fallback for reports that lack "passed" count but have total/failed.
        passed = total - failed if passed == 0 and total > 0 else passed

        failures_list: list[TestFailure] = []
        tests = report.get("tests", [])
        for t in tests:
            outcome = t.get("outcome", "")
            if outcome in ("failed", "error"):
                call = t.get("call", {})
                # Crash details may be None if no traceback was captured.
                crashdetails = call.get("crash", {}) or {}
                traceback_str = crashdetails.get("traceback", "")
                pruned = prune_library_frames(traceback_str)

                failures_list.append(
                    TestFailure(
                        test=t.get("nodeid", t.get("name", "unknown")),
                        # Use pruned traceback if available, else fall back to
                        # the call's message (e.g. "AssertionError").
                        error=pruned if pruned else call.get("message", "unknown"),
                        file=t.get("file", ""),
                        line=int(t.get("line", 0)),
                    )
                )

        status = "failed" if failed > 0 else "ok"
        return TestReport(
            status=status,
            duration=duration,
            passed=passed,
            failed=failed,
            failures=failures_list if failures_list else None,
        )

    @classmethod
    def parse_json(cls, raw: str) -> TestReport:
        """Parse a raw JSON string (from pytest --json-report) into a TestReport."""
        data = json.loads(raw)
        return cls.parse(data)


# ---------------------------------------------------------------------------
# Jest adapter
# ---------------------------------------------------------------------------


@dataclass
class JestAdapter:
    """Adapt **jest --json** output.

    Expects the JSON object produced by ``jest --json`` with keys:
    ``numPassedTests``, ``numFailedTests``, ``testResults``, etc.
    """

    @staticmethod
    def parse(report: dict[str, Any]) -> TestReport:
        """Parse a jest --json dict into a TestReport."""
        duration = float(report.get("numRuntimeMs", 0)) / 1000.0
        passed = int(report.get("numPassedTests", 0))
        failed = int(report.get("numFailedTests", 0))

        failures_list: list[TestFailure] = []
        test_results = report.get("testResults", [])
        for suite in test_results:
            assertion_results = suite.get("assertionResults", [])
            for ar in assertion_results:
                if ar.get("status") in ("failed",):
                    failure_messages = ar.get("failureMessages", [])
                    error_text = ""
                    file = ""
                    line = 0
                    if failure_messages:
                        combined = "\n".join(failure_messages)
                        error_text = prune_library_frames(combined)
                        # Extract file:line from Jest stack traces.
                        # Jest error messages typically look like:
                        #   expect(received).toBe(expected)
                        #   at Object.<anonymous> (path/to/file.js:42:12)
                        #                      ^^^^^^^^^^^^^^^^^^^^
                        # The regex captures the file path (js/ts/jsx/tsx)
                        # and the first line number.
                        match = re.search(
                            r"\s+at\s.+?[ (]([^:(]+?\.(?:js|ts|jsx|tsx)):(\d+)",
                            failure_messages[0],
                        )
                        if match:
                            file = match.group(1)
                            line = int(match.group(2))

                    if not error_text:
                        error_text = (
                            f"Test failed with no failure messages; "
                            f"status={ar.get('status', 'unknown')}"
                        )

                    failures_list.append(
                        TestFailure(
                            test=ar.get("fullName", ar.get("title", "unknown")),
                            error=error_text,
                            file=file,
                            line=line,
                        )
                    )

        status = "failed" if failed > 0 else "ok"
        return TestReport(
            status=status,
            duration=duration,
            passed=passed,
            failed=failed,
            failures=failures_list if failures_list else None,
        )

    @classmethod
    def parse_json(cls, raw: str) -> TestReport:
        """Parse a raw JSON string (from jest --json) into a TestReport."""
        data = json.loads(raw)
        return cls.parse(data)


# ---------------------------------------------------------------------------
# Go test adapter
# ---------------------------------------------------------------------------


@dataclass
class GoTestAdapter:
    """Adapt **go test -json** stream output.

    ``go test -json`` produces a newline-delimited JSON stream (NDJSON),
    where each line is a JSON event (``Action``, ``Package``, ``Test``,
    ``Elapsed``, ``Output``).
    """

    @staticmethod
    def parse(events: list[dict[str, Any]]) -> TestReport:
        """Parse a list of go test -json event dicts into a TestReport."""
        # Collect per-test results (status + output lines).
        tests: dict[str, dict[str, Any]] = {}
        failures_list: list[TestFailure] = []
        passed_count = 0
        failed_count = 0
        duration = 0.0

        for event in events:
            action = event.get("Action", "")
            test_name = event.get("Test", "")

            # Individual test pass/fail events
            if action == "pass" and test_name:
                tests.setdefault(test_name, {})["status"] = "pass"
            elif action == "fail" and test_name:
                tests.setdefault(test_name, {})["status"] = "fail"
            # Test-level output (may contain error details)
            elif action == "output" and test_name:
                entry = tests.setdefault(test_name, {})
                entry.setdefault("output", [])
                entry["output"].append(event.get("Output", ""))
            # Package-level output – try to extract elapsed time.
            # Format: "ok   \tgithub.com/user/project\t0.523s\n"
            elif action == "output" and not test_name:
                text = event.get("Output", "")
                m = re.search(r"ok\s+\S+\s+([\d]+\.?[\d]*)s", text)
                if m:
                    duration = max(duration, float(m.group(1)))

        # Build failures list from collected data.
        for tname, tdata in tests.items():
            if tdata.get("status") == "fail":
                failed_count += 1
                output_lines = tdata.get("output", [])
                combined = "".join(output_lines)
                pruned = prune_library_frames(combined)

                # Extract file:line from go test output.
                # Go test outputs errors like:
                #   /path/to/file_test.go:42: expected 2, got 1
                file = ""
                line = 0
                for line_text in output_lines:
                    m = re.search(r"(/\S+\.go):(\d+):", line_text)
                    if m:
                        file = m.group(1)
                        line = int(m.group(2))
                        break

                failures_list.append(
                    TestFailure(
                        test=tname,
                        error=pruned if pruned else "unknown",
                        file=file,
                        line=line,
                    )
                )
            elif tdata.get("status") == "pass":
                passed_count += 1

        # Final event may carry Elapsed for overall duration.
        if events:
            last = events[-1]
            elapsed = last.get("Elapsed", None)
            if elapsed is not None:
                duration = max(duration, float(elapsed))

        status = "failed" if failed_count > 0 else "ok"
        return TestReport(
            status=status,
            duration=duration,
            passed=passed_count,
            failed=failed_count,
            failures=failures_list if failures_list else None,
        )

    @classmethod
    def parse_json(cls, raw: str) -> TestReport:
        """Parse a raw NDJSON string (from go test -json) into a TestReport."""
        events: list[dict[str, Any]] = []
        for line in raw.strip().split("\n"):
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return cls.parse(events)


# ---------------------------------------------------------------------------
# Convenience dispatcher
# ---------------------------------------------------------------------------


def parse_test_report(framework: str, raw_output: str) -> str:
    """Parse raw test output for *framework* and return JSON string.

    Supported frameworks: ``pytest``, ``jest``, ``go-test``.
    """
    adapter_map: dict[str, Any] = {
        "pytest": PytestAdapter.parse_json,
        "jest": JestAdapter.parse_json,
        "go-test": GoTestAdapter.parse_json,
    }
    parser = adapter_map.get(framework)
    if parser is None:
        raise ValueError(f"Unsupported test framework: {framework!r}")
    report = parser(raw_output)
    return export_test_report(report)
