"""Tests for the imperative transform_file edit path."""

from __future__ import annotations

import json
import os
import stat
import tempfile

import pytest

from src.sunaba.edit_verify import (
    transform_file_in_container,
)
from sunaba import undo
from sunaba.tools.file import transform_file, undo_file_edit
from tests.conftest import _FakeClient, _FakeContainer
from tests.test_edit_symbol import _FakeContainerWithIO


# ===================================================================
# _parse_ruff_output tests
# ===================================================================
class TestTransformFileInContainer:
    """Tests for the imperative transform_file edit path."""

    _POSIX = "/sandbox/x.py"

    def _run(self, real_path, code):  # noqa: ANN001
        """Invoke with a fixed posix path mapped to *real_path* on the host."""
        client = _FakeClient(_FakeContainer({self._POSIX: str(real_path)}))
        return transform_file_in_container(client, "abc123", self._POSIX, code)

    def test_rejects_relative_path(self) -> None:
        out = transform_file_in_container(
            _FakeClient(_FakeContainer()), "abc123", "rel/path.py", "x"
        )
        assert out["status"] == "error"
        assert "absolute" in out["error"]

    def test_applies_transform_and_returns_diff(self, tmp_path, monkeypatch) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.sunaba.edit_verify.edits.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        f = tmp_path / "x.py"
        f.write_text("aaa\nbbb\n", encoding="utf-8")

        code = "def transform(text):\n    return text.replace('a', 'z')\n"
        out = self._run(f, code)

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert "-aaa" in out["diff"] and "+zzz" in out["diff"]
        assert f.read_text(encoding="utf-8") == "zzz\nbbb\n"
        assert writes, "a successful change should be journaled"

    def test_no_change_is_reported(self, tmp_path, monkeypatch) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.sunaba.edit_verify.edits.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        f = tmp_path / "x.py"
        f.write_text("hello\n", encoding="utf-8")

        out = self._run(f, "def transform(text):\n    return text\n")

        assert out["status"] == "ok"
        assert out["changed"] is False
        assert not writes, "an unchanged file should not be journaled"

    def test_missing_transform_callable(self, tmp_path) -> None:
        f = tmp_path / "x.py"
        f.write_text("hello\n", encoding="utf-8")
        out = self._run(f, "y = 1\n")
        assert out["status"] == "error"
        assert "transform" in out["error"]

    def test_transform_raises_returns_traceback(self, tmp_path) -> None:
        f = tmp_path / "x.py"
        f.write_text("hello\n", encoding="utf-8")
        out = self._run(
            f, "def transform(text):\n    raise ValueError('boom')\n"
        )
        assert out["status"] == "error"
        assert "boom" in out["error"]
        assert "traceback" in out

    def test_file_not_found(self, tmp_path) -> None:
        missing = tmp_path / "missing.py"
        out = self._run(missing, "def transform(text):\n    return text\n")
        assert out["status"] == "error"
        assert "not found" in out["error"]


# ===================================================================
# _normalize_diff_for_git (pure) + apply_patch_to_file delegation
# ===================================================================
@pytest.fixture(autouse=True)
def _no_journal(monkeypatch) -> None:
    """Keep journal records out of the real journal during tests.

    In-container tests that assert journaling re-patch
    ``record_file_write`` themselves; wrapper tests that assert tool-use
    records re-patch ``record_tool_use``.
    """
    monkeypatch.setattr("sunaba.tools.file.record_tool_use", lambda *a, **k: None)
    monkeypatch.setattr(
        "src.sunaba.edit_verify.edits.record_file_write", lambda *a, **k: None
    )


# ===================================================================
# Multi-path transform: one transform applied to an explicit list
# ===================================================================
class TestTransformFileInContainerMulti:
    """Multi-path form of transform_file_in_container (issue #874)."""

    _A = "/sandbox/a.py"
    _B = "/sandbox/b.py"
    _C = "/sandbox/c.py"

    def _run(self, path_map, code, paths=None):  # noqa: ANN001
        client = _FakeClient(_FakeContainer(path_map))
        return transform_file_in_container(
            client, "abc123", None, code, paths=paths
        )

    def test_two_files_both_change(self, tmp_path, monkeypatch) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.sunaba.edit_verify.edits.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert fa.read_text(encoding="utf-8") == "AAA\n"
        assert fb.read_text(encoding="utf-8") == "BBB\n"
        # One combined diff, each file's section headed by its own path.
        assert "--- /sandbox/a.py" in out["diff"]
        assert "--- /sandbox/b.py" in out["diff"]
        assert "+AAA" in out["diff"] and "+BBB" in out["diff"]
        # Each written file is journaled as a write.
        assert len(writes) == 2

    def test_raise_on_second_leaves_all_untouched(self, tmp_path) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("boom\n", encoding="utf-8")
        fc = tmp_path / "c.py"
        fc.write_text("ccc\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa), self._B: str(fb), self._C: str(fc)},
            "def transform(text):\n"
            "    if 'boom' in text:\n"
            "        raise ValueError('boom')\n"
            "    return text.upper()\n",
            paths=[self._A, self._B, self._C],
        )

        assert out["status"] == "error"
        assert "boom" in out["error"]
        assert self._B in out["error"]
        # All-or-nothing: not even the files whose transform succeeded
        # were written.
        assert fa.read_text(encoding="utf-8") == "aaa\n"
        assert fb.read_text(encoding="utf-8") == "boom\n"
        assert fc.read_text(encoding="utf-8") == "ccc\n"

    def test_missing_path_modifies_nothing(self, tmp_path) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fc = tmp_path / "c.py"
        fc.write_text("ccc\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa), self._C: str(fc)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B, self._C],
        )

        assert out["status"] == "error"
        assert "not found" in out["error"]
        assert self._B in out["error"]
        assert fa.read_text(encoding="utf-8") == "aaa\n"
        assert fc.read_text(encoding="utf-8") == "ccc\n"

    def test_unreadable_target_leaves_first_untouched(self, tmp_path) -> None:
        # The second target maps to a directory: reading it fails in phase 1,
        # before any file is written, and the error names the path.
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        d = tmp_path / "not-a-file"
        d.mkdir()

        out = self._run(
            {self._A: str(fa), self._B: str(d)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "error"
        assert self._B in out["error"]
        assert fa.read_text(encoding="utf-8") == "aaa\n"

    def test_unwritable_target_leaves_first_untouched(self, tmp_path) -> None:
        # The second target is read-only (tests run as uid 999, so chmod
        # applies): reading it succeeds, but the phase-2 writability probe
        # (open "r+") fails before any file is written.
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fb.chmod(0o444)

        try:
            out = self._run(
                {self._A: str(fa), self._B: str(fb)},
                "def transform(text):\n    return text.upper()\n",
                paths=[self._A, self._B],
            )
        finally:
            fb.chmod(0o644)  # tmp_path cleanup needs write permission

        assert out["status"] == "error"
        assert self._B in out["error"]
        assert fa.read_text(encoding="utf-8") == "aaa\n"
        assert fb.read_text(encoding="utf-8") == "bbb\n"

    def test_unchanged_file_reported_and_not_journaled(
        self, tmp_path, monkeypatch,
    ) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.sunaba.edit_verify.edits.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        fa = tmp_path / "a.py"
        fa.write_text("todo\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("nothing to do\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    return text.replace('todo', 'TODO')\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert fa.read_text(encoding="utf-8") == "TODO\n"
        assert fb.read_text(encoding="utf-8") == "nothing to do\n"
        assert out["files"] == [
            {"path": self._A, "changed": True, "new_size": 5, "new_lines": 1},
            {"path": self._B, "changed": False, "new_size": 14, "new_lines": 1},
        ]
        assert len(writes) == 1  # only the changed file is journaled

    def test_all_unchanged_reports_changed_false(self, tmp_path) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("same\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa)},
            "def transform(text):\n    return text\n",
            paths=[self._A],
        )

        assert out["status"] == "ok"
        assert out["changed"] is False
        assert out["diff"] == ""
        assert out["files"] == [
            {"path": self._A, "changed": False, "new_size": 5, "new_lines": 1},
        ]

    def test_rejects_relative_path_in_list(self) -> None:
        out = self._run({}, "x", paths=["rel/path.py"])
        assert out["status"] == "error"
        assert "absolute" in out["error"]

    def test_dotdot_path_follows_single_path_contract(self, tmp_path) -> None:
        # Mirrors the single-path contract: normpath resolves ".." against
        # the root, and an absolute path never leaves a ".." component for
        # the traversal check to catch -- so the path is accepted and the
        # runner processes it as given (here unmapped, hence not found).
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa)},
            "def transform(text):\n    return text.upper()\n",
            paths=["/sandbox/sub/../a.py"],
        )

        assert out["status"] == "error"
        assert "traversal" not in out["error"].lower()
        assert "not found" in out["error"]

    def test_rejects_both_file_path_and_paths(self) -> None:
        out = transform_file_in_container(
            _FakeClient(_FakeContainer()), "abc123", self._A, "x",
            paths=[self._A],
        )
        assert out["status"] == "error"
        assert "exactly one" in out["error"]

    def test_rejects_neither_file_path_nor_paths(self) -> None:
        out = transform_file_in_container(
            _FakeClient(_FakeContainer()), "abc123", None, "x"
        )
        assert out["status"] == "error"
        assert "exactly one" in out["error"]

    def test_rejects_empty_paths(self) -> None:
        out = self._run({}, "x", paths=[])
        assert out["status"] == "error"
        assert "empty" in out["error"]

    def test_rejects_duplicate_paths(self) -> None:
        out = self._run({}, "x", paths=[self._A, self._A])
        assert out["status"] == "error"
        assert "duplicate" in out["error"]

    def test_staging_mkstemp_failure_leaves_all_untouched_and_no_temp_files(
        self, tmp_path, monkeypatch,
    ) -> None:
        writes: list = []
        monkeypatch.setattr(
            "src.sunaba.edit_verify.edits.record_file_write",
            lambda *a, **k: writes.append(a),
        )
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fc = tmp_path / "c.py"
        fc.write_text("ccc\n", encoding="utf-8")

        real_mkstemp = tempfile.mkstemp

        def fail_mkstemp(*a, **k):
            prefix = k.get("prefix", "")
            if ".b.py." in prefix or "b.py" in prefix:
                raise OSError("No space left on device")
            return real_mkstemp(*a, **k)

        monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)

        out = self._run(
            {self._A: str(fa), self._B: str(fb), self._C: str(fc)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B, self._C],
        )

        assert out["status"] == "error"
        assert "No space left on device" in out["error"]
        assert self._B in out["error"]
        assert fa.read_text(encoding="utf-8") == "aaa\n"
        assert fb.read_text(encoding="utf-8") == "bbb\n"
        assert fc.read_text(encoding="utf-8") == "ccc\n"
        assert set(p.name for p in tmp_path.iterdir()) == {"a.py", "b.py", "c.py"}
        assert len(writes) == 0

    def test_commit_phase_failure_leaves_landed_target_intact_and_unlanded_untouched(
        self, tmp_path, monkeypatch,
    ) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fc = tmp_path / "c.py"
        fc.write_text("ccc\n", encoding="utf-8")

        real_replace = os.replace

        def fail_replace(src, dst, *a, **k):
            if "b.py" in str(dst):
                raise OSError("Permission denied during rename")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", fail_replace)

        out = self._run(
            {self._A: str(fa), self._B: str(fb), self._C: str(fc)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B, self._C],
        )

        assert out["status"] == "error"
        assert "Permission denied during rename" in out["error"]
        assert self._B in out["error"]
        # Landed target holds complete new content
        assert fa.read_text(encoding="utf-8") == "AAA\n"
        # Failing target untouched
        assert fb.read_text(encoding="utf-8") == "bbb\n"
        # Unlanded target untouched
        assert fc.read_text(encoding="utf-8") == "ccc\n"
        # No stray temp files left behind
        assert set(p.name for p in tmp_path.iterdir()) == {"a.py", "b.py", "c.py"}

    def test_successful_transform_leaves_no_stray_temp_files(
        self, tmp_path,
    ) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert fa.read_text(encoding="utf-8") == "AAA\n"
        assert fb.read_text(encoding="utf-8") == "BBB\n"
        assert set(p.name for p in tmp_path.iterdir()) == {"a.py", "b.py"}

    def test_preserves_executable_mode(self, tmp_path) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fa.chmod(0o755)
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fb.chmod(0o644)

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert fa.read_text(encoding="utf-8") == "AAA\n"
        assert fb.read_text(encoding="utf-8") == "BBB\n"
        assert stat.S_IMODE(fa.stat().st_mode) == 0o755
        assert stat.S_IMODE(fb.stat().st_mode) == 0o644

    def test_preserves_non_default_non_executable_mode(self, tmp_path) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fa.chmod(0o640)
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fb.chmod(0o600)

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert fa.read_text(encoding="utf-8") == "AAA\n"
        assert fb.read_text(encoding="utf-8") == "BBB\n"
        assert stat.S_IMODE(fa.stat().st_mode) == 0o640
        assert stat.S_IMODE(fb.stat().st_mode) == 0o600

    def test_unchanged_file_preserves_mode_and_inode(self, tmp_path) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fa.chmod(0o755)
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fb.chmod(0o640)
        orig_inode_b = fb.stat().st_ino

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    if 'a' in text: return text.upper()\n    return text\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "ok"
        assert out["changed"] is True
        assert fa.read_text(encoding="utf-8") == "AAA\n"
        assert fb.read_text(encoding="utf-8") == "bbb\n"
        assert stat.S_IMODE(fa.stat().st_mode) == 0o755
        assert stat.S_IMODE(fb.stat().st_mode) == 0o640
        assert fb.stat().st_ino == orig_inode_b

    def test_staging_failure_leaves_target_modes_untouched(
        self, tmp_path, monkeypatch,
    ) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fa.chmod(0o755)
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")
        fb.chmod(0o640)

        real_mkstemp = tempfile.mkstemp

        def fail_mkstemp(*a, **k):
            prefix = k.get("prefix", "")
            if ".b.py." in prefix or "b.py" in prefix:
                raise OSError("No space left on device")
            return real_mkstemp(*a, **k)

        monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)

        out = self._run(
            {self._A: str(fa), self._B: str(fb)},
            "def transform(text):\n    return text.upper()\n",
            paths=[self._A, self._B],
        )

        assert out["status"] == "error"
        assert self._B in out["error"]
        assert fa.read_text(encoding="utf-8") == "aaa\n"
        assert fb.read_text(encoding="utf-8") == "bbb\n"
        assert stat.S_IMODE(fa.stat().st_mode) == 0o755
        assert stat.S_IMODE(fb.stat().st_mode) == 0o640


class _FakeContainerCaptureWrites(_FakeContainerWithIO):
    """Extends the fake with put_archive capture for undo-restore asserts."""

    def __init__(self, path_map=None) -> None:
        super().__init__(path_map)
        self.written: dict[str, str] = {}

    def put_archive(self, path, data) -> bool:  # noqa: ANN001
        import io as _io
        import tarfile as _tf

        buf = _io.BytesIO(data)
        with _tf.open(fileobj=buf, mode="r") as tar:
            for member in tar.getmembers():
                f = tar.extractfile(member)
                content = f.read().decode("utf-8") if f else ""
                self.written[path + "/" + member.name] = content
        return True


class TestTransformFileMultiWrapper:
    """Host wrapper for multi-path calls: snapshots, journal, envelope."""

    _A = "/sandbox/a.py"
    _B = "/sandbox/b.py"

    def test_writes_both_files_one_diff_and_undo_restores_each(
        self, tmp_path, monkeypatch,
    ) -> None:
        src_a = "todo: one\n"
        src_b = "todo: two\n"
        fa = tmp_path / "a.py"
        fa.write_text(src_a, encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text(src_b, encoding="utf-8")
        container = _FakeContainerCaptureWrites(
            {self._A: str(fa), self._B: str(fb)}
        )
        monkeypatch.setattr(
            "sunaba.tools.file._docker", lambda: _FakeClient(container),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            paths=[self._A, self._B],
            code="def transform(text):\n    return text.replace('todo', 'TODO')\n",
        ))

        assert result["status"] == "ok"
        assert result["changed"] is True
        assert fa.read_text(encoding="utf-8") == "TODO: one\n"
        assert fb.read_text(encoding="utf-8") == "TODO: two\n"
        # One combined diff, each file identifiable by its own headers.
        assert "--- /sandbox/a.py" in result["diff"]
        assert "--- /sandbox/b.py" in result["diff"]
        # Each changed file got its own snapshot under its own path.
        assert undo.get_version("abc123def456", self._A, 1) == src_a
        assert undo.get_version("abc123def456", self._B, 1) == src_b
        # Per-file sizes surface in the result.
        assert result["files"] == [
            {
                "path": self._A,
                "changed": True,
                "file_size": {"lines": 1, "bytes": 10, "approx_tokens": 2},
            },
            {
                "path": self._B,
                "changed": True,
                "file_size": {"lines": 1, "bytes": 10, "approx_tokens": 2},
            },
        ]

        # undo_file_edit restores each file independently by its own path.
        ua = json.loads(undo_file_edit("abc123def456", self._A))
        assert ua["status"] == "ok"
        assert container.written[self._A] == src_a
        # B's snapshot is untouched by A's undo.
        assert undo.get_version("abc123def456", self._B, 1) == src_b
        ub = json.loads(undo_file_edit("abc123def456", self._B))
        assert ub["status"] == "ok"
        assert container.written[self._B] == src_b

    def test_records_targets_and_skips_unchanged_snapshot(
        self, tmp_path, monkeypatch,
    ) -> None:
        uses: list = []
        monkeypatch.setattr(
            "sunaba.tools.file.record_tool_use", lambda *a, **k: uses.append(a),
        )
        fa = tmp_path / "a.py"
        fa.write_text("already done\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("todo\n", encoding="utf-8")
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(
                _FakeContainerWithIO({self._A: str(fa), self._B: str(fb)})
            ),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            paths=[self._A, self._B],
            code="def transform(text):\n    return text.replace('todo', 'TODO')\n",
        ))

        assert result["status"] == "ok"
        assert result["changed"] is True
        # The tool use records the targets it was given.
        assert uses == [
            ("abc123def456", "transform_file", {"paths": [self._A, self._B]}),
        ]
        # The unchanged file is reported and has no undo snapshot.
        assert result["files"] == [
            {
                "path": self._A,
                "changed": False,
                "file_size": {"lines": 1, "bytes": 13, "approx_tokens": 3},
            },
            {
                "path": self._B,
                "changed": True,
                "file_size": {"lines": 1, "bytes": 5, "approx_tokens": 1},
            },
        ]
        assert undo.get_version("abc123def456", self._A, 1) is None
        assert undo.get_version("abc123def456", self._B, 1) == "todo\n"

    def test_combined_diff_paging_bounds(self, tmp_path, monkeypatch) -> None:
        fa = tmp_path / "a.py"
        fa.write_text(
            "".join(f"line{i}\n" for i in range(10)), encoding="utf-8",
        )
        fb = tmp_path / "b.py"
        fb.write_text(
            "".join(f"line{i}\n" for i in range(10)), encoding="utf-8",
        )
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(
                _FakeContainerWithIO({self._A: str(fa), self._B: str(fb)})
            ),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            paths=[self._A, self._B],
            code="def transform(text):\n    return text.replace('line', 'LINE')\n",
            offset=0,
            limit=3,
        ))

        assert result["status"] == "ok"
        assert result["changed"] is True
        assert len(result["diff"].split("\n")) <= 3
        assert result["shown"] == 3
        assert result["total_lines"] > 3
        assert result["truncated"] is True
        assert result["next_offset"] == 3
        assert result["has_more"] is True

    def test_both_file_path_and_paths_is_an_error(self, monkeypatch) -> None:
        uses: list = []
        monkeypatch.setattr(
            "sunaba.tools.file.record_tool_use", lambda *a, **k: uses.append(a),
        )
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({})),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            file_path=self._A,
            paths=[self._B],
            code="def transform(text): return text",
        ))

        assert result["status"] == "error"
        assert "exactly one" in result["error"]
        assert uses == []  # nothing recorded, nothing touched

    def test_neither_file_path_nor_paths_is_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({})),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            code="def transform(text): return text",
        ))

        assert result["status"] == "error"
        assert "exactly one" in result["error"]

    def test_empty_paths_is_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "sunaba.tools.file._docker",
            lambda: _FakeClient(_FakeContainerWithIO({})),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            paths=[],
            code="def transform(text): return text",
        ))

        assert result["status"] == "error"
        assert "empty" in result["error"]

    def test_staging_failure_wrapper_records_no_snapshot_and_no_tool_writes(
        self, tmp_path, monkeypatch,
    ) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("aaa\n", encoding="utf-8")
        fb = tmp_path / "b.py"
        fb.write_text("bbb\n", encoding="utf-8")

        real_mkstemp = tempfile.mkstemp

        def fail_mkstemp(*a, **k):
            prefix = k.get("prefix", "")
            if ".b.py." in prefix or "b.py" in prefix:
                raise OSError("No space left on device")
            return real_mkstemp(*a, **k)

        monkeypatch.setattr(tempfile, "mkstemp", fail_mkstemp)

        container = _FakeContainerWithIO({self._A: str(fa), self._B: str(fb)})
        monkeypatch.setattr(
            "sunaba.tools.file._docker", lambda: _FakeClient(container),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            paths=[self._A, self._B],
            code="def transform(text):\n    return text.upper()\n",
        ))

        assert result["status"] == "error"
        assert self._B in result["error"]
        assert fa.read_text(encoding="utf-8") == "aaa\n"
        assert fb.read_text(encoding="utf-8") == "bbb\n"
        assert undo.get_version("abc123def456", self._A, 1) is None
        assert undo.get_version("abc123def456", self._B, 1) is None
        assert set(p.name for p in tmp_path.iterdir()) == {"a.py", "b.py"}

    def test_wrapper_preserves_modes(self, tmp_path, monkeypatch) -> None:
        fa = tmp_path / "a.py"
        fa.write_text("todo: one\n", encoding="utf-8")
        fa.chmod(0o755)
        fb = tmp_path / "b.py"
        fb.write_text("todo: two\n", encoding="utf-8")
        fb.chmod(0o640)
        container = _FakeContainerCaptureWrites(
            {self._A: str(fa), self._B: str(fb)}
        )
        monkeypatch.setattr(
            "sunaba.tools.file._docker", lambda: _FakeClient(container),
        )

        result = json.loads(transform_file(
            container_id="abc123def456",
            paths=[self._A, self._B],
            code="def transform(text):\n    return text.replace('todo', 'TODO')\n",
        ))

        assert result["status"] == "ok"
        assert result["changed"] is True
        assert fa.read_text(encoding="utf-8") == "TODO: one\n"
        assert fb.read_text(encoding="utf-8") == "TODO: two\n"
        assert stat.S_IMODE(fa.stat().st_mode) == 0o755
        assert stat.S_IMODE(fb.stat().st_mode) == 0o640
