"""Tests for structured test report adapters (test_report.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.sunaba.test_report import (
    GoTestAdapter,
    JestAdapter,
    PytestAdapter,
    TapAdapter,
    TestFailure,
    TestReport,
    export_test_report,
    prune_library_frames,
)

# ===================================================================
# Common schema tests
# ===================================================================


class TestTestFailure:
    """TestFailure dataclass."""

    def test_fields(self) -> None:
        f = TestFailure(test="test_login", error="AssertionError", file="auth/login.py", line=42)
        assert f.test == "test_login"
        assert f.error == "AssertionError"
        assert f.file == "auth/login.py"
        assert f.line == 42


class TestTestReport:
    """TestReport dataclass and serialization."""

    def test_success_minimal(self) -> None:
        """Success case returns only status, passed, duration."""
        r = TestReport(status="ok", duration=1.5, passed=42)
        d = r.to_dict()
        assert d == {
            "status": "ok",
            "duration": 1.5,
            "passed": 42,
        }
        assert "failed" not in d
        assert "failures" not in d

    def test_failure_full(self) -> None:
        """Failure case includes failed count and failures array."""
        r = TestReport(
            status="failed",
            duration=3.2,
            passed=120,
            failed=2,
            failures=[
                TestFailure(test="test_a", error="ErrA", file="a.py", line=10),
                TestFailure(test="test_b", error="ErrB", file="b.py", line=20),
            ],
        )
        d = r.to_dict()
        assert d["status"] == "failed"
        assert d["passed"] == 120
        assert d["failed"] == 2
        assert len(d["failures"]) == 2
        assert d["failures"][0] == {"test": "test_a", "error": "ErrA", "file": "a.py", "line": 10}

    def test_export_test_report_json(self) -> None:
        """export_test_report returns valid JSON."""
        r = TestReport(status="ok", duration=0.5, passed=10)
        js = export_test_report(r)
        parsed = json.loads(js)
        assert parsed["status"] == "ok"
        assert parsed["passed"] == 10


# ===================================================================
# Library frame pruning tests
# ===================================================================


class TestPruneLibraryFrames:

    def test_removes_site_packages(self) -> None:
        tb = """Traceback (most recent call last):
  File "/usr/lib/python3.12/site-packages/packaging/core.py", line 42, in func
    do_stuff()
  File "/home/user/project/app.py", line 10, in main
    raise ValueError("boom")
"""
        pruned = prune_library_frames(tb, max_frames=5)
        assert "site-packages" not in pruned
        assert "app.py" in pruned
        assert "ValueError" in pruned or "boom" in pruned

    def test_removes_pytest_frames(self) -> None:
        tb = """  File "/home/user/.local/lib/python3.12/site-packages/_pytest/runner.py", line 200
    return func()
  File "/home/user/.local/lib/python3.12/site-packages/pytest/__init__.py", line 50
    pass
  File "/home/user/project/test_app.py", line 15, in test_login
    assert result == expected
"""
        pruned = prune_library_frames(tb, max_frames=5)
        assert "_pytest/" not in pruned
        assert "pytest/" not in pruned
        assert "test_app.py" in pruned

    def test_removes_dist_packages(self) -> None:
        tb = """  File "/usr/lib/python3/dist-packages/requests/api.py", line 50
    return request()
  File "/home/user/project/my_test.py", line 5
    assert response.ok
"""
        pruned = prune_library_frames(tb, max_frames=5)
        assert "dist-packages" not in pruned
        assert "my_test.py" in pruned

    def test_fallback_when_all_removed(self) -> None:
        """When all frames are library frames, keep last N."""
        tb = """  File "/usr/lib/python3.12/site-packages/a.py", line 1
    x
  File "/usr/lib/python3.12/site-packages/b.py", line 2
    y
  File "/usr/lib/python3.12/site-packages/c.py", line 3
    z
"""
        pruned = prune_library_frames(tb, max_frames=2)
        lines = pruned.split("\n")
        assert len(lines) <= 2

    def test_empty_traceback(self) -> None:
        assert prune_library_frames("", max_frames=5) == ""

    def test_max_frames_limit(self) -> None:
        tb = "line1\nline2\nline3\nline4\nline5\nline6\n"
        pruned = prune_library_frames(tb, max_frames=3)
        assert len(pruned.split("\n")) == 3


# ===================================================================
# Pytest adapter tests
# ===================================================================


class TestPytestAdapter:

    def test_all_passed(self) -> None:
        data = {
            "summary": {"total": 5, "passed": 5, "failed": 0},
            "duration": 0.8,
            "tests": [
                {"nodeid": "test_a.py::test_one", "outcome": "passed"},
                {"nodeid": "test_a.py::test_two", "outcome": "passed"},
            ],
        }
        report = PytestAdapter.parse(data)
        assert report.status == "ok"
        assert report.passed == 5
        assert report.failed == 0
        assert report.duration == 0.8
        d = report.to_dict()
        assert d == {"status": "ok", "duration": 0.8, "passed": 5}

    def test_some_failed(self) -> None:
        data = {
            "summary": {"total": 4, "passed": 2, "failed": 1, "errors": 1},
            "duration": 2.1,
            "tests": [
                {"nodeid": "test_a.py::test_pass", "outcome": "passed"},
                {"nodeid": "test_a.py::test_pass2", "outcome": "passed"},
                {
                    "nodeid": "test_a.py::test_fail",
                    "outcome": "failed",
                    "call": {
                        "crash": {
                            "path": "test_a.py",
                            "lineno": 10,
                            "message": "AssertionError\nassert False",
                        },
                        "longrepr": (
                            "def test_fail():\n"
                            "    assert False\n"
                            "E   AssertionError\n"
                            "\n"
                            "test_a.py:10: AssertionError"
                        ),
                        "traceback": [
                            {"path": "test_a.py", "lineno": 10, "message": "AssertionError"},
                        ],
                    },
                },
                {
                    "nodeid": "test_b.py::test_error",
                    "outcome": "error",
                    "setup": {
                        "crash": {
                            "path": "test_b.py",
                            "lineno": 5,
                            "message": "RuntimeError: fixture boom",
                        },
                        "longrepr": (
                            "    @pytest.fixture\n"
                            "    def boom():\n"
                            ">       raise RuntimeError(\"fixture boom\")\n"
                            "E       RuntimeError: fixture boom\n"
                            "\n"
                            "test_b.py:5: RuntimeError"
                        ),
                    },
                    "call": {},
                },
            ],
        }
        report = PytestAdapter.parse(data)
        assert report.status == "failed"
        assert report.failed == 2
        assert report.passed == 2
        assert len(report.failures) == 2
        assert report.failures[0].test == "test_a.py::test_fail"
        assert report.failures[0].error == (
            "def test_fail():\n"
            "    assert False\n"
            "E   AssertionError\n"
            "\n"
            "test_a.py:10: AssertionError"
        )
        assert report.failures[0].file == "test_a.py"
        assert report.failures[0].line == 10
        assert report.failures[1].test == "test_b.py::test_error"
        assert report.failures[1].file == "test_b.py"
        assert report.failures[1].line == 5

    def test_error_outcome_setup_crash(self) -> None:
        """error outcome with crash in setup stage (fixture failure)."""
        data = {
            "summary": {"total": 2, "passed": 0, "failed": 0, "errors": 1},
            "duration": 0.5,
            "tests": [
                {
                    "nodeid": "test_fixture.py::test_uses_fixture",
                    "outcome": "error",
                    "setup": {
                        "crash": {
                            "path": "test_fixture.py",
                            "lineno": 8,
                            "message": "ValueError: invalid fixture param",
                        },
                        "longrepr": (
                            "    @pytest.fixture\n"
                            "    def param():\n"
                            ">       raise ValueError(\"invalid fixture param\")\n"
                            "E       ValueError: invalid fixture param\n"
                            "\n"
                            "test_fixture.py:8: ValueError"
                        ),
                    },
                    "call": {},
                },
                {"nodeid": "test_ok.py::test_ok", "outcome": "passed"},
            ],
        }
        report = PytestAdapter.parse(data)
        assert report.status == "failed"
        assert report.failed == 1
        assert report.passed == 1
        assert len(report.failures) == 1
        assert report.failures[0].test == "test_fixture.py::test_uses_fixture"
        assert "ValueError" in report.failures[0].error
        assert report.failures[0].file == "test_fixture.py"
        assert report.failures[0].line == 8

    def test_failure_without_longrepr_falls_back_to_crash_message(self) -> None:
        """When longrepr is missing, fall back to crash.message."""
        data = {
            "summary": {"total": 1, "passed": 0, "failed": 1},
            "duration": 0.3,
            "tests": [
                {
                    "nodeid": "test_x.py::test_x",
                    "outcome": "failed",
                    "call": {
                        "crash": {
                            "path": "test_x.py",
                            "lineno": 3,
                            "message": "AssertionError: x should be 3",
                        },
                    },
                },
            ],
        }
        report = PytestAdapter.parse(data)
        assert report.status == "failed"
        assert report.failed == 1
        assert len(report.failures) == 1
        assert report.failures[0].error == "AssertionError: x should be 3"
        assert report.failures[0].file == "test_x.py"
        assert report.failures[0].line == 3

    def test_empty_report(self) -> None:
        data = {"summary": {"total": 0, "passed": 0, "failed": 0}, "duration": 0.0, "tests": []}
        report = PytestAdapter.parse(data)
        assert report.status == "ok"
        assert report.passed == 0
        assert report.failed == 0
        assert report.failures is None

    def test_parse_json_round_trip(self) -> None:
        raw = json.dumps(
            {
                "summary": {"total": 1, "passed": 1, "failed": 0},
                "duration": 0.3,
                "tests": [{"nodeid": "t.py::t", "outcome": "passed"}],
            }
        )
        report = PytestAdapter.parse_json(raw)
        assert report.status == "ok"
        assert report.passed == 1

    def test_snapshot_real_data(self) -> None:
        """Real pytest-json-report output shape (snapshot test)."""
        raw = json.dumps({
            "summary": {"total": 2, "passed": 1, "failed": 1, "errors": 0},
            "duration": 0.42,
            "tests": [
                {"nodeid": "test_x.py::test_ok", "outcome": "passed"},
                {
                    "nodeid": "test_x.py::test_fail",
                    "outcome": "failed",
                    "call": {
                        "crash": {
                            "path": "/work/test_x.py",
                            "lineno": 6,
                            "message": "AssertionError: x should be 3\nassert 2 == 3",
                        },
                        "traceback": [
                            {"path": "test_x.py", "lineno": 6, "message": "AssertionError"},
                        ],
                        "longrepr": (
                            "def test_fail():\n"
                            "        x = 2\n"
                            ">       assert x == 3, \"x should be 3\"\n"
                            "E       AssertionError: x should be 3\n"
                            "E       assert 2 == 3\n"
                            "\n"
                            "test_x.py:6: AssertionError"
                        ),
                    },
                },
            ],
        })
        report = PytestAdapter.parse_json(raw)
        assert report.status == "failed"
        assert report.failed == 1
        assert report.passed == 1
        assert len(report.failures) == 1
        f = report.failures[0]
        assert f.test == "test_x.py::test_fail"
        assert "assert 2 == 3" in f.error
        assert f.file == "/work/test_x.py"
        assert f.line == 6


# ===================================================================
# Jest adapter tests
# ===================================================================


class TestJestAdapter:

    def test_all_passed(self) -> None:
        data = {
            "numPassedTests": 10,
            "numFailedTests": 0,
            "startTime": 1000000,
            "testResults": [
                {
                    "startTime": 1000000,
                    "endTime": 1001500,
                    "assertionResults": [
                        {"status": "passed", "fullName": "sum adds", "title": "adds"},
                    ],
                },
            ],
        }
        report = JestAdapter.parse(data)
        assert report.status == "ok"
        assert report.passed == 10
        assert report.failed == 0
        assert report.duration == 1.5

    def test_some_failed(self) -> None:
        data = {
            "numPassedTests": 8,
            "numFailedTests": 2,
            "startTime": 2000000,
            "testResults": [
                {
                    "startTime": 2000000,
                    "endTime": 2002000,
                    "assertionResults": [
                        {
                            "status": "failed",
                            "fullName": "sum fails on invalid input",
                            "title": "fails on invalid input",
                            "failureMessages": [
                                "expect(received).toBe(expected)\n    at Object.<anonymous> (/home/user/project/sum.test.js:42:12)",
                            ],
                        },
                    ],
                },
            ],
        }
        report = JestAdapter.parse(data)
        assert report.status == "failed"
        assert report.failed == 2
        assert report.passed == 8
        assert len(report.failures) == 1
        assert "sum.test.js" in report.failures[0].file or "sum.test.js" in report.failures[0].test

    def test_empty_report(self) -> None:
        data = {
            "numPassedTests": 0,
            "numFailedTests": 0,
            "startTime": 0,
            "testResults": [],
        }
        report = JestAdapter.parse(data)
        assert report.status == "ok"
        assert report.passed == 0
        assert report.failures is None

    def test_failure_no_messages(self) -> None:
        """When failureMessages is empty, a descriptive message should be used."""
        data = {
            "numPassedTests": 0,
            "numFailedTests": 1,
            "startTime": 3000000,
            "testResults": [
                {
                    "startTime": 3000000,
                    "endTime": 3000100,
                    "assertionResults": [
                        {
                            "status": "failed",
                            "fullName": "broken",
                            "title": "broken",
                            "failureMessages": [],
                        },
                    ],
                },
            ],
        }
        report = JestAdapter.parse(data)
        assert report.status == "failed"
        assert len(report.failures) == 1
        # Should contain a descriptive message, not just "unknown".
        assert "no failure messages" in report.failures[0].error

    def test_parse_json_round_trip(self) -> None:
        raw = json.dumps(
            {
                "numPassedTests": 3,
                "numFailedTests": 0,
                "startTime": 500000,
                "testResults": [
                    {
                        "startTime": 500000,
                        "endTime": 500500,
                        "assertionResults": [
                            {"status": "passed", "fullName": "t1", "title": "t1"},
                        ],
                    },
                ],
            }
        )
        report = JestAdapter.parse_json(raw)
        assert report.status == "ok"
        assert report.passed == 3


# ===================================================================
# Go test adapter tests
# ===================================================================


class TestGoTestAdapter:

    def test_all_passed(self) -> None:
        events = [
            {"Action": "output", "Output": "ok   \tgithub.com/user/project\t0.523s\n"},
            {"Action": "pass", "Test": "TestAdd", "Elapsed": 0.5},
            {"Action": "pass", "Test": "TestSub", "Elapsed": 0.3},
            {"Action": "pass", "Package": "github.com/user/project", "Elapsed": 0.523},
        ]
        report = GoTestAdapter.parse(events)
        assert report.status == "ok"
        assert report.passed == 2
        assert report.failed == 0
        assert report.failures is None

    def test_some_failed(self) -> None:
        events = [
            {
                "Action": "output",
                "Test": "TestFail",
                "Output": "    /home/user/project/fail_test.go:42: expected 2, got 1\n",
            },
            {"Action": "output", "Test": "TestFail", "Output": "FAIL\n"},
            {"Action": "fail", "Test": "TestFail", "Elapsed": 0.1},
            {"Action": "pass", "Test": "TestPass", "Elapsed": 0.2},
            {"Action": "pass", "Package": "github.com/user/project", "Elapsed": 0.523},
        ]
        report = GoTestAdapter.parse(events)
        assert report.status == "failed"
        assert report.passed == 1
        assert report.failed == 1
        assert len(report.failures) == 1
        assert report.failures[0].test == "TestFail"
        assert "fail_test.go" in report.failures[0].file or "FAIL" in report.failures[0].error

    def test_empty_events(self) -> None:
        report = GoTestAdapter.parse([])
        assert report.status == "ok"
        assert report.passed == 0
        assert report.failed == 0
        assert report.failures is None

    def test_package_level_fail_only(self) -> None:
        """Package-level failure with no individual test failures (e.g. build failure)."""
        events = [
            {"Action": "output", "Output": "# github.com/user/project\n./main.go:5:2: undefined: x\n"},
            {"Action": "fail", "Package": "github.com/user/project", "Elapsed": 0.1},
        ]
        report = GoTestAdapter.parse(events)
        assert report.status == "failed"
        assert report.passed == 0
        assert report.failed == 1
        assert len(report.failures) == 1
        assert "undefined" in report.failures[0].error

    def test_parse_json_ndjson(self) -> None:
        raw = (
            '{"Action":"pass","Test":"TestA","Elapsed":0.2}\n'
            '{"Action":"pass","Package":"pkg","Elapsed":0.5}\n'
        )
        report = GoTestAdapter.parse_json(raw)
        assert report.status == "ok"
        assert report.passed == 1

    def test_parse_json_with_elapsed_in_last_event(self) -> None:
        events = [
            {"Action": "pass", "Test": "TestOne"},
            {"Action": "pass", "Package": "pkg", "Elapsed": 1.234},
        ]
        report = GoTestAdapter.parse(events)
        assert report.duration == pytest.approx(1.234, rel=1e-3)


# ===================================================================
# TAP (node --test) adapter tests  —  real captured output as fixtures
# ===================================================================


class TestTapAdapter:

    _FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "tap"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load(self, name: str) -> str:
        return (self._FIXTURE_DIR / name).read_text("utf-8")

    # ------------------------------------------------------------------
    # Healthy run — 3 tests, all pass, no skips/todos
    # ------------------------------------------------------------------

    def test_healthy_run_multi_test(self) -> None:
        """A run with 3 passing tests reports correct counts."""
        raw = self._load("tap_ok.txt")
        report = TapAdapter.parse_json(raw)
        d = report.to_dict()

        assert d["status"] == "ok"
        assert d["passed"] == 3
        assert d["total"] == 3
        assert d["duration"] > 0
        # Skipped and todo are 0 — they are not included in to_dict()
        # when their value is 0, keeping the output minimal.
        assert "skipped" not in d
        assert "todo" not in d
        assert "failed" not in d

    # ------------------------------------------------------------------
    # Zero-test run — possible when discovery finds no files
    # ------------------------------------------------------------------

    def test_zero_test_run(self) -> None:
        """A run that executed 0 tests (exits 0) is distinguishable.

        The caller can distinguish a zero-test run from a passing run
        with tests by checking ``total``: ``total: 0`` means no tests
        were discovered.  A run with unparseable output would lack
        the ``total`` key entirely.
        """
        raw = self._load("tap_zero.txt")
        report = TapAdapter.parse_json(raw)
        d = report.to_dict()

        assert d["status"] == "ok"
        assert d["passed"] == 0
        assert d["total"] == 0
        assert d["duration"] >= 0
        assert "skipped" not in d
        assert "todo" not in d

    # ------------------------------------------------------------------
    # Run with pass, fail, skipped and todo
    # ------------------------------------------------------------------

    def test_fail_skip_todo(self) -> None:
        """A run with mixed outcomes reports skipped and todo counts."""
        raw = self._load("tap_fail_skip_todo.txt")
        report = TapAdapter.parse_json(raw)
        d = report.to_dict()

        assert d["status"] == "failed"
        assert d["passed"] == 1
        assert d["total"] == 4
        assert d["skipped"] == 1
        assert d["todo"] == 1
        assert d["duration"] > 0
        # ``failed`` key is present because failures exist in to_dict()
        assert d["failed"] == 1

    # ------------------------------------------------------------------
    # Non-TAP output raises ValueError
    # ------------------------------------------------------------------

    def test_nontap_output_raises_valueerror(self) -> None:
        """Output that is not TAP at all raises ValueError."""
        raw = self._load("nontap_output.txt")
        with pytest.raises(ValueError, match="TAP plan line"):
            TapAdapter.parse_json(raw)

    def test_empty_string_raises_valueerror(self) -> None:
        with pytest.raises(ValueError):
            TapAdapter.parse_json("")

    def test_only_plan_line_raises_valueerror(self) -> None:
        """A plan line without a following summary block raises."""
        with pytest.raises(ValueError, match="TAP summary block"):
            TapAdapter.parse_json("1..0\n")

    # ------------------------------------------------------------------
    # Round-trip: parse → to_dict → JSON → parse → match
    # ------------------------------------------------------------------

    def test_to_dict_stable(self) -> None:
        """to_dict() produces the same result when called twice."""
        raw = self._load("tap_ok.txt")
        report = TapAdapter.parse_json(raw)
        d1 = report.to_dict()
        d2 = report.to_dict()
        assert d1 == d2

    def test_reparse_identical(self) -> None:
        """Parsing the same raw TAP twice yields identical dicts."""
        raw = self._load("tap_ok.txt")
        r1 = TapAdapter.parse_json(raw)
        r2 = TapAdapter.parse_json(raw)
        assert r1.to_dict() == r2.to_dict()

    # ------------------------------------------------------------------
    # Duration conversion: milliseconds → seconds
    # ------------------------------------------------------------------

    def test_duration_converted_to_seconds(self) -> None:
        raw = self._load("tap_ok.txt")
        report = TapAdapter.parse_json(raw)
        # The fixture has duration_ms around 47.8 → ~0.048 seconds.
        assert 0.01 < report.duration < 0.5

    # ------------------------------------------------------------------
    # Per-test failures from ``not ok`` lines (Issue #804)
    # ------------------------------------------------------------------

    def test_midstream_failure_outside_tail(self) -> None:
        """A failure early in the stream is captured even when 20+ passing
        lines follow it (the raw_tail window would miss it)."""
        raw = self._load("tap_fail_midstream.txt")
        report = TapAdapter.parse_json(raw)
        d = report.to_dict()

        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["the failing test"]
        assert failures[0].file == ""
        assert failures[0].line == 0
        assert failures[0].error == "Expected values to be strictly equal:\n\n1 !== 2"

        # Summary counts unchanged.
        assert d["status"] == "failed"
        assert d["passed"] == 25
        assert d["failed"] == 1
        assert d["total"] == 26

        # Guard: the failure is genuinely outside the 20-line tail.
        tail = "\n".join(raw.strip().split("\n")[-20:])
        assert "the failing test" not in tail

    def test_nested_subtest_failure(self) -> None:
        """Indented ``not ok`` lines (nested subtests) are captured in
        stream order, alongside the parent suite's own ``not ok``."""
        raw = self._load("tap_fail_nested.txt")
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["subtracts", "math"]
        assert failures[0].error == "Expected 5 to equal 4"

    def test_not_ok_skip_todo_excluded(self) -> None:
        """``not ok ... # SKIP`` / ``# TODO`` are not failures per TAP."""
        raw = self._load("tap_fail_skip_todo_notok.txt")
        report = TapAdapter.parse_json(raw)
        d = report.to_dict()
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["real failure"]
        assert d["failed"] == 1
        assert d["skipped"] == 1
        assert d["todo"] == 1

    def test_yaml_error_block_echo_not_collected(self) -> None:
        """A ``not ok``-shaped line inside a YAML ``error:`` block (test
        console output echoed through the TAP stream) is not read as a
        test point: exactly one failure is reported, the real one
        (Issue #804 review finding 1)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - the failing test\n"
            "  ---\n"
            "  error: |-\n"
            "    not ok 9 - text echoed inside the error block\n"
            "    some other line\n"
            "  ...\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        # Exactly ONE failure entry -- the echoed line inside the YAML
        # block must not fabricate a phantom "text echoed inside the
        # error block" test point.
        assert [f.test for f in failures] == ["the failing test"]
        assert failures[0].file == ""
        assert failures[0].line == 0
        # The excerpt still carries the diagnostic content (the echoed
        # line is part of the error text); only its *collection* as a
        # separate failure is prevented.
        assert "not ok 9 - text echoed inside the error block" in failures[0].error

    def test_bare_not_ok_without_number_or_dash_not_collected(self) -> None:
        """A bare ``not ok`` line with neither a test number nor a ``- ``
        separator (console echo shape, outside any YAML block) is not a
        TAP test point and is not collected."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - real failure\n"
            "  ---\n"
            "  error: |-\n"
            "    boom\n"
            "  ...\n"
            "not ok plain echo with neither number nor dash\n"
            "not ok\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["real failure"]

    def test_embedded_tap_in_error_block_not_collected(self) -> None:
        """An inner TAP fragment embedded in an ``error:`` message (e.g.
        a test asserting on a nested ``node --test`` run) carries its own
        ``---``/``...`` pairs and ``not ok`` lines at a deeper
        indentation.  Only a ``...`` at the ``---`` opener's indent ends
        the outer block (TAP 13 semantics), so the inner fragment's
        ``...`` and its ``not ok 2 - inner two`` line are content:
        exactly one failure is reported (Issue #804 review finding 1)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - embedded inner TAP with two failures\n"
            "  ---\n"
            "  error: |-\n"
            "    inner run output:\n"
            "    TAP version 13\n"
            "    not ok 1 - inner one\n"
            "      ---\n"
            "      error: |-\n"
            "        boom one\n"
            "      ...\n"
            "    not ok 2 - inner two\n"
            "      ---\n"
            "      error: |-\n"
            "        boom two\n"
            "      ...\n"
            "    1..2\n"
            "  code: 'ERR_TEST_FAILURE'\n"
            "  ...\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        # Exactly ONE failure entry -- the inner fragment's ``not ok``
        # lines and ``...`` closers must not fabricate phantoms.
        assert [f.test for f in failures] == [
            "embedded inner TAP with two failures"
        ]
        assert all("inner two" != f.test for f in failures)

    def test_deeper_ellipsis_before_error_key_keeps_excerpt(self) -> None:
        """A ``...`` at a deeper indentation than the ``---`` opener
        (the closer of an inner TAP fragment echoed inside the block,
        before the ``error:`` key) is content, not the terminator: the
        excerpt is still extracted from the ``error:`` key past it.
        Only a ``...`` at the opener's own indent ends the block (TAP
        13 semantics; #809)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - failing test\n"
            "  ---\n"
            "  code: 'ERR_TEST_FAILURE'\n"
            "    inner fragment echoed before the error key:\n"
            "    TAP version 13\n"
            "    not ok 1 - inner test\n"
            "      ---\n"
            "      some inner detail\n"
            "      ...\n"
            "  error: |-\n"
            "    the real error text\n"
            "  ...\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        # Exactly ONE failure entry -- the inner fragment's ``not ok``
        # line is block content, not a test point.
        assert [f.test for f in failures] == ["failing test"]
        # The excerpt comes from the real ``error:`` key, not truncated
        # at the deeper ``...`` before it.
        assert failures[0].error == "the real error text"

    def test_opener_indent_ellipsis_still_terminates_block(self) -> None:
        """The block's real closer -- a ``...`` at the ``---`` opener's
        own indentation -- still terminates the block: indented lines
        after it must not leak into the excerpt (TAP 13; #809)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - failing test\n"
            "  ---\n"
            "  error: |-\n"
            "    the real error text\n"
            "  ...\n"
            "    this line is after the block closer\n"
            "ok 2 - next test\n"
            "1..2\n"
            "# tests 2\n"
            "# suites 0\n"
            "# pass 1\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["failing test"]
        # Nothing after the closer leaks into the excerpt.
        assert failures[0].error == "the real error text"
        assert "after the block closer" not in failures[0].error

    def test_deeper_ellipsis_inside_fallback_region_keeps_lines(self) -> None:
        """Without an ``error:`` key, a deeper-indented ``...`` inside
        the block (the fallback region) is content too: diagnostic
        lines after it are still included in the first-lines fallback
        excerpt (TAP 13; #809)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - failing test\n"
            "  ---\n"
            "  code: 'ERR_TEST_FAILURE'\n"
            "  first diagnostic line\n"
            "    ...\n"
            "  second diagnostic line\n"
            "  ...\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["failing test"]
        assert failures[0].error == "\n".join(
            [
                "code: 'ERR_TEST_FAILURE'",
                "first diagnostic line",
                "second diagnostic line",
            ]
        )

    def test_interior_dashes_in_openerless_block_stay_legacy(self) -> None:
        """Without a leading ``---`` opener there is no YAML block, and
        an interior ``---`` line must not switch the excerpt into
        opener mode: the legacy stop rule (any-indent ``...``
        terminates) applies to the whole opener-less region (#809
        review finding 1)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - failing test\n"
            "  line one\n"
            "  ---\n"
            "  line two\n"
            "    ...\n"
            "  line three\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["failing test"]
        # Legacy rule: the any-indent ``...`` still terminates the
        # opener-less region, so ``line three`` is excluded; the
        # interior ``---`` is filtered from the fallback excerpt.
        assert failures[0].error == "line one\nline two"

    def test_deeper_ellipsis_inside_error_scalar_is_content(self) -> None:
        """A deeper-indented ``...`` inside the ``error:`` block scalar
        itself (an inner TAP fragment echoed within the error text --
        the docstring's motivating case) is content: the scalar lines
        after it are kept in the excerpt (TAP 13; #809)."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - failing test\n"
            "  ---\n"
            "  error: |-\n"
            "    inner:\n"
            "    ...\n"
            "    more text\n"
            "  ...\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["failing test"]
        assert failures[0].error == "inner:\n...\nmore text"

    def test_unterminated_block_keeps_later_test_points(self) -> None:
        """A YAML block with no ``...`` at the opener's indent (truncated
        or malformed output) is closed by the first non-indented line, so
        real test points after it are still collected instead of being
        silently dropped."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - first failure\n"
            "  ---\n"
            "  error: |-\n"
            "    boom\n"
            "not ok 2 - second failure\n"
            "1..2\n"
            "# tests 2\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 2\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert [f.test for f in failures] == ["first failure", "second failure"]

    def test_failure_without_diagnostic_block(self) -> None:
        """A ``not ok`` with no YAML block yields error == \"\"."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - bare failure\n"
            "# Subtest: another\n"
            "ok 2 - another\n"
            "1..2\n"
            "# tests 2\n"
            "# suites 0\n"
            "# pass 1\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 10.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert len(failures) == 1
        assert failures[0].test == "bare failure"
        assert failures[0].error == ""
        assert failures[0].file == ""
        assert failures[0].line == 0

    def test_failure_error_excerpt_truncated_to_5_lines(self) -> None:
        """The YAML ``error:`` block scalar lands in ``error`` truncated."""
        raw = (
            "TAP version 13\n"
            "not ok 1 - failing test\n"
            "  ---\n"
            "  error: |-\n"
            "    line one\n"
            "    line two\n"
            "    line three\n"
            "    line four\n"
            "    line five\n"
            "    line six\n"
            "    line seven\n"
            "  ...\n"
            "1..1\n"
            "# tests 1\n"
            "# suites 0\n"
            "# pass 0\n"
            "# fail 1\n"
            "# cancelled 0\n"
            "# skipped 0\n"
            "# todo 0\n"
            "# duration_ms 20.0\n"
        )
        report = TapAdapter.parse_json(raw)
        failures = report.failures
        assert failures is not None
        assert failures[0].test == "failing test"
        assert failures[0].error == "\n".join(
            f"line {n}" for n in ("one", "two", "three", "four", "five")
        )

    def test_failures_capped_at_50(self) -> None:
        """More than 50 failing test points are capped with a synthetic entry."""
        lines = ["TAP version 13"]
        lines += [f"not ok {i} - failing test {i}" for i in range(1, 53)]
        lines += [
            "1..52",
            "# tests 52",
            "# suites 0",
            "# pass 0",
            "# fail 52",
            "# cancelled 0",
            "# skipped 0",
            "# todo 0",
            "# duration_ms 100.0",
        ]
        report = TapAdapter.parse_json("\n".join(lines))
        failures = report.failures
        assert failures is not None
        assert len(failures) == 51
        assert failures[0].test == "failing test 1"
        assert failures[49].test == "failing test 50"
        assert failures[50].test == "... (2 more not shown)"
        assert failures[50].error == ""

    def test_all_pass_failures_absent(self) -> None:
        """Passing runs keep the exact pre-#804 dict shape (no ``failures``)."""
        raw = self._load("tap_ok.txt")
        report = TapAdapter.parse_json(raw)
        d = report.to_dict()
        assert "failures" not in d
        assert d == {
            "status": "ok",
            "duration": 47.844101 / 1000.0,
            "passed": 3,
            "total": 3,
        }
