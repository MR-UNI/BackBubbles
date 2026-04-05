"""
BackBubbles Mac Helper — WebSocket bridge client.

Maintains a persistent WebSocket connection to the Android BackBubbles Bridge
app.  Outgoing messages are queued locally and flushed as soon as the
connection is (re)established.

Protocol (JSON over WebSocket)
------------------------------

Mac → Android::

    {
        "type": "send_sms",
        "message_id": "<guid>",
        "to": "+12025551234",
        "body": "Hello from Mac!",
        "service": "SMS"
    }

Android → Mac::

    {
        "type": "delivery_status",
        "message_id": "<guid>",
        "status": "sent" | "delivered" | "read" | "failed",
        "error_code": 4      // only when status == "failed"
    }

    {
        "type": "incoming_sms",
        "from": "+12025551234",
        "body": "Reply text",
        "service": "SMS",
        "timestamp": 1712345678.0   // Unix seconds (float)
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from typing import Callable

import websockets
import websockets.exceptions

from config import Config
from db_injector import ChatDBInjector

log = logging.getLogger(__name__)

# Type alias for the delivery-status / incoming-message callback.
IncomingCallback = Callable[[dict], None]


class BridgeClient:
    """
    Manages the WebSocket connection to the Android bridge.

    All public methods are thread-safe; the WebSocket I/O runs on a
    dedicated asyncio event loop in a background thread.

    Parameters
    ----------
    config:
        BackBubbles configuration.
    on_incoming:
        Optional callback invoked (in the asyncio thread) whenever a
        message arrives from the Android side.
    """

    def __init__(
        self,
        config: Config,
        on_incoming: IncomingCallback | None = None,
    ) -> None:
        self._config = config
        self._on_incoming = on_incoming
        self._send_queue: asyncio.Queue[dict] = None  # type: ignore[assignment]
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = asyncio.Event()
        self._ready = threading.Event()

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background asyncio loop and connect to the bridge."""
        self._thread = threading.Thread(
            target=self._run_loop, name="BridgeClientLoop", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10)

    def stop(self) -> None:
        """Gracefully stop the bridge client."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, message: dict) -> None:
        """
        Thread-safe: queue a message to be sent to the Android bridge.

        If the loop is not yet ready the method will block briefly until
        it is.
        """
        self._ready.wait(timeout=5)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._send_queue.put_nowait, message)

    # ------------------------------------------------------------------
    # Internal — asyncio thread
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._send_queue = asyncio.Queue(maxsize=self._config.send_queue_maxsize)
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()

    async def _main(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning(
                    "Bridge connection lost (%s). Reconnecting in %.1fs…",
                    exc,
                    self._config.reconnect_delay,
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._config.reconnect_delay,
                    )
                except asyncio.TimeoutError:
                    pass

    async def _connect_and_serve(self) -> None:
        uri = self._config.ws_uri
        log.info("Connecting to Android bridge at %s …", uri)
        async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
            log.info("Connected to Android bridge at %s", uri)
            send_task = asyncio.create_task(self._sender(ws))
            recv_task = asyncio.create_task(self._receiver(ws))
            stop_task = asyncio.create_task(self._stop_event.wait())
            done, pending = await asyncio.wait(
                [send_task, recv_task, stop_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            # Re-raise the first exception from completed tasks.
            for task in done:
                if task is stop_task:
                    return
                if task.exception():
                    raise task.exception()  # type: ignore[misc]

    async def _sender(self, ws) -> None:
        """Drain the send queue and push messages to the WebSocket."""
        while True:
            msg = await self._send_queue.get()
            payload = json.dumps(msg)
            await ws.send(payload)
            log.debug("Sent to bridge: %s", payload[:120])

    async def _receiver(self, ws) -> None:
        """Read messages from the bridge and dispatch them."""
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("Received non-JSON frame from bridge: %r", raw[:80])
                continue
            log.debug("Received from bridge: %s", str(data)[:120])
            if self._on_incoming:
                try:
                    self._on_incoming(data)
                except Exception:
                    log.exception("on_incoming callback raised")
