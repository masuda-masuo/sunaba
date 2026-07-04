"""Guard tests for the egress-proxy sidecar digest pin loader (Issue #432).

``load_proxy_pin`` mirrors ``load_image_pins`` (#331) but with one deliberate
difference: an **absent** pin file is a valid state (bootstrap before CI's first
proxy image, or a deployment that never enables the egress proxy), so it returns
``None`` rather than raising -- while a **present-but-malformed** pin still fails
loudly so a rotted pin can never silently fall back.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_sandbox_mcp import image_pins
from code_sandbox_mcp.image_pins import (
    _PROXY_PIN_PATTERN,
    PROXY_PIN_KEY,
    ImagePinError,
    load_proxy_pin,
)

_VALID_REF = f"ghcr.io/masuda-masuo/code-sandbox-mcp/proxy@sha256:{'a' * 64}"


def test_pattern_matches_digest_ref_only() -> None:
    assert _PROXY_PIN_PATTERN.match(_VALID_REF)
    # sandbox refs and mutable tags must not match a proxy pin.
    assert not _PROXY_PIN_PATTERN.match("ghcr.io/x/proxy:latest")
    assert not _PROXY_PIN_PATTERN.match(f"ghcr.io/x/sandbox@sha256:{'a' * 64}")


def test_absent_file_returns_none(monkeypatch) -> None:
    # Bootstrap / egress-proxy-disabled state: no file -> None, not an error.
    _patch_resource(monkeypatch, Path("/nonexistent/proxy_pin.json"))
    assert load_proxy_pin() is None


def test_valid_pin_returns_ref(tmp_path, monkeypatch) -> None:
    _write_pin(monkeypatch, tmp_path, json.dumps({PROXY_PIN_KEY: _VALID_REF}))
    assert load_proxy_pin() == _VALID_REF


def test_malformed_json_raises(tmp_path, monkeypatch) -> None:
    _write_pin(monkeypatch, tmp_path, "{not json")
    with pytest.raises(ImagePinError, match="not valid JSON"):
        load_proxy_pin()


def test_non_dict_json_raises(tmp_path, monkeypatch) -> None:
    _write_pin(monkeypatch, tmp_path, json.dumps([_VALID_REF]))
    with pytest.raises(ImagePinError, match="must be a JSON object"):
        load_proxy_pin()


def test_unexpected_key_raises(tmp_path, monkeypatch) -> None:
    _write_pin(monkeypatch, tmp_path, json.dumps({PROXY_PIN_KEY: _VALID_REF, "extra": _VALID_REF}))
    with pytest.raises(ImagePinError, match="keys mismatch"):
        load_proxy_pin()


def test_wrong_key_raises(tmp_path, monkeypatch) -> None:
    _write_pin(monkeypatch, tmp_path, json.dumps({"neutral": _VALID_REF}))
    with pytest.raises(ImagePinError, match="keys mismatch"):
        load_proxy_pin()


def test_non_digest_value_raises(tmp_path, monkeypatch) -> None:
    _write_pin(monkeypatch, tmp_path, json.dumps({PROXY_PIN_KEY: "ghcr.io/x/proxy:latest"}))
    with pytest.raises(ImagePinError, match="not a digest-pinned"):
        load_proxy_pin()


def _write_pin(monkeypatch, tmp_path, text: str) -> None:
    """Write *text* to a temp proxy pin file and point the loader at it."""
    pin_file = tmp_path / image_pins._PROXY_PINS_RESOURCE
    pin_file.write_text(text, encoding="utf-8")
    _patch_resource(monkeypatch, pin_file)


def _patch_resource(monkeypatch, pin_file: Path) -> None:
    """Redirect ``resources.files(...).joinpath(proxy_pin.json)`` to *pin_file*.

    ``monkeypatch`` restores the real ``resources.files`` when the test returns,
    so other tests (and the real packaged resource) are unaffected.
    """

    class _FakeFiles:
        def joinpath(self, name: str):
            assert name == image_pins._PROXY_PINS_RESOURCE
            return pin_file

    monkeypatch.setattr(image_pins.resources, "files", lambda _pkg: _FakeFiles())
