#!/usr/bin/env bash
# One-time interactive setup: register GitHub App credentials in the OS
# keystore so that sunaba can mint short-lived tokens at runtime.
#
# Prerequisites: pip install sunaba (Phase 1).
# Next step:     ./scripts/install-systemd.sh /path/to/venv
#
# This script:
#   1. Resolves the mcp-token binary (PATH → local cache → download from
#      the public masuda-masuo/mcp-launcher release).
#   2. Prompts for the three GitHub App values (App ID, Installation ID,
#      Private Key file path) and registers them in the OS keystore via
#      mcp-token register.
#
# The credentials are registered under service name "github", i.e. as
# mcp-token/github/* keys in the keystore.  That alone is not enough for
# sunaba's token broker (GITHUB_TOKEN_BROKER_SERVICE=sunaba → mcp-token
# sunaba): mcp-token resolves "sunaba" through launcher.json (next to the
# mcp-token binary, or $MCP_LAUNCHER_CONFIG), so a "sunaba" service entry
# whose env_keys reference these mcp-token/github/* keys must exist there.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${HOME}/.cache/sunaba/bin"
RELEASE_TAG="mcp-token/v1.3.2"
REPO="masuda-masuo/mcp-launcher"

# ---------------------------------------------------------------------------
# Platform detection (mirrors token_broker.py's _BROKER_ASSETS)
# ---------------------------------------------------------------------------
detect_asset() {
    local os arch asset_name
    case "$(uname -s)" in
        Linux)  os="linux" ;;
        Darwin) os="darwin" ;;
        *)      echo "Unsupported OS: $(uname -s)" >&2; return 1 ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch="amd64" ;;
        aarch64|arm64) arch="arm64" ;;
        *) echo "Unsupported arch: $(uname -m)" >&2; return 1 ;;
    esac
    asset_name="mcp-token-${os}-${arch}"
    [ "$os" = "windows" ] && asset_name="${asset_name}.exe"
    echo "$asset_name"
}

# ---------------------------------------------------------------------------
# Resolve mcp-token binary
# ---------------------------------------------------------------------------
resolve_mcp_token() {
    local asset_name="$1"
    local download_url="https://github.com/${REPO}/releases/download/${RELEASE_TAG}/${asset_name}"
    local cached="${CACHE_DIR}/${asset_name}"

    # 1. Check PATH
    if command -v mcp-token &>/dev/null; then
        echo "$(command -v mcp-token)"
        return 0
    fi

    # 2. Check local cache (same dir as token_broker.py)
    mkdir -p "$CACHE_DIR"
    if [ -x "$cached" ]; then
        echo "$cached"
        return 0
    fi

    # 3. Download from public release (no auth needed)
    echo "==> Downloading mcp-token from ${REPO} (${RELEASE_TAG}) ..." >&2
    if command -v curl &>/dev/null; then
        curl -fsSL -o "$cached" "$download_url"
    elif command -v wget &>/dev/null; then
        wget -q -O "$cached" "$download_url"
    else
        echo "ERROR: neither curl nor wget found" >&2
        return 1
    fi
    chmod +x "$cached"
    echo "$cached"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo "==> sunaba setup (Phase 2/3)"
    echo ""
    echo "This script registers your GitHub App credentials in the OS keystore."
    echo "The credentials are stored securely (libsecret / GNOME Keyring) and"
    echo "never written to config files or environment variables."
    echo ""

    local asset
    asset="$(detect_asset)"
    local mcp_token
    mcp_token="$(resolve_mcp_token "$asset")"
    echo "    mcp-token: ${mcp_token}"
    echo ""

    # --- GitHub App ID ---
    echo -n "GitHub App ID: "
    read -r app_id
    while [ -z "$app_id" ]; do
        echo -n "GitHub App ID (required): "
        read -r app_id
    done

    # --- GitHub App Installation ID ---
    echo -n "GitHub App Installation ID: "
    read -r installation_id
    while [ -z "$installation_id" ]; do
        echo -n "GitHub App Installation ID (required): "
        read -r installation_id
    done

    # --- GitHub App Private Key ---
    echo -n "Path to GitHub App Private Key (PEM file): "
    read -r key_path
    while [ -z "$key_path" ] || [ ! -f "$key_path" ]; do
        if [ -n "$key_path" ] && [ ! -f "$key_path" ]; then
            echo "File not found: ${key_path}"
        fi
        echo -n "Path to GitHub App Private Key (PEM file): "
        read -r key_path
    done

    echo ""
    echo "==> Registering credentials in OS keystore ..."

    "$mcp_token" register github APP_ID "$app_id"
    "$mcp_token" register github INSTALLATION_ID "$installation_id"
    "$mcp_token" register github PRIVATE_KEY "$(cat "$key_path")"

    echo ""
    echo "==> Done.  You can verify with:"
    echo "    ${mcp_token} list github"
    echo ""
    echo "Next step: install and start the systemd service:"
    echo "    ./scripts/install-systemd.sh /path/to/venv"
}

main
