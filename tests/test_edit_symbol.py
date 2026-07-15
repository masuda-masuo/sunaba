"""Tests for the edit_symbol_in_container driver (issue #581).

Covers the in-container driver via ``edit_symbol_in_container`` (symbol
resolution, decorator-inclusive ranges, re-indentation, seam blank-line
collapsing, post-edit syntax verification).

The MCP-facing ``edit_symbol`` tool was removed in #627; its AST
resolution is now integrated into ``write_file_sandbox``'s ``old_str``
path.
"""

import ast

import pytest

from src.sunaba.edit_verify import edit_symbol_in_container
from sunaba.tools.file import write_file_sandbox
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
        "src.sunaba.edit_verify.record_file_write", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "sunaba.edit_verify.record_file_write", lambda *a, **k: None
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
        assert "write_file_sandbox/transform_file" in out["error"]

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


# ===================================================================
# write_file_sandbox integration via old_str AST resolution
# ===================================================================


class _FakeContainerWithIO(_FakeContainer):
    """Extends _FakeContainer with cat/stat/mkdir for write_file_sandbox I/O."""

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
    """Build a _FakeClient with _FakeContainerWithIO for write_file_sandbox tests."""
    return _FakeClient(_FakeContainerWithIO(path_map))


class TestWriteFileSymbolIntegration:
    """write_file_sandbox + AST resolution integration (issue #627/#628)."""

    # ── _extract_symbol_from_old_str unit tests ──────────────────────

    def test_extract_symbol_from_def(self) -> None:
        from sunaba.tools.file import _extract_symbol_from_old_str
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
        from sunaba.tools.file import _extract_symbol_from_old_str
        assert _extract_symbol_from_old_str("def foo(): # type: ignore") == "foo"

    def test_extract_from_decorated_with_blank_lines(self) -> None:
        from sunaba.tools.file import _extract_symbol_from_old_str
        old = "\n\n# some comment\n@decorator\ndef foo():\n    pass\n"
        assert _extract_symbol_from_old_str(old) == "foo"

    # ── AST path is taken on .py files ───────────────────────────────

    def test_def_old_str_triggers_ast_on_py_file(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = write_file_sandbox(
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
        result = write_file_sandbox(
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
        result = write_file_sandbox(
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
        result = write_file_sandbox(
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
        result = write_file_sandbox(
            container_id="abc123",
            file_name="/sandbox/data.txt",
            file_contents="def bar():\n    return 2\n",
            old_str="def foo():",
        )
        assert "Error" not in result, result
        assert "replaced" in result

    # ── Fallthrough when AST returns no-change ───────────────────────

    def test_ast_no_change_falls_through_to_string_matching(
        self, tmp_path, monkeypatch,
    ) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = write_file_sandbox(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def foo():\n    return 1",
            old_str="def foo():\n    return 1",
        )
        # No change: AST driver returns changed=False, falls through
        # to string matching, still succeeds.
        assert "Error" not in result, result
        assert "replaced" in result

    # ── Ambiguous symbol on .py with definition old_str ──────────────

    def test_ambiguous_symbol_falls_through_to_string_match(
        self, tmp_path, monkeypatch,
    ) -> None:
        """AST ambiguity degrades gracefully to string matching.

        When the symbol is ambiguous, the AST driver returns an error;
        ``write_file_sandbox`` logs a DEBUG message and falls through
        to exact-string matching, which succeeds if the old_str text is
        unique in the file.
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
        result = write_file_sandbox(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x):\n    return x + 1\n",
            old_str="def process(x):",
        )
        # AST fails (ambiguous), falls through to string matching,
        # which finds the exact unique match on line 1.
        assert "Error" not in result, result
        assert "replaced" in result
        assert "return x + 1" in result

    # ── preserve and line params are passed through ──────────────────

    def test_preserve_param_passed_to_ast_driver(self, tmp_path, monkeypatch) -> None:
        f = tmp_path / "mod.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _write_fake_docker({POSIX: str(f)}),
        )
        result = write_file_sandbox(
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
        result = write_file_sandbox(
            container_id="abc123",
            file_name=POSIX,
            file_contents="def process(x):\n    return x + 1\n",
            old_str="def process(x):",
            line=9,
        )
        assert "Error" not in result, result
        assert "replaced" in result
