"""Tests for scripts/check_patch_targets.py (Issue #166).

The checker resolves every string ``patch("a.b.c")`` target the same way
``unittest.mock`` does at runtime and fails when the attribute is missing,
catching patch drift after refactors (see #154).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import check_patch_targets as cpt  # noqa: E402


class TestResolvePatchTarget:
    """resolve_patch_target returns None for valid targets, a reason otherwise."""

    def test_existing_attribute_resolves(self) -> None:
        assert cpt.resolve_patch_target("code_sandbox_mcp.server._docker") is None

    def test_missing_attribute_is_reported(self) -> None:
        reason = cpt.resolve_patch_target(
            "code_sandbox_mcp.server.totally_missing_attr"
        )
        assert reason is not None
        assert "totally_missing_attr" in reason
        assert "code_sandbox_mcp.server" in reason

    def test_missing_module_is_reported(self) -> None:
        reason = cpt.resolve_patch_target("code_sandbox_mcp.no_such_module.foo")
        assert reason is not None
        assert "code_sandbox_mcp.no_such_module" in reason

    def test_non_dotted_target_is_reported(self) -> None:
        assert cpt.resolve_patch_target("server") is not None

    def test_nested_class_attribute_resolves(self) -> None:
        # ``Path.exists`` is an attribute on a class reached through a module.
        assert cpt.resolve_patch_target("pathlib.Path.exists") is None


class TestIterPatchTargets:
    """iter_patch_targets picks string patch targets and skips other forms."""

    def _targets(self, source: str) -> list[tuple[int, str]]:
        return list(cpt.iter_patch_targets(ast.parse(source)))

    def test_decorator_and_context_manager(self) -> None:
        source = (
            "from unittest.mock import patch\n"
            "@patch('a.b.c')\n"
            "def test_x():\n"
            "    with patch('d.e.f'):\n"
            "        pass\n"
        )
        targets = self._targets(source)
        assert (2, "a.b.c") in targets
        assert (4, "d.e.f") in targets

    def test_qualified_patch_call(self) -> None:
        source = "import unittest.mock as m\nm.patch('a.b.c')\n"
        assert (2, "a.b.c") in self._targets(source)

    def test_patch_object_is_skipped(self) -> None:
        # patch.object's first string arg is an attribute name, not a target.
        source = "from unittest.mock import patch\npatch.object(obj, 'method')\n"
        assert self._targets(source) == []

    def test_patch_dict_is_skipped(self) -> None:
        source = "from unittest.mock import patch\npatch.dict('os.environ', {})\n"
        assert self._targets(source) == []

    def test_non_string_target_is_ignored(self) -> None:
        source = "from unittest.mock import patch\npatch(SomeClass.attr)\n"
        assert self._targets(source) == []


class TestCheckTestSuite:
    """The repository's own test suite must have no unresolved patch targets."""

    def test_repo_tests_have_resolvable_patch_targets(self) -> None:
        cpt._ensure_src_importable()
        errors = cpt.check_paths([_REPO_ROOT / "tests"])
        assert errors == [], "\n".join(str(e) for e in errors)


class TestCheckFile:
    """check_file flags a drifted target, demonstrating the #154 scenario."""

    def test_drifted_target_is_flagged(self, tmp_path: Path) -> None:
        cpt._ensure_src_importable()
        test_file = tmp_path / "test_drift.py"
        # _docker exists; _docker_gone does not -> simulates an attribute that
        # was moved out of server.py but is still patched.
        test_file.write_text(
            "from unittest.mock import patch\n"
            "@patch('code_sandbox_mcp.server._docker_gone')\n"
            "def test_foo(m):\n"
            "    pass\n",
            encoding="utf-8",
        )
        errors = cpt.check_file(test_file)
        assert len(errors) == 1
        assert errors[0].target == "code_sandbox_mcp.server._docker_gone"
        assert errors[0].lineno == 2
