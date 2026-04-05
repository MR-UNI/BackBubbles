"""
BackBubbles Mac Helper — main entry point.

Orchestrates:
  1. Watching ~/Library/Messages/chat.db for outgoing non-iMessage messages.
  2. Forwarding those messages to the Android BackBubbles Bridge app.
  3. Receiving delivery status updates from Android and updating chat.db.
  4. Receiving incoming SMS/RCS from Android and injecting them into chat.db.

Usage
-----
    python main.py

Required environment variable:
    BB_ANDROID_HOST   — IP address of your Android device (e.g. 192.168.1.42)

Optional environment variables (see config.py for defaults):
    BB_ANDROID_PORT   — WebSocket port (default: 8765)
    BB_POLL_INTERVAL  — Seconds between chat.db polls (default: 2.0)
    BB_RECONNECT_DELAY — Seconds before reconnecting (default: 5.0)
"""

from __future__ import annotations

import logging
import signal
import sys
import threading

from config import Config
from bridge_client import BridgeClient
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
    def __init__(self, config: Config) -> None:
        self._config = config
        self._injector = ChatDBInjector(config.chat_db_path)
        self._bridge = BridgeClient(config, on_incoming=self._handle_android_message)
        self._watcher = ChatDBWatcher(config)
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> None:
        log.info("BackBubbles starting up…")
        self._config.validate()

        self._bridge.start()
        log.info("Bridge client started.")

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
    # Outgoing message (Mac → Android)
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
    # Incoming message from Android
    # ------------------------------------------------------------------

    def _handle_android_message(self, data: dict) -> None:
        """Dispatch messages arriving from the Android bridge."""
        msg_type = data.get("type")

        if msg_type == "delivery_status":
            self._on_delivery_status(data)

        elif msg_type == "incoming_sms":
            self._on_incoming_sms(data)

        else:
            log.warning("Unknown message type from bridge: %r", msg_type)

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


def main() -> None:
    config = Config()

    app = BackBubbles(config)

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
