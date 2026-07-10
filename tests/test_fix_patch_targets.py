"""Tests for scripts/fix_patch_targets.py (Issue #164).

The codemod rewrites string ``patch("a.b.c")`` targets when a symbol moves
modules, the prevent-side companion to ``check_patch_targets.py`` (#166) which
only detects the resulting drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import fix_patch_targets as fpt  # noqa: E402


class TestRename:
    """Rename.apply matches whole dotted components, exact or as a prefix."""

    def test_exact_match(self) -> None:
        rule = fpt.Rename("a.b.c", "x.y.c")
        assert rule.apply("a.b.c") == "x.y.c"

    def test_attribute_under_symbol(self) -> None:
        # Moving a class a.b.C also rewrites patches of its members.
        rule = fpt.Rename("a.b.C", "x.y.C")
        assert rule.apply("a.b.C.method") == "x.y.C.method"

    def test_no_partial_component_match(self) -> None:
        # 'a.bc' must not be matched by a rule for 'a.b'.
        rule = fpt.Rename("a.b", "x.y")
        assert rule.apply("a.bc") is None

    def test_unrelated_target(self) -> None:
        rule = fpt.Rename("a.b.c", "x.y.c")
        assert rule.apply("d.e.f") is None


class TestRewriteSource:
    """rewrite_source edits only matched literals and preserves the rest."""

    def _rename(self) -> list[fpt.Rename]:
        return [fpt.Rename("sunaba.server._docker", "sunaba.tools.exec._docker")]

    def test_decorator_target_rewritten(self) -> None:
        source = (
            "from unittest.mock import patch\n"
            "@patch('sunaba.server._docker')\n"
            "def test_x():\n"
            "    pass\n"
        )
        new_source, replacements = fpt.rewrite_source(source, self._rename())
        assert "sunaba.tools.exec._docker" in new_source
        assert "server._docker" not in new_source
        assert len(replacements) == 1
        assert replacements[0].lineno == 2

    def test_quote_style_preserved(self) -> None:
        source = 'patch("sunaba.server._docker")\n'
        new_source, _ = fpt.rewrite_source(source, self._rename())
        assert new_source == 'patch("sunaba.tools.exec._docker")\n'

    def test_unrelated_target_untouched(self) -> None:
        source = "patch('sunaba.server._other')\n"
        new_source, replacements = fpt.rewrite_source(source, self._rename())
        assert replacements == []
        assert new_source == source

    def test_multiple_targets_same_line(self) -> None:
        # Right-to-left application keeps column offsets valid after a
        # length-changing edit to an earlier literal on the same line.
        source = "f(patch('a.b'), patch('a.b.c'))\n"
        renames = [fpt.Rename("a.b", "xxxxxxxx.b")]
        new_source, replacements = fpt.rewrite_source(source, renames)
        assert new_source == "f(patch('xxxxxxxx.b'), patch('xxxxxxxx.b.c'))\n"
        assert len(replacements) == 2

    def test_patch_object_not_rewritten(self) -> None:
        # patch.object's first string arg is an attribute name, not a target.
        source = "patch.object(obj, 'a.b.c')\n"
        new_source, replacements = fpt.rewrite_source(source, [fpt.Rename("a.b.c", "x.y.z")])
        assert replacements == []
        assert new_source == source

    def test_triple_single_quote_preserved(self) -> None:
        source = "patch('''a.b.c''')\n"
        new_source, _ = fpt.rewrite_source(source, [fpt.Rename("a.b.c", "x.y.z")])
        assert new_source == "patch('''x.y.z''')\n"

    def test_triple_double_quote_preserved(self) -> None:
        source = 'patch("""a.b.c""")\n'
        new_source, _ = fpt.rewrite_source(source, [fpt.Rename("a.b.c", "x.y.z")])
        assert new_source == 'patch("""x.y.z""")\n'

    def test_implicit_string_concatenation_normalised(self) -> None:
        # Python's AST folds 'a.b.' 'c' into Constant('a.b.c') and the
        # end_col_offset covers the full span, so the codemod rewrites the
        # entire span to a single quoted literal.
        source = "patch('a.b.' 'c')\n"
        new_source, replacements = fpt.rewrite_source(source, [fpt.Rename("a.b.c", "x.y.z")])
        assert len(replacements) == 1
        assert new_source == "patch('x.y.z')\n"


class TestRewritePaths:
    """rewrite_paths previews without writing and applies only with write=True."""

    def _write_test(self, tmp_path: Path) -> Path:
        test_file = tmp_path / "test_drift.py"
        test_file.write_text(
            "from unittest.mock import patch\n"
            "@patch('sunaba.server._docker')\n"
            "def test_foo(m):\n"
            "    pass\n",
            encoding="utf-8",
        )
        return test_file

    def _renames(self) -> list[fpt.Rename]:
        return [fpt.Rename("sunaba.server._docker", "sunaba.tools.exec._docker")]

    def test_preview_does_not_write(self, tmp_path: Path) -> None:
        test_file = self._write_test(tmp_path)
        before = test_file.read_text(encoding="utf-8")
        results = fpt.rewrite_paths([tmp_path], self._renames(), write=False)
        assert test_file in results
        assert test_file.read_text(encoding="utf-8") == before

    def test_write_applies(self, tmp_path: Path) -> None:
        test_file = self._write_test(tmp_path)
        results = fpt.rewrite_paths([tmp_path], self._renames(), write=True)
        assert test_file in results
        after = test_file.read_text(encoding="utf-8")
        assert "sunaba.tools.exec._docker" in after
        assert "server._docker" not in after


class TestMain:
    """The CLI entry point previews by default and writes with -w."""

    def test_preview_reports_without_writing(self, tmp_path: Path, capsys) -> None:
        test_file = tmp_path / "test_a.py"
        test_file.write_text("patch('a.b.c')\n", encoding="utf-8")
        rc = fpt.main(["--move", "a.b.c", "x.y.z", str(test_file)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Would rewrite" in out
        assert test_file.read_text(encoding="utf-8") == "patch('a.b.c')\n"

    def test_write_flag_applies(self, tmp_path: Path, capsys) -> None:
        test_file = tmp_path / "test_a.py"
        test_file.write_text("patch('a.b.c')\n", encoding="utf-8")
        rc = fpt.main(["-w", "--move", "a.b.c", "x.y.z", str(test_file)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Rewrote" in out
        assert test_file.read_text(encoding="utf-8") == "patch('x.y.z')\n"

    def test_no_match_is_reported(self, tmp_path: Path, capsys) -> None:
        test_file = tmp_path / "test_a.py"
        test_file.write_text("patch('d.e.f')\n", encoding="utf-8")
        rc = fpt.main(["--move", "a.b.c", "x.y.z", str(test_file)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "No matching patch targets" in out
