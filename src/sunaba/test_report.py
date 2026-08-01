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
    path: str | list[str],
    sandbox_env: str = "",
) -> str:
    """Build a pytest --json-report command that emits JSON + raw tail.

    The command writes JSON report to *json_file*, captures full raw
    output to *raw_file*, then prints the JSON followed by
    :data:`PYTEST_RAW_MARKER` and the last :data:`_PYTEST_RAW_LINES`
    lines of raw output.  Both temp files are cleaned up on exit.

    *path* is a single file/dir path, or a list of paths (each quoted
    individually) so an affected-test selection can be passed as
    positional pytest arguments (Issue #781).  Selected tests are always
    passed positionally -- never via ``-k``, where a file path matches
    nothing.

    Runs tests in parallel via ``-n auto`` (capped at CPU count) for
    faster verification (Issue #590).  The sandbox image's ``pids.max``
    is high enough that Python xdist workers do not exhaust it.

    Callers should split the result with :func:`split_pytest_output`.
    """
    if isinstance(path, list):
        quoted_path = " ".join(shlex.quote(p) for p in path)
    else:
        quoted_path = shlex.quote(path)
    return (
        f"{sandbox_env}python3 -m pytest --json-report "
        f"--json-report-file={json_file} -n auto -q{filter_args} "
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
    """Structured test result.

    Framework-specific runners may set additional fields (*total*,
    *skipped*, *todo*) that are not part of the common schema;
    :meth:`to_dict` includes them only when they carry meaningful
    values (e.g. ``total`` when not None, ``skipped``/``todo`` when
    > 0).
    """

    status: str  # "ok" | "failed"
    duration: float  # seconds
    passed: int
    failed: int = 0
    failures: list[TestFailure] | None = None
    # Extra fields used by the TAP adapter (node --test)
    total: int | None = None  # total test count (None = not reported)
    skipped: int = 0
    todo: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the common JSON schema.

        Base fields (status, duration, passed) are always present.
        ``failed`` and ``failures`` are included only for failures.
        ``total``, ``skipped``, ``todo`` are included only when they
        carry meaningful values.
        """
        base: dict[str, Any] = {
            "status": self.status,
            "duration": self.duration,
            "passed": self.passed,
        }
        if self.total is not None:
            base["total"] = self.total
        if self.skipped:
            base["skipped"] = self.skipped
        if self.todo:
            base["todo"] = self.todo
        if self.failed:
            base["failed"] = self.failed
        if self.failures:
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
# Rust (cargo test) adapter
# ---------------------------------------------------------------------------


@dataclass
class RustTestAdapter:
    """Adapt **cargo test** plain-text output.

    Unlike ``go test -json``, stable cargo has no structured test-report
    format -- ``cargo test``'s own ``--format json`` is gated behind
    ``-Z unstable-options`` (nightly only), so this parses the same
    human-readable text a developer would see.  ``cargo test`` runs one
    test binary per target (lib unit tests, each integration test file,
    doctests, ...) in a single invocation, printing one independent
    block per binary::

        running 3 tests
        test tests::it_adds_two ... ok
        test tests::it_fails ... FAILED
        test tests::it_is_ignored ... ignored

        failures:

        ---- tests::it_fails stdout ----
        thread 'tests::it_fails' panicked at src/lib.rs:15:5:
        assertion `left == right` failed
          left: 4
         right: 5
        note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

        failures:
            tests::it_fails

        test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

    This adapter sums the ``test result: ...`` summary line's counts
    across every block found in *raw*, and pulls failure detail (file
    and line) out of the ``thread '<name>' panicked at <file>:<line>:<col>:``
    line inside each failing test's ``---- <name> stdout ----`` section
    -- the same "grep the location out of the panic/output text" idea
    :class:`GoTestAdapter` uses for ``file_test.go:42:``.

    Raises :class:`ValueError` when no ``test result: ...`` summary line
    is found, so callers can distinguish "genuinely unparseable output"
    from a clean parse (mirrors :class:`TapAdapter`).
    """

    #: ``test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s``
    _RESULT_RE = re.compile(
        r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; "
        r"(\d+) measured; (\d+) filtered out(?:; finished in ([\d.]+)s)?",
        re.MULTILINE,
    )
    #: ``test tests::it_fails ... FAILED`` -- the name may contain spaces
    #: (doctest names look like ``src/lib.rs - foo (line 3)``), so the
    #: name is captured non-greedily up to the literal `` ... ``.
    _TEST_LINE_RE = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored)$", re.MULTILINE)
    #: ``---- tests::it_fails stdout ----`` section header.
    _STDOUT_HEADER_RE = re.compile(r"^---- (.+?) stdout ----$", re.MULTILINE)
    #: ``thread 'tests::it_fails' panicked at src/lib.rs:15:5:``
    _PANIC_LOC_RE = re.compile(r"panicked at ([^\s:][^:]*):(\d+):(\d+)")

    @classmethod
    def parse(cls, raw: str) -> TestReport:
        """Parse cargo test's plain-text output into a TestReport."""
        results = cls._RESULT_RE.findall(raw)
        if not results:
            raise ValueError("cargo test 'test result: ...' summary line not found in output")

        passed = failed = ignored = 0
        duration = 0.0
        for _status, p, f, i, _measured, _filtered, dur in results:
            passed += int(p)
            failed += int(f)
            ignored += int(i)
            if dur:
                duration += float(dur)

        # Failing test names, in the order cargo printed them, deduped
        # (a name could in principle repeat across binaries).
        failed_names: list[str] = []
        seen: set[str] = set()
        for name, status in cls._TEST_LINE_RE.findall(raw):
            if status == "FAILED" and name not in seen:
                seen.add(name)
                failed_names.append(name)

        # Per-test stdout sections: name -> body text, so each failure's
        # panic location/message can be pulled out individually.
        headers = list(cls._STDOUT_HEADER_RE.finditer(raw))
        sections: dict[str, str] = {}
        for idx, m in enumerate(headers):
            start = m.end()
            end = headers[idx + 1].start() if idx + 1 < len(headers) else len(raw)
            sections[m.group(1)] = raw[start:end]

        failures_list: list[TestFailure] = []
        for name in failed_names:
            body = sections.get(name, "")
            file = ""
            line = 0
            loc_m = cls._PANIC_LOC_RE.search(body)
            if loc_m:
                file = loc_m.group(1)
                line = int(loc_m.group(2))
            pruned = prune_library_frames(body) if body else "unknown"
            failures_list.append(
                TestFailure(test=name, error=pruned, file=file, line=line)
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
        """Parse raw cargo test text output into a TestReport.

        cargo test's stable output is not JSON; ``parse_json`` exists for
        interface consistency with the other adapter classes (mirrors
        :meth:`TapAdapter.parse_json`).
        """
        return cls.parse(raw)


# ---------------------------------------------------------------------------
# TAP (node --test) adapter
# ---------------------------------------------------------------------------


@dataclass
class TapAdapter:
    """Adapt **TAP v13** output produced by ``node --test``.

    ``node --test`` outputs TAP version 13 with per-test diagnostic
    YAML blocks and a trailing summary block that looks like::

        1..N
        # tests N
        # suites N
        # pass N
        # fail N
        # cancelled N
        # skipped N
        # todo N
        # duration_ms NNN.NNN

    This adapter extracts the summary block and maps it to a
    :class:`TestReport`, and additionally collects per-test failures
    from ``not ok`` lines (test names plus a short excerpt of the
    YAML diagnostic block that follows them) into
    ``TestReport.failures`` — so a failing run names each failing
    test even when the failure scrolls out of the raw output tail
    (Issue #804).  Summary counts still come from the summary block.

    Raises :class:`ValueError` when no parseable summary block is
    found, allowing callers to distinguish ``not_available`` output
    from genuinely unparseable content.
    """

    _SUMMARY_RE = re.compile(
        r"^#\s+(tests|suites|pass|fail|cancelled|skipped|todo|duration_ms)\s+([\d.]+)",
    )
    #: ``not ok <N> - <name>`` test-point lines, at any indentation depth
    #: (node --test indents nested subtests).  At least one of the test
    #: number or the ``- `` separator is required, so console output that
    #: merely echoes a bare ``not ok <text>`` line is not misread.  Lines
    #: inside YAML diagnostic blocks are excluded separately by
    #: :meth:`_collect_failures` -- node --test passes test console output
    #: through to the TAP stream, so an echoed ``not ok <N> - <text>`` line
    #: inside an ``error:`` block would match even this tightened pattern.
    _NOT_OK_RE = re.compile(
        r"^not ok\s+(?:(?:\d+\s+)?-\s+|(?:\d+\s+))(?P<name>.*)$"
    )
    #: TAP directives that mark a test point as *not* a failure: ``# SKIP``
    #: (test not run) and ``# TODO`` (known/expected failure).
    _DIRECTIVE_RE = re.compile(r"#\s*(SKIP|TODO)\b", re.IGNORECASE)
    #: Maximum number of per-test failures to collect (Issue #804).  When
    #: more failing test points exist, a synthetic entry reports the rest.
    _MAX_FAILURES = 50

    @staticmethod
    def _collect_failures(lines: list[str]) -> list[TestFailure]:
        """Collect failing test points from ``not ok`` lines.

        Scans the whole output in stream order (test points are emitted
        before the trailing summary), matching ``not ok`` lines at any
        indentation depth.  ``# SKIP`` / ``# TODO`` test points are not
        failures per TAP semantics and are excluded.

        Lines inside a YAML diagnostic block (the indented region opened
        by a ``---`` line right after a test point) are skipped entirely:
        node --test passes test console output through to the TAP stream,
        so ``not ok``-shaped text echoed inside an ``error:`` block must
        not be read as a test point (Issue #804 review finding 1).  The
        block ends at a ``...`` line at the ``---`` opener's indentation
        (TAP 13 semantics): deeper-indented ``...`` lines -- such as the
        closers of an inner TAP fragment embedded in an ``error:``
        message -- are content, not the terminator.  An unterminated
        block (no ``...`` at the opener's indent) is closed by the first
        non-indented line, so real test points after it are still
        collected.

        The list is capped at :data:`_MAX_FAILURES` entries; when capped,
        a final synthetic entry reports how many more were not shown.
        """
        failures_list: list[TestFailure] = []
        overflow = 0
        in_yaml_block = False
        block_indent = 0
        prev_test_point = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if in_yaml_block:
                # Content lines of a YAML diagnostic block are arbitrary
                # test output, not TAP test points.  TAP 13 pairs the
                # closing ``...`` with the ``---`` opener's indentation,
                # so only a ``...`` at exactly that indent ends the
                # block; a deeper-indented ``...`` (the closer of an
                # inner TAP fragment embedded in the error text) is
                # content.  A non-indented non-empty line closes an
                # unterminated block so later real test points are not
                # silently dropped.
                if stripped == "..." and indent == block_indent:
                    in_yaml_block = False
                elif stripped and not line[:1].isspace():
                    in_yaml_block = False
                else:
                    continue
            if stripped == "---" and line[:1].isspace() and prev_test_point:
                # The indented ``---`` right after a test point opens the
                # YAML diagnostic block; skip its content until the
                # matching ``...``.
                in_yaml_block = True
                block_indent = indent
                continue
            prev_test_point = (
                stripped.startswith("not ok")
                or stripped == "ok"
                or stripped.startswith("ok ")
            )
            m = TapAdapter._NOT_OK_RE.match(stripped)
            if not m:
                continue
            if TapAdapter._DIRECTIVE_RE.search(stripped):
                continue
            # Trailing TAP directives/comments (`` # ...``) are not part
            # of the test name.
            name = re.sub(r"\s+#.*$", "", m.group("name")).strip()
            if not name:
                continue
            if len(failures_list) < TapAdapter._MAX_FAILURES:
                failures_list.append(
                    TestFailure(
                        test=name,
                        error=TapAdapter._error_from_block(lines, i + 1),
                        file="",
                        line=0,
                    )
                )
            else:
                overflow += 1
        if overflow:
            failures_list.append(
                TestFailure(
                    test=f"... ({overflow} more not shown)",
                    error="",
                    file="",
                    line=0,
                )
            )
        return failures_list

    @staticmethod
    def _error_from_block(lines: list[str], start: int) -> str:
        """Extract up to 5 lines of the YAML block after a test point.

        The block runs from the ``not ok`` line's next line until the
        indented ``...`` terminator or the first non-indented line.  The
        ``error:`` key's value (block scalar or inline) is preferred;
        without an ``error:`` key, the first lines of the block itself
        are used.  Returns ``""`` when no block follows.
        """
        block: list[str] = []
        for line in lines[start:]:
            if line.strip() == "":
                block.append(line)
                continue
            if not line[:1].isspace() or line.strip() == "...":
                break
            block.append(line)

        err_idx = -1
        err_indent = 0
        for k, line in enumerate(block):
            s = line.strip()
            if s.startswith("error:"):
                err_idx = k
                err_indent = len(line) - len(line.lstrip())
                break

        if err_idx >= 0:
            after = block[err_idx].strip()[len("error:"):].strip()
            if after and not re.fullmatch(r"[|>][-+]?", after):
                # Inline scalar: ``error: <text>``.
                return after
            # Block scalar (``error: |-`` etc.): the following lines that
            # are indented more deeply than the key itself.
            content: list[str] = []
            for line in block[err_idx + 1:]:
                if line.strip() == "":
                    content.append("")
                    continue
                if len(line) - len(line.lstrip()) > err_indent:
                    content.append(line.strip())
                else:
                    break
            return "\n".join(content[:5]).strip()

        # No ``error:`` key: fall back to the first lines of the block
        # (excluding the ``---`` opener) so some diagnostic text remains.
        fallback = [
            ln.strip()
            for ln in block
            if ln.strip() and ln.strip() not in ("---", "...")
        ]
        return "\n".join(fallback[:5])

    @staticmethod
    def parse(raw: str) -> TestReport:
        """Parse TAP v13 output into a TestReport.

        Parameters
        ----------
        raw:
            The full stdout/stderr from ``node --test`` (TAP v13).

        Returns
        -------
        TestReport
            With ``total`` (test count), ``passed``, ``failed``,
            ``skipped``, ``todo`` and ``duration`` (converted from
            milliseconds to seconds) populated.  Failing runs also
            carry ``failures``: one :class:`TestFailure` per ``not ok``
            test point, in stream order, with the test name and a short
            excerpt of its YAML diagnostic block (``file``/``line`` are
            always ``""``/``0`` — TAP has no location info).  Passing
            runs leave ``failures`` absent.

        Raises
        ------
        ValueError
            When the TAP plan line (``1..N``) or the trailing summary
            block cannot be found.
        """
        lines = raw.strip().splitlines()

        # -- Find the last ``1..N`` plan line ---------------------------
        plan_idx = -1
        for i in range(len(lines) - 1, -1, -1):
            stripped = lines[i].strip()
            if stripped.startswith("1.."):
                plan_idx = i
                break

        if plan_idx == -1:
            raise ValueError("TAP plan line (1..N) not found in output")

        plan_match = re.match(r"^1\.\.(\d+)", lines[plan_idx].strip())
        plan_total = int(plan_match.group(1)) if plan_match else 0

        # -- Parse summary lines following the plan line -----------------
        # The summary block immediately follows the final plan line.
        summary: dict[str, str] = {}
        for j in range(plan_idx + 1, min(plan_idx + 15, len(lines))):
            cline = lines[j].strip()
            m = TapAdapter._SUMMARY_RE.match(cline)
            if m:
                summary[m.group(1)] = m.group(2)
            elif cline:
                # Non-empty, non-# line signals end of summary.
                break

        if not summary:
            raise ValueError("TAP summary block not found after plan line")

        total = int(summary.get("tests", plan_total))
        passed = int(summary.get("pass", 0))
        failed = int(summary.get("fail", 0))
        skipped = int(summary.get("skipped", 0))
        todo = int(summary.get("todo", 0))
        duration_ms = float(summary.get("duration_ms", 0))
        duration_s = duration_ms / 1000.0

        failures_list = TapAdapter._collect_failures(lines)

        return TestReport(
            status="failed" if failed > 0 else "ok",
            duration=duration_s,
            passed=passed,
            failed=failed,
            failures=failures_list if failed > 0 and failures_list else None,
            total=total,
            skipped=skipped,
            todo=todo,
        )

    @classmethod
    def parse_json(cls, raw: str) -> TestReport:
        """Parse raw TAP v13 string into a TestReport.

        TAP is not JSON; ``parse_json`` exists for interface
        consistency with the other adapter classes.
        """
        return cls.parse(raw)


# ---------------------------------------------------------------------------
# Convenience dispatcher
# ---------------------------------------------------------------------------
