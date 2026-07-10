"""Push notifications for observability (§9).

Supports:
- OS desktop notifications (``notify-send`` on Linux, ``osascript`` on macOS)
- Webhook notifications (HTTP POST to a configurable URL)

Triggers:
- Boundary-crossing operations
- Failure threshold exceeded
- Long-running execution
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any
from urllib import request as urllib_request

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_webhook_url: str | None = None
_failure_threshold: int = 5  # notify after N consecutive failures
_long_run_seconds: int = 300  # notify after 5 minutes


def configure(
    webhook_url: str | None = None,
    failure_threshold: int = 5,
    long_run_seconds: int = 300,
) -> None:
    """Configure notification parameters."""
    global _webhook_url, _failure_threshold, _long_run_seconds
    _webhook_url = webhook_url
    _failure_threshold = failure_threshold
    _long_run_seconds = long_run_seconds


# ---------------------------------------------------------------------------
# OS notification
# ---------------------------------------------------------------------------


def _notify_os(title: str, message: str) -> bool:
    """Send an OS desktop notification.

    Returns True if the notification was attempted.
    """
    try:
        if sys.platform == "linux":
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True,
                timeout=3,
            )
            return True
        elif sys.platform == "darwin":
            script = (
                f'display notification "{message}" with title "{title}"'
            )
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=3,
            )
            return True
        elif sys.platform == "win32":
            # Windows: use PowerShell toast
            ps_script = (
                f'[Windows.UI.Notifications.ToastNotificationManager,'
                f'Windows.UI.Notifications,ContentType=WindowsRuntime]'
                f'| Out-Null; '
                f'$template = [Windows.UI.Notifications.'
                f'ToastNotificationManager]::'
                f'GetTemplateContent(0); '
                f'$template.GetElementsByTagName("text")[0]'
                f'.AppendChild($template.CreateTextNode("{title}")); '
                f'$template.GetElementsByTagName("text")[1]'
                f'.AppendChild($template.CreateTextNode("{message}"))'
            )
            subprocess.run(
                ["powershell.exe", "-Command", ps_script],
                capture_output=True,
                timeout=5,
            )
            return True
    except Exception as e:
        logger.warning("OS notification failed: %s", e)
    return False


# ---------------------------------------------------------------------------
# Webhook notification
# ---------------------------------------------------------------------------


def _notify_webhook(payload: dict[str, Any]) -> bool:
    """Send a webhook notification via HTTP POST."""
    if not _webhook_url:
        return False
    try:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib_request.Request(
            _webhook_url,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib_request.urlopen(req, timeout=5)
        return True
    except Exception as e:
        logger.warning("Webhook notification failed: %s", e)
        return False


