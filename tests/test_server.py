"""Phase 10, P10.3: the server's bounds on how a request arrives
(registerwatch.http.server), under a real uvicorn on a socket, with a small app
and the clocks shortened. scripts/verify/net.sh checks the same against the
image with the real clocks (T10.3.b, T10.3.c).
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time

import pytest
import uvicorn

from registerwatch.http import server

TIMEOUT_S = 0.6
KEEP_ALIVE_S = 1
LINGER_S = 0.5


async def app(scope, receive, send):
    """GET answers at once without reading a body; POST /echo reads its whole body first."""
    if scope["type"] != "http":
        return
    if scope["method"] == "POST" and scope["path"] == "/echo":
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                answered.append("disconnect")
                break
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload = b"%d" % len(body)
    else:
        payload = b"ok"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain"), (b"content-length", b"%d" % len(payload))]})
    await send({"type": "http.response.body", "body": payload})
    answered.append(scope["path"])


answered: list[str] = []


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(server, "REQUEST_TIMEOUT_S", TIMEOUT_S)
    monkeypatch.setattr(server, "LINGER_S", LINGER_S)
    answered.clear()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    srv = uvicorn.Server(uvicorn.Config(app, http=server.PROTOCOL, timeout_keep_alive=KEEP_ALIVE_S,
                                        log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield sock.getsockname()
    finally:
        srv.should_exit = True
        thread.join(10)
        sock.close()


@contextlib.contextmanager
def connect(addr):
    s = socket.create_connection(addr, timeout=10)
    try:
        yield s
    finally:
        s.close()


def read_until_closed(s: socket.socket) -> tuple[bytes, float]:
    """Everything the server sends until it closes, and when it closed."""
    data = b""
    while chunk := s.recv(65536):
        data += chunk
    return data, time.monotonic()


def get(addr, path: str = "/") -> float:
    """A complete GET on a fresh connection; its time in seconds."""
    start = time.monotonic()
    with connect(addr) as s:
        s.sendall(b"GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % path.encode())
        data, _ = read_until_closed(s)
    assert data.startswith(b"HTTP/1.1 200"), data[:80]
    return time.monotonic() - start


def test_the_protocol_is_uvicorns_own_otherwise(serve):
    with connect(serve) as s:
        for _ in range(3):  # keep-alive: three requests, one connection
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert s.recv(65536).startswith(b"HTTP/1.1 200")
    with connect(serve) as s:
        s.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
        assert s.recv(65536).endswith(b"\r\n\r\n5")


def test_a_connection_that_never_sends_is_closed_after_the_keep_alive(serve):
    with connect(serve) as s:
        start = time.monotonic()
        data, closed = read_until_closed(s)
    assert data == b"" and KEEP_ALIVE_S - 0.1 <= closed - start < KEEP_ALIVE_S + 1


def test_a_head_sent_a_byte_at_a_time_is_cut_off_with_a_408(serve):
    head = b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: " + b"a" * 100 + b"\r\n\r\n"
    with connect(serve) as s:
        start = time.monotonic()
        sent = 0
        s.settimeout(0.05)
        data = b""
        for byte in head:
            try:
                s.send(bytes([byte]))
                sent += 1
                data = s.recv(65536)
                break  # the server answered (or closed) mid-head
            except TimeoutError:
                continue
            except OSError:
                break
        s.settimeout(5)
        rest, closed = read_until_closed(s)
        data += rest
    assert data.startswith(b"HTTP/1.1 408 Request Timeout\r\n"), data[:80]
    assert b"connection: close" in data.lower() and data.endswith(b"Request Timeout")
    assert TIMEOUT_S - 0.1 <= closed - start < TIMEOUT_S + 1 and sent < len(head)
    assert answered == []  # no request reached the app


def test_other_clients_are_served_while_a_head_trickles(serve):
    with connect(serve) as slow:
        slow.sendall(b"GET / HTTP/1.1\r\n")
        times = [get(serve) for _ in range(5)]
        slow.settimeout(5)
        data, _ = read_until_closed(slow)
    assert max(times) < 0.5 and data.startswith(b"HTTP/1.1 408")


def test_an_oversized_head_is_a_431(serve):
    with connect(serve) as s:
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        try:
            for i in range(100):
                s.sendall(b"X-Big-%d: %s\r\n" % (i, b"a" * 1000))
        except OSError:
            pass  # closed under us: the answer is still in the receive buffer
        data, _ = read_until_closed(s)
    assert data.startswith(b"HTTP/1.1 431 Request Header Fields Too Large\r\n"), data[:80]
    assert answered == []


def test_a_head_under_the_bound_is_fine(serve):
    with connect(serve) as s:
        big = b"".join(b"X-Big-%d: %s\r\n" % (i, b"a" * 1000) for i in range(50))
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n" + big + b"Connection: close\r\n\r\n")
        data, _ = read_until_closed(s)
    assert data.startswith(b"HTTP/1.1 200")


def test_a_body_the_route_waits_for_must_arrive_in_time(serve):
    with connect(serve) as s:
        start = time.monotonic()
        s.sendall(b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nabc")
        data, closed = read_until_closed(s)
    assert data.startswith(b"HTTP/1.1 408") and b"200" not in data, data[:80]  # the route's answer went nowhere
    assert TIMEOUT_S - 0.1 <= closed - start < TIMEOUT_S + LINGER_S + 1
    assert answered[0] == "disconnect"  # and the route heard the client was gone


def test_a_body_still_coming_after_the_answer_does_not_hold_the_connection(serve):
    """Each byte would restart uvicorn's keep-alive clock; the request clock does not restart."""
    with connect(serve) as s:
        start = time.monotonic()
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n")
        assert s.recv(65536).startswith(b"HTTP/1.1 200")
        s.settimeout(0.2)
        closed = None
        while time.monotonic() - start < 5 and closed is None:
            try:
                s.send(b"x")
                if s.recv(65536) == b"":
                    closed = time.monotonic()
            except TimeoutError:
                continue
            except OSError:
                closed = time.monotonic()
    assert closed is not None and closed - start < TIMEOUT_S + 1


def test_a_slow_request_is_one_request_not_a_slow_connection(serve):
    """The clock runs per request: a keep-alive connection that sends a request
    now and then, each one whole, is never cut off."""
    with connect(serve) as s:
        for _ in range(3):
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
            assert s.recv(65536).startswith(b"HTTP/1.1 200")
            time.sleep(TIMEOUT_S * 0.8)


# --- the real app: a body over the limit (plan.md T10.4.c) ---------------------------------

@pytest.fixture
def serve_app(monkeypatch, configure):
    from registerwatch import api

    configure(INGEST_TOKEN="ingest-token-0123456789abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setattr(server, "LINGER_S", LINGER_S)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    srv = uvicorn.Server(uvicorn.Config(api.app, http=server.PROTOCOL, log_level="critical"))
    thread = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield sock.getsockname()
    finally:
        srv.should_exit = True
        thread.join(10)
        sock.close()


def test_a_client_that_sends_a_2_mb_body_regardless_reads_its_413(serve_app):
    """No `Expect: 100-continue`: the whole body goes out before the client reads.
    The server answers at once and lingers, so the client gets the answer, not a reset."""
    body = b"[" + b" " * (2 * 1024 * 1024 - 2) + b"]"
    with connect(serve_app) as s:
        start = time.monotonic()
        s.sendall(b"POST /v1/ingest-runs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\n\r\n" % len(body) + body)
        sent = time.monotonic()
        data, closed = read_until_closed(s)
    head, _, problem = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 413 "), head
    assert b"connection: close" in head.lower() and b"content-type: application/problem+json" in head.lower()
    assert json.loads(problem)["type"].endswith("#content-too-large")
    assert closed - sent < LINGER_S + 1 and sent - start < 5


def test_a_client_that_waits_for_100_continue_never_sends_the_body(serve_app):
    with connect(serve_app) as s:
        s.sendall(b"POST /v1/ingest-runs HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: 2097152\r\nExpect: 100-continue\r\n\r\n")
        data, _ = read_until_closed(s)
    assert data.startswith(b"HTTP/1.1 413") and b"100 Continue" not in data
