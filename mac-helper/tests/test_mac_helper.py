"""
Unit tests for the BackBubbles Mac Helper.

These tests run entirely in-process; they do not require a real chat.db,
an Android device, or a network connection.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Ensure the package root is on sys.path when run directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from db_injector import ChatDBInjector, unix_to_apple_ns, apple_ns_to_unix
from chat_db_watcher import ChatDBWatcher, OutgoingMessage


# ---------------------------------------------------------------------------
# Helpers — create a minimal in-memory / temp-file chat.db schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS handle (
    ROWID                 INTEGER PRIMARY KEY AUTOINCREMENT,
    id                    TEXT     NOT NULL,
    country               TEXT     DEFAULT '',
    service               TEXT     DEFAULT '',
    uncanonicalized_id    TEXT     DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chat (
    ROWID                 INTEGER PRIMARY KEY AUTOINCREMENT,
    guid                  TEXT,
    style                 INTEGER  DEFAULT 45,
    state                 INTEGER  DEFAULT 3,
    account_id            TEXT     DEFAULT '',
    chat_identifier       TEXT,
    service_name          TEXT,
    is_filtered           INTEGER  DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_handle_join (
    chat_id    INTEGER,
    handle_id  INTEGER,
    PRIMARY KEY (chat_id, handle_id)
);
CREATE TABLE IF NOT EXISTS message (
    ROWID           INTEGER  PRIMARY KEY AUTOINCREMENT,
    guid            TEXT,
    text            TEXT,
    handle_id       INTEGER,
    service         TEXT,
    date            INTEGER  DEFAULT 0,
    is_from_me      INTEGER  DEFAULT 0,
    is_read         INTEGER  DEFAULT 0,
    is_delivered    INTEGER  DEFAULT 0,
    is_sent         INTEGER  DEFAULT 0,
    error           INTEGER  DEFAULT 0,
    date_delivered  INTEGER  DEFAULT 0,
    date_read       INTEGER  DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_message_join (
    chat_id    INTEGER,
    message_id INTEGER
);
"""


def _create_temp_db() -> Path:
    """Create a temporary file-backed SQLite DB with the chat.db schema."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


def _add_outgoing_sms(db_path: Path, guid: str, handle_id: int, text: str, service="SMS") -> int:
    """Insert a fake outgoing SMS row; return its ROWID."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO message (guid, text, handle_id, service, is_from_me) VALUES (?,?,?,?,1)",
        (guid, text, handle_id, service),
    )
    rowid = cur.lastrowid
    conn.commit()
    conn.close()
    return rowid


def _add_handle(db_path: Path, address: str, service: str = "SMS") -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO handle (id, service) VALUES (?,?)", (address, service)
    )
    handle_id = cur.lastrowid
    conn.commit()
    conn.close()
    return handle_id


# ===========================================================================
# Config tests
# ===========================================================================


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.android_port, 8765)
        self.assertAlmostEqual(cfg.poll_interval, 2.0)
        self.assertIn("SMS", cfg.sms_services)

    def test_validate_raises_when_host_missing(self):
        cfg = Config(android_host="")
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_validate_passes_with_host(self):
        cfg = Config(android_host="192.168.1.42")
        cfg.validate()  # should not raise

    def test_ws_uri(self):
        cfg = Config(android_host="10.0.0.5", android_port=9000)
        self.assertEqual(cfg.ws_uri, "ws://10.0.0.5:9000")

    def test_validate_bad_port(self):
        cfg = Config(android_host="1.2.3.4", android_port=0)
        with self.assertRaises(ValueError):
            cfg.validate()

    def test_validate_bad_poll_interval(self):
        cfg = Config(android_host="1.2.3.4", poll_interval=-1.0)
        with self.assertRaises(ValueError):
            cfg.validate()


# ===========================================================================
# Time-conversion helpers
# ===========================================================================


class TestTimeConversion(unittest.TestCase):
    def test_round_trip(self):
        now = time.time()
        self.assertAlmostEqual(apple_ns_to_unix(unix_to_apple_ns(now)), now, places=3)

    def test_known_value(self):
        # 2024-01-01 00:00:00 UTC = 1704067200 Unix
        unix = 1704067200.0
        apple_ns = unix_to_apple_ns(unix)
        self.assertEqual(apple_ns, (1704067200 - 978307200) * 1_000_000_000)


# ===========================================================================
# ChatDBWatcher tests
# ===========================================================================


class TestChatDBWatcher(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_temp_db()
        self.handle_id = _add_handle(self.db_path, "+12025551234")
        self.config = Config(
            android_host="127.0.0.1",
            chat_db_path=self.db_path,
            sms_services=["SMS", "MMS"],
        )

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_no_messages_initially(self):
        watcher = ChatDBWatcher(self.config)
        msgs = list(watcher.poll())
        self.assertEqual(msgs, [])

    def test_detects_new_outgoing_sms(self):
        watcher = ChatDBWatcher(self.config)
        _add_outgoing_sms(self.db_path, "guid-1", self.handle_id, "Hello!")
        msgs = list(watcher.poll())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].guid, "guid-1")
        self.assertEqual(msgs[0].handle, "+12025551234")
        self.assertEqual(msgs[0].body, "Hello!")
        self.assertEqual(msgs[0].service, "SMS")

    def test_does_not_return_same_message_twice(self):
        watcher = ChatDBWatcher(self.config)
        _add_outgoing_sms(self.db_path, "guid-2", self.handle_id, "Dup?")
        list(watcher.poll())         # first poll
        msgs = list(watcher.poll())  # second poll — should be empty
        self.assertEqual(msgs, [])

    def test_returns_only_new_messages(self):
        watcher = ChatDBWatcher(self.config)
        _add_outgoing_sms(self.db_path, "guid-3", self.handle_id, "First")
        list(watcher.poll())
        _add_outgoing_sms(self.db_path, "guid-4", self.handle_id, "Second")
        msgs = list(watcher.poll())
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].guid, "guid-4")

    def test_ignores_empty_body(self):
        watcher = ChatDBWatcher(self.config)
        _add_outgoing_sms(self.db_path, "guid-empty", self.handle_id, "   ")
        msgs = list(watcher.poll())
        self.assertEqual(msgs, [])

    def test_run_forever_stops_on_event(self):
        watcher = ChatDBWatcher(self.config)
        stop = threading.Event()
        received = []

        def _cb(msg):
            received.append(msg)

        # Add a message before starting.
        _add_outgoing_sms(self.db_path, "guid-rf", self.handle_id, "run_forever test")

        t = threading.Thread(target=watcher.run_forever, args=(_cb, stop))
        t.start()
        time.sleep(0.5)  # let at least one poll cycle complete
        stop.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].guid, "guid-rf")


# ===========================================================================
# ChatDBInjector tests
# ===========================================================================


class TestChatDBInjector(unittest.TestCase):
    def setUp(self):
        self.db_path = _create_temp_db()
        self.injector = ChatDBInjector(self.db_path)
        # Pre-insert a handle and a fake outgoing message.
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("INSERT INTO handle (id, service) VALUES ('+19995551234', 'SMS')")
        conn.execute(
            "INSERT INTO message (guid, text, handle_id, service, is_from_me, is_sent, error) "
            "VALUES ('test-guid-1', 'hi', 1, 'SMS', 1, 0, 0)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def _fetch_msg(self, guid: str) -> dict:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM message WHERE guid=?", (guid,)).fetchone()
        conn.close()
        return dict(row) if row else {}

    def test_mark_message_sent(self):
        self.injector.mark_message_sent("test-guid-1")
        row = self._fetch_msg("test-guid-1")
        self.assertEqual(row["is_sent"], 1)
        self.assertEqual(row["error"], 0)

    def test_mark_message_delivered(self):
        self.injector.mark_message_delivered("test-guid-1")
        row = self._fetch_msg("test-guid-1")
        self.assertEqual(row["is_delivered"], 1)
        self.assertEqual(row["is_sent"], 1)
        self.assertGreater(row["date_delivered"], 0)

    def test_mark_message_read(self):
        self.injector.mark_message_read("test-guid-1")
        row = self._fetch_msg("test-guid-1")
        self.assertEqual(row["is_read"], 1)
        self.assertGreater(row["date_read"], 0)

    def test_mark_message_failed(self):
        self.injector.mark_message_failed("test-guid-1")
        row = self._fetch_msg("test-guid-1")
        self.assertEqual(row["error"], 4)
        self.assertEqual(row["is_sent"], 0)

    def test_mark_message_failed_custom_code(self):
        self.injector.mark_message_failed("test-guid-1", error_code=7)
        row = self._fetch_msg("test-guid-1")
        self.assertEqual(row["error"], 7)

    def test_insert_incoming_message(self):
        self.injector.insert_incoming_message(
            sender="+12025551234",
            body="Incoming test",
            service="SMS",
            unix_timestamp=1704067200.0,
        )
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM message WHERE text='Incoming test'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["is_from_me"], 0)
        self.assertEqual(row["service"], "SMS")
        self.assertEqual(row["is_delivered"], 1)

    def test_insert_creates_handle_and_chat(self):
        self.injector.insert_incoming_message(
            sender="+13335551234",
            body="New sender",
        )
        conn = sqlite3.connect(str(self.db_path))
        handle = conn.execute(
            "SELECT * FROM handle WHERE id='+13335551234'"
        ).fetchone()
        chat = conn.execute(
            "SELECT * FROM chat WHERE chat_identifier='+13335551234'"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(handle)
        self.assertIsNotNone(chat)

    def test_insert_reuses_existing_handle(self):
        self.injector.insert_incoming_message(sender="+19995551234", body="msg1")
        self.injector.insert_incoming_message(sender="+19995551234", body="msg2")
        conn = sqlite3.connect(str(self.db_path))
        count = conn.execute(
            "SELECT COUNT(*) FROM handle WHERE id='+19995551234'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)


# ===========================================================================
# Main
# ===========================================================================

if __name__ == "__main__":
    unittest.main()
