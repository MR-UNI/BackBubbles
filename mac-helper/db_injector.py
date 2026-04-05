"""
BackBubbles Mac Helper — chat.db injector.

Writes delivery-status updates and incoming messages back into
~/Library/Messages/chat.db so that Messages.app shows them without
manual intervention.

Apple's chat.db uses a number of normalised tables; we replicate the minimum
set of writes that Messages.app itself performs for an incoming SMS.

WARNING: Writing directly to chat.db is an undocumented hack.  It works on
macOS Ventura / Sonoma but may break on future OS versions.  Always make a
backup before enabling write-back.

IMPORTANT: The process must have Full Disk Access (System Settings →
Privacy & Security → Full Disk Access) for the writes to succeed.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Apple's reference epoch: 2001-01-01 00:00:00 UTC in Unix time.
_APPLE_EPOCH_OFFSET = 978307200  # seconds


def unix_to_apple_ns(unix_timestamp: float) -> int:
    """Convert a Unix timestamp (seconds) to Apple nanoseconds since 2001-01-01."""
    return int((unix_timestamp - _APPLE_EPOCH_OFFSET) * 1_000_000_000)


def apple_ns_to_unix(apple_ns: int) -> float:
    """Convert Apple nanoseconds to a Unix timestamp (seconds)."""
    return apple_ns / 1_000_000_000 + _APPLE_EPOCH_OFFSET


class ChatDBInjector:
    """
    Writes messages and status updates into chat.db.

    Parameters
    ----------
    db_path:
        Path to chat.db (default: ~/Library/Messages/chat.db).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = Path.home() / "Library" / "Messages" / "chat.db"
        self._db_path = db_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_message_sent(self, guid: str) -> None:
        """
        Mark an outgoing message as *sent* (not yet delivered).

        Sets ``is_sent = 1`` and ``error = 0`` for the message identified by
        *guid*.  This removes the red "not delivered" exclamation mark in
        Messages.app.
        """
        self._execute(
            "UPDATE message SET is_sent = 1, error = 0 WHERE guid = ?",
            (guid,),
            description=f"mark_sent guid={guid}",
        )

    def mark_message_delivered(self, guid: str) -> None:
        """
        Mark an outgoing message as *delivered*.

        Sets ``is_delivered = 1``, ``is_sent = 1``, ``error = 0``, and stamps
        the ``date_delivered`` field with the current time.
        """
        now_ns = unix_to_apple_ns(time.time())
        self._execute(
            """
            UPDATE message
            SET    is_delivered   = 1,
                   is_sent        = 1,
                   error          = 0,
                   date_delivered = ?
            WHERE  guid = ?
            """,
            (now_ns, guid),
            description=f"mark_delivered guid={guid}",
        )

    def mark_message_read(self, guid: str) -> None:
        """
        Mark an outgoing message as *read* by the remote party.

        Sets ``is_read = 1`` and stamps ``date_read``.
        """
        now_ns = unix_to_apple_ns(time.time())
        self._execute(
            """
            UPDATE message
            SET    is_read  = 1,
                   date_read = ?
            WHERE  guid = ?
            """,
            (now_ns, guid),
            description=f"mark_read guid={guid}",
        )

    def mark_message_failed(self, guid: str, error_code: int = 4) -> None:
        """
        Mark an outgoing message as *failed* to send.

        Uses error code 4 (generic send failure) by default — the same value
        that Messages.app sets when SMS forwarding fails.
        """
        self._execute(
            "UPDATE message SET error = ?, is_sent = 0 WHERE guid = ?",
            (error_code, guid),
            description=f"mark_failed guid={guid} error={error_code}",
        )

    def insert_incoming_message(
        self,
        sender: str,
        body: str,
        service: str = "SMS",
        unix_timestamp: float | None = None,
    ) -> None:
        """
        Insert an incoming SMS/MMS into chat.db so it appears in Messages.app.

        Parameters
        ----------
        sender:
            The sender's phone number (e.g. ``"+12025551234"``).
        body:
            The message text.
        service:
            ``"SMS"`` or ``"MMS"``.
        unix_timestamp:
            When the message was received (Unix seconds).  Defaults to now.
        """
        if unix_timestamp is None:
            unix_timestamp = time.time()
        date_ns = unix_to_apple_ns(unix_timestamp)
        guid = self._make_guid()

        try:
            with self._connect() as conn:
                # Ensure the handle (sender) exists.
                handle_id = self._get_or_create_handle(conn, sender, service)
                # Ensure the chat exists.
                chat_id = self._get_or_create_chat(conn, handle_id, sender, service)
                # Insert the message row.
                conn.execute(
                    """
                    INSERT INTO message (
                        guid, text, handle_id, service, date,
                        is_from_me, is_read, is_delivered, is_sent, error
                    ) VALUES (?, ?, ?, ?, ?, 0, 0, 1, 1, 0)
                    """,
                    (guid, body, handle_id, service, date_ns),
                )
                # Link message to chat via chat_message_join.
                message_id = conn.execute(
                    "SELECT ROWID FROM message WHERE guid = ?", (guid,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO chat_message_join (chat_id, message_id) VALUES (?, ?)",
                    (chat_id, message_id),
                )
                conn.commit()
            log.info(
                "Injected incoming %s from %s (guid=%s)", service, sender, guid
            )
        except Exception:
            log.exception(
                "Failed to inject incoming message from %s into chat.db", sender
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Use WAL mode to reduce lock contention with Messages.app.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _execute(self, sql: str, params: tuple, description: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(sql, params)
                conn.commit()
            log.debug("DB update OK: %s", description)
        except Exception:
            log.exception("DB update failed: %s", description)

    @staticmethod
    def _get_or_create_handle(
        conn: sqlite3.Connection, address: str, service: str
    ) -> int:
        row = conn.execute(
            "SELECT ROWID FROM handle WHERE id = ? AND service = ?",
            (address, service),
        ).fetchone()
        if row:
            return row[0]
        conn.execute(
            "INSERT INTO handle (id, country, service, uncanonicalized_id) VALUES (?, '', ?, ?)",
            (address, service, address),
        )
        return conn.execute(
            "SELECT last_insert_rowid()"
        ).fetchone()[0]

    @staticmethod
    def _get_or_create_chat(
        conn: sqlite3.Connection, handle_id: int, address: str, service: str
    ) -> int:
        # chat_identifier for 1-to-1 SMS is simply the phone number.
        row = conn.execute(
            "SELECT ROWID FROM chat WHERE chat_identifier = ? AND service_name = ?",
            (address, service),
        ).fetchone()
        if row:
            return row[0]
        guid = f"SMS;-;{address}"
        conn.execute(
            """
            INSERT INTO chat (guid, style, state, account_id, chat_identifier, service_name, is_filtered)
            VALUES (?, 45, 3, '', ?, ?, 0)
            """,
            (guid, address, service),
        )
        chat_id: int = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # Link handle to chat.
        conn.execute(
            "INSERT OR IGNORE INTO chat_handle_join (chat_id, handle_id) VALUES (?, ?)",
            (chat_id, handle_id),
        )
        return chat_id

    @staticmethod
    def _make_guid() -> str:
        import uuid
        return str(uuid.uuid4()).upper()
