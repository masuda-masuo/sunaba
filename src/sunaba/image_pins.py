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
            resources.files("sunaba")
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


# ---------------------------------------------------------------------------
# Egress-proxy sidecar pin (Issue #432)
# ---------------------------------------------------------------------------
#
# The egress-proxy sidecar image (``docker/Dockerfile.proxy``) is pinned the
# same way as the sandbox variants -- as data in a JSON file that CI rewrites
# structurally and re-reads through this loader -- so a server ``pip install``
# picks up the current sidecar instead of leaving a locally built tag stale
# after a redeploy (the #432 incident).  It is kept *separate* from
# ``image_pins.json`` on purpose: :func:`load_image_pins` deliberately rejects
# any key outside :data:`PIN_KEYS` (#331), and the proxy ref is a
# ``.../proxy@sha256`` image, not a ``.../sandbox`` one.

#: Sole role key in ``proxy_pin.json``.
PROXY_PIN_KEY: str = "proxy"

#: Data file for the proxy pin, shipped alongside this module.  Unlike
#: ``image_pins.json`` it may be *absent*: the egress proxy is default-on (can
#: be disabled via ``SUNABA_ENABLE_EGRESS_PROXY=false``) and the pin is
#: bootstrapped by CI after the first GHCR push (#432), so a missing file is
#: a valid pre-pin state that falls back to the locally built tag rather than
#: an error.
_PROXY_PINS_RESOURCE: str = "proxy_pin.json"

#: A proxy pin must be a fully digest-pinned GHCR ``.../proxy@sha256:<64hex>``
#: reference -- never a mutable tag.
_PROXY_PIN_PATTERN: re.Pattern[str] = re.compile(
    r"^ghcr\.io/[A-Za-z0-9._/-]+/proxy@sha256:[0-9a-f]{64}$"
)


def load_proxy_pin() -> str | None:
    """Load the egress-proxy sidecar digest pin, or ``None`` when unset.

    Returns the digest-pinned GHCR reference from ``proxy_pin.json`` when the
    file is present and valid.  Returns ``None`` when the file is **absent** --
    a legitimate state before CI has published the proxy image (#432) and for
    deployments that never enable the egress proxy -- so the caller can fall
    back to the locally built tag.

    Unlike :func:`load_image_pins`, absence is tolerated but **malformation is
    not**: a present-but-broken pin (invalid JSON, a wrong or extra key, or a
    value that is not a ``@sha256`` digest ref) raises :class:`ImagePinError`
    so a rotted pin fails loudly rather than silently falling back.
    """
    try:
        raw = (
            resources.files("sunaba")
            .joinpath(_PROXY_PINS_RESOURCE)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError):
        # Absent pin: bootstrap / egress-proxy disabled -> caller uses the tag.
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImagePinError(f"{_PROXY_PINS_RESOURCE} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ImagePinError(
            f"{_PROXY_PINS_RESOURCE} must be a JSON object, got {type(data).__name__}"
        )

    keys = set(data)
    if keys != {PROXY_PIN_KEY}:
        raise ImagePinError(
            f"{_PROXY_PINS_RESOURCE} keys mismatch: got {sorted(keys)}; "
            f"expected exactly [{PROXY_PIN_KEY!r}]"
        )

    value = data[PROXY_PIN_KEY]
    if not isinstance(value, str) or not _PROXY_PIN_PATTERN.match(value):
        raise ImagePinError(
            f"{_PROXY_PINS_RESOURCE}[{PROXY_PIN_KEY!r}] is not a digest-pinned "
            f"GHCR ref (ghcr.io/.../proxy@sha256:<64hex>): {value!r}"
        )
    return value
