"""Tests for scripts/check_patch_sabotage.py (Issue #824).

The sabotage checker verifies that patch targets actually bind at runtime by
temporarily mutating the definition and running only the patcher tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_patch_sabotage.py"


def _run_sabotage(
    root: Path,
    *,
    files: str | None = None,
    base: str | None = None,
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """Run the sabotage checker in a subprocess, returning the completed process."""
    cmd = [sys.executable, str(_SCRIPT), "--root", str(root), "--json"]
    if files is not None:
        cmd.extend(["--files", files])
    if base is not None:
        cmd.extend(["--base", base])
    if timeout is not None:
        cmd.extend(["--timeout-seconds", str(timeout)])
    return subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=120)


def _parse(proc: subprocess.CompletedProcess) -> dict:
    """Parse JSON stdout from the checker subprocess."""
    return json.loads(proc.stdout)


# ── Helper: mini-project fixture builder ─────────────────────────────────────

def _write_py(path: Path, content: str) -> None:
    """Write a .py file with proper dedented content."""
    # Dedent common leading whitespace
    lines = content.split("\n")
    if lines and lines[0] == "":
        lines = lines[1:]  # strip leading blank line
    # Find minimum indent (ignore empty lines)
    min_indent = min(
        (len(line) - len(line.lstrip())) for line in lines if line.strip()
    )
    dedented = "\n".join(
        line[min_indent:] if line.strip() else "" for line in lines
    )
    path.write_text(dedented + "\n", encoding="utf-8")


def _make_tests_dir(root: Path) -> Path:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    return tests


# ── Test 1: Clean case — patch binds ────────────────────────────────────────

class TestCleanPatchBinds:
    """When the patch target actually binds, no defect is reported."""

    def test_clean_patch(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_clean.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=99)
            def test_clean_patch(_mock_compute):
                assert impl.compute() == 99
        """)

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_clean.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}, stdout={proc.stdout}"
        assert data["defects"] == 0
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        assert targets["impl.compute"]["defect"] is False
        assert targets["impl.compute"]["skipped"] is False


# ── Test 2: Sabotage case — stale re-export ─────────────────────────────────

class TestSabotageCase:
    """A patch on a re-export does not bind when the consumer imported directly."""

    def test_stale_re_export_detected(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        _write_py(tmp_path / "wrapper.py", """
            from impl import compute
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_broken.py", """
            from unittest.mock import patch
            from impl import compute

            @patch("wrapper.compute")
            def test_broken_patch(mock_compute):
                result = compute()
                assert result == 42
        """)

        proc = _run_sabotage(
            tmp_path, files="impl.py,wrapper.py,tests/test_broken.py"
        )
        data = _parse(proc)

        assert proc.returncode != 0, "Expected non-zero exit for defect"
        assert data["defects"] == 1
        targets = {t["target"]: t for t in data["targets"]}
        assert "wrapper.compute" in targets
        assert targets["wrapper.compute"]["defect"] is True


# ── Test 3: Subject exclusion ────────────────────────────────────────────────

class TestSubjectExclusion:
    """Tests that call the symbol directly are listed as excluded."""

    def test_subject_excluded(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_subject.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=42)
            def test_patcher(mock_compute):
                assert impl.compute() == 42

            def test_subject():
                # Calls compute directly — should be excluded
                assert impl.compute() == 42
        """)

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_subject.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        # test_subject should be in the excluded list
        assert "test_subject" in t["subject_tests"], (
            f"Expected test_subject in subjects, got patchers={t['patcher_tests']} "
            f"subjects={t['subject_tests']}"
        )
        assert "test_patcher" in t["patcher_tests"]


# ── Test 4: Both patcher and subject ─────────────────────────────────────────

class TestBothPatcherAndSubject:
    """A test that patches a symbol and also calls it is classified as patcher."""

    def test_both_classified_as_patcher(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_both.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=99)
            def test_both(mock_compute):
                # Both patches and calls the symbol
                assert impl.compute() == 99
        """)

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_both.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        # test_both should be a patcher, not a subject
        assert "test_both" in t["patcher_tests"]
        assert "test_both" not in t["subject_tests"]


# ── Test 5: Non-function target ──────────────────────────────────────────────

class TestNonFunctionTarget:
    """Non-function targets (constants, classes) are skipped."""

    def test_constant_target_skipped(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            CONSTANT = 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_constant.py", """
            from unittest.mock import patch

            @patch("impl.CONSTANT", 99)
            def test_constant(_mock):
                import impl
                assert impl.CONSTANT == 99
        """)

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_constant.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.CONSTANT" in targets
        t = targets["impl.CONSTANT"]
        assert t["skipped"] is True
        assert t["skipped_reason"] is not None
        assert "not a patchable callable" in t["skipped_reason"]
        assert t["defect"] is False


# ── Test 6: No relevant changes ──────────────────────────────────────────────

class TestNoRelevantChanges:
    """When there are no relevant changes, nothing is checked."""

    def test_base_head_nothing_to_check(self, tmp_path: Path) -> None:
        """--base HEAD on a committed git fixture returns 'nothing to check'."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_x.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=99)
            def test_x():
                assert impl.compute() == 99
        """)
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
             "commit", "-qm", "init"],
            cwd=tmp_path, check=True,
        )

        proc = _run_sabotage(tmp_path, base="HEAD")
        assert proc.returncode == 0, f"stderr={proc.stderr}"
        data = _parse(proc)
        assert data["defects"] == 0
        assert data["targets"] == []

    def test_git_failure_is_not_green(self, tmp_path: Path) -> None:
        """A root where git cannot produce a change set fails closed (exit 2)."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)

        proc = _run_sabotage(tmp_path, base="HEAD")
        assert proc.returncode == 2, (
            f"Expected fail-closed exit 2 on a non-git root, got "
            f"{proc.returncode}\nstderr={proc.stderr}\nstdout={proc.stdout}"
        )
        assert "refusing to report green" in proc.stderr

    def test_files_nothing_relevant(self, tmp_path: Path) -> None:
        """--files with only unrelated files returns 'nothing to check'."""
        _write_py(tmp_path / "other.py", """
            x = 1
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_x.py", """
            from unittest.mock import patch
            import impl_fake

            @patch("impl_fake.compute", return_value=99)
            def test_x():
                pass
        """)

        proc = _run_sabotage(tmp_path, files="other.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        assert data["targets"] == []


# ── Test 7: Restoration ──────────────────────────────────────────────────────

class TestRestoration:
    """After every run, mutated files are byte-identical to their original."""

    def test_restoration_after_clean(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_clean.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=99)
            def test_clean(_mock_compute):
                assert impl.compute() == 99
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_clean.py")
        assert proc.returncode == 0

        assert impl_file.read_bytes() == original, (
            "impl.py should be byte-identical to original after clean run"
        )

    def test_restoration_after_defect(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        _write_py(tmp_path / "wrapper.py", """
            from impl import compute
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_broken.py", """
            from unittest.mock import patch
            from impl import compute

            @patch("wrapper.compute")
            def test_broken(mock_compute):
                result = compute()
                assert result == 42
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(
            tmp_path, files="impl.py,wrapper.py,tests/test_broken.py"
        )
        # Expect defect (non-zero exit)
        assert proc.returncode != 0

        assert impl_file.read_bytes() == original, (
            "impl.py should be byte-identical to original after defect run"
        )

    def test_restoration_after_pytest_failure(self, tmp_path: Path) -> None:
        """Even when pytest itself crashes (bad test), the file is restored."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        # This test will fail with an import error (references nonexistent module)
        _write_py(tests / "test_broken.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=99)
            def test_bad(_mock_compute):
                import nonexistent_module
                assert impl.compute() == 99
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        _run_sabotage(tmp_path, files="impl.py,tests/test_broken.py")
        # Test might fail for import error, but restoration must still happen
        assert impl_file.read_bytes() == original, (
            "impl.py should be byte-identical to original, even after test failure"
        )


# ── Test 8: Real repo --base HEAD ────────────────────────────────────────────

class TestRealRepoBaseHead:
    """Running the checker against a disposable clean checkout of HEAD."""

    def test_base_head_on_real_repo(self, tmp_path: Path) -> None:
        """The checker runs against a disposable checkout of HEAD, never the
        live working tree.

        The checker writes a sabotage raise into the target's source file
        while it runs, and a subprocess timeout kill is SIGKILL, which
        bypasses the script's finally-restore.  Running it against the live
        tree therefore risks leaving a sabotage raise behind whenever the
        developer has uncommitted changes.  A disposable checkout of HEAD is
        clean by construction (empty change set -> deterministic and fast) and
        is removed afterwards, so the live working tree is never depended on
        or damaged.
        """
        repo_root = Path(__file__).resolve().parent.parent
        copy = tmp_path / "checkout"
        if shutil.which("git") is None:
            # Skip before the try: the cleanup below shells out to git too, and
            # a FileNotFoundError raised there would replace this skip.
            pytest.skip("git is not available; cannot create a disposable checkout")
        try:
            try:
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(copy), "HEAD"],
                    capture_output=True, text=True, cwd=repo_root, timeout=60,
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError,
                    subprocess.TimeoutExpired) as exc:
                pytest.skip(
                    f"cannot create disposable checkout of HEAD: {exc}"
                )
            # Run the copy's own script with --root pointing at the copy:
            # ensure_src_importable() derives src/ from the script's
            # __file__, so the copy's script is what makes target definitions
            # resolve inside the copy rather than in the live tree.
            script = copy / "scripts" / "check_patch_sabotage.py"
            if not script.is_file():
                pytest.skip(
                    "disposable checkout has no scripts/check_patch_sabotage.py"
                )
            proc = subprocess.run(
                [sys.executable, str(script), "--root", str(copy),
                 "--base", "HEAD", "--json"],
                capture_output=True, text=True, cwd=copy, timeout=60,
            )
            assert proc.returncode == 0, (
                f"Expected exit 0, got {proc.returncode}\n"
                f"stderr={proc.stderr}\nstdout={proc.stdout}"
            )
            data = json.loads(proc.stdout)
            assert data["defects"] == 0, (
                f"Expected 0 defects, got {data['defects']}\n"
                f"stderr={proc.stderr}\nstdout={proc.stdout}"
            )
            assert data["targets"] == [], (
                f"Expected an empty change set on a clean HEAD checkout, got "
                f"{len(data['targets'])} target(s)\n"
                f"stderr={proc.stderr}\nstdout={proc.stdout}"
            )
        finally:
            # Remove the disposable checkout and any registration it left in
            # the source repo's .git -- even when the assertions above fail.
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(copy)],
                capture_output=True, text=True, cwd=repo_root,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                capture_output=True, text=True, cwd=repo_root,
            )


# ── Test 9: Module-level (fixture) patches ───────────────────────────────────

class TestModuleLevelPatch:
    """Patches at module level (fixtures) must never run subject tests."""

    def test_autouse_fixture_with_patcher_runs_only_patcher(
        self, tmp_path: Path
    ) -> None:
        """A module-level fixture plus a test-level patcher runs the patcher by
        nodeid; the subject is excluded."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_auto.py", """
            import pytest
            from unittest.mock import patch
            import impl

            @pytest.fixture(autouse=True)
            def _autouse_compute():
                with patch("impl.compute", return_value=99):
                    yield

            @patch("impl.compute", return_value=99)
            def test_patcher(_mock_compute):
                assert impl.compute() == 99

            def test_subject():
                assert impl.compute() == 99
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_auto.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        assert t["skipped"] is False
        assert t["module_level"] is True
        assert "test_patcher" in t["patcher_tests"]
        assert "test_subject" in t["subject_tests"]
        assert impl_file.read_bytes() == original, (
            "impl.py should be byte-identical after module-level run"
        )

    def test_fixture_only_patch_skips_subjects_not_false_defect(
        self, tmp_path: Path
    ) -> None:
        """Regression: a fixture-only patch must not run the file wholesale.

        The subject asserts a value the fixture's mock does not return, so
        running it under mutation fails for its own reasons (a false-positive
        defect, error does not mention sabotage).  The checker must skip the
        target instead of reporting a defect.
        """
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_auto.py", """
            import pytest
            from unittest.mock import patch
            import impl

            @pytest.fixture(autouse=True)
            def _autouse_compute():
                with patch("impl.compute", return_value=99):
                    yield

            def test_subject():
                assert impl.compute() == 42
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_auto.py")
        data = _parse(proc)

        assert proc.returncode == 0, (
            f"Expected exit 0 (no false defect), stderr={proc.stderr}"
        )
        assert data["defects"] == 0
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        assert t["skipped"] is True
        assert t["module_level"] is True
        assert "module level" in t["skipped_reason"]
        assert "test_subject" in t["subject_tests"]
        assert impl_file.read_bytes() == original

    def test_module_level_patch_in_conftest(self, tmp_path: Path) -> None:
        """An autouse fixture in conftest.py is a module-level patch; patcher
        nodeids come only from the files that declare them (no bogus nodeids
        for conftest itself)."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "conftest.py", """
            import pytest
            from unittest.mock import patch
            import impl

            @pytest.fixture(autouse=True)
            def _autouse_compute():
                with patch("impl.compute", return_value=99):
                    yield
        """)
        _write_py(tests / "test_x.py", """
            from unittest.mock import patch
            import impl

            @patch("impl.compute", return_value=99)
            def test_patcher(_mock_compute):
                assert impl.compute() == 99

            def test_subject():
                assert impl.compute() == 99
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(tmp_path, files="impl.py,tests/conftest.py,tests/test_x.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        assert t["module_level"] is True
        assert "test_patcher" in t["patcher_tests"]
        assert "test_subject" in t["subject_tests"]
        assert impl_file.read_bytes() == original


# ── Test 10: monkeypatch.setattr targets ─────────────────────────────────────

class TestMonkeypatchSetattr:
    """monkeypatch.setattr("dotted.path", ...) is treated as a patch target."""

    def test_clean_binding(self, tmp_path: Path) -> None:
        """An in-function monkeypatch.setattr that binds reports no defect."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_monkey.py", """
            import impl

            def test_monkeypatch_binds(monkeypatch):
                monkeypatch.setattr("impl.compute", lambda: 99)
                assert impl.compute() == 99
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_monkey.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        assert "test_monkeypatch_binds" in t["patcher_tests"]
        assert impl_file.read_bytes() == original

    def test_stale_reexport_detected(self, tmp_path: Path) -> None:
        """monkeypatch.setattr on a stale re-export reports a defect."""
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        _write_py(tmp_path / "wrapper.py", """
            from impl import compute
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_monkey.py", """
            from impl import compute

            def test_broken(monkeypatch):
                monkeypatch.setattr("wrapper.compute", lambda: 99)
                assert compute() == 42
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(
            tmp_path, files="impl.py,wrapper.py,tests/test_monkey.py"
        )
        data = _parse(proc)

        assert proc.returncode != 0, "Expected non-zero exit for defect"
        assert data["defects"] == 1
        targets = {t["target"]: t for t in data["targets"]}
        assert "wrapper.compute" in targets
        assert targets["wrapper.compute"]["defect"] is True
        assert impl_file.read_bytes() == original


# ── Test 11: as-aliased subject imports ──────────────────────────────────────

class TestAliasedSubjectImport:
    """Subjects imported with an ``as`` alias are still detected and excluded."""

    def test_aliased_subject_detected(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_alias.py", """
            from unittest.mock import patch
            from impl import compute as calc
            import impl

            @patch("impl.compute", return_value=99)
            def test_patcher(_mock_compute):
                assert impl.compute() == 99

            def test_aliased_subject():
                assert calc() == 42
        """)

        impl_file = tmp_path / "impl.py"
        original = impl_file.read_bytes()

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_alias.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        assert "impl.compute" in targets
        t = targets["impl.compute"]
        assert t["defect"] is False
        assert "test_patcher" in t["patcher_tests"]
        assert "test_aliased_subject" in t["subject_tests"], (
            f"Expected aliased subject in subjects, got patchers={t['patcher_tests']} "
            f"subjects={t['subject_tests']}"
        )
        assert impl_file.read_bytes() == original


# ── Test 12: bare references are not subjects ────────────────────────────────

class TestReferenceOnlyNotSubject:
    """A test that references the symbol without calling it is not a subject."""

    def test_reference_only_not_excluded(self, tmp_path: Path) -> None:
        _write_py(tmp_path / "impl.py", """
            def compute():
                return 42
        """)
        tests = _make_tests_dir(tmp_path)
        _write_py(tests / "test_ref.py", """
            from unittest.mock import patch
            from impl import compute
            import impl

            @patch("impl.compute", return_value=99)
            def test_patcher(_mock_compute):
                assert impl.compute() == 99

            def test_reference_only():
                # References the symbol but never calls it — the sabotage
                # raise is unreachable, so this is not a subject.
                assert compute is not None
        """)

        proc = _run_sabotage(tmp_path, files="impl.py,tests/test_ref.py")
        data = _parse(proc)

        assert proc.returncode == 0, f"stderr={proc.stderr}"
        targets = {t["target"]: t for t in data["targets"]}
        t = targets["impl.compute"]
        assert t["defect"] is False
        assert "test_patcher" in t["patcher_tests"]
        assert "test_reference_only" not in t["subject_tests"], (
            f"Bare reference must not be classified as subject, got "
            f"subjects={t['subject_tests']}"
        )
