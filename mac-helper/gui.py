"""
BackBubbles — macOS menu bar GUI.

Run ``gui.py`` instead of ``main.py`` for a graphical menu bar interface.
The BackBubbles daemon (chat.db watcher + Google Messages for Web relay)
runs in a background thread while this process owns the macOS menu bar icon.

Usage
-----
    # Normal GUI launch (headless, already paired):
    python gui.py

    # Force pairing mode on startup (opens headed browser):
    python gui.py --pair

The "Pair Phone…" menu item can also trigger (re-)pairing at any time without
restarting the process.

Architecture
------------
    Main thread  → rumps event loop (AppKit NSRunLoop)
    BBEngine thread → BackBubbles.run() → chat.db polling + GMWeb relay
    GMessagesLoop thread → asyncio + Playwright (managed by GMessagesClient)

UI updates from the engine thread are enqueued into a thread-safe queue and
drained every 0.5 s by a rumps timer running on the main thread.
"""

from __future__ import annotations

import argparse
import collections
import logging
import queue
import subprocess
import sys
import threading
from pathlib import Path

import rumps

# Ensure mac-helper directory is on sys.path when invoked directly.
sys.path.insert(0, str(Path(__file__).parent))

from config import Config
from main import BackBubbles

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LOG_DIR = Path.home() / "Library" / "Logs" / "BackBubbles"
_MAX_RECENT = 5  # number of recent-activity lines shown in the menu

# Menu-bar title text for each daemon state.
_ICON_RUNNING = "💬"
_ICON_STOPPED = "⏸"
_ICON_PAIRING = "🔗"
_ICON_ERROR   = "⚠️"

# ---------------------------------------------------------------------------
# Logging → GUI bridge
# ---------------------------------------------------------------------------


class _GuiLogHandler(logging.Handler):
    """Forwards INFO+ log records to BackBubblesApp.push_event()."""

    def __init__(self, app: "BackBubblesApp") -> None:
        super().__init__(level=logging.INFO)
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._app.push_event(msg)
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Menu bar application
# ---------------------------------------------------------------------------


class BackBubblesApp(rumps.App):
    """macOS menu bar application wrapping the BackBubbles daemon."""

    def __init__(self, start_pair_mode: bool = False) -> None:
        super().__init__("BackBubbles", title=_ICON_STOPPED, quit_button=None)

        self._config = Config()
        self._engine: BackBubbles | None = None
        self._engine_thread: threading.Thread | None = None
        self._running = False
        self._deferred_pair = start_pair_mode
        self._started = False  # set to True on first _flush_ui tick

        # Thread-safe queue: background threads post callables; main thread runs them.
        self._ui_queue: queue.Queue = queue.Queue()

        # Most-recent activity lines (newest first).
        self._recent: collections.deque[str] = collections.deque(maxlen=_MAX_RECENT)

        # Attach log handler so daemon log lines appear in the activity list.
        _handler = _GuiLogHandler(self)
        _handler.setFormatter(logging.Formatter("%(message)s"))
        for name in ("backbubbles", "gmessages_client"):
            logging.getLogger(name).addHandler(_handler)

        # ----- Build menu items -----

        self._status_item = rumps.MenuItem("⏸  Stopped")

        self._start_item = rumps.MenuItem("▶  Start",       callback=self._cb_start)
        self._stop_item  = rumps.MenuItem("⏹  Stop",        callback=self._cb_stop)
        self._pair_item  = rumps.MenuItem("🔗  Pair Phone…", callback=self._cb_pair)

        # Activity display slots — non-interactive, updated by _flush_ui.
        self._activity_items = [rumps.MenuItem("") for _ in range(_MAX_RECENT)]
        self._activity_items[0].title = "  (no activity yet)"

        self.menu = [
            self._status_item,
            None,                                                        # separator
            self._start_item,
            self._stop_item,
            self._pair_item,
            None,
            rumps.MenuItem("Recent Activity"),                           # section label
            *self._activity_items,
            None,
            rumps.MenuItem("Open Logs…", callback=self._cb_open_logs),
            None,
            rumps.MenuItem("Quit BackBubbles", callback=self._cb_quit),
        ]

        # Stop is disabled until the engine is running.
        self._stop_item.set_callback(None)

    # ------------------------------------------------------------------
    # Main-thread timer: drains UI queue and auto-starts the engine
    # ------------------------------------------------------------------

    @rumps.timer(0.5)
    def _flush_ui(self, _sender):
        """Drain pending UI-update callables on the main thread (every 0.5 s)."""
        # Boot the engine on the very first tick so the AppKit run loop is ready.
        if not self._started:
            self._started = True
            self._start_engine(pair_mode=self._deferred_pair)

        try:
            while True:
                fn = self._ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass

    def _enqueue_ui(self, fn) -> None:
        """Thread-safe: schedule *fn* to run on the main thread."""
        self._ui_queue.put(fn)

    # ------------------------------------------------------------------
    # Engine lifecycle
    # ------------------------------------------------------------------

    def _start_engine(self, pair_mode: bool = False) -> None:
        """Launch the BackBubbles daemon in a background thread."""
        if self._running:
            return
        self._running = True
        self._update_status("pairing" if pair_mode else "starting")

        def _run() -> None:
            try:
                self._engine = BackBubbles(self._config, pair_mode=pair_mode)
                self._update_status("running")
                self._engine.run()   # blocks until stop() is called
            except Exception as exc:
                logging.getLogger("backbubbles").exception("Engine crashed")
                self._update_status("error", str(exc))
            finally:
                self._running = False
                self._update_status("stopped")

        self._engine_thread = threading.Thread(
            target=_run, name="BBEngine", daemon=True
        )
        self._engine_thread.start()

    def _stop_engine_async(self, then_pair: bool = False) -> None:
        """Stop the engine in a background thread; optionally re-pair afterward."""
        def _work() -> None:
            if self._engine:
                self._engine.stop()
            if self._engine_thread:
                self._engine_thread.join(timeout=10)
            if then_pair:
                self._start_engine(pair_mode=True)

        threading.Thread(target=_work, name="BBStop", daemon=True).start()

    # ------------------------------------------------------------------
    # Menu callbacks
    # ------------------------------------------------------------------

    def _cb_start(self, _):
        if not self._running:
            self._start_engine(pair_mode=False)

    def _cb_stop(self, _):
        if self._running:
            self._stop_engine_async()

    def _cb_pair(self, _):
        """Re-pair: stop → open headed browser for QR scan → resume headless."""
        if self._running:
            self._stop_engine_async(then_pair=True)
        else:
            self._start_engine(pair_mode=True)

    def _cb_open_logs(self, _):
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(_LOG_DIR)], check=False)

    def _cb_quit(self, _):
        """Gracefully stop the engine before exiting."""
        if self._engine and self._running:
            self._engine.stop()
        rumps.quit_application(self)

    # ------------------------------------------------------------------
    # Status & event helpers  (called from background threads)
    # ------------------------------------------------------------------

    def _update_status(self, state: str, detail: str = "") -> None:
        """Thread-safe: update the status label and menu bar icon."""
        _states = {
            "starting": ("🔄  Starting…",                _ICON_STOPPED),
            "pairing":  ("🔗  Pairing… (scan QR code)", _ICON_PAIRING),
            "running":  ("●  Running",                   _ICON_RUNNING),
            "stopped":  ("⏸  Stopped",                   _ICON_STOPPED),
            "error":    ("⚠️  Error",                    _ICON_ERROR),
        }
        label, icon = _states.get(state, ("?  Unknown", _ICON_STOPPED))
        is_running = state == "running"

        def _apply():
            self._status_item.title = label
            self.title = icon
            # Start is clickable only when stopped.
            self._start_item.set_callback(None if is_running else self._cb_start)
            # Stop is clickable only when running.
            self._stop_item.set_callback(self._cb_stop if is_running else None)

        self._enqueue_ui(_apply)

    def push_event(self, message: str) -> None:
        """Thread-safe: prepend a log line to the Recent Activity list."""
        short = message[:72] + ("…" if len(message) > 72 else "")
        self._recent.appendleft(short)
        snapshot = list(self._recent)

        def _apply():
            for i, item in enumerate(self._activity_items):
                item.title = f"  {snapshot[i]}" if i < len(snapshot) else ""

        self._enqueue_ui(_apply)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="BackBubbles — macOS menu bar SMS relay"
    )
    p.add_argument(
        "--pair",
        action="store_true",
        help=(
            "Open a headed browser window to (re-)pair with Google Messages. "
            "After pairing the daemon continues in headless mode."
        ),
    )
    return p.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = _parse_args()
    BackBubblesApp(start_pair_mode=args.pair).run()


if __name__ == "__main__":
    main()
