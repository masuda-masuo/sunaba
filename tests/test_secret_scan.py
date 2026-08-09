"""Tests for secret_scan module — unit tests with mocked container.

No committed file may contain a literal secret-shaped string (#676 trap 1).
Fixture values are built at runtime from parts.

The scanner is gitleaks (#842): scan output is a JSON report **array**, the
findings exit code is 99, and a finding's identity is ``sha1(Secret)``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from sunaba.tools.secret_scan import (  # noqa: I001, F401
    _GITLEAKS_FINDINGS_EXIT,
    _OVERRIDE_LOCK,
    _OVERRIDE_MAP,
    _OVERRIDE_REGISTRY,
    _REGISTRY_LOCK,
    _add_to_override_registry,
    _baseline_enabled,
    _check_gitleaks,
    _exclude_baseline,
    _update_baseline,
    check_override,
    consume_override,
    exec_in_container,
    get_override_registry_hashes,
    run_secret_scan,
    secret_scan_override,
)

# ============================================================================
# Helpers: build safe fixture data at runtime
# ============================================================================


def _dummy_version_output() -> str:
    """Return fake ``gitleaks version`` output — matches the real binary."""
    return "8.30.1"


def _fake_secret(seed: str) -> str:
    """Build a distinct fake secret string at runtime (never a literal)."""
    return "".join(chr(ord(c) + 1) for c in seed)


def _sha1(secret: str) -> str:
    """Return the baseline identity of *secret* (what the scanner path hashes)."""
    return hashlib.sha1(secret.encode(), usedforsecurity=False).hexdigest()


def _make_clean_scan_json() -> str:
    """Return the report a clean gitleaks run writes: an empty array."""
    return "[]"


def _make_report(entries: list[tuple[str, int, str, str]]) -> str:
    """Build a gitleaks JSON report from ``(file, line, rule_id, secret)``.

    Field names and shape are copied from the real binary's report (see
    ``tests/fixtures/secret_scan_real_output.json``); the secret values are
    built at runtime so no literal secret is committed.
    """
    return json.dumps([
        {
            "RuleID": rule_id,
            "Description": f"test finding for {rule_id}",
            "StartLine": line,
            "EndLine": line,
            "StartColumn": 1,
            "EndColumn": 1 + len(secret),
            "Match": secret,
            "Secret": secret,
            "File": filename,
            "SymlinkFile": "",
            "Commit": "",
            "Entropy": 4.2,
            "Author": "",
            "Email": "",
            "Date": "",
            "Message": "",
            "Tags": [],
            "Fingerprint": f"{filename}:{rule_id}:{line}",
        }
        for filename, line, rule_id, secret in entries
    ])


def _make_finding_json(filename: str, line: int, secret_type: str) -> str:
    """Build a gitleaks report with exactly one finding in *filename*.

    The secret is derived from the file name so two different files never
    collide on the same ``hashed_secret``.
    """
    return _make_report([
        (filename, line, secret_type, _fake_secret(f"no-real-secret-{filename}")),
    ])


def _hashes_in(report_json: str) -> list[str]:
    """Return the hashed_secret values run_secret_scan will derive from a report."""
    return [_sha1(entry["Secret"]) for entry in json.loads(report_json)]


def _load_real_scan_fixture() -> str:
    """Load genuine gitleaks report output from the committed fixture.

    The fixture was captured by running::

        gitleaks dir --no-banner --exit-code 99 --report-format json \
            --report-path - real_secret_input.py

    on a file containing secret-shaped values, then redacting the ``Match`` /
    ``Secret`` values (the shape is the point; committing the matched strings
    would put real-looking credentials in the repo and block publish).
    """
    import pathlib
    fixture_path = pathlib.Path(__file__).parent / "fixtures" / "secret_scan_real_output.json"
    return fixture_path.read_text()


# ============================================================================
# Fake container with scriptable exec_run
# ============================================================================

# ExecResult is a namedtuple (exit_code, output) returned by container.exec_run.
# Defined inline here so the test module does not depend on docker at import time.
_ExecResult = __import__("collections").namedtuple("ExecResult", ("exit_code", "output"))


def _make_container(
    exec_results: list[tuple[int, str, str]],
) -> MagicMock:
    """Build a mock container where successive exec_run calls return
    scripted (exit_code, stdout, stderr).

    Each exec_run call on the returned mock returns an ``ExecResult``
    namedtuple whose ``.output`` is ``(stdout_bytes, stderr_bytes)``
    (the demux=True shape).
    """
    container = MagicMock()
    results: list[_ExecResult] = []

    for ec, stdout, stderr in exec_results:
        stdout_bytes = stdout.encode("utf-8")
        stderr_bytes = stderr.encode("utf-8")
        results.append(_ExecResult(ec, (stdout_bytes, stderr_bytes)))

    container.exec_run.side_effect = results

    return container


# ============================================================================
# _exclude_baseline (issue #703)
# ============================================================================


class TestExcludeBaseline:
    """_exclude_baseline filters out the repo-root .secrets.baseline path
    with an exact match, preserving lookalike paths."""

    def test_exact_baseline_is_excluded(self) -> None:
        """.secrets.baseline alone -> removed."""
        result = _exclude_baseline([".secrets.baseline"])
        assert result == []

    def test_ordinary_files_preserved(self) -> None:
        """Non-baseline files are preserved."""
        result = _exclude_baseline(["main.py", "config.json"])
        assert result == ["main.py", "config.json"]

    def test_baseline_among_ordinary_files(self) -> None:
        """.secrets.baseline next to normal files -> only baseline removed."""
        result = _exclude_baseline(["main.py", ".secrets.baseline", "config.json"])
        assert result == ["main.py", "config.json"]

    def test_nested_baseline_not_excluded(self) -> None:
        """sub/dir/.secrets.baseline is NOT the repo-root baseline -> kept."""
        result = _exclude_baseline(["sub/dir/.secrets.baseline"])
        assert result == ["sub/dir/.secrets.baseline"]

    def test_baseline_lookalike_extensions_not_excluded(self) -> None:
        """.secrets.baseline.bak and .secrets.baseline.txt are NOT excluded."""
        result = _exclude_baseline([
            "notes/.secrets.baseline.bak",
            "sub/dir/.secrets.baseline.txt",
        ])
        assert result == [
            "notes/.secrets.baseline.bak",
            "sub/dir/.secrets.baseline.txt",
        ]

    def test_baseline_only_among_lookalikes(self) -> None:
        """Only the exact .secrets.baseline is filtered among lookalikes."""
        result = _exclude_baseline([
            ".secrets.baseline",
            "notes/.secrets.baseline.bak",
            "sub/dir/.secrets.baseline.txt",
            "sub/dir/.secrets.baseline",
        ])
        assert result == [
            "notes/.secrets.baseline.bak",
            "sub/dir/.secrets.baseline.txt",
            "sub/dir/.secrets.baseline",
        ]

    def test_empty_list(self) -> None:
        """Empty list -> empty list."""
        result = _exclude_baseline([])
        assert result == []


# ============================================================================
# _check_gitleaks
# ============================================================================


class TestCheckGitleaks:
    """_check_gitleaks probes for the gitleaks binary."""

    def test_available(self) -> None:
        """Version output on exit 0 → available."""
        container = _make_container([(0, _dummy_version_output(), "")])
        assert _check_gitleaks(container) is True

    def test_probe_command_is_gitleaks_version(self) -> None:
        """The probe is ``gitleaks version`` (not ``--version``, which the
        real binary rejects — a probe that always fails would silently turn
        every publish into ``skipped``)."""
        container = _make_container([(0, _dummy_version_output(), "")])
        _check_gitleaks(container)
        cmds = [c.kwargs.get("cmd") for c in container.exec_run.call_args_list]
        assert ["gitleaks", "version"] in cmds, cmds

    def test_not_available(self) -> None:
        """Non-zero exit → not available."""
        container = _make_container([(1, "", "command not found")])
        assert _check_gitleaks(container) is False

    def test_empty_output(self) -> None:
        """Zero exit but empty output → not available."""
        container = _make_container([(0, "", "")])
        assert _check_gitleaks(container) is False


# ============================================================================
# run_secret_scan
# ============================================================================


class TestRunSecretScan:
    """run_secret_scan: the core scanning logic."""

    def test_no_files(self) -> None:
        """Empty file list → clean, no scan attempted."""
        container = _make_container([])
        result = run_secret_scan(container, [], "/tmp/repo")
        assert result["secret_scan"] == "clean"
        assert result["files_scanned"] == []

    def test_gitleaks_unavailable(self) -> None:
        """gitleaks missing → prominent skip, no block."""
        container = _make_container([(1, "", "not found")])
        result = run_secret_scan(container, ["file.py"], "/tmp/repo")
        assert "SKIPPED" in result["secret_scan"]
        assert "gitleaks unavailable" in result["secret_scan"]

    def test_clean_scan(self) -> None:
        """gitleaks reports an empty array on exit 0 → clean.

        The scanner is never handed the suppression list, so the scan is a
        plain read; the baseline subtraction happens host-side afterwards.
        """
        container = _make_container([
            (0, _dummy_version_output(), ""),       # check available
            (0, _make_clean_scan_json(), ""),         # scan output
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(container, ["file.py"], "/tmp/repo")
        assert result["secret_scan"] == "clean"
        assert result["files_scanned"] == ["file.py"]

    def test_findings_detected(self) -> None:
        """Scan with a finding — returns findings with file/line/type/hash.

        Every entry in a gitleaks report is a finding; there is no
        "reviewed" flag to filter on.  A filter reintroduced here would
        drop real findings silently, so the mapping is asserted field by
        field, including the ``sha1(Secret)`` identity the baseline uses.
        """
        report = _make_finding_json("keys.py", 5, "aws-access-token")
        container = _make_container([
            (0, _dummy_version_output(), ""),       # check available
            (_GITLEAKS_FINDINGS_EXIT, report, ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(container, ["keys.py"], "/tmp/repo")
        assert result["secret_scan"] == "findings"
        assert len(result["findings"]) == 1
        f = result["findings"][0]
        assert f["file"] == "keys.py"
        assert f["line"] == 5
        assert f["type"] == "aws-access-token"
        assert f["hashed_secret"] == _hashes_in(report)[0]

    def test_findings_detected_with_real_fixture(self) -> None:
        """Parser handles a report captured from the real gitleaks binary."""
        fixture_str = _load_real_scan_fixture()
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, fixture_str, ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(
                container, ["real_secret_input.py"], "/tmp/repo",
            )
        assert result["secret_scan"] == "findings"
        assert len(result["findings"]) >= 2

    def test_scan_failure(self) -> None:
        """gitleaks crash (exit code that is neither 0 nor 99) → warning
        message and a blocking state, not a crash."""
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (1, "", "segfault"),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(container, ["bad.py"], "/tmp/repo")
        assert "WARNING" in result["secret_scan"]
        assert "failed" in result["secret_scan"]

    def test_unparseable_output_is_error_not_clean(self) -> None:
        """Scan that produces garbage output → error, never ``clean``.

        Bug 2 regression: unparseable output used to be reported as
        ``"clean"``, silently swallowing the scan failure.
        """
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (0, "this is not json {{{", ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(container, ["bad.py"], "/tmp/repo")
        assert result["secret_scan"] != "clean"
        assert "ERROR" in result["secret_scan"]
        assert "publish blocked" in result["secret_scan"]

    def test_empty_output_is_error_not_clean(self) -> None:
        """Scan that produces empty stdout → error, never ``clean``.

        Bug 2 regression: a clean gitleaks run still writes ``[]``, so
        empty stdout means the report never arrived.  It used to hit
        JSONDecodeError and be silently treated as clean.
        """
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (0, "", ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(container, ["bad.py"], "/tmp/repo")
        assert result["secret_scan"] != "clean"
        assert "ERROR" in result["secret_scan"]
        assert "empty output" in result["secret_scan"]

    def test_baseline_suppresses_known_finding(self) -> None:
        """A finding present in .secrets.baseline is NOT reported."""
        known_secret = _fake_secret("known-secret")
        known_hash = _sha1(known_secret)
        # Baseline with the known finding (the historical baseline shape,
        # unchanged by the gitleaks swap)
        baseline_json = json.dumps({
            "generated_at": "2026-01-01T00:00:00Z",
            "plugins_used": [],
            "results": {
                "safe.py": [
                    {
                        "type": "generic-api-key",
                        "filename": "safe.py",
                        "line_number": 10,
                        "hashed_secret": known_hash,
                        "is_verified": False,
                    }
                ]
            },
        })
        # Scan output has the SAME secret (already in baseline)
        scan_json = _make_report([
            ("safe.py", 10, "generic-api-key", known_secret),
        ])
        # Scan output with a NEW finding (not in baseline)
        new_secret = _fake_secret("new-secret-value")
        new_hash = _sha1(new_secret)
        scan_with_new_json = _make_report([
            ("safe.py", 10, "generic-api-key", known_secret),
            ("new.py", 5, "generic-api-key", new_secret),
        ])

        # CASE 1: finding exists in baseline → suppressed
        container = _make_container([
            (0, _dummy_version_output(), ""),       # check available
            (_GITLEAKS_FINDINGS_EXIT, scan_json, ""),   # scan output
            (0, baseline_json, ""),                  # cat .secrets.baseline
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["safe.py"], "/tmp/repo",
                baseline_hashes={known_hash},
            )
        assert result["secret_scan"] == "clean", (
            "Known finding in the host-resolved baseline should be suppressed"
        )

        # CASE 2: both known AND new finding → only new is reported
        container2 = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, scan_with_new_json, ""),
            (0, baseline_json, ""),                  # same baseline
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result2 = run_secret_scan(
                container2, ["safe.py", "new.py"], "/tmp/repo",
                baseline_hashes={known_hash},
            )
        assert result2["secret_scan"] == "findings"
        assert len(result2["findings"]) == 1
        assert result2["findings"][0]["hashed_secret"] == new_hash

    def test_baseline_absent_all_findings_reported(self) -> None:
        """When no baseline file exists, cat returns ec=1, all findings shown."""
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT,
             _make_finding_json("keys.py", 5, "aws-access-token"), ""),
            # cat .secrets.baseline -> ec=1 (file not found)
            (1, "", "No such file"),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(container, ["keys.py"], "/tmp/repo")
        assert result["secret_scan"] == "findings"
        assert len(result["findings"]) == 1

    # -------------------------------------------------------------------
    # Baseline exclusion (issue #703): .secrets.baseline is not scanned
    # -------------------------------------------------------------------

    def test_baseline_path_excluded_from_scan(self) -> None:
        """.secrets.baseline in the file list -> not scanned, no findings from it.

        Criterion 1: publishing with .secrets.baseline is not blocked by
        the baseline's own stored hashes.
        """
        # Baseline content that *would* produce findings if scanned
        # (hashed_secret values look like hex strings to the detector).
        baseline_content = json.dumps({
            "generated_at": "2026-07-20T00:00:00Z",
            "plugins_used": [],
            "results": {
                ".secrets.baseline": [
                    {
                        "type": "Hex High Entropy String",
                        "filename": ".secrets.baseline",
                        "line_number": 94,
                        "hashed_secret": "a" * 40,
                        "is_verified": False,
                    },
                ],
            },
        })
        container = _make_container([
            (0, _dummy_version_output(), ""),       # check available
            (0, baseline_content, ""),               # scan output (has baseline findings)
            (0, baseline_content, ""),               # cat .secrets.baseline (baseline = scan)
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, [".secrets.baseline"], "/tmp/repo",
            )
        # The baseline path is excluded from the scan, so no findings.
        assert result["secret_scan"] == "clean"
        assert result["files_scanned"] == []

    def test_ordinary_file_still_scanned_with_baseline(self) -> None:
        """An ordinary secret file + .secrets.baseline -> finding reported.

        Criterion 3: excluding the baseline must not suppress findings
        from real source files.
        """
        finding_json = _make_finding_json("secret.py", 3, "private-key")
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, finding_json, ""),  # finding from secret.py
            (1, "", "No such file"),                 # cat .secrets.baseline -> absent
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["secret.py", ".secrets.baseline"], "/tmp/repo",
            )
        assert result["secret_scan"] == "findings"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["file"] == "secret.py"
        assert result["files_scanned"] == ["secret.py"]

    def test_lookalike_baseline_paths_still_caught(self) -> None:
        """Files that resemble the baseline path are still scanned.

        Criterion 2: ``notes/.secrets.baseline.bak``,
        ``sub/dir/.secrets.baseline.txt``, and a nested
        ``sub/dir/.secrets.baseline`` with real secrets must be caught.
        """
        lookalikes = [
            "notes/.secrets.baseline.bak",
            "sub/dir/.secrets.baseline.txt",
            "sub/dir/.secrets.baseline",
        ]
        # One finding per lookalike file.  The per-file gitleaks loop
        # concatenates one report array per scanned file, so the parser must
        # cope with several arrays in a row -- build the fixture that way.
        reports = "\n".join(
            _make_report([(f, 1, "generic-api-key", _fake_secret(f + "-secret"))])
            for f in lookalikes
        )
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, reports, ""),  # scan with all lookalikes
            (1, "", ""),                              # cat .secrets.baseline absent
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, lookalikes, "/tmp/repo",
            )
        # All three lookalike paths should produce findings
        assert result["secret_scan"] == "findings"
        assert len(result["findings"]) == 3
        found_files = {f["file"] for f in result["findings"]}
        assert found_files == set(lookalikes), (
            f"Expected {set(lookalikes)}, got {found_files}"
        )
        assert result["files_scanned"] == lookalikes


# ============================================================================


# ============================================================================
# End-to-end tests with the real gitleaks binary
# ============================================================================


class _RealGitleaksContainer:
    """A container stand-in that runs commands with subprocess.

    ``run_secret_scan`` only needs ``exec_run``, so the real binary can be
    driven through the *production* code path -- the scan command, the exit
    code mapping and the report parser all execute for real.  Mocking the
    scan output instead is what let #696 ship: the layer that carried the
    guarantee was the one replaced by the mock.
    """

    def exec_run(self, cmd, stdout=True, stderr=True, workdir=None, demux=False):
        import subprocess
        result = subprocess.run(
            cmd, capture_output=True, cwd=workdir, timeout=60,
        )
        if demux:
            return (result.returncode, (result.stdout, result.stderr))
        return (result.returncode, result.stdout + result.stderr)


class TestRealGitleaks:
    """Drive the actual gitleaks binary installed in this container.

    Skipped (not failed) when gitleaks is absent: the binary comes from the
    sandbox image or a CI install step, and a developer running the suite on
    a bare checkout should not see a red suite for it.
    """

    @pytest.fixture(autouse=True)
    def _requires_gitleaks(self, tmp_path: object) -> None:
        if shutil.which("gitleaks") is None:
            pytest.skip("gitleaks is not installed on PATH")
        self.scratch = tmp_path  # type: ignore[attr-defined]

    @staticmethod
    def _aws_key() -> str:
        """Build a realistic-looking (fake) AWS key ID at runtime.

        Assembled from parts so the committed file never contains the
        contiguous string -- and so scanning this repo does not flag this
        file.  Well-known documentation keys are useless here: gitleaks
        allowlists them, so a check built on one reads as "the guard is
        broken" (the same trap #699 fell into).
        """
        return "AKIA" + "Q3EGPBCWXK4VJZ5H"

    def test_real_secret_produces_findings(self) -> None:
        """A file with a key-shaped value -> state findings, hash = sha1(key)."""
        key = self._aws_key()
        secret_file = self.scratch / "secret_input.py"  # type: ignore[attr-defined]
        secret_file.write_text('aws_access_key = "' + key + '"\n')

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["secret_input.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "findings", result
        assert len(result["findings"]) == 1, result
        finding = result["findings"][0]
        assert finding["file"] == "secret_input.py"
        assert finding["line"] == 1
        assert finding["type"] == "aws-access-token"
        # The baseline identity is sha1 of the matched string -- the same
        # value the previous scanner wrote, which is what keeps committed
        # baselines valid across the scanner swap (#842).  The literal pin
        # is deliberate alongside the recomputed one: it is the exact value
        # detect-secrets 1.5.0 wrote for this key, measured before the swap.
        assert finding["hashed_secret"] == _sha1(key)
        assert finding["hashed_secret"] == "af8334519c7a8648cb53b36d2634d46b88b9ca5a"

    def test_real_clean_file_is_clean(self) -> None:
        """A file with no secrets -> clean (the report is ``[]``, not empty)."""
        clean_file = self.scratch / "clean_input.py"  # type: ignore[attr-defined]
        clean_file.write_text('greeting = "hello world"\n')

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["clean_input.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "clean", result

    def test_real_missing_file_is_error_not_clean(self) -> None:
        """A scan that cannot run -> state error, so publish blocks.

        This is why ``--exit-code 99`` is passed: the gitleaks default for
        findings is 1, which is also what this fatal error returns, and the
        two must not be confusable.
        """
        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["no_such_file_here.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "error", result

    def test_real_baseline_hash_suppresses_finding(self) -> None:
        """The host-side hash set suppresses the real binary's finding."""
        key = self._aws_key()
        secret_file = self.scratch / "suppressed_input.py"  # type: ignore[attr-defined]
        secret_file.write_text('aws_access_key = "' + key + '"\n')

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["suppressed_input.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes={_sha1(key)},
        )

        assert result["secret_scan_state"] == "clean", result
        assert result["suppressed_count"] == 1, result

    def test_real_multi_file_findings_beat_clean(self) -> None:
        """Several files in ONE scan: a finding anywhere wins over clean.

        This is the shell loop's exit-code aggregation running against the
        real binary -- the layer the single-file tests above never reach.
        A publish manifest almost always carries more than one file, so the
        aggregation itself must be covered end to end, not only traced.
        """
        key = self._aws_key()
        (self.scratch / "a_clean.py").write_text('x = 1\n')  # type: ignore[attr-defined]
        (self.scratch / "b_secret.py").write_text(  # type: ignore[attr-defined]
            'aws_access_key = "' + key + '"\n'
        )
        (self.scratch / "c_clean.py").write_text('y = 2\n')  # type: ignore[attr-defined]

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["a_clean.py", "b_secret.py", "c_clean.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "findings", result
        assert len(result["findings"]) == 1, result
        assert result["findings"][0]["file"] == "b_secret.py"
        assert result["files_scanned"] == ["a_clean.py", "b_secret.py", "c_clean.py"]

    def test_real_multi_file_error_beats_findings(self) -> None:
        """Several files in ONE scan: any scan failure wins over findings.

        The file order puts the finding BEFORE the missing file, so this
        fails if the loop stops rewarding early findings instead of
        surfacing the later error (#704: partial results are not results).
        """
        key = self._aws_key()
        (self.scratch / "has_secret.py").write_text(  # type: ignore[attr-defined]
            'aws_access_key = "' + key + '"\n'
        )

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["has_secret.py", "missing_file.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "error", result

    def test_real_inline_gitleaks_allow_is_denied(self) -> None:
        """An inline ``gitleaks:allow`` comment must NOT suppress the finding.

        Without ``--ignore-gitleaks-allow`` the real binary returns clean for
        this exact input (verified live) -- an agent-writable one-line bypass
        of the publish gate, the #708 pattern in new clothes.
        """
        key = self._aws_key()
        secret_file = self.scratch / "allowed_input.py"  # type: ignore[attr-defined]
        secret_file.write_text(
            'aws_access_key = "' + key + '"  # gitleaks:allow\n'
        )

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["allowed_input.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "findings", result
        assert result["findings"][0]["hashed_secret"] == _sha1(key)

    def test_real_gitleaksignore_file_is_denied(self) -> None:
        """A repo-root ``.gitleaksignore`` must NOT suppress the finding.

        gitleaks' ignore-file lookup defaults to the scan cwd -- the repo
        root, which the agent can write.  The scan pins the lookup to a
        scanner-owned empty temp dir instead; this test plants a correct
        fingerprint for the finding and expects it to change nothing.
        """
        key = self._aws_key()
        secret_file = self.scratch / "ignored_input.py"  # type: ignore[attr-defined]
        secret_file.write_text('aws_access_key = "' + key + '"\n')
        ignore_file = self.scratch / ".gitleaksignore"  # type: ignore[attr-defined]
        ignore_file.write_text("ignored_input.py:aws-access-token:1\n")

        result = run_secret_scan(
            _RealGitleaksContainer(),
            ["ignored_input.py"],
            str(self.scratch),  # type: ignore[attr-defined]
            baseline_hashes=set(),
        )

        assert result["secret_scan_state"] == "findings", result
        assert result["findings"][0]["hashed_secret"] == _sha1(key)


# ============================================================================
# _baseline_enabled
# ============================================================================


class TestBaselineEnabled:
    """SUNABA_SECRETS_BASELINE environment variable."""

    def test_default_enabled(self) -> None:
        """Unset -> enabled (default)."""
        with patch.dict(os.environ, {}, clear=True):
            assert _baseline_enabled() is True

    def test_explicit_true(self) -> None:
        with patch.dict(os.environ, {"SUNABA_SECRETS_BASELINE": "true"}):
            assert _baseline_enabled() is True

    def test_false(self) -> None:
        with patch.dict(os.environ, {"SUNABA_SECRETS_BASELINE": "false"}):
            assert _baseline_enabled() is False

    def test_zero(self) -> None:
        with patch.dict(os.environ, {"SUNABA_SECRETS_BASELINE": "0"}):
            assert _baseline_enabled() is False


# ============================================================================
# check_override
# ============================================================================


class TestCheckOverride:
    """One-time in-memory override (baseline OFF path).

    ``check_override`` peeks without consuming.  ``consume_override``
    pops the flag (called after a successful push).
    """

    def test_no_override(self) -> None:
        """No override set -> False."""
        _OVERRIDE_MAP.clear()
        assert check_override("cid123456789") is False
        assert consume_override("cid123456789") is False

    def test_override_peek_and_consume(self) -> None:
        """Peek returns True but does NOT consume; consume pops once."""
        _OVERRIDE_MAP.clear()
        cid = "abc123def456"
        with _OVERRIDE_LOCK:
            _OVERRIDE_MAP[cid] = True
        assert check_override(cid) is True    # peek
        assert check_override(cid) is True    # still there
        assert consume_override(cid) is True  # consumed
        assert check_override(cid) is False   # gone
        assert consume_override(cid) is False # already consumed

    def test_override_different_container(self) -> None:
        """Override for container A does not affect container B."""
        _OVERRIDE_MAP.clear()
        with _OVERRIDE_LOCK:
            _OVERRIDE_MAP["aaa"] = True
        assert check_override("aaa") is True
        assert check_override("bbb") is False
        assert consume_override("bbb") is False


# ============================================================================
# _update_baseline
# ============================================================================


class TestUpdateBaseline:
    """Updating .secrets.baseline (baseline ON path)."""

    def test_update_creates_baseline(self) -> None:
        """When no baseline exists, creates one with merged results."""
        container = _make_container([
            # cat .secrets.baseline -> ec=1 (not found)
            (1, "", "No such file"),
            # gitleaks dir
            (_GITLEAKS_FINDINGS_EXIT,
             _make_finding_json("secret.py", 3, "private-key"), ""),
            # cat > .secrets.baseline (write)
            (0, "", ""),
        ])
        err, _ = _update_baseline(container, ["secret.py"], "/tmp/repo")
        assert err is None

    def test_update_merge_existing(self) -> None:
        """When baseline exists, merges new results with old."""
        old_baseline = json.dumps({
            "generated_at": "2026-01-01T00:00:00Z",
            "plugins_used": [],
            "results": {"old.py": []},
        })
        container = _make_container([
            (0, old_baseline, ""),                    # cat existing
            (_GITLEAKS_FINDINGS_EXIT,
             _make_finding_json("new.py", 1, "aws-secret-key"), ""),
            (0, "", ""),                               # write
        ])
        err, _ = _update_baseline(container, ["new.py"], "/tmp/repo")
        assert err is None

    def test_update_scan_fails(self) -> None:
        """When scan fails, returns error message."""
        container = _make_container([
            (1, "", "No such file"),
            (1, "", "gitleaks crashed"),
        ])
        err, _ = _update_baseline(container, ["bad.py"], "/tmp/repo")
        assert err is not None
        assert "failed" in err

    # -------------------------------------------------------------------
    # Baseline exclusion in _update_baseline (issue #703, criterion 4)
    # -------------------------------------------------------------------

    def test_baseline_excluded_from_update(self) -> None:
        """When the file list includes .secrets.baseline, it is excluded
        from the scan so its own hashed_secret values are not re-appended
        to the baseline.

        Criterion 4: secret_scan_override does not add the baseline's own
        stored hashes to the baseline.
        """
        # A baseline that *would* produce findings if scanned
        baseline_content = json.dumps({
            "generated_at": "2026-07-20T00:00:00Z",
            "plugins_used": [],
            "results": {
                ".secrets.baseline": [
                    {
                        "type": "Hex High Entropy String",
                        "filename": ".secrets.baseline",
                        "line_number": 10,
                        "hashed_secret": "a" * 40,
                        "is_verified": False,
                    },
                ],
            },
        })
        # The mock container has no "gitleaks dir" calls because
        # the baseline is the only file and gets excluded.
        # Only "cat .secrets.baseline" is called (ec=0, returns baseline_content).
        container = _make_container([
            # cat .secrets.baseline -> found
            (0, baseline_content, ""),
        ])
        # No gitleaks scan calls should be made since baseline is excluded
        err, _ = _update_baseline(
            container, [".secrets.baseline"], "/tmp/repo",
        )
        assert err is None, f"Expected success, got error: {err}"
        # Verify no scan was requested (only the cat call)
        scan_calls = [
            c for c in container.exec_run.call_args_list
            if "gitleaks dir" in str(c)
        ]
        assert len(scan_calls) == 0, (
            f"Expected no scan calls when only baseline is in file list, "
            f"got {len(scan_calls)}"
        )

    def test_baseline_excluded_from_update_with_real_files(self) -> None:
        """When files include both .secrets.baseline and real files, only
        the real files are scanned.  Overriding twice does not grow the
        baseline without bound (no self-referential ratchet).

        Criterion 4: Overriding twice in a row does not grow the baseline.
        """
        baseline_content = json.dumps({
            "generated_at": "2026-07-20T00:00:00Z",
            "plugins_used": [],
            "results": {"real.py": []},
        })
        finding_json = _make_finding_json("real.py", 1, "generic-api-key")
        container = _make_container([
            # cat .secrets.baseline -> found
            (0, baseline_content, ""),
            # gitleaks dir (only real.py, not .secrets.baseline)
            (_GITLEAKS_FINDINGS_EXIT, finding_json, ""),
            # write baseline
            (0, "", ""),
        ])
        err, _ = _update_baseline(
            container, ["real.py", ".secrets.baseline"], "/tmp/repo",
        )
        assert err is None

        # Verify the scan command ran with only real.py (not .secrets.baseline)
        scan_calls = [
            c for c in container.exec_run.call_args_list
            if "gitleaks dir" in str(c)
        ]
        assert len(scan_calls) == 1
        # scan_calls[0] is a call object; get its command list from kwargs
        call_obj = scan_calls[0]
        cmd_list = call_obj.kwargs.get("cmd", [])
        scan_cmd_str = " ".join(str(x) for x in cmd_list)
        assert "real.py" in scan_cmd_str
        assert ".secrets.baseline" not in scan_cmd_str


# ============================================================================


# ============================================================================
# secret_scan_override (MCP tool — Docker-dependent via _docker())
# ============================================================================


class TestSecretScanOverride:
    """secret_scan_override MCP tool — tested via mocked container."""

    @pytest.fixture(autouse=True)
    def _clear_overrides(self) -> None:
        _OVERRIDE_MAP.clear()
        _OVERRIDE_REGISTRY.clear()

    def test_container_not_found(self) -> None:
        """Unknown container_id → error message."""
        with patch("sunaba.tools.secret_scan._docker") as mock_docker:
            client = MagicMock()
            client.containers.get.side_effect = Exception("not found")
            mock_docker.return_value = client
            result = secret_scan_override("nonexistent1234")
            payload = json.loads(result)
            assert payload["status"] == "error"

    def test_baseline_off_sets_override(self) -> None:
        """Baseline OFF → in-memory override flag set."""
        container = _make_container([
            # gitleaks version
            (0, "8.30.1\n", ""),
        ])

        client = MagicMock()
        client.containers.get.return_value = container

        with (
            patch("sunaba.tools.secret_scan._docker", return_value=client),
            patch(
                "sunaba.tools.secret_scan._baseline_enabled",
                return_value=False,
            ),
            patch("sunaba.tools.secret_scan.record_boundary_crossing"),
            patch("sunaba.tools.secret_scan.record_tool_use"),
        ):
            result = secret_scan_override(
                "testcid123456", working_dir="/workspace",
                files=["secret.py", "config.py"],
            )
            payload = json.loads(result)
            assert payload["status"] == "ok"
            assert payload["action"] == "override_set"
            # In-memory flag should be set (peek)
            assert check_override("testcid123456") is True
            # Peek is non-destructive — still there
            assert check_override("testcid123456") is True
            # consume_override consumes it
            assert consume_override("testcid123456") is True
            # Gone after consumption
            assert check_override("testcid123456") is False

    def test_no_gitleaks_returns_error(self) -> None:
        """gitleaks not available → error from override too."""
        container = _make_container([
            # gitleaks version fails
            (1, "", "command not found"),
        ])

        client = MagicMock()
        client.containers.get.return_value = container

        with (
            patch("sunaba.tools.secret_scan._docker", return_value=client),
            patch("sunaba.tools.secret_scan.record_tool_use"),
            patch("sunaba.tools.vcs.gitroot.resolve_git_root",
                  return_value="/workspace"),
        ):
            result = secret_scan_override(
                "testcid123456", working_dir="/workspace",
                files=["secret.py"],
            )
            payload = json.loads(result)
            assert payload["status"] == "error"
            assert "not available" in payload["error"]

# ============================================================================
# Regression: exec_in_container must use real Container API surface
# ============================================================================


class TestExecInContainerAPISurface:
    """Verify that exec_in_container only calls methods from the real
    ``docker.models.containers.Container`` class API (acceptance criterion 4).

    These tests use ``create_autospec(Container)`` rather than checking
    ``hasattr``, so that exec_in_container is actually exercised against a
    mock with the **exact** interface of the real class.  If it ever calls
    ``exec_create`` / ``exec_start`` / ``exec_inspect``, the autospec raises
    ``AttributeError`` — a MagicMock fabricating those attributes cannot
    make the test pass.
    """

    def test_exec_run_is_called_through_autospec(self) -> None:
        """exec_in_container calls exec_run on an autospec(Container)."""
        from unittest.mock import create_autospec

        from docker.models.containers import Container

        # autospec creates a mock that only has Container's real methods
        mock = create_autospec(Container, instance=True)
        mock.exec_run.return_value = _ExecResult(0, (b"8.30.1", b""))

        ec, out, err = exec_in_container(mock, ["gitleaks", "version"])

        assert ec == 0
        assert out == "8.30.1"
        assert err == ""
        mock.exec_run.assert_called_once_with(
            cmd=["gitleaks", "version"],
            stdout=True,
            stderr=True,
            workdir=None,
            demux=True,
        )

    def test_non_demux_fallback_is_exercised(self) -> None:
        """When exec_run(demux=True) raises TypeError, the fallback without
        demux is used.  This covers the non-demux fallback path (finding 3)."""
        from unittest.mock import create_autospec

        from docker.models.containers import Container

        mock = create_autospec(Container, instance=True)
        # First call (demux=True) raises TypeError
        # Second call (no demux) succeeds with multiplexed output
        mock.exec_run.side_effect = [
            TypeError("demux not supported"),
            _ExecResult(0, b"8.30.1"),
        ]

        ec, out, err = exec_in_container(mock, ["gitleaks", "version"])

        assert ec == 0
        assert out == "8.30.1"
        assert err == ""  # non-demux: all output is stdout
        assert mock.exec_run.call_count == 2
        # Second call had no demux keyword
        _, second_kwargs = mock.exec_run.call_args_list[1]
        assert "demux" not in second_kwargs

# ============================================================================
# Scan invocation contract (issues #701, #703, #842)
# ============================================================================


class TestScanInvocationContract:
    """The gitleaks command line is part of the guard, not a detail.

    Three properties are asserted at both invocation sites (publish scan and
    baseline update):

    * ``--exit-code 99`` -- the default findings exit is 1, which gitleaks
      also returns for a fatal error.  Sharing one code makes a scan that
      never ran indistinguishable from an ordinary findings result.
    * ``--report-format json --report-path -`` -- the report is read from
      stdout; without it there is nothing to parse and the guard has only
      an exit code to go on.
    * no scanner-side suppression (``--baseline-path`` / ``.gitleaksignore``)
      -- that would move the suppression list inside the container, where the
      agent can write it (#708), and make running the scan the act of
      suppressing (#703).
    * gitleaks-native suppression is actively denied: ``--ignore-gitleaks-allow``
      neutralises inline ``gitleaks:allow`` comments, and
      ``--gitleaks-ignore-path`` points at a scanner-owned empty temp dir so
      the repo-root default (``.``) never picks up an agent-written
      ``.gitleaksignore``.  Both bypasses were verified live against the
      real 8.30.1 binary before these flags were added (#842 [high]).

    The predecessor of this class asserted ``--no-verify`` (#701).  gitleaks
    performs no verification at all, so that flag has no successor: the
    fail-open it defended against cannot occur.
    """

    @staticmethod
    def _scan_commands(calls) -> list[str]:
        return [
            " ".join(c.kwargs["cmd"])
            for c in calls
            if isinstance(c.kwargs.get("cmd"), list)
            and "gitleaks dir" in " ".join(str(x) for x in c.kwargs["cmd"])
        ]

    def _assert_contract(self, calls) -> None:
        scan_cmds = self._scan_commands(calls)
        assert scan_cmds, (
            "No exec_run call issued a gitleaks scan. "
            f"Calls: {[c.kwargs.get('cmd') for c in calls]}"
        )
        for cmd in scan_cmds:
            assert "--exit-code 99" in cmd, cmd
            assert "--report-format json" in cmd, cmd
            assert "--report-path -" in cmd, cmd
            assert "--baseline-path" not in cmd, cmd
            assert ".gitleaksignore" not in cmd, cmd
            assert "--ignore-gitleaks-allow" in cmd, cmd
            assert '--gitleaks-ignore-path "$ig"' in cmd, cmd

    def test_scan_command_contract(self) -> None:
        """run_secret_scan issues the contracted gitleaks command."""
        container = _make_container([
            (0, _dummy_version_output(), ""),        # version probe
            (0, _make_clean_scan_json(), ""),         # scan output
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            run_secret_scan(container, ["file.py"], "/tmp/repo")

        self._assert_contract(container.exec_run.call_args_list)

    def test_baseline_update_command_contract(self) -> None:
        """_update_baseline issues the same contracted command."""
        container = _make_container([
            # cat .secrets.baseline -> ec=1 (not found)
            (1, "", "No such file"),
            # gitleaks dir
            (0, _make_clean_scan_json(), ""),
            # write baseline
            (0, "", ""),
        ])
        _update_baseline(container, ["file.py"], "/tmp/repo")

        self._assert_contract(container.exec_run.call_args_list)


# ============================================================================
# AWS key pair detection (issue #701 regression guard)
# ============================================================================


def _make_aws_key_pair_json(filename: str) -> str:
    """Build a gitleaks report with an AWS key ID AND a secret key in the
    same file -- the scenario that was silently dropped by the previous
    scanner's verification fail-open (#701).

    All values are constructed at runtime so no literal credentials are
    committed.
    """
    return _make_report([
        (filename, 5, "aws-access-token", _fake_secret("no-real-access-key")),
        (filename, 6, "aws-secret-key", _fake_secret("no-real-secret-key")),
    ])


class TestAWSKeyPairDetection:
    """An AWS access key combined with a secret key must produce a blocking
    result.  gitleaks reports both matches independently and never calls out
    to STS, so the #701 collision has no way back in."""

    def test_aws_key_pair_findings_survive(self) -> None:
        """Two AWS findings (access key + secret key) both survive parsing."""
        report = _make_aws_key_pair_json("secrets.py")
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, report, ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=False,
        ):
            result = run_secret_scan(
                container, ["secrets.py"], "/tmp/repo",
            )
        assert result["secret_scan"] == "findings", (
            f"AWS key pair should produce findings, got: {result['secret_scan']}"
        )
        assert len(result["findings"]) == 2, (
            f"Expected 2 AWS findings, got {len(result.get('findings', []))}"
        )
        assert {f["hashed_secret"] for f in result["findings"]} == set(
            _hashes_in(report),
        )
        for f in result["findings"]:
            assert f["type"].startswith("aws-")
            assert f["file"] == "secrets.py"


# ============================================================================
# baseline_hashes parameter (host-side baseline, issue #708)
# ============================================================================


class TestBaselineHashesParameter:
    """Tests for the ``baseline_hashes`` keyword parameter.

    When ``baseline_hashes`` is provided (not ``None``), it is used directly
    for subtraction instead of reading the baseline from the container.
    This is the mechanism that fixes the #708 bypass.
    """

    def test_host_side_hashes_suppress_known_finding(self) -> None:
        """Host-side hashes set containing the finding hash -> suppressed."""
        known_secret = _fake_secret("known-secret-for-hashes")
        known_hash = _sha1(known_secret)

        # Scan has one finding with a known hash
        scan_json = _make_report([
            ("safe.py", 10, "generic-api-key", known_secret),
        ])
        container = _make_container([
            (0, _dummy_version_output(), ""),       # check available
            (_GITLEAKS_FINDINGS_EXIT, scan_json, ""),   # scan output
            # NOTE: no cat .secrets.baseline call because
            # baseline_hashes is provided (not None).
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["safe.py"], "/tmp/repo",
                baseline_hashes={known_hash},
            )
        assert result["secret_scan"] == "clean", (
            "Known hash in baseline_hashes should be suppressed; "
            f"got {result['secret_scan']}"
        )

    def test_host_side_empty_hashes_no_suppression(self) -> None:
        """Empty set from host-side = no suppression, all findings reported."""
        scan_json = _make_report([
            ("keys.py", 5, "aws-secret-key", _fake_secret("unmatched-secret")),
        ])
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, scan_json, ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["keys.py"], "/tmp/repo",
                baseline_hashes=set(),  # empty set = no known hashes
            )
        assert result["secret_scan"] == "findings"
        assert len(result["findings"]) == 1

    def test_host_side_hashes_ignore_container_baseline(self) -> None:
        """When baseline_hashes is provided, container's baseline is NOT read.

        This is the core of the #708 fix: even if the container's
        .secrets.baseline contains the finding hash, an empty host-side
        set means no suppression.  The mock container would return the
        hash if read, but since baseline_hashes is provided, the cat
        command is never issued.
        """
        scan_json = _make_report([
            ("keys.py", 5, "generic-api-key", _fake_secret("tampered-secret")),
        ])
        # The mock container has the baseline content ready, but since
        # baseline_hashes is provided (empty set = no known hashes),
        # the cat command is NEVER called.
        container = _make_container([
            (0, _dummy_version_output(), ""),       # check available
            (_GITLEAKS_FINDINGS_EXIT, scan_json, ""),   # finding matches
            # NOTE: no cat .secrets.baseline at all
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["keys.py"], "/tmp/repo",
                baseline_hashes=set(),  # empty = no known hashes
            )
        # Finding is NOT suppressed because host-side set is empty
        assert result["secret_scan"] == "findings", (
            "Container baseline must be ignored when baseline_hashes is set; "
            f"got {result['secret_scan']}"
        )
        assert len(result["findings"]) == 1


# ============================================================================
# Override registry (issue #722)
# ============================================================================


class TestOverrideRegistry:
    """Host-side in-memory override registry (hashed-secret-level)."""

    def test_registry_add_and_retrieve(self) -> None:
        """Hashes added to the registry are retrievable."""
        _OVERRIDE_REGISTRY.clear()
        hashes = {"abc123", "def456"}
        _add_to_override_registry("testcid123456", hashes)
        result = get_override_registry_hashes("testcid123456")
        assert result == hashes

    def test_registry_accumulates(self) -> None:
        """Multiple adds accumulate hashes for the same container."""
        _OVERRIDE_REGISTRY.clear()
        _add_to_override_registry("testcid123456", {"hash1"})
        _add_to_override_registry("testcid123456", {"hash2"})
        result = get_override_registry_hashes("testcid123456")
        assert result == {"hash1", "hash2"}

    def test_registry_isolation(self) -> None:
        """Different containers have independent registries."""
        _OVERRIDE_REGISTRY.clear()
        _add_to_override_registry("containerA", {"hashA"})
        _add_to_override_registry("containerB", {"hashB"})
        assert get_override_registry_hashes("containerA") == {"hashA"}
        assert get_override_registry_hashes("containerB") == {"hashB"}
        # container C sees nothing
        assert get_override_registry_hashes("containerC") == set()

    def test_registry_returns_copy(self) -> None:
        """get_override_registry_hashes returns a mutable copy."""
        _OVERRIDE_REGISTRY.clear()
        _add_to_override_registry("testcid123456", {"hashX"})
        result = get_override_registry_hashes("testcid123456")
        result.add("hacker")  # mutate the copy
        # Original registry unchanged
        original = get_override_registry_hashes("testcid123456")
        assert "hacker" not in original
        assert original == {"hashX"}


# ============================================================================
# Registry + run_secret_scan integration (issue #722)
# ============================================================================


class TestRegistryScanIntegration:
    """Registry hashes suppress findings in run_secret_scan."""

    def test_registry_suppresses_findings(self) -> None:
        """A finding whose hash is in the registry is suppressed."""
        _OVERRIDE_REGISTRY.clear()
        finding_json = _make_finding_json("secret.py", 3, "private-key")
        finding_hash = _hashes_in(finding_json)[0]

        _add_to_override_registry("testcid123456", {finding_hash})

        container = _make_container([
            (0, _dummy_version_output(), ""),     # version probe
            (_GITLEAKS_FINDINGS_EXIT, finding_json, ""),   # scan output
        ])
        # Simulate publish: baseline_hashes empty set + registry populated
        # via the union that publish does.
        registry_hashes = get_override_registry_hashes("testcid123456")
        baseline_hashes: set[str] = set() | registry_hashes

        result = run_secret_scan(
            container, ["secret.py"], "/tmp/repo",
            baseline_hashes=baseline_hashes,
        )
        assert result["secret_scan"] == "clean", (
            f"Finding should be suppressed by registry hash; "
            f"got {result.get('secret_scan')}: {result.get('scan_summary')}"
        )

    def test_registry_unknown_hash_not_suppressed(self) -> None:
        """A finding whose hash is NOT in the registry is reported."""
        _OVERRIDE_REGISTRY.clear()
        finding_json = _make_finding_json("secret.py", 3, "private-key")

        # Register a DIFFERENT hash
        _add_to_override_registry("testcid123456", {"unrelated-hash"})

        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, finding_json, ""),
        ])
        registry_hashes = get_override_registry_hashes("testcid123456")
        baseline_hashes: set[str] = set() | registry_hashes

        result = run_secret_scan(
            container, ["secret.py"], "/tmp/repo",
            baseline_hashes=baseline_hashes,
        )
        assert result["secret_scan"] == "findings", (
            f"Unregistered finding should NOT be suppressed; "
            f"got {result.get('secret_scan')}"
        )

    def test_registry_different_container_not_suppressed(self) -> None:
        """Registry for container A does not affect scan with container B's
        published baseline_hashes."""
        _OVERRIDE_REGISTRY.clear()
        finding_json = _make_finding_json("secret.py", 3, "private-key")
        finding_hash = _hashes_in(finding_json)[0]

        # Register for container A
        _add_to_override_registry("containerA", {finding_hash})

        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, finding_json, ""),
        ])
        # Publish from container B — gets container B's registry
        registry_hashes = get_override_registry_hashes("containerB")
        baseline_hashes: set[str] = set() | registry_hashes

        result = run_secret_scan(
            container, ["secret.py"], "/tmp/repo",
            baseline_hashes=baseline_hashes,
        )
        # containerB's registry is empty, so finding is NOT suppressed
        assert result["secret_scan"] == "findings", (
            f"Container B should not inherit container A's override; "
            f"got {result.get('secret_scan')}"
        )

    def test_registry_with_remote_baseline_both_suppress(self) -> None:
        """Registry + remote baseline both suppress their respective hashes."""
        _OVERRIDE_REGISTRY.clear()
        secret1 = _fake_secret("registry-secret-one")
        secret2 = _fake_secret("registry-secret-two")
        hash1 = _sha1(secret1)
        hash2 = _sha1(secret2)

        # hash1 from remote baseline, hash2 from registry
        remote_hashes = {hash1}
        _add_to_override_registry("testcid123456", {hash2})

        # Combined scan output with both findings
        combined_json = _make_report([
            ("file1.py", 1, "private-key", secret1),
            ("file2.py", 2, "aws-secret-key", secret2),
        ])

        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, combined_json, ""),
        ])

        registry_hashes = get_override_registry_hashes("testcid123456")
        baseline_hashes = remote_hashes | registry_hashes

        result = run_secret_scan(
            container, ["file1.py", "file2.py"], "/tmp/repo",
            baseline_hashes=baseline_hashes,
        )
        # Both should be suppressed → clean
        assert result["secret_scan"] == "clean", (
            f"Both hashes should be suppressed (remote + registry); "
            f"got {result.get('secret_scan')}: {result.get('scan_summary')}"
        )


# ============================================================================
# Override message content (issue #722)
# ============================================================================


class TestOverrideMessage:
    """secret_scan_override result message describes both mechanisms."""

    def test_baseline_on_message_describes_both_mechanisms(self) -> None:
        """Baseline ON → message mentions immediate (registry) AND durable
        (baseline) mechanisms."""
        container = _make_container([
            # gitleaks version
            (0, "8.30.1\n", ""),
            # cat .secrets.baseline → not found
            (1, "", "No such file"),
            # gitleaks dir
            (_GITLEAKS_FINDINGS_EXIT,
             _make_finding_json("secret.py", 3, "private-key"), ""),
            # write baseline
            (0, "", ""),
        ])

        client = MagicMock()
        client.containers.get.return_value = container

        with (
            patch("sunaba.tools.secret_scan._docker", return_value=client),
            patch(
                "sunaba.tools.secret_scan._baseline_enabled",
                return_value=True,
            ),
            patch("sunaba.tools.secret_scan.record_boundary_crossing"),
            patch("sunaba.tools.secret_scan.record_tool_use"),
        ):
            result = secret_scan_override(
                "testcid123456", working_dir="/workspace",
                files=["secret.py"],
            )
            payload = json.loads(result)
            assert payload["status"] == "ok"
            assert payload["action"] == "baseline_updated"

            detail = payload["detail"]
            # Must mention BOTH mechanisms
            assert "Override registered" in detail, (
                f"Expected 'Override registered' in message; got: {detail}"
            )
            assert "this container" in detail.lower(), (
                f"Expected mention of THIS container; got: {detail}"
            )
            assert "immediate" in detail.lower(), (
                f"Expected 'immediate' mechanism description; got: {detail}"
            )
            assert "durable" in detail.lower(), (
                f"Expected 'durable' mechanism description; got: {detail}"
            )
            assert ".secrets.baseline" in detail, (
                f"Expected mention of .secrets.baseline; got: {detail}"
            )
            assert "merge" in detail.lower(), (
                f"Expected mention of merging to base branch; got: {detail}"
            )

            # Registry should be populated
            registry = get_override_registry_hashes("testcid123456")
            assert len(registry) > 0, (
                "Registry should have hashes after override; got empty"
            )

    def test_baseline_off_message_unchanged(self) -> None:
        """Baseline OFF → message keeps the in-memory override wording."""
        container = _make_container([
            (0, "8.30.1\n", ""),
        ])

        client = MagicMock()
        client.containers.get.return_value = container

        with (
            patch("sunaba.tools.secret_scan._docker", return_value=client),
            patch(
                "sunaba.tools.secret_scan._baseline_enabled",
                return_value=False,
            ),
            patch("sunaba.tools.secret_scan.record_boundary_crossing"),
            patch("sunaba.tools.secret_scan.record_tool_use"),
        ):
            result = secret_scan_override(
                "testcid123456", working_dir="/workspace",
                files=["secret.py"],
            )
            payload = json.loads(result)
            assert payload["status"] == "ok"
            assert payload["action"] == "override_set"
            assert "in-memory" in payload["detail"]


# ---------------------------------------------------------------------------
# should_consume_override — stale one-time flag must not survive a
# registry/baseline-suppressed publish (#722 review, [high])
# ---------------------------------------------------------------------------


class TestShouldConsumeOverride:
    """Truth table for the flag-consumption decision after a successful push."""

    def test_findings_state_consumes(self) -> None:
        from sunaba.tools.secret_scan import should_consume_override
        assert should_consume_override("findings", 0) is True

    def test_error_state_consumes(self) -> None:
        from sunaba.tools.secret_scan import should_consume_override
        assert should_consume_override("error", 0) is True

    def test_genuinely_clean_keeps_flag(self) -> None:
        from sunaba.tools.secret_scan import should_consume_override
        assert should_consume_override("clean", 0) is False

    def test_skipped_keeps_flag(self) -> None:
        from sunaba.tools.secret_scan import should_consume_override
        assert should_consume_override("skipped", 0) is False

    def test_suppressed_clean_consumes(self) -> None:
        """A clean that exists only because suppressions fired = override use."""
        from sunaba.tools.secret_scan import should_consume_override
        assert should_consume_override("clean", 2) is True


class TestSuppressedCountReporting:
    """run_secret_scan reports how many findings suppression removed."""

    def _scan_json_with_secret(self, secret: str) -> str:
        return _make_report([("safe.py", 10, "generic-api-key", secret)])

    def test_suppressed_count_when_hash_known(self) -> None:
        known_secret = _fake_secret("known-secret-for-count")
        known_hash = _sha1(known_secret)
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT, self._scan_json_with_secret(known_secret), ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["safe.py"], "/tmp/repo",
                baseline_hashes={known_hash},
            )
        assert result["secret_scan_state"] == "clean"
        assert result["suppressed_count"] == 1
        assert "suppressed" in result["scan_summary"]

    def test_suppressed_count_zero_when_nothing_known(self) -> None:
        unknown_secret = _fake_secret("unknown-secret-for-count")
        container = _make_container([
            (0, _dummy_version_output(), ""),
            (_GITLEAKS_FINDINGS_EXIT,
             self._scan_json_with_secret(unknown_secret), ""),
        ])
        with patch(
            "sunaba.tools.secret_scan._baseline_enabled",
            return_value=True,
        ):
            result = run_secret_scan(
                container, ["safe.py"], "/tmp/repo",
                baseline_hashes=set(),
            )
        assert result["secret_scan_state"] == "findings"
        assert result["suppressed_count"] == 0
