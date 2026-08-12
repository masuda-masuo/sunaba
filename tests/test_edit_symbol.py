"""Tests for the edit_symbol_in_container driver (issue #581).

Covers the in-container driver via ``edit_symbol_in_container`` (symbol
resolution, decorator-inclusive ranges, re-indentation, seam blank-line
collapsing, post-edit syntax verification).

The MCP-facing ``edit_symbol`` tool was removed in #627; its AST
resolution is now integrated into ``edit_file``'s ``old_str``
path.
"""

import ast

import pytest

from src.sunaba.edit_verify import edit_symbol_in_container
from sunaba.tools.file import edit_file
from tests.conftest import _FakeClient, _FakeContainer

POSIX = "/sandbox/mod.py"

MODULE_SRC = """\
import os


def foo():
    return 1


def bar():
    return 2
"""

CLASS_SRC = """\
class C:
    def a(self):
        return 1

    def b(self):
        return 2
"""

AMBIG_SRC = """\
def process(x):
    return x


class Handler:
    def process(self, x):
        return x
"""

OVERLOAD_SRC = """\
from typing import overload


@overload
def process(x: int) -> int: ...
@overload
def process(x: str) -> str: ...
def process(x):
    return x
"""


@pytest.fixture(autouse=True)
def _no_journal(monkeypatch) -> None:
    """Keep the execution journal out of unit tests."""
    monkeypatch.setattr(
        "src.sunaba.edit_verify.edits.record_file_write", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "sunaba.edit_verify.edits.record_file_write", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "sunaba.tools.file.record_tool_use", lambda *a, **k: None
    )


def _run(tmp_path, source, symbol, new_code, line=None):  # noqa: ANN001
    """Run the driver against *source* materialized in *tmp_path*."""
    f = tmp_path / "mod.py"
    f.write_text(source, encoding="utf-8")
    client = _FakeClient(_FakeContainer({POSIX: str(f)}))
    out = edit_symbol_in_container(client, "abc123", POSIX, symbol, new_code, line)
    return out, f


# ===================================================================
# Replace / delete: module level, methods, async, classes
# ===================================================================
class TestReplaceAndDelete:
    """Basic replace/delete across definition kinds."""

    def test_replace_module_level_function(self, tmp_path) -> None:
        out, f = _run(tmp_path, MODULE_SRC, "foo", "def foo():\n    return 99\n")
        assert out["status"] == "ok"
        assert out["changed"] is True
        assert out["resolved"] == {
            "qualname": "foo", "kind": "function", "start_line": 4, "end_line": 5,
        }
        text = f.read_text(encoding="utf-8")
        assert "return 99" in text and "return 1" not in text
        assert "def bar():" in text
        assert "-    return 1" in out["diff"] and "+    return 99" in out["diff"]

    def test_delete_module_level_function_collapses_to_two_blanks(self, tmp_path) -> None:
        out, f = _run(tmp_path, MODULE_SRC, "foo", "")
        assert out["status"] == "ok"
        assert f.read_text(encoding="utf-8") == "import os\n\n\ndef bar():\n    return 2\n"

    def test_method_replace_is_reindented(self, tmp_path) -> None:
        out, f = _run(tmp_path, CLASS_SRC, "C.a", "def a(self):\n    return 10\n")
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "    def a(self):" in text
        assert "        return 10" in text
        ast.parse(text)

    def test_delete_removes_decorators(self, tmp_path) -> None:
        src = (
            "import functools\n\n\n"
            "@functools.lru_cache\n@functools.wraps\ndef deco():\n    return 3\n\n\n"
            "def keep():\n    return 4\n"
        )
        out, f = _run(tmp_path, src, "deco", "")
        assert out["status"] == "ok"
        assert out["resolved"]["start_line"] == 4
        assert out["resolved"]["end_line"] == 7
        text = f.read_text(encoding="utf-8")
        assert "@functools.lru_cache" not in text and "@functools.wraps" not in text
        assert "def keep():" in text

    def test_async_def_replace(self, tmp_path) -> None:
        src = "async def fetch():\n    return 0\n"
        out, f = _run(tmp_path, src, "fetch", "async def fetch():\n    return 1\n")
        assert out["status"] == "ok"
        assert out["resolved"]["kind"] == "function"
        assert "return 1" in f.read_text(encoding="utf-8")

    def test_class_replace_and_delete(self, tmp_path) -> None:
        src = (
            "class Old:\n    x = 1\n\n    def m(self):\n        return self.x\n\n\n"
            "def keep():\n    return 4\n"
        )
        out, f = _run(tmp_path, src, "Old", "class Old:\n    y = 2\n")
        assert out["status"] == "ok"
        assert out["resolved"]["kind"] == "class"
        assert "y = 2" in f.read_text(encoding="utf-8")

        out, f = _run(tmp_path, src, "Old", "")
        assert out["status"] == "ok"
        assert f.read_text(encoding="utf-8") == "def keep():\n    return 4\n"

    def test_replace_one_function_with_two(self, tmp_path) -> None:
        new = "def foo_a():\n    return 1\n\n\ndef foo_b():\n    return 2\n"
        out, f = _run(tmp_path, MODULE_SRC, "foo", new)
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "def foo_a():" in text and "def foo_b():" in text
        assert "def foo():" not in text
        ast.parse(text)

    def test_replace_with_identical_code_reports_unchanged(self, tmp_path) -> None:
        out, f = _run(tmp_path, MODULE_SRC, "foo", "def foo():\n    return 1")
        assert out["status"] == "ok"
        assert out["changed"] is False
        assert out["diff"] == ""
        assert out["resolved"]["qualname"] == "foo"
        assert f.read_text(encoding="utf-8") == MODULE_SRC


# ===================================================================
# Nested scopes and qualified names
# ===================================================================
class TestNestedResolution:
    """Scope-stack qualnames: functions in functions, classes in classes."""

    NESTED_SRC = (
        "def outer():\n    def inner():\n        return 1\n    return inner\n\n\n"
        "class Foo:\n    class Inner:\n        def method(self):\n            return 2\n\n"
        "        def other(self):\n            return 3\n"
    )

    def test_function_nested_in_function(self, tmp_path) -> None:
        out, f = _run(
            tmp_path, self.NESTED_SRC, "outer.inner", "def inner():\n    return 42\n"
        )
        assert out["status"] == "ok"
        assert out["resolved"]["qualname"] == "outer.inner"
        assert "        return 42" in f.read_text(encoding="utf-8")

    def test_method_in_nested_class(self, tmp_path) -> None:
        out, f = _run(tmp_path, self.NESTED_SRC, "Foo.Inner.method", "")
        assert out["status"] == "ok"
        assert out["resolved"]["qualname"] == "Foo.Inner.method"
        text = f.read_text(encoding="utf-8")
        assert "def method" not in text and "def other" in text
        ast.parse(text)

    def test_suffix_match_on_partial_qualifier(self, tmp_path) -> None:
        out, _ = _run(tmp_path, self.NESTED_SRC, "Inner.other", "")
        assert out["status"] == "ok"
        assert out["resolved"]["qualname"] == "Foo.Inner.other"


# ===================================================================
# Ambiguity, line= disambiguation, not-found
# ===================================================================
class TestResolutionErrors:
    """Ambiguous, unresolvable, and line-disambiguated lookups."""

    def test_ambiguous_unqualified_name(self, tmp_path) -> None:
        out, f = _run(tmp_path, AMBIG_SRC, "process", "")
        assert out["status"] == "error"
        err = out["error"]
        assert "'process' is ambiguous in /sandbox/mod.py" in err
        assert "lines 1-2" in err and "lines 6-7" in err
        # Mixed qualnames are spelled out per candidate.
        assert "Handler.process:" in err
        assert "Retry with line=" in err
        assert f.read_text(encoding="utf-8") == AMBIG_SRC

    def test_ambiguity_error_lists_decorators_and_def_text(self, tmp_path) -> None:
        long_sig = (
            "def process(argument_number_one: int, argument_number_two: str, "
            "argument_number_three: float = 3.0) -> None:"
        )
        src = (
            f"{long_sig}\n    return None\n\n\n"
            "class H:\n    @staticmethod\n    def process(x):\n        return x\n"
        )
        out, _ = _run(tmp_path, src, "process", "")
        assert out["status"] == "error"
        err = out["error"]
        assert "@staticmethod" in err
        assert long_sig[:80] in err
        assert long_sig not in err  # 80-char truncation
        assert "H.process:" in err

    def test_line_disambiguates_overloads(self, tmp_path) -> None:
        out, f = _run(
            tmp_path, OVERLOAD_SRC, "process",
            "def process(x):\n    return x + 1\n", line=8,
        )
        assert out["status"] == "ok"
        assert out["resolved"]["start_line"] == 8
        text = f.read_text(encoding="utf-8")
        assert "return x + 1" in text
        assert text.count("@overload") == 2  # stubs untouched

    def test_line_outside_all_candidates_is_an_error(self, tmp_path) -> None:
        out, f = _run(tmp_path, OVERLOAD_SRC, "process", "", line=2)
        assert out["status"] == "error"
        err = out["error"]
        assert "line=2 does not fall within any definition of 'process'" in err
        assert "@overload" in err  # candidate listing reuses the ambiguity format
        assert "Retry with line=" in err
        assert f.read_text(encoding="utf-8") == OVERLOAD_SRC

    def test_not_found_suggests_close_matches(self, tmp_path) -> None:
        out, _ = _run(tmp_path, MODULE_SRC, "fooo", "")
        assert out["status"] == "error"
        err = out["error"]
        assert "symbol 'fooo' not found in /sandbox/mod.py" in err
        assert "Did you mean" in err
        assert "foo (line 4)" in err

    def test_not_found_without_close_matches(self, tmp_path) -> None:
        out, _ = _run(tmp_path, MODULE_SRC, "zzz_qqq", "")
        assert out["status"] == "error"
        assert "not found" in out["error"]
        assert "Did you mean" not in out["error"]


# ===================================================================
# Edit-boundary edge cases
# ===================================================================
class TestEditBoundaries:
    """EOF deletion, empty file, seam blank collapsing, final newline."""

    def test_delete_symbol_at_eof_keeps_single_final_newline(self, tmp_path) -> None:
        out, f = _run(tmp_path, MODULE_SRC, "bar", "")
        assert out["status"] == "ok"
        assert f.read_text(encoding="utf-8") == "import os\n\n\ndef foo():\n    return 1\n"

    def test_delete_only_symbol_leaves_empty_file(self, tmp_path) -> None:
        out, f = _run(tmp_path, "def only():\n    return 1\n", "only", "")
        assert out["status"] == "ok"
        assert out["changed"] is True
        assert f.read_text(encoding="utf-8") == ""

    def test_delete_first_symbol_strips_leading_blanks(self, tmp_path) -> None:
        src = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        out, f = _run(tmp_path, src, "a", "")
        assert out["status"] == "ok"
        assert f.read_text(encoding="utf-8") == "def b():\n    return 2\n"

    def test_method_deletion_collapses_seam_to_one_blank(self, tmp_path) -> None:
        out, f = _run(tmp_path, CLASS_SRC, "C.a", "")
        assert out["status"] == "ok"
        assert f.read_text(encoding="utf-8") == "class C:\n\n    def b(self):\n        return 2\n"


# ===================================================================
# Validation and safety gates
# ===================================================================
class TestValidationGates:
    """Syntax verification, whitespace-only new_code, CRLF, bad paths."""

    def test_new_code_syntax_error_leaves_file_untouched(self, tmp_path) -> None:
        out, f = _run(tmp_path, MODULE_SRC, "foo", "def foo(:\n    pass\n")
        assert out["status"] == "error"
        assert "syntax error" in out["error"]
        assert "nothing was written" in out["error"]
        assert f.read_text(encoding="utf-8") == MODULE_SRC

    def test_original_file_syntax_error(self, tmp_path) -> None:
        out, _ = _run(tmp_path, "def broken(:\n    pass\n", "broken", "")
        assert out["status"] == "error"
        assert "has a syntax error at line 1" in out["error"]
        assert "edit_file (complete old_str) or transform_file" in out["error"]

    def test_whitespace_only_new_code_is_rejected(self, tmp_path) -> None:
        out, f = _run(tmp_path, MODULE_SRC, "foo", "  \n")
        assert out["status"] == "error"
        assert out["error"] == (
            'Error: new_code is whitespace-only; use new_code="" to delete the symbol'
        )
        assert f.read_text(encoding="utf-8") == MODULE_SRC

    def test_crlf_file_is_rejected(self, tmp_path) -> None:
        f = tmp_path / "mod.py"
        f.write_bytes(b"def f():\r\n    pass\r\n")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(client, "abc123", POSIX, "f", "")
        assert out["status"] == "error"
        assert "CRLF" in out["error"]
        assert f.read_bytes() == b"def f():\r\n    pass\r\n"

    def test_relative_path_is_rejected(self) -> None:
        out = edit_symbol_in_container(
            _FakeClient(_FakeContainer()), "abc123", "rel/mod.py", "f", ""
        )
        assert out["status"] == "error"
        assert "absolute" in out["error"]

    def test_missing_file(self, tmp_path) -> None:
        missing = tmp_path / "missing.py"
        client = _FakeClient(_FakeContainer({POSIX: str(missing)}))
        out = edit_symbol_in_container(client, "abc123", POSIX, "f", "")
        assert out["status"] == "error"
        assert "not found" in out["error"]


# ===================================================================
# Preserve decorators and docstring
# ===================================================================
class TestPreserveDecoratorsAndDocstring:
    """preserve= parameter: keeps decorators/docstring from the old definition."""

    DEC_SRC = """\
import functools


@functools.lru_cache
@functools.wraps
def cached():
    \"\"\"This is a docstring.\"\"\"
    return 3
"""

    def test_default_preserves_both(self, tmp_path) -> None:
        out, f = _run(
            tmp_path, self.DEC_SRC, "cached",
            "def cached():\n    return 99\n",
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "@functools.lru_cache" in text
        assert "@functools.wraps" in text
        assert '"""This is a docstring."""' in text
        assert "return 99" in text

    def test_preserve_none_removes_everything(self, tmp_path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(self.DEC_SRC, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "cached",
            "def cached():\n    return 99\n",
            preserve="none",
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "@functools.lru_cache" not in text
        assert "@functools.wraps" not in text
        assert "docstring" not in text
        assert "return 99" in text

    def test_preserve_decorators_only(self, tmp_path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(self.DEC_SRC, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "cached",
            "def cached():\n    return 99\n",
            preserve="decorators",
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "@functools.lru_cache" in text
        assert "@functools.wraps" in text
        assert "docstring" not in text
        assert "return 99" in text

    def test_preserve_docstring_only(self, tmp_path) -> None:
        f = tmp_path / "mod.py"
        f.write_text(self.DEC_SRC, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "cached",
            "def cached():\n    return 99\n",
            preserve="docstring",
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "@functools.lru_cache" not in text
        assert "@functools.wraps" not in text
        assert '"""This is a docstring."""' in text
        assert "return 99" in text

    def test_new_decorators_win_over_old(self, tmp_path) -> None:
        """When new_code already has decorators, old ones are not duplicated."""
        f = tmp_path / "mod.py"
        f.write_text(self.DEC_SRC, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "cached",
            "@other_decorator\ndef cached():\n    return 99\n",
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "@other_decorator" in text
        assert "@functools.lru_cache" not in text  # old ones gone
        assert "return 99" in text

    def test_new_docstring_wins_over_old(self, tmp_path) -> None:
        """When new_code has a docstring, the old one is not inserted."""
        f = tmp_path / "mod.py"
        f.write_text(self.DEC_SRC, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "cached",
            'def cached():\n    """New docstring."""\n    return 99\n',
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert '"""New docstring."""' in text
        assert "This is a docstring" not in text
        assert "return 99" in text

    DEC_WITH_ARGS_SRC = """\
import functools


@functools.lru_cache(maxsize=128)
def cached():
    return 3
"""

    def test_docstring_reindented_to_new_body_indent(self, tmp_path) -> None:
        """Docstring indent adjusts when new_code uses a different body indent."""
        src = """\
def foo():
    \"\"\"A docstring.\"\"\"
    pass
"""
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "foo",
            "def foo():\n  return 1\n",
        )
        # new_code uses 2-space body indent; docstring should be re-indented to 2
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        for line in text.splitlines():
            if '"""' in line:
                assert line == '  """A docstring."""'
                break
        else:
            pytest.fail("docstring not found")

    def test_decorator_with_args_preserved(self, tmp_path) -> None:
        """Decorators with arguments (calls) are preserved correctly."""
        f = tmp_path / "mod.py"
        f.write_text(self.DEC_WITH_ARGS_SRC, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "cached",
            "def cached():\n    return 99\n",
        )
        assert out["status"] == "ok"
        text = f.read_text(encoding="utf-8")
        assert "@functools.lru_cache(maxsize=128)" in text
        assert "return 99" in text

    def test_multiline_signature_preserves_docstring(self, tmp_path) -> None:
        """Docstring lands after the whole signature, not inside it.

        The old implementation inserted the docstring right after the
        first ``def`` line, which broke multi-line signatures and made
        the driver reject valid new_code.
        """
        src = 'def foo():\n    """Doc."""\n    return 1\n'
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "foo",
            "def foo(\n    a=1,\n    b=2,\n):\n    return a + b\n",
        )
        assert out["status"] == "ok", out.get("error")
        text = f.read_text(encoding="utf-8")
        ast.parse(text)
        assert text == (
            "def foo(\n    a=1,\n    b=2,\n):\n"
            '    """Doc."""\n    return a + b\n'
        )

    def test_one_liner_new_def_skips_docstring_preservation(self, tmp_path) -> None:
        """A one-liner replacement has no body line to host the docstring.

        Preservation is skipped instead of producing an IndentationError
        that rejected valid new_code.
        """
        src = 'def foo():\n    """Doc."""\n    return 1\n'
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "foo", "def foo(): return 2\n",
        )
        assert out["status"] == "ok", out.get("error")
        text = f.read_text(encoding="utf-8")
        ast.parse(text)
        assert "return 2" in text

    def test_multiline_docstring_keeps_relative_indent(self, tmp_path) -> None:
        """Nested docstring lines shift as a block, not flattened.

        The old implementation re-indented every docstring line to the
        body indent, destroying the internal structure of Args:/Returns:
        sections.
        """
        src = (
            "def foo():\n"
            '    """Summary.\n'
            "\n"
            "    Args:\n"
            "        x: something.\n"
            '    """\n'
            "    return 1\n"
        )
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "foo", "def foo():\n    return 99\n",
        )
        assert out["status"] == "ok", out.get("error")
        text = f.read_text(encoding="utf-8")
        assert '    """Summary.' in text
        assert "    Args:" in text
        assert "        x: something." in text
        assert "return 99" in text

    def test_decorators_and_docstring_with_multiline_signature(self, tmp_path) -> None:
        """Decorator prepend and docstring insert compose (offset math)."""
        src = (
            "@wraps\n"
            "def foo():\n"
            '    """Doc."""\n'
            "    return 1\n"
        )
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "foo",
            "def foo(\n    a=1,\n):\n    return a\n",
        )
        assert out["status"] == "ok", out.get("error")
        text = f.read_text(encoding="utf-8")
        ast.parse(text)
        assert text == (
            "@wraps\ndef foo(\n    a=1,\n):\n"
            '    """Doc."""\n    return a\n'
        )

    def test_comment_between_signature_and_body(self, tmp_path) -> None:
        """A comment before the first statement stays above the docstring."""
        src = 'def foo():\n    """Doc."""\n    return 1\n'
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, "foo",
            "def foo():\n    # note\n    return 2\n",
        )
        assert out["status"] == "ok", out.get("error")
        text = f.read_text(encoding="utf-8")
        ast.parse(text)
        assert '"""Doc."""' in text
        assert "# note" in text
        assert "return 2" in text


# ===================================================================
# edit_file integration via old_str AST resolution
# ===================================================================


class _FakeContainerWithIO(_FakeContainer):
    """Extends _FakeContainer with cat/stat/mkdir for edit_file I/O."""

    def exec_run(self, cmd, **kwargs):  # noqa: ANN001, ANN201
        import shlex
        shell_cmd = cmd[-1] if isinstance(cmd, (list, tuple)) else cmd

        if shell_cmd.startswith("cat "):
            file_path = shlex.split(shell_cmd)[1]
            real_path = self.path_map.get(file_path, file_path)
            try:
                with open(real_path) as f:
                    return (0, (f.read().encode("utf-8"), b""))
            except FileNotFoundError:
                return (0, (b"", b""))

        if shell_cmd.startswith("mkdir "):
            return (0, (b"", b""))

        if shell_cmd.startswith("test -f "):
            file_path = shlex.split(shell_cmd)[1]
            real_path = self.path_map.get(file_path, file_path)
            try:
                with open(real_path) as f:
                    return (0, (b"", b""))
            except FileNotFoundError:
                return (1, (b"", b""))

        if shell_cmd.startswith("stat "):
            return (0, (b"1000 1000 644\n", b""))

        if shell_cmd.startswith("echo ") and "base64" in shell_cmd:
            return super().exec_run(cmd, **kwargs)

        return (0, (b"", b""))

    def put_archive(self, _path, _data) -> bool:
        return True


def _write_fake_docker(path_map):
    """Build a _FakeClient with _FakeContainerWithIO for edit_file tests."""
    return _FakeClient(_FakeContainerWithIO(path_map))


class TestWriteFileSymbolIntegration:
    """edit_file + AST resolution integration (issue #627/#628)."""

    # ── _extract_symbol_from_old_str unit tests ──────────────────────

    def test_extract_symbol_from_def(self) -> None:
        from sunaba.tools.edit_engine import _extract_symbol_from_old_str
        assert _extract_symbol_from_old_str("def foo():") == "foo"
        assert _extract_symbol_from_old_str("async def fetch():") == "fetch"
        assert _extract_symbol_from_old_str("class Bar:") == "Bar"
        assert _extract_symbol_from_old_str("@decorator\ndef foo():") == "foo"
        assert _extract_symbol_from_old_str("@dec1\n@dec2\ndef f():") == "f"
        assert _extract_symbol_from_old_str("# comment\ndef foo():") == "foo"
        assert _extract_symbol_from_old_str("\n\ndef foo():") == "foo"
        assert _extract_symbol_from_old_str("x = 1") is None
        assert _extract_symbol_from_old_str("") is None
        assert _extract_symbol_from_old_str("   def foo():") == "foo"

    def test_extract_symbol_from_non_py_old_str(self) -> None:
        from sunaba.tools.edit_engine import _extract_symbol_from_old_str
        assert _extract_symbol_from_old_str("def foo(): # type: ignore") == "foo"

    def test_extract_from_decorated_with_blank_lines(self) -> None:
        from sunaba.tools.edit_engine import _extract_symbol_from_old_str
        old = "\n\n# some comment\n@decorator\ndef foo():\n    pass\n"
        assert _extract_symbol_from_old_str(old) == "foo"

    # ── _is_bare_signature unit tests ────────────────────────────────

    def test_is_bare_signature(self) -> None:
        from sunaba.tools.edit_engine import _is_bare_signature
        assert _is_bare_signature("def foo():") is True
        assert _is_bare_signature("async def fetch():") is True
        assert _is_bare_signature("class Bar:") is True
        assert _is_bare_signature("@decorator\ndef foo():") is True
        assert _is_bare_signature("# comment\ndef foo():\n") is True
        assert _is_bare_signature("def foo(") is True  # multi-line sig start
        # Multi-line decorators and multi-line signatures are still bare
        # (PR #629 review: the old line scan rejected the continuation
        # lines and re-opened the unsafe string fallback).
        assert _is_bare_signature(
            "@decorator(\n    arg1,\n    arg2,\n)\ndef foo():"
        ) is True
        assert _is_bare_signature(
            "def foo(\n    a: int,\n    b: str = 'x',\n) -> None:"
        ) is True
        # Anything carrying a body line is NOT bare -- even mis-indented
        # bodies that only the whitespace-flexible matcher can place.
        assert _is_bare_signature("def foo():\npass") is False
        assert _is_bare_signature("def foo():\n    return 1") is False
        assert _is_bare_signature(
            "@decorator(\n    arg,\n)\ndef foo():\n    return 1"
        ) is False
        assert _is_bare_signature("x = 1") is False
        assert _is_bare_signature("") is False
        # Complete ONE-LINER definitions (inline body, overload stubs)
        # are whole definitions: string-replacing them orphans nothing,
        # so they keep the exact-string fallback.
        assert _is_bare_signature("def foo(): pass") is False
        assert _is_bare_signature("def foo(): return 1") is False
        assert _is_bare_signature(
            "@overload\ndef p(x: int) -> int: ..."
        ) is False

    def test_one_liner_stub_old_str_keeps_string_fallback(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Replacing an @overload stub by its exact text must still work.

        The symbol is ambiguous for AST resolution (three same-name
        defs), but the stub text is unique in the file and complete, so
        the exact-string fallback is safe and must not be blocked by
        the bare-signature guard.
        """
        f = tmp_path / "mod.py"
        src = (
            "from typing import overload\n\n\n"
            "@overload\ndef process(x: int) -> int: ...\n"
            "@overload\ndef process(x: str) -> str: ...\n"
            "def process(x):\n    return x\n"
        )
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x: bytes) -> bytes: ...",
            old_str="def process(x: int) -> int: ...",
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "bytes" in result

    # ── AST path is taken on .py files ───────────────────────────────

    def test_def_old_str_triggers_ast_on_py_file(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 99\n",
            old_str="def foo():",
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "return 99" in result

    def test_class_old_str_triggers_ast_on_py_file(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        f.write_text("class C:\n    def m(self):\n        return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="class C:\n    def m(self):\n        return 99\n",
            old_str="class C:",
        )
        assert "Error" not in result, result
        assert "replaced" in result

    def test_async_def_old_str_triggers_ast_on_py_file(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        f.write_text("async def fetch():\n    return 0\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="async def fetch():\n    return 1\n",
            old_str="async def fetch():",
        )
        assert "Error" not in result, result
        assert "replaced" in result

    # ── Non-.py files bypass AST path ────────────────────────────────

    def test_non_py_file_bypasses_ast_path(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "data.txt"
        f.write_text("hello world\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({"/sandbox/data.txt": str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name="/sandbox/data.txt",
            file_contents="goodbye\n",
            old_str="hello world",
        )
        assert "Error" not in result, result
        assert "replaced" in result

    def test_non_py_def_like_old_str_bypasses_ast(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "data.txt"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({"/sandbox/data.txt": str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name="/sandbox/data.txt",
            file_contents="def bar():\n    return 2\n",
            old_str="def foo():",
        )
        assert "Error" not in result, result
        assert "replaced" in result

    # ── AST no-change returns "No changes" (no string fallthrough) ───

    def test_ast_no_change_reports_no_changes(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A no-op AST edit must NOT fall through to string matching.

        Falling through used to re-match the signature line and splice
        a duplicate body into the file (silent corruption).
        """
        src = "def foo():\n    return 1\n"
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 1",
            old_str="def foo():\n    return 1",
        )
        assert "Error" not in result, result
        assert "No changes" in result
        assert "'foo'" in result
        assert f.read_text(encoding="utf-8") == src

    def test_bare_signature_no_change_does_not_duplicate_body(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Regression: bare-signature old_str + identical file_contents.

        The old fallthrough replaced the ``def foo():`` line with the
        whole definition, leaving ``return 1`` duplicated.
        """
        src = "def foo():\n    return 1\n"
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 1\n",
            old_str="def foo():",
        )
        assert "No changes" in result, result
        assert f.read_text(encoding="utf-8") == src

    # ── Ambiguous symbol on .py with definition old_str ──────────────

    def test_ambiguous_bare_signature_surfaces_ast_error(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Bare signature + complete definition must not string-fall-through.

        The old fallthrough replaced only the signature line and left
        the old body orphaned in the file.  The AST ambiguity error
        (with its ``line=`` guidance) is surfaced instead.
        """
        f = tmp_path / "mod.py"
        src = (
            "def process(x):\n    return x\n\n\n"
            "class Handler:\n    def process(self, x):\n        return x\n"
        )
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x):\n    return x + 1\n",
            old_str="def process(x):",
        )
        assert "ambiguous" in result
        assert "Retry with line=" in result
        assert "Note: old_str looks like a bare 'process' signature" in result
        assert f.read_text(encoding="utf-8") == src

    def test_full_def_old_str_still_falls_through_on_ast_failure(
        self, tmp_path, monkeypatch,
    ) -> None:
        """AST ambiguity degrades to string matching for full-definition old_str.

        When old_str contains the whole old definition, an exact string
        replacement is safe (nothing is orphaned), so the fallthrough
        is kept.
        """
        f = tmp_path / "mod.py"
        src = (
            "def process(x):\n    return x\n\n\n"
            "class Handler:\n    def process(self, x):\n        return x\n"
        )
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x):\n    return x + 1",
            old_str="def process(x):\n    return x",
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "return x + 1" in result

    def test_bare_signature_rename_still_falls_through(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Signature-to-signature rename keeps working via string match.

        file_contents is itself a bare signature (not a complete
        definition), so replacing just the signature line is exactly
        what the caller wants.
        """
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo_renamed():",
            old_str="def foo():",
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "foo_renamed" in result

    def test_near_miss_error_includes_ast_failure_note(
        self, tmp_path, monkeypatch,
    ) -> None:
        """When AST failed first and string matching finds nothing, say so."""
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="x = 1",
            old_str="def nope():",
        )
        assert "Error: old_str not found" in result
        assert "Note: AST resolution for 'nope' was attempted first" in result
        assert "not found" in result

    # ── preserve and line params are passed through ──────────────────

    def test_preserve_param_passed_to_ast_driver(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 99\n",
            old_str="def foo():",
            preserve="none",
        )
        assert "Error" not in result, result
        assert "replaced" in result

    def test_line_param_passed_to_ast_driver(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        src = (
            "from typing import overload\n\n\n"
            "@overload\ndef process(x: int) -> int: ...\n"
            "@overload\ndef process(x: str) -> str: ...\n"
            "def process(x):\n    return x\n"
        )
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x):\n    return x + 1\n",
            old_str="def process(x):",
            line=9,
        )
        assert "Error" not in result, result
        assert "replaced" in result

    # ── ast= parameter (issue #632) ─────────────────────────────────

    def test_ast_false_skips_ast_for_docstring_only_edit(
        self, tmp_path, monkeypatch,
    ) -> None:
        """ast=False must do a plain string replace, not a whole-body AST edit.

        This is the motivating case from #632 (via shiori#287): old_str
        is the full old definition and only the docstring changes, but
        the def-line trigger used to route this through AST resolution
        regardless of intent.  With ast=False it is a plain string
        match and the AST driver is never invoked.
        """
        src = (
            "def foo():\n"
            "    \"\"\"old doc.\"\"\"\n"
            "    return 1\n"
        )
        f = tmp_path / "mod.py"
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents=(
                "def foo():\n"
                "    \"\"\"new doc.\"\"\"\n"
                "    return 1\n"
            ),
            old_str=src,
            ast=False,
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "new doc" in result

    def test_ast_false_bare_signature_replaces_only_signature_line(
        self, tmp_path, monkeypatch,
    ) -> None:
        """ast=False on a bare-signature old_str is a plain line replace.

        No AST path is attempted at all, so the bare-signature safety
        net (which exists to force AST resolution) does not apply --
        the caller explicitly opted out of AST semantics.
        """
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo_renamed():",
            old_str="def foo():",
            ast=False,
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "foo_renamed" in result

    def test_ast_true_forces_resolution_on_success(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 99\n",
            old_str="def foo():",
            ast=True,
        )
        assert "Error" not in result, result
        assert "replaced" in result
        assert "return 99" in result

    def test_ast_true_on_non_py_file_errors(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "data.txt"
        f.write_text("hello world\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({"/sandbox/data.txt": str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name="/sandbox/data.txt",
            file_contents="goodbye\n",
            old_str="hello world",
            ast=True,
        )
        assert "Error: ast=True requires a .py file" in result
        assert f.read_text(encoding="utf-8") == "hello world\n"

    def test_ast_true_with_non_definition_old_str_errors(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "mod.py"
        f.write_text("x = 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="x = 2\n",
            old_str="x = 1",
            ast=True,
        )
        assert "Error: ast=True requires old_str to start with a" in result
        assert f.read_text(encoding="utf-8") == "x = 1\n"

    def test_ast_true_does_not_fall_back_on_resolution_failure(
        self, tmp_path, monkeypatch,
    ) -> None:
        """ast=True must surface the AST error, never degrade to string match.

        Same ambiguous-symbol setup as test_full_def_old_str_still_falls_through_on_ast_failure,
        where ast=None (default) falls back to a safe string replace.
        ast=True forbids that fallback even though it would have been
        safe here, because the caller explicitly asked for AST-only
        semantics.
        """
        f = tmp_path / "mod.py"
        src = (
            "def process(x):\n    return x\n\n\n"
            "class Handler:\n    def process(self, x):\n        return x\n"
        )
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x):\n    return x + 1",
            old_str="def process(x):\n    return x",
            ast=True,
        )
        assert "Error" in result
        assert "ambiguous" in result
        assert f.read_text(encoding="utf-8") == src

    def test_ast_none_default_unchanged_behavior(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Omitting ast entirely keeps the pre-#632 implicit trigger."""
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 99\n",
            old_str="def foo():",
        )
        assert "Error" not in result, result
        assert "replaced" in result


# ===================================================================
# Body-loss guard on the AST path (sunaba PR #822)
# ===================================================================


class _WritingFakeContainer(_FakeContainerWithIO):
    """A fake whose ``put_archive`` really writes the file.

    ``_FakeContainerWithIO`` stubs ``put_archive`` out, which is fine
    for the AST path (the driver edits the file directly) but hides
    what a string-replace edit produced -- and "what landed on disk"
    is exactly what these tests are about.
    """

    def put_archive(self, path, data):  # noqa: ANN001, ANN201
        import io
        import posixpath
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                dest = posixpath.join(path, member.name)
                real_path = self.path_map.get(dest, dest)
                extracted = tar.extractfile(member)
                if extracted is None:
                    continue
                with open(real_path, "wb") as fh:
                    fh.write(extracted.read())
        return True


class TestBodyLossGuard:
    """An AST edit must never silently drop a definition's body.

    In PR #822 an ``edit_file`` call meant to update a docstring
    removed the body of ``done()``, including its ``_control.stop()``
    call: ``old_str`` carried the ``def`` line, so the edit resolved
    through AST, which replaces the whole definition -- and a
    docstring-only replacement is a legal body, so the file still
    parsed and the gate stayed green.
    """

    SRC = (
        "class Runner:\n"
        "    def done(self):\n"
        '        """Old doc."""\n'
        "        for item in self.items:\n"
        "            if item.failed:\n"
        "                raise RuntimeError(item)\n"
        "        self._control.stop()\n"
        "        return True\n"
    )

    def _docker(self, tmp_path, monkeypatch):  # noqa: ANN001, ANN202
        f = tmp_path / "mod.py"
        f.write_text(self.SRC, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_WritingFakeContainer({POSIX: str(f)})),
        )
        return f

    def test_docstring_only_replacement_is_refused(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = self._docker(tmp_path, monkeypatch)
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='    def done(self):\n        """New doc."""\n',
            old_str='    def done(self):\n        """Old doc."""\n',
        )
        assert "Error" in result, result
        assert "'done'" in result
        assert "delete" in result and "body" in result
        assert "ast=False" in result
        # The file is untouched -- the guard runs before any write.
        assert f.read_text(encoding="utf-8") == self.SRC

    def test_refusal_names_the_statements_at_risk(
        self, tmp_path, monkeypatch,
    ) -> None:
        self._docker(tmp_path, monkeypatch)
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='    def done(self):\n        """New doc."""\n',
            old_str='    def done(self):\n        """Old doc."""\n',
        )
        # for / self._control.stop() / return -- the three statements the
        # #822 edit deleted.
        assert "3 statements" in result

    def test_ast_false_performs_a_literal_replacement(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The escape hatch the refusal points at must actually work."""
        f = self._docker(tmp_path, monkeypatch)
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='    def done(self):\n        """New doc."""\n',
            old_str='    def done(self):\n        """Old doc."""\n',
            ast=False,
        )
        assert "Error" not in result, result
        text = f.read_text(encoding="utf-8")
        assert '"""New doc."""' in text
        assert '"""Old doc."""' not in text
        # The body survives a literal replacement.
        assert "self._control.stop()" in text
        assert "raise RuntimeError(item)" in text
        assert "return True" in text

    def test_genuinely_shorter_body_still_replaces(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A real new body goes through, however much smaller."""
        f = self._docker(tmp_path, monkeypatch)
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents=(
                "    def done(self):\n"
                '        """New doc."""\n'
                "        return False\n"
            ),
            old_str="    def done(self):\n",
        )
        assert "Error" not in result, result
        text = f.read_text(encoding="utf-8")
        assert "return False" in text
        assert "self._control.stop()" not in text

    def test_module_level_function_is_guarded_too(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "mod.py"
        src = 'def run():\n    """Doc."""\n    return compute()\n'
        f.write_text(src, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='def run():\n    """Better doc."""\n',
            old_str="def run():",
        )
        assert "Error" in result, result
        assert "'run'" in result
        assert f.read_text(encoding="utf-8") == src

    def test_docstring_only_old_definition_is_not_blocked(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Nothing to lose: the old body is a docstring too."""
        f = tmp_path / "mod.py"
        f.write_text('def run():\n    """Doc."""\n', encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='def run():\n    """Better doc."""\n',
            old_str="def run():",
        )
        assert "Error" not in result, result
        assert "Better doc." in f.read_text(encoding="utf-8")

    def test_ast_true_is_guarded_as_well(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = self._docker(tmp_path, monkeypatch)
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='    def done(self):\n        """New doc."""\n',
            old_str="    def done(self):",
            ast=True,
        )
        assert "Error" in result, result
        assert "'done'" in result
        assert f.read_text(encoding="utf-8") == self.SRC

    def test_guard_reads_content_not_the_shape_of_old_str(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A def line in old_str is not by itself an error."""
        f = self._docker(tmp_path, monkeypatch)
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents=(
                "    def done(self):\n"
                '        """Old doc."""\n'
                "        self._control.stop()\n"
                "        return True\n"
            ),
            old_str="    def done(self):",
        )
        assert "Error" not in result, result
        assert "self._control.stop()" in f.read_text(encoding="utf-8")


class TestBodyLossHelpers:
    """Unit tests for the pure-string half of the guard."""

    def test_docstring_only_body_is_body_free(self) -> None:
        from sunaba.tools.edit_engine import _is_body_free, _sole_definition

        node = _sole_definition('def f():\n    """Doc."""\n')
        assert node is not None
        assert _is_body_free(node) is True

    def test_stub_bodies_are_not_body_free(self) -> None:
        """``pass`` / ``...`` are bodies the caller wrote deliberately."""
        from sunaba.tools.edit_engine import _is_body_free, _sole_definition

        for text in ("def f():\n    pass\n", "def f(): ...\n"):
            node = _sole_definition(text)
            assert node is not None
            assert _is_body_free(node) is False, text

    def test_two_definitions_are_not_a_sole_definition(self) -> None:
        from sunaba.tools.edit_engine import _sole_definition

        assert _sole_definition("def a():\n    pass\n\n\ndef b():\n    pass\n") is None
        assert _sole_definition("x = 1") is None

    def test_no_error_when_symbol_is_absent(self) -> None:
        from sunaba.tools.edit_engine import _body_loss_error

        assert _body_loss_error(
            "def other():\n    return 1\n",
            "missing",
            'def missing():\n    """Doc."""\n',
            "/sandbox/mod.py",
        ) is None

    def test_line_narrows_the_candidates(self) -> None:
        from sunaba.tools.edit_engine import _body_loss_error

        src = (
            'def f():\n    """Doc."""\n\n\n'
            "class C:\n    def f(self):\n        return 1\n"
        )
        new = 'def f():\n    """New."""\n'
        # Line 1 selects the module-level stub: nothing to lose.
        assert _body_loss_error(src, "f", new, "/sandbox/mod.py", line=1) is None
        # Line 6 selects the method, which has a real body.
        blocked = _body_loss_error(src, "f", new, "/sandbox/mod.py", line=6)
        assert blocked is not None
        assert "'f'" in blocked


# ===================================================================
# The guard must resolve what the driver resolves
# ===================================================================


DECORATED_SRC = (
    "class Foo:\n"            # 1
    "    @property\n"         # 2
    "    def bar(self):\n"    # 3
    '        """Doc."""\n'    # 4
    "        return self._bar\n"  # 5
)

NESTED_SRC = (
    "def helper():\n"          # 1
    "    def helper():\n"      # 2
    '        """stub"""\n'     # 3
    "    return helper\n"      # 4
)


class TestGuardResolvesLikeTheDriver:
    """The pre-flight has to judge the definition the driver replaces.

    ``ast.FunctionDef.lineno`` is the ``def`` line, so a decorator sits
    outside it -- while the driver's span starts at the first decorator
    and its own ambiguity error tells callers to "retry with
    line=<start line>", i.e. the decorator's line.  On that line the
    guard used to filter the real target out and pass.
    """

    def test_line_on_the_decorator_still_refuses(self) -> None:
        """The measured false pass: line=2 is the @property line."""
        from sunaba.tools.edit_engine import _body_loss_error

        new = '    @property\n    def bar(self):\n        """New."""\n'
        for line in (2, 3, None):
            blocked = _body_loss_error(
                DECORATED_SRC, "bar", new, "/sandbox/mod.py", line,
            )
            assert blocked is not None, f"line={line} passed"
            assert "'bar'" in blocked
            assert "Foo.bar" in blocked

    def test_edit_file_refuses_on_the_decorator_line(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "mod.py"
        f.write_text(DECORATED_SRC, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='    @property\n    def bar(self):\n        """New."""\n',
            old_str="    def bar(self):",
            line=2,
        )
        assert "Error" in result, result
        assert "return self._bar" in f.read_text(encoding="utf-8")

    def test_nested_same_name_stub_is_not_blocked(self) -> None:
        """The measured false refusal: the driver targets the inner stub."""
        from sunaba.tools.edit_engine import _body_loss_error

        new = 'def helper():\n    """new stub"""\n'
        for line in (2, None):
            assert _body_loss_error(
                NESTED_SRC, "helper", new, "/sandbox/mod.py", line,
            ) is None, f"line={line} refused"

    def test_edit_file_allows_the_nested_stub_edit(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "mod.py"
        f.write_text(NESTED_SRC, encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = edit_file(
            container_id="abc123",
            file_name=POSIX,
            file_contents='    def helper():\n        """new stub"""\n',
            old_str="    def helper():",
            line=2,
        )
        assert "Error" not in result, result
        text = f.read_text(encoding="utf-8")
        assert "new stub" in text
        assert "return helper" in text

    def test_outer_definition_is_still_guarded(self) -> None:
        """line=1 contains only the outer helper, which has a real body."""
        from sunaba.tools.edit_engine import _body_loss_error

        blocked = _body_loss_error(
            NESTED_SRC,
            "helper",
            'def helper():\n    """new stub"""\n',
            "/sandbox/mod.py",
            line=1,
        )
        assert blocked is not None
        assert "lines 1-4" in blocked

    def test_unresolvable_symbol_is_not_a_refusal(self) -> None:
        """The driver edits nothing there, so there is nothing to guard."""
        from sunaba.tools.edit_engine import _body_loss_error

        new = 'def missing():\n    """Doc."""\n'
        assert _body_loss_error(
            "def other():\n    return 1\n", "missing", new, "/sandbox/mod.py",
        ) is None
        # line outside every match
        assert _body_loss_error(
            "def missing():\n    return 1\n", "missing", new,
            "/sandbox/mod.py", line=99,
        ) is None
        # unparseable source: the driver fails on it too
        assert _body_loss_error(
            "def missing(:\n    return 1\n", "missing", new, "/sandbox/mod.py",
        ) is None

    def test_resolution_failure_refuses_rather_than_passes(
        self, monkeypatch,
    ) -> None:
        """An unexpected resolver failure must not read as "safe"."""
        from sunaba.tools import edit_engine

        def boom(*_a, **_k):
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(edit_engine, "_resolve_symbol", boom)
        blocked = edit_engine._body_loss_error(
            DECORATED_SRC,
            "bar",
            '    def bar(self):\n        """New."""\n',
            "/sandbox/mod.py",
        )
        assert blocked is not None
        assert "resolver exploded" in blocked
        assert "ast=False" in blocked


# -- Differential: host-side resolver vs the real in-container driver --

#: (label, source, symbol, line).  Each case is resolved twice -- once by
#: _resolve_symbol on the host, once by the real _EDIT_SYMBOL_DRIVER
#: source executed by the fake container -- and the two must agree.
_RESOLUTION_CASES = [
    ("decorated: line on the decorator", DECORATED_SRC, "bar", 2),
    ("decorated: line on the def", DECORATED_SRC, "bar", 3),
    ("decorated: line in the body", DECORATED_SRC, "bar", 5),
    ("decorated: no line", DECORATED_SRC, "bar", None),
    ("decorated: the class itself", DECORATED_SRC, "Foo", None),
    ("nested: inner def line", NESTED_SRC, "helper", 2),
    ("nested: outer only", NESTED_SRC, "helper", 1),
    ("nested: ambiguous without line", NESTED_SRC, "helper", None),
    ("nested: qualified inner", NESTED_SRC, "helper.helper", None),
    ("ambiguous module fn vs method", AMBIG_SRC, "process", None),
    ("ambiguous resolved by line", AMBIG_SRC, "process", 6),
    ("overload stub by line", OVERLOAD_SRC, "process", 6),
    ("overload: decorator line", OVERLOAD_SRC, "process", 4),
    ("symbol not found", MODULE_SRC, "nope", None),
    ("line outside every match", MODULE_SRC, "foo", 99),
    (
        "multi-line decorator, line on its first row",
        "@deco(\n    arg,\n)\ndef wrapped():\n    return 1\n",
        "wrapped",
        1,
    ),
    (
        "async def with decorator",
        "@deco\nasync def fetch():\n    return await x()\n",
        "fetch",
        1,
    ),
    (
        "definition inside an if block",
        "if True:\n    def conditional():\n        return 1\n",
        "conditional",
        None,
    ),
    (
        "method of a nested class",
        "class Outer:\n    class Inner:\n        def m(self):\n            return 1\n",
        "Inner.m",
        None,
    ),
]


class TestResolutionMatchesTheDriver:
    """Pin the host-side pre-flight against the driver it mirrors.

    They are two implementations of one rule: the driver is a
    standalone script executed by a bare ``python3`` inside the
    container, with no access to this package, so it cannot import the
    host-side resolver.  This test is what keeps them from drifting --
    it runs the real ``_EDIT_SYMBOL_DRIVER`` source (via the fake
    container that execs it) and the host resolver over the same
    inputs, and fails if either changes without the other.
    """

    @staticmethod
    def _driver_resolution(tmp_path, source, symbol, line):  # noqa: ANN001, ANN205
        """Resolve via the real driver; returns its ``resolved`` dict or None."""
        f = tmp_path / "mod.py"
        f.write_text(source, encoding="utf-8")
        client = _FakeClient(_FakeContainer({POSIX: str(f)}))
        out = edit_symbol_in_container(
            client, "abc123", POSIX, symbol,
            "def _probe_replacement():\n    return 1\n", line,
        )
        if out.get("status") != "ok":
            return None
        return out["resolved"]

    @pytest.mark.parametrize(
        ("label", "source", "symbol", "line"),
        _RESOLUTION_CASES,
        ids=[c[0] for c in _RESOLUTION_CASES],
    )
    def test_same_definition_as_the_driver(
        self, tmp_path, label, source, symbol, line,
    ) -> None:
        from sunaba.tools.edit_engine import _resolve_symbol

        driver = self._driver_resolution(tmp_path, source, symbol, line)
        host = _resolve_symbol(source, symbol, line)

        if driver is None:
            assert host is None, (
                f"{label}: the driver resolves nothing, the guard resolved "
                f"{host.qualname if host else None}"
            )
            return

        assert host is not None, f"{label}: the driver resolved {driver}, the guard did not"
        assert host.qualname == driver["qualname"], label
        assert host.kind == driver["kind"], label
        assert host.start == driver["start_line"], label
        assert host.end == driver["end_line"], label
