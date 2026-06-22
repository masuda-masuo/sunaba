"""Tests for scripts/refactor_aid.py (issue #165)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "refactor_aid", ROOT / "scripts" / "refactor_aid.py"
)
ra = importlib.util.module_from_spec(_spec)
sys.modules["refactor_aid"] = ra
_spec.loader.exec_module(ra)


FILES = {
    "__init__.py": "",
    "common.py": (
        "def helper():\n"
        '    """A shared, independent helper."""\n'
        "    return 1\n"
        "\n"
        "def dup():\n"
        '    """Collision target in common."""\n'
        "    return 1\n"
    ),
    "util.py": (
        "def standalone():\n"
        '    """Another independent helper."""\n'
        "    return 2\n"
        "\n"
        "def dup():\n"
        '    """Collision target in util."""\n'
        "    return 2\n"
    ),
    "mod.py": (
        "from pathlib import Path\n"
        "\n"
        "from .common import helper\n"
        "from .util import standalone as standalone_alias\n"
        "\n"
        "def shared_helper():\n"
        '    """Used by target and by other.consumer."""\n'
        "    return 0\n"
        "\n"
        "def local_helper():\n"
        "    return 0\n"
        "\n"
        "def target(x):\n"
        "    p = Path(__file__)\n"
        "    helper()\n"
        "    standalone_alias()\n"
        "    shared_helper()\n"
        "    local_helper()\n"
        "    return p, x\n"
    ),
    "other.py": (
        "from .mod import shared_helper\n"
        "\n"
        "def consumer():\n"
        '    """Calls shared_helper from another module."""\n'
        "    return shared_helper()\n"
    ),
}


@pytest.fixture()
def model(tmp_path):
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    for name, content in FILES.items():
        (pkg / name).write_text(content, encoding="utf-8")
    return ra.build_model(tmp_path / "src")


def _ann(deps):
    return {name: annotation for _, name, annotation in deps}


def test_annotations_cover_all_kinds(model):
    ann = _ann(ra.deps_of(model, "pkg.mod", "target"))
    # import as-is: defined in another module, reached via import (plain + alias)
    assert ann["helper"].startswith("import as-is")
    assert ann["standalone"].startswith("import as-is")
    # shared: local def referenced from another module too
    assert ann["shared_helper"].startswith("shared")
    # move together: local def referenced only within this module
    assert ann["local_helper"] == "move together"
    # external (stdlib) names are excluded entirely
    assert "Path" not in ann


def test_alias_resolves_to_real_symbol(model):
    deps = {(m, n) for m, n, _ in ra.deps_of(model, "pkg.mod", "target")}
    assert ("pkg.util", "standalone") in deps


def test_extract_check_collects_risks(model):
    ej = ra.extract_check_json(model, "pkg.mod", "target")
    assert ej["move_together"] == ["local_helper"]
    assert ej["risks"]["shared_dependencies"] == ["shared_helper"]
    assert ej["risks"]["dunder_file_lines"]  # __file__ used in target
    missing = ej["risks"]["missing_docstrings"]
    assert "target" in missing  # the function itself
    assert "local_helper" in missing  # a move-together dep


def test_reverse_lists_callers(model):
    callers = model.callers.get(("pkg.mod", "shared_helper"), set())
    assert ("pkg.other", "consumer") in callers
    assert ("pkg.mod", "target") in callers


def test_find_func_disambiguates_by_package(model):
    assert ra.find_func(model, "dup", "common") == ("pkg.common", "dup")
    assert ra.find_func(model, "dup", "util") == ("pkg.util", "dup")


def test_find_func_missing_raises(model):
    with pytest.raises(SystemExit):
        ra.find_func(model, "does_not_exist")


def test_graph_json_shape(model):
    gj = ra.graph_json(model, "pkg.mod", "target")
    assert gj["function"] == "target"
    assert gj["module"] == "pkg.mod"
    names = {d["name"] for d in gj["dependencies"]}
    assert {"helper", "standalone", "shared_helper", "local_helper"} <= names
