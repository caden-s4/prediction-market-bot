"""
data.markets.kalshi_ws -- Kalshi WebSocket orderbook client.

Maintains a persistent WebSocket connection to the Kalshi streaming API,
receiving orderbook snapshots and deltas to keep an in-memory book for
each subscribed market.  All public methods are thread-safe: the WS
receive loop runs in a daemon thread; the main bot thread reads via
get_book() / get_book_age().

Channel reference (Kalshi WS v2):
  - orderbook_delta  -> orderbook_snapshot (initial) + orderbook_delta (incremental)
  - ticker           -> ticker messages with yes_bid/yes_ask dollars

Auth: same RSA-PSS/SHA-256 signing as the REST API.
URL : wss://api.elections.kalshi.com/trade-api/ws/v2
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .base import OrderBook, PriceLevel
from monitoring.gate_events import log_gate_event

logger = logging.getLogger(__name__)


def _validate_kalshi_mid(mid: float, ticker: str, source: str) -> bool:
    """Return True if mid is in [0.0, 1.0], False and log on anomaly."""
    if mid is None:
        return False
    if not (0.0 <= mid <= 1.0):
        log_gate_event(
            ticker=ticker,
            gate="invariant_violation",
            decision="kalshi_mid_out_of_range",
            extra={"mid": mid, "source": source},
        )
        return False
    return True

# ── Ticker cache entry ───────────────────────────────────────────────────────

@dataclass
class TickerSnapshot:
    """Latest ticker data for a single market from the ticker channel."""
    market_id: str
    yes_bid: Optional[float] = None   # decimal [0-1]
    yes_ask: Optional[float] = None   # decimal [0-1]
    last_updated: float = 0.0         # time.time()


# ── Book cache entry ─────────────────────────────────────────────────────────

@dataclass
class _BookEntry:
    """Internal wrapper around an OrderBook with a timestamp."""
    book: OrderBook
    last_updated: float = field(default_factory=time.time)


# ── WS command envelope ──────────────────────────────────────────────────────

@dataclass
class _WsCommand:
    """Queued command to send over the WebSocket from the main thread."""
    action: str         # "subscribe" | "unsubscribe"
    channel: str        # "orderbook_delta" | "ticker"
    tickers: List[str]


# ── Main class ───────────────────────────────────────────────────────────────

class KalshiWebSocket:
    """
    Realtime Kalshi orderbook client.

    Usage::

        ws = KalshiWebSocket(api_key, api_secret)
        ws.start()
        ws.subscribe(["KXNASDAQ100U-26APR05-T24399.99"])
        ...
        book = ws.get_book("KXNASDAQ100U-26APR05-T24399.99")
    """

    # Reconnect back-off parameters.
    _BACKOFF_BASE = 1.0
    _BACKOFF_MAX = 30.0

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2",
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")

        # Thread-safe caches — written by the WS thread, read by the main thread.
        self._lock = threading.Lock()
        self._books: Dict[str, _BookEntry] = {}
        self._tickers: Dict[str, TickerSnapshot] = {}

        # Subscription state.  Server-authoritative: only mutated on confirmed acks.
        self._subscribed: Set[str] = set()
        # Tickers for which a subscribe frame has been sent but no ok ack has
        # arrived yet — prevents duplicate subscribes during the ack window.
        self._subscribing_in_flight: Set[str] = set()
        # Kalshi v2 unsubscribe requires params.sids (array); track ticker -> sid
        # from ok/subscribed acks so we can address the right server-side sub.
        self._ticker_to_sid: Dict[str, Any] = {}
        self._sid_to_tickers: Dict[Any, Set[str]] = {}
        self._sub_lock = threading.Lock()

        # Command queue (main thread -> WS thread).
        self._cmd_queue: queue.Queue[_WsCommand] = queue.Queue()

        # Background thread / event loop handles.
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._started = False

        # RSA private key (loaded lazily on first use).
        self._private_key: Any = None

        # Monotonically increasing message ID counter (thread-safe).
        self._msg_id: int = 0
        self._id_lock = threading.Lock()

    # ── Public API ───────────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the background WS thread.  Safe to call multiple times."""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._run_loop, name="kalshi-ws", daemon=True
        )
        self._thread.start()
        logger.info("KalshiWebSocket background thread started")

    def subscribe(self, market_tickers: list[str]) -> None:
        """Subscribe to the orderbook_delta channel for the given tickers."""
        new_tickers: list[str] = []
        with self._sub_lock:
            for t in market_tickers:
                if t in self._subscribed or t in self._subscribing_in_flight:
                    continue
                # In-flight only; _subscribed is populated by the ok ack handler.
                self._subscribing_in_flight.add(t)
                new_tickers.append(t)
        if not new_tickers:
            return
        self._cmd_queue.put(
            _WsCommand("subscribe", "orderbook_delta", new_tickers)
        )
        logger.info(
            "Queued subscribe for %d tickers (confirmed=%d in_flight=%d)",
            len(new_tickers), len(self._subscribed), len(self._subscribing_in_flight),
        )

    def unsubscribe(self, market_tickers: list[str]) -> None:
        """Queue an unsubscribe for confirmed tickers.

        _subscribed and the book caches are NOT mutated here — they are cleared
        only when the server acks the unsubscribe (audit §8.2). If the server
        rejects the unsubscribe, state is preserved so the next sync retries.
        """
        to_send: list[str] = []
        with self._sub_lock:
            for t in market_tickers:
                # Only unsubscribe tickers the server has confirmed for us.
                if t in self._subscribed:
                    to_send.append(t)
        if not to_send:
            return
        self._cmd_queue.put(
            _WsCommand("unsubscribe", "orderbook_delta", to_send)
        )
        logger.info("Queued unsubscribe for %d tickers", len(to_send))

    def sync_subscriptions(self, target_tickers: list[str]) -> None:
        """Bring WS subscriptions in sync with *target_tickers*.

        Subscribes to any ticker absent from the current set and unsubscribes
        any ticker present in the current set but absent from *target_tickers*.
        An empty *target_tickers* list unsubscribes everything currently held.
        """
        target = set(target_tickers)
        with self._sub_lock:
            confirmed = set(self._subscribed)
            in_flight = set(self._subscribing_in_flight)
        # Skip subscribe for anything already confirmed OR awaiting an ack.
        new_subs = target - confirmed - in_flight
        # Only unsubscribe tickers the server has confirmed; in-flight ones will
        # be reconciled on the next cycle once their ack arrives.
        to_remove = confirmed - target
        if new_subs:
            self.subscribe(list(new_subs))
        if to_remove:
            self.unsubscribe(list(to_remove))
        logger.info(
            "KalshiWebSocket: sync_subscriptions added=%d removed=%d total=%d",
            len(new_subs), len(to_remove), len(target),
        )

    def get_book(self, market_id: str) -> Optional[OrderBook]:
        """Return the latest in-memory OrderBook, or None if unavailable."""
        with self._lock:
            entry = self._books.get(market_id)
            return entry.book if entry else None

    def get_book_age(self, market_id: str) -> Optional[float]:
        """Seconds since the last update for *market_id*, or None if no data."""
        with self._lock:
            entry = self._books.get(market_id)
            if entry is None:
                return None
            return time.time() - entry.last_updated

    def get_ticker(self, market_id: str) -> Optional[TickerSnapshot]:
        """Return the latest ticker snapshot, or None."""
        with self._lock:
            return self._tickers.get(market_id)

    @property
    def connected(self) -> bool:
        """True if the background loop is alive (not necessarily connected)."""
        return self._thread is not None and self._thread.is_alive()

    def _next_id(self) -> int:
        """Return the next unique message ID (thread-safe)."""
        with self._id_lock:
            self._msg_id += 1
            return self._msg_id

    # ── RSA signing (mirrors KalshiClient._sign) ────────────────────────────

    def _load_private_key(self) -> Any:
        """Load the RSA private key from *self._api_secret*.

        Accepts either:
        - A PEM-encoded key string (with literal ``\\n`` or real newlines).
        - A bare base64 body (no PEM headers); PKCS#8 headers are added.
        """
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        secret = self._api_secret
        secret = secret.replace("\\n", "\n")

        if "BEGIN" in secret:
            pem_bytes = secret.encode("utf-8")
        else:
            body = "\n".join(secret[i:i + 64] for i in range(0, len(secret), 64))
            pem_bytes = (
                "-----BEGIN PRIVATE KEY-----\n"
                + body
                + "\n-----END PRIVATE KEY-----\n"
            ).encode("utf-8")

        return load_pem_private_key(pem_bytes, password=None)

    def _get_private_key(self) -> Any:
        if self._private_key is None:
            self._private_key = self._load_private_key()
        return self._private_key

    def _sign_ws(self) -> Dict[str, str]:
        """Generate auth headers for the WebSocket handshake.

        Message format: ``timestamp_ms + "GET" + "/trade-api/ws/v2"``
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        pk = self._get_private_key()
        ts_ms = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        message = (ts_ms + "GET" + path).encode("utf-8")
        signature = pk.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }

    # ── Background event loop ────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Entry point for the daemon thread — runs an asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_loop())
        except Exception:
            logger.exception("KalshiWebSocket event loop crashed")
        finally:
            self._loop.close()

    async def _ws_loop(self) -> None:
        """Outer loop: connect, run, reconnect with exponential back-off."""
        import websockets
        import websockets.exceptions

        backoff = self._BACKOFF_BASE
        while True:
            try:
                headers = self._sign_ws()
                logger.info("Connecting to Kalshi WS at %s", self._base_url)
                async with websockets.connect(
                    self._base_url,
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("Kalshi WS connected")
                    backoff = self._BACKOFF_BASE  # reset on success

                    # Re-subscribe to all tickers after reconnect.
                    await self._resubscribe_all(ws)

                    # Run the receive + command pump loop.
                    await self._run_connection(ws)

            except websockets.exceptions.InvalidStatusCode as exc:
                logger.warning(
                    "Kalshi WS rejected connection (HTTP %s), retrying in %.0fs",
                    exc.status_code, backoff,
                )
            except (
                OSError,
                websockets.exceptions.ConnectionClosed,
                websockets.exceptions.WebSocketException,
            ) as exc:
                logger.warning(
                    "Kalshi WS connection lost (%s), retrying in %.0fs",
                    type(exc).__name__, backoff,
                )
            except Exception:
                logger.exception(
                    "Unexpected error in WS loop, retrying in %.0fs", backoff,
                )

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._BACKOFF_MAX)

    async def _resubscribe_all(self, ws: Any) -> None:
        """Re-subscribe to all tracked markets after reconnect, in 100-ticker chunks."""
        with self._sub_lock:
            # On reconnect server state is gone — old sids are stale. Move all
            # confirmed tickers back to in-flight; they'll be re-confirmed by
            # new ok acks under fresh sids.
            tickers = list(self._subscribed | self._subscribing_in_flight)
            self._subscribed.clear()
            self._subscribing_in_flight.update(tickers)
            self._ticker_to_sid.clear()
            self._sid_to_tickers.clear()
        if not tickers:
            return
        for i in range(0, len(tickers), 100):
            await self._send_subscribe(ws, tickers[i:i + 100])
            await asyncio.sleep(0.05)
        logger.info("Re-subscribed to %d tickers after reconnect", len(tickers))

    async def _run_connection(self, ws: Any) -> None:
        """Pump incoming messages and outgoing commands concurrently."""
        recv_task = asyncio.ensure_future(self._recv_loop(ws))
        cmd_task = asyncio.ensure_future(self._cmd_pump(ws))
        done, pending = await asyncio.wait(
            [recv_task, cmd_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        # Propagate any exception from the completed task.
        for t in done:
            t.result()

    async def _recv_loop(self, ws: Any) -> None:
        """Receive and dispatch messages from the WebSocket."""
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Kalshi WS: non-JSON message: %.200s", raw)
                continue
            msg_type = data.get("type")
            logger.debug(
                "WS recv type=%s market=%s",
                msg_type,
                data.get("msg", {}).get("market_ticker", data.get("market_ticker", "?")),
            )
            self._process_message(data)

    async def _cmd_pump(self, ws: Any) -> None:
        """Drain the command queue and send subscribe/unsubscribe frames."""
        while True:
            # Yield to the recv loop, then drain all pending commands.
            await asyncio.sleep(0.05)
            while True:
                try:
                    cmd: _WsCommand = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break

                if cmd.action == "subscribe":
                    for i in range(0, len(cmd.tickers), 100):
                        await self._send_subscribe(ws, cmd.tickers[i:i + 100])
                        await asyncio.sleep(0.05)
                elif cmd.action == "unsubscribe":
                    # Batch unsubscribes 100/frame to mirror subscribe batching
                    # (audit §8.4); v2 sids payload supports a list per frame.
                    for i in range(0, len(cmd.tickers), 100):
                        await self._send_unsubscribe(ws, cmd.tickers[i:i + 100])
                        await asyncio.sleep(0.05)

    # ── WS frame helpers ─────────────────────────────────────────────────────

    async def _send_subscribe(self, ws: Any, tickers: List[str]) -> None:
        """Send one subscribe frame for *tickers* (up to 100) on orderbook_delta."""
        msg = {
            "id": self._next_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }
        await ws.send(json.dumps(msg))
        logger.debug("WS subscribe sent: %d tickers", len(tickers))

    async def _send_unsubscribe(self, ws: Any, tickers: List[str]) -> None:
        """Send one unsubscribe frame for a chunk of *tickers* via Kalshi v2 sids.

        v2 requires ``params.sids`` (array of subscription IDs), not
        ``market_ticker``. Tickers without a known sid are skipped — the next
        sync cycle will retry them once an ack maps the sid.
        """
        sids: List[Any] = []
        seen_sids: set = set()
        with self._sub_lock:
            for t in tickers:
                sid = self._ticker_to_sid.get(t)
                if sid is None:
                    logger.debug(
                        "WS unsubscribe skipped: no sid known for ticker %s", t,
                    )
                    continue
                # De-dupe: if the server bound several tickers to one sid,
                # send the sid once.
                if sid in seen_sids:
                    continue
                seen_sids.add(sid)
                sids.append(sid)
        if not sids:
            return
        msg = {
            "id": self._next_id(),
            "cmd": "unsubscribe",
            "params": {
                "sids": sids,
            },
        }
        await ws.send(json.dumps(msg))
        logger.debug("WS unsubscribe sent: %d sids", len(sids))

    # ── Message processing ───────────────────────────────────────────────────

    def _process_message(self, data: dict) -> None:
        """Route an incoming WS message to the appropriate handler."""
        msg_type = data.get("type")
        if msg_type == "orderbook_snapshot":
            self._handle_snapshot(data)
        elif msg_type == "orderbook_delta":
            self._handle_delta(data)
        elif msg_type == "ticker":
            self._handle_ticker(data)
        elif msg_type == "error":
            body = data.get("msg") if isinstance(data.get("msg"), dict) else {}
            code = body.get("code", data.get("code", "?"))
            message = body.get("msg", body.get("message", data.get("message", "")))
            logger.warning("Kalshi WS error (code=%s): %s", code, message)
        elif msg_type == "ok":
            # Server ack for each subscribe command. Extract sid + ticker(s) and
            # move them from in-flight to confirmed; cache the sid for later
            # unsubscribe (audit §A).
            self._handle_sub_ack(data, source="ok")
        elif msg_type == "subscribed":
            # Session-level confirmation; same parse path as ok ack so any
            # ticker info present is captured.
            self._handle_sub_ack(data, source="subscribed")
        elif msg_type == "unsubscribed":
            self._handle_unsub_ack(data)
        else:
            logger.warning("WS unknown msg type=%s raw: %.200s", msg_type, data)

    def _handle_sub_ack(self, data: dict, source: str) -> None:
        """Process a subscribe-side ack (``ok`` or ``subscribed``).

        Confirms in-flight tickers and records the ticker->sid mapping that
        unsubscribes need (audit §A, §8.2). Only tickers currently in
        ``_subscribing_in_flight`` are confirmed — defensive against cumulative
        ack semantics where the server may echo previously-subscribed tickers.
        """
        msg_body = data.get("msg") if isinstance(data.get("msg"), dict) else {}
        # sid may live at top level (ok acks observed in current code path) or
        # nested under msg (subscribed acks). Try both.
        sid = data.get("sid")
        if sid is None:
            sid = msg_body.get("sid")
        tickers_in_ack: List[str] = []
        raw_list = msg_body.get("market_tickers")
        if isinstance(raw_list, list):
            tickers_in_ack = [t for t in raw_list if isinstance(t, str)]
        else:
            single = msg_body.get("market_ticker")
            if isinstance(single, str) and single:
                tickers_in_ack = [single]

        confirmed_now: List[str] = []
        if sid is not None and tickers_in_ack:
            with self._sub_lock:
                for t in tickers_in_ack:
                    if t in self._subscribing_in_flight:
                        self._subscribing_in_flight.discard(t)
                        self._subscribed.add(t)
                        self._ticker_to_sid[t] = sid
                        self._sid_to_tickers.setdefault(sid, set()).add(t)
                        confirmed_now.append(t)

        if source == "subscribed":
            channel = msg_body.get("channel", "?")
            logger.info(
                "WS session subscribed: channel=%s sid=%s tickers=%d confirmed=%d",
                channel, sid if sid is not None else "?",
                len(tickers_in_ack), len(confirmed_now),
            )
        else:
            logger.debug(
                "WS ok id=%s sid=%s seq=%s tickers=%d confirmed=%d",
                data.get("id"), sid, data.get("seq"),
                len(tickers_in_ack), len(confirmed_now),
            )

    def _handle_unsub_ack(self, data: dict) -> None:
        """Process an ``unsubscribed`` ack: clear state for affected tickers.

        Accepts ack payloads keyed by ``sids`` / ``sid`` (preferred — matches
        what we send) or ``market_tickers`` / ``market_ticker`` (defensive).
        """
        msg_body = data.get("msg") if isinstance(data.get("msg"), dict) else {}
        sids_in_ack: List[Any] = []
        raw_sids = msg_body.get("sids")
        if isinstance(raw_sids, list):
            sids_in_ack.extend(raw_sids)
        elif msg_body.get("sid") is not None:
            sids_in_ack.append(msg_body["sid"])

        affected: Set[str] = set()
        raw_list = msg_body.get("market_tickers")
        if isinstance(raw_list, list):
            for t in raw_list:
                if isinstance(t, str):
                    affected.add(t)
        single = msg_body.get("market_ticker")
        if isinstance(single, str) and single:
            affected.add(single)

        with self._sub_lock:
            for sid in sids_in_ack:
                tickers_for_sid = self._sid_to_tickers.pop(sid, set())
                affected.update(tickers_for_sid)
            for t in affected:
                self._subscribed.discard(t)
                self._subscribing_in_flight.discard(t)
                old_sid = self._ticker_to_sid.pop(t, None)
                if old_sid is not None:
                    bucket = self._sid_to_tickers.get(old_sid)
                    if bucket:
                        bucket.discard(t)
                        if not bucket:
                            self._sid_to_tickers.pop(old_sid, None)

        if affected:
            with self._lock:
                for t in affected:
                    self._books.pop(t, None)
                    self._tickers.pop(t, None)

        logger.info(
            "WS unsubscribed ack: sids=%d tickers=%d",
            len(sids_in_ack), len(affected),
        )

    def _handle_snapshot(self, data: dict) -> None:
        """Replace the full order book for a market from a snapshot message.

        Kalshi WS v2 snapshot format::

            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "TICKER",
                    "yes_dollars_fp": [["0.6500", "120.00"], ...],
                    "no_dollars_fp":  [["0.3500", "80.00"], ...],
                }
            }

        ``yes_dollars_fp`` = YES bid levels [price_decimal_str, size_dollars_str].
        ``no_dollars_fp``  = NO bid levels  (equivalent to YES asks at 1 - price).
        """
        msg = data.get("msg") or {}
        market_id = msg.get("market_ticker", "")
        if not market_id:
            logger.warning("WS snapshot missing market_ticker: %s", list(data.keys()))
            return

        yes_bids = self._parse_levels(msg.get("yes_dollars_fp") or [])
        yes_bids.sort(key=lambda lv: -lv.price)

        # NO bids at decimal price p => YES asks at (1 - p)
        no_bids_raw = msg.get("no_dollars_fp") or []
        yes_asks = [
            PriceLevel(price=1.0 - float(lvl[0]), size=float(lvl[1]))
            for lvl in no_bids_raw
            if len(lvl) >= 2
        ]
        yes_asks.sort(key=lambda lv: lv.price)

        book = OrderBook(
            market_id=market_id,
            platform="kalshi",
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            timestamp=datetime.now(timezone.utc),
        )
        with self._lock:
            self._books[market_id] = _BookEntry(book=book)

        logger.info(
            "Received orderbook_snapshot for %s (%d bids, %d asks)",
            market_id, len(yes_bids), len(yes_asks),
        )

    def _handle_delta(self, data: dict) -> None:
        """Apply an incremental update to an existing order book.

        Kalshi WS v2 delta format::

            {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "TICKER",
                    "price_dollars": "0.6500",   # decimal in [0,1]
                    "delta_fp": "-115.00",        # signed dollar amount
                    "side": "yes" | "no",
                }
            }

        A delta with resulting size <= 0 means the level should be removed.
        """
        msg = data.get("msg") or {}
        market_id = msg.get("market_ticker", "")
        if not market_id:
            logger.warning("WS delta missing market_ticker: %s", list(data.keys()))
            return

        price_str = msg.get("price_dollars")
        delta_str = msg.get("delta_fp")
        side = msg.get("side", "")

        if price_str is None or delta_str is None:
            logger.debug("WS delta missing price/delta for %s: %s", market_id, msg)
            return

        price_dec = float(price_str)   # already decimal in [0,1]
        delta_size = float(delta_str)  # signed dollar amount

        with self._lock:
            entry = self._books.get(market_id)
            if entry is None:
                # No snapshot yet; cannot apply delta — skip silently.
                return

            book = entry.book
            now = datetime.now(timezone.utc)

            if side == "yes":
                # Delta on the YES side affects bids.
                book.yes_bids = self._apply_delta_to_levels(
                    book.yes_bids, price_dec, delta_size, descending=True
                )
            elif side == "no":
                # NO bid delta at decimal price p => YES ask at (1 - p).
                ask_price_dec = 1.0 - price_dec
                book.yes_asks = self._apply_delta_to_levels(
                    book.yes_asks, ask_price_dec, delta_size, descending=False
                )

            book.timestamp = now
            entry.last_updated = time.time()

    def _handle_ticker(self, data: dict) -> None:
        """Update the ticker cache from a ticker channel message.

        Kalshi ticker format::

            {
                "type": "ticker",
                "market_ticker": "TICKER",
                "yes_bid": 65,   # cents (dollars field name varies)
                "yes_ask": 67,
                ...
            }
        """
        market_id = data.get("market_ticker", "")
        if not market_id:
            return

        # The API uses cents; normalise to [0-1].
        yes_bid_raw = data.get("yes_bid")
        yes_ask_raw = data.get("yes_ask")
        yes_bid_val = float(yes_bid_raw) / 100.0 if yes_bid_raw is not None else None
        yes_ask_val = float(yes_ask_raw) / 100.0 if yes_ask_raw is not None else None

        if yes_bid_val is not None and yes_ask_val is not None:
            mid = (yes_bid_val + yes_ask_val) / 2.0
            if not _validate_kalshi_mid(mid, market_id, "ws_ticker"):
                return

        snap = TickerSnapshot(
            market_id=market_id,
            yes_bid=yes_bid_val,
            yes_ask=yes_ask_val,
            last_updated=time.time(),
        )
        with self._lock:
            self._tickers[market_id] = snap

    # ── Level helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_levels(raw: list) -> List[PriceLevel]:
        """Parse ``[[price_decimal_str, size_dollars_str], ...]`` into PriceLevel list."""
        levels: List[PriceLevel] = []
        for entry in raw:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            try:
                levels.append(
                    PriceLevel(price=float(entry[0]), size=float(entry[1]))
                )
            except (TypeError, ValueError):
                continue
        return levels

    @staticmethod
    def _apply_delta_to_levels(
        levels: List[PriceLevel],
        price: float,
        delta: float,
        descending: bool,
    ) -> List[PriceLevel]:
        """Apply a signed size delta to a list of PriceLevels.

        If the resulting size is <= 0 the level is removed.
        If the price doesn't exist yet a new level is inserted.
        Returns a freshly sorted list.
        """
        found = False
        new_levels: List[PriceLevel] = []
        for lv in levels:
            if abs(lv.price - price) < 1e-9:
                found = True
                new_size = lv.size + delta
                if new_size > 0:
                    new_levels.append(PriceLevel(price=lv.price, size=new_size))
                # else: level removed (size <= 0)
            else:
                new_levels.append(lv)

        if not found and delta > 0:
            new_levels.append(PriceLevel(price=price, size=delta))

        new_levels.sort(key=lambda lv: lv.price, reverse=descending)
        return new_levels
