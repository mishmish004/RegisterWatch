"""The server's side of a connection, before the app sees a request (plan.md P10.3).

Uvicorn closes a keep-alive connection that sits idle between requests, and
times nothing else. A client that connects and sends nothing, or sends its
request a byte at a time (slowloris), keeps its connection for as long as it
likes, and every open connection counts toward `--limit-concurrency`, past
which everyone else gets a 503. Bytes that keep coming after the answer (a
body the route never read) restart the idle clock with every one. This is
uvicorn's own httptools protocol with three bounds added:

  idle     a connection that has not begun a request is closed after the
           keep-alive timeout, counted from when it was made as well as from
           the end of each answer (uvicorn counts only the latter)
  head     a request line and headers over MAX_HEAD_BYTES: 431
  request  a request must have arrived whole, body included, REQUEST_TIMEOUT_S
           after its first byte. Still sending its head: 408. Still sending a
           body the route is waiting for: 408, and the route's own answer is
           dropped. Still sending a body after the route answered: the
           connection is closed

These answers come from the server, as uvicorn's own 400 and 503 do: plain
text with `Connection: close`, not problem documents, because no route saw the
request. In production the platform's proxy meets a slow client first; these
bounds hold for whatever reaches the server anyway.

Closing a connection while the client is still sending (a 408, a 431, a 413
for a body over the limit) makes the kernel answer the unread bytes with a
reset, and a client still writing its request may then never read the answer.
So such a close lingers (RFC 9112, 9.6): the answer goes out, then the end of
the server's side (FIN), and what the client still sends is read and thrown
away for at most LINGER_S before the connection closes.

`registerwatch serve` runs uvicorn with this protocol (`http=PROTOCOL`).
"""

from __future__ import annotations

import asyncio
import http
import logging
from typing import Any

from uvicorn.protocols.http.httptools_impl import HttpToolsProtocol

log = logging.getLogger(__name__)

PROTOCOL = "registerwatch.http.server:BoundedHttpToolsProtocol"
REQUEST_TIMEOUT_S = 10.0
MAX_HEAD_BYTES = 64 * 1024
LINGER_S = 2.0


class BoundedHttpToolsProtocol(HttpToolsProtocol):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.request_timer: asyncio.TimerHandle | None = None
        # Bytes of the request head received so far; None when no head is arriving.
        self.head_bytes: int | None = None
        self.lingering = False

    # --- the connection ---------------------------------------------------------------

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        # Until its first request a connection is as idle as one between requests.
        self.timeout_keep_alive_task = self.loop.call_later(self.timeout_keep_alive, self.timeout_keep_alive_handler)

    def connection_lost(self, exc: Exception | None) -> None:
        self._stop_clock()
        super().connection_lost(exc)

    def data_received(self, data: bytes) -> None:
        if self.lingering:
            return  # answered and closing: what the client still sends goes nowhere
        super().data_received(data)
        if self.head_bytes is not None and not self.transport.is_closing():
            self.head_bytes += len(data)
            if self.head_bytes > MAX_HEAD_BYTES:
                self._refuse(431, f"request head over {MAX_HEAD_BYTES} bytes")

    # --- the parser's events ------------------------------------------------------------

    def on_message_begin(self) -> None:
        super().on_message_begin()
        self.head_bytes = 0
        self._stop_clock()
        self.request_timer = self.loop.call_later(REQUEST_TIMEOUT_S, self._too_slow)

    def on_headers_complete(self) -> None:
        self.head_bytes = None
        before = self.cycle
        super().on_headers_complete()
        if self.parser.should_upgrade() and self._should_upgrade():
            self._stop_clock()  # a websocket now; this protocol is done with the connection
        elif self.cycle is not before:
            self.cycle.transport = _LingeringTransport(self, self.cycle)  # type: ignore[assignment]

    def on_message_complete(self) -> None:
        self._stop_clock()
        super().on_message_complete()

    def on_response_complete(self) -> None:
        if self.lingering:  # no keep-alive, and no pipelined request: the connection is closing
            self.server_state.total_requests += 1
            return
        super().on_response_complete()

    # --- the bounds ---------------------------------------------------------------------

    def _stop_clock(self) -> None:
        if self.request_timer is not None:
            self.request_timer.cancel()
            self.request_timer = None

    def _too_slow(self) -> None:
        self.request_timer = None
        if self.transport.is_closing():
            return
        cycle = self.cycle
        if self.head_bytes is not None:  # the head itself is late
            if cycle is not None and not cycle.response_complete:
                # The answer to the request before it (pipelined) is still going
                # out: let it finish, then close instead of reading on.
                cycle.keep_alive = False
                return
            self._refuse(408, f"request head incomplete after {REQUEST_TIMEOUT_S:g} s")
        elif cycle is None or cycle.response_complete:
            # Answered without the rest of the body; stop listening for it.
            log.info("%s - closed: body still arriving %g s after the answer", self._peer(), REQUEST_TIMEOUT_S)
            self.linger_then_close()
        elif not cycle.response_started:
            # The route is waiting for a body that is not coming fast enough.
            # Whatever it answers later goes nowhere.
            cycle.disconnected = True
            cycle.message_event.set()
            self._refuse(408, f"request body incomplete after {REQUEST_TIMEOUT_S:g} s")
        else:
            cycle.keep_alive = False  # mid-answer: finish it, then close

    def _refuse(self, status: int, why: str) -> None:
        self._stop_clock()
        self.head_bytes = None
        phrase = http.HTTPStatus(status).phrase.encode()
        head = [b"HTTP/1.1 %d %s\r\n" % (status, phrase)]
        head += [name + b": " + value + b"\r\n" for name, value in self.server_state.default_headers]
        head += [b"content-type: text/plain; charset=utf-8\r\n",
                 b"content-length: %d\r\n" % len(phrase), b"connection: close\r\n\r\n", phrase]
        self.transport.write(b"".join(head))
        self.linger_then_close()
        log.info("%s - %d: %s", self._peer(), status, why)

    def linger_then_close(self) -> None:
        """Close without a reset: stop writing (FIN), discard what the client is
        still sending, and close when it does, or after LINGER_S."""
        if self.lingering or self.transport.is_closing():
            return
        self.lingering = True
        self._stop_clock()
        self._unset_keepalive_if_required()
        self.flow.resume_reading()
        if self.transport.can_write_eof():
            self.transport.write_eof()
        self.loop.call_later(LINGER_S, self.transport.close)

    def _peer(self) -> str:
        return "%s:%d" % self.client if self.client else "-"


class _LingeringTransport:
    """The connection as one request's cycle sees it. When the cycle closes it
    (a `Connection: close` answer) while that request's body is still arriving,
    the close lingers instead of resetting the connection under the client."""

    def __init__(self, protocol: BoundedHttpToolsProtocol, cycle: Any) -> None:
        self._protocol, self._cycle = protocol, cycle

    def close(self) -> None:
        if self._cycle.more_body and not self._cycle.disconnected:
            self._protocol.linger_then_close()
        else:
            self._protocol.transport.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._protocol.transport, name)
