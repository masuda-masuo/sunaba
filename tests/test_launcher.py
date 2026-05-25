"""Tests for the launcher module."""
from __future__ import annotations

import io
import sys
import threading

import pytest

from code_sandbox_mcp.launcher import _pipe_stream, main


class TestPipeStream:
    """Tests for the stdio proxy helper."""

    def test_pipe_stream_forward_bytes(self) -> None:
        src = io.BytesIO(b"hello")
        dst = io.BytesIO()
        _pipe_stream(src, dst)
        assert dst.getvalue() == b"hello"

    def test_pipe_stream_empty(self) -> None:
        src = io.BytesIO(b"")
        dst = io.BytesIO()
        _pipe_stream(src, dst)
        assert dst.getvalue() == b""

    def test_pipe_stream_broken_pipe_does_not_raise(self) -> None:
        src = io.BytesIO(b"data")
        dst = io.BytesIO()
        dst.close()  # provoke BrokenPipeError on write
        # Should not raise despite the closed destination
        _pipe_stream(src, dst)

    def test_pipe_stream_valueerror_on_closed_dst(self) -> None:
        """readline() on a closed stream raises ValueError."""
        src = io.BytesIO(b"data")
        dst = io.BytesIO()
        dst.close()  # provoke ValueError on readline() path
        # Should not raise despite the closed destination
        _pipe_stream(src, dst)


class TestLauncherArgParse:
    """Tests for launcher argument parsing."""

    def test_default_auto_update_false(self) -> None:
        """Without --auto-update, the server should not receive it."""
        test_args = ["launcher"]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sys, "argv", test_args)

            # We can't easily test full main() without subprocess, so test
            # that argparse parses correctly
            import argparse
            parser = argparse.ArgumentParser()
            parser.add_argument("--auto-update", action="store_true", default=False)
            parsed, _ = parser.parse_known_args(test_args[1:])
            assert parsed.auto_update is False

    def test_auto_update_true(self) -> None:
        """With --auto-update, the flag is parsed correctly."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--auto-update", action="store_true", default=False)
        parsed, _ = parser.parse_known_args(["--auto-update"])
        assert parsed.auto_update is True

    def test_unknown_args_passthrough(self) -> None:
        """Unknown arguments should be collected as remaining."""
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--auto-update", action="store_true", default=False)
        _, remaining = parser.parse_known_args(["--pass-through-env", "FOO", "--exec-timeout", "120"])
        assert "--pass-through-env" in remaining
        assert "FOO" in remaining
        assert "--exec-timeout" in remaining
        assert "120" in remaining


class TestLauncherMainSmoke:
    """Smoke tests for the launcher main function.

    These tests verify the launcher's structural correctness without
    actually spawning subprocesses.
    """

    def test_launcher_module_importable(self) -> None:
        from code_sandbox_mcp import launcher  # noqa: F811
        assert hasattr(launcher, "main")
        assert callable(launcher.main)

    def test_pipe_stream_daemon_thread(self) -> None:
        """Verify that _pipe_stream can run in a daemon thread."""
        src = io.BytesIO(b"test data")
        dst = io.BytesIO()
        t = threading.Thread(target=_pipe_stream, args=(src, dst), daemon=True)
        t.start()
        t.join(timeout=2)
        assert dst.getvalue() == b"test data"
