# BackBubbles

> **Send SMS/RCS messages from your Mac using your Android phone's SIM card — no Android app required.**

BackBubbles watches macOS Messages.app for outgoing green-bubble (non-iMessage)
messages and sends them through **Google Messages for Web** running in a
headless Chromium browser.  Incoming SMS/RCS messages are detected from the
same session and injected back into Messages.app so everything appears in one
place.

A **macOS menu bar app** (`gui.py`) provides a native interface for controlling
the relay, watching live activity, triggering pairing, and opening logs — all
from the 💬 / ⏸ icon in the menu bar.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│               macOS (Mac Helper)                 │
│                                                  │
│  Messages.app                                    │
│      │  (write outgoing green bubble)            │
│      ▼                                           │
│  chat.db  ──── SQLite poll ────► gui.py          │
│                                  (menu bar app)  │
│  db_injector.py ◄── delivery ───── │             │
│  (inject incoming / mark delivered)│             │
│                                    ▼             │
│                             gmessages_client.py  │
│                             (Playwright/Chromium) │
└─────────────────────────────────────────────────┘
                        │  HTTPS (internet)
                        ▼
              messages.google.com/web
                        │
                        │  Google's servers
                        ▼
              Your Android phone's
              SMS / RCS stack
```

### How it works

1. The Mac helper polls `~/Library/Messages/chat.db` for new outgoing
   green-bubble messages.
2. Each message is typed into a headless Chromium browser session running
   **Google Messages for Web** (`messages.google.com/web`).
3. Google's servers relay the message to your paired Android phone, which
   sends it via its native SMS/RCS stack.
4. The Mac helper polls the same GMWeb session for delivery confirmations
   and incoming messages, updating `chat.db` so Messages.app reflects them.

**Your phone does not need to be on the same Wi-Fi network** — it only needs
an internet connection (cellular or Wi-Fi).

---

## Requirements

### Mac side
- macOS Ventura (13) or later
- Python 3.11+
- **Full Disk Access** granted to Terminal (or your IDE) so the helper can
  read and write `~/Library/Messages/chat.db`
  → *System Settings → Privacy & Security → Full Disk Access*

### Android side
- An Android phone with **Google Messages** installed as the default SMS app
- The phone must be signed into a Google account and have internet access

> **No Android app to install.** BackBubbles uses the same pairing mechanism
> as the official Google Messages for Web feature already built into
> Google Messages.

---

## Setup

### 1 — Install the Mac Helper

```bash
cd mac-helper
bash install.sh
```

This will:
- Create a Python virtual environment in `mac-helper/.venv`
- Install Playwright, Chromium, and **rumps** (the menu bar library)
- Write a **LaunchAgent** plist to `~/Library/LaunchAgents/com.backbubbles.helper.plist`
- Load the agent so the menu bar app starts automatically at login

**Optional environment variables for `install.sh`:**

| Variable | Default | Description |
|----------|---------|-------------|
| `BB_GMESSAGES_PROFILE_DIR` | `~/.backbubbles/gmessages-profile` | Where the Chromium session is persisted |
| `BB_GMESSAGES_POLL_INTERVAL` | `3.0` | Seconds between inbox scans |
| `BB_POLL_INTERVAL` | `2.0` | Seconds between chat.db polls |

### 2 — Pair your phone (first-time setup)

After the menu bar app starts, a **⏸** (stopped) or **💬** (running) icon appears in the menu bar.

Click the icon and choose **🔗 Pair Phone…**.  A Chromium window opens and
navigates to the Google Messages for Web authentication page.  On your Android
phone:

1. Open **Google Messages**.
2. Tap the three-dot menu → **Device pairing** (or **Messages for web**).
3. Tap **QR code scanner** and scan the code on screen.

Once paired, the Chromium window closes, the session is saved to
`~/.backbubbles/gmessages-profile/`, and the icon turns **●** (running) in
the menu.  All future launches use this saved session headlessly — no QR
code needed.

You can also pair from the command line:

```bash
cd mac-helper
source .venv/bin/activate
python gui.py --pair
```

### 3 — Send a test message

1. Open **Messages.app** on your Mac.
2. Start a new conversation with a non-iMessage contact (green bubble).
3. Type a message and press **Return**.
4. BackBubbles detects the outgoing row in `chat.db`, types the message into
   GMWeb, and your Android phone sends it via SMS/RCS.
5. When delivery is confirmed, the Mac helper updates `chat.db` to mark the
   message as delivered.

### 4 — Incoming messages

When your Android phone receives an SMS or RCS message, it appears in
`messages.google.com/web`.  BackBubbles polls for new incoming bubbles and
inserts them into `chat.db` so Messages.app displays them as incoming
messages from that contact.

---

## Menu bar controls

| Item | Action |
|------|--------|
| ● Running / ⏸ Stopped | Current daemon status (not clickable) |
| ▶ Start | Start the relay (when stopped) |
| ⏹ Stop | Stop the relay (when running) |
| 🔗 Pair Phone… | Stop, open headed Chromium for QR scan, then resume |
| Recent Activity | Last 5 log lines from the daemon |
| Open Logs… | Reveal the log folder in Finder |
| Quit BackBubbles | Gracefully stop the relay and exit |

---

## Reconnecting / re-pairing

Google Messages for Web sessions can expire if:
- You sign into GMWeb in another browser (only one session is allowed)
- You sign out of Google on your phone
- The session cookie expires after a long period of inactivity

To re-pair, click **🔗 Pair Phone…** in the menu bar, or run:

```bash
cd mac-helper
source .venv/bin/activate
python gui.py --pair
```

After scanning the QR code the relay resumes automatically — no daemon restart needed.

---

## Troubleshooting

### "Conversation list did not appear — session may have expired"
Click **🔗 Pair Phone…** in the menu bar to re-authenticate.

### Messages show as "Not Delivered" on Mac
This is expected if SMS relay is turned off in iMessage settings (which is
recommended so Apple doesn't also try to send via a relay iPhone).
BackBubbles will still mark them as delivered once GMWeb confirms.

### Mac helper can't read chat.db
Grant **Full Disk Access** to Terminal (or the Python binary) in
*System Settings → Privacy & Security → Full Disk Access*.

### High memory usage
The headless Chromium browser uses approximately 200 MB of RAM.  This is
normal for a background browser session.

---

## Logs

```bash
# Mac helper logs
tail -f ~/Library/Logs/BackBubbles/backbubbles.log
tail -f ~/Library/Logs/BackBubbles/backbubbles.error.log

# Stop the daemon
launchctl unload ~/Library/LaunchAgents/com.backbubbles.helper.plist

# Restart the daemon
launchctl unload ~/Library/LaunchAgents/com.backbubbles.helper.plist
launchctl load  ~/Library/LaunchAgents/com.backbubbles.helper.plist
```

---

## Development

### Run Mac helper without installing
```bash
cd mac-helper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# Pair first (opens headed browser via menu or CLI flag):
python gui.py --pair

# Run the GUI (menu bar app, headless after pairing):
python gui.py

# Run headless only (no menu bar icon):
python main.py
```

### Run Mac helper tests
```bash
cd mac-helper
python3 -m pytest tests/ -v
# or without pytest:
python3 -m unittest tests/test_mac_helper.py -v
```

---

## Android fallback (deprecated)

The `android/` directory contains the original Android WebSocket bridge app.
It is **no longer required** and is kept only as a historical reference.
Using the GMWeb approach (this README) is strongly recommended.

---

## Caveats & Known Limitations

- **Google can change the GMWeb DOM** at any time, which may break the
  Playwright selectors in `gmessages_client.py`.  The selectors target
  Angular component element names and `aria-label` attributes, which tend to
  be more stable than class names.  Update the `_SEL_*` constants at the top
  of `gmessages_client.py` if Google changes the UI.
- **chat.db schema may change** with future macOS updates, breaking the
  injector.  Always back up your Messages database before enabling write-back.
- **iMessage messages** are handled entirely by Apple and are never touched by
  BackBubbles.
- **RCS** works automatically when both parties support it — no extra
  configuration required.  GMWeb sends RCS by default when available and
  falls back to SMS otherwise.
- **One active GMWeb session** — Google only allows one paired web session at
  a time.  Opening `messages.google.com` in Chrome while BackBubbles is
  running will disconnect the BackBubbles session.
- **Incoming message cursor resets on restart** — on the first poll after
  the daemon restarts, recently received messages may be re-reported to
  `chat.db`.  Duplicate rows are generally harmless (Messages.app deduplicates
  by phone number and timestamp).

---

## License

MIT — see [LICENSE](LICENSE).
