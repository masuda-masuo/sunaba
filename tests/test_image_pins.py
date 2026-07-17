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
from pathlib import Path

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
    assert container._JS_IMAGE == pins["js"]
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


def test_js_alias_resolves_to_a_digest() -> None:
    """``image="js"`` (#588) must resolve the same way as the other aliases."""
    from sunaba.tools import container

    assert container._image_pins.get("js") == load_image_pins()["js"]


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


# ===================================================================
# Dispatch matrix ⊆ HEALTHCHECK (Issue #584, extended for js in #588)
# ===================================================================
#
# design_multilang_support.md §6.1: "the image contract is ⊇ dispatch
# matrix" -- each variant image's HEALTHCHECK must assert every primary
# binary edit_verify's dispatch table can invoke, so a bake omission
# fails *here* / in CI's `docker run` healthcheck, not a user's first
# verify.  #584 built this contract for python/go; #588 adds js
# (eslint/tsc/jest) and must not regress the ones already covered.

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _dockerfile_healthcheck_text(name: str) -> str:
    """Return the HEALTHCHECK CMD line(s) of docker/Dockerfile.<name>."""
    text = (_REPO_ROOT / "docker" / f"Dockerfile.{name}").read_text(encoding="utf-8")
    # Grab from the HEALTHCHECK keyword onward (there is exactly one per
    # Dockerfile in this repo); good enough to check tool-name substrings.
    assert "HEALTHCHECK" in text, f"Dockerfile.{name} has no HEALTHCHECK"
    return text.split("HEALTHCHECK", 1)[1]


class TestFullImageHealthcheckCoversDispatchMatrix:
    """sandbox:full is the runtime default -- every dispatch tool must be baked."""

    # golangci-lint is deliberately excluded: edit_verify falls back to
    # `go vet` (bundled with the `go` binary, asserted via "go version")
    # when golangci-lint is absent, so it has no not_available failure
    # mode to guard against and is never baked.
    _REQUIRED_TOOLS = (
        "ruff", "pyright", "pytest",  # python
        "go version",  # go
        "eslint", "tsc", "jest",  # js/ts (#588)
    )

    def test_healthcheck_names_every_dispatch_tool(self) -> None:
        healthcheck = _dockerfile_healthcheck_text("full")
        missing = [t for t in self._REQUIRED_TOOLS if t not in healthcheck]
        assert not missing, (
            f"docker/Dockerfile.full HEALTHCHECK is missing dispatch-matrix "
            f"tool(s) {missing}: a bake omission here would surface as "
            f"not_available deep inside a user's first verify instead of "
            f"failing CI's docker-run healthcheck (#584/#588)."
        )


class TestJsImageHealthcheckCoversJsDispatchTools:
    """sandbox:js (explicit image=js) must assert its own three js tools."""

    def test_healthcheck_names_eslint_tsc_jest(self) -> None:
        healthcheck = _dockerfile_healthcheck_text("js")
        missing = [t for t in ("eslint", "tsc", "jest") if t not in healthcheck]
        assert not missing, f"docker/Dockerfile.js HEALTHCHECK is missing {missing}"
