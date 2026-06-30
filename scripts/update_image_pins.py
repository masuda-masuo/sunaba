"""CI helper: rewrite ``image_pins.json`` and verify the loader returns it (#331).

Run by ``.github/workflows/build-sandbox-variants.yml`` after each variant build.
It replaces the old ``sed``-on-source update that broke silently (#214): instead
of a regex anchor that can match nothing, this writes the pin file structurally
and then re-reads it through :func:`code_sandbox_mcp.image_pins.load_image_pins`
-- the *same* loader the server uses at runtime.  If a key drifts, the file
moved, or a value is not a digest ref, the loader raises or the post-condition
mismatch exits non-zero, so the failure is loud in CI rather than a stale pin.

The pin file is located via ``importlib.resources`` (not a hardcoded path), so
writer and loader can never disagree about *which* file is authoritative.

Usage (digests are the ``sha256:...`` outputs of docker/build-push-action)::

    PYTHONPATH=src python3 scripts/update_image_pins.py \\
        --repo owner/name --neutral sha256:... --python sha256:... --go sha256:...
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from code_sandbox_mcp.image_pins import PIN_KEYS, _PINS_RESOURCE, load_image_pins


def _build_refs(repo: str, digests: dict[str, str]) -> dict[str, str]:
    refs = {key: f"ghcr.io/{repo}/sandbox@{digests[key]}" for key in PIN_KEYS}
    # Guard against this script and the pin schema drifting apart.
    assert set(refs) == set(PIN_KEYS), (refs, PIN_KEYS)
    return refs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GHCR owner/name")
    parser.add_argument("--neutral", required=True, help="base/neutral image digest (sha256:...)")
    parser.add_argument("--python", required=True, help="python image digest (sha256:...)")
    parser.add_argument("--go", required=True, help="go image digest (sha256:...)")
    args = parser.parse_args(argv)

    refs = _build_refs(
        args.repo,
        {"neutral": args.neutral, "python": args.python, "go": args.go},
    )

    # Locate the authoritative file the loader reads, and write it there.
    path = Path(str(resources.files("code_sandbox_mcp").joinpath(_PINS_RESOURCE)))
    path.write_text(json.dumps(refs, indent=2) + "\n", encoding="utf-8")

    # Post-condition: the loader must now return exactly what we wrote.  This is
    # the gate that makes a silent failure impossible.
    pins = load_image_pins()
    mismatch = {k: (pins.get(k), v) for k, v in refs.items() if pins.get(k) != v}
    if mismatch:
        print(f"image pin update FAILED (loader/key drift): {mismatch}", file=sys.stderr)
        return 1

    print(f"image pins updated and verified at {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
