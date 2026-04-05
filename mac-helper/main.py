"""
BackBubbles Mac Helper — main entry point.

Orchestrates:
  1. Watching ~/Library/Messages/chat.db for outgoing non-iMessage messages.
  2. Forwarding those messages to Google Messages for Web via Playwright.
  3. Receiving delivery status updates from GMWeb and updating chat.db.
  4. Receiving incoming SMS/RCS from GMWeb and injecting them into chat.db.

Usage
-----
    # Normal operation (headless, uses persisted session):
    python main.py

    # First-time setup or re-pairing (opens headed browser for QR scan):
    python main.py --pair

Optional environment variables (see config.py for defaults):
    BB_GMESSAGES_PROFILE_DIR   — path to the Chromium profile directory
    BB_GMESSAGES_HEADLESS      — '0' to force headed mode
    BB_GMESSAGES_POLL_INTERVAL — seconds between inbox scans (default: 3.0)
    BB_POLL_INTERVAL           — seconds between chat.db polls (default: 2.0)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from config import Config
from gmessages_client import GMessagesClient
from chat_db_watcher import ChatDBWatcher, OutgoingMessage
from db_injector import ChatDBInjector

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backbubbles")

# ---------------------------------------------------------------------------
# Main application class
# ---------------------------------------------------------------------------


class BackBubbles:
    def __init__(self, config: Config, pair_mode: bool = False) -> None:
        self._config = config
        self._injector = ChatDBInjector(config.chat_db_path)
        self._bridge = GMessagesClient(
            config,
            on_incoming=self._handle_android_message,
            pair_mode=pair_mode,
        )
        self._watcher = ChatDBWatcher(config)
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("BackBubbles starting up…")
        self._config.validate()

        self._bridge.start()
        log.info("Google Messages for Web client started.")

        try:
            self._watcher.run_forever(
                on_message=self._handle_outgoing_message,
                stop_event=self._stop_event,
            )
        finally:
            self._bridge.stop()
            log.info("BackBubbles shut down.")

    def stop(self) -> None:
        log.info("Shutdown requested.")
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Outgoing message (Mac → GMWeb)
    # ------------------------------------------------------------------

    def _handle_outgoing_message(self, msg: OutgoingMessage) -> None:
        """Called by ChatDBWatcher for each new outgoing SMS/MMS."""
        log.info(
            "Relaying outgoing %s to %s (guid=%s)", msg.service, msg.handle, msg.guid
        )
        self._bridge.enqueue(
            {
                "type": "send_sms",
                "message_id": msg.guid,
                "to": msg.handle,
                "body": msg.body,
                "service": msg.service,
            }
        )

    # ------------------------------------------------------------------
    # Incoming message from GMWeb
    # ------------------------------------------------------------------

    def _handle_android_message(self, data: dict) -> None:
        """Dispatch events arriving from the GMWeb client."""
        msg_type = data.get("type")

        if msg_type == "delivery_status":
            self._on_delivery_status(data)

        elif msg_type == "incoming_sms":
            self._on_incoming_sms(data)

        else:
            log.warning("Unknown message type from GMWeb client: %r", msg_type)

    def _on_delivery_status(self, data: dict) -> None:
        guid = data.get("message_id", "")
        status = data.get("status", "")
        log.info("Delivery status: guid=%s status=%s", guid, status)

        if status == "sent":
            self._injector.mark_message_sent(guid)
        elif status == "delivered":
            self._injector.mark_message_delivered(guid)
        elif status == "read":
            self._injector.mark_message_read(guid)
        elif status == "failed":
            error_code = int(data.get("error_code", 4))
            self._injector.mark_message_failed(guid, error_code)
        else:
            log.warning("Unrecognised delivery status: %r", status)

    def _on_incoming_sms(self, data: dict) -> None:
        sender = data.get("from", "")
        body = data.get("body", "")
        service = data.get("service", "SMS")
        timestamp = data.get("timestamp")
        log.info("Incoming %s from %s: %r", service, sender, body[:60])
        self._injector.insert_incoming_message(
            sender=sender,
            body=body,
            service=service,
            unix_timestamp=timestamp,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BackBubbles Mac Helper — SMS relay via Google Messages for Web"
    )
    parser.add_argument(
        "--pair",
        action="store_true",
        help=(
            "Open a headed browser window and prompt the user to scan the "
            "Google Messages QR code.  Use this on first run or whenever the "
            "session expires.  After pairing, restart without --pair for "
            "normal headless operation."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = Config()

    app = BackBubbles(config, pair_mode=args.pair)

    def _signal_handler(sig, frame):
        app.stop()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        app.run()
    except ValueError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)
    except Exception:
        log.exception("Fatal error")
        sys.exit(2)


if __name__ == "__main__":
    main()

