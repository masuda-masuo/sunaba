"""Detection-based default image selection (Issue #313).

When :func:`sandbox_initialize` is called without an explicit ``image``, pick
the sandbox *variant* image that matches the project's language instead of
hardcoding a single language default.  "Python is the default" only ever made
sense because this repository happens to be Python; a general-purpose sandbox
must not bake one language into its default.

Detection runs **host-side, before the container starts** -- a container's
image is immutable once running, and for ``clone_repo`` the project files only
appear *inside* the container after start, so the host inspects the GitHub
repository contents via the REST API (best-effort, network) when a repo is
being cloned.

Unknown / unsupported / py+go-polyglot projects fall back to the neutral
``sandbox:base`` image (node + VCS + search tooling, *no* language toolchain)
and never block init.  A human-readable *notice* explains any fallback so the
caller can pass an explicit ``image=`` override.  The neutral base does not
pretend to provide a language toolchain: a later verify / type_check / lint
loudly reports ``not_available`` per the loud-failure contract
(``docs/design-multilang-support.md`` §4/§6).

This module is intentionally free of Docker / token plumbing so the mapping
logic stays pure and unit-testable; callers supply the image map, the neutral
fallback, and (optionally) a token.
"""

from __future__ import annotations

import fnmatch
import json
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field

# Reuse the §3 project markers so image selection and verify dispatch never
# drift apart (single source of truth for "which file means which language").
from sunaba.edit_verify import _DETECTION_MARKERS

#: Marker files for languages the tool deliberately does *not* support
#: (``docs/design-multilang-support.md`` §9 freezes support at py / js / go).
#: Used only to turn a silent neutral fallback into a *loud* notice so the
#: caller understands why no language toolchain is present.
_UNSUPPORTED_MARKERS: dict[str, str] = {
    "Cargo.toml": "Rust",
    "pom.xml": "Java",
    "build.gradle": "Java",
    "build.gradle.kts": "Kotlin",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "mix.exs": "Elixir",
    "Package.swift": "Swift",
    "pubspec.yaml": "Dart",
    "CMakeLists.txt": "C/C++",
}

@dataclass
class LanguageDetection:
    """Outcome of inspecting a project's root for language markers.

    Attributes:
        supported: Detected language keys that have a toolchain story
            (subset of ``{"python", "go", "js", "ts"}``).
        unsupported: Friendly names of detected-but-unsupported languages
            (e.g. ``{"Rust"}``) used purely for the fallback notice.
        source: Where the signal came from -- ``"github-api"`` or
            ``"none"`` (nothing inspected).
    """

    supported: set[str] = field(default_factory=set)
    unsupported: set[str] = field(default_factory=set)
    source: str = "none"

    @property
    def is_empty(self) -> bool:
        """True when nothing -- supported or unsupported -- was detected."""
        return not self.supported and not self.unsupported


def _classify_names(names: Iterable[str]) -> tuple[set[str], set[str]]:
    """Map a flat list of top-level file names to (supported, unsupported)."""
    supported: set[str] = set()
    unsupported: set[str] = set()
    for name in names:
        if not name:
            continue
        for pattern, lang in _DETECTION_MARKERS:
            if fnmatch.fnmatch(name, pattern):
                supported.add(lang)
                break
        if name in _UNSUPPORTED_MARKERS:
            unsupported.add(_UNSUPPORTED_MARKERS[name])
    return supported, unsupported


def detect_from_github(
    repo: str,
    token: str | None = None,
    timeout: float = 5.0,
) -> LanguageDetection | None:
    """Best-effort language detection via the GitHub contents API.

    Lists the repository root and classifies the top-level files.  Returns
    ``None`` (caller falls back to neutral) on *any* failure -- network error,
    rate limit, private repo without a token, unexpected payload.  Never
    raises; never blocks init.
    """
    url = f"https://api.github.com/repos/{repo}/contents"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sunaba",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    names = [
        item.get("name", "")
        for item in payload
        if isinstance(item, dict) and item.get("type") == "file"
    ]
    supported, unsupported = _classify_names(names)
    return LanguageDetection(supported, unsupported, source="github-api")


def select_variant(
    detection: LanguageDetection,
    *,
    language_image_map: dict[str, str],
    neutral_image: str,
) -> tuple[str, str | None]:
    """Map a detection result to ``(image, notice)``.

    Only languages with a dedicated variant image (``python``, ``go``) upgrade
    off the neutral base.  ``js`` / ``ts`` ride on the neutral base (node lives
    there; js dev tooling is not yet packaged).  py+go polyglot, unsupported,
    and unknown projects all stay neutral -- the first two with an explanatory
    notice, the last silently (neutral *is* the correct default when unknown).
    """
    py_image = language_image_map.get("python")
    go_image = language_image_map.get("go")
    has_py = "python" in detection.supported and py_image is not None
    has_go = "go" in detection.supported and go_image is not None

    if has_py and has_go:
        return neutral_image, (
            "py+go polyglot project detected, but no combined image is built. "
            "Started on neutral base (no language toolchain). Pass image= "
            "explicitly (the python or go variant) to run language tools."
        )
    if has_py:
        return py_image, None  # type: ignore[return-value]
    if has_go:
        return go_image, None  # type: ignore[return-value]
    if detection.supported & {"js", "ts"}:
        # node is in the base image; no js-specific toolchain image exists yet.
        return neutral_image, None
    if detection.unsupported:
        names = ", ".join(sorted(detection.unsupported))
        return neutral_image, (
            f"detected {names}, which has no variant image (supported: "
            "Python / Go / JS). Started on neutral base; language toolchain "
            "unavailable. Pass image= explicitly to override."
        )
    # No recognized markers -> neutral base is the correct silent default.
    return neutral_image, None


def resolve_initial_image(
    *,
    explicit_image: str | None,
    target_repo: str | None,
    token: str | None,
    language_image_map: dict[str, str],
    neutral_image: str,
) -> tuple[str, str | None]:
    """Resolve the image to start a container with, plus an optional notice.

    Precedence:

    1. *explicit_image* wins outright (manual escape hatch) -- no detection.
    2. If a *target_repo* is known, the GitHub API is probed (best-effort).
    3. Otherwise the *neutral_image* is used (bare init has nothing to inspect).
    """
    if explicit_image:
        return explicit_image, None

    detection: LanguageDetection | None = None
    if target_repo:
        detection = detect_from_github(target_repo, token=token)

    if detection is None:
        return neutral_image, None
    return select_variant(
        detection,
        language_image_map=language_image_map,
        neutral_image=neutral_image,
    )
