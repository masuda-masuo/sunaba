"""Guard tests for the default image digest pins (Issue #331).

These run in the normal pytest CI on every PR, so a refactor that breaks the
pin wiring -- a renamed key, a moved/unshipped JSON, a non-digest ref, or a
disconnect between the JSON and what ``container.py`` actually uses -- fails
*here*, before it can silently rot the way the old ``sed``-on-source approach
did (#214).
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from sunaba import image_pins
from sunaba.image_pins import (
    _PIN_PATTERN,
    PIN_KEYS,
    ImagePinError,
    load_image_pins,
)


def test_pins_load_with_expected_keys() -> None:
    pins = load_image_pins()
    assert set(pins) == set(PIN_KEYS)


def test_every_pin_is_a_digest_ref() -> None:
    for key, ref in load_image_pins().items():
        assert _PIN_PATTERN.match(ref), f"pin {key!r} is not digest-pinned: {ref!r}"


def test_json_resource_is_packaged() -> None:
    # importlib.resources must find the data file the way the runtime loader
    # does; a packaging regression (missing package-data) is caught here.
    resource = resources.files("sunaba").joinpath("image_pins.json")
    assert resource.is_file()
    data = json.loads(resource.read_text(encoding="utf-8"))
    assert set(data) == set(PIN_KEYS)


def test_container_constants_are_wired_from_loader() -> None:
    # The values container.py actually starts containers with must equal the
    # pin data -- the wiring, not just the file, is what matters.
    from sunaba.tools import container

    pins = load_image_pins()
    assert container._NEUTRAL_IMAGE == pins["neutral"]
    assert container._PYTHON_IMAGE == pins["python"]
    assert container._GO_IMAGE == pins["go"]
    assert container._FULL_IMAGE == pins["full"]
    # The default is the all-in-one image: nothing about the project's language
    # is guessed before the container starts (#584).
    assert container._DEFAULT_IMAGE == pins["full"]


def test_full_alias_resolves_to_a_digest() -> None:
    """``image="full"`` must resolve via the pin data, like the other aliases.

    Alias resolution is ``_image_pins.get(name, name)`` (#545), so a missing
    pin key would silently pass the literal string "full" to Docker.
    """
    from sunaba.tools import container

    assert container._image_pins.get("full") == load_image_pins()["full"]


def test_loader_rejects_unknown_keys(tmp_path, monkeypatch) -> None:
    # Simulate drift: a pin file with a stray/renamed key must fail loudly
    # rather than load a partial mapping.
    bad = {k: f"ghcr.io/x/sandbox@sha256:{'0' * 64}" for k in PIN_KEYS}
    bad["rust"] = f"ghcr.io/x/sandbox@sha256:{'0' * 64}"
    _patch_resource(monkeypatch, tmp_path, json.dumps(bad))
    with pytest.raises(ImagePinError, match="keys mismatch"):
        load_image_pins()


def test_loader_rejects_non_digest_value(tmp_path, monkeypatch) -> None:
    bad = {k: f"ghcr.io/x/sandbox@sha256:{'0' * 64}" for k in PIN_KEYS}
    bad["python"] = "ghcr.io/x/sandbox:python"  # mutable tag, not a digest
    _patch_resource(monkeypatch, tmp_path, json.dumps(bad))
    with pytest.raises(ImagePinError, match="not a digest-pinned"):
        load_image_pins()


def test_loader_rejects_malformed_json(tmp_path, monkeypatch) -> None:
    _patch_resource(monkeypatch, tmp_path, "{not json")
    with pytest.raises(ImagePinError, match="not valid JSON"):
        load_image_pins()


def test_loader_rejects_non_dict_json(tmp_path, monkeypatch) -> None:
    _patch_resource(monkeypatch, tmp_path, json.dumps(["not", "a", "dict"]))
    with pytest.raises(ImagePinError, match="must be a JSON object"):
        load_image_pins()


def test_loader_rejects_missing_keys(tmp_path, monkeypatch) -> None:
    bad = {k: f"ghcr.io/x/sandbox@sha256:{'0' * 64}" for k in PIN_KEYS}
    del bad["python"]
    _patch_resource(monkeypatch, tmp_path, json.dumps(bad))
    with pytest.raises(ImagePinError, match="keys mismatch"):
        load_image_pins()


def _patch_resource(monkeypatch, tmp_path, text: str) -> None:
    """Point the loader at a temp pin file instead of the packaged one.

    The monkeypatch fixture restores the original ``resources.files`` when
    the test function returns, so the patch is effectively scoped to the
    calling test.  New tests that need the real ``resources.files()`` are
    unaffected.
    """
    pin_file = tmp_path / image_pins._PINS_RESOURCE
    pin_file.write_text(text, encoding="utf-8")

    class _FakeFiles:
        def joinpath(self, name: str):
            assert name == image_pins._PINS_RESOURCE
            return pin_file

    monkeypatch.setattr(image_pins.resources, "files", lambda _pkg: _FakeFiles())
