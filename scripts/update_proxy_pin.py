"""CI helper: rewrite ``proxy_pin.json`` and verify the loader returns it (#432).

Run by ``.github/workflows/build-proxy-image.yml`` after the egress-proxy
sidecar image (``docker/Dockerfile.proxy``) is built and pushed to GHCR.  It is
the proxy-side sibling of ``update_image_pins.py`` and follows the same
anti-#214 discipline: the pin is written *structurally* (no ``sed`` regex anchor
that can silently match nothing) and then re-read through the SAME loader the
server uses at runtime -- :func:`code_sandbox_mcp.image_pins.load_proxy_pin`.
If a key drifts, the file moves, or the value is not a digest ref, the loader
raises or the post-condition mismatch exits non-zero, so a stale/unusable pin
fails loudly in CI instead of shipping in silence.

The pin file is located via ``importlib.resources`` (not a hardcoded path), so
writer and loader can never disagree about *which* file is authoritative.

Usage (digest is the ``sha256:...`` output of docker/build-push-action)::

    PYTHONPATH=src python3 scripts/update_proxy_pin.py \\
        --repo owner/name --digest sha256:...
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from code_sandbox_mcp.image_pins import (
    PROXY_PIN_KEY,
    _PROXY_PINS_RESOURCE,
    load_proxy_pin,
)


def main(argv: list[str] | None = None) -> int:
    """Write and verify ``proxy_pin.json`` from the pushed image digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GHCR owner/name")
    parser.add_argument("--digest", required=True, help="proxy image digest (sha256:...)")
    args = parser.parse_args(argv)

    ref = f"ghcr.io/{args.repo}/proxy@{args.digest}"

    # Locate the authoritative file the loader reads, and write it there.
    path = Path(str(resources.files("code_sandbox_mcp").joinpath(_PROXY_PINS_RESOURCE)))
    path.write_text(json.dumps({PROXY_PIN_KEY: ref}, indent=2) + "\n", encoding="utf-8")

    # Post-condition: the loader must now return exactly what we wrote.
    pin = load_proxy_pin()
    if pin != ref:
        print(
            f"proxy pin update FAILED (loader/key drift): got {pin!r}, want {ref!r}",
            file=sys.stderr,
        )
        return 1

    print(f"proxy pin updated and verified at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
