"""Single source of truth for the default sandbox image digest pins (Issue #331).

The default variant images (neutral / python / go) are digest-pinned so that
``sandbox_initialize`` always starts a reproducible image
(``docs/design-multilang-support.md`` §6).  Historically these pins lived as
three string constants in ``container.py`` and CI rewrote them with ``sed``.
That approach broke **silently** twice (#214): a refactor moved or reformatted
the constant, the ``sed`` anchor matched nothing, ``git diff --quiet`` read the
empty diff as "digest unchanged", and the pin rotted with no error.

To kill that class of bug, the pins now live as *data* in ``image_pins.json``
(no regex anchor to drift) and CI rewrites that file structurally then verifies
the post-condition: the value this loader returns must equal the digest just
built.  Any drift -- a renamed key, a moved file, a malformed entry -- makes
:func:`load_image_pins` raise, so the failure is loud at import and in CI rather
than a stale pin shipped in silence.
"""

from __future__ import annotations

import json
import re
from importlib import resources

#: Role keys every pin file must define, in the order consumers expect.
PIN_KEYS: tuple[str, ...] = ("neutral", "python", "go")

#: A pin must be a fully digest-pinned GHCR reference -- never a mutable tag.
#: ``<registry>/.../sandbox@sha256:<64 hex>`` (``docs/design-multilang-support.md`` §6).
_PIN_PATTERN: re.Pattern[str] = re.compile(
    r"^ghcr\.io/[A-Za-z0-9._/-]+/sandbox@sha256:[0-9a-f]{64}$"
)

#: Name of the data file shipped alongside this module (see pyproject package-data).
_PINS_RESOURCE: str = "image_pins.json"


class ImagePinError(RuntimeError):
    """Raised when the image pin data is missing, malformed, or not digest-pinned.

    Deliberately fatal: a sandbox started from a non-pinned or absent image is a
    reproducibility bug, so callers should never paper over it with a fallback.
    """


def load_image_pins() -> dict[str, str]:
    """Load and validate the default image pins from ``image_pins.json``.

    Returns a mapping with exactly the keys in :data:`PIN_KEYS`, each a
    digest-pinned GHCR reference.  Raises :class:`ImagePinError` if the file is
    absent, not valid JSON, missing or carrying unexpected keys, or holding a
    value that is not a ``@sha256`` digest reference.
    """
    try:
        raw = (
            resources.files("code_sandbox_mcp")
            .joinpath(_PINS_RESOURCE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError) as exc:  # pragma: no cover - packaging bug
        raise ImagePinError(
            f"image pin file {_PINS_RESOURCE!r} not found in package "
            "(packaging regression: check [tool.setuptools.package-data])"
        ) from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImagePinError(f"{_PINS_RESOURCE} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ImagePinError(f"{_PINS_RESOURCE} must be a JSON object, got {type(data).__name__}")

    keys = set(data)
    expected = set(PIN_KEYS)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        raise ImagePinError(
            f"{_PINS_RESOURCE} keys mismatch: missing={missing} unexpected={extra}; "
            f"expected exactly {sorted(expected)}"
        )

    for key in PIN_KEYS:
        value = data[key]
        if not isinstance(value, str) or not _PIN_PATTERN.match(value):
            raise ImagePinError(
                f"{_PINS_RESOURCE}[{key!r}] is not a digest-pinned GHCR ref "
                f"(ghcr.io/.../sandbox@sha256:<64hex>): {value!r}"
            )

    return {key: data[key] for key in PIN_KEYS}
