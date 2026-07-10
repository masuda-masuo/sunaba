#!/usr/bin/env bash
# Install sunaba systemd user unit and enable auto-start.
# Run once after pip install.  Requires systemd (Linux, WSL2 with systemd enabled).
#
# Usage:
#   ./scripts/install-systemd.sh /path/to/venv
#
# The venv path is required so the unit file's ExecStart points to the
# correct Python interpreter.  When omitted, the script prompts for it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ $# -ge 1 ]; then
    VENV_DIR="$1"
else
    echo -n "Enter path to the venv (e.g. /home/user/venv/sunaba): "
    read -r VENV_DIR
fi

if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "ERROR: $VENV_DIR/bin/python not found.  Is this a valid venv?" >&2
    exit 1
fi

echo "==> Installing sunaba systemd user unit"
echo "    venv dir    : $VENV_DIR"
echo "    project dir : $PROJECT_DIR"
echo "    unit dir    : $USER_UNIT_DIR"
echo ""

mkdir -p "$USER_UNIT_DIR"

sed -e "s|@VENV_DIR@|$VENV_DIR|g" \
    -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
    "$SCRIPT_DIR/sunaba.service" > "$USER_UNIT_DIR/sunaba.service"

systemctl --user daemon-reload
systemctl --user enable --now sunaba.service

echo ""
echo "==> Done.  Useful commands:"
echo "    systemctl --user status sunaba"
echo "    systemctl --user stop sunaba"
echo "    systemctl --user restart sunaba"
echo "    journalctl --user -u sunaba -f"
echo ""

# Ensure user services survive logout (optional, requires root or polkit).
if ! loginctl show-user "$USER" --property=Linger | grep -q '=yes'; then
    echo "NOTE: user lingering is off.  Run this once as root to keep services"
    echo "      running after logout:"
    echo "      sudo loginctl enable-linger $USER"
fi
