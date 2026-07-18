"""Image resolution and prewarming for sandbox containers."""

from __future__ import annotations

import logging
import re

from sunaba import image_pins

logger: logging.Logger = logging.getLogger(__name__)

# -- Default sandbox image (Issue #584) -----------------------------------
# The default is the *union* of every language toolchain, not a guess at which
# one this project needs.  Host-side language detection used to run here (#313):
# it probed the GitHub contents API before starting the container and picked a
# matching variant.  That ordering was the bug -- the guess came before an
# irreversible decision (an image is immutable once the container runs), while
# the accurate detector (``edit_verify.detect_languages``, which reads the real
# files) only runs afterwards.  A failed probe therefore landed init on an image
# without the toolchain the code needed, and the first verify failed the gate for
# a reason unrelated to the code (#584).  Making the default a superset removes
# the guess instead of trying to make it more reliable.
#
# The digest pins live as data in ``sunaba/image_pins.json``; CI
# (``.github/workflows/build-sandbox-variants.yml``) rewrites that file after
# each variant build, then verifies this loader returns the new digest.  This
# replaces the old ``sed``-on-source approach that broke silently when the
# constants moved or were reformatted (#214 / #331).  All refs are digest-pinned
# per ``docs/design_multilang_support.md`` section 6.
_image_pins: dict[str, str] = image_pins.load_image_pins()

#: All-in-one image: base + every language toolchain verify can dispatch to
#: (#584).  A superset of the dispatch matrix on purpose -- see
#: ``docker/Dockerfile.full``.
_FULL_IMAGE: str = _image_pins["full"]

#: Lean images, reachable only through an explicit ``image=`` (which also
#: accepts the aliases "neutral" / "python" / "go" / "js" / "full"; alias
#: resolution reads :data:`_image_pins` directly).  ``neutral`` is also the
#: ``FROM`` parent the variants are built on.
_NEUTRAL_IMAGE: str = _image_pins["neutral"]
_PYTHON_IMAGE: str = _image_pins["python"]
_GO_IMAGE: str = _image_pins["go"]
_JS_IMAGE: str = _image_pins["js"]

#: Image used when ``sandbox_initialize`` is called without ``image=``.
#: Overridable via the ``--default-image`` CLI flag (server.py).
_DEFAULT_IMAGE: str = _FULL_IMAGE


def _resolve_image_ref(image: str) -> str:
    """Resolve a tag-based image reference to a digest-based one.

    If *image* already contains a ``@sha256:...`` digest, return it
    as-is.  Otherwise pull the image by tag and extract its digest
    from the local Docker metadata, returning ``image@sha256:...``.

    This allows callers to pass variant tags (e.g. ``sandbox:go``)
    instead of requiring a fully-qualified digest every time.
    """
    from sunaba.tools.container import _docker
    # Already a digest reference — nothing to resolve
    if re.search(r"@sha256:[a-f0-9]{64}$", image):
        return image

    _ensure_image(image)
    try:
        img = _docker().images.get(image)
    except Exception as e:
        raise ValueError(
            f"Could not resolve digest for image: {image!r}: {e}"
        )

    for repo_digest in img.attrs.get("RepoDigests") or []:
        if "@sha256:" in repo_digest:
            logger.info("Resolved image %s → %s", image, repo_digest)
            return repo_digest

    raise ValueError(
        f"Could not resolve digest for image: {image!r}. "
        "The image may not have been pushed to a registry."
    )


def _ensure_image(image: str) -> None:
    """Ensure the specified Docker image is available locally.

    Calls ``docker pull`` to fetch the image if not already present.
    """
    import docker.errors

    import docker

    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        logger.info("Pulling image %s...", image)
        client.images.pull(image)


def prewarm_default_image() -> None:
    """Pull the default image so first use is warm.

    A cold-start image pull can exceed the MCP/HTTP request timeout, so the
    first ``sandbox_initialize`` fails even though the pull finishes in the
    background and the next call succeeds (Issue #303).  Pulling ahead of time
    — at server startup and periodically — removes that first-call cliff and
    does not depend on progress notifications keeping the connection alive.

    Only the *default* image is prewarmed.  It used to pull the python and go
    variants too, because host-side detection could silently pick one of them
    and trade one cold pull for another.  Detection is gone (#584): an init
    with no ``image=`` always lands on the default, and the lean variants are
    reachable only by asking for them explicitly — a caller who does that can
    afford the pull, and ``sandbox_initialize`` keeps the connection alive with
    progress notifications while it happens (#298).

    Reads the module-level :data:`_DEFAULT_IMAGE` at call time so a
    ``--default-image`` override applied before the prewarm thread starts is
    honoured.  Any failure (registry hiccup, Docker down) is swallowed so a bad
    pull never blocks startup; the next refresh cycle retries.
    """
    images = {_DEFAULT_IMAGE}
    for image in images:
        try:
            _ensure_image(image)
            logger.info("prewarmed sandbox image %s", image)
        except Exception:  # noqa: BLE001 - prewarm must never break startup
            logger.exception("prewarm of image %s failed", image)


def _select_initial_image(image: str | None) -> str:
    """Choose the image for a new container: the explicit one, or the default.

    There is deliberately **no language detection here** (Issue #584).  There
    used to be: the host probed the GitHub contents API before starting the
    container and picked a matching variant image.  That put the *guess* before
    an irreversible decision -- a container's image is immutable once running,
    and for ``clone_repo`` the files only exist inside the container afterwards
    -- while the *accurate* detector (``edit_verify.detect_languages``, which
    reads the real files) runs afterwards, when nothing can be changed about it.
    So when the probe failed (rate limit, timeout, private repo), init silently
    landed on an image without the toolchain the code actually needed, and the
    first verify failed the gate for a reason that had nothing to do with the
    code.

    The fix is to remove the guess rather than improve it: the default image is
    the union of every toolchain verify can dispatch to (``sandbox:full``), so
    whatever the in-container detector concludes, the tools are there.  An
    explicit ``image=`` remains the escape hatch -- and the only way to ask for
    a lean variant.
    """
    return image or _DEFAULT_IMAGE

