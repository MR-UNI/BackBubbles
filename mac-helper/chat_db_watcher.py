"""
BackBubbles Mac Helper — chat.db watcher.

Polls ~/Library/Messages/chat.db for new outgoing, non-iMessage messages and
yields them as ``OutgoingMessage`` dataclass instances.

Apple stores messages in an SQLite WAL-mode database.  We keep track of the
highest ``ROWID`` we have already processed so each poll only surfaces new
rows.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from config import Config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutgoingMessage:
    """A non-iMessage outgoing message found in chat.db."""

    rowid: int
    guid: str
    handle: str          # destination phone number / address
    body: str
    service: str         # 'SMS' or 'MMS'
    date_ns: int         # Apple epoch nanoseconds (seconds since 2001-01-01)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_QUERY = """
SELECT
    m.ROWID         AS rowid,
    m.guid          AS guid,
    h.id            AS handle,
    m.text          AS body,
    m.service       AS service,
    m.date          AS date_ns
FROM
    message          m
    JOIN handle      h ON h.ROWID = m.handle_id
WHERE
    m.is_from_me = 1
    AND m.service IN ({placeholders})
    AND m.ROWID   > ?
ORDER BY
    m.ROWID ASC
"""

# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


class ChatDBWatcher:
    """
    Watches ``chat.db`` for new outgoing non-iMessage messages.

    Usage::

        watcher = ChatDBWatcher(config)
        for msg in watcher.poll():
            # handle msg ...
            time.sleep(config.poll_interval)

    The :meth:`poll` method is a *generator* that yields zero or more
    ``OutgoingMessage`` objects each time it is called/iterated.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._db_path = config.chat_db_path
        self._last_rowid: int = self._get_current_max_rowid()
        log.info("ChatDBWatcher ready; starting after ROWID=%d", self._last_rowid)

    # ------------------------------------------------------------------
    def poll(self) -> Iterator[OutgoingMessage]:
        """Yield any new outgoing SMS/MMS messages since the last call."""
        try:
            yield from self._query_new_messages()
        except sqlite3.OperationalError as exc:
            # Transient lock conflicts are expected while Messages.app writes.
            log.debug("Transient DB error (will retry): %s", exc)
        except Exception:
            log.exception("Unexpected error polling chat.db")

    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """Return a read-only URI connection to chat.db."""
        uri = self._db_path.as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_current_max_rowid(self) -> int:
        """Return the current highest message ROWID so we only watch *new* ones."""
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
                return row[0] if row and row[0] is not None else 0
        except Exception as exc:
            log.warning("Could not read initial ROWID from chat.db: %s", exc)
            return 0

    def _query_new_messages(self) -> Iterator[OutgoingMessage]:
        services = self._config.sms_services
        placeholders = ",".join("?" * len(services))
        query = _QUERY.format(placeholders=placeholders)
        # Build positional params: service values first, then last_rowid.
        positional_params = (*services, self._last_rowid)

        with self._connect() as conn:
            rows = conn.execute(query, positional_params).fetchall()

        for row in rows:
            body = row["body"] or ""
            if not body.strip():
                continue  # skip empty / attachment-only messages
            msg = OutgoingMessage(
                rowid=row["rowid"],
                guid=row["guid"],
                handle=row["handle"],
                body=body,
                service=row["service"] or "SMS",
                date_ns=row["date_ns"] or 0,
            )
            log.info(
                "Detected outgoing SMS ROWID=%d to=%s len=%d",
                msg.rowid, msg.handle, len(msg.body),
            )
            self._last_rowid = max(self._last_rowid, msg.rowid)
            yield msg

    # ------------------------------------------------------------------
    def run_forever(self, on_message, stop_event) -> None:
        """
        Blocking loop: polls every ``poll_interval`` seconds and calls
        *on_message* with each ``OutgoingMessage``.  Stops when
        *stop_event* is set.
        """
        while not stop_event.is_set():
            for msg in self.poll():
                try:
                    on_message(msg)
                except Exception:
                    log.exception("on_message callback raised")
            stop_event.wait(timeout=self._config.poll_interval)
