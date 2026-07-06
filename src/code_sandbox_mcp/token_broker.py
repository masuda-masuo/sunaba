"""Keystore-broker token provider: mint a fresh GITHUB_TOKEN per container (issue #232).

Background
----------
When the MCP server runs as a long-lived ``streamable-http`` daemon (issue #203)
it is no longer launched per-session by mcp-launcher, so the launcher's keystore
``GITHUB_TOKEN`` injection stops.  PR-A (#223, ``github_auth.py``) solves this by
letting the daemon hold the GitHub App private key itself, but that leaves the
secret residing on the host.

This module is the *broker* alternative: the secret stays in the launcher's OS
keystore and we shell out to a small pinned CLI (mcp-launcher's ``mcp-token``,
issue #25) that mints a short-lived installation token on demand.  Because the
command runs on every container start, the token is always fresh and no daemon
needs to hold long-lived credentials.

Configuration (host env)
------------------------
``GITHUB_TOKEN_COMMAND``
    Explicit command to exec; stdout becomes ``GITHUB_TOKEN``.  Takes priority.
``GITHUB_TOKEN_BROKER_SERVICE``
    Service name for the *vendored* broker.  When set (and no explicit command),
    the pinned ``mcp-token`` binary is resolved/downloaded and run as
    ``mcp-token <service>``.
``GITHUB_TOKEN_BROKER_BIN``
    Override path to an already-present broker binary (skips download).
``GITHUB_TOKEN_COMMAND_TIMEOUT``
    Command timeout in seconds (default 30).
``CODE_SANDBOX_TOKEN_BROKER_CACHE_DIR``
    Override the directory used to cache the downloaded broker binary.
``CODE_SANDBOX_TOKEN_BROKER_NO_DOWNLOAD``
    Disable the network fetch entirely (verify-only): an already-cached,
    checksum-matching binary is reused, otherwise resolution fails and the
    caller falls back to the static token.  Use this to pin operators to a
    pre-provisioned binary and forbid implicit downloads.

The binary itself is never committed; the repository pins only the release tag
and per-platform SHA-256 (``_BROKER_ASSETS``).  The asset is fetched once,
checksum-verified, and cached locally (pin + fetch + verify).

Bootstrapping caveat
--------------------
The broker binary lives in a *private* repo, so ``_download_and_verify`` needs a
``GITHUB_TOKEN`` / ``GH_TOKEN`` to fetch it -- the very token the broker exists
to provide.  This is only a one-time bootstrap: because the version is pinned
and checksum-verified, fetch it once while a token is available (e.g. during
setup, or a session still launched via mcp-launcher) and every later run reuses
the cache.  For a fully tokenless daemon, pre-provision the binary out of band
and point ``GITHUB_TOKEN_BROKER_BIN`` at it (or pre-warm
``CODE_SANDBOX_TOKEN_BROKER_CACHE_DIR``); unauthenticated fetch of a private asset does
not work.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx
import platformdirs

logger = logging.getLogger(__name__)

# Pinned mcp-launcher "mcp-token" broker release (issue #232 / mcp-launcher#25).
BROKER_REPO = "masuda-masuo/mcp-launcher"
BROKER_TAG = "mcp-token/v1.1.0"

# (os, arch) -> (asset_name, sha256). Verified against the GitHub release.
_BROKER_ASSETS: dict[tuple[str, str], tuple[str, str]] = {
    ("linux", "amd64"): ("mcp-token-linux-amd64", "55479987cb280258605ec22fde720a2fc4e289ad31abb446731bde8311bc1fdc"),
    ("linux", "arm64"): ("mcp-token-linux-arm64", "1b88014180f48c25f22902fd31fcbe95ea9c5e8d264c6af36080b1c961e3f87b"),
    ("darwin", "amd64"): ("mcp-token-darwin-amd64", "1729994f22411c87920ac96d8050ca6bbe3fca05d9a21460708c11ff3d61d57b"),
    ("darwin", "arm64"): ("mcp-token-darwin-arm64", "e0ac4ce9e5f460a3332764f5d716a8a688ef48bf867e25531ef81ba763cc9b4e"),
    ("windows", "amd64"): ("mcp-token-windows-amd64.exe", "4e93ebd444d413ee04d839209ade81675a6807b5d6e9cdee2cf46edab1fca24f"),
}

# Normalise platform.machine() spellings to our short arch tokens.
_MACHINE_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def _platform_key() -> tuple[str, str] | None:
    """Return the ``(os, arch)`` key for this host, or ``None`` if unsupported."""
    system = platform.system().lower()
    arch = _MACHINE_ALIASES.get(platform.machine().lower())
    if system not in {"linux", "darwin", "windows"} or arch is None:
        return None
    if (system, arch) not in _BROKER_ASSETS:
        return None
    return (system, arch)


def _cache_dir() -> Path:
    """Return the directory used to cache the downloaded broker binary."""
    override = os.environ.get("CODE_SANDBOX_TOKEN_BROKER_CACHE_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_cache_dir("code-sandbox-mcp")) / "bin"


def _dest_path(key: tuple[str, str]) -> Path:
    """Return the cache destination for the broker binary of *key*."""
    suffix = ".exe" if key[0] == "windows" else ""
    tag = BROKER_TAG.replace("/", "_")
    return _cache_dir() / f"mcp-token-{tag}-{key[0]}-{key[1]}{suffix}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_sha256(data: bytes, expected: str) -> None:
    """Raise ``RuntimeError`` when *data* does not match *expected* SHA-256."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise RuntimeError(f"sha256 mismatch: expected {expected}, got {digest}")


def _download_and_verify(asset_name: str, expected_sha: str, dest: Path) -> None:
    """Download *asset_name* from the pinned release, verify, and place at *dest* (0700)."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    tag = quote(BROKER_TAG, safe="")
    rel_url = f"https://api.github.com/repos/{BROKER_REPO}/releases/tags/{tag}"
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        rel = client.get(rel_url, headers={"Accept": "application/vnd.github+json", **auth})
        rel.raise_for_status()
        asset = next((a for a in rel.json().get("assets", []) if a["name"] == asset_name), None)
        if asset is None:
            raise RuntimeError(f"asset {asset_name} not found in release {BROKER_TAG}")
        resp = client.get(asset["url"], headers={"Accept": "application/octet-stream", **auth})
        resp.raise_for_status()
        data = resp.content
    _check_sha256(data, expected_sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, stat.S_IRWXU)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def resolve_broker_binary(*, allow_download: bool = True) -> Path | None:
    """Resolve the pinned ``mcp-token`` binary path, fetching+verifying if needed.

    Returns ``None`` (never raises) when the platform is unsupported, the
    cached copy is corrupt with downloads disabled, or the fetch fails.
    """
    override = os.environ.get("GITHUB_TOKEN_BROKER_BIN")
    if override:
        path = Path(override)
        return path if path.exists() else None
    key = _platform_key()
    if key is None:
        logger.warning(
            "token broker: unsupported platform %s/%s", platform.system(), platform.machine()
        )
        return None
    asset_name, sha256 = _BROKER_ASSETS[key]
    dest = _dest_path(key)
    if dest.exists() and _sha256_file(dest) == sha256:
        return dest
    if not allow_download or os.environ.get("CODE_SANDBOX_TOKEN_BROKER_NO_DOWNLOAD"):
        return None
    try:
        _download_and_verify(asset_name, sha256, dest)
    except Exception as exc:  # noqa: BLE001 - best-effort; fall back to static token
        logger.warning("token broker: failed to fetch %s: %s", asset_name, exc)
        return None
    return dest


def _resolve_command() -> list[str] | None:
    """Return the broker command argv, or ``None`` when no broker is configured."""
    explicit = os.environ.get("GITHUB_TOKEN_COMMAND")
    if explicit:
        return shlex.split(explicit, posix=(os.name != "nt"))
    service = os.environ.get("GITHUB_TOKEN_BROKER_SERVICE")
    if service:
        binary = resolve_broker_binary()
        if binary is not None:
            return [str(binary), service]
    return None


def mint_token() -> str | None:
    """Mint a fresh token via the configured broker command, or ``None``.

    Never raises and never logs token material: on any failure it returns
    ``None`` so the caller can fall back to a static ``GITHUB_TOKEN``.
    """
    command = _resolve_command()
    if command is None:
        return None
    timeout = float(os.environ.get("GITHUB_TOKEN_COMMAND_TIMEOUT", "30"))
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("token broker: command %r failed to run: %s", command[0], exc)
        return None
    if proc.returncode != 0:
        logger.warning("token broker: command %r exited %d", command[0], proc.returncode)
        return None
    token = proc.stdout.strip()
    if not token:
        logger.warning("token broker: command %r produced empty output", command[0])
        return None
    logger.info("token broker: minted fresh GITHUB_TOKEN via %s", command[0])
    return token
