"""
BackBubbles Mac Helper — configuration.

All tuneable constants live here.  Copy ``config.example.toml`` and edit
it, or just override the defaults by exporting environment variables before
starting the daemon.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # ------------------------------------------------------------------ paths
    chat_db_path: Path = field(
        default_factory=lambda: Path.home() / "Library" / "Messages" / "chat.db"
    )

    # -------------------------------------------------- android bridge address
    # The hostname or IP of the Android device running BackBubbles Bridge.
    android_host: str = field(
        default_factory=lambda: os.environ.get("BB_ANDROID_HOST", "")
    )
    # The WebSocket port the Android app listens on.
    android_port: int = field(
        default_factory=lambda: int(os.environ.get("BB_ANDROID_PORT", "8765"))
    )

    # -------------------------------------------------- polling & reconnection
    # How often (seconds) to poll chat.db for new outgoing messages.
    poll_interval: float = field(
        default_factory=lambda: float(os.environ.get("BB_POLL_INTERVAL", "2.0"))
    )
    # Seconds to wait before attempting a reconnect after a WebSocket failure.
    reconnect_delay: float = field(
        default_factory=lambda: float(os.environ.get("BB_RECONNECT_DELAY", "5.0"))
    )

    # ------------------------------------------------- message-service filter
    # chat.db service value used for non-iMessage (green-bubble) conversations.
    # Common values: 'SMS', 'MMS'.  An empty list means *every* non-iMessage
    # service is watched.
    sms_services: list[str] = field(
        default_factory=lambda: ["SMS", "MMS"]
    )

    # ----------------------------------------------------------- misc options
    # Maximum number of messages to buffer when the bridge is disconnected.
    send_queue_maxsize: int = 256

    @property
    def ws_uri(self) -> str:
        return f"ws://{self.android_host}:{self.android_port}"

    def validate(self) -> None:
        """Raise *ValueError* for obviously wrong configuration."""
        if not self.android_host:
            raise ValueError(
                "android_host is not set.  "
                "Export BB_ANDROID_HOST=<ip-of-your-android-device> "
                "or edit config.py before starting BackBubbles."
            )
        if not (1 <= self.android_port <= 65535):
            raise ValueError(f"android_port must be 1–65535, got {self.android_port}")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.reconnect_delay < 0:
            raise ValueError("reconnect_delay must be non-negative")
