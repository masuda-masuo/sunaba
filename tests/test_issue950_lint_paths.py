"""Acceptance regressions for #950 single-file lint path handling."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from sunaba.edit_verify import lint_file
from sunaba.edit_verify.paths import ScopeWorkdir, _determine_scope
from sunaba.verify_state import record_environment_state


@pytest.fixture(autouse=True)
def _isolate_environment_state():
    with (
        patch("sunaba.verify_state._environment_state", {}),
        patch("sunaba.verify_state._verify_map", {}),
    ):
        yield


class _SubprocessContainer:
    """Run Docker exec argv locally, honoring the container WORKDIR and workdir."""

    def __init__(self, root: Path, container_id: str):
        self.id = container_id
        self.root = root
        self.attrs = {"Config": {"WorkingDir": str(root)}}
        self.calls: list[tuple[list[str], Path]] = []

    def exec_run(self, command, **kwargs):
        argv = list(command) if isinstance(command, (list, tuple)) else [command]
        cwd = Path(kwargs.get("workdir") or self.root)
        if not cwd.is_absolute():
            cwd = self.root / cwd
        self.calls.append((argv, cwd))
        result = subprocess.run(  # noqa: S603
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
        return result.returncode, (result.stdout, result.stderr)


def _client(root: Path, container_id: str):
    container = _SubprocessContainer(root, container_id)
    client = MagicMock()
    client.containers.get.return_value = container
    return client, container


def _ruff():
    executable = shutil.which("ruff")
    assert executable, "actual Ruff executable must be available for this regression"
    return executable


def test_relative_tests_file_reports_ruff_f401_without_duplicated_path(tmp_path):
    root = tmp_path / "relative-project"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text("import os\n", encoding="utf-8")
    client, _ = _client(root, "issue950-relative")

    path = "tests/test_x.py"
    scope = _determine_scope(path)
    assert tuple(scope) == ("tests", "tests")

    findings = lint_file(client, "issue950-relative", path, scope_workdir=scope)

    assert any(item["rule"] == "F401" for item in findings), findings
    f401 = next(item for item in findings if item["rule"] == "F401")
    assert {"file", "line", "rule", "message"} <= set(f401)
    assert not any("tests/tests/test_x.py" in item["message"] for item in findings), findings
    assert not any(item["rule"] == "error" for item in findings), findings


def test_absolute_non_src_uv_file_uses_project_ruff_from_project_root(tmp_path):
    root = tmp_path / "uv project with spaces"
    tests = root / "tests"
    tests.mkdir(parents=True)
    target = tests / "test_x.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    project_ruff = root / ".venv" / "bin" / "ruff"
    project_ruff.parent.mkdir(parents=True)
    project_ruff.symlink_to(_ruff())

    cid = "issue950-uv-ready"
    record_environment_state(cid, "ready")
    client, container = _client(root, cid)

    scope = _determine_scope(str(target))
    assert tuple(scope) == (str(tests), str(tests))

    findings = lint_file(client, cid, str(target), scope_workdir=scope)

    assert findings == []
    lint_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if any("ruff" in part and re.search(r"\bcheck\b", part) for part in argv)
    ]
    assert lint_calls
    assert all(str(project_ruff) in " ".join(argv) for argv, _ in lint_calls), lint_calls
    assert all(cwd == root for _, cwd in lint_calls), lint_calls


def test_absolute_non_src_uv_file_blocks_when_project_environment_is_unprepared(tmp_path):
    root = tmp_path / "unprepared-project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    target = tests / "test_x.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='0'\n", encoding="utf-8")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    cid = "issue950-uv-unprepared"
    client, container = _client(root, cid)

    findings = lint_file(
        client, cid, str(target), scope_workdir=_determine_scope(str(target))
    )

    assert len(findings) == 1
    assert findings[0]["rule"] == "error"
    assert "uv environment not_prepared" in findings[0]["message"]
    assert not any("ruff check" in part for argv, _ in container.calls for part in argv)


def test_nested_relative_uv_file_blocks_when_project_environment_is_unprepared(tmp_path):
    base = tmp_path / "X"
    root = base / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    target = tests / "test_x.py"
    target.write_text("value = 1\n", encoding="utf-8")
    cid = "issue950-nested-relative-unprepared"
    client, container = _client(base, cid)

    relative_path = "repo/tests/test_x.py"
    findings = lint_file(
        client, cid, relative_path,
        scope_workdir=_determine_scope(relative_path),
    )

    assert len(findings) == 1
    assert findings[0]["rule"] == "error"
    assert "uv environment not_prepared" in findings[0]["message"]
    assert not any(
        "ruff" in part and re.search(r"\bcheck\b", part)
        for argv, _ in container.calls
        for part in argv
    )


def test_nested_relative_uv_file_uses_project_ruff_and_project_relative_paths(tmp_path):
    base = tmp_path / "X"
    root = base / "repo"
    tests = root / "tests"
    tests.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    target = tests / "test_x.py"
    target.write_text("value = 1\n", encoding="utf-8")
    project_ruff = root / ".venv" / "bin" / "ruff"
    project_ruff.parent.mkdir(parents=True)
    project_ruff.symlink_to(_ruff())

    cid = "issue950-nested-relative-ready"
    record_environment_state(cid, "ready")
    client, container = _client(base, cid)
    relative_path = "repo/tests/test_x.py"

    findings = lint_file(
        client, cid, relative_path,
        scope_workdir=_determine_scope(relative_path),
    )

    assert findings == []
    lint_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if any("ruff" in part and re.search(r"\bcheck\b", part) for part in argv)
    ]
    assert lint_calls
    assert all(str(project_ruff) in " ".join(argv) for argv, _ in lint_calls), lint_calls
    assert all(cwd == root for _, cwd in lint_calls), lint_calls
    assert all("repo/tests" not in " ".join(argv) for argv, _ in lint_calls), lint_calls


def test_absolute_uv_file_with_empty_working_dir_still_selects_project_ruff(tmp_path):
    root = tmp_path / "empty workdir project"
    tests = root / "tests"
    tests.mkdir(parents=True)
    target = tests / "test_x.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname='fixture'\nversion='0'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    project_ruff = root / ".venv" / "bin" / "ruff"
    project_ruff.parent.mkdir(parents=True)
    project_ruff.symlink_to(_ruff())

    cid = "issue950-empty-working-dir"
    record_environment_state(cid, "ready")
    client, container = _client(root, cid)
    container.attrs["Config"]["WorkingDir"] = ""

    findings = lint_file(
        client, cid, str(target), scope_workdir=_determine_scope(str(target))
    )

    assert findings == []
    lint_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if any("ruff" in part and re.search(r"\bcheck\b", part) for part in argv)
    ]
    assert lint_calls
    assert all(str(project_ruff) in " ".join(argv) for argv, _ in lint_calls), lint_calls
    assert all(cwd == root for _, cwd in lint_calls), lint_calls


def test_clean_file_scope_uses_tests_directory_read_only_and_eslint_paths(tmp_path):
    root = tmp_path / "project with spaces"
    tests = root / "tests"
    tests.mkdir(parents=True)
    target = tests / "test_clean.py"
    sibling = tests / "test_sibling.py"
    target.write_text("value = 1\n", encoding="utf-8")
    sibling.write_text("import os\n", encoding="utf-8")
    before = sibling.read_bytes()
    client, _ = _client(root, "issue950-scope")

    findings = lint_file(
        client,
        "issue950-scope",
        "tests/test_clean.py",
        scope_workdir=_determine_scope("tests/test_clean.py"),
        fix=True,
    )

    assert any(item["rule"] == "F401" for item in findings), findings
    assert sibling.read_bytes() == before


def test_eslint_receives_project_root_and_keeps_scope_phase_read_only(tmp_path):
    root = tmp_path / "project with spaces"
    (root / "tests with spaces").mkdir(parents=True)
    client, _ = _client(root, "issue950-eslint")
    calls = []
    clean = SimpleNamespace(status="ok", detail="", findings=[])

    with patch(
        "sunaba.edit_verify.single_file._run_eslint_verify",
        side_effect=lambda *args, **kwargs: calls.append((args, kwargs)) or clean,
    ):
        findings = lint_file(
            client,
            "issue950-eslint",
            "tests with spaces/app.test.ts",
            scope_workdir=_determine_scope("tests with spaces/app.test.ts"),
            fix=True,
        )

    assert findings == []
    paths = [call[0][1] for call in calls]
    assert len(paths) == 2
    workdirs = [call[1].get("workdir") for call in calls]
    resolved = [
        Path(path) if Path(path).is_absolute() else Path(workdir or root) / path
        for path, workdir in zip(paths, workdirs, strict=True)
    ]
    assert resolved[0].resolve() == (root / "tests with spaces/app.test.ts").resolve()
    assert resolved[1].resolve() == (root / "tests with spaces").resolve()
    assert all(workdir in (".", str(root)) for workdir in workdirs)
    assert [call[1].get("fix") for call in calls] == [True, False]


@pytest.mark.parametrize(
    "probe_output",
    [
        "",
        "unexpected output\n",
        "__SUNABA_PROJECT_ROOT__/tmp/one\n__SUNABA_PROJECT_ROOT__/tmp/two\n",
        "__SUNABA_PROJECT_ROOT__relative/path\n",
    ],
)
def test_malformed_project_probe_output_is_an_explicit_error(tmp_path, probe_output):
    root = tmp_path / "probe-root"
    root.mkdir()
    target = root / "test_x.py"
    target.write_text("import os\n", encoding="utf-8")
    client, _ = _client(root, "issue950-malformed-probe")

    with (
        patch(
            "sunaba.edit_verify.single_file._exec_run",
            return_value=(0, probe_output, ""),
        ),
        patch("sunaba.edit_verify.single_file._run_ruff_verify") as ruff,
    ):
        findings = lint_file(
            client,
            "issue950-malformed-probe",
            str(target),
            scope_workdir=_determine_scope(str(target)),
        )

    assert len(findings) == 1
    assert findings[0]["rule"] == "error"
    assert "project ancestor probe returned malformed output" in findings[0]["message"]
    ruff.assert_not_called()


def test_absolute_unmarked_target_uses_its_parent_instead_of_unrelated_uv_workdir(tmp_path):
    base = tmp_path / "X"
    workdir = base / "uv"
    standalone = base / "standalone"
    workdir.mkdir(parents=True)
    standalone.mkdir()
    (workdir / "pyproject.toml").write_text(
        "[project]\nname='unrelated'\nversion='0'\n", encoding="utf-8"
    )
    (workdir / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    target = standalone / "test_x.py"
    target.write_text("import os\n", encoding="utf-8")
    cid = "issue950-standalone-unrelated-uv"
    client, container = _client(workdir, cid)

    findings = lint_file(
        client, cid, str(target), scope_workdir=_determine_scope(str(target))
    )

    assert any(item["rule"] == "F401" for item in findings), findings
    assert not any("uv environment not_prepared" in item["message"] for item in findings)
    lint_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if any("ruff" in part and re.search(r"\bcheck\b", part) for part in argv)
    ]
    assert lint_calls
    assert all(cwd == standalone for _, cwd in lint_calls), lint_calls


@pytest.mark.parametrize("root_name,directory,absolute", [
    ("nested project", "src", True),
    ("nested project", "src", False),
    ("nested project", "tests", True),
    ("nested project", "tests", False),
    ("src", "src", False),
])
@pytest.mark.parametrize("marked", [True, False], ids=["marked", "unmarked"])
def test_nested_scope_keeps_its_input_anchor(
    tmp_path, directory, absolute, marked, root_name
):
    base = tmp_path / "Docker workdir"
    root = base / root_name
    scope_dir = root / directory
    scope_dir.mkdir(parents=True)
    if marked:
        (root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    target = scope_dir / "clean.py"
    sibling = scope_dir / "sibling.py"
    target.write_text("value = 1\n", encoding="utf-8")
    sibling.write_text("import os\n", encoding="utf-8")
    # Both a wrong Docker anchor and an accidentally widened scope have
    # real files to lint, so neither can pass by merely finding some F401.
    decoy = base / directory
    decoy.mkdir(exist_ok=True)
    (decoy / "decoy.py").write_text("import sys\n", encoding="utf-8")
    (root / "outside.py").write_text("import json\n", encoding="utf-8")
    cid = "issue950-nested-scope"
    client, container = _client(base, cid)
    path = str(target) if absolute else str(target.relative_to(base))
    before = sibling.read_bytes()
    _ruff()

    findings = lint_file(
        client, cid, path, scope_workdir=_determine_scope(path), fix=True
    )

    assert [(item["rule"], item["file"]) for item in findings] == [
        ("F401", str(sibling))
    ], findings
    assert sibling.read_bytes() == before
    lint_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if any("ruff" in part and re.search(r"\bcheck\b", part) for part in argv)
    ]
    assert len(lint_calls) == 2, lint_calls
    if marked:
        assert all(cwd == root for _, cwd in lint_calls), lint_calls
    assert "--fix" in " ".join(lint_calls[0][0])
    assert "--fix" not in " ".join(lint_calls[1][0])


@pytest.mark.parametrize("absolute_anchor", [True, False], ids=["absolute", "relative"])
@pytest.mark.parametrize("directory,review_name", [
    ("src", "separate review"),
    ("review files", "separate review"),
    ("review files", "review files"),
])
def test_explicit_scope_uses_supplied_anchor_without_retargeting_file(
    tmp_path, absolute_anchor, directory, review_name
):
    base = tmp_path / "Docker workdir"
    root = base / "project"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    target = root / "src" / "clean.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (root / "src" / "outside.py").write_text("import sys\n", encoding="utf-8")
    review_root = base / review_name
    scope_dir = review_root / directory
    scope_dir.mkdir(parents=True)
    sibling = scope_dir / "sibling.py"
    sibling.write_text("import os\n", encoding="utf-8")
    (review_root / "outside.py").write_text("import json\n", encoding="utf-8")
    anchor = str(review_root) if absolute_anchor else str(review_root.relative_to(base))
    cid = "issue950-explicit-scope"
    client, _ = _client(base, cid)
    _ruff()

    findings = lint_file(
        client, cid, str(target.relative_to(base)),
        scope_workdir=ScopeWorkdir(directory, anchor),
    )

    assert [(item["rule"], item["file"]) for item in findings] == [
        ("F401", str(sibling))
    ], findings


def test_pylint_fallback_reads_rebased_target_from_the_same_project_cwd(tmp_path):
    base = tmp_path / "Docker workdir"
    root = base / "nested project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (root / "tests" / "test_x.py").write_text("import os\n", encoding="utf-8")
    # A tiny pylint stand-in emits a diagnostic only if its input is readable.
    # Ruff absence is controlled independently; the shell and cwd are real.
    executable = root / "pylint"
    executable.write_text(
        '#!/bin/sh\n'
        '[ -f "$3" ] || exit 1\n'
        """printf '[{"path":"tests/test_x.py","line":1,"message-id":"W0611","message":"Unused import os"}]'\n""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    cid = "issue950-pylint-cwd"
    client, _ = _client(base, cid)
    unavailable = SimpleNamespace(status="not_available", detail="absent", findings=[])

    with (
        patch("sunaba.edit_verify.single_file._run_ruff_verify", return_value=unavailable),
        patch("sunaba.edit_verify.single_file._SANDBOX_ENV", f"PATH='{root}':$PATH "),
    ):
        findings = lint_file(
            client, cid, "nested project/tests/test_x.py",
            scope_workdir=_determine_scope("nested project/tests/test_x.py"),
        )

    assert [(item["rule"], item["file"]) for item in findings] == [
        ("W0611", "tests/test_x.py")
    ], findings


@pytest.mark.parametrize("metadata", [
    {"Config": {"WorkingDir": ""}},
    {"Config": {}},
    {},
], ids=["empty-workdir", "missing-workdir", "missing-config"])
@pytest.mark.parametrize("fix", [False, True], ids=["read-only", "fix"])
def test_empty_workdir_keeps_relative_target_separate_from_absolute_scope_anchor(
    tmp_path, metadata, fix
):
    root = tmp_path / "project with spaces"
    target = root / "src" / "package" / "target.py"
    target.parent.mkdir(parents=True)
    target.write_text("import os\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    scope_dir = root / "tests"
    scope_dir.mkdir()
    sibling = scope_dir / "sibling.py"
    sibling.write_text("import sys\n", encoding="utf-8")
    # Docker's empty WORKDIR means '/', so this input names the original
    # target. Joining it to the explicit scope anchor would select the decoy.
    relative_target = str(target.relative_to("/"))
    decoy = root / relative_target
    decoy.parent.mkdir(parents=True)
    decoy.write_text("import json\n", encoding="utf-8")
    target_before = target.read_bytes()
    decoy_before = decoy.read_bytes()
    sibling_before = sibling.read_bytes()
    cid = "issue950-empty-workdir-relative-target"
    client, container = _client(Path("/"), cid)
    container.attrs = metadata
    _ruff()

    findings = lint_file(
        client, cid, relative_target,
        scope_workdir=ScopeWorkdir("tests", str(root)), fix=fix,
    )

    assert decoy.read_bytes() == decoy_before
    assert sibling.read_bytes() == sibling_before
    assert target.read_bytes() == (b"" if fix else target_before)
    expected_file = sibling if fix else target
    assert [(item["rule"], item["file"]) for item in findings] == [
        ("F401", str(expected_file))
    ], findings
    lint_calls = [
        (argv, cwd)
        for argv, cwd in container.calls
        if any("ruff" in part and re.search(r"\bcheck\b", part) for part in argv)
    ]
    assert len(lint_calls) == (2 if fix else 1), lint_calls
    assert all(cwd == root for _, cwd in lint_calls), lint_calls
    assert "src/package/target.py" in " ".join(lint_calls[0][0])
    assert ("--fix" in " ".join(lint_calls[0][0])) == fix
    if fix:
        assert "--fix" not in " ".join(lint_calls[1][0])
