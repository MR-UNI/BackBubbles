"""
BackBubbles Mac Helper — Google Messages for Web client.

Replaces bridge_client.py / the Android WebSocket bridge entirely.

Uses Playwright to automate messages.google.com/web so that:

  * Outgoing messages are sent by typing them into the GMWeb compose box.
  * Delivery status is tracked by watching message status indicators.
  * Incoming messages are detected by polling the unread-badge list.

First-run pairing
-----------------
Run with ``pair_mode=True`` (or pass ``--pair`` to ``main.py``) to open a
**headed** Chromium window, navigate to the GMWeb authentication page, and
wait for the user to scan the QR code on their Android phone running Google
Messages.  Once paired, the browser profile (cookies + localStorage) is
persisted to ``config.gmessages_profile_dir`` so subsequent launches are
**headless** and already authenticated.

Drop-in interface
-----------------
``GMessagesClient`` exposes exactly the same public API as the old
``BridgeClient``::

    client = GMessagesClient(config, on_incoming=..., pair_mode=False)
    client.start()
    client.enqueue({"type": "send_sms", "message_id": "…", "to": "+1…", "body": "…"})
    client.stop()

Selector notes
--------------
Google Messages for Web is an Angular app.  The ``_SEL_*`` constants below
target component element names and ``aria-label`` attributes that tend to be
more stable than class names.  If Google changes the DOM, update the
constants and test with ``pair_mode=True`` (headed mode).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from config import Config

log = logging.getLogger(__name__)

# Type alias — same as bridge_client.py for drop-in compatibility.
IncomingCallback = Callable[[dict], None]

# ---------------------------------------------------------------------------
# Selectors
# Update these constants if Google changes the GMWeb DOM structure.
# ---------------------------------------------------------------------------

# Present once the app has fully loaded and is authenticated.
_SEL_CONV_LIST = "mws-conversations-list"

# Individual items in the left-hand conversation list.
_SEL_CONV_ITEM = "mws-conversation-list-item"

# Unread badge / dot inside a conversation list item.
_SEL_UNREAD_ITEM = (
    "mws-conversation-list-item.unread, "
    "mws-conversation-list-item [aria-label*='unread'], "
    "mws-conversation-list-item .unread-icon"
)

# The FAB that opens a new-conversation / recipient-search dialog.
_SEL_NEW_CONV_BTN = (
    "mws-new-conversation-button button, "
    "[aria-label='New conversation'], "
    "[aria-label='Start chat']"
)

# Recipient search input inside the new-conversation panel.
_SEL_RECIPIENT_INPUT = (
    "mws-contact-chips-input input, "
    "input[placeholder*='phone'], "
    "input[placeholder*='name or number']"
)

# Message compose text box.
_SEL_COMPOSE = (
    "mws-message-compose .input-box, "
    "mws-message-compose [contenteditable='true'], "
    "mws-message-compose textarea"
)

# Send-message button.
_SEL_SEND_BTN = (
    "mws-message-compose button.send-message-button, "
    "mws-message-compose [aria-label='Send message']"
)

# Status text / icon beneath the last sent message.
_SEL_MSG_STATUS = (
    "mws-message-read-status, "
    ".message-status, "
    "mws-conversation-message-status"
)

# All message bubbles in an open conversation.
_SEL_MSG_BUBBLE = "mws-message-part-content"

# Incoming-message containers (not sent by the local user).
_SEL_INCOMING_MSG = "mws-message-part.incoming, .message-wrapper.incoming"

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_URL_AUTH = "https://messages.google.com/web/authentication"
_URL_HOME = "https://messages.google.com/web/conversations"

# Marker file written inside the profile dir after a successful pairing.
_PAIRED_MARKER = ".backbubbles_paired"

# How long (ms) to wait for selectors before declaring failure.
_SHORT_TIMEOUT_MS = 5_000
_LONG_TIMEOUT_MS = 30_000
_PAIR_TIMEOUT_MS = 300_000  # 5 minutes to scan QR code


# ---------------------------------------------------------------------------
# GMessagesClient
# ---------------------------------------------------------------------------


class GMessagesClient:
    """
    Manages a headless (or headed) Google Messages for Web session.

    All public methods are thread-safe.  Playwright I/O runs on a dedicated
    asyncio event loop in a background thread, mirroring the pattern used by
    the old ``BridgeClient``.

    Parameters
    ----------
    config:
        BackBubbles configuration (see :class:`config.Config`).
    on_incoming:
        Optional callback invoked whenever a message arrives from the
        remote side (delivery status or incoming SMS).  The callback
        receives the same dict schema as the old Android bridge::

            {"type": "delivery_status", "message_id": "…", "status": "sent"}
            {"type": "incoming_sms", "from": "+1…", "body": "…", …}
    pair_mode:
        When *True*, force a headed (visible) browser window so the user can
        scan the GMWeb QR code.  After pairing the profile is persisted and
        future runs can be headless.
    """

    def __init__(
        self,
        config: Config,
        on_incoming: IncomingCallback | None = None,
        pair_mode: bool = False,
    ) -> None:
        self._config = config
        self._on_incoming = on_incoming
        self._pair_mode = pair_mode

        # Set in the asyncio thread.
        self._send_queue: Optional[asyncio.Queue[dict]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None

        # Signals that the asyncio loop is ready to accept enqueue() calls.
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

        # guid → {"to": phone, "sent_at": time}  — awaiting delivery updates.
        self._pending_deliveries: dict[str, dict] = {}
        # conv_url → count of incoming messages already reported.
        self._seen_incoming: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background asyncio loop and launch the browser."""
        self._thread = threading.Thread(
            target=self._run_loop, name="GMessagesLoop", daemon=True
        )
        self._thread.start()
        # Playwright startup is slower than a plain socket; allow 30 s.
        if not self._ready.wait(timeout=30):
            log.warning("GMessagesClient ready-event timed out after 30 s")

    def stop(self) -> None:
        """Gracefully stop the client and close the browser."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=15)

    def enqueue(self, message: dict) -> None:
        """
        Thread-safe: queue a message to be sent via GMWeb.

        Only ``{"type": "send_sms", …}`` messages are acted upon; others are
        logged and discarded.
        """
        self._ready.wait(timeout=10)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, message)

    # ------------------------------------------------------------------
    # Internal — asyncio thread bootstrap
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._send_queue = asyncio.Queue(maxsize=256)
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            log.exception("GMessagesClient main loop raised")
        finally:
            self._loop.close()

    # ------------------------------------------------------------------
    # Main coroutine
    # ------------------------------------------------------------------

    async def _main(self) -> None:
        profile_dir = str(self._config.gmessages_profile_dir)
        headless = not self._pair_mode and self._config.gmessages_headless

        # A realistic desktop user-agent avoids Google's "insecure browser"
        # warning that it shows when it detects a headless/automation agent.
        _USER_AGENT = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )

        async with async_playwright() as pw:
            ctx: BrowserContext = await pw.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=headless,
                user_agent=_USER_AGENT,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    # Suppress the automation-controlled banner and remove the
                    # navigator.webdriver flag that Google inspects.
                    "--disable-blink-features=AutomationControlled",
                    # Exclude the --enable-automation switch that Playwright
                    # adds by default (it triggers bot-detection).
                    "--disable-features=AutomationControlled",
                ],
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 900},
            )
            # Delete navigator.webdriver before any page script runs so that
            # Google's sign-in page cannot detect the Playwright session.
            await ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                // Restore a plausible plugins list (empty in headless Chromium).
                // Use named plugin objects that match a real Chrome installation.
                const makePlugin = (name, desc, filename) => {
                    const plugin = { name, description: desc, filename, length: 0 };
                    Object.setPrototypeOf(plugin, Plugin.prototype);
                    return plugin;
                };
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const list = [
                            makePlugin('Chrome PDF Plugin', 'Portable Document Format', 'internal-pdf-viewer'),
                            makePlugin('Chrome PDF Viewer', '', 'mhjfbmdgcfjbbpaeojofohoefgiehjai'),
                            makePlugin('Native Client', '', 'internal-nacl-plugin'),
                        ];
                        list.length = list.length;
                        Object.setPrototypeOf(list, PluginArray.prototype);
                        return list;
                    },
                });
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            """)
            page: Page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            # Pair if requested or if this is a fresh profile.
            if self._pair_mode or not self._profile_is_paired():
                await self._do_pair(page)

            # Load the main conversations view.
            log.info("Navigating to Google Messages for Web …")
            await page.goto(_URL_HOME, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector(_SEL_CONV_LIST, timeout=_LONG_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                log.error(
                    "Conversation list did not appear — session may have expired. "
                    "Re-run with --pair to re-authenticate."
                )
                await ctx.close()
                return

            log.info("Google Messages for Web is ready.")

            send_task = asyncio.create_task(self._sender(page))
            inbox_task = asyncio.create_task(self._inbox_poller(page))
            stop_task = asyncio.create_task(self._stop_event.wait())

            done, pending = await asyncio.wait(
                [send_task, inbox_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            await ctx.close()
            log.info("Browser closed.")

    # ------------------------------------------------------------------
    # Pairing
    # ------------------------------------------------------------------

    def _profile_is_paired(self) -> bool:
        marker = self._config.gmessages_profile_dir / _PAIRED_MARKER
        return marker.exists()

    def _mark_paired(self) -> None:
        marker = self._config.gmessages_profile_dir / _PAIRED_MARKER
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    async def _do_pair(self, page: Page) -> None:
        log.info(
            "Opening GMWeb authentication page — please scan the QR code in "
            "Google Messages on your Android phone (timeout: 5 min) …"
        )
        await page.goto(_URL_AUTH, wait_until="domcontentloaded")

        # Wait until the app fully redirects to the conversations view.  Google
        # routes through intermediate pages (e.g. accounts.google.com SetSID,
        # messages.google.com/web/u/0/postSignIn) before landing on the home
        # URL, so waiting only for "authentication" to leave the URL is not
        # sufficient — the selector wait that follows would time out while the
        # page is still mid-redirect.
        try:
            await page.wait_for_url(
                lambda url: "/conversations" in url,
                timeout=_PAIR_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            raise RuntimeError(
                "Pairing timed out after 5 minutes. "
                "Run again with --pair to retry."
            )

        # Confirm the conversation list is present before declaring success.
        await page.wait_for_selector(_SEL_CONV_LIST, timeout=_LONG_TIMEOUT_MS)
        self._mark_paired()
        log.info("Pairing successful — session persisted to %s", self._config.gmessages_profile_dir)

    # ------------------------------------------------------------------
    # Sender
    # ------------------------------------------------------------------

    async def _sender(self, page: Page) -> None:
        """Drain the send queue and relay each message via GMWeb."""
        while True:
            msg = await self._send_queue.get()
            if msg.get("type") != "send_sms":
                log.warning("GMessagesClient: ignoring unknown message type %r", msg.get("type"))
                continue
            guid = msg.get("message_id", "")
            to = msg.get("to", "")
            body = msg.get("body", "")
            try:
                await self._send_message(page, to=to, body=body, guid=guid)
            except Exception:
                log.exception("Failed to send message guid=%s to=%s", guid, to)
                self._emit({"type": "delivery_status", "message_id": guid, "status": "failed", "error_code": 4})

    async def _send_message(self, page: Page, to: str, body: str, guid: str) -> None:
        """
        Open (or create) a conversation with *to* and send *body*.

        Flow
        ----
        1. Click the "New conversation" FAB.
        2. Type the recipient's phone number; press Enter.
        3. Wait for the compose box to appear.
        4. Type *body* and press Enter (or click Send).
        5. Confirm the message bubble appears; emit delivery_status=sent.
        """
        log.info("Sending to %s (guid=%s) …", to, guid)

        # Ensure we're on the home page before starting.
        if _URL_HOME not in page.url:
            await page.goto(_URL_HOME, wait_until="domcontentloaded")
            await page.wait_for_selector(_SEL_CONV_LIST, timeout=_LONG_TIMEOUT_MS)

        # Click "New conversation".
        await page.locator(_SEL_NEW_CONV_BTN).first.click(timeout=_LONG_TIMEOUT_MS)

        # Type the recipient number.
        recipient_input = page.locator(_SEL_RECIPIENT_INPUT).first
        await recipient_input.wait_for(state="visible", timeout=_LONG_TIMEOUT_MS)
        await recipient_input.fill(to)
        await recipient_input.press("Enter")

        # Wait for the compose box to become active.
        compose = page.locator(_SEL_COMPOSE).first
        try:
            await compose.wait_for(state="visible", timeout=_LONG_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            raise RuntimeError(f"Compose box did not appear for recipient {to!r}")

        # Type the message body and send.
        await compose.click()
        await compose.fill(body)

        # Prefer the send button; fall back to Enter key.
        send_btn = page.locator(_SEL_SEND_BTN).first
        try:
            await send_btn.click(timeout=_SHORT_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            await compose.press("Enter")

        log.info("Message sent to %s (guid=%s)", to, guid)

        # Record for delivery tracking and emit optimistic "sent" status.
        self._pending_deliveries[guid] = {"to": to, "sent_at": time.time(), "conv_url": page.url}
        self._emit({"type": "delivery_status", "message_id": guid, "status": "sent"})

        # Attempt to detect delivery/read status from the DOM.
        await self._poll_delivery_status(page, guid)

    async def _poll_delivery_status(self, page: Page, guid: str) -> None:
        """
        Watch the current conversation's message status for up to 60 seconds.

        Maps the GMWeb status text to the bridge protocol values:
          "Sent"      → "sent"      (already emitted above; re-emits to confirm)
          "Delivered" → "delivered"
          "Read"      → "read"
        """
        deadline = time.monotonic() + 60.0
        last_status = "sent"

        while time.monotonic() < deadline:
            try:
                status_locator = page.locator(_SEL_MSG_STATUS).last
                visible = await status_locator.is_visible()
                if visible:
                    text = (await status_locator.inner_text()).strip().lower()
                    if "read" in text and last_status != "read":
                        last_status = "read"
                        self._pending_deliveries.pop(guid, None)
                        self._emit({"type": "delivery_status", "message_id": guid, "status": "read"})
                        break
                    elif "delivered" in text and last_status not in ("read", "delivered"):
                        last_status = "delivered"
                        self._emit({"type": "delivery_status", "message_id": guid, "status": "delivered"})
                    elif "failed" in text or "not delivered" in text:
                        self._pending_deliveries.pop(guid, None)
                        self._emit({"type": "delivery_status", "message_id": guid, "status": "failed", "error_code": 4})
                        break
            except Exception:
                pass  # DOM may be mid-update; retry

            await asyncio.sleep(2.0)

        self._pending_deliveries.pop(guid, None)

    # ------------------------------------------------------------------
    # Inbox poller (incoming messages)
    # ------------------------------------------------------------------

    async def _inbox_poller(self, page: Page) -> None:
        """
        Periodically scan the conversation list for unread messages.

        When an unread conversation is found:
          1. Open it.
          2. Count incoming message bubbles.
          3. Report any new ones via the ``on_incoming`` callback.
          4. Return to the home page.
        """
        interval = self._config.gmessages_poll_interval
        while True:
            await asyncio.sleep(interval)
            try:
                await self._check_inbox(page)
            except PlaywrightTimeoutError:
                log.debug("Inbox poll timed out — will retry next cycle")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # If the page/context has been closed, stop the poller so the
                # main task can tear down cleanly instead of spinning forever.
                if "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__:
                    raise
                log.exception("Unexpected error in inbox poller")

    async def _check_inbox(self, page: Page) -> None:
        """Scan unread conversation list items and report new incoming messages."""
        # Make sure we're on the home page.
        if _URL_HOME not in page.url:
            await page.goto(_URL_HOME, wait_until="domcontentloaded")
            await page.wait_for_selector(_SEL_CONV_LIST, timeout=_LONG_TIMEOUT_MS)

        unread_items = page.locator(_SEL_UNREAD_ITEM)
        count = await unread_items.count()
        if count == 0:
            return

        log.debug("Found %d unread conversation(s)", count)

        for i in range(count):
            item = unread_items.nth(i)
            try:
                # Derive a stable identifier from the item's aria-label or text.
                label = await item.get_attribute("aria-label") or ""
                conv_key = label or f"conv_{i}"

                await item.click(timeout=_SHORT_TIMEOUT_MS)
                await page.wait_for_selector(_SEL_MSG_BUBBLE, timeout=_LONG_TIMEOUT_MS)

                conv_url = page.url
                await self._scrape_incoming_messages(page, conv_url)

                # Navigate back to home for the next iteration.
                await page.goto(_URL_HOME, wait_until="domcontentloaded")
                await page.wait_for_selector(_SEL_CONV_LIST, timeout=_LONG_TIMEOUT_MS)

            except PlaywrightTimeoutError:
                log.debug("Timed out processing unread item %d; skipping", i)
                # Return home before continuing.
                if _URL_HOME not in page.url:
                    await page.goto(_URL_HOME, wait_until="domcontentloaded")

    async def _scrape_incoming_messages(self, page: Page, conv_url: str) -> None:
        """
        Extract and report incoming message bubbles not yet reported.

        Uses the total count of incoming bubbles seen in this conversation as
        a monotonically increasing cursor so each message is reported exactly
        once per session.  (The cursor resets to 0 on daemon restart, so the
        first run after a restart may re-report messages received while the
        daemon was offline — this is acceptable for a v1.)
        """
        # Derive the sender's phone from the page title or URL.
        sender = await self._extract_sender(page)

        incoming_bubbles = page.locator(_SEL_INCOMING_MSG + " " + _SEL_MSG_BUBBLE)
        total = await incoming_bubbles.count()
        already_seen = self._seen_incoming.get(conv_url, 0)

        if total <= already_seen:
            self._seen_incoming[conv_url] = total
            return

        for idx in range(already_seen, total):
            bubble = incoming_bubbles.nth(idx)
            try:
                body = (await bubble.inner_text()).strip()
                if not body:
                    continue
                log.info("Incoming SMS from %s: %r", sender, body[:60])
                self._emit(
                    {
                        "type": "incoming_sms",
                        "from": sender,
                        "body": body,
                        "service": "SMS",
                        "timestamp": time.time(),
                    }
                )
            except Exception:
                log.debug("Failed to extract bubble %d in %s", idx, conv_url)

        self._seen_incoming[conv_url] = total

    async def _extract_sender(self, page: Page) -> str:
        """
        Best-effort extraction of the remote party's phone number / name.

        Tries the conversation header first, falls back to the page title,
        then to a placeholder string.
        """
        for selector in [
            "mws-conversation-container .conversation-header-title",
            "[data-e2e-conversation-name]",
            "mws-conversation-info-header",
            ".contact-name",
        ]:
            try:
                el = page.locator(selector).first
                if await el.is_visible():
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                pass

        # Fall back to page <title>.
        title = await page.title()
        return title.replace("Messages", "").strip(" -|") or "unknown"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        """Call the on_incoming callback, logging any exception."""
        if self._on_incoming:
            try:
                self._on_incoming(event)
            except Exception:
                log.exception("on_incoming callback raised")
