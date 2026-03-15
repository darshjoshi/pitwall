"""
Raw SignalR Core client for F1 Live Timing.

No dependency on the `signalrcore` library — we implement the SignalR Core
JSON Hub Protocol directly over WebSockets. This gives full control over
the connection lifecycle, message parsing, and reconnection logic.

Protocol reference (reverse-engineered):
  1. OPTIONS /signalrcore/negotiate → extract AWSALBCORS cookie
  2. WebSocket connect to wss://livetiming.formula1.com/signalrcore
  3. Send handshake: {"protocol":"json","version":1}\x1e
  4. Receive handshake response: {}\x1e
  5. Send Subscribe invocation with topic list
  6. Receive initial state as CompletionMessage (type=3)
  7. Receive live updates as Invocation messages (type=1, target="feed")
  8. Respond to Ping messages (type=6)
"""

import asyncio
import json
import logging
import ssl
import time
from collections.abc import Callable
from typing import Any, Optional

import certifi
import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from decompressor import decompress_z_data
from merger import StateStore
from topics import ALL_TOPICS, FREE_TOPICS, TOPICS

logger = logging.getLogger("f1livetiming")

# SignalR Core uses ASCII Record Separator (0x1E) as message terminator
RECORD_SEPARATOR = "\x1e"

# SignalR Core message types
MSG_INVOCATION = 1       # Server→Client method call (live data)
MSG_STREAM_ITEM = 2      # Stream item
MSG_COMPLETION = 3       # Response to an invocation (initial state)
MSG_STREAM_INVOCATION = 4
MSG_CANCEL_INVOCATION = 5
MSG_PING = 6             # Keep-alive
MSG_CLOSE = 7            # Server closing connection

# Connection constants
NEGOTIATE_URL = "https://livetiming.formula1.com/signalrcore/negotiate"
WEBSOCKET_URL = "wss://livetiming.formula1.com/signalrcore"
MAX_RECONNECT_DELAY = 60  # seconds
INITIAL_RECONNECT_DELAY = 1  # seconds


class F1LiveClient:
    """Real-time F1 live timing client using raw SignalR Core WebSocket.

    Supports two modes:
    - no_auth=True: Free, receives all non-gated topics (timing, weather,
      race control, etc.) — no telemetry or GPS positions
    - no_auth=False: Requires F1 TV subscription token, receives everything
      including CarData.z and Position.z

    Usage:
        client = F1LiveClient(no_auth=True)

        @client.on("TimingData")
        def on_timing(data, timestamp):
            print(f"Timing update: {data}")

        @client.on("RaceControlMessages")
        def on_rc(data, timestamp):
            print(f"Race control: {data}")

        await client.connect()

    The client automatically:
    - Deep-merges incremental deltas onto keyframe state
    - Decompresses .z topics (CarData.z, Position.z)
    - Reconnects with exponential backoff on disconnection
    - Responds to server Ping messages
    """

    def __init__(
        self,
        topics: Optional[list[str]] = None,
        no_auth: bool = True,
        auth_token: Optional[str] = None,
        auto_decompress: bool = True,
        auto_merge: bool = True,
    ):
        if topics is None:
            topics = FREE_TOPICS if no_auth else ALL_TOPICS

        self.topics = topics
        self.no_auth = no_auth
        self.auth_token = auth_token
        self.auto_decompress = auto_decompress
        self.auto_merge = auto_merge

        self._handlers: dict[str, list[Callable]] = {}
        self._wildcard_handlers: list[Callable] = []
        self._state = StateStore()
        self._ws = None
        self._running = False
        self._invocation_id = 0
        self._cookies = {}

    # --- Public API ---

    def on(self, topic: str):
        """Decorator to register a handler for a specific topic.

        The handler receives (data, timestamp) where data is already
        decompressed and merged if auto_decompress/auto_merge are True.
        """
        def decorator(func):
            self._handlers.setdefault(topic, []).append(func)
            return func
        return decorator

    def on_all(self, func: Callable):
        """Register a handler that receives ALL topic updates.

        Handler signature: func(topic: str, data: Any, timestamp: str)
        """
        self._wildcard_handlers.append(func)
        return func

    def get_state(self, topic: str) -> Any:
        """Get the current merged state for a topic."""
        return self._state.get(topic)

    async def connect(self):
        """Connect to F1 live timing and start receiving data.

        Automatically reconnects with exponential backoff on disconnection.
        Blocks until manually stopped or an unrecoverable error occurs.
        """
        self._running = True
        reconnect_delay = INITIAL_RECONNECT_DELAY

        while self._running:
            try:
                await self._negotiate()
                await self._connect_and_stream()
                reconnect_delay = INITIAL_RECONNECT_DELAY  # reset on clean run
            except (
                ConnectionClosed,
                ConnectionError,
                OSError,
            ) as e:
                if not self._running:
                    break
                logger.warning(
                    f"Connection lost: {e}. Reconnecting in {reconnect_delay}s..."
                )
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)
            except asyncio.CancelledError:
                break

        logger.info("Client stopped")

    async def stop(self):
        """Gracefully stop the client."""
        self._running = False
        if self._ws:
            await self._ws.close()

    # --- Protocol implementation ---

    def _ssl_context(self) -> ssl.SSLContext:
        """Create SSL context using certifi's CA bundle (fixes macOS Python)."""
        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx

    async def _negotiate(self):
        """Step 1: Pre-negotiate to get the AWSALBCORS load balancer cookie."""
        ssl_ctx = self._ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            headers = {}
            if not self.no_auth and self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"

            async with session.options(NEGOTIATE_URL, headers=headers) as resp:
                # Extract the AWSALBCORS cookie for load balancer affinity
                for cookie in resp.cookies.values():
                    if cookie.key == "AWSALBCORS":
                        self._cookies["AWSALBCORS"] = cookie.value
                        break

        logger.debug(f"Negotiated, cookies: {list(self._cookies.keys())}")

    async def _connect_and_stream(self):
        """Steps 2-8: WebSocket connect, handshake, subscribe, stream."""
        extra_headers = {}
        if self._cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
            extra_headers["Cookie"] = cookie_str

        if not self.no_auth and self.auth_token:
            extra_headers["Authorization"] = f"Bearer {self.auth_token}"

        ssl_ctx = self._ssl_context()
        async with websockets.connect(
            WEBSOCKET_URL,
            additional_headers=extra_headers,
            ping_interval=None,  # we handle pings via the protocol
            max_size=2**24,      # 16MB — some keyframes are large
            ssl=ssl_ctx,
        ) as ws:
            self._ws = ws

            # Step 3: SignalR Core handshake
            await self._send_handshake(ws)

            # Step 5: Subscribe to topics
            await self._subscribe(ws)

            # Step 7: Stream messages
            await self._receive_loop(ws)

    async def _send_handshake(self, ws):
        """Send and validate the SignalR Core JSON Hub Protocol handshake."""
        handshake = json.dumps({"protocol": "json", "version": 1})
        await ws.send(handshake + RECORD_SEPARATOR)

        response = await ws.recv()
        messages = response.split(RECORD_SEPARATOR)

        for msg_str in messages:
            if not msg_str.strip():
                continue
            msg = json.loads(msg_str)
            if "error" in msg:
                raise ConnectionError(f"Handshake failed: {msg['error']}")

        logger.info("SignalR Core handshake complete")

    async def _subscribe(self, ws):
        """Send Subscribe invocation with our topic list."""
        self._invocation_id += 1
        subscribe_msg = json.dumps({
            "type": MSG_INVOCATION,
            "invocationId": str(self._invocation_id),
            "target": "Subscribe",
            "arguments": [self.topics],
        })
        await ws.send(subscribe_msg + RECORD_SEPARATOR)
        logger.info(f"Subscribed to {len(self.topics)} topics")

    async def _receive_loop(self, ws):
        """Main message processing loop."""
        async for raw_message in ws:
            if not self._running:
                break

            # SignalR Core can batch multiple messages separated by \x1e
            parts = raw_message.split(RECORD_SEPARATOR)
            for part in parts:
                if not part.strip():
                    continue
                try:
                    msg = json.loads(part)
                except json.JSONDecodeError:
                    logger.debug(f"Skipping non-JSON message: {part[:100]}")
                    continue

                await self._handle_message(msg, ws)

    async def _handle_message(self, msg: dict, ws):
        """Route a parsed SignalR Core message by type."""
        msg_type = msg.get("type")

        if msg_type == MSG_PING:
            # Respond to keep-alive pings
            pong = json.dumps({"type": MSG_PING})
            await ws.send(pong + RECORD_SEPARATOR)

        elif msg_type == MSG_COMPLETION:
            # Initial state from Subscribe response
            result = msg.get("result", {})
            if isinstance(result, dict):
                for topic, data in result.items():
                    self._process_keyframe(topic, data)

        elif msg_type == MSG_INVOCATION:
            target = msg.get("target", "")
            args = msg.get("arguments", [])

            if target == "feed" and len(args) >= 2:
                topic = args[0]
                data = args[1]
                timestamp = args[2] if len(args) > 2 else ""
                self._process_update(topic, data, timestamp)

        elif msg_type == MSG_CLOSE:
            error = msg.get("error", "Server closed connection")
            logger.warning(f"Server close: {error}")
            raise ConnectionError(error)

    def _process_keyframe(self, topic: str, raw_data: Any):
        """Process initial keyframe data from Subscribe response."""
        if isinstance(raw_data, str):
            try:
                raw_data = json.loads(raw_data)
            except (json.JSONDecodeError, TypeError):
                pass

        data = self._maybe_decompress(topic, raw_data)
        self._state.set_keyframe(topic, data)
        self._dispatch(topic, data, "")
        logger.debug(f"Keyframe set for {topic}")

    def _process_update(self, topic: str, raw_data: Any, timestamp: str):
        """Process a live delta update from the feed."""
        data = self._maybe_decompress(topic, raw_data)

        if self.auto_merge:
            data = self._state.apply_delta(topic, data)

        self._dispatch(topic, data, timestamp)

    def _maybe_decompress(self, topic: str, data: Any) -> Any:
        """Decompress .z topic data if auto_decompress is enabled."""
        if not self.auto_decompress:
            return data
        topic_meta = TOPICS.get(topic, {})
        if topic_meta.get("compressed") and isinstance(data, str):
            try:
                return decompress_z_data(data)
            except Exception as e:
                logger.warning(f"Decompression failed for {topic}: {e}")
        return data

    def _dispatch(self, topic: str, data: Any, timestamp: str):
        """Dispatch data to registered handlers."""
        for handler in self._handlers.get(topic, []):
            try:
                handler(data, timestamp)
            except Exception:
                logger.exception(f"Handler error for {topic}")

        for handler in self._wildcard_handlers:
            try:
                handler(topic, data, timestamp)
            except Exception:
                logger.exception(f"Wildcard handler error for {topic}")
