"""Tests for apply_patch_to_file journal recording (Issue #96)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestApplyPatchJournal:
    """Tests that apply_patch_to_file records journal entries (Issue #96)."""

    @patch("sunaba.edit_verify.record_file_write")
    def test_apply_patch_records_journal(
        self, mock_record: MagicMock, tmp_path
    ) -> None:
        import base64
        import io
        import sys

        from sunaba.edit_verify import apply_patch_to_file

        real = tmp_path / "test.py"
        real.write_text("hello\n", encoding="utf-8", newline="")
        posix = "/root/test.py"

        class _RunnerContainer:
            def exec_run(self, cmd, **kwargs):  # noqa: ANN001
                blob = cmd[-1].split("echo ", 1)[1].split(" | base64 -d", 1)[0].strip("'")
                src = base64.b64decode(blob).decode("utf-8")
                real_open = open

                def mapped_open(p, *a, **k):  # noqa: ANN001
                    return real_open(str(real) if p == posix else p, *a, **k)

                buf = io.StringIO()
                old = sys.stdout
                sys.stdout = buf
                try:
                    try:
                        exec(compile(src, "<runner>", "exec"), {"open": mapped_open})
                    except SystemExit:
                        pass
                finally:
                    sys.stdout = old
                return 0, (buf.getvalue().encode("utf-8"), b"")

        mock_client = MagicMock()
        mock_client.containers.get.return_value = _RunnerContainer()

        diff = "--- a/test.py\n+++ b/test.py\n@@ -1,1 +1,1 @@\n-hello\n+world\n"
        result = apply_patch_to_file(mock_client, "abc123", posix, diff)
        assert "Error" not in result
        assert real.read_text(encoding="utf-8") == "world\n"
        mock_record.assert_called_once()
        args, kwargs = mock_record.call_args
        assert args[0] == "abc123"
        assert args[1] == "test.py"
        assert "/root" in args[2]
        assert args[3] > 0
        assert kwargs.get("is_test") is False
