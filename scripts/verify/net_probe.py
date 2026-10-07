"""Helpers for scripts/verify/net.sh (plan.md Phase 10). net.sh runs this inside
the image under test, like os_probe.py, so the host needs Docker and python3.
Standard library only, besides the package itself for `fixtures`.

  net_probe.py fixtures DIR
      Every register gets a complete snapshot holding its real fixture rows
      (tests/fixtures/registers, mounted at DIR), unless it already has one.
      Needs DATABASE_URL.

  net_probe.py slowloris URL [--clients 20] [--every 1] [--give-up 60]
      CLIENTS connections each send a request head one byte every EVERY
      seconds, while /livez is asked on fresh connections every 0.1 s. One JSON
      line: when the server closed each slow connection and what it answered,
      and /livez's latencies meanwhile.

  net_probe.py idle URL --sleep 60 [--sleep 80 ...]
      One connection per SLEEP: a request, SLEEP seconds idle, then a second
      request on the same socket. One JSON line per connection.

  net_probe.py oversize URL --bytes 2097152 [--token T]
      A POST declaring BYTES of JSON body, sent as fast as the server takes it:
      the answer, how long it took, how much of the body went out, and whether
      and when the server closed the connection.

  net_probe.py poll URL --every 0.5 --seconds 60
      GET URL every EVERY seconds; one JSON line with the statuses and the
      slowest answer.
"""

from __future__ import annotations

import argparse
import http.client
import json
import pathlib
import socket
import sys
import threading
import time
from collections import Counter
from urllib.parse import urlsplit


# --- fixtures ------------------------------------------------------------------------

def fixtures(root: pathlib.Path) -> None:
    import gzip
    from datetime import datetime, timezone

    from registerwatch.db.engine import tx
    from registerwatch.db.repos import observations
    from registerwatch.db.repos import snapshots as repo
    from registerwatch.ingest.engine import _shape, row_hashes
    from registerwatch.registers import all_registers
    from registerwatch.registers.base import Bundle

    loaded, rows = [], 0
    with tx() as c:
        for r in all_registers():
            src = repo.upsert_source(c, r.slug, r.name, r.country, r.config())
            if c.execute("SELECT 1 FROM raw_snapshots WHERE source_id = %s AND complete", (src["id"],)).fetchone():
                continue
            bundle = Bundle({p.name[:-3]: gzip.decompress(p.read_bytes())
                             for p in (root / r.slug).glob("*.gz") if not p.name.startswith("_")})
            parsed = {t.name: _shape(t, t.parse(bundle)) for t in r.tables}
            sid = repo.insert_snapshot(
                c, source_id=src["id"], run_started_at=datetime.now(timezone.utc), raw_hash=b"net.sh",
                blob_ref="net.sh", http_status=200, pages_expected=1, pages_ok=1, fetch_log=[], complete=True,
                incomplete_reason=None, record_count=sum(map(len, parsed.values())), canonical_hash=None,
                parsed=True)
            observations.apply(c, r, sid, {t: row_hashes(v) for t, v in parsed.items()})
            loaded.append(r.slug)
            rows += sum(map(len, parsed.values()))
    print(json.dumps({"registers_loaded": len(loaded), "rows": rows}))


# --- connections ---------------------------------------------------------------------

def _connect(url: str, timeout: float) -> tuple[socket.socket, str, str]:
    parts = urlsplit(url)
    sock = socket.create_connection((parts.hostname, parts.port or 80), timeout=timeout)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return sock, parts.hostname or "", path


def _read_answer(sock: socket.socket) -> tuple[str | None, bytes]:
    """The status line of whatever the server sent before closing, and all of it."""
    data = b""
    try:
        while chunk := sock.recv(65536):
            data += chunk
    except OSError:
        pass
    line = data.split(b"\r\n", 1)[0].decode("latin-1") if data else None
    return line, data


def _livez_ms(url: str) -> float | None:
    parts = urlsplit(url)
    start = time.perf_counter()
    try:
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=5)
        conn.request("GET", "/livez", headers={"Connection": "close"})
        ok = conn.getresponse().status == 200
        conn.close()
    except OSError:
        return None
    return round((time.perf_counter() - start) * 1000, 1) if ok else None


def slowloris(url: str, clients: int, every: float, give_up: float) -> int:
    head = f"GET {urlsplit(url).path or '/'} HTTP/1.1\r\nHost: x\r\nUser-Agent: net.sh slowloris\r\n" + \
           "".join(f"X-Slow-{i}: {'a' * 20}\r\n" for i in range(40)) + "\r\n"
    results: list[dict] = [{} for _ in range(clients)]

    def slow(i: int) -> None:
        start = time.perf_counter()
        sent = 0
        try:
            sock, _, _ = _connect(url, timeout=give_up + 5)
        except OSError as exc:
            results[i] = {"error": f"{type(exc).__name__}: {exc}"}
            return
        closed_by = "client"
        try:
            for ch in head.encode():
                if time.perf_counter() - start > give_up:
                    break
                try:
                    sock.send(bytes([ch]))
                except OSError:
                    closed_by = "server"
                    break
                sent += 1
                # Has the server answered or closed?
                sock.settimeout(every)
                try:
                    sock.recv(65536, socket.MSG_PEEK)
                except TimeoutError:
                    continue
                except OSError:
                    closed_by = "server"
                    break
                closed_by = "server"
                break
            sock.settimeout(2)
            line, _ = _read_answer(sock)
        finally:
            sock.close()
        results[i] = {"closed_by": closed_by, "after_s": round(time.perf_counter() - start, 2),
                      "bytes_sent": sent, "answer": line}

    threads = [threading.Thread(target=slow, args=(i,)) for i in range(clients)]
    for t in threads:
        t.start()
    latencies: list[float | None] = []
    time.sleep(0.5)
    while any(t.is_alive() for t in threads):
        latencies.append(_livez_ms(url))
        time.sleep(0.1)
    for t in threads:
        t.join()
    ok = [x for x in latencies if x is not None]
    print(json.dumps({
        "clients": clients, "every_s": every, "head_bytes": len(head),
        "closed_by": dict(Counter(r.get("closed_by", "error") for r in results)),
        "answers": dict(Counter(str(r.get("answer")) for r in results)),
        "after_s_min": min((r["after_s"] for r in results if "after_s" in r), default=None),
        "after_s_max": max((r["after_s"] for r in results if "after_s" in r), default=None),
        "bytes_sent_max": max((r.get("bytes_sent", 0) for r in results), default=0),
        "livez": {"n": len(latencies), "failed": len(latencies) - len(ok),
                  "max_ms": max(ok, default=None), "p50_ms": sorted(ok)[len(ok) // 2] if ok else None},
    }))
    return 0


def idle(url: str, sleeps: list[float]) -> int:
    parts = urlsplit(url)
    path = parts.path or "/livez"
    out: list[dict] = [{} for _ in sleeps]

    def one(i: int, pause: float) -> None:
        conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=10)
        conn.request("GET", path)
        r1 = conn.getresponse()
        r1.read()
        local = conn.sock.getsockname()
        time.sleep(pause)
        try:
            # http.client reconnects on its own when the socket is closed:
            # compare the local port to tell reuse from a new connection.
            conn.auto_open = 0
            conn.request("GET", path)
            r2 = conn.getresponse()
            r2.read()
            second: int | str = r2.status
            same = conn.sock is not None and conn.sock.getsockname() == local
        except (http.client.HTTPException, OSError) as exc:
            second, same = f"{type(exc).__name__}", False
        conn.close()
        out[i] = {"idle_s": pause, "first": r1.status, "second": second, "same_connection": same,
                  "keep_alive_header": r1.getheader("connection")}

    threads = [threading.Thread(target=one, args=(i, s)) for i, s in enumerate(sleeps)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for line in out:
        print(json.dumps(line))
    return 0


def oversize(url: str, size: int, token: str | None) -> int:
    sock, host, path = _connect(url, timeout=10)
    head = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {size}\r\n" + (f"Authorization: Bearer {token}\r\n" if token else "") + "\r\n")
    start = time.perf_counter()
    sock.sendall(head.encode())
    sent, answered_at, closed_at, data = 0, None, None, b""
    chunk = b" " * 16384
    sock.setblocking(False)
    while time.perf_counter() - start < 10:
        if answered_at is None or closed_at is None:
            try:
                got = sock.recv(65536)
                if got:
                    data += got
                    answered_at = answered_at or time.perf_counter()
                else:
                    closed_at = time.perf_counter()
                    break
            except BlockingIOError:
                pass
            except OSError:
                closed_at = time.perf_counter()
                break
        if sent < size and answered_at is None:
            try:
                sent += sock.send(chunk[: size - sent])
            except BlockingIOError:
                time.sleep(0.001)
            except OSError:
                closed_at = closed_at or time.perf_counter()
                break
        else:
            time.sleep(0.001)
    sock.close()
    head_end = data.find(b"\r\n\r\n")
    headers = data[:head_end].decode("latin-1").split("\r\n") if head_end >= 0 else []
    body = data[head_end + 4:] if head_end >= 0 else b""
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = body.decode("utf-8", "replace")[:300]
    ms = (lambda t: None if t is None else round((t - start) * 1000, 1))
    print(json.dumps({"declared": size, "body_sent": sent, "status_line": headers[0] if headers else None,
                      "headers": headers[1:], "body": parsed, "answered_ms": ms(answered_at),
                      "closed_ms": ms(closed_at)}))
    return 0


def poll(url: str, every: float, seconds: float) -> int:
    parts = urlsplit(url)
    statuses: Counter[str] = Counter()
    slowest = 0.0
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        start = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=5)
            conn.request("GET", parts.path, headers={"Connection": "close"})
            statuses[str(conn.getresponse().status)] += 1
            conn.close()
        except OSError as exc:
            statuses[type(exc).__name__] += 1
        took = time.perf_counter() - start
        slowest = max(slowest, took)
        time.sleep(max(0.0, every - took))
    print(json.dumps({"url": url, "statuses": dict(statuses), "max_ms": round(slowest * 1000, 1)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="net_probe.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fixtures")
    f.add_argument("root", type=pathlib.Path)
    s = sub.add_parser("slowloris")
    s.add_argument("url")
    s.add_argument("--clients", type=int, default=20)
    s.add_argument("--every", type=float, default=1.0)
    s.add_argument("--give-up", type=float, default=60.0)
    i = sub.add_parser("idle")
    i.add_argument("url")
    i.add_argument("--sleep", type=float, action="append", required=True)
    o = sub.add_parser("oversize")
    o.add_argument("url")
    o.add_argument("--bytes", type=int, default=2 * 1024 * 1024)
    o.add_argument("--token")
    q = sub.add_parser("poll")
    q.add_argument("url")
    q.add_argument("--every", type=float, default=0.5)
    q.add_argument("--seconds", type=float, default=60)
    args = p.parse_args(argv)
    if args.cmd == "fixtures":
        fixtures(args.root)
        return 0
    if args.cmd == "slowloris":
        return slowloris(args.url, args.clients, args.every, args.give_up)
    if args.cmd == "idle":
        return idle(args.url, args.sleep)
    if args.cmd == "oversize":
        return oversize(args.url, args.bytes, args.token)
    return poll(args.url, args.every, args.seconds)


if __name__ == "__main__":
    sys.exit(main())
