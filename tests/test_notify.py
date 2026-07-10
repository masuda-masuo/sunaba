"""Tests for the notification module (Issue #44)."""
from __future__ import annotations

from sunaba.notify import (
    configure,
)


class TestNotifyConfigure:
    """Tests for notification configuration."""

    def test_configure_defaults(self) -> None:
        configure()
        # Should not raise

    def test_configure_with_webhook(self) -> None:
        configure(webhook_url="https://example.com/webhook")
        # Should not raise

    def test_configure_with_custom_thresholds(self) -> None:
        configure(failure_threshold=10, long_run_seconds=600)
        # Should not raise
