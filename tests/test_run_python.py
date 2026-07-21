"""Tests for the run_python MCP tool."""

from __future__ import annotations

import json
from io import StringIO

from src.sunaba.tools.run_python import run_python

# ---------------------------------------------------------------------------
# Fake container / client for tests
# ---------------------------------------------------------------------------


class _FakeRunPythonClient:
    """Fake Docker client whose containers.get returns a fake container."""

    def __init__(self, container) -> None:
        self._container = container

    class _Containers:
        def __init__(self, c) -> None:
            self._c = c

        def get(self, _cid):
            return self._c

    @property
    def containers(self):
        return _FakeRunPythonClient._Containers(self._container)


class _FakeRunPythonContainer:
    """Emulates the in-container shell for the run_python runner."""

    def exec_run(self, cmd, **kwargs):
        import base64 as _b64
        import sys

        shell_cmd = cmd[-1]
        blob = (
            shell_cmd.split("echo ", 1)[1]
            .split(" | base64 -d", 1)[0]
            .strip("'\"")
        )
        runner_src = _b64.b64decode(blob).decode("utf-8")

        runner_globals = {}
        buf = StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            try:
                exec(compile(runner_src, "<runner>", "exec"), runner_globals)
            except SystemExit:
                pass
        finally:
            sys.stdout = old
        return 0, (buf.getvalue().encode("utf-8"), b"")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunPython:
    """Tests for run_python execution inside the sandbox container."""

    @staticmethod
    def _run(code: str, monkeypatch, *, working_dir: str = "", **kwargs) -> dict:
        """Invoke run_python with a patched _docker via monkeypatch."""
        fake_c = _FakeRunPythonClient(_FakeRunPythonContainer())
        monkeypatch.setattr(
            "src.sunaba.tools.run_python._docker",
            lambda: fake_c,
        )
        return json.loads(
            run_python("abc123", code, working_dir, **kwargs)
        )

    def test_stdout_roundtrip(self, monkeypatch) -> None:
        """Simple stdout output returns correctly."""
        result = self._run("print('hello world')", monkeypatch)
        assert result["status"] == "ok"
        assert result["stdout"] == "hello world\n"
        assert result["stderr"] == ""
        assert result["exit_code"] == 0
        assert result["stdout_truncated"] is False
        assert result["stdout_shown"] > 0

    def test_multi_line_with_quotes(self, monkeypatch) -> None:
        """Multi-line code with both single and double quotes works."""
        code = (
            "print(\"double's quote\")\n"
            "print('single\"quote')\n"
            "print('''triple''')\n"
            "x = 42\n"
        )
        result = self._run(code, monkeypatch)
        assert result["status"] == "ok"
        lines = result["stdout"].strip().split("\n")
        assert len(lines) == 3
        assert lines[0] == "double's quote"
        assert lines[1] == 'single"quote'
        assert lines[2] == "triple"

    def test_stderr_and_exit_code_3(self, monkeypatch) -> None:
        """Code printing to stderr and exiting 3 returns status error."""
        code = (
            "import sys\n"
            "print('error msg', file=sys.stderr)\n"
            "sys.exit(3)\n"
        )
        result = self._run(code, monkeypatch)
        assert result["status"] == "error"
        assert result["exit_code"] == 3
        assert "error msg" in result["stderr"]
        assert result["stdout"] == ""

    def test_no_output(self, monkeypatch) -> None:
        """Code with no output returns empty strings and exit_code 0."""
        result = self._run("x = 42\ny = x + 1", monkeypatch)
        assert result["status"] == "ok"
        assert result["stdout"] == ""
        assert result["stderr"] == ""
        assert result["exit_code"] == 0

    def test_exit_code_0_explicit(self, monkeypatch) -> None:
        """sys.exit(0) still reports status ok."""
        result = self._run("import sys; sys.exit(0)", monkeypatch)
        assert result["status"] == "ok"
        assert result["exit_code"] == 0

    def test_syntax_error(self, monkeypatch) -> None:
        """Code with a syntax error reports error and non-zero exit."""
        result = self._run("print(", monkeypatch)
        assert result["status"] == "error"
        assert result["exit_code"] != 0
        assert "SyntaxError" in result["stderr"]

    def test_runtime_exception(self, monkeypatch) -> None:
        """Code that raises an exception captures traceback in stderr."""
        result = self._run("raise ValueError('boom')", monkeypatch)
        assert result["status"] == "error"
        assert result["exit_code"] != 0
        assert "ValueError" in result["stderr"]
        assert "boom" in result["stderr"]

    def test_empty_code(self, monkeypatch) -> None:
        """Empty code runs successfully with no output."""
        result = self._run("", monkeypatch)
        assert result["status"] == "ok"
        assert result["stdout"] == ""
        assert result["stderr"] == ""
        assert result["exit_code"] == 0

    def test_temp_file_cleanup(self, monkeypatch, tmp_path) -> None:
        """Temporary script files are removed after execution."""
        import tempfile as _tf

        iso_tmp = tmp_path / "iso_tmp"
        iso_tmp.mkdir()
        monkeypatch.setattr(_tf, "tempdir", str(iso_tmp))

        self._run("print('hello')", monkeypatch)
        leftovers = list(iso_tmp.iterdir())
        assert len(leftovers) == 0, f"Temp files not cleaned up: {leftovers}"

    def test_temp_file_cleanup_on_failure(self, monkeypatch, tmp_path) -> None:
        """Temp script files are removed even when the code fails."""
        import tempfile as _tf

        iso_tmp = tmp_path / "iso_tmp_fail"
        iso_tmp.mkdir()
        monkeypatch.setattr(_tf, "tempdir", str(iso_tmp))

        result = self._run("import sys; sys.exit(7)", monkeypatch)
        assert result["exit_code"] == 7
        leftovers = list(iso_tmp.iterdir())
        assert len(leftovers) == 0, f"Temp files not cleaned up: {leftovers}"

    def test_stderr_truncation(self, monkeypatch) -> None:
        """Large stderr is truncated under the same line budget as stdout."""
        code = (
            "import sys\n"
            "for i in range(3000): print(i, file=sys.stderr)\n"
        )
        result = self._run(code, monkeypatch, max_lines=10)
        assert result["stderr_truncated"] is True
        assert result["stderr_shown"] <= 20
        assert result["stderr_total_lines"] >= 2999
        assert "omitted" in result["stderr"]

    def test_stderr_not_truncated_when_small(self, monkeypatch) -> None:
        """Small stderr passes through verbatim."""
        code = "import sys; print('boom', file=sys.stderr)"
        result = self._run(code, monkeypatch)
        assert result["stderr_truncated"] is False
        assert "boom" in result["stderr"]
        assert result["stderr_shown"] == result["stderr_total_lines"]

    def test_stderr_truncation_verbose_full(self, monkeypatch) -> None:
        """verbose=full disables stderr truncation too."""
        code = (
            "import sys\n"
            "for i in range(100): print(i, file=sys.stderr)\n"
        )
        result = self._run(code, monkeypatch, max_lines=10, verbose="full")
        assert result["stderr_truncated"] is False
        assert result["stderr_shown"] == result["stderr_total_lines"]

    def test_working_dir_default(self, monkeypatch) -> None:
        """Default working_dir is the repo root (/workspace)."""
        result = self._run("import os; print(os.getcwd())", monkeypatch)
        assert result["status"] == "ok"
        cwd = result["stdout"].strip()
        assert cwd == "/workspace", f"Expected /workspace, got {cwd!r}"

    def test_working_dir_custom(self, monkeypatch) -> None:
        """Custom working_dir changes the cwd for execution."""
        result = self._run(
            "import os; print(os.getcwd())", monkeypatch,
            working_dir="/tmp",
        )
        assert result["status"] == "ok"
        cwd = result["stdout"].strip()
        assert cwd == "/tmp", f"Expected /tmp, got {cwd!r}"

    # ------------------------------------------------------------------
    # Output truncation
    # ------------------------------------------------------------------

    def test_stdout_truncation_not_truncated(self, monkeypatch) -> None:
        """Small output is not flagged as truncated."""
        result = self._run("print('hello')", monkeypatch)
        assert result["status"] == "ok"
        assert result["stdout_truncated"] is False
        assert result["stdout"] == "hello\n"
        assert result["stdout_shown"] == result["stdout_total_lines"]

    def test_stdout_truncation(self, monkeypatch) -> None:
        """Output exceeding max_lines is truncated."""
        code = "for i in range(3000): print(i)"
        raw = self._run(code, monkeypatch, max_lines=10)
        assert raw["stdout_truncated"] is True
        assert raw["stdout_shown"] <= 20  # head + tail + omission line
        assert raw["stdout_total_lines"] >= 2999  # each print adds \n
        assert "omitted" in raw["stdout"]

    def test_stdout_truncation_verbose_full(self, monkeypatch) -> None:
        """verbose=full disables truncation."""
        code = "for i in range(100): print(i)"
        raw = self._run(code, monkeypatch, max_lines=10, verbose="full")
        assert raw["stdout_truncated"] is False
        assert raw["stdout_total_lines"] == raw["stdout_shown"]

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def test_container_not_found(self, monkeypatch) -> None:
        """Missing container returns a proper error JSON."""

        class _NotFoundClient:
            class _Containers:
                @staticmethod
                def get(_cid):
                    from docker.errors import NotFound as Nf

                    raise Nf("mock not found")

            @property
            def containers(self):
                return _NotFoundClient._Containers()

        monkeypatch.setattr(
            "src.sunaba.tools.run_python._docker",
            lambda: _NotFoundClient(),
        )
        raw = json.loads(run_python("nonexistent", "print(1)"))
        assert raw["status"] == "error"
        assert "not found" in raw["error"]
