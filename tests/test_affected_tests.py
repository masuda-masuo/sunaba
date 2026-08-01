"""Tests for the affected-test selector (Issue #781).

The selector is a stdlib-only standalone script (``src/sunaba/edit_verify/
affected_tests.py``) that ``verify_in_container`` copies into target
containers and executes with their python.  These tests therefore drive the
CLI contract as a subprocess against throwaway repo trees, and assert the
JSON contract: ``{"selected": [...], "widen_reason": null}`` with exit 0 --
widening is expressed in JSON, never via the exit code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sunaba.edit_verify import affected_tests

SELECTOR = Path(affected_tests.__file__)


def run_selector(root: Path, *changed: str, deleted: list[str] | None = None) -> dict:
    """Run the selector CLI against *root*; returns the parsed JSON result."""
    cmd = [sys.executable, str(SELECTOR), "--root", str(root)]
    for d in deleted or []:
        cmd += ["--deleted", d]
    cmd += list(changed)
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    assert proc.returncode == 0, f"selector exited {proc.returncode}:\n{proc.stderr}"
    return json.loads(proc.stdout)


def write(root: Path, rel: str, content: str) -> None:
    """Create *rel* (and parents) inside the tmp repo *root*."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def make_repo(tmp_path: Path) -> Path:
    """A src-layout repo: package ``pkg`` under ``src/``, tests under ``tests/``."""
    root = tmp_path / "repo"
    write(root, "src/pkg/__init__.py", "")
    write(root, "src/pkg/a.py", "VALUE = 1\n")
    write(root, "src/pkg/shared.py", "SHARED = 2\n")
    write(root, "src/pkg/sub/__init__.py", "")
    write(root, "src/pkg/sub/mod.py", "from .. import shared\nX = shared.SHARED\n")
    write(root, "src/pkg/sub/y.py", "Y = 3\n")
    write(root, "src/pkg/sub/mod2.py", "from . import y\nX2 = y.Y\n")
    write(root, "tests/__init__.py", "")
    write(root, "tests/test_b.py", "import pkg.b\n")
    write(root, "tests/test_mod.py", "from pkg.sub import mod\n")
    write(root, "tests/test_mod2.py", "from pkg.sub import mod2\n")
    write(root, "tests/test_unrelated.py", "def test_nothing():\n    pass\n")
    return root


class TestSelectorCliContract:
    """The standalone CLI contract (exit 0, one JSON object)."""

    def test_exit_zero_and_json_shape(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        result = run_selector(root, "src/pkg/a.py")
        assert set(result) == {"selected", "widen_reason"}

    def test_importable_api_matches_cli(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        cli = run_selector(root, "src/pkg/a.py")
        api = affected_tests.select_affected_tests(str(root), ["src/pkg/a.py"])
        assert api == cli


class TestSelection:
    """The import-graph selection itself."""

    def test_dependency_reachable_only_through_function_body_import(self, tmp_path: Path) -> None:
        """A deferred (function-body) import must be captured.

        ``pkg/b.py`` imports ``pkg.a`` ONLY inside a function.  A
        top-level-only scan would miss the edge, so changing ``pkg/a.py``
        must still select ``tests/test_b.py`` (which imports ``pkg.b``)
        and nothing else.
        """
        root = make_repo(tmp_path)
        write(root, "src/pkg/b.py", (
            "def load():\n"
            "    from pkg import a\n"
            "    return a.VALUE\n"
        ))
        write(root, "tests/test_b.py", "import pkg.b\n")

        result = run_selector(root, "src/pkg/a.py")
        assert result["widen_reason"] is None
        assert result["selected"] == ["tests/test_b.py"]

    def test_relative_import_dotdot(self, tmp_path: Path) -> None:
        """``from .. import shared`` in pkg/sub/mod.py -> pkg.shared."""
        root = make_repo(tmp_path)
        result = run_selector(root, "src/pkg/shared.py")
        assert result["widen_reason"] is None
        assert "tests/test_mod.py" in result["selected"]
        assert "tests/test_b.py" not in result["selected"]

    def test_relative_import_dot(self, tmp_path: Path) -> None:
        """``from . import y`` in pkg/sub/mod2.py -> pkg.sub.y."""
        root = make_repo(tmp_path)
        result = run_selector(root, "src/pkg/sub/y.py")
        assert result["widen_reason"] is None
        assert "tests/test_mod2.py" in result["selected"]
        assert "tests/test_mod.py" not in result["selected"]

    def test_import_dotted_executes_prefixes(self, tmp_path: Path) -> None:
        """``import pkg.sub.mod`` touches pkg, pkg.sub and pkg.sub.mod.

        Changing the package ``__init__`` must therefore select the test
        that imports the deep module.
        """
        root = make_repo(tmp_path)
        write(root, "tests/test_deep.py", "import pkg.sub.mod\n")
        for changed in ("src/pkg/__init__.py", "src/pkg/sub/mod.py"):
            result = run_selector(root, changed)
            assert "tests/test_deep.py" in result["selected"], changed

    def test_changed_test_file_always_selected(self, tmp_path: Path) -> None:
        """A changed test file is selected even when nothing imports it."""
        root = make_repo(tmp_path)
        write(root, "tests/test_foo.py", "def test_foo():\n    pass\n")
        result = run_selector(root, "tests/test_foo.py")
        assert result["widen_reason"] is None
        assert result["selected"] == ["tests/test_foo.py"]

    def test_transitive_closure(self, tmp_path: Path) -> None:
        """A module importing a module that imports the changed one is selected."""
        root = make_repo(tmp_path)
        write(root, "src/pkg/b.py", "from pkg import a\nB = a.VALUE\n")
        write(root, "tests/test_b.py", "import pkg.b\n")
        write(root, "tests/test_bb.py", "import pkg.b\n")
        result = run_selector(root, "src/pkg/a.py")
        assert set(result["selected"]) == {"tests/test_b.py", "tests/test_bb.py"}

    def test_naming_convention_safety_net(self, tmp_path: Path) -> None:
        """Changed ``.../y.py`` also selects ``test_y*.py`` when the graph missed it."""
        root = make_repo(tmp_path)
        # test_y_helpers.py imports nothing, so only the naming net can catch it.
        write(root, "tests/test_y_helpers.py", "def test_helper():\n    pass\n")
        result = run_selector(root, "src/pkg/sub/y.py")
        assert result["widen_reason"] is None
        assert "tests/test_y_helpers.py" in result["selected"]
        assert "tests/test_mod2.py" in result["selected"]  # graph edge still works

    def test_leaf_module_selects_small_fraction(self, tmp_path: Path) -> None:
        """A change to a single leaf module selects only its importers."""
        root = make_repo(tmp_path)
        write(root, "src/pkg/b.py", "from pkg import a\nB = a.VALUE\n")
        write(root, "tests/test_b.py", "import pkg.b\n")
        result = run_selector(root, "src/pkg/a.py")
        assert result["widen_reason"] is None
        assert len(result["selected"]) >= 1
        assert all(p.startswith("tests/") for p in result["selected"])


class TestWidenTriggers:
    """Every widen trigger must produce widen_reason != null (fail open)."""

    @pytest.mark.parametrize("cfg", [
        "conftest.py",
        "pyproject.toml",
        "setup.cfg",
        "pytest.ini",
        "tox.ini",
    ])
    def test_config_or_fixture_change_widens(self, tmp_path: Path, cfg: str) -> None:
        root = make_repo(tmp_path)
        write(root, cfg, "")
        result = run_selector(root, cfg)
        assert result["selected"] == []
        assert result["widen_reason"] is not None

    def test_conftest_anywhere_widens(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        write(root, "tests/conftest.py", "import pytest\n")
        result = run_selector(root, "tests/conftest.py")
        assert result["selected"] == []
        assert result["widen_reason"] is not None

    def test_non_py_file_widens(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        write(root, "README.md", "docs\n")
        result = run_selector(root, "README.md")
        assert result["widen_reason"] is not None
        assert "non-.py" in result["widen_reason"]

    def test_deleted_path_flag_widens(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        result = run_selector(root, "src/pkg/a.py", deleted=["src/pkg/old.py"])
        assert result["selected"] == []
        assert "deleted or renamed" in result["widen_reason"]

    def test_deleted_file_passed_as_changed_path_widens(self, tmp_path: Path) -> None:
        """A changed path that no longer exists on disk (deletion) widens."""
        root = make_repo(tmp_path)
        result = run_selector(root, "src/pkg/gone.py")
        assert result["selected"] == []
        assert result["widen_reason"] is not None

    def test_empty_selection_widens(self, tmp_path: Path) -> None:
        """Non-empty change set with no reachable tests widens."""
        root = make_repo(tmp_path)
        write(root, "src/pkg/orphan.py", "ORPHAN = 1\n")
        result = run_selector(root, "src/pkg/orphan.py")
        assert result["selected"] == []
        assert "no tests selected" in result["widen_reason"]

    def test_empty_change_set_widens(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        result = run_selector(root)
        assert result["selected"] == []
        assert "empty" in result["widen_reason"]

    def test_parse_failure_widens(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        write(root, "src/pkg/broken.py", "def broken(:\n")
        result = run_selector(root, "src/pkg/broken.py")
        assert result["selected"] == []
        assert "analysis failure" in result["widen_reason"]

    def test_unrelated_parse_failure_does_not_widen(self, tmp_path: Path) -> None:
        """A broken file OUTSIDE the change set must not disable narrowing.

        Repos often keep intentionally-broken fixtures (e.g.
        tests/fixtures/broken.py used to exercise error paths).  Such a
        file is skipped -- it contributes no edges -- and never widens, so
        the feature keeps narrowing for unrelated changes.
        """
        root = make_repo(tmp_path)
        write(root, "tests/fixtures/broken.py", "def broken(:\n")
        write(root, "src/pkg/b.py", "from pkg import a\nB = a.VALUE\n")
        write(root, "tests/test_b.py", "import pkg.b\n")
        result = run_selector(root, "src/pkg/a.py")
        assert result["widen_reason"] is None
        assert result["selected"] == ["tests/test_b.py"]


class TestTestRootRestriction:
    """test_*.py files outside a test root are source modules, not tests."""

    def test_src_named_test_module_is_not_selected(self, tmp_path: Path) -> None:
        """``src/pkg/test_runners.py``-style helpers must not be executed.

        Basename-only matching would select them and hand them to pytest as
        positional paths; pytest would import and collect them.  They are
        only counted when they live under a known test root.
        """
        root = make_repo(tmp_path)
        # A source helper named test_*.py under the package, NOT under a
        # tests/test directory and NOT under any conftest.py directory.
        write(root, "src/pkg/test_runners.py", (
            "from pkg import a\n"
            "RUNNER = a.VALUE\n"
        ))
        # A real test also exercises pkg.a so the selection is non-empty
        # (an empty selection would legitimately widen).
        write(root, "src/pkg/b.py", "from pkg import a\nB = a.VALUE\n")
        write(root, "tests/test_b.py", "import pkg.b\n")
        result = run_selector(root, "src/pkg/a.py")
        assert result["widen_reason"] is None
        assert "src/pkg/test_runners.py" not in result["selected"]
        assert result["selected"] == ["tests/test_b.py"]

    def test_conftest_directory_marks_test_root(self, tmp_path: Path) -> None:
        """Files under a directory containing conftest.py count as tests
        even when the directory is not named tests/test."""
        root = make_repo(tmp_path)
        write(root, "qa/conftest.py", "import pytest\n")
        write(root, "qa/test_app.py", "import pkg.a\n")
        result = run_selector(root, "src/pkg/a.py")
        assert result["widen_reason"] is None
        assert "qa/test_app.py" in result["selected"]

    def test_changed_src_named_test_module_widens(self, tmp_path: Path) -> None:
        """A changed test_*.py source module maps to a module with no test
        coverage -> empty selection widens (fail open), it is never run."""
        root = make_repo(tmp_path)
        write(root, "src/pkg/test_helpers.py", "HELP = 1\n")
        result = run_selector(root, "src/pkg/test_helpers.py")
        assert result["selected"] == []
        assert result["widen_reason"] is not None

    def test_unmappable_py_file_widens(self, tmp_path: Path) -> None:
        """Root-level ``__init__.py`` has no importable name -> widen."""
        root = make_repo(tmp_path)
        write(root, "__init__.py", "")
        result = run_selector(root, "__init__.py")
        assert result["selected"] == []
        assert "cannot map" in result["widen_reason"]

    def test_changed_file_outside_root_widens(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_text("X = 1\n")
        result = run_selector(root, str(outside))
        assert result["selected"] == []
        assert result["widen_reason"] is not None


class TestRealTreeSelectionFraction:
    """Selector behavior on the real sunaba tree (Issue #781 review).

    The selection is the true reverse transitive closure over the import
    graph, so a change that reaches a hub module (a package __init__
    re-export, a widely-imported entry point) selects the hub's whole
    importer population -- on this hub-dense repo that can be a
    substantial share for hub-adjacent modules, and that is the shipped
    contract (documented in affected_tests.py).  What must ALWAYS hold is
    that a genuinely leaf change stays a small fraction of the suite.
    """

    # The repo root is the parent of tests/.
    REPO_ROOT = Path(__file__).resolve().parent.parent

    # A genuinely leaf module: measured 2 direct importers
    # (sunaba.server, tests.test_notify) and ~8% of the test files.
    LEAF_CHANGE = "src/sunaba/notify.py"

    def test_leaf_change_selects_bounded_fraction(self) -> None:
        root = self.REPO_ROOT
        result = affected_tests.select_affected_tests(str(root), [self.LEAF_CHANGE])
        assert result["widen_reason"] is None

        imports, module_to_file, _errors = affected_tests._build_graph(str(root))
        total = sum(
            1 for rel in module_to_file.values()
            if affected_tests._is_test_file(str(root), rel)
        )
        selected = len(result["selected"])
        fraction = selected / total if total else 0.0
        assert fraction < 0.20, (
            f"leaf change {self.LEAF_CHANGE} selected {selected}/{total} "
            f"test files ({fraction:.0%}); the affected-run contract requires "
            "a small fraction for genuinely leaf changes"
        )
        assert selected > 0

    def test_leaf_change_has_few_direct_importers(self) -> None:
        """Guard that the bounded-fraction module is actually a leaf: if the
        repo evolves and this module becomes a hub, the guard fails first
        with a clear message instead of silently invalidating the bound."""
        root = str(self.REPO_ROOT)
        mod = "sunaba.notify"
        imports, _module_to_file, _errors = affected_tests._build_graph(root)
        direct = [im for im, deps in imports.items() if mod in deps]
        assert len(direct) <= 3, (
            f"{mod} now has {len(direct)} direct importers ({sorted(direct)}); "
            "it is no longer a leaf -- pick another leaf module for the "
            "bounded-fraction test"
        )
