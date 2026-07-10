"""Tests for the imperative transform_file edit path."""

from __future__ import annotations

from src.sunaba.edit_verify import (
    transform_file_in_container,
)
from tests.conftest import _FakeClient, _FakeContainer


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
            "src.sunaba.edit_verify.record_file_write",
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
            "src.sunaba.edit_verify.record_file_write",
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

