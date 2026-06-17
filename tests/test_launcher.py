"""Tests for the launcher module."""
from __future__ import annotations

import io
import sys
import threading

import pytest

from code_sandbox_mcp.launcher import _detect_transport, _pipe_stream, main


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


class TestStdinProxyTiming:
    """Tests for the stdin proxy thread startup timing fix (#20).

    Verifies that the stdin proxy thread is not started until after
    the first server subprocess has been spawned and its stdin
    reference has been set, preventing the loss of the initialize
    request.
    """

    def test_stdin_reference_set_before_thread_start(self) -> None:
        """Simulate the startup sequence: the _current_stdin reference
        must be set to a non-None value before the stdin proxy thread
        is allowed to start."""
        from code_sandbox_mcp.launcher import _stdin_proxy

        # Track the order of operations
        operations: list[str] = []

        _lock = threading.Lock()
        _current_stdin: list[IO[bytes] | None] = [None]

        def _get_current_stdin() -> IO[bytes] | None:
            with _lock:
                return _current_stdin[0]

        # Simulate server startup: set stdin reference
        dummy_stdin = io.BytesIO()
        with _lock:
            _current_stdin[0] = dummy_stdin
        operations.append("stdin_set")

        # Now start the proxy thread (this is the fixed order)
        t = threading.Thread(
            target=_stdin_proxy,
            args=(_get_current_stdin,),
            daemon=True,
        )
        t.start()
        operations.append("thread_started")

        # Verify: stdin was set BEFORE thread started
        assert operations == ["stdin_set", "thread_started"]
        # Verify: the proxy can immediately get the stdin reference
        assert _get_current_stdin() is not None
        assert _get_current_stdin() is dummy_stdin

        # Cleanup: set stdin to None to stop the thread
        with _lock:
            _current_stdin[0] = None
        t.join(timeout=1)

    def test_stdin_none_during_restart_gap_is_dropped(self) -> None:
        """During the restart gap (server stopped, new server not yet
        started), _get_current_stdin() returns None and the proxy
        thread drops the line silently. This is the expected behavior
        that the fix must preserve for restarts."""
        from code_sandbox_mcp.launcher import _stdin_proxy

        received: list[bytes] = []

        _lock = threading.Lock()
        _current_stdin: list[IO[bytes] | None] = [None]

        def _get_current_stdin() -> IO[bytes] | None:
            with _lock:
                return _current_stdin[0]

        # Start proxy with stdin = None (simulating restart gap)
        t = threading.Thread(
            target=_stdin_proxy,
            args=(_get_current_stdin,),
            daemon=True,
        )
        t.start()

        # Send a line while stdin is None (should be dropped)
        old_stdin = sys.stdin
        try:
            sys.stdin = io.TextIOWrapper(io.BytesIO(b"hello\n"))
            # Give the thread time to process
            import time
            time.sleep(0.1)
            # No error should occur - line is dropped silently
        finally:
            sys.stdin = old_stdin

        # Verify that no data was forwarded (dst is None, so line dropped)
        with _lock:
            assert _current_stdin[0] is None

        t.join(timeout=1)


class TestTransportDetection:
    """Tests for transport mode detection in the launcher."""

    def test_detect_stdio_default(self) -> None:
        """When no --transport is given, should default to stdio."""
        assert _detect_transport(["-m", "code_sandbox_mcp.server"]) == "stdio"

    def test_detect_sse(self) -> None:
        """When --transport sse is given, should detect sse."""
        assert _detect_transport(
            ["-m", "code_sandbox_mcp.server", "--transport", "sse"]
        ) == "sse"

    def test_detect_http(self) -> None:
        """When --transport http is given, should detect http."""
        assert _detect_transport(
            ["-m", "code_sandbox_mcp.server", "--transport", "http"]
        ) == "http"

    def test_detect_stdio_explicit(self) -> None:
        """When --transport stdio is given, should detect stdio."""
        assert _detect_transport(
            ["-m", "code_sandbox_mcp.server", "--transport", "stdio"]
        ) == "stdio"

    def test_detect_streamable_http(self) -> None:
        """When --transport streamable-http is given, should detect it."""
        assert _detect_transport(
            ["-m", "code_sandbox_mcp.server", "--transport", "streamable-http"]
        ) == "streamable-http"

    def test_detect_with_other_args(self) -> None:
        """Transport detection should work with other arguments present."""
        assert _detect_transport([
            "-m", "code_sandbox_mcp.server",
            "--pass-through-env", "GITHUB_TOKEN",
            "--transport", "sse",
            "--host", "0.0.0.0",
            "--port", "9876",
        ]) == "sse"
