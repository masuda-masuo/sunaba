'''Tests for structured test report adapters (test_report.py).'''

from __future__ import annotations

import json
import pytest

from src.code_sandbox_mcp.test_report import (
    TestFailure,
    TestReport,
    export_test_report,
    prune_library_frames,
    PytestAdapter,
    JestAdapter,
    GoTestAdapter,
    parse_test_report,
)


# ===================================================================
# Common schema tests
# ===================================================================


class TestTestFailure:
    '''TestFailure dataclass.'''

    def test_fields(self) -> None:
        f = TestFailure(test='test_login', error='AssertionError', file='auth/login.py', line=42)
        assert f.test == 'test_login'
        assert f.error == 'AssertionError'
        assert f.file == 'auth/login.py'
        assert f.line == 42


class TestTestReport:
    '''TestReport dataclass and serialization.'''

    def test_success_minimal(self) -> None:
        '''Success case returns only status, passed, duration.'''
        r = TestReport(status='ok', duration=1.5, passed=42)
        d = r.to_dict()
        assert d == {
            'status': 'ok',
            'duration': 1.5,
            'passed': 42,
        }
        assert 'failed' not in d
        assert 'failures' not in d

    def test_failure_full(self) -> None:
        '''Failure case includes failed count and failures array.'''
        r = TestReport(
            status='failed',
            duration=3.2,
            passed=120,
            failed=2,
            failures=[
                TestFailure(test='test_a', error='ErrA', file='a.py', line=10),
                TestFailure(test='test_b', error='ErrB', file='b.py', line=20),
            ],
        )
        d = r.to_dict()
        assert d['status'] == 'failed'
        assert d['passed'] == 120
        assert d['failed'] == 2
        assert len(d['failures']) == 2
        assert d['failures'][0] == {'test': 'test_a', 'error': 'ErrA', 'file': 'a.py', 'line': 10}

    def test_export_test_report_json(self) -> None:
        '''export_test_report returns valid JSON.'''
        r = TestReport(status='ok', duration=0.5, passed=10)
        js = export_test_report(r)
        parsed = json.loads(js)
        assert parsed['status'] == 'ok'
        assert parsed['passed'] == 10


# ===================================================================
# Library frame pruning tests
# ===================================================================


class TestPruneLibraryFrames:

    def test_removes_site_packages(self) -> None:
        tb = '''Traceback (most recent call last):
  File "/usr/lib/python3.12/site-packages/packaging/core.py", line 42, in func
    do_stuff()
  File "/home/user/project/app.py", line 10, in main
    raise ValueError("boom")
'''
        pruned = prune_library_frames(tb, max_frames=5)
        assert 'site-packages' not in pruned
        assert 'app.py' in pruned
        assert 'ValueError' in pruned or 'boom' in pruned

    def test_removes_pytest_frames(self) -> None:
        tb = '''  File "/home/user/.local/lib/python3.12/site-packages/_pytest/runner.py", line 200
    return func()
  File "/home/user/.local/lib/python3.12/site-packages/pytest/__init__.py", line 50
    pass
  File "/home/user/project/test_app.py", line 15, in test_login
    assert result == expected
'''
        pruned = prune_library_frames(tb, max_frames=5)
        assert '_pytest/' not in pruned
        assert 'pytest/' not in pruned
        assert 'test_app.py' in pruned

    def test_removes_dist_packages(self) -> None:
        tb = '''  File "/usr/lib/python3/dist-packages/requests/api.py", line 50
    return request()
  File "/home/user/project/my_test.py", line 5
    assert response.ok
'''
        pruned = prune_library_frames(tb, max_frames=5)
        assert 'dist-packages' not in pruned
        assert 'my_test.py' in pruned

    def test_fallback_when_all_removed(self) -> None:
        '''When all frames are library frames, keep last N.'''
        tb = '''  File "/usr/lib/python3.12/site-packages/a.py", line 1
    x
  File "/usr/lib/python3.12/site-packages/b.py", line 2
    y
  File "/usr/lib/python3.12/site-packages/c.py", line 3
    z
'''
        pruned = prune_library_frames(tb, max_frames=2)
        # Should keep last 2 lines as fallback.
        lines = pruned.split('\n')
        assert len(lines) <= 2

    def test_empty_traceback(self) -> None:
        assert prune_library_frames('', max_frames=5) == ''

    def test_max_frames_limit(self) -> None:
        tb = 'line1\nline2\nline3\nline4\nline5\nline6\n'
        pruned = prune_library_frames(tb, max_frames=3)
        assert len(pruned.split('\n')) == 3


# ===================================================================
# Pytest adapter tests
# ===================================================================


class TestPytestAdapter:

    def test_all_passed(self) -> None:
        data = {
            'summary': {'total': 5, 'passed': 5, 'failed': 0},
            'duration': 0.8,
            'tests': [
                {'nodeid': 'test_a.py::test_one', 'outcome': 'passed', 'file': 'test_a.py', 'line': 1},
                {'nodeid': 'test_a.py::test_two', 'outcome': 'passed', 'file': 'test_a.py', 'line': 5},
            ],
        }
        report = PytestAdapter.parse(data)
        assert report.status == 'ok'
        assert report.passed == 5
        assert report.failed == 0
        assert report.duration == 0.8
        d = report.to_dict()
        assert d == {'status': 'ok', 'duration': 0.8, 'passed': 5}

    def test_some_failed(self) -> None:
        data = {
            'summary': {'total': 3, 'passed': 1, 'failed': 2},
            'duration': 2.1,
            'tests': [
                {'nodeid': 'test_a.py::test_pass', 'outcome': 'passed', 'file': 'test_a.py', 'line': 1},
                {
                    'nodeid': 'test_a.py::test_fail',
                    'outcome': 'failed',
                    'file': 'test_a.py',
                    'line': 10,
                    'call': {
                        'crash': {'traceback': '  File "/home/user/project/test_a.py", line 10\n    assert False\nAssertionError'},
                        'message': 'AssertionError',
                    },
                },
                {
                    'nodeid': 'test_b.py::test_error',
                    'outcome': 'error',
                    'file': 'test_b.py',
                    'line': 20,
                    'call': {'crash': None, 'message': 'ZeroDivisionError'},
                },
            ],
        }
        report = PytestAdapter.parse(data)
        assert report.status == 'failed'
        assert report.failed == 2
        assert report.passed == 1
        assert len(report.failures) == 2
        assert report.failures[0].test == 'test_a.py::test_fail'
        assert report.failures[1].test == 'test_b.py::test_error'

    def test_empty_report(self) -> None:
        data = {'summary': {'total': 0, 'passed': 0, 'failed': 0}, 'duration': 0.0, 'tests': []}
        report = PytestAdapter.parse(data)
        assert report.status == 'ok'
        assert report.passed == 0
        assert report.failed == 0
        assert report.failures is None

    def test_parse_json_round_trip(self) -> None:
        raw = json.dumps({
            'summary': {'total': 1, 'passed': 1, 'failed': 0},
            'duration': 0.3,
            'tests': [{'nodeid': 't.py::t', 'outcome': 'passed', 'file': 't.py', 'line': 1}],
        })
        report = PytestAdapter.parse_json(raw)
        assert report.status == 'ok'
        assert report.passed == 1


# ===================================================================
# Jest adapter tests
# ===================================================================


class TestJestAdapter:

    def test_all_passed(self) -> None:
        data = {
            'numPassedTests': 10,
            'numFailedTests': 0,
            'numRuntimeMs': 1500,
            'testResults': [
                {
                    'assertionResults': [
                        {'status': 'passed', 'fullName': 'sum adds', 'title': 'adds'},
                    ],
                },
            ],
        }
        report = JestAdapter.parse(data)
        assert report.status == 'ok'
        assert report.passed == 10
        assert report.failed == 0
        assert report.duration == 1.5

    def test_some_failed(self) -> None:
        data = {
            'numPassedTests': 8,
            'numFailedTests': 2,
            'numRuntimeMs': 2000,
            'testResults': [
                {
                    'assertionResults': [
                        {
                            'status': 'failed',
                            'fullName': 'sum fails on invalid input',
                            'title': 'fails on invalid input',
                            'failureMessages': [
                                'expect(received).toBe(expected)\n    at Object.<anonymous> (/home/user/project/sum.test.js:42:12)',
                            ],
                        },
                    ],
                },
            ],
        }
        report = JestAdapter.parse(data)
        assert report.status == 'failed'
        assert report.failed == 2
        assert report.passed == 8
        assert len(report.failures) == 1
        assert 'sum.test.js' in report.failures[0].file or 'sum.test.js' in report.failures[0].test

    def test_empty_report(self) -> None:
        data = {
            'numPassedTests': 0,
            'numFailedTests': 0,
            'numRuntimeMs': 0,
            'testResults': [],
        }
        report = JestAdapter.parse(data)
        assert report.status == 'ok'
        assert report.passed == 0
        assert report.failures is None

    def test_failure_no_messages(self) -> None:
        data = {
            'numPassedTests': 0,
            'numFailedTests': 1,
            'numRuntimeMs': 100,
            'testResults': [
                {
                    'assertionResults': [
                        {'status': 'failed', 'fullName': 'broken', 'title': 'broken', 'failureMessages': []},
                    ],
                },
            ],
        }
        report = JestAdapter.parse(data)
        assert report.status == 'failed'
        assert len(report.failures) == 1
        assert report.failures[0].error == 'unknown'

    def test_parse_json_round_trip(self) -> None:
        raw = json.dumps({
            'numPassedTests': 3,
            'numFailedTests': 0,
            'numRuntimeMs': 500,
            'testResults': [
                {
                    'assertionResults': [
                        {'status': 'passed', 'fullName': 't1', 'title': 't1'},
                    ],
                },
            ],
        })
        report = JestAdapter.parse_json(raw)
        assert report.status == 'ok'
        assert report.passed == 3


# ===================================================================
# Go test adapter tests
# ===================================================================


class TestGoTestAdapter:

    def test_all_passed(self) -> None:
        events = [
            {'Action': 'output', 'Output': 'ok   \tgithub.com/user/project\t0.523s\n'},
            {'Action': 'pass', 'Test': 'TestAdd', 'Elapsed': 0.5},
            {'Action': 'pass', 'Test': 'TestSub', 'Elapsed': 0.3},
            {'Action': 'pass', 'Package': 'github.com/user/project', 'Elapsed': 0.523},
        ]
        report = GoTestAdapter.parse(events)
        assert report.status == 'ok'
        assert report.passed == 2
        assert report.failed == 0
        assert report.failures is None

    def test_some_failed(self) -> None:
        events = [
            {'Action': 'output', 'Test': 'TestFail', 'Output': '    /home/user/project/fail_test.go:42: expected 2, got 1\n'},
            {'Action': 'output', 'Test': 'TestFail', 'Output': 'FAIL\n'},
            {'Action': 'fail', 'Test': 'TestFail', 'Elapsed': 0.1},
            {'Action': 'pass', 'Test': 'TestPass', 'Elapsed': 0.2},
            {'Action': 'pass', 'Package': 'github.com/user/project', 'Elapsed': 0.523},
        ]
        report = GoTestAdapter.parse(events)
        assert report.status == 'failed'
        assert report.passed == 1
        assert report.failed == 1
        assert len(report.failures) == 1
        assert report.failures[0].test == 'TestFail'
        assert 'fail_test.go' in report.failures[0].file or 'FAIL' in report.failures[0].error

    def test_empty_events(self) -> None:
        report = GoTestAdapter.parse([])
        assert report.status == 'ok'
        assert report.passed == 0
        assert report.failed == 0
        assert report.failures is None

    def test_package_level_fail_only(self) -> None:
        '''Package-level failure with no individual test failures (e.g. build failure).'''
        events = [
            {'Action': 'output', 'Output': '# github.com/user/project\n./main.go:5:2: undefined: x\n'},
            {'Action': 'fail', 'Package': 'github.com/user/project', 'Elapsed': 0.1},
        ]
        report = GoTestAdapter.parse(events)
        assert report.status == 'ok'  # no individual test failures => ok
        assert report.passed == 0
        assert report.failed == 0

    def test_parse_json_ndjson(self) -> None:
        raw = '{"Action":"pass","Test":"TestA","Elapsed":0.2}\n{"Action":"pass","Package":"pkg","Elapsed":0.5}\n'
        report = GoTestAdapter.parse_json(raw)
        assert report.status == 'ok'
        assert report.passed == 1

    def test_parse_json_with_elapsed_in_last_event(self) -> None:
        events = [
            {'Action': 'pass', 'Test': 'TestOne'},
            {'Action': 'pass', 'Package': 'pkg', 'Elapsed': 1.234},
        ]
        report = GoTestAdapter.parse(events)
        assert report.duration == pytest.approx(1.234, rel=1e-3)


# ===================================================================
# parse_test_report dispatcher tests
# ===================================================================


class TestParseTestReport:

    def test_pytest_dispatch(self) -> None:
        raw = json.dumps({
            'summary': {'total': 1, 'passed': 1, 'failed': 0},
            'duration': 0.1,
            'tests': [{'nodeid': 't.py::t', 'outcome': 'passed', 'file': 't.py', 'line': 1}],
        })
        result = parse_test_report('pytest', raw)
        parsed = json.loads(result)
        assert parsed['status'] == 'ok'
        assert parsed['passed'] == 1

    def test_jest_dispatch(self) -> None:
        raw = json.dumps({
            'numPassedTests': 5,
            'numFailedTests': 0,
            'numRuntimeMs': 100,
            'testResults': [],
        })
        result = parse_test_report('jest', raw)
        parsed = json.loads(result)
        assert parsed['status'] == 'ok'
        assert parsed['passed'] == 5

    def test_go_test_dispatch(self) -> None:
        raw = '{"Action":"pass","Test":"TestX","Elapsed":0.1}\n{"Action":"pass","Package":"pkg","Elapsed":0.2}\n'
        result = parse_test_report('go-test', raw)
        parsed = json.loads(result)
        assert parsed['status'] == 'ok'
        assert parsed['passed'] == 1

    def test_unsupported_framework_raises(self) -> None:
        with pytest.raises(ValueError, match='Unsupported test framework'):
            parse_test_report('unknown', '{}')
