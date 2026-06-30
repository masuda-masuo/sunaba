"""Structured test report adapters for pytest, jest, and go test.

Provides a common schema and framework-specific adapters that convert
raw test runner output into a structured JSON format suitable for AI
consumption (minimal, frame-pruned, consistent).
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import asdict, dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared test-runner helpers
# ---------------------------------------------------------------------------

#: Marker used by :func:`_build_pytest_cmd` to separate JSON report from
#: raw pytest output in the combined stdout stream.
PYTEST_RAW_MARKER = "---PYTEST-RAW---"
_PYTEST_RAW_LINES = 40


def build_pytest_cmd(
    json_file: str,
    raw_file: str,
    filter_args: str,
    path: str,
    sandbox_env: str = "",
) -> str:
    """Build a pytest --json-report command that emits JSON + raw tail.

    The command writes JSON report to *json_file*, captures full raw
    output to *raw_file*, then prints the JSON followed by
    :data:`PYTEST_RAW_MARKER` and the last :data:`_PYTEST_RAW_LINES`
    lines of raw output.  Both temp files are cleaned up on exit.

    Callers should split the result with :func:`split_pytest_output`.
    """
    quoted_path = shlex.quote(path)
    return (
        f"{sandbox_env}python3 -m pytest --json-report "
        f"--json-report-file={json_file} -q{filter_args} "
        f"{quoted_path} >{raw_file} 2>&1; "
        f"_ec=$?; cat {json_file} 2>/dev/null; "
        f"echo '{PYTEST_RAW_MARKER}'; tail -n {_PYTEST_RAW_LINES} {raw_file} 2>/dev/null; "
        f"rm -f {json_file} {raw_file}; exit $_ec"
    )


def split_pytest_output(stdout_text: str) -> tuple[str, str]:
    """Split combined stdout at :data:`PYTEST_RAW_MARKER`.

    Returns ``(json_part, raw_tail)``.  Either may be empty.
    """
    parts = stdout_text.split(PYTEST_RAW_MARKER, 1)
    json_part = parts[0].strip() if parts else ""
    raw_tail = parts[1].strip() if len(parts) > 1 else ""
    return json_part, raw_tail


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
        errors = int(summary.get("errors", 0))
        # Fallback for reports that lack "passed" count but have total/failed.
        passed = total - failed - errors if passed == 0 and total > 0 else passed
        failed_total = failed + errors

        failures_list: list[TestFailure] = []
        tests = report.get("tests", [])
        for t in tests:
            outcome = t.get("outcome", "")
            if outcome not in ("failed", "error"):
                continue
            # Search for the failing stage: call → setup → teardown
            stage: dict[str, Any] = {}
            for name in ("call", "setup", "teardown"):
                s = t.get(name) or {}
                if s.get("outcome") in ("failed", "error") or s.get("crash"):
                    stage = s
                    break
            crash = stage.get("crash") or {}
            longrepr = stage.get("longrepr", "") or ""
            error_text = prune_library_frames(longrepr) if longrepr else ""
            if not error_text:
                error_text = crash.get("message", "unknown")
            nodeid = t.get("nodeid", t.get("name", "unknown"))

            failures_list.append(
                TestFailure(
                    test=nodeid,
                    error=error_text,
                    file=crash.get("path", "") or nodeid.split("::", 1)[0],
                    # or 0 guards against crash.lineno being None (which int() rejects).
                    line=int(crash.get("lineno", t.get("lineno", 0)) or 0),
                )
            )

        status = "failed" if failed_total > 0 else "ok"
        return TestReport(
            status=status,
            duration=duration,
            passed=passed,
            failed=failed_total,
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
        # Compute duration from startTime and testResults endTime/startTime.
        # numRuntimeMs is NOT a real key in jest --json output.
        # If startTime is 0 (older jest versions that don't emit it),
        # duration stays 0.0 as a fallback.
        start_time = float(report.get("startTime", 0))
        duration = 0.0
        if start_time > 0:
            latest_end = start_time
            for suite in report.get("testResults", []):
                suite_start = float(suite.get("startTime", 0)) or start_time
                suite_end = float(suite.get("endTime", 0)) or suite_start
                if suite_end > latest_end:
                    latest_end = suite_end
            duration = (latest_end - start_time) / 1000.0
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
        package_failed = False
        package_output: list[str] = []

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
                # May include passing tests' output too, but build failures
                # typically produce short compile-error text, and
                # prune_library_frames caps at 5 lines, so it's acceptable.
                package_output.append(text)
            # Package-level fail (build/compile error, no individual test fails)
            elif action == "fail" and not test_name:
                package_failed = True

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

        # Package-level fail (e.g. build/compile error) with no individual fails
        if package_failed and failed_count == 0:
            failed_count = 1
            combined_output = "".join(package_output)
            pruned = prune_library_frames(combined_output)
            failures_list.append(
                TestFailure(
                    test="(package)",
                    error=pruned if pruned else "build failed",
                    file="",
                    line=0,
                )
            )

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

