# BackBubbles

> **Send SMS/RCS messages from your Mac using your Android phone's SIM card.**

BackBubbles is a relay bridge that watches macOS Messages.app for outgoing
green-bubble (non-iMessage) messages and forwards them to your Android phone
to be sent via its native SMS/RCS stack.  Incoming Android SMS/MMS messages
are injected back into Messages.app so everything appears in one place.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│               macOS (Mac Helper)                 │
│                                                  │
│  Messages.app                                    │
│      │  (write outgoing green bubble)            │
│      ▼                                           │
│  chat.db  ──── SQLite poll ────► main.py         │
│                                     │            │
│  db_injector.py ◄── delivery ───────┤            │
│  (inject incoming / mark delivered) │            │
│                                     ▼            │
│                             bridge_client.py     │
│                             (WebSocket client)   │
└─────────────────────────────────────────────────┘
                        │  WebSocket (LAN)
                        ▼
┌─────────────────────────────────────────────────┐
│             Android (BackBubbles Bridge)          │
│                                                  │
│  BridgeService.kt  (WebSocket client)            │
│      │                                           │
│      ├──► SmsSender.kt  ──► SmsManager (send)   │
│      │                          │                │
│      │                          └── PendingIntent│
│      │                              (sent/deliv) │
│      ◄── SmsReceiver.kt (incoming SMS)           │
└─────────────────────────────────────────────────┘
```

### Message protocol

All messages are JSON frames over a WebSocket connection.

| Direction | Type | Fields |
|-----------|------|--------|
| Mac → Android | `send_sms` | `message_id`, `to`, `body`, `service` |
| Android → Mac | `delivery_status` | `message_id`, `status` (`sent`/`delivered`/`read`/`failed`), `error_code` (on failure) |
| Android → Mac | `incoming_sms` | `from`, `body`, `service`, `timestamp` |

---

## Requirements

### Mac side
- macOS Ventura (13) or later
- Python 3.11+
- **Full Disk Access** granted to Terminal (or your IDE) so the helper can
  read and write `~/Library/Messages/chat.db`
  → *System Settings → Privacy & Security → Full Disk Access*

### Android side
- Android 8.0 (API 26) or later
- The Android device and Mac **must be on the same local network** (Wi-Fi)
- The Android device is set as the **default SMS app** (or at minimum has
  `SEND_SMS` and `RECEIVE_SMS` permissions granted)

---

## Setup

### 1 — Android Bridge app

1. Clone this repo and open the `android/` folder in **Android Studio**.
2. Build and install the app on your Android device:
   ```bash
   cd android
   ./gradlew installDebug
   ```
3. Open the **BackBubbles** app on your Android device.
4. Grant all requested permissions (SMS, notifications).
5. Note the **Device IP** shown on the main screen (e.g. `192.168.1.42`).
6. Tap **Start Bridge** — the service will wait for the Mac helper to connect.

> **First-time setup:** Go to *Settings → Apps → BackBubbles → Set as default*
> if you want BackBubbles to be the SMS handler, or grant SMS permissions
> manually in App Info.

### 2 — Mac Helper daemon

```bash
cd mac-helper
BB_ANDROID_HOST=192.168.1.42 bash install.sh
```

This will:
- Create a Python virtual environment in `mac-helper/.venv`
- Install dependencies (`websockets`)
- Write a **LaunchAgent** plist to `~/Library/LaunchAgents/com.backbubbles.helper.plist`
- Load the agent so it starts automatically at login

**Optional environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `BB_ANDROID_HOST` | *(required)* | IP address of your Android device |
| `BB_ANDROID_PORT` | `8765` | WebSocket port |
| `BB_POLL_INTERVAL` | `2.0` | Seconds between chat.db polls |
| `BB_RECONNECT_DELAY` | `5.0` | Seconds before reconnecting |

### 3 — Send a test message

1. Open **Messages.app** on your Mac.
2. Start a new conversation with a non-iMessage contact (green bubble).
3. Type a message and press **Return**.
4. BackBubbles will detect the outgoing row in `chat.db`, forward it to your
   Android phone, and the Android will send the SMS via `SmsManager`.
5. When Android confirms delivery, the Mac helper updates `chat.db` to mark
   the message as delivered (no red "not delivered" indicator).

### 4 — Incoming messages

When your Android phone receives an SMS, `SmsReceiver` forwards it to
`BridgeService`, which sends it to the Mac helper, which inserts a new row
into `chat.db`.  Messages.app will display it as an incoming message from
that contact.

---

## Troubleshooting

### "Configuration error: android_host is not set"
Export `BB_ANDROID_HOST` before running, or re-run `install.sh` with the
variable set.

### Messages show as "Not Delivered" on Mac
This is expected if SMS relay is turned off in iMessage settings (which is
recommended so Apple doesn't also try to send via a relay iPhone).
BackBubbles will still mark them as delivered once the Android confirms.

### Mac helper can't read chat.db
Grant **Full Disk Access** to Terminal (or the Python binary) in
*System Settings → Privacy & Security → Full Disk Access*.

### Android Bridge not receiving connections
- Make sure both devices are on the same Wi-Fi network.
- Check that the port (`8765` by default) is not blocked by a firewall.
- Verify the IP address shown in the BackBubbles app matches `BB_ANDROID_HOST`.

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
BB_ANDROID_HOST=192.168.1.42 python main.py
```

### Run Mac helper tests
```bash
cd mac-helper
python3 -m pytest tests/ -v
# or without pytest:
python3 -m unittest tests/test_mac_helper.py -v
```

### Build Android app
```bash
cd android
./gradlew assembleDebug
```

---

## Caveats & Known Limitations

- **No end-to-end encryption** — messages travel in plain-text JSON over your
  local network.  Do not expose the WebSocket port to the internet.
- **chat.db schema may change** with future macOS updates, breaking the
  injector.  Always back up your Messages database before enabling write-back.
- **iMessage messages** are handled entirely by Apple and are never touched by
  BackBubbles.
- **RCS** requires that Google Messages is the default SMS app on Android and
  that the recipient also supports RCS.  From macOS Messages.app, RCS messages
  appear as green bubbles just like SMS.
- **Delivery receipts** depend on carrier support for SMS delivery reports.

---

## License

MIT — see [LICENSE](LICENSE).
