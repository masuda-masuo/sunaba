"""Tests for detection-based default image selection (Issue #313)."""

from __future__ import annotations

import io
import json

from code_sandbox_mcp import image_selection as imgsel
from code_sandbox_mcp.image_selection import (
    LanguageDetection,
    detect_from_github,
    detect_from_local_dir,
    resolve_initial_image,
    select_variant,
)

NEUTRAL = "ghcr.io/x/sandbox@sha256:" + "0" * 64
PY = "ghcr.io/x/sandbox@sha256:" + "1" * 64
GO = "ghcr.io/x/sandbox@sha256:" + "2" * 64
IMAGE_MAP = {"python": PY, "go": GO}


def _select(detection: LanguageDetection) -> tuple[str, str | None]:
    return select_variant(
        detection, language_image_map=IMAGE_MAP, neutral_image=NEUTRAL
    )


# --------------------------------------------------------------------------
# select_variant: detection set -> (image, notice)
# --------------------------------------------------------------------------

def test_python_marker_selects_python_image():
    image, notice = _select(LanguageDetection(supported={"python"}))
    assert image == PY
    assert notice is None


def test_go_marker_selects_go_image():
    image, notice = _select(LanguageDetection(supported={"go"}))
    assert image == GO
    assert notice is None


def test_js_only_rides_on_neutral_base_silently():
    # node lives in base; no js-toolchain image exists yet.
    image, notice = _select(LanguageDetection(supported={"js"}))
    assert image == NEUTRAL
    assert notice is None


def test_unknown_falls_back_to_neutral_silently():
    image, notice = _select(LanguageDetection())
    assert image == NEUTRAL
    assert notice is None


def test_py_go_polyglot_stays_neutral_with_notice():
    image, notice = _select(LanguageDetection(supported={"python", "go"}))
    assert image == NEUTRAL
    assert notice is not None and "polyglot" in notice


def test_unsupported_language_neutral_with_loud_notice():
    image, notice = _select(LanguageDetection(unsupported={"Rust"}))
    assert image == NEUTRAL
    assert notice is not None and "Rust" in notice


def test_missing_map_entry_degrades_to_neutral():
    # If a variant image is somehow absent from the map, do not crash.
    image, notice = select_variant(
        LanguageDetection(supported={"go"}),
        language_image_map={"python": PY},  # no "go"
        neutral_image=NEUTRAL,
    )
    assert image == NEUTRAL


# --------------------------------------------------------------------------
# detect_from_local_dir: marker files at a directory root
# --------------------------------------------------------------------------

def test_local_dir_detects_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    d = detect_from_local_dir(tmp_path)
    assert d.supported == {"go"}
    assert d.source == "preclone"


def test_local_dir_detects_python_via_glob(tmp_path):
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    d = detect_from_local_dir(tmp_path)
    assert d.supported == {"python"}


def test_local_dir_flags_unsupported(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    d = detect_from_local_dir(tmp_path)
    assert d.supported == set()
    assert d.unsupported == {"Rust"}


def test_local_dir_missing_path_is_neutral(tmp_path):
    d = detect_from_local_dir(tmp_path / "does-not-exist")
    assert d.is_empty
    assert d.source == "none"


# --------------------------------------------------------------------------
# detect_from_github: best-effort, never raises
# --------------------------------------------------------------------------

def _fake_urlopen(payload):
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = json.dumps(payload).encode()

    def _open(request, timeout=5.0):  # noqa: ARG001
        return _Resp(body)

    return _open


def test_github_detection_classifies_root_files(monkeypatch):
    payload = [
        {"name": "go.mod", "type": "file"},
        {"name": "README.md", "type": "file"},
        {"name": "cmd", "type": "dir"},
    ]
    monkeypatch.setattr(
        imgsel.urllib.request, "urlopen", _fake_urlopen(payload)
    )
    d = detect_from_github("owner/repo")
    assert d is not None
    assert d.supported == {"go"}
    assert d.source == "github-api"


def test_github_detection_returns_none_on_error(monkeypatch):
    def _boom(request, timeout=5.0):  # noqa: ARG001
        raise OSError("network down")

    monkeypatch.setattr(imgsel.urllib.request, "urlopen", _boom)
    assert detect_from_github("owner/repo") is None


# --------------------------------------------------------------------------
# resolve_initial_image: orchestration / precedence
# --------------------------------------------------------------------------

def test_explicit_image_wins_without_detection(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    image, notice = resolve_initial_image(
        explicit_image="ghcr.io/x/custom@sha256:" + "a" * 64,
        target_repo="owner/repo",
        preclone_root=tmp_path,
        token=None,
        language_image_map=IMAGE_MAP,
        neutral_image=NEUTRAL,
    )
    assert image.endswith("custom@sha256:" + "a" * 64)
    assert notice is None


def test_preclone_drives_selection(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image, notice = resolve_initial_image(
        explicit_image=None,
        target_repo="owner/repo",
        preclone_root=tmp_path,
        token=None,
        language_image_map=IMAGE_MAP,
        neutral_image=NEUTRAL,
        allow_network_detection=False,
    )
    assert image == PY


def test_bare_init_is_neutral():
    image, notice = resolve_initial_image(
        explicit_image=None,
        target_repo=None,
        preclone_root=None,
        token=None,
        language_image_map=IMAGE_MAP,
        neutral_image=NEUTRAL,
    )
    assert image == NEUTRAL
    assert notice is None


def test_network_detection_used_when_no_preclone(monkeypatch):
    payload = [{"name": "go.mod", "type": "file"}]
    monkeypatch.setattr(
        imgsel.urllib.request, "urlopen", _fake_urlopen(payload)
    )
    image, notice = resolve_initial_image(
        explicit_image=None,
        target_repo="owner/repo",
        preclone_root=None,
        token=None,
        language_image_map=IMAGE_MAP,
        neutral_image=NEUTRAL,
        allow_network_detection=True,
    )
    assert image == GO


def test_network_detection_skipped_when_disabled(monkeypatch):
    def _boom(request, timeout=5.0):  # noqa: ARG001
        raise AssertionError("network must not be probed")

    monkeypatch.setattr(imgsel.urllib.request, "urlopen", _boom)
    image, _ = resolve_initial_image(
        explicit_image=None,
        target_repo="owner/repo",
        preclone_root=None,
        token=None,
        language_image_map=IMAGE_MAP,
        neutral_image=NEUTRAL,
        allow_network_detection=False,
    )
    assert image == NEUTRAL
