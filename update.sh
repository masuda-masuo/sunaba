#!/usr/bin/env bash
# sunaba local update script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "==> Pulling latest changes from Git..."
git pull

echo "==> Reinstalling package inside virtual environment..."
.venv/bin/pip install -e .

echo "==> Restarting systemd user service..."
systemctl --user daemon-reload
systemctl --user restart sunaba.service

echo ""
echo "==> Done! sunaba has been updated and restarted."
systemctl --user status sunaba.service --no-pager
