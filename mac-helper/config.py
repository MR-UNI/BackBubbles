"""
BackBubbles Mac Helper — configuration.

All tuneable constants live here.  Override defaults by exporting environment
variables before starting the daemon (or passing keyword arguments in tests).
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

    # ------------------------------------------- Google Messages for Web
    # Directory where the persistent Chromium profile is stored.
    # Cookies and localStorage are preserved here between daemon restarts so
    # the user only needs to scan the QR code once.
    gmessages_profile_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "BB_GMESSAGES_PROFILE_DIR",
                str(Path.home() / ".backbubbles" / "gmessages-profile"),
            )
        )
    )
    # Run the browser in headless mode (True) or with a visible window (False).
    # Set to False (or use --pair) only when re-pairing.
    gmessages_headless: bool = field(
        default_factory=lambda: os.environ.get("BB_GMESSAGES_HEADLESS", "1") not in ("0", "false", "False")
    )
    # Seconds between inbox scans for incoming messages.
    gmessages_poll_interval: float = field(
        default_factory=lambda: float(os.environ.get("BB_GMESSAGES_POLL_INTERVAL", "3.0"))
    )

    # -------------------------------------------------- chat.db poll interval
    # How often (seconds) to poll chat.db for new outgoing messages.
    poll_interval: float = field(
        default_factory=lambda: float(os.environ.get("BB_POLL_INTERVAL", "2.0"))
    )

    # ------------------------------------------------- message-service filter
    # chat.db service value used for non-iMessage (green-bubble) conversations.
    # Common values: 'SMS', 'MMS'.  An empty list means *every* non-iMessage
    # service is watched.
    sms_services: list[str] = field(
        default_factory=lambda: ["SMS", "MMS"]
    )

    def validate(self) -> None:
        """Raise *ValueError* for obviously wrong configuration."""
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.gmessages_poll_interval <= 0:
            raise ValueError("gmessages_poll_interval must be positive")
